"""
Tests for OCR Service - Receipt image parsing.

Tests cover:
- Image preprocessing (resize, format conversion)
- OpenAI Vision API integration
- Data extraction and validation
- Error handling (invalid images, API failures)
- Nigerian context handling
"""

import base64
import io
import json
from asyncio import to_thread
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from PIL import Image

from app.services.ocr_service import OCRService


@pytest.fixture
def ocr_service():
    """Create OCR service instance."""
    return OCRService()


@pytest.fixture
def sample_image_bytes():
    """Create sample image for testing."""
    img = Image.new("RGB", (800, 600), color="white")
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="JPEG")
    return img_bytes.getvalue()


@pytest.fixture
def large_image_bytes():
    """Create large image that needs resizing."""
    img = Image.new("RGB", (4000, 3000), color="white")
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="JPEG")
    return img_bytes.getvalue()


@pytest.fixture
def mock_vision_response():
    """Mock successful OpenAI Vision API response."""
    return {
        "customer_name": "Jane Doe",
        "business_name": "Beauty Palace",
        "amount": "50000",
        "currency": "NGN",
        "items": [{"description": "Hair braiding", "quantity": 1, "unit_price": "50000"}],
        "date": "2025-10-30",
        "confidence": "high",
        "raw_text": "BEAUTY PALACE\nCustomer: Jane Doe\nHair braiding: ₦50,000\nTotal: ₦50,000",
    }


# ========== Image Preprocessing Tests ==========


def test_preprocess_valid_image(ocr_service, sample_image_bytes):
    """Test preprocessing of valid JPEG image."""
    result = ocr_service._preprocess_image(sample_image_bytes)

    assert result is not None
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_preprocess_large_image_resizes(ocr_service, large_image_bytes):
    """Test that large images are resized to max dimensions."""
    result = ocr_service._preprocess_image(large_image_bytes)

    assert result is not None

    # Verify resized
    img = Image.open(io.BytesIO(result))
    assert img.size[0] <= 2048
    assert img.size[1] <= 2048


def test_preprocess_converts_rgba_to_rgb(ocr_service):
    """Test that RGBA images are converted to RGB."""
    # Create RGBA image
    img = Image.new("RGBA", (800, 600), color=(255, 255, 255, 128))
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")

    result = ocr_service._preprocess_image(img_bytes.getvalue())

    assert result is not None
    # Verify converted to RGB
    result_img = Image.open(io.BytesIO(result))
    assert result_img.mode == "RGB"


def test_preprocess_invalid_image_returns_none(ocr_service):
    """Test that invalid image data returns None."""
    invalid_bytes = b"not an image"
    result = ocr_service._preprocess_image(invalid_bytes)

    assert result is None


def test_preprocess_empty_bytes_returns_none(ocr_service):
    """Test that empty bytes returns None."""
    result = ocr_service._preprocess_image(b"")

    assert result is None


# ========== Base64 Encoding Tests ==========


def test_encode_image(ocr_service, sample_image_bytes):
    """Test image encoding to base64."""
    result = ocr_service._encode_image(sample_image_bytes)

    assert isinstance(result, str)
    assert len(result) > 0

    # Verify can decode back
    decoded = base64.b64decode(result)
    assert decoded == sample_image_bytes


# ========== Vision API Tests ==========


@pytest.mark.asyncio
async def test_call_vision_api_success(ocr_service, sample_image_bytes, mock_vision_response):
    """Test successful Vision API call."""
    base64_image = ocr_service._encode_image(sample_image_bytes)

    # Mock httpx response
    mock_response = Mock()
    mock_response.json.return_value = {"choices": [{"message": {"content": json.dumps(mock_vision_response)}}]}
    mock_response.raise_for_status = Mock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)

        result = await ocr_service._call_vision_api(base64_image, context=None)

        assert result == mock_vision_response
        assert result["customer_name"] == "Jane Doe"
        assert result["amount"] == "50000"


@pytest.mark.asyncio
async def test_call_vision_api_with_context(ocr_service, sample_image_bytes):
    """Test Vision API call includes business context."""
    base64_image = ocr_service._encode_image(sample_image_bytes)

    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"amount": "10000", "items": []})}}]
    }
    mock_response.raise_for_status = Mock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_post = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__.return_value.post = mock_post

        await ocr_service._call_vision_api(base64_image, context="hair salon")

        # Verify context was included in prompt
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        prompt = payload["messages"][0]["content"][0]["text"]
        assert "hair salon" in prompt.lower()


# ========== Validation Tests ==========


def test_validate_and_format_success(ocr_service, mock_vision_response):
    """Test validation of successful OCR result."""
    result = ocr_service._validate_and_format(mock_vision_response)

    assert result["success"] is True
    assert result["amount"] == "50000"
    assert result["customer_name"] == "Jane Doe"
    assert len(result["items"]) == 1
    assert result["items"][0]["description"] == "Hair braiding"


