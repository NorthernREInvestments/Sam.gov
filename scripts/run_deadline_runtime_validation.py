"""Deadline runtime validation: Iowa blade (SYSTEM clock) + expired portal override case."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from application_clock import (
    CLOCK_SYSTEM,
    clock_mode,
    complete_run_metadata,
    freeze_time,
    now_in_timezone,
    now_utc,
    reset_clock,
    start_run_metadata,
)
from deadline_conflict import deadline_evidence_record
from deadline_runtime import (
    STATUS_EXPIRED,
    STATUS_OPEN,
    classify_queue_opportunities,
    evaluate_deadline,
)
from discovery.deadline_viability import VIABILITY_TOO_LATE
from portal_registration_decision import assess_portal_registration
from public_evidence_constants import EV_AUTHORITATIVE_CURRENT, EV_THIRD_PARTY_MIRROR

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
PACKET = ARTIFACTS / "transactional_procurement_packets" / "645-DOTRFB-2975-2027.json"


def _default_iowa_evidence() -> list[dict]:
    return [
        deadline_evidence_record(
            raw="9/15/2026, 1:00 PM CDT",
            role="OPEN",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            document="645-DOTRFB-2975-2027-event.pdf",
            page_section="Open",
            confidence="HIGH",
        ),
        deadline_evidence_record(
            raw="10/7/2026, 1:00 PM CDT",
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            document="645-DOTRFB-2975-2027-event.pdf",
            page_section="Close/Sealed Until",
            confidence="HIGH",
        ),
        deadline_evidence_record(
            raw="9/15/2026",
            role="MIRROR_CLAIMED_DEADLINE",
            evidence_class=EV_THIRD_PARTY_MIRROR,
            confidence="LOW",
        ),
    ]


def _iowa_evidence_from_packet() -> list[dict]:
    if not PACKET.exists():
        return _default_iowa_evidence()
    packet = json.loads(PACKET.read_text(encoding="utf-8"))
    per = packet.get("public_evidence_recovery") or {}
    recon = per.get("deadline_reconciliation") or {}
    records = recon.get("records") or []
    if records:
        return records
    return _default_iowa_evidence()


def validate_iowa_blade() -> dict:
    reset_clock()  # SYSTEM clock — real runtime
    assert clock_mode() == CLOCK_SYSTEM
    meta = start_run_metadata(extra={"run_kind": "iowa_blade_deadline_validation", "solicitation": "645-DOTRFB-2975-2027"})
    evidence = _iowa_evidence_from_packet()
    # Ensure we have open+close; if packet records incomplete, augment
    roles = {r.get("role") for r in evidence}
    if "OPEN" not in roles or "CLOSE" not in roles:
        evidence = [
            deadline_evidence_record(
                raw="9/15/2026, 1:00 PM CDT",
                role="OPEN",
                evidence_class=EV_AUTHORITATIVE_CURRENT,
                document="645-DOTRFB-2975-2027-event.pdf",
                page_section="Open",
                confidence="HIGH",
            ),
            deadline_evidence_record(
                raw="10/7/2026, 1:00 PM CDT",
                role="CLOSE",
                evidence_class=EV_AUTHORITATIVE_CURRENT,
                document="645-DOTRFB-2975-2027-event.pdf",
                page_section="Close/Sealed Until",
                confidence="HIGH",
            ),
        ] + [r for r in evidence if r.get("role") == "MIRROR_CLAIMED_DEADLINE"]

    ev = evaluate_deadline(
        deadline_evidence=evidence,
        portal_status="OPEN",
        local_timezone="America/Chicago",
    )
    portal = assess_portal_registration(
        deadline_reconciliation=ev.get("reconciliation"),
        deadline_evaluation=ev,
        auth_blocked_critical_doc=True,
        authoritative_spec_available=False,
        bid_submission_requires_registration=True,
        product_id_state="BRAND_OR_EQUAL_IDENTIFIED",
    )
    meta = complete_run_metadata(meta)
    return {
        "case": "iowa_blade_645-DOTRFB-2975-2027",
        "run_metadata": meta,
        "actual_runtime_timestamp_utc": ev["evaluated_at"],
        "clock_mode": ev["clock_mode"],
        "current_local_time": ev["current_datetime_local"],
        "current_date_local": ev["current_date_local"],
        "open_date": ev["open_date"],
        "close_date": ev["close_date"],
        "operational_deadline": ev["operational_deadline"],
        "time_remaining": ev["time_remaining"],
        "deadline_status": ev["deadline_status"],
        "deadline_confidence": ev["deadline_confidence"],
        "deadline_viability": ev["deadline_viability"],
        "actionable": ev["actionable"],
        "portal_registration_temporally_viable": ev["portal_registration_temporally_viable"],
        "portal_registration_state": portal.get("state"),
        "open_is_not_response_deadline": ev.get("open_is_not_response_deadline"),
        "evaluation": ev,
        "portal": portal,
    }


def validate_expired_portal_override() -> dict:
    """portal_status=OPEN, deadline=yesterday, runtime=today → EXPIRED, not actionable."""
    reset_clock()
    today = now_utc()
    yesterday = (today - timedelta(days=1)).astimezone(ZoneInfo("America/Chicago"))
    # Build aware yesterday 1pm CDT
    y = yesterday.date()
    deadline_local = datetime(y.year, y.month, y.day, 13, 0, 0, tzinfo=ZoneInfo("America/Chicago"))
    raw = deadline_local.strftime("%-m/%-d/%Y, 1:00 PM CDT") if False else f"{y.month}/{y.day}/{y.year}, 1:00 PM CDT"
    evidence = [
        deadline_evidence_record(
            raw=raw,
            role="CLOSE",
            evidence_class=EV_AUTHORITATIVE_CURRENT,
            confidence="HIGH",
        )
    ]
    ev = evaluate_deadline(
        deadline_evidence=evidence,
        portal_status="OPEN",
        local_timezone="America/Chicago",
    )
    queue = classify_queue_opportunities(
        [
            {
                "id": "expired-control",
                "portal_status": "OPEN",
                "deadline_evidence": evidence,
                "operational_deadline": raw,
            }
        ]
    )
    return {
        "case": "expired_with_portal_open",
        "portal_status": "OPEN",
        "deadline_raw": raw,
        "evaluated_at": ev["evaluated_at"],
        "clock_mode": ev["clock_mode"],
        "deadline_status": ev["deadline_status"],
        "deadline_viability": ev["deadline_viability"],
        "actionable": ev["actionable"],
        "portal_status_overridden_by_clock": ev["portal_status_overridden_by_clock"],
        "in_actionable_queue": any(r.get("id") == "expired-control" for r in queue["actionable_queue"]),
        "in_expired_historical": any(r.get("id") == "expired-control" for r in queue["expired_historical"]),
        "expected": {
            "deadline_status": STATUS_EXPIRED,
            "actionable": False,
            "portal_overridden": True,
        },
        "passed": (
            ev["deadline_status"] == STATUS_EXPIRED
            and ev["actionable"] is False
            and ev["portal_status_overridden_by_clock"] is True
            and not any(r.get("id") == "expired-control" for r in queue["actionable_queue"])
        ),
        "evaluation": ev,
        "queue_counts": queue["counts"],
    }


def update_iowa_packet(iowa: dict) -> None:
    if not PACKET.exists():
        return
    packet = json.loads(PACKET.read_text(encoding="utf-8"))
    packet["runtime_deadline_evaluation"] = {
        "evaluated_at": iowa["actual_runtime_timestamp_utc"],
        "clock_mode": iowa["clock_mode"],
        "current_runtime_datetime_local": iowa["current_local_time"],
        "operational_deadline": iowa["operational_deadline"],
        "time_remaining": iowa["time_remaining"],
        "deadline_status": iowa["deadline_status"],
        "deadline_confidence": iowa["deadline_confidence"],
        "deadline_viability": iowa["deadline_viability"],
        "actionable": iowa["actionable"],
        "portal_registration_temporally_viable": iowa["portal_registration_temporally_viable"],
    }
    # Do not overwrite procurement evidence
    PACKET.write_text(json.dumps(packet, indent=2, default=str), encoding="utf-8")
    md_path = PACKET.with_suffix(".md")
    if md_path.exists():
        block = f"""

