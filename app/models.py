"""Database tables. Audio is never stored; resumes are stored only as redacted text.

Every row reaches its owner (users.id) in at most one join. Schema changes go through Alembic (migrations/).
"""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # Who signed in: the token's issuer + subject. "local" in local mode.
    auth_subject: Mapped[str | None] = mapped_column(String(300), unique=True, nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    feedback_language: Mapped[str] = mapped_column(String(8), default="hi-IN")
    interview_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    busy_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # one answer at a time
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PracticeSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    round_type: Mapped[str] = mapped_column(String(16))
    target_role: Mapped[str] = mapped_column(String(120), default="")
    resume_text: Mapped[str] = mapped_column(Text)  # phone numbers, emails and links removed
    jd_text: Mapped[str] = mapped_column(Text)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)  # skills, claims, requirements, match
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(12), default="main")  # main|follow_up
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("question_id", "number", name="uq_attempts_question_number"),
                      UniqueConstraint("user_id", "idempotency_key", name="uq_attempts_user_key"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    question_id: Mapped[str] = mapped_column(ForeignKey("questions.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)  # for progress queries
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    number: Mapped[int] = mapped_column(Integer)
    transcript: Mapped[str] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    acoustics: Mapped[dict] = mapped_column(JSON, default=dict)  # loudness, pauses, pitch, trailing off
    language: Mapped[dict] = mapped_column(JSON, default=dict)  # fillers, Indian-English fixes, wpm
    scores: Mapped[dict] = mapped_column(JSON, default=dict)  # delivery, fluency, content, overall
    content: Mapped[dict] = mapped_column(JSON, default=dict)  # rubric with reasons
    rewrite: Mapped[str] = mapped_column(Text, default="")
    tips: Mapped[list] = mapped_column(JSON, default=list)
    native_feedback: Mapped[str] = mapped_column(Text, default="")
    feedback_language: Mapped[str] = mapped_column(String(8), default="hi-IN")
    notes: Mapped[list] = mapped_column(JSON, default=list)  # e.g. "content scoring unavailable"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VoiceClone(Base):
    __tablename__ = "voice_clones"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    embedding: Mapped[str] = mapped_column(Text)  # what Gnani returns for the cloned voice
    consent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UsageDaily(Base):
    """What one student used on one day (UTC). Drives the per-day limits. Kept 90 days."""
    __tablename__ = "usage_daily"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    answers: Mapped[int] = mapped_column(Integer, default=0)
    sessions: Mapped[int] = mapped_column(Integer, default=0)
    audio_plays: Mapped[int] = mapped_column(Integer, default=0)
    voice_clones: Mapped[int] = mapped_column(Integer, default=0)
    claude_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    claude_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    prisma_seconds: Mapped[float] = mapped_column(Float, default=0)


class AppBudget(Base):
    """What the whole app spent in one calendar month (UTC), e.g. "2026-11". Drives the monthly budget."""
    __tablename__ = "app_budget"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    claude_usd: Mapped[float] = mapped_column(Float, default=0)
    prisma_seconds: Mapped[float] = mapped_column(Float, default=0)
    timbre_chars: Mapped[int] = mapped_column(Integer, default=0)
