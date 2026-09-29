"""Tax Reporting Module.

This module provides tax report generation with multi-period aggregation support.
Refactored from monolithic tax_reporting_service.py for better SRP compliance.

Sub-modules:
- computations: PIT and CIT calculation and profit computation functions
- period_utils: Date range calculations for different period types
- inventory_integration: COGS data from inventory system
- reporting_service: Main TaxReportingService class
"""

from .computations import (
    CIT_THRESHOLDS,
    PIT_BANDS,
    compute_actual_profit_by_date_range,
    compute_company_income_tax,
    compute_expenses_by_date_range,
    compute_personal_income_tax,
    compute_revenue_by_date_range,
)
from .inventory_integration import get_inventory_cogs
from .period_utils import calculate_period_range
from .reporting_service import TaxReportingService

__all__ = [
    # Constants
    "PIT_BANDS",
    "CIT_THRESHOLDS",
    # Computation functions
    "compute_personal_income_tax",
    "compute_company_income_tax",
    "compute_revenue_by_date_range",
    "compute_expenses_by_date_range",
    "compute_actual_profit_by_date_range",
    # Utilities
    "calculate_period_range",
    "get_inventory_cogs",
    # Service class
    "TaxReportingService",
]
