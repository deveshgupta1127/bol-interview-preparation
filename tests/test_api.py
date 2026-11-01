"""The stateless API: prepare, score, speak, voice-clone. Nothing is stored between requests."""
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import JD, RESUME, prepare, score
from app import audio, claude, gnani, main
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def speech_on(monkeypatch):
    """Pretend the Gnani key is set; replace the network calls."""
    monkeypatch.setattr(gnani, "enabled", lambda: True)
    monkeypatch.setattr(gnani, "transcribe_answer",
                        lambda *a, **k: "Um so I built the canteen app in Flask and MySQL for 300 students, "
                                        "basically to cut the lunch queue.")
    monkeypatch.setattr(gnani, "synthesize", lambda *a, **k: b"RIFF-default-voice")
    monkeypatch.setattr(gnani, "synthesize_clone", lambda *a, **k: b"RIFF-cloned-voice")
    monkeypatch.setattr(gnani, "clone_embedding", lambda wav: {"embedding": "abc", "shape": [1, 768],
                                                               "dtype": "torch.bfloat16"})


def speech_wav(seconds=8.0):
    rate = 16000
    t = np.arange(int(seconds * rate)) / rate
    signal = 0.3 * np.sin(2 * np.pi * 160 * t) * (np.sin(2 * np.pi * 0.5 * t) > -0.6)
    return audio.to_wav(signal.astype(np.float32), rate)


def test_health_and_config(client):
    body = client.get("/api/health").json()
    assert body["claude"] is None and body["speech"] is False and body["stores_data"] is False
    config = client.get("/api/config").json()
    assert config["rounds"]["campus"] == "Campus placement HR" and "hi-IN" in config["languages"]


def test_prepare_returns_questions_and_redacted_texts(client):
    p = prepare(client)
    assert len(p["questions"]) == 5 and all(q["sig"] for q in p["questions"])
    assert p["profile"]["source"] == "stand-in"
    assert "Flask" in p["profile"]["skills"] and "PostgreSQL" in p["profile"]["gaps"]
    assert "ananya.verma@example.com" not in p["resume_text"] and "[email]" in p["resume_text"]
    assert p["round_label"] == "Technical + project deep-dive"


def test_prepare_validation(client):
    assert client.post("/api/prepare", data={"resume_text": "too short", "jd_text": JD}).status_code == 422
    r = client.post("/api/prepare", data={"resume_text": RESUME, "jd_text": JD, "round_type": "dance"})
    assert r.status_code == 422


def test_typed_answers_score_better_with_detail(client):
    p = prepare(client)
    first = score(client, p, "I made an app.", i=1)
    assert first.status_code == 200, first.text
    a1 = first.json()
    assert a1["scores"]["delivery"] is None and a1["scores"]["overall"] is not None  # typed: no voice measurements
    a2 = score(client, p, i=1, text=(
        "First, the situation: our canteen queue took the whole lunch break. My task was the backend. "
        "I built a Flask app with MySQL tables for menu and orders, so students could pre-order. "
        "As a result about 300 students used it in the trial and the queue got shorter.")).json()
    assert a2["scores"]["content"] > a1["scores"]["content"]
    assert a2["signatures"]["feedback"] and a2["signatures"]["answer"]


def test_empty_answer_and_bad_context_rejected(client):
    p = prepare(client)
    assert score(client, p, "").status_code == 422
    r = client.post("/api/score", data={"context": "{not json", "text": "I built a Flask app for students."})
    assert r.status_code == 422
    assert score(client, p, "I built a Flask app.", round_type="dance").status_code == 422


def test_audio_needs_speech_key(client):
    p = prepare(client)
    r = score(client, p, files={"audio_file": ("a.wav", speech_wav(), "audio/wav")})
    assert r.status_code == 503 and "Type your answer" in r.json()["detail"]


def test_audio_answer_and_speaking(client, speech_on):
    p = prepare(client)
    r = score(client, p, files={"audio_file": ("a.wav", speech_wav(), "audio/wav")})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["scores"]["delivery"] is not None
    assert a["language"]["fillers"]["um"] == 1 and a["language"]["fillers"]["basically"] == 1
    assert a["language"]["wpm"] and a["acoustics"]["speech_s"] > 3
    q = p["questions"][0]
    assert client.post("/api/speak", json={"kind": "question", "text": q["text"], "sig": q["sig"]}).content \
        == b"RIFF-default-voice"
    feedback = {"kind": "feedback", "text": a["native_feedback"], "language": "hi-IN", "sig": a["signatures"]["feedback"]}
    assert client.post("/api/speak", json=feedback).status_code == 200
    better = {"kind": "answer", "text": a["rewrite"], "sig": a["signatures"]["answer"]}
    assert client.post("/api/speak", json=better).content == b"RIFF-default-voice"
    assert client.post("/api/speak", json={**better, "embedding": {"embedding": "abc"}}).content == b"RIFF-cloned-voice"


