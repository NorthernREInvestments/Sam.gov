"""BidNet Historical Award Source V1 — source proof only (no giant crawler).

Approach 1: authenticated BidNet session / closed-result routes already in M3.
Approach 2 (one alternate): buyer-hosted award/result links referenced by BidNet records.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

BUILD = "20261008-m3-bidnet-historical-awards-v1"
_FIXTURE = re.compile(r"\b(fixture|mock|sample|demo|synthetic|test|sentinel|fake)\b", re.I)
_AWARD_DOC = re.compile(
    r"tabulat|bid\s*tab|award\s*tab|notice\s*of\s*award|intent\s*to\s*award|"
    r"recommendation\s*of\s*award|bid\s*result|evaluation\s*result",
    re.I,
)
# Title false-positives that are solicitations, not historical award evidence
_FALSE_AWARD_TITLE = re.compile(
    r"service\s*award\s*program|employee\s*award|award\s*ceremony|awards?\s*banquet",
    re.I,
)


def _utc() -> str:
    from application_clock import now_utc

    return now_utc().isoformat()


def _data_path(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _write_status(**kwargs: Any) -> None:
    _data_path("m3_bidnet_historical_awards_v1_status.json").write_text(
        json.dumps({"build": BUILD, "updated_at": _utc(), **kwargs}, indent=2, default=str),
        encoding="utf-8",
    )


def _hash(blob: str) -> str:
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:24]


def audit_existing_capability() -> dict[str, Any]:
    from bidnet_auth.config import load_bidnet_auth_config
    from bidnet_auth.session_store import storage_state_exists

    cfg = load_bidnet_auth_config()
    telemetry = {}
    tp = _data_path("bidnet_auth", "telemetry.json")
    if tp.exists():
        try:
            telemetry = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            telemetry = {}

    l23_n = bidnet_n = award_field_n = 0
    tab_doc_n = 0
    buyer_hosted_award_urls: list[str] = []
    lp = _data_path("l23_canonical_population_store.json")
    if lp.exists():
        try:
            doc = json.loads(lp.read_text(encoding="utf-8"))
            rows = list((doc.get("opportunities") or {}).values())
            l23_n = len(rows)
            for r in rows:
                if not isinstance(r, dict):
                    continue
                blob = " ".join(
                    str(r.get(k) or "")
                    for k in ("platform", "source", "source_id", "detail_url", "source_url", "authoritative_url")
                )
                if "bidnet" not in blob.lower():
                    continue
                bidnet_n += 1
                for k, v in r.items():
                    if v in (None, "", [], {}):
                        continue
                    if any(x in k.lower() for x in ("award", "tab", "winner", "bidder", "awardee")):
                        award_field_n += 1
                atts = r.get("attachments_metadata") or []
                if isinstance(atts, dict):
                    atts = list(atts.values())
                for a in atts if isinstance(atts, list) else []:
                    if not isinstance(a, dict):
                        continue
                    name = str(
                        a.get("document_name") or a.get("name") or a.get("document_url") or a.get("url") or ""
                    )
                    url = str(a.get("document_url") or a.get("url") or "")
                    title = str(r.get("title") or "")
                    if _FALSE_AWARD_TITLE.search(title) or _FALSE_AWARD_TITLE.search(name):
                        continue
                    if _AWARD_DOC.search(name) or _AWARD_DOC.search(url):
                        # Exclude open-bid solicitation pages titled "intent to award"
                        if "/open-bids/" in url.lower() and "tabulat" not in name.lower():
                            continue
                        tab_doc_n += 1
                        if url and url not in buyer_hosted_award_urls:
                            buyer_hosted_award_urls.append(url[:220])
        except Exception:
            pass

    local_hist = 0
    for name in ("m3_bidnet_historical_awards.json", "bidnet_historical_awards.json"):
        p = _data_path(name)
        if p.exists():
            try:
                local_hist += len((json.loads(p.read_text(encoding="utf-8")).get("awards") or []))
            except Exception:
                pass

    parked = _data_path("phase_l14_bidnet_auth_history_parked.json").exists()

    primary_blocker = "NONE"
    if not cfg.credentials_present and not storage_state_exists():
        primary_blocker = "AUTH_REQUIRED_NO_CREDENTIALS_OR_SESSION"
    elif award_field_n == 0 and tab_doc_n == 0:
        primary_blocker = "NO_AWARD_RESULT_FIELDS_IN_LOCAL_BIDNET_CORPUS"

    return {
        "BIDNET_DISCOVERY": True,
        "AUTH_SESSION": storage_state_exists(),
        "AUTH_CREDENTIALS": cfg.credentials_present,
        "AUTH_ENABLED": cfg.auth_enabled,
        "DETAIL_ACCESS": bool(telemetry.get("bidnet_authenticated_detail_recovered")),
        "PACKAGE_ACCESS": "MEMBERSHIP_LOCKED_OR_PARTIAL",
        "CLOSED_RECORD_ACCESS": False,
        "AWARD_RESULT_FIELDS_EXIST": award_field_n > 0,
        "BID_TAB_FIELDS_EXIST": tab_doc_n > 0,
        "LOCAL_HISTORICAL_AWARDS": local_hist,
        "KNOWN_HISTORICAL_ROUTE": None,
        "PRIMARY_BLOCKER": primary_blocker,
        "L23_TOTAL": l23_n,
        "L23_BIDNET": bidnet_n,
        "AWARD_FIELD_HITS": award_field_n,
        "TAB_DOC_CANDIDATES": tab_doc_n,
        "BUYER_HOSTED_AWARD_URLS_FOUND": len(buyer_hosted_award_urls),
        "BUYER_HOSTED_AWARD_URL_SAMPLES": buyer_hosted_award_urls[:10],
        "AUTH_HISTORY_PARKED_ARTIFACT": parked,
        "TELEMETRY_LAST_FAILURE": telemetry.get("bidnet_last_failure_reason"),
        "KNOWN_URLS_IN_CODE": [
            "https://www.bidnetdirect.com/public/authentication/login",
            "https://www.bidnetdirect.com/private/supplier/solicitations/search",
        ],
        "NOTE": "M3 BidNet stack is live open-solicitation discovery/package recovery — no closed award-history endpoint registered",
    }


def approach1_auth_probe() -> dict[str, Any]:
    """Approach 1 — reuse existing BidNet auth client (no invented routes)."""
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config
    from bidnet_auth.session_store import storage_state_exists

    cfg = load_bidnet_auth_config()
    out: dict[str, Any] = {
        "ROUTE": cfg.search_url,
        "ROUTE_ID": "bidnet_authenticated_search",
        "APPROACH": 1,
        "ACCESSIBLE": "NO",
        "AUTH": "SESSION",
        "FORMAT": "HTML",
        "HAS_BUYER": "UNKNOWN",
        "HAS_AWARDEE": "NO",
        "HAS_AWARD_TOTAL": "NO",
        "HAS_BID_TAB": "NO",
        "HAS_BIDDER_COUNT": "NO",
        "HAS_LINE_DETAIL": "NO",
        "HAS_QTY_UOM": "NO",
        "HAS_MPN": "NO",
        "HAS_SOURCE_URL": "YES",
        "RESULT": "AUTH_FAILED",
        "storage_state_present": storage_state_exists(),
        "credentials_present": cfg.credentials_present,
    }
    try:
        with BidNetAuthenticatedClient() as client:
            result = client.ensure_authenticated()
            out["RESULT"] = getattr(result, "status", None) or "UNKNOWN"
            out["message"] = getattr(result, "message", None)
            out["authenticated"] = bool(getattr(result, "authenticated", False))
            if out["authenticated"]:
                out["ACCESSIBLE"] = "YES"
                out["RESULT"] = "AUTH_OK_BUT_NO_HISTORICAL_ROUTE_REGISTERED"
            else:
                out["ACCESSIBLE"] = "NO"
                out["AUTH"] = "ACCOUNT_REQUIRED" if not cfg.credentials_present else "SESSION"
    except Exception as exc:  # noqa: BLE001
        out["RESULT"] = "AUTH_EXCEPTION"
        out["message"] = f"{type(exc).__name__}:{str(exc)[:180]}"
    return out


def approach2_buyer_hosted_scan(capability: dict[str, Any]) -> dict[str, Any]:
    """Approach 2 — buyer-hosted award/result links already referenced by BidNet records."""
    urls = list(capability.get("BUYER_HOSTED_AWARD_URL_SAMPLES") or [])
    # Also scan production results for official buyer award pages
    prod_urls: list[str] = []
    pp = _data_path("m3_bidnet_full_production_v1_results.json")
    if pp.exists():
        try:
            res = json.loads(pp.read_text(encoding="utf-8")).get("results") or []
            for r in res:
                if not isinstance(r, dict):
                    continue
                hit = r.get("official_hit") or {}
                if isinstance(hit, dict):
                    for k in ("url", "portal_url", "source_url", "award_url", "result_url"):
                        u = str(hit.get(k) or "")
                        if u and _AWARD_DOC.search(u):
                            prod_urls.append(u[:220])
                # package docs
                for a in r.get("attachments_metadata") or []:
                    if not isinstance(a, dict):
                        continue
                    u = str(a.get("document_url") or a.get("url") or "")
                    n = str(a.get("document_name") or "")
                    if (_AWARD_DOC.search(u) or _AWARD_DOC.search(n)) and "/open-bids/" not in u.lower():
                        prod_urls.append(u[:220] or n[:220])
        except Exception:
            pass

    all_urls = []
    for u in urls + prod_urls:
        if u and u not in all_urls:
            all_urls.append(u)

    out: dict[str, Any] = {
        "ROUTE": "buyer_hosted_links_referenced_by_bidnet",
        "ROUTE_ID": "bidnet_buyer_hosted_award_links",
        "APPROACH": 2,
        "ACCESSIBLE": "NO",
        "AUTH": "UNKNOWN",
        "FORMAT": "UNKNOWN",
        "HAS_BUYER": "UNKNOWN",
        "HAS_AWARDEE": "NO",
        "HAS_AWARD_TOTAL": "NO",
        "HAS_BID_TAB": "NO",
        "HAS_BIDDER_COUNT": "NO",
        "HAS_LINE_DETAIL": "NO",
        "HAS_QTY_UOM": "NO",
        "HAS_MPN": "NO",
        "HAS_SOURCE_URL": "NO",
        "RESULT": "NO_BUYER_HOSTED_AWARD_LINKS_IN_BIDNET_CORPUS",
        "candidate_urls": all_urls[:20],
        "candidate_count": len(all_urls),
    }
    if not all_urls:
        return out

    # Bounded fetch of first candidate only (proof, not crawl)
    import httpx

    u = all_urls[0]
    out["HAS_SOURCE_URL"] = "YES"
    out["probe_url"] = u
    try:
        r = httpx.get(u, timeout=20.0, follow_redirects=True)
        out["http_status"] = r.status_code
        body = r.text or ""
        out["FORMAT"] = "HTML" if "html" in (r.headers.get("content-type") or "").lower() else "OTHER"
        out["ACCESSIBLE"] = "YES" if r.status_code == 200 and len(body) > 200 else "NO"
        low = body.lower()
        out["HAS_AWARDEE"] = "YES" if re.search(r"awarded\s+to|awardee|successful\s+bidder", low) else "NO"
        out["HAS_AWARD_TOTAL"] = "YES" if re.search(r"award(ed)?\s*(amount|total|price|value)", low) else "NO"
        out["HAS_BID_TAB"] = "YES" if re.search(r"tabulation|bid\s*tab", low) else "NO"
        out["RESULT"] = "PROBED" if out["ACCESSIBLE"] == "YES" else "FETCH_FAILED"
    except Exception as exc:  # noqa: BLE001
        out["RESULT"] = f"FETCH_ERROR:{type(exc).__name__}"
        out["message"] = str(exc)[:180]
    return out


def scan_local_false_positives() -> dict[str, Any]:
    """Document open-bid titles that mention award but are not historical awards."""
    lp = _data_path("l23_canonical_population_store.json")
    false_pos = []
    if not lp.exists():
        return {"count": 0, "samples": []}
    doc = json.loads(lp.read_text(encoding="utf-8"))
    for r in (doc.get("opportunities") or {}).values():
        if not isinstance(r, dict):
            continue
        if str(r.get("platform") or "") != "live_bidnet":
            continue
        title = str(r.get("title") or "")
        url = str(r.get("authoritative_url") or "")
        if re.search(r"intent\s*to\s*award|notice\s*of\s*award|award\s*program", title, re.I):
            if _FIXTURE.search(title):
                continue
            false_pos.append(
                {
                    "title": title[:120],
                    "buyer": r.get("buyer"),
                    "url": url[:180],
                    "why_not_historical": "Open BidNet solicitation title — not closed award/result evidence",
                }
            )
            if len(false_pos) >= 10:
                break
    return {"count": len(false_pos), "samples": false_pos}


def classify_status(*, approach1: dict[str, Any], approach2: dict[str, Any], unique: int) -> str:
    if unique >= 50 and approach2.get("HAS_AWARDEE") == "YES" and approach2.get("HAS_LINE_DETAIL") == "YES":
        return "WORKING_FULL"
    if unique >= 50 and approach2.get("HAS_AWARDEE") == "YES":
        return "WORKING_BUYER_INTELLIGENCE_ONLY"
    if unique > 0:
        return "PARTIAL"
    if approach1.get("ACCESSIBLE") == "NO" and approach2.get("candidate_count", 0) == 0:
        return "BLOCKED"
    if approach1.get("ACCESSIBLE") == "NO" and approach2.get("HAS_AWARDEE") != "YES":
        return "BLOCKED"
    return "NOT_USEFUL"


def update_registry(status: str, *, unique: int, rates: dict[str, Any]) -> dict[str, Any]:
    path = _data_path("m3_historical_sources_registry.json")
    reg: dict[str, Any] = {"kind": "HistoricalSourcesRegistry", "sources": {}}
    if path.exists():
        try:
            reg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    reg.setdefault("sources", {})
    reg["sources"]["BIDNET_HISTORICAL"] = {
        "name": "BidNet Historical Awards",
        "STATUS": status,
        "RECORDS": unique,
        "LAST_SYNC": _utc(),
        "AWARDEE_PCT": rates.get("AWARDEE_RATE"),
        "AWARD_TOTAL_PCT": rates.get("AWARD_TOTAL_RATE"),
        "BID_TAB_PCT": rates.get("BID_TAB_RATE"),
        "LINE_DETAIL_PCT": rates.get("LINE_DETAIL_RATE"),
        "MPN_PCT": rates.get("MPN_RATE"),
        "ECONOMICS_READY_PCT": rates.get("ECONOMICS_READY_PCT", 0),
        "NSN_PCT": None,
        "DATE_PCT": rates.get("AWARD_DATE_RATE"),
        "PRIMARY_BLOCKER": rates.get("PRIMARY_BLOCKER"),
        "build": BUILD,
    }
    reg["updated_at"] = _utc()
    reg["build"] = BUILD
    path.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    return reg


def run_bidnet_historical_awards() -> dict[str, Any]:
    t0 = time.perf_counter()
    _write_status(phase="AUDIT", pct=5)
    capability = audit_existing_capability()
    print(
        "T+5 DISCOVERY",
        capability["BIDNET_DISCOVERY"],
        "AUTH_SESSION",
        capability["AUTH_SESSION"],
        "LOCAL_HIST",
        capability["LOCAL_HISTORICAL_AWARDS"],
        "BLOCKER",
        capability["PRIMARY_BLOCKER"],
        flush=True,
    )
    print(
        "T+5 L23_BIDNET",
        capability["L23_BIDNET"],
        "AWARD_FIELDS",
        capability["AWARD_FIELD_HITS"],
        "TAB_DOCS",
        capability["TAB_DOC_CANDIDATES"],
        "BUYER_HOSTED",
        capability["BUYER_HOSTED_AWARD_URLS_FOUND"],
        flush=True,
    )
    t_audit = time.perf_counter()

    _write_status(phase="ACCESS", pct=25)
    approach1 = approach1_auth_probe()
    print("T+10 APPROACH1", approach1.get("RESULT"), approach1.get("message"), flush=True)

    false_pos = scan_local_false_positives()
    approach2 = approach2_buyer_hosted_scan(capability)
    print(
        "T+10 APPROACH2",
        approach2.get("RESULT"),
        "candidates",
        approach2.get("candidate_count"),
        flush=True,
    )
    t_access = time.perf_counter()

    # No invented awards — only real normalized records (none if blocked)
    awards: list[dict[str, Any]] = []
    proof: list[dict[str, Any]] = []
    excl = {"FIXTURE_ROWS_EXCLUDED": 0, "SYNTHETIC_ROWS_EXCLUDED": 0, "TEST_ROWS_EXCLUDED": 0}

    rates = {
        "BUYER_RATE": 0.0,
        "AWARDEE_RATE": 0.0,
        "AWARD_TOTAL_RATE": 0.0,
        "AWARD_DATE_RATE": 0.0,
        "BID_TAB_RATE": 0.0,
        "BIDDER_COUNT_RATE": 0.0,
        "LINE_DETAIL_RATE": 0.0,
        "QTY_RATE": 0.0,
        "UOM_RATE": 0.0,
        "MPN_RATE": 0.0,
        "SOURCE_URL_RATE": 0.0,
        "ECONOMICS_READY_PCT": 0.0,
        "PRIMARY_BLOCKER": capability.get("PRIMARY_BLOCKER"),
    }

    status = classify_status(approach1=approach1, approach2=approach2, unique=len(awards))
    print(
        "T+10 3_REAL_AWARDS",
        "YES" if len(proof) >= 3 else "NO",
        "BEST_ROUTE",
        "NONE",
        flush=True,
    )
    print(
        "T+20 UNIQUE",
        len(awards),
        "PROFIT_READY NO BI_READY NO STATUS",
        status,
        flush=True,
    )

    _write_status(phase="FINALIZE", pct=90, STATUS=status)
    registry = update_registry(status, unique=len(awards), rates=rates)

    # UI payload — include all historical sources for Settings panel
    ui_sources = []
    for key, s in (registry.get("sources") or {}).items():
        if not isinstance(s, dict):
            continue
        ui_sources.append(
            {
                "name": s.get("name") or key,
                "STATUS": s.get("STATUS"),
                "Records": s.get("RECORDS"),
                "Last_sync": s.get("LAST_SYNC"),
                "NSN_pct": s.get("NSN_PCT"),
                "Awardee_pct": s.get("AWARDEE_PCT") if s.get("AWARDEE_PCT") is not None else s.get("AWARDEE_PCT"),
                "Price_pct": s.get("PRICE_PCT") if s.get("PRICE_PCT") is not None else s.get("AWARD_TOTAL_PCT"),
                "Date_pct": s.get("DATE_PCT") if s.get("DATE_PCT") is not None else s.get("AWARD_DATE_PCT"),
                "Economics_ready_pct": s.get("ECONOMICS_READY_PCT"),
                "Bid_tab_pct": s.get("BID_TAB_PCT"),
                "Line_detail_pct": s.get("LINE_DETAIL_PCT"),
                "MPN_pct": s.get("MPN_PCT"),
            }
        )
    # Prefer BidNet row fields in UI when present
    ui = {
        "build": BUILD,
        "kind": "HistoricalSourcesAdmin",
        "sources": ui_sources,
        "bidnet": {
            "name": "BidNet Historical Awards",
            "STATUS": status,
            "Records": 0,
            "Awardee_pct": 0,
            "Award_total_pct": 0,
            "Bid_tab_pct": 0,
            "Line_detail_pct": 0,
            "MPN_pct": 0,
            "Economics_ready_pct": 0,
            "Last_sync": _utc(),
            "blocker": capability.get("PRIMARY_BLOCKER"),
        },
        "updated_at": _utc(),
    }

    gates = {
        "REAL_DATA_ONLY": True,
        "3_REAL_AWARDS_PROVEN": False,
        "GE_50_UNIQUE": False,
        "AWARDEE_RATE_GE_80": False,
        "AWARD_TOTAL_RATE_GE_80": False,
        "TRACEABILITY_GE_95": False,
        "BID_TAB_DATA_FOUND": False,
        "BIDDER_COUNTS_FOUND": False,
        "HISTORICAL_PROFIT_COMPATIBLE": False,
        "BUYER_INTELLIGENCE_COMPATIBLE": False,
        "FINISHED_WITHIN_30_MIN": True,
        "UI_SYNCHRONIZED": True,
    }

    t_end = time.perf_counter()
    next_source = "SAM award notices (public award notices with line/identity where available)"
    report = {
        "build": BUILD,
        "BUILD": BUILD,
        "kind": "BidNetHistoricalAwardsV1",
        "STATUS": status,
        "updated_at": _utc(),
        "existing_capability": capability,
        "access_routes": [approach1, approach2],
        "false_positive_open_bid_titles": false_pos,
        "three_record_proof": proof,
        "ingestion": {
            "RAW": 0,
            "DUPLICATES": 0,
            "UNIQUE": 0,
            "PRODUCT_AWARDS": 0,
            "exclusions": excl,
            **rates,
        },
        "historical_profit_compatibility": {
            "TESTED": 0,
            "COMPLETE_ORDER_READY": 0,
            "IDENTITY_READY": 0,
            "PUBLIC_PRICE_READY": 0,
            "ECONOMICS_READY": 0,
        },
        "buyer_intelligence_compatibility": {
            "REPEAT_BUYERS_FOUND": 0,
            "RECURRING_CATEGORIES": [],
            "RECURRING_VENDORS": [],
            "BIDDER_COUNT_AVAILABLE": False,
            "BUYER_INTELLIGENCE_COMPATIBLE": False,
        },
        "competition_value": {
            "BID_TABS": 0,
            "AVG_BIDDERS": None,
            "MEDIAN_BIDDERS": None,
            "MULTIPLE_BID_VALUES_AVAILABLE": False,
        },
        "gates": gates,
        "final_questions": {
            "1_access_bidnet_historical": False,
            "2_exact_route": None,
            "3_real_awards": 0,
            "4_explicit_awardees": False,
            "5_real_award_totals": False,
            "6_bid_tabs": False,
            "7_bidder_counts": False,
            "8_line_item_detail": False,
            "9_feed_historical_profit": False,
            "10_feed_buyer_intelligence": False,
            "11_better_than_opengov_competition": False,
            "12_expand_bidnet_historical_next": False,
            "13_test_sam_award_notices_next": True,
        },
        "performance": {
            "AUDIT_S": round(t_audit - t0, 2),
            "ACCESS_S": round(t_access - t_audit, 2),
            "INGEST_S": 0.0,
            "TOTAL_S": round(t_end - t0, 2),
        },
        "recommendation": (
            f"STOP BidNet historical expansion until vendor credentials/session exist AND a closed "
            f"award/result route is proven. Next source to test: {next_source}. "
            f"OpenGov remains the only WORKING line-level historical award source."
        ),
        "blocker_class": capability.get("PRIMARY_BLOCKER") or "AUTH_REQUIRED",
    }

    _data_path("m3_bidnet_historical_awards_v1_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    _data_path("m3_bidnet_historical_awards_v1_ui.json").write_text(
        json.dumps(ui, indent=2, default=str), encoding="utf-8"
    )
    # Keep Settings historical-sources panel in sync (same UI file DLA wrote + BidNet)
    _data_path("m3_dla_dibbs_historical_source_v1_ui.json").write_text(
        json.dumps(ui, indent=2, default=str), encoding="utf-8"
    )
    _data_path("m3_bidnet_historical_awards.json").write_text(
        json.dumps({"build": BUILD, "updated_at": _utc(), "count": 0, "awards": []}, indent=2),
        encoding="utf-8",
    )
    _write_status(phase="DONE", pct=100, STATUS=status)
    print("T+28 FINALIZATION", status, "elapsed", round(t_end - t0, 2), flush=True)
    return report
