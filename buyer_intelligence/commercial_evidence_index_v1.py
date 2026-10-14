"""Unified commercial evidence index — reconnects existing research stores by OID/product.

Fixes the full-cohort scorecard miss where historical/gov prices existed in SPF /
profit-first / HT stores but were not joined into Available Deals overlays.
Never invents prices.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from m3_data_root import data_path

OUT = "_og_commercial_research_pipeline_out"
BUILD = "20261013-m3-commercial-evidence-index-v1"
INDEX_OUT = f"{OUT}/m3_commercial_evidence_index_v1.json"

_WS = re.compile(r"\s+")
_NONALNUM = re.compile(r"[^a-z0-9]+")


def _norm_title(s: str) -> str:
    t = _WS.sub(" ", str(s or "").strip().lower())
    return _NONALNUM.sub(" ", t).strip()


def _stem(s: str, n: int = 6) -> str:
    parts = _norm_title(s).split()
    return " ".join(parts[:n])


def _load(rel: str) -> Any:
    p = Path(data_path(rel))
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(rel: str, obj: Any) -> Path:
    path = Path(data_path(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return path


def build_commercial_evidence_index() -> dict[str, Any]:
    """Build OID → evidence bag + product-stem → historical unit prices."""
    by_oid: dict[str, dict[str, Any]] = {}
    by_product_stem: dict[str, list[dict[str, Any]]] = {}
    sources_used: list[str] = []

    def touch(oid: str) -> dict[str, Any]:
        bag = by_oid.setdefault(
            str(oid),
            {
                "opportunity_id": str(oid),
                "sources": [],
                "historical_unit": None,
                "historical_government_price": None,
                "public_acquisition_cost": None,
                "acquisition_prices": None,
                "verified_remaining_contribution": None,
                "conditional_remaining_contribution": None,
                "financing_status": None,
                "supplier_quote": None,
                "quotes": None,
                "package_depth": None,
                "real_lines": None,
                "line_items": None,
                "ht_evidence": None,
            },
        )
        return bag

    # 1) Existing available-deals overlays
    for name in (
        "m3_cex_v1_available_deals.json",
        "m3_rse_v1_available_deals.json",
        "m3_cgc_v1_available_deals.json",
        "m3_frds_v1_available_deals.json",
        "m3_source_product_v1_available_deals.json",
        "m3_adv_v1_available_deals.json",
        "m3_revenue_first_v1_available_deals.json",
    ):
        blob = _load(f"{OUT}/{name}")
        if not blob:
            continue
        sources_used.append(name)
        for d in blob.get("deals") or []:
            if not isinstance(d, dict):
                continue
            oid = str(d.get("opportunity_id") or "")
            if not oid:
                continue
            bag = touch(oid)
            if name not in bag["sources"]:
                bag["sources"].append(name)
            for k in (
                "historical_unit",
                "historical_government_price",
                "public_acquisition_cost",
                "acquisition_prices",
                "verified_remaining_contribution",
                "conditional_remaining_contribution",
                "financing_status",
                "supplier_quote",
                "quotes",
            ):
                if d.get(k) not in (None, "", [], {}) and bag.get(k) in (None, "", [], {}):
                    bag[k] = d.get(k)
            # status fields that may hide values
            if str(d.get("historical_pricing_status") or "").upper() == "KNOWN" and d.get("historical_unit"):
                bag["historical_unit"] = d.get("historical_unit")
            if str(d.get("pricing_status") or "").upper() == "KNOWN" and d.get("public_acquisition_cost"):
                bag["public_acquisition_cost"] = d.get("public_acquisition_cost")

    # 2) Profit-first index (economics may be null — still attach structure)
    pf = _load("m3_profit_first_index.json") or {}
    if pf:
        sources_used.append("m3_profit_first_index.json")
        for oid, v in (pf.get("by_opportunity") or {}).items():
            if not isinstance(v, dict):
                continue
            bag = touch(oid)
            if "m3_profit_first_index.json" not in bag["sources"]:
                bag["sources"].append("m3_profit_first_index.json")
            econ = v.get("economics") if isinstance(v.get("economics"), dict) else {}
            # Map known aliases without inventing
            for src_k, dst_k in (
                ("historical_unit", "historical_unit"),
                ("gov_unit", "historical_government_price"),
                ("award_unit", "historical_government_price"),
                ("acquisition_cost", "public_acquisition_cost"),
                ("product_cost", "public_acquisition_cost"),
                ("expected_profit", "conditional_remaining_contribution"),
                ("post_financing_profit", "verified_remaining_contribution"),
            ):
                val = econ.get(src_k)
                if val not in (None, "", [], {}) and bag.get(dst_k) in (None, "", [], {}):
                    bag[dst_k] = val
            if econ.get("profit_status"):
                bag["profit_status"] = econ.get("profit_status")
            bag["profit_first_title"] = v.get("title")

    # 3) SPF historical unit price store — product-stem index (not OID-keyed)
    spf = _load(f"{OUT}/m3_spf_v1_historical_unit_price_store.json") or {}
    if spf:
        sources_used.append("m3_spf_v1_historical_unit_price_store.json")
        for rec in spf.get("records") or []:
            if not isinstance(rec, dict):
                continue
            up = rec.get("unit_price")
            if up in (None, ""):
                continue
            for key in (rec.get("product"), rec.get("description"), rec.get("mpn")):
                stem = _stem(str(key or ""))
                if len(stem) < 4:
                    continue
                by_product_stem.setdefault(stem, []).append(
                    {
                        "unit_price": up,
                        "quantity": rec.get("quantity"),
                        "mpn": rec.get("mpn"),
                        "product": rec.get("product"),
                        "match_level": rec.get("match_level"),
                        "source": "spf_historical_unit_price_store",
                        "source_file": rec.get("source_file"),
                    }
                )

    # 4) HT max evidence — package/line depth by OID
    ht = _load("m3_ht_max_evidence_v2_results.json") or {}
    if ht:
        sources_used.append("m3_ht_max_evidence_v2_results.json")
        for row in ht.get("rows") or []:
            if not isinstance(row, dict):
                continue
            oid = str(row.get("OPPORTUNITY_ID") or "")
            if not oid:
                continue
            bag = touch(oid)
            if "m3_ht_max_evidence_v2_results.json" not in bag["sources"]:
                bag["sources"].append("m3_ht_max_evidence_v2_results.json")
            bag["package_depth"] = row.get("PACKAGE_DEPTH")
            bag["real_lines"] = row.get("REAL_LINES")
            bag["ht_evidence"] = {
                "detail_success": row.get("DETAIL_SUCCESS"),
                "attachments_fetched": row.get("ATTACHMENTS_FETCHED"),
                "qty_proven_lines": row.get("QTY_PROVEN_LINES"),
                "product_fit": row.get("PRODUCT_FIT"),
            }

    # 5) Scale evidence profit store — alternate ID formats
    scale = _load("m3_scale_evidence_profit_store.json") or {}
    if scale:
        sources_used.append("m3_scale_evidence_profit_store.json")
        for oid, v in (scale.get("by_opportunity") or {}).items():
            if not isinstance(v, dict):
                continue
            # Prefer bare hash suffix if present
            bare = str(oid)
            if ":" in bare:
                bare = bare.split(":")[-1]
            bag = touch(bare)
            if "m3_scale_evidence_profit_store.json" not in bag["sources"]:
                bag["sources"].append("m3_scale_evidence_profit_store.json")
            econ = v.get("economics") if isinstance(v.get("economics"), dict) else {}
            for src_k, dst_k in (
                ("gov_unit", "historical_government_price"),
                ("historical_unit", "historical_unit"),
                ("acquisition_cost", "public_acquisition_cost"),
                ("verified_contribution", "verified_remaining_contribution"),
            ):
                val = econ.get(src_k)
                if val not in (None, "", [], {}) and bag.get(dst_k) in (None, "", [], {}):
                    bag[dst_k] = val

    # Stats
    with_hist = sum(
        1
        for b in by_oid.values()
        if b.get("historical_unit") not in (None, "", [], {})
        or b.get("historical_government_price") not in (None, "", [], {})
    )
    with_public = sum(
        1
        for b in by_oid.values()
        if b.get("public_acquisition_cost") not in (None, "", [], {})
        or b.get("acquisition_prices") not in (None, "", [], {})
    )
    out = {
        "kind": "CommercialEvidenceIndex",
        "build": BUILD,
        "sources_used": sources_used,
        "by_opportunity_count": len(by_oid),
        "by_product_stem_count": len(by_product_stem),
        "opportunities_with_historical_price": with_hist,
        "opportunities_with_public_price": with_public,
        "by_opportunity": by_oid,
        "by_product_stem": {k: v[:5] for k, v in by_product_stem.items()},  # cap fanout
    }
    _save(INDEX_OUT, out)
    return out


def lookup_evidence_for_opportunity(
    oid: str,
    *,
    title: str | None = None,
    index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    idx = index or _load(INDEX_OUT) or build_commercial_evidence_index()
    bag = dict((idx.get("by_opportunity") or {}).get(str(oid)) or {})
    # Product-stem historical fill (exact stem only — never invent category prices)
    if title and bag.get("historical_unit") in (None, "", [], {}):
        stems = idx.get("by_product_stem") or {}
        hits: list[dict[str, Any]] = []
        for n in (6, 5, 4, 3):
            cand = _stem(title, n)
            if len(cand) < 4:
                continue
            hits = stems.get(cand) or []
            if hits:
                break
        if hits:
            bag["historical_unit"] = hits[0].get("unit_price")
            bag["historical_match"] = hits[0]
            bag.setdefault("sources", []).append("spf_product_stem_match")
    return bag
