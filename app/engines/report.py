"""Deterministic report composer.

This module must never import OASIS, an LLM client, or anything under
`app/integrations`. It assembles a complete report from typed artifacts only,
which is what makes the fallback path in docs/04 a tested main road rather than
an untested branch.

Nothing here invents a number. Every value is copied from the finance result,
the score result, or an evidence record, and every recommendation names the
artifact it came from.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.domain.agents import (
    ARCHETYPE_LABELS,
    CustomerSimulationResult,
    FinanceReview,
    MarketAssessment,
    ReportNarrative,
    SimulationOutcome,
)
from app.domain.analysis_state import SKIP_REASON_SIMULATING, AnalysisStatus
from app.domain.evidence import (
    METRIC_COMPARABLE_PRICE_MEDIAN,
    METRIC_COMPARABLE_PRICE_SAMPLE_SIZE,
    METRIC_COMPETITOR_COUNT,
    METRIC_LABELS,
    METRIC_POPULATION_COUNT,
    EvidenceSnapshot,
)
from app.domain.taxonomy import BUSINESS_TAXONOMY_VERSION
from app.engines.finance import ROUNDING_NOTE
from app.engines.scoring import DIMENSION_LABELS
from app.schemas.analysis import (
    AgentCritiqueView,
    AgentNarrativeSectionView,
    AgentObservationView,
    AgentReview,
    AgentRunManifestView,
    AnalysisInput,
    AnalysisReport,
    AnalysisWarning,
    EvidenceConfidence,
    FinanceResult,
    MarketSection,
    ReportRecommendation,
    ReportRisk,
    ScoreResult,
    SyntheticObjectionView,
    SyntheticPriceBandView,
    SyntheticQuoteView,
    SyntheticSegmentView,
    SyntheticSimulation,
)

BASE_LIMITATIONS: tuple[str, ...] = (
    "Bobot Launch Readiness Score berstatus hipotesis pada rule set "
    "lrs-v0.2-unvalidated dan belum melewati expert review maupun kalibrasi.",
    "Dimensi kesiapan operasional dinilai dari data yang dilaporkan pengguna, "
    "bukan dari pengukuran lapangan.",
    "Bobot Evidence Confidence masih berupa hipotesis pada formula "
    "evidence-confidence-v0.1-unvalidated dan belum dikalibrasi.",
    ROUNDING_NOTE,
)

SIMULATION_UNAVAILABLE_LIMITATION = (
    "Simulasi persona sintetis tidak tersedia pada laporan ini sehingga sinyal "
    "permintaan dari agent tidak dapat ditampilkan."
)

SIMULATION_AVAILABLE_LIMITATION = (
    "Hasil simulasi persona adalah sinyal sintetis eksploratif yang belum "
    "dikalibrasi terhadap wawancara manusia, dan tidak dipakai sebagai input skor."
)

NO_QUOTE_LIMITATION = "Tidak ada kutipan persona pada laporan ini karena simulasi belum dijalankan."

SEVERITY_LABELS: dict[str, str] = {"high": "tinggi", "medium": "sedang", "low": "rendah"}


def _market_section(analysis_input: AnalysisInput, evidence: EvidenceSnapshot) -> MarketSection:
    notes: list[str] = []
    values: dict[str, int | None] = {}
    for metric in (
        METRIC_COMPETITOR_COUNT,
        METRIC_POPULATION_COUNT,
        METRIC_COMPARABLE_PRICE_MEDIAN,
        METRIC_COMPARABLE_PRICE_SAMPLE_SIZE,
    ):
        record = evidence.find(metric)
        values[metric] = record.value if record is not None else None
        if record is not None:
            notes.append(
                f"{METRIC_LABELS[metric]} bersumber dari {record.source}, "
                f"diamati {record.observed_at.date().isoformat()}."
            )
    for entry in evidence.missing:
        notes.append(f"{METRIC_LABELS.get(entry.metric, entry.metric)}: {entry.reason}")

    return MarketSection(
        area_id=analysis_input.location.area_id,
        area_name=analysis_input.location.area_name,
        analysis_radius_m=analysis_input.location.analysis_radius_m,
        category_mapping_version=BUSINESS_TAXONOMY_VERSION,
        competitor_count=values[METRIC_COMPETITOR_COUNT],
        population_count=values[METRIC_POPULATION_COUNT],
        comparable_price_median_idr=values[METRIC_COMPARABLE_PRICE_MEDIAN],
        comparable_price_sample_size=values[METRIC_COMPARABLE_PRICE_SAMPLE_SIZE],
        notes=notes,
    )


def _risks(
    analysis_input: AnalysisInput,
    finance: FinanceResult,
    score: ScoreResult,
    confidence: EvidenceConfidence,
) -> list[ReportRisk]:
    risks: list[ReportRisk] = []
    margin = finance.contribution_margin_per_unit_idr
    if margin < 0:
        risks.append(
            ReportRisk(
                id="RISK-FIN-001",
                severity="tinggi",
                title="Marjin kontribusi negatif",
                detail=(
                    "Harga jual di bawah biaya variabel per unit sehingga setiap "
                    "penjualan menambah kerugian."
                ),
                source="finance.contribution_margin",
            )
        )
    elif margin == 0:
        risks.append(
            ReportRisk(
                id="RISK-FIN-002",
                severity="tinggi",
                title="Marjin kontribusi nol",
                detail="Penjualan tidak menyisakan apa pun untuk menutup biaya tetap.",
                source="finance.contribution_margin",
            )
        )

    if (
        finance.bep_units_day is not None
        and finance.bep_units_day > analysis_input.operations.capacity_units_day
    ):
        risks.append(
            ReportRisk(
                id="RISK-FIN-003",
                severity="tinggi",
                title="Volume BEP melampaui kapasitas",
                detail=(
                    f"BEP memerlukan {finance.bep_units_day} unit per hari, "
                    f"sedangkan kapasitas rencana "
                    f"{analysis_input.operations.capacity_units_day} unit per hari."
                ),
                source="finance.bep",
            )
        )

    base = finance.scenario("base")
    if base is not None and base.monthly_operating_profit_idr <= 0:
        risks.append(
            ReportRisk(
                id="RISK-FIN-004",
                severity="tinggi",
                title="Laba operasional skenario dasar tidak positif",
                detail=(
                    "Pada volume dasar, pendapatan belum menutup biaya tetap bulanan "
                    "sehingga payback tidak terdefinisi."
                ),
                source="finance.scenario.base",
            )
        )

    if finance.runway_months is not None and finance.runway_months < 3:
        risks.append(
            ReportRisk(
                id="RISK-FIN-005",
                severity="sedang",
                title="Runway modal pendek",
                detail=(f"Modal awal hanya menutup {finance.runway_months} bulan biaya tetap."),
                source="finance.runway",
            )
        )

    if score.status == "unavailable":
        missing = ", ".join(DIMENSION_LABELS[key] for key in score.missing_dimensions)
        risks.append(
            ReportRisk(
                id="RISK-SCORE-001",
                severity="tinggi",
                title="Skor kelayakan belum dapat dihitung",
                detail=f"Dimensi yang belum dapat dinilai: {missing}.",
                source="score.status",
            )
        )

    if confidence.label in {"rendah", "tidak_tersedia"}:
        risks.append(
            ReportRisk(
                id="RISK-EVIDENCE-001",
                severity="tinggi",
                title="Keyakinan bukti rendah",
                detail=(
                    "Sebagian besar metrik pasar belum memiliki sumber sehingga hasil "
                    "belum layak dipakai sebagai dasar keputusan tunggal."
                ),
                source="evidence_confidence",
            )
        )
    return risks


def _recommendations(
    analysis_input: AnalysisInput,
    evidence: EvidenceSnapshot,
    finance: FinanceResult,
    score: ScoreResult,
) -> list[ReportRecommendation]:
    recommendations: list[ReportRecommendation] = []
    margin = finance.contribution_margin_per_unit_idr
    if margin <= 0:
        recommendations.append(
            ReportRecommendation(
                id="REC-FIN-001",
                priority="tinggi",
                title="Perbaiki struktur harga atau biaya bahan",
                rationale=(
                    "Marjin kontribusi per unit belum positif sehingga BEP dan payback "
                    "tidak dapat dihitung."
                ),
                source="finance.contribution_margin",
            )
        )

    base = finance.scenario("base")
    if base is not None and base.monthly_operating_profit_idr <= 0:
        recommendations.append(
            ReportRecommendation(
                id="REC-FIN-002",
                priority="tinggi",
                title="Tinjau ulang biaya tetap atau target volume",
                rationale=(
                    "Skenario dasar belum menghasilkan laba operasional bulanan yang positif."
                ),
                source="finance.scenario.base",
            )
        )

    if (
        finance.bep_units_day is not None
        and finance.bep_units_day > analysis_input.operations.capacity_units_day
    ):
        recommendations.append(
            ReportRecommendation(
                id="REC-FIN-003",
                priority="tinggi",
                title="Sesuaikan kapasitas harian atau turunkan biaya tetap",
                rationale=(
                    "Volume BEP harian berada di atas kapasitas rencana sehingga BEP "
                    "tidak tercapai."
                ),
                source="finance.bep",
            )
        )

    if finance.runway_months is not None and finance.runway_months < 3:
        recommendations.append(
            ReportRecommendation(
                id="REC-FIN-004",
                priority="sedang",
                title="Siapkan dana operasional minimal tiga bulan",
                rationale="Runway modal terhadap biaya tetap saat ini di bawah tiga bulan.",
                source="finance.runway",
            )
        )

    if base is not None and base.exceeds_capacity:
        recommendations.append(
            ReportRecommendation(
                id="REC-OPS-001",
                priority="sedang",
                title="Selaraskan target volume dengan kapasitas",
                rationale="Volume skenario dasar melampaui kapasitas harian yang direncanakan.",
                source="finance.scenario.base",
            )
        )

    missing_metrics = evidence.missing_metrics()
    for entry in evidence.missing:
        recommendations.append(
            ReportRecommendation(
                id=f"REC-EVIDENCE-{entry.metric}",
                priority="sedang",
                title=f"Kumpulkan data {METRIC_LABELS.get(entry.metric, entry.metric).lower()}",
                rationale=entry.reason,
                source=f"evidence.missing.{entry.metric}",
            )
        )

    for dimension in score.dimensions:
        if dimension.status != "not_scorable":
            continue
        # Metrics the provider never returned already have a recommendation above.
        if dimension.missing_inputs and all(
            metric in missing_metrics for metric in dimension.missing_inputs
        ):
            continue
        recommendations.append(
            ReportRecommendation(
                id=f"REC-SCORE-{dimension.key}",
                priority="tinggi",
                title=f"Lengkapi bukti untuk dimensi {dimension.label.lower()}",
                rationale=dimension.rationale,
                source=f"score.dimension.{dimension.key}",
            )
        )

    return recommendations


def _simulation_section(
    result: CustomerSimulationResult | None, *, reason: str | None
) -> SyntheticSimulation:
    """Render the simulation section, present in both outcomes.

    docs/12 forbids removing a failed section from the table of contents, so an
    unavailable simulation still occupies its slot and states why.
    """
    if result is None:
        return SyntheticSimulation(
            status="unavailable",
            reason=reason or SKIP_REASON_SIMULATING,
            cohort_size=None,
            metrics={},
            limitations=[NO_QUOTE_LIMITATION],
        )

    return SyntheticSimulation(
        status="experimental",
        reason=None,
        cohort_size=result.cohort_size,
        cohort_version=result.cohort_version,
        rounds=result.rounds,
        metrics={
            "cohort_size": result.cohort_size,
            "activated_persona_count": result.activated_persona_count,
            "purchase_intent_count": result.purchase_intent_count,
            "positive_reaction_count": result.positive_reaction_count,
            "opinion_shift_count": result.opinion_shift_count,
        },
        segments=[
            SyntheticSegmentView(
                archetype=segment.archetype,
                label=ARCHETYPE_LABELS.get(segment.archetype, segment.archetype),
                persona_count=segment.persona_count,
                purchase_intent_count=segment.purchase_intent_count,
            )
            for segment in result.segments
        ],
        objections=[
            SyntheticObjectionView(code=item.code, label=item.label, count=item.count)
            for item in result.objections
        ],
        acceptable_price_band=(
            SyntheticPriceBandView(
                min_idr=result.acceptable_price_band.min_idr,
                max_idr=result.acceptable_price_band.max_idr,
            )
            if result.acceptable_price_band is not None
            else None
        ),
        quotes=[
            SyntheticQuoteView(
                agent_id=quote.agent_id,
                archetype=quote.archetype,
                text=quote.text,
            )
            for quote in result.quotes
        ],
        limitations=list(result.limitations),
    )


def _agent_review(simulation: SimulationOutcome | None, *, reason: str | None) -> AgentReview:
    if simulation is None:
        return AgentReview(status="unavailable", reason=reason or SKIP_REASON_SIMULATING)

    market = simulation.market_assessment
    finance_review = simulation.finance_review
    narrative = simulation.report_narrative
    manifest = simulation.manifest
    status: Literal["available", "partial", "unavailable"]
    if simulation.status == "completed":
        status = "available"
    elif market is None and finance_review is None and narrative is None:
        status = "unavailable"
    else:
        status = "partial"

    return AgentReview(
        status=status,
        reason=None if status == "available" else (reason or "Sebagian council tidak selesai."),
        manifest=AgentRunManifestView(
            adapter_id=manifest.adapter_id,
            provider=manifest.provider,
            model_id=manifest.model_id,
            prompt_version=manifest.prompt_version,
            cohort_version=manifest.cohort.cohort_version,
            oasis_version=manifest.oasis_version,
            camel_version=manifest.camel_version,
            seed=manifest.seed,
            persona_count=manifest.budget.persona_count,
            round_limit=manifest.budget.round_limit,
            token_budget=manifest.budget.token_budget,
            tokens_used=simulation.total_tokens,
        ),
        market_observations=_observations(market),
        evidence_gaps=list(market.evidence_gaps) if market else [],
        disagreements=list(market.disagreements) if market else [],
        finance_critiques=_critiques(finance_review),
        fragile_assumptions=list(finance_review.fragile_assumptions) if finance_review else [],
        narrative_sections=_narrative_sections(narrative),
        red_team_findings=list(narrative.red_team_findings) if narrative else [],
    )


def _observations(market: MarketAssessment | None) -> list[AgentObservationView]:
    if market is None:
        return []
    return [
        AgentObservationView(
            id=observation.id,
            stance=observation.stance,
            claim=observation.claim,
            evidence_metrics=list(observation.evidence_metrics),
            confidence=observation.confidence,
        )
        for observation in market.observations
    ]


def _critiques(review: FinanceReview | None) -> list[AgentCritiqueView]:
    if review is None:
        return []
    return [
        AgentCritiqueView(
            id=critique.id,
            assumption=critique.assumption,
            concern=critique.concern,
            severity=critique.severity,
            tool_call_ids=list(critique.tool_call_ids),
        )
        for critique in review.critiques
    ]


def _narrative_sections(narrative: ReportNarrative | None) -> list[AgentNarrativeSectionView]:
    if narrative is None:
        return []
    return [
        AgentNarrativeSectionView(
            id=section.id,
            title=section.title,
            body=section.body,
            source_artifact_types=list(section.source_artifact_types),
        )
        for section in narrative.sections
    ]


def _agent_risks(simulation: SimulationOutcome | None) -> list[ReportRisk]:
    """Promote finance critiques to risks, each pointing back at its artifact.

    The agent contributes judgement, not numbers: the critique text is carried
    through unchanged and the source names the artifact it came from, so a
    reader can tell a rule-derived risk from an agent-derived one.
    """
    review = simulation.finance_review if simulation is not None else None
    if review is None:
        return []
    return [
        ReportRisk(
            id=f"RISK-AGENT-{critique.id}",
            severity=SEVERITY_LABELS.get(critique.severity, "sedang"),
            title=critique.assumption,
            detail=critique.concern,
            source=f"agent.FinanceReview.{critique.id}",
        )
        for critique in review.critiques
    ]


def compose_report(
    *,
    analysis_id: UUID,
    status: AnalysisStatus,
    generated_at: datetime,
    analysis_input: AnalysisInput,
    evidence: EvidenceSnapshot,
    finance: FinanceResult,
    score: ScoreResult,
    confidence: EvidenceConfidence,
    warnings: Sequence[AnalysisWarning] = (),
    extra_limitations: Sequence[str] = (),
    simulation: SimulationOutcome | None = None,
    simulation_reason: str | None = None,
) -> AnalysisReport:
    customer = simulation.customer_simulation if simulation is not None else None

    limitations = list(BASE_LIMITATIONS)
    limitations.append(
        SIMULATION_AVAILABLE_LIMITATION
        if customer is not None
        else SIMULATION_UNAVAILABLE_LIMITATION
    )
    for record in evidence.items:
        for limitation in record.limitations:
            if limitation not in limitations:
                limitations.append(limitation)
    for limitation in extra_limitations:
        if limitation not in limitations:
            limitations.append(limitation)

    return AnalysisReport(
        analysis_id=analysis_id,
        status=status,
        generated_at=generated_at,
        evidence_snapshot_version=evidence.snapshot_version,
        input_snapshot=analysis_input,
        readiness=score,
        evidence_confidence=confidence,
        market=_market_section(analysis_input, evidence),
        synthetic_simulation=_simulation_section(customer, reason=simulation_reason),
        agent_review=_agent_review(simulation, reason=simulation_reason),
        finance=finance,
        risks=_risks(analysis_input, finance, score, confidence) + _agent_risks(simulation),
        recommendations=_recommendations(analysis_input, evidence, finance, score),
        evidence=list(evidence.items),
        missing_evidence=list(evidence.missing),
        limitations=limitations,
        warnings=list(warnings),
    )
