"""Phase 2 (Azure): signed-in users, per-user attempts, idempotent answers, usage counters and the app budget.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Lets batch mode on SQLite name the unnamed UNIQUE(email) that Phase 1's create_all made, so it can be dropped.
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    with op.batch_alter_table("users", naming_convention=NAMING) as users:
        users.add_column(sa.Column("auth_subject", sa.String(300), nullable=True))
        users.add_column(sa.Column("busy_until", sa.DateTime, nullable=True))
        users.add_column(sa.Column("last_seen_at", sa.DateTime, nullable=True))
        # Email now comes from the sign-in token and may be missing or shared by two sign-in methods.
        users.drop_constraint("uq_users_email", type_="unique")
        users.alter_column("email", existing_type=sa.String(200), nullable=True)
        users.create_unique_constraint("uq_users_auth_subject", ["auth_subject"])
    op.execute("UPDATE users SET auth_subject = 'local' WHERE email = 'local@bol.app'")

    with op.batch_alter_table("attempts") as attempts:
        attempts.add_column(sa.Column("user_id", sa.String(32), nullable=True))
        attempts.add_column(sa.Column("idempotency_key", sa.String(64), nullable=True))
        attempts.create_index("ix_attempts_user_id", ["user_id"])
        attempts.create_unique_constraint("uq_attempts_question_number", ["question_id", "number"])
        attempts.create_unique_constraint("uq_attempts_user_key", ["user_id", "idempotency_key"])
    op.execute("UPDATE attempts SET user_id = (SELECT s.user_id FROM questions q JOIN sessions s "
               "ON s.id = q.session_id WHERE q.id = attempts.question_id)")

    op.create_table(
        "usage_daily",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", name="fk_usage_daily_user_id"),
                  primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("answers", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sessions", sa.Integer, nullable=False, server_default="0"),
        sa.Column("audio_plays", sa.Integer, nullable=False, server_default="0"),
        sa.Column("voice_clones", sa.Integer, nullable=False, server_default="0"),
        sa.Column("claude_input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("claude_output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("prisma_seconds", sa.Float, nullable=False, server_default="0"),
    )
    op.create_table(
        "app_budget",
        sa.Column("month", sa.String(7), primary_key=True),
        sa.Column("claude_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("prisma_seconds", sa.Float, nullable=False, server_default="0"),
        sa.Column("timbre_chars", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("app_budget")
    op.drop_table("usage_daily")
    with op.batch_alter_table("attempts") as attempts:
        attempts.drop_constraint("uq_attempts_user_key", type_="unique")
        attempts.drop_constraint("uq_attempts_question_number", type_="unique")
        attempts.drop_index("ix_attempts_user_id")
        attempts.drop_column("idempotency_key")
        attempts.drop_column("user_id")
    with op.batch_alter_table("users") as users:
        users.drop_constraint("uq_users_auth_subject", type_="unique")
        users.drop_column("last_seen_at")
        users.drop_column("busy_until")
        users.drop_column("auth_subject")
