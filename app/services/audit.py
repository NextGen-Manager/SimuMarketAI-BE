from __future__ import annotations

from uuid import UUID

from app.core.correlation import get_correlation_id
from app.persistence.models import AuditEvent


def audit_event(
    *,
    actor_user_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    outcome: str,
) -> AuditEvent:
    return AuditEvent(
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        correlation_id=UUID(get_correlation_id()),
    )
