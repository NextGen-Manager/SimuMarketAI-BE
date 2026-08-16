from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.auth import IdentityContext
from app.repositories.business import BusinessRepository
from app.repositories.education import EducationRepository
from app.repositories.identity import IdentityRepository
from app.services.analysis import AnalysisService
from app.services.analysis_queue import AnalysisDispatcher
from app.services.auth import AuthService
from app.services.business import BusinessService
from app.services.education import EducationService


def build_auth_service(session: AsyncSession, settings: Settings) -> AuthService:
    return AuthService(
        IdentityRepository(session),
        BusinessRepository(session),
        settings,
    )


def build_business_service(session: AsyncSession, settings: Settings) -> BusinessService:
    return BusinessService(
        BusinessRepository(session),
        IdentityRepository(session),
        settings,
    )


def build_education_service(session: AsyncSession, identity: IdentityContext) -> EducationService:
    return EducationService(
        EducationRepository(session, identity),
        BusinessRepository(session),
        identity,
    )


def build_analysis_service(
    session: AsyncSession,
    identity: IdentityContext,
    settings: Settings,
    dispatcher: AnalysisDispatcher | None = None,
) -> AnalysisService:
    return AnalysisService(session, identity, settings, dispatcher)
