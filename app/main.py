"""Bol API. Run: uvicorn app.main:app --reload --port 8020

Stores nothing. The browser keeps the student's sessions, answers and voice in its own storage and sends what each
request needs; the server does the work in memory and forgets it. There is no sign-in, no database and no file
storage. Recordings, resumes and transcripts never touch a disk.

Texts the server writes (questions, feedback, better answers) come back with a signature. /api/speak only reads
aloud signed texts, so nobody can use Bol as a free text-to-speech service on our Gnani credits.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import audio, claude, factcheck, gnani, language, limits, pdf, pii, scoring
from .config import LANGUAGES, ROUNDS, settings

log = logging.getLogger("bol")
MAX_AUDIO_BYTES = 8 * 1024 * 1024  # 3 min of 16 kHz mono 16-bit is ~5.8 MB

if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    # Request timings, errors and the "bol" logs go to Application Insights. Logs never carry transcripts,
    # resume text or voice data. Must run before FastAPI() so requests are traced.
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(logger_name="bol")
    log.setLevel(logging.INFO)

app = FastAPI(title="Bol")


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


def _client(request: Request) -> str:
    return limits.client_ip(request.headers, request.client.host if request.client else None)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Every request: refuse huge bodies and floods from one address, then add security headers.

    Registered before CORS, so CORS wraps it and the browser can read these refusals too.
    """
    path = request.url.path
    if path.startswith("/api/") and path != "/api/health":  # health is polled by Azure's probes
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > settings.max_request_mb * 1024 * 1024:
            return JSONResponse({"detail": "That upload is too large."}, status_code=413)
        wait = limits.per_ip.hit(_client(request), settings.limits.requests_per_minute_per_ip)
        if wait:
            return JSONResponse({"detail": f"Too many requests. Wait {wait} seconds and try again."},
                                status_code=429, headers={"Retry-After": str(wait)})
    response = await call_next(request)
    for name, value in (API_HEADERS if path.startswith("/api/") else WEB_HEADERS).items():
        response.headers.setdefault(name, value)
    return response


if settings.cors_origins:  # the Static Web Apps site calls the API from another origin
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Idempotency-Key"], max_age=600)


# ---------- signatures on texts Bol may read aloud ----------

def sign(kind: str, lang: str, text: str) -> str:
    return hmac.new(settings.signing_key, f"{kind}|{lang}|{text}".encode(), hashlib.sha256).hexdigest()[:32]


def _check_signature(kind: str, lang: str, text: str, sig: str) -> None:
    if not hmac.compare_digest(sign(kind, lang, text), sig or ""):
        raise HTTPException(403, "Bol only reads aloud the texts it wrote. Reload the page and try again.")


# ---------- routes ----------

@app.get("/api/health")
def health():
    """Liveness probe for Container Apps."""
    return {"ok": True, "claude": settings.claude_model if settings.claude_enabled else None,
            "speech": gnani.enabled(), "stores_data": False}


@app.get("/api/config")
def config():
    """What the browser needs to draw the app: languages, rounds, and which services are on or paused."""
    return {"languages": LANGUAGES, "rounds": ROUNDS, "claude": settings.claude_enabled,
            "claude_model": settings.claude_model, "speech": gnani.enabled(),
            "claude_paused": limits.budget.claude_paused(), "speech_paused": limits.budget.speech_paused(),
            "voices_paused": limits.budget.voices_paused(), "feedback_voices": sorted(gnani.FEEDBACK_VOICES),
            "interviewer_voice": gnani.ENGLISH_VOICE, "max_typed_chars": settings.max_typed_chars}


@app.post("/api/prepare")
def prepare(request: Request, jd_text: str = Form(...), round_type: str = Form("hr"), target_role: str = Form(""),
            resume_text: str = Form(""), resume_file: UploadFile | None = File(None)):
    """Read a resume and job description; return the cleaned texts, the profile and 5 questions."""
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
    limits.check_heavy(_client(request))
    limits.check_daily(_client(request), "sessions")

    notes = []
    usage = claude.Usage()
    try:
        if limits.budget.claude_paused():
            raise claude.LLMError("this month's coaching budget is used up")
        prep = claude.prepare(resume_text, jd_text, round_type, target_role, usage=usage)
        source = "claude"
    except claude.LLMError as exc:
        log.warning("prepare fell back to stand-in: %s", exc)
        prep = claude.stub_prepare(resume_text, jd_text, round_type, target_role)
        source = "stand-in"
        if settings.claude_enabled:
            notes.append(f"Claude was unavailable ({exc}), so these are template questions.")
    used = usage.as_dict()
    limits.budget.add(claude_usd=used["usd"] or 0)
    log.info("session prepared, claude usage %s", json.dumps(used))

    profile = prep.model_dump(exclude={"questions"})
    profile.update(source=source, notes=notes, usage=used)
    return {"round_type": round_type, "round_label": ROUNDS[round_type],
            "target_role": (target_role or prep.target_role)[:120], "first_name": prep.first_name,
            "resume_text": resume_text, "jd_text": jd_text, "profile": profile,
            "questions": [{"text": q.text, "why": q.why, "sig": sign("question", "en-IN", q.text)}
                          for q in prep.questions]}


