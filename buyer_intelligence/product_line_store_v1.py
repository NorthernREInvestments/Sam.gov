"""Canonical product-line persistence — first-class structured records.

Survives HT extraction → storage → reload → commercial research → UI.
Does NOT mutate completed HT v2 evidence snapshots.
Uses atomic writes + richer-evidence merge on upsert.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

log = logging.getLogger("govtracker.product_line_store_v1")

BUILD = "20261012-m3-product-line-store-v1"
CONTRACT = "product_line_v1"
STORE_REL = "_og_commercial_research_pipeline_out/m3_canonical_product_lines_v1.json"
INDEX_REL = "_og_commercial_research_pipeline_out/m3_canonical_product_lines_v1_index.json"

_lock = threading.RLock()
_cache: dict[str, Any] | None = None

_RICH_FIELDS = (
    "MANUFACTURER",
    "BRAND",
    "MODEL",
    "MPN",
    "NSN",
    "UPC_GTIN",
    "QTY",
    "UOM",
    "PACKAGE_SIZE",
    "TECHNICAL_SPECS",
    "SOURCE_DOCUMENT",
    "SOURCE_LOCATION",
    "PROVENANCE",
    "IDENTITY_CLASS",
)


def _utc() -> str:
    return now_utc().isoformat()


def store_path() -> Path:
    return data_path(STORE_REL)


def _empty_store() -> dict[str, Any]:
    return {
        "kind": "canonical_product_lines_v1",
        "build": BUILD,
        "contract": CONTRACT,
        "updated_at": _utc(),
        "by_opportunity": {},
        "opportunity_count": 0,
        "line_count": 0,
        "NOTE": "Canonical product-line payloads — HT v2 result snapshots remain count-only",
    }


def _line_richness(ln: dict[str, Any]) -> int:
    score = 0
    for k in _RICH_FIELDS:
        v = ln.get(k)
        if v is None or v == "" or v == {} or v == []:
            continue
        score += 2 if k in {"MPN", "NSN", "MANUFACTURER", "QTY", "UOM"} else 1
    desc = str(ln.get("DESCRIPTION") or "")
    score += min(5, len(desc) // 40)
    return score


def _line_key(ln: dict[str, Any], idx: int) -> str:
    num = str(ln.get("LINE_NUMBER") or ln.get("line_number") or "").strip()
    if num:
        return f"LN:{num}"
    desc = re.sub(r"\s+", " ", str(ln.get("DESCRIPTION") or ln.get("description") or "").strip().lower())
    mpn = str(ln.get("MPN") or ln.get("PART_NUMBER") or "").strip().upper()
    h = hashlib.sha1(f"{desc}|{mpn}".encode("utf-8")).hexdigest()[:12]
    return f"H:{h}" if desc or mpn else f"IDX:{idx}"


def _merge_line(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Prefer richer evidence; never overwrite a verified value with empty."""
    if _line_richness(incoming) > _line_richness(existing):
        out = deepcopy(incoming)
        for k, v in existing.items():
            if out.get(k) in (None, "", {}, []) and v not in (None, "", {}, []):
                out[k] = v
        return out
    out = deepcopy(existing)
    for k, v in incoming.items():
        if out.get(k) in (None, "", {}, []) and v not in (None, "", {}, []):
            out[k] = v
    return out


def _normalize_incoming_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from buyer_intelligence.source_to_product_extraction_audit_v1 import normalize_enrichment_line

    out: list[dict[str, Any]] = []
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            continue
        if ln.get("CONTRACT_VERSION") == CONTRACT and ln.get("DESCRIPTION"):
            out.append(dict(ln))
            continue
        try:
            out.append(
                normalize_enrichment_line(
                    ln,
                    provenance=str(ln.get("PROVENANCE") or ln.get("SOURCE") or "HT_OR_RECOVERY"),
                )
            )
        except Exception:
            desc = str(ln.get("DESCRIPTION") or ln.get("description") or "").strip()
            if not desc:
                continue
            out.append(
                {
                    "LINE_NUMBER": str(ln.get("LINE_NUMBER") or ln.get("line_number") or i + 1),
                    "DESCRIPTION": desc,
                    "QTY": ln.get("QTY") if ln.get("QTY") is not None else ln.get("qty"),
                    "UOM": ln.get("UOM") or ln.get("uom"),
                    "MPN": ln.get("MPN") or ln.get("PART_NUMBER"),
                    "MANUFACTURER": ln.get("MANUFACTURER") or ln.get("manufacturer"),
                    "PROVENANCE": ln.get("PROVENANCE") or "RAW",
                    "CONTRACT_VERSION": CONTRACT,
                    "MANDATORY": True,
                }
            )
    return [ln for ln in out if ln.get("DESCRIPTION")]


