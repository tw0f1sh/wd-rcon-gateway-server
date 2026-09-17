from __future__ import annotations

import re
from functools import lru_cache

ADMIN_ROUTES: tuple[str, ...] = (
    "GET /v1/health",
    "GET /v1/capabilities",
    "GET /v1/status",
    "GET /v1/players",
    "GET /v1/reserved-slots",
    "POST /v1/players/{id}/message",
    "POST /v1/players/{id}/kick",
    "POST /v1/players/{id}/kill",
    "PATCH /v1/players/{id}",
    "POST /v1/bans",
)

ROLES = {"admin", "full_admin"}


def normalize_permission(value: str) -> str:
    parts = value.strip().split(None, 1)
    if len(parts) != 2:
        raise ValueError(f"Ungültige Berechtigung: {value!r}. Erwartet: 'METHOD /v1/path'.")
    method, path = parts[0].upper(), parts[1].strip()
    if method == "ANY":
        method = "*"
    if not path.startswith("/v1/") and path != "/v1":
        raise ValueError(f"Berechtigung muss unter /v1 liegen: {value!r}")
    if ".." in path.split("/"):
        raise ValueError("'..' ist in Berechtigungspfaden nicht erlaubt.")
    return f"{method} {path}"


@lru_cache(maxsize=1024)
def _pattern_regex(pattern_path: str) -> re.Pattern[str]:
    token = "__DOUBLE_STAR__"
    escaped = re.escape(pattern_path.replace("**", token))
    escaped = escaped.replace(re.escape(token), ".*")
    escaped = escaped.replace(r"\*", "[^/]*")
    escaped = re.sub(r"\\\{[^{}]+\\\}", r"[^/]+", escaped)
    return re.compile(rf"^{escaped}$")


def permission_matches(permission: str, method: str, path: str) -> bool:
    normalized = normalize_permission(permission)
    allowed_method, pattern_path = normalized.split(" ", 1)
    if allowed_method != "*" and allowed_method != method.upper():
        return False
    return bool(_pattern_regex(pattern_path).fullmatch(path))


def effective_permissions(role: str, custom_permissions: list[str] | None) -> list[str] | None:
    if custom_permissions is not None:
        return [normalize_permission(item) for item in custom_permissions]
    if role == "admin":
        return list(ADMIN_ROUTES)
    if role == "full_admin":
        return None
    raise ValueError(f"Unbekannte Rolle: {role}")


def is_allowed(role: str, custom_permissions: list[str] | None, method: str, path: str) -> bool:
    if not path.startswith("/v1/") and path != "/v1":
        return False
    permissions = effective_permissions(role, custom_permissions)
    if permissions is None:
        return True
    return any(permission_matches(item, method, path) for item in permissions)


def filter_capability_routes(routes: list[object], role: str, custom_permissions: list[str] | None) -> list[object]:
    filtered: list[object] = []
    for route in routes:
        if not isinstance(route, str):
            continue
        parts = route.strip().split(None, 1)
        if len(parts) != 2:
            continue
        method, path = parts[0].upper(), parts[1]
        if is_allowed(role, custom_permissions, method, path):
            filtered.append(route)
    return filtered
