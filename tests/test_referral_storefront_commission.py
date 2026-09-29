"""Tests for referral commission on SuoOps' 3% earnings.

Covers ReferralService.process_storefront_commission — the influencer earns a
percentage of SuoOps' flat 3% fee on a referred business's online/storefront
sale (never the gross sale value).
"""

from __future__ import annotations

import itertools

from app.models import models
from app.models.referral_models import (
    Referral,
    ReferralCode,
    ReferralReward,
    ReferralStatus,
    ReferralType,
)
from app.services.referral_service import ReferralService

_counter = itertools.count(1)


def _make_user(db, **kw):
    n = next(_counter)
    u = models.User(
        name=kw.pop("name", f"User {n}"),
        email=kw.pop("email", f"u{n}@example.com"),
        phone=kw.pop("phone", f"+23481{n:08d}"),
        business_name=kw.pop("business_name", "Biz"),
        **kw,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _setup_referral(db, *, pct: int, active: bool = True):
    """Create referrer + referred + an active COMPLETED referral. Returns the referred user."""
    referrer = _make_user(db)
    referred = _make_user(db)
    code = ReferralCode(
        user_id=referrer.id,
        code=f"CODE{next(_counter):04d}",
        is_active=active,
        is_influencer=True,
        commission_perpetual_pct=pct,
    )
    db.add(code)
    db.commit()
    db.refresh(code)
    referral = Referral(
        referral_code_id=code.id,
        referrer_id=referrer.id,
        referred_id=referred.id,
        status=ReferralStatus.COMPLETED,
        referral_type=ReferralType.FREE_SIGNUP,
    )
    db.add(referral)
    db.commit()
    return referrer, referred, code


def test_storefront_commission_pays_pct_of_fee(db_session):
    referrer, referred, _ = _setup_referral(db_session, pct=20)

    ok = ReferralService(db_session).process_storefront_commission(
        referred.id,
        suoops_fee_naira=300,  # SuoOps' 3% on a ₦10,000 sale
    )

    assert ok is True
    reward = db_session.query(ReferralReward).filter(ReferralReward.user_id == referrer.id).one()
    assert reward.reward_type == "commission_online"
    # 20% of the ₦300 fee = ₦60
    assert "₦60 commission" in reward.reward_description
    # Referral is converted to paid
    referral = db_session.query(Referral).filter(Referral.referred_id == referred.id).one()
    assert referral.referral_type == ReferralType.PAID_SIGNUP


def test_storefront_commission_no_referral_returns_false(db_session):
    orphan = _make_user(db_session)
    ok = ReferralService(db_session).process_storefront_commission(orphan.id, suoops_fee_naira=300)
    assert ok is False


def test_storefront_commission_inactive_code_no_reward(db_session):
    _, referred, _ = _setup_referral(db_session, pct=20, active=False)
    ok = ReferralService(db_session).process_storefront_commission(referred.id, suoops_fee_naira=300)
    assert ok is False
    assert db_session.query(ReferralReward).count() == 0


def test_storefront_commission_zero_fee_returns_false(db_session):
    _, referred, _ = _setup_referral(db_session, pct=20)
    ok = ReferralService(db_session).process_storefront_commission(referred.id, suoops_fee_naira=0)
    assert ok is False
    assert db_session.query(ReferralReward).count() == 0


def test_storefront_commission_zero_pct_converts_but_no_reward(db_session):
    _, referred, _ = _setup_referral(db_session, pct=0)
    ok = ReferralService(db_session).process_storefront_commission(referred.id, suoops_fee_naira=300)
    assert ok is False
    # No reward, but the referral is still marked paid.
    assert db_session.query(ReferralReward).count() == 0
    referral = db_session.query(Referral).filter(Referral.referred_id == referred.id).one()
    assert referral.referral_type == ReferralType.PAID_SIGNUP
