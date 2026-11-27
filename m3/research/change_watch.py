"""Solicitation change-order / amendment watch + 30-day closed retention.

Goal
----
1. Catch government change orders / addenda / revisions the day they go live
   on a tracked solicitation (even after the original close date).
2. If a solicitation closes and no change appears, drop it from working files
   after 30 days — unless M3 bid on it (bid records are retained separately).

Not a historical warehouse. Does not activate the scheduler by itself.
Paid evidence (NextBid paid responses, etc.) is never hard-deleted here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from m3.models import Opportunity, PackageDocument, SolicitationPackage
from m3.research.deadline_retention import has_business_activity, retention_state

CLOSED_RETENTION_DAYS = 30
CHANGE_WATCH_OBJECTIVE = "solicitation_change_watch"

# Opportunity.status values that mean "we are/were in a bid path"
BID_PROTECTED_STATUSES = frozenset({"pursuing"})


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _utc().isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def package_doc_fingerprint(docs: list[dict[str, Any]] | list[PackageDocument]) -> dict[str, Any]:
    """Stable fingerprint of package document identity (URLs + hashes + amendments)."""
    rows: list[tuple[str, str, bool]] = []
    for d in docs or []:
        if isinstance(d, PackageDocument):
            url = (d.source_url or "").strip()
            ch = (d.content_hash or "").strip()
            is_am = bool(d.is_amendment)
        else:
            url = str(d.get("source_url") or d.get("url") or "").strip()
            ch = str(d.get("content_hash") or "").strip()
            is_am = bool(d.get("is_amendment"))
        if not url and not ch:
            continue
        rows.append((url.lower(), ch.lower(), is_am))
    rows.sort()
    url_blob = "\n".join(u for u, _, _ in rows)
    hash_blob = "\n".join(h for _, h, _ in rows if h)
    amend_urls = [u for u, _, a in rows if a]
    return {
        "doc_count": len(rows),
        "amendment_count": len(amend_urls),
        "url_set_hash": hashlib.sha256(url_blob.encode("utf-8")).hexdigest()[:32],
        "content_hash_set": hashlib.sha256(hash_blob.encode("utf-8")).hexdigest()[:32] if hash_blob else "",
        "amendment_url_set_hash": hashlib.sha256(
            "\n".join(amend_urls).encode("utf-8")
        ).hexdigest()[:32]
        if amend_urls
        else "",
        "urls": [u for u, _, _ in rows][:100],
    }


def compare_fingerprints(
    previous: dict[str, Any] | None, current: dict[str, Any] | None
) -> dict[str, Any]:
    """Return change detection result. Missing previous ⇒ baseline only (not a change)."""
    prev = previous or {}
    cur = current or {}
    if not prev.get("url_set_hash") and not prev.get("doc_count"):
        return {
            "changed": False,
            "baseline_only": True,
            "reasons": ["no_prior_fingerprint"],
            "previous": prev,
            "current": cur,
        }
    reasons: list[str] = []
    if prev.get("url_set_hash") != cur.get("url_set_hash"):
        reasons.append("document_url_set_changed")
    if (prev.get("amendment_count") or 0) != (cur.get("amendment_count") or 0):
        reasons.append("amendment_count_changed")
    if prev.get("amendment_url_set_hash") != cur.get("amendment_url_set_hash"):
        reasons.append("amendment_urls_changed")
    if (
        prev.get("content_hash_set")
        and cur.get("content_hash_set")
        and prev.get("content_hash_set") != cur.get("content_hash_set")
    ):
        reasons.append("downloaded_content_hash_changed")
    # New URLs not in previous
    prev_urls = set(prev.get("urls") or [])
    cur_urls = set(cur.get("urls") or [])
    added = sorted(cur_urls - prev_urls)
    removed = sorted(prev_urls - cur_urls)
    if added:
        reasons.append("new_document_urls")
    if removed:
        reasons.append("removed_document_urls")
    return {
        "changed": bool(reasons),
        "baseline_only": False,
        "reasons": reasons,
        "added_urls": added[:50],
        "removed_urls": removed[:50],
        "previous": {
            "doc_count": prev.get("doc_count"),
            "amendment_count": prev.get("amendment_count"),
            "url_set_hash": prev.get("url_set_hash"),
        },
        "current": {
            "doc_count": cur.get("doc_count"),
            "amendment_count": cur.get("amendment_count"),
            "url_set_hash": cur.get("url_set_hash"),
        },
    }


def is_bid_protected(opportunity: Opportunity | dict[str, Any] | None, *, nb_id: str | None = None) -> bool:
    """Contracts we bid on are retained differently — never auto-purged here."""
    if opportunity is None and not nb_id:
        return False
    if isinstance(opportunity, Opportunity):
        status = (opportunity.status or "").lower()
        raw = opportunity.raw_evidence or {}
        external = opportunity.external_key or ""
    else:
        status = str((opportunity or {}).get("status") or "").lower()
        raw = (opportunity or {}).get("raw_evidence") or {}
        external = str((opportunity or {}).get("external_key") or (opportunity or {}).get("nb_id") or "")
    if status in BID_PROTECTED_STATUSES:
        return True
    if raw.get("bid_submitted") or raw.get("we_bid") or raw.get("bid_record"):
        return True
    if nb_id and has_business_activity(str(nb_id)):
        return True
    # external_key often nb_id / source key
    if external and has_business_activity(external):
        return True
    if external.startswith("NB") and has_business_activity(external):
        return True
    return False


def mark_bid_submitted(opportunity: Opportunity, *, note: str | None = None) -> None:
    """Owner/path marks that M3 bid — protects from 30-day purge."""
    raw = dict(opportunity.raw_evidence or {})
    raw["bid_submitted"] = True
    raw["bid_submitted_at"] = _utc_iso()
    if note:
        raw["bid_submitted_note"] = note[:500]
    opportunity.raw_evidence = raw
    if (opportunity.status or "").lower() in {"saved", "archived", "not_interested", ""}:
        opportunity.status = "pursuing"


def close_plus_retention_deadline(
    deadline_at: Any, *, last_change_at: Any = None, days: int = CLOSED_RETENTION_DAYS
) -> datetime | None:
    """Purge eligible after max(close, last_change) + days."""
    close = _parse_dt(deadline_at)
    change = _parse_dt(last_change_at)
    anchors = [d for d in (close, change) if d is not None]
    if not anchors:
        return None
    return max(anchors) + timedelta(days=int(days))


def retention_30d_decision(
    *,
    deadline_at: Any,
    now: datetime | None = None,
    bid_protected: bool = False,
    change_unresolved: bool = False,
    last_change_at: Any = None,
) -> dict[str, Any]:
    """Decide whether working files may leave active inventory after close+30d."""
    now = now or _utc()
    # Still use 48h gate for *actionable queue* removal
    item = {"deadline_at": deadline_at}
    early = retention_state(item, now=now)

    purge_after = close_plus_retention_deadline(deadline_at, last_change_at=last_change_at)
    if bid_protected:
        return {
            "actionable": early.get("actionable"),
            "purge_eligible": False,
            "reason": "bid_protected_retain_separately",
            "purge_after": purge_after.isoformat() if purge_after else None,
            "closed_retention_days": CLOSED_RETENTION_DAYS,
            "early_48h": early,
        }
    if change_unresolved:
        return {
            "actionable": early.get("actionable"),
            "purge_eligible": False,
            "reason": "unresolved_change_detected_keep_until_acknowledged",
            "purge_after": purge_after.isoformat() if purge_after else None,
            "closed_retention_days": CLOSED_RETENTION_DAYS,
            "early_48h": early,
        }
    if not purge_after:
        return {
            "actionable": early.get("actionable"),
            "purge_eligible": False,
            "reason": "deadline_unknown_do_not_auto_purge",
            "purge_after": None,
            "closed_retention_days": CLOSED_RETENTION_DAYS,
            "early_48h": early,
        }
    eligible = now >= purge_after
    return {
        "actionable": early.get("actionable"),
        "purge_eligible": eligible,
        "reason": "past_close_plus_30d_no_change" if eligible else "within_30d_or_still_open",
        "purge_after": purge_after.isoformat(),
        "closed_retention_days": CLOSED_RETENTION_DAYS,
        "early_48h": early,
    }


def apply_change_event_to_package(
    pkg: SolicitationPackage,
    comparison: dict[str, Any],
    *,
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    """Persist watch state on package.amendment_summary."""
    summary = dict(pkg.amendment_summary or {})
    watch = dict(summary.get("change_watch") or {})
    watch["fingerprint"] = {
        "doc_count": fingerprint.get("doc_count"),
        "amendment_count": fingerprint.get("amendment_count"),
        "url_set_hash": fingerprint.get("url_set_hash"),
        "content_hash_set": fingerprint.get("content_hash_set"),
        "amendment_url_set_hash": fingerprint.get("amendment_url_set_hash"),
        "urls": fingerprint.get("urls") or [],
    }
    watch["last_checked_at"] = _utc_iso()
    if comparison.get("baseline_only"):
        watch.setdefault("baseline_at", _utc_iso())
        summary["change_watch"] = watch
        pkg.amendment_summary = summary
        return {"status": "baseline_recorded", "changed": False}
    if comparison.get("changed"):
        watch["change_detected"] = True
        watch["change_unresolved"] = True
        watch["last_change_at"] = _utc_iso()
        watch["last_change_reasons"] = comparison.get("reasons") or []
        watch["added_urls"] = comparison.get("added_urls") or []
        watch["removed_urls"] = comparison.get("removed_urls") or []
        summary["change_watch"] = watch
        summary["invalidates_prior_approvals"] = True
        pkg.amendment_summary = summary
        return {
            "status": "CHANGE_DETECTED",
            "changed": True,
            "reasons": comparison.get("reasons"),
            "added_urls": comparison.get("added_urls"),
        }
    watch["change_detected"] = bool(watch.get("change_unresolved"))
    summary["change_watch"] = watch
    pkg.amendment_summary = summary
    return {"status": "unchanged", "changed": False}


def acknowledge_change(pkg: SolicitationPackage) -> None:
    """Owner/process acknowledged the change — allows 30d clock to proceed from last_change."""
    summary = dict(pkg.amendment_summary or {})
    watch = dict(summary.get("change_watch") or {})
    watch["change_unresolved"] = False
    watch["acknowledged_at"] = _utc_iso()
    summary["change_watch"] = watch
    pkg.amendment_summary = summary


def scan_package_for_changes(
    db: Session,
    pkg: SolicitationPackage,
    *,
    live_docs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare current package docs (or provided live doc list) to last fingerprint.

    Pass live_docs from a fresh listing/detail discover when available.
    If live_docs is None, fingerprints the stored package only (baseline / self-check).
    """
    stored = package_doc_fingerprint(list(pkg.documents or []))
    summary = dict(pkg.amendment_summary or {})
    watch = dict(summary.get("change_watch") or {})
    previous = watch.get("fingerprint")
    current = package_doc_fingerprint(live_docs) if live_docs is not None else stored
    comparison = compare_fingerprints(previous, current)
    result = apply_change_event_to_package(pkg, comparison, fingerprint=current)
    pkg.last_verified_at = _utc()
    db.add(pkg)
    return {
        "package_id": str(pkg.id),
        "solicitation_number": pkg.solicitation_number,
        "source_key": pkg.source_key,
        **result,
        "comparison": comparison,
    }


