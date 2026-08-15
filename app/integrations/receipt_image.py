from __future__ import annotations

import hashlib
from io import BytesIO
from typing import cast

from PIL import Image, ImageOps, UnidentifiedImageError

from app.domain.receipts import ReceiptImageValidationError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_and_sanitize_receipt_image(
    body: bytes,
    *,
    expected_size: int,
    expected_sha256: str,
    maximum_size: int,
    maximum_pixels: int,
) -> tuple[bytes, str]:
    if len(body) != expected_size or len(body) > maximum_size:
        raise ReceiptImageValidationError("RECEIPT_FILE_SIZE_MISMATCH")
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise ReceiptImageValidationError("RECEIPT_CHECKSUM_MISMATCH")

    if body.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
        output_format = "JPEG"
    elif body.startswith(PNG_SIGNATURE):
        mime_type = "image/png"
        output_format = "PNG"
    else:
        raise ReceiptImageValidationError("RECEIPT_MIME_NOT_ALLOWED")

    try:
        with Image.open(BytesIO(body)) as image:
            image.verify()
        with Image.open(BytesIO(body)) as image:
            if image.width * image.height > maximum_pixels:
                raise ReceiptImageValidationError("RECEIPT_IMAGE_TOO_LARGE")
            normalized = cast(Image.Image, ImageOps.exif_transpose(image))
            if output_format == "JPEG":
                normalized = normalized.convert("RGB")
            output = BytesIO()
            normalized.save(output, format=output_format, optimize=True)
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise ReceiptImageValidationError("RECEIPT_IMAGE_INVALID") from exc
    return output.getvalue(), mime_type
