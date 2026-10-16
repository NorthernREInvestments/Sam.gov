"""Mandatory anonymous source admission tests before production ingestion."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.anonymous_source_admission_v1")

PUBLIC_FULL_ACCESS = "PUBLIC_FULL_ACCESS"
PUBLIC_PARTIAL_DOCUMENT_ACCESS = "PUBLIC_PARTIAL_DOCUMENT_ACCESS"
PUBLIC_LISTING_ONLY = "PUBLIC_LISTING_ONLY"
AUTH_REQUIRED = "AUTH_REQUIRED"
PAID_REQUIRED = "PAID_REQUIRED"
CAPTCHA_BLOCKED = "CAPTCHA_BLOCKED"
BROKEN = "BROKEN"
NO_LIVE_OPPORTUNITIES = "NO_LIVE_OPPORTUNITIES"

_UA = {
    "User-Agent": "M3AnonymousAdmission/1.0 (public procurement research)",
    "Accept": "application/json,text/html,*/*",
}


def _get(url: str, *, timeout: float = 20.0) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), str(resp.headers.get("Content-Type") or ""), resp.read(250_000)


def _classify_body(status: int, content_type: str, body: bytes, *, list_url: str) -> dict[str, Any]:
    text = body.decode("utf-8", "replace")
    low = text.lower()
    ct = (content_type or "").lower()

    if status in (401, 403):
        if "captcha" in low or "cloudflare" in low or "awswaf" in low or "challenge.js" in low:
            return {"access_class": CAPTCHA_BLOCKED, "live_count": 0, "note": f"HTTP {status} bot/WAF"}
        return {"access_class": AUTH_REQUIRED, "live_count": 0, "note": f"HTTP {status}"}

    if "awswaf" in low or "challenge.js" in low or ("captcha" in low and "bid" not in low):
        return {"access_class": CAPTCHA_BLOCKED, "live_count": 0, "note": "WAF/CAPTCHA challenge page"}

    if any(x in low for x in ("sign in", "log in", "login required", "vendor login", "create an account")) and not (
        "open bid" in low or "solicitation" in low or body[:1] in (b"[", b"{")
    ):
        return {"access_class": AUTH_REQUIRED, "live_count": 0, "note": "Login wall"}

    if "subscription" in low and "upgrade" in low and "bid" in low:
        return {"access_class": PAID_REQUIRED, "live_count": 0, "note": "Paid wall signals"}

    # JSON listing
    if body[:1] in (b"[", b"{") or "json" in ct:
        try:
            data = json.loads(body)
        except Exception:
            data = None
        if isinstance(data, list):
            n = len(data)
            if n == 0:
                return {"access_class": NO_LIVE_OPPORTUNITIES, "live_count": 0, "note": "Empty JSON list"}
            sample = data[0] if data else {}
            keys = list(sample.keys()) if isinstance(sample, dict) else []
            has_id = any(k.lower() in {"pin", "request_id", "solicitation_number", "number", "id", "title", "short_title"} for k in keys)
            return {
                "access_class": PUBLIC_LISTING_ONLY if has_id else PUBLIC_PARTIAL_DOCUMENT_ACCESS,
                "live_count": n,
                "sample_keys": keys[:20],
                "note": "Public JSON listing",
                "document_anonymous": False,
            }
        if isinstance(data, dict) and data:
            return {
                "access_class": PUBLIC_PARTIAL_DOCUMENT_ACCESS,
                "live_count": 1,
                "note": "JSON object response",
            }

    # HTML listing signals
    bid_hits = len(re.findall(r"\b(rfp|rfq|ifb|solicitation|invitation\s+to\s+bid|open\s+bids?)\b", low))
    link_hits = len(re.findall(r"href=[\"'][^\"']+(?:bid|solicitation|opportunity|event)[^\"']*[\"']", low, re.I))
    if bid_hits >= 2 or link_hits >= 3:
        return {
            "access_class": PUBLIC_LISTING_ONLY,
            "live_count": max(bid_hits, link_hits),
            "note": "HTML listing signals present; documents not verified in admission",
            "document_anonymous": False,
        }

    if status >= 400:
        return {"access_class": BROKEN, "live_count": 0, "note": f"HTTP {status}"}
    return {"access_class": BROKEN, "live_count": 0, "note": "No extractable solicitation listing"}


def admit_source(
    *,
    source_id: str,
    name: str,
    list_url: str,
    portal_family: str = "",
    state: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    started = time.time()
    row: dict[str, Any] = {
        "source_id": source_id,
        "name": name,
        "list_url": list_url,
        "portal_family": portal_family,
        "state": state,
        "tested_at": now_utc().isoformat(),
    }
    try:
        status, ct, body = _get(list_url, timeout=timeout)
        cls = _classify_body(status, ct, body, list_url=list_url)
        row.update(cls)
        row["http_status"] = status
        row["content_type"] = (ct or "")[:80]
        row["bytes"] = len(body)
        # Promote to FULL only when JSON listing + known public doc pattern (conservative)
        if row.get("access_class") == PUBLIC_LISTING_ONLY and "json" in (ct or "").lower():
            row["access_class"] = PUBLIC_PARTIAL_DOCUMENT_ACCESS
            row["note"] = (row.get("note") or "") + "; docs require per-opportunity verification"
    except urllib.error.HTTPError as exc:
        body = exc.read(8000) if hasattr(exc, "read") else b""
        low = body.decode("utf-8", "replace").lower()
        if exc.code in (401, 403):
            if "awswaf" in low or "captcha" in low or "cloudflare" in low:
                row.update({"access_class": CAPTCHA_BLOCKED, "live_count": 0, "http_status": exc.code})
            else:
                row.update({"access_class": AUTH_REQUIRED, "live_count": 0, "http_status": exc.code})
        else:
            row.update({"access_class": BROKEN, "live_count": 0, "http_status": exc.code, "note": str(exc)[:200]})
    except Exception as exc:
        row.update({"access_class": BROKEN, "live_count": 0, "note": f"{type(exc).__name__}: {exc}"[:240]})
    row["elapsed_ms"] = int((time.time() - started) * 1000)
    row["production_ingest_ok"] = row.get("access_class") in {
        PUBLIC_FULL_ACCESS,
        PUBLIC_PARTIAL_DOCUMENT_ACCESS,
        PUBLIC_LISTING_ONLY,
    } and int(row.get("live_count") or 0) > 0
    return row


def default_admission_targets() -> list[dict[str, Any]]:
    """Representative multi-agency / multi-family admission set."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000")
    nyc_live = (
        "https://data.cityofnewyork.us/resource/3khw-qi8f.json?"
        + urllib.parse.urlencode(
            {
                "$where": f"type_of_notice_description='Solicitation' AND due_date > '{now}'",
                "$limit": "50",
                "$order": "due_date ASC",
            }
        )
    )
    return [
        {
            "source_id": "admit_nyc_current_solicitations",
            "name": "NYC Current Solicitations (3khw-qi8f live)",
            "list_url": nyc_live,
            "portal_family": "Socrata",
            "state": "NY",
        },
        {
            "source_id": "admit_nyc_city_record",
            "name": "NYC City Record Online",
            "list_url": "https://data.cityofnewyork.us/resource/dg92-zbpx.json?"
            + urllib.parse.urlencode(
                {
                    "$where": "type_of_notice_description='Solicitation' AND start_date > '2025-01-01T00:00:00.000'",
                    "$limit": "50",
                    "$order": "start_date DESC",
                }
            ),
            "portal_family": "Socrata",
            "state": "NY",
        },
        {
            "source_id": "admit_montgomery_solicitations",
            "name": "Montgomery County MD Solicitations",
            "list_url": "https://data.montgomerycountymd.gov/resource/eeq6-nnwe.json?"
            + urllib.parse.urlencode({"$where": "status='Active'", "$limit": "50"}),
            "portal_family": "Socrata",
            "state": "MD",
        },
        {
            "source_id": "admit_la_ramp",
            "name": "LA RAMP Open Bids",
            "list_url": "https://data.lacity.org/resource/hf3r-utnq.json?"
            + urllib.parse.urlencode({"$where": "stagename='Open'", "$limit": "50"}),
            "portal_family": "Socrata",
            "state": "CA",
        },
        {
            "source_id": "admit_planetbids_sandiego",
            "name": "PlanetBids San Diego",
            "list_url": "https://pbsystem.planetbids.com/portal/15820/bo/bo-search",
            "portal_family": "PlanetBids",
            "state": "CA",
        },
        {
            "source_id": "admit_planetbids_longbeach",
            "name": "PlanetBids Long Beach",
            "list_url": "https://pbsystem.planetbids.com/portal/13784/bo/bo-search",
            "portal_family": "PlanetBids",
            "state": "CA",
        },
        {
            "source_id": "admit_caleprocure",
            "name": "California Cal eProcure",
            "list_url": "https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx",
            "portal_family": "StateOwned",
            "state": "CA",
        },
        {
            "source_id": "admit_ga_gpr",
            "name": "Georgia Procurement Registry",
            "list_url": "https://ssl.doas.state.ga.us/gpr/",
            "portal_family": "SimpleHTML",
            "state": "GA",
        },
        {
            "source_id": "admit_nc_ips",
            "name": "North Carolina IPS",
            "list_url": "https://www.ips.state.nc.us/IPS/Search.aspx",
            "portal_family": "SimpleHTML",
            "state": "NC",
        },
        {
            "source_id": "admit_la_pac",
            "name": "Louisiana LaPAC",
            "list_url": "https://wwwcfprd.doa.louisiana.gov/osp/lapac/pubMain.cfm",
            "portal_family": "SimpleHTML",
            "state": "LA",
        },
        {
            "source_id": "admit_ia_sciquest",
            "name": "Iowa SciQuest Public Events",
            "list_url": "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa",
            "portal_family": "Jaggaer",
            "state": "IA",
        },
        {
            "source_id": "admit_mt_sciquest",
            "name": "Montana SciQuest Public Events",
            "list_url": "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana",
            "portal_family": "Jaggaer",
            "state": "MT",
        },
        {
            "source_id": "admit_tx_esbd",
            "name": "Texas ESBD",
            "list_url": "https://www.txsmartbuy.gov/esbd",
            "portal_family": "StateOwned",
            "state": "TX",
        },
        {
            "source_id": "admit_nh_bids",
            "name": "New Hampshire Bids",
            "list_url": "https://apps.das.nh.gov/bidscontracts/bids.aspx",
            "portal_family": "SimpleHTML",
            "state": "NH",
        },
        {
            "source_id": "admit_ar_osp",
            "name": "Arkansas OSP Bid Opportunities",
            "list_url": "https://sas.arkansas.gov/procurement/bid-opportunities/",
            "portal_family": "SimpleHTML",
            "state": "AR",
        },
    ]


