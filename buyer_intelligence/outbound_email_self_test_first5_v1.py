"""Outbound Email Configuration + Self-Test + First 5 RFQ Send-Ready V1.

Configure/verify outbound email from existing env, run owner-only self-test,
leave five verified RFQs READY_FOR_OWNER_APPROVAL. Lumec stays HOLD.
Never auto-send supplier emails. QUOTE_OUTREACH_DRY_RUN must remain true.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from buyer_intelligence.quote_outreach_send_channel_v1 import (
    audit_send_channel,
    env_presence,
    outbound_email_health,
    send_owner_self_test_email,
)

BUILD = "20261009-m3-outbound-email-self-test-first5-v1"
FAMILY = "outbound_email_self_test_first5_v1"

RESOLVED_IN = "m3_rfq_first_batch_resolved_v1.json"
REPORT_FILE = "m3_outbound_email_self_test_first5_v1_report.json"
UI_FILE = "m3_outbound_email_first5_today_v1.json"
SELF_TEST_FILE = "m3_outbound_email_self_test_v1.json"
HEALTH_FILE = "m3_outbound_email_health_v1.json"
READY_FILE = "m3_first5_rfq_ready_for_owner_v1.json"
JOB_FILE = "m3_outbound_email_self_test_first5_v1_job.json"

FIRST5_CHANNELS = (
    "NSN_AUTH_DIST",
    "TELEDYNE_API",
    "UTILITY_BREAKER_OEM",
    "EOI_LIGHTING",
    "HONEYWELL_PPE",
)
LUMEC_CHANNEL = "LUMEC_LIGHTING"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_job_lock = threading.Lock()
_worker = False


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str):
    from m3_data_root import data_path

    return data_path(*parts)


def _load(name: str) -> Any:
    p = _data(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(name: str, obj: Any) -> None:
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def _job_update(**kwargs: Any) -> None:
    with _job_lock:
        cur = _load(JOB_FILE) or {"kind": "OutboundEmailFirst5Job", "build": BUILD}
        cur.update(kwargs)
        cur["heartbeat_at"] = _utc()
        cur["build"] = BUILD
        _save(JOB_FILE, cur)


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "IDLE", "build": BUILD}


def audit_email_env() -> dict[str, Any]:
    """Step 1 — presence only."""
    keys = [
        "EMAIL_FROM",
        "EMAIL_REPLY_TO",
        "RESEND_API_KEY",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_TLS",
        "SMTP_SSL",
        "APP_EMAIL",
        "QUOTE_OUTREACH_DRY_RUN",
        "QUOTE_EMAIL_FROM",
        "QUOTE_EMAIL_REPLY_TO",
    ]
    return {k: env_presence(k) for k in keys}


def propose_canonical_config() -> dict[str, Any]:
    """Propose EMAIL_REPLY_TO from APP_EMAIL when usable — never invent SMTP/Resend."""
    presence = audit_email_env()
    app_email = (os.environ.get("APP_EMAIL") or "").strip()
    proposal: dict[str, Any] = {
        "EMAIL_FROM": None,
        "EMAIL_REPLY_TO": None,
        "PROVIDER": "NONE",
        "NOTES": [],
        "CAN_APPLY_WITHOUT_SECRETS": [],
    }
    if presence.get("APP_EMAIL") == "PRESENT" and _EMAIL_RE.match(app_email):
        proposal["EMAIL_REPLY_TO"] = "APP_EMAIL"  # symbolic — do not echo address in reports if avoidable
        proposal["NOTES"].append("APP_EMAIL may be used for EMAIL_REPLY_TO (owner-monitored).")
        proposal["CAN_APPLY_WITHOUT_SECRETS"].append("EMAIL_REPLY_TO←APP_EMAIL")
    if presence.get("RESEND_API_KEY") == "PRESENT":
        proposal["PROVIDER"] = "RESEND"
    elif presence.get("SMTP_HOST") == "PRESENT" and presence.get("SMTP_PASSWORD") == "PRESENT":
        proposal["PROVIDER"] = "SMTP"
    else:
        proposal["PROVIDER"] = "NONE"
        proposal["NOTES"].append(
            "No SMTP/Resend credentials present. Owner must add RESEND_API_KEY "
            "or SMTP_HOST+SMTP_USER+SMTP_PASSWORD before self-test can transmit."
        )
    if presence.get("EMAIL_FROM") != "PRESENT":
        proposal["NOTES"].append("EMAIL_FROM required and must be valid for the selected provider.")
        if proposal["PROVIDER"] == "NONE" and presence.get("APP_EMAIL") == "PRESENT":
            proposal["NOTES"].append(
                "EMAIL_FROM may equal APP_EMAIL only if that mailbox is authorized on the chosen provider "
                "(e.g. Gmail SMTP with App Password — do not reuse APP_PASSWORD)."
            )
    return proposal


def select_provider() -> dict[str, Any]:
    audit = audit_send_channel()
    presence = audit_email_env()
    if presence.get("SMTP_HOST") == "PRESENT" and (
        presence.get("SMTP_PASSWORD") == "PRESENT" or env_presence("SMTP_ALLOW_UNAUTH") == "PRESENT"
    ):
        provider = "SMTP"
    elif presence.get("RESEND_API_KEY") == "PRESENT":
        provider = "RESEND"
    elif presence.get("SMTP_HOST") == "PRESENT" and presence.get("RESEND_API_KEY") == "PRESENT":
        provider = "SMTP"  # prefer SMTP if both — already-configured path
    else:
        provider = audit.get("SEND_CHANNEL_TYPE") or "UNKNOWN"
        if provider == "UNKNOWN":
            provider = "NONE"
    return {
        "PROVIDER": provider if audit.get("CONFIGURED") == "YES" else ("NONE" if provider == "NONE" else provider),
        "CONFIGURED": audit.get("CONFIGURED") == "YES",
        "MISSING_FOR_RESEND": [k for k in ("RESEND_API_KEY", "EMAIL_FROM", "EMAIL_REPLY_TO") if presence.get(k) != "PRESENT"],
        "MISSING_FOR_SMTP": [
            k
            for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_REPLY_TO")
            if presence.get(k) != "PRESENT"
        ],
    }


def _deadline_still_open(deadline: Any) -> bool:
    if not deadline:
        return False
    try:
        s = str(deadline).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > now_utc()
    except Exception:
        return True  # unknown format — do not invent expiry


def revalidate_first5(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Revalidate first five + Lumec hold. No research redo."""
    by_ch = {str(r.get("CHANNEL_KEY")): r for r in rows}
    first5: list[dict[str, Any]] = []
    for ch in FIRST5_CHANNELS:
        p = by_ch.get(ch)
        if not p:
            continue
        lines = p.get("LINES") or []
        open_ok = _deadline_still_open(p.get("DEADLINE"))
        recip = p.get("RECIPIENT_EMAIL") or (p.get("PRIMARY_RECIPIENT") or {}).get("EMAIL")
        complete = bool(
            p.get("PACKET_COMPLETE")
            or (
                p.get("BUYER")
                and p.get("DEADLINE")
                and lines
                and all(ln.get("EFF_QTY") is not None and ln.get("EFF_UOM") for ln in lines)
            )
        )
        verified = str(p.get("RECIPIENT_STATUS") or "").upper() == "VERIFIED" or str(
            p.get("RECIPIENT_VERIFICATION") or ""
        ).startswith("VERIFIED")
        state = "READY_FOR_OWNER_APPROVAL" if (open_ok and complete and verified and recip) else "HOLD"
        preview = {
            "PACKET_ID": p.get("PACKET_ID"),
            "CHANNEL_KEY": ch,
            "SUPPLIER": (p.get("PRIMARY_RECIPIENT") or {}).get("SUPPLIER_NAME") or p.get("SUPPLIER"),
            "TO": recip,
            "BUYER": p.get("BUYER"),
            "PRODUCT": (lines[0].get("PRODUCT") if lines else None),
            "QTY": (lines[0].get("EFF_QTY") if lines else None),
            "LINE_COUNT": p.get("LINE_COUNT") or len(lines),
            "BUYER_DEADLINE": p.get("DEADLINE"),
            "SUPPLIER_RESPONSE_DUE": p.get("RESPONSE_DUE"),
            "SUBJECT": p.get("EMAIL_SUBJECT") or (p.get("EMAIL") or {}).get("SUBJECT"),
            "BODY_PREVIEW": (p.get("EMAIL_PREVIEW") or (p.get("EMAIL") or {}).get("BODY") or "")[:800],
            "PACKET_ATTACHMENT": "INLINE_TABLE",
            "RECIPIENT_SOURCE": p.get("RECIPIENT_SOURCE"),
            "RECIPIENT_VERIFICATION": p.get("RECIPIENT_VERIFICATION"),
            "STILL_OPEN": open_ok,
            "PACKET_COMPLETE": complete,
            "STATE": state,
            "OWNER_ACTIONS": ["APPROVE_SEND", "EDIT", "HOLD"],
            "AUTO_SEND": False,
        }
        first5.append(preview)

    lumec = by_ch.get(LUMEC_CHANNEL) or {}
    lumec_out = {
        "PACKET_ID": lumec.get("PACKET_ID"),
        "CHANNEL_KEY": LUMEC_CHANNEL,
        "SUPPLIER": lumec.get("SUPPLIER") or "Lumec / Signify RoadFocus Channel",
        "TO": None,
        "STATE": "HOLD_RECIPIENT_RESEARCH",
        "BUYER": lumec.get("BUYER"),
        "PRODUCT": ((lumec.get("LINES") or [{}])[0].get("PRODUCT") if lumec.get("LINES") else None),
        "LINE_COUNT": lumec.get("LINE_COUNT"),
        "BUYER_DEADLINE": lumec.get("DEADLINE"),
        "AUTO_SEND": False,
    }
    return first5, lumec_out


