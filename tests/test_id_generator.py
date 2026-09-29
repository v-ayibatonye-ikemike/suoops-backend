"""Tests for ID generator utility."""

from app.utils.id_generator import generate_id


def test_generate_id_with_prefix():
    """Test ID generation with custom prefix."""
    invoice_id = generate_id("INV")

    # Should start with INV-
    assert invoice_id.startswith("INV-")

    # Should have reasonable length
    assert len(invoice_id) > 10


def test_generate_id_uniqueness():
    """Test that generated IDs are unique."""
    ids = {generate_id("TEST") for _ in range(100)}

    # All 100 IDs should be unique
    assert len(ids) == 100


def test_generate_id_different_prefixes():
    """Test IDs with different prefixes."""
    invoice_id = generate_id("INV")
    expense_id = generate_id("EXP")

    # Should have different prefixes
    assert invoice_id.startswith("INV-")
    assert expense_id.startswith("EXP-")

    # Should be different values
    assert invoice_id != expense_id


def test_generate_id_format():
    """Test generated ID format — UUID hex after prefix."""
    id_value = generate_id("PREFIX")

    # Should contain prefix and separator
    assert "-" in id_value
    assert id_value.startswith("PREFIX-")

    # Suffix should be 32 hex characters (UUID4 without dashes)
    suffix = id_value.replace("PREFIX-", "")
    assert len(suffix) == 32
    # Verify it's valid hex
    int(suffix, 16)
