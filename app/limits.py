"""Spend guards: per-student daily limits (usage_daily) and the app's monthly budget (app_budget).

Counters are added with one atomic INSERT ... ON CONFLICT DO UPDATE, so parallel replicas never lose a count.
Claude spend comes from the token counts in each response, not from estimates.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .config import settings
from .db import is_postgres
from .models import AppBudget, UsageDaily, User, utcnow

ANSWER_LEASE = timedelta(seconds=240)  # the ingress timeout; a crashed request frees the slot after this
DAILY = {  # usage_daily column -> (limit setting, message)
    "answers": ("answers_per_day", "Daily practice limit reached ({n} answers). See you tomorrow!"),
    "sessions": ("sessions_per_day", "You can start {n} new practice sessions a day. Continue an existing one."),
    "audio_plays": ("audio_plays_per_day", "Daily listening limit reached ({n} plays). Read the text instead."),
    "voice_clones": ("voice_clones_per_day", "You can set up your voice {n} times a day. Try again tomorrow."),
}


def today():
    return datetime.now(timezone.utc).date()


def this_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _add(db: Session, model, keys: dict, amounts: dict) -> None:
    amounts = {k: v for k, v in amounts.items() if v}
    if not amounts:
        return
    insert = pg_insert if is_postgres() else sqlite_insert
    stmt = insert(model).values(**keys, **amounts).on_conflict_do_update(
        index_elements=list(keys), set_={k: getattr(model, k) + v for k, v in amounts.items()})
    db.execute(stmt)


def check_daily(db: Session, user: User, column: str) -> None:
    """429 if the student already reached today's limit for this action."""
    setting, message = DAILY[column]
    limit = getattr(settings.limits, setting)
    row = db.get(UsageDaily, (user.id, today()))
    if limit and row and getattr(row, column) >= limit:
        raise HTTPException(429, message.format(n=limit))


def record(db: Session, user: User, *, claude_usage: dict | None = None, prisma_seconds: float = 0,
           timbre_chars: int = 0, **counts: int) -> None:
    """Adds what one request used. Caller commits."""
    claude_usage = claude_usage or {}
    _add(db, UsageDaily, {"user_id": user.id, "day": today()},
         {**counts, "claude_input_tokens": claude_usage.get("input_tokens", 0),
          "claude_output_tokens": claude_usage.get("output_tokens", 0), "prisma_seconds": round(prisma_seconds, 2)})
    _add(db, AppBudget, {"month": this_month()},
         {"claude_usd": claude_usage.get("usd") or 0, "prisma_seconds": round(prisma_seconds, 2),
          "timbre_chars": timbre_chars})


def _month(db: Session) -> AppBudget | None:
    return db.get(AppBudget, this_month())


def claude_paused(db: Session) -> bool:
    limit, row = settings.limits.claude_usd_per_month, _month(db)
    return bool(limit and row and row.claude_usd >= limit)


def speech_paused(db: Session) -> bool:
    limit, row = settings.limits.prisma_minutes_per_month, _month(db)
    return bool(limit and row and row.prisma_seconds >= limit * 60)


def voices_paused(db: Session) -> bool:
    limit, row = settings.limits.timbre_chars_per_month, _month(db)
    return bool(limit and row and row.timbre_chars >= limit)


def take_answer_slot(db: Session, user: User) -> bool:
    """One answer at a time per student, across all replicas: a short lease on the users row. Commits."""
    now = utcnow()
    result = db.execute(update(User).where(User.id == user.id, or_(User.busy_until.is_(None), User.busy_until < now))
                        .values(busy_until=now + ANSWER_LEASE))
    db.commit()
    return result.rowcount == 1


def free_answer_slot(db: Session, user: User) -> None:
    db.rollback()
    db.execute(update(User).where(User.id == user.id).values(busy_until=None))
    db.commit()


# ---------- burst limits (per minute, in this replica's memory) ----------

class MinuteCounter:
    """Counts hits per key in the current clock minute. Memory is cleared every minute, so it stays small.

    Fixed windows allow up to 2x the limit across a minute boundary; that's fine for stopping floods. These are a
    first line of defence; the daily limits in the database are the exact ones.
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
per_student = MinuteCounter()


def check_burst(user: User) -> None:
    """Expensive calls (Claude, Prisma, Timbre, voice cloning) per student per minute."""
    wait = per_student.hit(user.id, settings.limits.heavy_per_minute)
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
