from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from starlette.datastructures import Headers, UploadFile

from app.api.routes_user_logo import _delete_branding_image, _upload_branding_image
from app.models import models


def _image_upload(filename: str) -> UploadFile:
    return UploadFile(
        file=BytesIO(b"\x89PNG\r\n\x1a\nvalid-enough-for-magic-byte-check"),
        filename=filename,
        headers=Headers({"content-type": "image/png"}),
    )


@pytest.mark.asyncio
async def test_logo_and_storefront_cover_use_separate_fields(db_session, monkeypatch):
    user = models.User(phone="+2348160000040", name="Brand Owner", pro_override=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    monkeypatch.setattr("app.api.routes_user_logo.require_plan_feature", lambda *args: None)
    monkeypatch.setattr(
        "app.utils.image_optimizer.optimize_for_storefront",
        lambda content, content_type, **kwargs: (content, content_type),
    )
    upload = AsyncMock(
        side_effect=[
            "https://cdn.example.com/logos/user.webp",
            "https://cdn.example.com/storefront-covers/user.webp",
        ]
    )
    monkeypatch.setattr("app.api.routes_user_logo.s3_client.upload_file", upload)

    await _upload_branding_image(
        file=_image_upload("logo.png"),
        current_user_id=user.id,
        db=db_session,
        field_name="logo_url",
        key_prefix="logos",
        label="Logo",
    )
    await _upload_branding_image(
        file=_image_upload("cover.png"),
        current_user_id=user.id,
        db=db_session,
        field_name="storefront_cover_url",
        key_prefix="storefront-covers",
        label="Storefront cover",
    )

    db_session.refresh(user)
    assert user.logo_url == "https://cdn.example.com/logos/user.webp"
    assert user.storefront_cover_url == "https://cdn.example.com/storefront-covers/user.webp"
    assert upload.await_args_list[0].args[1].startswith("logos/")
    assert upload.await_args_list[1].args[1].startswith("storefront-covers/")

    _delete_branding_image(
        current_user_id=user.id,
        db=db_session,
        field_name="storefront_cover_url",
        label="Storefront cover",
    )
    db_session.refresh(user)
    assert user.storefront_cover_url is None
    assert user.logo_url is not None
