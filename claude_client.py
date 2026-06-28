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
MAX_PDF_BYTES = 4_500_000
MAX_PDFS = 8
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

PLAIN ENGLISH SUMMARY (plain_english_summary field — MOST IMPORTANT):
Write under 200 words. Sound like you're explaining it to a friend, not a lawyer. No jargon.

Cover these points in simple conversational language:
1. What they actually want done — one to three sentences max
2. Where the work is — city and state, plus the nearest decent-size city to find subcontractors
3. How big — square footage or unit count if available
4. How often — daily, weekly, monthly, or one-time
5. How long — base year plus any option years
6. What kind of subcontractor is needed — be specific (e.g. "licensed commercial janitorial company" or "licensed landscaping crew")
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
  - square_footage: integer or null — exact sq ft from "gross square footage", "cleanable square footage", "area to be cleaned", "net square footage"
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
- sub_type_needed: string
- red_flags: array of strings
- far_52_219_14: true or false
- security_clearance_required: true or false
- option_years: number or null
- attachments_reviewed: array of strings listing PDF filenames or URLs you read (empty array if none downloaded — note external portal instead in plain_english_summary)
- document_access: object echoing the document_access block from the user message (status, summary, external_portals, requires_external_portal)
- external_links: array of {url, label} objects from the posting when provided

Respond with JSON only. No markdown fences."""

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
    data = json.loads(cleaned)
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


def _pdf_blocks_from_bytes(pdfs: list[tuple[str, bytes]]) -> tuple[list[dict[str, Any]], list[str]]:
    blocks: list[dict[str, Any]] = []
    labels: list[str] = []
    for name, data in pdfs:
        if len(blocks) >= MAX_PDFS:
            break
        if not data.startswith(b"%PDF"):
            continue
        blocks.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(data).decode("ascii"),
                },
            }
        )
        labels.append(name)
    return blocks, labels


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
    blocks, labels = _pdf_blocks_from_bytes(pdfs)
    if pdfs and not blocks:
        skipped.append("PIEE returned files but none were readable PDFs.")
    return blocks, labels, skipped


def _attachment_blocks(urls: list[str]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    from api_budget import can_download_screening_pdf, record_sam_pdf_download

    blocks: list[dict[str, Any]] = []
    reviewed: list[str] = []
    skipped: list[str] = []
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        for url in urls:
            if len(blocks) >= MAX_PDFS:
                skipped.append(f"{url} (max {MAX_PDFS} PDFs per screen)")
                continue
            is_sam_download = "sam.gov" in url.lower()
            if is_sam_download and not can_download_screening_pdf():
                skipped.append(f"{url} (SAM PDF download budget reached)")
                continue
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                skipped.append(url)
                continue
            content_type = (resp.headers.get("content-type") or "").lower()
            is_pdf = (
                "pdf" in content_type
                or url.lower().split("?")[0].endswith(".pdf")
                or resp.content[:4] == b"%PDF"
            )
            if not is_pdf:
                skipped.append(f"{url} (not a PDF)")
                continue
            if len(resp.content) > MAX_PDF_BYTES:
                skipped.append(f"{url} (PDF too large)")
                continue
            if is_sam_download and not record_sam_pdf_download():
                skipped.append(f"{url} (SAM PDF download budget reached)")
                continue
            label = url.split("/")[-1][:80] or url[:80]
            reviewed.append(label)
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(resp.content).decode("ascii"),
                    },
                }
            )
    return blocks, reviewed, skipped


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


SOLICITATION_META_PROMPT = """You extract contracting and submission details from federal solicitation PDFs.

Read the attached solicitation, PWS, and instruction documents. Return JSON only:
{
  "contracting_officer_name": string or null,
  "contracting_officer_email": string or null,
  "submission_method": string or null,
  "base_year_start": string or null,
  "base_year_end": string or null,
  "agency_address": string or null,
  "solicitation_number": string or null
}

Rules:
- submission_method should state HOW to submit (email address, SAM.gov, PIEE, FedConnect, etc.)
- Use exact names and emails from the document — do not invent values
- Use null for anything not clearly stated
No markdown fences."""


def _contract_pdf_blocks(contract: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Load PDF blocks for Claude (same prioritization as screening)."""
    from sam_enrich import ensure_enriched_sam_raw, needs_attachment_refresh

    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    ensure_enriched_sam_raw(
        contract,
        force=needs_attachment_refresh(raw) or not raw.get("descriptionText"),
    )
    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    urls = _collect_attachment_urls(raw)
    piee_blocks, piee_labels, _ = _piee_pdf_blocks(raw)
    sam_blocks, sam_labels, _ = _attachment_blocks(urls)

    pdf_blocks: list[dict[str, Any]] = []
    fetched_labels: list[str] = []
    for block, label in zip(piee_blocks + sam_blocks, piee_labels + sam_labels):
        if len(pdf_blocks) >= MAX_PDFS:
            break
        pdf_blocks.append(block)
        fetched_labels.append(label)
    return pdf_blocks, fetched_labels


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
        "Extract contracting officer and submission details from the solicitation documents.",
        "SAM posting excerpt:",
        (contract.description or sam.get("descriptionText") or "")[:4000],
    ]
    if labels:
        lines.append(f"\nPDFs attached: {', '.join(labels[:8])}")

    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}, *pdf_blocks]
    client = Anthropic(api_key=_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SOLICITATION_META_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = response.content[0].text if response.content else "{}"
    data = _extract_json(text)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if v is not None and str(v).strip()}


def screen_contract(contract: Any, system_prompt: str | None = None) -> dict[str, Any]:
    """Fetch USAspending data, send contract + pricing to Claude, return screening JSON."""
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

    return analysis


SUB_ANALYSIS_PROMPT = """You are helping a prime contractor evaluate potential subcontractors found via Google Places.

For each subcontractor candidate, assign a recommendation score from 1-10 and a one-sentence reason using these signals:
- Rating above 4.0 stars — positive
- More than 20 reviews — positive
- Website present — positive
- Review count under 5 — negative (may be too small)
- Rating under 3.5 — negative
- Distance under 10 miles — positive

Return JSON only:
{
  "subs": [
    {"place_id": "string", "score": 7, "reason": "One sentence."}
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
    summary_lines = [
        f"Contract: {contract.title}",
        f"Location: {work.get('label') or contract.location}",
        f"Sub type needed: {(contract.analysis or {}).get('sub_type_needed') or 'unknown'}",
        "",
        "Candidates:",
        json.dumps(candidates, indent=2, default=str),
    ]
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