class AnswerContext(BaseModel):
    """What the browser sends with an answer: the question and the session it belongs to."""
    question: str = Field(min_length=1, max_length=400)
    why: str = Field("", max_length=400)
    kind: Literal["main", "follow_up"] = "main"
    round_type: str
    target_role: str = Field("", max_length=200)
    requirements: list[str] = Field(default_factory=list, max_length=20)
    skills: list[str] = Field(default_factory=list, max_length=40)
    resume_text: str = Field(min_length=1, max_length=16000)
    feedback_language: str = "hi-IN"


def _clean_key(value: str | None) -> str | None:
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,64}", value):
        raise HTTPException(422, "Idempotency-Key must be 8-64 letters, digits, - or _.")
    return value


@app.post("/api/score")
def score(request: Request, context: str = Form(...), audio_file: UploadFile | None = File(None),
          text: str = Form(""), idempotency_key: str | None = Header(None)):
    """One answer turn: transcribe (if spoken), measure, coach. A retried upload with the same Idempotency-Key
    gets the first result instead of being scored (and paid for) twice."""
    try:
        ctx = AnswerContext.model_validate_json(context)
    except ValidationError:
        raise HTTPException(422, "The question details sent with the answer are incomplete. Reload the page.")
    if ctx.round_type not in ROUNDS or ctx.feedback_language not in LANGUAGES:
        raise HTTPException(422, "Unknown round or feedback language.")
    client = _client(request)
    key = _clean_key(idempotency_key)
    if key and (done := limits.replies.get(f"{client}:{key}")):
        return done
    limits.check_heavy(client)
    limits.check_daily(client, "answers")
    reply = _score_answer(ctx, audio_file, text)
    if key:
        limits.replies.put(f"{client}:{key}", reply)
    return reply


def _score_answer(ctx: AnswerContext, audio_file: UploadFile | None, text: str) -> dict:
    started = time.monotonic()
    notes: list[str] = []
    acoustic: dict = {}
    timings: dict = {}
    resume = pii.redact(ctx.resume_text)[:15000]
    skills = [s[:40] for s in ctx.skills]
    requirements = [r[:120] for r in ctx.requirements]

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
        if limits.budget.speech_paused():
            raise HTTPException(503, "Speech-to-text is paused for this month. Type your answer instead.")
        t = time.monotonic()
        try:
            transcript = gnani.transcribe_answer(samples, rate, bias=gnani.bias_words(skills))
        except gnani.GnaniError as exc:
            log.warning("transcription failed: %s", exc)
            raise HTTPException(502, "Speech-to-text failed. Please try again, or type your answer.")
        timings["stt_s"] = round(time.monotonic() - t, 2)
        limits.budget.add(prisma_seconds=acoustic.get("duration_s") or seconds)
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

    eval_args = dict(question=ctx.question, why=ctx.why, round_type=ctx.round_type, role=ctx.target_role,
                     requirements=requirements, resume=resume, answer=transcript,
                     speech_notes=speech_notes, feedback_language=ctx.feedback_language)
    t = time.monotonic()
    usage = claude.Usage()
    unverified, source = [], "stand-in"
    try:
        if limits.budget.claude_paused():
            raise claude.LLMError("this month's coaching budget is used up")
        ev = claude.evaluate(**eval_args, usage=usage)
        source = "claude"
        missing = factcheck.unsupported(ev.rewrite, resume, transcript, ctx.question)
        if missing:
            try:
                ev.rewrite = claude.repair(ev.rewrite, missing, resume, transcript, usage=usage)
                unverified = factcheck.unsupported(ev.rewrite, resume, transcript, ctx.question)
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
    used = usage.as_dict()
    limits.budget.add(claude_usd=used["usd"] or 0)
    rubric = {k: getattr(ev, k).model_dump() for k in ("answered", "structure", "specifics", "jd_fit")}
    if unverified:
        notes.append("Check the suggested answer: we couldn't find " + ", ".join(unverified[:5]) +
                     " in your resume or answer. Only say it if it's true.")

    parts = {"content": scoring.content(rubric), "fluency": fluency_score, "delivery": delivery_score}
    follow_up = ev.follow_up if ctx.kind == "main" else ""
    timings["total_s"] = round(time.monotonic() - started, 2)
    log.info("answer scored, timings %s claude usage %s", json.dumps(timings), json.dumps(used))
    return {
        "transcript": transcript, "duration_s": acoustic.get("duration_s"), "acoustics": acoustic,
        "language": {**lang, "delivery_notes": delivery_notes, "fluency_notes": fluency_notes},
        "scores": {**parts, "overall": scoring.overall(parts), "content_source": source},
        "content": {**rubric, "strength": ev.strength, "follow_up": follow_up, "unverified": unverified,
                    "usage": used},
        "rewrite": ev.rewrite, "tips": ev.tips[:3], "native_feedback": ev.native_feedback,
        "feedback_language": ctx.feedback_language, "notes": notes, "follow_up": follow_up,
        "signatures": {"feedback": sign("feedback", ctx.feedback_language, ev.native_feedback),
                       "answer": sign("answer", "en-IN", ev.rewrite),
                       "follow_up": sign("question", "en-IN", follow_up) if follow_up else ""},
        "timings": timings,
    }


