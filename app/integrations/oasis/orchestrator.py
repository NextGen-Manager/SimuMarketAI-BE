"""The docs/04 execution protocol, independent of any provider.

This is the four-agent protocol as written, not a sequence of chat completions
dressed up as one:

    1. Market Analyst Council: Scout proposes -> Skeptic challenges ->
       Evidence Auditor verifies. Each member receives the previous member's
       draft, so "challenges" has something to challenge.
    2. Customer Persona Council: private baseline interview -> stimulus exposure
       -> interaction -> controlled intervention -> final ballot. Rounds are real
       environment steps and the persona feed is real social influence.
    3. Finance Council: the deterministic calculator runs first, and the council
       may only critique its output.
    4. Report Council: receives the validated artifacts of all three, and what it
       received is recorded so provenance describes the run rather than the plan.

Three numbers deliberately are not taken from the model. Positive reactions come
from actions observed in the trace, opinion shift comes from comparing a
persona's baseline ballot with its final one, and every finance figure comes
from the calculator. ADR-001 does not allow an LLM to report a count that ends
up in a report, even a count about itself.

The controlled intervention varies the *message*, not the price. docs/04 allows
"harga, promo, atau message"; a changed price would put a rupiah figure into
persona replies that no engine ever produced, and the report validator would
correctly reject the run for it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.agents import (
    AGENT_ROLES,
    AgentArtifactPayload,
    AgentInstanceRecord,
    AgentRole,
    AgentRunRecord,
    ArtifactType,
    FinanceTool,
    FinanceToolCall,
    OasisSchemaError,
    RoundRecord,
    RunManifest,
    SimulationOutcome,
    SimulationRequest,
)
from app.integrations.oasis.council_runtime import (
    POSITIVE_ACTIONS,
    AgentReply,
    CouncilRuntime,
)
from app.integrations.oasis.orchestration_records import build_instance, finish_run
from app.integrations.oasis.orchestration_support import (
    RunBudget,
    build_roster,
    extract_json,
    rounds_for,
)
from app.integrations.oasis.prompts import (
    DELIBERATION_TASK,
    PERSONA_BASELINE_TASK,
    PERSONA_FINAL_TASK,
    REPORT_TASK,
    build_prompt,
    deliberation_turn,
    simulation_round,
)

logger = logging.getLogger(__name__)

# Which validated artifacts each council is handed. Anything not listed here is
# not shown to that council, and `consumed_artifact_types` says so.
UPSTREAM_FOR: dict[AgentRole, tuple[ArtifactType, ...]] = {
    "market_analyst": (),
    "customer_persona": (),
    "finance": (),
    "report": ("MarketAssessment", "CustomerSimulationResult", "FinanceReview"),
}


class CouncilOrchestrator:
    """Runs the four councils against a `CouncilRuntime`."""

    def __init__(
        self,
        runtime: CouncilRuntime,
        *,
        model_id: str,
        request: SimulationRequest,
        manifest: RunManifest,
    ) -> None:
        self._runtime = runtime
        self._model_id = model_id
        self._request = request
        self._manifest = manifest
        self._roster = build_roster(request)
        self._semaphore = asyncio.Semaphore(request.budget.concurrency_limit)

    # ------------------------------------------------------------------ entry

    async def run(self, finance_tool: FinanceTool) -> SimulationOutcome:
        request = self._request
        budget = RunBudget(
            token_budget=request.budget.token_budget,
            deadline=time.monotonic() + request.budget.wall_clock_seconds,
        )
        artifacts: dict[ArtifactType, AgentArtifactPayload] = {}
        runs: list[AgentRunRecord] = []
        warnings: list[str] = []
        tool_calls: tuple[FinanceToolCall, ...] = ()

        await self._runtime.start()
        try:
            await self._apply_action_allowlists()
            for role in AGENT_ROLES:
                budget.check_clock()
                if role == "finance":
                    tool_calls = self._run_finance_tool(finance_tool)

                if role == "customer_persona":
                    record = await self._persona_protocol(budget)
                else:
                    record = await self._deliberate(role, budget, tool_calls, artifacts)

                runs.append(record)
                if record.artifact is not None and record.status == "completed":
                    artifacts[record.artifact.artifact_type] = record.artifact
                else:
                    warnings.append(f"Council {role} tidak menghasilkan artifact yang valid.")
        finally:
            await self._runtime.close()

        return self._outcome(runs, warnings)

    def _outcome(self, runs: list[AgentRunRecord], warnings: list[str]) -> SimulationOutcome:
        succeeded = [record for record in runs if record.status == "completed"]
        if not succeeded:
            return SimulationOutcome(
                status="failed",
                manifest=self._manifest,
                agent_runs=runs,
                warnings=warnings,
                failure_code="oasis_all_councils_failed",
            )
        status = "completed" if len(succeeded) == len(AGENT_ROLES) else "partial"
        return SimulationOutcome(
            status=status,
            manifest=self._manifest,
            agent_runs=runs,
            warnings=warnings,
            failure_code=None if status == "completed" else "oasis_council_partial",
        )

    # ------------------------------------------------------------------ setup

    async def _apply_action_allowlists(self) -> None:
        for index, (_, member) in enumerate(self._roster):
            await self._runtime.restrict_actions(index, member.allowed_actions)

    def _run_finance_tool(self, finance_tool: FinanceTool) -> tuple[FinanceToolCall, ...]:
        bounds = self._request.finance_bounds
        return tuple(
            finance_tool({"volume_units_day": volume})
            for volume in (
                bounds.volume_units_day_min,
                bounds.volume_units_day_base,
                bounds.volume_units_day_max,
            )
        )

    def _indices_for(self, role: AgentRole) -> tuple[int, ...]:
        return tuple(
            index for index, (member_role, _) in enumerate(self._roster) if member_role == role
        )

    # ----------------------------------------------------- deliberative council

    async def _deliberate(
        self,
        role: AgentRole,
        budget: RunBudget,
        tool_calls: tuple[FinanceToolCall, ...],
        artifacts: Mapping[ArtifactType, AgentArtifactPayload],
    ) -> AgentRunRecord:
        """Draft, challenge, revise — in that order, each seeing the last.

        Members run sequentially on purpose: the whole point of the council is
        that the Skeptic reads what the Scout proposed. Running them in parallel
        would be cheaper and would produce three independent monologues.
        """
        indices = self._indices_for(role)
        upstream = self._upstream_for(role, artifacts)
        consumed = [
            artifact_type for artifact_type in UPSTREAM_FOR[role] if artifact_type in artifacts
        ]

        instances: list[AgentInstanceRecord] = []
        draft: dict[str, Any] | None = None
        schema_failures = 0
        tokens = 0
        began = time.perf_counter_ns()

        for order, index in enumerate(indices):
            budget.check_clock()
            member = self._roster[index][1]
            context = dict(upstream)
            if draft is not None:
                context["draft"] = draft
            prompt = build_prompt(
                member,
                self._request,
                position=deliberation_turn(order),
                finance_tool_calls=tool_calls if role == "finance" else (),
                upstream=context or None,
                task=REPORT_TASK if role == "report" else (DELIBERATION_TASK if draft else None),
            )
            # A deliberative council takes no part in the persona rounds, so it
            # claims none: the trace records it at round 0 and distinguishes it
            # by action label instead.
            reply, elapsed_ms = await self._ask(
                index, prompt, round_index=0, purpose=f"deliberation:{role}"
            )
            budget.spend(reply.tokens)
            tokens += reply.tokens

            outcome = "completed"
            try:
                draft = extract_json(reply.content)
            except OasisSchemaError:
                schema_failures += 1
                outcome = "failed"
            instances.append(
                build_instance(
                    member,
                    role,
                    order,
                    reply.tokens,
                    elapsed_ms,
                    outcome,
                    model_id=self._model_id,
                )
            )

        duration_ms = (time.perf_counter_ns() - began) // 1_000_000
        return finish_run(
            role,
            request=self._request,
            payload=draft,
            instances=instances,
            tokens=tokens,
            duration_ms=duration_ms,
            schema_failures=schema_failures,
            tool_calls=tool_calls,
            consumed=consumed,
        )

    def _upstream_for(
        self,
        role: AgentRole,
        artifacts: Mapping[ArtifactType, AgentArtifactPayload],
    ) -> dict[str, object]:
        return {
            artifact_type: artifacts[artifact_type].model_dump(mode="json")
            for artifact_type in UPSTREAM_FOR[role]
            if artifact_type in artifacts
        }

    # -------------------------------------------------------- persona protocol

    async def _persona_protocol(self, budget: RunBudget) -> AgentRunRecord:
        request = self._request
        indices = self._indices_for("customer_persona")
        plan = rounds_for(request.budget.round_limit)
        order = self._activation_order(indices)
        subset = self._activation_subset(order)

        rounds: list[RoundRecord] = []
        reactions: dict[str, int] = {}
        actions_by_agent: dict[str, list[str]] = {}
        baseline: list[dict[str, Any]] = []
        instances: list[AgentInstanceRecord] = []
        # Per-agent, never an average. An averaged per-instance cost hides the
        # one persona whose reply was three times the size of the others.
        tokens_by_index: dict[int, int] = dict.fromkeys(order, 0)
        duration_by_index: dict[int, int] = dict.fromkeys(order, 0)
        schema_failures = 0
        began = time.perf_counter_ns()

        for round_index, kind in plan:
            budget.check_clock()
            if kind == "baseline_interview":
                baseline, spent, failures = await self._ballot_round(
                    order, round_index=round_index, kind=kind, observed=None
                )
                schema_failures += failures
                for index, (agent_tokens, agent_ms) in spent.items():
                    tokens_by_index[index] += agent_tokens
                    duration_by_index[index] += agent_ms
                budget.spend(sum(value for value, _ in spent.values()))
                rounds.append(
                    RoundRecord(
                        index=round_index,
                        kind=kind,
                        activated_agent_ids=[self._agent_id(index) for index in order],
                    )
                )
                continue

            activated = order if kind == "exposure" else subset
            if kind in {"exposure", "intervention"}:
                await self._runtime.publish_stimulus(
                    self._stimulus(kind),
                    round_index=round_index,
                    label=kind,
                )
            taken = await self._runtime.step(activated, round_index=round_index)
            budget.spend(sum(result.tokens for result in taken.values()))
            for index, result in taken.items():
                tokens_by_index[index] += result.tokens
                duration_by_index[index] += result.duration_ms
                action = result.action
                agent_id = self._agent_id(index)
                actions_by_agent.setdefault(agent_id, []).append(action)
                if action in POSITIVE_ACTIONS:
                    reactions[agent_id] = reactions.get(agent_id, 0) + 1
            rounds.append(
                RoundRecord(
                    index=round_index,
                    kind=kind,
                    activated_agent_ids=[self._agent_id(index) for index in activated],
                    actions={
                        self._agent_id(index): result.action for index, result in taken.items()
                    },
                )
            )

        budget.check_clock()
        # The final interview closes the last round rather than opening a new
        # one. docs/04 counts four rounds and a final ballot, not five, so a run
        # must never report more rounds than its budget allowed.
        final_round = plan[-1][0]
        final, spent, failures = await self._ballot_round(
            order, round_index=final_round, kind="final_ballot", observed=actions_by_agent
        )
        schema_failures += failures
        for index, (agent_tokens, agent_ms) in spent.items():
            tokens_by_index[index] += agent_tokens
            duration_by_index[index] += agent_ms
        budget.spend(sum(value for value, _ in spent.values()))
        rounds.append(
            RoundRecord(
                index=final_round,
                kind="final_ballot",
                activated_agent_ids=[self._agent_id(index) for index in order],
            )
        )

        voted = {entry.get("agent_id") for entry in final}
        for position, index in enumerate(order):
            member = self._roster[index][1]
            instances.append(
                build_instance(
                    member,
                    "customer_persona",
                    position,
                    tokens_by_index[index],
                    duration_by_index[index],
                    "completed" if member.agent_id in voted else "failed",
                    model_id=self._model_id,
                )
            )

        payload: dict[str, Any] = {
            "baseline_ballots": baseline,
            "final_ballots": final,
            "observed_reactions": reactions,
            "rounds": len(plan),
        }
        duration_ms = (time.perf_counter_ns() - began) // 1_000_000
        return finish_run(
            "customer_persona",
            request=self._request,
            payload=payload if final else None,
            instances=instances,
            tokens=sum(tokens_by_index.values()),
            duration_ms=duration_ms,
            schema_failures=schema_failures,
            tool_calls=(),
            consumed=[],
            rounds=rounds,
        )

    async def _ballot_round(
        self,
        indices: Sequence[int],
        *,
        round_index: int,
        kind: str,
        observed: Mapping[str, list[str]] | None,
    ) -> tuple[list[dict[str, Any]], dict[int, tuple[int, int]], int]:
        """Interview every persona privately.

        Personas are independent of one another in an interview round, which is
        the only place the concurrency budget can honestly be spent: the
        deliberative councils are sequential by design.
        """
        task = PERSONA_BASELINE_TASK if kind == "baseline_interview" else PERSONA_FINAL_TASK

        async def ask(index: int) -> tuple[AgentReply, int]:
            member = self._roster[index][1]
            prompt = build_prompt(
                member,
                self._request,
                position=simulation_round(round_index),
                task=task,
                observed=(
                    {"actions": list(observed.get(member.agent_id, []))} if observed else None
                ),
            )
            return await self._ask(index, prompt, round_index=round_index, purpose=kind)

        replies = await asyncio.gather(*(ask(index) for index in indices))

        ballots: list[dict[str, Any]] = []
        cost: dict[int, tuple[int, int]] = {}
        failures = 0
        for (reply, elapsed_ms), index in zip(replies, indices, strict=True):
            cost[index] = (reply.tokens, elapsed_ms)
            try:
                entry = extract_json(reply.content)
            except OasisSchemaError:
                failures += 1
                continue
            # The agent identity is ours, not the model's: a persona that names
            # itself something else would corrupt the baseline-to-final pairing
            # that opinion shift is computed from.
            entry["agent_id"] = self._roster[index][1].agent_id
            entry.setdefault("archetype", self._roster[index][1].archetype or "budget_driven")
            ballots.append(entry)
        return ballots, cost, failures

    def _activation_order(self, indices: Sequence[int]) -> tuple[int, ...]:
        # Seeded so the manifest alone reproduces the activation order, which is
        # what docs/04 asks to be stored for a counterfactual comparison.
        shuffled = list(indices)
        random.Random(self._request.seed).shuffle(shuffled)
        return tuple(shuffled)

    def _activation_subset(self, order: Sequence[int]) -> tuple[int, ...]:
        # Not everyone sees every round. A cohort where all personas act in all
        # rounds has no activation signal at all, and docs/04 asks for the subset
        # and its order to be recorded.
        size = max(1, (len(order) * 3) // 4)
        return tuple(order[:size])

    def _stimulus(self, kind: str) -> dict[str, object]:
        concept = self._request.concept
        card: dict[str, object] = {
            "business_type": concept.business_type,
            "offer": concept.concept_name,
            "price_idr": concept.price_idr,
            "service_mode": list(concept.channels),
            "claims": [concept.value_proposition] if concept.value_proposition else [],
        }
        if kind == "intervention":
            # One variable, and a non-numeric one. Changing the price here would
            # introduce a rupiah figure no engine produced into persona replies.
            card["message_variant"] = "kecepatan_layanan"
            card["claims"] = ["Pesanan siap cepat pada jam sibuk"]
        return card

    # ---------------------------------------------------------------- helpers

    async def _ask(
        self, index: int, prompt: str, *, round_index: int, purpose: str
    ) -> tuple[AgentReply, int]:
        began = time.perf_counter_ns()
        async with self._semaphore:
            reply = await self._runtime.interview(
                index, prompt, round_index=round_index, purpose=purpose
            )
        return reply, (time.perf_counter_ns() - began) // 1_000_000

    def _agent_id(self, index: int) -> str:
        return self._roster[index][1].agent_id
