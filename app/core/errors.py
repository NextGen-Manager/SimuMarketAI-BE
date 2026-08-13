import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.correlation import get_correlation_id

logger = logging.getLogger(__name__)


class FieldError(BaseModel):
    path: str
    reason: str


class ErrorBody(BaseModel):
    code: str
    message: str
    fields: list[FieldError] = []
    correlation_id: str
    retryable: bool


class ErrorResponse(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Base for errors that are safe to show a user.

    Anything not derived from this is treated as unexpected and reported as a
    generic message, because docs/07 forbids leaking internals to the client.
    """

    status_code = 400
    code = "BAD_REQUEST"
    message = "Permintaan tidak dapat diproses."
    retryable = False

    def __init__(
        self,
        message: str | None = None,
        *,
        fields: list[FieldError] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self.detail = message or self.message
        self.fields = fields or []


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    message = "Data yang diminta tidak ditemukan."


class UnauthorizedError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"
    message = "Silakan masuk terlebih dahulu."


class ValidationFailedError(AppError):
    status_code = 422
    code = "VALIDATION_FAILED"
    message = "Ada isian yang belum benar."


def _render(
    status_code: int,
    code: str,
    message: str,
    *,
    fields: list[FieldError] | None = None,
    retryable: bool = False,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            fields=fields or [],
            correlation_id=get_correlation_id(),
            retryable=retryable,
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return _render(
            exc.status_code,
            exc.code,
            exc.detail,
            fields=exc.fields,
            retryable=exc.retryable,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            FieldError(
                path=".".join(str(part) for part in error["loc"][1:]) or str(error["loc"][0]),
                reason=error["type"],
            )
            for error in exc.errors()
        ]
        return _render(
            422,
            ValidationFailedError.code,
            ValidationFailedError.message,
            fields=fields,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        message = (
            "Halaman atau data tidak ditemukan."
            if exc.status_code == 404
            else "Permintaan tidak dapat diproses."
        )
        return _render(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # The trace goes to the log, never to the client.
        logger.exception("unhandled_error", extra={"correlation_id": get_correlation_id()})
        return _render(
            500,
            "INTERNAL_ERROR",
            "Terjadi gangguan pada sistem. Coba lagi beberapa saat.",
            retryable=True,
        )
