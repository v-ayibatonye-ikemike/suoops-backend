"""
Backward Compatibility Redirect.

This module redirects imports from the old routes_tax location
to the new modular routes_tax package.

DEPRECATED: Import directly from app.api.routes_tax instead:
    from app.api.routes_tax import router
"""

from app.api.routes_tax import router

__all__ = ["router"]
