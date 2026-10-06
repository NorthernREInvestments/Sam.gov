"""Platform family + state coverage matrices from registry and last discovery run."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from discovery_expansion.constants import (
    ADAPTER_AUTH_REQUIRED,
    ADAPTER_BROKEN,
    ADAPTER_NOT_IMPLEMENTED,
    ADAPTER_PARTIAL,
    ADAPTER_WORKING,
    PRIORITY_FAMILIES,
    STATE_AUTH_REQUIRED,
    STATE_BROKEN,
    STATE_LIVE,
    STATE_NO_SOURCE,
    STATE_PARTIAL,
    STATE_RESEARCH_NEEDED,
)
from discovery_expansion.detect import adapter_status_from_health


def _repo() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _last_per_source() -> dict[str, Any]:
    merged: dict[str, Any] = {}
    # Prefer expansion harvest (highest volume free sources)
    try:
        from m3_data_root import data_path

        exp = _load_json(data_path("m3_discovery_expansion_last_harvest.json"))
        if isinstance(exp, dict) and isinstance(exp.get("per_source"), dict):
            merged.update(exp["per_source"])
    except Exception:
        pass
    for rel in (
        "artifacts/m3_discovery_run_state.json",
        "data/m3_discovery_run_state.json",
    ):
        data = _load_json(_repo() / rel)
        if not data:
            continue
        exp_block = data.get("last_expansion_harvest") or {}
        if isinstance(exp_block.get("per_source_summary"), dict):
            for k, v in exp_block["per_source_summary"].items():
                merged.setdefault(k, v)
        last = data.get("last_successful_completion") or data.get("last_attempt") or {}
        per = last.get("per_source_summary") or {}
        if isinstance(per, dict):
            for k, v in per.items():
                merged.setdefault(k, v)
    return merged


def _family_from_source_id(sid: str, row: dict[str, Any] | None = None) -> str:
    s = sid.lower()
    if "bidnet" in s:
        return "BidNet"
    if "opengov" in s:
        return "OpenGov"
    if "bonfire" in s:
        return "Bonfire"
    if "planetbids" in s:
        return "PlanetBids"
    if "ionwave" in s:
        return "IonWave"
    if "demandstar" in s:
        return "DemandStar"
    if "publicpurchase" in s or "public_purchase" in s:
        return "PublicPurchase"
    if "jaggaer" in s or "sciquest" in s:
        return "Jaggaer"
    if "socrata" in s or "structured_" in s or "ckan" in s or "arcgis" in s:
        return "Socrata"
    if "dibbs" in s or "dla" in s:
        return "DIBBS"
    if s.startswith("fed_") or "sam" in s:
        return "SAM"
    if "coop" in s or "sourcewell" in s or "naspo" in s:
        return "Cooperative"
    if s.startswith("state_"):
        return "StateHosted"
    if "periscope" in s or "bidsync" in s:
        return "Periscope"
    return "Other"


def platform_family_status_matrix() -> dict[str, Any]:
    per = _last_per_source()
    reg = _load_json(_repo() / "artifacts" / "procurement_source_registry.json") or {}
    sources = reg.get("sources") if isinstance(reg, dict) else None
    if not isinstance(sources, list):
        sources = []

    entities_by_family: dict[str, set[str]] = defaultdict(set)
    for src in sources:
        if not isinstance(src, dict):
            continue
        fam = str(src.get("platform_family") or src.get("platform") or "")
        if not fam or fam.upper() in {"UNKNOWN", "NONE", ""}:
            fam = _family_from_source_id(str(src.get("source_id") or ""), src)
        entities_by_family[fam].add(str(src.get("source_id") or src.get("list_url") or id(src)))

    # Periscope: known but not implemented
    if "Periscope" not in entities_by_family:
        entities_by_family["Periscope"] = set()

    rows = []
    for fam in list(PRIORITY_FAMILIES) + sorted(
        f for f in entities_by_family if f not in PRIORITY_FAMILIES
    ):
        related = {sid: row for sid, row in per.items() if _family_from_source_id(sid) == fam}
        live_opps = sum(int((r or {}).get("raw") or (r or {}).get("unique") or 0) for r in related.values() if (r or {}).get("ok"))
        ok_n = sum(1 for r in related.values() if (r or {}).get("ok"))
        fail_n = sum(1 for r in related.values() if r and not r.get("ok"))
        statuses = [adapter_status_from_health(r or {}) for r in related.values()] if related else []
        if fam == "Periscope":
            status = ADAPTER_NOT_IMPLEMENTED
        elif not related and not entities_by_family.get(fam):
            status = ADAPTER_NOT_IMPLEMENTED
        elif any(s == ADAPTER_WORKING for s in statuses):
            status = ADAPTER_WORKING if fail_n == 0 or ok_n >= fail_n else ADAPTER_PARTIAL
        elif any(s == ADAPTER_AUTH_REQUIRED for s in statuses):
            status = ADAPTER_AUTH_REQUIRED
        elif any(s == ADAPTER_BROKEN for s in statuses):
            status = ADAPTER_BROKEN
        elif related:
            status = ADAPTER_PARTIAL
        else:
            status = ADAPTER_PARTIAL if entities_by_family.get(fam) else ADAPTER_NOT_IMPLEMENTED

        known_entities = len(entities_by_family.get(fam) or set()) or len(related)
        observed_per = (live_opps / max(ok_n, 1)) if ok_n else 0.0
        access_p = 0.9 if status == ADAPTER_WORKING else (
            0.55 if status == ADAPTER_PARTIAL else (
                0.35 if status == ADAPTER_AUTH_REQUIRED else (
                    0.15 if status == ADAPTER_BROKEN else 0.05
                )
            )
        )
        # Prefer registry entity counts for expected gain when larger
        entity_n = max(known_entities, len(related))
        expected_gain = round(entity_n * max(observed_per, 5.0) * access_p, 1)

        rows.append(
            {
                "platform": fam,
                "entities": entity_n,
                "live_opps": live_opps,
                "sources_ok": ok_n,
                "sources_failed": fail_n,
                "status": status,
                "expected_coverage_gain": expected_gain,
                "blocker": (
                    None
                    if status == ADAPTER_WORKING
                    else (
                        "No live fetcher implemented"
                        if status == ADAPTER_NOT_IMPLEMENTED
                        else (
                            "Login/captcha required"
                            if status == ADAPTER_AUTH_REQUIRED
                            else (
                                "Parser/schema failure"
                                if status == ADAPTER_BROKEN
                                else "Partial / zero yield"
                            )
                        )
                    )
                ),
            }
        )

    rows.sort(key=lambda r: -float(r.get("expected_coverage_gain") or 0))
    return {
        "kind": "PlatformFamilyStatusMatrix",
        "platforms": rows,
        "priority_build_order": [r["platform"] for r in rows if r["status"] != ADAPTER_WORKING][:10],
    }


def state_coverage_matrix() -> dict[str, Any]:
    """50-state coverage from BidNet networks + state_* sources."""
    per = _last_per_source()
    from discovery.bidnet_network import BIDNET_STATE_NETWORKS

    states = []
    for net in BIDNET_STATE_NETWORKS:
        code = net["state_code"]
        sid = f"network_bidnet_{net['slug'].replace('-', '_')}"
        row = per.get(sid) or {}
        state_sid = f"state_{code.lower()}"
        state_row = per.get(state_sid) or {}
        live = int(row.get("raw") or 0) if row.get("ok") else 0
        live += int(state_row.get("raw") or 0) if state_row.get("ok") else 0
        if row.get("ok") or state_row.get("ok"):
            status = STATE_LIVE if live > 0 else STATE_PARTIAL
        elif "AUTH" in str((row or state_row).get("source_stop_reason") or (row or state_row).get("root_cause") or "").upper():
            status = STATE_AUTH_REQUIRED
        elif row or state_row:
            status = STATE_BROKEN
        else:
            status = STATE_RESEARCH_NEEDED
        states.append(
            {
                "state": code,
                "name": net["name"],
                "platform_family": "BidNet+StateHosted",
                "public_search": True,
                "adapter_exists": True,
                "adapter_works": bool(row.get("ok") or state_row.get("ok")),
                "live_opportunities": live,
                "status": status,
                "bidnet_source_id": sid,
                "state_source_id": state_sid if state_row else None,
            }
        )
    live_n = sum(1 for s in states if s["status"] == STATE_LIVE)
    return {
        "kind": "StateCoverageMatrix",
        "states": states,
        "summary": {
            "live": live_n,
            "partial": sum(1 for s in states if s["status"] == STATE_PARTIAL),
            "broken": sum(1 for s in states if s["status"] == STATE_BROKEN),
            "auth_required": sum(1 for s in states if s["status"] == STATE_AUTH_REQUIRED),
            "research_needed": sum(1 for s in states if s["status"] == STATE_RESEARCH_NEEDED),
            "no_source": sum(1 for s in states if s["status"] == STATE_NO_SOURCE),
            "total_live_opportunities": sum(s["live_opportunities"] for s in states),
        },
    }
