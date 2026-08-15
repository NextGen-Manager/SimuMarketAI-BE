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
from uuid import UUID

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
    AnalysisInput,
    AnalysisReport,
    AnalysisWarning,
    EvidenceConfidence,
    FinanceResult,
    MarketSection,
    ReportRecommendation,
    ReportRisk,
    ScoreResult,
    SyntheticSimulation,
)

BASE_LIMITATIONS: tuple[str, ...] = (
    "Bobot Launch Readiness Score berstatus hipotesis pada rule set "
    "lrs-v0.2-unvalidated dan belum melewati expert review maupun kalibrasi.",
    "Dimensi kesiapan operasional dinilai dari data yang dilaporkan pengguna, "
    "bukan dari pengukuran lapangan.",
    "Bobot Evidence Confidence masih berupa hipotesis pada formula "
    "evidence-confidence-v0.1-unvalidated dan belum dikalibrasi.",
    "Simulasi persona sintetis belum dijalankan sehingga sinyal permintaan dari "
    "agent tidak tersedia pada laporan ini.",
    ROUNDING_NOTE,
)


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
) -> AnalysisReport:
    limitations = list(BASE_LIMITATIONS)
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
        synthetic_simulation=SyntheticSimulation(
            status="unavailable",
            reason=SKIP_REASON_SIMULATING,
            cohort_size=None,
            metrics={},
            limitations=[
                "Tidak ada kutipan persona pada laporan ini karena simulasi belum dijalankan."
            ],
        ),
        finance=finance,
        risks=_risks(analysis_input, finance, score, confidence),
        recommendations=_recommendations(analysis_input, evidence, finance, score),
        evidence=list(evidence.items),
        missing_evidence=list(evidence.missing),
        limitations=limitations,
        warnings=list(warnings),
    )
