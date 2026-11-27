"""NextBid targeted acquisition for M3 Build 002I.

Prioritizes state/local portals. Prefers original government documents.
Never fabricates opportunities. Checkpoints every batch.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from m3.config import get_settings
from m3.research.classify import classify_batch
from m3.research.commercial_criteria import STAGE_C, evaluate_procurement_pipeline
from m3.research.extract_fields import enrich_item_from_text
from m3.research.http_util import fetch_bytes, fetch_text
from m3.research.nextbid_client import (
    NextBidClient,
    NextBidConfigError,
    NextBidQuotaError,
    load_checkpoint,
    save_checkpoint,
)
PRIORITY_SOURCES = (
    "opengov",
    "bonfire",
    "planetbids",
    "calprocure",
    "publicpurchase",
    "instantmarkets",
)
# Keep independent SAM/DLA acquisition disabled until CAGE
EXCLUDED_SOURCES = {"sam.gov", "dibbs"}

PRODUCT_QUERIES = [
    "commodities",
    "industrial supplies",
    "tools equipment",
    "janitorial supplies",
    "janitorial equipment",
    "grounds maintenance equipment",
    "safety equipment PPE",
    "medical supplies",
    "furniture",
    "appliances",
    "automotive supplies",
    "trailer",
    "golf cart",
    "material handling",
    "electrical supplies",
    "plumbing supplies",
    "HVAC equipment",
    "traffic control",
    "signs",
    "hardware supplies",
    "agricultural equipment",
    "pump",
    "generator",
    "office supplies",
    "institutional supplies",
    "food service equipment",
    "recreation equipment",
    "emergency response supplies",
    "lawn mower",
    "custodial",
    "paper products",
    "uniforms",
    "first aid",
    "fire extinguisher",
]

SERVICE_ONLY_RE = re.compile(
    r"(?i)\b(consulting|professional\s+services|staffing|temp\s+labor|"
    r"construction\s+services|design[- ]build|architectural|engineering\s+services|"
    r"software\s+license|saas|managed\s+services|maintenance\s+and\s+repair|"
    r"lease\s+of|rental\s+of|rentals?\b)\b"
)
PRODUCT_HINT_RE = re.compile(
    r"(?i)\b(suppl(y|ies)|equipment|furniture|appliance|generator|pump|trailer|"
    r"golf\s*cart|ppe|glove|respirator|janitorial|custodial|mower|tractor|"
    r"signage|signs?\b|hardware|plumbing|electrical|hvac|medical\s+suppl|"
    r"office\s+suppl|tools?\b|materials?\b|commodit)\b"
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _normalize_listing(row: dict[str, Any]) -> dict[str, Any]:
    nb_id = str(row.get("nb_id") or "")
    source = str(row.get("source") or "").lower()
    title = str(row.get("title") or "").strip()
    source_url = (row.get("source_url") or "").strip() or None
    sol = row.get("solicitation_number")
    sol_s = str(sol).strip() if sol not in (None, "") else None
    deadline = row.get("response_deadline") or row.get("deadline_at")
    raw_life = row.get("lifecycle_status")
    status = str(raw_life or "").strip().lower()
    # Provider open=true search filter is retained evidence when lifecycle was null
    open_filter = bool(
        row.get("provider_open_filter")
        or row.get("open_search")
        or (isinstance(row.get("evidence"), dict) and row["evidence"].get("provider_open_filter"))
    )
    status_evidence: list[str] = []
    if status in {"open", "active", "posted", "advertised"}:
        sol_status = "open"
        status_evidence.append(f"lifecycle:{status}")
    elif status == "amended":
        sol_status = "amended"
        status_evidence.append("lifecycle:amended")
    elif status in {"closed", "expired", "inactive"}:
        sol_status = "closed"
        status_evidence.append(f"lifecycle:{status}")
    elif status in {"cancelled", "canceled"}:
        sol_status = "cancelled"
        status_evidence.append(f"lifecycle:{status}")
    elif status == "awarded":
        sol_status = "historical_award"
        status_evidence.append("lifecycle:awarded")
    elif open_filter and not status:
        # Not fabricated: provider returned row under open=true; lifecycle simply omitted
        sol_status = "open"
        status = "open"
        status_evidence.append("provider_open_filter_null_lifecycle")
    else:
        sol_status = "unknown"
        if not status:
            status = "unknown"

    bonding = row.get("bonding_required")
    states = row.get("states") or []
    geo_state = None
    if isinstance(states, list) and states:
        geo_state = str(states[0]).upper()[:2]

    notice = str(row.get("notice_type") or "")
    # Forecast / award notices are not active solicitations
    record_type = "INDIVIDUAL_SOLICITATION"
    verified = True
    if re.search(r"(?i)\b(forecast|sources\s*sought|award\s*notice|pre[- ]?solicitation)\b", notice + " " + title):
        if re.search(r"(?i)\b(forecast|award\s*notice)\b", notice + " " + title):
            record_type = "NON_SOLICITATION"
            verified = False

    item = {
        "external_key": _key("nextbid", nb_id or source_url or title),
        "nb_id": nb_id,
        "title": title[:500],
        "solicitation_number": sol_s,
        "buyer_name": row.get("agency") or row.get("agency_department"),
        "geo_state": geo_state,
        "geo_city": (row.get("cities") or [None])[0] if isinstance(row.get("cities"), list) else None,
        "geo_location": geo_state,
        "deadline_at": deadline,
        "deadline_timezone": "UTC",
        "posted_at": row.get("posted_date"),
        "procurement_type": str(row.get("procurement_kind") or row.get("notice_type") or "unknown"),
        "product_category": "unknown",
        "set_aside": row.get("set_aside") or "unknown",
        "set_aside_verified": bool(row.get("set_aside")),
        "estimated_value_min": None,
        "estimated_value_max": None,
        "estimated_value_unknown": True,
        "official_url": (source_url or f"https://nextbid.us/opportunities/{nb_id}")[:2048],
        "discovery_url": f"https://api.nextbid.us/v1/opportunities/{nb_id}",
        "source_url_original": source_url,
        "documents_available": "yes" if int(row.get("document_count") or 0) > 0 else "unknown",
        "quantity": None,
        "requirement_class": "pending_classification",
        "classification_evidence": ["deferred_to_central_classifier", "nextbid_listing"],
        "lifecycle_status": status if status != "unknown" else (str(raw_life).lower() if raw_life else None),
        "solicitation_status": sol_status,
        "provider_open_filter": open_filter,
        "status_evidence": status_evidence,
        "source_key": f"nextbid_{source}" if source else "nextbid",
        "nextbid_source": source,
        "source": source,
        "source_verified_at": _utc(),
        "source_purpose": "individual_solicitation_candidate",
        "discovery_stage": "nextbid_targeted",
        "verified_individual_solicitation": verified,
        "record_type": record_type,
        "record_type_evidence": [f"nextbid_notice_type:{notice or 'unknown'}", f"nb_id:{nb_id}"],
        "bonding_required_hint": bonding,
        "document_count_hint": row.get("document_count"),
        "attachment_count_hint": row.get("attachment_count"),
        "tradelines": row.get("tradelines") or [],
        "summary": row.get("summary"),
        "description": row.get("summary"),
        "evidence": {
            "nextbid_nb_id": nb_id,
            "nextbid_source": source,
            "source_url": source_url,
            "lifecycle_status": raw_life,
            "provider_open_filter": open_filter,
            "status_evidence": status_evidence,
            "listing_only": True,
        },
        "identity_strength": "strong" if sol_s and source_url else "moderate",
    }
    return item


def _is_priority_candidate(item: dict[str, Any]) -> bool:
    src = (item.get("nextbid_source") or "").lower()
    if src in EXCLUDED_SOURCES:
        return False
    if src in PRIORITY_SOURCES:
        return True
    # Allow other non-federal if present
    return src not in EXCLUDED_SOURCES and bool(src)


def _looks_productish(item: dict[str, Any]) -> bool:
    blob = f"{item.get('title') or ''} {item.get('summary') or ''} {' '.join(item.get('tradelines') or [])}"
    if SERVICE_ONLY_RE.search(blob) and not PRODUCT_HINT_RE.search(blob):
        return False
    if PRODUCT_HINT_RE.search(blob):
        return True
    tls = {str(t).lower() for t in (item.get("tradelines") or [])}
    if "commodities" in tls:
        return True
    # UNKNOWN classification is not proof of incompatibility — keep for detail
    return True


def discover_opportunities(
    client: NextBidClient,
    *,
    max_discovery_calls: int | None = None,
    page_size: int = 25,
    checkpoint_name: str = "nextbid_acquire_v1",
) -> dict[str, Any]:
    """Paged keyword/tradeline discovery across priority state/local sources."""
    ck = load_checkpoint(checkpoint_name)
    state = ck.get("state") or {}
    seen_ids: set[str] = set(state.get("seen_nb_ids") or [])
    items_by_id: dict[str, dict] = {i["nb_id"]: i for i in (state.get("listings") or []) if i.get("nb_id")}
    completed_queries: set[str] = set(state.get("completed_queries") or [])
    errors: list[str] = []
    discovery_calls = 0
    duplicates = 0

    queries: list[dict[str, Any]] = []
    # Broad commodities tradeline by source groups
    for src_group in (
        "opengov,bonfire,planetbids",
        "calprocure,publicpurchase,instantmarkets",
    ):
        queries.append(
            {
                "label": f"tradeline:commodities:{src_group}",
                "params": {
                    "tradelines": "commodities",
                    "sources": src_group,
                    "open": "true",
                    "page_size": page_size,
                    "sort": "deadline",
                },
            }
        )
    for q in PRODUCT_QUERIES:
        queries.append(
            {
                "label": f"q:{q}",
                "params": {
                    "q": q,
                    "sources": ",".join(PRIORITY_SOURCES),
                    "open": "true",
                    "page_size": page_size,
                    "sort": "relevance",
                },
            }
        )

    for qspec in queries:
        label = qspec["label"]
        if label in completed_queries:
            continue
        if max_discovery_calls is not None and discovery_calls >= max_discovery_calls:
            break
        if not client.budget.can_spend("discovery", 1):
            errors.append("discovery_budget_stop")
            break

        page = 1
        total_pages = 1
        while page <= total_pages:
            if max_discovery_calls is not None and discovery_calls >= max_discovery_calls:
                break
            if not client.budget.can_spend("discovery", 1):
                errors.append("discovery_budget_stop")
                break
            params = dict(qspec["params"])
            params["page"] = page
            try:
                res = client.search(params)
            except NextBidQuotaError as exc:
                errors.append(f"quota:{exc}")
                break
            discovery_calls += 1
            if not res.get("ok", True) and res.get("error"):
                errors.append(f"{label}:page{page}:{res.get('error')}")
                break
            total_pages = int(res.get("total_pages") or 1)
            # Cap pages per query to protect budget (intelligent breadth over depth)
            hard_page_cap = 8 if label.startswith("tradeline:") else 3
            total_pages = min(total_pages, hard_page_cap)
            for row in res.get("results") or []:
                if not isinstance(row, dict):
                    continue
                src = str(row.get("source") or "").lower()
                if src in EXCLUDED_SOURCES:
                    continue
                nb_id = str(row.get("nb_id") or "")
                if not nb_id:
                    continue
                if nb_id in seen_ids:
                    duplicates += 1
                    continue
                seen_ids.add(nb_id)
                item = _normalize_listing(row)
                if not _is_priority_candidate(item):
                    continue
                items_by_id[nb_id] = item
            page += 1

        completed_queries.add(label)
        state = {
            "seen_nb_ids": sorted(seen_ids),
            "listings": list(items_by_id.values()),
            "completed_queries": sorted(completed_queries),
            "discovery_calls": discovery_calls,
            "duplicates_skipped": duplicates,
            "errors": errors[-50:],
            "budget": client.budget.snapshot(),
        }
        save_checkpoint(state, checkpoint_name)

    listings = list(items_by_id.values())
    productish = [i for i in listings if _looks_productish(i)]
    return {
        "ok": True,
        "listings": listings,
        "productish": productish,
        "discovered": len(listings),
        "duplicates_skipped": duplicates,
        "discovery_calls": discovery_calls,
        "completed_queries": sorted(completed_queries),
        "errors": errors,
        "budget": client.budget.snapshot(),
    }


def _extract_gov_doc_urls_from_detail(detail: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for doc in detail.get("documents") or []:
        if not isinstance(doc, dict):
            continue
        for k in ("url", "download_url", "href", "source_url", "file_url"):
            u = doc.get(k)
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)
    # attachments index sometimes nested
    for att in detail.get("attachments") or []:
        if isinstance(att, dict):
            for k in ("url", "download_url", "href", "source_url"):
                u = att.get(k)
                if isinstance(u, str) and u.startswith("http"):
                    urls.append(u)
    # dedupe
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def enrich_with_detail(
    client: NextBidClient,
    item: dict[str, Any],
    *,
    known_nb_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch full opportunity if not already cached locally by nb_id."""
    nb_id = item.get("nb_id")
    if not nb_id:
        return item
    known_nb_ids = known_nb_ids or set()
    # Skip detail if we already have description + contacts from a prior run marker
    if item.get("nextbid_detail_fetched") and item.get("description"):
        return item
    if nb_id in known_nb_ids and item.get("description") and item.get("nextbid_detail_fetched"):
        return item
    try:
        detail = client.get_opportunity(nb_id)
    except NextBidQuotaError:
        item["nextbid_detail_error"] = "quota"
        return item
    if not detail.get("ok", True) and detail.get("error"):
        item["nextbid_detail_error"] = detail.get("error")
        return item

    desc = detail.get("description") or detail.get("ai_summary") or item.get("description")
    # Prefer real description over AI summary as primary text; keep AI as secondary
    if detail.get("description"):
        item["description"] = detail.get("description")
        item["document_text"] = detail.get("description")
    elif detail.get("ai_summary"):
        item["description"] = detail.get("ai_summary")
        item["ai_summary_secondary"] = True
    item["contacts"] = detail.get("contacts") or []
    item["amendments"] = detail.get("amendments") or []
    item["nextbid_documents_meta"] = detail.get("documents") or []
    item["contract_type"] = detail.get("contract_type")
    item["opp_kind"] = detail.get("opp_kind")
    item["is_goods_only"] = detail.get("is_goods_only")
    if detail.get("source_url"):
        item["official_url"] = detail["source_url"]
        item["source_url_original"] = detail["source_url"]
    item["attachment_candidates"] = [
        {"url": u, "source": "nextbid_detail_or_gov"}
        for u in _extract_gov_doc_urls_from_detail(detail)
    ]
    # Evidence: AI summary is NOT primary source evidence
    item["evidence"] = {
        **(item.get("evidence") or {}),
        "listing_only": False,
        "nextbid_detail": True,
        "description_present": bool(detail.get("description")),
        "ai_summary_present": bool(detail.get("ai_summary")),
        "amendments": len(detail.get("amendments") or []),
        "contacts": len(detail.get("contacts") or []),
    }
    item["nextbid_detail_fetched"] = True
    if desc:
        item = enrich_item_from_text(item, str(desc), source="nextbid_detail_description")
    return item


