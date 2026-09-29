"""Integration tests for tax and fiscalization features.

Run manually with INTEGRATION=1 and server running.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

import pytest
import requests

# Base URL
BASE_URL = "http://localhost:8000"


@pytest.mark.skipif(not os.getenv("INTEGRATION"), reason="Integration test requires running server")
def test_vat_calculator():
    """Exercise VAT calculation endpoint (standard + zero rated)."""
    cases: Iterable[tuple[int, str]] = [
        (10000, "standard"),
        (5000, "zero_rated"),
    ]
    for amount, category in cases:
        response = requests.get(
            f"{BASE_URL}/tax/vat/calculate",
            params={"amount": amount, "category": category},
            timeout=5,
        )
        assert response.status_code == 200
        data = response.json()
        assert "vat_amount" in data


@pytest.mark.skipif(not os.getenv("INTEGRATION"), reason="Integration test requires running server")
def test_api_docs_includes_tax_paths():
    """Ensure OpenAPI spec exposes tax-related paths."""
    response = requests.get(f"{BASE_URL}/openapi.json", timeout=5)
    assert response.status_code == 200
    openapi_spec = response.json()
    tax_paths = [p for p in openapi_spec.get("paths", {}) if "/tax/" in p]
    assert tax_paths, "Expected at least one /tax/ path in OpenAPI spec"


if __name__ == "__main__":  # pragma: no cover
    # Minimal manual quick check without pytest harness
    if not os.getenv("INTEGRATION"):
        print("Set INTEGRATION=1 to run integration checks.")
    else:
        test_vat_calculator()
        test_api_docs_includes_tax_paths()
        print("Integration checks passed.")
