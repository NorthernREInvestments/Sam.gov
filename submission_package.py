"""Proposal package detection, submission checklist, and CO questions."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from models import Contract, ContractAttachment

EASTERN = ZoneInfo("America/New_York")
SUBMISSION_METHODS = ("Email", "PIEE", "SAM.gov", "Mail", "Unknown")
EVAL_TYPES = ("LPTA", "Technical", "Unknown")

PRICING_NAME_RE = re.compile(
    r"pric|cost\s*sched|clin|contract\s*line\s*item|schedule\s*of\s*suppl|price\s*sched|"
    r"supplies\s*or\s*services|offer\s*sheet",
    re.I,
)
PRICING_CONTENT_RE = re.compile(
    r"(unit\s*price|quantity\s+unit|clin\s*\d|line\s*item\s*no|price\s*schedule|"
    r"schedule\s*of\s*supplies)",
    re.I,
)
MULTI_PRICING_RE = re.compile(
    r"multiple\s+pric|alternative\s+proposal|offerors\s+are\s+encouraged\s+to\s+submit\s+alternative|"
    r"offerors\s+may\s+submit\s+more\s+than\s+one|pricing\s+options|alternatives\s+are\s+encouraged",
    re.I,
)
SF1449_RE = re.compile(r"sf[\s\-]?1449|standard\s+form\s+1449", re.I)
EMAIL_SUBMIT_RE = re.compile(
    r"submit\s+(?:via|by|through)\s+email|email\s+(?:your\s+)?(?:proposal|offer|quote)\s+to|"
    r"send\s+(?:your\s+)?(?:proposal|offer)\s+to\s+[\w.\-+]+@",
    re.I,
)
PIEE_RE = re.compile(r"\bpiee\b|procurement\s+integrated\s+enterprise\s+environment", re.I)
SAM_SUBMIT_RE = re.compile(r"submit\s+(?:via|through|on)\s+sam\.gov|sam\.gov\s+workspace", re.I)
MAIL_RE = re.compile(r"mail\s+(?:your\s+)?(?:proposal|offer)\s+to|postal\s+mail|u\.s\.\s+mail", re.I)
EMAIL_ADDR_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")
LPTA_RE = re.compile(r"\blpta\b|lowest\s+price\s+technically\s+acceptable", re.I)
TECH_EVAL_RE = re.compile(
    r"best\s+value|technical\s+evaluation|trade[\s-]?off|rated\s+on\s+technical", re.I
)
QUESTIONS_DEADLINE_RE = re.compile(
    r"questions?\s+(?:must\s+be\s+)?(?:received|submitted)\s+(?:by|no\s+later\s+than)\s+([^\n.;]{6,40})",
    re.I,
)


CHECKLIST_TEMPLATE: list[dict[str, Any]] = [
    {"key": "incumbent_researched", "section": "pre_research", "label": "Incumbent price researched", "has_notes": True},
    {"key": "evaluation_criteria", "section": "pre_research", "label": "Evaluation criteria identified — LPTA or Technical", "has_notes": True},
    {"key": "submission_method_confirmed", "section": "pre_research", "label": "Submission method confirmed", "has_notes": True},
    {"key": "questions_submitted", "section": "pre_research", "label": "Questions submitted to CO if needed", "na_allowed": True},
    {"key": "technical_proposal", "section": "proposal_docs", "label": "Technical proposal generated and reviewed"},
    {"key": "past_performance", "section": "proposal_docs", "label": "Past performance section complete — references from selected sub"},
    {"key": "pricing_section", "section": "proposal_docs", "label": "Pricing section complete"},
    {"key": "pricing_spreadsheet", "section": "proposal_docs", "label": "Pricing spreadsheet completed if required", "conditional": "pricing_schedule"},
    {"key": "multiple_pricing", "section": "proposal_docs", "label": "Multiple pricing options prepared if encouraged", "conditional": "multiple_pricing"},
    {"key": "sf1449", "section": "proposal_docs", "label": "SF-1449 completed if required", "conditional": "sf1449"},
    {"key": "reps_certs", "section": "attachments", "label": "Reps and Certs downloaded from SAM.gov"},
    {"key": "all_forms", "section": "attachments", "label": "All required forms attached"},
    {"key": "amendments", "section": "attachments", "label": "All solicitation amendments acknowledged"},
    {"key": "compliance_review", "section": "final", "label": "Proposal reviewed for compliance with all solicitation instructions"},
    {"key": "page_limits", "section": "final", "label": "Page limits verified if applicable", "na_allowed": True},
    {"key": "deadline_timezone", "section": "final", "label": "Submission deadline confirmed including time zone"},
    {"key": "submission_method_final", "section": "final", "label": "Submission method confirmed"},
    {"key": "proposal_submitted", "section": "submission", "label": "Proposal submitted", "records_timestamp": True},
    {"key": "submission_confirmation", "section": "submission", "label": "Submission confirmation saved", "has_notes": True},
]

SECTION_LABELS = {
    "pre_research": "Pre-Submission Research",
    "proposal_docs": "Proposal Documents",
    "attachments": "Required Attachments",
    "final": "Final Checks",
    "submission": "Submission",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text[:40], fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
    if m:
        y = int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def normalize_submission_method(raw: str | None) -> str:
    if not raw:
        return "Unknown"
    text = raw.lower()
    if EMAIL_SUBMIT_RE.search(text) or "@" in text:
        return "Email"
    if PIEE_RE.search(text):
        return "PIEE"
    if SAM_SUBMIT_RE.search(text) or "sam.gov" in text:
        return "SAM.gov"
    if MAIL_RE.search(text):
        return "Mail"
    for label in SUBMISSION_METHODS:
        if label.lower() in text:
            return label
    return "Unknown"


def extract_submission_email(text: str) -> str | None:
    for pat in (
        r"submit(?:ted|tal)?\s+(?:to|via)\s+([\w.\-+]+@[\w.\-]+\.\w+)",
        r"email\s+(?:proposals?|offers?)\s+to\s+([\w.\-+]+@[\w.\-]+\.\w+)",
        r"send\s+(?:proposals?|offers?)\s+to\s+([\w.\-+]+@[\w.\-]+\.\w+)",
    ):
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).lower()
    addrs = EMAIL_ADDR_RE.findall(text)
    for addr in addrs:
        low = addr.lower()
        if not any(x in low for x in ("sam.gov", "gsa.gov", "example.com")):
            return low
    return None


def _attachment_candidates(contract: Contract, session: Session) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in session.query(ContractAttachment).filter_by(contract_id=contract.id).all():
        items.append(
            {
                "id": row.id,
                "filename": row.filename,
                "text": (row.extracted_text or "")[:50000],
                "source": "stored",
            }
        )
    sam = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    for att in (sam.get("opportunityAttachments") or []) + (sam.get("pieeAttachments") or []):
        if not isinstance(att, dict):
            continue
        name = att.get("name") or att.get("fileName") or att.get("description") or ""
        items.append({"id": None, "filename": name, "text": "", "source": "sam_meta"})
    return items


def detect_pricing_schedule(contract: Contract, session: Session, full_text: str) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    for att in _attachment_candidates(contract, session):
        name = att.get("filename") or ""
        text = att.get("text") or ""
        name_hit = bool(PRICING_NAME_RE.search(name))
        content_hit = bool(PRICING_CONTENT_RE.search(text)) if text else False
        if name_hit or content_hit:
            score = (2 if name_hit else 0) + (1 if content_hit else 0)
            if not best or score > best.get("score", 0):
                best = {
                    "required": True,
                    "filename": name,
                    "attachment_id": att.get("id"),
                    "score": score,
                    "reason": "Filename or content matches pricing schedule patterns.",
                }
    if not best and PRICING_CONTENT_RE.search(full_text):
        best = {
            "required": True,
            "filename": None,
            "attachment_id": None,
            "score": 1,
            "reason": "Attachment text contains pricing line-item tables.",
        }
    return best or {"required": False, "filename": None, "attachment_id": None, "reason": None}


def detect_from_text(full_text: str) -> dict[str, Any]:
    return {
        "multiple_pricing_encouraged": bool(MULTI_PRICING_RE.search(full_text)),
        "sf1449_required": bool(SF1449_RE.search(full_text)),
        "evaluation_criteria_type": (
            "LPTA"
            if LPTA_RE.search(full_text)
            else "Technical"
            if TECH_EVAL_RE.search(full_text)
            else "Unknown"
        ),
        "questions_deadline_raw": (
            QUESTIONS_DEADLINE_RE.search(full_text).group(1).strip()
            if QUESTIONS_DEADLINE_RE.search(full_text)
            else None
        ),
        "submission_method_raw": _infer_method_from_text(full_text),
        "submission_email": extract_submission_email(full_text),
    }


def _infer_method_from_text(text: str) -> str:
    if PIEE_RE.search(text):
        return "PIEE"
    if SAM_SUBMIT_RE.search(text):
        return "SAM.gov"
    if MAIL_RE.search(text):
        return "Mail"
    if EMAIL_SUBMIT_RE.search(text) or extract_submission_email(text):
        return "Email"
    return "Unknown"


def merge_claude_package(analysis: dict[str, Any]) -> dict[str, Any]:
    pkg = analysis.get("submission_package") if isinstance(analysis.get("submission_package"), dict) else {}
    sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}
    out: dict[str, Any] = {}
    out["pricing_schedule_required"] = bool(pkg.get("pricing_schedule_required"))
    out["pricing_schedule_filename"] = pkg.get("pricing_schedule_filename")
    out["multiple_pricing_encouraged"] = bool(pkg.get("multiple_pricing_encouraged"))
    out["sf1449_required"] = bool(pkg.get("sf1449_required"))
    method = pkg.get("submission_method") or sol.get("submission_method") or analysis.get("submission_method")
    out["submission_method"] = normalize_submission_method(str(method) if method else None)
    out["submission_email"] = pkg.get("submission_email") or extract_submission_email(str(method or ""))
    out["evaluation_criteria_type"] = pkg.get("evaluation_criteria") or pkg.get("evaluation_criteria_type") or "Unknown"
    if out["evaluation_criteria_type"] not in EVAL_TYPES:
        out["evaluation_criteria_type"] = normalize_eval_type(str(out["evaluation_criteria_type"]))
    qd = pkg.get("questions_deadline") or sol.get("questions_deadline")
    out["questions_deadline"] = _parse_date(str(qd)) if qd else None
    return out


def normalize_eval_type(raw: str) -> str:
    if LPTA_RE.search(raw):
        return "LPTA"
    if TECH_EVAL_RE.search(raw) or "technical" in raw.lower():
        return "Technical"
    return "Unknown"


def apply_submission_package(contract: Contract, session: Session, *, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run heuristics + merge Claude extraction; persist on contract."""
    analysis = analysis if isinstance(analysis, dict) else (contract.analysis or {})
    full_text = contract.attachment_text or ""
    heur = detect_from_text(full_text)
    pricing = detect_pricing_schedule(contract, session, full_text)
    claude = merge_claude_package(analysis)

    contract.pricing_schedule_required = pricing.get("required") or claude.get("pricing_schedule_required")
    if pricing.get("attachment_id"):
        contract.pricing_schedule_attachment_id = pricing["attachment_id"]
    elif claude.get("pricing_schedule_filename"):
        for att in _attachment_candidates(contract, session):
            fn = att.get("filename") or ""
            if claude["pricing_schedule_filename"].lower() in fn.lower() and att.get("id"):
                contract.pricing_schedule_attachment_id = att["id"]
                break

    contract.multiple_pricing_encouraged = heur["multiple_pricing_encouraged"] or claude.get(
        "multiple_pricing_encouraged", False
    )
    contract.sf1449_required = heur["sf1449_required"] or claude.get("sf1449_required", False)

    method = claude.get("submission_method") or normalize_submission_method(heur.get("submission_method_raw"))
    if method == "Unknown" and heur.get("submission_method_raw"):
        method = normalize_submission_method(heur["submission_method_raw"])
    contract.submission_method = method if method in SUBMISSION_METHODS else "Unknown"

    email = claude.get("submission_email") or heur.get("submission_email")
    if not email and contract.submission_method == "Email":
        sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}
        email = extract_submission_email(str(sol.get("submission_method") or ""))
    contract.submission_email = email

    eval_type = claude.get("evaluation_criteria_type") or heur.get("evaluation_criteria_type") or "Unknown"
    contract.evaluation_criteria_type = eval_type if eval_type in EVAL_TYPES else "Unknown"

    qd = claude.get("questions_deadline") or _parse_date(heur.get("questions_deadline_raw"))
    if qd:
        contract.questions_deadline = qd

    if contract.co_questions is None or not contract.co_questions:
        contract.co_questions = generate_co_questions(contract, analysis)

    _ensure_checklist_initialized(contract)
    merged = submission_package_dict(contract, session)
    analysis = dict(analysis)
    analysis["submission_package"] = merged
    contract.analysis = analysis
    return merged