def try_original_gov_documents(item: dict[str, Any]) -> dict[str, Any]:
    """Download from original government URLs when possible (no NextBid call).

    Bounded: prefer real file attachments; avoid hammering portal login shells.
    """
    urls: list[str] = []
    for c in item.get("attachment_candidates") or []:
        if isinstance(c, dict) and c.get("url"):
            urls.append(c["url"])
    # Listing source_url last — often an HTML portal page, not a solicitation PDF
    if item.get("source_url_original"):
        urls.append(item["source_url_original"])

    # Prefer direct document URLs first
    def _is_file(u: str) -> bool:
        return urlparse(u).path.lower().endswith(
            (".pdf", ".docx", ".xlsx", ".xls", ".doc", ".zip", ".csv", ".txt")
        )

    urls = sorted(dict.fromkeys(urls), key=lambda u: (0 if _is_file(u) else 1, u))
    saved = 0
    texts: list[str] = []
    failures: list[str] = []
    # Hard cap per opportunity to keep fleet acquisition bounded
    for u in urls[:4]:
        path = urlparse(u).path.lower()
        host = (urlparse(u).hostname or "").lower()
        try:
            if _is_file(u):
                status, final, content, ctype = fetch_bytes(u, timeout=12)
                if status < 400 and content and len(content) > 200:
                    saved += 1
                    item.setdefault("gov_downloaded", []).append(
                        {"url": final or u, "bytes": len(content), "content_type": ctype}
                    )
                    # Light text extraction for pdf/xlsx happens later in package engine;
                    # here keep raw marker only unless plain text
                    if path.endswith((".txt", ".csv")):
                        try:
                            texts.append(content.decode("utf-8", errors="ignore")[:20000])
                        except Exception:
                            pass
                else:
                    failures.append(f"bytes:{status}")
            else:
                # Skip known authenticated vendor portals for HTML shell fetches
                if any(
                    h in host
                    for h in (
                        "planetbids.com",
                        "procure.opengov.com",
                        "vendors.planetbids",
                        "bonfirehub.com",
                        "publicpurchase.com",
                    )
                ):
                    failures.append("portal_html_skipped_prefer_files_or_nextbid")
                    continue
                status, final, html = fetch_text(u, timeout=10)
                if status >= 400 or not html:
                    failures.append(f"html:{status}")
                    continue
                low = html[:4000].lower()
                if any(m in low for m in ("sign in", "log in", "login", "create an account")) and len(html) < 12000:
                    failures.append("login_shell")
                    continue
                texts.append(re.sub(r"<[^>]+>", " ", html)[:20000])
                saved += 1
                item.setdefault("gov_downloaded", []).append({"url": final or u, "html_bytes": len(html)})
        except Exception as exc:
            failures.append(type(exc).__name__)
    if texts:
        blob = "\n".join(texts)
        item["document_text"] = ((item.get("document_text") or "") + "\n" + blob)[:100000]
        item = enrich_item_from_text(item, blob, source="original_government_html")
    item["gov_doc_saved_calls"] = saved
    item["gov_doc_failures"] = failures[:10]
    return item


