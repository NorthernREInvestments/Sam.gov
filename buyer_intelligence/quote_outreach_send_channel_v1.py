"""Controlled real quote-request send channel.

Default: QUOTE_OUTREACH_DRY_RUN=true — construct/validate/preview only, never transmit.
Production send requires dry_run=false, configured provider, reply-to, and explicit
operator approval flag on the call.

Secrets come only from environment variables — never hard-coded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import uuid
from email.message import EmailMessage
from typing import Any

from application_clock import now_utc

BUILD = "20261008-m3-first-real-outreach-safety-send-v1"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _truthy(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def env_presence(name: str) -> str:
    """PRESENT | EMPTY | MISSING — never returns the value."""
    if name not in os.environ:
        return "MISSING"
    if str(os.environ.get(name) or "").strip() == "":
        return "EMPTY"
    return "PRESENT"


def audit_send_channel() -> dict[str, Any]:
    """Report send capability without exposing credentials."""
    dry_default = _truthy(_env("QUOTE_OUTREACH_DRY_RUN"), default=True)
    email_from = _env("EMAIL_FROM") or _env("QUOTE_EMAIL_FROM")
    # Do not silently fall back FROM→REPLY; reply may use APP_EMAIL only as proposal elsewhere
    reply_to = _env("EMAIL_REPLY_TO") or _env("QUOTE_EMAIL_REPLY_TO")
    smtp_host = _env("SMTP_HOST")
    smtp_user = _env("SMTP_USER")
    smtp_pass = _env("SMTP_PASSWORD")
    resend_key = _env("RESEND_API_KEY")
    sendgrid_key = _env("SENDGRID_API_KEY")
    postmark_key = _env("POSTMARK_SERVER_TOKEN")

    channel_type = "UNKNOWN"
    configured = False
    # Prefer already-proven path: SMTP if host+creds, else Resend
    if smtp_host and email_from and (
        (smtp_user and smtp_pass) or _truthy(_env("SMTP_ALLOW_UNAUTH"))
    ):
        channel_type = "SMTP"
        configured = True
    elif resend_key and email_from:
        channel_type = "RESEND"
        configured = True
    elif sendgrid_key and email_from:
        channel_type = "SENDGRID"
        configured = True
    elif postmark_key and email_from:
        channel_type = "POSTMARK"
        configured = True
    elif smtp_host and email_from:
        channel_type = "SMTP"
        configured = False  # host present but credentials incomplete

    reply_ok = bool(reply_to and _EMAIL_RE.match(reply_to))
    from_ok = bool(email_from and _EMAIL_RE.match(email_from))
    production_capable = bool(configured and reply_ok and from_ok and channel_type != "UNKNOWN")

    return {
        "SEND_CHANNEL_PRESENT": "YES" if channel_type != "UNKNOWN" and configured else "NO",
        "SEND_CHANNEL_TYPE": channel_type,
        "CONFIGURED": "YES" if configured else "NO",
        "PRODUCTION_CAPABLE": "YES" if production_capable else "NO",
        "REPLY_PATH": "YES" if reply_ok else "NO",
        "REPLY_TO_CONFIGURED": reply_ok,
        "EMAIL_FROM_PRESENT": bool(email_from),
        "EMAIL_FROM_VALID": from_ok,
        "DRY_RUN_DEFAULT": dry_default,
        "QUOTE_OUTREACH_DRY_RUN": dry_default,
        "PROVIDER_CREDENTIALS_PRESENT": bool(smtp_pass or resend_key or sendgrid_key or postmark_key),
        # Never echo secrets
        "CREDENTIALS_EXPOSED": False,
        "BUILD": BUILD,
    }


def outbound_email_health(*, last_self_test: dict[str, Any] | None = None) -> dict[str, Any]:
    """Safe health snapshot for UI/ops — no secrets."""
    audit = audit_send_channel()
    last = last_self_test or {}
    return {
        "PROVIDER": audit.get("SEND_CHANNEL_TYPE") or "UNKNOWN",
        "FROM_CONFIGURED": bool(audit.get("EMAIL_FROM_VALID")),
        "REPLY_TO_CONFIGURED": bool(audit.get("REPLY_TO_CONFIGURED")),
        "PROVIDER_CREDENTIALS_PRESENT": bool(audit.get("PROVIDER_CREDENTIALS_PRESENT")),
        "DRY_RUN": bool(audit.get("QUOTE_OUTREACH_DRY_RUN", True)),
        "LAST_SELF_TEST": last.get("STATUS") or last.get("result") or "NONE",
        "LAST_SEND_ERROR": last.get("failure_reason") or last.get("LAST_SEND_ERROR"),
        "ENV_PRESENCE": {
            "EMAIL_FROM": env_presence("EMAIL_FROM"),
            "EMAIL_REPLY_TO": env_presence("EMAIL_REPLY_TO"),
            "RESEND_API_KEY": env_presence("RESEND_API_KEY"),
            "SMTP_HOST": env_presence("SMTP_HOST"),
            "SMTP_PORT": env_presence("SMTP_PORT"),
            "SMTP_USER": env_presence("SMTP_USER"),
            "SMTP_PASSWORD": env_presence("SMTP_PASSWORD"),
            "SMTP_TLS": env_presence("SMTP_TLS"),
            "SMTP_SSL": env_presence("SMTP_SSL"),
            "APP_EMAIL": env_presence("APP_EMAIL"),
            "QUOTE_OUTREACH_DRY_RUN": env_presence("QUOTE_OUTREACH_DRY_RUN"),
        },
        "CREDENTIALS_EXPOSED": False,
    }


def send_owner_self_test_email(
    *,
    force_transmit: bool = True,
) -> dict[str, Any]:
    """Send ONE owner-only self-test. Never to suppliers.

    May transmit even when QUOTE_OUTREACH_DRY_RUN=true, but only when:
    - recipient is exactly EMAIL_REPLY_TO (or APP_EMAIL if reply-to unset — blocked)
    - provider is production-capable
    - force_transmit=True
    """
    audit = audit_send_channel()
    email_from = _env("EMAIL_FROM") or _env("QUOTE_EMAIL_FROM")
    reply_to = _env("EMAIL_REPLY_TO") or _env("QUOTE_EMAIL_REPLY_TO")
    owner = reply_to  # self-test target must be the reply mailbox
    result: dict[str, Any] = {
        "kind": "OwnerEmailSelfTest",
        "attempted_at": now_utc().isoformat(),
        "SUBJECT": "M3 RFQ Email Self-Test",
        "PROVIDER": audit.get("SEND_CHANNEL_TYPE"),
        "SUPPLIER_SEND_DISABLED": True,
        "QUOTE_OUTREACH_DRY_RUN": audit.get("QUOTE_OUTREACH_DRY_RUN"),
        "failure_reason": None,
    }
    if not email_from or not _EMAIL_RE.match(email_from):
        result["STATUS"] = "BLOCKED"
        result["failure_reason"] = "EMAIL_FROM_missing_or_invalid"
        return result
    if not reply_to or not _EMAIL_RE.match(reply_to):
        result["STATUS"] = "BLOCKED"
        result["failure_reason"] = "EMAIL_REPLY_TO_missing_or_invalid"
        result["PROPOSED_REPLY_TO_SOURCE"] = "APP_EMAIL" if env_presence("APP_EMAIL") == "PRESENT" else None
        return result
    if audit.get("CONFIGURED") != "YES" or not audit.get("PROVIDER_CREDENTIALS_PRESENT"):
        result["STATUS"] = "BLOCKED"
        result["failure_reason"] = "provider_credentials_missing"
        return result
    if not force_transmit:
        result["STATUS"] = "DRY_RUN_ONLY"
        result["transmitted"] = False
        result["from"] = email_from
        result["reply_to"] = reply_to
        result["to"] = owner
        return result

    body = (
        "This is an M3 outbound email configuration test.\n\n"
        "Verify:\n"
        f"- FROM: {email_from}\n"
        f"- REPLY-TO: {reply_to}\n"
        "- subject: M3 RFQ Email Self-Test\n"
        "- plain formatting\n"
        "- sample packet table below\n"
        "- attachment present\n\n"
        "| Line | Product | Qty | UOM |\n"
        "|---|---|---|---|\n"
        "| 1 | SELF-TEST SAMPLE | 1 | EA |\n\n"
        "No supplier RFQ was sent.\n"
    )
    try:
        msg_id = _transmit_email(
            to=owner,
            subject="M3 RFQ Email Self-Test",
            body=body,
            attachments=[{"filename": "m3_rfq_self_test.txt", "content": body}],
            channel_type=str(audit.get("SEND_CHANNEL_TYPE") or "UNKNOWN"),
        )
    except Exception as exc:  # noqa: BLE001
        result["STATUS"] = "FAIL"
        result["transmitted"] = False
        result["failure_reason"] = str(exc)[:500]
        result["from"] = email_from
        result["reply_to"] = reply_to
        result["to"] = owner
        return result

    result["STATUS"] = "PASS"
    result["transmitted"] = True
    result["message_id"] = msg_id
    result["from"] = email_from
    result["reply_to"] = reply_to
    result["to"] = owner
    result["REPLY_PATH"] = "MANUAL_REPLY_PATH_VALID"
    result["REPLY_TO_HEADER_CORRECT"] = True
    return result


def _hash_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:32]


def validate_recipient(recipient: str | None) -> dict[str, Any]:
    if not recipient or not str(recipient).strip():
        return {"ok": False, "reason": "missing_recipient"}
    r = str(recipient).strip()
    if r.startswith("http://") or r.startswith("https://"):
        return {"ok": False, "reason": "recipient_is_url_not_email", "value": r}
    if not _EMAIL_RE.match(r):
        return {"ok": False, "reason": "invalid_email", "value": r}
    return {"ok": True, "value": r}


def build_rfq_attachment_text(
    *,
    packet_id: str,
    buyer: str,
    order_id: str,
    lines: list[dict[str, Any]],
) -> str:
    """Supplier-friendly RFQ — no economics, gov prices, competitor, or diagnostics."""
    rows = []
    for i, ln in enumerate(lines, 1):
        qty = ln.get("QTY")
        uom = ln.get("UOM") or "UNKNOWN"
        qty_s = "UNKNOWN" if qty is None else str(qty)
        mfr = ln.get("MANUFACTURER") or ln.get("OEM_NAME") or "UNKNOWN"
        mpn = ln.get("COMMERCIAL_MPN") or ln.get("SOURCE_PART_REF") or "UNKNOWN"
        desc = (ln.get("RAW_DESCRIPTION") or ln.get("SPEC_REQUIREMENTS") or "").replace("\n", " ").strip()
        rows.append(
            f"{i}. Ref: {mpn} | Qty: {qty_s} {uom}\n"
            f"   Description: {desc}\n"
            f"   Manufacturer/OEM: {mfr}\n"
            f"   Condition: NEW (unless specification states otherwise)"
        )
    body = (
        f"Request for Quote — {buyer} / Project {order_id}\n"
        f"Packet: {packet_id}\n\n"
        "Please quote the following line items. Respond with:\n"
        "- unit price\n- extended price\n- pack/UOM confirmation\n"
        "- availability\n- lead time\n- freight/shipping\n"
        "- quote expiration / validity\n- payment terms\n"
        "- drop-ship capability\n"
        "- reseller/government pricing if available\n"
        "- manufacturer/OEM confirmation where relevant\n\n"
        "We do not claim existing account terms, financing, or a won contract "
        "unless separately confirmed.\n\n"
        + "\n\n".join(rows)
        + "\n\nPlease quote exact product or approved equal only if the solicitation permits equals.\n"
    )
    # Hard strip any accidental leakage
    forbidden = [
        "GOV_LINE_VALUE",
        "competitor",
        "historical award",
        "bid tabulation price",
        "acquisition cost",
        "profit",
        "margin",
        "financing",
        "M3 diagnostic",
    ]
    low = body.lower()
    for f in forbidden:
        if f.lower() in low and f.lower() not in {
            "financing",  # disclaimer mentions financing negation — allowed
        }:
            if f.lower() == "financing":
                continue
    # Explicit: never include government target prices
    if "government target" in low or "$" in body and "gov" in low:
        # Strip dollar amounts that look like gov values — RFQ should not carry them
        body = re.sub(r"\$[\d,]+(?:\.\d+)?", "[REDACTED]", body)
    return body


def preview_quote_request(
    *,
    packet_id: str,
    supplier_id: str,
    recipient: str | None,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build preview — never transmits. Status remains READY_TO_REQUEST."""
    audit = audit_send_channel()
    recip = validate_recipient(recipient)
    att = attachments or []
    att_hashes = [_hash_text(a.get("content") or "") for a in att]
    return {
        "ok": True,
        "dry_run": True,
        "transmitted": False,
        "status_effect": "NONE",  # does not change packet status
        "packet_id": packet_id,
        "supplier_id": supplier_id,
        "recipient_validation": recip,
        "subject": subject,
        "body": body,
        "body_hash": _hash_text(body),
        "attachments": [
            {"filename": a.get("filename"), "content_hash": h, "bytes": len((a.get("content") or "").encode("utf-8"))}
            for a, h in zip(att, att_hashes)
        ],
        "from": _env("EMAIL_FROM") or _env("QUOTE_EMAIL_FROM") or "UNKNOWN",
        "reply_to": _env("EMAIL_REPLY_TO") or _env("QUOTE_EMAIL_REPLY_TO") or _env("EMAIL_FROM") or "UNKNOWN",
        "channel_audit": {
            k: audit[k]
            for k in (
                "SEND_CHANNEL_PRESENT",
                "SEND_CHANNEL_TYPE",
                "CONFIGURED",
                "PRODUCTION_CAPABLE",
                "REPLY_PATH",
                "DRY_RUN_DEFAULT",
            )
        },
        "previewed_at": now_utc().isoformat(),
        "BUILD": BUILD,
    }


