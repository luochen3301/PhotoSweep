from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from app.core.config import get_settings


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_library_root(raw_path: str) -> tuple[Path, bool]:
    path, _root = _safe_browsable_directory(raw_path)
    writable = os.access(path, os.W_OK)
    return path, writable


def _allowed_root_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for configured in get_settings().allowed_media_roots:
        lexical = Path(os.path.abspath(configured.expanduser()))
        exists = lexical.exists()
        is_directory = exists and lexical.is_dir()
        is_symlink = lexical.is_symlink()
        readable = is_directory and not is_symlink and os.access(lexical, os.R_OK | os.X_OK)
        writable = readable and os.access(lexical, os.W_OK)
        entries.append(
            {
                "name": lexical.name or str(lexical),
                "path": str(lexical),
                "exists": exists,
                "readable": readable,
                "writable": writable,
                "is_symlink": is_symlink,
                "has_children": _has_child_directories(lexical) if readable else False,
                "selectable": readable,
                "is_root": True,
            }
        )
    return entries


def allowed_media_root_status() -> list[dict[str, Any]]:
    return _allowed_root_entries()


def _has_child_directories(path: Path) -> bool:
    try:
        with os.scandir(path) as children:
            return any(
                child.is_dir(follow_symlinks=False) and not child.is_symlink()
                for child in children
            )
    except OSError:
        return False


def _safe_browsable_directory(raw_path: str) -> tuple[Path, Path]:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目录路径必须是绝对路径")
    lexical_candidate = Path(os.path.abspath(candidate))
    matching_roots: list[Path] = []
    for configured in get_settings().allowed_media_roots:
        lexical_root = Path(os.path.abspath(configured.expanduser()))
        if is_relative_to(lexical_candidate, lexical_root):
            matching_roots.append(lexical_root)
    if not matching_roots:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目录不在允许的媒体根目录中")
    lexical_root = max(matching_roots, key=lambda item: len(item.parts))
    current = lexical_root
    if current.is_symlink():
        raise HTTPException(status.HTTP_409_CONFLICT, "不允许浏览符号链接目录")
    for part in lexical_candidate.relative_to(lexical_root).parts:
        current = current / part
        if current.is_symlink():
            raise HTTPException(status.HTTP_409_CONFLICT, "不允许浏览符号链接目录")
    if not lexical_candidate.exists() or not lexical_candidate.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "目录不存在")
    resolved_root = lexical_root.resolve(strict=True)
    resolved_candidate = lexical_candidate.resolve(strict=True)
    if not is_relative_to(resolved_candidate, resolved_root):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目录越界")
    if not os.access(resolved_candidate, os.R_OK | os.X_OK):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "目录不可读")
    return resolved_candidate, resolved_root


def list_media_directories(raw_path: str | None = None) -> dict[str, Any]:
    if raw_path is None:
        roots = _allowed_root_entries()
        return {
            "current": None,
            "parent_path": None,
            "entries": roots,
        }
    current, root = _safe_browsable_directory(raw_path)
    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(current) as children:
            for child in children:
                try:
                    is_symlink = child.is_symlink()
                    is_directory = child.is_dir(follow_symlinks=False)
                    if not is_directory and not (
                        is_symlink and child.is_dir(follow_symlinks=True)
                    ):
                        continue
                    child_path = Path(child.path)
                    readable = (
                        is_directory
                        and not is_symlink
                        and os.access(child_path, os.R_OK | os.X_OK)
                    )
                    entries.append(
                        {
                            "name": child.name,
                            "path": str(child_path),
                            "exists": True,
                            "readable": readable,
                            "writable": readable and os.access(child_path, os.W_OK),
                            "is_symlink": is_symlink,
                            "has_children": _has_child_directories(child_path)
                            if readable
                            else False,
                            "selectable": readable,
                            "is_root": False,
                        }
                    )
                except OSError:
                    continue
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "目录不可读") from exc
    entries.sort(key=lambda item: item["name"].casefold())
    parent_path = None if current == root else str(current.parent)
    return {
        "current": {
            "name": current.name or str(current),
            "path": str(current),
            "exists": True,
            "readable": True,
            "writable": os.access(current, os.W_OK),
            "is_symlink": False,
            "has_children": bool(entries),
            "selectable": True,
            "is_root": current == root,
        },
        "parent_path": parent_path,
        "entries": entries,
    }


def resolve_library_file(root_path: str, relative_path: str, *, must_exist: bool = True) -> Path:
    root = Path(root_path).resolve(strict=True)
    candidate = root.joinpath(relative_path).resolve(strict=must_exist)
    if not is_relative_to(candidate, root):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "媒体路径越界")
    if must_exist and candidate.is_symlink():
        raise HTTPException(status.HTTP_409_CONFLICT, "不处理符号链接")
    return candidate


def resolve_trash_file(relative_path: str, *, must_exist: bool = True) -> Path:
    root = get_settings().trash_dir.resolve(strict=True)
    candidate = root.joinpath(relative_path).resolve(strict=must_exist)
    if not is_relative_to(candidate, root):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Trash 路径越界")
    return candidate
