"""Backward compatibility redirect for tax_reporting_service.

DEPRECATED: This module has been refactored into app/services/tax_reporting/
for better SRP compliance and code organization.

All imports should continue to work via this redirect module.
New code should import from app.services.tax_reporting directly.
"""

from app.services.tax_reporting import (
    # Constants
    PIT_BANDS,
    # Service class
    TaxReportingService,
    # Utilities
    calculate_period_range,
    compute_actual_profit_by_date_range,
    compute_expenses_by_date_range,
    # Computation functions
    compute_personal_income_tax,
    compute_revenue_by_date_range,
    get_inventory_cogs,
)

__all__ = [
    "PIT_BANDS",
    "compute_personal_income_tax",
    "compute_revenue_by_date_range",
    "compute_expenses_by_date_range",
    "compute_actual_profit_by_date_range",
    "calculate_period_range",
    "get_inventory_cogs",
    "TaxReportingService",
]
