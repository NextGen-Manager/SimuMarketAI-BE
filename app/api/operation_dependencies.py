from typing import Annotated

from fastapi import Depends

from app.api.dependencies import CurrentIdentity, DatabaseSession
from app.services.operations import OperationsService


def get_operations_service(
    session: DatabaseSession,
    identity: CurrentIdentity,
) -> OperationsService:
    return OperationsService(session, identity)


OperationsServiceDependency = Annotated[OperationsService, Depends(get_operations_service)]
