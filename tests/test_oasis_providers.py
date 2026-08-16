"""Deployment-level selection of the live OASIS model provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.agents import TraceArtifact, build_cohort_manifest
from app.integrations.oasis import UnavailableOasisAdapter, select_oasis_adapter
from app.integrations.oasis.live import LiveOasisAdapter
from app.integrations.oasis.providers import resolve_model_platform, resolve_provider
from app.integrations.oasis.runtime import budget_from_settings, build_manifest

JWT_SECRET = "test-secret-with-at-least-thirty-two-characters"


def _settings(**overrides: object) -> Settings:
    return Settings(environment="test", jwt_secret=JWT_SECRET, **overrides)  # type: ignore[arg-type]


def test_provider_is_restricted_to_the_supported_allowlist() -> None:
    with pytest.raises(ValueError, match="oasis_provider"):
        _settings(oasis_provider="anthropic")


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [
        ("gemini", "gpt-5-mini"),
        ("openai", "gemini-3.1-flash-lite"),
    ],
)
def test_provider_and_model_must_match(provider: str, model_id: str) -> None:
    with pytest.raises(ValueError, match="OASIS_MODEL_ID"):
        _settings(oasis_provider=provider, oasis_model_id=model_id)


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [
        ("gemini", "gemini-3.1-flash-lite"),
        ("openai", "gpt-5-mini"),
    ],
)
def test_supported_provider_model_pairs_are_accepted(provider: str, model_id: str) -> None:
    settings = _settings(oasis_provider=provider, oasis_model_id=model_id)
    assert settings.oasis_provider == provider
    assert settings.oasis_model_id == model_id


def test_resolver_uses_only_the_selected_provider_key() -> None:
    gemini = resolve_provider(
        _settings(gemini_api_key="gemini-secret", openai_api_key="openai-secret")
    )
    openai = resolve_provider(
        _settings(
            oasis_provider="openai",
            oasis_model_id="gpt-5-mini",
            gemini_api_key="gemini-secret",
            openai_api_key="openai-secret",
        )
    )

    assert gemini.api_key == "gemini-secret"
    assert openai.api_key == "openai-secret"
    assert "secret" not in repr(gemini)
    assert "secret" not in repr(openai)


def test_missing_selected_key_does_not_fall_back_to_another_provider() -> None:
    settings = _settings(
        oasis_provider="openai",
        oasis_model_id="gpt-5-mini",
        gemini_api_key="installed-but-not-selected",
        openai_api_key="",
    )

    assert isinstance(select_oasis_adapter(settings), UnavailableOasisAdapter)


def test_selected_key_builds_a_live_adapter_for_that_provider() -> None:
    settings = _settings(
        oasis_provider="openai",
        oasis_model_id="gpt-5-mini",
        openai_api_key="openai-secret",
    )

    adapter = select_oasis_adapter(settings)
    assert isinstance(adapter, LiveOasisAdapter)
    assert adapter._provider == "openai"


def test_provider_resolves_to_the_matching_camel_platform() -> None:
    class Platforms:
        GEMINI = object()
        OPENAI = object()

    assert resolve_model_platform("gemini", Platforms) is Platforms.GEMINI
    assert resolve_model_platform("openai", Platforms) is Platforms.OPENAI


def test_run_manifest_records_the_selected_provider_and_model(tmp_path: Path) -> None:
    settings = _settings(oasis_provider="openai", oasis_model_id="gpt-5-mini")
    manifest = build_manifest(
        settings,
        adapter_id="oasis-live",
        environment="manifest-test",
        cohort=build_cohort_manifest(cohort_version="jabodetabek-fnb-v1", size=16),
        budget=budget_from_settings(settings),
        trace=TraceArtifact(object_key=str(tmp_path / "trace.db"), retention_days=30),
        evidence_snapshot_version="evidence-v1",
        snapshot_hash="0" * 64,
    )

    assert manifest.provider == "openai"
    assert manifest.model_id == "gpt-5-mini"
