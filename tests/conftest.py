import json
import os
import sys
from pathlib import Path

# Tests never call paid APIs: empty keys switch on the stand-ins. Bol has no database or storage to set up.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["ANTHROPIC_AUTH_TOKEN"] = ""
os.environ["VACHANA_API_KEY"] = ""
os.environ["SIGNING_SECRET"] = "test-signing-secret"
for name in ("CORS_ORIGINS", "APPLICATIONINSIGHTS_CONNECTION_STRING", "COUNTERS_TABLE_URL"):
    os.environ[name] = ""
for name in ("LIMIT_HEAVY_PER_MINUTE", "LIMIT_REQUESTS_PER_MINUTE_PER_IP", "LIMIT_SESSIONS_PER_DAY",
             "LIMIT_ANSWERS_PER_DAY", "LIMIT_SPEECH_PER_DAY", "LIMIT_VOICE_CLONES_PER_DAY"):  # off unless a test sets them
    os.environ[name] = "100000"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"
RESUME = (SAMPLES / "resume_ananya.txt").read_text(encoding="utf-8")
JD = (SAMPLES / "jd_associate_engineer.txt").read_text(encoding="utf-8")


def prepare(client, round_type="technical", **extra):
    """What the browser does to start a session: POST /api/prepare with the resume and JD."""
    r = client.post("/api/prepare", data={"resume_text": RESUME, "jd_text": JD, "round_type": round_type,
                                          "target_role": "Associate Software Engineer", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def context(prep, i=0, kind="main", feedback_language="hi-IN", **overrides):
    """The question and session details the browser sends with each answer."""
    q = prep["questions"][i]
    ctx = {"question": q["text"], "why": q["why"], "kind": kind, "round_type": prep["round_type"],
           "target_role": prep["target_role"], "requirements": prep["profile"]["jd_requirements"],
           "skills": prep["profile"]["skills"], "resume_text": prep["resume_text"],
           "feedback_language": feedback_language, **overrides}
    return json.dumps(ctx)


def score(client, prep, text=None, i=0, files=None, headers=None, **ctx):
    data = {"context": context(prep, i, **ctx)}
    if text is not None:
        data["text"] = text
    return client.post("/api/score", data=data, files=files, headers=headers)
