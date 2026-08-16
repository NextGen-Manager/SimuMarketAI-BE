import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "X-Correlation-ID"

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    return _correlation_id.get()


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


class CorrelationIdMiddleware:
    """Carries one ID from the API through workers, OASIS traces, and reports.

    Without it a failed run cannot be traced across process boundaries, and the
    ID shown to the user in an error body would point at nothing.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(CORRELATION_HEADER)
        correlation_id = _sanitize(incoming) or str(uuid.uuid4())
        set_correlation_id(correlation_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((CORRELATION_HEADER.lower().encode(), correlation_id.encode()))
            await send(message)

        await self.app(scope, receive, send_with_header)


def _sanitize(value: str | None) -> str | None:
    # A client-supplied ID is untrusted input that ends up in logs, so only accept
    # something that is already a UUID.
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None
