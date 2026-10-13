from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.paths import (
    allowed_media_root_status,
    list_media_directories,
    resolve_library_file,
    validate_library_root,
)
from app.core.security import (
    clear_session_cookies,
    create_session,
    hash_password,
    login_limiter,
    reauth_limiter,
    require_admin,
    require_csrf,
    revoke_current_session,
    verify_password,
)
from app.database import get_db
from app.file_actions import move_to_trash, permanently_delete, restore_from_trash
from app.jobs import job_runner
from app.models import (
    AdminAccount,
    Classification,
    DuplicateGroup,
    DuplicateMember,
    DuplicateRun,
    FileOperation,
    Job,
    Library,
    MediaFile,
    Setting,
    TrashItem,
)
from app.schemas.api import (
    BatchDecisionRequest,
    BatchMediaRequest,
    BatchTrashRequest,
    DuplicateRunRequest,
    KeepMemberRequest,
    LibraryCreate,
    LibraryUpdate,
    LoginRequest,
    OnboardingDismissRequest,
    PermanentDeleteRequest,
    ScanRequest,
    SettingsUpdate,
    SetupRequest,
)
from app.services.thumbnails import enforce_cache_quota, thumbnail_path

api_router = APIRouter()
_mutation_lock = threading.RLock()


def serialized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _mutation_lock:
            return function(*args, **kwargs)

    return wrapped


