from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domain.auth import BusinessRole


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=10, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    display_name: str
    created_at: datetime


class MembershipRead(BaseModel):
    business_id: UUID
    business_name: str
    location_name: str
    role: BusinessRole


class SessionResponse(BaseModel):
    user: UserRead
    memberships: list[MembershipRead]


class MessageResponse(BaseModel):
    message: str
