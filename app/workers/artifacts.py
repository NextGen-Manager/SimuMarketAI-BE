from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.correlation import set_correlation_id
from app.domain.artifacts import ObjectStorage
from app.domain.receipts import ReceiptOcr, ReceiptOcrUnavailableError
from app.integrations.object_storage import build_object_storage
from app.integrations.receipt_ocr import build_receipt_ocr
from app.persistence.database import dispose_engine, get_session_factory
from app.repositories.receipts import ReceiptWorkerRepository
from app.services.exports import execute_export, purge_expired_artifacts
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def execute_receipt_ocr(
    receipt_import_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: ObjectStorage | None = None,
    ocr: ReceiptOcr | None = None,
) -> None:
    object_storage = storage or build_object_storage(settings)
    receipt_ocr = ocr or build_receipt_ocr(settings)
    async with session_factory() as session:
        repository = ReceiptWorkerRepository(session)
        record = await repository.claim(receipt_import_id)
        if record is None:
            return
        attempt_number = await repository.next_attempt_number(record.id)
        started = monotonic()
        await repository.commit_stage(record, "preprocessing")
        try:
            stored = await object_storage.read(record.object_key)
            await repository.commit_stage(record, "extracting")
            extraction = await receipt_ocr.extract(stored.body)
            raw_key = f"receipt-ocr/{record.business_id}/{record.id}/{attempt_number}.txt"
            await object_storage.write(
                raw_key,
                extraction.raw_text.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
            await repository.save_extraction(
                record,
                extraction,
                attempt_number=attempt_number,
                duration_ms=int((monotonic() - started) * 1000),
                raw_text_object_key=raw_key,
                preprocessing_version=settings.receipt_preprocessing_version,
            )
        except ReceiptOcrUnavailableError:
            await repository.fail(
                record,
                attempt_number=attempt_number,
                engine_version=settings.receipt_ocr_engine_version,
                preprocessing_version=settings.receipt_preprocessing_version,
                duration_ms=int((monotonic() - started) * 1000),
                error_code="RECEIPT_OCR_UNAVAILABLE",
            )
        except Exception:
            logger.exception("receipt_ocr_failed", extra={"receipt_import_id": str(record.id)})
            await repository.fail(
                record,
                attempt_number=attempt_number,
                engine_version=settings.receipt_ocr_engine_version,
                preprocessing_version=settings.receipt_preprocessing_version,
                duration_ms=int((monotonic() - started) * 1000),
                error_code="RECEIPT_OCR_FAILED",
            )


async def _dispose_resources() -> None:
    await dispose_engine()


async def _run_receipt(receipt_import_id: UUID, settings: Settings) -> None:
    try:
        await execute_receipt_ocr(
            receipt_import_id,
            session_factory=get_session_factory(),
            settings=settings,
        )
    finally:
        await _dispose_resources()


async def _run_export(export_id: UUID, settings: Settings) -> None:
    try:
        await execute_export(
            export_id,
            session_factory=get_session_factory(),
            settings=settings,
            storage=build_object_storage(settings),
        )
    finally:
        await _dispose_resources()


async def _run_retention(settings: Settings) -> None:
    try:
        await purge_expired_artifacts(
            session_factory=get_session_factory(),
            settings=settings,
            storage=build_object_storage(settings),
        )
    finally:
        await _dispose_resources()


@celery_app.task(bind=True, name="receipt.process", autoretry_for=(ConnectionError, TimeoutError))
def process_receipt(self: Task, receipt_import_id: str, correlation_id: str) -> None:
    set_correlation_id(correlation_id)
    settings = get_settings()
    self.max_retries = settings.celery_artifact_max_retries
    asyncio.run(_run_receipt(UUID(receipt_import_id), settings))


@celery_app.task(bind=True, name="export.render", autoretry_for=(ConnectionError, TimeoutError))
def render_export(self: Task, export_id: str, correlation_id: str) -> None:
    set_correlation_id(correlation_id)
    settings = get_settings()
    self.max_retries = settings.celery_artifact_max_retries
    asyncio.run(_run_export(UUID(export_id), settings))


@celery_app.task(name="artifacts.retention")
def retain_artifacts() -> None:
    asyncio.run(_run_retention(get_settings()))


def enqueue_receipt(receipt_import_id: UUID, correlation_id: UUID) -> Any:
    return process_receipt.apply_async(
        args=(str(receipt_import_id), str(correlation_id)),
        queue=get_settings().celery_artifact_queue,
    )


def enqueue_export(export_id: UUID, correlation_id: UUID) -> Any:
    return render_export.apply_async(
        args=(str(export_id), str(correlation_id)),
        queue=get_settings().celery_artifact_queue,
    )
