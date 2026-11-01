"""Spend guards, all in this server's memory (Bol stores nothing).

  - per address, per minute: every API call, and separately the expensive ones (Claude, Prisma, Timbre, cloning)
  - for the whole app, per calendar month: Claude dollars, Prisma minutes, Timbre characters (0 = no limit)

Memory resets when the server restarts, so these are a first line of defence. The hard caps sit with the
providers: the Claude workspace spend limit and the fixed Gnani credits.
"""
import threading
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from .config import settings


class MinuteCounter:
    """Counts hits per key in the current clock minute. Memory is cleared every minute, so it stays small.

    Fixed windows allow up to 2x the limit across a minute boundary; that's fine for stopping floods.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._minute = 0
        self._counts: dict[str, int] = {}

    def hit(self, key: str, limit: int) -> int:
        """Count one hit. Returns 0 if allowed, else the seconds until the next minute starts."""
        now = time.time()
        minute = int(now // 60)
        with self._lock:
            if minute != self._minute:
                self._minute, self._counts = minute, {}
            n = self._counts[key] = self._counts.get(key, 0) + 1
        return 0 if not limit or n <= limit else 60 - int(now % 60)

    def reset(self) -> None:
        with self._lock:
            self._counts = {}


per_ip = MinuteCounter()
heavy = MinuteCounter()


def check_heavy(client: str) -> None:
    """Expensive calls (Claude, Prisma, Timbre, voice cloning) per address per minute."""
    wait = heavy.hit(client, settings.limits.heavy_per_minute)
    if wait:
        raise HTTPException(429, f"That's a lot at once. Wait {wait} seconds and try again.",
                            headers={"Retry-After": str(wait)})


def client_ip(headers, peer: str | None) -> str:
    """The caller's address. Only the entries our own proxies appended to X-Forwarded-For are trusted."""
    hops = settings.trusted_proxy_hops
    forwarded = [p.strip() for p in (headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    if hops and forwarded:
        return forwarded[-min(hops, len(forwarded))]
    return peer or "unknown"


# ---------- monthly budget for the whole app ----------

class Budget:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.month = ""
        self.claude_usd = self.prisma_seconds = 0.0
        self.timbre_chars = 0

    def _roll(self) -> None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        if month != self.month:
            self.month, self.claude_usd, self.prisma_seconds, self.timbre_chars = month, 0.0, 0.0, 0

    def add(self, *, claude_usd: float = 0, prisma_seconds: float = 0, timbre_chars: int = 0) -> None:
        with self._lock:
            self._roll()
            self.claude_usd += claude_usd or 0
            self.prisma_seconds += prisma_seconds or 0
            self.timbre_chars += timbre_chars or 0

    def reset(self) -> None:
        with self._lock:
            self.month = ""
            self._roll()

    def claude_paused(self) -> bool:
        with self._lock:
            self._roll()
            limit = settings.limits.claude_usd_per_month
            return bool(limit and self.claude_usd >= limit)

    def speech_paused(self) -> bool:
        with self._lock:
            self._roll()
            limit = settings.limits.prisma_minutes_per_month
            return bool(limit and self.prisma_seconds >= limit * 60)

    def voices_paused(self) -> bool:
        with self._lock:
            self._roll()
            limit = settings.limits.timbre_chars_per_month
            return bool(limit and self.timbre_chars >= limit)


budget = Budget()


# ---------- a retried upload returns the first result instead of being scored (and paid for) twice ----------

class Replies:
    """Recent answers by Idempotency-Key, kept 15 minutes."""

    TTL = 15 * 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        with self._lock:
            item = self._items.get(key)
            return item[1] if item and item[0] > time.time() else None

    def put(self, key: str, reply: dict) -> None:
        now = time.time()
        with self._lock:
            if len(self._items) > 2000:
                self._items = {k: v for k, v in self._items.items() if v[0] > now}
            self._items[key] = (now + self.TTL, reply)


replies = Replies()
