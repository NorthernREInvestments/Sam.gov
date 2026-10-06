"""Phases 6–7 — Bounded current-revenue search (Bridgeport / Go-Metro / DeKalb)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from document_quality import extract_pdf_text
from final_pre_scale_proof.models import (
    BRIDGEPORT_OID,
    BUYER_CATEGORY_REFERENCE,
    DEKALB_OID,
    GO_METRO_OID,
    PRIOR_REV,
    REVENUE_NOT_READY,
)
from m3_data_root import data_path

_DOLLAR = re.compile(r"\$\s*([\d,]{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")
_SKIP = re.compile(
    r"\b(insurance|liability|workers?\s*comp|bond|surety|umbrella|"
    r"davis[- ]bacon|liquidated\s+damages?|per\s+calendar\s+day|"
    r"coverage|aggregate\s+limit)\b",
    re.I,
)
_VALUE = re.compile(
    r"\b(estimate|budget|contract\s+(?:sum|value|amount)|not[- ]to[- ]exceed|"
    r"NTE|engineer's?\s+estimate|bid\s+total|project\s+cost|authorization|"
    r"capital|grant\s+award|annual\s+(?:value|spend|quantity)|ceiling)\b",
    re.I,
)


def _docs_for(buyer: str, sid: str) -> Path:
    return data_path(f"opengov_public_docs/documents/{buyer}/{sid}")


def _scan_pdfs(folder: Path, *, max_pages: int = 25) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    if not folder.exists():
        return hits
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() != ".pdf":
            continue
        try:
            text = extract_pdf_text(str(f), max_pages=max_pages) or ""
        except Exception:
            continue
        for m in _DOLLAR.finditer(text):
            try:
                amt = float(m.group(1).replace(",", ""))
            except Exception:
                continue
            if amt < 100:
                continue
            start = max(0, m.start() - 100)
            end = min(len(text), m.end() + 120)
            window = re.sub(r"\s+", " ", text[start:end])
            if _SKIP.search(window):
                continue
            if _VALUE.search(window) or amt >= 1000:
                hits.append(
                    {
                        "document": f.name,
                        "amount": amt,
                        "window": window[:280],
                        "value_language": bool(_VALUE.search(window)),
                    }
                )
    # Prefer value-language hits
    hits.sort(key=lambda h: (not h["value_language"], -h["amount"]))
    return hits[:20]


def _prior(oid: str) -> dict[str, Any]:
    p = data_path(PRIOR_REV)
    if not p.exists():
        return {}
    store = json.loads(p.read_text(encoding="utf-8"))
    return ((store.get("by_opportunity") or {}).get(oid) or {}).get("revenue") or {}


def search_bridgeport() -> dict[str, Any]:
    """Find stronger current revenue than $12.3M BUYER_CATEGORY_REFERENCE."""
    folder = _docs_for("bridgeportct", "299806")
    hits = _scan_pdfs(folder)
    defensible = None
    classification = REVENUE_NOT_READY
    source = None
    for h in hits:
        # Reject insurance/bond already filtered; require explicit value language
        if h["value_language"] and 2_000 <= h["amount"] <= 5_000_000:
            defensible = h["amount"]
            classification = "ESTIMATE_OR_BUDGET_CANDIDATE"
            source = h["document"]
            break

    prior = _prior(BRIDGEPORT_OID)
    prior_best = (prior.get("best") or {}) if prior else {}

    if defensible is None:
        return {
            "opportunity_id": BRIDGEPORT_OID,
            "prior_value": 12304488.0,
            "prior_classification": BUYER_CATEGORY_REFERENCE,
            "current_defensible_value": None,
            "source": None,
            "classification": REVENUE_NOT_READY,
            "PASS_FAIL": "FAIL",
            "do_not_use_for_economics": [12304488.0],
            "scan_hits_sample": hits[:5],
            "note": "No current solicitation budget/estimate/contract sum found; insurance/bond amounts excluded",
        }

    return {
        "opportunity_id": BRIDGEPORT_OID,
        "prior_value": prior_best.get("reference_value") or 12304488.0,
        "prior_classification": BUYER_CATEGORY_REFERENCE,
        "current_defensible_value": defensible,
        "source": source,
        "classification": classification,
        "PASS_FAIL": "PASS",
        "scan_hits_sample": hits[:5],
    }


def search_go_metro_bounded() -> dict[str, Any]:
    """Bounded search — do not waste deep budget."""
    folder = _docs_for("go-metro", "298984")
    hits = _scan_pdfs(folder, max_pages=15)
    # Prefer current estimated spend / line extensions near basket scale
    candidates = [h for h in hits if h["value_language"] and h["amount"] >= 500]
    if not candidates:
        return {
            "opportunity_id": GO_METRO_OID,
            "prior_historical": 1149.0,
            "current_defensible_value": None,
            "source": None,
            "classification": "SUPPLIER_CHANNEL_PROOF_ONLY",
            "PASS_FAIL": "FAIL",
            "scan_hits_sample": hits[:5],
            "note": "Keep as SUPPLIER_CHANNEL_PROOF; historical ~$1,149 too weak for $5K/$10K",
        }
    best = candidates[0]
    return {
        "opportunity_id": GO_METRO_OID,
        "prior_historical": 1149.0,
        "current_defensible_value": best["amount"],
        "source": best["document"],
        "classification": "CURRENT_VALUE_CANDIDATE",
        "PASS_FAIL": "PASS",
        "window": best["window"],
        "scan_hits_sample": hits[:5],
    }


def search_dekalb_bounded() -> dict[str, Any]:
    folder = _docs_for("dekalbcountyga", "286698")
    hits = _scan_pdfs(folder, max_pages=15)
    candidates = [h for h in hits if h["value_language"] and h["amount"] >= 500]
    if not candidates:
        return {
            "opportunity_id": DEKALB_OID,
            "prior_historical": 142.5,
            "current_defensible_value": None,
            "source": None,
            "classification": "SUPPLIER_CHANNEL_PROOF_ONLY",
            "PASS_FAIL": "FAIL",
            "scan_hits_sample": hits[:5],
            "note": "Keep as SUPPLIER_CHANNEL_PROOF; historical ~$142.50 too weak for $5K/$10K",
        }
    best = candidates[0]
    return {
        "opportunity_id": DEKALB_OID,
        "prior_historical": 142.5,
        "current_defensible_value": best["amount"],
        "source": best["document"],
        "classification": "CURRENT_VALUE_CANDIDATE",
        "PASS_FAIL": "PASS",
        "window": best["window"],
        "scan_hits_sample": hits[:5],
    }
