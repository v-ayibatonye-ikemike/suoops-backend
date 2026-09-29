"""Subscription plan pricing constants.

Pro Features is a RECURRING monthly subscription (₦1,500/mo, features only, no
invoices) billed automatically via Paystack. The legacy ``PRO`` recurring plan is
archived and kept only so old subscribers don't break. Pro Pack (₦2,000) remains
a one-time prepaid purchase handled through the invoice-pack flow.
"""

from app.core.config import settings

# Plan prices in Naira (display prices - what customers see)
PLAN_PRICES = {
    "FREE": 0,
    # STARTER removed - users are FREE and buy invoice packs as needed
    "PRO": 2000,  # Prepaid Pro Pack: ₦2,000 one-time (20 invoices + 30 days Pro features)
    "PRO_FEATURES": 1500,  # Recurring Pro Features: ₦1,500/month (features only, auto-renew)
    "BUSINESS": 10000,  # ₦10,000 - 50 invoices included + Photo OCR (15 max)
}

# Paystack Plan Codes.
# PRO_FEATURES is the ACTIVE recurring monthly plan. Its live plan code is set
# below as the default and can be overridden per-environment via the
# PAYSTACK_PRO_FEATURES_PLAN_CODE env var (e.g. a test-mode plan code).
# PRO is a legacy archived recurring plan kept so old subscribers don't break.
PAYSTACK_PLAN_CODES = {
    "PRO": "PLN_b9uq4itr2415bdf",  # archived legacy recurring plan
    "PRO_FEATURES": settings.PAYSTACK_PRO_FEATURES_PLAN_CODE or "PLN_4fwppc0p1s9y448",
    # "BUSINESS": "PLN_xxxxxxxx",  # Add when Business plan is enabled
}

# Invoice pack pricing (available to all tiers)
INVOICE_PACK_SIZE = 50
INVOICE_PACK_PRICE = 1250  # ₦1,250 per 50 invoices
