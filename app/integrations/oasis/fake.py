"""Deterministic stand-in for the live OASIS runtime.

CI has no provider key and must still exercise the whole simulation path, so
this is a `CouncilRuntime`, not a shortcut around one. It is driven by the same
`CouncilOrchestrator` as the live adapter, which means CI really runs the docs/04
protocol: private baseline interview, stimulus exposure, interaction, controlled
intervention, final ballot, the seeded activation subset, the token and
wall-clock budgets, the upstream artifact hand-off, and the opinion-shift
arithmetic. What it does not do is call a model.

It is fake, so it carries `is_fake = True` and `select_oasis_adapter` refuses it
in staging and production for exactly the reason a fixture evidence provider is
refused: invented content must never be able to reach a real report.

The failure switches exist to make the honest paths testable. Every one of them
corresponds to a real failure mode: a stage timing out, a council returning
output that does not validate, a single council failing while the others
succeed, or a narrative smuggling in a number nobody computed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.domain.agents import (
    AgentRole,
    FinanceTool,
    OasisError,
    RunManifest,
    SimulationOutcome,
    SimulationRequest,
)
from app.integrations.oasis.council_runtime import (
    ACTION_CREATE_COMMENT,
    ACTION_DISLIKE_POST,
    ACTION_DO_NOTHING,
    ACTION_LIKE_POST,
    ACTION_PURCHASE_PRODUCT,
    AgentReply,
    SocialActionResult,
)
from app.integrations.oasis.fake_payloads import (
    finance_payload,
    market_payload,
    persona_ballot,
    report_payload,
)
from app.integrations.oasis.orchestration_support import build_roster
from app.integrations.oasis.orchestrator import CouncilOrchestrator
from app.integrations.oasis.prompts import CouncilMember

# A fixed, small token cost per reply keeps budget enforcement testable without
# pretending to predict what a model would actually consume.
TOKENS_PER_INSTANCE = 320

# Which action a persona takes when activated, derived from seed and position so
# the same manifest reproduces the same reaction counts.
ROUND_ACTIONS = (
    ACTION_LIKE_POST,
    ACTION_CREATE_COMMENT,
    ACTION_DO_NOTHING,
    ACTION_PURCHASE_PRODUCT,
    ACTION_DISLIKE_POST,
)


class FakeCouncilRuntime:
    """A runtime that answers from fixtures and writes a real trace."""

    def __init__(
        self,
        *,
        request: SimulationRequest,
        manifest: RunManifest,
        roster: tuple[tuple[AgentRole, CouncilMember], ...],
        failing_roles: frozenset[AgentRole],
        invalid_roles: frozenset[AgentRole],
        stage_delay_seconds: float,
        narrative_extra_number: int | None,
        token_cost: int,
    ) -> None:
        self._request = request
        self._manifest = manifest
        self._roster = roster
        self._failing = failing_roles
        self._invalid = invalid_roles
        self._delay = stage_delay_seconds
        self._narrative_extra_number = narrative_extra_number
        self._token_cost = token_cost
        self._trace: _TraceWriter | None = None
        self._restricted: dict[int, tuple[str, ...]] = {}
        self._drafts: dict[AgentRole, int] = {}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._trace = _TraceWriter(Path(self._manifest.trace.object_key))

    async def close(self) -> None:
        if self._trace is not None:
            self._trace.close()

    async def restrict_actions(self, agent_index: int, actions: Sequence[str]) -> None:
        self._restricted[agent_index] = tuple(actions)

    # --------------------------------------------------------------- rounds

    async def interview(
        self,
        agent_index: int,
        prompt: str,
        *,
        round_index: int,
        purpose: str,
    ) -> AgentReply:
        await self._sleep()
        role, member = self._roster[agent_index]
        self._write(
            agent_id=member.agent_id,
            role=role,
            round_index=round_index,
            action=f"interview:{purpose}",
            payload={"prompt_length": len(prompt)},
        )
        if role in self._failing:
            # No JSON at all: the orchestrator's schema path handles it, rather
            # than a shortcut that skips the path being tested.
            return AgentReply(content="", tokens=self._token_cost)
        if role in self._invalid:
            return AgentReply(
                content=json.dumps({"unexpected": "payload"}), tokens=self._token_cost
            )
        payload = self._payload(role, agent_index, purpose, prompt)
        return AgentReply(content=json.dumps(payload, ensure_ascii=False), tokens=self._token_cost)

    async def publish_stimulus(
        self,
        payload: Mapping[str, object],
        *,
        round_index: int,
        label: str,
    ) -> None:
        await self._sleep()
        self._write(
            agent_id="orchestrator",
            role="customer_persona",
            round_index=round_index,
            action=f"publish:{label}",
            payload=dict(payload),
        )

    async def step(
        self,
        agent_indices: Sequence[int],
        *,
        round_index: int,
    ) -> Mapping[int, SocialActionResult]:
        await self._sleep()
        taken: dict[int, SocialActionResult] = {}
        for position, index in enumerate(agent_indices):
            action = ROUND_ACTIONS[(self._request.seed + index + round_index) % len(ROUND_ACTIONS)]
            allowed = self._restricted.get(index, ())
            if allowed and action not in allowed:
                action = ACTION_DO_NOTHING
            taken[index] = SocialActionResult(
                action=action,
                tokens=self._token_cost,
                duration_ms=max(1, int(self._delay * 1_000)),
            )
            self._write(
                agent_id=self._roster[index][1].agent_id,
                role="customer_persona",
                round_index=round_index,
                action=action,
                payload={"activation_order": position},
            )
        return taken

    # -------------------------------------------------------------- payloads

    def _payload(
        self, role: AgentRole, agent_index: int, purpose: str, prompt: str
    ) -> dict[str, Any]:
        if role == "customer_persona":
            return persona_ballot(
                self._request,
                self._roster[agent_index][1],
                agent_index,
                baseline=purpose == "baseline_interview",
            )
        # Council members deliberate in sequence; each one emits the whole
        # artifact so the last valid output is the council's position.
        order = self._drafts.get(role, 0)
        self._drafts[role] = order + 1
        if role == "market_analyst":
            return market_payload(self._request)
        if role == "finance":
            return finance_payload(self._request, prompt)
        return report_payload(prompt, extra_number=self._narrative_extra_number)

    async def _sleep(self) -> None:
        if self._delay > 0:
            await asyncio.sleep(self._delay)

    def _write(
        self,
        *,
        agent_id: str,
        role: str,
        round_index: int,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        if self._trace is not None:
            self._trace.write(
                agent_id=agent_id,
                role=role,
                round_index=round_index,
                action=action,
                payload=payload,
            )


class FakeOasisAdapter:
    adapter_id = "oasis-fake"
    is_fake = True

    def __init__(
        self,
        *,
        error: OasisError | None = None,
        failing_roles: Sequence[AgentRole] = (),
        invalid_roles: Sequence[AgentRole] = (),
        stage_delay_seconds: float = 0.0,
        narrative_extra_number: int | None = None,
        token_cost_per_instance: int = TOKENS_PER_INSTANCE,
    ) -> None:
        self._error = error
        self._failing = frozenset(failing_roles)
        self._invalid = frozenset(invalid_roles)
        self._stage_delay = stage_delay_seconds
        self._narrative_extra_number = narrative_extra_number
        self._token_cost = token_cost_per_instance

    async def simulate(
        self,
        request: SimulationRequest,
        *,
        finance_tool: FinanceTool,
        manifest: RunManifest,
    ) -> SimulationOutcome:
        if self._error is not None:
            raise self._error

        roster = build_roster(request)
        runtime = FakeCouncilRuntime(
            request=request,
            manifest=manifest,
            roster=roster,
            failing_roles=self._failing,
            invalid_roles=self._invalid,
            stage_delay_seconds=self._stage_delay,
            narrative_extra_number=self._narrative_extra_number,
            token_cost=self._token_cost,
        )
        orchestrator = CouncilOrchestrator(
            runtime,
            model_id=self.adapter_id,
            request=request,
            manifest=manifest,
        )
        return await orchestrator.run(finance_tool)


# ----------------------------------------------------------------------- trace


class _TraceWriter:
    """Writes a per-run SQLite trace, mirroring what the live runtime leaves behind."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS interactions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, "
            "role TEXT NOT NULL, round_index INTEGER NOT NULL, action TEXT NOT NULL, "
            "payload TEXT NOT NULL)"
        )

    def write(
        self,
        *,
        agent_id: str,
        role: str,
        round_index: int,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        self._connection.execute(
            "INSERT INTO interactions (agent_id, role, round_index, action, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, role, round_index, action, json.dumps(payload, ensure_ascii=False)),
        )

    def close(self) -> None:
        self._connection.commit()
        self._connection.close()
