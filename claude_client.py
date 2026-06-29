"""Claude contract screening via Anthropic API."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

MODEL = "claude-sonnet-4-6"
MAX_PDF_BYTES = 8_000_000  # send native PDF to Claude when under this size
MAX_PDF_DOWNLOAD_BYTES = 40_000_000  # download and text-extract up to 40 MB
MAX_PDFS = 12  # read every solicitation attachment (PWS, drawings, WD, etc.)
MAX_TOKENS = 4096

DEFAULT_SCREENING_PROMPT = """You are a government contract screening specialist for a small business prime contractor using the subcontracting middleman model.

YOUR #1 JOB: Read the contract posting, every attached PDF/solicitation document you receive, AND the historical pricing intelligence from USAspending.gov. Extract the full scope of work and produce actionable bid guidance.

IMPORTANT — EXTERNAL SOLICITATION PORTALS:
Many DoD and federal postings do NOT attach PDFs directly on SAM.gov. Documents may live on PIEE, FedConnect, NECO, or other portals linked from the SAM.gov "Attachments/Links" section.
- If document_access in the user message shows external portal or external links, DO NOT say "no attachments" or "no PDFs included."
- Instead explain where documents live, that quotes/SOW are on that portal, and what the posting description already tells us about scope, states, dates, and size.
- Always use the full posting description text provided — it often contains the real scope even when PDFs are external.

SCREENING RULES (for pursue/skip):
- FAR 52.219-14 present and checked → pursue false, flag SKIP
- Security clearances or unescorted access to restricted areas required → pursue false, flag SKIP
- Not standard service work a local subcontractor could do with basic business licensing → pursue false
- Location must have a realistic market of subcontractors

SERVICE-TYPE AWARENESS — tailor sub_type_needed, red flags, and bid reasoning to the NAICS/service type:
- Janitorial / carpet (561720, 561790, 561740): licensed commercial cleaning/janitorial subs; watch for sq ft, frequency, floor wax/carpet/window scope, wage determinations, bonded crews, after-hours access.
- Facilities support / building maintenance (561210): read the PWS — may be integrated facilities, trades (HVAC, electrical, plumbing), or preventive maintenance NOT janitorial; do NOT default to cleaning unless the SOW is actually custodial work.
- Landscaping / grounds (561730): licensed landscaping or grounds maintenance crews; watch for seasonal vs year-round, mowing/snow/irrigation, equipment requirements, acreage, pesticide applicator licenses.
- Pest control (561710): licensed commercial exterminators; watch for integrated pest management scope, restricted pesticides, recurring service vs one-time treatment, interior/exterior coverage.
- Waste (562111, 562119): waste haulers; watch for hazardous waste, DOT licensing, roll-off vs route collection.
- Document shredding (561439): NAID-certified destruction vendors; recurring pickup routes, multi-location scope.
- Translation (541930): certified interpreters/translators; language pairs, clearance, on-site vs remote.
- Vehicle washing (811192): fleet washing vendors; fleet size, mobile vs fixed bay, frequency.
- HVAC/plumbing maintenance (238220): licensed HVAC/plumbing contractors; maintenance-only vs install, refrigerant EPA 608, emergency response.
- Remediation (562910): environmental remediation firms; hazmat, EPA certs, site complexity.
- Moving (484210): commercial movers; local vs interstate, crating, timing windows.
- Local freight (484110): local carriers; DOT authority, liftgate/dock, insurance.
- Couriers (492110): courier services; clearance, specialized delivery requirements.
- Photography (711320): commercial photographers; clearance, equipment, deliverables.
- Equipment rental (532490): equipment rental companies; specialized gear, operator certs, maintenance.
- Telephone answering (561422): answering services; after-hours, call volume, specialized knowledge.

PLAIN ENGLISH SUMMARY (plain_english_summary field — MOST IMPORTANT):
Write under 200 words. Sound like you're explaining it to a friend, not a lawyer. No jargon.

Cover these points in simple conversational language:
1. What they actually want done — one to three sentences max
2. Where the work is — city and state, plus the nearest decent-size city to find subcontractors
3. How big — square footage or unit count if available
4. How often — daily, weekly, monthly, or one-time
5. How long — base year plus any option years
6. What kind of subcontractor is needed — be specific (e.g. "licensed commercial HVAC maintenance contractor" or "licensed commercial janitorial company"). Base this on PWS/attachment scope, not NAICS alone.
7. Any gotchas — security requirements, specialized equipment, tight deadline, unusual requirements
8. END with one sentence summarizing pricing — e.g. "Similar contracts in this area have awarded between $X and $Y. I recommend bidding around $Z to be competitive." Use the historical pricing data provided.

PRICING INTELLIGENCE (pricing_intelligence field):
Use the USAspending.gov regional benchmark data in the user message for context only.
USAspending does NOT include square footage or cleaning frequency — do not invent $/sq ft per visit from public awards alone.
- Reference regional average/highest/lowest award amounts when summarizing market context
- Format dollar amounts as strings like "$125,000"
- incumbent: the most recent dated award's winner, or the recency-weighted most frequent winner
- competition_level: "low" (1-3 unique past winners), "medium" (4-9), or "high" (10+)
- pricing_confidence: match regional benchmark confidence when provided (high/medium/low)
- pricing_summary: 2-3 plain English sentences on regional award levels — not a specific bid recommendation unless internal pricing is provided

Return JSON only with these exact fields:
- plain_english_summary: string
- pricing_intelligence: object with:
  - recommended_bid_low: string (dollar amount)
  - recommended_bid_high: string (dollar amount)
  - average_historical_award: string (dollar amount)
  - highest_historical_award: string (dollar amount)
  - lowest_historical_award: string (dollar amount)
  - most_frequent_winner: string
  - incumbent: string or null
  - competition_level: "low", "medium", or "high"
  - pricing_confidence: "high", "medium", or "low"
  - pricing_summary: string (2-3 sentences)
