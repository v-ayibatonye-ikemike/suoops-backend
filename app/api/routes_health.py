from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.storage.s3_client import s3_client
from app.workers.celery_app import celery_app

router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    status: str


class ReadyOut(BaseModel):
    status: str
    db: bool
    redis: bool
    s3: bool
    celery: bool
    latency_ms: int


def _check_db(db: Session) -> bool:
    db.execute(text("SELECT 1"))
    return True


def _check_redis() -> bool:
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        return r.ping()
    except Exception:  # noqa: BLE001
        return False


def _check_s3() -> bool:
    try:
        # Perform a lightweight bucket head via internal client if available
        client = getattr(s3_client, "_client", None)
        if client is None:
            return True  # filesystem fallback considered healthy
        client.head_bucket(Bucket=s3_client.bucket)
        return True
    except Exception:  # noqa: BLE001
        return False


def _check_celery() -> bool:
    try:
        insp = celery_app.control.inspect(timeout=1)
        active = insp.active() if insp else None
        return bool(active)
    except Exception:  # noqa: BLE001
        return False


@router.get("/healthz", response_model=HealthOut)
def healthz(db: Annotated[Session, Depends(get_db)]) -> dict[str, str]:
    """Basic liveness probe (cheap)."""
    try:
        _check_db(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Database connectivity check failed") from exc
    return {"status": "ok"}


@router.get("/live", response_model=HealthOut)
def live() -> dict[str, str]:
    """Kubernetes-style liveness endpoint (no dependencies)."""
    return {"status": "alive"}


@router.get("/ready", response_model=ReadyOut)
def ready(db: Annotated[Session, Depends(get_db)]) -> dict[str, object]:
    """Readiness probe aggregating critical dependencies."""
    start = time.time()
    db_ok = redis_ok = s3_ok = celery_ok = False
    try:
        db_ok = _check_db(db)
    except Exception:  # noqa: BLE001
        db_ok = False
    redis_ok = _check_redis()
    s3_ok = _check_s3()
    celery_ok = _check_celery()
    duration_ms = int((time.time() - start) * 1000)
    overall = db_ok and redis_ok and s3_ok and celery_ok
    if not overall:
        raise HTTPException(
            status_code=503,
            detail={
                "db": db_ok,
                "redis": redis_ok,
                "s3": s3_ok,
                "celery": celery_ok,
                "latency_ms": duration_ms,
            },
        )
    return {
        "status": "ready",
        "db": db_ok,
        "redis": redis_ok,
        "s3": s3_ok,
        "celery": celery_ok,
        "latency_ms": duration_ms,
    }


@router.get("/sentry-debug")
def trigger_error():
    """Test endpoint to verify Sentry integration."""
    raise ZeroDivisionError("Sentry test error")


_ROBOTS_TXT = """\
User-agent: *
Disallow: /
"""


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    """Block all crawlers from the API domain."""
    return _ROBOTS_TXT
