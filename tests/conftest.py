from __future__ import annotations

import os
import sys
import warnings
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

# Ensure project root (suoops-backend) is on sys.path for 'app' imports when running tests directly.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Keep the suite hermetic: default the OTP store + rate limiter to their in-memory
# fallbacks so tests never need a live Redis. A local .env can set REDIS_URL, which
# pydantic would otherwise apply and push the OTP store onto a real Redis (failing
# without one running). Env vars outrank .env, and setdefault respects an explicit
# REDIS_URL if a dev really wants to test against Redis. Must run before app import.
os.environ.setdefault("REDIS_URL", "")

app = import_module("app.api.main").app
settings = import_module("app.core.config").settings
db_session = import_module("app.db.session")
Base = import_module("app.db.base_class").Base
SessionLocal = db_session.SessionLocal

try:
    WebhookEvent = import_module("app.models.models").WebhookEvent
except ImportError:  # pragma: no cover - legacy tables may be removed
    WebhookEvent = None

# --- WhatsApp send patching ---
try:
    from app.bot.whatsapp_client import WhatsAppClient
except Exception:  # pragma: no cover - if module path changes
    WhatsAppClient = None  # type: ignore


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite:///:memory:")


test_engine = create_engine(
    TEST_DATABASE_URL,
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

# Ensure application code uses the test engine
settings.DATABASE_URL = TEST_DATABASE_URL  # type: ignore[attr-defined]
settings.ENV = "test"  # type: ignore[attr-defined]
# Use a 32+ char secret so JWT (HS256) token operations during tests don't emit
# pyjwt's InsecureKeyLengthWarning from the short dev secret in .env.
settings.JWT_SECRET = "test-jwt-secret-key-min-32-characters-long"  # type: ignore[attr-defined]
db_session.engine = test_engine  # type: ignore[assignment]
SessionLocal.configure(bind=test_engine)

# Suppress third-party utcnow deprecation chatter (botocore) until upstream fixes.
warnings.filterwarnings(
    "ignore",
    message=r".*datetime\.datetime\.utcnow\(\) is deprecated.*",
    module="botocore.auth",
)


@pytest.fixture(scope="session", autouse=True)
def _setup_database_schema():
    """Create all tables once for the test session and drop afterwards."""
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(autouse=True)
def _reset_database_state():
    """Ensure each test sees a fresh database schema."""
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield


@pytest.fixture(autouse=True)
def _reset_webhook_events():
    """Ensure webhook idempotency table doesn't leak state between tests."""
    session = SessionLocal()
    try:
        if WebhookEvent is not None:
            session.query(WebhookEvent).delete()
            session.commit()
        yield
        if WebhookEvent is not None:
            session.query(WebhookEvent).delete()
            session.commit()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _patch_whatsapp_send(monkeypatch):
    """Prevent real WhatsApp HTTP calls & noisy logs during tests.

    Replaces WhatsAppClient.send_text with a lightweight recorder that stores
    calls on the function object (for assertion if needed) without network activity.
    """
    if WhatsAppClient is None:  # pragma: no cover - safety
        return

    calls: list[tuple[str, str]] = []

    def fake_send_text(self, to: str, body: str):  # noqa: D401 - simple test double
        calls.append((to, body))

    monkeypatch.setattr(WhatsAppClient, "send_text", fake_send_text, raising=True)
    yield SimpleNamespace(calls=calls)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset the slowapi rate limiter between tests.

    All tests share the same TestClient IP (127.0.0.1) and the same in-memory
    storage, so per-IP quotas accumulate across tests and eventually return 429
    for endpoints like /auth/signup/request (rate-limited at a few/min). This
    manifests as flaky assertion failures in tests that call OTP/auth helpers
    late in the suite. Clearing storage before each test isolates them.
    """
    try:
        from app.api.rate_limit import limiter

        storage = getattr(limiter, "_storage", None)
        if storage is not None and hasattr(storage, "reset"):
            storage.reset()
    except Exception:
        pass
    yield


@pytest.fixture
def db_session():
    """Provide a transactional database session for tests."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    finally:
        session.close()


# FastAPI TestClient fixture expected by some tests (e.g., invoice verification)


@pytest.fixture
def client():  # noqa: D401 - simple factory fixture
    """Provide a FastAPI TestClient bound to the application."""
    return TestClient(app)
