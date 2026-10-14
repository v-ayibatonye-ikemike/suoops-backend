import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import AdminUserDep, CurrentUserDep, DataOwnerDep, DbDep
from app.api.routes_admin_auth import get_current_admin
from app.core.audit import log_audit_event
from app.db.session import get_db
from app.models.models import User
from app.models.retention_models import RetentionEnrollment
from app.services.merchant_retention import (
    UTC,
    create_pilot,
    merchant_query,
    set_consent,
    set_pilot_enabled,
    snapshot,
)
from app.services.retention_metrics import retention_metrics

merchant_router = APIRouter(prefix="/retention", tags=["retention"])
admin_router = APIRouter(prefix="/retention", tags=["admin-retention"])


class ConsentIn(BaseModel):
    enabled: bool


class PilotIn(BaseModel):
    user_ids: list[int] = Field(min_length=50, max_length=50)


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
        raise HTTPException(403, "Only the phone-owning business owner can consent")
    try:
        set_consent(db, data_owner_id, payload.enabled, "web_settings", dt.datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log_audit_event("retention.consent", user_id=admin_user_id, enabled=payload.enabled)
    return {"whatsapp_opted_in": payload.enabled}


def _authorize(admin):
    if not admin.is_super_admin:
        raise HTTPException(403, "Only a super admin can manage the retention experiment")


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
        item["eligible"] = bool(
            user.phone
            and user.phone_verified
            and item["whatsapp_opted_in"]
            and item["state"] in {"at_risk", "dormant"}
            and item["facts"]["invoices"] > 0
            and item["recommendation"]
            and not db.get(RetentionEnrollment, user.id)
        )
        candidates.append(item)
    db.commit()
    return {"businesses": candidates, "next_after_id": users[-1].id if len(users) == 100 else None}


@admin_router.post("/pilot", status_code=201)
def enroll_retention_pilot(payload: PilotIn, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _authorize(admin)
    try:
        create_pilot(db, payload.user_ids, admin.id, dt.datetime.now(UTC))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Pilot enrollment already exists; refresh before proceeding.") from exc
    log_audit_event("retention.pilot.created", user_id=admin.id, businesses=50)
    return retention_metrics(db, dt.datetime.now(UTC))


@admin_router.patch("/pilot")
def toggle_retention_pilot(payload: ConsentIn, db: Session = Depends(get_db), admin=Depends(get_current_admin)):
    _authorize(admin)
    try:
        set_pilot_enabled(db, payload.enabled, dt.datetime.now(UTC))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    log_audit_event("retention.pilot.enabled", user_id=admin.id, enabled=payload.enabled)
    return retention_metrics(db, dt.datetime.now(UTC))
