"""Settings read from environment variables (and .env for local runs).

On Azure the same variables come from the container app: secrets as Key Vault references, the rest as plain env.
"""
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


def database_url() -> str:
    url = os.getenv("DATABASE_URL") or f"sqlite:///{ROOT / 'bol.db'}"
    # Azure and most hosts hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the psycopg 3 driver named.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Limits:
    """Spend guards. Per student per day, and for the whole app per calendar month (0 = no limit)."""
    answers_per_day: int = 30
    sessions_per_day: int = 5
    audio_plays_per_day: int = 40
    voice_clones_per_day: int = 3
    # Burst limits, per replica's memory (so up to 3x across 3 replicas; the daily limits above are the exact ones).
    requests_per_minute_per_ip: int = 120  # every /api call, before sign-in is checked
    heavy_per_minute: int = 10  # per student: new sessions, answers, voice set-ups and new (uncached) audio
    claude_usd_per_month: float = 25.0
    prisma_minutes_per_month: float = 0
    timbre_chars_per_month: int = 0


@dataclass(frozen=True)
class Settings:
    database_url: str
    claude_model: str
    claude_enabled: bool
    vachana_api_key: str
    vachana_base_url: str
    web_dir: Path
    serve_web: bool = True  # local runs serve web/ from the API; on Azure, Static Web Apps serves it
    cors_origins: tuple[str, ...] = ()
    # Sign-in with Entra External ID. Empty issuer = local mode: one built-in user, for development only.
    auth_issuer: str = ""
    auth_audience: str = ""
    auth_jwks_url: str = ""
    auth_scope: str = "practice.readwrite"
    limits: Limits = Limits()
    tts_cache_url: str = ""  # https://<account>.blob.core.windows.net; empty = no cache
    tts_cache_container: str = "tts-cache"
    max_answer_seconds: int = 180
    max_typed_chars: int = 3000  # a 3-minute spoken answer is ~2,500 characters
    max_request_mb: int = 10  # bodies larger than this are refused before they are read
    # Proxies in front of the API that append to X-Forwarded-For (Container Apps ingress: 1). Only those entries
    # are trusted for the client address; anything to their left was written by the client and can be faked.
    trusted_proxy_hops: int = 0
    min_answer_seconds: int = 3

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_issuer)


def load_settings() -> Settings:
    return Settings(
        database_url=database_url(),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
        claude_enabled=bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
        vachana_api_key=os.getenv("VACHANA_API_KEY", ""),
        vachana_base_url=os.getenv("VACHANA_BASE_URL", "https://api.vachana.ai").rstrip("/"),
        web_dir=ROOT / "web",
        serve_web=_bool("SERVE_WEB", True),
        cors_origins=tuple(o.strip().rstrip("/") for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()),
        auth_issuer=os.getenv("AUTH_ISSUER", "").strip(),
        auth_audience=os.getenv("AUTH_AUDIENCE", "").strip(),
        auth_jwks_url=os.getenv("AUTH_JWKS_URL", "").strip(),
        auth_scope=os.getenv("AUTH_SCOPE", "practice.readwrite").strip(),
        limits=Limits(
            answers_per_day=_int("LIMIT_ANSWERS_PER_DAY", 30),
            sessions_per_day=_int("LIMIT_SESSIONS_PER_DAY", 5),
            audio_plays_per_day=_int("LIMIT_AUDIO_PLAYS_PER_DAY", 40),
            voice_clones_per_day=_int("LIMIT_VOICE_CLONES_PER_DAY", 3),
            requests_per_minute_per_ip=_int("LIMIT_REQUESTS_PER_MINUTE_PER_IP", 120),
            heavy_per_minute=_int("LIMIT_HEAVY_PER_MINUTE", 10),
            claude_usd_per_month=_float("BUDGET_CLAUDE_USD_PER_MONTH", 25.0),
            prisma_minutes_per_month=_float("BUDGET_PRISMA_MINUTES_PER_MONTH", 0),
            timbre_chars_per_month=_int("BUDGET_TIMBRE_CHARS_PER_MONTH", 0),
        ),
        tts_cache_url=os.getenv("TTS_CACHE_URL", "").strip().rstrip("/"),
        tts_cache_container=os.getenv("TTS_CACHE_CONTAINER", "tts-cache"),
        trusted_proxy_hops=_int("TRUSTED_PROXY_HOPS", 1 if os.getenv("CONTAINER_APP_NAME") else 0),
    )


settings = load_settings()

# Container Apps sets CONTAINER_APP_NAME. Never run there without sign-in: everyone would share one account.
if os.getenv("CONTAINER_APP_NAME") and not settings.auth_enabled:
    raise RuntimeError("AUTH_ISSUER is not set. Bol refuses to start on Azure without sign-in.")
# Nor with SQLite: each replica would keep its own file, and every restart would wipe it.
if os.getenv("CONTAINER_APP_NAME") and not settings.database_url.startswith("postgresql"):
    raise RuntimeError("DATABASE_URL must point at PostgreSQL on Azure.")
