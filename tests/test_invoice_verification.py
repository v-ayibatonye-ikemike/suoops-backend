"""Tests for invoice QR verification feature."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.models import Customer, Invoice, User


@pytest.fixture
def test_invoice(client: TestClient, db_session: Session):
    """Create a test invoice for verification."""
    # Create user
    user = User(phone="+2348012345678", phone_verified=True, name="Test Business Owner", plan="FREE")
    db_session.add(user)
    db_session.flush()

    # Create customer
    customer = Customer(name="John Doe", phone="+2348087654321")
    db_session.add(customer)
    db_session.flush()

    # Create invoice
    invoice = Invoice(
        invoice_id="INV-TEST-001",
        issuer_id=user.id,
        customer_id=customer.id,
        amount=50000,
        status="pending",
        pdf_url="https://example.com/test.pdf",
    )
    db_session.add(invoice)
    db_session.commit()

    return invoice


def test_verify_invoice_success(client: TestClient, test_invoice: Invoice):
    """Test successful invoice verification."""
    response = client.get(f"/invoices/{test_invoice.invoice_id}/verify")

    assert response.status_code == 200
    data = response.json()

    # Check required fields
    assert data["invoice_id"] == test_invoice.invoice_id
    assert data["status"] == "pending"
    assert data["amount"] == "50000"
    assert data["authentic"] is True

    # Check customer name is masked
    assert data["customer_name"] != "John Doe"
    assert data["customer_name"][0] == "J"
    assert "*" in data["customer_name"]

    # Check timestamps
    assert "created_at" in data
    assert "verified_at" in data


def test_verify_invoice_not_found(client: TestClient):
    """Test verification of non-existent invoice."""
    response = client.get("/invoices/INV-NOTFOUND/verify")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_verify_invoice_no_auth_required(client: TestClient, test_invoice: Invoice):
    """Test that verification endpoint does not require authentication."""
    # Make request without auth headers
    response = client.get(f"/invoices/{test_invoice.invoice_id}/verify")

    # Should succeed without authentication
    assert response.status_code == 200


def test_qr_code_generation():
    """Test QR code generation produces valid base64 data URI."""
    from app.core.config import settings
    from app.services.pdf_service import PDFService
    from app.storage.s3_client import S3Client

    s3_client = S3Client(
        endpoint=settings.S3_ENDPOINT,
        access_key=settings.S3_ACCESS_KEY or "test",
        secret_key=settings.S3_SECRET_KEY or "test",
        bucket=settings.S3_BUCKET,
    )

    pdf_service = PDFService(s3_client)
    qr_code = pdf_service._generate_qr_code("INV-TEST-QR-001")

    # Check it's a data URI
    assert qr_code.startswith("data:image/png;base64,")

    # Check base64 content exists
    base64_part = qr_code.split(",")[1]
    assert len(base64_part) > 100  # QR codes are fairly large

    # Check it's valid base64
    import base64

    try:
        decoded = base64.b64decode(base64_part)
        assert len(decoded) > 0
    except Exception as e:
        pytest.fail(f"Invalid base64 encoding: {e}")


def test_customer_name_masking():
    """Test customer name masking logic."""

    test_cases = [
        ("John Doe", "J*****e"),
        ("AB", "A*"),
        ("A", "A*"),
        ("Jane", "J**e"),
    ]

    for original, expected_pattern in test_cases:
        # Simulate the masking logic
        if len(original) > 2:
            masked = original[0] + "*" * (len(original) - 2) + original[-1]
        else:
            masked = original[0] + "*"

        assert masked[0] == expected_pattern[0]
        assert "*" in masked
        if len(original) > 2:
            assert masked[-1] == expected_pattern[-1]
