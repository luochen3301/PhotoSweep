from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import api_router
from app.core.config import get_settings
from app.database import SessionLocal, init_db
from app.jobs import job_runner


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with SessionLocal() as db:
        from app.file_actions.trash_service import recover_file_operations

        recover_file_operations(db)
    job_runner.start()
    yield
    job_runner.stop()


app = FastAPI(
    title="PhotoSweep API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        expected_origin = get_settings().public_origin or str(request.base_url)
        if origin and origin.rstrip("/") != expected_origin.rstrip("/"):
            return JSONResponse(
                {
                    "type": "about:blank",
                    "title": "请求失败",
                    "status": 403,
                    "detail": "不允许跨站请求",
                },
                status_code=403,
                media_type="application/problem+json",
            )
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.exception_handler(StarletteHTTPException)
async def http_problem(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        {
            "type": "about:blank",
            "title": "请求失败",
            "status": exc.status_code,
            "detail": exc.detail,
        },
        status_code=exc.status_code,
        media_type="application/problem+json",
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_problem(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {
            "type": "about:blank",
            "title": "请求参数无效",
            "status": 422,
            "detail": "请检查输入内容",
            "errors": [
                {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                for item in exc.errors()
            ],
        },
        status_code=422,
        media_type="application/problem+json",
    )


@app.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> dict:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, "数据库尚未就绪") from exc
    return {"status": "ready"}


app.include_router(api_router, prefix="/api/v1")

frontend_dist = get_settings().resolved_frontend_dist
if frontend_dist.exists():
    assets = frontend_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path == "api" or full_path.startswith(("api/", "health/")):
            raise HTTPException(404, "接口不存在")
        requested = frontend_dist / full_path
        if full_path and requested.is_file() and requested.resolve().is_relative_to(frontend_dist):
            return FileResponse(requested)
        return FileResponse(frontend_dist / "index.html")
