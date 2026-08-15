from __future__ import annotations

from app.core.errors import NotFoundError
from app.domain.auth import BusinessRole, IdentityContext
from app.repositories.business import BusinessRepository


async def require_owner_workspace(
    businesses: BusinessRepository, identity: IdentityContext
) -> None:
    """Reject users whose only role anywhere is cashier.

    Analysis and education belong to owners (docs/15). A user with no membership
    at all is still allowed, because running an analysis before opening a
    business is exactly the intended first use. Denial is a 404 so the existence
    of the resource is not disclosed.
    """
    memberships = await businesses.list_for_user(identity)
    if not memberships:
        return
    if any(membership.role == BusinessRole.OWNER.value for _, membership in memberships):
        return
    raise NotFoundError()
