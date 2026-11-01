"""Settings read from environment variables (and .env for local runs).

On Azure the same variables come from the container app: the API keys as secrets, the rest as plain env.
"""
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

LANGUAGES = {"hi-IN": "Hindi", "en-IN": "English", "ta-IN": "Tamil", "te-IN": "Telugu", "kn-IN": "Kannada",
             "bn-IN": "Bengali", "mr-IN": "Marathi", "gu-IN": "Gujarati", "ml-IN": "Malayalam", "pa-IN": "Punjabi"}
ROUNDS = {"technical": "Technical + project deep-dive", "hr": "HR round", "behavioural": "Behavioural (STAR)",
          "campus": "Campus placement HR"}


def _int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


def _float(name: str, default: float) -> float:
    return float(os.getenv(name) or default)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value in (None, "") else value.strip().lower() in ("1", "true", "yes", "on")


def _signing_key() -> bytes:
    """Signs the texts Bol may read aloud. SIGNING_SECRET if set; otherwise derived from the API keys, so it stays
    the same across restarts without another setting (and is as secret as they are)."""
    explicit = os.getenv("SIGNING_SECRET", "")
    seed = explicit or (os.getenv("ANTHROPIC_API_KEY", "") + "|" + os.getenv("VACHANA_API_KEY", "") + "|bol-speak")
    return hashlib.sha256(seed.encode()).digest()


@dataclass(frozen=True)
class Limits:
    """Spend guards (0 = no limit). Per address per minute (in memory) and per day, and for the whole app per
    calendar month (counters.py). The hard caps are the Claude workspace spend limit and the Gnani credits.
    A college lab can share one address, so the daily limits are generous."""
    requests_per_minute_per_ip: int = 120  # every /api call
    heavy_per_minute: int = 20  # per address: preparing a session, scoring an answer, speaking, voice cloning
    sessions_per_day: int = 10
    answers_per_day: int = 60
    speech_per_day: int = 120
    voice_clones_per_day: int = 5
    claude_usd_per_month: float = 25.0
    prisma_minutes_per_month: float = 0
    timbre_chars_per_month: int = 0


@dataclass(frozen=True)
class Settings:
    claude_model: str
    claude_enabled: bool
    vachana_api_key: str
    vachana_base_url: str
    web_dir: Path
    signing_key: bytes = b""
    serve_web: bool = True  # local runs serve web/ from the API; on Azure, Static Web Apps serves it
    cors_origins: tuple[str, ...] = ()
    limits: Limits = Limits()
    counters_table_url: str = ""  # https://<account>.table.core.windows.net; empty = counters in memory
    counters_table: str = "counters"
    max_answer_seconds: int = 180
    min_answer_seconds: int = 3
    max_typed_chars: int = 3000  # a 3-minute spoken answer is ~2,500 characters
    max_request_mb: int = 10  # bodies larger than this are refused before they are read
    # Proxies in front of the API that append to X-Forwarded-For (Container Apps ingress: 1). Only those entries
    # are trusted for the client address; anything to their left was written by the client and can be faked.
    trusted_proxy_hops: int = 0


def load_settings() -> Settings:
    return Settings(
        claude_model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
        claude_enabled=bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
        vachana_api_key=os.getenv("VACHANA_API_KEY", ""),
        vachana_base_url=os.getenv("VACHANA_BASE_URL", "https://api.vachana.ai").rstrip("/"),
        web_dir=ROOT / "web",
        signing_key=_signing_key(),
        serve_web=_bool("SERVE_WEB", True),
        cors_origins=tuple(o.strip().rstrip("/") for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()),
        limits=Limits(
            requests_per_minute_per_ip=_int("LIMIT_REQUESTS_PER_MINUTE_PER_IP", 120),
            heavy_per_minute=_int("LIMIT_HEAVY_PER_MINUTE", 20),
            sessions_per_day=_int("LIMIT_SESSIONS_PER_DAY", 10),
            answers_per_day=_int("LIMIT_ANSWERS_PER_DAY", 60),
            speech_per_day=_int("LIMIT_SPEECH_PER_DAY", 120),
            voice_clones_per_day=_int("LIMIT_VOICE_CLONES_PER_DAY", 5),
            claude_usd_per_month=_float("BUDGET_CLAUDE_USD_PER_MONTH", 25.0),
            prisma_minutes_per_month=_float("BUDGET_PRISMA_MINUTES_PER_MONTH", 0),
            timbre_chars_per_month=_int("BUDGET_TIMBRE_CHARS_PER_MONTH", 0),
        ),
        trusted_proxy_hops=_int("TRUSTED_PROXY_HOPS", 1 if os.getenv("CONTAINER_APP_NAME") else 0),
        counters_table_url=os.getenv("COUNTERS_TABLE_URL", "").strip().rstrip("/"),
        counters_table=os.getenv("COUNTERS_TABLE", "counters"),
    )


settings = load_settings()
