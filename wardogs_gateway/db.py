from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .permissions import ROLES, normalize_permission


KEY_PREFIX = "wdg"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    public_id: str
    name: str
    role: str
    key_hash: str
    active: bool
    created_at: str
    expires_at: str | None
    last_used_at: str | None
    permissions: list[str] | None

    @property
    def expired(self) -> bool:
        expires = parse_iso(self.expires_at)
        return bool(expires and expires <= utc_now())


class KeyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    public_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NULL,
                    last_used_at TEXT NULL,
                    permissions_json TEXT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_name ON api_keys(name)")

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ApiKeyRecord:
        raw_permissions = row["permissions_json"]
        permissions = json.loads(raw_permissions) if raw_permissions is not None else None
        return ApiKeyRecord(
            public_id=row["public_id"],
            name=row["name"],
            role=row["role"],
            key_hash=row["key_hash"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            last_used_at=row["last_used_at"],
            permissions=permissions,
        )

    def get(self, public_id: str) -> ApiKeyRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM api_keys WHERE public_id = ?", (public_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def list(self) -> list[ApiKeyRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        return [self._row_to_record(row) for row in rows]

    def create(
        self,
        name: str,
        role: str,
        *,
        permissions: Iterable[str] | None = None,
        expires_days: int | None = None,
    ) -> tuple[ApiKeyRecord, str]:
        name = name.strip()
        if not name:
            raise ValueError("Name darf nicht leer sein.")
        if role not in ROLES:
            raise ValueError(f"Rolle muss eine von {sorted(ROLES)} sein.")
        public_id = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        raw_key = f"{KEY_PREFIX}_{public_id}_{secret}"
        created = utc_now()
        expires = created + timedelta(days=expires_days) if expires_days else None
        normalized = [normalize_permission(item) for item in permissions] if permissions is not None else None
        permissions_json = json.dumps(normalized, ensure_ascii=False) if normalized is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO api_keys(public_id, name, role, key_hash, active, created_at, expires_at, permissions_json)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (public_id, name, role, hash_key(raw_key), iso(created), iso(expires), permissions_json),
            )
        record = self.get(public_id)
        assert record is not None
        return record, raw_key

    def rotate(self, public_id: str) -> tuple[ApiKeyRecord, str]:
        record = self.get(public_id)
        if not record:
            raise KeyError(public_id)
        secret = secrets.token_urlsafe(32)
        raw_key = f"{KEY_PREFIX}_{public_id}_{secret}"
        with self._connect() as conn:
            conn.execute(
                "UPDATE api_keys SET key_hash = ?, active = 1 WHERE public_id = ?",
                (hash_key(raw_key), public_id),
            )
        updated = self.get(public_id)
        assert updated is not None
        return updated, raw_key

    def set_active(self, public_id: str, active: bool) -> None:
        with self._connect() as conn:
            cur = conn.execute("UPDATE api_keys SET active = ? WHERE public_id = ?", (1 if active else 0, public_id))
            if cur.rowcount != 1:
                raise KeyError(public_id)

    def delete(self, public_id: str) -> None:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM api_keys WHERE public_id = ?", (public_id,))
            if cur.rowcount != 1:
                raise KeyError(public_id)

    def set_role(self, public_id: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"Rolle muss eine von {sorted(ROLES)} sein.")
        with self._connect() as conn:
            cur = conn.execute("UPDATE api_keys SET role = ? WHERE public_id = ?", (role, public_id))
            if cur.rowcount != 1:
                raise KeyError(public_id)

    def set_permissions(self, public_id: str, permissions: Iterable[str] | None) -> None:
        if permissions is None:
            payload = None
        else:
            normalized = [normalize_permission(item) for item in permissions]
            payload = json.dumps(normalized, ensure_ascii=False)
        with self._connect() as conn:
            cur = conn.execute("UPDATE api_keys SET permissions_json = ? WHERE public_id = ?", (payload, public_id))
            if cur.rowcount != 1:
                raise KeyError(public_id)

    def authenticate(self, raw_key: str) -> ApiKeyRecord | None:
        parts = raw_key.split("_", 2)
        if len(parts) != 3 or parts[0] != KEY_PREFIX or not parts[1]:
            return None
        record = self.get(parts[1])
        if not record or not record.active or record.expired:
            return None
        if not hmac.compare_digest(record.key_hash, hash_key(raw_key)):
            return None
        now = iso(utc_now())
        # last_used_at höchstens einmal pro Minute aktualisieren, damit das
        # 3-Sekunden-GUI-Polling SQLite nicht unnötig beschreibt.
        cutoff = iso(utc_now() - timedelta(minutes=1))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE api_keys SET last_used_at = ?
                WHERE public_id = ? AND (last_used_at IS NULL OR last_used_at < ?)
                """,
                (now, record.public_id, cutoff),
            )
        return record
