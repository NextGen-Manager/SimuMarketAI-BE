import json
import logging
import sys
from typing import Any

from app.core.correlation import get_correlation_id

# docs/07 forbids these in logs. Kept as a list so the filter fails loudly during
# review if someone adds a field that carries a secret.
REDACTED_KEYS = frozenset(
    {
        "password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "gemini_api_key",
        "openai_api_key",
        "prompt",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }

        for key, value in record.__dict__.items():
            if key in payload or key.startswith("_") or key in logging.LogRecord.__dict__:
                continue
            if key.lower() in REDACTED_KEYS:
                continue
            if isinstance(value, str | int | float | bool | type(None)):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(debug: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if debug else logging.INFO)