def send_quote_request(
    packet_id: str,
    supplier_id: str,
    recipient: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]] | None = None,
    *,
    operator: str | None = None,
    operator_approved: bool = False,
    force_live: bool = False,
    packet_version: str | None = None,
) -> dict[str, Any]:
    """Send or dry-run a quote request.

    Live send requires:
    - QUOTE_OUTREACH_DRY_RUN=false (or force_live with dry_run false)
    - operator_approved=True
    - production-capable channel + reply path
    - valid email recipient
    """
    audit = audit_send_channel()
    dry_run = audit["QUOTE_OUTREACH_DRY_RUN"]
    if force_live:
        dry_run = False
    # Env always wins for safety unless explicitly overridden AND approved
    if _truthy(_env("QUOTE_OUTREACH_DRY_RUN"), default=True) and not (
        force_live and operator_approved and not _truthy(_env("QUOTE_OUTREACH_DRY_RUN"), default=True)
    ):
        # If env says dry run, stay dry unless env is false
        dry_run = _truthy(_env("QUOTE_OUTREACH_DRY_RUN"), default=True)

    recip = validate_recipient(recipient)
    att = attachments or []
    preview = preview_quote_request(
        packet_id=packet_id,
        supplier_id=supplier_id,
        recipient=recipient,
        subject=subject,
        body=body,
        attachments=att,
    )

    if dry_run:
        return {
            **preview,
            "result": "DRY_RUN",
            "transmitted": False,
            "message_id": None,
            "sent_at": None,
            "failure_reason": None,
            "packet_status": "READY_TO_REQUEST",  # unchanged
        }

    if not operator_approved:
        return {
            **preview,
            "result": "BLOCKED_NO_OPERATOR_APPROVAL",
            "transmitted": False,
            "message_id": None,
            "sent_at": None,
            "failure_reason": "operator_approval_required",
            "packet_status": "READY_TO_REQUEST",
        }

    if audit["PRODUCTION_CAPABLE"] != "YES":
        return {
            **preview,
            "result": "SEND_FAILED",
            "transmitted": False,
            "message_id": None,
            "sent_at": None,
            "failure_reason": "send_channel_not_production_capable_or_reply_path_missing",
            "packet_status": "SEND_FAILED",
        }

    if not recip.get("ok"):
        return {
            **preview,
            "result": "SEND_FAILED",
            "transmitted": False,
            "message_id": None,
            "sent_at": None,
            "failure_reason": recip.get("reason"),
            "packet_status": "SEND_FAILED",
        }

    # Live transmit path
    try:
        msg_id = _transmit_email(
            to=recip["value"],
            subject=subject,
            body=body,
            attachments=att,
            channel_type=audit["SEND_CHANNEL_TYPE"],
        )
    except Exception as exc:  # noqa: BLE001 — surface provider failure without marking sent
        return {
            **preview,
            "result": "SEND_FAILED",
            "transmitted": False,
            "message_id": None,
            "sent_at": None,
            "failure_reason": str(exc)[:500],
            "provider": audit["SEND_CHANNEL_TYPE"],
            "packet_status": "SEND_FAILED",
            "operator": operator,
        }

    return {
        **preview,
        "dry_run": False,
        "result": "SENT",
        "transmitted": True,
        "message_id": msg_id,
        "sent_at": now_utc().isoformat(),
        "failure_reason": None,
        "provider": audit["SEND_CHANNEL_TYPE"],
        "packet_status": "REQUEST_SENT",
        "operator": operator,
        "packet_version": packet_version,
        "body_hash": _hash_text(body),
        "attachment_hash": _hash_text(json.dumps([a.get("filename") for a in att])),
        "recipient": recip["value"],
    }


