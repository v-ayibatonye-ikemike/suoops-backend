"""
OCR Service for extracting invoice/receipt data from images.

Uses OpenAI Vision API (GPT-4V) for intelligent receipt parsing.
Supports Nigerian receipts with local context (Naira, common Nigerian business names).

Design:
- Single responsibility: Image → Structured invoice data
- Provider abstraction: Easy to swap OCR backend
- Validation: Ensures extracted data meets minimum requirements
- Error handling: Graceful degradation with partial results

Cost: ~₦20 per image (~$0.01 at ₦1,500/$)
Accuracy: ~85-95% for clear photos
"""

import base64
import io
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

# Per-user monthly OCR cap. At ~$0.01/image, 20 scans × 50K users = $10K/month.
# Cap at 10/user/month → $5K max, but most users use 2-3 → ~$1,250 realistic.
OCR_MONTHLY_LIMIT = 10


def check_ocr_quota(user_id: int) -> bool:
    """Return True if the user is within their monthly OCR quota."""
    try:
        from datetime import datetime, timezone

        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        key = f"ocr:usage:{user_id}:{month}"
        count = int(r.get(key) or 0)
        return count < OCR_MONTHLY_LIMIT
    except Exception:
        return True  # fail-open so users aren't blocked by Redis outage


def record_ocr_use(user_id: int) -> None:
    """Increment the user's monthly OCR counter."""
    try:
        from datetime import datetime, timezone

        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        key = f"ocr:usage:{user_id}:{month}"
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, 35 * 24 * 3600)  # auto-expire after 35 days
        pipe.execute()
    except Exception:
        pass


