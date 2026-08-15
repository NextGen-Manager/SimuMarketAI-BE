"""Per-run environment, trace allocation, and budget derived from settings.

docs/02 treats the OASIS trace as an artifact of one run, not a shared file.
Every run therefore gets its own directory keyed by the analysis and a random
suffix, and nothing here ever deletes a previous run's directory the way the
upstream quick-start example does.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.core.config import Settings
from app.domain.agents import (
    CohortManifest,
    RunManifest,
    SimulationBudget,
    TraceArtifact,
    build_cohort_manifest,
)

TRACE_FILE_NAME = "trace.db"


def budget_from_settings(settings: Settings) -> SimulationBudget:
    return SimulationBudget(
        persona_count=settings.oasis_cohort_size,
        round_limit=settings.oasis_round_limit,
        token_budget=settings.oasis_token_budget,
        max_output_tokens_per_stage=settings.oasis_max_output_tokens_per_stage,
        concurrency_limit=settings.oasis_concurrency_limit,
        wall_clock_seconds=settings.oasis_wall_clock_seconds,
        retry_limit=settings.oasis_retry_limit,
    )


def cohort_from_settings(settings: Settings) -> CohortManifest:
    return build_cohort_manifest(
        cohort_version=settings.oasis_cohort_version,
        size=settings.oasis_cohort_size,
    )


def environment_id(analysis_id: UUID) -> str:
    """Unique environment name for one attempt at one run.

    The random suffix matters as much as the analysis ID: a retried run must
    not reuse the previous attempt's trace file, otherwise two attempts become
    indistinguishable in the audit trail.
    """
    return f"analysis-{analysis_id}-{uuid4().hex[:12]}"


def allocate_trace_directory(settings: Settings, environment: str) -> Path:
    root = Path(settings.oasis_trace_root).expanduser()
    directory = root / environment
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def trace_artifact(directory: Path, *, retention_days: int) -> TraceArtifact:
    trace_path = directory / TRACE_FILE_NAME
    checksum: str | None = None
    byte_size: int | None = None
    if trace_path.exists():
        payload = trace_path.read_bytes()
        checksum = hashlib.sha256(payload).hexdigest()
        byte_size = len(payload)
    return TraceArtifact(
        object_key=str(trace_path),
        checksum=checksum,
        byte_size=byte_size,
        retention_days=retention_days,
    )


def input_snapshot_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_manifest(
    settings: Settings,
    *,
    adapter_id: str,
    environment: str,
    cohort: CohortManifest,
    budget: SimulationBudget,
    trace: TraceArtifact,
    evidence_snapshot_version: str,
    snapshot_hash: str,
) -> RunManifest:
    return RunManifest(
        environment_id=environment,
        adapter_id=adapter_id,
        provider=settings.oasis_provider,
        model_id=settings.oasis_model_id,
        oasis_version=settings.oasis_package_version,
        camel_version=settings.camel_package_version,
        prompt_version=settings.oasis_prompt_version,
        cohort=cohort,
        seed=settings.oasis_seed,
        budget=budget,
        trace=trace,
        evidence_snapshot_version=evidence_snapshot_version,
        input_snapshot_hash=snapshot_hash,
        created_at=datetime.now(UTC),
    )
