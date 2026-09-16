from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    config_path: Path
    rcon_base_url: str
    request_timeout: float
    database_path: Path
    log_path: Path
    listen_host: str
    listen_port: int
    global_requests_per_minute: int
    per_key_requests_per_minute: int


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_settings(path: str | Path = "gateway.toml") -> Settings:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Gateway-Konfiguration nicht gefunden: {config_path}. "
            "Kopiere gateway.toml.example nach gateway.toml."
        )

    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    cfg = raw.get("gateway", {})
    base = config_path.parent

    rcon_base_url = os.getenv("WARDOGS_RCON_URL", str(cfg.get("rcon_base_url", ""))).strip().rstrip("/")
    if not rcon_base_url:
        raise ValueError("gateway.rcon_base_url fehlt.")
    if not rcon_base_url.endswith("/v1"):
        raise ValueError("gateway.rcon_base_url muss auf /v1 enden.")

    return Settings(
        config_path=config_path,
        rcon_base_url=rcon_base_url,
        request_timeout=max(1.0, float(cfg.get("request_timeout", 10.0))),
        database_path=_resolve(base, str(cfg.get("database", "data/gateway.db"))),
        log_path=_resolve(base, str(cfg.get("log_file", "logs/gateway.log"))),
        listen_host=str(cfg.get("listen_host", "127.0.0.1")),
        listen_port=int(cfg.get("listen_port", 9012)),
        global_requests_per_minute=max(0, int(cfg.get("global_requests_per_minute", 540))),
        per_key_requests_per_minute=max(0, int(cfg.get("per_key_requests_per_minute", 180))),
    )


def master_token() -> str:
    token = os.getenv("WARDOGS_MASTER_RCON_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "WARDOGS_MASTER_RCON_TOKEN ist nicht gesetzt. "
            "Das Master-RCON-Passwort gehört ausschließlich in die Server-Umgebung/EnvironmentFile."
        )
    return token
