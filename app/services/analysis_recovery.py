"""Reconciliation for runs that no worker is executing.

docs/11 makes PostgreSQL, not Redis, the place a lost job is recovered from:
"queued job yang hilang direkonsiliasi dari PostgreSQL state", and the recovery
procedure must be able to find a stuck job and requeue it idempotently.

Two ways a run ends up with nobody working on it:

- the API committed it and the broker was unreachable, so the task was never
  queued. The run sits in `queued` with no lease;
- a worker claimed it and then died. The run sits in a working stage with a
  lease that nothing renews.

Both are invisible to Celery — a lost task cannot report that it is lost — so
the reconciler scans for them and puts them back into `queued`. Requeueing is
idempotent by construction: the conditional update only fires for a run that is
still non-terminal, and the previous attempt's rows are discarded first so a
rerun cannot append a second evidence set to the same analysis.

A run is not retried forever. After `analysis_max_attempts` it is failed with
`worker_lost`, because a run that dies at the same point every time is a defect,
and hiding it behind an endless retry loop would be the opposite of the honest
failure this product promises.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.analysis_events import AnalysisEvent, AnalysisEventWarning
from app.domain.analysis_state import (
    STAGE_MESSAGES,
    WORKER_LOST_FAILURE_CODE,
    WORKER_LOST_MESSAGE,
    WORKER_LOST_WARNING,
    AnalysisStage,
    plan_from_stored,
)
from app.persistence.models import AnalysisEventRecord, AnalysisRun
from app.repositories.analysis_worker import AnalysisWorkerRepository
from app.schemas.analysis import AnalysisWarning
from app.services.analysis_events import AnalysisEventPublisher, NullEventPublisher
from app.services.analysis_queue import AnalysisDispatcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    requeued: list[str]
    failed: list[str]

    @property
    def total(self) -> int:
        return len(self.requeued) + len(self.failed)


async def recover_stuck_runs(
    session: AsyncSession,
    *,
    settings: Settings,
    dispatcher: AnalysisDispatcher,
    publisher: AnalysisEventPublisher | None = None,
    now: datetime | None = None,
) -> RecoveryReport:
    repository = AnalysisWorkerRepository(session)
    events = publisher or NullEventPublisher()
    moment = now or datetime.now(UTC)

    stuck = await repository.find_stuck(
        now=moment,
        queue_grace_seconds=settings.analysis_queue_grace_seconds,
        limit=settings.analysis_recovery_batch_size,
    )

    requeued: list[str] = []
    failed: list[str] = []
    for run in stuck:
        if run.attempt_count >= settings.analysis_max_attempts:
            await _fail_exhausted(repository, run, events)
            failed.append(str(run.id))
            continue

        correlation_id = run.correlation_id
        analysis_id = run.id
        if not await repository.requeue(
            run,
            queue_grace_seconds=settings.analysis_queue_grace_seconds,
        ):
            # The run reached a terminal status between the scan and the update.
            continue
        logger.warning(
            "analysis_run_requeued",
            extra={
                "analysis_id": str(analysis_id),
                "correlation_id": str(correlation_id),
                "attempt_count": run.attempt_count,
            },
        )
        dispatcher.dispatch(analysis_id, correlation_id)
        requeued.append(str(analysis_id))

    return RecoveryReport(requeued=requeued, failed=failed)


async def _fail_exhausted(
    repository: AnalysisWorkerRepository,
    run: AnalysisRun,
    publisher: AnalysisEventPublisher,
) -> None:
    # `in STAGE_MESSAGES` is what narrows a stored string back to a known stage.
    # A stage the state machine no longer recognises degrades to `queued` rather
    # than crashing the reconciler on a run written by an older version.
    stage: AnalysisStage = run.current_stage if run.current_stage in STAGE_MESSAGES else "queued"
    completed: list[AnalysisStage] = [
        item for item in run.completed_stages if item in STAGE_MESSAGES
    ]
    skipped: list[AnalysisStage] = [item for item in run.skipped_stages if item in STAGE_MESSAGES]

    warnings = [AnalysisWarning.model_validate(item) for item in run.warnings]
    if all(item.code != WORKER_LOST_WARNING for item in warnings):
        warnings.append(
            AnalysisWarning(code=WORKER_LOST_WARNING, stage=stage, message=WORKER_LOST_MESSAGE)
        )

    run.status = "failed"
    run.failure_code = WORKER_LOST_FAILURE_CODE
    run.completed_at = datetime.now(UTC)
    run.lease_expires_at = None
    run.warnings = [item.model_dump(mode="json") for item in warnings]

    plan = plan_from_stored([str(item) for item in skipped])
    percent = plan.percent(completed)
    sequence = await repository.next_event_sequence(run.id)
    occurred_at = datetime.now(UTC)
    repository.add_event(
        AnalysisEventRecord(
            analysis_run_id=run.id,
            sequence=sequence,
            status="failed",
            current_stage=stage,
            completed_stages=[str(item) for item in completed],
            skipped_stages=[str(item) for item in skipped],
            percent=percent,
            message=STAGE_MESSAGES[stage],
            warnings=[item.model_dump(mode="json") for item in warnings],
            failure_code=WORKER_LOST_FAILURE_CODE,
            correlation_id=run.correlation_id,
            occurred_at=occurred_at,
        )
    )
    await repository.commit()
    logger.error(
        "analysis_run_abandoned",
        extra={
            "analysis_id": str(run.id),
            "correlation_id": str(run.correlation_id),
            "attempt_count": run.attempt_count,
        },
    )
    await publisher.publish(
        AnalysisEvent(
            event_id=str(sequence),
            analysis_id=run.id,
            status="failed",
            current_stage=stage,
            completed_stages=completed,
            skipped_stages=skipped,
            percent=percent,
            message=STAGE_MESSAGES[stage],
            warnings=[
                AnalysisEventWarning(code=item.code, stage=item.stage, message=item.message)
                for item in warnings
            ],
            failure_code=WORKER_LOST_FAILURE_CODE,
            correlation_id=run.correlation_id,
            occurred_at=occurred_at,
        )
    )
