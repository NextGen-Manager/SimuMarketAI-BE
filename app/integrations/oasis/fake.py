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

    # ---------------------------------------------------------------- public

    async def simulate(
        self,
        request: SimulationRequest,
        *,
        finance_tool: FinanceTool,
        manifest: RunManifest,
    ) -> SimulationOutcome:
        if self._error is not None:
            raise self._error

        trace = _TraceWriter(Path(manifest.trace.object_key))
        started = time.monotonic()
        consumed = 0
        runs: list[AgentRunRecord] = []
        warnings: list[str] = []
        tool_calls: tuple[FinanceToolCall, ...] = ()

        try:
            for role in AGENT_ROLES:
                await self._sleep_stage()
                elapsed = time.monotonic() - started
                if elapsed > request.budget.wall_clock_seconds:
                    raise OasisTimeoutError(OasisTimeoutError.reason)

                members = council_for(role, request)
                consumed += len(members) * self._token_cost
                if consumed > request.budget.token_budget:
                    raise OasisBudgetExceededError(OasisBudgetExceededError.reason)

                if role == "finance":
                    tool_calls = self._run_finance_tool(request, finance_tool)
                    for call in tool_calls:
                        trace.write(
                            agent_id="finance-calculator",
                            role=role,
                            round_index=0,
                            action="run_finance_calculator",
                            payload=call.model_dump(mode="json"),
                        )

                run = self._run_council(
                    role,
                    request,
                    members,
                    trace=trace,
                    tool_calls=tool_calls,
                    token_cost=self._token_cost,
                )
                runs.append(run)
                if run.status == "failed":
                    warnings.append(f"Council {role} gagal: {run.failure_code}.")
                elif run.validation_status == "rejected":
                    warnings.append(f"Artifact {role} ditolak validasi schema.")
        finally:
            trace.close()

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

    # --------------------------------------------------------------- councils

    async def _sleep_stage(self) -> None:
        if self._stage_delay > 0:
            await asyncio.sleep(self._stage_delay)

    def _run_finance_tool(
        self, request: SimulationRequest, finance_tool: FinanceTool
    ) -> tuple[FinanceToolCall, ...]:
        bounds = request.finance_bounds
        proposals = (
            {"volume_units_day": bounds.volume_units_day_min},
            {"volume_units_day": bounds.volume_units_day_base},
            {"volume_units_day": bounds.volume_units_day_max},
        )
        return tuple(finance_tool(proposal) for proposal in proposals)

    def _run_council(
        self,
        role: AgentRole,
        request: SimulationRequest,
        members: tuple[CouncilMember, ...],
        *,
        trace: _TraceWriter,
        tool_calls: tuple[FinanceToolCall, ...],
        token_cost: int,
    ) -> AgentRunRecord:
        instances = [
            AgentInstanceRecord(
                agent_id=member.agent_id,
                role=role,
                archetype=member.archetype,
                profile_version=PROFILE_VERSION,
                model_id=self.adapter_id,
                allowed_actions=list(member.allowed_actions),
                activation_order=index,
                total_tokens=token_cost,
                duration_ms=1,
                outcome="failed" if role in self._failing else "completed",
            )
            for index, member in enumerate(members)
        ]
        total_tokens = token_cost * len(members)

        for index, member in enumerate(members):
            prompt = build_prompt(
                member,
                request,
                round_index=min(index, request.budget.round_limit - 1),
                finance_tool_calls=tool_calls if role == "finance" else (),
            )
            trace.write(
                agent_id=member.agent_id,
                role=role,
                round_index=min(index, request.budget.round_limit - 1),
                action="prompt",
                payload={"prompt_length": len(prompt)},
            )

        if role in self._failing:
            return AgentRunRecord(
                role=role,
                status="failed",
                instances=instances,
                total_tokens=total_tokens,
                duration_ms=1,
                failure_code="oasis_council_failed",
            )

        raw = self._emit(role, request, members, tool_calls)
        trace.write(
            agent_id=f"{role}-reducer",
            role=role,
            round_index=request.budget.round_limit - 1,
            action="reduce",
            payload={"keys": sorted(raw)},
        )
        try:
            artifact = validate_council_payload(role, raw, request, tool_calls)
        except (ValidationError, ValueError, KeyError):
            return AgentRunRecord(
                role=role,
                status="failed",
                instances=instances,
                total_tokens=total_tokens,
                duration_ms=1,
                schema_failures=1,
                failure_code="oasis_schema_invalid",
                validation_status="rejected",
            )

        return AgentRunRecord(
            role=role,
            status="completed",
            instances=instances,
            total_tokens=total_tokens,
            duration_ms=1,
            artifact=artifact,
        )

    # ----------------------------------------------------------------- output

    def _emit(
        self,
        role: AgentRole,
        request: SimulationRequest,
        members: tuple[CouncilMember, ...],
        tool_calls: tuple[FinanceToolCall, ...],
    ) -> dict[str, Any]:
        if role in self._invalid:
            # A shape the schema cannot accept, so the caller exercises the real
            # rejection path instead of a simulated one.
            return {"unexpected": "payload"}
        if role == "market_analyst":
            return _market_payload(request)
        if role == "customer_persona":
            return _persona_payload(request, members)
        if role == "finance":
            return _finance_payload(request, tool_calls)
        return _report_payload(request, extra_number=self._narrative_extra_number)


# ------------------------------------------------------------------- payloads


