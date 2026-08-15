"""Agent boundary contract: typed artifacts, run manifest, and adapter protocol.

This module lives in `app/domain` so that `app/engines` can consume simulation
artifacts without importing `app/integrations`. ADR-001 puts the deterministic
boundary here in code: an agent may judge, critique, and narrate, but every
authoritative number is produced by an engine and reaches the agent as data.

Nothing in this module imports FastAPI, SQLAlchemy, Celery, or a provider SDK.
It also deliberately avoids importing `app.schemas`, because `app.schemas`
imports this package and a cycle would force the boundary to be re-declared in
two places.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentRole = Literal["market_analyst", "customer_persona", "finance", "report"]

AGENT_ROLES: tuple[AgentRole, ...] = (
    "market_analyst",
    "customer_persona",
    "finance",
    "report",
)

ArtifactType = Literal[
    "MarketAssessment",
    "CustomerSimulationResult",
    "FinanceReview",
    "ReportNarrative",
]

ValidationStatus = Literal["valid", "rejected"]
Confidence = Literal["low", "medium", "high"]
Severity = Literal["low", "medium", "high"]

SIMULATION_LABEL: Final = "respons sintetis"

PERSONA_ARCHETYPES: tuple[str, ...] = (
    "budget_driven",
    "convenience_driven",
    "quality_driven",
    "social_family_driven",
)

ARCHETYPE_LABELS: dict[str, str] = {
    "budget_driven": "Sensitif harga",
    "convenience_driven": "Mengutamakan kepraktisan",
    "quality_driven": "Mengutamakan kualitas",
    "social_family_driven": "Pembeli sosial dan keluarga",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------- errors


class OasisError(RuntimeError):
    """Base for every OASIS failure that the pipeline is allowed to absorb.

    `failure_code` is what reaches persistence and the report. It never carries
    a provider response, a prompt, or a stack trace.
    """

    failure_code = "oasis_failed"
    reason = "Simulasi agent tidak dapat diselesaikan."
    retryable = False


class OasisUnavailableError(OasisError):
    failure_code = "oasis_unavailable"
    reason = "Integrasi simulasi agent belum aktif pada lingkungan ini."


class OasisTimeoutError(OasisError):
    failure_code = "oasis_timeout"
    reason = "Simulasi agent melewati batas waktu yang ditetapkan."
    retryable = True


class OasisBudgetExceededError(OasisError):
    failure_code = "oasis_budget_exceeded"
    reason = "Simulasi agent dihentikan karena melewati batas token atau round."


class OasisSchemaError(OasisError):
    failure_code = "oasis_schema_invalid"
    reason = "Keluaran agent tidak sesuai schema sehingga tidak dapat dipakai."


# ------------------------------------------------------------ deterministic tool


class FinanceToolCall(StrictModel):
    """Result of the deterministic finance calculator, as an agent sees it.

    The agent receives `outputs` as read-only data. `tool_call_id` is what the
    Finance council must cite; a critique without one cannot be traced back to
    a real calculation and is rejected.
    """

    tool_call_id: str
    rule_version: str
    assumptions: dict[str, int]
    outputs: dict[str, int | None]


FinanceTool = Callable[[Mapping[str, int]], FinanceToolCall]


# ------------------------------------------------------------------ artifacts


class MarketObservation(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    stance: Literal["opportunity", "risk", "uncertainty"]
    claim: str = Field(min_length=1, max_length=600)
    evidence_metrics: list[str] = Field(default_factory=list)
    confidence: Confidence


class MarketAssessment(StrictModel):
    artifact_type: Literal["MarketAssessment"] = "MarketAssessment"
    schema_version: Literal["market-assessment-v1"] = "market-assessment-v1"
    headline: str = Field(min_length=1, max_length=300)
    observations: list[MarketObservation] = Field(min_length=1, max_length=12)
    evidence_gaps: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_support(self) -> MarketAssessment:
        # An opportunity or a risk is a claim about the market and must point at
        # evidence. Only an explicit `uncertainty` may stand without one.
        for observation in self.observations:
            if observation.stance != "uncertainty" and not observation.evidence_metrics:
                raise ValueError(f"observasi {observation.id} tidak menunjuk evidence")
        return self


class ObjectionTally(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    count: int = Field(ge=0)


class SegmentTally(StrictModel):
    archetype: str = Field(min_length=1, max_length=64)
    persona_count: int = Field(ge=0)
    purchase_intent_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> SegmentTally:
        if self.purchase_intent_count > self.persona_count:
            raise ValueError("purchase_intent_count melebihi persona_count")
        return self


class PriceBand(StrictModel):
    """Synthetic acceptable-price band. Integer rupiah, never a market fact."""

    min_idr: int = Field(ge=0)
    max_idr: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> PriceBand:
        if self.min_idr > self.max_idr:
            raise ValueError("min_idr melebihi max_idr")
        return self


class SyntheticQuote(StrictModel):
    agent_id: str = Field(min_length=1, max_length=64)
    archetype: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=400)
    label: Literal["respons sintetis"] = SIMULATION_LABEL


class CustomerSimulationResult(StrictModel):
    artifact_type: Literal["CustomerSimulationResult"] = "CustomerSimulationResult"
    schema_version: Literal["customer-simulation-result-v1"] = "customer-simulation-result-v1"
    cohort_version: str = Field(min_length=1, max_length=80)
    cohort_size: int = Field(ge=1)
    rounds: int = Field(ge=1)
    activated_persona_count: int = Field(ge=0)
    purchase_intent_count: int = Field(ge=0)
    positive_reaction_count: int = Field(ge=0)
    opinion_shift_count: int = Field(ge=0)
    segments: list[SegmentTally] = Field(default_factory=list)
    objections: list[ObjectionTally] = Field(default_factory=list)
    acceptable_price_band: PriceBand | None = None
    quotes: list[SyntheticQuote] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> CustomerSimulationResult:
        if self.activated_persona_count > self.cohort_size:
            raise ValueError("activated_persona_count melebihi cohort_size")
        if self.purchase_intent_count > self.activated_persona_count:
            raise ValueError("purchase_intent_count melebihi activated_persona_count")
        if self.positive_reaction_count > self.activated_persona_count:
            raise ValueError("positive_reaction_count melebihi activated_persona_count")
        if sum(segment.persona_count for segment in self.segments) > self.cohort_size:
            raise ValueError("jumlah persona per segmen melebihi cohort_size")
        return self


class FinanceCritique(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    assumption: str = Field(min_length=1, max_length=300)
    concern: str = Field(min_length=1, max_length=600)
    severity: Severity
    tool_call_ids: list[str] = Field(min_length=1)


class FinanceReview(StrictModel):
    artifact_type: Literal["FinanceReview"] = "FinanceReview"
    schema_version: Literal["finance-review-v1"] = "finance-review-v1"
    finance_rule_version: str = Field(min_length=1, max_length=80)
    tool_calls: list[FinanceToolCall] = Field(min_length=1)
    critiques: list[FinanceCritique] = Field(default_factory=list, max_length=12)
    fragile_assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_known_tool_calls(self) -> FinanceReview:
        known = {call.tool_call_id for call in self.tool_calls}
        for critique in self.critiques:
            unknown = [item for item in critique.tool_call_ids if item not in known]
            if unknown:
                raise ValueError(f"kritik {critique.id} menunjuk tool call yang tidak dikenal")
        return self


class NarrativeSection(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2_000)
    source_artifact_types: list[ArtifactType] = Field(min_length=1)


class ReportNarrative(StrictModel):
    artifact_type: Literal["ReportNarrative"] = "ReportNarrative"
    schema_version: Literal["report-narrative-v1"] = "report-narrative-v1"
    sections: list[NarrativeSection] = Field(min_length=1, max_length=8)
    red_team_findings: list[str] = Field(default_factory=list)
    removed_unsupported_claims: list[str] = Field(default_factory=list)


AgentArtifactPayload = MarketAssessment | CustomerSimulationResult | FinanceReview | ReportNarrative


# ------------------------------------------------------------------- manifests


class SimulationBudget(StrictModel):
    """Hard limits for one run. Every field is enforced, none is advisory."""

    persona_count: int = Field(ge=4, le=24)
    round_limit: int = Field(ge=1, le=8)
    token_budget: int = Field(gt=0)
    max_output_tokens_per_stage: int = Field(gt=0)
    concurrency_limit: int = Field(ge=1, le=16)
    wall_clock_seconds: int = Field(gt=0)
    retry_limit: int = Field(ge=0, le=3)


class CohortManifest(StrictModel):
    cohort_version: str = Field(min_length=1, max_length=80)
    size: int = Field(ge=4, le=24)
    allocation: dict[str, int]
    representativeness: Literal["exploratory_unweighted"] = "exploratory_unweighted"
    source_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_allocation(self) -> CohortManifest:
        if sum(self.allocation.values()) != self.size:
            raise ValueError("alokasi archetype tidak berjumlah sama dengan size")
        return self


class TraceArtifact(StrictModel):
    """Pointer to the OASIS trace, never the trace content itself."""

    object_key: str = Field(min_length=1, max_length=500)
    checksum: str | None = None
    byte_size: int | None = Field(default=None, ge=0)
    retention_days: int = Field(ge=1)
    access_scope: Literal["owner_only"] = "owner_only"


class RunManifest(StrictModel):
    """Reproducibility manifest from docs/04. Holds no secret, ever."""

    environment_id: str = Field(min_length=1, max_length=120)
    adapter_id: str = Field(min_length=1, max_length=60)
    provider: str = Field(min_length=1, max_length=60)
    model_id: str = Field(min_length=1, max_length=120)
    oasis_version: str = Field(min_length=1, max_length=40)
    camel_version: str = Field(min_length=1, max_length=40)
    prompt_version: str = Field(min_length=1, max_length=80)
    cohort: CohortManifest
    seed: int
    budget: SimulationBudget
    trace: TraceArtifact
    evidence_snapshot_version: str = Field(min_length=1, max_length=80)
    input_snapshot_hash: str = Field(min_length=1, max_length=64)
    created_at: datetime


# --------------------------------------------------------------------- request


class ConceptCard(StrictModel):
    """The stimulus every persona sees. Untrusted user text, already delimited."""

    business_type: str
    concept_name: str
    area_id: str
    analysis_radius_m: int = Field(ge=0)
    price_idr: int = Field(ge=0)
    variable_cost_per_unit_idr: int = Field(ge=0)
    channels: list[str] = Field(default_factory=list)
    value_proposition: str = ""


class EvidenceDigest(StrictModel):
    """Evidence as the agent may see it: value plus provenance, nothing more."""

    metric: str
    value: int
    unit: str
    source: str
    observed_at: datetime
    confidence_percent: int = Field(ge=0, le=100)


class FinanceBounds(StrictModel):
    """The range the Finance council is allowed to choose assumptions from.

    docs/03 lets Conservative, Base, and Optimistic pick different bounds, but
    only bounds the user's own input permits. Anything outside this range is
    clamped by the deterministic tool, so an agent cannot widen its own mandate.
    """

    volume_units_day_min: int = Field(ge=0)
    volume_units_day_base: int = Field(ge=0)
    volume_units_day_max: int = Field(ge=0)
    variable_cost_per_unit_idr: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> FinanceBounds:
        if not (
            self.volume_units_day_min <= self.volume_units_day_base <= self.volume_units_day_max
        ):
            raise ValueError("volume bounds harus memenuhi min <= base <= max")
        return self


class SimulationRequest(StrictModel):
    """The complete payload allowed to leave the process toward a provider.

    Identifiers are pseudonymous by construction: `analysis_ref` is a hash, and
    there is no field able to carry an email, a phone number, a customer name,
    or raw receipt text.
    """

    analysis_ref: str = Field(min_length=8, max_length=64)
    correlation_ref: str = Field(min_length=8, max_length=64)
    concept: ConceptCard
    evidence: list[EvidenceDigest] = Field(default_factory=list)
    missing_evidence_metrics: list[str] = Field(default_factory=list)
    finance_bounds: FinanceBounds
    finance_rule_version: str
    budget: SimulationBudget
    cohort: CohortManifest
    seed: int


# ---------------------------------------------------------------------- result


class AgentInstanceRecord(StrictModel):
    agent_id: str = Field(min_length=1, max_length=64)
    role: AgentRole
    archetype: str | None = None
    profile_version: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=120)
    allowed_actions: list[str] = Field(default_factory=list)
    activation_order: int = Field(ge=0)
    total_tokens: int = Field(ge=0, default=0)
    duration_ms: int = Field(ge=0, default=0)
    outcome: Literal["completed", "failed", "skipped"]


class RoundRecord(StrictModel):
    """One round of the persona protocol, as it was actually executed."""

    index: int = Field(ge=0)
    kind: Literal["baseline_interview", "exposure", "interaction", "intervention", "final_ballot"]
    activated_agent_ids: list[str] = Field(default_factory=list)
    actions: dict[str, str] = Field(default_factory=dict)


class AgentRunRecord(StrictModel):
    role: AgentRole
    status: Literal["completed", "failed", "skipped"]
    instances: list[AgentInstanceRecord] = Field(default_factory=list)
    total_tokens: int = Field(ge=0, default=0)
    duration_ms: int = Field(ge=0, default=0)
    schema_failures: int = Field(ge=0, default=0)
    failure_code: str | None = None
    artifact: AgentArtifactPayload | None = None
    validation_status: ValidationStatus = "valid"
    # Upstream artifacts this council was actually given, reported by the
    # adapter that assembled the prompt. docs/04 makes every arrow in the
    # protocol an artifact ID in the manifest; recording an arrow the council
    # never received would make provenance look sound while being false.
    consumed_artifact_types: list[ArtifactType] = Field(default_factory=list)
    rounds: list[RoundRecord] = Field(default_factory=list)


class SimulationOutcome(StrictModel):
    status: Literal["completed", "partial", "failed"]
    manifest: RunManifest
    agent_runs: list[AgentRunRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    failure_code: str | None = None

    def artifact(self, artifact_type: ArtifactType) -> AgentArtifactPayload | None:
        for run in self.agent_runs:
            if (
                run.artifact is not None
                and run.artifact.artifact_type == artifact_type
                and run.validation_status == "valid"
            ):
                return run.artifact
        return None

    @property
    def market_assessment(self) -> MarketAssessment | None:
        found = self.artifact("MarketAssessment")
        return found if isinstance(found, MarketAssessment) else None

    @property
    def customer_simulation(self) -> CustomerSimulationResult | None:
        found = self.artifact("CustomerSimulationResult")
        return found if isinstance(found, CustomerSimulationResult) else None

    @property
    def finance_review(self) -> FinanceReview | None:
        found = self.artifact("FinanceReview")
        return found if isinstance(found, FinanceReview) else None

    @property
    def report_narrative(self) -> ReportNarrative | None:
        found = self.artifact("ReportNarrative")
        return found if isinstance(found, ReportNarrative) else None

    @property
    def total_tokens(self) -> int:
        return sum(run.total_tokens for run in self.agent_runs)


# -------------------------------------------------------------------- protocol


@runtime_checkable
class OasisAdapter(Protocol):
    """Runs the four councils for one analysis.

    Implementations never persist anything and never read the database. They
    receive a sanitised request, call the deterministic finance tool for any
    number they need, and return typed artifacts plus a manifest.
    """

    adapter_id: str
    is_fake: bool

    async def simulate(
        self,
        request: SimulationRequest,
        *,
        finance_tool: FinanceTool,
        manifest: RunManifest,
    ) -> SimulationOutcome: ...


def build_cohort_manifest(*, cohort_version: str, size: int) -> CohortManifest:
    """Split a cohort as evenly as docs/04 requires across four archetypes.

    Remainders go to the earliest archetypes in a fixed order so that the same
    size always produces the same allocation; a run must be reproducible from
    its manifest alone.
    """
    base, remainder = divmod(size, len(PERSONA_ARCHETYPES))
    allocation = {
        archetype: base + (1 if index < remainder else 0)
        for index, archetype in enumerate(PERSONA_ARCHETYPES)
    }
    return CohortManifest(
        cohort_version=cohort_version,
        size=size,
        allocation=allocation,
        source_notes=["Hipotesis persona belum dikalibrasi melalui wawancara narasumber manusia."],
    )


def unsupported_evidence_metrics(
    assessment: MarketAssessment, available: Sequence[str]
) -> list[str]:
    """Metrics an assessment cites that the evidence snapshot does not contain.

    A non-empty result means the agent invented evidence, which docs/03 forbids
    outright, so the caller rejects the artifact rather than repairing it.
    """
    known = set(available)
    cited: list[str] = []
    for observation in assessment.observations:
        for metric in observation.evidence_metrics:
            if metric not in known and metric not in cited:
                cited.append(metric)
    return cited
