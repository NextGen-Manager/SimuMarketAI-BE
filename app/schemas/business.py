from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.auth import BusinessRole


class BusinessWrite(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    location_name: str = Field(min_length=2, max_length=180)


class BusinessRead(BaseModel):
    id: UUID
    name: str
    location_name: str
    role: BusinessRole


class InviteRead(BaseModel):
    id: UUID
    business_id: UUID
    code: str
    expires_at: datetime


class InviteStatusRead(BaseModel):
    id: UUID
    business_id: UUID
    expires_at: datetime
    created_at: datetime
    status: Literal["active", "redeemed", "revoked", "expired"]


class InviteRedeemRequest(BaseModel):
    code: str = Field(min_length=8, max_length=8)


class MembershipResult(BaseModel):
    business_id: UUID
    role: BusinessRole
