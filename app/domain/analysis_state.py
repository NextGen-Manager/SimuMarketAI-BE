"""Analysis state machine from docs/02.

Progress is derived from weighted completion of the stages a run actually
plans to execute. A stage that is deliberately skipped is removed from the
plan rather than credited, so the percentage never describes work that did
not happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

WORKER_LOST_FAILURE_CODE = "worker_lost"
WORKER_LOST_WARNING = "worker_lost"
WORKER_LOST_MESSAGE = (
    "Proses analisis berhenti sebelum selesai dan sudah dicoba ulang sampai batas maksimum."
)

AnalysisStage = Literal[
    "queued",
    "collecting_evidence",
    "building_context",
    "simulating",
    "calculating_finance",
    "scoring",
    "composing_report",
    "validating_report",
]

TerminalStatus = Literal["completed", "partial", "failed", "cancelled"]

AnalysisStatus = Literal[
    "queued",
    "collecting_evidence",
    "building_context",
    "simulating",
    "calculating_finance",
    "scoring",
    "composing_report",
    "validating_report",
    "completed",
    "partial",
    "failed",
    "cancelled",
]

ANALYSIS_STAGES: tuple[AnalysisStage, ...] = get_args(AnalysisStage)
TERMINAL_STATUSES: frozenset[str] = frozenset(get_args(TerminalStatus))

# Relative cost of each stage, used for weighted progress.
STAGE_WEIGHTS: dict[AnalysisStage, int] = {
    "queued": 0,
    "collecting_evidence": 15,
    "building_context": 10,
    "simulating": 25,
    "calculating_finance": 15,
    "scoring": 15,
    "composing_report": 10,
    "validating_report": 10,
}

STAGE_MESSAGES: dict[AnalysisStage, str] = {
    "queued": "Menyiapkan run",
    "collecting_evidence": "Mengumpulkan bukti lokal",
    "building_context": "Menyusun konteks",
    "simulating": "Panel persona berjalan",
    "calculating_finance": "Menghitung skenario",
    "scoring": "Menilai kelayakan",
    "composing_report": "Menyusun laporan",
    "validating_report": "Memvalidasi klaim",
}

# Used when a deployment has no agent adapter wired at all, so the stage is
# removed from the plan instead of being credited as work that happened.
SKIP_REASON_SIMULATING = (
    "Simulasi agent belum dijalankan karena integrasi OASIS belum aktif pada versi ini."
)

TERMINAL_STAGE: AnalysisStage = "validating_report"


class InvalidStageTransitionError(RuntimeError):
    """Raised when a run is moved to a stage that the plan does not allow next."""


@dataclass(frozen=True, slots=True)
class StagePlan:
    """The ordered stages a specific run intends to execute."""

    stages: tuple[AnalysisStage, ...]
    skipped: tuple[AnalysisStage, ...]

    @property
    def total_weight(self) -> int:
        return sum(STAGE_WEIGHTS[stage] for stage in self.stages)

    def next_stage(self, current: AnalysisStage) -> AnalysisStage | None:
        index = self.stages.index(current)
        if index + 1 >= len(self.stages):
            return None
        return self.stages[index + 1]

    def require_transition(self, current: AnalysisStage, target: AnalysisStage) -> None:
        if self.next_stage(current) != target:
            raise InvalidStageTransitionError(f"{current} -> {target}")

    def percent(self, completed: list[AnalysisStage]) -> int:
        total = self.total_weight
        if total <= 0:
            return 0
        done = sum(STAGE_WEIGHTS[stage] for stage in completed if stage in self.stages)
        return min(100, (done * 100 + total // 2) // total)


def build_stage_plan(*, skip: tuple[AnalysisStage, ...] = ()) -> StagePlan:
    return StagePlan(
        stages=tuple(stage for stage in ANALYSIS_STAGES if stage not in skip),
        skipped=skip,
    )


DETERMINISTIC_STAGE_PLAN = build_stage_plan(skip=("simulating",))
FULL_STAGE_PLAN = build_stage_plan()


def stage_plan_for(*, simulation_planned: bool) -> StagePlan:
    """Pick the plan a run will actually execute.

    A run that has an adapter keeps `simulating` in the plan even when the
    adapter later fails: the stage was attempted, so removing it afterwards
    would rewrite history and inflate the percentage of a partial run.
    """
    return FULL_STAGE_PLAN if simulation_planned else DETERMINISTIC_STAGE_PLAN


def plan_from_stored(skipped: list[str]) -> StagePlan:
    known = tuple(stage for stage in ANALYSIS_STAGES if stage in set(skipped))
    return build_stage_plan(skip=known)


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
