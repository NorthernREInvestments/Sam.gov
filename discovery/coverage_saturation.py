"""L.17.2 coverage saturation — resume-safe enrichment of JurisdictionProcurementRegistry.

Never rebuilds the national inventory from scratch. Only unresolved / partial jurisdictions
are processed. Progress checkpoints continuously.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.county_portal_catalog import PRIORITY_COUNTY_STATES, county_city_portal_catalog
from discovery.jurisdiction_registry import (
    CHECKPOINT_PATH,
    REGISTRY_PATH,
    _match_city,
    _match_county,
    coverage_percentages,
    empty_jurisdiction,
    load_checkpoint,
    platform_jurisdiction_map,
    save_checkpoint,
    status_breakdown,
)
from discovery.lower48 import (
    BUILD_L172,
    FREE_REGISTRATION_REQUIRED,
    LOWER_48_SET,
    PORTAL_DISCOVERED_NOT_INTEGRATED,
    UNKNOWN_RESEARCH_PENDING,
    normalize_jurisdiction_id,
    registration_unlock_score,
)
from discovery.platform_buyer_catalog import platform_buyer_catalog, platform_catalog_by_family

DATA = Path(__file__).resolve().parents[1] / "data"
L172_CHECKPOINT = DATA / "l172_saturation_checkpoint.json"
USER_AGENT = "GovTrackerCoverageBot/1.72 (+local research; respectful)"


def _utc() -> str:
    return now_utc().isoformat()


def load_persisted_registry() -> dict[str, Any]:
    """Load existing registry — NEVER reset national inventory."""
    if not REGISTRY_PATH.exists():
        raise FileNotFoundError(
            f"Missing {REGISTRY_PATH}; run Phase L.17 first to build JurisdictionProcurementRegistry"
        )
    reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if not reg.get("jurisdictions"):
        raise ValueError("Registry has no jurisdictions — refusing to reset")
    return reg


def persist_registry(reg: dict[str, Any]) -> None:
    reg["build_l172"] = BUILD_L172
    reg["generated_at"] = _utc()
    # recount
    juris = reg["jurisdictions"]
    reg["counts"] = {
        "total": len(juris),
        "states": sum(1 for j in juris.values() if j["buyer_type"] == "STATE"),
        "counties": sum(1 for j in juris.values() if j["buyer_type"] == "COUNTY"),
        "municipalities": sum(1 for j in juris.values() if j["buyer_type"] == "CITY"),
        "supplemental": sum(1 for j in juris.values() if j.get("supplemental_buyer")),
        "enriched": sum(
            1
            for j in juris.values()
            if j.get("source_status") not in {UNKNOWN_RESEARCH_PENDING, None, ""}
        ),
    }
    REGISTRY_PATH.write_text(json.dumps(reg, separators=(",", ":"), default=str), encoding="utf-8")


def load_l172_checkpoint() -> dict[str, Any]:
    if L172_CHECKPOINT.exists():
        return json.loads(L172_CHECKPOINT.read_text(encoding="utf-8"))
    return {
        "completed_jurisdiction_ids": [],
        "probe_failures": {},
        "catalog_applied": False,
        "updated_at": None,
    }


def save_l172_checkpoint(cp: dict[str, Any]) -> None:
    cp["updated_at"] = _utc()
    cp["build"] = BUILD_L172
    L172_CHECKPOINT.write_text(json.dumps(cp, indent=2, default=str), encoding="utf-8")


def _apply_entry(reg: dict[str, Any], entry: dict[str, Any], *, cp: dict[str, Any]) -> str | None:
    """Crosswalk catalog entry onto registry. Returns jurisdiction_id if updated."""
    jurisdictions = reg["jurisdictions"]
    st = entry["state"]
    bt = str(entry["buyer_type"]).upper()
    name = entry["name"]
    url = entry.get("portal_url")
    plat = entry.get("platform")
    status = entry.get("source_status") or PORTAL_DISCOVERED_NOT_INTEGRATED
    auth = bool(entry.get("auth_required"))

    target = None
    if bt == "COUNTY":
        target = _match_county(jurisdictions, st, name)
    elif bt == "CITY":
        target = _match_city(jurisdictions, st, name)
    elif bt == "STATE":
        for j in jurisdictions.values():
            if j["state"] == st and j["buyer_type"] == "STATE":
                target = j
                break
    elif bt in {
        "SCHOOL_DISTRICT",
        "PUBLIC_UNIVERSITY",
        "AIRPORT",
        "PUBLIC_UTILITY",
        "TRANSIT",
        "MULTI_AGENCY_NETWORK",
    }:
        sid = normalize_jurisdiction_id(state=st, buyer_type=bt, name=name)
        if sid not in jurisdictions:
            row = empty_jurisdiction(
                state=st, buyer_type=bt, name=name, automation_priority="A"
            )
            row["supplemental_buyer"] = True
            jurisdictions[sid] = row
        target = jurisdictions[sid]

    if not target:
        return None

    # Do not downgrade stronger automation statuses
    existing = str(target.get("source_status") or "")
    stronger = {
        "AUTOMATED_TIER1",
        "AUTOMATED_TIER2",
        "AUTOMATED_STATIC",
    }
    if existing in stronger and status not in stronger:
        # still attach portal / platform metadata
        if url and not target.get("procurement_page"):
            target["procurement_page"] = url
            target["bid_portal"] = url
        if plat and not target.get("procurement_platform"):
            target["procurement_platform"] = plat
        return target["jurisdiction_id"]

    target["procurement_page"] = url or target.get("procurement_page")
    target["bid_portal"] = url or target.get("bid_portal")
    if plat:
        target["procurement_platform"] = plat
    target["source_status"] = status
    target["automation_status"] = status
    target["registration_required"] = auth
    if entry.get("registration_free") is not None:
        target["registration_free"] = entry["registration_free"]
    target["last_discovery_attempt"] = _utc()
    if url:
        target["last_successful_discovery"] = _utc()
    target["coverage_basis"] = entry.get("catalog") or "l172_catalog"
    target["automation_priority"] = target.get("automation_priority") or "A"
    if target["buyer_type"] in {"COUNTY", "CITY"} and target.get("automation_priority") == "C":
        target["automation_priority"] = "B"

    jid = target["jurisdiction_id"]
    done = set(cp.get("completed_jurisdiction_ids") or [])
    done.add(jid)
    cp["completed_jurisdiction_ids"] = sorted(done)
    return jid


def apply_catalogs(reg: dict[str, Any], *, cp: dict[str, Any]) -> dict[str, Any]:
    """Apply platform + county/city catalogs; resume-safe."""
    applied = 0
    skipped = 0
    for entry in list(platform_buyer_catalog()) + list(county_city_portal_catalog()):
        jid = _apply_entry(reg, entry, cp=cp)
        if jid:
            applied += 1
        else:
            skipped += 1
    cp["catalog_applied"] = True
    cp["catalog_applied_count"] = applied
    cp["catalog_unmatched"] = skipped
    save_l172_checkpoint(cp)
    # also merge into L.17 checkpoint
    l17 = load_checkpoint()
    merged = set(l17.get("completed_jurisdiction_ids") or []) | set(
        cp.get("completed_jurisdiction_ids") or []
    )
    l17["completed_jurisdiction_ids"] = sorted(merged)
    l17["enriched_count"] = len(merged)
    l17["l172_build"] = BUILD_L172
    save_checkpoint(l17)
    return {"applied": applied, "unmatched": skipped}


def candidate_county_urls(j: dict[str, Any]) -> list[str]:
    """Heuristic official purchasing URL candidates (verified before status change)."""
    st = (j.get("state") or "").lower()
    raw = re.sub(r"[^a-z0-9]+", "", (j.get("name") or "").lower().replace("county", "").replace("parish", ""))
    hyphen = re.sub(r"[^a-z0-9]+", "-", (j.get("name") or "").lower().replace(" county", "").replace(" parish", "")).strip("-")
    if not raw:
        return []
    urls = [
        f"https://www.{raw}county.gov/purchasing",
        f"https://www.{raw}county.gov/procurement",
        f"https://www.{hyphen}county.gov/purchasing",
        f"https://www.co.{raw}.{st}.us/purchasing",
        f"https://www.{raw}county{st}.gov/purchasing",
        f"https://www.{raw}countytx.gov/Purchasing" if st == "tx" else "",
        f"https://www.{raw}countyga.gov/purchasing" if st == "ga" else "",
        f"https://www.{raw}countyva.gov/purchasing" if st == "va" else "",
    ]
    return [u for u in urls if u]


def _probe_url(url: str, *, timeout: float = 3.5) -> tuple[str, bool, int | None]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            body = resp.read(8000).decode("utf-8", errors="ignore").lower()
            ok = int(code or 0) < 400 and any(
                k in body
                for k in (
                    "purchas",
                    "procure",
                    "bid",
                    "rfp",
                    "rfq",
                    "solicitation",
                    "vendor",
                    "contract",
                )
            )
            return url, ok, int(code) if code else None
    except Exception:
        return url, False, None


def probe_unresolved_counties(
    reg: dict[str, Any],
    *,
    cp: dict[str, Any],
    states: tuple[str, ...] | None = None,
    max_counties: int | None = None,
    workers: int = 16,
) -> dict[str, Any]:
    """Live-probe candidate purchasing pages for unresolved counties. Resume-safe."""
    states = states or PRIORITY_COUNTY_STATES
    done = set(cp.get("completed_jurisdiction_ids") or [])
    failures = dict(cp.get("probe_failures") or {})
    targets: list[dict[str, Any]] = []
    for j in (reg.get("jurisdictions") or {}).values():
        if j.get("buyer_type") != "COUNTY":
            continue
        if j.get("state") not in states:
            continue
        if j.get("source_status") not in {UNKNOWN_RESEARCH_PENDING, None, ""}:
            continue
        if j["jurisdiction_id"] in done:
            continue
        fail = failures.get(j["jurisdiction_id"])
        if isinstance(fail, dict) and int(fail.get("attempts") or 0) >= 2:
            continue
        targets.append(j)
    if max_counties is not None:
        targets = targets[:max_counties]

    found = 0
    probed = 0
    for j in targets:
        urls = candidate_county_urls(j)
        if not urls:
            continue
        hit_url = None
        with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as ex:
            futs = {ex.submit(_probe_url, u): u for u in urls}
            for fut in as_completed(futs):
                url, ok, _code = fut.result()
                probed += 1
                if ok and not hit_url:
                    hit_url = url
        j["last_discovery_attempt"] = _utc()
        if hit_url:
            j["procurement_page"] = hit_url
            j["bid_portal"] = hit_url
            j["source_status"] = PORTAL_DISCOVERED_NOT_INTEGRATED
            j["automation_status"] = PORTAL_DISCOVERED_NOT_INTEGRATED
            j["last_successful_discovery"] = _utc()
            j["coverage_basis"] = "l172_live_probe"
            j["procurement_platform"] = j.get("procurement_platform") or "SimpleHTML"
            done.add(j["jurisdiction_id"])
            failures.pop(j["jurisdiction_id"], None)
            found += 1
        else:
            # Record failure but keep UNKNOWN — allow future resume with new URL patterns
            attempts = int((failures.get(j["jurisdiction_id"]) or {}).get("attempts") or 0) + 1 if isinstance(
                failures.get(j["jurisdiction_id"]), dict
            ) else 1
            failures[j["jurisdiction_id"]] = {"reason": "no_candidate_hit", "attempts": attempts}
            if attempts >= 2:
                done.add(j["jurisdiction_id"])  # stop-loss after 2 failed probe waves
        # checkpoint frequently
        if (found + len(failures)) % 25 == 0:
            cp["completed_jurisdiction_ids"] = sorted(done)
            cp["probe_failures"] = failures
            save_l172_checkpoint(cp)
            persist_registry(reg)

    cp["completed_jurisdiction_ids"] = sorted(done)
    cp["probe_failures"] = failures
    cp["last_probe"] = {"found": found, "probed_urls": probed, "targets": len(targets)}
    save_l172_checkpoint(cp)
    persist_registry(reg)
    return {"targets": len(targets), "found": found, "probed_urls": probed}


def attach_bidnet_network_hints(reg: dict[str, Any]) -> dict[str, Any]:
    """Record statewide BidNet discovery path on unresolved counties WITHOUT claiming membership.

    Does NOT change UNKNOWN → mapped. Sets network_discovery_paths only.
    """
    from discovery.bidnet_network import all_bidnet_networks_enriched

    by_state = {n["state_code"]: n for n in all_bidnet_networks_enriched() if n["state_code"] in LOWER_48_SET}
    hinted = 0
    for j in (reg.get("jurisdictions") or {}).values():
        if j.get("buyer_type") not in {"COUNTY", "CITY"}:
            continue
        net = by_state.get(j.get("state"))
        if not net:
            continue
        paths = list(j.get("network_discovery_paths") or [])
        url = net.get("list_url")
        if url and url not in paths:
            paths.append(url)
            j["network_discovery_paths"] = paths
            j["bidnet_network_hint"] = True
            hinted += 1
    return {"hinted": hinted}


def build_registration_unlocks(reg: dict[str, Any]) -> dict[str, Any]:
    by_plat: dict[str, list[str]] = defaultdict(list)
    for j in (reg.get("jurisdictions") or {}).values():
        if j.get("source_status") != FREE_REGISTRATION_REQUIRED:
            continue
        plat = j.get("procurement_platform") or "Unknown"
        by_plat[str(plat)].append(j["jurisdiction_id"])
    # BidNet statewide networks unlock many buyers via one account
    from discovery.bidnet_network import BIDNET_STATE_NETWORKS

    rows = []
    for plat, ids in sorted(by_plat.items(), key=lambda x: -len(x[1])):
        score = registration_unlock_score(
            buyers_unlocked=len(ids),
            open_opportunities=0,
            product_opportunities=0,
            recurring=plat in {"BidNet", "PublicPurchase", "DemandStar"},
            cost=0.0,
            setup_burden="easy",
        )
        rows.append(
            {
                "platform": plat,
                "buyers_mapped_free_reg": len(ids),
                "sample_ids": ids[:12],
                **score,
                "action": "REGISTER_NOW_RECURRING_BUYER" if len(ids) >= 5 else "REGISTER_BEFORE_BID",
            }
        )
    # Explicit BidNet network leverage (one account → statewide listing)
    for n in BIDNET_STATE_NETWORKS:
        if n["state_code"] not in LOWER_48_SET:
            continue
        counties = sum(
            1
            for j in (reg.get("jurisdictions") or {}).values()
            if j.get("state") == n["state_code"] and j.get("buyer_type") == "COUNTY"
        )
        score = registration_unlock_score(
            buyers_unlocked=max(counties // 4, 5),  # conservative unlock estimate
            recurring=True,
            cost=0.0,
            setup_burden="easy",
        )
        rows.append(
            {
                "platform": "BidNet",
                "network": n["name"],
                "state": n["state_code"],
                "list_url": f"https://www.bidnetdirect.com/{n['slug']}/solicitations/open-bids",
                "counties_in_state": counties,
                "note": "Statewide public open-bids listing; packages often gated; one free account",
                **score,
                "action": "REGISTER_NOW_RECURRING_BUYER",
            }
        )
    rows.sort(key=lambda x: -float(x.get("RegistrationUnlockScore") or 0))
    return {
        "kind": "RegistrationUnlocks",
        "build": BUILD_L172,
        "generated_at": _utc(),
        "top": rows[:40],
    }


def platform_leverage_report(reg: dict[str, Any], *, opportunity_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    pm = platform_jurisdiction_map(reg)
    opp = opportunity_stats or {}
    enriched = []
    for p in pm.get("platforms") or []:
        name = p["platform"]
        o = opp.get(name) or {}
        jurisdictions = int(p["jurisdiction_count"])
        accessible_now = int(o.get("accessible_now") or 0)
        commercial = int(o.get("commercial_stage3") or 0)
        tangible_pct = float(o.get("tangible_pct") or 0)
        reliability = float(o.get("reliability") or 0.7)
        reg_burden = 0.4 if name in {"BidNet", "PublicPurchase", "DemandStar"} else 0.85
        eng = 0.5 if name in {"OpenGov", "Bonfire", "PlanetBids", "IonWave", "Socrata"} else 0.3
        leverage = (jurisdictions * (1 + accessible_now) * (1 + commercial / 10) * reliability * reg_burden) / max(
            0.2, eng * 10
        )
        enriched.append(
            {
                **p,
                "accessible_now_opportunities": accessible_now,
                "commercial_stage3": commercial,
                "tangible_product_pct": tangible_pct,
                "JurisdictionsUnlockedPerAdapter": jurisdictions,
                "PlatformLeverageScore": round(leverage, 2),
            }
        )
    enriched.sort(key=lambda x: -float(x["PlatformLeverageScore"]))
    return {
        "kind": "PlatformLeverage",
        "build": BUILD_L172,
        "generated_at": _utc(),
        "platforms": enriched,
        "catalog_families": {k: len(v) for k, v in platform_catalog_by_family().items()},
    }


def unknown_reduction_stats(before: dict[str, int], after: dict[str, int], reg: dict[str, Any]) -> dict[str, Any]:
    pct = coverage_percentages(reg)
    return {
        "kind": "UnknownReduction",
        "build": BUILD_L172,
        "generated_at": _utc(),
        "unknown_before": before.get(UNKNOWN_RESEARCH_PENDING, 0),
        "unknown_after": after.get(UNKNOWN_RESEARCH_PENDING, 0),
        "delta_unknown": before.get(UNKNOWN_RESEARCH_PENDING, 0) - after.get(UNKNOWN_RESEARCH_PENDING, 0),
        "status_before": before,
        "status_after": after,
        "coverage_percentages": pct,
        "counties_mapped": sum(
            1
            for j in reg["jurisdictions"].values()
            if j["buyer_type"] == "COUNTY"
            and j.get("source_status") not in {UNKNOWN_RESEARCH_PENDING, None, ""}
        ),
        "municipalities_mapped": sum(
            1
            for j in reg["jurisdictions"].values()
            if j["buyer_type"] == "CITY"
            and not j.get("supplemental_buyer")
            and j.get("source_status") not in {UNKNOWN_RESEARCH_PENDING, None, ""}
        ),
    }


def run_saturation_pass(
    *,
    probe: bool = True,
    probe_max_counties: int | None = 400,
    probe_states: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Main L.17.2 enrichment pass — resume from checkpoints, no registry reset."""
    reg = load_persisted_registry()
    before = status_breakdown(reg)
    cp = load_l172_checkpoint()
    catalog_stats = apply_catalogs(reg, cp=cp)
    hints = attach_bidnet_network_hints(reg)
    probe_stats: dict[str, Any] = {"skipped": True}
    if probe:
        # Priority states first
        probe_stats = probe_unresolved_counties(
            reg,
            cp=cp,
            states=probe_states or PRIORITY_COUNTY_STATES,
            max_counties=probe_max_counties,
        )
        # Then remaining Lower-48 counties (additional batch)
        remaining_states = tuple(
            s for s in sorted(LOWER_48_SET) if s not in set(probe_states or PRIORITY_COUNTY_STATES)
        )
        more = probe_unresolved_counties(
            reg,
            cp=cp,
            states=remaining_states,
            max_counties=(probe_max_counties or 400) // 2,
        )
        probe_stats = {
            "priority": probe_stats,
            "nationwide_batch": more,
            "found_total": int(probe_stats.get("found") or 0) + int(more.get("found") or 0),
        }
    persist_registry(reg)
    after = status_breakdown(reg)
    reduction = unknown_reduction_stats(before, after, reg)
    return {
        "registry": reg,
        "catalog_stats": catalog_stats,
        "bidnet_hints": hints,
        "probe_stats": probe_stats,
        "reduction": reduction,
        "checkpoint_path": str(L172_CHECKPOINT),
        "registry_path": str(REGISTRY_PATH),
        "l17_checkpoint_path": str(CHECKPOINT_PATH),
    }