- pursue: true or false
- score: 1-10 (how good a fit for the subcontracting middleman model)
- reason: one sentence
- contract_title: string
- agency: string
- location: string
- due_date: string
- naics_code: string
- estimated_value: string or null
- square_footage: string or null
- pws_extraction: object with fields extracted from PWS/solicitation attachments (use null for any field not found):
- square_footage: integer or null — exact sq ft from "gross square footage", "cleanable square footage", "area to be cleaned", "net square footage". Search ALL attachments including PWS, PRS, drawings, and floor plans — sq ft is often in drawings even when absent from the PWS narrative.
  - building_type: one of office, medical, warehouse, military, courthouse, other — classify from agency and building description
  - cleaning_frequency_per_week: number or null — convert phrases like "five days per week", "Monday through Friday", "daily", "three times per week" to a numeric days-per-week value
  - special_requirements: array of strings — e.g. floor waxing, carpet cleaning, window cleaning, exterior, restrooms only
  - wage_determination_number: string or null — format WD XXXX-XXXX
  - wage_determination_rate: number or null — hourly rate from wage determination if stated
- solicitation_meta: object with submission and contracting details from the solicitation/bid package PDFs (use null if not found):
  - contracting_officer_name: string or null — KO or contracting officer name from cover page, block 5, or instructions
  - contracting_officer_email: string or null — email for questions or proposal submission
  - submission_method: string or null — how to submit: email address, SAM.gov, PIEE, FedConnect, etc.
  - base_year_start: string or null — performance period or base year start date
  - base_year_end: string or null — base year end date
  - agency_address: string or null — mailing address for the agency or contracting office
  - solicitation_number: string or null — solicitation/RFP number if stated in the PDF (may differ from SAM notice ID)
- sub_type_needed: string — REQUIRED. Name the exact trade(s) a local subcontractor must perform to execute this contract (e.g. "licensed commercial HVAC maintenance contractor", "NAID-certified document shredding vendor"). Derive from PWS/solicitation PDFs and posting text — NOT from NAICS code alone. Never label janitorial/cleaning unless the scope is actually custodial cleaning. For 561210 Facilities Support, read the SOW first — it is often building/trades maintenance, not janitorial.
- red_flags: array of strings
- far_52_219_14: true or false
- security_clearance_required: true or false
- option_years: number or null
- attachments_reviewed: array of strings listing PDF filenames or URLs you read (empty array if none downloaded — note external portal instead in plain_english_summary)
- document_access: object echoing the document_access block from the user message (status, summary, external_portals, requires_external_portal)
- external_links: array of {url, label} objects from the posting when provided

Respond with JSON only. No markdown fences."""

TEXT_SCREENING_PROMPT = """You are a government contract screening specialist for a small business prime contractor using the subcontracting middleman model.

This is STEP 1 — TEXT-ONLY triage. You only have the SAM.gov posting description and metadata. No PDFs are attached.

Score how good a fit this is for a prime who finds local subs and bids as middleman across our NAICS portfolio:

TIER 1 (daily search — janitorial, facilities, grounds, pest, waste):
561720 Janitorial · 561210 Facilities Support · 561730 Landscaping · 561710 Pest Control · 562111 Solid Waste · 561790 Building Services · 561740 Carpet Cleaning · 562119 Other Waste

TIER 2 (Mon/Wed/Fri — specialized services):
561439 Document Shredding · 541930 Translation · 811192 Vehicle Washing · 238220 HVAC/Plumbing Maintenance · 562910 Remediation · 484210 Moving · 484110 Local Freight · 492110 Couriers

TIER 3 (weekly — expansion):
711320 Photography · 532490 Equipment Rental · 561422 Telephone Answering

Use the contract NAICS code and posting text to identify the service type. Tailor sub_type_needed, red_flags, and your reason to that specific category.

CATEGORY-SPECIFIC EVALUATION (apply the matching block):
- Janitorial / carpet (561720, 561790, 561740): sq ft, frequency, floor/carpet/window scope, wage determinations, after-hours access.
- Facilities support (561210): building maintenance, trades, or integrated facilities — do NOT assume janitorial; infer from posting text only.
- Landscaping (561730): seasonal vs year-round, mowing/snow/irrigation, acreage, pesticide applicator licenses.
- Pest control (561710): IPM scope, restricted pesticides, recurring vs one-time, interior/exterior coverage.
- Waste (562111, 562119): roll-off vs route collection, hazardous waste, DOT licensing.
- Document shredding (561439): NAID certification requirements, recurring pickup schedules, number of locations.
- Translation (541930): language pairs required, clearance requirements, certified interpreter requirements.
- Vehicle washing (811192): fleet size, frequency, mobile vs fixed location.
- HVAC/plumbing maintenance (238220): maintenance-only vs installation, refrigerant handling (EPA 608), emergency response scope.
- Remediation (562910): hazmat requirements, EPA certifications, site complexity, soil/contamination scope.
- Moving (484210): local vs interstate, specialized equipment, timing/window requirements.
- Local freight (484110): liftgate/dock requirements, DOT authority, local delivery radius.
- Couriers (492110): security clearance requirements, specialized delivery (medical, legal, classified).
- Photography (711320): clearance requirements, specialized equipment, deliverables and turnaround.
- Equipment rental (532490): specialized equipment types, maintenance requirements, operator certification.
- Telephone answering (561422): after-hours coverage, specialized knowledge, call volume.

UNIVERSAL SCREENING RULES:
- FAR 52.219-14 or performance-of-work clause requiring prime to self-perform → pursue false
- Security clearances or restricted access required → pursue false
- Not standard service work a local subcontractor could do → pursue false
- Location must plausibly have local subs for THIS service type

