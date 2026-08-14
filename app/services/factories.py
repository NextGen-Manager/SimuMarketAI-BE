from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.repositories.business import BusinessRepository
from app.repositories.identity import IdentityRepository
from app.services.auth import AuthService
from app.services.business import BusinessService


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
