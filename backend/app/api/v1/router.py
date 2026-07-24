from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.paths import (
    allowed_media_root_status,
    list_media_directories,
    validate_library_root,
)
from app.core.security import (
    clear_session_cookies,
    create_session,
    hash_password,
    login_limiter,
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


def envelope(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": meta}


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
            "media_available": any(
                item["readable"] and item["has_children"] for item in roots
            ),
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
    return envelope([serialize_library(item) for item in libraries])


@api_router.post("/libraries", status_code=status.HTTP_201_CREATED)
def create_library(
    payload: LibraryCreate,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    path, writable = validate_library_root(payload.root_path)
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
        library.enabled = payload.enabled
    db.commit()
    return envelope(serialize_library(library))


@api_router.delete("/libraries/{library_id}")
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
            TrashItem.library_id == library_id, TrashItem.status == "active"
        )
    )
    if active_trash:
        raise HTTPException(status.HTTP_409_CONFLICT, "请先处理该 Library 的回收站项目")
    db.delete(library)
    db.commit()
    return envelope({"removed": True, "files_deleted": False})


@api_router.post("/libraries/{library_id}/scans", status_code=status.HTTP_202_ACCEPTED)
def create_scan(
    library_id: int,
    payload: ScanRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    library = db.get(Library, library_id)
    if library is None or not library.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Library 不存在或已停用")
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
def rescan_media(
    media_id: int,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    media = db.get(MediaFile, media_id)
    if media is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "媒体不存在")
    job = Job(kind="scan", payload={"library_id": media.library_id, "mode": "full"})
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
    _admin: AdminAccount = Depends(require_admin),
) -> StreamingResponse:
    async def stream():
        last_seq = -1
        while True:
            from app.database import SessionLocal

            with SessionLocal() as event_db:
                job = event_db.get(Job, job_id)
                if job is None:
                    yield 'event: job.failed\ndata: {"error":"任务不存在"}\n\n'
                    break
                if job.progress_seq != last_seq:
                    last_seq = job.progress_seq
                    event_name = f"job.{job.status}" if job.status != "running" else "job.progress"
                    data = json.dumps(serialize_job(job), default=str, ensure_ascii=False)
                    yield f"event: {event_name}\ndata: {data}\n\n"
                else:
                    yield ": heartbeat\n\n"
                if job.status in {"completed", "failed", "cancelled", "interrupted"}:
                    break
            await asyncio.sleep(0.75)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _category_summary(db: Session, category: str) -> dict[str, int]:
    row = db.execute(
        select(func.count(MediaFile.id), func.coalesce(func.sum(MediaFile.size), 0))
        .join(Classification)
        .where(
            MediaFile.status == "active",
            Classification.category == category,
            Classification.decision == "review",
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
    screenshot = _category_summary(db, "screenshot")
    recording = _category_summary(db, "screen_recording")
    app_images = _category_summary(db, "app_image")
    latest_run_id = db.scalar(select(func.max(DuplicateRun.id)))
    duplicate_photos = {"count": 0, "bytes": 0}
    duplicate_videos = {"count": 0, "bytes": 0}
    if latest_run_id:
        for media_type, target in (
            ("photo", duplicate_photos),
            ("video", duplicate_videos),
        ):
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
    type: str | None = Query(default=None),
    sort: str = Query(default="newest", pattern="^(newest|oldest|largest|smallest)$"),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    query = select(MediaFile).options(
        selectinload(MediaFile.library), selectinload(MediaFile.classifications)
    )
    count_query = select(func.count(func.distinct(MediaFile.id)))
    filters = [MediaFile.status == "active"]
    if category:
        query = query.join(Classification)
        count_query = count_query.join(Classification)
        filters.extend([Classification.category == category, Classification.decision == "review"])
        if app_source:
            filters.append(Classification.app_source == app_source)
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
    count_query = count_query.where(*filters)
    total = int(db.scalar(count_query) or 0)
    items = db.scalars(query.offset((page - 1) * limit).limit(limit)).unique().all()
    return envelope(
        [serialize_media(item) for item in items],
        page=page,
        limit=limit,
        total=total,
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
    size: int = Query(default=512),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FileResponse:
    media = db.get(MediaFile, media_id)
    if media is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "媒体不存在")
    try:
        path = thumbnail_path(db, media, size)
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"缩略图生成失败: {exc}") from exc
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@api_router.post("/media/batch-decision")
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
        item.decision = payload.decision
        item.decided_at = now if payload.decision == "keep" else None
    db.commit()
    return envelope({"updated": len(items)})


@api_router.post("/duplicate-runs", status_code=status.HTTP_202_ACCEPTED)
def create_duplicate_run(
    payload: DuplicateRunRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    found = set(db.scalars(select(Library.id).where(Library.id.in_(payload.library_ids))).all())
    if found != set(payload.library_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "部分 Library 不存在")
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
    filters = [DuplicateGroup.run_id == run_id]
    if media_type:
        filters.append(DuplicateGroup.media_type == media_type)
    total = int(db.scalar(select(func.count(DuplicateGroup.id)).where(*filters)) or 0)
    groups = db.scalars(
        select(DuplicateGroup)
        .options(
            selectinload(DuplicateGroup.members)
            .selectinload(DuplicateMember.media)
            .selectinload(MediaFile.library)
        )
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
                "members": [
                    {
                        **serialize_media(member.media),
                        "is_keep": member.is_keep,
                        "is_hardlink": member.is_hardlink,
                    }
                    for member in group.members
                ],
            }
        )
    return envelope(data, page=page, limit=limit, total=total)


@api_router.put("/duplicate-groups/{group_id}/keep")
def choose_duplicate_keep(
    group_id: int,
    payload: KeepMemberRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    members = db.scalars(select(DuplicateMember).where(DuplicateMember.group_id == group_id)).all()
    if not members or payload.media_id not in {member.media_id for member in members}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "重复组或文件不存在")
    for member in members:
        member.is_keep = member.media_id == payload.media_id
    db.commit()
    return envelope({"group_id": group_id, "keep_media_id": payload.media_id})


@api_router.post("/trash/actions/move")
def trash_move(
    payload: BatchMediaRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    items = move_to_trash(db, payload.media_ids)
    return envelope({"moved": len(items), "trash_item_ids": [item.id for item in items]})


@api_router.get("/trash")
def list_trash(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    _admin: AdminAccount = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    filters = [TrashItem.status == "active"]
    total = int(db.scalar(select(func.count(TrashItem.id)).where(*filters)) or 0)
    items = db.scalars(
        select(TrashItem)
        .options(selectinload(TrashItem.library))
        .where(*filters)
        .order_by(TrashItem.moved_at.desc(), TrashItem.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()
    data = [
        {
            "id": item.id,
            "media_id": item.media_id,
            "filename": item.filename,
            "library_name": item.library.name,
            "original_relative_path": item.original_relative_path,
            "size": item.size,
            "category_snapshot": item.category_snapshot,
            "moved_at": item.moved_at,
        }
        for item in items
    ]
    return envelope(data, page=page, limit=limit, total=total)


@api_router.post("/trash/actions/restore")
def trash_restore(
    payload: BatchTrashRequest,
    _admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    items = restore_from_trash(db, payload.trash_item_ids)
    return envelope({"restored": len(items)})


@api_router.post("/trash/actions/permanent-delete")
def trash_permanent_delete(
    payload: PermanentDeleteRequest,
    admin: AdminAccount = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_password(admin.password_hash, payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "管理员密码错误")
    deleted_count = permanently_delete(db, payload.trash_item_ids)
    return envelope({"deleted": deleted_count})


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
