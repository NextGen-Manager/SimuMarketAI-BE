"""Builds the only payload allowed to leave the process toward a provider.

Two rules from docs/07 are enforced here rather than trusted to callers.

First, the payload is an allowlist. `SimulationRequest` has no field able to
carry an email, a phone number, a customer name, or raw receipt text, and this
module is the single place that constructs one. Identifiers are replaced with
salted digests so a provider never sees a real `user_id` or `analysis_id`.

Second, user free text is data, not instruction. Business names and value
propositions are neutralised before they are wrapped in a delimiter by the
prompt builder: control characters go, length is capped, and anything that
looks like a contact detail is redacted even though the field should never
have held one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from uuid import UUID

from app.domain.agents import (
    CohortManifest,
    ConceptCard,
    EvidenceDigest,
    FinanceBounds,
    SimulationBudget,
    SimulationRequest,
)
from app.domain.evidence import EvidenceSnapshot

MAX_FREE_TEXT_LENGTH = 400
REDACTED = "[dihapus]"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Indonesian phone numbers appear as +62..., 62..., or 08... with optional
# separators. The pattern is deliberately wide: a false positive costs a few
# redacted characters, a false negative leaks a contact detail.
_PHONE = re.compile(r"(?:\+?62|0)[\s.-]?\d(?:[\s.-]?\d){7,13}")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
# C0/C1 control characters plus the bidirectional and zero-width formatting
# marks that can hide an injected instruction from a human reviewer.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]")


def pseudonymize(value: UUID | str, *, salt: str) -> str:
    """Stable, non-reversible reference for one run.

    The salt is the deployment's JWT secret, so two environments never produce
    the same reference for the same run and a leaked digest cannot be joined
    back to a user.
    """
    digest = hashlib.sha256(f"{salt}:{value}".encode())
    return digest.hexdigest()[:32]


def neutralize(text: str, *, limit: int = MAX_FREE_TEXT_LENGTH) -> str:
    """Strip anything that turns user text into an instruction or a contact."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _CONTROL.sub(" ", normalized)
    normalized = _EMAIL.sub(REDACTED, normalized)
    normalized = _URL.sub(REDACTED, normalized)
    normalized = _PHONE.sub(REDACTED, normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if len(normalized) > limit:
        normalized = normalized[:limit].rstrip()
    return normalized


def _confidence_percent(value: float) -> int:
    return max(0, min(100, round(value * 100)))


def build_simulation_request(
    *,
    analysis_id: UUID,
    correlation_id: UUID,
    salt: str,
    business_type: str,
    concept_name: str,
    area_id: str,
    analysis_radius_m: int,
    price_idr: int,
    variable_cost_per_unit_idr: int,
    channels: list[str],
    value_proposition: str,
    evidence: EvidenceSnapshot,
    finance_bounds: FinanceBounds,
    finance_rule_version: str,
    budget: SimulationBudget,
    cohort: CohortManifest,
    seed: int,
) -> SimulationRequest:
    digests = [
        EvidenceDigest(
            metric=record.metric,
            value=record.value,
            unit=record.unit,
            source=record.source,
            observed_at=record.observed_at,
            confidence_percent=_confidence_percent(record.quality.source_quality),
        )
        for record in evidence.items
    ]
    return SimulationRequest(
        analysis_ref=pseudonymize(analysis_id, salt=salt),
        correlation_ref=pseudonymize(correlation_id, salt=salt),
        concept=ConceptCard(
            business_type=business_type,
            concept_name=neutralize(concept_name, limit=120),
            area_id=neutralize(area_id, limit=80),
            analysis_radius_m=analysis_radius_m,
            price_idr=price_idr,
            variable_cost_per_unit_idr=variable_cost_per_unit_idr,
            channels=list(channels),
            value_proposition=neutralize(value_proposition),
        ),
        evidence=digests,
        missing_evidence_metrics=evidence.missing_metrics(),
        finance_bounds=finance_bounds,
        finance_rule_version=finance_rule_version,
        budget=budget,
        cohort=cohort,
        seed=seed,
    )
