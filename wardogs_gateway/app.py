from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .cache import CacheEntry, GetCache, canonical_query
from .config import Settings, load_settings, master_token
from .db import ApiKeyRecord, KeyStore
from .logging_utils import event, make_logger
from .permissions import filter_capability_routes, is_allowed
from .rate_limit import SlidingWindowRateLimiter

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "host", "content-length",
}
SENSITIVE_INBOUND = {"authorization", "cookie", "set-cookie"}


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _safe_path(path: str) -> bool:
    return not any(segment in {".", ".."} for segment in path.replace("\\", "/").split("/"))


def _forward_headers(request: Request | None, upstream_token: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    if request is not None:
        for key, value in request.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP or lower in SENSITIVE_INBOUND or lower.startswith("x-forwarded-"):
                continue
            headers[key] = value
    headers["Authorization"] = f"Bearer {upstream_token}"
    headers["X-Wardogs-Gateway"] = "1"
    return headers


def _response_headers(headers: httpx.Headers | dict[str, str]) -> dict[str, str]:
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


def _discover_static_get_routes(content: bytes) -> list[str]:
    try:
        payload: Any = json.loads(content)
    except (ValueError, TypeError):
        return []
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, list):
        return []
    result: list[str] = []
    for item in routes:
        if not isinstance(item, str):
            continue
        parts = item.strip().split(None, 1)
        if len(parts) != 2 or parts[0].upper() != "GET":
            continue
        path = parts[1].split("?", 1)[0]
        if "{" in path or "}" in path:
            continue
        if path.startswith("/v1/") or path == "/v1":
            result.append(path)
    return result


