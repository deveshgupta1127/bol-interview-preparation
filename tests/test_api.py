import dataclasses
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLES
from app import audio, claude, gnani
from app.main import app

RESUME = (SAMPLES / "resume_ananya.txt").read_text(encoding="utf-8")
JD = (SAMPLES / "jd_associate_engineer.txt").read_text(encoding="utf-8")


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


def new_session(client):
    r = client.post("/api/sessions", data={"resume_text": RESUME, "jd_text": JD, "round_type": "technical",
                                           "target_role": "Associate Software Engineer"})
    assert r.status_code == 201, r.text
    return r.json()


def test_health_shows_demo_mode(client):
    body = client.get("/api/health").json()
    assert body["claude"] is None and body["speech"] is False


def test_session_has_questions_and_redacted_resume(client):
    s = new_session(client)
    assert len(s["questions"]) == 5
    assert s["profile"]["source"] == "stand-in"
    assert "Flask" in s["profile"]["skills"]
    assert "PostgreSQL" in s["profile"]["gaps"]
    from app.db import SessionLocal
    from app.models import PracticeSession
    with SessionLocal() as db:
        stored = db.get(PracticeSession, s["id"]).resume_text
    assert "ananya.verma@example.com" not in stored and "[email]" in stored


def test_session_validation(client):
    r = client.post("/api/sessions", data={"resume_text": "too short", "jd_text": JD})
    assert r.status_code == 422
    r = client.post("/api/sessions", data={"resume_text": RESUME, "jd_text": JD, "round_type": "dance"})
    assert r.status_code == 422


def test_typed_attempts_and_compare(client):
    s = new_session(client)
    qid = s["questions"][1]["id"]
    first = client.post(f"/api/questions/{qid}/attempts", data={"text": "I made an app."})
    assert first.status_code == 201, first.text
    a1 = first.json()
    assert a1["scores"]["delivery"] is None  # typed: no voice measurements
    assert a1["scores"]["overall"] is not None
    second = client.post(f"/api/questions/{qid}/attempts", data={
        "text": "First, the situation: our canteen queue took the whole lunch break. My task was the backend. "
                "I built a Flask app with MySQL tables for menu and orders, so students could pre-order. "
                "As a result about 300 students used it in the trial and the queue got shorter."})
    a2 = second.json()
    assert a2["number"] == 2 and a2["previous"]["number"] == 1
    assert a2["scores"]["content"] > a1["scores"]["content"]
    assert client.get(f"/api/attempts/{a2['id']}").json()["question"]["id"] == qid
    assert client.get("/api/progress").json()["count"] >= 2


def test_empty_answer_rejected(client):
    s = new_session(client)
    r = client.post(f"/api/questions/{s['questions'][0]['id']}/attempts", data={"text": ""})
    assert r.status_code == 422


def test_audio_needs_speech_key(client):
    s = new_session(client)
    r = client.post(f"/api/questions/{s['questions'][0]['id']}/attempts",
                    files={"audio_file": ("a.wav", speech_wav(), "audio/wav")})
    assert r.status_code == 503 and "Type your answer" in r.json()["detail"]


def test_audio_attempt(client, speech_on):
    s = new_session(client)
    r = client.post(f"/api/questions/{s['questions'][0]['id']}/attempts",
                    files={"audio_file": ("a.wav", speech_wav(), "audio/wav")})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["scores"]["delivery"] is not None
    assert a["language"]["fillers"]["um"] == 1 and a["language"]["fillers"]["basically"] == 1
    assert a["language"]["wpm"] and a["acoustics"]["speech_s"] > 3
    assert client.get(f"/api/attempts/{a['id']}/feedback-audio").content == b"RIFF-default-voice"
    assert client.get(f"/api/attempts/{a['id']}/model-answer-audio").content == b"RIFF-default-voice"