def soft_purge_opportunity(
    db: Session,
    opportunity: Opportunity,
    *,
    reason: str,
) -> dict[str, Any]:
    """Remove from active working set without destroying paid evidence.

    - opportunity.status → archived
    - raw_evidence records purge metadata
    - does NOT delete NextBid paid response archives
    - free package working files may be marked purge_eligible in amendment_summary
    """
    if is_bid_protected(opportunity):
        return {"purged": False, "reason": "bid_protected"}
    raw = dict(opportunity.raw_evidence or {})
    raw["purged_after_close"] = True
    raw["purged_at"] = _utc_iso()
    raw["purge_reason"] = reason
    opportunity.raw_evidence = raw
    opportunity.status = "archived"
    db.add(opportunity)

    pkgs = (
        db.query(SolicitationPackage)
        .filter(SolicitationPackage.opportunity_id == opportunity.id)
        .all()
    )
    for pkg in pkgs:
        summary = dict(pkg.amendment_summary or {})
        summary["purge_eligible"] = True
        summary["purged_at"] = _utc_iso()
        summary["purge_reason"] = reason
        # Preserve fingerprint for identity / re-open if change appears later
        pkg.amendment_summary = summary
        pkg.package_status = "ARCHIVED_AFTER_CLOSE"
        db.add(pkg)
    return {
        "purged": True,
        "opportunity_id": str(opportunity.id),
        "packages_marked": len(pkgs),
        "reason": reason,
        "paid_evidence_deleted": False,
    }