def test_validate_and_format_no_amount_fails(ocr_service):
    """Test that missing amount causes validation failure."""
    data = {"amount": "0", "items": []}

    result = ocr_service._validate_and_format(data)

    assert result["success"] is False
    assert "amount" in result["error"].lower()


def test_validate_and_format_invalid_amount_format(ocr_service):
    """Test that invalid amount format is handled."""
    data = {"amount": "not-a-number", "items": []}

    result = ocr_service._validate_and_format(data)

    assert result["success"] is False
    assert "invalid amount" in result["error"].lower()


def test_validate_and_format_creates_default_item_if_none(ocr_service):
    """Test that default item is created if none extracted."""
    data = {"amount": "50000", "items": []}

    result = ocr_service._validate_and_format(data)

    assert result["success"] is True
    assert len(result["items"]) == 1
    assert result["items"][0]["description"] == "Service (from receipt)"
    assert result["items"][0]["unit_price"] == "50000"


def test_validate_and_format_handles_commas_in_amount(ocr_service):
    """Test that commas in amounts are removed."""
    data = {"amount": "50,000.00", "items": []}

    result = ocr_service._validate_and_format(data)

    assert result["success"] is True
    # Should normalize to string without trailing zeros
    assert result["amount"] in ["50000", "50000.00"]  # Accept both formats


def test_validate_and_format_sets_defaults_for_missing_fields(ocr_service):
    """Test that missing optional fields get defaults."""
    data = {
        "amount": "50000"
        # Missing: customer_name, business_name, items, etc.
    }

    result = ocr_service._validate_and_format(data)

    assert result["success"] is True
    assert result["customer_name"] == "Unknown"
    assert result["business_name"] == ""
    assert result["currency"] == "NGN"
    assert result["confidence"] == "medium"


# ========== Full Parse Tests ==========


@pytest.mark.asyncio
async def test_parse_receipt_success(ocr_service, sample_image_bytes, mock_vision_response):
    """Test full receipt parsing flow."""
    # Mock Vision API
    mock_response = Mock()
    mock_response.json.return_value = {"choices": [{"message": {"content": json.dumps(mock_vision_response)}}]}
    mock_response.raise_for_status = Mock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)

        result = await ocr_service.parse_receipt(sample_image_bytes, context="hair salon")

        assert result["success"] is True
        assert result["customer_name"] == "Jane Doe"
        assert result["amount"] == "50000"
        assert result["confidence"] == "high"


@pytest.mark.asyncio
async def test_parse_receipt_invalid_image(ocr_service):
    """Test parsing with invalid image data."""
    invalid_bytes = b"not an image"

    result = await ocr_service.parse_receipt(invalid_bytes)

    assert result["success"] is False
    assert "invalid image" in result["error"].lower()


@pytest.mark.asyncio
async def test_parse_receipt_api_error(ocr_service, sample_image_bytes):
    """Test handling of Vision API errors."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(side_effect=Exception("API Error"))

        result = await ocr_service.parse_receipt(sample_image_bytes)

        assert result["success"] is False
        assert "error" in result["error"].lower()


@pytest.mark.asyncio
async def test_parse_receipt_no_api_key(sample_image_bytes):
    """Test that missing API key causes OCR failure."""
    from app.core.config import settings

    orig_key = settings.OPENAI_API_KEY
    try:
        settings.OPENAI_API_KEY = None
        ocr = OCRService()

        # Should fail fast with no API key
        result = await ocr.parse_receipt(sample_image_bytes)
        assert result["success"] is False
    finally:
        settings.OPENAI_API_KEY = orig_key


# ========== Prompt Building Tests ==========


def test_build_prompt_without_context(ocr_service):
    """Test prompt building without business context."""
    prompt = ocr_service._build_prompt(context=None)

    assert "Nigerian" in prompt
    assert "Naira" in prompt or "NGN" in prompt
    assert "JSON" in prompt
    assert "customer_name" in prompt
    assert "amount" in prompt


def test_build_prompt_with_context(ocr_service):
    """Test prompt building with business context."""
    prompt = ocr_service._build_prompt(context="hair salon invoice")

    assert "hair salon invoice" in prompt.lower()
    assert "context" in prompt.lower()


# ========== Integration Tests ==========


@pytest.mark.asyncio
@pytest.mark.integration
async def test_parse_real_receipt_image(ocr_service):
    """
    Integration test with real image (requires OPENAI_API_KEY).

    Skipped in CI - run manually to test with real images.
    """
    pytest.skip("Integration test - requires real API key and image")

    # To test manually:
    # 1. Set OPENAI_API_KEY environment variable
    # 2. Place test receipt image as 'test_receipt.jpg'
    # 3. pytest -k test_parse_real_receipt_image -v

    image_bytes = await to_thread(Path("test_receipt.jpg").read_bytes)

    result = await ocr_service.parse_receipt(image_bytes, context="test receipt")

    print(f"\nOCR Result: {json.dumps(result, indent=2)}")

    assert result["success"] is True
    assert float(result["amount"]) > 0
