"""Identity confidence A–G mapping + commercial validation + clusters."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from material_line_identity_price_recovery.models import (
    A_EXACT_MPN,
    B_EXACT_MODEL,
    BUILD,
    C_NSN_TO_EXACT,
    CLUSTERS,
    D_PERMITTED_EQUAL,
    E_STRONG_GENERIC,
    F_FAMILY_ONLY,
    G_AMBIGUOUS,
    USABLE_IDENTITY,
)
from m3_data_root import data_path
from product_identity.models import (
    EXACT_CATALOG_NUMBER,
    EXACT_MODEL,
    EXACT_MPN,
    EXACT_NSN,
    EXACT_UPC,
    NO_IDENTITY,
    PERMITTED_EQUAL,
    STRONG_GENERIC_SPEC,
    WEAK_IDENTITY,
)

_MAP = {
    EXACT_MPN: A_EXACT_MPN,
    EXACT_CATALOG_NUMBER: A_EXACT_MPN,
    EXACT_UPC: A_EXACT_MPN,
    EXACT_MODEL: B_EXACT_MODEL,
    EXACT_NSN: C_NSN_TO_EXACT,
    PERMITTED_EQUAL: D_PERMITTED_EQUAL,
    STRONG_GENERIC_SPEC: E_STRONG_GENERIC,
    WEAK_IDENTITY: F_FAMILY_ONLY,
    NO_IDENTITY: G_AMBIGUOUS,
}


def map_identity_confidence(identity_type: str | None, *, source: dict[str, Any] | None = None) -> str:
    src = source or {}
    if src.get("nsn") and not src.get("part_number") and not src.get("model"):
        # NSN present but not yet resolved to commercial MPN — still C if NSN exact
        return C_NSN_TO_EXACT
    return _MAP.get(str(identity_type or NO_IDENTITY), G_AMBIGUOUS)


def is_usable(confidence: str) -> bool:
    return confidence in USABLE_IDENTITY


def validate_commercial_identity(line: dict[str, Any]) -> dict[str, Any]:
    """Lightweight validation — search snippets alone are not enough."""
    conf = line.get("identity_confidence")
    if not is_usable(conf):
        return {"validated": False, "reason": "identity_not_usable"}

    # Require at least one of: source_document, manufacturer+token, NSN, strong generic with cues
    has_doc = bool(line.get("source_document"))
    has_mfr_token = bool(line.get("manufacturer") and (line.get("mpn") or line.get("model") or line.get("part_number")))
    has_nsn = bool(line.get("nsn"))
    has_generic = conf == E_STRONG_GENERIC and len(str(line.get("description") or "")) >= 24

    if has_doc or has_mfr_token or has_nsn or has_generic:
        return {
            "validated": True,
            "reason": "source_or_commercial_token",
            "via": "manufacturer_page_or_package" if has_doc else "token_evidence",
        }
    return {"validated": False, "reason": "no_credible_source"}


def cluster_key(line: dict[str, Any]) -> str | None:
    mpn = re.sub(r"[^A-Z0-9]+", "", str(line.get("mpn") or line.get("part_number") or "").upper())
    mfr = re.sub(r"[^A-Z0-9]+", "", str(line.get("manufacturer") or "").upper())
    nsn = re.sub(r"[^0-9]+", "", str(line.get("nsn") or ""))
    model = re.sub(r"[^A-Z0-9]+", "", str(line.get("model") or "").upper())
    if nsn and len(nsn) >= 13:
        return f"NSN:{nsn}"
    if mpn and len(mpn) >= 4:
        return f"MPN:{mfr}:{mpn}" if mfr else f"MPN:{mpn}"
    if model and len(model) >= 4 and mfr:
        return f"MODEL:{mfr}:{model}"
    if line.get("identity_confidence") == D_PERMITTED_EQUAL:
        desc = re.sub(r"[^A-Z0-9]+", "", str(line.get("description") or "")[:60].upper())
        if desc:
            return f"EQUAL:{desc}"
    return None


def build_identity_clusters(lines: list[dict[str, Any]]) -> dict[str, Any]:
    clusters: dict[str, dict[str, Any]] = {}
    reuse = 0
    for ln in lines:
        key = cluster_key(ln)
        if not key:
            continue
        cid = "MIC-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        bucket = clusters.setdefault(
            cid,
            {
                "cluster_id": cid,
                "shared_identity": key,
                "member_lines": [],
                "shared_pricing_evidence": None,
                "mpn": ln.get("mpn") or ln.get("part_number"),
                "manufacturer": ln.get("manufacturer"),
                "nsn": ln.get("nsn"),
            },
        )
        bucket["member_lines"].append(
            {
                "opportunity_id": ln.get("opportunity_id"),
                "line_id": ln.get("line_id"),
                "quantity": ln.get("quantity"),
                "uom": ln.get("uom"),
            }
        )
        ln["cluster_id"] = cid
        if ln.get("production_price") and not bucket["shared_pricing_evidence"]:
            bucket["shared_pricing_evidence"] = ln.get("production_price")
        elif bucket["shared_pricing_evidence"] and not ln.get("production_price"):
            # will propagate later
            reuse += 1

    # Propagate shared prices
    by_cid = {c["cluster_id"]: c for c in clusters.values()}
    prop = 0
    for ln in lines:
        cid = ln.get("cluster_id")
        if not cid:
            continue
        shared = (by_cid.get(cid) or {}).get("shared_pricing_evidence")
        if shared and not ln.get("production_price") and ln.get("identity_confidence") in USABLE_IDENTITY:
            ln["production_price"] = dict(shared)
            ln["price_propagated_from_cluster"] = True
            prop += 1

    payload = {
        "build": BUILD,
        "cluster_count": len(clusters),
        "unique_commercial_identities": len(clusters),
        "repeated_lines_collapsed": sum(1 for c in clusters.values() if len(c["member_lines"]) > 1),
        "research_reuse_count": prop,
        "clusters": [
            {**c, "member_count": len(c["member_lines"])}
            for c in clusters.values()
        ],
    }
    data_path(CLUSTERS).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload
