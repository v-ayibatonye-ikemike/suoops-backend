"""Image optimization for user-uploaded storefront assets.

Products & logos are uploaded raw (up to 5MB) but shown at ~256px in the
storefront grid. Serving the originals makes the page feel slow and eats data.

`optimize_for_storefront` resizes the image to a sensible max side and
re-encodes it as WebP (a single, small format the browser handles) so uploads
land as ~40–150 KB instead of megabytes. Falls back to returning the original
bytes if Pillow can't open the file (e.g. SVG logos) — nothing is worse than a
missing image.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# Storefront product cards render at ~256px on mobile, ~360px on desktop; 1080
# gives us 2–3x for retina without paying for megapixel originals.
_MAX_SIDE_PX = 1080
_WEBP_QUALITY = 80
# Decompression-bomb guard: a small on-disk file can decode to gigabytes and
# OOM the worker. ~24MP covers legitimate DSLR photos with a wide margin; Pillow
# raises DecompressionBombError above this and we fall back to the original bytes.
_MAX_IMAGE_PIXELS = 24_000_000


def optimize_for_storefront(
    content: bytes,
    content_type: str,
    *,
    max_side: int = _MAX_SIDE_PX,
    quality: int = _WEBP_QUALITY,
) -> tuple[bytes, str]:
    """Return (optimized_bytes, content_type). WebP on success, original on skip.

    Skips (returns input unchanged) for:
      - SVG or non-image types (Pillow can't handle vectors).
      - Anything Pillow refuses to open (returns original — never breaks upload).
    """
    if not content or not content_type or not content_type.startswith("image/"):
        return content, content_type
    if content_type == "image/svg+xml":
        return content, content_type

    try:
        from PIL import Image, ImageOps
    except Exception:  # noqa: BLE001 — Pillow missing shouldn't break uploads
        logger.warning("Pillow unavailable; serving original image bytes")
        return content, content_type

    # Bomb guard — must be set before Image.open() to take effect.
    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS

    try:
        img = Image.open(io.BytesIO(content))
        # Respect EXIF orientation (phones upload sideways otherwise).
        img = ImageOps.exif_transpose(img)
        # Downscale ONLY (LANCZOS keeps text/logos crisp); never upscale.
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        # WebP handles both photos and logos well; flatten alpha for JPEG-source
        # inputs to avoid a black background surprise.
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.mode else "RGB")
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=quality, method=6)
        return buf.getvalue(), "image/webp"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Image optimization failed (%s); serving original", exc)
        return content, content_type
