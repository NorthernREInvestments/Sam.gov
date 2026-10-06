"""Full-file eligibility mining — scan solicitation package for bid killers.

Build: 20261004-m3-eligibility-liveprice-v2
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from company_eligibility import set_aside_eligibility
from document_quality import extract_pdf_text
from eligibility_and_recovery.models import (
    BID_ELIGIBLE,
    BID_ELIGIBLE_WITH_ACTION,
    BID_INELIGIBLE,
    ELIGIBILITY_UNKNOWN,
)
from eligibility_gate import (
    ELIGIBLE_CONDITIONAL,
    ELIGIBLE_CONFIRMED,
    ELIGIBILITY_NOT_APPLICABLE,
    ELIGIBILITY_UNKNOWN as GATE_UNKNOWN,
    NOT_CURRENTLY_ELIGIBLE,
    evaluate_eligibility_gate,
    load_company_eligibility_profile,
)
from m3_data_root import data_path

BUILD = "20261004-m3-eligibility-liveprice-v2"

# FAR/DFARS clause patterns → resolution template
_FAR_CLAUSES: list[tuple[re.Pattern[str], str, str, str]] = [
    # pattern, clause_id, plain_meaning, default_status
    (re.compile(r"\b52\.219[-–]14\b|\bLimitations?\s+on\s+Subcontracting\b", re.I), "FAR 52.219-14", "Limitations on subcontracting for set-asides", "ACTION_REQUIRED"),
    (re.compile(r"\b52\.219[-–]6\b|\bNotice\s+of\s+Total\s+Small\s+Business\s+Set[- ]Aside\b", re.I), "FAR 52.219-6", "Total small business set-aside", "APPLIES"),
    (re.compile(r"\b52\.219[-–]27\b|\bNotice\s+of\s+Service[- ]Disabled\s+Veteran", re.I), "FAR 52.219-27", "SDVOSB set-aside", "BID_BLOCKER"),
    (re.compile(r"\b52\.219[-–]29\b|\bNotice\s+of\s+Set[- ]Aside\s+for.{0,20}Economically\s+Disadvantaged\s+Women", re.I), "FAR 52.219-29", "EDWOSB set-aside", "BID_BLOCKER"),
    (re.compile(r"\b52\.219[-–]30\b|\bNotice\s+of\s+Set[- ]Aside\s+for.{0,20}Women[- ]Owned", re.I), "FAR 52.219-30", "WOSB set-aside", "BID_BLOCKER"),
    (re.compile(r"\b52\.219[-–]3\b|\bNotice\s+of\s+HUBZone", re.I), "FAR 52.219-3", "HUBZone set-aside", "BID_BLOCKER"),
    (re.compile(r"\b52\.219[-–]8\b|\bUtilization\s+of\s+Small\s+Business", re.I), "FAR 52.219-8", "Small business subcontracting plan may apply", "NEEDS_REVIEW"),
    (re.compile(r"\b52\.225[-–]1\b|\bBuy\s+American[-–]Supplies\b", re.I), "FAR 52.225-1", "Buy American Act for supplies", "ACTION_REQUIRED"),
    (re.compile(r"\b52\.225[-–]5\b|\bTrade\s+Agreements\b", re.I), "FAR 52.225-5", "Trade Agreements Act (TAA) country restrictions", "ACTION_REQUIRED"),
    (re.compile(r"\b52\.204[-–]21\b|\bBasic\s+Safeguarding\s+of\s+Covered\s+Contractor", re.I), "FAR 52.204-21", "Basic cyber safeguarding", "ACTION_REQUIRED"),
    (re.compile(r"\b252\.204[-–]7012\b|\bSafeguarding\s+Covered\s+Defense", re.I), "DFARS 252.204-7012", "NIST 800-171 / covered defense information", "ACTION_REQUIRED"),
    (re.compile(r"\b252\.225[-–]7009\b|\bSpecialty\s+Metals\b", re.I), "DFARS 252.225-7009", "Specialty metals domestic sourcing", "ACTION_REQUIRED"),
    (re.compile(r"\b252\.225[-–]7012\b|\bPreference\s+for\s+Certain\s+Domestic\s+Commodities\b|\bBerry\s+Amendment\b", re.I), "DFARS 252.225-7012", "Berry Amendment domestic commodities", "BID_BLOCKER"),
    (re.compile(r"\bCMMC\s+Level\s+[23]\b", re.I), "CMMC", "Cybersecurity Maturity Model Certification required", "BID_BLOCKER"),
]

_FATAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:total|100%|exclusive)\s+SDVOSB\s+set[- ]aside\b|\bonly\s+SDVOSB\b|\bService[- ]Disabled\s+Veteran.{0,40}set[- ]aside\b", re.I), "SDVOSB_SET_ASIDE"),
    (re.compile(r"\b(?:total|exclusive)\s+WOSB\s+set[- ]aside\b|\bonly\s+WOSB\b|\bWomen[- ]Owned\s+Small\s+Business.{0,40}set[- ]aside\b", re.I), "WOSB_SET_ASIDE"),
    (re.compile(r"\bEDWOSB\s+set[- ]aside\b|\bEconomically\s+Disadvantaged\s+Women", re.I), "EDWOSB_SET_ASIDE"),
    (re.compile(r"\bHUBZone\s+set[- ]aside\b|\bonly\s+HUBZone\b", re.I), "HUBZONE_SET_ASIDE"),
    (re.compile(r"\b8\(a\)\s+set[- ]aside\b|\bonly\s+8\(a\)\b|\b8a\s+set[- ]aside\b", re.I), "8A_SET_ASIDE"),
    (re.compile(r"\bonly\s+(?:open\s+to\s+)?(?:current\s+|active\s+)?(?:IDIQ|MATOC|MAC|BPA|GSA\s+Schedule|SEWP|GWAC)\s+holders?\b", re.I), "MANDATORY_VEHICLE"),
    (re.compile(r"\bfacility\s+clearance\s+required\b|\bmust\s+(?:possess|hold).{0,40}facility\s+clearance\b", re.I), "FACILITY_CLEARANCE"),
    (re.compile(r"\bpersonnel\s+clearance\s+required\b|\bsecret\s+clearance\s+required\b", re.I), "PERSONNEL_CLEARANCE"),
    (re.compile(r"\bCMMC\s+Level\s+[23]\b|\bmust\s+be\s+CMMC", re.I), "CMMC_REQUIRED"),
    (re.compile(r"\bapproved\s+source\s+(?:list|only)\b|\bQPL\s+required\b|\bsource\s+approval\s+required\b", re.I), "SOURCE_APPROVAL"),
    (re.compile(r"\bonly\s+authorized\s+(?:OEM\s+)?(?:dealers?|distributors?)\b|\bmust\s+be\s+an?\s+authorized\s+(?:dealer|distributor)\b", re.I), "DEALER_AUTHORIZATION"),
    (re.compile(r"\bBerry\s+Amendment\b", re.I), "BERRY_AMENDMENT"),
]

_ACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bvendor\s+registration\b|\bsupplier\s+registration\b|\bmust\s+register\b", re.I), "VENDOR_REGISTRATION"),
    (re.compile(r"\bcertificate\s+of\s+insurance\b|\binsurance\s+(?:certificate|endorsement|COI)\b", re.I), "INSURANCE_COI"),
    (re.compile(r"\bW-?9\b|\btax\s+identification\b", re.I), "W9_SUBMISSION"),
    (re.compile(r"\bbid\s+bond\b|\bbid\s+guarantee\b|\bperformance\s+bond\b|\bpayment\s+bond\b", re.I), "BONDING"),
    (re.compile(r"\bmandatory\s+site\s+visit\b|\bpre[- ]bid\s+(?:conference|meeting)\b", re.I), "SITE_VISIT_OR_PREBID"),
    (re.compile(r"\bsample(?:s)?\s+required\b|\bproduct\s+sample\b", re.I), "SAMPLES_REQUIRED"),
    (re.compile(r"\bBuy\s+American\b|\bdomestic\s+(?:preference|content|origin)\b", re.I), "BUY_AMERICAN"),
    (re.compile(r"\bTrade\s+Agreements?\s+Act\b|\bTAA\b", re.I), "TAA"),
    (re.compile(r"\bactive\s+SAM\b|\bmust\s+be\s+registered\s+in\s+SAM\b", re.I), "SAM_ACTIVE"),
    (re.compile(r"\bCAGE\s+code\s+required\b|\bmust\s+have.{0,20}CAGE\b", re.I), "CAGE_REQUIRED"),
    (re.compile(r"\bportal\s+setup\b|\bcreate\s+an?\s+account\b", re.I), "PORTAL_SETUP"),
    (re.compile(r"\bminimum\s+(?:\d+|one|two|three|five)\s+years?\s+(?:in\s+business|experience)\b", re.I), "PAST_PERFORMANCE_YEARS"),
    (re.compile(r"\blocal\s+(?:office|preference|residency)\b|\bmust\s+maintain.{0,40}office\b", re.I), "LOCAL_REQUIREMENT"),
]

_SUPPORTED_EXT = {".pdf", ".txt", ".html", ".htm", ".md", ".csv", ".docx", ".doc", ".xlsx", ".xls"}


def _map_gate(overall: str) -> str:
    if overall in {ELIGIBLE_CONFIRMED, ELIGIBILITY_NOT_APPLICABLE}:
        return BID_ELIGIBLE
    if overall == ELIGIBLE_CONDITIONAL:
        return BID_ELIGIBLE_WITH_ACTION
    if overall == NOT_CURRENTLY_ELIGIBLE:
        return BID_INELIGIBLE
    return ELIGIBILITY_UNKNOWN


def package_docs_dir(oid: str) -> Path | None:
    if not (oid.startswith("opengov:") and oid.count(":") >= 2):
        return None
    parts = oid.split(":")
    code, pid = parts[1], parts[-1]
    d = data_path("opengov_public_docs", "documents", code, pid)
    return d if d.exists() else None


def list_package_documents(oid: str) -> list[Path]:
    d = package_docs_dir(oid)
    if not d:
        return []
    out: list[Path] = []
    for fp in d.rglob("*"):
        if fp.is_file() and fp.suffix.lower() in _SUPPORTED_EXT:
            out.append(fp)
    return sorted(out, key=lambda p: p.name.lower())


def _extract_docx(path: Path) -> str:
    try:
        import zipfile
        from xml.etree import ElementTree as ET

        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
        root = ET.fromstring(xml)
        texts = []
        for t in root.iter():
            if t.tag.endswith("}t") and t.text:
                texts.append(t.text)
        return "\n".join(texts)
    except Exception:
        return ""


def _extract_xlsx_text(path: Path, *, max_cells: int = 2000) -> str:
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts: list[str] = []
        n = 0
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    if cell is None:
                        continue
                    parts.append(str(cell))
                    n += 1
                    if n >= max_cells:
                        return "\n".join(parts)
        return "\n".join(parts)
    except Exception:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")[:80_000]
        except Exception:
            return ""


def extract_document_pages(path: Path, *, max_pages: int = 80) -> list[dict[str, Any]]:
    """Return [{page, text, document}] for PDF; single blob for other types."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            import fitz

            doc = fitz.open(str(path))
            pages = []
            try:
                limit = min(max_pages, int(doc.page_count or 0))
                for i in range(limit):
                    try:
                        txt = doc.load_page(i).get_text("text") or ""
                    except Exception:
                        txt = ""
                    pages.append({"document": path.name, "page": i + 1, "text": txt, "chars": len(txt)})
                # Scanned pages with near-zero text — flag
                empty = sum(1 for p in pages if p["chars"] < 40)
                if empty and empty == len(pages):
                    # fallback whole-doc extract
                    blob = extract_pdf_text(str(path), max_pages=max_pages)
                    return [{"document": path.name, "page": 1, "text": blob, "chars": len(blob), "scanned_warning": True}]
            finally:
                doc.close()
            return pages
        except Exception:
            blob = extract_pdf_text(str(path), max_pages=max_pages)
            return [{"document": path.name, "page": 1, "text": blob, "chars": len(blob)}]
    if ext in {".txt", ".html", ".htm", ".md", ".csv"}:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except Exception:
            text = ""
        return [{"document": path.name, "page": 1, "text": text, "chars": len(text)}]
    if ext == ".docx":
        text = _extract_docx(path)
        return [{"document": path.name, "page": 1, "text": text, "chars": len(text)}]
    if ext in {".xlsx", ".xls"}:
        text = _extract_xlsx_text(path)
        return [{"document": path.name, "page": 1, "text": text, "chars": len(text)}]
    if ext == ".doc":
        # binary .doc — best-effort latin1 scrape
        try:
            raw = path.read_bytes()
            text = re.sub(rb"[^\x09\x0a\x0d\x20-\x7e]", b" ", raw).decode("ascii", errors="ignore")
            text = re.sub(r"\s+", " ", text)[:100_000]
        except Exception:
            text = ""
        return [{"document": path.name, "page": 1, "text": text, "chars": len(text)}]
    return []


