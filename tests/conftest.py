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


# ---------- a fake Claude (no network, no cost) ----------

class FakeStream:
    """What client.messages.stream(...) returns: the reply's text in small pieces, then the final message."""

    def __init__(self, text, stop_reason, usage):
        self.text, self.stop_reason, self.usage = text, stop_reason, usage

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        for i in range(0, len(self.text), 7):
            yield self.text[i:i + 7]

    def get_final_message(self):
        from types import SimpleNamespace
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=self.text)],
                               stop_reason=self.stop_reason, usage=self.usage)


class FakeMessages:
    """Each call streams the next prepared reply (a pydantic model, sent as its JSON); the last one repeats."""

    def __init__(self, *results, stop_reason="end_turn", usage=None):
        self.results, self.stop_reason, self.usage, self.calls = list(results), stop_reason, usage, []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        text = result if isinstance(result, str) else result.model_dump_json()
        return FakeStream(text, self.stop_reason, self.usage)


def install_fake_claude(monkeypatch, *results, model=None, **kwargs):
    """Route app.claude to a FakeMessages; returns it (its .calls holds every request body)."""
    import dataclasses
    from types import SimpleNamespace
    from app import claude, config
    changes = {"claude_enabled": True, **({"claude_model": model} if model else {})}
    monkeypatch.setattr(claude, "settings", dataclasses.replace(config.settings, **changes))
    messages = FakeMessages(*results, **kwargs)
    monkeypatch.setattr(claude, "_client", lambda: SimpleNamespace(messages=messages))
    return messages


def schema_fields(call):
    """The field names of the JSON schema a request asked Claude to follow, in order."""
    return list(call["output_config"]["format"]["schema"]["properties"])