def load_store(*, force: bool = False) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        path = store_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("by_opportunity"), dict):
                    _cache = data
                    return _cache
            except Exception as exc:
                log.warning("product_line_store load failed: %s", exc)
        _cache = _empty_store()
        return _cache


def save_store(store: dict[str, Any] | None = None) -> Path:
    with _lock:
        data = store if store is not None else load_store()
        by = data.get("by_opportunity") or {}
        data["opportunity_count"] = len(by)
        data["line_count"] = sum(int(v.get("LINE_COUNT") or len(v.get("LINES") or [])) for v in by.values())
        data["updated_at"] = _utc()
        data["build"] = BUILD
        path = store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
        # Slim index for UI / commercial research
        idx = {
            "kind": "canonical_product_lines_index_v1",
            "build": BUILD,
            "updated_at": data["updated_at"],
            "opportunity_count": data["opportunity_count"],
            "line_count": data["line_count"],
            "ids": sorted(by.keys()),
            "summary": {
                oid: {
                    "LINE_COUNT": e.get("LINE_COUNT"),
                    "IDENTITY_BEST": e.get("IDENTITY_BEST"),
                    "SOURCE": e.get("SOURCE"),
                    "RECOVERY_STATUS": e.get("RECOVERY_STATUS"),
                }
                for oid, e in by.items()
            },
        }
        ip = data_path(INDEX_REL)
        itmp = ip.with_suffix(ip.suffix + ".tmp")
        itmp.write_text(json.dumps(idx, indent=2, default=str), encoding="utf-8")
        itmp.replace(ip)
        global _cache
        _cache = data
        return path


def get_lines(opportunity_id: str) -> list[dict[str, Any]]:
    store = load_store()
    entry = (store.get("by_opportunity") or {}).get(str(opportunity_id)) or {}
    return list(entry.get("LINES") or [])


def get_entry(opportunity_id: str) -> dict[str, Any] | None:
    store = load_store()
    return (store.get("by_opportunity") or {}).get(str(opportunity_id))


