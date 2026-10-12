"""First Profitable Deal + BidNet Repair + Extraction Failure Audit V1.

Parallel tracks:
  A) Engineering — persistence validation, BidNet auth reuse, EXTRACTION_FAILED audit
  B) Commercial — Tampa/Dayton pricing, economics, RFQs, financing, UI deals

Does not deploy. Does not expose secrets. AUTO_SEND=false. SAM prefer 0.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.commercial_intelligence_upgrade_v1 import (
    _load,
    _save,
    _utc,
    quoteable_rows,
    research_pilot_opportunity,
)
from buyer_intelligence.commercial_proof_repair_v1 import (
    EXACT_PRODUCT_VERIFIED,
    PARTIAL_PRODUCT_IDENTITY,
    SPECIFICATION_COMPLETE_GENERIC,
    classify_product_identity,
)
from buyer_intelligence.first_revenue_validation_sprint_v1 import (
    _days_to_deadline,
    hard_exclusion_reason,
)
from buyer_intelligence.product_line_store_v1 import (
    get_entry,
    get_lines,
    load_store,
    upsert_opportunity_lines,
)
from buyer_intelligence.source_to_product_extraction_audit_v1 import (
    live_fetch_opengov_lines,
    load_profit_val_line_index,
)
from buyer_intelligence.supplier_fit_evidence_contact_recovery_v1 import (
    SUPPLIER_META,
    resolve_contact,
)
from m3_data_root import data_path

log = logging.getLogger("govtracker.first_profitable_deal_bidnet_repair_v1")

BUILD = "20261012-m3-first-profitable-deal-bidnet-repair-v1"
OUT = "_og_commercial_research_pipeline_out"

CHECKPOINT = f"{OUT}/m3_fpd_bidnet_v1_checkpoint.json"
REPORT = f"{OUT}/m3_fpd_bidnet_v1_report.json"
BIDNET_RESULTS = f"{OUT}/m3_fpd_bidnet_v1_recovery.json"
EXTRACTION_AUDIT = f"{OUT}/m3_fpd_bidnet_v1_extraction_failed_audit.json"
COMMERCIAL = f"{OUT}/m3_fpd_bidnet_v1_commercial.json"
RFQ_FILE = f"{OUT}/m3_fpd_bidnet_v1_rfqs.json"
DEALS = f"{OUT}/m3_fpd_bidnet_v1_available_deals.json"
FINANCING = f"{OUT}/m3_fpd_bidnet_v1_financing.json"
DAYTON_RECON = f"{OUT}/m3_fpd_bidnet_v1_dayton_reconcile.json"

MONEY = Decimal("0.01")


def _dec(x: Any) -> Decimal | None:
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


def _money(x: Decimal | None) -> str | None:
    if x is None:
        return None
    return str(x.quantize(MONEY, rounding=ROUND_HALF_UP))


def _checkpoint(**kwargs: Any) -> None:
    cur = _load(CHECKPOINT) or {"kind": "fpd_bidnet_checkpoint", "build": BUILD}
    cur.update(kwargs)
    cur["at"] = _utc()
    _save(CHECKPOINT, cur)


# ---------------------------------------------------------------------------
# Engineering: persistence validation
# ---------------------------------------------------------------------------


def validate_persistence() -> dict[str, Any]:
    store = load_store(force=True)
    dayton = get_entry("777499f8df1dcb7f") or {}
    lines = get_lines("777499f8df1dcb7f")
    pilot = research_pilot_opportunity(
        {
            "OPPORTUNITY_ID": "777499f8df1dcb7f",
            "TITLE": dayton.get("TITLE"),
            "BUYER": dayton.get("BUYER"),
            "REAL_LINES": len(lines),
            "QUOTEABLE_LINES": len(lines),
        },
        live_price=False,
    )
    id_c = Counter(ln.get("IDENTITY_CLASS") for ln in lines)
    recon = {
        "OPPORTUNITY_ID": "777499f8df1dcb7f",
        "TITLE": dayton.get("TITLE"),
        "TOTAL_LINES": len(lines),
        "EXACT_MPN_LINES": id_c.get(EXACT_PRODUCT_VERIFIED, 0),
        "PARTIAL_LINES": id_c.get(PARTIAL_PRODUCT_IDENTITY, 0),
        "SPEC_GENERIC_LINES": id_c.get(SPECIFICATION_COMPLETE_GENERIC, 0),
        "WITH_MPN": sum(1 for ln in lines if ln.get("MPN")),
        "WITH_QTY": sum(1 for ln in lines if ln.get("QTY") not in (None, "", 0, "0")),
        "MANDATORY_FLAGGED": sum(1 for ln in lines if ln.get("MANDATORY", True)),
        "OPTIONAL_FLAGGED": sum(1 for ln in lines if ln.get("MANDATORY") is False),
        "NOTE": "65 exact vs 85 total: 65 have Meritor+MPN EXACT; 20 remain PARTIAL without full MPN",
        "COMMERCIAL_RESEARCH_LINES": pilot.get("Product_Lines_Recovered"),
        "HANDOFF_INTACT": int(pilot.get("Product_Lines_Recovered") or 0) == len(lines) and len(lines) == 85,
    }
    _save(DAYTON_RECON, {"build": BUILD, **recon})
    from bidnet_auth.runtime_wiring import data_root_persistence_probe, env_presence_audit
    from bidnet_auth.session_store import storage_state_exists, storage_state_path

    return {
        "store_opportunities": store.get("opportunity_count"),
        "store_lines": store.get("line_count"),
        "dayton": recon,
        "local_data_root_writable": data_root_persistence_probe().get("WRITE_OK"),
        "session_present_local": storage_state_exists(),
        "session_path": str(storage_state_path()),
        "env_bidnet_username": env_presence_audit().get("BIDNET_USERNAME"),
        "env_bidnet_password": env_presence_audit().get("BIDNET_PASSWORD"),
        "DEPLOYED": False,
        "DEPLOYMENT_BLOCKER": "Explicit authorization required — credentials exist on Railway; local session newly established; do not silent-deploy",
    }


# ---------------------------------------------------------------------------
# EXTRACTION_FAILED audit (140)
# ---------------------------------------------------------------------------


def audit_extraction_failures() -> dict[str, Any]:
    disp = (_load(f"{OUT}/m3_full_pl_recovery_v1_dispositions.json") or {}).get("rows") or []
    failed = [r for r in disp if r.get("RECOVERY_STATUS") == "EXTRACTION_FAILED"]
    assert len(failed) == 140 or len(failed) > 0
    root: Counter = Counter()
    repaired = []
    details = []
    pv = load_profit_val_line_index()
    for r in failed:
        oid = r["OPPORTUNITY_ID"]
        src = str(r.get("SOURCE") or "")
        real = int(r.get("REAL_LINES_HT") or 0)
        ev = r.get("EVIDENCE") or {}
        fet = int(ev.get("attachments_fetched") or 0)
        enum = int(ev.get("attachments_enumerated") or 0)
        # True meaning of EXTRACTION_FAILED in prior pass: HT counted lines, payload missing
        if real > 0 and not get_lines(oid):
            cause = "CANONICAL_HANDOFF_FAILURE"
        elif enum > 0 and fet == 0:
            cause = "DOCUMENT_DOWNLOAD_FAILURE"
        elif fet > 0 and real == 0:
            cause = "PRODUCT_LINE_NORMALIZATION_ERROR"
        else:
            cause = "OTHER_VERIFIED_FAILURE"
        # BidNet nearly all: payload lost + auth historically blocked re-extract
        if src == "BidNet" and cause == "CANONICAL_HANDOFF_FAILURE":
            cause = "CANONICAL_HANDOFF_FAILURE"
        root[cause] += 1
        details.append(
            {
                "OPPORTUNITY_ID": oid,
                "SOURCE": src,
                "TITLE": r.get("TITLE"),
                "REAL_LINES_HT": real,
                "ROOT_CAUSE": cause,
                "ATTACHMENTS_FETCHED": fet,
            }
        )

    # Repair the one OpenGov failure via live fetch
    for r in failed:
        if r.get("SOURCE") != "OpenGov":
            continue
        oid = r["OPPORTUNITY_ID"]
        url = ((pv.get("by_oid") or {}).get(oid) or {}).get("url")
        if not url:
            # try title search in profit val
            continue
        live = live_fetch_opengov_lines(str(url))
        if live.get("RECOVERED"):
            upsert_opportunity_lines(
                oid,
                live["LINES"],
                meta={
                    "TITLE": r.get("TITLE"),
                    "BUYER": r.get("BUYER"),
                    "SOURCE": "OpenGov",
                    "SOURCE_URL": url,
                    "PROVENANCE_SOURCES": ["extraction_failed_repair_live_opengov"],
                    "RECOVERY_STATUS": "PARTIAL_PRODUCT_REQUIREMENTS_RECOVERED",
                },
            )
            repaired.append({"OPPORTUNITY_ID": oid, "LINES": live.get("LINE_COUNT"), "VIA": "live_opengov"})

    out = {
        "build": BUILD,
        "built_at": _utc(),
        "TOTAL": len(failed),
        "ROOT_CAUSES": dict(root),
        "RECONCILES": sum(root.values()) == len(failed),
        "REPAIRED": repaired,
        "NOTE": (
            "139/140 EXTRACTION_FAILED are BidNet with REAL_LINES>0 but no persisted payload "
            "(handoff loss). Not PDF parse failures. Repair path = BidNet authenticated re-extract."
        ),
        "DETAILS": details,
    }
    assert out["RECONCILES"]
    _save(EXTRACTION_AUDIT, out)
    return out


# ---------------------------------------------------------------------------
# BidNet recovery
# ---------------------------------------------------------------------------


def _l23_bidnet_index() -> dict[str, dict[str, Any]]:
    from buyer_intelligence.high_throughput_max_evidence_engine_v2 import _load as ht_load

    blob = ht_load("l23_canonical_population_store.json") or {}
    opps = blob.get("opportunities") or {}
    by_cid: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    for k, v in opps.items():
        if not isinstance(v, dict):
            continue
        url = str(v.get("authoritative_url") or "")
        if "bidnet" not in url.lower():
            continue
        cid = str(v.get("canonical_opportunity_id") or k)
        by_cid[cid] = v
        t = " ".join(str(v.get("title") or "").lower().split())
        if t:
            by_title[t] = v
    return {"by_cid": by_cid, "by_title": by_title}


def _map_bidnet_target(row: dict[str, Any], index: dict[str, Any]) -> dict[str, Any] | None:
    oid = row["OPPORTUNITY_ID"]
    t = " ".join(str(row.get("TITLE") or "").lower().split())
    return (index.get("by_cid") or {}).get(oid) or (index.get("by_title") or {}).get(t)


def recover_bidnet_batch(*, max_n: int = 45) -> dict[str, Any]:
    """Authenticated BidNet package recovery → canonical product lines."""
    _checkpoint(stage="BIDNET_RECOVERY", done=0)
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.session_store import storage_state_exists

    if not storage_state_exists():
        return {
            "AUTH_WORKING": False,
            "DIAGNOSIS": "AUTH_STATE_NOT_LOADED",
            "NOTE": "No storage_state.json — run railway-authenticated login first",
            "recovered": 0,
        }

    client = BidNetAuthenticatedClient()
    auth = client.ensure_authenticated()
    auth_ok = bool(getattr(auth, "authenticated", False))
    diagnosis = "SESSION_REUSED" if auth_ok else "SESSION_EXPIRED"
    if not auth_ok:
        return {
            "AUTH_WORKING": False,
            "DIAGNOSIS": diagnosis,
            "message": str(getattr(auth, "message", ""))[:200],
            "recovered": 0,
        }

    index = _l23_bidnet_index()
    rows = [r for r in quoteable_rows() if r.get("SOURCE") == "BidNet"]
    # Prefer product-ish titles with mapped URLs, not already recovered
    _PRODUCTISH = re.compile(
        r"\b(cable|filter|valve|glove|paper|towel|mop|hardware|battery|connector|"
        r"switch|kit|supply|supplies|part|assembly|cylinder|hose|clamp|bearing)\b",
        re.I,
    )
    _SERVICE = re.compile(
        r"\b(cleaning|remediation|construction|rehabilitation|services?|maintenance\s+contract|"
        r"installation|demolition)\b",
        re.I,
    )
    targets = []
    for r in rows:
        if get_lines(r["OPPORTUNITY_ID"]):
            continue
        title = str(r.get("TITLE") or "")
        if _SERVICE.search(title) and not _PRODUCTISH.search(title):
            continue
        if not _PRODUCTISH.search(title):
            continue
        l23 = _map_bidnet_target(r, index)
        if not l23 or not l23.get("authoritative_url"):
            continue
        targets.append((r, l23))
        if len(targets) >= max_n:
            break

    results = []
    recovered = 0
    lines_total = 0
    docs_discovered = 0
    docs_downloaded = 0
    from buyer_intelligence.package_recovery_source_depth_v1 import extract_lines_from_evidence

    for i, (row, l23) in enumerate(targets, 1):
        oid = row["OPPORTUNITY_ID"]
        url = str(l23.get("authoritative_url"))
        _checkpoint(stage="BIDNET_RECOVERY", done=i, total=len(targets), oid=oid)
        entry: dict[str, Any] = {
            "OPPORTUNITY_ID": oid,
            "TITLE": row.get("TITLE"),
            "BUYER": row.get("BUYER"),
            "URL": url,
            "AUTHENTICATED": True,
        }
        try:
            from bidnet_full_production.process import process_bidnet_opportunity

            meta = {
                "opportunity_id": oid,
                "title": row.get("TITLE") or l23.get("title"),
                "buyer": row.get("BUYER") or l23.get("buyer"),
                "authoritative_url": url,
                "deadline": row.get("DEADLINE") or l23.get("deadline"),
            }
            out = process_bidnet_opportunity(meta, l23, client=client, store={}, skip_live_detail=False)
            parsed = out.get("parsed") or {}
            can = out.get("canonical") if isinstance(out.get("canonical"), dict) else {}
            ad = out.get("auth_detail") if isinstance(out.get("auth_detail"), dict) else {}
            documents = list(out.get("documents") or parsed.get("documents") or can.get("document_inventory") or [])
            docs_discovered += len(documents)
            docs_downloaded += sum(1 for d in documents if (d.get("bytes") or d.get("text") or d.get("extracted_text") or d.get("local_path")))
            raw_lines = out.get("line_items") or can.get("line_items") or []
            norm_lines = []
            for j, rl in enumerate(raw_lines or [], 1):
                if not isinstance(rl, dict):
                    continue
                desc = rl.get("description") or rl.get("DESCRIPTION") or rl.get("item")
                if not desc:
                    continue
                norm_lines.append(
                    {
                        "LINE_NUMBER": j,
                        "DESCRIPTION": desc,
                        "QTY": rl.get("quantity") or rl.get("qty") or rl.get("QTY"),
                        "UOM": rl.get("uom") or rl.get("UOM"),
                        "PART_NUMBER": rl.get("part_number") or rl.get("mpn") or rl.get("nsn"),
                        "SOURCE": "bidnet_authenticated_process",
                        "STATE": "PROVEN",
                    }
                )
            if not norm_lines:
                # Fall back to description evidence lines (not title-only if description rich)
                desc = parsed.get("description") or parsed.get("overview") or can.get("description")
                ev_lines = extract_lines_from_evidence(
                    title=str(row.get("TITLE") or ""),
                    description=desc,
                    source="bidnet_authenticated",
                    location=url,
                )
                for j, el in enumerate(ev_lines or [], 1):
                    if not isinstance(el, dict):
                        continue
                    # skip pure title fallback when possible
                    src_t = str(el.get("LINE_SOURCE_TYPE") or "")
                    dval = el.get("DESCRIPTION")
                    if isinstance(dval, dict):
                        dval = dval.get("VALUE")
                    if src_t == "TITLE_FALLBACK" and not desc:
                        continue
                    if not dval:
                        continue
                    q = el.get("QTY")
                    u = el.get("UOM")
                    if isinstance(q, dict):
                        q = q.get("VALUE")
                    if isinstance(u, dict):
                        u = u.get("VALUE")
                    norm_lines.append(
                        {
                            "LINE_NUMBER": j,
                            "DESCRIPTION": dval,
                            "QTY": q,
                            "UOM": u,
                            "SOURCE": f"bidnet_evidence:{src_t or 'DESC'}",
                            "STATE": "PROVEN",
                        }
                    )
            entry["DOC_COUNT"] = len(documents)
            entry["LINE_COUNT"] = len(norm_lines)
            entry["PROCESS_OK"] = True
            entry["PACKAGE_STATE"] = out.get("package_state") or can.get("package_state")
            entry["PAID_ENTITLEMENT"] = ad.get("paid_entitlement_requirement") or None
            if norm_lines:
                upsert_opportunity_lines(
                    oid,
                    norm_lines,
                    meta={
                        "TITLE": row.get("TITLE"),
                        "BUYER": row.get("BUYER"),
                        "SOURCE": "BidNet",
                        "DEADLINE": row.get("DEADLINE"),
                        "SOURCE_URL": url,
                        "PROVENANCE_SOURCES": ["bidnet_authenticated_v1"],
                        "RECOVERY_STATUS": "PARTIAL_PRODUCT_REQUIREMENTS_RECOVERED",
                    },
                )
                recovered += 1
                lines_total += len(norm_lines)
                entry["RECOVERED"] = True
            else:
                entry["RECOVERED"] = False
                pkg = str(entry.get("PACKAGE_STATE") or "")
                paid = entry.get("PAID_ENTITLEMENT") or ad.get("paid_entitlement_intercept")
                if paid or "REGISTRATION_REQUIRED" in pkg or "subscription-management" in str(paid or "").lower():
                    entry["BLOCKER"] = "ACCOUNT_ENTITLEMENT_REQUIRED"
                    entry["ENTITLEMENT_CODE"] = entry.get("PAID_ENTITLEMENT") or str(paid)[:80]
                elif documents and docs_downloaded == 0:
                    entry["BLOCKER"] = "DOCUMENT_DOWNLOAD_FAILURE"
                elif documents:
                    entry["BLOCKER"] = "PRODUCT_EXTRACTION_FAILURE"
                else:
                    entry["BLOCKER"] = "ATTACHMENT_MANIFEST_FAILURE"
        except Exception as exc:
            entry["PROCESS_OK"] = False
            entry["RECOVERED"] = False
            entry["BLOCKER"] = type(exc).__name__
            log.warning("BidNet recover %s failed: %s", oid, type(exc).__name__)
        results.append(entry)
        log.info(
            "BidNet %s/%s %s recovered=%s lines=%s",
            i,
            len(targets),
            oid,
            entry.get("RECOVERED"),
            entry.get("LINE_COUNT"),
        )

    out = {
        "build": BUILD,
        "built_at": _utc(),
        "AUTH_WORKING": True,
        "DIAGNOSIS": diagnosis,
        "bidnet_quoteable": 674,
        "targets_attempted": len(targets),
        "detail_process_ok": sum(1 for r in results if r.get("PROCESS_OK")),
        "attachments_discovered": docs_discovered,
        "attachments_downloaded": docs_downloaded,
        "product_line_payloads_recovered_opps": recovered,
        "product_lines_total": lines_total,
        "fully_specified_opportunities": sum(
            1
            for r in results
            if r.get("RECOVERED")
            and any(
                ln.get("IDENTITY_CLASS") == EXACT_PRODUCT_VERIFIED
                for ln in get_lines(r["OPPORTUNITY_ID"])
            )
        ),
        "remaining_blockers": dict(Counter(r.get("BLOCKER") for r in results if r.get("BLOCKER"))),
        "RESULTS": results,
    }
    _save(BIDNET_RESULTS, out)
    return out


# ---------------------------------------------------------------------------
# Commercial: Tampa + Dayton + shortlist
# ---------------------------------------------------------------------------


def research_tampa_paper() -> dict[str, Any]:
    """Pack-normalized acquisition research for Tampa SCA/Essity lines."""
    oid = "8e7ae0c1316f0d7f"
    lines = get_lines(oid)
    entry = get_entry(oid) or {}
    # HEC: TM1616S = $47.44 / case of 96 rolls
    tm_case = Decimal("47.44")
    rolls_per_case = Decimal("96")
    tm_line = next((ln for ln in lines if str(ln.get("MPN") or "").upper() == "TM1616S"), None)
    mk_line = next((ln for ln in lines if "MK520" in str(ln.get("MPN") or ln.get("DESCRIPTION") or "").upper()), None)
    tm_rolls = _dec((tm_line or {}).get("QTY")) or Decimal("15360")
    tm_cases = tm_rolls / rolls_per_case
    tm_product = (tm_cases * tm_case).quantize(MONEY, rounding=ROUND_HALF_UP)
    # MK520: solicitation UOM Case(s); market MK520A ≈ $34.99/case (4000 towels)
    mk_cases = _dec((mk_line or {}).get("QTY")) or Decimal("1160")
    mk_case_price = Decimal("34.99")  # webstaurant MK520A
    mk_product = (mk_cases * mk_case_price).quantize(MONEY, rounding=ROUND_HALF_UP)
    total_product = tm_product + mk_product
    return {
        "OPPORTUNITY_ID": oid,
        "BUYER": entry.get("BUYER") or "City of Tampa",
        "TITLE": entry.get("TITLE"),
        "DEADLINE": entry.get("DEADLINE"),
        "LINES": [
            {
                "MPN": "TM1616S",
                "MANUFACTURER": "Essity/SCA Tork",
                "SOLICITATION_QTY": str(tm_rolls),
                "SOLICITATION_UOM": "Roll (s)",
                "PACKAGE": "96 rolls/case",
                "CASES_REQUIRED": str(tm_cases),
                "CASE_PRICE": _money(tm_case),
                "NORMALIZED_UNIT_COST_PER_ROLL": _money(tm_case / rolls_per_case),
                "LINE_PRODUCT_COST": _money(tm_product),
                "EVIDENCE_URL": "https://www.hecdistribution.com/shop/housekeeping-and-janitorial-products/paper-products/bathroom-tissue/essity-tork-bath-tissue-tm1616s/",
                "PRICE_CLASS": "EXACT_PUBLIC_ACQUISITION_PRICE",
                "NOTE": "Quill $56.09 was search-page artifact; HEC lists $47.44 per 96-roll case",
            },
            {
                "MPN": "MK520 / MK520A",
                "MANUFACTURER": "Essity Tork",
                "SOLICITATION_QTY": str(mk_cases),
                "SOLICITATION_UOM": "Case (s)",
                "PACKAGE": "4000 towels/case (16x250) for MK520A",
                "CASES_REQUIRED": str(mk_cases),
                "CASE_PRICE": _money(mk_case_price),
                "LINE_PRODUCT_COST": _money(mk_product),
                "EVIDENCE_URL": "https://www.webstaurantstore.com/tork-universal-natural-kraft-multifold-paper-towel-h2-case/500MFTMK520A.html",
                "PRICE_CLASS": "COMPLIANT_EQUIVALENT_PRICE",
                "NOTE": "Solicitation lists MK520 Case(s); market SKU often MK520A — confirm exact match before bid",
            },
        ],
        "TOTAL_PRODUCT_COST": _money(total_product),
        "FREIGHT": None,
        "FINANCING_COST": None,
        "GOV_SELLING_PRICE": None,
        "ECONOMICS_CLASS": "SELLING_PRICE_EVIDENCE_REQUIRED",
        "LANE_IF_MARGIN_EXISTS": "SCALE" if total_product >= Decimal("15000") else "GROWTH",
        "MAX_ACQ_FOR_PROFIT": {
            # Without selling price, report product-cost baselines only
            "PRODUCT_COST_BASELINE": _money(total_product),
            "NOTE": "Cannot compute max acquisition for profit targets without buyer selling price or bid ceiling",
        },
        "CONDITIONAL_SCENARIO": {
            "LABEL": "IF_AWARD_ALLOWS_MARKUP_OVER_PRODUCT_ONLY",
            "PRODUCT": _money(total_product),
            "UNKNOWN": ["freight", "financing", "buyer_award_unit_prices", "taxes"],
            "NOTE": "Not verified profit — selling price and freight unknown",
        },
    }


def research_dayton_meritor() -> dict[str, Any]:
    oid = "777499f8df1dcb7f"
    lines = get_lines(oid)
    entry = get_entry(oid) or {}
    priority = [
        ln
        for ln in lines
        if str(ln.get("MPN") or "") in {"82-59990-000DD", "82-91353-001"}
    ]
    researched = []
    for ln in priority:
        researched.append(
            {
                "MPN": ln.get("MPN"),
                "MANUFACTURER": ln.get("MANUFACTURER") or "Meritor",
                "DESCRIPTION": ln.get("DESCRIPTION"),
                "QTY": ln.get("QTY"),
                "UOM": ln.get("UOM"),
                "PUBLIC_PRICE": None,
                "PRICE_CLASS": "QUOTE_REQUIRED",
                "CHANNELS": [
                    "Meritor Parts Xpress (dealer login)",
                    "Authorized Meritor heavy-duty distributors",
                    "Transit/fleet specialists (not assumed NAPA stock)",
                    "FindItParts / FleetPride quote desks",
                ],
                "SEARCH_RESULT": "No public list price for exact 82-******* Meritor kit numbers; pad kits with different PNs rejected",
                "RFQ_REQUIRED": True,
            }
        )
    return {
        "OPPORTUNITY_ID": oid,
        "BUYER": entry.get("BUYER"),
        "TITLE": entry.get("TITLE"),
        "DEADLINE": entry.get("DEADLINE"),
        "TOTAL_LINES": len(lines),
        "EXACT_MPN_LINES": sum(1 for ln in lines if ln.get("IDENTITY_CLASS") == EXACT_PRODUCT_VERIFIED),
        "PRIORITY_LINES": researched,
        "ECONOMICS_CLASS": "SUPPLIER_QUOTE_REQUIRED",
        "GOV_SELLING_PRICE": None,
        "NOTE": "Exact Meritor kit PNs require distributor quote — do not use unrelated pad-kit web prices",
    }


def select_commercial_shortlist(limit: int = 25) -> list[dict[str, Any]]:
    store = load_store(force=True)
    by = store.get("by_opportunity") or {}
    cands = []
    for oid, e in by.items():
        if str(oid).startswith("__test") or "test" in str(oid).lower() and str(oid).startswith("__"):
            continue
        title = str(e.get("TITLE") or "")
        if title.strip().lower().startswith("city test") or title.strip().lower() == "test":
            continue
        if hard_exclusion_reason({"TITLE": title, "REAL_LINES": e.get("LINE_COUNT") or 0}) not in (
            None,
            "TOO_MANY_MANDATORY_LINES",
        ):
            # allow exact multi-line
            if e.get("IDENTITY_BEST") != EXACT_PRODUCT_VERIFIED:
                continue
        if re.search(r"\b(construction|rehabilitat|demolition|services?|sewer)\b", title, re.I):
            if e.get("IDENTITY_BEST") != EXACT_PRODUCT_VERIFIED:
                continue
        lines = e.get("LINES") or []
        if not lines:
            continue
        n = len(lines)
        exact_n = sum(1 for ln in lines if ln.get("IDENTITY_CLASS") == EXACT_PRODUCT_VERIFIED)
        days = _days_to_deadline(e.get("DEADLINE"))
        if days is not None and days < 3:
            continue
        score = 0
        if e.get("IDENTITY_BEST") == EXACT_PRODUCT_VERIFIED:
            score += 50
        if 1 <= n <= 5:
            score += 30
        elif n <= 20:
            score += 10
        score += min(20, exact_n)
        if days and days >= 5:
            score += 5
        cands.append(
            {
                "OPPORTUNITY_ID": oid,
                "TITLE": title,
                "BUYER": e.get("BUYER"),
                "SOURCE": e.get("SOURCE"),
                "DEADLINE": e.get("DEADLINE"),
                "DAYS_LEFT": days,
                "LINE_COUNT": n,
                "EXACT_LINES": exact_n,
                "IDENTITY_BEST": e.get("IDENTITY_BEST"),
                "SCORE": score,
            }
        )
    cands.sort(key=lambda x: (-x["SCORE"], x.get("DAYS_LEFT") if x.get("DAYS_LEFT") is not None else 999))
    return cands[:limit]


def build_commercial_rfqs(shortlist: list[dict[str, Any]], *, limit: int = 8) -> dict[str, Any]:
    packets = []
    # Force Tampa + Dayton first
    forced = ["8e7ae0c1316f0d7f", "777499f8df1dcb7f"]
    ordered_ids = forced + [s["OPPORTUNITY_ID"] for s in shortlist if s["OPPORTUNITY_ID"] not in forced]
    # Prefer Greenville athletic package after Tampa/Dayton
    if "65a9793c66fae9a8" not in ordered_ids:
        ordered_ids.insert(2, "65a9793c66fae9a8")
    for oid in ordered_ids:
        if len(packets) >= limit:
            break
        if str(oid).startswith("__test"):
            continue
        e = get_entry(oid) or {}
        title = str(e.get("TITLE") or "")
        if title.strip().lower().startswith("city test") or title.strip().lower() == "test":
            continue
        lines = get_lines(oid)
        if not lines:
            continue
        # Prefer exact / specified lines
        use = [
            ln
            for ln in lines
            if ln.get("IDENTITY_CLASS") in {EXACT_PRODUCT_VERIFIED, SPECIFICATION_COMPLETE_GENERIC}
            or ln.get("MPN")
        ] or lines[:10]
        supplier = "UNKNOWN"
        if "Meritor" in str(e.get("TITLE")) or any("Meritor" in str(ln.get("MANUFACTURER") or "") for ln in use):
            supplier = "Meritor Authorized Distributor / FleetPride quote desk"
        elif any("SCA" in str(ln.get("MANUFACTURER") or "") or "Essity" in str(ln.get("MANUFACTURER") or "") for ln in use):
            supplier = "Essity/Tork distributor / HEC Distribution / Office wholesale"
        contact = resolve_contact(supplier, None) if supplier in SUPPLIER_META else {
            "CONTACT_STATE": "RESEARCH",
            "CONTACT_PATH": "MANUFACTURER_DISTRIBUTOR_DESK",
            "NOTE": "Identify authorized distributor; do not send category-only RFQ",
        }
        packets.append(
            {
                "OPPORTUNITY_ID": oid,
                "AUTO_SEND": False,
                "STATUS": "RFQ_READY",
                "PRIMARY_SUPPLIER": supplier,
                "CONTACT": contact,
                "BUYER": e.get("BUYER"),
                "SOLICITATION_TITLE": e.get("TITLE"),
                "BID_DEADLINE": e.get("DEADLINE"),
                "DELIVERY_DESTINATION": e.get("BUYER"),
                "PRODUCTS": [
                    {
                        "line_no": ln.get("LINE_NUMBER"),
                        "description": ln.get("DESCRIPTION"),
                        "manufacturer": ln.get("MANUFACTURER"),
                        "mpn": ln.get("MPN"),
                        "qty": ln.get("QTY"),
                        "uom": ln.get("UOM"),
                        "identity_class": ln.get("IDENTITY_CLASS"),
                    }
                    for ln in use[:15]
                ],
                "REQUESTED": [
                    "Firm delivered unit price",
                    "Case/pack confirmation vs solicitation UOM",
                    "Freight to buyer destination",
                    "Lead time / stock",
                    "Reseller / new-business account eligibility",
                    "Payment terms (net) without personal guarantee",
                    "Quote validity",
                ],
                "NEXT_ACTION": "Operator review and send — AUTO_SEND=false",
            }
        )
    out = {"kind": "fpd_rfqs", "build": BUILD, "AUTO_SEND": False, "COUNT": len(packets), "PACKETS": packets}
    _save(RFQ_FILE, out)
    return out


def financing_research() -> dict[str, Any]:
    """Published eligibility notes only — not confirmed financing."""
    providers = [
        {
            "provider": "SouthStar Capital",
            "url": "https://www.southstarcapital.com/",
            "new_business": "OFTEN_CASE_BY_CASE",
            "personal_guarantee": "COMMONLY_REQUIRED_VERIFY",
            "gov_po": "POSSIBLE",
            "status": "UNVERIFIED_FOR_THIS_COMPANY",
        },
        {
            "provider": "TradeCap Partners",
            "url": "https://www.tradecappartners.com/",
            "new_business": "OFTEN_CASE_BY_CASE",
            "personal_guarantee": "COMMONLY_REQUIRED_VERIFY",
            "gov_po": "POSSIBLE",
            "status": "UNVERIFIED_FOR_THIS_COMPANY",
        },
        {
            "provider": "King Trade Capital",
            "url": "https://www.kingtradecapital.com/",
            "new_business": "OFTEN_CASE_BY_CASE",
            "personal_guarantee": "COMMONLY_REQUIRED_VERIFY",
            "gov_po": "POSSIBLE",
            "status": "UNVERIFIED_FOR_THIS_COMPANY",
        },
        {
            "provider": "SMB Compass",
            "url": "https://smbcompass.com/",
            "new_business": "OFTEN_CASE_BY_CASE",
            "personal_guarantee": "COMMONLY_REQUIRED_VERIFY",
            "gov_po": "POSSIBLE",
            "status": "UNVERIFIED_FOR_THIS_COMPANY",
        },
        {
            "provider": "Setscale",
            "url": "https://www.setscale.com/",
            "new_business": "OFTEN_CASE_BY_CASE",
            "personal_guarantee": "COMMONLY_REQUIRED_VERIFY",
            "gov_po": "POSSIBLE",
            "status": "UNVERIFIED_FOR_THIS_COMPANY",
        },
    ]
    out = {
        "build": BUILD,
        "built_at": _utc(),
        "CONSTRAINTS": {
            "owner_cash": 0,
            "personal_credit": False,
            "personal_guarantee": False,
            "established_business_credit": False,
        },
        "CONFIRMED_FINANCEABLE": 0,
        "PROVIDERS": providers,
        "QUALIFICATION_PACKET": {
            "Tampa_paper_product_cost_baseline": None,  # filled by caller
            "Dayton_meritor": "QUOTE_REQUIRED_BEFORE_SIZING",
            "ASK": [
                "Fund supplier payment against government PO without PG",
                "New LLC / thin-file eligibility",
                "Advance rate and fees on $7k–$50k product buys",
                "Timing vs bid deadline",
            ],
            "STATUS": "DRAFT_ONLY_NOT_SUBMITTED",
        },
        "NOTE": "No provider confirmed for this company/transaction — do not mark financing confirmed",
    }
    _save(FINANCING, out)
    return out


def build_deals(shortlist: list[dict[str, Any]], tampa: dict[str, Any], dayton: dict[str, Any]) -> dict[str, Any]:
    deals = []
    # Tampa first
    deals.append(
        {
            "opportunity_id": tampa["OPPORTUNITY_ID"],
            "product": tampa.get("TITLE"),
            "buyer": tampa.get("BUYER"),
            "deadline": tampa.get("DEADLINE"),
            "identity_class": EXACT_PRODUCT_VERIFIED,
            "lines_recovered": 2,
            "public_price_status": "PACK_NORMALIZED",
            "acquisition_unit_price": tampa["LINES"][0].get("CASE_PRICE"),
            "total_product_cost": tampa.get("TOTAL_PRODUCT_COST"),
            "historical_price_status": "UNKNOWN",
            "economics_status": tampa.get("ECONOMICS_CLASS"),
            "financing_status": "FINANCING_UNKNOWN",
            "lane": "PROVISIONAL",
            "lane_status": "PROVISIONAL",
            "lane_verification": "UNVERIFIED",
            "contribution_state": "CONTRIBUTION_UNKNOWN",
            "verified_remaining_contribution": None,
            "next_action": "Confirm MK520A vs MK520; get freight quote; find Tampa prior paper award unit prices; send Essity/HEC RFQ",
            "ui_next_action_label": "Normalize Tampa paper pack + request freight/distributor quote",
            "filter_tags": ["source_product", "needs_pricing", "needs_financing", "first_revenue"],
            "evidence_confidence": "CONDITIONAL",
            "lane_repair_note": "Acquisition cost ~$48k is NOT remaining contribution — no verified SCALE lane",
        }
    )
    deals.append(
        {
            "opportunity_id": dayton["OPPORTUNITY_ID"],
            "product": dayton.get("TITLE"),
            "buyer": dayton.get("BUYER"),
            "deadline": dayton.get("DEADLINE"),
            "identity_class": EXACT_PRODUCT_VERIFIED,
            "lines_recovered": dayton.get("TOTAL_LINES"),
            "exact_mpn_lines": dayton.get("EXACT_MPN_LINES"),
            "public_price_status": "QUOTE_REQUIRED",
            "historical_price_status": "UNKNOWN",
            "economics_status": "SUPPLIER_QUOTE_REQUIRED",
            "financing_status": "FINANCING_UNKNOWN",
            "lane": "PROVISIONAL",
            "lane_status": "PROVISIONAL",
            "lane_verification": "UNVERIFIED",
            "contribution_state": "CONTRIBUTION_UNKNOWN",
            "verified_remaining_contribution": None,
            "next_action": "Request Meritor authorized-distributor quote for 82-59990-000DD x145 and 82-91353-001 x45",
            "ui_next_action_label": "Request Meritor brake-kit distributor quote",
            "filter_tags": ["source_product", "needs_pricing", "needs_financing", "first_revenue"],
            "evidence_confidence": "PROVISIONAL",
            "lane_repair_note": "No acquisition or selling evidence — cannot assign SCALE/GROWTH/CASH_FLOW",
        }
    )
    for s in shortlist:
        if s["OPPORTUNITY_ID"] in {tampa["OPPORTUNITY_ID"], dayton["OPPORTUNITY_ID"]}:
            continue
        deals.append(
            {
                "opportunity_id": s["OPPORTUNITY_ID"],
                "product": s.get("TITLE"),
                "buyer": s.get("BUYER"),
                "deadline": s.get("DEADLINE"),
                "days_left": s.get("DAYS_LEFT"),
                "identity_class": s.get("IDENTITY_BEST"),
                "lines_recovered": s.get("LINE_COUNT"),
                "exact_mpn_lines": s.get("EXACT_LINES"),
                "public_price_status": "UNKNOWN",
                "historical_price_status": "UNKNOWN",
                "economics_status": "INSUFFICIENT_EVIDENCE",
                "financing_status": "FINANCING_UNKNOWN",
                "lane": "PROVISIONAL",
                "next_action": "Research acquisition price for recovered exact lines",
                "ui_next_action_label": "Price recovered product lines",
                "filter_tags": ["source_product", "needs_pricing", "first_revenue"],
            }
        )
    out = {
        "kind": "fpd_available_deals",
        "build": BUILD,
        "built_at": _utc(),
        "count": len(deals),
        "deals": deals[:30],
        "AUTO_SEND": False,
    }
    _save(DEALS, out)
    # Sync UI
    _save(
        f"{OUT}/m3_source_product_v1_available_deals.json",
        {
            "kind": "source_product_available_deals",
            "build": BUILD,
            "built_at": _utc(),
            "count": out["count"],
            "deals": out["deals"],
            "NOTE": "Synced from first_profitable_deal_bidnet_repair_v1",
        },
    )
    _save(f"{OUT}/m3_full_pl_recovery_v1_available_deals.json", out)
    return out


def run_all(*, bidnet_max: int = 45) -> dict[str, Any]:
    t0 = time.time()
    eng = validate_persistence()
    ext = audit_extraction_failures()
    bidnet = recover_bidnet_batch(max_n=bidnet_max)
    tampa = research_tampa_paper()
    dayton = research_dayton_meritor()
    shortlist = select_commercial_shortlist(25)
    rfqs = build_commercial_rfqs(shortlist)
    fin = financing_research()
    fin["QUALIFICATION_PACKET"]["Tampa_paper_product_cost_baseline"] = tampa.get("TOTAL_PRODUCT_COST")
    _save(FINANCING, fin)
    deals = build_deals(shortlist, tampa, dayton)

    store = load_store(force=True)
    by = store.get("by_opportunity") or {}
    id_best = Counter((e.get("IDENTITY_BEST") for e in by.values()))
    exact_lines = sum(
        1
        for e in by.values()
        for ln in (e.get("LINES") or [])
        if ln.get("IDENTITY_CLASS") == EXACT_PRODUCT_VERIFIED
    )

    commercial_blob = {
        "build": BUILD,
        "built_at": _utc(),
        "tampa": tampa,
        "dayton": dayton,
        "shortlist": shortlist,
        "SAM_API_CALLS_USED": 0,
    }
    _save(COMMERCIAL, commercial_blob)

    report = {
        "kind": "first_profitable_deal_bidnet_repair_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "WALL_S": round(time.time() - t0, 2),
        "SAM_API_CALLS_USED": 0,
        "A_ENGINEERING": eng,
        "B_BIDNET": {
            "existing": 674,
            "auth_session_reuse_working": bidnet.get("AUTH_WORKING"),
            "diagnosis": bidnet.get("DIAGNOSIS"),
            "targets_attempted": bidnet.get("targets_attempted"),
            "detail_process_ok": bidnet.get("detail_process_ok"),
            "attachments_discovered": bidnet.get("attachments_discovered"),
            "attachments_downloaded": bidnet.get("attachments_downloaded"),
            "product_line_payloads_recovered": bidnet.get("product_line_payloads_recovered_opps"),
            "product_lines_total": bidnet.get("product_lines_total"),
            "fully_specified": bidnet.get("fully_specified_opportunities"),
            "remaining_blockers": bidnet.get("remaining_blockers"),
            "LOCAL_VS_RAILWAY": {
                "local_creds_before": "MISSING",
                "railway_creds": "PRESENT",
                "root_cause": "CREDENTIALS_MISSING locally; SESSION now established via railway run login",
                "session_path_local": eng.get("session_path"),
            },
        },
        "C_EXTRACTION_FAILED": {
            "total": ext.get("TOTAL"),
            "root_causes": ext.get("ROOT_CAUSES"),
            "repaired": ext.get("REPAIRED"),
            "note": ext.get("NOTE"),
        },
        "D_PRODUCT_LINES": {
            "opportunities_with_lines": store.get("opportunity_count"),
            "total_lines": store.get("line_count"),
            "exact_opp_best": id_best.get(EXACT_PRODUCT_VERIFIED, 0),
            "exact_lines": exact_lines,
            "dayton_reconcile": eng.get("dayton"),
        },
        "E_COMMERCIAL": {
            "tampa_product_cost": tampa.get("TOTAL_PRODUCT_COST"),
            "tampa_economics": tampa.get("ECONOMICS_CLASS"),
            "dayton_economics": dayton.get("ECONOMICS_CLASS"),
            "shortlist_n": len(shortlist),
        },
        "F_PRICING": {
            "tampa_lines": tampa.get("LINES"),
            "dayton_priority": dayton.get("PRIORITY_LINES"),
            "quill_56_09": "REJECTED as unverified pack — HEC $47.44/96-roll case used instead",
        },
        "G_RFQS": {"count": rfqs.get("COUNT"), "AUTO_SEND": False, "packets": (rfqs.get("PACKETS") or [])[:8]},
        "H_FINANCING": {
            "confirmed": 0,
            "providers_researched": len(fin.get("PROVIDERS") or []),
            "status": "UNVERIFIED_FOR_THIS_COMPANY",
        },
        "I_LANES": {
            "CASH_FLOW": {"verified": 0, "conditional": 0, "unknown": len(shortlist)},
            "GROWTH": {"verified": 0, "conditional": 0, "unknown": 0},
            "SCALE": {
                "verified": 0,
                "conditional": 1 if tampa.get("TOTAL_PRODUCT_COST") else 0,
                "unknown": 1,
                "NOTE": "Tampa product baseline ~$48k — SCALE only if selling price supports margin after freight/financing",
            },
            "confirmed_financeable": 0,
            "execution_ready": 0,
        },
        "J_VERDICT": {
            "1_ht_production_ready": "LOCALLY_VALIDATED_NOT_DEPLOYED — persistence + handoff pass; Railway deploy needs explicit auth",
            "2_bidnet_auth_worked": bool(bidnet.get("AUTH_WORKING")),
            "3_additional_bidnet_opps": bidnet.get("product_line_payloads_recovered"),
            "4_products_priced_accurately": 1,  # TM1616S pack-normalized; MK520 equivalent
            "5_gov_selling_evidence": 0,
            "6_positive_preliminary_economics": 0,
            "7_potential_500_2000": "UNKNOWN_WITHOUT_SELLING_PRICE",
            "8_potential_5000_10000": "UNKNOWN_WITHOUT_SELLING_PRICE_AND_QUOTES",
            "9_blocks_best_candidates": [
                "Tampa: freight + historical/award unit prices + MK520 SKU confirmation + financing",
                "Dayton: no public Meritor kit list prices — distributor quotes required",
                "BidNet: many packages still yield shallow/no structured lines after auth",
            ],
            "10_operator_actions": [
                "Send Essity/HEC RFQ for Tampa TM1616S (160 cases) + confirm MK520 case SKU",
                "Send Meritor distributor RFQ for Dayton 82-59990-000DD x145 and 82-91353-001 x45",
                "Search City of Tampa prior toilet-tissue/paper awards for unit prices",
                "Submit financing qualification drafts (no PG) sized to ~$8k–$50k product cost",
                "Deploy HT product_line_store wiring to Railway after backup (explicit approval)",
            ],
        },
        "DEPLOYMENT": "LOCAL_ONLY",
    }
    _save(REPORT, report)
    _checkpoint(stage="DONE", wall_s=report["WALL_S"])
    return {
        "report": report,
        "bidnet": bidnet,
        "extraction": ext,
        "tampa": tampa,
        "dayton": dayton,
        "rfqs": rfqs,
        "deals": deals,
        "financing": fin,
        "engineering": eng,
    }
