"""Enforce append-only on audit_log (block UPDATE/DELETE/TRUNCATE)

Revision ID: 20260720_audit_append_only
Revises: 20260720_audit_log
Create Date: 2026-07-20

Turns the audit trail from tamper-EVIDENT (hash chain) into tamper-RESISTANT: a
Postgres trigger raises on any UPDATE, DELETE or TRUNCATE of ``audit_log`` rows.
The app only ever INSERTs, so normal operation is unaffected. Anyone wanting to
alter history must first explicitly DISABLE the trigger — a deliberate,
superuser-level act, not a silent programmatic edit.

Postgres-only; a no-op on other dialects (e.g. the SQLite test DB), which never
runs migrations anyway.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260720_audit_append_only"
down_revision = "20260720_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_no_mutate() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_no_mutate();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_no_mutate();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;")
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;")
    op.execute("DROP FUNCTION IF EXISTS audit_log_no_mutate();")
