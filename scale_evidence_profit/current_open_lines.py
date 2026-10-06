"""Extract current OpenGov open-project price-table lines for both-sides matching."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from evidence_breakthrough.opengov_history import fetch_project, list_public_projects

log = logging.getLogger("govtracker.scale_evidence_profit.current_open")


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_identities_from_project(project: dict[str, Any], government_code: str) -> list[dict[str, Any]]:
    """Turn priceTables priceItems into identity-like rows (PN from custom1/custom2)."""
    out: list[dict[str, Any]] = []
    pid = project.get("id")
    oid = f"opengov:{government_code}:{pid}"
    for pt in project.get("priceTables") or []:
        if not isinstance(pt, dict):
            continue
        for it in pt.get("priceItems") or []:
            if not isinstance(it, dict) or it.get("isHeaderRow"):
                continue
            pn = it.get("custom1") or it.get("custom2") or it.get("itemCode") or it.get("itemName")
            desc = it.get("description") or ""
            if not pn and not desc:
                continue
            # Skip pure numeric UOM-only junk with no description
            if not desc and not pn:
                continue
            # Prefer rows with a real part-looking token
            pn_s = str(pn).strip() if pn else None
            if pn_s and (pn_s in {"0", "None"} or len(pn_s) < 3):
                pn_s = None
            if not pn_s and len(desc) < 8:
                continue
            out.append(
                {
                    "opportunity_id": oid,
                    "line_id": f"OG-{pid}-{it.get('id') or it.get('lineItem') or len(out)}",
                    "raw_description": desc,
                    "manufacturer": None,
                    "model": pn_s,
                    "part_number": pn_s,
                    "catalog_number": it.get("custom2") if it.get("custom2") != pn_s else None,
                    "quantity": _f(it.get("quantity")),
                    "uom": it.get("unitToMeasure") if not str(it.get("unitToMeasure") or "").isdigit() else "EA",
                    "uom_normalized": "EA",
                    "confidence_grade": "A" if pn_s else "C",
                    "identity_type": "EXACT_PART_NUMBER" if pn_s else "DESCRIPTION",
                    "commercial_search_key": f"{pn_s} {desc}".strip() if pn_s else desc,
                    "project_title": project.get("title"),
                    "financial_id": project.get("financialId"),
                    "status": project.get("status"),
                    "source": "opengov_price_table",
                }
            )
    return out


def harvest_open_project_identities(
    government_codes: list[str],
    *,
    max_buyers: int = 25,
    max_open_per_buyer: int = 8,
    client: httpx.Client | None = None,
    on_progress: Any = None,
) -> list[dict[str, Any]]:
    """Harvest identities from current open/evaluation projects for history-rich buyers."""
    own = client is None
    c = client or httpx.Client(timeout=40.0)
    harvested: list[dict[str, Any]] = []
    try:
        for bi, code in enumerate(government_codes[:max_buyers]):
            if on_progress:
                on_progress(phase="HARVEST_OPEN", pct=int(10 + 30 * bi / max(max_buyers, 1)), buyer=code)
            listed = list_public_projects(code, limit=40, offset=0, client=c)
            if not listed.get("ok"):
                continue
            rows = [r for r in (listed.get("rows") or []) if isinstance(r, dict)]
            # Prefer open / evaluation
            openish = [
                r
                for r in rows
                if str(r.get("status") or "").lower() in {"open", "evaluation", "published"}
            ]
            if not openish:
                openish = rows[:max_open_per_buyer]
            for meta in openish[:max_open_per_buyer]:
                pid = meta.get("id")
                if not pid:
                    continue
                fr = fetch_project(pid, code, client=c)
                if not fr.get("ok") or not fr.get("project"):
                    continue
                ids = extract_identities_from_project(fr["project"], code)
                # Keep only those with PN-like tokens for matching yield
                usable = [x for x in ids if x.get("part_number")]
                harvested.extend(usable if usable else ids[:20])
                time.sleep(0.08)
    finally:
        if own:
            c.close()
    return harvested
