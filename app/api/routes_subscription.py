"""Backward compatibility redirect for routes_subscription.

DEPRECATED: This module has been refactored into app/api/routes_subscription/
for better SRP compliance and code organization.

All imports should continue to work via this redirect module.
New code should import from app.api.routes_subscription directly.
"""

from app.api.routes_subscription import PLAN_PRICES, router

__all__ = ["router", "PLAN_PRICES"]
