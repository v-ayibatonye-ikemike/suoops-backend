"""Deprecated legacy NotificationService module.

All functionality has moved to app.services.notification.service.NotificationService,
with channel classes in app.services.notification.channels.*. This stub remains to
preserve import compatibility until callers are migrated.
"""

from app.services.notification.service import NotificationService  # re-export

__all__ = ["NotificationService"]
