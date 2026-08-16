from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response

from app.api.dependencies import BusinessServiceDependency, CurrentIdentity
from app.schemas.business import (
    BusinessRead,
    BusinessWrite,
    InviteRead,
    InviteRedeemRequest,
    InviteStatusRead,
    MembershipResult,
)

router = APIRouter(tags=["businesses"])


@router.get("/businesses", response_model=list[BusinessRead])
async def list_businesses(
    identity: CurrentIdentity,
    service: BusinessServiceDependency,
) -> list[BusinessRead]:
    return await service.list_businesses(identity)


@router.post("/businesses", response_model=BusinessRead, status_code=201)
async def create_business(
    payload: BusinessWrite,
    identity: CurrentIdentity,
    service: BusinessServiceDependency,
) -> BusinessRead:
    return await service.create_business(
        identity,
        name=payload.name,
        location_name=payload.location_name,
    )


@router.put("/businesses/{business_id}", response_model=BusinessRead)
async def update_business(
    business_id: UUID,
    payload: BusinessWrite,
    identity: CurrentIdentity,
    service: BusinessServiceDependency,
) -> BusinessRead:
    actor = await service.actor_for(identity, business_id)
    return await service.update_business(
        actor,
        name=payload.name,
        location_name=payload.location_name,
    )


@router.post("/businesses/{business_id}/invites", response_model=InviteRead, status_code=201)
async def create_invite(
    business_id: UUID,
    identity: CurrentIdentity,
    service: BusinessServiceDependency,
) -> InviteRead:
    actor = await service.actor_for(identity, business_id)
    return await service.create_invite(actor)


@router.post("/invites/redeem", response_model=MembershipResult)
async def redeem_invite(
    payload: InviteRedeemRequest,
    identity: CurrentIdentity,
    service: BusinessServiceDependency,
) -> MembershipResult:
    return await service.redeem_invite(identity, code=payload.code)


@router.get("/businesses/{business_id}/invites/{invite_id}", response_model=InviteStatusRead)
async def get_invite(
    business_id: UUID,
    invite_id: UUID,
    identity: CurrentIdentity,
    service: BusinessServiceDependency,
) -> InviteStatusRead:
    actor = await service.actor_for(identity, business_id)
    return await service.get_invite(actor, invite_id)


@router.delete("/businesses/{business_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(
    business_id: UUID,
    invite_id: UUID,
    identity: CurrentIdentity,
    service: BusinessServiceDependency,
) -> Response:
    actor = await service.actor_for(identity, business_id)
    await service.revoke_invite(actor, invite_id)
    return Response(status_code=204)


@router.delete("/businesses/{business_id}/members/{member_user_id}", status_code=204)
async def remove_member(
    business_id: UUID,
    member_user_id: UUID,
    identity: CurrentIdentity,
    service: BusinessServiceDependency,
) -> Response:
    actor = await service.actor_for(identity, business_id)
    await service.remove_cashier(actor, member_user_id)
    return Response(status_code=204)
