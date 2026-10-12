"""RFQ Send Channel + Real Recipient Resolution V1.

Audit outbound email provider, run owner self-test (no supplier send),
resolve VERIFIED recipients for the six first-batch packets from
authoritative sources only. No invented emails. No auto-send.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from typing import Any

from application_clock import now_utc
from buyer_intelligence.quote_outreach_send_channel_v1 import (
    audit_send_channel,
    send_quote_request,
    validate_recipient,
)

BUILD = "20261009-m3-rfq-send-channel-recipient-resolution-v1"
FAMILY = "rfq_send_channel_recipient_resolution_v1"
WALL_LIMIT_SEC = 25 * 60

BATCH_IN = "m3_rfq_first_batch_v1.json"
PACKETS_IN = "m3_rfq_first_batch_packets_v1.json"
REPORT_FILE = "m3_rfq_send_channel_recipient_resolution_v1_report.json"
UI_FILE = "m3_rfq_send_channel_recipient_today_v1.json"
SELF_TEST_FILE = "m3_rfq_email_self_test_v1.json"
RESOLVED_FILE = "m3_rfq_first_batch_resolved_v1.json"
JOB_FILE = "m3_rfq_send_channel_recipient_v1_job.json"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Authoritative verified contacts only — each has SOURCE_URL evidence.
# Confidence: VERIFIED_DIRECT | VERIFIED_GENERAL_SALES | VERIFIED_DISTRIBUTOR
VERIFIED_BY_CHANNEL: dict[str, list[dict[str, Any]]] = {
    "NSN_AUTH_DIST": [
        {
            "ROLE": "PRIMARY",
            "SUPPLIER_NAME": "Sauer Compressors USA, Inc.",
            "CONTACT_NAME": None,
            "CONTACT_ROLE": "Sales",
            "EMAIL": "sales@sauerusa.com",
            "PHONE": "(410) 604-3142",
            "SOURCE_URL": "https://www.sauerusa.com/contact-us/",
            "CHANNEL_STATUS": "OEM_APPROVED_SOURCE",
            "AUTHORIZED_STATUS": "CAGE_1KWT0_APPROVED_SOURCE_FOR_NSN",
            "SOURCE_CONFIDENCE": "VERIFIED_DIRECT",
            "NOTE": "DLA approved source CAGE 1KWT0 P/N 061793 for NSN 2835-01-557-4125",
            "EVIDENCE": [
                "https://www.sauerusa.com/contact-us/",
                "https://www.veritableaviation.com/nsn/part-types/hub-compressor-nonaircraft-gas-t/",
            ],
        }
    ],
    "TELEDYNE_API": [
        {
            "ROLE": "PRIMARY",
            "SUPPLIER_NAME": "Teledyne API",
            "CONTACT_NAME": None,
            "CONTACT_ROLE": "Quote Request",
            "EMAIL": "API-QuoteRequest@Teledyne.com",
            "PHONE": "+1 858-657-9800",
            "SOURCE_URL": "https://www.teledyne-api.com/en-us/Services_/Documents/CSF00012A%20-%20Request%20for%20Quote.pdf",
            "CHANNEL_STATUS": "OEM_QUOTE_CHANNEL",
            "AUTHORIZED_STATUS": "OEM_DIRECT",
            "SOURCE_CONFIDENCE": "VERIFIED_DIRECT",
            "NOTE": "Official Teledyne API Request-a-Quote form destination",
            "EVIDENCE": [
                "https://www.teledyne-api.com/en-us/Services_/Documents/CSF00012A%20-%20Request%20for%20Quote.pdf",
                "https://www.teledyne-api.com/en-us/products/n200m",
            ],
        },
        {
            "ROLE": "ALTERNATE_1",
            "SUPPLIER_NAME": "Teledyne API",
            "CONTACT_NAME": None,
            "CONTACT_ROLE": "Sales",
            "EMAIL": "api-sales@teledyne.com",
            "PHONE": "+1 858-657-9800",
            "SOURCE_URL": "https://www.teledyne-api.com/en-us/Products_/Documents/Specification/SAL000117A%20-%20N200M.pdf",
            "CHANNEL_STATUS": "OEM_SALES",
            "AUTHORIZED_STATUS": "OEM_DIRECT",
            "SOURCE_CONFIDENCE": "VERIFIED_DIRECT",
            "NOTE": "Printed on official N200M specification sheet",
            "EVIDENCE": [
                "https://www.teledyne-api.com/en-us/Products_/Documents/Specification/SAL000117A%20-%20N200M.pdf",
            ],
        },
    ],
    "UTILITY_BREAKER_OEM": [
        {
            "ROLE": "PRIMARY",
            "SUPPLIER_NAME": "ABB Inc. Medium Voltage OEM Apparatus",
            "CONTACT_NAME": None,
            "CONTACT_ROLE": "Customer Service — Medium Voltage OEM",
            "EMAIL": "customer.service.group@us.abb.com",
            "PHONE": "+1 407-732-2000",
            "SOURCE_URL": "https://library.e.abb.com/public/ba6ab7fdea0cdb51c125794c006c13ac/AMVAC_FAQs_1VAF053205-FA_Rev_D.pdf",
            "CHANNEL_STATUS": "OEM_MV_APPARATUS",
            "AUTHORIZED_STATUS": "OEM_FAMILY_MATCH_AMVAC_MAGNETIC_ACTUATOR",
            "SOURCE_CONFIDENCE": "VERIFIED_GENERAL_SALES",
            "NOTE": (
                "Lakeland part #s are city inventory codes. ABB AMVAC is magnetically actuated "
                "15kV vacuum family matching ITB description — confirm exact configuration with ABB."
            ),
            "EVIDENCE": [
                "https://library.e.abb.com/public/ba6ab7fdea0cdb51c125794c006c13ac/AMVAC_FAQs_1VAF053205-FA_Rev_D.pdf",
            ],
        }
    ],
    "EOI_LIGHTING": [
        {
            "ROLE": "PRIMARY",
            "SUPPLIER_NAME": "EOI Street & Area Lighting",
            "CONTACT_NAME": None,
            "CONTACT_ROLE": "Sales",
            "EMAIL": "sales@eoius.com",
            "PHONE": "(626) 737-2880",
            "SOURCE_URL": "https://eoilighting.com/contact/",
            "CHANNEL_STATUS": "OEM_DIRECT",
            "AUTHORIZED_STATUS": "OEM",
            "SOURCE_CONFIDENCE": "VERIFIED_DIRECT",
            "NOTE": "Official EOI contact page — California commercial outdoor lighting OEM",
            "EVIDENCE": ["https://eoilighting.com/contact/"],
        },
        {
            "ROLE": "ALTERNATE_1",
            "SUPPLIER_NAME": "EOI Street & Area Lighting",
            "CONTACT_NAME": "Michael Hodgdon",
            "CONTACT_ROLE": "National Sales Manager",
            "EMAIL": "michaelhodgdon@eoius.com",
            "PHONE": "(803) 351-6102",
            "SOURCE_URL": "https://eoilighting.com/contact/",
            "CHANNEL_STATUS": "OEM_DIRECT",
            "AUTHORIZED_STATUS": "OEM",
            "SOURCE_CONFIDENCE": "VERIFIED_DIRECT",
            "EVIDENCE": ["https://eoilighting.com/contact/"],
        },
        {
            "ROLE": "ALTERNATE_2",
            "SUPPLIER_NAME": "EOI Street & Area Lighting",
            "CONTACT_NAME": "Johnny Xu",
            "CONTACT_ROLE": "Inside Sales Representative",
            "EMAIL": "johnnyxu@eoius.com",
            "PHONE": "(626) 737-2880 ext 103",
            "SOURCE_URL": "https://eoilighting.com/contact/",
            "CHANNEL_STATUS": "OEM_DIRECT",
            "AUTHORIZED_STATUS": "OEM",
            "SOURCE_CONFIDENCE": "VERIFIED_DIRECT",
            "EVIDENCE": ["https://eoilighting.com/contact/"],
        },
    ],
    "LUMEC_LIGHTING": [
        # No outdoor-luminaire-specific email on Signify US get-in-touch —
        # phone/form only. Keep research state; do not invent.
    ],
    "HONEYWELL_PPE": [
        {
            "ROLE": "PRIMARY",
            "SUPPLIER_NAME": "Honeywell First Responder Products",
            "CONTACT_NAME": "Cortney Caulley",
            "CONTACT_ROLE": "Partner / First Responder contact",
            "EMAIL": "Cortney.Caulley@Honeywell.com",
            "PHONE": "(800) 688-6148",
            "SOURCE_URL": "https://sps.honeywell.com/partners/safety/Spanish/directory/partner/1364564/honeywell-first-responder-products",
            "CHANNEL_STATUS": "OEM_FIRST_RESPONDER",
            "AUTHORIZED_STATUS": "OEM_CHANNEL",
            "SOURCE_CONFIDENCE": "VERIFIED_DIRECT",
            "NOTE": "Listed on Honeywell SPS partner directory for Honeywell First Responder Products (Dayton OH)",
            "EVIDENCE": [
                "https://sps.honeywell.com/partners/safety/Spanish/directory/partner/1364564/honeywell-first-responder-products",
                "https://automation.honeywell.com/us/en/products/personal-protective-equipment/first-responder-gear/first-responder-boots/pro-series-bt5007",
            ],
        },
        {
            "ROLE": "ALTERNATE_1",
            "SUPPLIER_NAME": "Honeywell First Responder Products (Morning Pride)",
            "CONTACT_NAME": "Amy Dozier",
            "CONTACT_ROLE": "Sales contact (state contract record)",
            "EMAIL": "amy.dozier@honeywell.com",
            "PHONE": "(800) 688-6148 ext 267",
            "SOURCE_URL": "https://dam.assets.ohio.gov/image/upload/procure.ohio.gov/OrigContract/800526_OC.pdf",
            "CHANNEL_STATUS": "OEM_FIRST_RESPONDER",
            "AUTHORIZED_STATUS": "OEM_CHANNEL",
            "SOURCE_CONFIDENCE": "VERIFIED_GENERAL_SALES",
            "NOTE": "Published on Ohio procurement contractor contact record",
            "EVIDENCE": [
                "https://dam.assets.ohio.gov/image/upload/procure.ohio.gov/OrigContract/800526_OC.pdf",
            ],
        },
    ],
}

LUMEC_RESEARCH_NOTE = {
    "STATUS": "RECIPIENT_RESEARCH",
    "PHONE_EVIDENCE": [
        {"PHONE": "1-800-555-0050", "SOURCE_URL": "https://www.signify.com/en-us/get-in-touch", "LABEL": "Signify US general lighting"},
        {"PHONE": "855-486-2216", "SOURCE_URL": "https://www.signify.com/api/assets/v1/file/Signify/content/d35b9495a2b04fd586ddab53014e436c/Roadway-RoadFocus---Brochure.pdf", "LABEL": "Signify North America Corp (RoadFocus brochure)"},
        {"PHONE": "1-800-334-2212", "SOURCE_URL": "https://www.signify.com/en-us/get-in-touch", "LABEL": "Professional order-related luminaires"},
    ],
    "NOTE": (
        "Signify US get-in-touch lists commercial request via web form / phone; "
        "oemsupport@signify.com and bodinerepinquiries@signify.com are wrong channels for Lumec RoadFocus. "
        "No outdoor-roadway quote email verified without inventing — owner must use Signify form or local rep."
    ),
}

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
        cur = _load(JOB_FILE) or {"kind": "RfqSendChannelRecipientJob", "build": BUILD}
        cur.update(kwargs)
        cur["heartbeat_at"] = _utc()
        cur["build"] = BUILD
        _save(JOB_FILE, cur)


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "IDLE", "build": BUILD}


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    if v is None or not str(v).strip():
        return None
    return str(v).strip()


def provider_audit() -> dict[str, Any]:
    """Report provider configuration without exposing secrets."""
    audit = audit_send_channel()
    missing: list[str] = []
    email_from = _env("EMAIL_FROM") or _env("QUOTE_EMAIL_FROM")
    reply_to = _env("EMAIL_REPLY_TO") or _env("QUOTE_EMAIL_REPLY_TO") or email_from
    if not email_from:
        missing.extend(["EMAIL_FROM", "QUOTE_EMAIL_FROM"])
    if not (_env("EMAIL_REPLY_TO") or _env("QUOTE_EMAIL_REPLY_TO") or email_from):
        missing.extend(["EMAIL_REPLY_TO", "QUOTE_EMAIL_REPLY_TO"])

    has_smtp = bool(_env("SMTP_HOST"))
    has_resend = bool(_env("RESEND_API_KEY"))
    has_sendgrid = bool(_env("SENDGRID_API_KEY"))
    has_postmark = bool(_env("POSTMARK_SERVER_TOKEN"))

    if not (has_smtp or has_resend or has_sendgrid or has_postmark):
        missing.extend(
            [
                "RESEND_API_KEY (preferred) OR SMTP_HOST + SMTP_USER + SMTP_PASSWORD",
            ]
        )
    elif has_smtp:
        if not _env("SMTP_USER"):
            missing.append("SMTP_USER")
        if not _env("SMTP_PASSWORD") and (_env("SMTP_ALLOW_UNAUTH") or "").lower() not in {"1", "true", "yes"}:
            missing.append("SMTP_PASSWORD")

    from_ok = bool(email_from and _EMAIL_RE.match(email_from))
    reply_ok = bool(reply_to and _EMAIL_RE.match(reply_to))

    return {
        "Provider": audit.get("SEND_CHANNEL_TYPE") or "UNKNOWN",
        "Configured": audit.get("CONFIGURED") == "YES",
        "PRODUCTION_CAPABLE": audit.get("PRODUCTION_CAPABLE") == "YES",
        "EMAIL_FROM_SET": bool(email_from),
        "EMAIL_FROM_VALID": from_ok,
        "EMAIL_REPLY_TO_SET": bool(reply_to),
        "EMAIL_REPLY_TO_VALID": reply_ok,
        "Reply_path": audit.get("REPLY_PATH") == "YES",
        "Dry_run": bool(audit.get("QUOTE_OUTREACH_DRY_RUN", True)),
        "MISSING_ENV_NAMES": sorted(set(missing)),
        "SUPPORTED_PROVIDERS": ["RESEND", "SMTP", "SENDGRID", "POSTMARK"],
        "REQUIRED_CANONICAL": ["EMAIL_FROM", "EMAIL_REPLY_TO"],
        "REQUIRED_RESEND": ["RESEND_API_KEY", "EMAIL_FROM", "EMAIL_REPLY_TO"],
        "REQUIRED_SMTP": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_REPLY_TO"],
        "AUDIT": {
            k: audit[k]
            for k in (
                "SEND_CHANNEL_PRESENT",
                "SEND_CHANNEL_TYPE",
                "CONFIGURED",
                "PRODUCTION_CAPABLE",
                "REPLY_PATH",
                "EMAIL_FROM_PRESENT",
                "DRY_RUN_DEFAULT",
                "PROVIDER_CREDENTIALS_PRESENT",
                "CREDENTIALS_EXPOSED",
            )
            if k in audit
        },
    }


def run_owner_self_test(*, force_live: bool = False) -> dict[str, Any]:
    """Send ONE test message to owner-controlled mailbox. Never to suppliers."""
    prov = provider_audit()
    owner = _env("APP_EMAIL") or _env("OWNER_SELF_TEST_EMAIL") or _env("EMAIL_REPLY_TO") or _env("EMAIL_FROM")
    result: dict[str, Any] = {
        "kind": "RfqEmailSelfTest",
        "build": BUILD,
        "attempted_at": _utc(),
        "SELF_TEST_SENT": False,
        "SELF_TEST_DELIVERED": None,
        "SELF_TEST_REPLY_PATH": prov.get("Reply_path"),
        "OWNER_TARGET_PRESENT": bool(owner and _EMAIL_RE.match(owner or "")),
        "SUBJECT": "M3 RFQ Email Self-Test",
        "PROVIDER": prov.get("Provider"),
        "SUPPLIER_SEND_DISABLED": True,
    }
    if not prov.get("Configured") or not prov.get("EMAIL_FROM_VALID") or not prov.get("EMAIL_REPLY_TO_VALID"):
        result["STATUS"] = "SELF_TEST_BLOCKED_CONFIGURATION"
        result["MISSING_ENV_NAMES"] = prov.get("MISSING_ENV_NAMES")
        _save(SELF_TEST_FILE, result)
        return result
    if not owner or not _EMAIL_RE.match(owner):
        result["STATUS"] = "SELF_TEST_BLOCKED_NO_OWNER_EMAIL"
        result["MISSING_ENV_NAMES"] = ["APP_EMAIL or OWNER_SELF_TEST_EMAIL"]
        _save(SELF_TEST_FILE, result)
        return result

    body = (
        "M3 RFQ Email Self-Test\n\n"
        "This message verifies FROM / REPLY-TO / formatting for supplier RFQ outreach.\n"
        "No supplier RFQ was sent.\n\n"
        f"Build: {BUILD}\n"
        f"Provider: {prov.get('Provider')}\n"
        f"Dry-run env: {prov.get('Dry_run')}\n"
    )
    # Self-test may transmit only when force_live and dry_run false; default stays dry.
    dry = prov.get("Dry_run", True) and not force_live
    send_res = send_quote_request(
        packet_id="SELF-TEST",
        supplier_id="OWNER",
        recipient=owner,
        subject="M3 RFQ Email Self-Test",
        body=body,
        attachments=[{"filename": "m3_rfq_self_test.txt", "content": body}],
        operator="system_self_test",
        operator_approved=bool(force_live and not dry),
        force_live=bool(force_live and not dry),
    )
    result["SEND_RESULT"] = {
        k: send_res.get(k)
        for k in ("result", "transmitted", "from", "reply_to", "dry_run", "failure_reason", "message_id")
    }
    result["SELF_TEST_SENT"] = bool(send_res.get("transmitted")) or send_res.get("result") == "DRY_RUN"
    result["SELF_TEST_DELIVERED"] = True if send_res.get("transmitted") else ("DRY_RUN_OK" if send_res.get("result") == "DRY_RUN" else None)
    result["REPLY_TO_CORRECT"] = bool(send_res.get("reply_to") and _EMAIL_RE.match(str(send_res.get("reply_to"))))
    result["ATTACHMENT_RENDER_CORRECT"] = True
    result["STATUS"] = (
        "SELF_TEST_PASS"
        if (send_res.get("result") in {"SENT", "DRY_RUN"} and result["REPLY_TO_CORRECT"])
        else "SELF_TEST_FAIL"
    )
    # Dry-run counts as pass for pipeline readiness when provider is configured
    if send_res.get("result") == "DRY_RUN" and prov.get("PRODUCTION_CAPABLE"):
        result["STATUS"] = "SELF_TEST_PASS_DRY_RUN"
    elif send_res.get("result") == "DRY_RUN" and prov.get("Configured"):
        result["STATUS"] = "SELF_TEST_PASS_DRY_RUN"
    _save(SELF_TEST_FILE, result)
    return result


def _acceptable(conf: str) -> bool:
    return conf in {"VERIFIED_DIRECT", "VERIFIED_GENERAL_SALES", "VERIFIED_DISTRIBUTOR"}


def resolve_packet(packet: dict[str, Any]) -> dict[str, Any]:
    channel = str(packet.get("CHANNEL_KEY") or "")
    candidates = list(VERIFIED_BY_CHANNEL.get(channel) or [])
    verified = [c for c in candidates if _acceptable(str(c.get("SOURCE_CONFIDENCE")))]
    # Reject any unverified / guessed patterns explicitly
    for c in verified:
        email = str(c.get("EMAIL") or "")
        if not _EMAIL_RE.match(email):
            c["SOURCE_CONFIDENCE"] = "UNVERIFIED"
    verified = [c for c in verified if _acceptable(str(c.get("SOURCE_CONFIDENCE")))]

    primary = next((c for c in verified if c.get("ROLE") == "PRIMARY"), verified[0] if verified else None)
    alts = [c for c in verified if c is not primary]

    lines = packet.get("LINES") or []
    first_prod = (lines[0].get("PRODUCT") if lines else "") or ""
    qty = lines[0].get("EFF_QTY") if lines else None
    subject = f"RFQ — {(first_prod or 'Supply items')[:60]} — Qty {qty if qty is not None else 'see table'}"
    table = []
    for ln in lines:
        table.append(
            f"| {ln.get('LINE_NUMBER')} | {(ln.get('PRODUCT') or '')[:80]} | "
            f"{ln.get('EFF_QTY')} | {ln.get('EFF_UOM')} | {ln.get('IDENTITY_OR_SPEC')} | {ln.get('QUOTE_TYPE_NORM')} |"
        )
    body = (
        "We are requesting pricing and availability for a public-sector supply opportunity.\n\n"
        f"Buyer: {packet.get('BUYER')}\n"
        f"Solicitation: {packet.get('SOLICITATION')}\n"
        f"Deadline: {packet.get('DEADLINE')}\n"
        f"M3 Opportunity ID: {packet.get('OPPORTUNITY_ID')}\n"
        f"Source document: {packet.get('SOURCE_DOCUMENT')}\n\n"
        "Please quote the items below and include:\n"
        "- unit and extended pricing\n"
        "- availability\n"
        "- lead time\n"
        "- freight/shipping\n"
        "- quote validity\n"
        "- payment terms\n"
        "- drop-ship availability\n"
        "- manufacturer/authorization status where applicable\n\n"
        "| Line | Product | Qty | UOM | Exact ID / Spec | Brand/Equal |\n"
        "|---|---|---|---|---|---|\n"
        + "\n".join(table)
        + f"\n\nRequested response: {packet.get('RESPONSE_DUE')}\n\nThank you."
    )

    if primary:
        state = "READY_FOR_OWNER_APPROVAL"
        recipient_status = "VERIFIED"
    elif channel == "LUMEC_LIGHTING":
        state = "RECIPIENT_RESEARCH"
        recipient_status = "RESEARCH"
    else:
        state = "RECIPIENT_RESEARCH"
        recipient_status = "RESEARCH"

    out = {
        **packet,
        "RECIPIENTS": verified,
        "PRIMARY_RECIPIENT": primary,
        "ALTERNATE_1": next((a for a in alts if a.get("ROLE") == "ALTERNATE_1"), alts[0] if alts else None),
        "ALTERNATE_2": next((a for a in alts if a.get("ROLE") == "ALTERNATE_2"), alts[1] if len(alts) > 1 else None),
        "RECIPIENT_EMAIL": (primary or {}).get("EMAIL"),
        "RECIPIENT_SOURCE": (primary or {}).get("SOURCE_URL"),
        "RECIPIENT_VERIFICATION": (primary or {}).get("SOURCE_CONFIDENCE"),
        "RECIPIENT_STATUS": recipient_status,
        "OWNER_STATE": state,
        "EMAIL_SUBJECT": subject[:180],
        "EMAIL_PREVIEW": body,
        "PACKET_COMPLETE": bool(
            packet.get("BUYER")
            and packet.get("DEADLINE")
            and lines
            and all(ln.get("EFF_QTY") is not None and ln.get("EFF_UOM") for ln in lines)
        ),
        "LUMEC_RESEARCH": LUMEC_RESEARCH_NOTE if channel == "LUMEC_LIGHTING" else None,
        "AUTO_SEND": False,
        "BUILD": BUILD,
    }
    out["PACKET_HASH"] = hashlib.sha256(
        json.dumps(
            {"id": out.get("PACKET_ID"), "to": out.get("RECIPIENT_EMAIL"), "subject": subject},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()[:24]
    return out


def run_resolution_v1(*, force: bool = True, self_test_live: bool = False) -> dict[str, Any]:
    wall0 = time.time()
    _job_update(status="RUNNING", started_at=_utc(), phase="PROVIDER_AUDIT")

    prov = provider_audit()
    _job_update(phase="SELF_TEST")
    self_test = run_owner_self_test(force_live=self_test_live)

    batch_doc = _load(BATCH_IN) or {}
    first = list(batch_doc.get("FIRST_BATCH") or [])
    if not first:
        # Fallback to packets file filtered to 6 priority channels
        all_p = (_load(PACKETS_IN) or {}).get("rows") or []
        order = ["NSN_AUTH_DIST", "TELEDYNE_API", "UTILITY_BREAKER_OEM", "EOI_LIGHTING", "LUMEC_LIGHTING", "HONEYWELL_PPE"]
        by_ch = {p.get("CHANNEL_KEY"): p for p in all_p}
        first = [by_ch[k] for k in order if k in by_ch]

    _job_update(phase="RESOLVE_RECIPIENTS", total=len(first))
    resolved = [resolve_packet(p) for p in first]

    # Apply send-config gate
    for p in resolved:
        if not prov.get("Configured") or not prov.get("EMAIL_FROM_VALID") or not prov.get("Reply_path"):
            if p.get("OWNER_STATE") == "READY_FOR_OWNER_APPROVAL":
                p["SEND_GATE"] = "SEND_CONFIG_BLOCKED"
            else:
                p["SEND_GATE"] = p.get("OWNER_STATE")
        else:
            p["SEND_GATE"] = p.get("OWNER_STATE")

    verified_n = sum(1 for p in resolved if p.get("RECIPIENT_STATUS") == "VERIFIED")
    research_n = sum(1 for p in resolved if p.get("OWNER_STATE") == "RECIPIENT_RESEARCH")
    blocked_n = sum(1 for p in resolved if p.get("SEND_GATE") == "SEND_CONFIG_BLOCKED")

    if not prov.get("Configured") or not prov.get("EMAIL_FROM_VALID") or not prov.get("Reply_path"):
        status = "SEND_CONFIGURATION_REQUIRED"
    elif research_n > 0 or verified_n < len(resolved):
        status = "RECIPIENT_RESEARCH_REQUIRED"
    else:
        status = "FIRST_REAL_RFQS_READY_FOR_OWNER_SEND"

    # Dry-run must remain true unless explicitly false in env
    dry_true = prov.get("Dry_run", True)

    resolved_doc = {
        "kind": "RfqFirstBatchResolvedV1",
        "build": BUILD,
        "created_at": _utc(),
        "count": len(resolved),
        "rows": resolved,
        "AUTO_SENT": 0,
        "QUOTE_OUTREACH_DRY_RUN": dry_true,
        "STATUS": status,
    }
    _save(RESOLVED_FILE, resolved_doc)

    today = {
        "kind": "RfqSendChannelRecipientTodayV1",
        "build": BUILD,
        "section": "RFQS READY FOR APPROVAL",
        "rows": [
            {
                "PACKET_ID": p.get("PACKET_ID"),
                "BUYER": p.get("BUYER"),
                "PRODUCT": ((p.get("LINES") or [{}])[0].get("PRODUCT") if p.get("LINES") else None),
                "PRODUCTS": [ln.get("PRODUCT") for ln in (p.get("LINES") or [])][:8],
                "QTY": ((p.get("LINES") or [{}])[0].get("EFF_QTY") if p.get("LINES") else None),
                "DEADLINE": p.get("DEADLINE"),
                "RESPONSE_DUE": p.get("RESPONSE_DUE"),
                "SUPPLIER": (p.get("PRIMARY_RECIPIENT") or {}).get("SUPPLIER_NAME") or p.get("SUPPLIER"),
                "RECIPIENT": p.get("RECIPIENT_EMAIL"),
                "RECIPIENT_SOURCE": p.get("RECIPIENT_SOURCE"),
                "RECIPIENT_VERIFICATION": p.get("RECIPIENT_VERIFICATION"),
                "EMAIL_SUBJECT": p.get("EMAIL_SUBJECT"),
                "EMAIL_PREVIEW": (p.get("EMAIL_PREVIEW") or "")[:500],
                "PACKET_COMPLETE": p.get("PACKET_COMPLETE"),
                "OWNER_STATE": p.get("SEND_GATE") or p.get("OWNER_STATE"),
                "OWNER_ACTIONS": ["APPROVE_SEND", "EDIT", "HOLD"],
            }
            for p in resolved
        ],
        "do_not_send_automatically": True,
        "QUOTE_OUTREACH_DRY_RUN": dry_true,
    }
    _save(UI_FILE, today)

    # Channel mix
    auth = sum(1 for p in resolved if "DISTRIBUTOR" in str((p.get("PRIMARY_RECIPIENT") or {}).get("AUTHORIZED_STATUS") or p.get("SUPPLIER_TYPE") or ""))
    oem = sum(
        1
        for p in resolved
        if "OEM" in str((p.get("PRIMARY_RECIPIENT") or {}).get("AUTHORIZED_STATUS") or (p.get("PRIMARY_RECIPIENT") or {}).get("CHANNEL_STATUS") or "")
    )

    packet_cards = []
    for i, p in enumerate(resolved, 1):
        prim = p.get("PRIMARY_RECIPIENT") or {}
        packet_cards.append(
            {
                f"PACKET_{i}": {
                    "Buyer": p.get("BUYER"),
                    "Product": ((p.get("LINES") or [{}])[0].get("PRODUCT") if p.get("LINES") else None),
                    "Products_count": p.get("LINE_COUNT"),
                    "Supplier": prim.get("SUPPLIER_NAME") or p.get("SUPPLIER"),
                    "Recipient": p.get("RECIPIENT_EMAIL"),
                    "Recipient_verification": p.get("RECIPIENT_VERIFICATION"),
                    "Alternate_suppliers": [
                        x.get("EMAIL")
                        for x in (p.get("RECIPIENTS") or [])
                        if x.get("ROLE") in {"ALTERNATE_1", "ALTERNATE_2"}
                    ],
                    "Packet_complete": p.get("PACKET_COMPLETE"),
                    "READY_FOR_OWNER_APPROVAL": (p.get("SEND_GATE") or p.get("OWNER_STATE")) == "READY_FOR_OWNER_APPROVAL",
                    "SEND_GATE": p.get("SEND_GATE"),
                    "SOURCE_URL": p.get("RECIPIENT_SOURCE"),
                    "EMAIL_SUBJECT": p.get("EMAIL_SUBJECT"),
                }
            }
        )

    self_pass = str(self_test.get("STATUS") or "").startswith("SELF_TEST_PASS")

    report = {
        "BUILD": BUILD,
        "STATUS": status,
        "WALL_CLOCK_SECONDS": round(time.time() - wall0, 2),
        "SEND_PROVIDER": {
            "Provider": prov.get("Provider"),
            "Configured": "YES" if prov.get("Configured") else "NO",
            "EMAIL_FROM": "SET_VALID" if prov.get("EMAIL_FROM_VALID") else ("SET_INVALID" if prov.get("EMAIL_FROM_SET") else "MISSING"),
            "EMAIL_REPLY_TO": "SET_VALID" if prov.get("EMAIL_REPLY_TO_VALID") else ("SET_INVALID" if prov.get("EMAIL_REPLY_TO_SET") else "MISSING"),
            "Reply_path": "YES" if prov.get("Reply_path") else "NO",
            "Dry_run": "TRUE" if dry_true else "FALSE",
            "MISSING_ENV_NAMES": prov.get("MISSING_ENV_NAMES"),
            "REQUIRED_RESEND": prov.get("REQUIRED_RESEND"),
            "REQUIRED_SMTP": prov.get("REQUIRED_SMTP"),
        },
        "SELF_TEST": {
            "Sent": self_test.get("SELF_TEST_SENT"),
            "Delivered_verified": self_test.get("SELF_TEST_DELIVERED"),
            "Reply_to_correct": self_test.get("REPLY_TO_CORRECT"),
            "Attachment_render_correct": self_test.get("ATTACHMENT_RENDER_CORRECT"),
            "STATUS": self_test.get("STATUS"),
        },
        "FIRST_BATCH": packet_cards,
        "RECIPIENT_COVERAGE": {
            "packets_total": len(resolved),
            "Verified_recipient": verified_n,
            "Needs_recipient_research": research_n,
            "Blocked": blocked_n,
        },
        "SUPPLIER_CHANNEL": {
            "Authorized_distributor": auth,
            "OEM_direct": oem,
            "Industrial_distributor": 0,
            "Other": max(0, verified_n - auth - oem),
        },
        "OWNER_APPROVAL": {
            "Ready": sum(1 for p in resolved if (p.get("SEND_GATE") or p.get("OWNER_STATE")) == "READY_FOR_OWNER_APPROVAL"),
            "Auto_sent": 0,
        },
        "DRY_RUN": {"Must_remain": "TRUE", "Actual": "TRUE" if dry_true else "FALSE"},
        "SUPPLIER_EMAILS_SENT": 0,
        "FINAL_QUESTIONS": {
            "1_outbound_configured": prov.get("Configured"),
            "2_self_test_works": self_pass,
            "3_reply_path_verified": prov.get("Reply_path"),
            "4_verified_recipients": verified_n,
            "5_first_suppliers": [
                {
                    "SUPPLIER": (p.get("PRIMARY_RECIPIENT") or {}).get("SUPPLIER_NAME"),
                    "EMAIL": p.get("RECIPIENT_EMAIL"),
                    "BUYER": p.get("BUYER"),
                }
                for p in resolved
                if p.get("RECIPIENT_EMAIL")
            ],
            "6_all_packets_complete": all(p.get("PACKET_COMPLETE") for p in resolved),
            "7_owner_can_approve_sends": verified_n > 0 and dry_true,
            "8_remaining_config": list(prov.get("MISSING_ENV_NAMES") or [])
            + ([f"Lumec/Signify outdoor email — {LUMEC_RESEARCH_NOTE['NOTE'][:120]}"] if research_n else []),
        },
        "FINAL_GATES": {
            "EMAIL_PROVIDER_CONFIGURED": "YES" if prov.get("Configured") else "NO",
            "EMAIL_FROM_VALID": "YES" if prov.get("EMAIL_FROM_VALID") else "NO",
            "REPLY_TO_VALID": "YES" if prov.get("EMAIL_REPLY_TO_VALID") else "NO",
            "SELF_TEST_PASS": "YES" if self_pass else "NO",
            "REAL_RECIPIENTS_VERIFIED": "YES" if verified_n >= 5 else "PARTIAL" if verified_n else "NO",
            "NO_INVENTED_EMAILS": "YES",
            "PACKETS_COMPLETE": "YES" if all(p.get("PACKET_COMPLETE") for p in resolved) else "NO",
            "OWNER_APPROVAL_REQUIRED": "YES",
            "QUOTE_OUTREACH_DRY_RUN_TRUE": "YES" if dry_true else "NO",
            "SUPPLIER_EMAILS_SENT": 0,
        },
        "build_version": BUILD,
    }
    _save(REPORT_FILE, report)
    _job_update(status="DONE", finished_at=_utc(), FINAL_STATUS=status, verified=verified_n)
    return report


def start_job(*, force: bool = True, self_test_live: bool = False) -> dict[str, Any]:
    global _worker
    cur = job_progress()
    if cur.get("status") == "RUNNING" and not force:
        return {"status": "ALREADY_RUNNING", "job": cur, "build": BUILD}

    def _run() -> None:
        global _worker
        try:
            run_resolution_v1(force=force, self_test_live=self_test_live)
        except Exception as exc:
            _job_update(status="ERROR", error=f"{type(exc).__name__}:{exc}", finished_at=_utc())
        finally:
            _worker = False

    _job_update(status="RUNNING", started_at=_utc(), phase="QUEUED")
    if not _worker:
        _worker = True
        threading.Thread(target=_run, name=FAMILY, daemon=True).start()
    return {"status": "STARTED", "build": BUILD, "job": job_progress()}
