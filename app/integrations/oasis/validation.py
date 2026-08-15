"""Schema and provenance validation for council output.

Both adapters funnel raw payloads through here, so a fake run and a live run
are rejected for exactly the same reasons. Two checks matter beyond the Pydantic
schema itself:

- a market observation may only cite metrics the evidence snapshot actually
  contains, otherwise the agent has invented evidence;
- a finance critique may only cite tool calls the deterministic calculator
  really produced, otherwise a critique has no traceable basis.
"""

from __future__ import annotations

from typing import Any

from app.domain.agents import (
    AgentArtifactPayload,
    AgentRole,
    CustomerSimulationResult,
    FinanceReview,
    FinanceToolCall,
    MarketAssessment,
    ReportNarrative,
    SimulationRequest,
    unsupported_evidence_metrics,
)
from app.integrations.oasis.reducers import PersonaBallot, reduce_persona_ballots


class ArtifactRejectedError(ValueError):
    """Raised when a payload is well-formed JSON but not an acceptable artifact."""


def validate_council_payload(
    role: AgentRole,
    raw: dict[str, Any],
    request: SimulationRequest,
    tool_calls: tuple[FinanceToolCall, ...],
) -> AgentArtifactPayload:
    if role == "market_analyst":
        assessment = MarketAssessment.model_validate(raw)
        invented = unsupported_evidence_metrics(
            assessment, [item.metric for item in request.evidence]
        )
        if invented:
            raise ArtifactRejectedError(
                "agent menunjuk evidence yang tidak ada: " + ", ".join(invented)
            )
        return assessment

    if role == "customer_persona":
        entries = raw.get("ballots")
        if not isinstance(entries, list) or not entries:
            raise ArtifactRejectedError("ballot persona kosong")
        ballots = [PersonaBallot.model_validate(entry) for entry in entries]
        return reduce_persona_ballots(
            ballots,
            cohort_version=request.cohort.cohort_version,
            cohort_size=request.cohort.size,
            rounds=request.budget.round_limit,
        )

    if role == "finance":
        if not tool_calls:
            raise ArtifactRejectedError("finance review tanpa tool call deterministik")
        payload = dict(raw)
        payload["tool_calls"] = [call.model_dump(mode="json") for call in tool_calls]
        payload.setdefault("finance_rule_version", request.finance_rule_version)
        return FinanceReview.model_validate(payload)

    return ReportNarrative.model_validate(raw)


__all__ = [
    "ArtifactRejectedError",
    "CustomerSimulationResult",
    "validate_council_payload",
]