def maybe_nextbid_documents(client: NextBidClient, item: dict[str, Any]) -> dict[str, Any]:
    """Use NextBid document/file endpoints only when original gov acquisition insufficient."""
    nb_id = item.get("nb_id")
    if not nb_id:
        return item
    has_text = len(str(item.get("document_text") or "")) > 400
    has_gov = bool(item.get("gov_downloaded"))
    if has_text and has_gov:
        item["nextbid_docs_skipped"] = "gov_docs_sufficient"
        return item
    if not client.budget.can_spend("documents", 1):
        item["nextbid_docs_skipped"] = "budget"
        return item
    try:
        docs = client.list_documents(nb_id)
    except NextBidQuotaError:
        item["nextbid_docs_error"] = "quota"
        return item
    item["nextbid_document_list"] = docs.get("documents") or docs.get("results") or docs
    # Prefer real sha256 attachments via get_document_download — never burn credits
    # on NextBid document0 .json/.md sidecars (blocked in client.read_file anyway).
    from m3.research.document_fetch_plan import build_document_fetch_plan, is_document0_sidecar

    doc_rows = item.get("nextbid_document_list")
    entries: list[dict] = []
    if isinstance(doc_rows, list):
        entries.extend(x for x in doc_rows if isinstance(x, dict))
    elif isinstance(doc_rows, dict):
        for key in ("source_documents", "nextbid_documents", "documents", "files"):
            val = doc_rows.get(key)
            if isinstance(val, list):
                entries.extend(x for x in val if isinstance(x, dict))
    plan = build_document_fetch_plan(nb_id=nb_id, entries=entries)
    for target in plan.get("targets_for_download") or []:
        sha = str(target.get("sha256") or target.get("hash") or "").strip()
        if not sha or not client.budget.can_spend("documents", 1):
            break
        try:
            dl = client.get_document_download(nb_id, sha)
        except NextBidQuotaError:
            break
        url = str(((dl.get("download") or {}) if isinstance(dl, dict) else {}).get("url") or "")
        if not url:
            continue
        item.setdefault("nextbid_download_urls", []).append({"sha256": sha[:16], "url": url[:160]})
        item.setdefault("nextbid_files_read", []).append(f"get_document_download:{sha[:16]}")
        break

    if not client.budget.can_spend("documents", 1):
        return item
    try:
        files = client.list_files(nb_id)
    except NextBidQuotaError:
        item["nextbid_files_error"] = "quota"
        return item
    file_rows = files.get("files") or files.get("results") or []
    item["nextbid_files"] = file_rows if isinstance(file_rows, list) else []
    # Only non-document0 requirement/normalized text paths — document0 is hard-blocked.
    targets = []
    for f in item["nextbid_files"]:
        if not isinstance(f, dict):
            continue
        if is_document0_sidecar(f):
            continue
        kind = str(f.get("kind") or "")
        path = str(f.get("path") or "")
        if kind in {"requirements", "normalized", "case"} and path:
            targets.append(path)
    for path in targets[:2]:
        if not path or not client.budget.can_spend("documents", 1):
            break
        try:
            content = client.read_file(nb_id, path)
        except NextBidQuotaError:
            break
        except Exception as exc:
            if "document0_sidecar_metered_read_blocked" in str(exc):
                item.setdefault("nextbid_docs_skipped_sidecars", []).append(path)
                continue
            raise
        text = ""
        if isinstance(content, dict):
            text = content.get("text") or content.get("content") or ""
            if not text and isinstance(content.get("data"), str):
                text = content["data"]
        if text and len(text) > 80:
            item["document_text"] = ((item.get("document_text") or "") + "\n" + text)[:120000]
            item = enrich_item_from_text(item, text, source=f"nextbid_file:{path}")
            item.setdefault("nextbid_files_read", []).append(path)
    return item


