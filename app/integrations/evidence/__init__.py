from __future__ import annotations

from app.core.config import Settings
from app.domain.evidence import EvidenceProvider
from app.integrations.evidence.unavailable import UnavailableEvidenceProvider

# Environments where a fixture must never be able to answer a real request.
PROTECTED_ENVIRONMENTS = frozenset({"staging", "production"})


class FixtureProviderNotAllowedError(RuntimeError):
    """Raised when a fixture provider is wired into a protected environment."""


def select_evidence_provider(
    settings: Settings,
    provider: EvidenceProvider | None = None,
) -> EvidenceProvider:
    """Return the provider for this run.

    Tests inject their own provider. The guard below is what keeps a fixture
    from ever becoming the source of a production number, which would turn
    invented data into apparent fact.
    """
    if provider is None:
        return UnavailableEvidenceProvider()
    if provider.is_fixture and settings.environment in PROTECTED_ENVIRONMENTS:
        raise FixtureProviderNotAllowedError(
            "Fixture evidence provider is not allowed outside development and test."
        )
    return provider


__all__ = [
    "FixtureProviderNotAllowedError",
    "UnavailableEvidenceProvider",
    "select_evidence_provider",
]