class SpeakRequest(BaseModel):
    kind: Literal["question", "feedback", "answer"]
    text: str = Field(min_length=1, max_length=1600)
    language: str = "en-IN"
    sig: str = Field(max_length=64)
    embedding: dict | None = None  # the student's cloned voice, for kind "answer"


@app.post("/api/speak")
def speak(body: SpeakRequest, request: Request):
    """Read a Bol-written text aloud: a question (interviewer voice), feedback (the student's language) or the
    better answer (the student's cloned voice when they send it, else the interviewer voice)."""
    _check_signature(body.kind, body.language, body.text, body.sig)
    if not gnani.enabled():
        raise HTTPException(503, "Voices are off (VACHANA_API_KEY not set). Read the text instead.")
    if limits.budget.voices_paused():
        raise HTTPException(503, "Voices are paused for this month. Read the text instead.")
    limits.check_heavy(_client(request))
    limits.check_daily(_client(request), "speech")
    text = body.text
    try:
        if body.kind == "question":
            data = gnani.synthesize(text, gnani.ENGLISH_LANGUAGE, gnani.ENGLISH_VOICE, speed=0.95)
        elif body.kind == "feedback":
            voice = gnani.ENGLISH_VOICE if body.language == "en-IN" else gnani.voice_for(body.language)
            if not voice:
                raise HTTPException(404, f"No voice set up for {LANGUAGES.get(body.language)} yet; read it instead.")
            data = gnani.synthesize(text, body.language, voice)
        else:
            text = text.replace("[", "").replace("]", "")
            if body.embedding and body.embedding.get("embedding"):
                if len(json.dumps(body.embedding)) > 500_000:
                    raise HTTPException(413, "That voice data is too large. Set up your voice again.")
                data = gnani.synthesize_clone(text, body.embedding)
            else:
                data = gnani.synthesize(text, gnani.ENGLISH_LANGUAGE, gnani.ENGLISH_VOICE, speed=0.95)
    except gnani.GnaniError as exc:
        raise HTTPException(503, str(exc))
    limits.budget.add(timbre_chars=len(text))
    return Response(content=data, media_type="audio/wav")


@app.post("/api/voice-clone", status_code=201)
def create_voice_clone(request: Request, sample: UploadFile = File(...), consent: bool = Form(False)):
    """Turn 5-30 s of the student's own voice into voice data. It goes back to the browser; Bol keeps no copy."""
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
    limits.check_heavy(_client(request))
    limits.check_daily(_client(request), "voice_clones")
    try:
        embedding = gnani.clone_embedding(data)
    except gnani.GnaniError as exc:
        raise HTTPException(503, str(exc))
    return {"embedding": embedding}


if settings.serve_web:  # local runs only; on Azure, Static Web Apps serves web/
    app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
