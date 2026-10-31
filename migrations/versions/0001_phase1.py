"""Phase 1 schema (single local user, SQLite).

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("feedback_language", sa.String(8), nullable=False),
        sa.Column("interview_date", sa.Date, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", name="fk_sessions_user_id"), nullable=False),
        sa.Column("round_type", sa.String(16), nullable=False),
        sa.Column("target_role", sa.String(120), nullable=False),
        sa.Column("resume_text", sa.Text, nullable=False),
        sa.Column("jd_text", sa.Text, nullable=False),
        sa.Column("profile", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_table(
        "questions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("sessions.id", name="fk_questions_session_id"),
                  nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("why", sa.Text, nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("parent_id", sa.String(32), nullable=True),
    )
    op.create_index("ix_questions_session_id", "questions", ["session_id"])
    op.create_table(
        "attempts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("question_id", sa.String(32), sa.ForeignKey("questions.id", name="fk_attempts_question_id"),
                  nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("transcript", sa.Text, nullable=False),
        sa.Column("duration_s", sa.Float, nullable=True),
        sa.Column("acoustics", sa.JSON, nullable=False),
        sa.Column("language", sa.JSON, nullable=False),
        sa.Column("scores", sa.JSON, nullable=False),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("rewrite", sa.Text, nullable=False),
        sa.Column("tips", sa.JSON, nullable=False),
        sa.Column("native_feedback", sa.Text, nullable=False),
        sa.Column("feedback_language", sa.String(8), nullable=False),
        sa.Column("notes", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_attempts_question_id", "attempts", ["question_id"])
    op.create_table(
        "voice_clones",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", name="fk_voice_clones_user_id"),
                  primary_key=True),
        sa.Column("embedding", sa.Text, nullable=False),
        sa.Column("consent_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    for table in ("voice_clones", "attempts", "questions", "sessions", "users"):
        op.drop_table(table)
