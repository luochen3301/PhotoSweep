from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database import get_db
from app.models import AdminAccount, SessionToken

SESSION_COOKIE = "photosweep_session"
CSRF_COOKIE = "photosweep_csrf"
password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_session(db: Session, admin: AdminAccount, response: Response) -> str:
    settings = get_settings()
    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.session_days)
    db.add(
        SessionToken(
            admin_id=admin.id,
            token_hash=token_hash(raw_token),
            csrf_hash=token_hash(csrf_token),
            expires_at=expires,
        )
    )
    db.commit()
    cookie_kwargs = {
        "max_age": settings.session_days * 86400,
        "secure": settings.cookie_secure,
        "samesite": "strict",
        "path": "/",
    }
    response.set_cookie(SESSION_COOKIE, raw_token, httponly=True, **cookie_kwargs)
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, **cookie_kwargs)
    return csrf_token


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def current_session(
    request: Request,
    db: Session = Depends(get_db),
) -> tuple[SessionToken, AdminAccount]:
    raw_token = request.cookies.get(SESSION_COOKIE)
    if not raw_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "需要登录")
    session = db.scalar(
        select(SessionToken).where(SessionToken.token_hash == token_hash(raw_token))
    )
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话无效")
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        db.delete(session)
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话已过期")
    admin = db.get(AdminAccount, session.admin_id)
    if admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "管理员不存在")
    now = datetime.now(timezone.utc)
    seen = session.last_seen_at.replace(tzinfo=timezone.utc)
    if now - seen >= timedelta(minutes=5):
        session.last_seen_at = now
        db.commit()
    return session, admin


def require_admin(
    session_and_admin: tuple[SessionToken, AdminAccount] = Depends(current_session),
) -> AdminAccount:
    return session_and_admin[1]


def require_csrf(
    request: Request,
    session_and_admin: tuple[SessionToken, AdminAccount] = Depends(current_session),
) -> AdminAccount:
    session, admin = session_and_admin
    supplied = request.headers.get("X-CSRF-Token")
    cookie_value = request.cookies.get(CSRF_COOKIE)
    if (
        not supplied
        or not cookie_value
        or not secrets.compare_digest(supplied, cookie_value)
        or not secrets.compare_digest(token_hash(supplied), session.csrf_hash)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF 验证失败")
    return admin


def revoke_current_session(request: Request, db: Session) -> None:
    raw_token = request.cookies.get(SESSION_COOKIE)
    if raw_token:
        db.execute(delete(SessionToken).where(SessionToken.token_hash == token_hash(raw_token)))
        db.commit()


class LoginLimiter:
    def __init__(self, attempts: int = 5, window_seconds: int = 900) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._records: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            for old_key in list(self._records):
                if (
                    not self._records[old_key]
                    or now - self._records[old_key][-1] > self.window_seconds
                ):
                    del self._records[old_key]
            if key not in self._records and len(self._records) >= 10000:
                raise HTTPException(429, "请求过多，请稍后再试")
            queue = self._records[key]
            while queue and now - queue[0] > self.window_seconds:
                queue.popleft()
            if len(queue) >= self.attempts:
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "登录尝试过多，请稍后再试")
            # Reserve before expensive password verification; concurrent requests count too.
            queue.append(now)

    def fail(self, key: str) -> None:
        pass  # check() already records the attempt atomically.

    def success(self, key: str) -> None:
        with self._lock:
            self._records.pop(key, None)


login_limiter = LoginLimiter()
reauth_limiter = LoginLimiter()
