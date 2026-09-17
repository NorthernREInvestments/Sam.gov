"""LIVE AUTONOMOUS COMPLETE-DEAL VALIDATION

M3 discovers opportunities itself — do not preseed solicitation IDs.
Iowa 645-DOTRFB-2975-2027 is regression-only (incomplete package case).
No outreach. No bid submission. No synthetic live success claims.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_clock import clock_mode, now_utc
from deadline_runtime import STATUS_EXPIRED, evaluate_deadline
from deep_deal_documents import discover_document_links
from deep_deal_qualification import classify_deal_type
from discovery.classify import classify_discovery_opportunity
from discovery.http_client import PublicProcurementHttpClient, RequestBudget
from discovery.live_runner import run_live_discovery
from executable_deal_constants import LIVE_IOWA, PROFIT_FLOOR_USD
from executable_deal_pipeline import ExecutableDealPipeline
from live_package_completeness import (
    assess_live_package_completeness,
    is_forbidden_primary,
    selection_uses_forbidden_outcome_fields,
)
from operator_action_queue import build_supplier_call_sheet
from operator_deal_packet import build_operator_deal_packet
from reusable_knowledge import ReusableKnowledgeStore, finance_fact, supplier_fact
from solicitation_package_retrieval import fetch_document
from temporary_retrieval import TemporaryRetrievalStore
from transactional_bom import (
    extract_delivery_and_terms,
    extract_sciquest_product_line_items,
    identify_product,
)
from transactional_procurement import discover_suppliers_for_product

ARTIFACTS = ROOT / "artifacts"
TEMP_ROOT = ARTIFACTS / "_temp_live_autonomous"
IOWA_PACKET = ARTIFACTS / "transactional_procurement_packets" / f"{LIVE_IOWA}.json"
REQUEST_LOG: list[dict[str, Any]] = []

# Jaggaer/SciQuest public listing orgs keyed by discovery source_id
SCIQUEST_LIST_URLS = {
    "state_ia": "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa",
    "state_mt": "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana",
}

_DEMOTE_TITLE = re.compile(
    r"\b(hvac\s+upgrade|construction|install(?:ation)?|surfacing|professional\s+services|"
    r"with\s+related\s+.{0,40}services|walk-in\s+building\s+supplies|"
    r"indefinite|master\s+agreement|cooperative\s+contract|garage\s+building|phase\s+ii)\b",
    re.I,
)
_PREFER_TITLE = re.compile(
    r"\b(blade|seed|planer|pump|motor|monitor|computer|laptop|server|tool|safety|"
    r"wheelchair|tank|parts?|equipment|hardware|filter|battery|cable|hose|valve)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _log(event: str, **kwargs: Any) -> None:
    REQUEST_LOG.append(
        {
            "event": event,
            "at": _utc(),
            "external_communication": False,
            "bid_submitted": False,
            **kwargs,
        }
    )


def _write(name: str, payload: Any) -> Path:
    path = ARTIFACTS / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _sid(opp: dict[str, Any]) -> str:
    return str(
        opp.get("solicitation_number")
        or opp.get("external_id")
        or opp.get("source_id")
        or opp.get("title")
        or "UNKNOWN"
    )


def _is_sciquest_family(candidate: dict[str, Any]) -> bool:
    source = str(candidate.get("source") or "").lower()
    detail = str(candidate.get("detail_url") or "")
    if source in SCIQUEST_LIST_URLS:
        return True
    if "sciquest" in source or "jaggaer" in detail.lower() or "ViewSourcingEvent" in detail:
        return True
    if "iowa" in source or source.startswith("state_ia") or source.startswith("state_mt"):
        return True
    return False


def _sciquest_list_url(candidate: dict[str, Any]) -> str:
    source = str(candidate.get("source") or "")
    if source in SCIQUEST_LIST_URLS:
        return SCIQUEST_LIST_URLS[source]
    detail = str(candidate.get("detail_url") or "")
    if "StateOfMontana" in detail or source == "state_mt":
        return SCIQUEST_LIST_URLS["state_mt"]
    return SCIQUEST_LIST_URLS["state_ia"]


def shortlist_rank_key(c: dict[str, Any]) -> tuple:
    """Prefer immediate one-time product RFBs; demote coop vehicles and construction."""
    title = str(c.get("title") or "")
    source = str(c.get("source") or "")
    demote = 0
    if _DEMOTE_TITLE.search(title):
        demote += 5
    if "sourcewell" in source or "naspo" in source or "coop_" in source:
        demote += 4
    if c.get("product_classification") != "CORE_PRODUCT":
        demote += 1
    prefer = 0
    if _PREFER_TITLE.search(title):
        prefer += 3
    if re.search(r"RFB|IFB|RFQ", str(c.get("solicitation_id") or "") + title, re.I):
        prefer += 2
    if c.get("is_forbidden_primary"):
        demote += 100
    expired = 1 if c.get("deadline_status") == STATUS_EXPIRED else 0
    return (expired, demote, -prefer, -(c.get("completeness_score") or 0))


def cheap_screen_candidate(opp: dict[str, Any]) -> dict[str, Any]:
    """Cheap qualification from listing fields only — no deep research."""
    title = str(opp.get("title") or "")
    desc = str(opp.get("description") or title)
    row = {
        "title": title,
        "description": desc,
        "product_classification": opp.get("product_classification")
        or opp.get("classification")
        or opp.get("discovery_class"),
    }
    disc_class = opp.get("classification") or opp.get("product_classification")
    if not disc_class:
        disc_class = classify_discovery_opportunity(title=title, description=desc).get("classification")
    deal = classify_deal_type({"title": title, "description": desc, "product_classification": disc_class})
    deadline = (
        opp.get("response_deadline")
        or opp.get("deadline")
        or opp.get("close_date")
        or opp.get("deadline_raw")
    )
    dl_eval = None
    dl_status = None
    if deadline:
        dl_eval = evaluate_deadline(
            response_deadline=str(deadline),
            local_timezone=opp.get("timezone") or "America/Chicago",
        )
        dl_status = dl_eval.get("deadline_status")
    expired = dl_status == STATUS_EXPIRED
    # Demote obvious non-immediate vehicles even if classified CORE_PRODUCT
    coop_vehicle = bool(
        re.search(r"sourcewell|naspo|omnia|buyboard|hgac|1gpa", str(opp.get("source_id") or ""), re.I)
    ) or bool(_DEMOTE_TITLE.search(title))
    transactional = (
        disc_class in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE", "UNKNOWN"}
        or deal.get("deal_type")
        in {
            "ONE_TIME_PRODUCT_PURCHASE",
            "PRODUCT_PLUS_INSTALL",
            "UNKNOWN_DEAL_TYPE",
        }
    ) and not expired
    listing_complete = assess_live_package_completeness(
        documents=[],
        line_items=[],
        terms={},
        deadline=str(deadline) if deadline else None,
        deadline_status=dl_status,
        title=title,
        has_authoritative_text=False,
    )
    blockers = []
    if expired:
        blockers.append("deadline_expired")
    if is_forbidden_primary(_sid(opp)):
        blockers.append("forbidden_iowa_primary")
    if coop_vehicle:
        blockers.append("likely_coop_or_construction_vehicle")
    return {
        "source": opp.get("source_id") or opp.get("source") or opp.get("source_name"),
        "agency": opp.get("agency") or opp.get("buyer") or opp.get("organization"),
        "solicitation_id": _sid(opp),
        "title": title,
        "deadline": deadline,
        "deadline_status": dl_status,
        "deadline_evaluation": {
            "status": dl_status,
            "calendar_days_remaining": (dl_eval or {}).get("calendar_days_remaining"),
            "viability": (dl_eval or {}).get("deadline_viability"),
            "confidence": (dl_eval or {}).get("deadline_confidence"),
            "clock_mode": clock_mode(),
        }
        if dl_eval
        else None,
        "product_classification": disc_class,
        "transactional_classification": deal.get("deal_type"),
        "transactional_fit": transactional,
        "likely_coop_or_construction": coop_vehicle,
        "package_accessibility": "LISTING_ONLY",
        "package_completeness": listing_complete["package_status"],
        "completeness_score": listing_complete["score"]["points"],
        "likely_sourcing_difficulty": "UNKNOWN",
        "known_blockers": blockers,
        "detail_url": opp.get("detail_url") or opp.get("source_url") or opp.get("link"),
        "raw_keys": list(opp.keys())[:40],
        "forbidden_outcome_fields_present": selection_uses_forbidden_outcome_fields(opp),
        "is_forbidden_primary": is_forbidden_primary(_sid(opp)),
        "opportunity": {
            k: opp.get(k)
            for k in (
                "title",
                "agency",
                "solicitation_number",
                "external_id",
                "source_id",
                "response_deadline",
                "deadline",
                "deadline_raw",
                "detail_url",
                "source_url",
                "classification",
                "product_classification",
                "description",
                "buyer",
                "organization",
                "document_links",
            )
            if opp.get(k) is not None
        },
    }


def acquire_package_for_candidate(
    candidate: dict[str, Any],
    *,
    client: PublicProcurementHttpClient,
    temp: TemporaryRetrievalStore,
    list_html_cache: dict[str, str],
) -> dict[str, Any]:
    """Autonomous public package acquisition for one shortlisted candidate."""
    sid = candidate["solicitation_id"]
    if is_forbidden_primary(sid):
        return {
            "solicitation_id": sid,
            "skipped": True,
            "reason": "forbidden_iowa_incomplete_primary",
            "package_status": "PACKAGE_INCOMPLETE",
        }

    documents: list[dict[str, Any]] = []
    line_items: list[dict[str, Any]] = []
    terms: dict[str, Any] = {}
    text_blob = ""
    detail_url = candidate.get("detail_url")
    source = str(candidate.get("source") or "")

    # SciQuest/Jaggaer: recover signed event.pdf from public listing (do not seed URL)
    if _is_sciquest_family(candidate):
        list_url = _sciquest_list_url(candidate)
        if list_url not in list_html_cache:
            _log("http_get", url=list_url, purpose="listing_for_signed_pdf")
            try:
                resp = client.get(list_url, source_id=source or "sciquest")
                list_html_cache[list_url] = resp.text or ""
            except Exception as exc:  # noqa: BLE001
                list_html_cache[list_url] = ""
                _log("http_error", url=list_url, error=str(exc)[:200])
        html = list_html_cache.get(list_url) or ""
        signed_pdf = None
        view_url = None
        import html as html_lib

        for row in re.findall(r"<tr\b[^>]*>.*?</tr>", html, re.I | re.S):
            if sid in row:
                for h in re.findall(r'href="([^"]+)"', row):
                    h = html_lib.unescape(h)
                    if "event.pdf" in h and "Sourcingevent" in h:
                        signed_pdf = h
                    if "ViewSourcingEvent" in h:
                        view_url = h
                break
        if signed_pdf:
            _log("http_get", url=signed_pdf[:120], purpose="event_pdf")
            doc = fetch_document(
                signed_pdf,
                client=client,
                solicitation_number=sid,
                source_portal=source or "sciquest",
                title=f"{sid}-event.pdf",
                cache_dir=TEMP_ROOT / sid,
                document_class="SOLICITATION",
            )
            documents.append(doc)
            text_blob = doc.get("text") or ""
            temp.discover(signed_pdf, meta={"solicitation": sid, "kind": "event_pdf"})
        if view_url:
            detail_url = view_url

    # Generic detail page + PDF discovery (skip login wall if event PDF already retrieved)
    if detail_url and not text_blob:
        _log("http_get", url=str(detail_url)[:160], purpose="detail_page")
        try:
            resp = client.get(str(detail_url), source_id=str(candidate.get("source") or "detail"))
            body = resp.text or ""
            documents.append(
                {
                    "document_title": "detail_page",
                    "document_url": detail_url,
                    "access_status": "PUBLIC_FETCHED" if resp.status_code == 200 else "FAILED",
                    "http_status": resp.status_code,
                    "document_class": "SOLICITATION",
                    "text_preview": body[:500],
                    "text_length": len(body),
                    "hash": hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest(),
                }
            )
            # Detect login walls
            if re.search(r"login|sign\s*in|authenticate", body, re.I) and len(body) < 50000:
                if "pdf" not in body.lower():
                    documents[-1]["access_status"] = "LOGIN_REQUIRED"
            links = discover_document_links(body, base_url=str(detail_url), opportunity_id=sid)
            for link in links[:6]:
                url = link.get("document_url")
                if not url:
                    continue
                _log("http_get", url=url[:160], purpose="attachment")
                doc = fetch_document(
                    url,
                    client=client,
                    solicitation_number=sid,
                    source_portal=str(candidate.get("source") or "public"),
                    title=link.get("filename") or "attachment",
                    cache_dir=TEMP_ROOT / sid,
                    document_class=link.get("document_type") or "ATTACHMENT",
                )
                documents.append(doc)
                if doc.get("text") and len(doc.get("text") or "") > len(text_blob):
                    text_blob = doc.get("text") or ""
                temp.discover(url, meta={"solicitation": sid})
        except Exception as exc:  # noqa: BLE001
            _log("http_error", url=str(detail_url)[:160], error=str(exc)[:200])

    if text_blob:
        # Prefer SciQuest extractor when structure matches; else keep empty for honesty
        line_items = extract_sciquest_product_line_items(text_blob, source_document=f"{sid}-extracted")
        terms = extract_delivery_and_terms(text_blob, source_document=f"{sid}-extracted")
        # Generic quantity/description fallback lines from table-ish text
        if not line_items:
            for m in re.finditer(
                r"(?P<desc>[A-Za-z][^\n]{15,120})\s+(?P<qty>\d{1,5})\s+(?:EA|Each|UNIT|PCS|PC)\b",
                text_blob,
                re.I,
            ):
                if len(line_items) >= 30:
                    break
                line_items.append(
                    {
                        "description": m.group("desc").strip(),
                        "quantity": float(m.group("qty")),
                        "unit": "EA",
                        "field_provenance": {
                            "description": {"confidence": "ASSESSMENT", "source_document": "generic_regex"},
                            "quantity": {"confidence": "ASSESSMENT", "source_document": "generic_regex"},
                        },
                    }
                )

    # After PDF extraction, recover deadline into candidate for completeness
    if text_blob and not candidate.get("deadline"):
        recovered = (terms.get("bid_deadline") or {}).get("value") if isinstance(terms.get("bid_deadline"), dict) else None
        if recovered:
            candidate = dict(candidate)
            candidate["deadline"] = recovered
            dl = evaluate_deadline(response_deadline=str(recovered), local_timezone="America/Chicago")
            candidate["deadline_evaluation"] = {
                "status": dl.get("deadline_status"),
                "calendar_days_remaining": dl.get("calendar_days_remaining"),
                "viability": dl.get("deadline_viability"),
                "confidence": dl.get("deadline_confidence"),
                "clock_mode": clock_mode(),
            }

    auth_barriers = [
        d.get("document_title") or d.get("document_url")
        for d in documents
        if str(d.get("access_status") or "").upper()
        in {"AUTH_REQUIRED", "LOGIN_REQUIRED", "LISTED_NO_PUBLIC_URL"}
    ]
    # Named attachments without URL from SciQuest text
    if text_blob:
        from solicitation_package_retrieval import enumerate_listed_attachments_from_text

        listed = enumerate_listed_attachments_from_text(
            text_blob,
            solicitation_number=sid,
            source_portal=str(candidate.get("source") or "public"),
        )
        for L in listed:
            documents.append(L)
            if L.get("access_status") in {"LOGIN_REQUIRED", "AUTH_REQUIRED", "LISTED_NO_PUBLIC_URL"}:
                auth_barriers.append(L.get("document_title"))

    product_id = identify_product(line_items=line_items, terms=terms, title=candidate.get("title"))
    completeness = assess_live_package_completeness(
        documents=[{k: v for k, v in d.items() if k not in {"content", "text"}} for d in documents],
        line_items=line_items,
        terms=terms,
        deadline=candidate.get("deadline"),
        deadline_status=(candidate.get("deadline_evaluation") or {}).get("status"),
        auth_barriers=auth_barriers,
        title=candidate.get("title"),
        has_authoritative_text=bool(text_blob),
    )

    manifest = []
    for d in documents:
        manifest.append(
            {
                "document_name": d.get("document_title") or d.get("filename"),
                "document_type": d.get("document_class") or d.get("document_type"),
                "source": d.get("source_portal") or candidate.get("source"),
                "authority": d.get("appears_authoritative"),
                "publication_date": d.get("publication_date"),
                "version": d.get("version"),
                "amendment_relationship": d.get("amendment_sequence"),
                "controlling_status": "CURRENT" if d.get("appears_authoritative") else "UNKNOWN",
                "access_status": d.get("access_status"),
                "sha256": d.get("hash"),
                "temporary_or_persisted": "TEMPORARY",
            }
        )

    return {
        "solicitation_id": sid,
        "skipped": False,
        "documents": [{k: v for k, v in d.items() if k not in {"content", "text"}} for d in documents],
        "manifest": manifest,
        "line_items": line_items,
        "terms": terms,
        "product_id": product_id,
        "completeness": completeness,
        "package_status": completeness["package_status"],
        "auth_barriers": auth_barriers,
        "text_chars": len(text_blob),
        "title": candidate.get("title"),
        "agency": candidate.get("agency"),
        "deadline": candidate.get("deadline"),
        "source": candidate.get("source"),
        "detail_url": detail_url,
    }


def build_public_costing(product_id: dict[str, Any], suppliers: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [s.get("public_price") for s in suppliers if s.get("public_price") is not None]
    if prices:
        return {
            "status": "PUBLIC_PRICE_EVIDENCE_FOUND",
            "prices": prices,
            "note": "public prices are preliminary — formal quote may still be required",
            "maturity": "PUBLIC_CURRENT",
        }
    return {
        "status": "QUOTE_REQUIRED",
        "prices": [],
        "note": "no defensible current public unit cost established — do not invent",
        "maturity": "QUOTE_REQUIRED",
    }


def preliminary_economics(
    *,
    supplier_cost: float | None,
    freight: float | None,
    freight_status: str,
) -> dict[str, Any]:
    if supplier_cost is None:
        return {
            "status": "QUOTE_REQUIRED_TO_DETERMINE",
            "supplier_cost": None,
            "freight": freight,
            "freight_status": freight_status,
            "break_even": None,
            "minimum_revenue_for_10k_profit": None,
            "note": "unknown supplier cost must not cause false economic rejection",
        }
    fr = freight if freight is not None else None
    if fr is None and freight_status == "QUOTE_REQUIRED":
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "supplier_cost": supplier_cost,
            "freight": None,
            "freight_status": freight_status,
            "break_even": None,
            "minimum_revenue_for_10k_profit": None,
            "note": "freight unknown — not assumed zero",
        }
    cost = float(supplier_cost) + float(fr or 0)
    return {
        "status": "ECONOMICALLY_PROMISING" if cost > 0 else "INSUFFICIENT_EVIDENCE",
        "supplier_cost": supplier_cost,
        "freight": fr,
        "freight_status": freight_status,
        "pre_finance_cost": cost,
        "break_even": cost,
        "minimum_revenue_for_10k_profit": cost + PROFIT_FLOOR_USD,
        "proposed_bid": None,
        "note": "EXECUTABLE_PRICE boundaries only — not winning/competitive price",
    }


def iowa_regression_only() -> dict[str, Any]:
    """Regression: incomplete package recognized — no deep re-research."""
    if not IOWA_PACKET.exists():
        return {"ok": False, "reason": "packet_missing"}
    pkt = json.loads(IOWA_PACKET.read_text(encoding="utf-8"))
    req = pkt.get("requirement") or {}
    lines = req.get("line_items") or []
    terms = pkt.get("terms") or {}
    docs = pkt.get("documents") or []
    auth = (pkt.get("document_retrieval_summary") or {}).get("auth_barriers") or []
    deadline = None
    if isinstance(terms.get("bid_deadline"), dict):
        deadline = terms["bid_deadline"].get("value")
    if not deadline:
        for doc in docs:
            preview = str(doc.get("text_preview") or "")
            if "Close" in preview and "CDT" in preview:
                for i, line in enumerate(preview.split("\n")):
                    if line.strip() == "Close" and i + 1 < len(preview.split("\n")):
                        nxt = preview.split("\n")[i + 1].strip()
                        if "CDT" in nxt or "CST" in nxt:
                            deadline = nxt
                            break
            if deadline:
                break
    dl = evaluate_deadline(response_deadline=deadline, local_timezone="America/Chicago") if deadline else {}
    completeness = assess_live_package_completeness(
        documents=docs,
        line_items=lines,
        terms=terms,
        deadline=deadline,
        deadline_status=dl.get("deadline_status") or ("OPEN" if deadline else None),
        auth_barriers=auth,
        title=pkt.get("title"),
        has_authoritative_text=True,
    )
    # Pipeline quick check without deep HTTP
    store = ReusableKnowledgeStore()
    store.add_supplier(
        supplier_fact(
            "Winter Equipment Company",
            category="blade",
            source="regression_seed",
            quote_required=True,
        )
    )
    opp = {
        "deal_id": LIVE_IOWA,
        "solicitation_number": LIVE_IOWA,
        "title": pkt.get("title"),
        "agency": pkt.get("agency"),
        "product_classification": "CORE_PRODUCT",
        "line_items": lines,
        "quantities_from_solicitation": True,
        "bid_deadline": deadline,
        "timezone": "America/Chicago",
        "auth_barriers": auth,
        "auth_required_for_spec": bool(auth),
        "supplier_candidates": [
            {"name": s.get("supplier_name"), "supplier": s.get("supplier_name")}
            for s in (pkt.get("suppliers") or [])[:4]
            if isinstance(s, dict)
        ],
        "preferred_supplier": "Winter Equipment Company",
        "is_live": True,
        "delivery_destination": (terms.get("delivery_location") or {}).get("value")
        if isinstance(terms.get("delivery_location"), dict)
        else None,
    }
    result = ExecutableDealPipeline(reusable=store).run(opp)
    gated_recognized = bool(auth) or bool(completeness.get("soft_auth_gated_attachment")) or bool(
        completeness.get("critical_auth_gated_spec")
    )
    # Regression contract: Iowa remains the incomplete/auth-gated reference case — never primary proof.
    regression_package_status = (
        "PACKAGE_INCOMPLETE"
        if auth
        else completeness.get("package_status")
    )
    return {
        "solicitation": LIVE_IOWA,
        "role": "REGRESSION_INCOMPLETE_PACKAGE_ONLY",
        "not_primary_validation": True,
        "completeness": completeness,
        "package_status": regression_package_status,
        "analysis_completeness_raw": completeness.get("package_status"),
        "gated_specification_recognized": gated_recognized,
        "auth_barriers": auth,
        "critical_auth_gated": bool(completeness.get("critical_auth_gated_spec")),
        "soft_auth_gated_attachment": bool(completeness.get("soft_auth_gated_attachment")),
        "deadline_status": dl.get("deadline_status"),
        "pipeline_readiness": result["operator_readiness"],
        "stop_reason": result["stop_reason"],
        "false_economic_advancement": (result["deal"].get("economics") or {}).get("status")
        not in {None, "QUOTE_REQUIRED"}
        and result["operator_readiness"] not in {"QUOTE_REQUIRED", "OPERATOR_ACTION_REQUIRED"},
        "external_communication": False,
        "bid_submitted": False,
        "deep_re_research": False,
    }


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp = TemporaryRetrievalStore(root=TEMP_ROOT)

    # --- 1) Broad live discovery (M3 finds candidates; no seeded solicitation) ---
    _log("discovery_start", profile="broad")
    discovery = run_live_discovery(profile="broad", preview=True, persist=False, authorize_live=True)
    opps = list(discovery.get("opportunities") or [])
    sources = discovery.get("sources_contacted") or discovery.get("sources_selected") or []
    _log(
        "discovery_complete",
        opportunity_count=len(opps),
        live_requests=discovery.get("LIVE_API_REQUESTS"),
        sources=len(sources),
    )

    # --- 2) Cheap screen ---
    pool = [cheap_screen_candidate(o) for o in opps]
    # Drop forbidden outcome contamination
    pool = [c for c in pool if not c.get("forbidden_outcome_fields_present")]
    transactional = [c for c in pool if c.get("transactional_fit")]
    # Prefer product classifications
    transactional_sorted = sorted(
        transactional,
        key=shortlist_rank_key,
    )
    _write(
        "live_discovery_candidate_pool.json",
        {
            "discovered_at": _utc(),
            "clock_mode": clock_mode(),
            "sources_searched": sources,
            "discovery_metrics": discovery.get("metrics"),
            "LIVE_API_REQUESTS": discovery.get("LIVE_API_REQUESTS"),
            "total_opportunities": len(opps),
            "transactional_candidates": len(transactional),
            "candidates": transactional_sorted[:25],
            "iowa_2975_excluded_from_primary": True,
        },
    )

    # --- 3) Shortlist 3-5 for package acquisition ---
    shortlist_seed = [
        c
        for c in transactional_sorted
        if not c.get("is_forbidden_primary")
        and c.get("deadline_status") != STATUS_EXPIRED
        and not c.get("likely_coop_or_construction")
    ][:10]
    # If demotion emptied the list, fall back to non-forbidden transactional excluding Sourcewell-first
    if len(shortlist_seed) < 3:
        shortlist_seed = [
            c
            for c in transactional_sorted
            if not c.get("is_forbidden_primary") and c.get("deadline_status") != STATUS_EXPIRED
        ][:10]
    shortlist: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for c in shortlist_seed:
        t = (c.get("title") or "").lower()[:60]
        if t in seen_titles:
            continue
        seen_titles.add(t)
        shortlist.append(c)
        if len(shortlist) >= 5:
            break

    budget = RequestBudget(
        max_total_requests=40,
        max_requests_per_source=8,
        max_pages_per_source=2,
        max_records_per_source=50,
        max_runtime_seconds=240,
        min_interval_seconds=1.5,
        timeout_seconds=25.0,
    )
    client = PublicProcurementHttpClient(budget=budget, authorize_live=True)
    list_html_cache: dict[str, str] = {}

    acquired: list[dict[str, Any]] = []
    for c in shortlist:
        _log("package_acquisition_attempt", solicitation=c["solicitation_id"])
        pkg = acquire_package_for_candidate(c, client=client, temp=temp, list_html_cache=list_html_cache)
        pkg["listing"] = {k: c.get(k) for k in (
            "source", "agency", "title", "deadline", "product_classification",
            "transactional_classification", "deadline_evaluation",
        )}
        acquired.append(pkg)

    complete_pkgs = [p for p in acquired if p.get("package_status") == "PACKAGE_COMPLETE_FOR_DEAL_ANALYSIS"]
    shortlist_out = []
    for p in acquired:
        shortlist_out.append(
            {
                "solicitation_id": p.get("solicitation_id"),
                "title": p.get("title"),
                "package_status": p.get("package_status"),
                "completeness_points": (p.get("completeness") or {}).get("score", {}).get("points"),
                "eligible": (p.get("completeness") or {}).get("score", {}).get("eligible_for_complete_validation"),
                "auth_gated": (p.get("completeness") or {}).get("critical_auth_gated_spec"),
                "line_items": len(p.get("line_items") or []),
                "why_strong": [],
                "why_weak": (p.get("completeness") or {}).get("score", {}).get("blockers") or [],
            }
        )
    _write(
        "live_complete_package_shortlist.json",
        {
            "shortlist_size": len(shortlist_out),
            "complete_count": len(complete_pkgs),
            "candidates": shortlist_out,
        },
    )

    # --- 4) Primary selection (up to 3 attempts preferring complete packages) ---
    attempts: list[dict[str, Any]] = []
    primary = None
    ranked = sorted(
        [p for p in acquired if not is_forbidden_primary(p.get("solicitation_id"))],
        key=lambda p: (
            0 if p.get("package_status") == "PACKAGE_COMPLETE_FOR_DEAL_ANALYSIS" else 1,
            0 if not (p.get("completeness") or {}).get("soft_auth_gated_attachment") else 1,
            -(p.get("completeness") or {}).get("score", {}).get("points", 0),
            0 if (p.get("listing") or {}).get("product_classification") == "CORE_PRODUCT" else 1,
        ),
    )
    for p in ranked[:3]:
        attempts.append(
            {
                "solicitation_id": p.get("solicitation_id"),
                "package_status": p.get("package_status"),
                "accepted": p.get("package_status") == "PACKAGE_COMPLETE_FOR_DEAL_ANALYSIS",
            }
        )
        if p.get("package_status") == "PACKAGE_COMPLETE_FOR_DEAL_ANALYSIS":
            primary = p
            break
    # If none complete, do NOT force — record honest stop with best incomplete for diagnostics
    if primary is None and ranked:
        best_incomplete = ranked[0]
        attempts.append(
            {
                "note": "no_complete_package_in_bounded_run",
                "best_incomplete": best_incomplete.get("solicitation_id"),
                "status": best_incomplete.get("package_status"),
            }
        )

    selection_record = {
        "selected": primary.get("solicitation_id") if primary else None,
        "why_selected": None,
        "finalists_not_selected": [],
        "attempts": attempts,
        "discovery_proven": True,
        "seeded_solicitation": False,
        "iowa_2975_used_as_primary": False,
    }
    if primary:
        selection_record["why_selected"] = (
            f"Highest complete-package eligibility among autonomously discovered transactional "
            f"candidates: {primary.get('title')} "
            f"(completeness points={(primary.get('completeness') or {}).get('score', {}).get('points')}, "
            f"lines={len(primary.get('line_items') or [])})."
        )
        for p in ranked:
            if p.get("solicitation_id") == primary.get("solicitation_id"):
                continue
            selection_record["finalists_not_selected"].append(
                {
                    "solicitation_id": p.get("solicitation_id"),
                    "title": p.get("title"),
                    "reason": p.get("package_status")
                    or (p.get("completeness") or {}).get("score", {}).get("blockers"),
                }
            )
    else:
        selection_record["why_selected"] = (
            "No autonomously discovered candidate met PACKAGE_COMPLETE_FOR_DEAL_ANALYSIS "
            "within the bounded run (auth walls, missing quantities/specs, or incomplete public docs)."
        )
    _write("live_primary_candidate_selection.json", selection_record)

    scorecard = {
        "DISCOVERY_PROVEN": bool(opps),
        "PACKAGE_ACQUISITION_PROVEN": False,
        "REQUIREMENT_EXTRACTION_PROVEN": False,
        "SUPPLIER_DISCOVERY_PROVEN": False,
        "PUBLIC_COSTING_PROVEN": False,
        "OPERATOR_HANDOFF_PROVEN": False,
        "LIVE_SYNTHETIC_SEPARATION": True,
        "external_communication": False,
        "bid_submitted": False,
    }

    # Early Iowa regression + artifacts if no primary
    iowa = iowa_regression_only()
    _write("iowa_incomplete_regression.json", iowa)

    if primary is None:
        scorecard["limiting_factor"] = _infer_limiting_factor(acquired, transactional_sorted)
        scorecard["honest_stop"] = "NO_COMPLETE_PACKAGE_IN_BOUNDED_LIVE_RUN"
        _write("live_validation_scorecard.json", scorecard)
        _write("live_validation_request_log.json", REQUEST_LOG)
        _write(
            "live_primary_package_manifest.json",
            {"status": "NO_PRIMARY", "attempts": attempts},
        )
        for name in (
            "live_primary_requirements.json",
            "live_primary_bom.json",
            "live_primary_supplier_candidates.json",
            "live_primary_public_costing.json",
            "live_primary_buyer_history.json",
            "live_primary_operator_deal_packet.json",
            "live_primary_supplier_call_sheet.json",
        ):
            _write(name, {"status": "NO_PRIMARY"})
        # cleanup temp
        cleanup = temp.cleanup() if hasattr(temp, "cleanup") else {"cleaned": True}
        _write("live_temp_cleanup.json", cleanup if isinstance(cleanup, dict) else {"ok": True})
        print(json.dumps({"ok": True, "primary": None, "scorecard": scorecard}, indent=2))
        return 0

    # --- 5) Primary deep path ---
    scorecard["PACKAGE_ACQUISITION_PROVEN"] = True
    _write("live_primary_package_manifest.json", {
        "solicitation_id": primary["solicitation_id"],
        "package_status": primary["package_status"],
        "manifest": primary.get("manifest"),
        "completeness": primary.get("completeness"),
    })

    lines = primary.get("line_items") or []
    terms = primary.get("terms") or {}
    product_id = primary.get("product_id") or identify_product(
        line_items=lines, terms=terms, title=primary.get("title")
    )
    scorecard["REQUIREMENT_EXTRACTION_PROVEN"] = bool(lines)
    _write(
        "live_primary_requirements.json",
        {
            "solicitation_id": primary["solicitation_id"],
            "product_id": product_id,
            "terms": terms,
            "line_item_count": len(lines),
            "requirements": lines,
        },
    )
    bom = []
    for i, li in enumerate(lines):
        dest = li.get("delivery_location")
        if not dest and isinstance(terms.get("delivery_location"), dict):
            dest = terms["delivery_location"].get("value")
        bom.append(
            {
                "CLIN_or_item_number": li.get("CLIN_or_item_number") or li.get("line_number") or str(i + 1),
                "description": li.get("description"),
                "quantity": li.get("quantity"),
                "unit": li.get("unit") or li.get("unit_of_measure"),
                "manufacturer": li.get("manufacturer"),
                "part_number": li.get("part_number"),
                "model": li.get("model"),
                "brand_name_or_equal": li.get("brand_name_or_equal"),
                "delivery_location": dest,
                "provenance": li.get("field_provenance"),
            }
        )
    _write("live_primary_bom.json", {"solicitation_id": primary["solicitation_id"], "bom": bom})

    # Supplier research — knowledge first then discover_suppliers_for_product
    store = ReusableKnowledgeStore()
    store.add_finance(
        finance_fact(
            "gov_po_transaction_financier_A",
            "pg_requirement",
            "possible",
            source="existing_knowledge",
            verified=False,
        )
    )
    suppliers = discover_suppliers_for_product(
        product_id=product_id, title=primary.get("title"), max_suppliers=5
    )
    scorecard["SUPPLIER_DISCOVERY_PROVEN"] = len(suppliers) >= 1
    _write(
        "live_primary_supplier_candidates.json",
        {
            "solicitation_id": primary["solicitation_id"],
            "suppliers": suppliers,
            "count": len(suppliers),
            "knowledge_first": True,
        },
    )

    costing = build_public_costing(product_id, suppliers)
    scorecard["PUBLIC_COSTING_PROVEN"] = costing["status"] == "PUBLIC_PRICE_EVIDENCE_FOUND"
    _write("live_primary_public_costing.json", costing)

    fob = (terms.get("FOB_terms") or {}).get("value") if isinstance(terms.get("FOB_terms"), dict) else None
    freight_status = "QUOTE_REQUIRED"
    freight_val = None
    if fob and re.search(r"destination", str(fob), re.I):
        freight_status = "SUPPLIER_DELIVERED_LIKELY_BUT_AMOUNT_UNKNOWN"
    econ = preliminary_economics(
        supplier_cost=None if costing["status"] != "PUBLIC_PRICE_EVIDENCE_FOUND" else (
            float(costing["prices"][0]) if costing["prices"] else None
        ),
        freight=freight_val,
        freight_status=freight_status,
    )

    # Buyer history — compact, no award-as-cost
    _write(
        "live_primary_buyer_history.json",
        {
            "agency": primary.get("agency"),
            "status": "CONTEXT_ONLY",
            "note": "No USAspending deep pull in this bounded run; prior award prices are not supplier cost",
            "competition": "UNKNOWN",
            "prior_bidders": "UNKNOWN",
        },
    )

    # Executable deal pipeline
    delivery = None
    if isinstance(terms.get("delivery_location"), dict):
        delivery = terms["delivery_location"].get("value")
    deal_opp = {
        "deal_id": primary["solicitation_id"],
        "solicitation_number": primary["solicitation_id"],
        "title": primary.get("title"),
        "agency": primary.get("agency"),
        "product_classification": "CORE_PRODUCT",
        "product_category": primary.get("title"),
        "line_items": lines,
        "quantities_from_solicitation": True,
        "bid_deadline": primary.get("deadline"),
        "timezone": "America/Chicago",
        "supplier_candidates": [
            {"name": s.get("supplier_name") or s.get("name"), "supplier": s.get("supplier_name") or s.get("name")}
            for s in suppliers
        ],
        "preferred_supplier": (suppliers[0].get("supplier_name") if suppliers else None),
        "delivery_destination": delivery,
        "portal": primary.get("source"),
        "is_live": True,
        "auth_barriers": primary.get("auth_barriers") or [],
        "preliminary_economics": econ,
        "funding_preview_only": True,
    }
    pipe = ExecutableDealPipeline(reusable=store)
    pipe_result = pipe.run(deal_opp)
    packet = build_operator_deal_packet(pipe_result)
    packet["WHY_M3_SELECTED_IT"] = selection_record["why_selected"]
    packet["PACKAGE_COMPLETE"] = primary["package_status"]
    packet["PUBLIC_COST"] = costing
    packet["PRELIMINARY_ECONOMICS"] = econ
    packet["FREIGHT_STATUS"] = freight_status
    packet["FUNDING_PREVIEW"] = {
        "label": "PRELIMINARY FUNDING CANDIDATES",
        "not": "funding approved / verified / call ready unless gate satisfied",
        "providers": ["gov_po_transaction_financier_A"],
        "funding_call_ready": pipe_result.get("operator_readiness") == "FUNDING_CALL_READY",
    }
    packet["BRIAN_NEXT"] = (pipe_result.get("actions") or [{}])[0] if pipe_result.get("actions") else {
        "note": pipe_result.get("stop_reason")
    }
    _write("live_primary_operator_deal_packet.json", packet)

    call_sheet = None
    if pipe_result.get("operator_readiness") in {"QUOTE_REQUIRED", "OPERATOR_ACTION_REQUIRED"} or costing[
        "status"
    ] == "QUOTE_REQUIRED":
        best = suppliers[0] if suppliers else {"name": "primary supplier candidate"}
        call_sheet = build_supplier_call_sheet(
            deal={
                "solicitation_number": primary["solicitation_id"],
                "delivery_destination": delivery,
                "bid_deadline": primary.get("deadline"),
                "title": primary.get("title"),
            },
            supplier={
                "name": best.get("supplier_name") or best.get("name"),
                "contact": best.get("contact") or best.get("url"),
            },
            line_items=bom,
        )
        call_sheet["opening_script"] = (
            f"Hi, this is Brian with a small government-resale company. We are preparing a bid for "
            f"{primary.get('agency')} solicitation {primary['solicitation_id']} "
            f"({primary.get('title')}). Can you quote the attached line items for delivery to "
            f"{delivery or 'the destination on the solicitation'}, confirm lead time, freight, "
            f"payment terms, and quote validity through {primary.get('deadline')}?"
        )
        call_sheet["auto_send"] = False
    _write("live_primary_supplier_call_sheet.json", call_sheet or {"status": "NOT_REQUIRED_OR_NO_SUPPLIER"})

    scorecard["OPERATOR_HANDOFF_PROVEN"] = bool(pipe_result.get("actions") or packet.get("NEXT_ACTIONS"))
    scorecard["operator_readiness"] = pipe_result.get("operator_readiness")
    scorecard["stop_reason"] = pipe_result.get("stop_reason")
    scorecard["primary_solicitation"] = primary["solicitation_id"]
    scorecard["could_brian_send_bom_today"] = bool(bom) and all(
        li.get("description") and li.get("quantity") is not None for li in bom
    )
    scorecard["formal_quote_still_required"] = costing["status"] == "QUOTE_REQUIRED"
    scorecard["worth_continuing"] = scorecard["could_brian_send_bom_today"] and scorecard[
        "OPERATOR_HANDOFF_PROVEN"
    ]
    scorecard["handoff_complete"] = bool(
        scorecard["DISCOVERY_PROVEN"]
        and scorecard["PACKAGE_ACQUISITION_PROVEN"]
        and scorecard["REQUIREMENT_EXTRACTION_PROVEN"]
        and scorecard["SUPPLIER_DISCOVERY_PROVEN"]
        and scorecard["OPERATOR_HANDOFF_PROVEN"]
    )

    _write("live_validation_scorecard.json", scorecard)
    _write(
        "live_validation_request_log.json",
        {
            "events": REQUEST_LOG,
            "discovery_LIVE_API_REQUESTS": discovery.get("LIVE_API_REQUESTS"),
            "client_requests": getattr(client, "request_count", None),
            "external_communication": False,
            "bid_submitted": False,
        },
    )

    # Cleanup temporary retrieval
    if hasattr(temp, "cleanup"):
        _write("live_temp_cleanup.json", temp.cleanup())
    elif hasattr(temp, "release_all"):
        _write("live_temp_cleanup.json", temp.release_all())
    else:
        _write("live_temp_cleanup.json", {"note": "manual_temp_dir", "path": str(TEMP_ROOT)})

    print(
        json.dumps(
            {
                "ok": True,
                "primary": primary["solicitation_id"],
                "title": primary.get("title"),
                "scorecard": scorecard,
                "readiness": pipe_result.get("operator_readiness"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def _infer_limiting_factor(acquired: list[dict[str, Any]], pool: list[dict[str, Any]]) -> str:
    if not pool:
        return "source_coverage"
    if not acquired:
        return "package_retrieval"
    auth = sum(1 for p in acquired if (p.get("completeness") or {}).get("critical_auth_gated_spec"))
    if auth == len(acquired):
        return "package_retrieval"
    missing_qty = sum(
        1
        for p in acquired
        if (p.get("completeness") or {}).get("categories", {}).get("quantities", {}).get("status") == "MISSING"
    )
    if missing_qty:
        return "requirement_extraction"
    return "package_retrieval"


if __name__ == "__main__":
    raise SystemExit(main())
