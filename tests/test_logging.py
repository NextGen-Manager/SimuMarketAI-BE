"""Secret-bearing structured log fields never reach rendered output."""

from __future__ import annotations

import json
import logging

from app.core.logging import JsonFormatter


def test_provider_api_keys_are_redacted() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="provider configured",
        args=(),
        exc_info=None,
    )
    record.gemini_api_key = "gemini-secret"
    record.OPENAI_API_KEY = "openai-secret"
    record.provider = "openai"

    rendered = JsonFormatter().format(record)
    payload = json.loads(rendered)

    assert payload["provider"] == "openai"
    assert "gemini_api_key" not in payload
    assert "OPENAI_API_KEY" not in payload
    assert "secret" not in rendered
