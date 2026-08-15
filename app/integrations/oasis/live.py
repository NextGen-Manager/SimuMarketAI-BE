"""Live OASIS adapter built on `camel-oasis` and CAMEL's Gemini model adapter.

`camel-oasis` is not a dependency of the main backend environment: it pins
`pytest-asyncio==0.23.6` and constrains `mcp<2`, which is why the Phase 0 spike
lives in `spikes/oasis` with its own lockfile. The import therefore happens
inside `simulate`, and a missing package becomes `OasisUnavailableError` — the
same honest failure as a missing API key, not an import-time crash that would
take the API down with it.

The design is carried over from the spike: one environment and one trace file
per run, an action allowlist, `INTERVIEW` driven by the orchestrator rather than
chosen by an agent, hard token and wall-clock budgets, and structured output
validated by the same module the fake adapter uses.

This path has never been executed against a real provider. `GEMINI_API_KEY` was
not available while it was written, so it is wired and typed but unverified;
`tests/test_oasis_live_adapter.py` skips with that reason rather than asserting
behaviour nobody has observed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.domain.agents import (
    AGENT_ROLES,
    AgentInstanceRecord,
    AgentRole,
    AgentRunRecord,
    FinanceTool,
    FinanceToolCall,
    OasisBudgetExceededError,
    OasisSchemaError,
    OasisTimeoutError,
    OasisUnavailableError,
    RunManifest,
    SimulationOutcome,
    SimulationRequest,
)
from app.integrations.oasis.prompts import (
    PROFILE_VERSION,
    CouncilMember,
    build_prompt,
    council_for,
)
from app.integrations.oasis.validation import validate_council_payload

logger = logging.getLogger(__name__)

PROFILE_FILE_NAME = "profiles.json"


def _extract_json(content: str) -> dict[str, Any]:
    """Pull the single JSON object out of a model response.

    Models wrap JSON in prose or code fences often enough that parsing the whole
    response would fail for reasons unrelated to the schema. Locating the
    outermost braces keeps "schema failure" meaning what it says.
    """
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


def _usage_tokens(info: object) -> int:
    if not isinstance(info, dict):
        return 0
    usage = info.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens", 0)
    return int(total) if isinstance(total, int) else 0


def build_roster(request: SimulationRequest) -> tuple[tuple[AgentRole, CouncilMember], ...]:
    """Flatten the four councils into one ordered roster.

    OASIS addresses agents by index inside a single `AgentGraph`, so the roster
    order is what maps a personality instance to its agent. Keeping it in one
    place means the profile file, the graph, and the persisted instance records
    describe the same run.
    """
    roster: list[tuple[AgentRole, CouncilMember]] = []
    for role in AGENT_ROLES:
        for member in council_for(role, request):
            roster.append((role, member))
    return tuple(roster)


def build_profiles(roster: tuple[tuple[AgentRole, CouncilMember], ...]) -> list[dict[str, str]]:
    """Profile rows for `generate_reddit_agent_graph`.

    Every field is derived from the council definition. No demographic attribute
    is invented: docs/03 forbids equating demography with behaviour, so the
    profile carries a mandate and an archetype and nothing else.
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


class LiveOasisAdapter:
    adapter_id = "oasis-live"
    is_fake = False

    def __init__(self, *, api_key: str, model_id: str, provider: str = "gemini") -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._provider = provider

    async def simulate(
        self,
        request: SimulationRequest,
        *,
        finance_tool: FinanceTool,
        manifest: RunManifest,
    ) -> SimulationOutcome:
        roster = build_roster(request)
        indices = self._role_indices(roster)
        environment, graph = await self._make_environment(request, manifest, roster)

        started = time.monotonic()
        deadline = started + request.budget.wall_clock_seconds
        consumed = 0
        runs: list[AgentRunRecord] = []
        warnings: list[str] = []
        tool_calls: tuple[FinanceToolCall, ...] = ()
        semaphore = asyncio.Semaphore(request.budget.concurrency_limit)

        try:
            await environment.reset()
            for role in AGENT_ROLES:
                if time.monotonic() > deadline:
                    raise OasisTimeoutError(OasisTimeoutError.reason)
                if role == "finance":
                    tool_calls = self._run_finance_tool(request, finance_tool)

                run, spent = await self._run_council(
                    role,
                    request,
                    tuple(indices[role]),
                    roster=roster,
                    graph=graph,
                    tool_calls=tool_calls,
                    semaphore=semaphore,
                    remaining_tokens=request.budget.token_budget - consumed,
                    deadline=deadline,
                )
                consumed += spent
                runs.append(run)
                if run.status != "completed":
                    warnings.append(f"Council {role} tidak menghasilkan artifact yang valid.")
                if consumed > request.budget.token_budget:
                    raise OasisBudgetExceededError(OasisBudgetExceededError.reason)
        finally:
            await environment.close()

        succeeded = [run for run in runs if run.status == "completed"]
        if not succeeded:
            return SimulationOutcome(
                status="failed",
                manifest=manifest,
                agent_runs=runs,
                warnings=warnings,
                failure_code="oasis_all_councils_failed",
            )
        status = "completed" if len(succeeded) == len(AGENT_ROLES) else "partial"
        return SimulationOutcome(
            status=status,
            manifest=manifest,
            agent_runs=runs,
            warnings=warnings,
            failure_code=None if status == "completed" else "oasis_council_partial",
        )

    # ------------------------------------------------------------ environment

    @staticmethod
    def _role_indices(
        roster: tuple[tuple[AgentRole, CouncilMember], ...],
    ) -> dict[AgentRole, list[int]]:
        indices: dict[AgentRole, list[int]] = {role: [] for role in AGENT_ROLES}
        for index, (role, _) in enumerate(roster):
            indices[role].append(index)
        return indices

    async def _make_environment(
        self,
        request: SimulationRequest,
        manifest: RunManifest,
        roster: tuple[tuple[AgentRole, CouncilMember], ...],
    ) -> tuple[Any, Any]:
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
