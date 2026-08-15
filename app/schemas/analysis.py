from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.analysis_state import AnalysisStage, AnalysisStatus
from app.domain.evidence import EvidenceRecord, MissingEvidence
from app.domain.taxonomy import BusinessType, SalesChannel

FINANCE_RULE_VERSION: Final = "finance-v1"
SCORING_RULE_VERSION: Final = "lrs-v0.2-unvalidated"
REPORT_VERSION: Final = "report-v1"
EVIDENCE_CONFIDENCE_VERSION: Final = "evidence-confidence-v0.1-unvalidated"

DSS_DISCLAIMER: Final = "Hasil adalah alat bantu keputusan, bukan jaminan keberhasilan usaha."

ScenarioName = Literal["conservative", "base", "optimistic"]
DimensionKey = Literal[
    "market_saturation",
    "demand_potential",
    "price_positioning",
    "operational_readiness",
]


# --------------------------------------------------------------------------- input


class VolumeRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    min: int = Field(ge=0, le=100_000)
    base: int = Field(ge=0, le=100_000)
    max: int = Field(ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_order(self) -> VolumeRange:
        if not self.min <= self.base <= self.max:
            raise ValueError("volume_units_day harus memenuhi min <= base <= max")
        return self


class AnalysisLocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    area_id: str = Field(min_length=2, max_length=80)
    area_name: str | None = Field(default=None, max_length=180)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    analysis_radius_m: int = Field(ge=100, le=10_000)


class AnalysisPricing(BaseModel):
    model_config = ConfigDict(frozen=True)

    average_selling_price_idr: int = Field(ge=0, le=100_000_000)
    variable_cost_per_unit_idr: int = Field(ge=0, le=100_000_000)


class AnalysisOperations(BaseModel):
    model_config = ConfigDict(frozen=True)

    initial_investment_idr: int = Field(ge=0, le=100_000_000_000)
    fixed_cost_month_idr: int = Field(ge=0, le=100_000_000_000)
    operating_days_month: int = Field(ge=1, le=31)
    capacity_units_day: int = Field(ge=0, le=100_000)
    volume_units_day: VolumeRange


class AnalysisInput(BaseModel):
    """Validated analysis request. Frozen once a run starts."""

    model_config = ConfigDict(frozen=True)

    business_type: BusinessType
    concept_name: str = Field(min_length=2, max_length=120)
    location: AnalysisLocation
    pricing: AnalysisPricing
    operations: AnalysisOperations
    channels: list[SalesChannel] = Field(min_length=1, max_length=4)
    value_proposition: str = Field(min_length=0, max_length=600, default="")


class AnalysisCreate(AnalysisInput):
    """Create payload. Identical to the snapshot so nothing is added later."""


# --------------------------------------------------------------------------- finance


class FinanceWarning(BaseModel):
    code: str
    message: str
    scenario: ScenarioName | None = None


class FinanceScenario(BaseModel):
    name: ScenarioName
    label: str
    volume_units_day: int
    monthly_units: int
    monthly_revenue_idr: int
    monthly_operating_profit_idr: int
    payback_months: int | None
    exceeds_capacity: bool


class FinanceResult(BaseModel):
    currency: Literal["IDR"] = "IDR"
    rule_version: Literal["finance-v1"] = FINANCE_RULE_VERSION
    contribution_margin_per_unit_idr: int
    contribution_margin_ratio_bps: int | None
    bep_units_month: int | None
    bep_units_day: int | None
    bep_revenue_month_idr: int | None
    runway_months: int | None
    scenarios: list[FinanceScenario]
    assumptions_included: list[str]
    assumptions_excluded: list[str]
    warnings: list[FinanceWarning]

    def scenario(self, name: ScenarioName) -> FinanceScenario | None:
        for item in self.scenarios:
            if item.name == name:
                return item
        return None


# --------------------------------------------------------------------------- scoring


class DimensionScore(BaseModel):
    key: DimensionKey
    label: str
    weight_percent: int
    status: Literal["scored", "not_scorable"]
    score: int | None
    applied_rules: list[str]
    rationale: str
    missing_inputs: list[str]
    evidence_metrics: list[str]


class ScoreResult(BaseModel):
    rule_version: Literal["lrs-v0.2-unvalidated"] = SCORING_RULE_VERSION
    validation_status: Literal["unvalidated"] = "unvalidated"
    status: Literal["available", "unavailable"]
    score: int | None
    interpretation: str | None
    interpretation_label: str | None
    dimensions: list[DimensionScore]
    missing_dimensions: list[DimensionKey]


# --------------------------------------------------------------------------- report


class EvidenceConfidence(BaseModel):
    formula_version: Literal["evidence-confidence-v0.1-unvalidated"] = EVIDENCE_CONFIDENCE_VERSION
    score: float | None
    label: Literal["tinggi", "sedang", "rendah", "tidak_tersedia"]
    missing: list[str]


class SyntheticSimulation(BaseModel):
    status: Literal["unavailable", "experimental"]
    reason: str | None
    cohort_size: int | None
    metrics: dict[str, int] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class MarketSection(BaseModel):
    area_id: str
    area_name: str | None
    analysis_radius_m: int
    category_mapping_version: str
    competitor_count: int | None
    population_count: int | None
    comparable_price_median_idr: int | None
    comparable_price_sample_size: int | None
    notes: list[str]


class AnalysisWarning(BaseModel):
    code: str
    stage: AnalysisStage | None = None
    message: str


class ReportRisk(BaseModel):
    id: str
    severity: Literal["tinggi", "sedang", "rendah"]
    title: str
    detail: str
    source: str


class ReportRecommendation(BaseModel):
    id: str
    priority: Literal["tinggi", "sedang", "rendah"]
    title: str
    rationale: str
    source: str


class AnalysisReport(BaseModel):
    analysis_id: UUID
    report_version: Literal["report-v1"] = REPORT_VERSION
    status: AnalysisStatus
    generated_at: datetime
    rule_version: Literal["lrs-v0.2-unvalidated"] = SCORING_RULE_VERSION
    evidence_snapshot_version: str
    input_snapshot: AnalysisInput
    readiness: ScoreResult
    evidence_confidence: EvidenceConfidence
    market: MarketSection
    synthetic_simulation: SyntheticSimulation
    finance: FinanceResult
    risks: list[ReportRisk]
    recommendations: list[ReportRecommendation]
    evidence: list[EvidenceRecord]
    missing_evidence: list[MissingEvidence]
    limitations: list[str]
    warnings: list[AnalysisWarning]
    disclaimer: Literal["Hasil adalah alat bantu keputusan, bukan jaminan keberhasilan usaha."] = (
        DSS_DISCLAIMER
    )


# --------------------------------------------------------------------------- api views


class AnalysisAccepted(BaseModel):
    analysis_id: UUID
    status: AnalysisStatus
    created_at: datetime
    status_url: str
    events_url: str


class AnalysisProgress(BaseModel):
    completed_stages: list[AnalysisStage]
    skipped_stages: list[AnalysisStage]
    current_stage: AnalysisStage
    message: str
    percent: int = Field(ge=0, le=100)


class AnalysisRead(BaseModel):
    analysis_id: UUID
    status: AnalysisStatus
    concept_name: str
    area_name: str
    business_type: BusinessType
    score: int | None
    interpretation: str | None
    rule_version: str
    evidence_snapshot_version: str
    correlation_id: UUID
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None
    progress: AnalysisProgress
    warnings: list[AnalysisWarning]


class AnalysisListItem(BaseModel):
    analysis_id: UUID
    status: AnalysisStatus
    concept_name: str
    area_name: str
    business_type: BusinessType
    score: int | None
    interpretation: str | None
    rule_version: str
    created_at: datetime
