"""Buyer identity aliases + source memory.

Build: 20261004-m3-revenue-evidence-v1
"""

from __future__ import annotations

import json
import re
from typing import Any

from m3_data_root import data_path

BUILD = "20261004-m3-revenue-evidence-v1"

# Known aliases — code → display / alternate names
_BUYER_ALIASES: dict[str, list[str]] = {
    "go-metro": [
        "SORTA",
        "Southwest Ohio Regional Transit Authority",
        "Metro",
        "go-metro",
        "Go Metro",
        "Cincinnati Metro",
    ],
    "acgov": ["Alameda County", "County of Alameda", "ACGOV", "Alameda County GSA"],
    "anaheim": ["City of Anaheim", "Anaheim", "Anaheim Public Works"],
    "collier-county-fl": ["Collier County", "Collier County Florida", "Collier County BOCC"],
    "bridgeportct": ["City of Bridgeport", "Bridgeport CT", "Bridgeport"],
    "districtgov": ["District of Columbia", "DC Government", "Washington DC"],
    "cambridgema": ["City of Cambridge", "Cambridge MA"],
}


def buyer_code_from_oid(opportunity_id: str) -> str | None:
    parts = str(opportunity_id or "").split(":")
    if len(parts) >= 2 and parts[0].lower() == "opengov":
        return parts[1].strip().lower() or None
    return None


def project_id_from_oid(opportunity_id: str) -> str | None:
    parts = str(opportunity_id or "").split(":")
    if len(parts) >= 3 and parts[0].lower() == "opengov":
        return parts[-1].strip() or None
    return None


def aliases_for(code: str | None) -> list[str]:
    if not code:
        return []
    c = code.strip().lower()
    out = list(_BUYER_ALIASES.get(c) or [])
    # Always include code and spaced form
    out.append(c)
    out.append(c.replace("-", " "))
    # Dedupe preserve order
    seen: set[str] = set()
    uniq = []
    for a in out:
        k = a.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(a)
    return uniq


def _memory_path() -> Any:
    return data_path("m3_buyer_source_memory.json")


def load_buyer_source_memory() -> dict[str, Any]:
    p = _memory_path()
    if not p.exists():
        return {"kind": "BuyerSourceMemory", "build": BUILD, "by_buyer": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "BuyerSourceMemory", "build": BUILD, "by_buyer": {}}


def save_buyer_source_memory(mem: dict[str, Any]) -> None:
    p = _memory_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    mem["build"] = BUILD
    p.write_text(json.dumps(mem, indent=2, default=str), encoding="utf-8")


def remember_buyer_sources(code: str, sources: dict[str, Any]) -> dict[str, Any]:
    mem = load_buyer_source_memory()
    by = mem.setdefault("by_buyer", {})
    row = by.setdefault(code, {"aliases": aliases_for(code), "sources": {}})
    row["aliases"] = aliases_for(code)
    src = row.setdefault("sources", {})
    for k, v in (sources or {}).items():
        if v:
            src[k] = v
    save_buyer_source_memory(mem)
    return row


def normalize_title(title: str | None) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    t = re.sub(r"\b(20\d{2}|fy\s*20\d{2}|fy\d{2})\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_without_buyer_prefix(title: str | None, code: str | None) -> str:
    t = normalize_title(title)
    for a in aliases_for(code):
        pref = a.lower().strip()
        if t.startswith(pref):
            t = t[len(pref) :].lstrip(" -:|")
    return t
