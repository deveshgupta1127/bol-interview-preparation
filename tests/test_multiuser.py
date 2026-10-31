"""Phase 2: sign-in, isolation between students, limits, one answer at a time, idempotency, account deletion.

Tokens are signed here with a test RSA key that stands in for Entra's published keys (no network).
"""
import dataclasses
import time
import uuid
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from conftest import SAMPLES
from app import auth, claude, config, limits, main, tts_cache
from app.db import SessionLocal
from app.models import AppBudget, Attempt, PracticeSession, UsageDaily, User, VoiceClone, utcnow

ISSUER = "https://tenant-id.ciamlogin.com/tenant-id/v2.0"
AUDIENCE = "api-client-id"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RESUME = (SAMPLES / "resume_ananya.txt").read_text(encoding="utf-8")
JD = (SAMPLES / "jd_associate_engineer.txt").read_text(encoding="utf-8")


def token(sub, *, aud=AUDIENCE, scp="practice.readwrite", exp=3600, key=KEY, **extra):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": aud, "sub": sub, "scp": scp, "iat": now, "exp": now + exp, **extra}
    return jwt.encode(claims, key, algorithm="RS256")


def bearer(sub, **kw):
    return {"Authorization": f"Bearer {token(sub, **kw)}"}


@pytest.fixture
def signed_in(monkeypatch):
    """Sign-in switched on, with test keys. Yields a helper making headers for a fresh student."""
    on = dataclasses.replace(config.settings, auth_issuer=ISSUER, auth_audience=AUDIENCE)
    for module in (main, auth):
        monkeypatch.setattr(module, "settings", on)
    monkeypatch.setattr(auth, "signing_key", lambda _: KEY.public_key())
    return lambda name="Ravi": bearer(f"user-{uuid.uuid4().hex}", given_name=name, email=f"{name}@example.com")


def set_limits(monkeypatch, **values):
    monkeypatch.setattr(limits, "settings", dataclasses.replace(
        config.settings, limits=dataclasses.replace(config.settings.limits, **values)))


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def new_session(client, headers):
    r = client.post("/api/sessions", headers=headers, data={"resume_text": RESUME, "jd_text": JD, "round_type": "hr"})
    assert r.status_code == 201, r.text
    return r.json()


def answer(client, headers, qid, key=None, text="I built the canteen app in Flask for 300 students."):
    h = {**headers, **({"Idempotency-Key": key} if key else {})}
    return client.post(f"/api/questions/{qid}/attempts", headers=h, data={"text": text})


# ---------- sign-in ----------

def test_sign_in_required(client, signed_in):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/health").status_code == 200  # the probe needs no sign-in


@pytest.mark.parametrize("bad, status", [
    ({"aud": "someone-else"}, 401),
    ({"exp": -120}, 401),
    ({"scp": "openid"}, 403),
    ({"key": rsa.generate_private_key(public_exponent=65537, key_size=2048)}, 401),
])
def test_bad_tokens_are_refused(client, signed_in, bad, status):
    assert client.get("/api/me", headers=bearer("user-x", **bad)).status_code == status


