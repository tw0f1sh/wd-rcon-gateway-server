from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlencode


@dataclass(slots=True)
class CacheEntry:
    path: str
    query: tuple[tuple[str, str], ...]
    status_code: int
    headers: dict[str, str]
    content: bytes
    updated_monotonic: float
    updated_unix: float
    source: str = "upstream"

    @property
    def key(self) -> str:
        return cache_key(self.path, self.query)


RefreshCallable = Callable[[str, tuple[tuple[str, str], ...], str], Awaitable[CacheEntry | None]]


def canonical_query(items: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(((str(k), str(v)) for k, v in items), key=lambda item: (item[0], item[1])))


def cache_key(path: str, query: tuple[tuple[str, str], ...]) -> str:
    if not query:
        return path
    return f"{path}?{urlencode(query, doseq=True)}"


class GetCache:
    def __init__(self, path: Path, refresh_interval: float, persist_interval: float, max_dynamic_entries: int) -> None:
        self.path = path
        self.refresh_interval = refresh_interval
        self.persist_interval = persist_interval
        self.max_dynamic_entries = max_dynamic_entries
        self._entries: dict[str, CacheEntry] = {}
        self._tracked: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {}
        self._preloaded: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()
        self._refresh_task: asyncio.Task[None] | None = None
        self._persist_task: asyncio.Task[None] | None = None
        self._refresh_callable: RefreshCallable | None = None
        self._last_cycle_unix: float | None = None

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            now_mono = time.monotonic()
            for raw in payload.get("entries", []):
                path = str(raw["path"])
                query = canonical_query(tuple(tuple(item) for item in raw.get("query", [])))
                entry = CacheEntry(
                    path=path,
                    query=query,
                    status_code=int(raw["status_code"]),
                    headers={str(k): str(v) for k, v in raw.get("headers", {}).items()},
                    content=base64.b64decode(raw.get("content_b64", "")),
                    updated_monotonic=now_mono,
                    updated_unix=float(raw.get("updated_unix", time.time())),
                    source="disk",
                )
                self._entries[entry.key] = entry
                self._tracked[entry.key] = (path, query)
        except Exception:
            # A broken cache file must never stop the gateway from starting.
            return

    def register(self, path: str, query: tuple[tuple[str, str], ...] = (), *, preload: bool = False) -> bool:
        query = canonical_query(query)
        key = cache_key(path, query)
        if key in self._tracked:
            if preload:
                self._preloaded.add(key)
            return True
        dynamic_count = len([k for k in self._tracked if k not in self._preloaded])
        if not preload and dynamic_count >= self.max_dynamic_entries:
            return False
        self._tracked[key] = (path, query)
        if preload:
            self._preloaded.add(key)
        return True

    def tracked_count(self) -> int:
        return len(self._tracked)

    def entry_count(self) -> int:
        return len(self._entries)

    def get(self, path: str, query: tuple[tuple[str, str], ...]) -> CacheEntry | None:
        return self._entries.get(cache_key(path, canonical_query(query)))

    def put(self, entry: CacheEntry) -> None:
        self._entries[entry.key] = entry
        self._tracked.setdefault(entry.key, (entry.path, entry.query))

    def lock_for(self, path: str, query: tuple[tuple[str, str], ...]) -> asyncio.Lock:
        key = cache_key(path, canonical_query(query))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def start(self, refresh_callable: RefreshCallable) -> None:
        self._refresh_callable = refresh_callable
        self._stop.clear()
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="wardogs-cache-refresh")
        self._persist_task = asyncio.create_task(self._persist_loop(), name="wardogs-cache-persist")

    async def stop(self) -> None:
        self._stop.set()
        tasks = [task for task in (self._refresh_task, self._persist_task) if task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self.persist)

    async def refresh_once(self) -> None:
        if self._refresh_callable is None:
            return
        items = list(self._tracked.values())
        if not items:
            return
        await asyncio.gather(*(self._refresh_one(path, query, "background") for path, query in items), return_exceptions=True)
        self._last_cycle_unix = time.time()

    async def _refresh_one(self, path: str, query: tuple[tuple[str, str], ...], reason: str) -> None:
        if self._refresh_callable is None:
            return
        lock = self.lock_for(path, query)
        async with lock:
            entry = await self._refresh_callable(path, query, reason)
            if entry is not None:
                self.put(entry)

    async def _refresh_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_run = loop.time() + self.refresh_interval
        while not self._stop.is_set():
            await asyncio.sleep(max(0.0, next_run - loop.time()))
            if self._stop.is_set():
                break
            await self.refresh_once()
            next_run += self.refresh_interval

    async def _persist_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.persist_interval)
            await asyncio.to_thread(self.persist)

    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "entries": [
                {
                    "path": entry.path,
                    "query": list(entry.query),
                    "status_code": entry.status_code,
                    "headers": entry.headers,
                    "content_b64": base64.b64encode(entry.content).decode("ascii"),
                    "updated_unix": entry.updated_unix,
                }
                for entry in self._entries.values()
            ],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    def summary(self) -> dict[str, object]:
        ages = [max(0.0, time.time() - entry.updated_unix) for entry in self._entries.values()]
        return {
            "tracked": self.tracked_count(),
            "entries": self.entry_count(),
            "oldestAgeSeconds": round(max(ages), 3) if ages else None,
            "lastCycleAt": self._last_cycle_unix,
        }
