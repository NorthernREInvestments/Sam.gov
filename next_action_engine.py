"""Deterministic next-action engine and Today / action queue (no AI priority)."""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date, datetime, timezone
from typing import Any

from bom_gate import BOM_COMPLETE
from deal_readiness import BID_READY, DEAL_READY
from missing_info import (
    CO_CLARIFICATION_CANDIDATE,
    CO_CLARIFICATION_REQUIRED,
    EXTERNAL_REFERENCE_REQUIRED,
    MISSING_CONFIRMED_LOCAL,
    POSSIBLE_MATCH_FOUND,
    SEARCHING_LOCAL_PACKAGE,
)
from solicitation_package import PACKAGE_COMPLETE, PACKAGE_INCOMPLETE, PACKAGE_UNRESOLVED

ACTION_OPEN = "OPEN"
ACTION_DONE = "DONE"
ACTION_SNOOZED = "SNOOZED"
ACTION_BLOCKED = "BLOCKED"


def _priority_score(
    *,
    bid_deadline: date | None,
    clarification_deadline: date | None,
    followup_due: date | None,
    is_blocking: bool,
    today: date,
    runway_days: int | None = None,
    viability: str | None = None,
    blocker: str = "generic",
) -> tuple[int, str]:
    """Lower score = higher priority. Deterministic."""
    score = 500
    reason = "default"
    if bid_deadline:
        days = (bid_deadline - today).days
        if days < 0:
            score = 5
            reason = "bid_deadline_passed"
        elif days <= 2:
            score = 10
            reason = "bid_deadline_imminent"
        elif days <= 7:
            score = 30
            reason = "bid_deadline_within_week"
        else:
            score = 80 + min(days, 100)
            reason = "bid_deadline"
    if clarification_deadline:
        days = (clarification_deadline - today).days
        if 0 <= days <= 3:
            score = min(score, 15)
            reason = "clarification_deadline_imminent"
    if followup_due and followup_due <= today:
        score = min(score, 20)
        reason = "followup_due"
    if is_blocking:
        score = min(score, score - 5 if score > 10 else score)
        reason = reason + "+blocking" if reason != "default" else "blocking_work"

    # Deadline viability pressure (product-resale floors)
    try:
        from discovery.deadline_viability import apply_deadline_to_action_priority

        adj = apply_deadline_to_action_priority(
            score,
            viability=viability,
            runway_days=runway_days if runway_days is not None else (
                (bid_deadline - today).days if bid_deadline else None
            ),
            blocker=blocker,
        )
        if adj["priority"] < score:
            score = adj["priority"]
            reason = f"{reason}+{adj['urgency']}:{adj['reason']}"
    except Exception:
        pass

    return max(1, score), reason


