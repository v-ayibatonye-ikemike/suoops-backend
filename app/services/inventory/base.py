"""
Base inventory service with shared functionality.

Provides the foundation for all inventory-related operations.
Follows OOP principles:
- Single Responsibility: Only handles base configuration and shared utilities
- Dependency Injection: Database session injected via constructor
- Encapsulation: Protected attributes with underscore prefix
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class BaseInventoryService:
    """
    Base service class with shared inventory functionality.

    All inventory-related services inherit from this class
    to share database session and user context.
    """

    def __init__(self, db: Session, user_id: int):
        """
        Initialize the base inventory service.

        Args:
            db: SQLAlchemy database session
            user_id: ID of the authenticated user (business owner)
        """
        self._db = db
        self._user_id = user_id

    @property
    def db(self) -> Session:
        """Database session accessor."""
        return self._db

    @property
    def user_id(self) -> int:
        """User ID accessor."""
        return self._user_id


# Alias for backward compatibility
InventoryServiceBase = BaseInventoryService
