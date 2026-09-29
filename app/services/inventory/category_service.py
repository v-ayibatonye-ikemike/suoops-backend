"""
Product Category Service - CRUD operations for product categories.

Follows SRP: Only handles category-related operations.
"""

from __future__ import annotations

import logging
from typing import Sequence

from app.models.inventory_models import ProductCategory
from app.models.inventory_schemas import ProductCategoryCreate, ProductCategoryUpdate
from app.services.inventory.base import BaseInventoryService

logger = logging.getLogger(__name__)


class CategoryService(BaseInventoryService):
    """
    Service for product category operations.

    Handles CRUD operations for organizing products into categories.
    Each user has their own set of categories.
    """

    def create_category(self, data: ProductCategoryCreate) -> ProductCategory:
        """Create a new product category."""
        category = ProductCategory(
            user_id=self._user_id,
            name=data.name,
            description=data.description,
            color=data.color,
            pack_price=data.pack_price,
        )
        self._db.add(category)
        self._db.commit()
        self._db.refresh(category)
        logger.info("Created category: %s (id=%s) for user %s", category.name, category.id, self._user_id)
        return category

    def get_category(self, category_id: int) -> ProductCategory | None:
        """Get a category by ID."""
        return (
            self._db.query(ProductCategory)
            .filter(
                ProductCategory.id == category_id,
                ProductCategory.user_id == self._user_id,
            )
            .first()
        )

    def list_categories(self, include_inactive: bool = False) -> Sequence[ProductCategory]:
        """List all categories for the user."""
        query = self._db.query(ProductCategory).filter(
            ProductCategory.user_id == self._user_id,
        )
        if not include_inactive:
            query = query.filter(ProductCategory.is_active.is_(True))
        return query.order_by(ProductCategory.name).all()

    def update_category(self, category_id: int, data: ProductCategoryUpdate) -> ProductCategory | None:
        """Update a category."""
        category = self.get_category(category_id)
        if not category:
            return None

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(category, key, value)

        self._db.commit()
        self._db.refresh(category)
        logger.info("Updated category: %s (id=%s)", category.name, category.id)
        return category

    def delete_category(self, category_id: int) -> bool:
        """Soft delete a category (set is_active=False)."""
        category = self.get_category(category_id)
        if not category:
            return False

        category.is_active = False
        self._db.commit()
        logger.info("Deleted category: %s (id=%s)", category.name, category.id)
        return True
