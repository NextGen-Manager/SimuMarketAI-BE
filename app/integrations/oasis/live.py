"""Binding from `CouncilRuntime` to `camel-oasis` 0.2.5.

Everything about *what* happens in a run — the round order, the activation
policy, the budget, the counting — lives in `orchestrator.py` and is exercised
in CI. This module is only the translation layer: OASIS agents, the Reddit
platform, the trace database, and CAMEL's Gemini backend.

Why each call is the one it is, against the OASIS 0.2.5 source:

- `OasisEnv.step` discards what the agents returned and gives back `None`. The
  orchestrator needs to know which action each persona chose and what it cost,
  so this runtime does what `step` does — refresh the recommendation table, then
  drive the activated agents — and keeps the responses. Nothing is bypassed:
  `update_rec_table` is what produces the feed that makes round 2 social.
- `SocialAgent.perform_interview` returns content but no token usage, and a
  budget that cannot see its own consumption is not a budget. `astep` returns
  usage, so the interview is issued through `astep` and the `INTERVIEW` action
  is recorded to the trace explicitly, exactly as `perform_interview` does.
- `INTERVIEW` stays out of `available_actions`, per docs/04, so no agent can
  select it for itself.

`camel-oasis` is an optional dependency (`uv sync --extra oasis`). The imports
are therefore lazy, and a missing package becomes `OasisUnavailableError` — the
same honest `partial` as a missing API key, not an import-time crash that would
take the API down with it.

This binding has never been executed against a real provider: `GEMINI_API_KEY`
was not available while it was written. It is typed and written against the
installed 0.2.5 source, but "it runs" is not something anybody has observed, and
`tests/test_oasis_live_adapter.py` skips with that reason rather than asserting
behaviour nobody has seen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.domain.agents import (
    AgentRole,
    FinanceTool,
    OasisUnavailableError,
    RunManifest,
    SimulationOutcome,
    SimulationRequest,
)
from app.integrations.oasis.council_runtime import (
    ACTION_DO_NOTHING,
    PERSONA_ACTION_SPACE,
    AgentReply,
    SocialActionResult,
)
from app.integrations.oasis.orchestration_support import build_roster
from app.integrations.oasis.orchestrator import CouncilOrchestrator
from app.integrations.oasis.prompts import PROFILE_VERSION, CouncilMember

logger = logging.getLogger(__name__)

PROFILE_FILE_NAME = "profiles.json"

# The orchestrator's posting account. It has to be an agent because a post needs
# an author, and it must not be a persona: a persona that authored the stimulus
# would then be reacting to itself.
STIMULUS_AUTHOR_INDEX = 0


def _usage_tokens(info: object) -> int:
    if not isinstance(info, dict):
        return 0
    usage = info.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens", 0)
    return int(total) if isinstance(total, int) else 0


def _chosen_action(info: object) -> str:
    """Which social action the agent actually took this round.

    Read from the tool call the model made, not from anything it said about
    itself. An agent that called no tool did nothing, and that is a distinct
    outcome docs/04 asks to be distinguishable in the trace.
    """
    if not isinstance(info, dict):
        return ACTION_DO_NOTHING
    calls = info.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return ACTION_DO_NOTHING
    name = getattr(calls[0], "tool_name", None)
    return str(name) if name else ACTION_DO_NOTHING


def build_profiles(roster: tuple[tuple[AgentRole, CouncilMember], ...]) -> list[dict[str, str]]:
    """Profile rows for `generate_reddit_agent_graph`.

    Every field is derived from the council definition. No demographic attribute
    is invented: docs/03 forbids equating demography with behaviour, so a profile
    carries a mandate and an archetype and nothing else.
    """
    return [
        {
            "username": member.agent_id,
            "realname": member.agent_id,
            "bio": f"Agent sintetis peran {role}.",
            "persona": member.mandate,
            "mbti": "N/A",
            "gender": "N/A",
            "age": "N/A",
            "country": "Indonesia",
        }
        for role, member in roster
    ]


class CamelCouncilRuntime:
    """`CouncilRuntime` implemented on OASIS's Reddit platform."""

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        request: SimulationRequest,
        manifest: RunManifest,
        roster: tuple[tuple[AgentRole, CouncilMember], ...],
    ) -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._request = request
        self._manifest = manifest
        self._roster = roster
        self._env: Any = None
        self._graph: Any = None
        self._stimulus_post_id: int | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        try:
            import oasis
            from camel.models import ModelFactory
            from camel.types import ModelPlatformType
            from oasis import ActionType, generate_reddit_agent_graph
        except ImportError as error:
            raise OasisUnavailableError(
                "Paket camel-oasis belum terpasang pada environment ini."
            ) from error

        trace_path = Path(manifest.trace.object_key)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        if trace_path.exists():
            # A per-run path that already exists means two runs would share one
            # environment. Refuse rather than overwrite another run's trace.
            raise OasisUnavailableError("Trace path untuk run ini sudah dipakai.")

        # Seeding only fixes activation order and sampling on our side. LLM output
        # is not bit-for-bit reproducible, which is why docs/04 asks for a manifest
        # rather than promising determinism.
        random.seed(manifest.seed)

        profile_path = trace_path.parent / PROFILE_FILE_NAME
        profile_path.write_text(
            json.dumps(build_profiles(roster), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        model = ModelFactory.create(
            model_platform=ModelPlatformType.GEMINI,
            model_type=self._model_id,
            model_config_dict={
                "temperature": 0,
                "max_tokens": request.budget.max_output_tokens_per_stage,
            },
            api_key=self._api_key,
            max_retries=request.budget.retry_limit,
            timeout=request.budget.wall_clock_seconds,
        )
        graph = await generate_reddit_agent_graph(
            profile_path=str(profile_path),
            model=model,
            # INTERVIEW is deliberately absent: docs/04 has the orchestrator drive
            # it, so an agent must not be able to select it autonomously.
            available_actions=[
                ActionType.CREATE_COMMENT,
                ActionType.LIKE_POST,
                ActionType.DISLIKE_POST,
                ActionType.PURCHASE_PRODUCT,
                ActionType.DO_NOTHING,
            ],
        )
        environment = oasis.make(
            agent_graph=graph,
            platform=oasis.DefaultPlatformType.REDDIT,
            database_path=str(trace_path),
        )
        return environment, graph

    # --------------------------------------------------------------- councils

    def _run_finance_tool(
        self, request: SimulationRequest, finance_tool: FinanceTool
    ) -> tuple[FinanceToolCall, ...]:
        bounds = request.finance_bounds
        return tuple(
            finance_tool({"volume_units_day": volume})
            for volume in (
                bounds.volume_units_day_min,
                bounds.volume_units_day_base,
                bounds.volume_units_day_max,
            )
        )

    async def _run_council(
        self,
        role: AgentRole,
        request: SimulationRequest,
        member_indices: tuple[int, ...],
        *,
        roster: tuple[tuple[AgentRole, CouncilMember], ...],
        graph: Any,
        tool_calls: tuple[FinanceToolCall, ...],
        semaphore: asyncio.Semaphore,
        remaining_tokens: int,
        deadline: float,
    ) -> tuple[AgentRunRecord, int]:
        from camel.messages import BaseMessage
        from oasis import ActionType

        instances: list[AgentInstanceRecord] = []
        payloads: list[dict[str, Any]] = []
        spent = 0
        schema_failures = 0

        for order, index in enumerate(member_indices):
            if time.monotonic() > deadline:
                raise OasisTimeoutError(OasisTimeoutError.reason)
            if spent >= remaining_tokens:
                raise OasisBudgetExceededError(OasisBudgetExceededError.reason)

            member = roster[index][1]
            prompt = build_prompt(
                member,
                request,
                round_index=min(order, request.budget.round_limit - 1),
                finance_tool_calls=tool_calls if role == "finance" else (),
            )
            agent = graph.get_agent(index)
            begin = time.perf_counter_ns()
            outcome: str = "completed"
            try:
                async with semaphore:
                    response = await asyncio.wait_for(
                        agent.astep(
                            BaseMessage.make_user_message(role_name="Orchestrator", content=prompt)
                        ),
                        timeout=max(1.0, deadline - time.monotonic()),
                    )
            except TimeoutError as error:
                raise OasisTimeoutError(OasisTimeoutError.reason) from error

            content = response.msgs[0].content if response.msgs else "{}"
            # Recorded as an orchestrator-driven INTERVIEW so the whole
            # deliberation is auditable from the trace, not only the final ballot.
            await agent.env.action.perform_action(
                {"prompt_version": PROFILE_VERSION, "role": role},
                ActionType.INTERVIEW.value,
            )
            spent += _usage_tokens(response.info)
            try:
                payloads.append(_extract_json(content))
            except OasisSchemaError:
                schema_failures += 1
                outcome = "failed"

            instances.append(
                AgentInstanceRecord(
                    agent_id=member.agent_id,
                    role=role,
                    archetype=member.archetype,
                    profile_version=PROFILE_VERSION,
                    model_id=self._model_id,
                    allowed_actions=list(member.allowed_actions),
                    activation_order=order,
                    total_tokens=spent,
                    duration_ms=(time.perf_counter_ns() - begin) // 1_000_000,
                    outcome="completed" if outcome == "completed" else "failed",
                )
            )

        merged = self._merge(role, payloads)
        rejected = AgentRunRecord(
            role=role,
            status="failed",
            instances=instances,
            total_tokens=spent,
            schema_failures=schema_failures + 1,
            failure_code="oasis_schema_invalid",
            validation_status="rejected",
        )
        if merged is None:
            return rejected, spent

        try:
            artifact = validate_council_payload(role, merged, request, tool_calls)
        except (ValidationError, ValueError, KeyError):
            # The provider response never leaves this process; only the role and
            # the pseudonymous reference are logged.
            logger.warning(
                "oasis_artifact_rejected",
                extra={"role": role, "analysis_ref": request.analysis_ref},
            )
            return rejected, spent

        return (
            AgentRunRecord(
                role=role,
                status="completed",
                instances=instances,
                total_tokens=spent,
                schema_failures=schema_failures,
                artifact=artifact,
            ),
            spent,
        )

    @staticmethod
    def _merge(role: AgentRole, payloads: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Reduce a council's instance outputs into one artifact payload.

        Personas contribute one ballot each. The other councils deliberate in
        sequence — draft, critique, revision — so the last valid output is the
        council's position and the earlier drafts stay in the trace.
        """
        if not payloads:
            return None
        if role == "customer_persona":
            return {"ballots": payloads}
        return payloads[-1]
