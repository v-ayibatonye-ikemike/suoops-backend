import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.rate_limit import limiter
from app.core.config import settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover
    from prometheus_client import Counter

    _TELEMETRY_COUNTER = Counter(
        "suoops_telemetry_events_total",
        "Total telemetry events received",
        labelnames=["type"],
    )
except Exception:  # noqa: BLE001
    _TELEMETRY_COUNTER = None


class TelemetryIn(BaseModel):
    """Telemetry event payload from frontend."""

    type: str = Field(min_length=1, max_length=100)
    ts: str
    trace_id: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


class TelemetryAck(BaseModel):
    status: str


router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.options("/frontend")
def telemetry_options():
    """Handle CORS preflight for telemetry endpoint."""
    return {"status": "ok"}


@router.post("/frontend", response_model=TelemetryAck, status_code=202)
@limiter.limit("120/minute")  # IP-based limit to mitigate abuse
def ingest_frontend(event: TelemetryIn, request: Request):  # noqa: D401
    """Ingest a frontend telemetry event.

    Validates basic structure; increments Prometheus counter; persists to log.
    Returns 202 on success.
    """
    if not event.type:
        raise HTTPException(status_code=400, detail="Missing event type")
    # Optional API key validation in production (log warning if invalid)
    if settings.ENV.lower() == "prod":
        header_key = request.headers.get("X-Telemetry-Key")
        if header_key and header_key != settings.TELEMETRY_INGEST_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")
        # Allow telemetry without key (rate limiting provides abuse protection)
    if _TELEMETRY_COUNTER:
        try:
            _TELEMETRY_COUNTER.labels(type=event.type).inc()
        except Exception:  # noqa: BLE001
            pass
    logger.info(
        "telemetry event_type=%s trace=%s ts=%s",
        event.type,
        event.trace_id,
        event.ts,
    )
    return {"status": "accepted"}
