"""OpenGovAgencyMirrorRegistry — recover official agency solicitation pages for CDN-blocked buyers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.county_portal_catalog import county_city_portal_catalog
from discovery.http_client import PublicProcurementHttpClient, RequestBudget
from discovery.jurisdiction_registry import REGISTRY_PATH
from discovery.live_fetchers import OpenGovLiveFetcher, SimpleHtmlLiveFetcher
from discovery.platform_adapters import (
    ANTI_BOT,
    INGESTION_ACTIVE,
    OPENGOV_AGENCY_MIRRORS,
    PUBLIC_LISTING_AUTOMATABLE,
    crosswalk_confidence,
    extract_portal_slug,
    _bid_board_child_urls,
    _is_cloudflare,
)
from discovery.platform_buyer_catalog import platform_buyer_catalog

BUILD = "20260928-m3-phase-l18-research-queue-conversion-opengov-mirrors"
OPENGOV_CDN_ANTI_BOT = "OPENGOV_CDN_ANTI_BOT"
DATA = Path(__file__).resolve().parents[1] / "data"
MIRROR_REGISTRY_PATH = DATA / "opengov_agency_mirror_registry.json"


def _utc() -> str:
    return now_utc().isoformat()


def load_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _agency_url(j: dict[str, Any]) -> str | None:
    for key in ("opengov_mirror", "bid_portal", "procurement_page"):
        url = str(j.get(key) or "")
        if url and "opengov.com" not in url.lower():
            return url
    return None


def blocked_opengov_jurisdictions(reg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """OpenGov buyers still needing agency-mirror recovery or reactivation."""
    reg = reg or load_registry()
    out = []
    for j in (reg.get("jurisdictions") or {}).values():
        if j.get("procurement_platform") != "OpenGov":
            continue
        jid = j.get("jurisdiction_id")
        url = str(j.get("bid_portal") or j.get("procurement_page") or "")
        st = j.get("platform_access_state")
        act = j.get("source_activation")
        on_cdn = "opengov.com" in url.lower()
        agency = _agency_url(j) or OPENGOV_AGENCY_MIRRORS.get(jid or "")
        # Skip already-active agency mirrors (still re-harvested via known pass)
        if act == INGESTION_ACTIVE and agency and not on_cdn and st == PUBLIC_LISTING_AUTOMATABLE:
            if jid in OPENGOV_AGENCY_MIRRORS:
                continue
            continue
        # CDN-walled or anti-bot central path
        if on_cdn or st == ANTI_BOT:
            out.append(j)
            continue
        # Agency URL mapped but not producing inventory
        if agency and act != INGESTION_ACTIVE:
            out.append(j)
    return out


def _catalog_agency_candidates() -> list[dict[str, Any]]:
    """Agency-side URLs from catalogs (never Cloudflare CDN)."""
    cands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in list(platform_buyer_catalog()) + list(county_city_portal_catalog()):
        url = str(e.get("portal_url") or "")
        if not url or "opengov.com" in url.lower():
            continue
        key = f"{e.get('state')}|{e.get('buyer_type')}|{url}"
        if key in seen:
            continue
        seen.add(key)
        cands.append(e)
    return cands


def propose_mirrors_for_jurisdiction(j: dict[str, Any]) -> list[dict[str, Any]]:
    """Propose agency mirror URLs with crosswalk confidence (no Cloudflare bypass)."""
    proposals: list[dict[str, Any]] = []
    jid = j["jurisdiction_id"]
    if jid in OPENGOV_AGENCY_MIRRORS:
        proposals.append(
            {
                "mirror_url": OPENGOV_AGENCY_MIRRORS[jid],
                "confidence": "EXACT",
                "source": "known_mirror_constant",
            }
        )
    existing = _agency_url(j)
    if existing:
        proposals.append(
            {
                "mirror_url": existing,
                "confidence": "EXACT",
                "source": "registry_agency_url",
            }
        )
    name = j.get("name") or ""
    st = j.get("state")
    bt = j.get("buyer_type")
    for e in _catalog_agency_candidates():
        if e.get("state") != st:
            continue
        if e.get("buyer_type") != bt and not (
            bt == "CITY" and e.get("buyer_type") == "CITY"
        ) and not (bt == "COUNTY" and e.get("buyer_type") == "COUNTY"):
            continue
        conf = crosswalk_confidence(name, str(e.get("name") or ""))
        if conf in {"EXACT", "STRONG"}:
            proposals.append(
                {
                    "mirror_url": e["portal_url"],
                    "confidence": conf,
                    "source": e.get("catalog") or "catalog",
                    "catalog_name": e.get("name"),
                }
            )
    # Heuristic official domains (validated later by probe — never invent as confirmed)
    slug = re.sub(r"[^a-z0-9]+", "", name.lower().replace("county", "").replace("city", ""))
    hy = re.sub(r"[^a-z0-9]+", "", name.lower())
    heuristics = []
    if bt == "COUNTY" and slug:
        heuristics += [
            f"https://www.{slug}county.gov/purchasing",
            f"https://www.{slug}county.gov/procurement",
            f"https://www.{hy}.gov/purchasing",
        ]
        if st == "TX":
            heuristics.append(f"https://www.{slug}countytx.gov/Purchasing")
        if st == "IL":
            heuristics.append(f"https://www.{slug}countyil.gov/purchasing")
        if st == "FL":
            heuristics.append(f"https://www.{slug}countyfl.gov/purchasing")
    if bt == "CITY" and slug:
        heuristics += [
            f"https://www.{slug}.gov/purchasing",
            f"https://www.cityof{slug}.gov/purchasing",
            f"https://www.{slug}.gov/149/Purchasing",
        ]
    for u in heuristics:
        proposals.append({"mirror_url": u, "confidence": "AMBIGUOUS", "source": "heuristic_unverified"})

    # Dedupe by URL prefer higher confidence
    rank = {"EXACT": 0, "STRONG": 1, "AMBIGUOUS": 2, "NO_MATCH": 3}
    best: dict[str, dict[str, Any]] = {}
    for p in proposals:
        u = p["mirror_url"]
        if u not in best or rank.get(p["confidence"], 9) < rank.get(best[u]["confidence"], 9):
            best[u] = p
    return sorted(best.values(), key=lambda x: rank.get(x["confidence"], 9))


def validate_mirror(
    client: PublicProcurementHttpClient,
    *,
    jurisdiction: dict[str, Any],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Probe mirror; do NOT fetch OpenGov CDN. Ambiguous heuristics need successful parse."""
    url = proposal["mirror_url"]
    conf = proposal.get("confidence")
    out = {
        "jurisdiction_id": jurisdiction["jurisdiction_id"],
        "state": jurisdiction.get("state"),
        "name": jurisdiction.get("name"),
        "central_opengov_url": jurisdiction.get("bid_portal") or jurisdiction.get("procurement_page"),
        "portal_slug": extract_portal_slug(
            str(jurisdiction.get("bid_portal") or jurisdiction.get("procurement_page") or "")
        ),
        "mirror_url": url,
        "confidence": conf,
        "proposal_source": proposal.get("source"),
        "public": False,
        "automatable": False,
        "documents_accessible": None,
        "bid_submission_path": None,
        "status": OPENGOV_CDN_ANTI_BOT,
        "live_rows": 0,
        "error": None,
    }
    if "opengov.com" in url.lower():
        out["error"] = "refused_cdn_url"
        out["status"] = OPENGOV_CDN_ANTI_BOT
        return out
    try:
        resp = client.get(url, source_id=f"l18_mirror_{jurisdiction['jurisdiction_id']}")
        body = resp.text or ""
        if _is_cloudflare(body, resp.status_code):
            out["error"] = "mirror_also_cloudflare"
            return out
        if resp.status_code and resp.status_code >= 400:
            out["error"] = f"http_{resp.status_code}"
            return out
        out["public"] = True
        fetcher = OpenGovLiveFetcher()
        raw = fetcher.fetch_listing(
            client, list_url=url, source_id=f"l18_og_{jurisdiction['jurisdiction_id']}", max_pages=1
        )
        opps = list(raw.get("opportunities") or [])
        used_url = url
        if not opps:
            for child in _bid_board_child_urls(url, body):
                if "opengov.com" in child.lower():
                    continue
                raw2 = fetcher.fetch_listing(
                    client,
                    list_url=child,
                    source_id=f"l18_ogc_{jurisdiction['jurisdiction_id']}",
                    max_pages=1,
                )
                opps = list(raw2.get("opportunities") or [])
                if not opps:
                    opps = list(
                        SimpleHtmlLiveFetcher()
                        .fetch_listing(
                            client,
                            list_url=child,
                            source_id=f"l18_sh_{jurisdiction['jurisdiction_id']}",
                            max_pages=1,
                        )
                        .get("opportunities")
                        or []
                    )
                if opps:
                    used_url = child
                    break
        if not opps:
            opps = list(
                SimpleHtmlLiveFetcher()
                .fetch_listing(
                    client,
                    list_url=url,
                    source_id=f"l18_sh2_{jurisdiction['jurisdiction_id']}",
                    max_pages=1,
                )
                .get("opportunities")
                or []
            )
        out["live_rows"] = len(opps)
        out["mirror_url"] = used_url
        out["rows"] = [
            (o.to_dict() if hasattr(o, "to_dict") else dict(o)) for o in opps
        ]
        if opps:
            out["automatable"] = True
            out["documents_accessible"] = "DOCS_PUBLIC"
            out["bid_submission_path"] = used_url
            out["status"] = "MIRROR_ACTIVE"
            if conf == "AMBIGUOUS":
                out["confidence"] = "STRONG"  # upgraded by live yield
        elif out["public"] and conf in {"EXACT", "STRONG"}:
            out["status"] = "MIRROR_PUBLIC_NO_CURRENT_BIDS"
            out["automatable"] = False
        else:
            out["status"] = "MIRROR_UNCONFIRMED"
            out["rows"] = []
    except Exception as e:
        out["error"] = str(e)[:200]
        out["status"] = "MIRROR_PROBE_FAILED"
    return out


