"""AI contract screening via OpenAI Responses API.

Public APIs match the legacy Claude client so callers stay stable.
PDFs are sent as OpenAI input_file parts (base64 data URLs).
TODO: durable file_id cache keyed by (contract_id, filename_key, sha256)
so identical solicitation PDFs are not re-encoded for every AI task.
"""

from __future__ import annotations
from application_clock import now_utc, today_local

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from ai_funnel import default_stage_for_task
from ai_model_router import FunnelStage, clamp_max_output_tokens
from openai_runtime import (
    create_response,
    extract_json_object,
    image_part,
    openai_model,
    pdf_file_part,
    text_part,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

MODEL = openai_model  # callable — prefer openai_model() at call time; kept for legacy refs
MAX_PDF_BYTES = 8_000_000  # send native PDF when under this size
MAX_PDF_DOWNLOAD_BYTES = 40_000_000  # download and text-extract up to 40 MB
MAX_PDFS = 12  # read every solicitation attachment (PWS, drawings, WD, etc.)
MAX_TOKENS = 4096
TEXT_SCREEN_MAX_TOKENS = 400  # Stage 1 compact structured output ceiling

DEFAULT_SCREENING_PROMPT = """You are a government contract screening specialist for a small business prime contractor using the subcontracting middleman model.

DATA INTEGRITY — NON-NEGOTIABLE:
Never fabricate missing facts or numeric values.
If the supplied evidence does not establish a value, return null/UNKNOWN.
Do not infer a factual price, quantity, contract value, bid count, cost,
deadline, certification, license, bond, financing term, historical value,
supplier term, or other factual field.
Clearly distinguish assessments from sourced facts.
Assessments (e.g. "installation likely") must remain labeled as assessments —
never as verified facts.
Unknown is acceptable. A fabricated value is never acceptable.

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
- The user message includes a NEARBY SUBCONTRACTORS section from a Google Places search already run for this site. Use it when scoring: no subs within 50 miles should lower the score; several strong local subs is a positive signal.

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

CONTRACT ADVICE (contract_advice field — honest bid coaching):
Give practical pursue/avoid guidance using the pricing data, location, and scope you read.
- reasons_to_pursue: array of 2-4 short bullet strings — real upsides (margin potential using prior/regional annual dollars, manageable scope, incumbent displacement opportunity, low competition, good fit score)
- reasons_to_avoid: array of 2-4 short bullet strings — real risks (remote site / subs hard to find, tight deadline, wage determination pressure, security or compliance burden, low score, red flags)
When prior contract or regional annual pricing is available, cite dollar amounts using the OWNER POLICY margin provided in the user message (never invent a margin %). If no policy margin is provided, discuss pricing without assuming a margin percentage.
Be direct and specific to this contract — not generic filler.

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
- contract_advice: object with:
  - reasons_to_pursue: array of strings (2-4 bullets)
  - reasons_to_avoid: array of strings (2-4 bullets)
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
  - incumbent_contractor: string or null — company name listed as "Incumbent contractor", "Current contractor", or "Existing contractor" in background/history sections
  - previous_contract_number: string or null — prior contract number / PIID / "Previous contract number" from background section (e.g. FA8821-19-F-0123)
  - questions_deadline: string or null — deadline for questions to the CO if stated
- submission_package: object — proposal submission requirements detected in attachments (use false/null when not found):
  - pricing_schedule_required: boolean — true if any attachment filename or content indicates a required pricing schedule, CLIN table, cost schedule, or schedule of supplies/services with unit prices
  - pricing_schedule_filename: string or null — filename of the pricing document if identified
  - multiple_pricing_encouraged: boolean — true if solicitation encourages alternative proposals or multiple pricing options
  - sf1449_required: boolean — true if SF-1449 or Standard Form 1449 is used
  - submission_method: one of "Email", "PIEE", "SAM.gov", "Mail", "Unknown" — normalized submission channel
  - submission_email: string or null — email address for proposal submission if method is Email
  - evaluation_criteria: one of "LPTA", "Technical", "Unknown" — LPTA vs best-value/technical evaluation
  - questions_deadline: string or null — ISO date or readable date for questions deadline
- co_questions_suggested: array of strings — compliance questions the offeror should ask the CO based on ambiguities or missing info in the documents (max 6)
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

SYSTEM_PROMPT = DEFAULT_SCREENING_PROMPT


def _extract_json(text: str) -> dict[str, Any]:
    return extract_json_object(text)


def _notice_id(contract: Any) -> str | None:
    return getattr(contract, "notice_id", None)


def _ai_text(
    *,
    task: str,
    system: str | None,
    content: list[dict[str, Any]],
    max_tokens: int,
    contract: Any = None,
    web_search: bool = False,
    funnel_stage: int | None = None,
    automatic: bool = False,
    use_cache: bool = True,
) -> str:
    """Route through funnel-aware OpenAI entry point. Never Stage 0."""
    from data_integrity import with_ai_guardrail

    stage = funnel_stage if funnel_stage is not None else default_stage_for_task(task)
    if stage == FunnelStage.STAGE_0:
        raise ValueError("Stage 0 must not call OpenAI")
    # Stage 1–2: force web_search off regardless of caller mistakes
    if stage < FunnelStage.STAGE_3:
        web_search = False
    capped = clamp_max_output_tokens(stage, max_tokens)
    return create_response(
        task=task,
        instructions=with_ai_guardrail(system),
        content=content,
        max_output_tokens=capped,
        web_search=web_search if stage >= FunnelStage.STAGE_3 else False,
        notice_id=_notice_id(contract) if contract is not None else None,
        funnel_stage=stage,
        automatic=automatic,
        use_cache=use_cache,
    )


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


def _pdf_content_parts(name: str, data: bytes) -> tuple[list[dict[str, Any]], str | None]:
    """Build OpenAI content parts for one PDF; text-extract when the file exceeds MAX_PDF_BYTES."""
    from pdf_text import extract_pdf_text

    label = name or "document.pdf"
    if not data.startswith(b"%PDF"):
        return [], f"{label} (not a PDF)"
    if len(data) > MAX_PDF_DOWNLOAD_BYTES:
        mb = MAX_PDF_DOWNLOAD_BYTES // 1_000_000
        return [], f"{label} (PDF exceeds {mb} MB download limit)"
    if len(data) <= MAX_PDF_BYTES:
        return [pdf_file_part(label, data)], None
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
        new_blocks, skip_note = _pdf_content_parts(name, data)
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
            new_blocks, skip_note = _pdf_content_parts(label, resp.content)
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


def _format_subs_block(subs_context: dict[str, Any] | None) -> str:
    ctx = subs_context if isinstance(subs_context, dict) else {}
    count = int(ctx.get("count") or 0)
    radius = ctx.get("radius_miles")
    nearest = ctx.get("nearest_miles")
    within = int(ctx.get("within_radius") or 0)
    if count <= 0:
        return (
            "NEARBY SUBCONTRACTORS (Google Places pre-search):\n"
            f"No qualifying businesses found within {radius or 'the configured'} miles. "
            "Treat staffing as difficult when scoring."
        )
    lines = [
        "NEARBY SUBCONTRACTORS (Google Places pre-search — already run before this screening):",
        f"Found {count} candidate(s); {within} within {radius} miles.",
    ]
    if nearest is not None:
        lines.append(f"Nearest sub: {nearest} miles away.")
    lines.append("Candidates:")
    for row in ctx.get("subs") or []:
        name = row.get("business_name") or "Unknown"
        dist = row.get("distance_miles")
        rating = row.get("rating")
        reviews = row.get("review_count")
        score = row.get("claude_score")
        reason = row.get("claude_reason")
        dist_part = f"{dist} mi" if dist is not None else "distance unknown"
        rating_part = f"{rating}★ ({reviews or 0} reviews)" if rating is not None else "no rating"
        fit_part = f"fit score {score}/10" if score is not None else "fit not scored"
        line = f"- {name}: {dist_part}, {rating_part}, {fit_part}"
        if reason:
            line += f" — {reason}"
        lines.append(line)
    return "\n".join(lines)


def build_screening_text(
    contract: Any,
    attachment_count: int,
    pricing_intel: dict[str, Any] | None = None,
    *,
    attachment_urls: list[str] | None = None,
    pdfs_skipped: list[str] | None = None,
    subs_context: dict[str, Any] | None = None,
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
        "",
        _format_subs_block(subs_context),
    ])
    if urls:
        lines.extend(["", "All linked URLs from posting:"])
        lines.extend(f"- {url}" for url in urls[:15])
    if raw:
        lines.extend(["", "Full SAM.gov record (JSON):", json.dumps(raw, default=str)[:10000]])
    return "\n".join(lines)


def _collect_attachment_urls(raw: dict[str, Any]) -> list[str]:
    """Collect every URL we might download as a PDF for AI (files first, then all links)."""
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
  "incumbent_contractor": string or null,
  "previous_contract_number": string or null,
  "questions_deadline": string or null,
  "submission_package": {
    "pricing_schedule_required": boolean,
    "pricing_schedule_filename": string or null,
    "multiple_pricing_encouraged": boolean,
    "sf1449_required": boolean,
    "submission_method": "Email" | "PIEE" | "SAM.gov" | "Mail" | "Unknown",
    "submission_email": string or null,
    "evaluation_criteria": "LPTA" | "Technical" | "Unknown",
    "questions_deadline": string or null
  },
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
- solicitation_number should be the current RFP/solicitation number from the PDF
- incumbent_contractor: MANDATORY when stated — search background, history, SF-1449 block 20, cover page, and amendment text for "Incumbent contractor", "Current contractor", "Awarded to", or similar. Extract the company name exactly. Never null if any document names the incumbent.
- previous_contract_number: MANDATORY when stated — search for "Previous contract number", "Prior contract", "Predecessor contract", "Contract number", PIID, or award number in background/history. Extract the federal contract number exactly (e.g. FA8821-19-F-0123, W9128F-24-C-0001). Never null if any document lists a predecessor award number.
- square_footage: exact sq ft — search every attached document (PWS, PRS, drawings, floor plans). Never null if any document states area or square footage.
- cleaning_frequency_per_week: convert "daily", "Monday through Friday", "five days per week", etc. to a number
- Use exact names, emails, and numbers from the document — do not invent values
- Use null for anything not clearly stated
No markdown fences."""


def _stored_db_pdf_blocks(
    contract: Any,
    session: Any,
    *,
    max_pdfs: int | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Build AI PDF blocks from bytes already stored in PostgreSQL — no SAM.gov calls."""
    from attachment_storage import stored_pdf_items

    cap = max_pdfs if max_pdfs is not None else MAX_PDFS
    if not getattr(contract, "id", None):
        return [], [], ["No contract id — cannot load stored PDFs."]
    items = stored_pdf_items(session, contract.id)
    if not items:
        return [], [], ["No PDF bytes stored in the database for this contract."]
    return _pdf_blocks_from_bytes(items, max_pdfs=cap)


def _contract_pdf_blocks(
    contract: Any,
    *,
    max_pdfs: int | None = None,
    db_only: bool = False,
    session: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load PDF blocks for AI — prefer PostgreSQL bytes when stored."""
    cap = max_pdfs if max_pdfs is not None else MAX_PDFS

    load_session = session
    own_session = False
    if load_session is None and getattr(contract, "id", None):
        from database import SessionLocal

        load_session = SessionLocal()
        own_session = True

    try:
        if load_session is not None and getattr(contract, "id", None):
            from attachment_storage import stored_pdf_items

            if stored_pdf_items(load_session, contract.id):
                blocks, labels, _ = _stored_db_pdf_blocks(contract, load_session, max_pdfs=cap)
                return blocks, labels
    finally:
        if own_session and load_session is not None:
            load_session.close()

    if db_only:
        from database import SessionLocal

        fallback = SessionLocal()
        try:
            blocks, labels, _ = _stored_db_pdf_blocks(contract, fallback, max_pdfs=cap)
            return blocks, labels
        finally:
            fallback.close()

    from sam_enrich import ensure_enriched_sam_raw, needs_attachment_refresh

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


def contract_attachment_text(
    contract: Any,
    *,
    max_pdfs: int | None = None,
    session: Any = None,
    db_only: bool = True,
) -> str:
    """Plain text from every contract PDF attachment (stored in DB when available)."""
    stored = getattr(contract, "attachment_text", None)
    if stored and str(stored).strip():
        return str(stored).strip()

    from attachment_pipeline import extract_contract_attachment_text
    from database import SessionLocal

    own_session = session is None
    if own_session:
        session = SessionLocal()
    try:
        result = extract_contract_attachment_text(
            contract,
            session,
            max_pdfs=max_pdfs or MAX_PDFS,
            db_only=db_only,
        )
        return result.text
    finally:
        if own_session and session is not None:
            session.close()


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


def iter_contract_pdf_bytes(contract: Any, *, session=None, db_only: bool = False) -> list[tuple[str, bytes]]:
    """PDF bytes from PostgreSQL when stored; otherwise download and persist."""
    from database import SessionLocal

    own_session = None
    if session is None and getattr(contract, "id", None):
        session = SessionLocal()
        own_session = session
    try:
        if session is not None and getattr(contract, "id", None):
            from attachment_storage import get_contract_pdf_bytes

            return get_contract_pdf_bytes(session, contract, db_only=db_only)
    finally:
        if own_session:
            own_session.close()

    if db_only:
        return []

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
    """Render PDF pages to PNG for AI vision (floor plans, scanned drawings)."""
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


def extract_sqft_from_drawings(contract: Any, *, db_only: bool = False, session: Any = None) -> dict[str, Any]:
    """
    Vision pass on floor-plan / drawing PDFs when square footage is not in text documents.
    Renders pages as images so AI can read graphic-only plans.
    """
    pdfs = iter_contract_pdf_bytes(contract, session=session, db_only=db_only)
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
            blocks, _ = _pdf_content_parts(name, data)
            content.extend(blocks)

    if len(content) <= 1:
        return {}

    text = _ai_text(
        task='extract_sqft_from_drawings',
        system=DRAWING_SQFT_PROMPT,
        content=content,
        max_tokens=2048,
        contract=contract,
        web_search=False,
    )
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
    """Merge drawing vision extraction into analysis + contract row.

    Estimated square footage stays in analysis only — never promoted to ORM as fact.
    """
    from pws_fields import apply_pws_extraction

    if not drawing.get("square_footage"):
        return
    pws = analysis.setdefault("pws_extraction", {})
    if not isinstance(pws, dict):
        pws = {}
        analysis["pws_extraction"] = pws
    analysis["drawing_sqft_extraction"] = drawing
    if drawing.get("estimated"):
        analysis["square_footage_estimated"] = True
        analysis["square_footage_assessment"] = drawing["square_footage"]
        if drawing.get("square_footage_basis"):
            analysis["square_footage_basis_assessment"] = drawing["square_footage_basis"]
        # Do not write estimated sqft onto pws/ORM factual fields
        return
    pws["square_footage"] = drawing["square_footage"]
    if drawing.get("square_footage_basis"):
        pws["square_footage_basis"] = drawing["square_footage_basis"]
    apply_pws_extraction(contract, analysis)


def try_extract_sqft_from_drawings(
    contract: Any,
    analysis: dict[str, Any],
    *,
    db_only: bool = False,
    session: Any = None,
) -> bool:
    """Run drawing vision pass if square footage still missing. Returns True if found."""
    if getattr(contract, "square_footage", None):
        return False
    pws = analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
    if pws.get("square_footage"):
        return False

    drawing = extract_sqft_from_drawings(contract, db_only=db_only, session=session)
    if not drawing.get("square_footage"):
        return False
    apply_drawing_sqft_to_analysis(contract, analysis, drawing)
    return True


SUB_TYPE_SCOPE_PROMPT = """You read federal solicitation PDFs to determine what local subcontractor trade is required to perform this contract.

Return JSON only:
{
  "sub_type_needed": "string — exact trade, e.g. licensed commercial janitorial contractor, commercial landscaping crew, licensed HVAC maintenance contractor",
  "scope_one_liner": "string — one plain-English sentence describing the work"
}

Rules:
- Derive from PWS / solicitation scope — NOT from NAICS code alone
- NAICS 561210 Facilities Support is often building/trades maintenance — read the SOW; do NOT default to janitorial unless custodial cleaning is the actual scope
- Never label janitorial/cleaning unless the documents describe custodial cleaning work
- Be specific enough to search Google Places for the right trade
No markdown fences."""


def extract_sub_type_for_sub_search(
    contract: Any,
    *,
    db_only: bool = False,
    session: Any = None,
) -> dict[str, Any]:
    """Read solicitation PDFs to determine subcontractor trade before Places search."""
    sam = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    pdf_blocks, labels = _contract_pdf_blocks(contract, max_pdfs=8, db_only=db_only, session=session)
    lines = [
        f"Contract: {contract.title}",
        f"Agency: {contract.agency}",
        f"Location: {contract.location}",
        f"NAICS: {contract.naics_code}",
        f"SAM notice ID: {contract.notice_id}",
        "",
        "Determine the exact subcontractor trade needed to perform this contract.",
        "SAM posting excerpt:",
        (contract.description or sam.get("descriptionText") or "")[:4000],
    ]
    if labels:
        lines.append(f"\nPDFs attached: {', '.join(labels[:8])}")
    elif not pdf_blocks:
        lines.append("\nNo PDFs attached — use posting text only.")

    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}, *pdf_blocks]
    text = _ai_text(
        task='extract_sub_type_for_sub_search',
        system=SUB_TYPE_SCOPE_PROMPT,
        content=content,
        max_tokens=1024,
        contract=contract,
        web_search=False,
    )
    data = _extract_json(text)
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if k in ("sub_type_needed", "scope_one_liner") and v is not None and str(v).strip()
    }


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
        "Extract contracting officer, submission details, PWS scope, incumbent contractor name, and previous contract number from the solicitation documents.",
        "If the incumbent or prior contract number appears anywhere in the PDFs, you MUST return those fields — never omit them.",
        "SAM posting excerpt:",
        (contract.description or sam.get("descriptionText") or "")[:4000],
    ]
    if labels:
        lines.append(f"\nPDFs attached: {', '.join(labels[:8])}")

    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}, *pdf_blocks]
    text = _ai_text(
        task='extract_solicitation_meta',
        system=SOLICITATION_META_PROMPT,
        content=content,
        max_tokens=2048,
        contract=contract,
        web_search=False,
    )
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
    """Stage 1: compact text-only Luna triage — never sends PDFs."""
    from ai_funnel import stage0_evaluate
    from ai_stage1 import run_stage1_triage

    stage0 = stage0_evaluate(contract)
    return run_stage1_triage(contract, stage0=stage0, automatic=False)


