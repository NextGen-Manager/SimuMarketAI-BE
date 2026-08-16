"""The docs/04 execution protocol, driven against a recording runtime.

These tests exist because the protocol used to be prose. The adapter ran one
flat pass of chat completions per council and called it a simulation: no
baseline interview, no stimulus, no interaction, no intervention, no upstream
hand-off, and an "opinion shift" the model reported about itself.

The runtime here records every call the orchestrator makes, so the protocol is
asserted as behaviour rather than described in a docstring. Only the binding to
`camel-oasis` is left unverified; the sequence, the activation policy, the
budgets, and the arithmetic are all covered from here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.agents import (
    ConceptCard,
    CustomerSimulationResult,
    FinanceBounds,
    FinanceToolCall,
    OasisBudgetExceededError,
    RunManifest,
    SimulationBudget,
    SimulationRequest,
    TraceArtifact,
    build_cohort_manifest,
)
from app.integrations.oasis.council_runtime import (
    ACTION_CREATE_COMMENT,
    ACTION_DO_NOTHING,
    ACTION_LIKE_POST,
    AgentReply,
    SocialActionResult,
)
from app.integrations.oasis.orchestration_support import build_roster, rounds_for
from app.integrations.oasis.orchestrator import CouncilOrchestrator

FINANCE_RULE_VERSION = "finance-v1"


def _request(*, cohort: int = 12, rounds: int = 4, tokens: int = 120_000) -> SimulationRequest:
    return SimulationRequest(
        analysis_ref="a" * 32,
        correlation_ref="b" * 32,
        concept=ConceptCard(
            business_type="food_stall",
            concept_name="Rice Bowl Sambal",
            area_id="jabodetabek-tebet",
            analysis_radius_m=1500,
            price_idr=18_000,
            variable_cost_per_unit_idr=11_000,
            channels=["takeaway"],
            value_proposition="Makan siang cepat",
        ),
        evidence=[],
        missing_evidence_metrics=["competitor_count"],
        finance_bounds=FinanceBounds(
            volume_units_day_min=25,
            volume_units_day_base=40,
            volume_units_day_max=55,
            variable_cost_per_unit_idr=11_000,
        ),
        finance_rule_version=FINANCE_RULE_VERSION,
        budget=SimulationBudget(
            persona_count=cohort,
            round_limit=rounds,
            token_budget=tokens,
            max_output_tokens_per_stage=1_024,
            concurrency_limit=4,
            wall_clock_seconds=240,
            retry_limit=1,
        ),
        cohort=build_cohort_manifest(cohort_version="jabodetabek-fnb-v1", size=cohort),
        seed=42,
    )


def _manifest(request: SimulationRequest) -> RunManifest:
    return RunManifest(
        environment_id="analysis-test-000000000000",
        adapter_id="oasis-stub",
        provider="stub",
        model_id="stub-model",
        oasis_version="0.2.5",
        camel_version="0.2.78",
        prompt_version="oasis-council-v1",
        cohort=request.cohort,
        seed=request.seed,
        budget=request.budget,
        trace=TraceArtifact(object_key="memory://trace", retention_days=30),
        evidence_snapshot_version="evidence-snapshot-fixture-v1",
        input_snapshot_hash="0" * 64,
        created_at=datetime.now(UTC),
    )


def _finance_tool(assumptions: Mapping[str, int]) -> FinanceToolCall:
    volume = int(assumptions["volume_units_day"])
    return FinanceToolCall(
        tool_call_id=f"finance-volume-{volume}",
        rule_version=FINANCE_RULE_VERSION,
        assumptions={"volume_units_day": volume},
        outputs={"contribution_margin_per_unit_idr": 7_000, "bep_units_month": 300},
    )


@dataclass
class _Call:
    kind: str
    agent_index: int | None = None
    round_index: int = 0
    purpose: str = ""
    prompt: str = ""
    label: str = ""
    activated: tuple[int, ...] = ()


@dataclass
class _StubRuntime:
    """Records what the orchestrator asked for and answers deterministically."""

    roster: tuple[Any, ...]
    calls: list[_Call] = field(default_factory=list)
    restricted: dict[int, tuple[str, ...]] = field(default_factory=dict)
    tokens_per_reply: int = 100
    tokens_per_action: int = 25
    reply_delay_seconds: float = 0.0
    started: bool = False
    closed: bool = False
    stimulus_published: bool = False
    # Personas whose final choice differs from their baseline choice.
    shifting: frozenset[str] = frozenset()
    liking: frozenset[str] = frozenset()
    unexposed: frozenset[str] = frozenset()

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def restrict_actions(self, agent_index: int, actions: Sequence[str]) -> None:
        self.restricted[agent_index] = tuple(actions)

    async def interview(
        self, agent_index: int, prompt: str, *, round_index: int, purpose: str
    ) -> AgentReply:
        if self.reply_delay_seconds:
            await asyncio.sleep(self.reply_delay_seconds)
        self.calls.append(
            _Call(
                kind="interview",
                agent_index=agent_index,
                round_index=round_index,
                purpose=purpose,
                prompt=prompt,
            )
        )
        role, member = self.roster[agent_index]
        payload = self._payload(role, member, purpose, prompt)
        return AgentReply(content=json.dumps(payload), tokens=self.tokens_per_reply)

    async def publish_stimulus(
        self, payload: Mapping[str, object], *, round_index: int, label: str
    ) -> None:
        self.stimulus_published = True
        self.calls.append(
            _Call(kind="publish", round_index=round_index, label=label, prompt=json.dumps(payload))
        )

    async def step(
        self, agent_indices: Sequence[int], *, round_index: int
    ) -> Mapping[int, SocialActionResult]:
        self.calls.append(
            _Call(kind="step", round_index=round_index, activated=tuple(agent_indices))
        )
        taken: dict[int, SocialActionResult] = {}
        for index in agent_indices:
            agent_id = self.roster[index][1].agent_id
            taken[index] = SocialActionResult(
                action=ACTION_LIKE_POST if agent_id in self.liking else ACTION_CREATE_COMMENT,
                tokens=self.tokens_per_action,
                duration_ms=1,
                observed_stimulus=(self.stimulus_published and agent_id not in self.unexposed),
            )
        return taken

    # ---------------------------------------------------------------- answers

    def _payload(self, role: str, member: Any, purpose: str, prompt: str) -> dict[str, Any]:
        if role == "customer_persona":
            baseline = purpose == "baseline_interview"
            shifts = member.agent_id in self.shifting
            choice = "consider" if baseline or not shifts else "purchase"
            return {
                "agent_id": member.agent_id,
                "archetype": member.archetype,
                "choice": choice,
                "objection_code": "price_above_comfort",
                "objection_label": "Harga di atas batas nyaman",
                "acceptable_price_min_idr": 16_000,
                "acceptable_price_max_idr": 20_000,
                "quote": "Respons sintetis: saya masih menimbang.",
            }
        if role == "market_analyst":
            return {
                "headline": "Bukti pasar belum lengkap.",
                "observations": [
                    {
                        "id": "MA-001",
                        "stance": "uncertainty",
                        "claim": "Metrik kompetitor belum tersedia.",
                        "evidence_metrics": [],
                        "confidence": "low",
                    }
                ],
                "evidence_gaps": ["competitor_count"],
                "disagreements": [],
            }
        if role == "finance":
            ids = [part.split('"')[0] for part in prompt.split('"tool_call_id": "')[1:]]
            return {
                "finance_rule_version": FINANCE_RULE_VERSION,
                "critiques": [
                    {
                        "id": "FIN-001",
                        "assumption": "Volume dasar tercapai sejak bulan pertama.",
                        "concern": "Volume awal umumnya di bawah rencana.",
                        "severity": "high",
                        "tool_call_ids": ids[:1],
                    }
                ],
                "fragile_assumptions": [],
            }
        received = [
            name
            for name in ("MarketAssessment", "CustomerSimulationResult", "FinanceReview")
            if f'"{name}"' in prompt
        ]
        return {
            "sections": [
                {
                    "id": "NAR-001",
                    "title": "Ringkasan",
                    "body": "Seluruh angka berasal dari engine.",
                    "source_artifact_types": received or ["MarketAssessment"],
                }
            ],
            "red_team_findings": [],
            "removed_unsupported_claims": [],
        }


async def _run(runtime: _StubRuntime, request: SimulationRequest) -> Any:
    orchestrator = CouncilOrchestrator(
        runtime,
        model_id="stub-model",
        request=request,
        manifest=_manifest(request),
    )
    return await orchestrator.run(_finance_tool)


def _stub(request: SimulationRequest, **kwargs: Any) -> _StubRuntime:
    return _StubRuntime(roster=build_roster(request), **kwargs)


# ------------------------------------------------------------------- protocol


async def test_persona_rounds_follow_the_documented_order() -> None:
    """Baseline interview, exposure, interaction, intervention, final ballot."""
    request = _request()
    runtime = _stub(request)

    await _run(runtime, request)

    persona_calls = [
        call
        for call in runtime.calls
        if call.kind != "interview" or not call.purpose.startswith("deliberation:")
    ]
    sequence = [
        (call.kind, call.purpose or call.label) for call in persona_calls if call.kind != "restrict"
    ]

    # Round 0 is private: nothing is published and no step happens before it.
    assert sequence[0] == ("interview", "baseline_interview")
    assert ("publish", "exposure") in sequence
    assert ("publish", "intervention") in sequence
    assert ("step", "") in sequence
    assert sequence[-1] == ("interview", "final_ballot")

    # Exposure is published before anyone acts on it.
    first_publish = next(i for i, call in enumerate(persona_calls) if call.kind == "publish")
    first_step = next(i for i, call in enumerate(persona_calls) if call.kind == "step")
    assert first_publish < first_step


async def test_every_round_is_recorded_with_its_activation_subset() -> None:
    request = _request()
    runtime = _stub(request)

    outcome = await _run(runtime, request)

    persona_run = next(run for run in outcome.agent_runs if run.role == "customer_persona")
    kinds = [record.kind for record in persona_run.rounds]
    assert kinds == [
        "baseline_interview",
        "exposure",
        "interaction",
        "intervention",
        "final_ballot",
    ]

    exposure = next(record for record in persona_run.rounds if record.kind == "exposure")
    interaction = next(record for record in persona_run.rounds if record.kind == "interaction")
    # docs/04 asks for the activation subset to be stored, which is only
    # meaningful if it is genuinely a subset.
    assert len(exposure.activated_agent_ids) == request.cohort.size
    assert exposure.exposed_agent_ids == exposure.activated_agent_ids
    assert 0 < len(interaction.activated_agent_ids) < request.cohort.size
    assert set(interaction.activated_agent_ids) <= set(exposure.activated_agent_ids)
    assert interaction.actions


async def test_a_smaller_round_budget_drops_rounds_instead_of_faking_them() -> None:
    request = _request(rounds=2)
    runtime = _stub(request)

    outcome = await _run(runtime, request)

    persona_run = next(run for run in outcome.agent_runs if run.role == "customer_persona")
    kinds = [record.kind for record in persona_run.rounds]
    assert kinds == ["baseline_interview", "exposure", "final_ballot"]
    assert "intervention" not in kinds

    assert isinstance(persona_run.artifact, CustomerSimulationResult)
    # The reported round count never exceeds the budget the manifest declares.
    assert persona_run.artifact.rounds == len(rounds_for(2)) == 2
    assert all(record.index < 2 for record in persona_run.rounds)


async def test_a_one_round_budget_is_never_silently_expanded() -> None:
    request = _request(rounds=1)
    runtime = _stub(request)

    outcome = await _run(runtime, request)

    persona_run = next(run for run in outcome.agent_runs if run.role == "customer_persona")
    assert isinstance(persona_run.artifact, CustomerSimulationResult)
    assert persona_run.artifact.rounds == len(rounds_for(1)) == 1
    assert not [call for call in runtime.calls if call.kind == "step"]


async def test_the_intervention_changes_one_variable_and_introduces_no_number() -> None:
    """docs/04 allows price, promo, or message; only message is number-free."""
    request = _request()
    runtime = _stub(request)

    await _run(runtime, request)

    exposure = next(call for call in runtime.calls if call.label == "exposure")
    intervention = next(call for call in runtime.calls if call.label == "intervention")
    before = json.loads(exposure.prompt)
    after = json.loads(intervention.prompt)

    assert after["price_idr"] == before["price_idr"]
    assert after["claims"] != before["claims"]
    assert "message_variant" in after


# -------------------------------------------------------- derived, not reported


async def test_opinion_shift_is_computed_not_self_reported() -> None:
    request = _request()
    shifting = {
        member.agent_id for role, member in build_roster(request) if role == "customer_persona"
    }
    changed = frozenset(sorted(shifting)[:3])
    runtime = _stub(request, shifting=changed)

    outcome = await _run(runtime, request)

    simulation = outcome.customer_simulation
    assert simulation is not None
    # Exactly the personas whose final choice differs from their baseline.
    assert simulation.opinion_shift_count == 3


async def test_positive_reactions_come_from_observed_actions() -> None:
    request = _request()
    personas = [
        member.agent_id for role, member in build_roster(request) if role == "customer_persona"
    ]
    runtime = _stub(request, liking=frozenset(personas[:5]))

    outcome = await _run(runtime, request)

    simulation = outcome.customer_simulation
    assert simulation is not None
    # Only the personas that actually took a LIKE action in a round are counted;
    # a comment is not a positive reaction.
    assert simulation.positive_reaction_count == 5


async def test_no_reactions_observed_means_a_zero_not_a_guess() -> None:
    request = _request()
    runtime = _stub(request, liking=frozenset())

    outcome = await _run(runtime, request)

    simulation = outcome.customer_simulation
    assert simulation is not None
    assert simulation.positive_reaction_count == 0
    assert simulation.activated_persona_count == request.cohort.size


async def test_unexposed_personas_are_not_counted_as_positive_reactions() -> None:
    request = _request()
    personas = [
        member.agent_id for role, member in build_roster(request) if role == "customer_persona"
    ]
    runtime = _stub(
        request,
        liking=frozenset(personas),
        unexposed=frozenset(personas[:1]),
    )

    outcome = await _run(runtime, request)

    simulation = outcome.customer_simulation
    assert simulation is not None
    assert simulation.positive_reaction_count == len(personas) - 1
    assert any("exposure" in item for item in simulation.limitations)


# ------------------------------------------------------------------- handoffs


async def test_the_report_council_actually_receives_the_upstream_artifacts() -> None:
    request = _request()
    runtime = _stub(request)

    outcome = await _run(runtime, request)

    report_prompts = [
        call.prompt
        for call in runtime.calls
        if call.kind == "interview" and call.purpose == "deliberation:report"
    ]
    assert report_prompts
    for prompt in report_prompts:
        assert "MarketAssessment" in prompt
        assert "CustomerSimulationResult" in prompt
        assert "FinanceReview" in prompt

    report_run = next(run for run in outcome.agent_runs if run.role == "report")
    assert set(report_run.consumed_artifact_types) == {
        "MarketAssessment",
        "CustomerSimulationResult",
        "FinanceReview",
    }


async def test_a_council_that_received_nothing_claims_nothing() -> None:
    """Provenance describes the run, not the plan."""
    request = _request()
    runtime = _stub(request)

    outcome = await _run(runtime, request)

    for role in ("market_analyst", "customer_persona", "finance"):
        record = next(run for run in outcome.agent_runs if run.role == role)
        assert record.consumed_artifact_types == []


async def test_a_failed_upstream_council_is_not_claimed_as_a_source() -> None:
    request = _request()

    class _MarketFails(_StubRuntime):
        async def interview(
            self, agent_index: int, prompt: str, *, round_index: int, purpose: str
        ) -> AgentReply:
            if self.roster[agent_index][0] == "market_analyst":
                self.calls.append(_Call(kind="interview", purpose=purpose, prompt=prompt))
                return AgentReply(content="", tokens=self.tokens_per_reply)
            return await super().interview(
                agent_index, prompt, round_index=round_index, purpose=purpose
            )

    runtime = _MarketFails(roster=build_roster(request))
    outcome = await _run(runtime, request)

    assert outcome.status == "partial"
    report_run = next(run for run in outcome.agent_runs if run.role == "report")
    # The Market council produced no artifact, so the Report council neither saw
    # it nor may claim it.
    assert "MarketAssessment" not in report_run.consumed_artifact_types
    assert set(report_run.consumed_artifact_types) == {
        "CustomerSimulationResult",
        "FinanceReview",
    }


async def test_each_council_member_sees_the_previous_member_draft() -> None:
    """Otherwise "Skeptic challenges" has nothing to challenge."""
    request = _request()
    runtime = _stub(request)
    await _run(runtime, request)
    market = [
        call.prompt
        for call in runtime.calls
        if call.kind == "interview" and call.purpose == "deliberation:market_analyst"
    ]
    assert len(market) == 3
    assert "draft" not in market[0]
    assert "draft" in market[1]
    assert "draft" in market[2]


async def test_the_finance_council_only_ever_sees_calculator_output() -> None:
    request = _request()
    runtime = _stub(request)
    outcome = await _run(runtime, request)
    finance_prompts = [
        call.prompt
        for call in runtime.calls
        if call.kind == "interview" and call.purpose == "deliberation:finance"
    ]
    assert finance_prompts
    for prompt in finance_prompts:
        assert "finance-volume-25" in prompt
        assert "finance-volume-40" in prompt
        assert "finance-volume-55" in prompt
    review = outcome.finance_review
    assert review is not None
    known = {call.tool_call_id for call in review.tool_calls}
    for critique in review.critiques:
        assert set(critique.tool_call_ids) <= known


async def test_action_allowlists_are_applied_per_agent_not_globally() -> None:
    request = _request()
    runtime = _stub(request)
    await _run(runtime, request)
    roster = build_roster(request)
    assert len(runtime.restricted) == len(roster)
    persona_index = next(i for i, (role, _) in enumerate(roster) if role == "customer_persona")
    finance_index = next(i for i, (role, _) in enumerate(roster) if role == "finance")
    assert ACTION_LIKE_POST in runtime.restricted[persona_index]
    assert ACTION_LIKE_POST not in runtime.restricted[finance_index]
    assert ACTION_DO_NOTHING not in runtime.restricted[finance_index]


async def test_instance_tokens_are_per_instance_not_cumulative() -> None:
    request = _request()
    runtime = _stub(request, tokens_per_reply=100)
    outcome = await _run(runtime, request)
    market_run = next(run for run in outcome.agent_runs if run.role == "market_analyst")
    assert [instance.total_tokens for instance in market_run.instances] == [100, 100, 100]
    assert market_run.total_tokens == 300
    persona_run = next(run for run in outcome.agent_runs if run.role == "customer_persona")
    assert all(instance.total_tokens >= 200 for instance in persona_run.instances)
    assert persona_run.total_tokens > 12 * 200


async def test_social_action_tokens_are_charged_to_the_hard_budget() -> None:
    request = _request(tokens=3_200)
    runtime = _stub(request, tokens_per_reply=100, tokens_per_action=1_000)
    with pytest.raises(OasisBudgetExceededError):
        await _run(runtime, request)


async def test_council_duration_is_measured_not_left_at_zero() -> None:
    request = _request()
    runtime = _stub(request, reply_delay_seconds=0.005)
    outcome = await _run(runtime, request)
    assert all(run.duration_ms > 0 for run in outcome.agent_runs)
    market_run = next(run for run in outcome.agent_runs if run.role == "market_analyst")
    assert all(instance.duration_ms > 0 for instance in market_run.instances)


async def test_the_token_budget_stops_a_run_that_would_exceed_it() -> None:
    request = _request(tokens=500)
    runtime = _stub(request, tokens_per_reply=100)
    with pytest.raises(OasisBudgetExceededError):
        await _run(runtime, request)
    assert runtime.closed is True


async def test_the_runtime_is_always_closed() -> None:
    request = _request()
    runtime = _stub(request)
    await _run(runtime, request)
    assert runtime.started is True
    assert runtime.closed is True