def run_admission_matrix(*, targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    targets = targets or default_admission_targets()
    results = []
    for t in targets:
        results.append(
            admit_source(
                source_id=str(t["source_id"]),
                name=str(t.get("name") or t["source_id"]),
                list_url=str(t["list_url"]),
                portal_family=str(t.get("portal_family") or ""),
                state=t.get("state"),
            )
        )
        time.sleep(0.25)

    by_class: dict[str, int] = {}
    for r in results:
        c = str(r.get("access_class") or "UNKNOWN")
        by_class[c] = by_class.get(c, 0) + 1

    ingestible = [r for r in results if r.get("production_ingest_ok")]
    report = {
        "kind": "AnonymousSourceAdmissionMatrix",
        "build": "20261016-m3-anonymous-replacement-v1",
        "tested_at": now_utc().isoformat(),
        "targets_tested": len(results),
        "by_access_class": by_class,
        "ingestible_count": len(ingestible),
        "results": results,
        "production_rule": (
            "Only PUBLIC_FULL_ACCESS is unrestricted; "
            "PUBLIC_PARTIAL_DOCUMENT_ACCESS / PUBLIC_LISTING_ONLY may ingest with access flags"
        ),
    }
    try:
        from m3_data_root import data_path

        path = data_path("m3_anonymous_source_admission_matrix_v1.json")
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["persisted_path"] = str(path)
    except Exception:
        log.exception("Could not persist admission matrix")
    return report
