from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.security import hash_invite_code, new_invite_code
from app.core.time import as_utc
from app.domain.auth import ActorContext, BusinessRole, IdentityContext
from app.repositories.business import BusinessRepository
from app.repositories.identity import IdentityRepository
from app.schemas.business import BusinessRead, InviteRead, InviteStatusRead, MembershipResult
from app.services.audit import audit_event


class BusinessService:
    def __init__(
        self,
        business_repository: BusinessRepository,
        identity_repository: IdentityRepository,
        settings: Settings,
    ) -> None:
        self._businesses = business_repository
        self._identity = identity_repository
        self._settings = settings

    async def list_businesses(self, identity: IdentityContext) -> list[BusinessRead]:
        rows = await self._businesses.list_for_user(identity)
        return [
            BusinessRead(
                id=business.id,
                name=business.name,
                location_name=business.location_name,
                role=membership.role,
            )
            for business, membership in rows
        ]

    async def create_business(
        self, identity: IdentityContext, *, name: str, location_name: str
    ) -> BusinessRead:
        business, _ = await self._businesses.create_for_owner(
            identity,
            name=name.strip(),
            location_name=location_name.strip(),
        )
        self._identity.add_audit_event(
            audit_event(
                actor_user_id=identity.user_id,
                action="business.create",
                resource_type="business",
                resource_id=business.id,
                outcome="success",
            )
        )
        await self._businesses.commit()
        return BusinessRead(
            id=business.id,
            name=business.name,
            location_name=business.location_name,
            role=BusinessRole.OWNER,
        )

    async def actor_for(self, identity: IdentityContext, business_id: UUID) -> ActorContext:
        actor = await self._businesses.get_actor(identity, business_id)
        if actor is None:
            raise NotFoundError()
        return actor

    async def update_business(
        self, actor: ActorContext, *, name: str, location_name: str
    ) -> BusinessRead:
        self._require_owner(actor)
        business = await self._businesses.update(
            actor, name=name.strip(), location_name=location_name.strip()
        )
        if business is None:
            raise NotFoundError()
        self._identity.add_audit_event(
            audit_event(
                actor_user_id=actor.user_id,
                action="business.update",
                resource_type="business",
                resource_id=actor.business_id,
                outcome="success",
            )
        )
        await self._businesses.commit()
        return BusinessRead(
            id=business.id,
            name=business.name,
            location_name=business.location_name,
            role=actor.role,
        )

    async def create_invite(self, actor: ActorContext) -> InviteRead:
        self._require_owner(actor)
        code = new_invite_code()
        expires_at = datetime.now(UTC) + timedelta(days=7)
        invite = await self._businesses.create_invite(
            actor,
            code_hash=hash_invite_code(code, self._settings.jwt_secret),
            expires_at=expires_at,
        )
        self._identity.add_audit_event(
            audit_event(
                actor_user_id=actor.user_id,
                action="business.invite.create",
                resource_type="business_invite",
                resource_id=invite.id,
                outcome="success",
            )
        )
        await self._businesses.commit()
        return InviteRead(
            id=invite.id,
            business_id=invite.business_id,
            code=code,
            expires_at=invite.expires_at,
        )

    async def get_invite(self, actor: ActorContext, invite_id: UUID) -> InviteStatusRead:
        self._require_owner(actor)
        invite = await self._businesses.get_invite_for_owner(actor, invite_id)
        if invite is None:
            raise NotFoundError()
        now = datetime.now(UTC)
        if invite.revoked_at is not None:
            status = "revoked"
        elif invite.redeemed_at is not None:
            status = "redeemed"
        elif as_utc(invite.expires_at) <= now:
            status = "expired"
        else:
            status = "active"
        return InviteStatusRead(
            id=invite.id,
            business_id=invite.business_id,
            expires_at=invite.expires_at,
            created_at=invite.created_at,
            status=status,
        )

    async def redeem_invite(self, identity: IdentityContext, *, code: str) -> MembershipResult:
        invite = await self._businesses.get_invite_by_hash(
            hash_invite_code(code, self._settings.jwt_secret)
        )
        now = datetime.now(UTC)
        if (
            invite is None
            or invite.revoked_at is not None
            or invite.redeemed_at is not None
            or as_utc(invite.expires_at) <= now
        ):
            raise ValidationFailedError("Kode undangan tidak valid atau sudah kedaluwarsa.")
        try:
            membership = await self._businesses.redeem_invite(identity, invite, now=now)
            self._identity.add_audit_event(
                audit_event(
                    actor_user_id=identity.user_id,
                    action="business.invite.redeem",
                    resource_type="business_invite",
                    resource_id=invite.id,
                    outcome="success",
                )
            )
            await self._businesses.commit()
        except IntegrityError as exc:
            await self._businesses.rollback()
            raise ConflictError("Akun sudah memiliki akses ke usaha ini.") from exc
        return MembershipResult(
            business_id=membership.business_id,
            role=BusinessRole.CASHIER,
        )

    async def revoke_invite(self, actor: ActorContext, invite_id: UUID) -> None:
        self._require_owner(actor)
        invite = await self._businesses.get_invite_for_owner(actor, invite_id)
        if invite is None or invite.redeemed_at is not None:
            raise NotFoundError()
        await self._businesses.revoke_invite(invite, now=datetime.now(UTC))
        self._identity.add_audit_event(
            audit_event(
                actor_user_id=actor.user_id,
                action="business.invite.revoke",
                resource_type="business_invite",
                resource_id=invite.id,
                outcome="success",
            )
        )
        await self._businesses.commit()

    async def remove_cashier(self, actor: ActorContext, member_user_id: UUID) -> None:
        self._require_owner(actor)
        if not await self._businesses.remove_member(actor, member_user_id):
            raise NotFoundError()
        self._identity.add_audit_event(
            audit_event(
                actor_user_id=actor.user_id,
                action="business.member.remove",
                resource_type="membership",
                resource_id=member_user_id,
                outcome="success",
            )
        )
        await self._businesses.commit()

    @staticmethod
    def _require_owner(actor: ActorContext) -> None:
        if not actor.is_owner():
            raise NotFoundError()
