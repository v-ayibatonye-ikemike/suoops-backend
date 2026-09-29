import logging
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.errors")


def register_error_handlers(app):
    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):  # noqa: BLE001
        correlation_id = uuid.uuid4().hex
        logger.exception("Unhandled error cid=%s path=%s method=%s", correlation_id, request.url.path, request.method)
        return JSONResponse(status_code=500, content={"detail": "Internal server error", "cid": correlation_id})

    return app
