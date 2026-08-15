from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.taxonomy import BusinessType

EDUCATION_GATE_RULE_VERSION: Final = "education-gate-v1"


class EducationProgressRead(BaseModel):
    module_id: UUID
    content_version: str
    started_at: datetime
    completed_at: datetime | None
    correct_answers: int
    total_questions: int
    passed: bool


class EducationModuleSummary(BaseModel):
    id: UUID
    slug: str
    title: str
    summary: str
    topic: str
    content_version: str
    estimated_minutes: int
    business_types: list[BusinessType]
    is_required: bool
    reviewed_at: datetime | None
    progress: EducationProgressRead | None


class EducationQuestionRead(BaseModel):
    id: UUID
    position: int
    prompt: str
    options: list[str]


class EducationModuleDetail(EducationModuleSummary):
    body: str | None
    passing_score_percent: int
    questions: list[EducationQuestionRead]


class EducationCompleteRequest(BaseModel):
    # The client echoes the version it studied; a mismatch means the content
    # changed underneath and the attempt is rejected rather than silently kept.
    content_version: str = Field(min_length=1, max_length=40)
    answers: list[int] = Field(min_length=0, max_length=50)


class EducationCompleteResponse(BaseModel):
    module_id: UUID
    content_version: str
    passed: bool
    correct_answers: int
    total_questions: int
    passing_score_percent: int
    completed_at: datetime | None


class EducationPrerequisiteModule(BaseModel):
    id: UUID
    slug: str
    title: str
    content_version: str
    estimated_minutes: int
    completed: bool


class EducationPrerequisites(BaseModel):
    rule_version: Literal["education-gate-v1"] = EDUCATION_GATE_RULE_VERSION
    business_type: BusinessType
    satisfied: bool
    content_available: bool
    required: list[EducationPrerequisiteModule]
    outstanding: list[EducationPrerequisiteModule]
    note: str | None
