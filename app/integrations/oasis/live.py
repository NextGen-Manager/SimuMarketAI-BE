"""Binding from `CouncilRuntime` to `camel-oasis` 0.2.5.

Everything about *what* happens in a run — the round order, the activation
policy, the budget, the counting — lives in `orchestrator.py` and is exercised
in CI. This module is only the translation layer: OASIS agents, the Reddit
platform, the trace database, and CAMEL's selected model backend.

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

Live provider verification remains separate from deterministic CI and requires
an explicitly selected provider, matching model, and corresponding API key.
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
from app.integrations.oasis.providers import OasisProvider, resolve_model_platform

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
            "persona": (
                member.mandate
                if member.archetype is None
                else f"Arketipe hipotesis {member.archetype}. {member.mandate}"
            ),
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
        provider: OasisProvider,
        request: SimulationRequest,
        manifest: RunManifest,
        roster: tuple[tuple[AgentRole, CouncilMember], ...],
    ) -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._provider = provider
        self._request = request
        self._manifest = manifest
        self._roster = roster
        self._env: Any = None
        self._graph: Any = None
        self._stimulus_post_id: int | None = None
        self._stimulus_marker: str | None = None
        self._llm_semaphore = asyncio.Semaphore(request.budget.concurrency_limit)

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

        trace_path = Path(self._manifest.trace.object_key)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        if trace_path.exists():
            # A per-run path that already exists means two runs would share one
            # environment. Refuse rather than overwrite another run's trace.
            raise OasisUnavailableError("Trace path untuk run ini sudah dipakai.")

        # Seeding fixes activation order and sampling on our side only. LLM
        # output is not bit-for-bit reproducible, which is why docs/04 asks for a
        # manifest rather than promising determinism.
        random.seed(self._manifest.seed)

        profile_path = trace_path.parent / PROFILE_FILE_NAME
        profile_path.write_text(
            json.dumps(build_profiles(self._roster), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        model = ModelFactory.create(
            model_platform=resolve_model_platform(self._provider, ModelPlatformType),
            model_type=self._model_id,
            model_config_dict={
                "temperature": 0,
                "max_tokens": self._request.budget.max_output_tokens_per_stage,
            },
            api_key=self._api_key,
            max_retries=self._request.budget.retry_limit,
            timeout=self._request.budget.wall_clock_seconds,
        )
        self._graph = await generate_reddit_agent_graph(
            profile_path=str(profile_path),
            model=model,
            available_actions=[ActionType(action) for action in PERSONA_ACTION_SPACE],
        )
        self._env = oasis.make(
            agent_graph=self._graph,
            platform=oasis.DefaultPlatformType.REDDIT,
            database_path=str(trace_path),
            semaphore=self._request.budget.concurrency_limit,
        )
        await self._env.reset()

    async def close(self) -> None:
        if self._env is not None:
            await self._env.close()

    # --------------------------------------------------------------- actions

    async def restrict_actions(self, agent_index: int, actions: Sequence[str]) -> None:
        """Drop every social tool this agent's council is not allowed to use.

        The graph is built with one action space for everyone. docs/04 gives each
        council its own allowlist, and the deliberative councils have no OASIS
        action at all — their tools are orchestrator-driven — so they are left
        with `do_nothing` and cannot post into the persona feed.
        """
        agent = self._agent(agent_index)
        allowed = {action for action in actions if action in PERSONA_ACTION_SPACE}
        allowed.add(ACTION_DO_NOTHING)
        for name in list(getattr(agent, "_internal_tools", {})):
            if name in PERSONA_ACTION_SPACE and name not in allowed:
                agent.remove_tool(name)

    async def interview(
        self,
        agent_index: int,
        prompt: str,
        *,
        round_index: int,
        purpose: str,
    ) -> AgentReply:
        from camel.messages import BaseMessage
        from oasis import ActionType

        agent = self._agent(agent_index)
        # Interviews must return the requested JSON, not take a social action.
        # Persona tools are restored immediately afterwards for exposure and
        # interaction rounds. Each persona is interviewed at most once at a
        # time, so this temporary narrowing cannot race another call on it.
        tools = list(getattr(agent, "_internal_tools", {}).values())
        for tool in tools:
            agent.remove_tool(tool.get_function_name())
        try:
            response = await agent.astep(
                BaseMessage.make_user_message(role_name="Orchestrator", content=prompt)
            )
        finally:
            for tool in tools:
                agent.add_tool(tool)
        content = response.msgs[0].content if response.msgs else ""
        # Recorded so the whole deliberation is auditable from the trace, not
        # only the final ballot.
        await agent.env.action.perform_action(
            {
                "prompt": f"[{PROFILE_VERSION}] round {round_index} {purpose}",
                "response": content,
            },
            ActionType.INTERVIEW.value,
        )
        return AgentReply(content=content, tokens=_usage_tokens(response.info))

    async def publish_stimulus(
        self,
        payload: Mapping[str, object],
        *,
        round_index: int,
        label: str,
    ) -> None:
        from oasis import ActionType

        author = self._agent(STIMULUS_AUTHOR_INDEX)
        self._stimulus_marker = (
            f"[simumarket-stimulus:{self._request.analysis_ref}:{round_index}:{label}]"
        )
        content = f"{self._stimulus_marker} " + json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True
        )
        result = await author.perform_action_by_data(
            ActionType.CREATE_POST,
            content=f"[round {round_index} {label}] {content}",
        )
        if isinstance(result, dict):
            post_id = result.get("post_id")
            if isinstance(post_id, int):
                self._stimulus_post_id = post_id

    async def step(
        self,
        agent_indices: Sequence[int],
        *,
        round_index: int,
    ) -> Mapping[int, SocialActionResult]:
        # What `OasisEnv.step` does before dispatching, and the reason round 2 is
        # a social round at all: without it every persona sees the same static
        # feed and "interaction" would be a second independent monologue.
        await self._env.platform.update_rec_table()

        async def act(index: int) -> tuple[int, object, int, bool]:
            began = time.perf_counter_ns()
            agent = self._agent(index)
            environment_prompt = await agent.env.to_text_prompt()
            observed_stimulus = bool(
                self._stimulus_marker and self._stimulus_marker in str(environment_prompt)
            )
            # `OasisEnv.step` normally applies its own LLM semaphore. We call
            # the agent directly so the chosen action and token usage remain
            # observable, therefore the adapter must preserve the same limit.
            async with self._llm_semaphore:
                response = await agent.perform_action_by_llm()
            elapsed_ms = (time.perf_counter_ns() - began) // 1_000_000
            return index, response, elapsed_ms, observed_stimulus

        responses = await asyncio.gather(
            *(act(index) for index in agent_indices),
            return_exceptions=True,
        )

        taken: dict[int, SocialActionResult] = {}
        for requested_index, response in zip(agent_indices, responses, strict=True):
            if isinstance(response, BaseException):
                logger.warning(
                    "oasis_agent_action_failed",
                    extra={"agent_index": requested_index, "round_index": round_index},
                )
                taken[requested_index] = SocialActionResult(
                    action=ACTION_DO_NOTHING,
                    observed_stimulus=False,
                )
                continue
            index, agent_response, elapsed_ms, observed_stimulus = response
            if isinstance(agent_response, BaseException):
                logger.warning(
                    "oasis_agent_action_failed",
                    extra={"agent_index": index, "round_index": round_index},
                )
                taken[index] = SocialActionResult(
                    action=ACTION_DO_NOTHING,
                    duration_ms=elapsed_ms,
                    observed_stimulus=observed_stimulus,
                )
                continue
            info = getattr(agent_response, "info", None)
            taken[index] = SocialActionResult(
                action=_chosen_action(info),
                tokens=_usage_tokens(info),
                duration_ms=elapsed_ms,
                observed_stimulus=observed_stimulus,
            )
        return taken

    # ---------------------------------------------------------------- helper

    def _agent(self, index: int) -> Any:
        if self._graph is None:
            raise OasisUnavailableError("Environment OASIS belum disiapkan.")
        return self._graph.get_agent(index)


class LiveOasisAdapter:
    adapter_id = "oasis-live"
    is_fake = False

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        provider: OasisProvider,
    ) -> None:
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
        runtime = CamelCouncilRuntime(
            api_key=self._api_key,
            model_id=self._model_id,
            provider=self._provider,
            request=request,
            manifest=manifest,
            roster=roster,
        )
        orchestrator = CouncilOrchestrator(
            runtime,
            model_id=self._model_id,
            request=request,
            manifest=manifest,
        )
        return await orchestrator.run(finance_tool)
