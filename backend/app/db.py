from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from .config import settings
from .models import Base

settings.prepare()
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args={"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def sqlite_pragmas(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")


def get_db():
    with SessionLocal() as db:
        yield db


def create_schema():
    if settings.env == "test":
        Base.metadata.create_all(engine)
        return
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    with engine.begin() as connection:
        # API and worker startup share one PostgreSQL migration lock.
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(473953001)"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
