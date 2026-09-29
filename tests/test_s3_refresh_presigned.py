"""Unit tests for S3Client.refresh_presigned_url.

Stored presigned URLs expire (~1h); serving the stored value later yields
'AccessDenied / Request has expired'. refresh_presigned_url re-signs from the
object key so invoice/receipt/product/tax links stay valid when clicked.
"""

from __future__ import annotations

from app.storage.s3_client import S3Client


def test_refresh_passes_through_non_s3_values():
    c = S3Client()
    assert c.refresh_presigned_url(None) is None
    assert c.refresh_presigned_url("") == ""
    # Local filesystem URLs must not be touched.
    assert c.refresh_presigned_url("file:///storage/x.pdf") == "file:///storage/x.pdf"


def test_refresh_resigns_from_key(monkeypatch):
    c = S3Client()
    seen = {}

    monkeypatch.setattr(c, "extract_key_from_url", lambda url: "tax-reports/1/2025-10.pdf")

    def fake_presign(key, expires_in=None):
        seen["key"] = key
        return "https://bucket.s3.amazonaws.com/tax-reports/1/2025-10.pdf?X-Amz-Signature=FRESH"

    monkeypatch.setattr(c, "get_presigned_url", fake_presign)

    stale = "https://bucket.s3.amazonaws.com/tax-reports/1/2025-10.pdf?X-Amz-Signature=STALE"
    fresh = c.refresh_presigned_url(stale)
    assert "FRESH" in fresh and "STALE" not in fresh
    assert seen["key"] == "tax-reports/1/2025-10.pdf"


def test_refresh_falls_back_when_cannot_resign(monkeypatch):
    c = S3Client()
    # Key unparseable → return original untouched.
    monkeypatch.setattr(c, "extract_key_from_url", lambda url: None)
    original = "https://bucket.s3.amazonaws.com/logos/user_1.png?X-Amz-Signature=abc"
    assert c.refresh_presigned_url(original) == original

    # Key parses but S3 can't sign (local dev) → return original untouched.
    monkeypatch.setattr(c, "extract_key_from_url", lambda url: "logos/user_1.png")
    monkeypatch.setattr(c, "get_presigned_url", lambda key, expires_in=None: None)
    assert c.refresh_presigned_url(original) == original
