"""Logo upload/delete endpoints split from routes_user.py."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.dependencies import AdminUserDep
from app.api.rate_limit import limiter
from app.db.session import get_db
from app.models import models, schemas
from app.storage.s3_client import s3_client
from app.utils.feature_gate import require_plan_feature
from app.utils.file_validation import get_safe_extension, validate_file_magic_bytes

logger = logging.getLogger(__name__)
router = APIRouter(tags=["users"])


async def _upload_branding_image(
    *,
    file: UploadFile,
    current_user_id: int,
    db: Session,
    field_name: str,
    key_prefix: str,
    label: str,
    max_side: int = 1080,
) -> schemas.MessageOut:
    try:
        require_plan_feature(db, current_user_id, "custom_branding", "Custom Storefront Branding")

        if not file.content_type or not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="File must be an image (PNG, JPG, JPEG, or SVG)")
        allowed_types = ["image/png", "image/jpeg", "image/jpg", "image/svg+xml"]
        if file.content_type not in allowed_types:
            raise HTTPException(status_code=400, detail="Unsupported image type. Allowed: PNG, JPG, JPEG, SVG")
        content = await file.read()
        max_size = 5 * 1024 * 1024
        if len(content) > max_size:
            raise HTTPException(status_code=400, detail="File size exceeds 5MB limit")

        # Validate magic bytes match claimed content type (prevents spoofed Content-Type)
        if not validate_file_magic_bytes(content, file.content_type):
            raise HTTPException(
                status_code=400, detail="File content does not match its declared type. Upload a valid image file."
            )

        user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        ext = get_safe_extension(file.filename, file.content_type)
        # Shrink branding images to a WebP the browser can render instantly (SVGs are left
        # alone by the optimizer since they can't be resized as bitmaps).
        from app.utils.image_optimizer import optimize_for_storefront

        optimized, optimized_type = optimize_for_storefront(content, file.content_type, max_side=max_side)
        if optimized_type != file.content_type:
            ext = get_safe_extension(file.filename, optimized_type)
        key = f"{key_prefix}/user_{current_user_id}.{ext}"
        logger.info(
            "Uploading %s for user %s: %s bytes, type: %s", label, current_user_id, len(optimized), optimized_type
        )
        image_url = await s3_client.upload_file(optimized, key, content_type=optimized_type)
        setattr(user, field_name, image_url)
        db.commit()
        logger.info("%s uploaded successfully for user %s: %s", label, current_user_id, image_url)
        return schemas.MessageOut(detail=f"{label} uploaded successfully")
    except HTTPException:
        raise
    except Exception as e:  # pragma: no cover
        logger.error("Failed to upload %s for user %s: %s", label, current_user_id, str(e), exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to upload {label.lower()}. Please try again.")


def _delete_branding_image(
    *,
    current_user_id: int,
    db: Session,
    field_name: str,
    label: str,
) -> schemas.MessageOut:
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not getattr(user, field_name):
        raise HTTPException(status_code=404, detail=f"No {label.lower()} configured")
    setattr(user, field_name, None)
    db.commit()
    return schemas.MessageOut(detail=f"{label} removed successfully")


@router.post("/me/logo", response_model=schemas.MessageOut)
@limiter.limit("5/minute")
async def upload_logo(
    request: Request,
    file: UploadFile = File(...),
    current_user_id: AdminUserDep = None,
    db: Annotated[Session, Depends(get_db)] = None,
):
    """Upload a custom business logo."""
    return await _upload_branding_image(
        file=file,
        current_user_id=current_user_id,
        db=db,
        field_name="logo_url",
        key_prefix="logos",
        label="Logo",
    )


@router.delete("/me/logo", response_model=schemas.MessageOut)
def delete_logo(
    current_user_id: AdminUserDep,
    db: Annotated[Session, Depends(get_db)],
):
    return _delete_branding_image(
        current_user_id=current_user_id,
        db=db,
        field_name="logo_url",
        label="Logo",
    )


@router.post("/me/storefront-cover", response_model=schemas.MessageOut)
@limiter.limit("5/minute")
async def upload_storefront_cover(
    request: Request,
    file: UploadFile = File(...),
    current_user_id: AdminUserDep = None,
    db: Annotated[Session, Depends(get_db)] = None,
):
    """Upload a landscape storefront cover."""
    return await _upload_branding_image(
        file=file,
        current_user_id=current_user_id,
        db=db,
        field_name="storefront_cover_url",
        key_prefix="storefront-covers",
        label="Storefront cover",
        max_side=1600,
    )


@router.delete("/me/storefront-cover", response_model=schemas.MessageOut)
def delete_storefront_cover(
    current_user_id: AdminUserDep,
    db: Annotated[Session, Depends(get_db)],
):
    return _delete_branding_image(
        current_user_id=current_user_id,
        db=db,
        field_name="storefront_cover_url",
        label="Storefront cover",
    )
