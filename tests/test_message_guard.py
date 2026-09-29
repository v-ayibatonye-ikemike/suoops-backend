"""Order-messaging leak-detection / redaction rules."""

from app.services.message_guard import MASK, scan_message


def test_masks_phone_and_account_numbers():
    r = scan_message("call me on 08031234567 or send to 0123456789")
    assert "08031234567" not in r.redacted
    assert "0123456789" not in r.redacted
    assert MASK in r.redacted
    assert "contact_or_account" in r.reasons
    assert r.flagged


def test_masks_email_and_links_and_handles():
    r = scan_message("email me a@b.com or wa.me/2348031234567 or @myshop_ig")
    assert "a@b.com" not in r.redacted
    assert "wa.me/2348031234567" not in r.redacted
    assert "@myshop_ig" not in r.redacted
    assert "email" in r.reasons
    assert "link_or_handle" in r.reasons


def test_masks_possible_delivery_code():
    r = scan_message("just tell me your code 481920 so I can release")
    assert "481920" not in r.redacted
    assert "possible_delivery_code" in r.reasons


def test_hard_block_circumvention_phrase():
    r = scan_message("let's just do this outside the app, pay me directly")
    assert r.blocked is True
    assert "payment_circumvention" in r.reasons


def test_soft_nudge_flags_but_not_blocked():
    r = scan_message("you can do a bank transfer if you like")
    assert r.blocked is False
    assert "payment_nudge" in r.reasons
    assert r.flagged


def test_clean_message_passes_through():
    r = scan_message("Hi! Your order will arrive tomorrow around noon, thanks.")
    assert r.redacted == "Hi! Your order will arrive tomorrow around noon, thanks."
    assert r.reasons == []
    assert r.blocked is False
    assert r.flagged is False


def test_spelled_out_number_is_masked_and_flagged():
    r = scan_message("reach me: zero eight zero three one two three four five six")
    assert MASK in r.redacted
    assert "spelled_contact" in r.reasons
    assert r.flagged is True


def test_short_spelled_count_not_flagged():
    # A couple of number-words in normal prose must NOT trip the filter.
    r = scan_message("I ordered two shirts and three caps, thanks.")
    assert "spelled_contact" not in r.reasons


def test_off_platform_channel_is_flagged():
    r = scan_message("just message me on whatsapp instead")
    assert MASK in r.redacted
    assert "off_platform_contact" in r.reasons
    assert r.flagged is True


def test_price_or_amount_talk_is_flagged():
    # Order is already paid via escrow — discussing a price/amount is a push to
    # collect more money off-platform.
    for text in (
        "the balance is ₦5000",
        "send 5k for the extra one",
        "how much for two more?",
        "top up 3000 naira please",
    ):
        r = scan_message(text)
        assert "money_amount" in r.reasons, text
        assert r.flagged is True


def test_address_and_quantity_numbers_not_money_flagged():
    # Short address / quantity numbers with no currency must NOT be money-flagged.
    for text in (
        "I'm at no 6, second gate",
        "block 12 flat 3",
        "bring 2 bags please",
    ):
        r = scan_message(text)
        assert "money_amount" not in r.reasons, text


def test_seller_circumvention_flags_at_threshold():
    """Enough circumvention attempts flag the seller (revokes trusted status)."""
    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.models import models
    from app.services.escrow_service import record_seller_circumvention

    s = SessionLocal()
    try:
        seller = models.User(name="Circumventor", phone="+2348007771234")
        s.add(seller)
        s.commit()
        s.refresh(seller)
        assert seller.flagged_for_review is False

        for _ in range(settings.ESCROW_SELLER_CIRCUMVENTION_FLAG_AT):
            record_seller_circumvention(s, seller)

        assert seller.circumvention_attempts == settings.ESCROW_SELLER_CIRCUMVENTION_FLAG_AT
        assert seller.flagged_for_review is True
    finally:
        s.rollback()
        s.close()