def create_app(config_path: str | Path | None = None) -> FastAPI:
    path = config_path or os.getenv("WARDOGS_GATEWAY_CONFIG", "gateway.toml")
    settings = load_settings(path)
    upstream_token = master_token()
    store = KeyStore(settings.database_path)

    # Existing global limit now protects the real RCON side. Existing per-key limit remains
    # for client requests that actually pass through (POST/PUT/PATCH/DELETE/HEAD/OPTIONS).
    upstream_limiter = SlidingWindowRateLimiter(settings.global_requests_per_minute, 0)
    client_limiter = SlidingWindowRateLimiter(0, settings.per_key_requests_per_minute)
    client_log = make_logger("wardogs_gateway.client", settings.client_log_path, stream=True)
    server_log = make_logger("wardogs_gateway.server", settings.server_log_path, stream=False)

    cache = GetCache(
        settings.cache_path,
        settings.cache_refresh_interval,
        settings.cache_persist_interval,
        settings.cache_max_dynamic_entries,
    )
    if settings.cache_enabled:
        cache.load()
        for preload in settings.cache_preload_paths:
            cache.register(preload, preload=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout), follow_redirects=False)

        async def refresh_get(path: str, query: tuple[tuple[str, str], ...], reason: str) -> CacheEntry | None:
            allowed, scope = upstream_limiter.allow("__upstream__")
            if not allowed:
                event(server_log, logging.WARNING, "upstream_rate_limit_block", method="GET", path=path, query=list(query), scope=scope, reason=reason)
                return None
            upstream_url = settings.rcon_base_url + path.removeprefix("/v1")
            started = time.perf_counter()
            event(server_log, logging.INFO, "rcon_request_out", method="GET", path=path, query=list(query), reason=reason)
            try:
                upstream = await app.state.http.request(
                    "GET", upstream_url, params=list(query), headers=_forward_headers(None, upstream_token)
                )
            except httpx.TimeoutException:
                event(server_log, logging.WARNING, "rcon_timeout", method="GET", path=path, query=list(query), reason=reason, duration_ms=round((time.perf_counter()-started)*1000, 2))
                return None
            except httpx.HTTPError as exc:
                event(server_log, logging.ERROR, "rcon_transport_error", method="GET", path=path, query=list(query), reason=reason, error=str(exc), duration_ms=round((time.perf_counter()-started)*1000, 2))
                return None

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            event(
                server_log, logging.WARNING if upstream.status_code == 429 else logging.INFO,
                "rcon_response_in", method="GET", path=path, query=list(query), reason=reason,
                status=upstream.status_code, duration_ms=duration_ms,
                retry_after=upstream.headers.get("retry-after"),
            )
            old = cache.get(path, query)
            if upstream.status_code == 429 or upstream.status_code >= 500:
                # Keep last known good/usable value instead of replacing it with a transient failure.
                return None if old is not None else CacheEntry(
                    path, query, upstream.status_code, _response_headers(upstream.headers), upstream.content,
                    time.monotonic(), time.time(), "upstream"
                )

            entry = CacheEntry(
                path=path,
                query=query,
                status_code=upstream.status_code,
                headers=_response_headers(upstream.headers),
                content=upstream.content,
                updated_monotonic=time.monotonic(),
                updated_unix=time.time(),
                source="upstream",
            )
            if path == "/v1/capabilities" and upstream.is_success:
                discovered = _discover_static_get_routes(upstream.content)
                auto_registered: list[str] = []
                if settings.cache_auto_register_static_gets:
                    for discovered_path in discovered:
                        if cache.register(discovered_path, preload=True):
                            auto_registered.append(discovered_path)
                projected_rpm = round(cache.tracked_count() * (60.0 / settings.cache_refresh_interval), 1)
                event(
                    server_log,
                    logging.WARNING if settings.global_requests_per_minute and projected_rpm > settings.global_requests_per_minute else logging.INFO,
                    "capabilities_get_routes_discovered",
                    count=len(discovered), routes=discovered, auto_registered=auto_registered,
                    projected_cache_requests_per_minute=projected_rpm,
                    upstream_budget_per_minute=settings.global_requests_per_minute,
                )
            return entry

        app.state.refresh_get = refresh_get
        if settings.cache_enabled:
            await cache.start(refresh_get)
            # Warm immediately so clients normally never see an empty cache after startup.
            await cache.refresh_once()
        try:
            yield
        finally:
            if settings.cache_enabled:
                await cache.stop()
            await app.state.http.aclose()

    app = FastAPI(
        title="Wardogs RCON Gateway",
        version="1.1.0-cache",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.cache = cache

    @app.get("/_gateway/health")
    async def gateway_health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "wardogs-gateway",
            "cacheEnabled": settings.cache_enabled,
            "cache": cache.summary() if settings.cache_enabled else None,
        }

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def proxy(path: str, request: Request) -> Response:
        started = time.perf_counter()
        route_path = "/v1/" + path.lstrip("/")
        method = request.method.upper()
        client_ip = request.client.host if request.client else None
        query = canonical_query(list(request.query_params.multi_items()))

        event(client_log, logging.INFO, "client_request_in", client_ip=client_ip, method=method, path=route_path, query=list(query))

        if not _safe_path(path):
            event(client_log, logging.WARNING, "client_reject_invalid_path", client_ip=client_ip, method=method, path=route_path)
            return _json_error(400, "invalid_path", "Ungültiger Pfad.")

        raw_key = _bearer_token(request)
        if not raw_key:
            event(client_log, logging.WARNING, "client_auth_missing", client_ip=client_ip, method=method, path=route_path)
            return _json_error(401, "missing_api_key", "Bearer API-Key fehlt.")
        principal = store.authenticate(raw_key)
        if not principal:
            event(client_log, logging.WARNING, "client_auth_invalid", client_ip=client_ip, method=method, path=route_path)
            return _json_error(401, "invalid_api_key", "API-Key ist ungültig, deaktiviert oder abgelaufen.")

        common = {"client_ip": client_ip, "key": principal.public_id, "key_name": principal.name, "role": principal.role, "method": method, "path": route_path, "query": list(query)}
        if not is_allowed(principal.role, principal.permissions, method, route_path):
            event(client_log, logging.WARNING, "client_permission_denied", **common)
            return _json_error(403, "endpoint_forbidden", "Dieser API-Key darf diesen Endpoint nicht verwenden.")

        if method == "GET" and settings.cache_enabled:
            registered = cache.register(route_path, query)
            entry = cache.get(route_path, query)
            cache_state = "hit" if entry else "miss"
            if entry is None:
                if not registered:
                    event(client_log, logging.WARNING, "cache_registration_limit", **common, max_dynamic=settings.cache_max_dynamic_entries)
                    return _json_error(503, "cache_capacity", "GET-Cache hat die maximale Anzahl dynamischer Endpoints erreicht.")
                lock = cache.lock_for(route_path, query)
                async with lock:
                    entry = cache.get(route_path, query)
                    if entry is None:
                        event(client_log, logging.INFO, "cache_miss", **common)
                        entry = await request.app.state.refresh_get(route_path, query, "client_miss")
                        if entry is not None:
                            cache.put(entry)
            if entry is None:
                event(client_log, logging.WARNING, "cache_unavailable", **common, duration_ms=round((time.perf_counter()-started)*1000, 2))
                return _json_error(503, "cache_unavailable", "Für diesen GET-Endpoint liegt derzeit kein Cachewert vor.")

            content = entry.content
            headers = dict(entry.headers)
            if route_path == "/v1/capabilities" and 200 <= entry.status_code < 300:
                content = _filter_capabilities(content, principal)
                headers["content-type"] = "application/json; charset=utf-8"
                headers.pop("content-length", None)
            event(
                client_log, logging.INFO, "client_response_out", **common, status=entry.status_code,
                source="cache", cache_state=cache_state, cache_age_ms=round(max(0.0, time.time()-entry.updated_unix)*1000, 2),
                duration_ms=round((time.perf_counter()-started)*1000, 2),
            )
            return Response(content=content, status_code=entry.status_code, headers=headers)

        # Non-GET methods stay passthrough. Cached GETs are deliberately not charged here.
        allowed, scope = client_limiter.allow(principal.public_id)
        if not allowed:
            event(client_log, logging.WARNING, "client_rate_limit", **common, scope=scope)
            return _json_error(429, "client_rate_limited", f"Client-Gateway-Rate-Limit erreicht ({scope}).")

        upstream_allowed, upstream_scope = upstream_limiter.allow("__upstream__")
        if not upstream_allowed:
            event(server_log, logging.WARNING, "upstream_rate_limit_block", key=principal.public_id, method=method, path=route_path, query=list(query), scope=upstream_scope, reason="client_passthrough")
            event(client_log, logging.WARNING, "upstream_rate_limit_to_client", **common, scope=upstream_scope)
            return _json_error(429, "upstream_rate_limited", f"Gateway-RCON-Rate-Limit erreicht ({upstream_scope}).")

        body = await request.body()
        upstream_url = settings.rcon_base_url + "/" + path.lstrip("/")
        upstream_started = time.perf_counter()
        event(server_log, logging.INFO, "rcon_request_out", key=principal.public_id, method=method, path=route_path, query=list(query), reason="client_passthrough")
        try:
            upstream = await request.app.state.http.request(
                method, upstream_url, params=list(request.query_params.multi_items()), content=body if body else None,
                headers=_forward_headers(request, upstream_token),
            )
        except httpx.TimeoutException:
            event(server_log, logging.WARNING, "rcon_timeout", key=principal.public_id, method=method, path=route_path, reason="client_passthrough")
            return _json_error(504, "upstream_timeout", "RCON-Upstream hat nicht rechtzeitig geantwortet.")
        except httpx.HTTPError as exc:
            event(server_log, logging.ERROR, "rcon_transport_error", key=principal.public_id, method=method, path=route_path, reason="client_passthrough", error=str(exc))
            return _json_error(502, "upstream_error", "RCON-Upstream ist nicht erreichbar.")

        event(
            server_log, logging.WARNING if upstream.status_code == 429 else logging.INFO,
            "rcon_response_in", key=principal.public_id, method=method, path=route_path, query=list(query),
            reason="client_passthrough", status=upstream.status_code,
            duration_ms=round((time.perf_counter()-upstream_started)*1000, 2), retry_after=upstream.headers.get("retry-after")
        )
        event(
            client_log, logging.INFO, "client_response_out", **common, status=upstream.status_code,
            source="upstream", duration_ms=round((time.perf_counter()-started)*1000, 2)
        )
        return Response(content=upstream.content, status_code=upstream.status_code, headers=_response_headers(upstream.headers))

    return app