## Runtime deadline evaluation (ApplicationClock)
- evaluated_at: {iowa['actual_runtime_timestamp_utc']}
- clock_mode: {iowa['clock_mode']}
- current local: {iowa['current_local_time']}
- operational deadline: {iowa['operational_deadline']}
- time remaining: {iowa['time_remaining']}
- deadline status: {iowa['deadline_status']}
- deadline confidence: {iowa['deadline_confidence']}
- deadline viability: {iowa['deadline_viability']}
- actionable: {iowa['actionable']}
- portal registration temporally viable: {iowa['portal_registration_temporally_viable']}
"""
        text = md_path.read_text(encoding="utf-8")
        if "## Runtime deadline evaluation" in text:
            # replace trailing section
            head = text.split("## Runtime deadline evaluation")[0].rstrip()
            text = head + "\n" + block
        else:
            text = text.rstrip() + "\n" + block
        md_path.write_text(text, encoding="utf-8")


def main() -> None:
    reset_clock()
    iowa = validate_iowa_blade()
    expired = validate_expired_portal_override()
    update_iowa_packet(iowa)
    results = {
        "generated_at": now_utc().isoformat(),
        "clock_mode": clock_mode(),
        "iowa_blade": iowa,
        "expired_portal_override": expired,
        "request_counts": {
            "public_http_search": 0,
            "SAM": 0,
            "USAspending": 0,
            "OpenAI": 0,
            "Paid": 0,
            "supplier_outreach": 0,
            "agency_outreach": 0,
            "lender_outreach": 0,
            "bid_submissions": 0,
        },
    }
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "deadline_runtime_validation.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    report = f"""# Deadline Runtime Validation

