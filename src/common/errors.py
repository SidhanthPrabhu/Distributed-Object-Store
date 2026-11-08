"""Reusable error envelope and request correlation for either service."""

import logging
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from common.models import ErrorDetail, ErrorEnvelope

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(request: Request, status: int, code: str, message: str,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    request_id = getattr(request.state, "request_id", uuid4())
    envelope = ErrorEnvelope(error=ErrorDetail(
        code=code, message=message, request_id=request_id,
    ))
    return JSONResponse(
        status_code=status, content=envelope.model_dump(mode="json"),
        headers={**(headers or {}), "X-Request-ID": str(request_id)},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlate(request: Request, call_next):
        try:
            request.state.request_id = UUID(request.headers.get("X-Request-ID", ""))
        except ValueError:
            request.state.request_id = uuid4()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Request failed: %s", request.state.request_id)
            return error_response(request, 500, "internal_error", "Unexpected internal error")
        response.headers["X-Request-ID"] = str(request.state.request_id)
        return response

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError):
        return error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo request bodies, filesystem paths, or validation input values.
        return error_response(request, 400, "invalid_request", "Request validation failed")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code = {400: "invalid_request", 404: "not_found", 409: "conflict",
                503: "service_unavailable"}.get(exc.status_code, "http_error")
        message = str(exc.detail) if exc.status_code < 500 else "Service request failed"
        return error_response(request, exc.status_code, code, message, exc.headers)
