"""Deterministic YOU MISSED THIS / deal-warning engine."""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date, datetime, timezone
from typing import Any

WARNING_ACTIVE = "ACTIVE"
WARNING_RESOLVED = "RESOLVED"

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"


def _warn(
    warning_type: str,
    message: str,
    *,
    severity: str = SEVERITY_MEDIUM,
    why: str | None = None,
    action: str | None = None,
    evidence: dict | None = None,
    active_key: str = "default",
) -> dict[str, Any]:
    return {
        "warning_type": warning_type,
        "active_key": active_key,
        "severity": severity,
        "message": message,
        "why_it_matters": why,
        "recommended_next_action": action,
        "evidence_json": evidence or {},
    }


def compute_warnings_from_workspace(ws: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure deterministic rules — no AI."""
    warnings: list[dict[str, Any]] = []
    fin = ws.get("financing") or {}
    pursuits = fin.get("pursuits") or []
    quotes = ws.get("quotes") or []
    pkg = ws.get("solicitation_package") or {}
    deal_rd = ws.get("deal_readiness") or {}
    bid_rd = ws.get("bid_readiness") or {}
    econ = ws.get("economics") or {}
    opp = ws.get("opportunity") or {}
    funding = ws.get("funding_plan") or {}

    for p in pursuits:
        if p.get("pg_required") is True:
            warnings.append(
                _warn(
                    "FINANCING_PG_REQUIRED",
                    "Provider requires personal guarantee.",
                    severity=SEVERITY_CRITICAL,
                    why="Violates company hard rule (no PG).",
                    action="Find alternate funding structure or supplier terms.",
                    evidence={"provider_id": p.get("provider_id"), "source": "gt_financing_pursuits"},
                )
            )
        if p.get("personal_credit_required") is True:
            warnings.append(
                _warn(
                    "FINANCING_PERSONAL_CREDIT_REQUIRED",
                    "Provider requires personal credit.",
                    severity=SEVERITY_CRITICAL,
                    why="Violates company hard rule (no personal credit).",
                    action="Find alternate provider or structure.",
                    evidence={"provider_id": p.get("provider_id")},
                )
            )
        if p.get("borrower_cash_required") is True:
            warnings.append(
                _warn(
                    "FINANCING_OWNER_CASH_REQUIRED",
                    "Provider requires owner cash/equity contribution.",
                    severity=SEVERITY_CRITICAL,
                    why="Violates $0 personal cash upfront rule.",
                    action="Negotiate terms or pursue different funding path.",
                    evidence={"provider_id": p.get("provider_id")},
                )
            )

    actual = econ.get("actual_profit")
    if actual is not None and float(actual) < 10000:
        warnings.append(
            _warn(
                "COMMERCIAL_PROFIT_BELOW_MINIMUM",
                f"Actual profit ${actual:,.0f} is below $10,000 minimum.",
                severity=SEVERITY_HIGH,
                why="Deal fails company economics gate.",
                action="Increase bid price or reduce acquisition/financing cost.",
                evidence={"actual_profit": actual},
            )
        )
    elif actual is None and deal_rd.get("status") != "DEAL_READY":
        warnings.append(
            _warn(
                "COMMERCIAL_ACTUAL_PROFIT_INCOMPLETE",
                "Actual profit not yet calculable.",
                severity=SEVERITY_MEDIUM,
                why="Cannot confirm $10K minimum profit gate.",
                action="Complete quote validation and proposed bid economics.",
            )
        )

    if not (ws.get("proposed_bid") or {}).get("amount") and not opp.get("operator_bid"):
        if quotes:
            warnings.append(
                _warn(
                    "COMMERCIAL_PROPOSED_BID_MISSING",
                    "Proposed bid price not set.",
                    severity=SEVERITY_MEDIUM,
                    why="Economics and Deal Ready require proposed bid.",
                    action="Enter proposed bid amount after quote review.",
                )
            )

    for q in quotes:
        val = q.get("validation") or {}
        issues = val.get("issues") or []
        for issue in issues:
            code = str(issue.get("code") or issue).upper()
            if "BOM" in code or "MISMATCH" in code:
                warnings.append(
                    _warn(
                        "QUOTE_BOM_MISMATCH",
                        issue.get("message") or "Quote BOM mismatch detected.",
                        severity=SEVERITY_HIGH,
                        why="Non-compliant quote cannot support bid.",
                        action="Obtain corrected supplier quote.",
                        evidence={"quote_id": q.get("id"), "issue": issue},
                        active_key=f"quote_{q.get('id')}_bom",
                    )
                )
            if "EXPIR" in code:
                warnings.append(
                    _warn(
                        "QUOTE_EXPIRATION_RISK",
                        issue.get("message") or "Quote expiration risk.",
                        severity=SEVERITY_HIGH,
                        why="Expired quote invalid for bid.",
                        action="Refresh quote before deadline.",
                        evidence={"quote_id": q.get("id")},
                        active_key=f"quote_{q.get('id')}_exp",
                    )
                )
            if "OEM" in code or "CHANNEL" in code or "AUTHORIZATION" in code:
                warnings.append(
                    _warn(
                        "QUOTE_OEM_CHANNEL_UNRESOLVED",
                        issue.get("message") or "OEM/channel authorization unresolved.",
                        severity=SEVERITY_HIGH,
                        why="Brand-name-only requirements need authorized channel.",
                        action="Confirm OEM letter or authorized reseller path.",
                        evidence={"quote_id": q.get("id")},
                        active_key=f"quote_{q.get('id')}_oem",
                    )
                )

    if pkg.get("status") in {"INCOMPLETE", "UNRESOLVED"}:
        warnings.append(
            _warn(
                "SOLICITATION_PACKAGE_INCOMPLETE",
                f"Solicitation package status: {pkg.get('status')}.",
                severity=SEVERITY_HIGH,
                why="Cannot bid without complete solicitation package.",
                action="Acquire missing documents/amendments.",
                evidence={"package_status": pkg.get("status")},
            )
        )

    due = opp.get("due_date")
    if due and deal_rd.get("status") != "DEAL_READY":
        try:
            d = date.fromisoformat(str(due)[:10])
            days = (d - today_local()).days
            if days <= 7:
                warnings.append(
                    _warn(
                        "DEADLINE_APPROACHING_WITH_BLOCKER",
                        f"Deadline in {days} day(s) with open blockers.",
                        severity=SEVERITY_CRITICAL,
                        why="Risk missing submission window.",
                        action=str((deal_rd.get("blockers") or ["Resolve blockers"])[0]),
                        evidence={"days_remaining": days},
                    )
                )
        except ValueError:
            pass

    if bid_rd.get("status") != "BID_READY" and (bid_rd.get("blockers") or []):
        warnings.append(
            _warn(
                "BID_COMPONENT_MISSING",
                f"Bid not ready: {bid_rd['blockers'][0]}",
                severity=SEVERITY_HIGH,
                why="Required submission component missing.",
                action=f"Complete: {bid_rd['blockers'][0]}",
                evidence={"blockers": bid_rd.get("blockers")},
            )
        )

    pre_bid = funding.get("pre_bid_status") or (funding.get("pre_bid_viability") or {}).get("status")
    if pre_bid == "BLOCKED":
        warnings.append(
            _warn(
                "FUNDING_PRE_BID_BLOCKED",
                "Pre-bid funding viability blocked.",
                severity=SEVERITY_CRITICAL,
                why="Cannot pursue deal without viable funding path.",
                action="Research alternate funding strategies.",
                evidence={"pre_bid_status": pre_bid},
            )
        )

    return warnings


def persist_warnings(session: Any, contract_id: int, computed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upsert active warnings; resolve stale ones — no duplicates."""
    from models import DealWarning

    now = now_utc()
    active_keys = {(w["warning_type"], w.get("active_key", "default")) for w in computed}
    existing = session.query(DealWarning).filter_by(contract_id=contract_id, status=WARNING_ACTIVE).all()

    for row in existing:
        key = (row.warning_type, row.active_key)
        if key not in active_keys:
            row.status = WARNING_RESOLVED
            row.resolved_at = now

    out: list[dict[str, Any]] = []
    for w in computed:
        key = (w["warning_type"], w.get("active_key", "default"))
        row = (
            session.query(DealWarning)
            .filter_by(contract_id=contract_id, warning_type=key[0], active_key=key[1])
            .first()
        )
        if row is None:
            row = DealWarning(
                contract_id=contract_id,
                warning_type=w["warning_type"],
                active_key=w.get("active_key", "default"),
                severity=w.get("severity", SEVERITY_MEDIUM),
                message=w["message"],
                why_it_matters=w.get("why_it_matters"),
                evidence_json=w.get("evidence_json"),
                recommended_next_action=w.get("recommended_next_action"),
                status=WARNING_ACTIVE,
            )
            session.add(row)
        else:
            row.severity = w.get("severity", row.severity)
            row.message = w["message"]
            row.why_it_matters = w.get("why_it_matters")
            row.evidence_json = w.get("evidence_json")
            row.recommended_next_action = w.get("recommended_next_action")
            row.status = WARNING_ACTIVE
            row.resolved_at = None
        session.flush()
        out.append(warning_to_dict(row))
    return out


def warning_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "warning_type": row.warning_type,
        "severity": row.severity,
        "message": row.message,
        "why_it_matters": row.why_it_matters,
        "evidence": row.evidence_json,
        "recommended_next_action": row.recommended_next_action,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


def recompute_deal_warnings(session: Any, contract_id: int, workspace: dict[str, Any]) -> list[dict[str, Any]]:
    computed = compute_warnings_from_workspace(workspace)
    return persist_warnings(session, contract_id, computed)