Return JSON only with these fields:
- score: integer 1-10 (fit for subcontracting middleman model for this service type)
- pursue: true or false (quick bid/no-bid)
- reason: one sentence explaining the score — name the service type
- contract_title: string
- agency: string
- location: string
- due_date: string or null
- naics_code: string or null
- estimated_value: string or null
- red_flags: array of strings (category-specific flags visible from posting text)
- far_52_219_14: true or false (best guess from posting text)
- security_clearance_required: true or false (best guess from posting text)
- sub_type_needed: string or null — best guess for the specific subcontractor trade from posting text (e.g. "commercial landscaping crew", NOT generic "janitorial" for facilities maintenance postings)

Do NOT invent square footage, wage determinations, or pricing. Keep reason under 30 words.
Respond with JSON only. No markdown fences."""

TEXT_SCREEN_MAX_TOKENS = 1024

SYSTEM_PROMPT = DEFAULT_SCREENING_PROMPT


def _api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ValueError("ANTHROPIC_API_KEY is missing from .env")
    return key


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.rfind("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Claude response was not a JSON object")
    return data


def _collect_urls(raw: dict[str, Any]) -> list[str]:
    urls: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key.lower() in {"url", "href", "link", "uilink", "attachmenturl"} and isinstance(value, str):
                    if value.startswith("http"):
                        urls.append(value)
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, str) and obj.startswith("http"):
            urls.append(obj)

    walk(raw)
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _claude_blocks_for_pdf(name: str, data: bytes) -> tuple[list[dict[str, Any]], str | None]:
    """Build Claude content blocks for one PDF; text-extract when the file exceeds MAX_PDF_BYTES."""
    from pdf_text import extract_pdf_text

    label = name or "document.pdf"
    if not data.startswith(b"%PDF"):
        return [], f"{label} (not a PDF)"
    if len(data) > MAX_PDF_DOWNLOAD_BYTES:
        mb = MAX_PDF_DOWNLOAD_BYTES // 1_000_000
        return [], f"{label} (PDF exceeds {mb} MB download limit)"
    if len(data) <= MAX_PDF_BYTES:
        return [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(data).decode("ascii"),
                },
            }
        ], None
    text = extract_pdf_text(data)
    if not text.strip():
        return [], f"{label} (PDF too large — {len(data)} bytes — and text extraction failed)"
    return [
        {
            "type": "text",
            "text": (
                f"--- Extracted text from {label} ({len(data)} byte PDF; sent as text) ---\n\n{text}"
            ),
        }
    ], None


def _pdf_blocks_from_bytes(
    pdfs: list[tuple[str, bytes]],
    *,
    max_pdfs: int | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    cap = max_pdfs if max_pdfs is not None else MAX_PDFS
    blocks: list[dict[str, Any]] = []
    labels: list[str] = []
    skipped: list[str] = []
    for name, data in pdfs:
        if len(labels) >= cap:
            skipped.append(f"{name} (max {cap} PDFs per screen)")
            continue
        new_blocks, skip_note = _claude_blocks_for_pdf(name, data)
        if skip_note:
            skipped.append(skip_note)
            continue
        blocks.extend(new_blocks)
        labels.append(name)
    return blocks, labels, skipped


def _piee_pdf_blocks(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    from piee_client import fetch_piee_pdfs

    skipped: list[str] = []
    try:
        pdfs, notice_url = fetch_piee_pdfs(raw)
    except ImportError:
        skipped.append("Playwright not installed — PIEE documents unavailable.")
        return [], [], skipped
    except Exception as exc:
        skipped.append(f"PIEE download failed: {exc}")
        return [], [], skipped

    if notice_url and not pdfs:
        skipped.append(f"No PIEE PDFs downloaded from {notice_url}")
    blocks, labels, pdf_skipped = _pdf_blocks_from_bytes(pdfs, max_pdfs=MAX_PDFS)
    skipped.extend(pdf_skipped)
    if pdfs and not blocks:
        skipped.append("PIEE returned files but none were readable PDFs.")
    return blocks, labels, skipped


def _attachment_blocks(urls: list[str], *, max_pdfs: int | None = None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    cap = max_pdfs if max_pdfs is not None else MAX_PDFS
    blocks: list[dict[str, Any]] = []
    reviewed: list[str] = []
    skipped: list[str] = []
    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        for url in urls:
            if len(reviewed) >= cap:
                skipped.append(f"{url} (max {cap} PDFs per screen)")
                continue
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                skipped.append(url)
                continue
            label = _attachment_label(url, resp)
            new_blocks, skip_note = _claude_blocks_for_pdf(label, resp.content)
            if skip_note:
                skipped.append(skip_note)
                continue
            reviewed.append(label)
            blocks.extend(new_blocks)
    return blocks, reviewed, skipped


def _attachment_label(url: str, resp: httpx.Response) -> str:
    """Best-effort filename for an attachment download."""
    cd = resp.headers.get("content-disposition") or ""
    match = re.search(r'filename="?([^";\n]+)"?', cd, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()[:120]
    tail = url.split("/")[-1].split("?")[0]
    return tail[:80] or url[:80]


def _format_pricing_block(pricing_intel: dict[str, Any] | None) -> str:
    if not pricing_intel:
        return "Regional pricing benchmarks: not available (NAICS or state could not be determined)."

    if pricing_intel.get("error"):
        return f"Regional pricing lookup note: {pricing_intel['error']}"

    lines = [
        "REGIONAL AWARD BENCHMARKS (USAspending.gov — same NAICS & state, last 3 years):",
        "NOTE: USAspending does NOT include square footage or cleaning frequency.",
        "Use these as regional context only — not $/sq ft per visit.",
        f"NAICS: {pricing_intel.get('naics_code', 'unknown')}",
        f"State: {pricing_intel.get('state_name') or pricing_intel.get('state_code', 'unknown')}",
        f"Contracts found: {pricing_intel.get('awards_count', 0)}",
        f"Confidence: {pricing_intel.get('confidence_label') or pricing_intel.get('confidence', 'unknown')}",
        f"Regional average award: {pricing_intel.get('average_annual_award')}",
        f"Highest award: {pricing_intel.get('highest_award')}",
        f"Lowest award: {pricing_intel.get('lowest_award')}",
        f"Most frequent winner: {pricing_intel.get('most_frequent_winner') or 'none identified'}",
        f"Likely incumbent (most recent): {pricing_intel.get('likely_incumbent') or 'unknown'}",
        f"Note: {pricing_intel.get('benchmark_note', '')}",
        "",
        "Recent awards (amounts only — no sq ft or frequency):",
        json.dumps(pricing_intel.get("awards") or [], indent=2, default=str),
    ]
    return "\n".join(lines)


def _format_document_access_block(raw: dict[str, Any]) -> str:
    access = raw.get("documentAccess") if isinstance(raw.get("documentAccess"), dict) else {}
    lines = [
        "DOCUMENT ACCESS (from SAM.gov):",
        f"Status: {access.get('status', 'unknown')}",
        f"Summary: {access.get('summary', 'unknown')}",
        f"PDF attachments on SAM.gov: {access.get('pdf_attachment_count', 0)}",
        f"External links on SAM.gov: {access.get('external_link_count', 0)}",
        f"External portals detected: {', '.join(access.get('external_portals') or []) or 'none'}",
        f"Requires external portal review: {access.get('requires_external_portal', False)}",
        f"Solicitation number: {access.get('solicitation_number') or 'unknown'}",
    ]
    links = raw.get("opportunityLinks") or []
    attachments = raw.get("opportunityAttachments") or []
    if attachments:
        lines.append("Attachments/links posted on SAM.gov:")
        for item in attachments[:12]:
            label = item.get("description") or "Attachment"
            if item.get("type") == "file":
                lines.append(f"- FILE: {label}")
            elif item.get("url"):
                lines.append(f"- LINK: {label} -> {item['url']}")
            else:
                lines.append(f"- {label}")
    elif links:
        lines.append("External / linked resources from SAM.gov:")
        for item in links[:10]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('label') or 'Link'}: {item.get('url')}")
            else:
                lines.append(f"- {item}")
    elif access.get("requires_external_portal"):
        lines.append(
            "No direct link URLs returned by API — user must open the SAM.gov posting "
            "and use Attachments/Links (often PIEE Solicitation Module)."
        )
        if access.get("sam_gov_link"):
            lines.append(f"SAM.gov UI link: {access['sam_gov_link']}")
    return "\n".join(lines)


def build_screening_text(
    contract: Any,
    attachment_count: int,
    pricing_intel: dict[str, Any] | None = None,
    *,
    attachment_urls: list[str] | None = None,
    pdfs_skipped: list[str] | None = None,
) -> str:
    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    description = (
        raw.get("descriptionText")
        or contract.description
        or raw.get("description")
        or raw.get("additionalInfoLink")
        or "Not provided in posting"
    )
    if isinstance(description, str) and description.startswith("http"):
        description = raw.get("descriptionText") or "Description URL provided but text not loaded."
    urls = _collect_urls(raw)
    work_states = raw.get("workStates") or []
    lines = [
        "Analyze this federal contract. Read all attached PDF documents included in this message.",
        "If documents are on an external portal (PIEE, etc.), use the full posting description and document access notes below.",
        "Use the historical pricing data below to fill pricing_intelligence and include a pricing sentence in plain_english_summary.",
        "",
        f"Notice ID: {contract.notice_id}",
        f"Title: {contract.title}",
        f"Agency: {contract.agency or 'Unknown'}",
        f"Location: {contract.location or 'Unknown'}",
        f"Work states detected: {', '.join(work_states) if work_states else 'Unknown'}",
        f"NAICS: {contract.naics_code or 'Unknown'}",
        f"Set-aside: {contract.set_aside or 'Unknown'}",
        f"Due date: {contract.due_date.isoformat() if contract.due_date else 'Unknown'}",
        f"SAM.gov link: {contract.link or 'Unknown'}",
        f"PDF attachments included in this message: {attachment_count}",
        f"Attachment URLs attempted: {len(attachment_urls or [])}",
        "",
        _format_document_access_block(raw),
    ]
    if pdfs_skipped:
        lines.extend([
            "",
            "PDF download notes (not included as documents — explain gaps in plain_english_summary if relevant):",
            *[f"- {note}" for note in pdfs_skipped[:8]],
        ])
    lines.extend([
        "",
        "Full posting description:",
        str(description)[:15000],
        "",
        _format_pricing_block(pricing_intel),
    ])
    if urls:
        lines.extend(["", "All linked URLs from posting:"])
        lines.extend(f"- {url}" for url in urls[:15])
    if raw:
        lines.extend(["", "Full SAM.gov record (JSON):", json.dumps(raw, default=str)[:10000]])
    return "\n".join(lines)


def _collect_attachment_urls(raw: dict[str, Any]) -> list[str]:
    """Collect every URL we might download as a PDF for Claude (files first, then all links)."""
    file_urls: list[str] = []
    link_urls: list[str] = []

    for item in raw.get("opportunityAttachments") or []:
        if not isinstance(item, dict):
            continue
        if item.get("download_url"):
            file_urls.append(str(item["download_url"]))
            continue
        url = item.get("url")
        if isinstance(url, str) and url.startswith("http"):
            link_urls.append(url)

    for url in raw.get("attachmentDownloadUrls") or []:
        if isinstance(url, str) and url.startswith("http"):
            file_urls.append(url)

    extra: list[str] = []
    for item in raw.get("resourceLinks") or []:
        if isinstance(item, str) and item.startswith("http"):
            extra.append(item)
    for item in raw.get("opportunityLinks") or []:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url.startswith("http"):
                extra.append(url)

    seen: set[str] = set()
    ordered: list[str] = []
    for url in file_urls + link_urls + extra:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _looks_like_pdf_link(url: str) -> bool:
    cleaned = url.lower().split("?")[0]
    return cleaned.endswith(".pdf")


SOLICITATION_META_PROMPT = """You extract contracting, submission, and PWS scope details from federal solicitation PDFs.

