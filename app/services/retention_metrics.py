from __future__ import annotations

import datetime as dt
from statistics import mean

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.inventory_models import StockMovement
from app.models.models import Invoice
from app.models.retention_models import RetentionContact, RetentionEnrollment, RetentionExperiment
from app.services.merchant_retention import (
    meaningful_stock_records,
    merchant_query,
    outreach_channels,
    outreach_configured,
    outreach_enabled,
    utc,
)


def _rate(numerator: int, denominator: int) -> dict:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "percent": round(100 * numerator / denominator, 2) if denominator else None,
    }


def retention_metrics(db: Session, now: dt.datetime) -> dict:
    users = merchant_query(db).all()
    records: dict[int, list[Invoice]] = {user.id: [] for user in users}
    fee_records: dict[int, list[Invoice]] = {user.id: [] for user in users}
    query = db.query(Invoice).filter(
        Invoice.issuer_id.in_(records),
        Invoice.created_at <= now,
    )
    if settings.METRICS_MAX_INVOICE_NAIRA > 0:
        query = query.filter(or_(Invoice.currency != "NGN", Invoice.amount <= settings.METRICS_MAX_INVOICE_NAIRA))
    for invoice in query.yield_per(500):
        fee_records[invoice.issuer_id].append(invoice)
        if invoice.status != "cancelled" and not (invoice.channel == "storefront" and invoice.status == "pending"):
            records[invoice.issuer_id].append(invoice)
    stock_events: dict[int, list[dt.datetime]] = {user.id: [] for user in users}
    for uid, created_at in (
        db.query(StockMovement.user_id, StockMovement.created_at)
        .filter(
            StockMovement.user_id.in_(records),
            StockMovement.created_at <= now,
            meaningful_stock_records(),
        )
        .yield_per(500)
    ):
        stock_events[uid].append(utc(created_at))

    def events(user_id: int) -> list[dt.datetime]:
        return stock_events.get(user_id, []) + [
            event
            for invoice in records.get(user_id, [])
            for event in (
                [utc(invoice.created_at), utc(invoice.paid_at)]
                if invoice.status == "paid" and invoice.paid_at and utc(invoice.paid_at) <= now
                else [utc(invoice.created_at)]
            )
        ]

    def performance(ids: list[int], start: dt.datetime, end: dt.datetime) -> dict:
        invoices = [invoice for user_id in ids for invoice in records.get(user_id, [])]
        created = [i for i in invoices if i.invoice_type == "revenue" and start <= utc(i.created_at) < end]
        paid = [
            i
            for i in invoices
            if i.invoice_type == "revenue" and i.status == "paid" and i.paid_at and start <= utc(i.paid_at) < end
        ]
        charged = [
            i
            for uid in ids
            for i in fee_records.get(uid, [])
            if i.invoice_type == "revenue"
            and (
                (i.channel != "storefront" and start <= utc(i.created_at) < end)
                or (i.channel == "storefront" and i.status == "paid" and i.paid_at and start <= utc(i.paid_at) < end)
            )
        ]
        active = sum(any(start <= event < end for event in events(uid)) for uid in ids)
        gmv_by_currency: dict[str, float] = {}
        for invoice in paid:
            gmv_by_currency[invoice.currency] = gmv_by_currency.get(invoice.currency, 0) + float(invoice.amount)
        gmv = gmv_by_currency.get("NGN", 0)
        commission = sum(i.platform_fee_kobo or 0 for i in charged) / 100
        return {
            "businesses": len(ids),
            "returning_active_businesses": active,
            "invoices_created": len(created),
            "payments_recorded": len(paid),
            "gmv": round(gmv, 2),
            "gmv_by_currency": {currency: round(amount, 2) for currency, amount in gmv_by_currency.items()},
            "recorded_commission": round(commission, 2),
            "missing_fee_records": sum(i.platform_fee_kobo is None for i in charged),
            "gmv_per_active_merchant": round(gmv / active, 2) if active else None,
            "commission_per_active_merchant": round(commission / active, 2) if active else None,
            "invoice_to_payment_conversion": _rate(
                sum(i.status == "paid" and i.paid_at is not None and utc(i.paid_at) < end for i in created),
                len(created),
            ),
        }

    activation_eligible = activation_success = 0
    invoice_times: list[float] = []
    payment_times: list[float] = []
    retention = {days: [0, 0] for days in (30, 60, 90)}
    for user in users:
        signup = utc(user.created_at)
        invoices = [i for i in records[user.id] if i.invoice_type == "revenue"]
        first_invoice = min((utc(i.created_at) for i in invoices), default=None)
        first_payment = min(
            (utc(i.paid_at) for i in invoices if i.status == "paid" and i.paid_at and utc(i.paid_at) <= now),
            default=None,
        )
        if first_invoice:
            invoice_times.append(max(0, (first_invoice - signup).total_seconds() / 3600))
        if first_payment:
            payment_times.append(max(0, (first_payment - signup).total_seconds() / 3600))
        if signup <= now - dt.timedelta(days=7):
            activation_eligible += 1
            activation_success += bool(first_invoice and first_invoice < signup + dt.timedelta(days=7))
        for days, counts in retention.items():
            window_start, window_end = signup + dt.timedelta(days=days), signup + dt.timedelta(days=days + 7)
            if window_end <= now:
                counts[1] += 1
                counts[0] += any(window_start <= event < window_end for event in events(user.id))

    pilot = db.get(RetentionExperiment, 1)
    experiment: dict | None = None
    if pilot:
        enrollments = db.query(RetentionEnrollment).filter_by(experiment_id=pilot.id).all()
        experiment = {
            "id": pilot.id,
            "enabled": pilot.enabled,
            "started_at": pilot.started_at,
            "ends_at": pilot.ends_at,
            "arms": {},
            "assignments": [
                {"user_id": e.user_id, "arm": e.arm, "initial_state": e.initial_state} for e in enrollments
            ],
        }
        if pilot.started_at and pilot.ends_at:
            start, end = utc(pilot.started_at), min(now, utc(pilot.ends_at))
            contacts = db.query(RetentionContact).filter_by(experiment_id=pilot.id).all()
            for arm in ("control", "treatment"):
                ids = [e.user_id for e in enrollments if e.arm == arm]
                delivered = [c for c in contacts if c.user_id in ids and c.delivered_at and utc(c.delivered_at) < end]
                contacted = {c.user_id for c in delivered}
                responded = {c.user_id for c in delivered if c.responded_at and utc(c.responded_at) < end}
                dormant = {e.user_id for e in enrollments if e.arm == arm and e.initial_state == "dormant"}
                dormant_contacted = dormant & contacted
                reactivated = {
                    uid
                    for uid in dormant_contacted
                    if any(
                        utc(c.delivered_at) < event < end
                        for c in delivered
                        if c.user_id == uid
                        for event in events(uid)
                    )
                }
                result = performance(ids, start, end)
                result.update(
                    {
                        "reactivation_rate_itt": _rate(result["returning_active_businesses"], 25),
                        "assigned_businesses": 25,
                        "remaining_businesses": len(ids),
                        "dormant_contacted": len(dormant_contacted),
                        "reactivation_rate_contacted": _rate(len(reactivated), len(dormant_contacted)),
                        "whatsapp_response_rate": _rate(len(responded), len(contacted)),
                        "delivered_businesses": len(contacted),
                        "accepted_contacts": sum(c.status == "accepted" for c in contacts if c.user_id in ids),
                        "unknown_or_failed_contacts": sum(
                            c.status in {"reserved", "unknown", "failed"} for c in contacts if c.user_id in ids
                        ),
                    }
                )
                experiment["arms"][arm] = result
            experiment["complete"] = now >= utc(pilot.ends_at)
            treatment = experiment["arms"]["treatment"]["reactivation_rate_itt"]["percent"]
            control = experiment["arms"]["control"]["reactivation_rate_itt"]["percent"]
            experiment["reactivation_lift_percentage_points"] = (
                round(treatment - control, 2) if treatment is not None and control is not None else None
            )
    recent_contacts = db.query(RetentionContact).filter(
        RetentionContact.created_at >= now - dt.timedelta(days=30),
        RetentionContact.created_at <= now,
        RetentionContact.experiment_id.is_(None),
    ).all()
    accepted = [c for c in recent_contacts if c.status in {"accepted", "delivered"}]
    dormant_contacted = {c.user_id for c in accepted if c.state == "dormant"}
    returned = {
        uid for uid in dormant_contacted
        if any(utc(c.created_at) < event <= now for c in accepted if c.user_id == uid for event in events(uid))
    }
    return {
        "as_of": now,
        "outreach_configured": outreach_configured(),
        "outreach": {
            "enabled": outreach_enabled(db),
            "channels": outreach_channels(),
            "accepted_last_30_days": {
                channel: sum(c.channel == channel for c in accepted) for channel in ("email", "whatsapp")
            },
            "unknown_or_failed": sum(c.status in {"reserved", "unknown", "failed"} for c in recent_contacts),
            "active_after_contact": _rate(len(returned), len(dormant_contacted)),
        },
        "activation_7_day": _rate(activation_success, activation_eligible),
        "retention": {str(days): _rate(counts[0], counts[1]) for days, counts in retention.items()},
        "mean_hours_to_first_invoice": round(mean(invoice_times), 2) if invoice_times else None,
        "mean_hours_to_first_payment": round(mean(payment_times), 2) if payment_times else None,
        "last_30_days": performance(list(records), now - dt.timedelta(days=30), now),
        "experiment": experiment,
        "definitions": {
            "retention": "Meaningful activity in days N through N+6 after signup; only fully matured cohorts.",
            "activity": (
                "Invoices/expenses, payments and explicit stock work; cancelled invoices, abandoned carts "
                "and automatic stock reversals excluded."
            ),
            "payments": "Recorded paid invoices, including merchant-marked payments; not proof of bank settlement.",
            "commission": (
                "Persisted fees only: manual at creation, storefront at payment. "
                "Missing legacy fees are reported, not estimated."
            ),
            "response": (
                "Inbound WhatsApp within 7 days after a delivered nudge (explicit reply context when present); "
                "not proof of causality."
            ),
            "experiment": (
                "Fixed assignments, intention-to-treat comparison over 30 days; "
                "contacted-only rate is descriptive, not causal."
            ),
            "ongoing_outreach": (
                "Ongoing existing-account outreach, not a controlled experiment. Email acceptance is not delivery. "
                "Active-after-contact uses accepted submissions and subsequent activity; it is not causal attribution."
            ),
        },
    }
