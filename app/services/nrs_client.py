"""NRS API Client Stub.

This module provides a placeholder client for the forthcoming National Revenue Service
(NRS) e-invoicing / fiscalization API integration planned for 2026 compliance.

Design Goals (now):
- Define minimal interface for future implementation.
- Allow dependency injection & mocking in services.
- Avoid committing to concrete HTTP schema until official spec confirmed.

Future Implementation Notes:
- Replace placeholder endpoints with real base URL & resource paths.
- Implement OAuth2 / API key auth handshake (depending on NRS spec).
- Add robust retry, idempotency keys, circuit breaker, telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class NRSNotConfigured(Exception):
    """Raised when attempting a network call without credentials/config."""


@dataclass
class NRSConfig:
    base_url: str | None = None
    api_key: str | None = None
    merchant_id: str | None = None
    timeout_seconds: int = 10
    enabled: bool = False  # Feature flag gating real transmissions


class NRSClient:
    """Lightweight stubbed client.

    Only returns synthetic responses until `enabled` and credentials provided.
    """

    def __init__(self, config: NRSConfig):
        self.config = config

    # --- Public API (to be called by fiscalization / tax services) ---

    def transmit_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Transmit a fiscalized invoice to NRS (stub).

        Args:
            payload: Normalized invoice data including fiscal codes & VAT breakdown.

        Returns:
            Dict representing NRS acknowledgment (stubbed now).
        """
        if not self.config.enabled:
            return {
                "status": "stubbed",
                "message": "NRS transmission skipped (client disabled)",
                "echo": {"invoice_id": payload.get("invoice_id")},
            }
        if not (self.config.base_url and self.config.api_key and self.config.merchant_id):
            raise NRSNotConfigured("NRS client missing configuration values")
        # TODO: Implement real HTTP POST here using httpx/requests with auth headers
        return {
            "status": "accepted",
            "nrs_reference": "STUB-NRS-REF-0001",
            "processing_mode": "simulated",
        }

    def get_status(self, reference: str) -> dict[str, Any]:
        """Fetch invoice transmission status (stub)."""
        if not self.config.enabled:
            return {"reference": reference, "status": "stubbed"}
        if not self.config.base_url:
            raise NRSNotConfigured("NRS base_url not configured")
        # TODO: Implement real GET request
        return {"reference": reference, "status": "accepted", "mode": "simulated"}


# Factory helper (could later read from runtime settings)


def get_nrs_client() -> NRSClient:
    # Lazy import to avoid circular with settings if any
    try:
        from app.core.config import settings  # type: ignore

        cfg = NRSConfig(
            base_url=getattr(settings, "NRS_BASE_URL", None),
            api_key=getattr(settings, "NRS_API_KEY", None),
            merchant_id=getattr(settings, "NRS_MERCHANT_ID", None),
            enabled=bool(getattr(settings, "NRS_ENABLED", False)),
        )
    except Exception:  # noqa: BLE001
        import logging as _log

        _log.getLogger(__name__).debug("NRS config not available, using defaults")
        cfg = NRSConfig()
    return NRSClient(cfg)
