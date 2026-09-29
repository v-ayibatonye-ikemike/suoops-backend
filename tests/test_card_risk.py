"""Card-fraud risk helpers: fingerprint, blocklist, per-card velocity."""

from __future__ import annotations

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import models
from app.services import card_risk


def test_extract_fingerprint():
    assert (
        card_risk.extract_fingerprint("paystack", {"data": {"authorization": {"signature": "ABC123"}}}) == "ps:ABC123"
    )
    assert card_risk.extract_fingerprint("flutterwave", {"data": {"card": {"token": "TOK9"}}}) == "fw:TOK9"
    assert (
        card_risk.extract_fingerprint(
            "flutterwave", {"data": {"card": {"first_6digits": "506099", "last_4digits": "1234"}}}
        )
        == "fw:5060991234"
    )
    assert card_risk.extract_fingerprint("paystack", {}) is None
    assert card_risk.extract_fingerprint("paystack", None) is None


def test_block_and_hold_reason():
    s = SessionLocal()
    fp = "ps:TESTBLOCK1"
    try:
        assert card_risk.is_card_blocked(s, fp) is False
        card_risk.block_card(s, fp, reason="test", days=30)
        assert card_risk.is_card_blocked(s, fp) is True
        assert "blocked" in (card_risk.card_hold_reason(s, fp) or "")
    finally:
        s.query(models.BlockedCard).filter(models.BlockedCard.fingerprint == fp).delete()
        s.commit()
        s.close()


def test_card_velocity_hold():
    s = SessionLocal()
    fp = "ps:VELOCITY1"
    try:
        for i in range(settings.CARD_MAX_ORDERS_PER_DAY):
            s.add(
                models.StorefrontOrderEscrow(
                    invoice_id=990000 + i,
                    seller_id=1,
                    status="held",
                    card_fingerprint=fp,
                    gross_kobo=1000,
                )
            )
        s.commit()
        assert card_risk.recent_order_count_for_card(s, fp) >= settings.CARD_MAX_ORDERS_PER_DAY
        assert "many orders" in (card_risk.card_hold_reason(s, fp) or "")
    finally:
        s.query(models.StorefrontOrderEscrow).filter(models.StorefrontOrderEscrow.card_fingerprint == fp).delete()
        s.commit()
        s.close()
