"""Rehearse JWT rotation on an isolated, restored source database."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from app.core.config import settings
from app.core.encryption import decrypt_value
from app.utils.token_encryption import decrypt_token, derive_token_encryption_key


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-settings", type=Path, required=True)
    parser.add_argument("--target-settings", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args()
    engine = create_engine(settings.DATABASE_URL, hide_parameters=True)
    if settings.ENV != "test" or engine.url.host != "db":
        raise RuntimeError("Rehearsal requires ENV=test and the isolated db host")
    source = json.loads(args.source_settings.read_text())["backend"]
    if settings.ENCRYPTION_KEY != source["ENCRYPTION_KEY"]:
        raise RuntimeError("The general data-encryption key must remain unchanged")
    manifest = json.loads(args.manifest.read_text())
    quote = engine.dialect.identifier_preparer.quote

    def reconcile() -> None:
        with engine.connect() as connection:
            major = int(connection.execute(text("SHOW server_version")).scalar_one().split(".")[0])
            if major != manifest["sourceMajor"]:
                raise RuntimeError("Source/target PostgreSQL majors differ")
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            if revision != args.expected_revision:
                raise RuntimeError("Restored Alembic revision differs from the source")
            for record in manifest["records"]:
                table = "public." + quote(record["table"])
                if "rows" in record:
                    actual = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                    expected = record["rows"]
                else:
                    column = quote(record["column"])
                    actual = connection.execute(
                        text(f"SELECT coalesce(sum({column}), 0)::text FROM {table}")
                    ).scalar_one()
                    expected = record["sum"]
                if actual != expected:
                    raise RuntimeError("Source reconciliation failed for " + record["table"])

    def tokens() -> list:
        with engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT id, access_token_encrypted, refresh_token_encrypted "
                    "FROM oauth_tokens ORDER BY id"
                )
            ).all()

    def emails() -> list:
        with engine.connect() as connection:
            return connection.execute(text('SELECT id, email_enc FROM "user" ORDER BY id')).all()

    reconcile()
    before = tokens()
    old_fernet = Fernet(derive_token_encryption_key(source["JWT_SECRET"]))
    plaintext = [
        (row[0], *(old_fernet.decrypt(value.encode()) if value else None for value in row[1:]))
        for row in before
    ]
    email_ciphertexts = emails()
    email_plaintext = [(row[0], decrypt_value(row[1])) for row in email_ciphertexts]
    command = [
        sys.executable, "-m", "deploy.vps.reencrypt_oauth_tokens",
        "--source-settings", str(args.source_settings),
        "--target-settings", str(args.target_settings),
        "--expected-count", str(len(before)),
    ]
    subprocess.run(command, check=True)
    subprocess.run([*command, "--apply"], check=True)
    after = tokens()
    decrypted = [
        (row[0], *(decrypt_token(value).encode() if value else None for value in row[1:]))
        for row in after
    ]
    unchanged_ciphertext = any(
        old_value and old_value == new_value
        for old_row, new_row in zip(before, after)
        for old_value, new_value in zip(old_row[1:], new_row[1:])
    )
    if decrypted != plaintext or unchanged_ciphertext:
        raise RuntimeError("OAuth plaintext or ciphertext rotation verification failed")
    subprocess.run([*command, "--apply"], check=True)
    if tokens() != after:
        raise RuntimeError("A repeated rotation changed target ciphertexts")
    reconcile()
    if emails() != email_ciphertexts or [(row[0], decrypt_value(row[1])) for row in emails()] != email_plaintext:
        raise RuntimeError("General encrypted email fields changed")
    engine.dispose()
    print(
        f"Verified {len(before)} OAuth rows, unchanged email fields, "
        f"and all {len(manifest['records'])} source reconciliation records."
    )


if __name__ == "__main__":
    main()