def extract_stage2_evidence(
    contract: Any,
    *,
    stage1: dict[str, Any] | None = None,
    automatic: bool = False,
) -> dict[str, Any]:
    """
    Stage 2: evidence/requirements extraction (Luna, no web).

    Resolves CURRENT Stage 1 via fingerprint cache — never uses stale Stage 1
    or ORM analysis as authorization. automatic must stay False unless configured.
    """
    from ai_funnel import stage0_evaluate
    from ai_stage1 import resolve_current_stage1_result
    from ai_stage2 import run_stage2_evidence

    if automatic:
        raise ValueError("Stage 2 automatic execution is disabled — call with automatic=False explicitly")

    stage0 = stage0_evaluate(contract)
    resolution = resolve_current_stage1_result(contract, stage0=stage0)
    # Explicit stage1 arg is ignored for production authorization unless current HIT
    # (tests use run_stage2_evidence with stage1_resolution / fixture marker).
    _ = stage1
    return run_stage2_evidence(
        contract,
        stage0=stage0,
        stage1_resolution=resolution,
        automatic=False,
    )


def screen_contract(
    contract: Any,
    system_prompt: str | None = None,
    *,
    subs_context: dict[str, Any] | None = None,
    db_only: bool = False,
    session: Any = None,
) -> dict[str, Any]:
    """Step 2: full screening with PDFs, pricing intel, and complete analysis fields."""
    if system_prompt is None:
        from settings_store import resolve_screening_prompt

        system_prompt = resolve_screening_prompt()

    from pricing import get_contract_pricing_intel

    if not db_only:
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

    if db_only:
        from database import SessionLocal

        load_session = SessionLocal()
        try:
            pdf_blocks, fetched_labels, skipped_labels = _stored_db_pdf_blocks(
                contract,
                load_session,
                max_pdfs=MAX_PDFS,
            )
        finally:
            load_session.close()
        urls: list[str] = []
        piee_labels: list[str] = []
    else:
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
                f"Sent {len(pdf_blocks)} of {len(piee_blocks) + len(sam_blocks)} PDFs to AI (SOW/solicitation prioritized)."
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
        subs_context=subs_context,
    )

    content: list[dict[str, Any]] = [
        {"type": "text", "text": text},
        *pdf_blocks,
    ]

    text = _ai_text(
        task='screen_contract',
        system=system_prompt,
        content=content,
        max_tokens=MAX_TOKENS,
        contract=contract,
        web_search=False,
    )

    response_text = text
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

    analysis = merge_contract_advice_from_screen(analysis)
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
    *,
    sub_type_hint: str | None = None,
    scope_hint: str | None = None,
) -> list[dict[str, Any]]:
    """AI scores each Google Places subcontractor candidate."""
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
        f"Sub type needed: {sub_type_hint or analysis.get('sub_type_needed') or 'unknown'}",
        f"Scope summary: {scope_hint or analysis.get('plain_english_summary') or analysis.get('executive_summary') or 'n/a'}",
    ]
    if scope_bits:
        summary_lines.append(f"PWS special requirements: {scope_bits}")
    summary_lines.extend(["", "Candidates:", json.dumps(candidates, indent=2, default=str)])
    text = _ai_text(
        task='analyze_subcontractors',
        system=SUB_ANALYSIS_PROMPT,
        content=[{"type": "text", "text": "\n".join(summary_lines)}],
        max_tokens=2048,
        contract=contract,
        web_search=False,
    )
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