def _find_hits(text: str, patterns: list[tuple[re.Pattern[str], str]], *, doc: str, page: int) -> list[dict[str, Any]]:
    hits = []
    for rx, code in patterns:
        m = rx.search(text)
        if not m:
            continue
        start = max(0, m.start() - 80)
        end = min(len(text), m.end() + 80)
        hits.append(
            {
                "blocking_requirement": code,
                "exact_requirement": text[start:end].replace("\n", " ").strip(),
                "source_document": doc,
                "page": page,
                "confidence": "B",
            }
        )
    return hits


def _resolve_far_clauses(text: str, *, doc: str, page: int) -> list[dict[str, Any]]:
    out = []
    for rx, clause, meaning, status in _FAR_CLAUSES:
        m = rx.search(text)
        if not m:
            continue
        # Soft set-asides that company may hold → reclassify
        final_status = status
        if clause.startswith("FAR 52.219-6"):
            sa = set_aside_eligibility("total small business set-aside")
            final_status = "APPLIES" if sa.get("eligible") else "BID_BLOCKER"
        if status == "BID_BLOCKER" and any(x in clause for x in ("SDVOSB", "WOSB", "HUBZone", "EDWOSB", "8(a)")):
            final_status = "BID_BLOCKER"
        out.append(
            {
                "clause": clause,
                "plain_english": meaning,
                "why_it_matters": meaning,
                "status": final_status,
                "required_action": (
                    "STOP — company does not qualify"
                    if final_status == "BID_BLOCKER"
                    else "Review and satisfy before award"
                    if final_status == "ACTION_REQUIRED"
                    else "Confirm applicability"
                ),
                "source_document": doc,
                "page": page,
                "confidence": "B",
            }
        )
    return out


