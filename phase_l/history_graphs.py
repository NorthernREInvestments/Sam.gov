"""Phase L.10 — product history graph + supplier graph + award normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc

BUILD = "20260928-m3-phase-l10-exact-evidence-workflow"
ROOT = Path(__file__).resolve().parents[1]
PRODUCT_GRAPH_PATH = ROOT / "data" / "phase_l10_product_history_graph.json"
SUPPLIER_GRAPH_PATH = ROOT / "data" / "phase_l10_supplier_graph.json"
BUYER_MEMORY_PATH = ROOT / "data" / "phase_l10_buyer_retrieval_memory.json"


def _utc() -> str:
    return now_utc().isoformat()


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"nodes": {}, "edges": [], "build": BUILD}
    return {"nodes": {}, "edges": [], "build": BUILD}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _utc()
    data["build"] = BUILD
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def normalize_award_tabulation(raw: dict[str, Any]) -> dict[str, Any]:
    """Exact award/tabulation normalization for reusable buyer/product history."""
    return {
        "kind": "NormalizedAward",
        "buyer": raw.get("buyer") or raw.get("agency") or raw.get("department"),
        "solicitation_id": raw.get("solicitation_id") or raw.get("solicitation_number") or raw.get("notice_id"),
        "vendor": raw.get("vendor") or raw.get("awardee") or raw.get("supplier"),
        "manufacturer": raw.get("manufacturer"),
        "model": raw.get("model") or raw.get("mpn"),
        "item": raw.get("item") or raw.get("description") or raw.get("title"),
        "quantity": raw.get("quantity"),
        "uom": raw.get("uom") or raw.get("unit_of_measure"),
        "unit_price": raw.get("unit_price") or raw.get("unit_value"),
        "total": raw.get("total") or raw.get("total_value") or raw.get("award_amount"),
        "award_date": raw.get("award_date") or raw.get("date"),
        "source": raw.get("source"),
        "platform": raw.get("platform"),
    }


def link_product_history(
    *,
    product_key: str,
    manufacturer: str | None = None,
    mpn_model: str | None = None,
    buyer: str | None = None,
    solicitation: str | None = None,
    award: dict[str, Any] | None = None,
    vendor: str | None = None,
    quantity: Any = None,
    price: Any = None,
    date: str | None = None,
) -> dict[str, Any]:
    g = _load(PRODUCT_GRAPH_PATH)
    nodes = g.setdefault("nodes", {})
    edges = g.setdefault("edges", [])
    pk = (product_key or mpn_model or manufacturer or "UNKNOWN")[:120]
    node = nodes.setdefault(
        pk,
        {
            "product": pk,
            "manufacturer": manufacturer,
            "mpn_model": mpn_model,
            "buyers": [],
            "awards": [],
            "vendors": [],
        },
    )
    if buyer and buyer not in node["buyers"]:
        node["buyers"].append(buyer)
    if vendor and vendor not in node["vendors"]:
        node["vendors"].append(vendor)
    award_rec = {
        "solicitation": solicitation,
        "buyer": buyer,
        "vendor": vendor,
        "quantity": quantity,
        "price": price,
        "date": date,
        "award": award,
    }
    node["awards"].append(award_rec)
    edges.append(
        {
            "type": "PRODUCT_AWARD",
            "product": pk,
            "buyer": buyer,
            "vendor": vendor,
            "solicitation": solicitation,
            "at": _utc(),
        }
    )
    # Cap growth
    if len(node["awards"]) > 50:
        node["awards"] = node["awards"][-50:]
    if len(edges) > 2000:
        g["edges"] = edges[-2000:]
    _save(PRODUCT_GRAPH_PATH, g)
    return {"product_key": pk, "node": node}


def query_product_history(product_key: str | None = None, *, manufacturer: str | None = None, model: str | None = None) -> dict[str, Any]:
    g = _load(PRODUCT_GRAPH_PATH)
    nodes = g.get("nodes") or {}
    if product_key and product_key in nodes:
        return {"hit": True, "node": nodes[product_key], "source": "exact_key"}
    mfr = (manufacturer or "").upper()
    mod = (model or "").upper()
    for k, n in nodes.items():
        if mod and mod in str(n.get("mpn_model") or k).upper():
            return {"hit": True, "node": n, "source": "model_match"}
        if mfr and mfr[:8] in str(n.get("manufacturer") or "").upper():
            return {"hit": True, "node": n, "source": "manufacturer_match"}
    return {"hit": False, "node": None}


def link_supplier(
    *,
    supplier: str,
    manufacturer: str | None = None,
    product_family: str | None = None,
    exact_model: str | None = None,
    authorization: str | None = None,
    past_gov_awards: list | None = None,
    quote_capability: bool | None = None,
) -> dict[str, Any]:
    g = _load(SUPPLIER_GRAPH_PATH)
    nodes = g.setdefault("nodes", {})
    key = (supplier or "UNKNOWN")[:120].lower()
    node = nodes.setdefault(
        key,
        {
            "supplier": supplier,
            "manufacturers": [],
            "product_families": [],
            "exact_models": [],
            "authorization": None,
            "past_government_awards": [],
            "quote_capability": None,
            "prior_m3_interactions": [],
        },
    )
    if manufacturer and manufacturer not in node["manufacturers"]:
        node["manufacturers"].append(manufacturer)
    if product_family and product_family not in node["product_families"]:
        node["product_families"].append(product_family)
    if exact_model and exact_model not in node["exact_models"]:
        node["exact_models"].append(exact_model)
    if authorization:
        node["authorization"] = authorization
    if past_gov_awards:
        node["past_government_awards"].extend(past_gov_awards)
        node["past_government_awards"] = node["past_government_awards"][-30:]
    if quote_capability is not None:
        node["quote_capability"] = quote_capability
    _save(SUPPLIER_GRAPH_PATH, g)
    return {"supplier_key": key, "node": node}


def query_supplier_graph(supplier: str | None = None, *, manufacturer: str | None = None) -> dict[str, Any]:
    g = _load(SUPPLIER_GRAPH_PATH)
    nodes = g.get("nodes") or {}
    if supplier:
        key = supplier.lower()[:120]
        if key in nodes:
            return {"hit": True, "node": nodes[key]}
    mfr = (manufacturer or "").lower()
    if mfr:
        for n in nodes.values():
            if any(mfr[:6] in str(x).lower() for x in (n.get("manufacturers") or [])):
                return {"hit": True, "node": n, "source": "manufacturer"}
    return {"hit": False}


def persist_buyer_retrieval(
    buyer: str,
    *,
    bid_tabs_url: str | None = None,
    board_packets_url: str | None = None,
    award_spreadsheet_pattern: str | None = None,
    quantity_path: str | None = None,
    platform: str | None = None,
    submission_pattern: str | None = None,
) -> dict[str, Any]:
    data = _load(BUYER_MEMORY_PATH)
    buyers = data.setdefault("buyers", {})
    key = (buyer or "UNKNOWN")[:120]
    rec = buyers.setdefault(key, {"buyer": buyer})
    if bid_tabs_url:
        rec["bid_tabs_url"] = bid_tabs_url
    if board_packets_url:
        rec["board_packets_url"] = board_packets_url
    if award_spreadsheet_pattern:
        rec["award_spreadsheet_pattern"] = award_spreadsheet_pattern
    if quantity_path:
        rec["quantity_path"] = quantity_path
    if platform:
        rec["procurement_platform"] = platform
    if submission_pattern:
        rec["submission_pattern"] = submission_pattern
    rec["updated_at"] = _utc()
    _save(BUYER_MEMORY_PATH, data)
    return rec