def test_short_and_silent_recordings(client, speech_on):
    s = new_session(client)
    url = f"/api/questions/{s['questions'][0]['id']}/attempts"
    r = client.post(url, files={"audio_file": ("a.wav", speech_wav(1.0), "audio/wav")})
    assert r.status_code == 422 and "too short" in r.json()["detail"]
    silent = audio.to_wav(np.zeros(16000 * 5, dtype=np.float32), 16000)
    r = client.post(url, files={"audio_file": ("a.wav", silent, "audio/wav")})
    assert r.status_code == 422


def test_voice_clone_flow(client, speech_on):
    r = client.post("/api/voice-clone", files={"sample": ("s.wav", speech_wav(10), "audio/wav")})
    assert r.status_code == 422  # no consent
    r = client.post("/api/voice-clone", files={"sample": ("s.wav", speech_wav(3), "audio/wav")},
                    data={"consent": "true"})
    assert r.status_code == 422  # too short
    r = client.post("/api/voice-clone", files={"sample": ("s.wav", speech_wav(10), "audio/wav")},
                    data={"consent": "true"})
    assert r.status_code == 201 and client.get("/api/me").json()["has_voice_clone"]

    s = new_session(client)
    a = client.post(f"/api/questions/{s['questions'][0]['id']}/attempts", data={"text": "I built a Flask app."}).json()
    assert client.get(f"/api/attempts/{a['id']}/model-answer-audio").content == b"RIFF-cloned-voice"
    assert client.delete("/api/voice-clone").json() == {"has_voice_clone": False}


def test_rewrite_with_invented_fact_is_repaired(client, monkeypatch):
    s = new_session(client)
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
    qid = s["questions"][1]["id"]
    a = client.post(f"/api/questions/{qid}/attempts", data={"text": "I made the canteen app in Flask"}).json()
    assert calls and "Kubernetes" in calls[0] and "40" in calls[0]
    assert "Kubernetes" not in a["rewrite"] and a["content"]["unverified"] == []
    # follow-up question was added under the main question, once
    assert a["follow_up_question_id"]
    again = client.post(f"/api/questions/{qid}/attempts", data={"text": "I made the canteen app in Flask"}).json()
    assert again["follow_up_question_id"] == a["follow_up_question_id"]
    detail = client.get(f"/api/sessions/{s['id']}").json()
    kinds = [q["kind"] for q in detail["questions"]]
    assert kinds.count("follow_up") == 1 and kinds[kinds.index("follow_up") - 1] == "main"


def test_pdf_resume_rejects_scans(client):
    from pypdf import PdfWriter
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(612, 792)
    w.write(buf)
    r = client.post("/api/sessions", data={"jd_text": JD},
                    files={"resume_file": ("cv.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 422 and "scan" in r.json()["detail"]


def test_feedback_language_setting(client):
    assert client.patch("/api/me", json={"feedback_language": "ta-IN"}).json()["feedback_language"] == "ta-IN"
    assert client.patch("/api/me", json={"feedback_language": "xx"}).status_code == 422
    client.patch("/api/me", json={"feedback_language": "hi-IN"})


def test_interview_date_and_question_place(client):
    assert client.patch("/api/me", json={"interview_date": "2026-11-20"}).json()["interview_date"] == "2026-11-20"
    assert client.patch("/api/me", json={"interview_date": "20/11"}).status_code == 422
    assert client.patch("/api/me", json={"interview_date": ""}).json()["interview_date"] is None
    s = new_session(client)
    q2 = client.get(f"/api/questions/{s['questions'][1]['id']}").json()
    assert (q2["number"], q2["total"]) == (2, 5) and q2["next_question_id"] == s["questions"][2]["id"]
    last = client.get(f"/api/questions/{s['questions'][4]['id']}").json()
    assert last["next_question_id"] is None
    a = client.post(f"/api/questions/{s['questions'][1]['id']}/attempts", data={"text": "I built a Flask app."}).json()
    detail = client.get(f"/api/attempts/{a['id']}").json()
    assert detail["question"]["number"] == 2 and detail["session_id"] == s["id"]


def test_campus_round(client):
    r = client.post("/api/sessions", data={"resume_text": RESUME, "jd_text": JD, "round_type": "campus"})
    assert r.status_code == 201 and r.json()["round_label"] == "Campus placement HR"