def _proposal_user_message(
    contract: Any,
    config: dict[str, Any],
    *,
    pdf_labels: list[str] | None = None,
) -> str:
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    sam = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}
    pws = analysis.get("pws_extraction") if isinstance(analysis.get("pws_extraction"), dict) else {}
    req = analysis.get("proposal_requirements") if isinstance(analysis.get("proposal_requirements"), dict) else {}
    drawing = analysis.get("drawing_sqft_extraction") if isinstance(analysis.get("drawing_sqft_extraction"), dict) else {}

    lines = [
        "Generate the complete seven-section proposal HTML for this contract.",
        "Use the attached solicitation PDFs, extracted requirements below, and configuration — do not invent requirements or evaluation factors.",
        "",
        f"Contract title: {contract.title}",
        f"Agency: {contract.agency}",
        f"Location: {contract.location}",
        f"NAICS: {contract.naics_code}",
        f"Square footage: {contract.square_footage}",
        f"Cleaning frequency (per week): {contract.cleaning_frequency_per_week}",
        f"Building type: {contract.building_type}",
        f"Special requirements: {contract.special_requirements}",
        f"Plain English summary: {analysis.get('plain_english_summary') or analysis.get('executive_summary') or ''}",
        "",
        "CONFIGURATION (use these exact values):",
        json.dumps(config, indent=2, default=str),
        "",
        "SOLICITATION METADATA (from attachments):",
        json.dumps(sol, indent=2, default=str),
        "",
        "PWS SCOPE EXTRACTION (from attachments):",
        json.dumps(pws, indent=2, default=str),
    ]
    if req:
        lines.extend(
            [
                "",
                "PROPOSAL REQUIREMENTS — SECTION L, SECTION M, AND PWS (extracted from attachments; use for compliance matrix):",
                json.dumps(req, indent=2, default=str),
            ]
        )
    if drawing:
        lines.extend(
            [
                "",
                "DRAWING / FLOOR PLAN NOTES:",
                json.dumps(drawing, indent=2, default=str),
            ]
        )
    if pdf_labels:
        lines.extend(["", f"PDF attachments included in this message ({len(pdf_labels)}):", ", ".join(pdf_labels)])
    lines.extend(
        [
            "",
            "SAM posting excerpt:",
            (contract.description or sam.get("descriptionText") or "")[:6000],
        ]
    )

    attachment_text = contract_attachment_text(contract, max_pdfs=MAX_PDFS)
    if attachment_text.strip():
        lines.extend(
            [
                "",
                "EXTRACTED ATTACHMENT TEXT (PWS / solicitation / Section L / Section M excerpt):",
                attachment_text[:100_000],
            ]
        )
        if len(attachment_text) > 100_000:
            lines.append(
                f"[Text truncated at 100k chars — {len(attachment_text)} total; read attached PDFs for full content.]"
            )
    return "\n".join(lines)