def evaluate_opportunity_retention(db: Session, opportunity: Opportunity) -> dict[str, Any]:
    pkgs = (
        db.query(SolicitationPackage)
        .filter(SolicitationPackage.opportunity_id == opportunity.id)
        .all()
    )
    unresolved = False
    last_change = None
    for pkg in pkgs:
        watch = ((pkg.amendment_summary or {}).get("change_watch") or {})
        if watch.get("change_unresolved"):
            unresolved = True
        lc = watch.get("last_change_at")
        if lc and (last_change is None or str(lc) > str(last_change)):
            last_change = lc
    decision = retention_30d_decision(
        deadline_at=opportunity.deadline_at,
        bid_protected=is_bid_protected(opportunity),
        change_unresolved=unresolved,
        last_change_at=last_change,
    )
    return {
        "opportunity_id": str(opportunity.id),
        "solicitation_number": opportunity.solicitation_number,
        "status": opportunity.status,
        **decision,
    }


def run_closed_retention_pass(
    db: Session, *, dry_run: bool = True, limit: int | None = 500
) -> dict[str, Any]:
    """Soft-purge closed opportunities past 30 days with no unresolved change / no bid."""
    q = db.query(Opportunity).filter(Opportunity.status.notin_(["archived", "not_interested"]))
    if limit is not None:
        q = q.limit(int(limit))
    rows = q.all()
    eligible: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    purged: list[dict[str, Any]] = []
    for opp in rows:
        decision = evaluate_opportunity_retention(db, opp)
        if decision.get("purge_eligible"):
            eligible.append(decision)
            if not dry_run:
                purged.append(
                    soft_purge_opportunity(
                        db, opp, reason=str(decision.get("reason") or "past_close_plus_30d_no_change")
                    )
                )
        else:
            kept.append(
                {
                    "opportunity_id": decision["opportunity_id"],
                    "reason": decision.get("reason"),
                    "purge_after": decision.get("purge_after"),
                }
            )
    if not dry_run and purged:
        db.commit()
    return {
        "ok": True,
        "dry_run": dry_run,
        "scanned": len(rows),
        "purge_eligible": len(eligible),
        "purged": len(purged),
        "kept": len(kept),
        "eligible_sample": eligible[:20],
        "note": (
            "Soft archive only. Bid-protected and unresolved-change records kept. "
            "Paid evidence never deleted. Scheduler not activated."
        ),
    }


def watch_state_public(pkg: SolicitationPackage | None) -> dict[str, Any]:
    if not pkg:
        return {"watching": False}
    watch = ((pkg.amendment_summary or {}).get("change_watch") or {})
    return {
        "watching": bool(watch.get("fingerprint")),
        "change_detected": bool(watch.get("change_detected") or watch.get("change_unresolved")),
        "change_unresolved": bool(watch.get("change_unresolved")),
        "last_checked_at": watch.get("last_checked_at"),
        "last_change_at": watch.get("last_change_at"),
        "last_change_reasons": watch.get("last_change_reasons") or [],
        "added_urls": watch.get("added_urls") or [],
    }