Read the attached solicitation, PWS, wage determination, and instruction documents. Return JSON only:
{
  "contracting_officer_name": string or null,
  "contracting_officer_email": string or null,
  "submission_method": string or null,
  "base_year_start": string or null,
  "base_year_end": string or null,
  "agency_address": string or null,
  "solicitation_number": string or null,
  "pws_extraction": {
    "square_footage": integer or null,
    "building_type": "office" | "medical" | "warehouse" | "military" | "courthouse" | "other" | null,
    "cleaning_frequency_per_week": number or null,
    "special_requirements": array of strings or null,
    "wage_determination_number": string or null,
    "wage_determination_rate": number or null
  }
}

Rules:
- submission_method should state HOW to submit (email address, SAM.gov, PIEE, FedConnect, etc.)
- square_footage: exact sq ft — search every attached document (PWS, PRS, drawings, floor plans). Never null if any document states area or square footage.
- cleaning_frequency_per_week: convert "daily", "Monday through Friday", "five days per week", etc. to a number
- Use exact names, emails, and numbers from the document — do not invent values
- Use null for anything not clearly stated
No markdown fences."""


def _contract_pdf_blocks(
    contract: Any,
    *,
    max_pdfs: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load PDF blocks for Claude (PIEE + SAM attachments)."""
    from sam_enrich import ensure_enriched_sam_raw, needs_attachment_refresh

    cap = max_pdfs if max_pdfs is not None else MAX_PDFS
    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    ensure_enriched_sam_raw(
        contract,
        force=needs_attachment_refresh(raw) or not raw.get("descriptionText"),
    )
    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    urls = _collect_attachment_urls(raw)
    piee_blocks, piee_labels, _ = _piee_pdf_blocks(raw)
    sam_blocks, sam_labels, _ = _attachment_blocks(urls, max_pdfs=cap)

    pdf_blocks: list[dict[str, Any]] = []
    fetched_labels: list[str] = []
    for block, label in zip(piee_blocks + sam_blocks, piee_labels + sam_labels):
        if len(pdf_blocks) >= cap:
            break
        pdf_blocks.append(block)
        fetched_labels.append(label)
    return pdf_blocks, fetched_labels


