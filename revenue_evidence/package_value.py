"""Mine current solicitation package for explicit value (Tier R1).

Build: 20261004-m3-revenue-evidence-v1
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from eligibility_and_recovery.file_mining import extract_document_pages, list_package_documents
from revenue_evidence.models import CURRENT_VALUE_EXPLICIT, TIER_R1

_MONEY = re.compile(
    r"(?:USD\s*|US\$|\$)\s*([\d,]{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)",
    re.I,
)
_VALUE_CTX = re.compile(
    r"(?:"
    r"estimated\s+(?:contract\s+)?(?:value|cost|amount|spend)|"
    r"anticipated\s+(?:spend|value|cost|amount)|"
    r"(?:total\s+)?(?:budget(?:ed)?\s+amount|budget)|"
    r"(?:not[- ]to[- ]exceed|NTE)\s*(?:amount|value|of)?|"
    r"(?:contract\s+)?ceiling|"
    r"engineer(?:'?s)?\s+estimate|"
    r"previous\s+contract\s+value|"
    r"estimated\s+annual\s+(?:value|spend|quantity)|"
    r"approximate\s+(?:value|cost|amount)"
    r")",
    re.I,
)
_NEG = re.compile(
    r"\b(?:bid\s+bond|performance\s+bond|insurance|deductible|liquidated\s+damages|"
    r"per\s+occurrence|aggregate\s+limit|page\s+\d+)\b",
    re.I,
)


def _f_money(s: str) -> float | None:
    try:
        v = float(s.replace(",", ""))
    except (TypeError, ValueError):
        return None
    if 100 <= v <= 500_000_000:  # filter tiny noise / absurd
        return v
    return None


def mine_current_package_value(
    opportunity_id: str,
    *,
    max_docs: int = 25,
    max_pages_per_doc: int = 40,
) -> dict[str, Any]:
    """Extract CURRENT_VALUE_EXPLICIT from solicitation package text."""
    docs = list_package_documents(opportunity_id)[:max_docs]
    hits: list[dict[str, Any]] = []
    scanned = 0
    for fp in docs:
        try:
            pages = extract_document_pages(fp, max_pages=max_pages_per_doc)
        except Exception:
            continue
        scanned += 1
        for p in pages:
            text = p.get("text") or ""
            if not text.strip():
                continue
            # Window around value cues
            for m in _VALUE_CTX.finditer(text):
                start = max(0, m.start() - 40)
                end = min(len(text), m.end() + 120)
                window = text[start:end]
                if _NEG.search(window) and not re.search(r"estimated|budget|ceiling|NTE", window, re.I):
                    continue
                for mm in _MONEY.finditer(window):
                    amt = _f_money(mm.group(1))
                    if amt is None:
                        continue
                    hits.append(
                        {
                            "amount": amt,
                            "cue": m.group(0),
                            "snippet": re.sub(r"\s+", " ", window).strip()[:240],
                            "document": p.get("document") or fp.name,
                            "page": p.get("page"),
                        }
                    )
    if not hits:
        return {
            "found": False,
            "tier": None,
            "status": None,
            "documents_scanned": scanned,
            "reason": "NO_EXPLICIT_VALUE_IN_PACKAGE" if docs else "NO_PACKAGE_DOCUMENTS",
        }

    # Prefer mid-range explicit values; drop likely insurance floors if multiple
    amounts = sorted({h["amount"] for h in hits})
    # Heuristic: if many small (~$1M insurance) and one larger contract — prefer largest under 50M with "estimated/budget/NTE"
    preferred = [
        h
        for h in hits
        if re.search(r"estimated|budget|ceiling|NTE|not[- ]to[- ]exceed|engineer", h["cue"], re.I)
    ] or hits
    best = max(preferred, key=lambda h: h["amount"])
    return {
        "found": True,
        "tier": TIER_R1,
        "status": CURRENT_VALUE_EXPLICIT,
        "reference_value": best["amount"],
        "exact_or_estimated": "estimated",
        "unit_or_total": "total",
        "qty_basis": None,
        "confidence": "B",
        "source": best["document"],
        "page": best["page"],
        "snippet": best["snippet"],
        "all_candidates": amounts[:12],
        "documents_scanned": scanned,
        "limitations": "Explicit package language; verify against amendments",
        "used_as_acquisition_cost": False,
    }