def run_outbound_email_first5_v1(*, force: bool = True, self_test_live: bool = True) -> dict[str, Any]:
    wall0 = time.time()
    _job_update(status="RUNNING", started_at=_utc(), phase="AUDIT")

    presence = audit_email_env()
    proposal = propose_canonical_config()
    provider = select_provider()
    audit = audit_send_channel()

    # Ensure dry-run stays true in report even if env missing (code default true)
    dry_true = bool(audit.get("QUOTE_OUTREACH_DRY_RUN", True))
    if env_presence("QUOTE_OUTREACH_DRY_RUN") == "PRESENT":
        dry_true = str(os.environ.get("QUOTE_OUTREACH_DRY_RUN") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    _job_update(phase="SELF_TEST")
    # Self-test may transmit to owner only when credentials exist; never to suppliers
    self_test = send_owner_self_test_email(force_transmit=bool(self_test_live))
    _save(SELF_TEST_FILE, {**self_test, "build": BUILD})

    health = outbound_email_health(last_self_test=self_test)
    health["build"] = BUILD
    health["ENV_PRESENCE"] = presence
    _save(HEALTH_FILE, health)

    _job_update(phase="REVALIDATE_FIRST5")
    resolved = _load(RESOLVED_IN) or {}
    rows = list(resolved.get("rows") or [])
    first5, lumec = revalidate_first5(rows)

    # Email gate: packets stay READY_FOR_OWNER_APPROVAL when content-ready;
    # overall status reflects whether outbound can actually send after approval.
    ready_n = sum(1 for p in first5 if p.get("STATE") == "READY_FOR_OWNER_APPROVAL")
    self_status = str(self_test.get("STATUS") or "BLOCKED")
    creds_ok = bool(health.get("PROVIDER_CREDENTIALS_PRESENT")) and bool(health.get("FROM_CONFIGURED")) and bool(
        health.get("REPLY_TO_CONFIGURED")
    )

    if not dry_true:
        # Safety: never leave dry-run false from this build
        final_status = "SEND_CONFIGURATION_REQUIRED"
        dry_violation = True
    else:
        dry_violation = False

    if not creds_ok or self_status == "BLOCKED":
        final_status = "SEND_CONFIGURATION_REQUIRED"
    elif self_status == "FAIL":
        final_status = "SELF_TEST_FAILED"
    elif ready_n >= 5 and self_status == "PASS":
        final_status = "FIRST_FIVE_RFQS_READY_FOR_OWNER_APPROVAL"
    elif ready_n >= 5 and self_status in {"DRY_RUN_ONLY", "PASS"}:
        # Dry-run-only self-test is not enough for FIRST_FIVE ready-to-send path
        final_status = "SEND_CONFIGURATION_REQUIRED" if not creds_ok else "FIRST_FIVE_RFQS_READY_FOR_OWNER_APPROVAL"
    else:
        final_status = "SEND_CONFIGURATION_REQUIRED"

    reply_path = "BLOCKED"
    if self_status == "PASS" and self_test.get("REPLY_TO_HEADER_CORRECT"):
        reply_path = "MANUAL_VALID"
    elif health.get("REPLY_TO_CONFIGURED"):
        reply_path = "MANUAL_VALID" if not creds_ok else "BLOCKED"

    ready_doc = {
        "kind": "First5RfqReadyForOwnerV1",
        "build": BUILD,
        "created_at": _utc(),
        "rows": first5,
        "lumec": lumec,
        "READY_FOR_OWNER_APPROVAL": ready_n,
        "SUPPLIER_EMAILS_ACTUALLY_SENT": 0,
        "QUOTE_OUTREACH_DRY_RUN": dry_true,
        "do_not_send_automatically": True,
    }
    _save(READY_FILE, ready_doc)

    today = {
        "kind": "OutboundEmailFirst5TodayV1",
        "build": BUILD,
        "section": "RFQS READY FOR APPROVAL",
        "rows": [
            {
                **p,
                "OWNER_STATE": p.get("STATE"),
                "RECIPIENT": p.get("TO"),
                "DEADLINE": p.get("BUYER_DEADLINE"),
                "RESPONSE_DUE": p.get("SUPPLIER_RESPONSE_DUE"),
                "EMAIL_SUBJECT": p.get("SUBJECT"),
                "EMAIL_PREVIEW": p.get("BODY_PREVIEW"),
                "OWNER_ACTIONS": p.get("OWNER_ACTIONS"),
            }
            for p in first5
        ]
        + [
            {
                **lumec,
                "OWNER_STATE": lumec.get("STATE"),
                "RECIPIENT": None,
                "OWNER_ACTIONS": ["HOLD"],
            }
        ],
        "do_not_send_automatically": True,
        "QUOTE_OUTREACH_DRY_RUN": dry_true,
    }
    _save(UI_FILE, today)

    labels = ["Sauer", "Teledyne", "ABB", "EOI", "Honeywell"]
    first_five_report = []
    for i, p in enumerate(first5):
        first_five_report.append(
            {
                "label": labels[i] if i < len(labels) else p.get("SUPPLIER"),
                "recipient": p.get("TO"),
                "packet": p.get("PACKET_ID"),
                "deadline": p.get("BUYER_DEADLINE"),
                "state": p.get("STATE"),
                "supplier": p.get("SUPPLIER"),
                "buyer": p.get("BUYER"),
                "product": p.get("PRODUCT"),
                "lines": p.get("LINE_COUNT"),
                "response_due": p.get("SUPPLIER_RESPONSE_DUE"),
                "subject": p.get("SUBJECT"),
            }
        )

    report = {
        "BUILD": BUILD,
        "STATUS": final_status,
        "WALL_CLOCK_SECONDS": round(time.time() - wall0, 2),
        "EMAIL": {
            "Provider": provider.get("PROVIDER") if provider.get("CONFIGURED") else (provider.get("PROVIDER") or "NONE"),
            "EMAIL_FROM": "YES" if health.get("FROM_CONFIGURED") else "NO",
            "EMAIL_REPLY_TO": "YES" if health.get("REPLY_TO_CONFIGURED") else "NO",
            "Credentials": "YES" if health.get("PROVIDER_CREDENTIALS_PRESENT") else "NO",
            "Self_test": self_status if self_status in {"PASS", "FAIL", "BLOCKED"} else self_status,
            "Reply_path": reply_path,
            "Dry_run": "TRUE" if dry_true else "FALSE",
            "ENV_PRESENCE": presence,
            "PROVIDER_SELECTION": provider,
            "PROPOSAL": proposal,
            "HEALTH": health,
            "MISSING_OWNER_MUST_ADD": (
                provider.get("MISSING_FOR_RESEND")
                if not provider.get("CONFIGURED")
                else []
            )
            + (
                []
                if provider.get("CONFIGURED")
                else ["OR SMTP: " + ", ".join(provider.get("MISSING_FOR_SMTP") or [])]
            ),
        },
        "SELF_TEST": {
            "STATUS": self_status,
            "transmitted": bool(self_test.get("transmitted")),
            "REPLY_TO_HEADER_CORRECT": self_test.get("REPLY_TO_HEADER_CORRECT"),
            "failure_reason": self_test.get("failure_reason"),
            "to_was_owner_only": True,
        },
        "FIRST_FIVE": first_five_report,
        "OWNER_PREVIEWS": first5,
        "LUMEC": {"State": "HOLD_RECIPIENT_RESEARCH", **{k: lumec.get(k) for k in ("PACKET_ID", "BUYER", "PRODUCT")}},
        "READY_FOR_OWNER_APPROVAL": ready_n,
        "SUPPLIER_EMAILS_ACTUALLY_SENT": 0,
        "DRY_RUN_VIOLATION": dry_violation,
        "FINAL_STATUS": final_status,
        "build_version": BUILD,
    }
    _save(REPORT_FILE, report)
    _job_update(status="DONE", finished_at=_utc(), FINAL_STATUS=final_status, ready=ready_n)
    return report


def start_job(*, force: bool = True, self_test_live: bool = True) -> dict[str, Any]:
    global _worker
    cur = job_progress()
    if cur.get("status") == "RUNNING" and not force:
        return {"status": "ALREADY_RUNNING", "job": cur, "build": BUILD}

    def _run() -> None:
        global _worker
        try:
            run_outbound_email_first5_v1(force=force, self_test_live=self_test_live)
        except Exception as exc:
            _job_update(status="ERROR", error=f"{type(exc).__name__}:{exc}", finished_at=_utc())
        finally:
            _worker = False

    _job_update(status="RUNNING", started_at=_utc(), phase="QUEUED")
    if not _worker:
        _worker = True
        threading.Thread(target=_run, name=FAMILY, daemon=True).start()
    return {"status": "STARTED", "build": BUILD, "job": job_progress()}
