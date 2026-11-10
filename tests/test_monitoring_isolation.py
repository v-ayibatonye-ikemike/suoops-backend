import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core import monitoring
from app.core.config import settings


@pytest.mark.parametrize("environment", ["test", "testing"])
def test_monitoring_does_not_initialize_sentry_in_tests(monkeypatch, environment):
    import sentry_sdk

    initialize = Mock()
    monkeypatch.setattr(sentry_sdk, "init", initialize)
    monkeypatch.setattr(settings, "ENV", environment)
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://test-public@ingest.example.test/1")
    monkeypatch.setattr(monitoring, "_initialized", False)

    monitoring.init_monitoring()

    initialize.assert_not_called()


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_monitoring_still_initializes_once_outside_tests(monkeypatch, environment):
    import sentry_sdk

    initialize = Mock()
    monkeypatch.setattr(sentry_sdk, "init", initialize)
    monkeypatch.setattr(settings, "ENV", environment)
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://test-public@ingest.example.test/1")
    monkeypatch.setattr(monitoring, "_initialized", False)

    monitoring.init_monitoring()
    monitoring.init_monitoring()

    initialize.assert_called_once()
    assert initialize.call_args.kwargs["environment"] == environment


def test_bootstrap_disables_telemetry_and_default_redis_before_app_import(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / ".env").write_text(
        "REDIS_URL=redis://localhost:6379/0\nSENTRY_DSN=https://test-public@ingest.example.test/1\n"
    )
    environment = os.environ.copy()
    environment.update({"APP_ENV": "dev", "ENV": "dev", "SENTRY_DSN": "https://test-public@ingest.example.test/1"})
    environment.pop("REDIS_URL", None)
    script = f"""
import os
import runpy
from unittest.mock import Mock, patch
import sentry_sdk
sentry_sdk.init = Mock()
runpy.run_path({str(root / 'tests' / 'conftest.py')!r})
from app.core.config import settings
from app.db.session import SessionLocal
from app.services.invoice_service import build_invoice_service
assert os.environ["APP_ENV"] == "test"
assert os.environ["ENV"] == "test"
assert os.environ["SENTRY_DSN"] == ""
assert settings.SENTRY_DSN == ""
assert settings.REDIS_URL == ""
sentry_sdk.init.assert_not_called()
with patch("app.storage.s3_client.S3Client"), patch("app.services.pdf_service.PDFService"), patch(
    "app.db.redis_client.get_redis_client", side_effect=AssertionError("Tests must not connect to Redis")
) as redis_client:
    with SessionLocal() as db:
        assert build_invoice_service(db).cache is None
    redis_client.assert_not_called()
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