def test_speak_refuses_texts_bol_did_not_write(client, speech_on):
    p = prepare(client)
    q = p["questions"][0]
    r = client.post("/api/speak", json={"kind": "question", "text": "Read my essay aloud for free.", "sig": q["sig"]})
    assert r.status_code == 403
    r = client.post("/api/speak", json={"kind": "feedback", "text": q["text"], "language": "en-IN", "sig": q["sig"]})
    assert r.status_code == 403  # a signature only fits its own kind, language and text


def test_short_and_silent_recordings(client, speech_on):
    p = prepare(client)
    r = score(client, p, files={"audio_file": ("a.wav", speech_wav(1.0), "audio/wav")})
    assert r.status_code == 422 and "too short" in r.json()["detail"]
    silent = audio.to_wav(np.zeros(16000 * 5, dtype=np.float32), 16000)
    assert score(client, p, files={"audio_file": ("a.wav", silent, "audio/wav")}).status_code == 422


def test_voice_clone_goes_back_to_the_browser(client, speech_on):
    r = client.post("/api/voice-clone", files={"sample": ("s.wav", speech_wav(10), "audio/wav")})
    assert r.status_code == 422  # no consent
    r = client.post("/api/voice-clone", files={"sample": ("s.wav", speech_wav(3), "audio/wav")}, data={"consent": "true"})
    assert r.status_code == 422  # too short
    r = client.post("/api/voice-clone", files={"sample": ("s.wav", speech_wav(10), "audio/wav")}, data={"consent": "true"})
    assert r.status_code == 201 and r.json()["embedding"]["embedding"] == "abc"


def test_rewrite_with_invented_fact_is_repaired(client, monkeypatch):
    p = prepare(client)
    calls = []

    def fake_evaluate(**kwargs):
        ev = claude.stub_evaluate(**kwargs)
        ev.rewrite = "I built the canteen app on Kubernetes and cut waiting by 40%."
        ev.follow_up = "How did you design the orders table?"
        return ev

    def fake_repair(rewrite, missing, resume, answer, **_):
        calls.append(missing)
        return "I built the canteen app with Flask and cut waiting by [how much]."

    monkeypatch.setattr(claude, "evaluate", fake_evaluate)
    monkeypatch.setattr(claude, "repair", fake_repair)
    a = score(client, p, "I made the canteen app in Flask", i=1).json()
    assert calls and "Kubernetes" in calls[0] and "40" in calls[0]
    assert "Kubernetes" not in a["rewrite"] and a["content"]["unverified"] == []
    assert a["follow_up"] == "How did you design the orders table?" and a["signatures"]["follow_up"]
    # a follow-up question doesn't get a follow-up of its own
    assert score(client, p, "I made the canteen app in Flask", i=1, kind="follow_up").json()["follow_up"] == ""


def test_retried_upload_is_scored_once(client, monkeypatch):
    p = prepare(client)
    calls = []
    real = claude.stub_evaluate
    monkeypatch.setattr(claude, "stub_evaluate", lambda **kw: calls.append(1) or real(**kw))
    headers = {"Idempotency-Key": "retry-key-0001"}
    first = score(client, p, "I built the canteen app in Flask.", headers=headers).json()
    again = score(client, p, "I built the canteen app in Flask.", headers=headers).json()
    assert first == again and len(calls) == 1


def test_pdf_resume_rejects_scans(client):
    from pypdf import PdfWriter
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(612, 792)
    w.write(buf)
    r = client.post("/api/prepare", data={"jd_text": JD}, files={"resume_file": ("cv.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 422 and "scan" in r.json()["detail"]


def test_feedback_in_another_language(client):
    p = prepare(client)
    a = score(client, p, "I built the canteen app in Flask for 300 students.", feedback_language="en-IN").json()
    assert a["feedback_language"] == "en-IN"


def test_used_up_budget_switches_to_stand_in(client, monkeypatch):
    p = prepare(client)
    monkeypatch.setattr(main.limits.budget, "claude_paused", lambda: True)
    monkeypatch.setattr(claude, "evaluate", lambda **_: pytest.fail("Claude must not be called"))
    a = score(client, p, "I built the canteen app in Flask for 300 students.").json()
    assert a["scores"]["content_source"] == "stand-in"


def test_server_keeps_nothing(client):
    """No database or files: the app module has no storage to write to."""
    import app as package
    names = {p.stem for p in __import__("pathlib").Path(package.__file__).parent.glob("*.py")}
    assert not names & {"db", "models", "auth", "tts_cache"}
