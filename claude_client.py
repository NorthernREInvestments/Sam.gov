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
MAX_PDFS = 5
MAX_TOKENS = 3000

DEFAULT_SCREENING_PROMPT = """You are a government contract screening specialist for a small business prime contractor using the subcontracting middleman model.

YOUR #1 JOB: Read the contract posting AND every attached PDF/solicitation document. Extract the full scope of work. Then write a plain-English summary a busy contractor can understand in 15 seconds.

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

Return JSON only with these exact fields:
- plain_english_summary: string (the summary above — MOST IMPORTANT FIELD)
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
- sub_type_needed: string
- red_flags: array of strings
- far_52_219_14: true or false
- security_clearance_required: true or false
- option_years: number or null
- attachments_reviewed: array of strings listing PDF filenames or URLs you read

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


def _attachment_blocks(urls: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    blocks: list[dict[str, Any]] = []
    reviewed: list[str] = []
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for url in urls:
            if len(blocks) >= MAX_PDFS:
                break
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            content_type = (resp.headers.get("content-type") or "").lower()
            is_pdf = (
                "pdf" in content_type
                or url.lower().split("?")[0].endswith(".pdf")
                or resp.content[:4] == b"%PDF"
            )
            if not is_pdf or len(resp.content) > MAX_PDF_BYTES:
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
    return blocks, reviewed


def build_screening_text(contract: Any, attachment_count: int) -> str:
    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    description = (
        contract.description
        or raw.get("description")
        or raw.get("additionalInfoLink")
        or "Not provided in posting"
    )
    urls = _collect_urls(raw)
    lines = [
        "Analyze this federal contract. Read all attached PDF documents carefully for scope, size, and requirements.",
        "",
        f"Notice ID: {contract.notice_id}",
        f"Title: {contract.title}",
        f"Agency: {contract.agency or 'Unknown'}",
        f"Location: {contract.location or 'Unknown'}",
        f"NAICS: {contract.naics_code or 'Unknown'}",
        f"Set-aside: {contract.set_aside or 'Unknown'}",
        f"Due date: {contract.due_date.isoformat() if contract.due_date else 'Unknown'}",
        f"SAM.gov link: {contract.link or 'Unknown'}",
        f"PDF attachments included in this message: {attachment_count}",
        "",
        "Posting description:",
        str(description)[:15000],
    ]
    if urls:
        lines.extend(["", "All linked URLs from posting:"])
        lines.extend(f"- {url}" for url in urls[:15])
    if raw:
        lines.extend(["", "Full SAM.gov record (JSON):", json.dumps(raw, default=str)[:10000]])
    return "\n".join(lines)


def screen_contract(contract: Any, system_prompt: str | None = None) -> dict[str, Any]:
    """Send one contract to Claude and return parsed screening JSON."""
    if system_prompt is None:
        from settings_store import resolve_screening_prompt

        system_prompt = resolve_screening_prompt()

    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    urls = _collect_urls(raw)
    pdf_blocks, fetched_labels = _attachment_blocks(urls)
    text = build_screening_text(contract, len(pdf_blocks))

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

    if analysis.get("pursue") is True:
        analysis["pursue"] = True
    elif analysis.get("pursue") is False:
        analysis["pursue"] = False

    return analysis
