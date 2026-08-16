from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class BusinessRole(StrEnum):
    OWNER = "owner"
    CASHIER = "cashier"


@dataclass(frozen=True, slots=True)
class IdentityContext:
    user_id: UUID
    session_id: UUID


@dataclass(frozen=True, slots=True)
class ActorContext:
    user_id: UUID
    session_id: UUID
    business_id: UUID
    role: BusinessRole

    def is_owner(self) -> bool:
        return self.role is BusinessRole.OWNER
