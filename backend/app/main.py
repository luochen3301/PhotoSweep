from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import api_router
from app.core.config import get_settings
from app.database import SessionLocal, init_db
from app.jobs import job_runner
from app.models import FileOperation


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with SessionLocal() as db:
        uncertain = db.query(FileOperation).filter(
            FileOperation.state.notin_(["completed", "failed"])
        )
        for operation in uncertain:
            operation.state = "failed"
            operation.error = "容器重启后操作状态不明确；未执行任何自动删除"
        db.commit()
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


@app.exception_handler(HTTPException)
async def http_problem(_request: Request, exc: HTTPException) -> JSONResponse:
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
            "errors": exc.errors(),
        },
        status_code=422,
        media_type="application/problem+json",
    )


@app.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> dict:
    get_settings().prepare_directories()
    return {"status": "ready"}


app.include_router(api_router, prefix="/api/v1")

frontend_dist = get_settings().resolved_frontend_dist
if frontend_dist.exists():
    assets = frontend_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        requested = frontend_dist / full_path
        if full_path and requested.is_file() and requested.resolve().is_relative_to(frontend_dist):
            return FileResponse(requested)
        return FileResponse(frontend_dist / "index.html")
