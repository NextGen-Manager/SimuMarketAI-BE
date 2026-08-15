"""OASIS integration boundary.

`select_oasis_adapter` is the only place that decides which adapter a run gets.
It mirrors `select_evidence_provider`: a fake is allowed in development and test
and refused outright in staging and production, because invented content must
never be able to reach a real report through a configuration mistake.

A deployment without a provider key gets `UnavailableOasisAdapter`, not a
silently degraded run. The pipeline turns that into `partial` with a stated
reason, which is the behaviour docs/04 requires.
"""

from __future__ import annotations

from app.core.config import Settings
from app.domain.agents import OasisAdapter
from app.integrations.oasis.fake import FakeOasisAdapter
from app.integrations.oasis.unavailable import UnavailableOasisAdapter

PROTECTED_ENVIRONMENTS = frozenset({"staging", "production"})

NO_API_KEY_REASON = "Kunci penyedia model belum tersedia sehingga simulasi agent tidak dijalankan."
DISABLED_REASON = "Simulasi agent dimatikan melalui konfigurasi pada lingkungan ini."


class FakeAdapterNotAllowedError(RuntimeError):
    """Raised when a fake adapter is wired into a protected environment."""


def select_oasis_adapter(
    settings: Settings,
    adapter: OasisAdapter | None = None,
) -> OasisAdapter:
    if adapter is not None:
        if adapter.is_fake and settings.environment in PROTECTED_ENVIRONMENTS:
            raise FakeAdapterNotAllowedError(
                "Fake OASIS adapter is not allowed outside development and test."
            )
        return adapter

    if not settings.oasis_enabled:
        return UnavailableOasisAdapter(DISABLED_REASON)
    if not settings.gemini_api_key:
        return UnavailableOasisAdapter(NO_API_KEY_REASON)

    # Imported lazily: `camel-oasis` is not installed in the default backend
    # environment, and a missing package must degrade to `partial` at run time
    # rather than break every import of this package.
    from app.integrations.oasis.live import LiveOasisAdapter

    return LiveOasisAdapter(
        api_key=settings.gemini_api_key,
        model_id=settings.oasis_model_id,
        provider=settings.oasis_provider,
    )


def simulation_is_planned(adapter: OasisAdapter) -> bool:
    """Whether `simulating` belongs in this run's stage plan.

    An adapter that can run keeps the stage in the plan even if it later fails,
    because the stage was genuinely attempted. Only a deployment with no adapter
    at all drops it, and then the report says so instead of showing a stage that
    silently did nothing.
    """
    return not isinstance(adapter, UnavailableOasisAdapter)


__all__ = [
    "DISABLED_REASON",
    "NO_API_KEY_REASON",
    "FakeAdapterNotAllowedError",
    "FakeOasisAdapter",
    "UnavailableOasisAdapter",
    "select_oasis_adapter",
    "simulation_is_planned",
]
