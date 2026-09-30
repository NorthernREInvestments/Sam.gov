"""M3 Owner UI — single mapping layer: backend funnel → operator labels.

All primary UI status text must come through this module.
Do not scatter status logic in the frontend.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

# Operator-facing status labels (primary UI)
CALL_SUPPLIER = "CALL SUPPLIER"
FOLLOW_UP = "FOLLOW UP"
WAITING_FOR_QUOTE = "WAITING FOR QUOTE"
QUOTE_RECEIVED = "QUOTE RECEIVED"
REGISTER_FIRST = "REGISTER FIRST"
BID_PREP = "BID PREP"
WATCH = "WATCH"
BLOCKED = "BLOCKED"
SKIP = "SKIP"

# Next-action codes (machine + button)
ACTION_CALL_SUPPLIER = "CALL_SUPPLIER"
ACTION_FOLLOW_UP = "FOLLOW_UP"
ACTION_REVIEW_QUOTE = "REVIEW_QUOTE"
ACTION_REGISTER = "REGISTER"
ACTION_START_BID_PREP = "START_BID_PREP"
ACTION_WATCH = "WATCH"
ACTION_WAIT = "WAIT"
ACTION_SKIP = "SKIP"
ACTION_RESOLVE_BLOCKER = "RESOLVE_BLOCKER"

# Priority bands shown to operators
PRIORITY_HIGH = "HIGH"
PRIORITY_NORMAL = "NORMAL"
PRIORITY_LOW = "LOW"

# Color tokens (always paired with text labels)
COLOR_READY = "green"
COLOR_REVIEW = "yellow"
COLOR_WAITING = "blue"
COLOR_WATCH = "gray"
COLOR_BLOCKED = "red"

# Backend funnel states we map from (L.23 + call-desk overlays)
_FUNNEL_CALL = frozenset({"READY_TO_CALL", "CALLS_IN_PROGRESS"})
_FUNNEL_QUOTE_WAIT = frozenset({"QUOTE_PENDING"})
_FUNNEL_QUOTE_GOT = frozenset({"QUOTES_RECEIVED", "READY_FOR_FINAL_ECONOMICS"})
_FUNNEL_BID = frozenset({"READY_TO_BID"})
_FUNNEL_WATCH = frozenset({"WATCH", "WATCH_OTHER", "LOW_PRIORITY_RESEARCH", "NEEDS_SOURCE_DATA"})
_FUNNEL_BLOCKED = frozenset({"WATCH_FEDERAL_ACCESS"})
_FUNNEL_SKIP = frozenset({"SKIP", "FAST_REJECT", "HARD_REJECT"})

STATUS_META: dict[str, dict[str, str]] = {
    CALL_SUPPLIER: {"color": COLOR_READY, "queue": "call_today"},
    FOLLOW_UP: {"color": COLOR_REVIEW, "queue": "follow_up"},
    WAITING_FOR_QUOTE: {"color": COLOR_WAITING, "queue": "follow_up"},
    QUOTE_RECEIVED: {"color": COLOR_REVIEW, "queue": "quotes"},
    REGISTER_FIRST: {"color": COLOR_REVIEW, "queue": "registrations"},
    BID_PREP: {"color": COLOR_READY, "queue": "bid_prep"},
    WATCH: {"color": COLOR_WATCH, "queue": "watch"},
    BLOCKED: {"color": COLOR_BLOCKED, "queue": "blocked"},
    SKIP: {"color": COLOR_WATCH, "queue": "skip"},
}

ACTION_BUTTON_LABELS: dict[str, str] = {
    ACTION_CALL_SUPPLIER: "CALL SUPPLIER",
    ACTION_FOLLOW_UP: "FOLLOW UP",
    ACTION_REVIEW_QUOTE: "REVIEW QUOTE",
    ACTION_REGISTER: "REGISTER",
    ACTION_START_BID_PREP: "START BID PREP",
    ACTION_WATCH: "KEEP WATCHING",
    ACTION_WAIT: "WAITING",
    ACTION_SKIP: "SKIP",
    ACTION_RESOLVE_BLOCKER: "VIEW BLOCKER",
}


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def deadline_urgency(deadline: Any, *, today: date | None = None) -> dict[str, Any]:
    """Plain-language deadline warning for operators."""
    today = today or datetime.now(timezone.utc).date()
    d = _parse_date(deadline)
    if not d:
        return {"label": None, "days": None, "level": None}
    days = (d - today).days
    if days < 0:
        return {"label": "PAST DEADLINE", "days": days, "level": "critical"}
    if days == 0:
        return {"label": "DUE TODAY", "days": 0, "level": "critical"}
    if days == 1:
        return {"label": "DUE TOMORROW", "days": 1, "level": "high"}
    if days < 3:
        return {"label": f"<{days + 1} DAYS" if days == 2 else "<3 DAYS", "days": days, "level": "high"}
    if days < 5:
        return {"label": "<5 DAYS", "days": days, "level": "medium"}
    return {"label": f"{days} DAYS", "days": days, "level": "normal"}


def priority_band(score: Any, *, call_priority: str | None = None) -> str:
    if call_priority in {"CALL_FIRST", "CALL FIRST"}:
        return PRIORITY_HIGH
    try:
        s = float(score)
    except (TypeError, ValueError):
        s = 0.0
    if s >= 70 or call_priority in {"CALL_SECOND", "CALL SECOND"}:
        return PRIORITY_HIGH
    if s >= 40:
        return PRIORITY_NORMAL
    return PRIORITY_LOW


def map_call_priority_label(band: str | None) -> str:
    b = str(band or "").upper().replace(" ", "_")
    return {
        "CALL_FIRST": "CALL FIRST",
        "CALL_SECOND": "CALL SECOND",
        "CALL_THIRD": "BACKUP",
        "BACKUP": "BACKUP",
    }.get(b, "BACKUP")


def _registration_needed(rec: dict[str, Any]) -> bool:
    rs = str(rec.get("registration_status") or "").upper()
    if rs in {"EASY", "REQUIRED", "NOT_REGISTERED", "EASY_REGISTRATION"}:
        # Easy portal registration that unlocks work — surface as register when not call-ready
        if rec.get("current_funnel_state") not in _FUNNEL_CALL | _FUNNEL_QUOTE_WAIT | _FUNNEL_QUOTE_GOT | _FUNNEL_BID:
            if rs in {"EASY", "EASY_REGISTRATION", "REQUIRED", "NOT_REGISTERED"} and rec.get("registration_action"):
                return True
    action = str(rec.get("registration_action") or "").upper()
    return action in {
        "REGISTER_NOW",
        "REGISTER_BEFORE_BID",
        "REGISTER_NOW_RECURRING_BUYER",
    }


def _blocked_reason(rec: dict[str, Any]) -> dict[str, Any]:
    state = str(rec.get("current_funnel_state") or "")
    access = str(rec.get("access_status") or "")
    is_federal = bool(rec.get("is_federal"))
    if state == "WATCH_FEDERAL_ACCESS" or (is_federal and "CAGE" in access.upper()):
        return {
            "blocker": "CAGE REQUIRED",
            "plain": "Federal bid access is blocked until CAGE is active.",
            "resolves_with": "Active CAGE code / SAM entity registration",
            "owner_action_required": True,
            "operator_action_inside_m3": False,
        }
    if "DIBBS" in access.upper() or "DIBBS" in str(rec.get("submission_path") or "").upper():
        return {
            "blocker": "DIBBS REGISTRATION REQUIRED",
            "plain": "Cannot submit DLA bid yet.",
            "resolves_with": "DIBBS vendor registration",
            "owner_action_required": True,
            "operator_action_inside_m3": False,
        }
    if "AUTH" in access.upper() or "MANUFACTURER" in access.upper():
        return {
            "blocker": "MANUFACTURER AUTHORIZATION",
            "plain": "Opportunity requires authorized source.",
            "resolves_with": "Authorized distributor path or OEM approval",
            "owner_action_required": True,
            "operator_action_inside_m3": False,
        }
    return {
        "blocker": "ACCESS BLOCKED",
        "plain": "Waiting on access or registration outside the daily call queue.",
        "resolves_with": str(rec.get("owner_reason") or rec.get("recheck_trigger") or "Owner review"),
        "owner_action_required": True,
        "operator_action_inside_m3": False,
    }


def map_funnel_to_owner_status(
    rec: dict[str, Any],
    *,
    overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map one canonical (or L.22-enriched) record to operator status + next action.

    overlay may include: call_status, follow_up_date, quote_status, promised_quote_date
    """
    overlay = overlay or {}
    state = str(rec.get("current_funnel_state") or overlay.get("funnel_state") or "")
    call_status = str(overlay.get("call_status") or rec.get("call_status") or "").upper()
    quote_status = str(overlay.get("quote_status") or "").upper()

    # Overlays from live call desk win when present
    if quote_status in {"NEW", "NEEDS_REVIEW", "RECEIVED", "GOOD", "MARGINAL", "TOO_HIGH"}:
        status = QUOTE_RECEIVED
        action = ACTION_REVIEW_QUOTE
        reason = overlay.get("ui_next_action_reason") or "Quote received; review landed cost and recommendation."
    elif call_status in {"FOLLOW_UP", "QUOTE_PROMISED"} or overlay.get("promised_quote_date"):
        status = FOLLOW_UP if call_status != "WAITING" else WAITING_FOR_QUOTE
        if overlay.get("promised_quote_date") and call_status != "FOLLOW_UP":
            status = WAITING_FOR_QUOTE
        action = ACTION_FOLLOW_UP if status == FOLLOW_UP else ACTION_WAIT
        reason = overlay.get("ui_next_action_reason") or (
            f"Quote promised by {overlay.get('promised_quote_date')}."
            if overlay.get("promised_quote_date")
            else "Callback or follow-up promised."
        )
    elif state in _FUNNEL_BLOCKED or (rec.get("is_federal") and state in _FUNNEL_WATCH):
        status = BLOCKED
        action = ACTION_RESOLVE_BLOCKER
        br = _blocked_reason(rec)
        reason = br["plain"]
    elif state in _FUNNEL_SKIP:
        status = SKIP
        action = ACTION_SKIP
        reason = "Not a fit — safe to ignore unless circumstances change."
    elif state in _FUNNEL_BID:
        status = BID_PREP
        action = ACTION_START_BID_PREP
        reason = "Economics and quote path ready for bid package work."
    elif state in _FUNNEL_QUOTE_GOT:
        status = QUOTE_RECEIVED
        action = ACTION_REVIEW_QUOTE
        reason = "Quote on file — review before bidding."
    elif state in _FUNNEL_QUOTE_WAIT:
        status = WAITING_FOR_QUOTE
        action = ACTION_WAIT
        reason = "Waiting on supplier quote."
    elif state in _FUNNEL_CALL or overlay.get("call_ready") or call_status in {"NOT_CALLED", "READY", "IN_PROGRESS"}:
        status = CALL_SUPPLIER
        action = ACTION_CALL_SUPPLIER
        reason = (
            overlay.get("why_call")
            or rec.get("call_ready_reason")
            or rec.get("owner_reason")
            or "Exact product path identified; pricing needed from supplier."
        )
    elif _registration_needed(rec) or overlay.get("register_first"):
        status = REGISTER_FIRST
        action = ACTION_REGISTER
        reason = "Register on this portal to unlock buyers and current opportunities."
    elif state in _FUNNEL_WATCH or state in {
        "DEEP_RESEARCH_COMPLETE",
        "DEEP_RESEARCH_PRIORITY",
        "DEEP_RESEARCH_IN_PROGRESS",
        "FAST_RESEARCH_COMPLETE",
        "ACCESSIBLE_PRODUCT",
        "RAW",
        "FAST_RESEARCH_PENDING",
    }:
        # Deep-research-complete without call gate → Watch, not technical label
        if state == "DEEP_RESEARCH_COMPLETE" and (rec.get("call_gate") or {}).get("ready"):
            status = CALL_SUPPLIER
            action = ACTION_CALL_SUPPLIER
            reason = rec.get("call_ready_reason") or "Research complete; ready to call suppliers."
        else:
            status = WATCH
            action = ACTION_WATCH
            reason = (
                rec.get("recheck_trigger")
                or rec.get("owner_reason")
                or "Weak evidence or not worth action yet — watch for a better signal."
            )
    else:
        status = WATCH
        action = ACTION_WATCH
        reason = "No immediate operator action."

    meta = STATUS_META[status]
    pri = priority_band(
        rec.get("deal_priority_score") or rec.get("priority_score") or overlay.get("priority_score"),
        call_priority=overlay.get("priority") or overlay.get("call_priority"),
    )
    blocked = _blocked_reason(rec) if status == BLOCKED else None

    return {
        "ui_status": status,
        "ui_status_color": meta["color"],
        "ui_queue": meta["queue"],
        "ui_next_action": action,
        "ui_next_action_label": ACTION_BUTTON_LABELS.get(action, action.replace("_", " ")),
        "ui_next_action_reason": _plain_reason(reason),
        "ui_priority": pri,
        "backend_funnel_state": state or None,
        "blocked": blocked,
    }


def _plain_reason(reason: Any) -> str:
    s = str(reason or "").strip()
    if not s:
        return "See deal detail for context."
    # Strip leaked technical tokens from primary reason text
    for token in (
        "DEEP_RESEARCH_COMPLETE",
        "WATCH_FEDERAL_ACCESS",
        "READY_TO_CALL",
        "FAST_REJECT",
        "L.21",
        "L.22",
        "L.23",
        "L.23.1",
    ):
        s = s.replace(token, "").replace(token.lower(), "")
    s = " ".join(s.split())
    return s[:280] if s else "See deal detail for context."


def operator_safe_error(exc: BaseException | str) -> dict[str, str]:
    """Translate technical failures into operator language."""
    msg = str(exc)
    lower = msg.lower()
    if "timeout" in lower or "connection" in lower or "unavailable" in lower:
        plain = "Source temporarily unavailable. Last known data retained."
    elif "json" in lower or "parse" in lower:
        plain = "Could not refresh this buyer. Last known data retained."
    else:
        plain = "Something went wrong saving or loading. Your notes were kept if already saved."
    return {"error": "operator_safe", "message": plain, "detail_for_advanced": msg[:500]}