def generate_co_questions(contract: Contract, analysis: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    analysis = analysis or {}
    items: list[dict[str, Any]] = []

    def add(text: str) -> None:
        items.append(
            {
                "id": uuid.uuid4().hex[:12],
                "text": text,
                "asked": False,
                "asked_date": None,
                "response": "",
                "resolved": False,
                "auto_generated": True,
            }
        )

    if contract.submission_method in (None, "Unknown") or not contract.submission_method_confirmed:
        add("How should offers be submitted for this solicitation?")
    if contract.pricing_schedule_required:
        add("Is there a required pricing format or spreadsheet for this solicitation?")
    pkg = analysis.get("proposal_requirements") if isinstance(analysis.get("proposal_requirements"), dict) else {}
    sec_m = pkg.get("section_m") if isinstance(pkg.get("section_m"), dict) else {}
    if not sec_m.get("evaluation_factors") and contract.evaluation_criteria_type == "Unknown":
        add("Is past performance from a proposed subcontractor acceptable in lieu of prime contractor past performance?")
    if contract.far_52219_14_present and contract.subcontracting_limitation_check == "NOT_FOUND":
        add(
            "Can you confirm that FAR 52.219-14 Limitations on Subcontracting does not apply to this solicitation?"
        )
    if contract.wage_determination_rate is None:
        add("What wage determination applies to this solicitation?")
    scope = (analysis.get("plain_english_summary") or "").lower()
    if any(k in scope for k in ("on-site", "onsite", "physically present", "resident")):
        add("Does this contract require the prime contractor to have personnel physically present at the facility?")
    elif "on-site" in (contract.attachment_text or "").lower()[:8000]:
        add("Does this contract require the prime contractor to have personnel physically present at the facility?")

    claude_q = analysis.get("co_questions_suggested")
    if isinstance(claude_q, list):
        for q in claude_q:
            if isinstance(q, str) and q.strip():
                add(q.strip())

    return items


def _ensure_checklist_initialized(contract: Contract) -> None:
    if isinstance(contract.submission_checklist, dict) and contract.submission_checklist.get("items"):
        return
    items = []
    for tpl in CHECKLIST_TEMPLATE:
        items.append(
            {
                "key": tpl["key"],
                "checked": False,
                "na": False,
                "na_reason": "",
                "notes": "",
                "checked_at": None,
            }
        )
    contract.submission_checklist = {"items": items, "updated_at": _now().isoformat()}


def _item_applicable(contract: Contract, tpl: dict[str, Any]) -> bool:
    cond = tpl.get("conditional")
    if cond == "pricing_schedule":
        return bool(contract.pricing_schedule_required)
    if cond == "multiple_pricing":
        return bool(contract.multiple_pricing_encouraged)
    if cond == "sf1449":
        return bool(contract.sf1449_required)
    return True


def checklist_view(contract: Contract) -> dict[str, Any]:
    _ensure_checklist_initialized(contract)
    stored = {i["key"]: i for i in (contract.submission_checklist or {}).get("items", []) if isinstance(i, dict)}
    sections: dict[str, list[dict[str, Any]]] = {k: [] for k in SECTION_LABELS}
    applicable = 0
    complete = 0
    for tpl in CHECKLIST_TEMPLATE:
        if not _item_applicable(contract, tpl):
            entry = {
                **tpl,
                "applicable": False,
                "checked": False,
                "na": True,
                "na_reason": "N/A",
                "notes": stored.get(tpl["key"], {}).get("notes", ""),
                "checked_at": None,
                "status": "na",
            }
        else:
            applicable += 1
            raw = stored.get(tpl["key"], {})
            checked = bool(raw.get("checked"))
            na = bool(raw.get("na"))
            if checked or na:
                complete += 1
            entry = {
                **tpl,
                "applicable": True,
                "checked": checked,
                "na": na,
                "na_reason": raw.get("na_reason") or "",
                "notes": raw.get("notes") or "",
                "checked_at": raw.get("checked_at"),
                "status": "done" if checked or na else "pending",
            }
        sections.setdefault(tpl["section"], []).append(entry)

    pct = round(complete / applicable * 100) if applicable else 0
    all_done = applicable > 0 and complete >= applicable
    return {
        "sections": [{"key": k, "label": SECTION_LABELS[k], "items": sections.get(k, [])} for k in SECTION_LABELS],
        "completion_pct": pct,
        "complete_count": complete,
        "applicable_count": applicable,
        "all_complete": all_done,
        "ready_message": "Checklist complete — ready to submit." if all_done else None,
        "reps_certs_instructions": REPS_CERTS_INSTRUCTIONS,
        "sf1449_instructions": SF1449_INSTRUCTIONS if contract.sf1449_required else None,
    }


REPS_CERTS_INSTRUCTIONS = (
    "Download your Representations and Certifications from SAM.gov before submitting. "
    "Log into SAM.gov, go to your workspace, find your entity, click Representations and Certifications, "
    "and download as PDF. Attach this PDF to every proposal submission."
)

SF1449_INSTRUCTIONS = (
    "This solicitation uses an SF-1449 form. Download it from the attachments, complete the following fields: "
    "Block 17a — your CAGE code or UEI, Block 17b — your business name and address, Block 30a — sign, "
    "Block 30b — complete, Block 30c — complete. If the form is already digitally signed by the government "
    "you must either print and sign manually or screenshot and convert to PDF to make it editable. "
    "Do not submit an unsigned form."
)


def update_checklist_item(
    contract: Contract,
    item_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    _ensure_checklist_initialized(contract)
    items = contract.submission_checklist.get("items", [])
    found = None
    for item in items:
        if item.get("key") == item_key:
            found = item
            break
    if not found:
        raise ValueError(f"Unknown checklist item: {item_key}")

    if "checked" in payload:
        found["checked"] = bool(payload["checked"])
        if found["checked"]:
            found["checked_at"] = _now().isoformat()
            found["na"] = False
    if "na" in payload:
        found["na"] = bool(payload["na"])
        if found["na"]:
            found["checked"] = False
    if "na_reason" in payload:
        found["na_reason"] = payload["na_reason"] or ""
    if "notes" in payload:
        found["notes"] = payload["notes"] or ""

    tpl = next((t for t in CHECKLIST_TEMPLATE if t["key"] == item_key), {})
    if tpl.get("records_timestamp") and found.get("checked"):
        found["checked_at"] = _now().isoformat()

    contract.submission_checklist = {
        "items": items,
        "updated_at": _now().isoformat(),
    }
    return checklist_view(contract)


def update_co_question(contract: Contract, question_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    questions = list(contract.co_questions or [])
    target = next((q for q in questions if q.get("id") == question_id), None)
    if not target:
        raise ValueError("Question not found")
    if "text" in payload:
        target["text"] = payload["text"]
    if "asked" in payload:
        target["asked"] = bool(payload["asked"])
        if target["asked"] and not target.get("asked_date"):
            target["asked_date"] = date.today().isoformat()
    if "response" in payload:
        target["response"] = payload["response"] or ""
    if "resolved" in payload:
        target["resolved"] = bool(payload["resolved"])
    contract.co_questions = questions
    return questions


def deadline_display(contract: Contract) -> dict[str, Any]:
    if not contract.due_date:
        return {
            "due_date": None,
            "label": "Deadline not confirmed — check solicitation immediately.",
            "days_remaining": None,
            "hours_remaining": None,
            "urgency": "unknown",
            "timezone_note": TIMEZONE_NOTE,
            "alert_24h": False,
        }
    now_et = datetime.now(EASTERN)
    deadline_et = datetime.combine(contract.due_date, time(23, 59, 59), tzinfo=EASTERN)
    delta = deadline_et - now_et
    hours = max(0, int(delta.total_seconds() // 3600))
    days = hours // 24
    rem_hours = hours % 24
    if days > 7:
        urgency = "green"
    elif days >= 3:
        urgency = "yellow"
    else:
        urgency = "red"
    label = f"{days} days, {rem_hours} hours remaining"
    if hours <= 0:
        label = "Deadline passed"
        urgency = "red"
    return {
        "due_date": contract.due_date.isoformat(),
        "label": label,
        "days_remaining": days,
        "hours_remaining": hours,
        "urgency": urgency,
        "timezone_note": TIMEZONE_NOTE,
        "alert_24h": hours < 24 and hours >= 0,
    }


TIMEZONE_NOTE = (
    "All federal deadlines are typically Eastern Time unless otherwise stated in the solicitation. "
    "Verify the time zone before submitting."
)


def submission_package_dict(contract: Contract, session: Session) -> dict[str, Any]:
    pricing_att = None
    if contract.pricing_schedule_attachment_id:
        row = session.get(ContractAttachment, contract.pricing_schedule_attachment_id)
        if row:
            pricing_att = {
                "id": row.id,
                "filename": row.filename,
                "file_size_bytes": row.file_size_bytes,
            }
    method = contract.submission_method or "Unknown"
    method_detail: dict[str, Any] = {"method": method}
    if method == "Email":
        method_detail["email"] = contract.submission_email
        method_detail["warning"] = None
    elif method == "PIEE":
        method_detail["link"] = "https://piee.eb.mil"
        method_detail["instructions"] = "Log in to PIEE and upload your proposal package."
    elif method == "Unknown":
        method_detail["warning"] = "Submission method not confirmed. Check the solicitation or contact the CO before submitting."

    return {
        "pricing_schedule_required": bool(contract.pricing_schedule_required),
        "pricing_schedule_attachment": pricing_att,
        "pricing_schedule_label": "PRICING SCHEDULE — REQUIRED" if contract.pricing_schedule_required else None,
        "multiple_pricing_encouraged": bool(contract.multiple_pricing_encouraged),
        "sf1449_required": bool(contract.sf1449_required),
        "submission_method": method,
        "submission_email": contract.submission_email,
        "submission_method_confirmed": bool(contract.submission_method_confirmed),
        "submission_method_notes": contract.submission_method_notes,
        "submission_method_detail": method_detail,
        "evaluation_criteria_type": contract.evaluation_criteria_type,
        "questions_deadline": contract.questions_deadline.isoformat() if contract.questions_deadline else None,
        "deadline": deadline_display(contract),
        "reps_certs_instructions": REPS_CERTS_INSTRUCTIONS,
        "sf1449_instructions": SF1449_INSTRUCTIONS if contract.sf1449_required else None,
        "pricing_schedule_warning": (
            "This solicitation includes a required pricing schedule. You must download, complete, and attach "
            "this document. Do not substitute your own pricing format."
            if contract.pricing_schedule_required
            else None
        ),
    }


def get_attachment_bytes(session: Session, contract_id: int, attachment_id: int) -> ContractAttachment:
    row = session.get(ContractAttachment, attachment_id)
    if not row or row.contract_id != contract_id:
        raise ValueError("Attachment not found")
    return row
