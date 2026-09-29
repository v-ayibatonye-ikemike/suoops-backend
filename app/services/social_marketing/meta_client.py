"""Meta Graph API client — posts to SuoOps's own Facebook Page and connected
Instagram Business account.

Endpoints used are Meta's long-stable, extensively-documented Graph API
surface for Page photo posts and the Instagram Content Publishing API (in
contrast to Mono's Lookup API, which couldn't be verified while building
that integration — these have been stable public Meta developer docs for
years). What still needs confirming on YOUR side: the connected Meta
Business App must have the `pages_manage_posts` and
`instagram_content_publish` permissions granted (Meta App Dashboard) —
having WhatsApp Business Platform access does not automatically include
these; they're a separate product/permission within the same app.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class MetaPostingError(Exception):
    """Raised when a Facebook/Instagram post attempt fails."""


class MetaGraphClient:
    def _base(self) -> str:
        return f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}"

    def post_to_facebook_page(self, image_url: str, caption: str) -> str:
        """Publish a photo post to the configured Facebook Page.

        Returns the new post's id. Raises MetaPostingError on any failure
        (missing config, network error, or a non-2xx/error response).
        """
        if not settings.FACEBOOK_PAGE_ID or not settings.FACEBOOK_PAGE_ACCESS_TOKEN:
            raise MetaPostingError("FACEBOOK_PAGE_ID / FACEBOOK_PAGE_ACCESS_TOKEN not configured")
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(
                    f"{self._base()}/{settings.FACEBOOK_PAGE_ID}/photos",
                    data={
                        "url": image_url,
                        "caption": caption,
                        "access_token": settings.FACEBOOK_PAGE_ACCESS_TOKEN,
                    },
                )
                data = resp.json()
        except httpx.HTTPError as exc:
            raise MetaPostingError(f"Facebook post failed: {exc}") from exc
        if "error" in data:
            raise MetaPostingError(f"Facebook post failed: {data['error'].get('message')}")
        post_id = data.get("post_id") or data.get("id")
        if not post_id:
            raise MetaPostingError("Facebook did not return a post id")
        return post_id

    def post_to_instagram(self, image_url: str, caption: str) -> str:
        """Publish a photo to the configured Instagram Business account.

        Two-step Content Publishing API: create a media container, then
        publish it. Returns the published media id.
        """
        if not settings.INSTAGRAM_BUSINESS_ACCOUNT_ID or not settings.FACEBOOK_PAGE_ACCESS_TOKEN:
            raise MetaPostingError("INSTAGRAM_BUSINESS_ACCOUNT_ID / FACEBOOK_PAGE_ACCESS_TOKEN not configured")
        try:
            with httpx.Client(timeout=20) as client:
                create_resp = client.post(
                    f"{self._base()}/{settings.INSTAGRAM_BUSINESS_ACCOUNT_ID}/media",
                    data={
                        "image_url": image_url,
                        "caption": caption,
                        "access_token": settings.FACEBOOK_PAGE_ACCESS_TOKEN,
                    },
                )
                create_data = create_resp.json()
                if "error" in create_data:
                    raise MetaPostingError(f"Instagram media creation failed: {create_data['error'].get('message')}")
                creation_id = create_data.get("id")
                if not creation_id:
                    raise MetaPostingError("Instagram did not return a creation id")

                publish_resp = client.post(
                    f"{self._base()}/{settings.INSTAGRAM_BUSINESS_ACCOUNT_ID}/media_publish",
                    data={
                        "creation_id": creation_id,
                        "access_token": settings.FACEBOOK_PAGE_ACCESS_TOKEN,
                    },
                )
                publish_data = publish_resp.json()
        except httpx.HTTPError as exc:
            raise MetaPostingError(f"Instagram post failed: {exc}") from exc
        if "error" in publish_data:
            raise MetaPostingError(f"Instagram publish failed: {publish_data['error'].get('message')}")
        media_id = publish_data.get("id")
        if not media_id:
            raise MetaPostingError("Instagram did not return a published media id")
        return media_id
