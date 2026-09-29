"""
Helpers for invoice payment delivery: deciding when an invoice is *online-only*
(pay via Paystack link, bank account hidden) vs *offline* (business shares their
account and confirms manually).

Single source of truth so the pay page, PDF, WhatsApp templates and billing all
agree — no duplicated logic.
"""

from __future__ import annotations


def invoice_has_contact(invoice) -> bool:
    """True when the invoice's customer has a phone or email (pay link deliverable)."""
    customer = getattr(invoice, "customer", None)
    return bool(customer and (getattr(customer, "phone", None) or getattr(customer, "email", None)))


def is_online_only(issuer, *, has_contact: bool, channel: str | None = None) -> bool:
    """
    True when an invoice must be paid online (bank account hidden from customer).

    Two cases:
    - Storefront orders (channel == "storefront"): the customer pays as part of
      ordering, so commission flows through Paystack.
    - Any invoice from a business that has enabled online payments: their bank is
      hidden and the customer pays via the Paystack link. The 3% commission is
      still charged from the business's prepaid wallet at creation, so Paystack
      takes no cut and the full amount settles to the business.
    """
    if channel == "storefront":
        return True
    return bool(getattr(issuer, "online_payments_active", False))


def template_bank_params(issuer, *, online_only: bool) -> tuple[str, str, str]:
    """
    (bank_name, account_number, account_name) for WhatsApp invoice templates.

    For online-only invoices the real bank account is replaced with a
    "pay online" instruction so payment flows through the link (and Paystack).
    Returns non-empty placeholders (Meta rejects empty template params).
    """
    if online_only:
        biz = getattr(issuer, "business_name", None) or getattr(issuer, "name", None) or "your order"
        return ("Pay securely online", "Use the link below \U0001f447", biz)
    return (
        getattr(issuer, "bank_name", None) or "N/A",
        getattr(issuer, "account_number", None) or "N/A",
        getattr(issuer, "account_name", None) or "N/A",
    )
