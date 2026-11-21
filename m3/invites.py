"""Invitation-only account registration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from m3.audit import record_audit
from m3.models import User, UserInvitation
from m3.security import generate_invite_token, hash_invite_token, hash_password


INVITE_TTL_DAYS = 14


def create_invitation(
    db: Session,
    *,
    email: str,
    role: str,
    actor: User,
    extra_permissions: dict[str, Any] | None = None,
) -> tuple[UserInvitation, str]:
    """Create pending invite; returns (row, raw_token) — raw_token shown once to owner."""
    email_n = email.lower().strip()
    if role not in {"supplier_manager", "contract_specialist"}:
        role = "contract_specialist"
    # Revoke prior pending invites for same email
    for old in db.query(UserInvitation).filter_by(email=email_n, status="pending").all():
        old.status = "revoked"
        db.add(old)
    raw = generate_invite_token()
    inv = UserInvitation(
        email=email_n,
        role=role,
        extra_permissions=extra_permissions or {},
        token_hash=hash_invite_token(raw),
        invited_by=actor.id,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS),
    )
    db.add(inv)
    db.flush()
    record_audit(
        db,
        action="user.invite_create",
        entity_type="invitation",
        entity_id=str(inv.id),
        actor_user_id=actor.id,
        actor_label=actor.email,
        after={"email": email_n, "role": role, "expires_at": inv.expires_at.isoformat()},
    )
    db.commit()
    db.refresh(inv)
    return inv, raw


def find_valid_invitation(db: Session, raw_token: str) -> UserInvitation | None:
    if not raw_token:
        return None
    th = hash_invite_token(raw_token)
    inv = db.query(UserInvitation).filter_by(token_hash=th).one_or_none()
    if not inv or inv.status != "pending":
        return None
    exp = inv.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        inv.status = "expired"
        db.add(inv)
        db.commit()
        return None
    return inv


def accept_invitation(
    db: Session,
    *,
    raw_token: str,
    full_name: str,
    password: str,
) -> User | None:
    inv = find_valid_invitation(db, raw_token)
    if not inv:
        return None
    existing = db.query(User).filter(User.email == inv.email).one_or_none()
    if existing:
        return None
    if len(password) < 10 or not full_name.strip():
        return None
    user = User(
        email=inv.email,
        full_name=full_name.strip(),
        password_hash=hash_password(password),
        role=inv.role,
        extra_permissions=inv.extra_permissions or {},
        is_active=True,
        is_primary_owner=False,
        session_version=1,
    )
    db.add(user)
    db.flush()
    inv.status = "accepted"
    inv.accepted_at = datetime.now(timezone.utc)
    inv.accepted_user_id = user.id
    db.add(inv)
    record_audit(
        db,
        action="user.invite_accept",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=user.id,
        actor_label=user.email,
        after={"email": user.email, "role": user.role, "invitation_id": str(inv.id)},
    )
    db.commit()
    db.refresh(user)
    return user


def bump_session_version(db: Session, user: User, *, actor: User | None = None, reason: str = "") -> None:
    user.session_version = int(user.session_version or 1) + 1
    db.add(user)
    record_audit(
        db,
        action="user.session_invalidate",
        entity_type="user",
        entity_id=str(user.id),
        actor_user_id=actor.id if actor else user.id,
        actor_label=actor.email if actor else user.email,
        after={"session_version": user.session_version, "reason": reason},
    )
