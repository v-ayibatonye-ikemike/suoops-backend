"""Inventory COGS integration for tax reporting.

Provides Cost of Goods Sold (COGS) data from inventory
system for accurate profit calculations in tax reports.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def get_inventory_cogs(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
) -> dict:
    """
    Get Cost of Goods Sold (COGS) data from inventory for a period.

    This integrates inventory tracking with tax reporting for accurate
    profit calculations. COGS = Beginning Inventory + Purchases - Ending Inventory

    Args:
        db: Database session
        user_id: User ID
        start_date: Start of the period
        end_date: End of the period

    Returns:
        dict with cogs_amount, purchases_amount, current_inventory_value
    """
    try:
        from app.services.inventory import build_inventory_service

        # Convert dates to datetime with timezone for inventory service
        start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)

        inventory_service = build_inventory_service(db, user_id)
        cogs_data = inventory_service.get_cogs_for_period(start_dt, end_dt)

        return {
            "cogs_amount": Decimal(str(cogs_data.get("cogs_amount", 0))),
            "purchases_amount": Decimal(str(cogs_data.get("purchases_amount", 0))),
            "current_inventory_value": Decimal(str(cogs_data.get("current_inventory_value", 0))),
        }
    except Exception as e:
        logger.warning(f"Could not get inventory COGS for user {user_id}: {e}")
        return {
            "cogs_amount": Decimal(0),
            "purchases_amount": Decimal(0),
            "current_inventory_value": Decimal(0),
        }
