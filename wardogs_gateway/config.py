from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CACHE_PRELOAD = (
    "/v1/health",
    "/v1/capabilities",
    "/v1/status",
    "/v1/players",
    "/v1/reserved-slots",
)


@dataclass(frozen=True, slots=True)
class Settings:
    config_path: Path
    rcon_base_url: str
    request_timeout: float
    database_path: Path
    client_log_path: Path
    server_log_path: Path
    listen_host: str
    listen_port: int
    global_requests_per_minute: int
    per_key_requests_per_minute: int
    cache_enabled: bool
    cache_refresh_interval: float
    cache_persist_interval: float
    cache_path: Path
    cache_max_dynamic_entries: int
    cache_auto_register_static_gets: bool
    cache_preload_paths: tuple[str, ...]


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _split_log_paths(base: Path, cfg: dict[str, object]) -> tuple[Path, Path]:
    if cfg.get("client_log_file") or cfg.get("server_log_file"):
        return (
            _resolve(base, str(cfg.get("client_log_file", "logs/rcon-client.log"))),
            _resolve(base, str(cfg.get("server_log_file", "logs/rcon-server.log"))),
        )
    legacy = str(cfg.get("log_file", "logs/gateway.log"))
    legacy_path = _resolve(base, legacy)
    return legacy_path.parent / "rcon-client.log", legacy_path.parent / "rcon-server.log"


def load_settings(path: str | Path = "gateway.toml") -> Settings:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Gateway-Konfiguration nicht gefunden: {config_path}. Kopiere gateway.toml.example nach gateway.toml."
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

    client_log_path, server_log_path = _split_log_paths(base, cfg)
    preload = cfg.get("cache_preload_paths", list(DEFAULT_CACHE_PRELOAD))
    if isinstance(preload, str):
        preload = [preload]
    preload_paths = tuple(str(item).strip() for item in preload if str(item).strip())
    for item in preload_paths:
        if not item.startswith("/v1/") and item != "/v1":
            raise ValueError(f"cache_preload_paths muss unter /v1 liegen: {item}")

    return Settings(
        config_path=config_path,
        rcon_base_url=rcon_base_url,
        request_timeout=max(1.0, float(cfg.get("request_timeout", 10.0))),
        database_path=_resolve(base, str(cfg.get("database", "data/gateway.db"))),
        client_log_path=client_log_path,
        server_log_path=server_log_path,
        listen_host=str(cfg.get("listen_host", "127.0.0.1")),
        listen_port=int(cfg.get("listen_port", 9012)),
        global_requests_per_minute=max(0, int(cfg.get("global_requests_per_minute", 540))),
        per_key_requests_per_minute=max(0, int(cfg.get("per_key_requests_per_minute", 180))),
        cache_enabled=bool(cfg.get("cache_enabled", True)),
        cache_refresh_interval=max(0.2, float(cfg.get("cache_refresh_interval", 1.0))),
        cache_persist_interval=max(1.0, float(cfg.get("cache_persist_interval", 5.0))),
        cache_path=_resolve(base, str(cfg.get("cache_file", "data/get-cache.json"))),
        cache_max_dynamic_entries=max(1, int(cfg.get("cache_max_dynamic_entries", 128))),
        cache_auto_register_static_gets=bool(cfg.get("cache_auto_register_static_gets", False)),
        cache_preload_paths=preload_paths,
    )


def master_token() -> str:
    token = os.getenv("WARDOGS_MASTER_RCON_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "WARDOGS_MASTER_RCON_TOKEN ist nicht gesetzt. Das Master-RCON-Passwort gehört ausschließlich in die Server-Umgebung/EnvironmentFile."
        )
    return token