def contract_attachment_text(contract: Any, *, max_pdfs: int | None = None) -> str:
    """Plain text from every contract PDF attachment."""
    pdf_blocks, _ = _contract_pdf_blocks(contract, max_pdfs=max_pdfs or MAX_PDFS)
    parts: list[str] = []
    for block in pdf_blocks:
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n\n".join(parts)


_DRAWING_NAME_HINTS: tuple[str, ...] = (
    "drawing",
    "drawings",
    "floor plan",
    "floor_plan",
    "floorplan",
    "blueprint",
    "site plan",
    "site_plan",
    "layout",
    "as-built",
    "as built",
    "architect",
    "floorplan",
)


def _pdf_page_count(data: bytes) -> int:
    try:
        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
        return doc.page_count
    except Exception:
        return 0


def _pdf_text_length(data: bytes) -> int:
    from pdf_text import extract_pdf_text

    return len(extract_pdf_text(data, max_chars=2000).strip())


def is_drawing_pdf(name: str, data: bytes) -> bool:
    """True for floor plans / image-heavy drawing attachments."""
    low = (name or "").lower()
    if any(hint in low for hint in _DRAWING_NAME_HINTS):
        return True
    # Image-only PDFs (no text layer) are usually floor plans or scans.
    if data.startswith(b"%PDF") and _pdf_page_count(data) >= 1 and _pdf_text_length(data) < 120:
        return True
    return False


def iter_contract_pdf_bytes(contract: Any) -> list[tuple[str, bytes]]:
    """Download every PIEE + SAM PDF for a contract."""
    from piee_client import fetch_piee_pdfs

    raw = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    merged: list[tuple[str, bytes]] = []
    seen: set[str] = set()

    try:
        piee_pdfs, _ = fetch_piee_pdfs(raw)
        for name, data in piee_pdfs:
            if not data.startswith(b"%PDF"):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append((name, data))
    except Exception:
        pass

    for url in _collect_attachment_urls(raw):
        try:
            with httpx.Client(timeout=180.0, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError:
            continue
        if not resp.content.startswith(b"%PDF"):
            continue
        label = _attachment_label(url, resp)
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append((label, resp.content))
    return merged


def _pdf_pages_as_image_blocks(
    data: bytes,
    *,
    max_pages: int = 8,
    zoom: float = 3.5,
) -> list[dict[str, Any]]:
    """Render PDF pages to PNG for Claude vision (floor plans, scanned drawings)."""
    try:
        import fitz
    except ImportError:
        return []

    blocks: list[dict[str, Any]] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        matrix = fitz.Matrix(zoom, zoom)
        for i in range(min(doc.page_count, max_pages)):
            pix = doc[i].get_pixmap(matrix=matrix, alpha=False)
            png = pix.tobytes("png")
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(png).decode("ascii"),
                    },
                }
            )
    except Exception:
        return []
    return blocks


DRAWING_SQFT_PROMPT = """You are a federal contract scope analyst. Find building square footage from architectural drawings and floor plans.

You will receive floor plan / drawing images. Read dimensions, room areas, scale bars, title block notes, and labeled totals.

For janitorial/custodial contracts:
- Sum only CLEANABLE area (exclude rooms hatched or labeled "NO CUSTODIAL SERVICES REQUIRED").
- If multiple buildings on one contract, sum cleanable area across all in-scope buildings.

Return a JSON object (you may include brief reasoning before the JSON):
{
  "square_footage": integer or null,
  "square_footage_basis": "gross" | "net" | "cleanable" | "rentable" | "total building" | null,
  "source_document": string,
  "calculation_notes": string,
  "estimated": boolean
}

Rules:
- Prefer an explicit labeled total (e.g. "GROSS SF", "TOTAL AREA") if shown.
- If no total, sum labeled room square footages when clearly stated.
- If only dimensions or a scale bar are shown, compute areas and sum in-scope rooms.
- If no numeric labels exist, estimate cleanable sq ft from plan proportions and room layout — set estimated=true.
- Use null only if the plans are unreadable or contain zero spatial information.
- calculation_notes: one sentence on how the number was derived."""


