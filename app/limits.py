"""Spend guards. Bol stores no student data; these keep only numbers.

  - per address, per minute (in memory): every API call, and separately the expensive ones
  - per address, per day (counters.py, survives restarts): sessions, answers, audio plays, voice set-ups
  - for the whole app, per calendar month (counters.py): Claude dollars, Prisma minutes, Timbre characters

Addresses are hashed before they're counted. If the counter store can't be reached, requests go ahead (and the
problem is logged): an outage of the counters shouldn't take Bol down. The hard caps sit with the providers:
the Claude workspace spend limit and the fixed Gnani credits.
"""
import hashlib
import hmac
import logging
import threading
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from . import counters
from .config import settings

log = logging.getLogger("bol")


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


# ---------- daily limits per address ----------

DAILY = {  # counter -> (limit setting, message)
    "sessions": ("sessions_per_day", "You can start {n} practice sessions a day. Continue one you've started."),
    "answers": ("answers_per_day", "Daily practice limit reached ({n} answers). See you tomorrow!"),
    "speech": ("speech_per_day", "Daily listening limit reached ({n} plays). Read the text instead."),
    "voice_clones": ("voice_clones_per_day", "You can set up your voice {n} times a day. Try again tomorrow."),
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def address_key(client: str) -> str:
    """A one-way hash of the address, so the counter store never holds an IP address."""
    return hmac.new(settings.signing_key, client.encode(), hashlib.sha256).hexdigest()[:20]


def check_daily(client: str, kind: str) -> None:
    """Count one use of `kind` by this address today; refuse it once the day's limit is passed."""
    setting, message = DAILY[kind]
    limit = getattr(settings.limits, setting)
    if not limit:
        return
    try:
        used = counters.store.add(f"day-{_today()}", f"{address_key(client)}:{kind}", 1)
    except Exception as exc:  # counters unreachable: let the request through, say so in the logs
        log.warning("daily counter unavailable: %s", type(exc).__name__)
        return
    if used > limit:
        raise HTTPException(429, message.format(n=limit))


# ---------- monthly budget for the whole app ----------

class Budget:
    """Claude dollars, Prisma minutes and Timbre characters spent this calendar month, across restarts."""

    LIMITS = {"claude_usd": "claude_usd_per_month", "prisma_seconds": "prisma_minutes_per_month",
              "timbre_chars": "timbre_chars_per_month"}

    def add(self, *, claude_usd: float = 0, prisma_seconds: float = 0, timbre_chars: int = 0) -> None:
        for name, amount in (("claude_usd", claude_usd), ("prisma_seconds", prisma_seconds),
                             ("timbre_chars", timbre_chars)):
            if amount:
                try:
                    counters.store.add(f"month-{_month()}", name, float(amount))
                except Exception as exc:
                    log.warning("budget counter unavailable: %s", type(exc).__name__)

    def _over(self, name: str, scale: float = 1) -> bool:
        limit = getattr(settings.limits, self.LIMITS[name])
        if not limit:
            return False
        try:
            return counters.store.get(f"month-{_month()}", name) >= limit * scale
        except Exception as exc:
            log.warning("budget counter unavailable: %s", type(exc).__name__)
            return False

    def claude_paused(self) -> bool:
        return self._over("claude_usd")

    def speech_paused(self) -> bool:
        return self._over("prisma_seconds", 60)  # the limit is in minutes

    def voices_paused(self) -> bool:
        return self._over("timbre_chars")

    def reset(self) -> None:  # tests
        if hasattr(counters.store, "reset"):
            counters.store.reset()


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
