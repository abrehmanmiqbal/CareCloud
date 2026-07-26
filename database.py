"""
Database layer
--------------
Uses DATABASE_URL if it's set (recommended in production: a free managed
Postgres instance, e.g. Neon / Supabase / Railway) so patient data survives
redeploys and container restarts even on hosts with an ephemeral local disk.

Falls back to a local SQLite file (carecloud.db) with zero setup, which is
perfectly fine for local development and for hosts that do give you a
persistent disk.

Why this matters: the assessment requires data to survive server restarts.
SQLite alone only guarantees that if the *file* survives restarts, which
depends on the host. Making DATABASE_URL swappable removes that risk without
changing a single line of business logic — same models, same queries.
"""
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

BASE_DIR = Path(__file__).parent
DEFAULT_SQLITE_URL = f"sqlite:///{BASE_DIR / 'carecloud.db'}"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_SQLITE_URL)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a request-scoped session, always closed after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    import models  # noqa: F401 — ensures the Patient model is registered on Base
    Base.metadata.create_all(bind=engine)