def extract_sqft_from_drawings(contract: Any) -> dict[str, Any]:
    """
    Vision pass on floor-plan / drawing PDFs when square footage is not in text documents.
    Renders pages as images so Claude can read graphic-only plans.
    """
    pdfs = iter_contract_pdf_bytes(contract)
    drawing_pdfs = [(name, data) for name, data in pdfs if is_drawing_pdf(name, data)]
    if not drawing_pdfs:
        return {}

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Contract: {contract.title}\n"
                f"Location: {contract.location}\n"
                "Task: total CLEANABLE square footage for janitorial/custodial bidding.\n"
                "Exclude hatched or labeled NO CUSTODIAL SERVICES REQUIRED areas.\n"
                f"Drawing files: {', '.join(n for n, _ in drawing_pdfs)}"
            ),
        }
    ]

    for name, data in drawing_pdfs:
        content.append({"type": "text", "text": f"\n--- Drawing file: {name} ({len(data)} bytes) ---"})
        page_images = _pdf_pages_as_image_blocks(data)
        if page_images:
            content.extend(page_images)
        elif len(data) <= MAX_PDF_BYTES:
            blocks, _ = _claude_blocks_for_pdf(name, data)
            content.extend(blocks)

    if len(content) <= 1:
        return {}

    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=DRAWING_SQFT_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = response.content[0].text if response.content else "{}"
    data = _extract_json(text)
    if not isinstance(data, dict):
        return {}

    sqft = data.get("square_footage")
    try:
        sqft_int = int(str(sqft).replace(",", "")) if sqft is not None else None
    except (TypeError, ValueError):
        sqft_int = None
    if not sqft_int or sqft_int < 100:
        return {}

    notes = str(data.get("calculation_notes") or "").lower()
    estimated = bool(data.get("estimated"))
    if not estimated:
        estimated = any(word in notes for word in ("estimat", "approximat", "proportional", "no explicit"))

    return {
        "square_footage": sqft_int,
        "square_footage_basis": data.get("square_footage_basis"),
        "source_document": data.get("source_document") or drawing_pdfs[0][0],
        "calculation_notes": data.get("calculation_notes"),
        "estimated": estimated,
        "drawing_files": [n for n, _ in drawing_pdfs],
    }


def apply_drawing_sqft_to_analysis(contract: Any, analysis: dict[str, Any], drawing: dict[str, Any]) -> None:
    """Merge drawing vision extraction into analysis + contract row."""
    from pws_fields import apply_pws_extraction

    if not drawing.get("square_footage"):
        return
    pws = analysis.setdefault("pws_extraction", {})
    if not isinstance(pws, dict):
        pws = {}
        analysis["pws_extraction"] = pws
    pws["square_footage"] = drawing["square_footage"]
    if drawing.get("square_footage_basis"):
        pws["square_footage_basis"] = drawing["square_footage_basis"]
    analysis["drawing_sqft_extraction"] = drawing
    apply_pws_extraction(contract, analysis)


def try_extract_sqft_from_drawings(contract: Any, analysis: dict[str, Any]) -> bool:
    """Run drawing vision pass if square footage still missing. Returns True if found."""
    if getattr(contract, "square_footage", None):
        return False
    pws = analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
    if pws.get("square_footage"):
        return False

    drawing = extract_sqft_from_drawings(contract)
    if not drawing.get("square_footage"):
        return False
    apply_drawing_sqft_to_analysis(contract, analysis, drawing)
    return True


def extract_solicitation_meta(contract: Any) -> dict[str, Any]:
    """Extract CO and submission fields from solicitation PDFs when not already in analysis."""
    sam = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    pdf_blocks, labels = _contract_pdf_blocks(contract)
    lines = [
        f"Contract: {contract.title}",
        f"Agency: {contract.agency}",
        f"SAM notice ID: {contract.notice_id}",
        f"Due date: {contract.due_date}",
        "",
        "Extract contracting officer, submission details, and PWS scope (square footage, cleaning frequency, wage determination) from the solicitation documents.",
        "SAM posting excerpt:",
        (contract.description or sam.get("descriptionText") or "")[:4000],
    ]
    if labels:
        lines.append(f"\nPDFs attached: {', '.join(labels[:8])}")

    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}, *pdf_blocks]
    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SOLICITATION_META_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = response.content[0].text if response.content else "{}"
    data = _extract_json(text)
    if not isinstance(data, dict):
        return {}
    pws = data.pop("pws_extraction", None)
    result = {k: v for k, v in data.items() if v is not None and str(v).strip()}
    if isinstance(pws, dict):
        cleaned_pws = {k: v for k, v in pws.items() if v is not None and v != ""}
        if cleaned_pws:
            result["pws_extraction"] = cleaned_pws
    return result


def build_text_screening_text(contract: Any) -> str:
    """Metadata + posting description only — no PDF references."""
    from naics_labels import naics_display

    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    description = (
        raw.get("descriptionText")
        or contract.description
        or raw.get("description")
        or "Not provided in posting"
    )
    if isinstance(description, str) and description.startswith("http"):
        description = raw.get("descriptionText") or "Description URL provided but text not loaded."
    work_states = raw.get("workStates") or []
    lines = [
        "Text-only triage — no PDFs attached. Score this posting for subcontracting middleman fit.",
        "",
        f"Notice ID: {contract.notice_id}",
        f"Title: {contract.title}",
        f"Agency: {contract.agency or 'Unknown'}",
        f"Location: {contract.location or 'Unknown'}",
        f"Work states: {', '.join(work_states) if work_states else 'Unknown'}",
        f"NAICS: {naics_display(contract.naics_code) if contract.naics_code else 'Unknown'}",
        f"Set-aside: {contract.set_aside or 'Unknown'}",
        f"Due date: {contract.due_date.isoformat() if contract.due_date else 'Unknown'}",
        f"SAM.gov link: {contract.link or 'Unknown'}",
        "",
        "Posting description:",
        str(description)[:12000],
    ]
    return "\n".join(lines)


