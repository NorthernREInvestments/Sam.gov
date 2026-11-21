"""Password hashing, sessions, permissions."""

from __future__ import annotations

import secrets
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext

from m3.config import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Role → default permission keys
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {
        "users.manage",
        "audit.read",
        "opportunities.read",
        "opportunities.write",
        "opportunities.search",
        "suppliers.read",
        "suppliers.write",
        "suppliers.search",
        "catalogs.write",
        "catalogs.read",
        "tasks.read",
        "tasks.write",
        "tasks.assign",
        "settings.manage",
        "research.run",
    },
    "supplier_manager": {
        "opportunities.read",
        "suppliers.read",
        "suppliers.write",
        "suppliers.search",
        "catalogs.write",
        "catalogs.read",
        "tasks.read",
        "tasks.write",
        "research.run",
    },
    "contract_specialist": {
        "opportunities.read",
        "opportunities.write",
        "opportunities.search",
        "suppliers.read",
        "tasks.read",
        "tasks.write",
        "research.run",
        "catalogs.read",
    },
    "custom": set(),
}


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


def serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().resolved_secret(), salt="m3-session-v1")


def issue_session_token(user_id: str) -> str:
    return serializer().dumps({"uid": user_id, "nonce": secrets.token_hex(8)})


def read_session_token(token: str, max_age: int | None = None) -> str | None:
    settings = get_settings()
    try:
        data = serializer().loads(token, max_age=max_age or settings.session_max_age_seconds)
        return str(data.get("uid") or "") or None
    except (BadSignature, SignatureExpired, Exception):
        return None


def effective_permissions(role: str, extra: dict[str, Any] | None = None) -> set[str]:
    perms = set(ROLE_PERMISSIONS.get(role, set()))
    extra = extra or {}
    for k, v in extra.items():
        if v is True:
            perms.add(k)
        elif v is False:
            perms.discard(k)
    return perms


def has_perm(role: str, extra: dict[str, Any] | None, needed: str) -> bool:
    return needed in effective_permissions(role, extra)


def is_primary_owner(user: Any) -> bool:
    """Only the original bootstrap owner may manage users. Not role-delegable."""
    return bool(getattr(user, "is_primary_owner", False) and getattr(user, "is_active", False))