def _proposal_message_content(
    contract: Any,
    config: dict[str, Any],
    *,
    preamble: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build AI message content with solicitation PDFs attached."""
    from sam_enrich import ensure_enriched_sam_raw, needs_attachment_refresh

    raw = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    ensure_enriched_sam_raw(
        contract,
        force=needs_attachment_refresh(raw) or not raw.get("descriptionText"),
    )

    pdf_blocks, labels = _contract_pdf_blocks(contract, max_pdfs=MAX_PDFS)
    text = preamble + _proposal_user_message(contract, config, pdf_labels=labels)
    content: list[dict[str, Any]] = [{"type": "text", "text": text}, *pdf_blocks]
    return content, labels


def extract_proposal_requirements(contract: Any) -> dict[str, Any]:
    """Extract Section L/M evaluation factors and PWS requirements from solicitation PDFs."""
    from proposal_prompt import PROPOSAL_REQUIREMENTS_PROMPT

    pdf_blocks, labels = _contract_pdf_blocks(contract, max_pdfs=MAX_PDFS)
    lines = [
        f"Contract: {contract.title}",
        f"Agency: {contract.agency}",
        f"Location: {contract.location}",
        "",
        "Extract Section L instructions, Section M evaluation factors, and all material PWS requirements from the attached solicitation documents.",
    ]
    if labels:
        lines.append(f"PDFs attached: {', '.join(labels)}")

    attachment_text = contract_attachment_text(contract, max_pdfs=MAX_PDFS)
    if attachment_text.strip():
        lines.extend(["", "Attachment text excerpt:", attachment_text[:60_000]])

    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}, *pdf_blocks]
    text = _ai_text(
        task='extract_proposal_requirements',
        system=PROPOSAL_REQUIREMENTS_PROMPT,
        content=content,
        max_tokens=4096,
        contract=contract,
        web_search=False,
    )
    data = _extract_json(text)
    if not isinstance(data, dict):
        return {}

    factors = []
    section_m = data.get("section_m")
    if isinstance(section_m, dict):
        raw_factors = section_m.get("evaluation_factors")
        if isinstance(raw_factors, list):
            factors = [f for f in raw_factors if isinstance(f, dict) and f.get("factor_name")]

    pws_reqs = data.get("pws_requirements")
    if not isinstance(pws_reqs, list):
        pws_reqs = []

    section_l = data.get("section_l") if isinstance(data.get("section_l"), dict) else {}
    facility = data.get("facility_characteristics") if isinstance(data.get("facility_characteristics"), dict) else {}

    if not factors and not pws_reqs and not any(section_l.values()):
        return {}

    return {
        "section_l": section_l,
        "section_m_evaluation_factors": factors,
        "pws_requirements": [r for r in pws_reqs if isinstance(r, dict) and r.get("requirement")],
        "facility_characteristics": facility,
        "source_pdfs": labels,
    }


def generate_proposal_content(contract: Any, config: dict[str, Any]) -> tuple[str, dict[str, str]]:
    from proposal_prompt import PROPOSAL_SYSTEM_PROMPT
    from proposal_service import parse_sections_from_html

    content, _labels = _proposal_message_content(contract, config)
    text = _ai_text(
        task='generate_proposal_content',
        system=PROPOSAL_SYSTEM_PROMPT,
        content=content,
        max_tokens=16000,
        contract=contract,
        web_search=False,
    )
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
    text = _ai_text(
        task='generate_subcontract_agreement',
        system=SUBCONTRACT_AGREEMENT_SYSTEM_PROMPT,
        content=[{"type": "text", "text": user_message}],
        max_tokens=16000,
        contract=contract,
        web_search=False,
    )
    html = text.strip()
    if html.startswith("```"):
        html = re.sub(r"^```(?:html)?\s*", "", html)
        html = re.sub(r"\s*```$", "", html)
    return html


def regenerate_proposal_section(contract: Any, config: dict[str, Any], section_key: str) -> str:
    from proposal_defaults import SECTION_TITLES
    from proposal_prompt import PROPOSAL_SYSTEM_PROMPT, SECTION_REGEN_PROMPT

    title = SECTION_TITLES.get(section_key, section_key)
    preamble = f"Regenerate SECTION: {title} ({section_key})\n\n"
    content, _labels = _proposal_message_content(contract, config, preamble=preamble)
    text = _ai_text(
        task='regenerate_proposal_section',
        system=f"{PROPOSAL_SYSTEM_PROMPT}\n\n{SECTION_REGEN_PROMPT}",
        content=content,
        max_tokens=4096,
        contract=contract,
        web_search=False,
    )
    return text.strip()


def humanize_proposal_text(fragment: str) -> str:
    from proposal_prompt import HUMANIZE_PROMPT

    text = _ai_text(
        task='humanize_proposal_text',
        system=HUMANIZE_PROMPT,
        content=[{"type": "text", "text": fragment}],
        max_tokens=2048,
        web_search=False,
    )
    return text.strip()


def reduce_proposal_ai_score(html: str) -> str:
    from proposal_prompt import REDUCE_AI_PROMPT

    text = _ai_text(
        task='reduce_proposal_ai_score',
        system=REDUCE_AI_PROMPT,
        content=[{"type": "text", "text": html}],
        max_tokens=16000,
        web_search=False,
    )
    return text.strip()


CONTRACT_ADVICE_PROMPT = """You are a government contracting advisor for a small business prime using the subcontracting middleman model (find local subs, bid as prime, keep margin).

You will receive a contract summary, pricing intelligence, subcontractor market signals, and scoring data. Give honest, practical bid coaching — not a sales pitch.

Return JSON only:
{
  "reasons_to_pursue": ["2-4 specific bullet strings"],
  "reasons_to_avoid": ["2-4 specific bullet strings"]
}

Rules:
- Use real dollar amounts when annual contract values are provided (prior contract, regional average). Use the OWNER POLICY margin from the user message when illustrating profit — never invent a margin percentage. If margin is unknown, discuss dollars without fabricating one.
- Mention subcontractor difficulty when location is remote, proximity score was penalized, or sub search found few candidates.
- Reference red flags, security clearance, FAR 52.219-14, tight deadlines, or wage determinations when relevant.
- Be direct and specific to this contract. No markdown fences."""


def _normalize_advice_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item)).strip(" -•")
        if len(text) >= 8 and text not in items:
            items.append(text[:280])
    return items[:4]


def normalize_contract_advice(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    pursue = _normalize_advice_list(raw.get("reasons_to_pursue"))
    avoid = _normalize_advice_list(raw.get("reasons_to_avoid"))
    if not pursue and not avoid:
        return None
    return {
        "reasons_to_pursue": pursue,
        "reasons_to_avoid": avoid,
    }


def build_contract_advice_context(contract: Any, session) -> str:
    from datetime import date

    from display_format import prior_hints_from_contract, pricing_card_display
    from proximity_scoring import proximity_context
    from proposal_defaults import resolve_contract_margin

    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    intel = contract.pricing_intel if isinstance(getattr(contract, "pricing_intel", None), dict) else {}
    pred = intel.get("predecessor_award") if isinstance(intel.get("predecessor_award"), dict) else {}
    prox = proximity_context(session, contract)
    margin = resolve_contract_margin(contract)

    lines = [
        f"Title: {contract.title or 'Unknown'}",
        f"Agency: {contract.agency or 'Unknown'}",
        f"Location: {contract.location or 'Unknown'}",
        f"NAICS: {contract.naics_code or '—'}",
        f"Due date: {contract.due_date.isoformat() if contract.due_date else '—'}",
        f"Score: {analysis.get('score')}/10 (distance-adjusted effective: {prox.get('effective_score')})",
        f"Pursue flag: {analysis.get('pursue')}",
        f"Target prime margin: {margin}%",
    ]
    if prox.get("proximity_note"):
        lines.append(f"Proximity: {prox['proximity_note']}")
    if prox.get("nearest_sub_miles") is not None:
        lines.append(f"Nearest sub in network: {prox['nearest_sub_miles']} miles")

    summary = analysis.get("plain_english_summary") or analysis.get("executive_summary")
    if summary:
        lines.extend(["", "PLAIN ENGLISH SUMMARY:", str(summary).strip()])

    sub_type = analysis.get("sub_type_needed")
    if sub_type:
        lines.append(f"Sub type needed: {sub_type}")

    red_flags = analysis.get("red_flags") or []
    if red_flags:
        lines.append("Red flags: " + "; ".join(str(f) for f in red_flags[:6]))

    pricing_line = pricing_card_display(
        intel,
        has_work_state=True,
        prior_hints=prior_hints_from_contract(contract),
    ).get("line")
    if pricing_line:
        lines.extend(["", f"Pricing card: {pricing_line}"])

    if pred.get("is_prior_contract"):
        lines.extend([
            "",
            "PRIOR CONTRACT (USAspending):",
            f"  Contract #: {pred.get('contract_number') or '—'}",
            f"  Incumbent: {pred.get('recipient_name') or '—'}",
            f"  Recent annual: {pred.get('recent_annual_amount') or pred.get('annual_amount') or '—'}",
            f"  Base year: {pred.get('base_year_amount') or '—'}",
            f"  Total obligated: {pred.get('total_obligated') or pred.get('total_value') or '—'}",
            f"  Note: {pred.get('pricing_calc_note') or '—'}",
        ])
    elif intel.get("average_annual_award"):
        lines.extend([
            "",
            "REGIONAL BENCHMARK (USAspending):",
            f"  Average annual: {intel.get('average_annual_award')}",
            f"  Range: {intel.get('lowest_award')} – {intel.get('highest_award')}",
            f"  Awards in sample: {intel.get('awards_count')}",
            f"  Likely incumbent: {intel.get('likely_incumbent') or intel.get('most_frequent_winner') or '—'}",
        ])

    try:
        from sub_finder import nearby_network_subs

        network = nearby_network_subs(session, contract.notice_id)
        lines.append(f"Nearby subs in network (Google Places radius): {network.get('count', 0)}")
    except Exception:
        pass

    if contract.due_date:
        days_left = (contract.due_date - today_local()).days
        lines.append(f"Days until due: {days_left}")

    return "\n".join(lines)


def generate_contract_advice(contract: Any, session) -> dict[str, Any]:
    """Generate pursue/avoid coaching from stored contract data — no SAM.gov, no PDFs."""
    from api_budget import ScreenBudgetExceeded, can_screen, record_screen_usage

    if not can_screen():
        raise ScreenBudgetExceeded()

    context = build_contract_advice_context(contract, session)
    text = _ai_text(
        task='generate_contract_advice',
        system=CONTRACT_ADVICE_PROMPT,
        content=[{"type": "text", "text": context}],
        max_tokens=1024,
        contract=contract,
        web_search=False,
    )
    if not record_screen_usage():
        raise ScreenBudgetExceeded()

    response_text = text
    parsed = _extract_json(response_text)
    advice = normalize_contract_advice(parsed)
    if not advice:
        raise ValueError("AI returned empty contract advice.")

    from datetime import datetime, timezone

    advice["generated_at"] = now_utc().isoformat()
    advice["source"] = "ai_advice"
    return advice


def merge_contract_advice_from_screen(analysis: dict[str, Any]) -> dict[str, Any]:
    """Normalize contract_advice from full screening JSON."""
    advice = normalize_contract_advice(analysis.get("contract_advice"))
    if advice:
        from datetime import datetime, timezone

        advice["generated_at"] = now_utc().isoformat()
        advice["source"] = "ai_screen"
        analysis["contract_advice"] = advice
    return analysis

