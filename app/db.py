"""Database engine and schema setup. SQLite for local runs, Azure Database for PostgreSQL in the cloud."""
from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import ROOT, settings

# Named constraints, so Alembic can change them later on both SQLite and PostgreSQL.
NAMING = {"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
          "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s",
          "pk": "pk_%(table_name)s"}

if settings.database_url.startswith("sqlite"):
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
else:
    # 5 + 5 connections per replica, 3 replicas max: 30, well under the B1ms limit. pre_ping drops dead connections.
    engine = create_engine(settings.database_url, pool_size=5, max_overflow=5, pool_pre_ping=True,
                           pool_recycle=1800)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["engine"] = engine
    return cfg


def init_db() -> None:
    """Bring the schema up to date with Alembic migrations."""
    from . import models  # noqa: F401  (registers tables)

    if not is_postgres():
        return _migrate()
    # Several replicas can start at once: only one migrates, the others wait for it, then find nothing to do.
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK})
        try:
            _migrate()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK})
            conn.commit()


MIGRATION_LOCK = 20261029  # any fixed number; names the advisory lock held while migrating


def _migrate() -> None:
    from alembic import command

    tables = set(inspect(engine).get_table_names())
    cfg = _alembic_config()
    if "users" in tables and "alembic_version" not in tables:
        # A Phase 1 database made with create_all: add the one column added by hand back then, and record it as
        # the Phase 1 schema so only the later migrations run.
        if "interview_date" not in {c["name"] for c in inspect(engine).get_columns("users")}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN interview_date DATE"))
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
