"""Backward compatibility redirect for routes_inventory.

DEPRECATED: This module has been refactored into app/api/routes_inventory/
for better SRP compliance and code organization.

All imports should continue to work via this redirect module.
New code should import from app.api.routes_inventory directly.
"""

from app.api.routes_inventory import router

__all__ = ["router"]
