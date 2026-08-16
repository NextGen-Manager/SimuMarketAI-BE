"""Provider selection for the live OASIS adapter.

The deployment names its provider explicitly. Keys are never probed to choose a
provider because that would make a run depend on whichever secret happened to
be installed first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.core.config import Settings

OasisProvider = Literal["gemini", "openai"]

_CAMEL_PLATFORM_MEMBER: dict[OasisProvider, str] = {
    "gemini": "GEMINI",
    "openai": "OPENAI",
}


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    provider: OasisProvider
    model_id: str
    api_key: str = field(repr=False)


def resolve_provider(settings: Settings) -> ProviderSelection:
    api_key = (
        settings.gemini_api_key if settings.oasis_provider == "gemini" else settings.openai_api_key
    )
    return ProviderSelection(
        provider=settings.oasis_provider,
        model_id=settings.oasis_model_id,
        api_key=api_key,
    )


def resolve_model_platform(provider: OasisProvider, model_platform_type: object) -> object:
    """Resolve the lazily imported CAMEL enum without importing the optional extra here."""

    try:
        return getattr(model_platform_type, _CAMEL_PLATFORM_MEMBER[provider])
    except AttributeError as error:
        raise ValueError(f"CAMEL tidak mendukung provider OASIS {provider}.") from error