def mine_opportunity_eligibility(
    oid: str,
    *,
    pack: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    max_docs: int = 40,
) -> dict[str, Any]:
    """Full-file eligibility mine for one opportunity."""
    profile = profile or load_company_eligibility_profile()
    docs = list_package_documents(oid)[:max_docs]
    documents_total = len(list_package_documents(oid))
    documents_processed = 0
    documents_failed = 0
    documents_pending = max(0, documents_total - len(docs))

    page_blobs: list[dict[str, Any]] = []
    all_text_parts: list[str] = [oid]
    fatal: list[dict[str, Any]] = []
    actionable: list[dict[str, Any]] = []
    far_hits: list[dict[str, Any]] = []
    scanned_warnings = 0

    for fp in docs:
        try:
            pages = extract_document_pages(fp)
            if not pages or (len(pages) == 1 and pages[0].get("chars", 0) == 0):
                documents_failed += 1
                continue
            documents_processed += 1
            for p in pages:
                if p.get("scanned_warning"):
                    scanned_warnings += 1
                page_blobs.append(p)
                txt = p.get("text") or ""
                if not txt.strip():
                    continue
                all_text_parts.append(txt)
                fatal.extend(_find_hits(txt, _FATAL_PATTERNS, doc=p["document"], page=int(p["page"])))
                actionable.extend(_find_hits(txt, _ACTION_PATTERNS, doc=p["document"], page=int(p["page"])))
                far_hits.extend(_resolve_far_clauses(txt, doc=p["document"], page=int(p["page"])))
        except Exception:
            documents_failed += 1

    # Also include identity descriptions as weak secondary signal
    for ident in (pack or {}).get("identities") or []:
        if isinstance(ident, dict) and ident.get("raw_description"):
            all_text_parts.append(str(ident["raw_description"]))

    package_text = "\n".join(all_text_parts)
    gate = evaluate_eligibility_gate(text=package_text[:180_000], profile=profile)
    status = _map_gate(str(gate.get("overall_status") or GATE_UNKNOWN))

    # Deduplicate blockers by code
    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out = []
        for it in items:
            k = str(it.get("blocking_requirement") or it.get("clause") or "")
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out

    fatal = _dedupe(fatal)
    actionable = _dedupe(actionable)
    far_hits = _dedupe(far_hits)

    # FAR BID_BLOCKER → fatal
    for fh in far_hits:
        if fh.get("status") == "BID_BLOCKER":
            fatal.append(
                {
                    "blocking_requirement": fh["clause"],
                    "exact_requirement": fh.get("plain_english"),
                    "source_document": fh.get("source_document"),
                    "page": fh.get("page"),
                    "confidence": fh.get("confidence"),
                    "required_action": fh.get("required_action"),
                }
            )
        elif fh.get("status") == "ACTION_REQUIRED":
            actionable.append(
                {
                    "blocking_requirement": fh["clause"],
                    "exact_requirement": fh.get("plain_english"),
                    "source_document": fh.get("source_document"),
                    "page": fh.get("page"),
                    "confidence": fh.get("confidence"),
                    "required_action": fh.get("required_action"),
                }
            )

    fatal = _dedupe(fatal)
    actionable = _dedupe(actionable)

    if fatal:
        status = BID_INELIGIBLE
    elif actionable and status == BID_ELIGIBLE:
        status = BID_ELIGIBLE_WITH_ACTION
    elif documents_total == 0:
        status = ELIGIBILITY_UNKNOWN
    elif documents_processed == 0 and documents_total > 0:
        status = ELIGIBILITY_UNKNOWN
    elif scanned_warnings and not fatal and not actionable and status == BID_ELIGIBLE:
        # All scanned/empty — cannot confirm
        status = ELIGIBILITY_UNKNOWN

    primary = None
    if status == BID_INELIGIBLE:
        primary = fatal[0] if fatal else {
            "blocking_requirement": gate.get("next_action") or "GATE_INELIGIBLE",
            "required_action": "Do not pursue",
            "source_document": "eligibility_gate",
        }
    elif status == BID_ELIGIBLE_WITH_ACTION:
        primary = actionable[0] if actionable else {
            "blocking_requirement": "CONDITIONAL_GATE",
            "required_action": str((gate.get("plain") or {}).get("label") if isinstance(gate.get("plain"), dict) else gate.get("plain") or "Complete action"),
        }
    elif status == ELIGIBILITY_UNKNOWN:
        primary = {
            "blocking_requirement": "PACKAGE_TEXT_INCOMPLETE" if documents_total == 0 else "ELIGIBILITY_UNRESOLVED",
            "required_action": "Recover/parse package before deep research",
            "source_document": "package",
        }

    can_bid = {
        BID_ELIGIBLE: "YES",
        BID_ELIGIBLE_WITH_ACTION: "YES WITH ACTION",
        ELIGIBILITY_UNKNOWN: "UNKNOWN",
        BID_INELIGIBLE: "NO",
    }.get(status, "UNKNOWN")

    return {
        "opportunity_id": oid,
        "eligibility_status": status,
        "can_we_bid": can_bid,
        "why": (primary or {}).get("blocking_requirement"),
        "what_action": (primary or {}).get("required_action"),
        "blocking_requirement": (primary or {}).get("blocking_requirement"),
        "required_action": (primary or {}).get("required_action"),
        "source_document": (primary or {}).get("source_document"),
        "page": (primary or {}).get("page"),
        "confidence": (primary or {}).get("confidence") or "B",
        "proceed_to_research": status in {BID_ELIGIBLE, BID_ELIGIBLE_WITH_ACTION},
        "file_coverage": {
            "documents_total": documents_total,
            "documents_processed": documents_processed,
            "documents_failed": documents_failed,
            "documents_pending": documents_pending,
            "scanned_warnings": scanned_warnings,
            "pages_mined": len(page_blobs),
            "package_text_chars": len(package_text),
        },
        "fatal_blockers": fatal,
        "actionable_blockers": actionable,
        "far_dfars": far_hits,
        "gate_overall": gate.get("overall_status"),
        "evaluated_at": now_utc().isoformat(),
        "build": BUILD,
    }


