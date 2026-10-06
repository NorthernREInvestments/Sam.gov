"""Commercial line clustering + category seller routing."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from line_basket_completion_strict_economics.models import BUILD, CLUSTERS, CATEGORY_ROUTES
from m3_data_root import data_path
import json


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (s or "").upper())


CATEGORY_SELLERS: dict[str, list[str]] = {
    "TOOLS": ["grainger.com", "zoro.com", "mscdirect.com"],
    "PLUMBING": ["supplyhouse.com", "grainger.com", "zoro.com"],
    "MRO": ["grainger.com", "zoro.com", "mscdirect.com", "globalindustrial.com"],
    "LIGHTING": ["1000bulbs.com", "grainger.com", "zoro.com"],
    "PPE": ["grainger.com", "zoro.com"],
    "OFFICE": ["staples.com", "grainger.com"],
    "FURNITURE": ["globalindustrial.com", "staples.com"],
    "HVAC": ["supplyhouse.com", "grainger.com", "zoro.com"],
    "AUTO_HD": ["finditparts.com", "fleetpride.com", "grainger.com"],
    "ELECTRICAL": ["grainger.com", "zoro.com", "1000bulbs.com"],
    "IRRIGATION": ["supplyhouse.com", "grainger.com"],
    "CONSTRUCTION_INSTALL": [],
    "OTHER": ["grainger.com", "zoro.com"],
}


def cluster_key(line: dict[str, Any]) -> str | None:
    mpn = _norm(line.get("mpn"))
    mfr = _norm(line.get("manufacturer"))
    if mpn and len(mpn) >= 4:
        return f"MPN:{mpn}"
    desc = _norm((line.get("description") or "")[:80])
    if mfr and desc and len(desc) >= 12:
        return f"FAM:{mfr}:{desc[:40]}"
    return None


def build_clusters(all_lines: list[dict[str, Any]]) -> dict[str, Any]:
    clusters: dict[str, dict[str, Any]] = {}
    for ln in all_lines:
        key = cluster_key(ln)
        if not key:
            continue
        cid = "CL-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        bucket = clusters.setdefault(
            cid,
            {
                "cluster_id": cid,
                "shared_identity": key,
                "member_lines": [],
                "shared_pricing_evidence": None,
                "quantity_values": [],
                "pack_values": [],
                "manufacturers": set(),
                "mpns": set(),
            },
        )
        bucket["member_lines"].append(
            {"opportunity_id": ln.get("opportunity_id"), "line_id": ln.get("line_id"), "quantity": ln.get("quantity")}
        )
        bucket["quantity_values"].append(ln.get("quantity"))
        bucket["pack_values"].append(ln.get("pack"))
        if ln.get("manufacturer"):
            bucket["manufacturers"].add(str(ln.get("manufacturer")))
        if ln.get("mpn"):
            bucket["mpns"].add(str(ln.get("mpn")))
        ln["cluster_id"] = cid
        # Reuse valid price if present
        if ln.get("terminal_state") == "PRICED_EXECUTABLE" and ln.get("unit_cost") and not bucket["shared_pricing_evidence"]:
            bucket["shared_pricing_evidence"] = {
                "unit_cost": ln.get("unit_cost"),
                "seller": ln.get("seller"),
                "source_url": ln.get("source_url"),
                "price_origin": ln.get("price_origin"),
            }

    # Serialize sets
    out_clusters = []
    for c in clusters.values():
        out_clusters.append(
            {
                **c,
                "manufacturers": sorted(c["manufacturers"]),
                "mpns": sorted(c["mpns"]),
                "member_count": len(c["member_lines"]),
            }
        )
    payload = {
        "build": BUILD,
        "cluster_count": len(out_clusters),
        "multi_member": sum(1 for c in out_clusters if c["member_count"] > 1),
        "clusters": out_clusters,
    }
    _save(CLUSTERS, payload)
    return payload


def sellers_for_line(line: dict[str, Any]) -> list[str]:
    cat = line.get("category") or "OTHER"
    if cat not in CATEGORY_ROUTES:
        cat = "OTHER"
    return list(CATEGORY_SELLERS.get(cat) or CATEGORY_SELLERS["OTHER"])


def resolve_category_hint(line: dict[str, Any]) -> str:
    cat = line.get("category") or "OTHER"
    mapping = {
        "TOOLS": "tools",
        "PLUMBING": "plumbing",
        "MRO": "mro",
        "LIGHTING": "lighting",
        "PPE": "ppe",
        "OFFICE": "office",
        "FURNITURE": "furniture",
        "HVAC": "hvac",
        "AUTO_HD": "automotive_heavy",
        "ELECTRICAL": "electrical",
        "IRRIGATION": "plumbing",
        "OTHER": "mro",
    }
    return mapping.get(cat, "mro")
