from fastapi import APIRouter, Response

try:  # pragma: no cover
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    _PROM_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PROM_AVAILABLE = False

router = APIRouter()


@router.get("/metrics")
def metrics_endpoint() -> Response:
    """
    Prometheus metrics endpoint.

    Public endpoint (no auth required) so monitoring tools can scrape metrics.
    Exposes application performance metrics for Grafana/CloudWatch/etc.
    """
    if not _PROM_AVAILABLE:
        return Response("prometheus client not installed", media_type="text/plain", status_code=503)
    # Native counters are incremented at event points; just expose registry.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
