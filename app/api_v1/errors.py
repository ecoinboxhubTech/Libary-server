"""The /api/v1 error format: {"error": {"code", "message", "fields"}}. Routes outside /api/v1 keep FastAPI's default."""
from fastapi import FastAPI, Query, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..apierrors import ApiError
from ..services import circulation as circ

PREFIX = "/api/v1"
STATUS_CODES = {400: "BAD_REQUEST", 413: "PAYLOAD_TOO_LARGE", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 422: "VALIDATION_FAILED",
                429: "RATE_LIMITED"}


def not_found(what: str = "Not found.") -> ApiError:
    return ApiError(404, "NOT_FOUND", what)


def conflict(message: str, code: str = "CONFLICT") -> ApiError:
    return ApiError(409, code, message)


def from_rule(e: circ.RuleError) -> ApiError:
    """Maps a circulation RuleError onto the v1 error codes."""
    first = e.blocks[0]["code"] if e.blocks else None
    if e.code == "blocked" and first == "fines":
        return ApiError(409, "FINES_BLOCK", e.message)
    if e.code == "limit" or (e.code == "blocked" and first == "limit"):
        return ApiError(409, "LOAN_LIMIT", e.message)
    if e.status == 403:
        return ApiError(403, "FORBIDDEN", e.message)
    return ApiError(e.status, STATUS_CODES.get(e.status, "CONFLICT"), e.message)


def body(code: str, message: str, fields: dict | None = None) -> dict:
    err = {"code": code, "message": message}
    if fields:
        err["fields"] = fields
    return {"error": err}


def page_params(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)) -> tuple[int, int]:
    return page, limit


def paged(data: list, page: int, limit: int, total: int) -> dict:
    return {"data": data, "page": page, "limit": limit, "total": total}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(body(exc.code, exc.message, exc.fields), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if not request.url.path.startswith(PREFIX):
            return await http_exception_handler(request, exc)
        detail = exc.detail
        if isinstance(detail, dict):
            code, message = detail.get("code") or STATUS_CODES.get(exc.status_code, "ERROR"), detail.get("message", "")
        else:
            code, message = STATUS_CODES.get(exc.status_code, "ERROR"), str(detail)
        if exc.status_code == 404 and message == "Not Found":
            message = "No such endpoint."
        return JSONResponse(body(code, message), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith(PREFIX):
            return await request_validation_exception_handler(request, exc)
        fields = {}
        for err in exc.errors():
            loc = [str(p) for p in err["loc"] if p not in ("body", "query", "path")]
            fields[".".join(loc) or "request"] = err["msg"]
        return JSONResponse(body("VALIDATION_FAILED", "Some fields are invalid.", fields), status_code=422)