def screen_contract_text(contract: Any) -> dict[str, Any]:
    """Step 1: fast text-only Claude screening — no PDF download."""
    from datetime import datetime, timezone

    text = build_text_screening_text(contract)
    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=TEXT_SCREEN_MAX_TOKENS,
        system=TEXT_SCREENING_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    response_text = "".join(block.text for block in response.content if hasattr(block, "text"))
    analysis = _extract_json(response_text)
    score = analysis.get("score")
    try:
        score_int = max(1, min(10, int(score)))
    except (TypeError, ValueError):
        score_int = 5
    analysis["score"] = score_int
    analysis["text_score"] = score_int
    analysis["screening_stage"] = "text"
    analysis["text_screened_at"] = datetime.now(timezone.utc).isoformat()
    analysis["pdfs_sent_to_claude"] = 0
    if analysis.get("reason"):
        analysis["text_reason"] = analysis["reason"]
    return analysis


def screen_contract(contract: Any, system_prompt: str | None = None) -> dict[str, Any]:
    """Step 2: full screening with PDFs, pricing intel, and complete analysis fields."""
    if system_prompt is None:
        from settings_store import resolve_screening_prompt

        system_prompt = resolve_screening_prompt()

    from pricing import get_contract_pricing_intel
    from sam_enrich import ensure_enriched_sam_raw, needs_attachment_refresh

    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    ensure_enriched_sam_raw(
        contract,
        force=needs_attachment_refresh(raw) or not raw.get("descriptionText"),
    )
    if contract.sam_raw and isinstance(contract.sam_raw, dict):
        if contract.sam_raw.get("descriptionText") and not contract.description:
            contract.description = contract.sam_raw["descriptionText"][:8000]
        from sam_client import normalize_opportunity

        refreshed = normalize_opportunity(contract.sam_raw)
        if refreshed.get("location"):
            contract.location = refreshed["location"]

    pricing_intel = get_contract_pricing_intel(contract, force_refresh=False)

    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    urls = _collect_attachment_urls(raw)
    piee_blocks, piee_labels, piee_skipped = _piee_pdf_blocks(raw)
    sam_blocks, sam_labels, sam_skipped = _attachment_blocks(urls)

    pdf_blocks: list[dict[str, Any]] = []
    fetched_labels: list[str] = []
    for block, label in zip(piee_blocks + sam_blocks, piee_labels + sam_labels):
        if len(pdf_blocks) >= MAX_PDFS:
            break
        pdf_blocks.append(block)
        fetched_labels.append(label)

    skipped_labels = piee_skipped + sam_skipped
    if len(piee_blocks) + len(sam_blocks) > len(pdf_blocks):
        skipped_labels.append(
            f"Sent {len(pdf_blocks)} of {len(piee_blocks) + len(sam_blocks)} PDFs to Claude (SOW/solicitation prioritized)."
        )

    if piee_labels:
        raw = dict(raw)
        raw["pieeDownloaded"] = piee_labels
        contract.sam_raw = raw

    text = build_screening_text(
        contract,
        len(pdf_blocks),
        pricing_intel,
        attachment_urls=urls,
        pdfs_skipped=skipped_labels,
    )

    content: list[dict[str, Any]] = [
        {"type": "text", "text": text},
        *pdf_blocks,
    ]

    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )

    response_text = "".join(block.text for block in response.content if hasattr(block, "text"))
    analysis = _extract_json(response_text)

    if fetched_labels and not analysis.get("attachments_reviewed"):
        analysis["attachments_reviewed"] = fetched_labels
    analysis["pdfs_sent_to_claude"] = len(pdf_blocks)
    analysis["pdf_urls_attempted"] = len(urls)
    analysis["piee_pdfs_sent"] = len(piee_labels)
    if skipped_labels:
        analysis["pdfs_not_included"] = skipped_labels[:12]

    document_access = raw.get("documentAccess") if isinstance(raw.get("documentAccess"), dict) else None
    if document_access:
        analysis["document_access"] = document_access
    opportunity_links = raw.get("opportunityLinks") or []
    if opportunity_links:
        analysis["external_links"] = opportunity_links
    attachments = raw.get("opportunityAttachments") or []
    if attachments:
        analysis["sam_attachments"] = attachments

    if analysis.get("pursue") is True:
        analysis["pursue"] = True
    elif analysis.get("pursue") is False:
        analysis["pursue"] = False

    if pricing_intel and not pricing_intel.get("error"):
        analysis["usaspending_source"] = {
            "naics_code": pricing_intel.get("naics_code"),
            "state_code": pricing_intel.get("state_code"),
            "awards_count": pricing_intel.get("awards_count"),
        }

    analysis["screening_stage"] = "full"
    if analysis.get("text_score") is None and analysis.get("score") is not None:
        analysis["text_score"] = analysis.get("score")

    return analysis


SUB_ANALYSIS_PROMPT = """You are helping a prime contractor evaluate potential subcontractors found via Google Places.

TRADE FIT IS THE TOP PRIORITY:
- Read sub_type_needed and contract scope — recommend subs who can actually perform that work.
- Score 1-3 if the business type clearly does NOT match (e.g. janitorial/cleaning company for HVAC-only maintenance, or a carpet cleaner for document shredding).
- Score 7-10 only when the candidate plausibly performs the required trade for this contract.
- If business name/category suggests the wrong trade, say so in the reason even if ratings are high.

Secondary signals (only after trade fit):
- Rating above 4.0 stars — positive
- More than 20 reviews — positive
- Website present — positive
- Review count under 5 — negative (may be too small)
- Rating under 3.5 — negative
- Distance under 10 miles — positive

Return JSON only:
{
  "subs": [
    {"place_id": "string", "score": 7, "reason": "One sentence mentioning trade fit."}
  ]
}
Include every place_id from the input list. No markdown."""