def utc_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
        )
    if isinstance(value, dict):
        return {key: utc_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [utc_json(item) for item in value]
    return value


def envelope(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": utc_json(data), "meta": utc_json(meta)}


def refresh_library_access(library: Library) -> None:
    try:
        _path, library.writable = validate_library_root(library.root_path)
    except (HTTPException, OSError):
        library.writable = False


def require_idle_library(db: Session, library_id: int) -> None:
    for job in db.scalars(select(Job).where(Job.status.in_(["queued", "running"]))):
        if job.kind == "scan" and job.payload.get("library_id") == library_id:
            raise HTTPException(409, "该 Library 已有扫描任务，请等待结束")
        if job.kind == "duplicate":
            run = db.get(DuplicateRun, job.payload.get("duplicate_run_id"))
            if run and library_id in run.library_ids:
                raise HTTPException(409, "该 Library 正在检测重复文件，请等待结束")


def serialize_library(library: Library) -> dict[str, Any]:
    return {
        "id": library.id,
        "name": library.name,
        "root_path": library.root_path,
        "enabled": library.enabled,
        "writable": library.writable,
        "last_scan_at": library.last_scan_at,
    }


def serialize_job(job: Job) -> dict[str, Any]:
    percent = None
    if job.total:
        percent = round(min(job.processed / job.total * 100, 100), 1)
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "phase": job.phase,
        "total": job.total,
        "processed": job.processed,
        "percent": percent,
        "result": job.result,
        "error": job.error,
        "progress_seq": job.progress_seq,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def serialize_media(media: MediaFile) -> dict[str, Any]:
    if media.library:
        refresh_library_access(media.library)
    return {
        "id": media.id,
        "library_id": media.library_id,
        "library_name": media.library.name if media.library else None,
        "library_writable": bool(media.library and media.library.writable),
        "relative_path": media.relative_path,
        "filename": media.filename,
        "extension": media.extension,
        "media_type": media.media_type,
        "status": media.status,
        "size": media.size,
        "mtime_ns": media.mtime_ns,
        "captured_at": media.captured_at,
        "width": media.width,
        "height": media.height,
        "duration_ms": media.duration_ms,
        "fps": media.fps,
        "codec": media.codec,
        "camera_make": media.camera_make,
        "camera_model": media.camera_model,
        "metadata_error": media.metadata_error,
        "classifications": [
            {
                "category": item.category,
                "app_source": item.app_source,
                "confidence": item.confidence,
                "reasons": item.reasons,
                "decision": item.decision,
            }
            for item in media.classifications
        ],
    }


@api_router.get("/setup/status")
def setup_status(db: Session = Depends(get_db)) -> dict:
    configured = db.scalar(select(func.count(AdminAccount.id))) > 0
    return envelope({"configured": configured})


@api_router.post("/setup/admin", status_code=status.HTTP_201_CREATED)
@serialized
def setup_admin(payload: SetupRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    if db.scalar(select(func.count(AdminAccount.id))) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "管理员已经设置")
    admin = AdminAccount(username=payload.username, password_hash=hash_password(payload.password))
    db.add(admin)
    db.commit()
    db.refresh(admin)
    create_session(db, admin, response)
    return envelope({"username": admin.username})


@api_router.get("/setup/onboarding")
def onboarding_status(
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    library_count = int(db.scalar(select(func.count(Library.id))) or 0)
    dismissed = db.get(Setting, "onboarding_dismissed")
    roots = allowed_media_root_status()
    return envelope(
        {
            "has_library": library_count > 0,
            "library_count": library_count,
            "dismissed": bool(dismissed and dismissed.value.get("value")),
            "allowed_roots": roots,
            "media_available": any(item["readable"] and item["has_children"] for item in roots),
        }
    )


@api_router.get("/setup/directories")
def onboarding_directories(
    path: str | None = Query(default=None, max_length=2048),
    _admin: AdminAccount = Depends(require_admin),
) -> dict:
    return envelope(list_media_directories(path))


@api_router.post("/setup/onboarding/dismiss")
def dismiss_onboarding(
    payload: OnboardingDismissRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    setting = db.get(Setting, "onboarding_dismissed")
    value = {
        "value": payload.dismissed,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if setting is None:
        db.add(Setting(key="onboarding_dismissed", value=value))
    else:
        setting.value = value
    db.commit()
    return envelope({"dismissed": payload.dismissed})


@api_router.post("/auth/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    key = request.client.host if request.client else "unknown"
    login_limiter.check(key)
    admin = db.scalar(select(AdminAccount).where(AdminAccount.username == payload.username))
    if admin is None or not verify_password(admin.password_hash, payload.password):
        login_limiter.fail(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    login_limiter.success(key)
    create_session(db, admin, response)
    return envelope({"username": admin.username})


@api_router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    revoke_current_session(request, db)
    clear_session_cookies(response)
    return envelope({"logged_out": True})


@api_router.get("/auth/me")
def me(admin: AdminAccount = Depends(require_admin)) -> dict:
    return envelope({"username": admin.username})


@api_router.get("/libraries")
def list_libraries(
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    libraries = db.scalars(select(Library).order_by(Library.id)).all()
    for item in libraries:
        refresh_library_access(item)
    db.commit()
    return envelope([serialize_library(item) for item in libraries])


@api_router.post("/libraries", status_code=status.HTTP_201_CREATED)
@serialized
def create_library(
    payload: LibraryCreate,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    path, writable = validate_library_root(payload.root_path)
    for existing in db.scalars(select(Library)):
        root = Path(existing.root_path)
        if path.is_relative_to(root) or root.is_relative_to(path):
            raise HTTPException(409, "目录与现有 Library 重叠，请选择不重叠的目录")
    library = Library(name=payload.name.strip(), root_path=str(path), writable=writable)
    db.add(library)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Library 名称或路径已存在") from exc
    db.refresh(library)
    return envelope(serialize_library(library))


@api_router.patch("/libraries/{library_id}")
@serialized
def update_library(
    library_id: int,
    payload: LibraryUpdate,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    library = db.get(Library, library_id)
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Library 不存在")
    if payload.name is not None:
        library.name = payload.name.strip()
    if payload.enabled is not None:
        require_idle_library(db, library_id)
        library.enabled = payload.enabled
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Library 名称已存在") from exc
    return envelope(serialize_library(library))


@api_router.delete("/libraries/{library_id}")
@serialized
def delete_library(
    library_id: int,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    library = db.get(Library, library_id)
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Library 不存在")
    active_trash = db.scalar(
        select(func.count(TrashItem.id)).where(
            TrashItem.library_id == library_id,
            TrashItem.status.notin_(["restored", "deleted", "aborted"]),
        )
    )
    if active_trash:
        raise HTTPException(status.HTTP_409_CONFLICT, "请先处理该 Library 的回收站项目")
    require_idle_library(db, library_id)
    # Delete only database history, never media/trash files. Invalidate whole cross-library groups.
    media_ids = select(MediaFile.id).where(MediaFile.library_id == library_id)
    group_ids = select(DuplicateMember.group_id).where(DuplicateMember.media_id.in_(media_ids))
    db.execute(delete(DuplicateGroup).where(DuplicateGroup.id.in_(group_ids)))
    db.execute(delete(TrashItem).where(TrashItem.library_id == library_id))
    db.execute(delete(Library).where(Library.id == library_id))
    db.commit()
    return envelope({"removed": True, "files_deleted": False})


@api_router.post("/libraries/{library_id}/scans", status_code=status.HTTP_202_ACCEPTED)
@serialized
def create_scan(
    library_id: int,
    payload: ScanRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    library = db.get(Library, library_id)
    if library is None or not library.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Library 不存在或已停用")
    validate_library_root(library.root_path)
    require_idle_library(db, library_id)
    running = db.scalar(
        select(func.count(Job.id)).where(
            Job.kind == "scan",
            Job.status.in_(["queued", "running"]),
            Job.payload["library_id"].as_integer() == library_id,
        )
    )
    if running:
        raise HTTPException(status.HTTP_409_CONFLICT, "该 Library 已有扫描任务")
    job = Job(kind="scan", payload={"library_id": library_id, "mode": payload.mode})
    db.add(job)
    db.commit()
    db.refresh(job)
    job_runner.enqueue(job.id)
    return envelope(serialize_job(job))


@api_router.post("/media/{media_id}/rescan", status_code=status.HTTP_202_ACCEPTED)
@serialized
def rescan_media(
    media_id: int,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    media = db.get(MediaFile, media_id)
    if media is None or media.status == "trashed" or not media.library.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "媒体不存在")
    require_idle_library(db, media.library_id)
    job = Job(
        kind="scan", payload={"library_id": media.library_id, "mode": "full", "media_id": media.id}
    )
    db.add(job)
    db.commit()
    job_runner.enqueue(job.id)
    return envelope(serialize_job(job))


@api_router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return envelope(serialize_job(job))


@api_router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: int,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if job.status not in {"queued", "running"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "任务已经结束")
    job.cancel_requested = True
    db.commit()
    return envelope(serialize_job(job))


@api_router.get("/events/jobs/{job_id}")
async def job_events(
    job_id: int,
    request: Request,
    _admin: AdminAccount = Depends(require_admin),
) -> StreamingResponse:
    from app.database import SessionLocal

    with SessionLocal() as event_db:
        if event_db.get(Job, job_id) is None:
            raise HTTPException(404, "任务不存在")

    async def stream():
        last_seq = -1
        first = True
        while not await request.is_disconnected():
            with SessionLocal() as event_db:
                job = event_db.get(Job, job_id)
                if job is None:
                    yield 'event: job.failed\ndata: {"error":"任务不存在"}\n\n'
                    break
                data = json.dumps(utc_json(serialize_job(job)), ensure_ascii=False)
                terminal = job.status in {"completed", "failed", "cancelled", "interrupted"}
                if first:
                    yield f"event: job.snapshot\ndata: {data}\n\n"
                if not first and job.progress_seq != last_seq or terminal:
                    last_seq = job.progress_seq
                    event_name = f"job.{job.status}" if terminal else "job.progress"
                    yield f"event: {event_name}\ndata: {data}\n\n"
                elif not first:
                    yield ": heartbeat\n\n"
                last_seq = job.progress_seq
                first = False
                if terminal:
                    break
            await asyncio.sleep(0.75)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _category_summary(db: Session, category: str, library_ids: list[int] | None) -> dict[str, int]:
    row = db.execute(
        select(func.count(MediaFile.id), func.coalesce(func.sum(MediaFile.size), 0))
        .join(Classification)
        .where(
            MediaFile.status == "active",
            Classification.category == category,
            Classification.decision == "review",
            MediaFile.library_id.in_(library_ids) if library_ids else True,
        )
    ).one()
    return {"count": int(row[0]), "bytes": int(row[1])}


@api_router.get("/dashboard/summary")
def dashboard_summary(
    library_ids: list[int] | None = Query(default=None),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    base = [MediaFile.status == "active"]
    if library_ids:
        base.append(MediaFile.library_id.in_(library_ids))

    def media_summary(media_type: str) -> dict[str, int]:
        row = db.execute(
            select(func.count(MediaFile.id), func.coalesce(func.sum(MediaFile.size), 0)).where(
                *base, MediaFile.media_type == media_type
            )
        ).one()
        return {"count": int(row[0]), "bytes": int(row[1])}

    photos = media_summary("photo")
    videos = media_summary("video")
    screenshot = _category_summary(db, "screenshot", library_ids)
    recording = _category_summary(db, "screen_recording", library_ids)
    app_images = _category_summary(db, "app_image", library_ids)
    duplicate_photos = {"count": 0, "bytes": 0}
    duplicate_videos = {"count": 0, "bytes": 0}
    scope = set(library_ids or db.scalars(select(Library.id)).all())
    for media_type, target in (("photo", duplicate_photos), ("video", duplicate_videos)):
        latest_run_id = next(
            (
                run.id
                for run in db.scalars(
                    select(DuplicateRun)
                    .join(Job, Job.id == DuplicateRun.job_id)
                    .where(
                        Job.status == "completed", DuplicateRun.media_type.in_([media_type, "both"])
                    )
                    .order_by(DuplicateRun.id.desc())
                )
                if set(run.library_ids) == scope
            ),
            None,
        )
        if latest_run_id:
            row = db.execute(
                select(
                    func.coalesce(func.sum(DuplicateGroup.copy_count - 1), 0),
                    func.coalesce(func.sum(DuplicateGroup.potential_savings), 0),
                ).where(
                    DuplicateGroup.run_id == latest_run_id,
                    DuplicateGroup.media_type == media_type,
                )
            ).one()
            target.update(count=int(row[0]), bytes=int(row[1]))
    return envelope(
        {
            "photos": photos,
            "videos": videos,
            "total_bytes": photos["bytes"] + videos["bytes"],
            "screenshot": screenshot,
            "screen_recording": recording,
            "duplicate_photos": duplicate_photos,
            "duplicate_videos": duplicate_videos,
            "app_images": app_images,
            "potential_savings": duplicate_photos["bytes"] + duplicate_videos["bytes"],
        }
    )


@api_router.get("/media")
def list_media(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    library_ids: list[int] | None = Query(default=None),
    category: str | None = Query(default=None),
    app_source: str | None = Query(default=None),
    type: str | None = Query(default=None, pattern="^(photo|video)$"),
    decision: str = Query(default="review", pattern="^(review|keep|all)$"),
    sort: str = Query(default="newest", pattern="^(newest|oldest|largest|smallest)$"),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    query = select(MediaFile).options(
        selectinload(MediaFile.library), selectinload(MediaFile.classifications)
    )
    filters = [MediaFile.status == "active"]
    if category or app_source:
        classification_filters = []
        if category:
            classification_filters.append(Classification.category == category)
        if decision != "all":
            classification_filters.append(Classification.decision == decision)
        if app_source:
            classification_filters.append(Classification.app_source == app_source)
        filters.append(MediaFile.classifications.any(and_(*classification_filters)))
    if library_ids:
        filters.append(MediaFile.library_id.in_(library_ids))
    if type:
        filters.append(MediaFile.media_type == type)
    sort_map = {
        "newest": (MediaFile.mtime_ns.desc(), MediaFile.id.desc()),
        "oldest": (MediaFile.mtime_ns.asc(), MediaFile.id.asc()),
        "largest": (MediaFile.size.desc(), MediaFile.id.desc()),
        "smallest": (MediaFile.size.asc(), MediaFile.id.asc()),
    }
    query = query.where(*filters).order_by(*sort_map[sort])
    total, total_bytes = db.execute(
        select(func.count(MediaFile.id), func.coalesce(func.sum(MediaFile.size), 0)).where(*filters)
    ).one()
    total = int(total)
    items = db.scalars(query.offset((page - 1) * limit).limit(limit)).unique().all()
    return envelope(
        [serialize_media(item) for item in items],
        page=page,
        limit=limit,
        total=total,
        total_bytes=int(total_bytes),
        pages=(total + limit - 1) // limit,
    )


@api_router.get("/media/{media_id}")
def get_media(
    media_id: int,
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    media = db.scalar(
        select(MediaFile)
        .options(selectinload(MediaFile.library), selectinload(MediaFile.classifications))
        .where(MediaFile.id == media_id)
    )
    if media is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "媒体不存在")
    return envelope(serialize_media(media))


@api_router.get("/media/{media_id}/thumbnail")
def get_thumbnail(
    media_id: int,
    size: int = Query(default=512, enum=[256, 512]),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FileResponse:
    media = db.get(MediaFile, media_id)
    if media is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "媒体不存在")
    if size not in (256, 512):
        raise HTTPException(422, "缩略图尺寸只能是 256 或 512")
    try:
        path = thumbnail_path(db, media, size)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "缩略图生成失败，文件可能损坏或不可读"
        ) from exc
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@api_router.post("/media/batch-decision")
@serialized
def batch_decision(
    payload: BatchDecisionRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    items = db.scalars(
        select(Classification).where(
            Classification.media_id.in_(payload.media_ids),
            Classification.category == payload.category,
        )
    ).all()
    if len(items) != len(set(payload.media_ids)):
        raise HTTPException(status.HTTP_409_CONFLICT, "部分文件不属于当前分类")
    now = datetime.now(timezone.utc)
    for item in items:
        media = item.media
        require_idle_library(db, media.library_id)
        if media.status != "active" or item.fingerprint != f"{media.size}:{media.mtime_ns}":
            raise HTTPException(409, "文件状态已改变，请重新扫描")
        path = resolve_library_file(media.library.root_path, media.relative_path)
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (media.size, media.mtime_ns):
            raise HTTPException(409, "文件已改变，请重新扫描后审核")
    for item in items:
        item.decision = payload.decision
        item.decided_at = now if payload.decision == "keep" else None
    db.commit()
    return envelope({"updated": len(items)})


@api_router.post("/duplicate-runs", status_code=status.HTTP_202_ACCEPTED)
@serialized
def create_duplicate_run(
    payload: DuplicateRunRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    found = set(
        db.scalars(
            select(Library.id).where(Library.id.in_(payload.library_ids), Library.enabled.is_(True))
        ).all()
    )
    if found != set(payload.library_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "部分 Library 不存在")
    for library_id in payload.library_ids:
        require_idle_library(db, library_id)
    job = Job(kind="duplicate", payload={})
    db.add(job)
    db.flush()
    run = DuplicateRun(
        job_id=job.id,
        library_ids=payload.library_ids,
        media_type=payload.media_type,
    )
    db.add(run)
    db.flush()
    job.payload = {"duplicate_run_id": run.id}
    db.commit()
    job_runner.enqueue(job.id)
    return envelope({"run_id": run.id, "job": serialize_job(job)})


@api_router.get("/duplicate-runs")
def list_duplicate_runs(
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    runs = db.scalars(select(DuplicateRun).order_by(DuplicateRun.id.desc()).limit(20)).all()
    data = []
    for run in runs:
        job = db.get(Job, run.job_id)
        data.append(
            {
                "id": run.id,
                "library_ids": run.library_ids,
                "media_type": run.media_type,
                "created_at": run.created_at,
                "job": serialize_job(job) if job else None,
            }
        )
    return envelope(data)


@api_router.get("/duplicate-groups")
def list_duplicate_groups(
    run_id: int,
    media_type: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    run = db.get(DuplicateRun, run_id)
    if run is None:
        raise HTTPException(404, "检测记录不存在")
    job = db.get(Job, run.job_id)
    if not job or job.status != "completed":
        return envelope([], page=page, limit=limit, total=0, pages=0)
    filters = [DuplicateGroup.run_id == run_id]
    if media_type:
        filters.append(DuplicateGroup.media_type == media_type)
    total = int(db.scalar(select(func.count(DuplicateGroup.id)).where(*filters)) or 0)
    groups = db.scalars(
        select(DuplicateGroup)
        .where(*filters)
        .order_by(DuplicateGroup.potential_savings.desc(), DuplicateGroup.id)
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()
    data = []
    for group in groups:
        data.append(
            {
                "id": group.id,
                "media_type": group.media_type,
                "size": group.size,
                "copy_count": group.copy_count,
                "potential_savings": group.potential_savings,
                "sha256": group.sha256,
                "members": _duplicate_members(db, group.id, 1, 50),
                "members_total": group.copy_count,
                "members_has_more": group.copy_count > 50,
            }
        )
    return envelope(data, page=page, limit=limit, total=total, pages=(total + limit - 1) // limit)


def _duplicate_members(db: Session, group_id: int, page: int, limit: int) -> list[dict]:
    members = db.scalars(
        select(DuplicateMember)
        .where(DuplicateMember.group_id == group_id)
        .options(
            selectinload(DuplicateMember.media).selectinload(MediaFile.library),
            selectinload(DuplicateMember.media).selectinload(MediaFile.classifications),
        )
        .order_by(DuplicateMember.is_keep.desc(), DuplicateMember.id)
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()
    return [
        {
            **serialize_media(member.media),
            "is_keep": member.is_keep,
            "is_default_keep": member.is_default_keep,
            "is_hardlink": member.is_hardlink,
        }
        for member in members
    ]


@api_router.get("/duplicate-groups/{group_id}/members")
def list_duplicate_members(
    group_id: int,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    group = db.get(DuplicateGroup, group_id)
    if group is None or db.get(Job, group.run.job_id).status != "completed":
        raise HTTPException(404, "重复组不存在或检测尚未完成")
    total = int(
        db.scalar(
            select(func.count(DuplicateMember.id)).where(DuplicateMember.group_id == group_id)
        )
        or 0
    )
    return envelope(
        _duplicate_members(db, group_id, page, limit),
        page=page,
        limit=limit,
        total=total,
        pages=(total + limit - 1) // limit,
    )


@api_router.put("/duplicate-groups/{group_id}/keep")
@serialized
def choose_duplicate_keep(
    group_id: int,
    payload: KeepMemberRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    member = db.scalar(
        select(DuplicateMember).where(
            DuplicateMember.group_id == group_id, DuplicateMember.media_id == payload.media_id
        )
    )
    if not member or member.media.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "重复组或文件不存在")
    group = member.group
    job = db.get(Job, group.run.job_id)
    if job is None or job.status != "completed":
        raise HTTPException(409, "重复检测尚未完成，不能修改保留项")
    for library_id in db.scalars(
        select(MediaFile.library_id)
        .join(DuplicateMember)
        .where(DuplicateMember.group_id == group_id)
        .distinct()
    ):
        require_idle_library(db, library_id)
    media = member.media
    try:
        source = resolve_library_file(media.library.root_path, media.relative_path)
        current = source.stat(follow_symlinks=False)
        if (
            (current.st_size, current.st_mtime_ns) != (media.size, media.mtime_ns)
            or (media.file_inode is not None and current.st_ino != media.file_inode)
            or (media.file_device is not None and current.st_dev != media.file_device)
        ):
            raise OSError("文件已经改变")
    except (OSError, HTTPException) as exc:
        db.execute(delete(DuplicateGroup).where(DuplicateGroup.id == group_id))
        db.commit()
        raise HTTPException(409, "保留文件已改变或不可读取，请重新扫描和检测重复项") from exc
    db.execute(
        update(DuplicateMember)
        .where(DuplicateMember.group_id == group_id)
        .values(is_keep=DuplicateMember.media_id == payload.media_id)
    )
    db.commit()
    return envelope({"group_id": group_id, "keep_media_id": payload.media_id})


@api_router.post("/trash/actions/move")
@serialized
def trash_move(
    payload: BatchMediaRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    for library_id in db.scalars(
        select(MediaFile.library_id).where(MediaFile.id.in_(payload.media_ids)).distinct()
    ):
        require_idle_library(db, library_id)
    items = move_to_trash(db, payload.media_ids)
    return envelope({"moved": len(items), "trash_item_ids": [item.id for item in items]})


@api_router.get("/trash")
def list_trash(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    library_ids: list[int] | None = Query(default=None),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    filters = [TrashItem.status == "active"]
    if library_ids:
        filters.append(TrashItem.library_id.in_(library_ids))
    total = int(db.scalar(select(func.count(TrashItem.id)).where(*filters)) or 0)
    items = db.scalars(
        select(TrashItem)
        .options(selectinload(TrashItem.library))
        .where(*filters)
        .order_by(TrashItem.moved_at.desc(), TrashItem.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()
    for item in items:
        refresh_library_access(item.library)
    data = [
        {
            "id": item.id,
            "media_id": item.media_id,
            "filename": item.filename,
            "library_name": item.library.name,
            "library_id": item.library_id,
            "library_writable": item.library.writable,
            "status": item.status,
            "original_relative_path": item.original_relative_path,
            "size": item.size,
            "category_snapshot": item.category_snapshot,
            "moved_at": item.moved_at,
        }
        for item in items
    ]
    return envelope(data, page=page, limit=limit, total=total, pages=(total + limit - 1) // limit)


@api_router.get("/trash/{item_id}/thumbnail")
def get_trash_thumbnail(
    item_id: int,
    size: int = Query(default=256),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FileResponse:
    from app.services.thumbnails import trash_thumbnail_path

    item = db.get(TrashItem, item_id)
    if item is None or item.status != "active":
        raise HTTPException(404, "回收站项目不存在")
    if size not in (256, 512):
        raise HTTPException(422, "缩略图尺寸只能是 256 或 512")
    try:
        path = trash_thumbnail_path(db, item, size)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "缩略图生成失败，文件可能损坏或不可读") from exc
    return FileResponse(
        path, media_type="image/webp", headers={"Cache-Control": "private, no-store"}
    )


@api_router.post("/trash/actions/restore")
@serialized
def trash_restore(
    payload: BatchTrashRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    for library_id in db.scalars(
        select(TrashItem.library_id).where(TrashItem.id.in_(payload.trash_item_ids)).distinct()
    ):
        require_idle_library(db, library_id)
    items = restore_from_trash(db, payload.trash_item_ids)
    return envelope({"restored": len(items)})


@api_router.post("/trash/actions/permanent-delete")
@serialized
def trash_permanent_delete(
    payload: PermanentDeleteRequest,
    admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    for library_id in db.scalars(
        select(TrashItem.library_id).where(TrashItem.id.in_(payload.trash_item_ids)).distinct()
    ):
        require_idle_library(db, library_id)
    reauth_limiter.check(str(admin.id))
    if not verify_password(admin.password_hash, payload.password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员密码错误")
    reauth_limiter.success(str(admin.id))
    deleted_count = permanently_delete(db, payload.trash_item_ids)
    return envelope({"deleted": deleted_count})


@api_router.get("/file-operations")
def list_file_operations(
    limit: int = Query(default=50, ge=1, le=100),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    filters = [FileOperation.state != "completed"]
    total = db.scalar(select(func.count(FileOperation.id)).where(*filters)) or 0
    operations = db.scalars(
        select(FileOperation).where(*filters).order_by(FileOperation.id.desc()).limit(limit)
    )
    return envelope(
        [
            {
                "id": operation.id,
                "operation": operation.operation,
                "state": operation.state,
                "media_id": operation.media_id,
                "trash_item_id": operation.trash_item_id,
                "error": operation.error,
                "updated_at": operation.updated_at,
            }
            for operation in operations
        ],
        total=total,
    )


@api_router.get("/settings")
def get_settings_api(
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    values = {item.key: item.value for item in db.scalars(select(Setting)).all()}
    return envelope(
        {
            "thumbnail_cache_gb": values.get("thumbnail_cache_gb", {}).get("value", 10),
            "metadata_workers": values.get("metadata_workers", {}).get("value", 2),
            "language": "zh-CN",
        }
    )


@api_router.patch("/settings")
def update_settings_api(
    payload: SettingsUpdate,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    changes = payload.model_dump(exclude_none=True)
    for key, value in changes.items():
        setting = db.get(Setting, key)
        if setting is None:
            setting = Setting(key=key, value={"value": value})
            db.add(setting)
        else:
            setting.value = {"value": value}
    db.commit()
    cache_result = None
    if "thumbnail_cache_gb" in changes:
        cache_result = enforce_cache_quota(changes["thumbnail_cache_gb"])
    return envelope({"updated": changes, "cache": cache_result})
