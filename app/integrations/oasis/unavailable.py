"""Adapter used when no live OASIS configuration exists.

It fails loudly and immediately rather than degrading into a plausible-looking
result. The pipeline catches `OasisUnavailableError` and finishes the run as
`partial` with the reason stated in the report, which is the honest outcome
docs/04 requires when the simulation cannot run.
"""

from __future__ import annotations

from app.domain.agents import (
    FinanceTool,
    OasisUnavailableError,
    RunManifest,
    SimulationOutcome,
    SimulationRequest,
)


class UnavailableOasisAdapter:
    adapter_id = "oasis-unavailable"
    is_fake = False

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason or OasisUnavailableError.reason

    async def simulate(
        self,
        request: SimulationRequest,
        *,
        finance_tool: FinanceTool,
        manifest: RunManifest,
    ) -> SimulationOutcome:
        raise OasisUnavailableError(self.reason)