def analyze_subcontractors(
    contract: Any,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Claude scores each Google Places subcontractor candidate."""
    if not candidates:
        return []

    from usaspending_client import extract_work_location

    work = extract_work_location(
        contract.location,
        contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else None,
    )
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    pws = analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
    special = pws.get("special_requirements")
    if isinstance(special, list):
        scope_bits = ", ".join(str(s) for s in special[:8])
    else:
        scope_bits = ""
    summary_lines = [
        f"Contract: {contract.title}",
        f"NAICS: {contract.naics_code}",
        f"Location: {work.get('label') or contract.location}",
        f"Sub type needed: {analysis.get('sub_type_needed') or 'unknown'}",
        f"Scope summary: {analysis.get('plain_english_summary') or analysis.get('executive_summary') or 'n/a'}",
    ]
    if scope_bits:
        summary_lines.append(f"PWS special requirements: {scope_bits}")
    summary_lines.extend(["", "Candidates:", json.dumps(candidates, indent=2, default=str)])
    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SUB_ANALYSIS_PROMPT,
        messages=[{"role": "user", "content": "\n".join(summary_lines)}],
    )
    text = response.content[0].text if response.content else "{}"
    data = _extract_json(text)
    rows = data.get("subs") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    by_place: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        place_id = str(row.get("place_id") or "").strip()
        if not place_id:
            continue
        score = row.get("score")
        try:
            score_int = max(1, min(10, int(score)))
        except (TypeError, ValueError):
            score_int = 5
        reason = str(row.get("reason") or "").strip() or "No reason provided."
        by_place[place_id] = {"place_id": place_id, "score": score_int, "reason": reason}
    return [by_place.get(c["place_id"], {"place_id": c["place_id"], "score": 5, "reason": "Not analyzed."}) for c in candidates if c.get("place_id")]


def _proposal_user_message(contract: Any, config: dict[str, Any]) -> str:
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    sam = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    lines = [
        "Generate the complete six-section proposal HTML for this contract.",
        "",
        f"Contract title: {contract.title}",
        f"Agency: {contract.agency}",
        f"Location: {contract.location}",
        f"NAICS: {contract.naics_code}",
        f"Square footage: {contract.square_footage}",
        f"Plain English summary: {analysis.get('plain_english_summary') or analysis.get('executive_summary') or ''}",
        "",
        "CONFIGURATION (use these exact values):",
        json.dumps(config, indent=2, default=str),
        "",
        "SAM posting excerpt:",
        (contract.description or sam.get("descriptionText") or "")[:6000],
    ]
    return "\n".join(lines)


def generate_proposal_content(contract: Any, config: dict[str, Any]) -> tuple[str, dict[str, str]]:
    from proposal_prompt import PROPOSAL_SYSTEM_PROMPT
    from proposal_service import parse_sections_from_html

    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=PROPOSAL_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _proposal_user_message(contract, config)}],
    )
    text = response.content[0].text if response.content else ""
    html = text.strip()
    if html.startswith("```"):
        html = re.sub(r"^```(?:html)?\s*", "", html)
        html = re.sub(r"\s*```$", "", html)
    sections = parse_sections_from_html(html)
    return html, sections


def generate_subcontract_agreement(contract: Any, config: dict[str, Any]) -> str:
    from agreement_prompt import SUBCONTRACT_AGREEMENT_SYSTEM_PROMPT, SUBCONTRACT_AGREEMENT_TEMPLATE

    user_message = "\n".join(
        [
            "Fill in the subcontract agreement template using ONLY the JSON data below.",
            "Return complete HTML for the filled agreement.",
            "",
            "TEMPLATE:",
            SUBCONTRACT_AGREEMENT_TEMPLATE,
            "",
            "DATA (JSON):",
            json.dumps(config, indent=2, default=str),
            "",
            "Contract context:",
            f"Title: {contract.title}",
            f"Agency: {contract.agency}",
            f"Location: {contract.location}",
        ]
    )
    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SUBCONTRACT_AGREEMENT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.content[0].text if response.content else ""
    html = text.strip()
    if html.startswith("```"):
        html = re.sub(r"^```(?:html)?\s*", "", html)
        html = re.sub(r"\s*```$", "", html)
    return html


def regenerate_proposal_section(contract: Any, config: dict[str, Any], section_key: str) -> str:
    from proposal_defaults import SECTION_TITLES
    from proposal_prompt import PROPOSAL_SYSTEM_PROMPT, SECTION_REGEN_PROMPT

    title = SECTION_TITLES.get(section_key, section_key)
    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=f"{PROPOSAL_SYSTEM_PROMPT}\n\n{SECTION_REGEN_PROMPT}",
        messages=[
            {
                "role": "user",
                "content": f"Regenerate SECTION: {title} ({section_key})\n\n{_proposal_user_message(contract, config)}",
            }
        ],
    )
    text = response.content[0].text if response.content else ""
    return text.strip()


def humanize_proposal_text(fragment: str) -> str:
    from proposal_prompt import HUMANIZE_PROMPT

    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=HUMANIZE_PROMPT,
        messages=[{"role": "user", "content": fragment}],
    )
    return (response.content[0].text if response.content else fragment).strip()


def reduce_proposal_ai_score(html: str) -> str:
    from proposal_prompt import REDUCE_AI_PROMPT

    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=REDUCE_AI_PROMPT,
        messages=[{"role": "user", "content": html}],
    )
    text = response.content[0].text if response.content else html
    return text.strip()