def upsert_opportunity_lines(
    opportunity_id: str,
    lines: list[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Safe upsert: merge by line key, prefer richer evidence, idempotent."""
    oid = str(opportunity_id)
    incoming = _normalize_incoming_lines(lines)
    meta = meta or {}
    with _lock:
        store = load_store()
        by = store.setdefault("by_opportunity", {})
        existing = by.get(oid) or {
            "OPPORTUNITY_ID": oid,
            "LINES": [],
            "LINE_COUNT": 0,
            "VERSIONS": [],
        }
        merged_map: dict[str, dict[str, Any]] = {}
        for i, ln in enumerate(existing.get("LINES") or []):
            if isinstance(ln, dict):
                merged_map[_line_key(ln, i)] = dict(ln)
        for i, ln in enumerate(incoming):
            key = _line_key(ln, i)
            if key in merged_map:
                merged_map[key] = _merge_line(merged_map[key], ln)
            else:
                merged_map[key] = dict(ln)
        merged = list(merged_map.values())
        # Best identity
        order = [
            "EXACT_PRODUCT_VERIFIED",
            "SPECIFICATION_COMPLETE_GENERIC",
            "PARTIAL_PRODUCT_IDENTITY",
            "CATEGORY_ONLY",
            "IDENTITY_CONFLICT",
            "IDENTITY_UNKNOWN",
        ]
        best = "IDENTITY_UNKNOWN"
        for ln in merged:
            cls = str(ln.get("IDENTITY_CLASS") or "IDENTITY_UNKNOWN")
            if cls in order and order.index(cls) < order.index(best):
                best = cls
        entry = {
            **existing,
            "OPPORTUNITY_ID": oid,
            "TITLE": meta.get("TITLE") or existing.get("TITLE"),
            "BUYER": meta.get("BUYER") or existing.get("BUYER"),
            "SOURCE": meta.get("SOURCE") or existing.get("SOURCE"),
            "DEADLINE": meta.get("DEADLINE") or existing.get("DEADLINE"),
            "SOURCE_URL": meta.get("SOURCE_URL") or existing.get("SOURCE_URL"),
            "LINES": merged[:500],
            "LINE_COUNT": len(merged),
            "IDENTITY_BEST": best,
            "RECOVERY_STATUS": meta.get("RECOVERY_STATUS") or existing.get("RECOVERY_STATUS"),
            "UPDATED_AT": _utc(),
            "CONTRACT": CONTRACT,
            "PROVENANCE_SOURCES": sorted(
                set(list(existing.get("PROVENANCE_SOURCES") or []) + list(meta.get("PROVENANCE_SOURCES") or []))
            ),
        }
        if meta.get("NOTE"):
            entry["NOTE"] = meta["NOTE"]
        # Version history (bounded)
        versions = list(existing.get("VERSIONS") or [])
        versions.append(
            {
                "at": _utc(),
                "line_count": len(merged),
                "identity_best": best,
                "source": (meta.get("PROVENANCE_SOURCES") or ["upsert"])[0],
            }
        )
        entry["VERSIONS"] = versions[-20:]
        by[oid] = entry
        if persist:
            save_store(store)
        return entry


def migrate_from_sidecar(sidecar_path: str | Path | None = None) -> dict[str, Any]:
    """Import existing audit sidecar into canonical store without losing richer data."""
    path = Path(sidecar_path) if sidecar_path else data_path(
        "_og_commercial_research_pipeline_out/m3_product_lines_contract_v1.json"
    )
    if not path.exists():
        return {"migrated": 0, "note": "sidecar missing"}
    blob = json.loads(path.read_text(encoding="utf-8"))
    by = blob.get("by_opportunity") or {}
    n = 0
    lines_n = 0
    for oid, entry in by.items():
        lines = entry.get("LINES") or []
        if not lines:
            continue
        upsert_opportunity_lines(
            oid,
            lines,
            meta={
                "TITLE": entry.get("TITLE"),
                "BUYER": entry.get("BUYER"),
                "SOURCE": entry.get("SOURCE"),
                "PROVENANCE_SOURCES": ["sidecar_v1"],
                "RECOVERY_STATUS": "PARTIAL_PRODUCT_REQUIREMENTS_RECOVERED"
                if lines
                else None,
            },
            persist=False,
        )
        n += 1
        lines_n += len(lines)
    save_store()
    return {"migrated": n, "lines": lines_n, "path": str(path)}


def persist_ht_lines(
    opportunity_id: str,
    lines: list[dict[str, Any]],
    *,
    rec: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Called from HT v2 after extraction — never raises into HT pipeline."""
    if not lines:
        return None
    try:
        rec = rec or {}
        return upsert_opportunity_lines(
            opportunity_id,
            lines,
            meta={
                "TITLE": rec.get("title") or rec.get("TITLE"),
                "BUYER": rec.get("buyer") or rec.get("BUYER"),
                "SOURCE": rec.get("source") or rec.get("SOURCE"),
                "DEADLINE": rec.get("deadline") or rec.get("DEADLINE"),
                "SOURCE_URL": rec.get("authoritative_url") or rec.get("solicitation_url"),
                "PROVENANCE_SOURCES": ["ht_v2_extraction"],
                "RECOVERY_STATUS": "PARTIAL_PRODUCT_REQUIREMENTS_RECOVERED",
            },
            persist=True,
        )
    except Exception as exc:
        log.warning("persist_ht_lines failed oid=%s: %s", opportunity_id, type(exc).__name__)
        return None
