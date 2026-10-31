"""Alembic environment: uses the app's own engine and tables."""
import sys
from pathlib import Path

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402,F401  (registers tables)
from app.db import Base, engine as app_engine  # noqa: E402

config = context.config
engine = config.attributes.get("engine", app_engine)


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata,
                          render_as_batch=connection.dialect.name == "sqlite")  # SQLite can't ALTER much
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


run_migrations_online()