Generated: {results['generated_at']}
Clock mode: {results['clock_mode']}

## Iowa blade 645-DOTRFB-2975-2027 (SYSTEM clock)
- actual runtime timestamp: {iowa['actual_runtime_timestamp_utc']}
- clock mode: {iowa['clock_mode']}
- current local time: {iowa['current_local_time']}
- OPEN: {iowa['open_date']}
- CLOSE: {iowa['close_date']}
- operational deadline: {iowa['operational_deadline']}
- time remaining: {iowa['time_remaining']}
- deadline status: {iowa['deadline_status']}
- deadline confidence: {iowa['deadline_confidence']}
- deadline viability: {iowa['deadline_viability']}
- actionable: {iowa['actionable']}
- portal registration temporally viable: {iowa['portal_registration_temporally_viable']}

## Expired + portal OPEN control case
- passed: {expired['passed']}
- deadline_status: {expired['deadline_status']}
- actionable: {expired['actionable']}
- portal overridden by clock: {expired['portal_status_overridden_by_clock']}
- in actionable queue: {expired['in_actionable_queue']}
- in expired_historical: {expired['in_expired_historical']}

NEXT STATE:
CENTRAL_RUNTIME_CLOCK_OPERATIONAL
"""
    (ARTIFACTS / "deadline_runtime_validation_report.md").write_text(report, encoding="utf-8")
    print(json.dumps({
        "iowa_status": iowa["deadline_status"],
        "iowa_actionable": iowa["actionable"],
        "iowa_operational": iowa["operational_deadline"],
        "expired_passed": expired["passed"],
        "clock_mode": clock_mode(),
    }, indent=2))


if __name__ == "__main__":
    main()