def _activate_mirror(reg: dict[str, Any], j: dict[str, Any], best: dict[str, Any], new_rows: list[dict[str, Any]]) -> None:
    jid = j["jurisdiction_id"]
    target = (reg.get("jurisdictions") or {}).get(jid)
    if target and best.get("mirror_url"):
        target["bid_portal"] = best["mirror_url"]
        target["procurement_page"] = best["mirror_url"]
        target["platform_access_state"] = PUBLIC_LISTING_AUTOMATABLE
        target["source_activation"] = INGESTION_ACTIVE
        target["source_status"] = "AUTOMATED_STATIC"
        target["opengov_mirror"] = best["mirror_url"]
        target["last_successful_discovery"] = _utc()
    for row in best.pop("rows", []) or []:
        row["source_id"] = f"platform_opengov_mirror_{jid}"
        row["source_portal"] = row["source_id"]
        row["platform_family"] = "OpenGov"
        row["jurisdiction_registry_id"] = jid
        row["state_code"] = j.get("state")
        row["agency"] = row.get("agency") or j.get("name")
        row["jurisdiction"] = j.get("buyer_type") or "LOCAL"
        row["our_bid_access"] = "YES"
        row["l18_opengov_mirror"] = True
        row["source_url"] = best["mirror_url"]
        if not row.get("detail_url"):
            row["detail_url"] = best["mirror_url"]
        new_rows.append(row)