def test_garbage_token(client, signed_in, monkeypatch):
    # The real key lookup: a token that isn't a JWT fails before any network call.
    on = dataclasses.replace(main.settings, auth_jwks_url="https://example.invalid/keys")
    monkeypatch.setattr(auth, "settings", on)
    monkeypatch.setattr(auth, "signing_key", lambda t: auth._jwks().get_signing_key_from_jwt(t).key)
    auth._jwks_url.cache_clear()
    auth._jwks.cache_clear()
    try:
        assert client.get("/api/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
    finally:
        auth._jwks_url.cache_clear()
        auth._jwks.cache_clear()


def test_first_sign_in_creates_the_student(client, signed_in):
    headers = signed_in("Meera")
    me = client.get("/api/me", headers=headers).json()
    assert me["name"] == "Meera" and me["email"] == "Meera@example.com" and me["sign_in"] is True
    assert client.get("/api/me", headers=headers).json()["name"] == "Meera"  # same row, not a second one


# ---------- isolation ----------

def test_students_cannot_see_each_other(client, signed_in, monkeypatch):
    a, b = signed_in("Asha"), signed_in("Bilal")
    s = new_session(client, a)
    qid = s["questions"][0]["id"]
    att = answer(client, a, qid).json()
    monkeypatch.setattr(main.gnani, "clone_embedding", lambda wav: {"embedding": "e", "shape": [1], "dtype": "x"})

    for url in (f"/api/sessions/{s['id']}", f"/api/questions/{qid}", f"/api/attempts/{att['id']}",
                f"/api/questions/{qid}/audio", f"/api/attempts/{att['id']}/feedback-audio",
                f"/api/attempts/{att['id']}/model-answer-audio"):
        assert client.get(url, headers=b).status_code == 404, url
    assert answer(client, b, qid).status_code == 404
    assert client.get("/api/sessions", headers=b).json() == []
    assert client.get("/api/progress", headers=b).json()["count"] == 0
    assert [x["id"] for x in client.get("/api/sessions", headers=a).json()] == [s["id"]]
    assert client.get("/api/progress", headers=a).json()["count"] == 1
    with SessionLocal() as db:
        stored = db.get(Attempt, att["id"])
        assert stored.user_id == db.get(PracticeSession, s["id"]).user_id


# ---------- limits and the monthly budget ----------

def test_daily_answer_limit(client, signed_in, monkeypatch):
    set_limits(monkeypatch, answers_per_day=2)
    h = signed_in()
    qid = new_session(client, h)["questions"][0]["id"]
    assert answer(client, h, qid).status_code == 201
    assert answer(client, h, qid).status_code == 201
    r = answer(client, h, qid)
    assert r.status_code == 429 and "See you tomorrow" in r.json()["detail"]
    other = signed_in()  # another student is not affected
    assert answer(client, other, new_session(client, other)["questions"][0]["id"]).status_code == 201


def test_daily_session_limit(client, signed_in, monkeypatch):
    set_limits(monkeypatch, sessions_per_day=1)
    h = signed_in()
    new_session(client, h)
    r = client.post("/api/sessions", headers=h, data={"resume_text": RESUME, "jd_text": JD})
    assert r.status_code == 429


def test_usage_is_counted(client, signed_in):
    h = signed_in()
    s = new_session(client, h)
    answer(client, h, s["questions"][0]["id"])
    with SessionLocal() as db:
        uid = db.get(PracticeSession, s["id"]).user_id
        row = db.get(UsageDaily, (uid, limits.today()))
    assert (row.sessions, row.answers) == (1, 1)


def test_used_up_budget_switches_to_stand_in(client, monkeypatch):
    set_limits(monkeypatch, claude_usd_per_month=1.0)
    with SessionLocal() as db:
        limits._add(db, AppBudget, {"month": limits.this_month()}, {"claude_usd": 5.0})
        db.commit()
    monkeypatch.setattr(claude, "evaluate", lambda **_: pytest.fail("Claude must not be called"))
    monkeypatch.setattr(claude, "prepare", lambda *a, **k: pytest.fail("Claude must not be called"))
    try:
        assert client.get("/api/me").json()["claude_paused"] is True
        s = client.post("/api/sessions", data={"resume_text": RESUME, "jd_text": JD}).json()
        assert s["profile"]["source"] == "stand-in"
        a = client.post(f"/api/questions/{s['questions'][0]['id']}/attempts", data={"text": "I built a Flask app."})
        assert a.status_code == 201 and a.json()["scores"]["content_source"] == "stand-in"
    finally:
        with SessionLocal() as db:
            db.query(AppBudget).delete()
            db.commit()


# ---------- one answer at a time, idempotency ----------

def test_parallel_answer_is_refused(client, signed_in):
    h = signed_in()
    qid = new_session(client, h)["questions"][0]["id"]
    client.get("/api/me", headers=h)
    with SessionLocal() as db:  # as if this student's other tab were still being scored
        user = db.query(User).order_by(User.created_at.desc()).first()
        user.busy_until = utcnow() + timedelta(seconds=60)
        db.commit()
    r = answer(client, h, qid)
    assert r.status_code == 429 and "Finish your current answer" in r.json()["detail"]
    with SessionLocal() as db:  # the slot expires on its own if a replica dies mid-answer
        db.get(User, user.id).busy_until = utcnow() - timedelta(seconds=1)
        db.commit()
    assert answer(client, h, qid).status_code == 201
    with SessionLocal() as db:
        assert db.get(User, user.id).busy_until is None  # freed after the answer


def test_retried_upload_is_scored_once(client, signed_in, monkeypatch):
    h = signed_in()
    qid = new_session(client, h)["questions"][0]["id"]
    first = answer(client, h, qid, key="retry-key-0001").json()
    again = answer(client, h, qid, key="retry-key-0001").json()
    assert again["id"] == first["id"] and again["number"] == 1
    assert answer(client, h, qid, key="retry-key-0002").json()["number"] == 2
    assert answer(client, h, qid, key="bad key!").status_code == 422
    other = signed_in()  # keys are per student
    oq = new_session(client, other)["questions"][0]["id"]
    assert answer(client, other, oq, key="retry-key-0001").json()["id"] != first["id"]


# ---------- cached speech ----------

def test_speech_is_cached_and_counted_once(client, signed_in, monkeypatch):
    store, calls = {}, []
    monkeypatch.setattr(tts_cache, "get", store.get)
    monkeypatch.setattr(tts_cache, "put", store.__setitem__)
    monkeypatch.setattr(main.gnani, "synthesize", lambda *a, **k: calls.append(1) or b"RIFF-voice")
    set_limits(monkeypatch, audio_plays_per_day=1)
    h = signed_in()
    s = new_session(client, h)
    q1, q2 = s["questions"][0]["id"], s["questions"][1]["id"]
    assert client.get(f"/api/questions/{q1}/audio", headers=h).content == b"RIFF-voice"
    assert client.get(f"/api/questions/{q1}/audio", headers=h).content == b"RIFF-voice"  # from the cache
    assert len(calls) == 1 and all(k.endswith(f"q/{q1}.wav") for k in store)
    assert client.get(f"/api/questions/{q2}/audio", headers=h).status_code == 429  # 1 play a day, cache was free


# ---------- delete my account ----------

def test_delete_my_account(client, signed_in, monkeypatch):
    deleted = []
    monkeypatch.setattr(tts_cache, "delete_user", deleted.append)
    h = signed_in("Zoya")
    s = new_session(client, h)
    answer(client, h, s["questions"][0]["id"])
    with SessionLocal() as db:
        uid = db.get(PracticeSession, s["id"]).user_id
        db.add(VoiceClone(user_id=uid, embedding="{}"))
        db.commit()
    assert client.delete("/api/me", headers=h).status_code == 204
    with SessionLocal() as db:
        assert db.get(User, uid) is None and db.get(PracticeSession, s["id"]) is None
        assert db.query(Attempt).filter(Attempt.user_id == uid).count() == 0
        assert db.get(VoiceClone, uid) is None and db.query(UsageDaily).filter(UsageDaily.user_id == uid).count() == 0
    assert deleted == [uid]
    assert client.get("/api/sessions", headers=h).json() == []  # signing in again starts a fresh account


# ---------- set-up ----------

def test_health_checks_the_database(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["database"] is True


def test_schema_matches_migrations(client):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db import Base, engine
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_postgres_urls_get_the_psycopg_driver(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://bol:pw@bol-pg.postgres.database.azure.com/bol?sslmode=require")
    assert config.database_url() == "postgresql+psycopg://bol:pw@bol-pg.postgres.database.azure.com/bol?sslmode=require"
