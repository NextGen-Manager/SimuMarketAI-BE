from typing import Annotated

from fastapi import Depends

from app.api.dependencies import AppSettings, CurrentIdentity, DatabaseSession
from app.services.analysis import AnalysisService
from app.services.education import EducationService
from app.services.factories import build_analysis_service, build_education_service


def get_education_service(
    session: DatabaseSession,
    identity: CurrentIdentity,
) -> EducationService:
    return build_education_service(session, identity)


def get_analysis_service(
    session: DatabaseSession,
    identity: CurrentIdentity,
    settings: AppSettings,
) -> AnalysisService:
    return build_analysis_service(session, identity, settings)


EducationServiceDependency = Annotated[EducationService, Depends(get_education_service)]
AnalysisServiceDependency = Annotated[AnalysisService, Depends(get_analysis_service)]
