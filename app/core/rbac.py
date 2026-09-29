from typing import Iterable

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models import models


def require_roles(allowed: Iterable[str]):
    allowed_set = set(r.lower() for r in allowed)

    async def _dependency(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)) -> models.User:
        user = db.query(models.User).filter(models.User.id == user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid user")
        if user.role.lower() not in allowed_set:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return _dependency


admin_required = require_roles(["admin"])  # Convenience dependency
staff_or_admin_required = require_roles(["staff", "admin"])
