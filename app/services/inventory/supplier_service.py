"""
Supplier Service.

Handles all supplier CRUD operations. Follows SRP by focusing
solely on supplier management.
"""

from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy.orm import Session

from app.models.inventory_models import Supplier
from app.models.inventory_schemas import SupplierCreate, SupplierUpdate

from .base import InventoryServiceBase

logger = logging.getLogger(__name__)


class SupplierService(InventoryServiceBase):
    """Service for supplier operations."""

    def __init__(self, db: Session, user_id: int):
        super().__init__(db, user_id)

    def create(self, data: SupplierCreate) -> Supplier:
        """Create a new supplier."""
        supplier = Supplier(
            user_id=self._user_id,
            name=data.name,
            contact_name=data.contact_name,
            email=data.email,
            phone=data.phone,
            address=data.address,
            notes=data.notes,
        )
        self._db.add(supplier)
        self._db.commit()
        self._db.refresh(supplier)
        logger.info("Created supplier: %s (id=%s)", supplier.name, supplier.id)
        return supplier

    def get(self, supplier_id: int) -> Supplier | None:
        """Get a supplier by ID."""
        return (
            self._db.query(Supplier)
            .filter(
                Supplier.id == supplier_id,
                Supplier.user_id == self._user_id,
            )
            .first()
        )

    def list(self, include_inactive: bool = False) -> Sequence[Supplier]:
        """List all suppliers."""
        query = self._db.query(Supplier).filter(Supplier.user_id == self._user_id)
        if not include_inactive:
            query = query.filter(Supplier.is_active.is_(True))
        return query.order_by(Supplier.name).all()

    def update(self, supplier_id: int, data: SupplierUpdate) -> Supplier | None:
        """Update a supplier."""
        supplier = self.get(supplier_id)
        if not supplier:
            return None

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(supplier, key, value)

        self._db.commit()
        self._db.refresh(supplier)
        return supplier

    def delete(self, supplier_id: int) -> bool:
        """Soft delete a supplier."""
        supplier = self.get(supplier_id)
        if not supplier:
            return False

        supplier.is_active = False
        self._db.commit()
        return True