def mine_all_opportunities(
    opp_ids: list[str],
    *,
    packs: dict[str, dict[str, Any]] | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    packs = packs or {}
    by: dict[str, Any] = {}
    counts = {BID_ELIGIBLE: 0, BID_ELIGIBLE_WITH_ACTION: 0, ELIGIBILITY_UNKNOWN: 0, BID_INELIGIBLE: 0}
    docs_total = docs_proc = docs_fail = 0
    fatal_c: dict[str, int] = {}
    action_c: dict[str, int] = {}
    far_detected = far_resolved = far_block = far_action = far_review = 0

    for i, oid in enumerate(opp_ids):
        ev = mine_opportunity_eligibility(oid, pack=packs.get(oid))
        by[oid] = ev
        counts[ev["eligibility_status"]] = counts.get(ev["eligibility_status"], 0) + 1
        fc = ev.get("file_coverage") or {}
        docs_total += int(fc.get("documents_total") or 0)
        docs_proc += int(fc.get("documents_processed") or 0)
        docs_fail += int(fc.get("documents_failed") or 0)
        for f in ev.get("fatal_blockers") or []:
            k = str(f.get("blocking_requirement") or "OTHER")
            fatal_c[k] = fatal_c.get(k, 0) + 1
        for a in ev.get("actionable_blockers") or []:
            k = str(a.get("blocking_requirement") or "OTHER")
            action_c[k] = action_c.get(k, 0) + 1
        for fh in ev.get("far_dfars") or []:
            far_detected += 1
            far_resolved += 1
            st = fh.get("status")
            if st == "BID_BLOCKER":
                far_block += 1
            elif st == "ACTION_REQUIRED":
                far_action += 1
            elif st == "NEEDS_REVIEW":
                far_review += 1
        if on_progress and (i + 1) % 25 == 0:
            on_progress(phase="ELIGIBILITY_MINE", pct=min(40, int(5 + 35 * (i + 1) / max(len(opp_ids), 1))), opp=i + 1)

    store = {
        "kind": "EligibilityFileMineStore",
        "build": BUILD,
        "by_opportunity": by,
        "summary": {
            "opportunities_checked": len(opp_ids),
            "documents_total": docs_total,
            "documents_processed": docs_proc,
            "documents_failed": docs_fail,
            **counts,
            "top_bid_blockers": dict(sorted(fatal_c.items(), key=lambda x: -x[1])[:15]),
            "top_action_required": dict(sorted(action_c.items(), key=lambda x: -x[1])[:15]),
            "far_dfars": {
                "clauses_detected": far_detected,
                "clauses_resolved": far_resolved,
                "BID_BLOCKER": far_block,
                "ACTION_REQUIRED": far_action,
                "NEEDS_REVIEW": far_review,
            },
        },
        "updated_at": now_utc().isoformat(),
    }
    path = data_path("m3_eligibility_file_mine_store.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")
    return store