def generate_next_actions(
    *,
    workspace: dict[str, Any] | None = None,
    missing_items: list[dict[str, Any]] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """
    Operator-facing next actions — no Stage 0–3 jargon required.
    """
    ws = workspace or {}
    as_of = today or today_local()
    actions: list[dict[str, Any]] = []
    opp = ws.get("opportunity") or {}
    deal_id = opp.get("id")
    due = None
    if opp.get("due_date"):
        try:
            due = date.fromisoformat(str(opp["due_date"])[:10])
        except ValueError:
            due = None

    runway_days = None
    viability = None
    try:
        from discovery.deadline_viability import compute_deadline_runway

        rw = compute_deadline_runway(
            response_deadline=opp.get("response_deadline") or opp.get("due_date"),
            deadline_raw=opp.get("deadline_raw"),
            deadline_timezone=opp.get("deadline_timezone"),
            deadline_tz_confidence=opp.get("deadline_tz_confidence"),
            now=datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc),
        )
        runway_days = rw.get("deadline_runway_days")
        viability = rw.get("deadline_viability")
    except Exception:
        if due:
            runway_days = (due - as_of).days

    pkg = (ws.get("solicitation_package") or {}).get("status")
    bom_gate = ws.get("bom_gate") or {}
    deal_rd = ws.get("deal_readiness") or {}
    bid_rd = ws.get("bid_readiness") or {}
    pursuits = ws.get("pursuit_plans") or []

    # Commercial execution override (quote-first unless CO deadline elevates)
    try:
        from commercial_next_action import commercial_primary_action

        commercial_first = commercial_primary_action(
            workspace=ws, missing_items=missing_items, today=as_of
        )
    except Exception:
        commercial_first = None

    def add(
        action: str,
        *,
        why: str,
        priority: int,
        organization: str | None = None,
        contact: str | None = None,
        due_at: date | None = None,
        status: str = ACTION_OPEN,
        blocking: bool = True,
        kind: str = "WORK",
    ) -> None:
        actions.append(
            {
                "priority": priority,
                "deadline": due.isoformat() if due else None,
                "opportunity_id": deal_id,
                "organization": organization,
                "contact": contact,
                "action": action,
                "why": why,
                "due_at": (due_at or due).isoformat() if (due_at or due) else None,
                "status": status,
                "blocking": blocking,
                "kind": kind,
            }
        )

    # Package incomplete first — highest operational priority (beats all other blockers)
    if pkg in {PACKAGE_INCOMPLETE, PACKAGE_UNRESOLVED}:
        add(
            "Acquire missing solicitation documents before CO questions or final quotes.",
            why=f"Package status is {pkg}",
            priority=5,
            blocking=True,
            kind="PACKAGE",
        )

    # Missing info items
    for item in missing_items or []:
        st = item.get("status")
        gate = item.get("co_gate") or {}
        fact = item.get("description") or item.get("fact_key")
        if st == POSSIBLE_MATCH_FOUND:
            # Do not outrank package acquisition or commercial quote-first path
            if pkg in {PACKAGE_INCOMPLETE, PACKAGE_UNRESOLVED}:
                base = 25
            elif commercial_first and commercial_first.get("kind") in {"SUPPLIER", "COMMERCIAL"}:
                base = 45  # commercial viability first for nonfatal FOB-like possibles
            else:
                base = 12
            add(
                f"Review possible match for: {fact}",
                why="Possible/indirect match found — do not escalate to CO yet",
                priority=base,
                kind="MISSING_INFO",
                blocking=False,
            )
        elif st in {MISSING_CONFIRMED_LOCAL, SEARCHING_LOCAL_PACKAGE} and pkg not in {
            PACKAGE_COMPLETE,
        }:
            pr, _ = _priority_score(bid_deadline=due, clarification_deadline=None, followup_due=None, is_blocking=True, today=as_of)
            add(
                f"Resolve missing configuration after package completeness: {fact}",
                why="Local search incomplete or package unresolved",
                priority=pr,
                kind="MISSING_INFO",
            )
        elif gate.get("safe_to_ask_co") or st in {CO_CLARIFICATION_CANDIDATE, CO_CLARIFICATION_REQUIRED}:
            # Keep on queue but do not outrank OBTAIN SUPPLIER QUOTE unless commercial engine elevates
            pr, _ = _priority_score(bid_deadline=due, clarification_deadline=None, followup_due=None, is_blocking=True, today=as_of)
            co_priority = max(40, pr) if not (commercial_first and commercial_first.get("kind") == "CO_CLARIFICATION") else max(1, pr - 2)
            add(
                f"Review drafted CO clarification: {fact}",
                why=f"Confidence absent={gate.get('confidence_absent')}; Safe to ask={gate.get('safe_to_ask_co')}; READY BUT NOT SENT unless deadline elevates",
                priority=co_priority,
                kind="CO_CLARIFICATION",
                blocking=False,
            )
        elif st == EXTERNAL_REFERENCE_REQUIRED:
            route = gate.get("route") or item.get("fact_class")
            if route in {"COMMERCIAL", "MANUFACTURER"} or "COMMERCIAL" in str(item.get("fact_class")):
                add(
                    f"Pursue commercial/manufacturer source (not CO): {fact}",
                    why="Not a CO solicitation fact",
                    priority=60,
                    blocking=False,
                    kind="EXTERNAL",
                )
            elif "FINANCING" in str(route) or "FINANCING" in str(item.get("fact_class")):
                add(
                    "Contact financing provider to resolve hard requirements.",
                    why="Financing fact — not CO",
                    priority=40,
                    kind="FINANCING",
                )

    # BOM incomplete
    if bom_gate.get("status") != BOM_COMPLETE:
        unknowns = bom_gate.get("unknown_components") or []
        pr, _ = _priority_score(bid_deadline=due, clarification_deadline=None, followup_due=None, is_blocking=True, today=as_of)
        add(
            "Resolve missing Dell server configuration before requesting quotes."
            if unknowns
            else "Complete BOM before supplier quote requests.",
            why=f"BOM gate={bom_gate.get('status')}; unknowns={unknowns}",
            priority=pr,
            kind="BOM",
        )

    # Quote ready (lower priority than commercial_primary OBTAIN SUPPLIER QUOTE)
    if bom_gate.get("supplier_quote_request_ready") and pursuits and not commercial_first:
        for p in sorted(pursuits, key=lambda x: x.get("priority") or 99)[:2]:
            add(
                "Call supplier for quote (operator-initiated).",
                why="BOM quote-request ready",
                priority=35,
                organization=str(p.get("supplier_id")),
                kind="SUPPLIER",
            )

    # Follow-ups from activities
    for a in ws.get("activities") or []:
        nad = a.get("next_action_date")
        if nad:
            try:
                d = date.fromisoformat(str(nad)[:10])
            except ValueError:
                continue
            if d <= as_of and a.get("next_action"):
                pr, _ = _priority_score(
                    bid_deadline=due, clarification_deadline=None, followup_due=d, is_blocking=False, today=as_of
                )
                add(
                    a.get("next_action") or "Follow up",
                    why=f"Follow-up due {d.isoformat()}",
                    priority=pr,
                    due_at=d,
                    kind="FOLLOWUP",
                )

    # Financing — after package/BOM blockers (priority softer).
    # Quote-first: unresolved commercial quote owns urgency; financing stays behind it.
    fin = ws.get("financing") or {}
    if str(fin.get("status") or "").upper() in {"FINANCING_UNRESOLVED", "UNRESOLVED", ""}:
        if deal_rd.get("deal_ready") is not True:
            commercial_owns = bool(
                commercial_first and commercial_first.get("kind") in {"SUPPLIER", "COMMERCIAL"}
            )
            if commercial_owns:
                pr, why = 55, "financing_after_commercial_quote"
            else:
                pr, why = _priority_score(
                    bid_deadline=due,
                    clarification_deadline=None,
                    followup_due=None,
                    is_blocking=True,
                    today=as_of,
                    runway_days=runway_days,
                    viability=viability,
                    blocker="funding_unresolved",
                )
            add(
                "Resolve financing hard requirements with provider (no PG / no personal credit / $0 cash).",
                why=f"Financing unresolved blocks Deal Ready ({why})",
                priority=pr,
                kind="FINANCING",
            )

    # Deal ready but bid incomplete
    if deal_rd.get("status") == DEAL_READY and bid_rd.get("status") != BID_READY:
        blockers = bid_rd.get("blockers") or ["required bid items"]
        add(
            f"Complete required bid item: {blockers[0]}",
            why="Deal Ready but Bid not ready",
            priority=25,
            kind="BID",
        )

    if bid_rd.get("status") == BID_READY:
        pr, why = _priority_score(
            bid_deadline=due,
            clarification_deadline=None,
            followup_due=None,
            is_blocking=False,
            today=as_of,
            runway_days=runway_days,
            viability=viability,
            blocker="bid_ready_submit",
        )
        add(
            "Final operator review / submission preparation.",
            why=f"Bid Ready ({why})",
            priority=pr,
            kind="SUBMISSION",
            blocking=False,
        )

    # Post-award lifecycle actions
    award_lc = ws.get("award_lifecycle") or {}
    lifecycle_status = award_lc.get("lifecycle_status") or ws.get("lifecycle")
    if lifecycle_status in {"SUBMITTED", "AWARDED", "PERFORMING", "INVOICED", "PAID"}:
        try:
            from award_lifecycle import post_award_next_action

            post = post_award_next_action(award_lc)
            if post.get("action") and "closed" not in str(post.get("action")).lower():
                add(
                    post["action"],
                    why=post.get("why") or "Post-award workflow",
                    priority=post.get("priority", 18),
                    kind=post.get("kind", "AWARD"),
                    blocking=True,
                )
        except Exception:
            pass

    # Critical warnings -> today actions
    for w in ws.get("warnings") or []:
        if w.get("severity") in {"CRITICAL", "HIGH"} and w.get("status") == "ACTIVE":
            add(
                w.get("recommended_next_action") or w.get("message") or "Resolve deal warning",
                why=w.get("why_it_matters") or w.get("message"),
                priority=8 if w.get("severity") == "CRITICAL" else 22,
                kind="WARNING",
                blocking=True,
            )

    if commercial_first:
        cf = dict(commercial_first)
        # Deadline pressure escalates the owning commercial action (quote / channel),
        # not financing while quote-first still owns the path.
        try:
            from discovery.deadline_viability import apply_deadline_to_action_priority

            action_u = str(cf.get("action") or "").upper()
            if "QUOTE" in action_u:
                blocker = "supplier_quote_missing"
            elif cf.get("kind") == "CO_CLARIFICATION":
                blocker = "required_document"
            else:
                blocker = "generic"
            adj = apply_deadline_to_action_priority(
                int(cf.get("priority") or 50),
                viability=viability,
                runway_days=runway_days,
                blocker=blocker,
            )
            if adj["priority"] < int(cf.get("priority") or 50):
                cf["priority"] = adj["priority"]
                cf["why"] = f"{cf.get('why') or ''} [{adj['urgency']}:{adj['reason']}]".strip()
        except Exception:
            pass
        actions.insert(0, cf)

    actions.sort(key=lambda a: (a["priority"], a.get("action") or ""))
    primary = actions[0] if actions else {
        "action": "No open actions — review Deal Workspace overview.",
        "why": "Queue empty",
        "priority": 999,
        "status": ACTION_OPEN,
    }
    return {
        "next_action": primary,
        "queue": actions,
        "generated_at": now_utc().isoformat(),
        "LIVE_API_REQUESTS": 0,
    }


def build_today_queue(next_actions_result: dict[str, Any] | None, *, today: date | None = None) -> dict[str, Any]:
    as_of = today or today_local()
    queue = list((next_actions_result or {}).get("queue") or [])
    today_items = []
    for a in queue:
        due_at = a.get("due_at")
        include = a.get("blocking") or False
        if due_at:
            try:
                d = date.fromisoformat(str(due_at)[:10])
                if d <= as_of:
                    include = True
            except ValueError:
                pass
        if a.get("priority", 999) <= 50:
            include = True
        if include and a.get("status") == ACTION_OPEN:
            today_items.append(a)
    return {
        "title": "WHAT DO I NEED TO DO TODAY?",
        "as_of": as_of.isoformat(),
        "actions": today_items,
        "count": len(today_items),
        "LIVE_API_REQUESTS": 0,
    }
