"""Request dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from m3.db import get_db
from m3.models import User
from m3.security import has_perm, read_session_token
from m3.config import get_settings


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    import uuid as _uuid

    token = request.cookies.get(get_settings().session_cookie)
    if not token:
        return None
    uid = read_session_token(token)
    if not uid:
        return None
    try:
        user = db.get(User, _uuid.UUID(uid))
    except (ValueError, TypeError):
        return None
    if not user or not user.is_active:
        return None
    return user


def require_user(user: User | None = Depends(current_user)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="login_required")
    return user


def require_perm(perm: str):
    def _inner(user: User = Depends(require_user)) -> User:
        if not has_perm(user.role, user.extra_permissions, perm):
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return _inner
