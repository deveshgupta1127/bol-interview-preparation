import os
import sys
import tempfile
from pathlib import Path

# Tests never call paid APIs: empty keys switch on the stand-ins, and the database is a temp file.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["ANTHROPIC_AUTH_TOKEN"] = ""
os.environ["VACHANA_API_KEY"] = ""
# TEST_DATABASE_URL runs the same tests on PostgreSQL (use an empty, throwaway database).
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or "sqlite:///" + str(Path(tempfile.mkdtemp()) / "test.db")
# Local mode (no sign-in) and no daily limits, unless a test switches them on.
for name in ("AUTH_ISSUER", "TTS_CACHE_URL", "CORS_ORIGINS", "APPLICATIONINSIGHTS_CONNECTION_STRING"):
    os.environ[name] = ""
for name in ("LIMIT_ANSWERS_PER_DAY", "LIMIT_SESSIONS_PER_DAY", "LIMIT_AUDIO_PLAYS_PER_DAY", "LIMIT_HEAVY_PER_MINUTE",
             "LIMIT_REQUESTS_PER_MINUTE_PER_IP",
             "LIMIT_VOICE_CLONES_PER_DAY"):
    os.environ[name] = "100000"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"
