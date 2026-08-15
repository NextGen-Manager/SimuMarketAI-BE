"""Small deterministic helpers shared by the OASIS council orchestrator."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from app.domain.agents import (
    AGENT_ROLES,
    AgentRole,
    OasisBudgetExceededError,
    OasisSchemaError,
    OasisTimeoutError,
    SimulationRequest,
)
from app.integrations.oasis.prompts import CouncilMember, council_for

RoundKind = str

ROUND_PLAN: tuple[tuple[int, RoundKind], ...] = (
    (0, "baseline_interview"),
    (1, "exposure"),
    (2, "interaction"),
    (3, "intervention"),
)


def extract_json(content: str) -> dict[str, Any]:
    """Extract one JSON object without accepting non-object model output."""
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise OasisSchemaError("Respons agent tidak memuat object JSON.")
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError as error:
        raise OasisSchemaError("Respons agent bukan JSON yang valid.") from error
    if not isinstance(parsed, dict):
        raise OasisSchemaError("Respons agent bukan object JSON.")
    return parsed


def rounds_for(round_limit: int) -> tuple[tuple[int, RoundKind], ...]:
    # A hard limit is more important than silently filling the documented plan.
    return ROUND_PLAN[: min(round_limit, len(ROUND_PLAN))]


def build_roster(request: SimulationRequest) -> tuple[tuple[AgentRole, CouncilMember], ...]:
    """Map every council member to the stable runtime agent index order."""
    return tuple((role, member) for role in AGENT_ROLES for member in council_for(role, request))


@dataclass(slots=True)
class RunBudget:
    token_budget: int
    deadline: float
    consumed: int = 0

    def spend(self, tokens: int) -> None:
        self.consumed += tokens
        if self.consumed > self.token_budget:
            raise OasisBudgetExceededError(OasisBudgetExceededError.reason)

    def check_clock(self) -> None:
        if time.monotonic() > self.deadline:
            raise OasisTimeoutError(OasisTimeoutError.reason)
