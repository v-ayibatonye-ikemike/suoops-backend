import datetime as dt
from html import escape
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.dependencies import AdminUserDep, CurrentUserDep, DataOwnerDep, DbDep
from app.api.routes_admin_auth import get_current_admin
from app.core.audit import log_audit_event
from app.db.session import get_db
from app.models.models import User
from app.services.merchant_retention import (
    UTC,
    merchant_query,
    set_consent,
    set_email_consent,
    set_outreach_enabled,
    snapshot,
    unsubscribe_email,
)
from app.services.retention_metrics import retention_metrics

merchant_router = APIRouter(prefix="/retention", tags=["retention"])
admin_router = APIRouter(prefix="/retention", tags=["admin-retention"])


class ConsentIn(BaseModel):
    enabled: bool
    channel: Literal["whatsapp", "email"] = "whatsapp"


@merchant_router.get("")
def get_merchant_retention(current_user_id: CurrentUserDep, data_owner_id: DataOwnerDep, db: DbDep):
    user = db.get(User, data_owner_id)
    if user is None:
        raise HTTPException(404, "Business not found")
    result = snapshot(db, user, dt.datetime.now(UTC))
    db.commit()
    return result


@merchant_router.patch("/consent")
def update_retention_consent(payload: ConsentIn, admin_user_id: AdminUserDep, data_owner_id: DataOwnerDep, db: DbDep):
    if admin_user_id != data_owner_id:
        raise HTTPException(403, "Only the business owner can change outreach preferences")
    try:
        if payload.channel == "email":
            set_email_consent(db, data_owner_id, payload.enabled, dt.datetime.now(UTC))
        else:
            set_consent(db, data_owner_id, payload.enabled, "web_settings", dt.datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log_audit_event("retention.consent", user_id=admin_user_id, enabled=payload.enabled, channel=payload.channel)
    return {"email_enabled" if payload.channel == "email" else "whatsapp_opted_in": payload.enabled}


@merchant_router.get("/unsubscribe", response_class=HTMLResponse)
def confirm_email_unsubscribe(token: str = Query(min_length=1, max_length=2000)):
    return HTMLResponse(
        '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Stop SuoOps business emails</title><body><h1>Stop SuoOps business emails</h1>'
        '<p>This stops retention emails, not login codes, invoices or receipts.</p>'
        f'<form method="post" action="?token={escape(token, quote=True)}">'
        '<button type="submit">Stop retention emails</button></form></body></html>',
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@merchant_router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_retention_email(db: DbDep, token: str = Query(min_length=1, max_length=2000)):
    try:
        unsubscribe_email(db, token, dt.datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><title>Unsubscribed</title>"
        "<body><h1>Business emails stopped</h1><p>Login codes, invoices and receipts are unchanged.</p></body></html>",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _authorize(admin):
    if not admin.is_super_admin:
        raise HTTPException(403, "Only a super admin can manage retention outreach")


@admin_router.get("")
def get_retention_overview(db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _authorize(admin)
    return retention_metrics(db, dt.datetime.now(UTC))


@admin_router.get("/candidates")
def get_retention_candidates(
    after_id: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    _authorize(admin)
    now = dt.datetime.now(UTC)
    users = merchant_query(db).filter(User.id > after_id).order_by(User.id).limit(100).all()
    candidates = []
    for user in users:
        item = snapshot(db, user, now)
        item["eligible"] = bool(item["channel"] and item["recommendation"])
        candidates.append(item)
    db.commit()
    return {"businesses": candidates, "next_after_id": users[-1].id if len(users) == 100 else None}


@admin_router.patch("/outreach")
def toggle_retention_outreach(payload: ConsentIn, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _authorize(admin)
    try:
        set_outreach_enabled(db, payload.enabled, dt.datetime.now(UTC), admin.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    log_audit_event("retention.outreach.enabled", user_id=admin.id, enabled=payload.enabled)
    return retention_metrics(db, dt.datetime.now(UTC))
