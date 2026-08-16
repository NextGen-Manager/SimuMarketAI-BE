from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID

from fastapi import FastAPI
from PIL import Image

from app.domain.receipts import ReceiptExtraction, ReceiptItemExtraction
from app.workers.artifacts import execute_receipt_ocr
from tests.support.api import client, create_business, register


class FakeReceiptOcr:
    async def extract(self, image: bytes) -> ReceiptExtraction:
        return ReceiptExtraction(
            merchant_name="Warung Contoh",
            merchant_confidence_bps=9400,
            occurred_at=datetime(2026, 8, 5, 5, 10, tzinfo=UTC),
            occurred_at_confidence_bps=8100,
            items=(
                ReceiptItemExtraction(
                    raw_name="RICE BOWL AYM",
                    quantity=2,
                    unit_price_idr=18_000,
                    confidence_bps=7600,
                ),
            ),
            total_idr=40_000,
            total_confidence_bps=9200,
            raw_text="WARUNG CONTOH\n2 x RICE BOWL AYM 18.000\nTOTAL 40.000",
            aggregate_confidence_bps=8200,
            engine_version="fake-receipt-v1",
        )


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), "white").save(output, format="PNG")
    return output.getvalue()


async def test_receipt_requires_review_and_explicit_mismatch_confirmation(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "receipt-owner@example.com", "Pemilik")
        business_id = await create_business(owner)
        product_response = await owner.post(
            "/v1/products",
            json={
                "business_id": business_id,
                "name": "Rice Bowl Ayam",
                "selling_price_idr": 18_000,
                "hpp_idr": 9_000,
            },
        )
        product_id = product_response.json()["id"]
        image = _png()
        created = await owner.post(
            "/v1/receipt-imports",
            params={"business_id": business_id},
            json={
                "file_name": "receipt.png",
                "content_type": "image/png",
                "size_bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
            },
        )
        assert created.status_code == 201, created.text
        upload_url = created.json()["upload"]["url"]
        object_key = upload_url.removeprefix("memory://upload/")
        await database_app.state.test_object_storage.write(
            object_key, image, content_type="image/png"
        )

        receipt_id = created.json()["receipt_import_id"]
        queued = await owner.post(
            f"/v1/receipt-imports/{receipt_id}/complete-upload",
            params={"business_id": business_id},
        )
        assert queued.status_code == 202, queued.text
        assert queued.json()["status"] == "queued"
        assert database_app.state.test_receipt_dispatcher.dispatched

        await execute_receipt_ocr(
            UUID(receipt_id),
            session_factory=database_app.state.test_session_factory,
            settings=database_app.state.test_settings,
            storage=database_app.state.test_object_storage,
            ocr=FakeReceiptOcr(),
        )
        review = await owner.get(
            f"/v1/receipt-imports/{receipt_id}", params={"business_id": business_id}
        )
        assert review.json()["status"] == "ready_for_review"
        assert review.json()["draft"]["total_matches_items"] is False

        draft = review.json()["draft"]
        corrected = await owner.patch(
            f"/v1/receipt-imports/{receipt_id}/draft",
            params={"business_id": business_id},
            json={
                "version": draft["version"],
                "merchant_name": "Warung Contoh",
                "occurred_at": "2026-08-05T05:10:00Z",
                "total_idr": 40_000,
                "items": [
                    {
                        "raw_name": "Rice Bowl Ayam",
                        "matched_product_id": product_id,
                        "quantity": 2,
                        "unit_price_idr": 18_000,
                    }
                ],
            },
        )
        assert corrected.status_code == 200, corrected.text
        version = corrected.json()["draft"]["version"]

        rejected = await owner.post(
            f"/v1/receipt-imports/{receipt_id}/confirm",
            params={"business_id": business_id},
            json={"version": version, "channel": "takeaway", "accept_total_mismatch": False},
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "RECEIPT_TOTAL_MISMATCH_CONFIRMATION_REQUIRED"

        accepted = await owner.post(
            f"/v1/receipt-imports/{receipt_id}/confirm",
            params={"business_id": business_id},
            json={"version": version, "channel": "takeaway", "accept_total_mismatch": True},
        )
        repeated = await owner.post(
            f"/v1/receipt-imports/{receipt_id}/confirm",
            params={"business_id": business_id},
            json={"version": version, "channel": "takeaway", "accept_total_mismatch": True},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["transaction"]["gross_total_idr"] == 36_000
        assert repeated.json()["transaction"]["id"] == accepted.json()["transaction"]["id"]
