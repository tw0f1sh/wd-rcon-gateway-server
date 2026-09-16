from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import Settings, load_settings, master_token
from .db import ApiKeyRecord, KeyStore
from .permissions import filter_capability_routes, is_allowed
from .rate_limit import SlidingWindowRateLimiter


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
SENSITIVE_INBOUND = {"authorization", "cookie", "set-cookie"}


def setup_logging(settings: Settings) -> logging.Logger:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wardogs_gateway")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        file_handler = RotatingFileHandler(settings.log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)
    return logger


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _safe_path(path: str) -> bool:
    return not any(segment in {".", ".."} for segment in path.replace("\\", "/").split("/"))


def _forward_headers(request: Request, upstream_token: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP or lower in SENSITIVE_INBOUND or lower.startswith("x-forwarded-"):
            continue
        headers[key] = value
    headers["Authorization"] = f"Bearer {upstream_token}"
    headers["X-Wardogs-Gateway"] = "1"
    return headers


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP or lower in {"server", "date", "set-cookie", "content-encoding"}:
            continue
        result[key] = value
    return result


def _json_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _filter_capabilities(content: bytes, principal: ApiKeyRecord) -> bytes:
    try:
        payload: Any = json.loads(content)
    except (ValueError, TypeError):
        return content
    if isinstance(payload, dict) and isinstance(payload.get("routes"), list):
        payload["routes"] = filter_capability_routes(payload["routes"], principal.role, principal.permissions)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return content


def create_app(config_path: str | Path | None = None) -> FastAPI:
    path = config_path or os.getenv("WARDOGS_GATEWAY_CONFIG", "gateway.toml")
    settings = load_settings(path)
    upstream_token = master_token()
    store = KeyStore(settings.database_path)
    limiter = SlidingWindowRateLimiter(
        settings.global_requests_per_minute,
        settings.per_key_requests_per_minute,
    )
    log = setup_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout),
            follow_redirects=False,
        )
        try:
            yield
        finally:
            await app.state.http.aclose()

    app = FastAPI(
        title="Wardogs RCON Gateway",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store

    @app.get("/_gateway/health")
    async def gateway_health() -> dict[str, object]:
        # Keine RCON-Daten und kein Secret; nur Prozesszustand für nginx/systemd Monitoring.
        return {"status": "ok", "service": "wardogs-gateway"}

    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy(path: str, request: Request) -> Response:
        if not _safe_path(path):
            return _json_error(400, "invalid_path", "Ungültiger Pfad.")

        raw_key = _bearer_token(request)
        if not raw_key:
            return _json_error(401, "missing_api_key", "Bearer API-Key fehlt.")
        principal = store.authenticate(raw_key)
        if not principal:
            return _json_error(401, "invalid_api_key", "API-Key ist ungültig, deaktiviert oder abgelaufen.")

        route_path = "/v1/" + path.lstrip("/")
        method = request.method.upper()
        if not is_allowed(principal.role, principal.permissions, method, route_path):
            log.warning("DENY key=%s name=%r role=%s %s %s", principal.public_id, principal.name, principal.role, method, route_path)
            return _json_error(403, "endpoint_forbidden", "Dieser API-Key darf diesen Endpoint nicht verwenden.")

        allowed, scope = limiter.allow(principal.public_id)
        if not allowed:
            return _json_error(429, "rate_limited", f"Gateway-Rate-Limit erreicht ({scope}).")

        body = await request.body()
        upstream_url = settings.rcon_base_url + "/" + path.lstrip("/")
        try:
            upstream = await request.app.state.http.request(
                method,
                upstream_url,
                params=list(request.query_params.multi_items()),
                content=body if body else None,
                headers=_forward_headers(request, upstream_token),
            )
        except httpx.TimeoutException:
            log.warning("TIMEOUT key=%s %s %s", principal.public_id, method, route_path)
            return _json_error(504, "upstream_timeout", "RCON-Upstream hat nicht rechtzeitig geantwortet.")
        except httpx.HTTPError as exc:
            log.error("UPSTREAM_ERROR key=%s %s %s: %s", principal.public_id, method, route_path, exc)
            return _json_error(502, "upstream_error", "RCON-Upstream ist nicht erreichbar.")

        content = upstream.content
        headers = _response_headers(upstream.headers)
        if method == "GET" and route_path == "/v1/capabilities" and upstream.is_success:
            content = _filter_capabilities(content, principal)
            headers["content-type"] = "application/json; charset=utf-8"
            headers.pop("content-length", None)

        log.info(
            "ALLOW key=%s name=%r role=%s %s %s -> %s",
            principal.public_id,
            principal.name,
            principal.role,
            method,
            route_path,
            upstream.status_code,
        )
        return Response(content=content, status_code=upstream.status_code, headers=headers)

    return app
