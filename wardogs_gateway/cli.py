from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from .config import load_settings
from .db import KeyStore
from .permissions import ADMIN_ROUTES, ROLES


def _store(config: str) -> KeyStore:
    settings = load_settings(config)
    return KeyStore(settings.database_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wardogs RCON Gateway")
    parser.add_argument("--config", default="gateway.toml", help="Pfad zu gateway.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("db-init", help="SQLite-Datenbank anlegen/prüfen")
    sub.add_parser("roles", help="Standard-Berechtigungen anzeigen")
    p = sub.add_parser("key-create", help="API-Key erzeugen")
    p.add_argument("--name", required=True)
    p.add_argument("--role", choices=sorted(ROLES), required=True)
    p.add_argument("--expires-days", type=int, default=None)
    p.add_argument("--allow", action="append", default=None, help="Exakte Custom-Berechtigung, wiederholbar")
    sub.add_parser("key-list", help="API-Keys anzeigen")
    p = sub.add_parser("key-show", help="Einen API-Key-Datensatz anzeigen (ohne Secret)")
    p.add_argument("id")
    p = sub.add_parser("key-revoke", help="API-Key deaktivieren")
    p.add_argument("id")
    p = sub.add_parser("key-enable", help="API-Key aktivieren")
    p.add_argument("id")
    p = sub.add_parser("key-delete", help="API-Key endgültig löschen")
    p.add_argument("id")
    p = sub.add_parser("key-rotate", help="Secret ersetzen; neues Secret wird nur einmal ausgegeben")
    p.add_argument("id")
    p = sub.add_parser("key-set-role", help="Rolle ändern")
    p.add_argument("id")
    p.add_argument("role", choices=sorted(ROLES))
    p = sub.add_parser("key-set-permissions", help="Custom-Endpoint-Allowlist setzen")
    p.add_argument("id")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--allow", action="append", help="z.B. 'GET /v1/status'; wiederholbar")
    group.add_argument("--inherit-role", action="store_true", help="Custom-Liste entfernen und Rollenstandard verwenden")
    p = sub.add_parser("serve", help="Gateway starten")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--reload", action="store_true")
    return parser


def _print_record(record) -> None:
    print(json.dumps({
        "id": record.public_id, "name": record.name, "role": record.role, "active": record.active,
        "expired": record.expired, "createdAt": record.created_at, "expiresAt": record.expires_at,
        "lastUsedAt": record.last_used_at, "permissions": record.permissions,
    }, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "roles":
            print("admin:")
            for route in ADMIN_ROUTES:
                print(f"  {route}")
            print("\nfull_admin:\n  alle Methoden/Pfade unter /v1 (sofern keine Custom-Allowlist gesetzt ist)")
            return 0
        if args.command == "serve":
            settings = load_settings(args.config)
            from .app import create_app
            uvicorn.run(create_app(args.config), host=args.host or settings.listen_host, port=args.port or settings.listen_port, reload=args.reload)
            return 0

        store = _store(args.config)
        if args.command == "db-init":
            print(f"OK: {store.path}")
        elif args.command == "key-create":
            record, raw_key = store.create(args.name, args.role, permissions=args.allow, expires_days=args.expires_days)
            _print_record(record)
            print("\nAPI KEY (wird nicht erneut angezeigt):")
            print(raw_key)
        elif args.command == "key-list":
            records = store.list()
            if not records:
                print("Keine API-Keys vorhanden.")
            for record in records:
                mode = "custom" if record.permissions is not None else "role"
                print(f"{record.public_id} {record.name!r} role={record.role} mode={mode} active={record.active} expires={record.expires_at or '-'} last={record.last_used_at or '-'}")
        elif args.command == "key-show":
            record = store.get(args.id)
            if not record:
                raise KeyError(args.id)
            _print_record(record)
        elif args.command == "key-revoke":
            store.set_active(args.id, False); print("OK: deaktiviert")
        elif args.command == "key-enable":
            store.set_active(args.id, True); print("OK: aktiviert")
        elif args.command == "key-delete":
            store.delete(args.id); print("OK: gelöscht")
        elif args.command == "key-rotate":
            record, raw_key = store.rotate(args.id); _print_record(record); print("\nNEUER API KEY (wird nicht erneut angezeigt):"); print(raw_key)
        elif args.command == "key-set-role":
            store.set_role(args.id, args.role); print("OK: Rolle geändert")
        elif args.command == "key-set-permissions":
            store.set_permissions(args.id, None if args.inherit_role else args.allow); print("OK: Berechtigungen geändert")
        return 0
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr); return 2
    except KeyError as exc:
        print(f"FEHLER: API-Key-ID nicht gefunden: {exc.args[0]}", file=sys.stderr); return 3


if __name__ == "__main__":
    raise SystemExit(main())
