import hashlib
from io import BytesIO

import pytest
from PIL import Image

from app.domain.receipts import ReceiptImageValidationError
from app.integrations.receipt_image import validate_and_sanitize_receipt_image
from app.integrations.receipt_ocr import parse_receipt_lines


def test_receipt_parser_extracts_integer_money_without_llm() -> None:
    extraction = parse_receipt_lines(
        ["WARUNG CONTOH", "05/08/2026", "2 x RICE BOWL AYM 18.000", "TOTAL 36.000"],
        engine_version="test",
    )
    assert extraction.items[0].quantity == 2
    assert extraction.items[0].unit_price_idr == 18_000
    assert extraction.total_idr == 36_000
    assert extraction.occurred_at is not None


def test_magic_bytes_reject_a_file_that_only_claims_to_be_an_image() -> None:
    body = b"not-an-image"
    with pytest.raises(ReceiptImageValidationError) as error:
        validate_and_sanitize_receipt_image(
            body,
            expected_size=len(body),
            expected_sha256=hashlib.sha256(body).hexdigest(),
            maximum_size=1024,
            maximum_pixels=1024,
        )
    assert error.value.code == "RECEIPT_MIME_NOT_ALLOWED"


def test_sanitized_image_has_no_exif_metadata() -> None:
    source = BytesIO()
    Image.new("RGB", (16, 16), "white").save(source, format="JPEG", exif=b"Exif\x00\x00demo")
    body = source.getvalue()
    sanitized, mime = validate_and_sanitize_receipt_image(
        body,
        expected_size=len(body),
        expected_sha256=hashlib.sha256(body).hexdigest(),
        maximum_size=1_000_000,
        maximum_pixels=1_000_000,
    )
    with Image.open(BytesIO(sanitized)) as image:
        assert not image.getexif()
    assert mime == "image/jpeg"
