from fastapi.testclient import TestClient

from app.api.main import app

client = TestClient(app)


def test_telemetry_ingestion_success():
    payload = {
        "type": "oauth_start",
        "ts": "2025-11-10T12:00:00Z",
        "trace_id": "abc123",
        "detail": {"codePresent": True},
    }
    r = client.post("/telemetry/frontend", json=payload)
    assert r.status_code == 200 or r.status_code == 202
    data = r.json()
    assert data.get("status") == "accepted"

    # Scrape metrics to ensure counter incremented
    m = client.get("/metrics")
    assert m.status_code == 200
    metrics_text = m.text
    assert "suoops_telemetry_events_total" in metrics_text
    # Optionally check that an oauth_start label is present
    assert 'type="oauth_start"' in metrics_text


def test_telemetry_ingestion_missing_type():
    payload = {
        "ts": "2025-11-10T12:00:00Z",
    }
    r = client.post("/telemetry/frontend", json=payload)
    assert r.status_code == 422  # Pydantic validation returns 422
    # Validation errors may be in "detail" key
    assert "detail" in r.json()
