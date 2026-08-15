"""Build persisted council records from validated runtime output."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.domain.agents import (
    AgentInstanceRecord,
    AgentRole,
    AgentRunRecord,
    ArtifactType,
    FinanceToolCall,
    RoundRecord,
    SimulationRequest,
)
from app.integrations.oasis.prompts import PROFILE_VERSION, CouncilMember
from app.integrations.oasis.validation import validate_council_payload

logger = logging.getLogger(__name__)


def build_instance(
    member: CouncilMember,
    role: AgentRole,
    order: int,
    tokens: int,
    duration_ms: int,
    outcome: str,
    *,
    model_id: str,
) -> AgentInstanceRecord:
    return AgentInstanceRecord(
        agent_id=member.agent_id,
        role=role,
        archetype=member.archetype,
        profile_version=PROFILE_VERSION,
        model_id=model_id,
        allowed_actions=list(member.allowed_actions),
        activation_order=order,
        total_tokens=tokens,
        duration_ms=duration_ms,
        outcome="completed" if outcome == "completed" else "failed",
    )


def finish_run(
    role: AgentRole,
    *,
    request: SimulationRequest,
    payload: dict[str, Any] | None,
    instances: list[AgentInstanceRecord],
    tokens: int,
    duration_ms: int,
    schema_failures: int,
    tool_calls: tuple[FinanceToolCall, ...],
    consumed: list[ArtifactType],
    rounds: list[RoundRecord] | None = None,
) -> AgentRunRecord:
    rejected = AgentRunRecord(
        role=role,
        status="failed",
        instances=instances,
        total_tokens=tokens,
        duration_ms=duration_ms,
        schema_failures=schema_failures + 1,
        failure_code="oasis_schema_invalid",
        validation_status="rejected",
        consumed_artifact_types=consumed,
        rounds=rounds or [],
    )
    if payload is None:
        return rejected
    try:
        artifact = validate_council_payload(role, payload, request, tool_calls)
    except (ValidationError, ValueError, KeyError):
        logger.warning(
            "oasis_artifact_rejected",
            extra={"role": role, "analysis_ref": request.analysis_ref},
        )
        return rejected
    return AgentRunRecord(
        role=role,
        status="completed",
        instances=instances,
        total_tokens=tokens,
        duration_ms=duration_ms,
        schema_failures=schema_failures,
        artifact=artifact,
        consumed_artifact_types=consumed,
        rounds=rounds or [],
    )
