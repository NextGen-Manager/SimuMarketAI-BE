from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.api.analysis_dependencies import EducationServiceDependency
from app.domain.taxonomy import BusinessType
from app.schemas.education import (
    EducationCompleteRequest,
    EducationCompleteResponse,
    EducationModuleDetail,
    EducationModuleSummary,
    EducationPrerequisites,
)

router = APIRouter(prefix="/education", tags=["education"])


@router.get("/modules", response_model=list[EducationModuleSummary])
async def list_modules(
    service: EducationServiceDependency,
    business_type: BusinessType | None = None,
) -> list[EducationModuleSummary]:
    return await service.list_modules(business_type)


@router.get("/prerequisites", response_model=EducationPrerequisites)
async def prerequisites(
    business_type: BusinessType,
    service: EducationServiceDependency,
) -> EducationPrerequisites:
    return await service.prerequisites(business_type)


@router.get("/modules/{module_id}", response_model=EducationModuleDetail)
async def get_module(
    module_id: UUID,
    service: EducationServiceDependency,
) -> EducationModuleDetail:
    return await service.get_module(module_id)


@router.post("/modules/{module_id}/complete", response_model=EducationCompleteResponse)
async def complete_module(
    module_id: UUID,
    payload: EducationCompleteRequest,
    service: EducationServiceDependency,
) -> EducationCompleteResponse:
    return await service.complete_module(module_id, payload)