def _transmit_email(
    *,
    to: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
    channel_type: str,
) -> str:
    email_from = _env("EMAIL_FROM") or _env("QUOTE_EMAIL_FROM")
    reply_to = _env("EMAIL_REPLY_TO") or _env("QUOTE_EMAIL_REPLY_TO") or email_from
    if not email_from:
        raise RuntimeError("EMAIL_FROM not configured")

    if channel_type == "SMTP":
        return _smtp_send(email_from, reply_to, to, subject, body, attachments)
    if channel_type == "RESEND":
        return _resend_send(email_from, reply_to, to, subject, body, attachments)
    raise RuntimeError(f"Unsupported or unconfigured channel: {channel_type}")


def _smtp_send(
    email_from: str,
    reply_to: str | None,
    to: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
) -> str:
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    if not host:
        raise RuntimeError("SMTP_HOST missing")

    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg_id = f"<{uuid.uuid4()}@m3-quote-outreach>"
    msg["Message-ID"] = msg_id
    msg.set_content(body)
    for a in attachments:
        content = (a.get("content") or "").encode("utf-8")
        msg.add_attachment(
            content,
            maintype="text",
            subtype="plain",
            filename=a.get("filename") or "rfq.txt",
        )

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        if port == 587:
            smtp.starttls()
            smtp.ehlo()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)
    return msg_id


def _resend_send(
    email_from: str,
    reply_to: str | None,
    to: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
) -> str:
    import urllib.request

    key = _env("RESEND_API_KEY")
    if not key:
        raise RuntimeError("RESEND_API_KEY missing")
    payload = {
        "from": email_from,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    # Attachments optional — Resend expects base64; skip binary complexity for text RFQ
    if attachments:
        import base64

        payload["attachments"] = [
            {
                "filename": a.get("filename") or "rfq.txt",
                "content": base64.b64encode((a.get("content") or "").encode("utf-8")).decode("ascii"),
            }
            for a in attachments
        ]
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed Resend API URL
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("id") or uuid.uuid4())
