"""Bol API. Run: uvicorn app.main:app --reload --port 8020

Stateless: every request finds its student from the sign-in token, and every row is scoped to that student,
so any replica can serve any request.
"""
import hashlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audio, auth, claude, factcheck, gnani, language, limits, pdf, pii, scoring, tts_cache
from .config import LANGUAGES, ROUNDS, settings
from .db import engine, get_session, init_db
from .models import Attempt, PracticeSession, Question, UsageDaily, User, VoiceClone, utcnow

log = logging.getLogger("bol")
MAX_AUDIO_BYTES = 8 * 1024 * 1024  # 3 min of 16 kHz mono 16-bit is ~5.8 MB
LOCAL_SUBJECT, LOCAL_EMAIL = "local", "local@bol.app"  # local mode (no AUTH_ISSUER): one built-in student

if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    # Request timings, errors and the "bol" logs go to Application Insights. Logs never carry transcripts,
    # resume text, embeddings or tokens. Must run before FastAPI() so requests are traced.
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(logger_name="bol")
    log.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Bol", lifespan=lifespan)


def _web_headers() -> dict[str, str]:
    """Static Web Apps sends the headers in web/staticwebapp.config.json; local runs send the same ones."""
    try:
        config = json.loads((settings.web_dir / "staticwebapp.config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    headers = config.get("globalHeaders", {})
    headers.pop("Strict-Transport-Security", None)  # localhost is plain http
    return {k: v.replace(" __API_ORIGIN__", "") for k, v in headers.items()}  # the API is same-origin locally


WEB_HEADERS = _web_headers() if settings.serve_web else {}
API_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


@app.middleware("http")
async def guard(request, call_next):
    """Every request: refuse huge bodies and floods from one address, then add security headers.

    Registered before CORS, so CORS wraps it and the browser can read these refusals too.
    """
    path = request.url.path
    if path.startswith("/api/") and path != "/api/health":  # health is polled by Azure's probes
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > settings.max_request_mb * 1024 * 1024:
            return JSONResponse({"detail": "That upload is too large."}, status_code=413)
        ip = limits.client_ip(request.headers, request.client.host if request.client else None)
        wait = limits.per_ip.hit(ip, settings.limits.requests_per_minute_per_ip)
        if wait:
            return JSONResponse({"detail": f"Too many requests. Wait {wait} seconds and try again."},
                                status_code=429, headers={"Retry-After": str(wait)})
    response = await call_next(request)
    for name, value in (API_HEADERS if path.startswith("/api/") else WEB_HEADERS).items():
        response.headers.setdefault(name, value)  # audio keeps its own Cache-Control
    return response


if settings.cors_origins:  # the Static Web Apps site calls the API from another origin
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key"], max_age=600)
bearer = HTTPBearer(auto_error=False)


def _tag_span(user_id: str) -> None:
    try:
        from opentelemetry import trace

        trace.get_current_span().set_attribute("enduser.id", user_id)
    except ImportError:
        pass


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                 db: Session = Depends(get_session)) -> User:
    """The signed-in student, created on first sign-in. Local mode: the one built-in student."""
    if settings.auth_enabled:
        if not creds:
            raise HTTPException(401, "Please sign in.", headers={"WWW-Authenticate": "Bearer"})
        try:
            subject, email, name = auth.identity(auth.verify(creds.credentials))
        except auth.AuthError as exc:
            raise HTTPException(exc.status, str(exc), headers={"WWW-Authenticate": "Bearer"})
    else:
        subject, email, name = LOCAL_SUBJECT, LOCAL_EMAIL, ""
    user = db.scalar(select(User).where(User.auth_subject == subject))
    if not user:
        try:
            user = User(auth_subject=subject, email=email, name=name, last_seen_at=utcnow())
            db.add(user)
            db.commit()
        except IntegrityError:  # the same student's first two requests arrived together
            db.rollback()
            user = db.scalar(select(User).where(User.auth_subject == subject))
    elif not user.last_seen_at or utcnow() - user.last_seen_at > timedelta(hours=1):
        user.last_seen_at = utcnow()  # for purging accounts unused for 12 months
        db.commit()
    _tag_span(user.id)
    return user


# ---------- helpers ----------

def _owned_session(db: Session, user: User, session_id: str) -> PracticeSession:
    s = db.get(PracticeSession, session_id)
    if not s or s.user_id != user.id:
        raise HTTPException(404, "Practice session not found")
    return s


def _owned_question(db: Session, user: User, question_id: str) -> tuple[Question, PracticeSession]:
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Question not found")
    return q, _owned_session(db, user, q.session_id)


def _owned_attempt(db: Session, user: User, attempt_id: str) -> tuple[Attempt, Question, PracticeSession]:
    a = db.get(Attempt, attempt_id)
    if not a:
        raise HTTPException(404, "Attempt not found")
    q, s = _owned_question(db, user, a.question_id)
    return a, q, s


def _questions(db: Session, session_id: str) -> list[Question]:
    rows = db.scalars(select(Question).where(Question.session_id == session_id)).all()
    return sorted(rows, key=lambda q: (q.position, q.kind != "main"))


def _attempt_json(a: Attempt, previous: Attempt | None = None) -> dict:
    out = {
        "id": a.id, "question_id": a.question_id, "number": a.number, "transcript": a.transcript,
        "duration_s": a.duration_s, "acoustics": a.acoustics, "language": a.language, "scores": a.scores,
        "content": a.content, "rewrite": a.rewrite, "tips": a.tips, "native_feedback": a.native_feedback,
        "feedback_language": a.feedback_language, "notes": a.notes, "created_at": a.created_at.isoformat() + "Z",
    }
    if previous:
        out["previous"] = {"id": previous.id, "number": previous.number, "scores": previous.scores,
                           "fillers_per_min": previous.language.get("fillers_per_min"),
                           "wpm": previous.language.get("wpm")}
    return out


def _question_json(q: Question, attempts: list[Attempt]) -> dict:
    best = max((a.scores.get("overall") or 0 for a in attempts), default=None)
    return {"id": q.id, "position": q.position, "text": q.text, "why": q.why, "kind": q.kind,
            "parent_id": q.parent_id, "attempts": len(attempts), "best": best,
            "last_attempt_id": attempts[-1].id if attempts else None}


def _place(db: Session, q: Question) -> dict:
    """Where a question sits in its session: "Question 2 of 5" and the next main question to go to."""
    mains = [x for x in _questions(db, q.session_id) if x.kind == "main"]
    anchor = q.parent_id or q.id
    idx = next((i for i, x in enumerate(mains) if x.id == anchor), 0)
    return {"number": idx + 1, "total": len(mains),
            "next_question_id": mains[idx + 1].id if idx + 1 < len(mains) else None}


def _wav_response(data: bytes) -> Response:
    return Response(content=data, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


def _speech(db: Session, user: User, key: str, text: str, make) -> Response:
    """Spoken audio from the cache, or made by Gnani (counted against the limits) and then cached."""
    cache_key = f"{user.id}/{key}.wav"
    cached = tts_cache.get(cache_key)
    if cached:
        return _wav_response(cached)
    limits.check_daily(db, user, "audio_plays")
    limits.check_burst(user)
    if limits.voices_paused(db):
        raise HTTPException(503, "Voices are paused for this month. Read the text instead.")
    db.commit()  # don't hold a database connection while Gnani works
    try:
        data = make()
    except gnani.GnaniError as exc:
        raise HTTPException(503, str(exc))
    tts_cache.put(cache_key, data)
    limits.record(db, user, audio_plays=1, timbre_chars=len(text))
    db.commit()
    return _wav_response(data)


def _clean_key(value: str | None) -> str | None:
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,64}", value):
        raise HTTPException(422, "Idempotency-Key must be 8-64 letters, digits, - or _.")
    return value


# ---------- routes ----------

@app.get("/api/health")
def health(response: Response):
    """Liveness and readiness probe for Container Apps; also checks the database."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database = True
    except Exception as exc:
        log.warning("health: database unreachable: %s", type(exc).__name__)
        database = False
        response.status_code = 503
    return {"ok": database, "database": database, "claude": settings.claude_model if settings.claude_enabled else None,
            "speech": gnani.enabled(), "sign_in": settings.auth_enabled}


class MeUpdate(BaseModel):
    name: str | None = None
    feedback_language: str | None = None
    interview_date: str | None = None  # YYYY-MM-DD, or "" to clear


def _me_json(db: Session, user: User) -> dict:
    return {"name": user.name, "feedback_language": user.feedback_language, "languages": LANGUAGES,
            "rounds": ROUNDS, "has_voice_clone": db.get(VoiceClone, user.id) is not None,
            "interview_date": user.interview_date.isoformat() if user.interview_date else None,
            "claude": settings.claude_enabled, "claude_model": settings.claude_model, "speech": gnani.enabled(),
            "claude_paused": limits.claude_paused(db), "speech_paused": limits.speech_paused(db),
            "voices_paused": limits.voices_paused(db), "sign_in": settings.auth_enabled, "email": user.email,
            "feedback_voices": sorted(gnani.FEEDBACK_VOICES), "interviewer_voice": gnani.ENGLISH_VOICE}


@app.get("/api/me")
def get_me(db: Session = Depends(get_session), user: User = Depends(current_user)):
    return _me_json(db, user)


@app.patch("/api/me")
def update_me(body: MeUpdate, db: Session = Depends(get_session), user: User = Depends(current_user)):
    if body.name is not None:
        user.name = body.name.strip()[:80]
    if body.feedback_language is not None:
        if body.feedback_language not in LANGUAGES:
            raise HTTPException(422, "Unsupported language")
        user.feedback_language = body.feedback_language
    if body.interview_date is not None:
        try:
            user.interview_date = date.fromisoformat(body.interview_date) if body.interview_date else None
        except ValueError:
            raise HTTPException(422, "Interview date must look like 2026-11-20")
    db.merge(user)
    db.commit()
    return _me_json(db, user)


@app.delete("/api/me", status_code=204)
def delete_me(db: Session = Depends(get_session), user: User = Depends(current_user)):
    """Delete my account: every row of this student, then their cached audio. The browser then signs out."""
    session_ids = select(PracticeSession.id).where(PracticeSession.user_id == user.id)
    question_ids = select(Question.id).where(Question.session_id.in_(session_ids))
    db.execute(delete(Attempt).where((Attempt.user_id == user.id) | Attempt.question_id.in_(question_ids)))
    db.execute(delete(Question).where(Question.session_id.in_(session_ids)))
    db.execute(delete(PracticeSession).where(PracticeSession.user_id == user.id))
    db.execute(delete(VoiceClone).where(VoiceClone.user_id == user.id))
    db.execute(delete(UsageDaily).where(UsageDaily.user_id == user.id))
    db.execute(delete(User).where(User.id == user.id))
    db.commit()
    tts_cache.delete_user(user.id)
    log.info("account deleted: %s", user.id)
    return Response(status_code=204)


@app.post("/api/sessions", status_code=201)
def create_session(jd_text: str = Form(...), round_type: str = Form("hr"), target_role: str = Form(""),
                   resume_text: str = Form(""), resume_file: UploadFile | None = File(None),
                   db: Session = Depends(get_session), user: User = Depends(current_user)):
    if round_type not in ROUNDS:
        raise HTTPException(422, "Unknown round type")
    if resume_file and resume_file.filename:
        try:
            resume_text = pdf.resume_text(resume_file.file.read(pdf.MAX_BYTES + 1))
        except pdf.PdfError as exc:
            raise HTTPException(422, str(exc))
    resume_text, jd_text = resume_text.strip(), jd_text.strip()
    if len(resume_text) < 200:
        raise HTTPException(422, "Add your resume (PDF or at least a few lines of text).")
    if len(jd_text) < 80:
        raise HTTPException(422, "Paste the job description (at least a few lines).")
    resume_text = pii.redact(resume_text)[:15000]
    jd_text = pii.redact(jd_text)[:10000]
    limits.check_daily(db, user, "sessions")
    limits.check_burst(user)
    paused = limits.claude_paused(db)
    db.commit()  # don't hold a database connection while Claude works

    notes = []
    usage = claude.Usage()
    try:
        if paused:
            raise claude.LLMError("this month's coaching budget is used up")
        prep = claude.prepare(resume_text, jd_text, round_type, target_role, usage=usage)
        source = "claude"
    except claude.LLMError as exc:
        log.warning("prepare fell back to stand-in: %s", exc)
        prep = claude.stub_prepare(resume_text, jd_text, round_type, target_role)
        source = "stand-in"
        if settings.claude_enabled:
            notes.append(f"Claude was unavailable ({exc}), so these are template questions.")

    profile = prep.model_dump(exclude={"questions"})
    profile["source"] = source
    profile["notes"] = notes
    profile["usage"] = usage.as_dict()
    log.info("session prepared, claude usage %s", json.dumps(profile["usage"]))
    s = PracticeSession(user_id=user.id, round_type=round_type, resume_text=resume_text, jd_text=jd_text,
                        target_role=(target_role or prep.target_role)[:120], profile=profile)
    db.add(s)
    db.flush()
    for i, q in enumerate(prep.questions):
        db.add(Question(session_id=s.id, position=i, text=q.text, why=q.why))
    if prep.first_name and not user.name:
        user.name = prep.first_name
        db.merge(user)
    limits.record(db, user, sessions=1, claude_usage=profile["usage"])
    db.commit()
    return get_session_detail(s.id, db, user)


@app.get("/api/sessions")
def list_sessions(db: Session = Depends(get_session), user: User = Depends(current_user)):
    rows = db.scalars(select(PracticeSession).where(PracticeSession.user_id == user.id)
                      .order_by(PracticeSession.created_at.desc())).all()
    return [{"id": s.id, "round_type": s.round_type, "target_role": s.target_role,
             "created_at": s.created_at.isoformat() + "Z"} for s in rows]


@app.get("/api/sessions/{session_id}")
def get_session_detail(session_id: str, db: Session = Depends(get_session), user: User = Depends(current_user)):
    s = _owned_session(db, user, session_id)
    questions = []
    for q in _questions(db, s.id):
        attempts = db.scalars(select(Attempt).where(Attempt.question_id == q.id).order_by(Attempt.number)).all()
        questions.append(_question_json(q, list(attempts)))
    return {"id": s.id, "round_type": s.round_type, "round_label": ROUNDS[s.round_type],
            "target_role": s.target_role, "profile": s.profile, "questions": questions, "jd_text": s.jd_text,
            "created_at": s.created_at.isoformat() + "Z"}


@app.get("/api/questions/{question_id}")
def get_question(question_id: str, db: Session = Depends(get_session), user: User = Depends(current_user)):
    q, s = _owned_question(db, user, question_id)
    attempts = db.scalars(select(Attempt).where(Attempt.question_id == q.id).order_by(Attempt.number)).all()
    data = _question_json(q, list(attempts))
    data.update(session_id=s.id, target_role=s.target_role, round_label=ROUNDS[s.round_type],
                history=[_attempt_json(a) for a in attempts], **_place(db, q))
    return data


@app.get("/api/questions/{question_id}/audio")
def question_audio(question_id: str, db: Session = Depends(get_session), user: User = Depends(current_user)):
    """The interviewer reads the question aloud (Timbre, English)."""
    q, _ = _owned_question(db, user, question_id)
    return _speech(db, user, f"q/{q.id}", q.text,
                   lambda: gnani.synthesize(q.text, gnani.ENGLISH_LANGUAGE, gnani.ENGLISH_VOICE, speed=0.95))


@app.post("/api/questions/{question_id}/attempts", status_code=201)
def create_attempt(question_id: str, audio_file: UploadFile | None = File(None), text: str = Form(""),
                   idempotency_key: str | None = Header(None), db: Session = Depends(get_session),
                   user: User = Depends(current_user)):
    """One answer turn. The browser sends an Idempotency-Key, so a retried upload never scores (or bills) twice."""
    q, s = _owned_question(db, user, question_id)
    key = _clean_key(idempotency_key)
    if key and (done := _attempt_by_key(db, user, key)):
        return _attempt_reply(db, done, q)
    limits.check_daily(db, user, "answers")
    limits.check_burst(user)
    if not limits.take_answer_slot(db, user):
        raise HTTPException(429, "Finish your current answer first.")
    try:
        if key and (done := _attempt_by_key(db, user, key)):  # finished while we waited for the slot
            return _attempt_reply(db, done, q)
        return _score_answer(db, user, q, s, audio_file, text, key)
    finally:
        limits.free_answer_slot(db, user)


def _attempt_by_key(db: Session, user: User, key: str) -> Attempt | None:
    return db.scalar(select(Attempt).where(Attempt.user_id == user.id, Attempt.idempotency_key == key))


def _attempt_reply(db: Session, a: Attempt, q: Question, timings: dict | None = None) -> dict:
    previous = db.scalar(select(Attempt).where(Attempt.question_id == q.id, Attempt.number == a.number - 1))
    follow = db.scalar(select(Question).where(Question.parent_id == q.id))
    out = _attempt_json(a, previous)
    out["follow_up_question_id"] = follow.id if follow else None
    out["timings"] = timings or {}
    return out


def _score_answer(db: Session, user: User, q: Question, s: PracticeSession, audio_file: UploadFile | None,
                  text: str, key: str | None) -> dict:
    started = time.monotonic()
    notes: list[str] = []
    acoustic: dict = {}
    timings: dict = {}

    if audio_file and audio_file.filename:
        data = audio_file.file.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Recording is too long (max 3 minutes).")
        try:
            samples, rate = audio.read_wav(data)
        except audio.AudioError as exc:
            raise HTTPException(422, str(exc))
        seconds = len(samples) / rate
        if seconds < settings.min_answer_seconds:
            raise HTTPException(422, "That recording is too short. Answer for at least a few seconds.")
        if seconds > settings.max_answer_seconds + 5:
            raise HTTPException(413, "Recording is too long (max 3 minutes).")
        acoustic = audio.analyse(samples, rate)
        if not acoustic.get("speech_s"):
            raise HTTPException(422, "We couldn't hear any speech. Check your microphone and try again.")
        if not gnani.enabled():
            raise HTTPException(503, "Speech-to-text is off (VACHANA_API_KEY not set). Type your answer instead.")
        if limits.speech_paused(db):
            raise HTTPException(503, "Speech-to-text is paused for this month. Type your answer instead.")
        db.commit()  # don't hold a database connection while Prisma works
        t = time.monotonic()
        try:
            transcript = gnani.transcribe_answer(samples, rate, bias=gnani.bias_words(s.profile.get("skills", [])))
        except gnani.GnaniError as exc:
            log.warning("transcription failed: %s", exc)
            raise HTTPException(502, "Speech-to-text failed. Please try again, or type your answer.")
        timings["stt_s"] = round(time.monotonic() - t, 2)
        if not transcript:
            raise HTTPException(422, "We couldn't make out any words. Try again a little closer to the mic.")
        spoken = True
    else:
        transcript = text.strip()
        if len(transcript) > settings.max_typed_chars:
            raise HTTPException(413, f"That answer is too long. Keep it under {settings.max_typed_chars:,} "
                                     "characters (about 3 minutes spoken).")
        if len(transcript.split()) < 3:
            raise HTTPException(422, "Record or type an answer first.")
        spoken = False

    lang = language.analyse(transcript, acoustic.get("duration_s"), acoustic.get("hesitations", 0))
    delivery_score, delivery_notes = scoring.delivery(acoustic, lang)
    fluency_score, fluency_notes = scoring.fluency(lang, spoken)
    speech_notes = delivery_notes + fluency_notes + [f"Said \"{h['said']}\": {h['why']}" for h in
                                                     lang["indian_english"]]

    eval_args = dict(question=q.text, why=q.why, round_type=s.round_type, role=s.target_role,
                     requirements=s.profile.get("jd_requirements", []), resume=s.resume_text, answer=transcript,
                     speech_notes=speech_notes, feedback_language=user.feedback_language)
    paused = limits.claude_paused(db)
    db.commit()  # don't hold a database connection while Claude works
    t = time.monotonic()
    usage = claude.Usage()
    rubric, unverified, source = None, [], "stand-in"
    try:
        if paused:
            raise claude.LLMError("this month's coaching budget is used up")
        ev = claude.evaluate(**eval_args, usage=usage)
        source = "claude"
        missing = factcheck.unsupported(ev.rewrite, s.resume_text, transcript, q.text)
        if missing:
            try:
                ev.rewrite = claude.repair(ev.rewrite, missing, s.resume_text, transcript, usage=usage)
                unverified = factcheck.unsupported(ev.rewrite, s.resume_text, transcript, q.text)
            except claude.LLMError:
                unverified = missing
    except claude.LLMError as exc:
        log.warning("evaluate failed: %s", exc)
        ev = claude.stub_evaluate(**eval_args)
        if settings.claude_enabled:
            notes.append(f"Content coaching is unavailable right now ({exc}). Speaking scores are real; "
                         "the content score is a rough estimate.")
        else:
            notes.append("Sample coaching: add ANTHROPIC_API_KEY for real content feedback.")
    timings["llm_s"] = round(time.monotonic() - t, 2)
    rubric = {k: getattr(ev, k).model_dump() for k in ("answered", "structure", "specifics", "jd_fit")}
    if unverified:
        notes.append("Check the suggested answer: we couldn't find " + ", ".join(unverified[:5]) +
                     " in your resume or answer. Only say it if it's true.")

    parts = {"content": scoring.content(rubric), "fluency": fluency_score, "delivery": delivery_score}
    scores = {**parts, "overall": scoring.overall(parts), "content_source": source}
    used = usage.as_dict()

    for retry in (False, True):  # attempt numbers are unique per question; recount once if two saves raced
        try:
            previous = db.scalars(select(Attempt).where(Attempt.question_id == q.id)
                                  .order_by(Attempt.number.desc())).first()
            a = Attempt(question_id=q.id, user_id=user.id, idempotency_key=key,
                        number=(previous.number + 1) if previous else 1, transcript=transcript,
                        duration_s=acoustic.get("duration_s"), acoustics=acoustic,
                        language={**lang, "delivery_notes": delivery_notes, "fluency_notes": fluency_notes},
                        scores=scores, content={**rubric, "strength": ev.strength, "follow_up": ev.follow_up,
                                                "unverified": unverified, "usage": used},
                        rewrite=ev.rewrite, tips=ev.tips[:3], native_feedback=ev.native_feedback,
                        feedback_language=user.feedback_language, notes=notes)
            db.add(a)
            if ev.follow_up and q.kind == "main" and not db.scalar(select(Question).where(Question.parent_id == q.id)):
                db.add(Question(session_id=s.id, position=q.position, text=ev.follow_up,
                                why="Follow-up on what you just said.", kind="follow_up", parent_id=q.id))
            limits.record(db, user, answers=1, claude_usage=used,
                          prisma_seconds=(acoustic.get("duration_s") or 0) if spoken else 0)
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            if retry:
                raise HTTPException(409, "This answer was saved twice at once. Refresh the page.")

    timings["total_s"] = round(time.monotonic() - started, 2)
    log.info("attempt %s user %s timings %s claude usage %s", a.id, user.id, json.dumps(timings), json.dumps(used))
    return _attempt_reply(db, a, q, timings)


@app.get("/api/attempts/{attempt_id}")
def get_attempt(attempt_id: str, db: Session = Depends(get_session), user: User = Depends(current_user)):
    a, q, s = _owned_attempt(db, user, attempt_id)
    previous = db.scalar(select(Attempt).where(Attempt.question_id == q.id, Attempt.number == a.number - 1))
    out = _attempt_json(a, previous)
    out["question"] = {"id": q.id, "text": q.text, "why": q.why, "kind": q.kind, **_place(db, q)}
    out["session_id"], out["round_label"] = s.id, ROUNDS[s.round_type]
    follow = db.scalar(select(Question).where(Question.parent_id == q.id))
    out["follow_up_question_id"] = follow.id if follow else None
    return out


@app.get("/api/attempts/{attempt_id}/feedback-audio")
def feedback_audio(attempt_id: str, db: Session = Depends(get_session), user: User = Depends(current_user)):
    """Mother-tongue feedback spoken by a Timbre voice."""
    a, _, _ = _owned_attempt(db, user, attempt_id)
    voice = gnani.voice_for(a.feedback_language)
    if a.feedback_language == "en-IN":
        voice = gnani.ENGLISH_VOICE
    if not voice:
        raise HTTPException(404, f"No voice set up for {LANGUAGES.get(a.feedback_language)} yet; read it instead.")
    return _speech(db, user, f"{a.id}/feedback", a.native_feedback,
                   lambda: gnani.synthesize(a.native_feedback, a.feedback_language, voice))


@app.get("/api/attempts/{attempt_id}/model-answer-audio")
def model_answer_audio(attempt_id: str, db: Session = Depends(get_session), user: User = Depends(current_user)):
    """The suggested answer, spoken in the student's own cloned voice if they set one up."""
    a, _, _ = _owned_attempt(db, user, attempt_id)
    text = a.rewrite.replace("[", "").replace("]", "")
    clone = db.get(VoiceClone, user.id)
    if clone:
        voice_id = hashlib.sha256(clone.embedding.encode()).hexdigest()[:12]  # a new clone gets new audio
        embedding = json.loads(clone.embedding)
        return _speech(db, user, f"{a.id}/answer-{voice_id}", text, lambda: gnani.synthesize_clone(text, embedding))
    return _speech(db, user, f"{a.id}/answer-default", text,
                   lambda: gnani.synthesize(text, gnani.ENGLISH_LANGUAGE, gnani.ENGLISH_VOICE, speed=0.95))


@app.post("/api/voice-clone", status_code=201)
def create_voice_clone(sample: UploadFile = File(...), consent: bool = Form(False),
                       db: Session = Depends(get_session), user: User = Depends(current_user)):
    if not consent:
        raise HTTPException(422, "Please confirm this is your own voice and you agree to clone it.")
    data = sample.file.read(MAX_AUDIO_BYTES + 1)
    try:
        samples, rate = audio.read_wav(data)
    except audio.AudioError as exc:
        raise HTTPException(422, str(exc))
    seconds = len(samples) / rate
    if not 5 <= seconds <= 30:
        raise HTTPException(422, f"The sample must be 5 to 30 seconds long (yours was {seconds:.0f} s).")
    if not audio.analyse(samples, rate).get("speech_s"):
        raise HTTPException(422, "We couldn't hear any speech in that sample.")
    limits.check_daily(db, user, "voice_clones")
    limits.check_burst(user)
    db.commit()
    try:
        embedding = gnani.clone_embedding(data)
    except gnani.GnaniError as exc:
        raise HTTPException(503, str(exc))
    db.merge(VoiceClone(user_id=user.id, embedding=json.dumps(embedding)))
    limits.record(db, user, voice_clones=1)
    db.commit()
    tts_cache.delete_user(user.id)  # old clips were in the old voice
    return {"has_voice_clone": True}


@app.delete("/api/voice-clone")
def delete_voice_clone(db: Session = Depends(get_session), user: User = Depends(current_user)):
    clone = db.get(VoiceClone, user.id)
    if clone:
        db.delete(clone)
        db.commit()
    tts_cache.delete_user(user.id)  # cached answers spoken in the cloned voice go too
    return {"has_voice_clone": False}


@app.get("/api/progress")
def progress(db: Session = Depends(get_session), user: User = Depends(current_user)):
    rows = db.execute(
        select(Attempt, Question, PracticeSession)
        .join(Question, Attempt.question_id == Question.id)
        .join(PracticeSession, Question.session_id == PracticeSession.id)
        .where(PracticeSession.user_id == user.id)
        .order_by(Attempt.created_at)
    ).all()
    points = [{"attempt_id": a.id, "at": a.created_at.isoformat() + "Z", "question": q.text[:80],
               "session_id": s.id, "role": s.target_role, **{k: a.scores.get(k) for k in
                                                             ("overall", "content", "fluency", "delivery")},
               "fillers_per_min": a.language.get("fillers_per_min"), "wpm": a.language.get("wpm"),
               "english_share": a.language.get("english_share")} for a, q, s in rows]
    return {"points": points, "count": len(points)}


if settings.serve_web:  # local runs only; on Azure, Static Web Apps serves web/
    app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
