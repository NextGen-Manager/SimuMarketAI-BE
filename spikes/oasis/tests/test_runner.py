from pathlib import Path

import pytest
from oasis_spike.contracts import RunLimits
from oasis_spike.runner import _extract_json, dependency_probe, run_live


def test_dependency_probe_imports_pinned_oasis() -> None:
    result = dependency_probe()

    assert result["status"] == "ok"
    assert result["camel_ai"] == "0.2.78"
    assert result["camel_oasis"] == "0.2.5"


def test_extract_json_accepts_fenced_model_output() -> None:
    assert _extract_json('```json\n{"confidence":"low"}\n```') == {"confidence": "low"}


async def test_live_run_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        await run_live(
            output_root=Path("unused"),
            model_id="test-model",
            seed=42,
            limits=RunLimits(
                max_total_tokens=100,
                max_output_tokens_per_stage=25,
                stage_timeout_seconds=1,
            ),
        )
