"""Rotate stored OAuth ciphertext on the private restored VPS database only."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Integer, String, column, create_engine, select, table, text, update
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.utils.token_encryption import derive_token_encryption_key

logger = logging.getLogger(__name__)
tokens = table(
    "oauth_tokens",
    column("id", Integer),
    column("access_token_encrypted", String),
    column("refresh_token_encrypted", String),
)


def read_private_file(path: Path) -> str:
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise ValueError("Recovery settings must be regular owner-only files.")
    return path.read_text()


def reencrypt_value(value: str, source: Fernet, target: Fernet) -> tuple[str, bool]:
    try:
        plaintext = source.decrypt(value.encode())
    except InvalidToken:
        try:
            target.decrypt(value.encode())
        except InvalidToken:
            raise ValueError("OAuth ciphertext cannot decrypt with either recovery key.") from None
        return value, False
    replacement = target.encrypt(plaintext)
    if not hmac.compare_digest(target.decrypt(replacement), plaintext):
        raise ValueError("OAuth plaintext verification failed.")
    return replacement.decode(), True


def rotate_rows(
    connection: Connection,
    source: Fernet,
    target: Fernet,
    expected_count: int,
    apply: bool,
) -> tuple[int, int]:
    if connection.dialect.name == "postgresql":
        connection.execute(text("LOCK TABLE oauth_tokens IN EXCLUSIVE MODE"))
    rows = connection.execute(select(tokens).order_by(tokens.c.id).with_for_update()).mappings().all()
    if len(rows) != expected_count:
        raise ValueError(f"Expected {expected_count} OAuth records, found {len(rows)}; refusing rotation.")
    changed = 0
    for row in rows:
        replacements: dict[str, str] = {}
        for field in ("access_token_encrypted", "refresh_token_encrypted"):
            value = row[field]
            if not isinstance(value, str) or not value:
                raise ValueError(f"OAuth record {row['id']} has an invalid {field}.")
            replacement, needs_rotation = reencrypt_value(value, source, target)
            if needs_rotation:
                replacements[field] = replacement
        if replacements:
            changed += 1
            if apply:
                connection.execute(
                    update(tokens).where(tokens.c.id == row["id"]).values(**replacements)
                )
    return len(rows), changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-settings", type=Path, required=True)
    parser.add_argument("--target-settings", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="Commit changes; otherwise validate only.")
    args = parser.parse_args()
    if args.expected_count < 0:
        parser.error("--expected-count must be nonnegative")
    url = make_url(settings.DATABASE_URL)
    if url.get_backend_name() != "postgresql" or url.host != "db":
        parser.error("Refusing any database except the private Compose PostgreSQL host 'db'.")
    try:
        source_settings = json.loads(read_private_file(args.source_settings))
        source_secret = source_settings["backend"]["JWT_SECRET"]
        if not isinstance(source_secret, str):
            raise ValueError("Missing source JWT recovery key.")
        target_values = [
            line.partition("=")[2]
            for line in read_private_file(args.target_settings).splitlines()
            if line.startswith("JWT_SECRET=")
        ]
        if len(target_values) != 1 or len(target_values[0]) < 32:
            raise ValueError("Target settings must contain exactly one strong raw JWT_SECRET.")
        if not hmac.compare_digest(target_values[0], settings.JWT_SECRET):
            raise ValueError("Target recovery key does not match the deployed JWT_SECRET.")
        source_key = derive_token_encryption_key(source_secret)
        target_key = derive_token_encryption_key(target_values[0])
        if hmac.compare_digest(source_key, target_key):
            raise ValueError("Source and target keys are identical; no JWT rotation configured.")
        engine = create_engine(url, hide_parameters=True)
        try:
            with engine.begin() as connection:
                count, changed = rotate_rows(
                    connection, Fernet(source_key), Fernet(target_key), args.expected_count, args.apply
                )
        finally:
            engine.dispose()
    except (OSError, ValueError, KeyError, TypeError):
        logger.exception("OAuth rotation failed; no transaction committed. Recovery values are not logged.")
        return 1
    except SQLAlchemyError as error:
        logger.error(
            "OAuth database transaction failed and rolled back (SQLSTATE %s).",
            getattr(getattr(error, "orig", None), "pgcode", "unavailable"),
        )
        return 1
    logger.info(
        "%s: verified %d OAuth records; %d %s; %d already use the target key.",
        "Committed" if args.apply else "Dry run (no writes)",
        count, changed, "rotated" if args.apply else "would rotate", count - changed,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