def discover_and_activate_mirrors(
    *,
    max_blocked: int | None = 40,
    resume: bool = True,
) -> dict[str, Any]:
    """Attempt agency mirrors for OpenGov CDN-blocked buyers. No Cloudflare bypass."""
    reg = load_registry()
    client = PublicProcurementHttpClient(
        authorize_live=True,
        budget=RequestBudget(max_total_requests=900, max_requests_per_source=8),
    )
    mirrors: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    if MIRROR_REGISTRY_PATH.exists() and resume:
        prior = json.loads(MIRROR_REGISTRY_PATH.read_text(encoding="utf-8"))
        for m in prior.get("mirrors") or []:
            by_id[m["jurisdiction_id"]] = m

    new_rows: list[dict[str, Any]] = []
    found = confirmed = automated = still_blocked = 0
    attempted = 0

    # Pass 1 — always revalidate/harvest known productive mirrors
    for jid, url in OPENGOV_AGENCY_MIRRORS.items():
        j = (reg.get("jurisdictions") or {}).get(jid) or {
            "jurisdiction_id": jid,
            "name": jid,
            "state": jid.split(":")[0] if ":" in jid else None,
            "buyer_type": jid.split(":")[1] if ":" in jid else None,
            "bid_portal": f"https://procurement.opengov.com/portal/{jid}",
        }
        attempted += 1
        res = validate_mirror(
            client,
            jurisdiction=j,
            proposal={"mirror_url": url, "confidence": "EXACT", "source": "known_mirror_constant"},
        )
        res["attempts"] = int((by_id.get(jid) or {}).get("attempts") or 0) + 1
        res["generated_at"] = _utc()
        if res.get("status") == "MIRROR_ACTIVE":
            found += 1
            confirmed += 1
            automated += 1
            _activate_mirror(reg, j, res, new_rows)
        elif res.get("status") == "MIRROR_PUBLIC_NO_CURRENT_BIDS":
            found += 1
            confirmed += 1
            if (reg.get("jurisdictions") or {}).get(jid):
                target = reg["jurisdictions"][jid]
                target["opengov_mirror"] = res["mirror_url"]
                target["bid_portal"] = res["mirror_url"]
                target["procurement_page"] = res["mirror_url"]
        elif not res.get("public"):
            still_blocked += 1
        res.pop("rows", None)
        by_id[jid] = res

    # Pass 2 — remaining blocked / inactive agency candidates
    blocked = [
        j
        for j in blocked_opengov_jurisdictions(reg)
        if j["jurisdiction_id"] not in OPENGOV_AGENCY_MIRRORS
    ]
    if max_blocked is not None:
        # Reserve known-pass count; apply remaining budget to blocked
        remaining_budget = max(0, max_blocked)
        blocked = blocked[:remaining_budget]

    for j in blocked:
        jid = j["jurisdiction_id"]
        attempted += 1
        proposals = propose_mirrors_for_jurisdiction(j)
        proposals = [p for p in proposals if p.get("confidence") in {"EXACT", "STRONG"}] + [
            p for p in proposals if p.get("confidence") == "AMBIGUOUS"
        ]
        best = None
        for prop in proposals[:5]:
            res = validate_mirror(client, jurisdiction=j, proposal=prop)
            res["attempts"] = 1
            res["generated_at"] = _utc()
            if res.get("status") == "MIRROR_ACTIVE":
                best = res
                break
            if res.get("status") == "MIRROR_PUBLIC_NO_CURRENT_BIDS" and (
                not best or best.get("status") != "MIRROR_PUBLIC_NO_CURRENT_BIDS"
            ):
                best = res
            elif not best and res.get("public"):
                best = res
        if not best:
            best = {
                "jurisdiction_id": jid,
                "state": j.get("state"),
                "name": j.get("name"),
                "central_opengov_url": j.get("bid_portal"),
                "mirror_url": None,
                "confidence": "NO_MATCH",
                "status": OPENGOV_CDN_ANTI_BOT,
                "public": False,
                "automatable": False,
                "live_rows": 0,
                "attempts": 1,
                "generated_at": _utc(),
            }
            still_blocked += 1
        else:
            found += 1
            if best.get("status") in {"MIRROR_ACTIVE", "MIRROR_PUBLIC_NO_CURRENT_BIDS"}:
                confirmed += 1
            if best.get("status") == "MIRROR_ACTIVE":
                automated += 1
                _activate_mirror(reg, j, best, new_rows)
            elif best.get("status") == OPENGOV_CDN_ANTI_BOT or not best.get("public"):
                still_blocked += 1
            best.pop("rows", None)
        by_id[jid] = best

    mirrors = list(by_id.values())
    payload = {
        "kind": "OpenGovAgencyMirrorRegistry",
        "build": BUILD,
        "generated_at": _utc(),
        "blocked_attempted": attempted,
        "mirrors_found": found,
        "mirrors_confirmed": confirmed,
        "automated": automated,
        "manual_public": max(0, confirmed - automated),
        "still_blocked": still_blocked,
        "mirrors": mirrors,
        "new_live_rows": len(new_rows),
        "no_cloudflare_bypass": True,
    }
    MIRROR_REGISTRY_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    REGISTRY_PATH.write_text(json.dumps(reg, separators=(",", ":"), default=str), encoding="utf-8")
    payload["rows"] = new_rows
    return payload