def classify_and_qualify(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return items
    batch = classify_batch(items)
    classified = batch.get("items") if isinstance(batch, dict) else batch
    out = []
    for it in classified or []:
        # Award / forecast notices are not active solicitations
        blob = f"{it.get('title') or ''} {it.get('official_url') or ''} {it.get('source_url_original') or ''}"
        if re.search(r"(?i)notice[-_ ]of[-_ ]awards?|/awards?\b|forecast", blob):
            it["record_type"] = "NON_SOLICITATION"
            it["verified_individual_solicitation"] = False
            it["solicitation_status"] = "historical_award" if re.search(r"(?i)award", blob) else it.get("solicitation_status")
            it["classification_evidence"] = list(it.get("classification_evidence") or []) + [
                "excluded_award_or_forecast_url_or_title"
            ]
        pipe = evaluate_procurement_pipeline(it, document_text=it.get("document_text"))
        # Write extracted fields back so Stage C evidence is durable on the item
        for k, v in (pipe.get("enriched_fields") or {}).items():
            if v is not None:
                it[k] = v
        if pipe.get("quantity") is not None:
            it["quantity"] = pipe.get("quantity")
        if pipe.get("quantity_kind") is not None:
            it["quantity_kind"] = pipe.get("quantity_kind")
        if pipe.get("quantity_evidence") is not None:
            it["quantity_evidence"] = pipe.get("quantity_evidence")
        it["quantity_semantic"] = it.get("quantity_semantic") or (
            "ESTIMATED_QUANTITY" if "quantity_estimated_not_guaranteed" in (pipe.get("unknown_flags") or []) else None
        )
        it["procurement_pipeline"] = pipe
        it["procurement_stage"] = pipe.get("stage")
        # Integrity: never count Stage C without a concrete quantity on the item
        if pipe.get("stage") == STAGE_C and it.get("quantity") in (None, "", "unknown"):
            pipe = dict(pipe)
            pipe["stage"] = "VERIFIED_PRODUCT_OPPORTUNITY"
            pipe["unknown_flags"] = list(pipe.get("unknown_flags") or []) + ["quantity_missing_after_enrichment"]
            it["procurement_pipeline"] = pipe
            it["procurement_stage"] = pipe["stage"]
        out.append(it)
    return out


def run_nextbid_acquisition(
    *,
    max_discovery_calls: int | None = None,
    max_detail: int | None = None,
    max_doc_candidates: int | None = None,
    checkpoint_name: str = "nextbid_acquire_v1",
    detail_workers: int = 4,
) -> dict[str, Any]:
    """Full targeted acquisition run within verified allowance and budgets."""
    settings = get_settings()
    report: dict[str, Any] = {
        "build": "002I",
        "build_id": settings.build_id,
        "started_at": _utc(),
        "ok": False,
    }
    from m3.research.nextbid_killswitch import emergency_stop_status

    kill = emergency_stop_status()
    if not kill.get("metered_calls_allowed"):
        report["error"] = "emergency_stop"
        report["killswitch"] = kill
        report["resume_requires_owner_authorization"] = True
        return report
    try:
        client = NextBidClient()
    except NextBidConfigError as exc:
        report["error"] = str(exc)
        report["requires_secure_configuration"] = True
        return report

    usage_before = client.usage()
    report["usage_before"] = {
        "remaining": client.budget.account_remaining,
        "used": client.budget.account_used,
        "allowance": client.budget.account_allowance,
        "plan": (usage_before.get("usage") or {}).get("plan")
        if isinstance(usage_before.get("usage"), dict)
        else usage_before.get("plan"),
    }

    # Discovery
    disc = discover_opportunities(
        client,
        max_discovery_calls=max_discovery_calls,
        page_size=min(25, settings.nextbid_page_size),
        checkpoint_name=checkpoint_name,
    )
    report["discovery"] = {
        "discovered": disc["discovered"],
        "productish": len(disc["productish"]),
        "duplicates_skipped": disc["duplicates_skipped"],
        "discovery_calls": disc["discovery_calls"],
        "completed_queries": disc["completed_queries"],
        "errors": disc["errors"],
    }

    candidates = disc["productish"] or disc["listings"]
    # Prefer productish with deadline soon / with docs
    def _score(it: dict) -> tuple:
        src = 0 if (it.get("nextbid_source") or "") in PRIORITY_SOURCES else 1
        docs = 0 if int(it.get("document_count_hint") or 0) > 0 else 1
        dl = str(it.get("deadline_at") or "9999")
        return (src, docs, dl)

    candidates = sorted(candidates, key=_score)
    if max_detail is not None:
        candidates = candidates[: max_detail]

    # Detail + gov docs + optional nextbid docs
    enriched: list[dict[str, Any]] = []
    lock = threading.Lock()
    detail_errors = 0
    gov_docs = 0
    nextbid_doc_reads = 0
    calls_saved_cache = 0
    nextbid_doc_candidates_used = 0
    # Cap NextBid document spends so discovery/detail remain primary
    max_nb_doc_opps = int(max_doc_candidates) if max_doc_candidates is not None else min(
        250, max(50, len(candidates) // 3)
    )

    def _work(it: dict) -> dict:
        nonlocal detail_errors, gov_docs, nextbid_doc_reads, calls_saved_cache, nextbid_doc_candidates_used
        row = dict(it)
        if row.get("nextbid_detail_fetched"):
            with lock:
                calls_saved_cache += 1
        else:
            row = enrich_with_detail(client, row)
            if row.get("nextbid_detail_error"):
                with lock:
                    detail_errors += 1
        row = try_original_gov_documents(row)
        if row.get("gov_downloaded"):
            with lock:
                gov_docs += len(row["gov_downloaded"])
        # Only spend NextBid doc calls for promising rows missing text, within cap
        need_docs = _looks_productish(row) and len(str(row.get("document_text") or "")) < 400
        spend = False
        if need_docs:
            with lock:
                if nextbid_doc_candidates_used < max_nb_doc_opps and client.budget.can_spend("documents", 2):
                    nextbid_doc_candidates_used += 1
                    spend = True
        if spend:
            before = len(row.get("nextbid_files_read") or [])
            row = maybe_nextbid_documents(client, row)
            after = len(row.get("nextbid_files_read") or [])
            with lock:
                nextbid_doc_reads += max(0, after - before)
        elif need_docs:
            row["nextbid_docs_skipped"] = "doc_candidate_cap"
        return row

    workers = max(1, min(detail_workers, 4))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_work, it) for it in candidates]
        for fut in as_completed(futs):
            try:
                enriched.append(fut.result())
            except Exception as exc:
                detail_errors += 1
                enriched.append({"error": type(exc).__name__})

    # Checkpoint enriched
    ck_state = load_checkpoint(checkpoint_name).get("state") or {}
    slim_enriched: list[dict[str, Any]] = []
    for e in enriched:
        if not isinstance(e, dict) or not e.get("nb_id"):
            continue
        row = {
            k: e.get(k)
            for k in (
                "nb_id",
                "external_key",
                "title",
                "solicitation_number",
                "buyer_name",
                "official_url",
                "source_url_original",
                "nextbid_source",
                "deadline_at",
                "solicitation_status",
                "requirement_class",
                "quantity",
                "quantity_unit",
                "quantity_semantic",
                "quantity_kind",
                "quantity_evidence",
                "procurement_stage",
                "document_count_hint",
                "nextbid_detail_fetched",
                "gov_doc_saved_calls",
                "nextbid_files_read",
                "record_type",
                "verified_individual_solicitation",
                "description",
            )
            if e.get(k) is not None
        }
        pipe = e.get("procurement_pipeline") if isinstance(e.get("procurement_pipeline"), dict) else {}
        if pipe.get("stage") and not row.get("procurement_stage"):
            row["procurement_stage"] = pipe.get("stage")
        if pipe.get("quantity_evidence") and not row.get("quantity_evidence"):
            row["quantity_evidence"] = pipe.get("quantity_evidence")
        full_txt = str(e.get("document_text") or "")
        # Build 002O: listing description/summary may be provider-capped (~500).
        # Persist full extracted text separately; keep only a short preview in checkpoint.
        row["document_text_len"] = len(full_txt)
        if full_txt:
            try:
                from m3.research.full_text_store import store_full_text

                meta = store_full_text(
                    str(e.get("nb_id")),
                    full_txt,
                    source="nextbid_acquire_checkpoint",
                    title=str(e.get("title") or ""),
                )
                row["document_text_preview"] = meta.get("preview")
                row["document_content_hash"] = meta.get("content_hash")
                row["full_text_path"] = meta.get("path")
            except Exception:
                row["document_text_preview"] = full_txt[:500]
        elif e.get("description"):
            # Explicitly mark provider listing summary (often 500-char capped)
            row["document_text_preview"] = str(e.get("description"))[:500]
            row["description_is_listing_summary"] = True
        if row["document_text_len"] == 0 and e.get("document_text") in (None, ""):
            row["document_text_missing"] = True
        slim_enriched.append(row)
    ck_state["enriched"] = slim_enriched
    ck_state["budget"] = client.budget.snapshot()
    save_checkpoint(ck_state, checkpoint_name)

    # Classify / qualify — use full in-memory enriched with document_text
    qualified = classify_and_qualify([e for e in enriched if isinstance(e, dict) and e.get("nb_id")])

    fits = []
    stage_counts: dict[str, int] = {}
    for it in qualified:
        stage = (it.get("procurement_pipeline") or {}).get("stage") or "none"
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        if stage == STAGE_C:
            pipe = it["procurement_pipeline"]
            fits.append(
                {
                    "nb_id": it.get("nb_id"),
                    "title": it.get("title"),
                    "solicitation_number": it.get("solicitation_number"),
                    "agency": it.get("buyer_name"),
                    "source": it.get("nextbid_source"),
                    "official_url": it.get("official_url"),
                    "deadline_at": it.get("deadline_at"),
                    "quantity": it.get("quantity"),
                    "quantity_unit": it.get("quantity_unit"),
                    "quantity_semantic": it.get("quantity_semantic"),
                    "quantity_evidence": it.get("quantity_evidence"),
                    "requirement_class": it.get("requirement_class"),
                    "stage": stage,
                    "evidence": {
                        "nextbid_nb_id": it.get("nb_id"),
                        "source_url": it.get("source_url_original") or it.get("official_url"),
                        "pipeline_reasons": pipe.get("reasons"),
                        "quantity_evidence": it.get("quantity_evidence"),
                        "record_type_evidence": it.get("record_type_evidence"),
                        "gov_docs": it.get("gov_downloaded"),
                        "nextbid_files_read": it.get("nextbid_files_read"),
                    },
                }
            )

    usage_after = client.usage()
    report.update(
        {
            "ok": True,
            "finished_at": _utc(),
            "usage_after": {
                "remaining": client.budget.account_remaining,
                "used": client.budget.account_used,
                "allowance": client.budget.account_allowance,
            },
            "budget": client.budget.snapshot(),
            "detail": {
                "candidates": len(candidates),
                "enriched": len(qualified),
                "detail_errors": detail_errors,
                "gov_documents_downloaded": gov_docs,
                "nextbid_doc_reads": nextbid_doc_reads,
                "calls_saved_via_cache": calls_saved_cache,
            },
            "qualification": {
                "stage_counts": stage_counts,
                "strict_m3_procurement_fits": len(fits),
                "fits": fits,
            },
            "source_yield": _source_yield(qualified),
            "verified_active_individual": sum(
                1
                for i in qualified
                if i.get("verified_individual_solicitation")
                and (i.get("solicitation_status") or "").lower() in {"open", "amended"}
            ),
            "verified_state_local": sum(
                1
                for i in qualified
                if (i.get("nextbid_source") or "") in PRIORITY_SOURCES
                and i.get("verified_individual_solicitation")
            ),
            "calls_saved_direct_gov_and_cache": gov_docs + calls_saved_cache,
        }
    )
    client.close()

    # Persist acceptance-friendly artifact
    out_dir = get_settings().data_root() / "acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "nextbid_002i_run.json"
    # Scrub any accidental key material
    scrubbed = json.loads(json.dumps(report, default=str))
    out_path.write_text(json.dumps(scrubbed, indent=2), encoding="utf-8")
    report["artifact"] = str(out_path)
    return report