class OCRService:
    """
    Extract structured invoice data from receipt/invoice images.

    Uses OpenAI GPT-4 Vision for intelligent parsing with Nigerian context.
    """

    def __init__(self):
        from app.core.config import settings

        self.api_key = settings.OPENAI_API_KEY
        if not self.api_key:
            logger.warning("OPENAI_API_KEY not set - OCR will fail")

        self.api_url = "https://api.openai.com/v1/chat/completions"
        self.model = "gpt-4o"  # GPT-4 Turbo with vision
        self.max_image_size = (2048, 2048)  # Resize large images

    async def parse_receipt(self, image_bytes: bytes, context: Optional[str] = None) -> dict:
        """
        Extract invoice data from receipt image.

        Args:
            image_bytes: Raw image bytes (JPEG, PNG, etc.)
            context: Optional business context (e.g., "hair salon invoice")

        Returns:
            dict with keys:
                - success: bool
                - customer_name: str (may be "Unknown")
                - amount: str (decimal format)
                - items: list[dict] with description, quantity, unit_price
                - date: str (ISO format if found)
                - business_name: str (if visible)
                - confidence: str (high/medium/low)
                - raw_text: str (all extracted text)
                - error: str (if success=False)

        Example:
            result = await ocr.parse_receipt(image_bytes, "hair salon")
            if result["success"]:
                amount = result["amount"]
                items = result["items"]
        """
        try:
            # Fail fast if no API key configured
            if not self.api_key:
                return {"success": False, "error": "OCR processing error: OPENAI_API_KEY not configured"}

            # Validate and preprocess image
            processed_image = self._preprocess_image(image_bytes)
            if not processed_image:
                return {"success": False, "error": "Invalid image format or corrupted image"}

            # Convert to base64 for API
            base64_image = self._encode_image(processed_image)

            # Call OpenAI Vision API
            extracted_data = await self._call_vision_api(base64_image, context)

            # Validate and structure response
            return self._validate_and_format(extracted_data)

        except Exception as e:
            # Log as warning - OCR failures are expected for invalid images or API issues
            logger.warning("OCR parsing failed: %s", e)
            return {"success": False, "error": f"OCR processing error: {str(e)}"}

    def _preprocess_image(self, image_bytes: bytes) -> Optional[bytes]:
        """
        Validate and resize image if needed.

        Returns:
            Processed image bytes or None if invalid
        """
        try:
            img = Image.open(io.BytesIO(image_bytes))

            # Convert to RGB if needed (handles RGBA, grayscale, etc.)
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Resize if too large (API limits)
            if img.size[0] > self.max_image_size[0] or img.size[1] > self.max_image_size[1]:
                img.thumbnail(self.max_image_size, Image.Resampling.LANCZOS)
                logger.info("Resized image from %s to fit %s", img.size, self.max_image_size)

            # Convert back to bytes
            output = io.BytesIO()
            img.save(output, format="JPEG", quality=85)
            return output.getvalue()

        except Exception as e:
            # User-provided invalid image - log as warning to avoid Sentry noise
            logger.warning("Image preprocessing failed: %s", e)
            return None

    def _encode_image(self, image_bytes: bytes) -> str:
        """Convert image bytes to base64 string."""
        return base64.b64encode(image_bytes).decode("utf-8")

    async def _call_vision_api(self, base64_image: str, context: Optional[str]) -> dict:
        """
        Call OpenAI Vision API to extract receipt data.

        Returns structured JSON with invoice details.
        """
        # Build context-aware prompt
        prompt = self._build_prompt(context)

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "auto",  # Let OpenAI choose; "high" costs 2-4× more
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 1000,
            "temperature": 0.1,  # Low temperature for consistent parsing
            "response_format": {"type": "json_object"},  # Force JSON response
        }

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(self.api_url, json=payload, headers=headers)
            response.raise_for_status()

            result = response.json()
            content = result["choices"][0]["message"]["content"]

            # Parse JSON response
            import json

            return json.loads(content)

    def _build_prompt(self, context: Optional[str]) -> str:
        """
        Build context-aware prompt for vision API.

        Includes Nigerian business context and currency handling.
        """
        base_prompt = """
You are an expert at extracting structured data from Nigerian receipts and invoices.

Analyze this image and extract the following information in JSON format:

{
  "customer_name": "string (customer/client name, or 'Unknown' if not found)",
  "business_name": "string (business/shop name if visible)",
  "amount": "string (total amount as decimal, e.g., '50000' or '50000.00')",
  "currency": "string (NGN/Naira if Nigerian, otherwise currency code)",
  "items": [
    {
      "description": "string (item/service name)",
      "quantity": "number (default 1 if not specified)",
      "unit_price": "string (price per item as decimal)"
    }
  ],
  "date": "string (ISO format YYYY-MM-DD if found, or null)",
  "confidence": "string (high/medium/low based on image clarity)",
  "raw_text": "string (all text visible in the image)"
}

Guidelines:
- Look for amounts in Naira (₦, NGN, N) format
- Common Nigerian services: hair styling, braiding, makeup, wigs, nails, etc.
- If multiple amounts visible, choose the TOTAL (usually largest or at bottom)
- Remove commas from amounts (50,000 → 50000)
- Extract all line items if visible (e.g., "2x Wig @ 12500")
- Date formats: DD/MM/YYYY or DD-MM-YYYY common in Nigeria
- Be generous with "Unknown" if data is unclear
"""

        if context:
            base_prompt += f"\n\nBusiness context: {context}\n"
            base_prompt += "Use this context to interpret ambiguous items/services.\n"

        return base_prompt

    def _validate_and_format(self, data: dict) -> dict:
        """
        Validate extracted data and format for invoice creation.

        Ensures minimum requirements met (at least amount extracted).
        """
        try:
            # Must have amount
            amount_str = data.get("amount", "0")
            try:
                amount = Decimal(amount_str.replace(",", ""))
                if amount <= 0:
                    return {"success": False, "error": "No valid amount found in image"}
            except (InvalidOperation, ValueError):
                return {"success": False, "error": f"Invalid amount format: {amount_str}"}

            # Format items
            items = data.get("items", [])
            if not items:
                # Create default item if none extracted
                items = [{"description": "Service (from receipt)", "quantity": 1, "unit_price": str(amount)}]

            # Ensure each item has required fields
            formatted_items = []
            for item in items:
                formatted_items.append(
                    {
                        "description": item.get("description", "Unknown item"),
                        "quantity": item.get("quantity", 1),
                        "unit_price": item.get("unit_price", "0"),
                    }
                )

            return {
                "success": True,
                "customer_name": data.get("customer_name", "Unknown"),
                "business_name": data.get("business_name", ""),
                "amount": str(amount),
                "currency": data.get("currency", "NGN"),
                "items": formatted_items,
                "date": data.get("date"),
                "confidence": data.get("confidence", "medium"),
                "raw_text": data.get("raw_text", ""),
            }

        except Exception as e:
            logger.warning("Validation failed: %s", e)
            return {"success": False, "error": f"Data validation error: {str(e)}"}
