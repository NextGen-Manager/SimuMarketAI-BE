from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import ActorContext, BusinessRole, IdentityContext
from app.persistence.models import BusinessInvite, BusinessProfile, Membership


class BusinessRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(
        self, identity: IdentityContext
    ) -> list[tuple[BusinessProfile, Membership]]:
        rows = await self._session.execute(
            select(BusinessProfile, Membership)
            .join(Membership, Membership.business_id == BusinessProfile.id)
            .where(Membership.user_id == identity.user_id)
            .order_by(BusinessProfile.created_at)
        )
        return list(rows.tuples())

    async def create_for_owner(
        self, identity: IdentityContext, *, name: str, location_name: str
    ) -> tuple[BusinessProfile, Membership]:
        business = BusinessProfile(name=name, location_name=location_name)
        self._session.add(business)
        await self._session.flush()
        membership = Membership(
            user_id=identity.user_id,
            business_id=business.id,
            role=BusinessRole.OWNER.value,
        )
        self._session.add(membership)
        await self._session.flush()
        return business, membership

    async def get_actor(self, identity: IdentityContext, business_id: UUID) -> ActorContext | None:
        membership = await self._session.scalar(
            select(Membership).where(
                Membership.user_id == identity.user_id,
                Membership.business_id == business_id,
            )
        )
        if membership is None:
            return None
        return ActorContext(
            user_id=identity.user_id,
            session_id=identity.session_id,
            business_id=business_id,
            role=BusinessRole(membership.role),
        )

    async def get(self, actor: ActorContext) -> BusinessProfile | None:
        return cast(
            BusinessProfile | None,
            await self._session.scalar(
                select(BusinessProfile).where(BusinessProfile.id == actor.business_id)
            ),
        )

    async def update(
        self, actor: ActorContext, *, name: str, location_name: str
    ) -> BusinessProfile | None:
        business = await self.get(actor)
        if business is None:
            return None
        business.name = name
        business.location_name = location_name
        await self._session.flush()
        return business

    async def create_invite(
        self,
        actor: ActorContext,
        *,
        code_hash: str,
        expires_at: datetime,
    ) -> BusinessInvite:
        invite = BusinessInvite(
            business_id=actor.business_id,
            created_by_user_id=actor.user_id,
            code_hash=code_hash,
            expires_at=expires_at,
        )
        self._session.add(invite)
        await self._session.flush()
        return invite

    async def get_invite_for_owner(
        self, actor: ActorContext, invite_id: UUID
    ) -> BusinessInvite | None:
        return cast(
            BusinessInvite | None,
            await self._session.scalar(
                select(BusinessInvite).where(
                    BusinessInvite.id == invite_id,
                    BusinessInvite.business_id == actor.business_id,
                )
            ),
        )

    async def get_invite_by_hash(self, code_hash: str) -> BusinessInvite | None:
        return cast(
            BusinessInvite | None,
            await self._session.scalar(
                select(BusinessInvite)
                .where(BusinessInvite.code_hash == code_hash)
                .with_for_update()
            ),
        )

    async def redeem_invite(
        self, identity: IdentityContext, invite: BusinessInvite, *, now: datetime
    ) -> Membership:
        membership = Membership(
            user_id=identity.user_id,
            business_id=invite.business_id,
            role=BusinessRole.CASHIER.value,
        )
        invite.redeemed_at = now
        invite.redeemed_by_user_id = identity.user_id
        self._session.add(membership)
        await self._session.flush()
        return membership

    async def revoke_invite(self, invite: BusinessInvite, *, now: datetime) -> None:
        invite.revoked_at = now
        await self._session.flush()

    async def remove_member(self, actor: ActorContext, member_user_id: UUID) -> bool:
        membership = cast(
            Membership | None,
            await self._session.scalar(
                select(Membership).where(
                    Membership.business_id == actor.business_id,
                    Membership.user_id == member_user_id,
                    Membership.role == BusinessRole.CASHIER.value,
                )
            ),
        )
        if membership is None:
            return False
        await self._session.execute(
            delete(Membership).where(
                Membership.id == membership.id,
                Membership.business_id == actor.business_id,
            )
        )
        return True

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
