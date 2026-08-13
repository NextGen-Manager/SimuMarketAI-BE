from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentRole(StrEnum):
    MARKET_ANALYST = "market_analyst"
    CUSTOMER_PERSONA = "customer_persona"
    FINANCE = "finance"
    REPORT = "report"


class AgentArtifact(StrictModel):
    agent_id: str
    role: AgentRole
    assessment: str = Field(min_length=1)
    recommendations: list[str] = Field(min_length=1, max_length=5)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]


class StructuredBallot(StrictModel):
    agent_id: str
    decision: Literal["proceed", "mitigate", "reconsider"]
    primary_reason: str = Field(min_length=1)
    objection: str = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]


class FinanceInput(StrictModel):
    fixed_cost_idr: int = Field(ge=0)
    selling_price_idr: int = Field(gt=0)
    variable_cost_idr: int = Field(ge=0)


class FinanceResult(StrictModel):
    artifact_id: str
    contribution_margin_idr: int
    break_even_units: int | None
    status: Literal["defined", "undefined"]
    warning: str | None
    source: Literal["deterministic-finance-spike-v1"]


class StageMetric(StrictModel):
    stage: AgentRole
    wall_clock_ms: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    schema_validation_failures: int = Field(ge=0)


class RunLimits(StrictModel):
    max_total_tokens: int = Field(gt=0)
    max_output_tokens_per_stage: int = Field(gt=0)
    stage_timeout_seconds: int = Field(gt=0)


class RunManifest(StrictModel):
    run_id: str
    status: Literal["completed", "partial", "failed"]
    model_id: str
    provider: Literal["gemini"]
    oasis_version: Literal["0.2.5"]
    camel_ai_version: Literal["0.2.78"]
    prompt_version: Literal["oasis-spike-v1"]
    cohort_version: Literal["four-role-cohort-v1"]
    seed: int
    limits: RunLimits
    trace_path: str
    finance: FinanceResult
    ballots: list[StructuredBallot]
    artifacts: list[AgentArtifact]
    metrics: list[StageMetric]
