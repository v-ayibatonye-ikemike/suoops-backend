from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select

from app.utils.token_encryption import derive_token_encryption_key
from deploy.vps.reencrypt_oauth_tokens import read_private_file, reencrypt_value, rotate_rows


@pytest.fixture
def ciphers():
    return (
        Fernet(derive_token_encryption_key("source-key-for-test-only-32-characters")),
        Fernet(derive_token_encryption_key("target-key-for-test-only-32-characters")),
    )


def test_rotation_preserves_plaintext_and_is_idempotent(ciphers):
    source, target = ciphers
    original = source.encrypt(b"oauth-token-value").decode()
    replacement, changed = reencrypt_value(original, source, target)
    assert changed
    assert replacement != original
    assert target.decrypt(replacement.encode()) == source.decrypt(original.encode())
    assert reencrypt_value(replacement, source, target) == (replacement, False)
    with pytest.raises(ValueError, match="cannot decrypt"):
        reencrypt_value("invalid-ciphertext", source, target)


@pytest.mark.parametrize("apply", [False, True])
def test_transaction_rolls_back_invalid_later_row(ciphers, apply):
    source, target = ciphers
    metadata = MetaData()
    oauth = Table(
        "oauth_tokens", metadata,
        Column("id", Integer, primary_key=True),
        Column("access_token_encrypted", String),
        Column("refresh_token_encrypted", String),
    )
    engine = create_engine("sqlite:///:memory:", hide_parameters=True)
    metadata.create_all(engine)
    original = source.encrypt(b"valid-token").decode()
    with engine.begin() as connection:
        connection.execute(oauth.insert(), [
            {"id": 1, "access_token_encrypted": original, "refresh_token_encrypted": original},
            {"id": 2, "access_token_encrypted": "invalid", "refresh_token_encrypted": original},
        ])
    with pytest.raises(ValueError, match="cannot decrypt"):
        with engine.begin() as connection:
            rotate_rows(connection, source, target, 2, apply)
    with engine.connect() as connection:
        assert connection.execute(select(oauth.c.access_token_encrypted).where(oauth.c.id == 1)).scalar_one() == original
        with pytest.raises(ValueError, match="Expected 74"):
            rotate_rows(connection, source, target, 74, apply)
    engine.dispose()


def test_recovery_files_require_owner_only_permissions(tmp_path: Path):
    path = tmp_path / "recovery.env"
    path.write_text("test-only")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        read_private_file(path)
    path.chmod(0o600)
    assert read_private_file(path) == "test-only"


def test_dry_run_commit_and_already_rotated_rows(ciphers):
    source, target = ciphers
    metadata = MetaData()
    oauth = Table(
        "oauth_tokens", metadata,
        Column("id", Integer, primary_key=True),
        Column("access_token_encrypted", String),
        Column("refresh_token_encrypted", String),
    )
    engine = create_engine("sqlite:///:memory:", hide_parameters=True)
    metadata.create_all(engine)
    original = source.encrypt(b"oauth-secret-test-only").decode()
    with engine.begin() as connection:
        connection.execute(oauth.insert(), {
            "id": 1, "access_token_encrypted": original, "refresh_token_encrypted": original,
        })
    with engine.begin() as connection:
        assert rotate_rows(connection, source, target, 1, False) == (1, 1)
        assert connection.execute(select(oauth.c.access_token_encrypted)).scalar_one() == original
    with engine.begin() as connection:
        assert rotate_rows(connection, source, target, 1, True) == (1, 1)
    with engine.begin() as connection:
        assert rotate_rows(connection, source, target, 1, True) == (1, 0)
        stored = connection.execute(select(oauth)).mappings().one()
        for field in ("access_token_encrypted", "refresh_token_encrypted"):
            assert target.decrypt(stored[field].encode()) == b"oauth-secret-test-only"
    engine.dispose()
