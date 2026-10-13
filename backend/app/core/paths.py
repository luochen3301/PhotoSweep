from __future__ import annotations

import os
import stat
from contextlib import contextmanager
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
                child.is_dir(follow_symlinks=False) and not child.is_symlink() for child in children
            )
    except OSError:
        return False


def _safe_browsable_directory(raw_path: str) -> tuple[Path, Path]:
    if "\x00" in raw_path:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目录路径无效")
    candidate = Path(raw_path).expanduser()
    if ".." in candidate.parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不允许目录路径穿越")
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
                    if not is_directory and not (is_symlink and child.is_dir(follow_symlinks=True)):
                        continue
                    child_path = Path(child.path)
                    readable = (
                        is_directory and not is_symlink and os.access(child_path, os.R_OK | os.X_OK)
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


def _resolve_confined_file(root: Path, relative_path: str, *, must_exist: bool) -> Path:
    # Inspect lexical components BEFORE resolve: resolve itself follows symlinks.
    relative = Path(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or relative.drive
        or ".." in relative.parts
        or "\x00" in relative_path
        or relative_path.startswith(("/", "\\"))
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件路径越界")
    root = Path(os.path.abspath(root))
    current = root
    for part in ("", *relative.parts):
        if part:
            current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise HTTPException(status.HTTP_409_CONFLICT, "不处理符号链接")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = current.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件或目录不存在") from exc
    if not is_relative_to(resolved, resolved_root) or resolved == resolved_root:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件路径越界")
    if must_exist and not stat.S_ISREG(current.stat(follow_symlinks=False).st_mode):
        raise HTTPException(status.HTTP_409_CONFLICT, "仅允许普通文件")
    return current


def resolve_library_file(root_path: str, relative_path: str, *, must_exist: bool = True) -> Path:
    root, _allowed = _safe_browsable_directory(root_path)
    return _resolve_confined_file(root, relative_path, must_exist=must_exist)


def resolve_trash_file(relative_path: str, *, must_exist: bool = True) -> Path:
    return _resolve_confined_file(get_settings().trash_dir, relative_path, must_exist=must_exist)


@contextmanager
def secure_parent(path: Path):
    """Pin Linux directory descriptors; never traverse a replaced symlink parent."""
    absolute = Path(os.path.abspath(path))
    if os.name != "posix":
        for parent in [*reversed(absolute.parents), absolute]:
            if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                raise OSError("不处理符号链接")
        yield None, str(absolute)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, absolute.name
    finally:
        os.close(descriptor)


@contextmanager
def open_regular_file(path: Path):
    with secure_parent(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("仅允许普通文件")
            with os.fdopen(descriptor, "rb", buffering=0, closefd=False) as handle:
                yield handle
        finally:
            os.close(descriptor)


def secure_mkdir_parents(path: Path) -> None:
    """Create descendants without following a symlink introduced between checks."""
    absolute = Path(os.path.abspath(path))
    if os.name != "posix":
        for current in [*reversed(absolute.parents), absolute]:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise OSError("不处理符号链接")
            current.mkdir(exist_ok=True)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