def _source_yield(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        src = it.get("nextbid_source") or it.get("source_key") or "unknown"
        out[str(src)] = out.get(str(src), 0) + 1
    return out


def search_nextbid_adapter(source: dict, *, limit: int | None = None) -> dict[str, Any]:
    """Engine-compatible adapter entrypoint (uses checkpointed discovery when possible)."""
    settings = get_settings()
    if not (settings.nextbid_api_key or "").strip():
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key") or "nextbid",
            "errors": ["NEXTBID_API_KEY missing — secure configuration required"],
            "verified_at": _utc(),
        }
    max_calls = int((source.get("config") or {}).get("max_discovery_calls") or 80)
    with NextBidClient() as client:
        client.usage()
        disc = discover_opportunities(client, max_discovery_calls=max_calls)
    items = disc.get("productish") or disc.get("listings") or []
    if limit:
        items = items[: int(limit)]
    return {
        "ok": bool(items),
        "items": items,
        "source_key": source.get("source_key") or "nextbid",
        "errors": disc.get("errors") or [],
        "verified_at": _utc(),
        "pages_fetched": disc.get("discovery_calls") or 0,
        "rows_examined": disc.get("discovered") or 0,
        "pagination_complete": False,
        "more_available": True,
        "stats": {
            "accepted": len(items),
            "duplicates_skipped": disc.get("duplicates_skipped"),
            "budget": disc.get("budget"),
        },
    }