def _market_payload(request: SimulationRequest) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for index, item in enumerate(request.evidence):
        observations.append(
            {
                "id": f"MA-{index + 1:03d}",
                "stance": "risk" if item.metric == "competitor_count" else "opportunity",
                "claim": (
                    f"Nilai {item.metric} pada radius analisis berasal dari {item.source} "
                    "dan perlu diperiksa ulang di lapangan."
                ),
                "evidence_metrics": [item.metric],
                "confidence": "medium" if item.confidence_percent >= 50 else "low",
            }
        )
    for index, metric in enumerate(request.missing_evidence_metrics):
        observations.append(
            {
                "id": f"MA-GAP-{index + 1:03d}",
                "stance": "uncertainty",
                "claim": f"Metrik {metric} belum tersedia sehingga kesimpulan pasar terbatas.",
                "evidence_metrics": [],
                "confidence": "low",
            }
        )
    if not observations:
        observations.append(
            {
                "id": "MA-GAP-001",
                "stance": "uncertainty",
                "claim": "Tidak ada bukti pasar yang dapat dinilai pada run ini.",
                "evidence_metrics": [],
                "confidence": "low",
            }
        )
    return {
        "headline": "Penilaian pasar dibatasi oleh cakupan bukti yang tersedia.",
        "observations": observations[:12],
        "evidence_gaps": list(request.missing_evidence_metrics),
        "disagreements": [
            "Opportunity Scout dan Competition Skeptic berbeda pandangan tentang "
            "kesiapan lokasi karena bukti belum lengkap."
        ],
    }


def _persona_payload(
    request: SimulationRequest, members: tuple[CouncilMember, ...]
) -> dict[str, Any]:
    price = request.concept.price_idr
    ballots: list[dict[str, Any]] = []
    for index, member in enumerate(members):
        # Deterministic from the manifest seed and roster position, so the same
        # manifest always reproduces the same distribution.
        rotation = (request.seed + index) % len(CHOICES)
        choice = CHOICES[rotation]
        objection = OBJECTION_CODES[(request.seed + index) % len(OBJECTION_CODES)]
        spread = (index % 3 + 1) * 1_000
        ballots.append(
            {
                "agent_id": member.agent_id,
                "archetype": member.archetype or "budget_driven",
                "choice": choice,
                "reacted": choice != "reject",
                "objection_code": objection,
                "objection_label": OBJECTION_LABELS[objection],
                "acceptable_price_min_idr": max(0, price - spread),
                "acceptable_price_max_idr": price + spread,
                "quote": (
                    "Respons sintetis: tawaran ini "
                    + {
                        "purchase": "cukup masuk akal untuk saya coba.",
                        "consider": "masih perlu saya bandingkan dengan pilihan lain.",
                        "reject": "belum sesuai kebutuhan saya saat ini.",
                    }[choice]
                ),
                "shifted": index % 4 == 0,
            }
        )
    return {"ballots": ballots}


def _finance_payload(
    request: SimulationRequest, tool_calls: tuple[FinanceToolCall, ...]
) -> dict[str, Any]:
    ids = [call.tool_call_id for call in tool_calls]
    return {
        "finance_rule_version": request.finance_rule_version,
        "critiques": [
            {
                "id": "FIN-001",
                "assumption": "Volume harian dasar dapat dicapai sejak bulan pertama.",
                "concern": (
                    "Volume awal usaha baru umumnya di bawah rencana sehingga skenario "
                    "dasar perlu diperlakukan sebagai target, bukan perkiraan."
                ),
                "severity": "high",
                "tool_call_ids": ids[:1] or ids,
            },
            {
                "id": "FIN-002",
                "assumption": "Biaya variabel per unit tetap sepanjang bulan.",
                "concern": (
                    "Harga bahan segar berfluktuasi sehingga marjin kontribusi dapat "
                    "berubah tanpa perubahan harga jual."
                ),
                "severity": "medium",
                "tool_call_ids": ids,
            },
        ],
        "fragile_assumptions": [
            "Susut bahan tidak dimasukkan ke perhitungan.",
            "Biaya platform pesan-antar belum diperhitungkan.",
        ],
    }


def _report_payload(request: SimulationRequest, *, extra_number: int | None) -> dict[str, Any]:
    body = (
        "Penilaian ini menggabungkan bukti pasar yang tersedia, hasil kalkulator "
        "deterministik, dan respons sintetis panel persona. Seluruh angka pada "
        "laporan berasal dari engine, bukan dari narasi ini."
    )
    if extra_number is not None:
        body += f" Perkiraan tambahan tanpa sumber: {extra_number}."
    return {
        "sections": [
            {
                "id": "NAR-001",
                "title": "Ringkasan penilaian",
                "body": body,
                "source_artifact_types": ["MarketAssessment", "FinanceReview"],
            },
            {
                "id": "NAR-002",
                "title": "Batas penggunaan",
                "body": (
                    "Respons persona adalah sinyal sintetis eksploratif dan tidak boleh "
                    "dibaca sebagai perilaku pelanggan nyata. Bukti yang belum tersedia "
                    "menurunkan keyakinan, bukan skor."
                ),
                "source_artifact_types": ["CustomerSimulationResult"],
            },
        ],
        "red_team_findings": [
            "Draft awal menyebut peluang tanpa menunjuk metrik; klaim tersebut dihapus."
        ],
        "removed_unsupported_claims": [
            "Klaim tentang pertumbuhan permintaan tahunan dihapus karena tidak ada bukti."
        ],
    }


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
