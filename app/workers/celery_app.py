"""Celery application for the analysis worker.

Kept in its own module so the API process can import the dispatcher without
importing task code, and so `celery -A app.workers.celery_app worker` has a
single obvious entry point.

`acks_late` with `reject_on_worker_lost` means a task killed mid-run is
redelivered. That is safe because the pipeline claims a run with a conditional
update: the redelivery finds nothing queued and stops, rather than producing a
second report.
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings


def create_celery_app() -> Celery:
    settings = get_settings()
    app = Celery(
        "simumarket",
        broker=settings.broker_url,
        backend=settings.result_backend_url,
        # `include` defers the import to finalisation. Autodiscovery with
        # `force=True` would import the task module here, while this one is
        # still initialising, and the task module imports `celery_app` back.
        include=["app.workers.analysis"],
    )
    app.conf.update(
        task_default_queue=settings.celery_analysis_queue,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_always_eager=settings.celery_task_always_eager,
        task_eager_propagates=settings.celery_task_always_eager,
        task_soft_time_limit=settings.celery_analysis_soft_time_limit_seconds,
        task_time_limit=settings.celery_analysis_time_limit_seconds,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        timezone="UTC",
        enable_utc=True,
        # Only the analysis payload shape is ever queued, so JSON is enough and
        # avoids pickle's deserialisation risk.
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # docs/11: a lost queued job is reconciled from PostgreSQL state. Nothing
        # else can notice a task that never reached the broker or a worker that
        # died holding one, so the reconciler runs on a schedule.
        beat_schedule={
            "analysis-recover-stuck-runs": {
                "task": "analysis.recover",
                "schedule": float(settings.analysis_recovery_interval_seconds),
                "options": {"queue": settings.celery_analysis_queue},
            }
        },
    )
    return app


celery_app = create_celery_app()
