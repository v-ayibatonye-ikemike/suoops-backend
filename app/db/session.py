"""Database engine setup.

For test runs (ENV=test) we intentionally fallback to synchronous in-memory SQLite
when DATABASE_URL is unset or explicitly requested via SUOOPS_TEST_SQLITE=1.
This avoids needing a PostgreSQL driver (psycopg2/pg_config) for logic tests.
"""

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

raw_url = settings.DATABASE_URL

use_sqlite_memory = settings.ENV.lower() == "test" and (
    os.getenv("SUOOPS_TEST_SQLITE") == "1" or not raw_url or raw_url.startswith("sqlite+aiosqlite:///:memory:")
)

if use_sqlite_memory:
    # Use a transient in-memory database; persistence not required for unit tests.
    raw_url = "sqlite:///file:test_db?mode=memory&cache=shared"  # shared cache enables multiple connections
    engine = create_engine(raw_url, future=True)
else:
    # If URL is PostgreSQL but driver not installed (tests), gracefully downgrade to file-based sqlite
    if raw_url and raw_url.startswith("postgresql"):
        try:
            # Configure connection pool for production PostgreSQL
            # Render Starter: 512MB total (OS ~100MB, app ~300MB available)
            # Each PG connection ~25-30MB overhead → cap at 13 total
            engine = create_engine(
                raw_url,
                future=True,
                pool_size=5,  # Base pool (web + celery share)
                max_overflow=8,  # Burst to 13 total (5 + 8)
                pool_recycle=1800,  # Recycle connections every 30 min to prevent stale
                pool_pre_ping=True,  # Verify connection health before use
            )
        except ModuleNotFoundError:
            fallback_url = "sqlite:///./storage/test_fallback.db"
            engine = create_engine(fallback_url, future=True)
    else:
        engine = create_engine(raw_url, future=True)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
