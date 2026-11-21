"""Invitation-only account registration and email-change activation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from m3.audit import record_audit
from m3.models import AuditEvent, Opportunity, Supplier, Task, User, UserInvitation
from m3.security import generate_invite_token, hash_invite_token, hash_password


INVITE_TTL_DAYS = 14


def create_invitation(
    db: Session,
    *,
    email: str,
    role: str,
    actor: User,
    full_name: str | None = None,
    extra_permissions: dict[str, Any] | None = None,
    purpose: str = "new_user",
    target_user_id=None,
) -> tuple[UserInvitation, str]:
    """Create pending invite; returns (row, raw_token) — raw_token shown once to owner."""
    email_n = email.lower().strip()
    if role not in {"supplier_manager", "contract_specialist"}:
        role = "contract_specialist"
    purpose = purpose if purpose in {"new_user", "email_change"} else "new_user"

    if purpose == "new_user":
        existing = db.query(User).filter(User.email == email_n, User.team_status == "active").one_or_none()
        if existing and existing.is_active:
            raise ValueError("An active team member already uses this email.")

    for old in db.query(UserInvitation).filter_by(email=email_n, status="pending").all():
        old.status = "revoked"
        db.add(old)

    raw = generate_invite_token()
    inv = UserInvitation(
        email=email_n,
        invited_name=(full_name or "").strip() or None,
        role=role,
        extra_permissions=extra_permissions or {},
        purpose=purpose,
        target_user_id=target_user_id,
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
        after={
            "email": email_n,
            "role": role,
            "purpose": purpose,
            "name": inv.invited_name,
            "expires_at": inv.expires_at.isoformat(),
        },
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
    if len(password) < 10 or not full_name.strip():
        return None

    # Email-change activation for an existing user id
    if inv.purpose == "email_change" and inv.target_user_id:
        user = db.get(User, inv.target_user_id)
        if not user or user.is_primary_owner:
            return None
        before = {"email": user.email, "pending_email": user.pending_email}
        user.email = inv.email
        user.pending_email = None
        user.is_active = True
        user.team_status = "active"
        user.full_name = full_name.strip() or user.full_name
        user.password_hash = hash_password(password)
        user.session_version = int(user.session_version or 1) + 1
        db.add(user)
        inv.status = "accepted"
        inv.accepted_at = datetime.now(timezone.utc)
        inv.accepted_user_id = user.id
        db.add(inv)
        record_audit(
            db,
            action="user.email_change_complete",
            entity_type="user",
            entity_id=str(user.id),
            actor_user_id=user.id,
            actor_label=user.email,
            before=before,
            after={"email": user.email},
        )
        db.commit()
        db.refresh(user)
        return user

    existing = db.query(User).filter(User.email == inv.email).one_or_none()
    if existing:
        # Revive archived / inactive non-owner accounts instead of duplicating email
        if existing.is_primary_owner or (
            (existing.team_status or "active") == "active" and existing.is_active
        ):
            return None
        existing.full_name = full_name.strip() or existing.full_name
        existing.password_hash = hash_password(password)
        existing.role = inv.role
        existing.extra_permissions = inv.extra_permissions or {}
        existing.is_active = True
        existing.team_status = "active"
        existing.pending_email = None
        existing.session_version = int(existing.session_version or 1) + 1
        db.add(existing)
        inv.status = "accepted"
        inv.accepted_at = datetime.now(timezone.utc)
        inv.accepted_user_id = existing.id
        db.add(inv)
        record_audit(
            db,
            action="user.invite_accept",
            entity_type="user",
            entity_id=str(existing.id),
            actor_user_id=existing.id,
            actor_label=existing.email,
            after={
                "email": existing.email,
                "role": existing.role,
                "invitation_id": str(inv.id),
                "reactivated": True,
            },
        )
        db.commit()
        db.refresh(existing)
        return existing

    user = User(
        email=inv.email,
        full_name=full_name.strip(),
        password_hash=hash_password(password),
        role=inv.role,
        extra_permissions=inv.extra_permissions or {},
        is_active=True,
        is_primary_owner=False,
        session_version=1,
        team_status="active",
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


def user_has_business_activity(db: Session, user_id) -> bool:
    """True when the user is tied to real business records (not routine account audits)."""
    if db.query(Opportunity).filter(
        (Opportunity.saved_by == user_id) | (Opportunity.assigned_to == user_id)
    ).limit(1).first():
        return True
    if db.query(Supplier).filter(
        (Supplier.created_by == user_id) | (Supplier.assigned_to == user_id)
    ).limit(1).first():
        return True
    if db.query(Task).filter(
        (Task.created_by == user_id) | (Task.assigned_to == user_id)
    ).limit(1).first():
        return True
    # Non-account audit actions (research, opportunity, supplier, etc.)
    if (
        db.query(AuditEvent)
        .filter(
            AuditEvent.actor_user_id == user_id,
            ~AuditEvent.action.like("user.%"),
        )
        .limit(1)
        .first()
    ):
        return True
    return False


def detach_user_for_safe_delete(db: Session, user: User) -> None:
    """Null account FKs so an unused test user can be deleted while keeping audit rows."""
    uid = user.id
    for ev in db.query(AuditEvent).filter(AuditEvent.actor_user_id == uid).all():
        if not ev.actor_label:
            ev.actor_label = user.email or str(uid)
        ev.actor_user_id = None
        db.add(ev)
    for inv in db.query(UserInvitation).filter(
        (UserInvitation.target_user_id == uid)
        | (UserInvitation.accepted_user_id == uid)
        | (UserInvitation.invited_by == uid)
    ).all():
        if inv.target_user_id == uid:
            inv.target_user_id = None
        if inv.accepted_user_id == uid:
            inv.accepted_user_id = None
        if inv.invited_by == uid:
            inv.invited_by = None
        db.add(inv)


def is_protected_samantha(user: User) -> bool:
    """Soft-protect Samantha accounts from accidental removal UX (owner can still confirm)."""
    name = (user.full_name or "").lower()
    email = (user.email or "").lower()
    return "samantha" in name or email.startswith("samantha@") or email.startswith("sam.supplier@")
