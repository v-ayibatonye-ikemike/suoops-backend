"""Durable audit-log DB persistence + tamper-evident hash chain."""

from __future__ import annotations

from app.core import audit
from app.db.session import SessionLocal
from app.models.models import AuditLog


def test_audit_db_persistence_and_hash_chain():
    audit._persist_audit_db({"ts": 1, "action": "test.one", "user_id": 5, "status": "success", "foo": "bar"})
    audit._persist_audit_db({"ts": 2, "action": "test.two", "user_id": None, "status": "denied"})

    with SessionLocal() as db:
        rows = db.query(AuditLog).order_by(AuditLog.id).all()

    assert len(rows) == 2
    # Row 1: fields + non-core metadata captured in details.
    assert rows[0].action == "test.one"
    assert rows[0].user_id == 5
    assert rows[0].details == {"foo": "bar"}
    assert rows[0].prev_hash is None
    assert rows[0].entry_hash  # genesis hash present
    # Row 2: hash chain links back to row 1 (tamper-evidence).
    assert rows[1].status == "denied"
    assert rows[1].prev_hash == rows[0].entry_hash
    assert rows[1].entry_hash != rows[0].entry_hash


def test_audit_db_persistence_never_raises(monkeypatch):
    # Even if the DB layer blows up, persistence must swallow the error.
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.db.session.SessionLocal", boom)
    # Should not raise.
    audit._persist_audit_db({"ts": 1, "action": "x", "user_id": 1, "status": "success"})


def test_audit_chain_recomputes_and_detects_tampering():
    from app.core.audit import hash_entry

    audit._persist_audit_db({"action": "a.one", "user_id": 1, "status": "success", "x": 1})
    audit._persist_audit_db({"action": "a.two", "user_id": 2, "status": "success"})

    with SessionLocal() as db:
        rows = db.query(AuditLog).order_by(AuditLog.id).all()

    # Chain recomputes exactly from the stored columns (verifiable).
    prev = ""
    for r in rows:
        assert r.entry_hash == hash_entry(prev, r.action, r.user_id, r.status, r.details)
        assert (r.prev_hash or "") == prev
        prev = r.entry_hash

    # Tampering with any field makes the recomputed hash diverge → detectable.
    tampered = hash_entry("", "a.HACKED", rows[0].user_id, rows[0].status, rows[0].details)
    assert tampered != rows[0].entry_hash
