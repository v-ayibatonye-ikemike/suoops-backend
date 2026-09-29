"""Flutterwave bank-name resolver tolerance tests (neobank naming differences)."""

from __future__ import annotations

from app.services.payouts.flutterwave import _match_bank_code, _normalize_bank_name


def _map(*names_codes):
    return {_normalize_bank_name(n): c for n, c in names_codes}


def test_exact_match():
    m = _map(("Guaranty Trust Bank", "058"))
    assert _match_bank_code("Guaranty Trust Bank", m) == "058"


def test_kuda_bank_matches_microfinance_listing():
    # Seller saved "Kuda Bank"; Flutterwave lists it as "Kuda Microfinance Bank".
    m = _map(("Kuda Microfinance Bank", "50211"), ("Access Bank", "044"))
    assert _match_bank_code("Kuda Bank", m) == "50211"


def test_kuda_bank_matches_short_listing():
    m = _map(("Kuda.", "50211"), ("Zenith Bank", "057"))
    assert _match_bank_code("Kuda Bank", m) == "50211"


def test_opay_alias():
    m = _map(("OPay Digital Services", "999992"), ("Sterling Bank", "232"))
    assert _match_bank_code("OPay", m) == "999992"


def test_moniepoint_core_match():
    m = _map(("Moniepoint Microfinance Bank", "50515"), ("First Bank", "011"))
    assert _match_bank_code("Moniepoint MFB", m) == "50515"


def test_ambiguous_is_refused():
    # Exact match always wins even when another entry shares the core.
    m = _map(("Sterling Bank", "232"), ("Sterling MFB", "999"))
    assert _match_bank_code("Sterling Bank", m) == "232"
    # A target that core-matches two entries equally → refuse rather than guess.
    assert _match_bank_code("Sterling", m) is None


def test_unknown_returns_none():
    m = _map(("Access Bank", "044"))
    assert _match_bank_code("Nonexistent Bank", m) is None
