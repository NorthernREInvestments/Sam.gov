"""Phase H cohort selection — deterministic top-N from Phase G LIVE_SOURCE products."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from phase_g.provenance import PROVENANCE_LIVE_SOURCE

ROOT = Path(__file__).resolve().parents[1]
PHASE_G_RUN = ROOT / "artifacts" / "phase_g" / "live_run_latest.json"
ARTIFACTS = ROOT / "artifacts" / "phase_h"

_NSN_RE = re.compile(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b")
_PN_RE = re.compile(r"\bP/?N[:\s]+([A-Z0-9][A-Z0-9._/-]{2,})\b", re.I)
_SERVICEISH = re.compile(
    r"\b(janitorial|consulting|staffing|construction|rehabilitation|services?\b|idiq\b|sacc\b)\b",
    re.I,
)


def _score_row(r: dict[str, Any]) -> tuple:
    title = str(r.get("title") or "")
    nsn = 1 if r.get("has_nsn") or _NSN_RE.search(title) else 0
    pn = 1 if _PN_RE.search(title) else 0
    dla = 1 if r.get("is_dla") else 0
    kit = 1 if re.search(r"\b(PARTS?\s+KIT|TEST\s+SET|ADAPTER|ASSEMBLY|PUMP|VALVE|BEARING)\b", title, re.I) else 0
    link = 1 if (r.get("ui_link") or r.get("listing_url")) else 0
    enriched = 1 if r.get("enriched") else 0
    service_pen = -2 if _SERVICEISH.search(title) else 0
    # Higher score first; then stable id
    return (nsn + pn + dla + kit + link + enriched + service_pen, str(r.get("canonical_id") or ""))


def load_phase_g_products() -> list[dict[str, Any]]:
    data = json.loads(PHASE_G_RUN.read_text(encoding="utf-8"))
    rows = [
        r
        for r in (data.get("all_results") or [])
        if isinstance(r, dict) and r.get("is_product") and r.get("phase_g_provenance") == PROVENANCE_LIVE_SOURCE
    ]
    return rows


def select_cohort(*, size: int = 25) -> list[dict[str, Any]]:
    rows = load_phase_g_products()
    # Prefer enriched; fill from non-enriched product if needed
    enriched = [r for r in rows if r.get("enriched")]
    other = [r for r in rows if not r.get("enriched")]
    ranked = sorted(enriched, key=_score_row, reverse=True) + sorted(other, key=_score_row, reverse=True)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in ranked:
        cid = str(r.get("canonical_id") or r.get("source_opportunity_id") or "")
        if not cid or cid in seen:
            continue
        if not r.get("title"):
            continue
        seen.add(cid)
        why = []
        title = str(r.get("title") or "")
        if r.get("has_nsn") or _NSN_RE.search(title):
            why.append("exact_or_title_NSN")
        if r.get("is_dla"):
            why.append("DLA_SAM_listing")
        if _PN_RE.search(title):
            why.append("P/N_in_title")
        if re.search(r"PARTS?\s+KIT|TEST\s+SET", title, re.I):
            why.append("clear_product_noun")
        if r.get("ui_link") or r.get("listing_url"):
            why.append("has_listing_url")
        if r.get("enriched"):
            why.append("phase_g_enriched")
        out.append(
            {
                **r,
                "phase_h_cohort": True,
                "phase_h_selection_reasons": why or ["product_classified_live_source"],
                "phase_h_provenance": PROVENANCE_LIVE_SOURCE,
                "known_blockers_before_deep_research": list(r.get("execution_critical_blockers") or [])[:12],
            }
        )
        if len(out) >= size:
            break
    return out


def write_cohort_docs(cohort: list[dict[str, Any]]) -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / "cohort.json"
    path.write_text(json.dumps({"kind": "PhaseHCohort", "count": len(cohort), "opportunities": cohort}, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Phase H — Deep-Research Cohort",
        "",
        f"**Size:** {len(cohort)} (target 25)",
        "**Provenance:** all `LIVE_SOURCE` from Phase G (no fixtures).",
        "**Selection:** deterministic score = NSN + P/N + DLA + product noun + listing URL + enriched − serviceish; tie-break `canonical_id`.",
        "",
        "| # | Source ID | Solicitation | Title | Buyer | Deadline | Category | Why selected | Pre-deep blockers |",
        "|---|-----------|--------------|-------|-------|----------|----------|--------------|-------------------|",
    ]
    for i, r in enumerate(cohort, 1):
        title = (str(r.get("title") or "")[:60]).replace("|", "/")
        blockers = ",".join(str(b) for b in (r.get("known_blockers_before_deep_research") or [])[:3]) or "listing_only_unknowns"
        why = ",".join(r.get("phase_h_selection_reasons") or [])
        lines.append(
            f"| {i} | `{r.get('source_opportunity_id') or r.get('canonical_id')}` | "
            f"{r.get('solicitation_number') or 'UNKNOWN'} | {title} | {r.get('buyer') or 'UNKNOWN'} | "
            f"{r.get('deadline') or 'UNKNOWN'} | {r.get('product_class') or 'UNKNOWN'} | {why} | `{blockers}` |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Estimated value: UNKNOWN on Phase G listing rows (not fabricated).",
            "- Deadlines often UNKNOWN until live listing hydration.",
            "- Cohort intentionally includes NSN, parts kits, and quote-required complexity — not only easy COTS.",
            "",
        ]
    )
    doc = ROOT / "docs" / "phase_h_cohort.md"
    doc.write_text("\n".join(lines), encoding="utf-8")
    return doc


if __name__ == "__main__":
    c = select_cohort(size=25)
    write_cohort_docs(c)
    print(json.dumps({"count": len(c), "titles": [x.get("title") for x in c]}, indent=2, default=str))
