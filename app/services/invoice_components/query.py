"""Query/list helpers for invoices."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.exceptions import InvoiceNotFoundError
from app.models import models

logger = logging.getLogger(__name__)


class InvoiceQueryMixin:
    db: Session

    def _base_invoice_query(
        self,
        issuer_id: int,
        invoice_type: str | None,
        start_date: dt.date | None,
        end_date: dt.date | None,
        search: str | None,
    ):
        """Shared invoice filter (issuer + storefront-hidden + type + date +
        search). Used by both the list page and the per-status counts so they
        never disagree."""
        from sqlalchemy import String as _String
        from sqlalchemy import cast as _cast
        from sqlalchemy import func as sa_func
        from sqlalchemy import or_ as _sa_or

        query = self.db.query(models.Invoice).filter(models.Invoice.issuer_id == issuer_id)

        # Abandoned/unpaid storefront orders (an online order the customer
        # started but never paid for) aren't real seller invoices yet — keep
        # them out of the list + count until payment confirms.
        query = query.filter(
            _sa_or(
                models.Invoice.channel.is_(None),
                models.Invoice.channel != "storefront",
                models.Invoice.status != "pending",
            )
        )

        if invoice_type:
            query = query.filter(models.Invoice.invoice_type == invoice_type)

        # Date filters applied at SQL level so pagination counts are accurate.
        date_col = sa_func.coalesce(models.Invoice.due_date, models.Invoice.created_at)
        if start_date:
            query = query.filter(sa_func.date(date_col) >= start_date)
        if end_date:
            query = query.filter(sa_func.date(date_col) <= end_date)

        # Free-text search across invoice id, amount and customer name — done in
        # SQL so it spans ALL invoices, not just the current page.
        if search and search.strip():
            like = f"%{search.strip()}%"
            query = query.outerjoin(models.Customer, models.Invoice.customer_id == models.Customer.id).filter(
                _sa_or(
                    models.Invoice.invoice_id.ilike(like),
                    _cast(models.Invoice.amount, _String).ilike(like),
                    models.Customer.name.ilike(like),
                )
            )
        return query

    def count_invoices_by_status(
        self,
        issuer_id: int,
        invoice_type: str | None = None,
        start_date: dt.date | None = None,
        end_date: dt.date | None = None,
        search: str | None = None,
    ) -> dict[str, int]:
        """Per-status counts (respecting type/date/search but NOT status) plus an
        ``all`` total — powers the filter chips accurately across every page."""
        from sqlalchemy import func as sa_func

        base = self._base_invoice_query(issuer_id, invoice_type, start_date, end_date, search)
        rows = base.with_entities(models.Invoice.status, sa_func.count()).group_by(models.Invoice.status).all()
        counts: dict[str, int] = {str(status): int(n) for status, n in rows}
        counts["all"] = sum(counts.values())
        return counts

    def list_invoices(
        self,
        issuer_id: int,
        invoice_type: str | None = None,
        skip: int = 0,
        limit: int = 50,
        start_date: dt.date | None = None,
        end_date: dt.date | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> tuple[list[models.Invoice], int]:
        """Return a page of invoices and the total count matching the filters."""
        query = self._base_invoice_query(issuer_id, invoice_type, start_date, end_date, search)

        if status == "unpaid":
            query = query.filter(models.Invoice.status.in_(("pending", "awaiting_confirmation")))
        elif status and status != "all":
            query = query.filter(models.Invoice.status == status)

        # Get total count before applying pagination
        total = query.count()

        invoices = (
            query.options(
                joinedload(models.Invoice.customer),
                joinedload(models.Invoice.issuer),
                joinedload(models.Invoice.created_by),
                joinedload(models.Invoice.status_updated_by),
            )
            .order_by(models.Invoice.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        if self.cache and invoices:
            self.cache.set_invoice_list(issuer_id, invoices)
        return invoices, total

    def get_invoice(self, issuer_id: int, invoice_id: str) -> models.Invoice:
        invoice = (
            self.db.query(models.Invoice)
            .options(
                selectinload(models.Invoice.lines),
                joinedload(models.Invoice.customer),
                joinedload(models.Invoice.issuer),
                joinedload(models.Invoice.created_by),
                joinedload(models.Invoice.status_updated_by),
            )
            .filter(models.Invoice.invoice_id == invoice_id, models.Invoice.issuer_id == issuer_id)
            .one_or_none()
        )
        if not invoice:
            raise InvoiceNotFoundError(invoice_id)

        if self.cache:
            self.cache.set_invoice(invoice)
        if invoice.paid_at is not None and invoice.paid_at.tzinfo is None:
            invoice.paid_at = invoice.paid_at.replace(tzinfo=dt.timezone.utc)
        return invoice
