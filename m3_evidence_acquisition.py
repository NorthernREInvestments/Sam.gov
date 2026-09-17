"""M3 progressive evidence acquisition ladder — extends existing recovery engines.

Reuses: document_locator, solicitation_package_retrieval, PublicProcurementHttpClient,
openai_runtime web_search, alternate_authoritative_routes, product_category_yield,
governing_documents, Cost Governor. Does not replace the research runner.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from copy import deepcopy
from typing import Any
from urllib.parse import urljoin, urlparse

from application_clock import now_utc
from m3_evidence_constants import (
    ALTERNATE_ROUTE_NOT_TRIED,
    ATTACHMENT_NOT_FETCHED,
    ATTACHMENT_PARSE_FAILED,
    AUTH_AUTHORITATIVE,
    AUTH_HISTORICAL,
    AUTH_REQUIRED,
    AUTH_SECONDARY,
    BOT_PROTECTED,
    DT_CONSTRUCTION,
    DT_MIXED,
    DT_PRODUCT_RESALE,
    DT_SERVICE,
    DT_UNKNOWN,
    GOVERNMENT_SOURCE_NOT_FOUND,
    INSUFFICIENT_REQUIREMENTS_AFTER_RECOVERY,
    LISTING_ONLY,
    PACKAGE_FOUND_IN_ALTERNATE_SOURCE,
    PACKAGE_URL_FOUND_NOT_FETCHED,
    PACKAGE_URL_MISSING,
    PUBLIC_METADATA_ONLY,
    REGISTRATION_REQUIRED,
    SA_AUTH_REQUIRED,
    SA_BOT_PROTECTED,
    SA_PUBLIC,
    SA_PUBLIC_METADATA_ONLY,
    SA_REGISTRATION_REQUIRED,
    SA_UNAVAILABLE,
    SOURCE_BLOCKED,
    SOURCE_CHANGED,
    TIER_0_EXISTING,
    TIER_1_DIRECT,
    TIER_2_ALTERNATE,
    TIER_3_WEB,
    TIER_4_OPENAI_WEB,
    WEB_SEARCH_NOT_TRIED,
)

log = logging.getLogger("govtracker.m3_evidence")

_CONSTRUCTION = re.compile(
    r"\b(design[- ]?bid[- ]?build|construction|demolition|renovation|rehab(?:ilitation)?|"
    r"roof\s+replacement|hvac\s+upgrade|elevator\s+repair|sidewalk|asphalt|concrete\s+services|"
    r"job\s+order\s+contract|cmar|construction\s+manager|building\s+(?:roof|garage)|"
    r"wetland\s+restoration|bridge\s+study|rest\s+area\s+rehab|boiler\s+replacement)\b",
    re.I,
)
_SERVICE = re.compile(
    r"\b((?:consulting|professional|engineering|housekeeping|laundry|courier|banking|"
    r"investment\s+banking|management\s+support|on[- ]call|auction|ticket\s+distribution|"
    r"software\s+development|technical\s+assistance|therapeutic)\s+services?|"
    r"services?\s+for\b|rfp\s+for\s+services)\b",
    re.I,
)
_NON_SOLICITATION = re.compile(
    r"\b(policy|template|form\s+\d+|attendance\s+sheet|contract\s+forms?|"
    r"reporting\s+spreadsheet|addendum\s+form|application\s+to\s+contract)\b",
    re.I,
)
_AUTH_HINT = re.compile(
    r"\b(login|sign\s+in|register|registration\s+required|create\s+an?\s+account|"
    r"vendor\s+portal|authenticated|password)\b",
    re.I,
)
_BOT_HINT = re.compile(r"\b(captcha|cloudflare|access\s+denied|bot\s+detect|challenge)\b", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def _fp(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()[:32]


def detail_url(row: dict[str, Any]) -> str | None:
    for k in ("detail_url", "source_url", "url", "portal", "listing_url"):
        v = row.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def has_line_items(row: dict[str, Any]) -> bool:
    return bool(row.get("line_items") or row.get("bom") or row.get("bom_lines"))


def classify_evidence_failure(row: dict[str, Any]) -> dict[str, Any]:
    """Structured failure reasons — never collapse solely to requirements_insufficient."""
    reasons: list[str] = []
    url = detail_url(row)
    docs = row.get("documents") if isinstance(row.get("documents"), list) else []
    lines = has_line_items(row)
    pkg = str(row.get("package_access") or "").upper()
    access = str(row.get("source_access_state") or "").upper()
    attempts = row.get("evidence_recovery_attempts") or []
    tier_done = {a.get("tier") for a in attempts if isinstance(a, dict)}

    if pkg in {"AUTH_GATED", "REGISTRATION_REQUIRED"} or access in {
        SA_AUTH_REQUIRED,
        SA_REGISTRATION_REQUIRED,
    }:
        reasons.append(REGISTRATION_REQUIRED if "REGISTRATION" in pkg or access == SA_REGISTRATION_REQUIRED else AUTH_REQUIRED)
    if access == SA_BOT_PROTECTED:
        reasons.append(BOT_PROTECTED)
    if access == SA_UNAVAILABLE:
        reasons.append(SOURCE_BLOCKED)

    if not url and not docs:
        reasons.append(PACKAGE_URL_MISSING)
        reasons.append(PUBLIC_METADATA_ONLY)
    elif url and not docs and not lines:
        reasons.append(PACKAGE_URL_FOUND_NOT_FETCHED)
    if docs and not lines:
        parsed = any(
            isinstance(d, dict) and (d.get("text") or d.get("text_preview") or d.get("extracted_text"))
            for d in docs
        )
        reasons.append(ATTACHMENT_PARSE_FAILED if parsed else ATTACHMENT_NOT_FETCHED)
    if not docs and not lines and len(str(row.get("description") or "")) < 120:
        if LISTING_ONLY not in reasons and PACKAGE_URL_FOUND_NOT_FETCHED not in reasons:
            reasons.append(LISTING_ONLY)

    if TIER_2_ALTERNATE not in tier_done and url:
        reasons.append(ALTERNATE_ROUTE_NOT_TRIED)
    if TIER_4_OPENAI_WEB not in tier_done and TIER_3_WEB not in tier_done:
        reasons.append(WEB_SEARCH_NOT_TRIED)

    if (url or docs) and not lines and AUTH_REQUIRED not in reasons and REGISTRATION_REQUIRED not in reasons:
        reasons.append(INSUFFICIENT_REQUIREMENTS_AFTER_RECOVERY)

    if not reasons:
        reasons.append(PUBLIC_METADATA_ONLY)

    primary = reasons[0]
    # Prefer actionable primary
    for preferred in (
        AUTH_REQUIRED,
        REGISTRATION_REQUIRED,
        BOT_PROTECTED,
        PACKAGE_URL_FOUND_NOT_FETCHED,
        PACKAGE_URL_MISSING,
        ATTACHMENT_NOT_FETCHED,
        WEB_SEARCH_NOT_TRIED,
    ):
        if preferred in reasons:
            primary = preferred
            break

    return {
        "kind": "M3EvidenceFailureClassification",
        "canonical_id": row.get("canonical_id"),
        "primary_reason": primary,
        "reasons": reasons,
        "has_detail_url": bool(url),
        "document_count": len(docs),
        "has_line_items": lines,
        "tiers_attempted": sorted(t for t in tier_done if t),
    }


def classify_deal_type(row: dict[str, Any], *, evidence_text: str = "") -> dict[str, Any]:
    """PRODUCT / SERVICE / CONSTRUCTION / MIXED / UNKNOWN from evidence when possible."""
    from product_category_yield import classify_product_category

    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    blob = f"{title}\n{desc}\n{evidence_text}".strip()

    if _NON_SOLICITATION.search(title) and not has_line_items(row):
        return {
            "deal_type": DT_UNKNOWN,
            "not_transactional_solicitation": True,
            "confidence": "HIGH",
            "evidence": "policy_form_template",
        }

    construction = bool(_CONSTRUCTION.search(blob))
    service = bool(_SERVICE.search(blob))
    cat = classify_product_category(title, f"{desc}\n{evidence_text}")

    if construction and not cat["category"].startswith(("IT_", "PARTS", "AGRICULTURAL", "INDUSTRIAL", "VEHICLES", "TOOLS", "ELECTRICAL", "HVAC", "BUILDING_MATERIALS", "OTHER_TANGIBLE")):
        # Construction language dominates and no strong product family
        if "supply" not in title.lower() and "purchase" not in title.lower() and "seed" not in title.lower():
            return {"deal_type": DT_CONSTRUCTION, "confidence": "HIGH" if len(evidence_text) > 80 else "MEDIUM", "product_category": cat, "evidence": "construction_phrase"}

    if cat["category"] == "LIKELY_SERVICE_FALSE_POSITIVE" or (service and not construction):
        return {"deal_type": DT_SERVICE, "confidence": cat.get("confidence") or "MEDIUM", "product_category": cat, "evidence": "service_phrase"}

    if cat["category"] == "MIXED_GOODS_SERVICES" or (construction and ("supply" in title.lower() or "equipment" in title.lower())):
        return {"deal_type": DT_MIXED, "confidence": "MEDIUM", "product_category": cat, "evidence": "mixed"}

    if cat["category"] not in {"UNKNOWN", "LIKELY_SERVICE_FALSE_POSITIVE"}:
        return {"deal_type": DT_PRODUCT_RESALE, "confidence": cat.get("confidence") or "MEDIUM", "product_category": cat, "evidence": cat.get("evidence")}

    return {"deal_type": DT_UNKNOWN, "confidence": "LOW", "product_category": cat, "evidence": "insufficient"}


def _evidence_item(
    *,
    row: dict[str, Any],
    source_url: str | None,
    source_type: str,
    authority: str,
    title: str | None = None,
    document_type: str | None = None,
    text: str | None = None,
    confidence: str = "MEDIUM",
    relevance: str = "POSSIBLE",
    tier: str,
) -> dict[str, Any]:
    body = text or ""
    return {
        "opportunity_id": row.get("canonical_id"),
        "solicitation_number": row.get("solicitation_number") or row.get("external_id"),
        "buyer": row.get("agency") or row.get("buyer"),
        "source_url": source_url,
        "source_type": source_type,
        "authority": authority,
        "retrieval_timestamp": _utc(),
        "document_title": title,
        "document_type": document_type,
        "amendment_version": None,
        "content_fingerprint": _fp(body[:8000] if body else (source_url or "")),
        "evidence_confidence": confidence,
        "relevance_validation": relevance,
        "provenance_chain": [tier, source_type],
        "text_preview": (body[:1500] if body else None),
    }


def _http_get(url: str, *, source_id: str = "m3_evidence") -> dict[str, Any]:
    try:
        from discovery.http_client import PublicProcurementHttpClient, RequestBudget

        client = PublicProcurementHttpClient(budget=RequestBudget(max_requests=8, max_bytes=2_000_000))
        resp = client.get(url, source_id=source_id)
        text = resp.text or ""
        code = int(resp.status_code or 0)
        final = getattr(resp.meta, "url", url) if resp.meta else url
        blocked = False
        auth = False
        if code in {401, 403}:
            auth = True
        if code == 403 and _BOT_HINT.search(text):
            blocked = True
        if _AUTH_HINT.search(text[:4000]) and code in {200, 401, 403}:
            # soft hint — page may still be public listing
            if code in {401, 403} or "login" in text[:2000].lower():
                auth = auth or code in {401, 403}
        return {
            "ok": 200 <= code < 400,
            "status_code": code,
            "text": text,
            "url": url,
            "final_url": final,
            "auth_required": auth,
            "bot_protected": blocked,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status_code": None, "text": "", "url": url, "error": str(exc)[:300]}


def _extract_links(html: str, base: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html or "", re.I):
        href = m.group(1).strip()
        if href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        full = urljoin(base, href)
        if full in seen or not full.startswith("http"):
            continue
        seen.add(full)
        low = full.lower()
        kind = "link"
        if any(low.endswith(ext) for ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip")):
            kind = "attachment"
        elif any(tok in low for tok in ("attach", "download", "document", "solicitation", "addend", "amend")):
            kind = "likely_document"
        out.append({"url": full, "kind": kind})
        if len(out) >= 40:
            break
    return out


def _tier0_existing(row: dict[str, Any]) -> dict[str, Any]:
    items = []
    if row.get("documents"):
        for d in row["documents"] if isinstance(row["documents"], list) else []:
            if isinstance(d, dict):
                items.append(
                    _evidence_item(
                        row=row,
                        source_url=d.get("url") or d.get("href"),
                        source_type="existing_document",
                        authority=AUTH_AUTHORITATIVE if d.get("authoritative") else AUTH_SECONDARY,
                        title=d.get("title") or d.get("filename"),
                        document_type=d.get("document_type") or d.get("doc_class"),
                        text=d.get("text") or d.get("text_preview") or d.get("extracted_text"),
                        confidence="HIGH",
                        relevance="CONFIRMED" if d.get("url") else "POSSIBLE",
                        tier=TIER_0_EXISTING,
                    )
                )
    for k in ("package_metadata", "evidence_cache", "historical_evidence"):
        if row.get(k):
            items.append(
                _evidence_item(
                    row=row,
                    source_url=None,
                    source_type=k,
                    authority=AUTH_HISTORICAL if "historical" in k else AUTH_SECONDARY,
                    title=k,
                    text=json.dumps(row.get(k), default=str)[:2000],
                    confidence="MEDIUM",
                    relevance="POSSIBLE",
                    tier=TIER_0_EXISTING,
                )
            )
    return {"tier": TIER_0_EXISTING, "items": items, "attempted": True}


def _tier1_direct(row: dict[str, Any]) -> dict[str, Any]:
    url = detail_url(row)
    items: list[dict[str, Any]] = []
    docs: list[dict[str, Any]] = list(row.get("documents") or []) if isinstance(row.get("documents"), list) else []
    access_state = SA_PUBLIC
    fetched_text = ""
    if not url:
        return {"tier": TIER_1_DIRECT, "items": [], "attempted": False, "reason": PACKAGE_URL_MISSING}

    # Reuse document_locator first
    try:
        from document_locator import locate_procurement_documents

        located = locate_procurement_documents(
            source_url=url,
            solicitation_id=str(row.get("solicitation_number") or row.get("external_id") or "") or None,
            agency=row.get("agency"),
            title=row.get("title"),
            known_document_links=docs or None,
        )
        for c in located.get("documents") or located.get("candidates") or []:
            if not isinstance(c, dict):
                continue
            cu = c.get("url")
            if not cu:
                continue
            items.append(
                _evidence_item(
                    row=row,
                    source_url=cu,
                    source_type="document_locator",
                    authority=AUTH_AUTHORITATIVE,
                    title=c.get("title"),
                    document_type=c.get("document_class") or c.get("doc_class"),
                    confidence="MEDIUM",
                    relevance="POSSIBLE",
                    tier=TIER_1_DIRECT,
                )
            )
            if not any(isinstance(d, dict) and d.get("url") == cu for d in docs):
                docs.append({"url": cu, "title": c.get("title"), "source": "document_locator"})
    except Exception as exc:  # noqa: BLE001
        log.debug("document_locator failed: %s", exc)

    hit = _http_get(url, source_id=str(row.get("source_id") or "m3_evidence"))
    if hit.get("bot_protected"):
        access_state = SA_BOT_PROTECTED
    elif hit.get("auth_required") and not hit.get("ok"):
        access_state = SA_AUTH_REQUIRED
        if "register" in (hit.get("text") or "")[:3000].lower():
            access_state = SA_REGISTRATION_REQUIRED
    elif hit.get("ok"):
        fetched_text = hit.get("text") or ""
        items.append(
            _evidence_item(
                row=row,
                source_url=hit.get("final_url") or url,
                source_type="solicitation_detail_page",
                authority=AUTH_AUTHORITATIVE,
                title=row.get("title"),
                document_type="SOLICITATION_DETAIL",
                text=fetched_text[:20000],
                confidence="HIGH",
                relevance="CONFIRMED",
                tier=TIER_1_DIRECT,
            )
        )
        for link in _extract_links(fetched_text, hit.get("final_url") or url):
            if link["kind"] in {"attachment", "likely_document"}:
                docs.append({"url": link["url"], "title": link["url"].rsplit("/", 1)[-1], "source": "detail_page_link"})
                # Fetch a few attachments (cheap public)
                if link["kind"] == "attachment" and len([d for d in docs if d.get("fetched")]) < 3:
                    att = _http_get(link["url"], source_id=str(row.get("source_id") or "m3_evidence"))
                    if att.get("ok") and att.get("text"):
                        docs[-1]["fetched"] = True
                        docs[-1]["text_preview"] = (att.get("text") or "")[:4000]
                        items.append(
                            _evidence_item(
                                row=row,
                                source_url=link["url"],
                                source_type="attachment",
                                authority=AUTH_AUTHORITATIVE,
                                title=docs[-1].get("title"),
                                document_type="ATTACHMENT",
                                text=att.get("text"),
                                confidence="HIGH",
                                relevance="CONFIRMED",
                                tier=TIER_1_DIRECT,
                            )
                        )
                    elif att.get("auth_required"):
                        access_state = SA_AUTH_REQUIRED
    else:
        if hit.get("status_code") in {404, 410}:
            access_state = SA_UNAVAILABLE
        elif hit.get("auth_required"):
            access_state = SA_AUTH_REQUIRED

    return {
        "tier": TIER_1_DIRECT,
        "items": items,
        "attempted": True,
        "documents": docs,
        "fetched_text": fetched_text[:50000],
        "source_access_state": access_state,
        "http_status": hit.get("status_code"),
    }


def _tier2_alternate(row: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    docs = list(row.get("documents") or []) if isinstance(row.get("documents"), list) else []
    text_bits: list[str] = []
    tried = []
    sol = str(row.get("solicitation_number") or row.get("external_id") or "").strip()
    agency = str(row.get("agency") or "")
    source_id = str(row.get("source_id") or "")

    # Known alternate route map by source family
    candidates: list[str] = []
    try:
        from alternate_authoritative_routes import ALTERNATE_ROUTES

        for key, route in (ALTERNATE_ROUTES or {}).items():
            if source_id and key.lower() in source_id.lower():
                u = route.get("url") or route.get("discovery_url")
                if u:
                    candidates.append(str(u))
    except Exception:
        pass

    # Iowa bid opportunities pattern
    if "iowa" in agency.lower() or source_id.startswith("state_ia") or "iowa" in str(row.get("jurisdiction") or "").lower():
        if sol:
            candidates.append(f"https://bidopportunities.iowa.gov/Home/BidInformation?bidId={sol}")
            candidates.append(f"https://bidopportunities.iowa.gov/?search={sol}")

    # Montana eMACS / public notices
    if "montana" in agency.lower() or source_id.startswith("state_mt"):
        if sol:
            candidates.append(f"https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana")

    # Phoenix
    if "phoenix" in agency.lower() or "phoenix" in source_id:
        candidates.append("https://www.phoenix.gov/solicitations")

    for url in candidates[:5]:
        if url == detail_url(row):
            continue
        tried.append(url)
        hit = _http_get(url, source_id=source_id or "alternate")
        if not hit.get("ok"):
            continue
        body = hit.get("text") or ""
        # Relevance: solicitation number or substantial title token must appear
        title_tok = (str(row.get("title") or "").split()[:4])
        relevant = False
        if sol and sol.lower() in body.lower():
            relevant = True
        elif title_tok and sum(1 for t in title_tok if len(t) > 3 and t.lower() in body.lower()) >= 2:
            relevant = True
        if not relevant:
            continue
        text_bits.append(body[:15000])
        items.append(
            _evidence_item(
                row=row,
                source_url=hit.get("final_url") or url,
                source_type="alternate_authoritative",
                authority=AUTH_AUTHORITATIVE,
                title=row.get("title"),
                document_type="ALTERNATE_LISTING",
                text=body[:20000],
                confidence="MEDIUM",
                relevance="CONFIRMED",
                tier=TIER_2_ALTERNATE,
            )
        )
        for link in _extract_links(body, hit.get("final_url") or url):
            if link["kind"] in {"attachment", "likely_document"}:
                docs.append({"url": link["url"], "title": link["url"].rsplit("/", 1)[-1], "source": "alternate_route"})

    return {
        "tier": TIER_2_ALTERNATE,
        "items": items,
        "attempted": True,
        "documents": docs,
        "fetched_text": "\n".join(text_bits),
        "urls_tried": tried,
        "package_found_alternate": bool(items),
    }


def _tier3_web_queries(row: dict[str, Any]) -> list[str]:
    sol = str(row.get("solicitation_number") or row.get("external_id") or "").strip()
    title = str(row.get("title") or "").strip()
    agency = str(row.get("agency") or "").strip()
    qs = []
    if sol:
        qs.append(f'"{sol}"')
        if agency:
            qs.append(f'"{sol}" {agency}')
    if title and agency:
        qs.append(f'"{title[:80]}" {agency}')
    if sol and title:
        qs.append(f'"{sol}" {title[:60]} specification OR attachment OR amendment')
    return qs[:4]


def _tier3_public_web(row: dict[str, Any]) -> dict[str, Any]:
    """Non-paid public web discovery via search-engine HTML (best-effort)."""
    items: list[dict[str, Any]] = []
    queries = _tier3_web_queries(row)
    found_urls: list[str] = []
    for q in queries:
        # DuckDuckGo HTML — free public discovery
        search_url = f"https://html.duckduckgo.com/html/?q={q.replace(' ', '+')}"
        hit = _http_get(search_url, source_id="web_discovery")
        if not hit.get("ok"):
            continue
        body = hit.get("text") or ""
        for m in re.finditer(r'href=["\'](https?://[^"\']+)["\']', body):
            u = m.group(1)
            if "duckduckgo" in u or "google." in u:
                continue
            found_urls.append(u)
            if len(found_urls) >= 8:
                break
        if len(found_urls) >= 8:
            break

    docs = list(row.get("documents") or []) if isinstance(row.get("documents"), list) else []
    text_bits = []
    sol = str(row.get("solicitation_number") or "").lower()
    for u in found_urls[:5]:
        hit = _http_get(u, source_id="web_recovery")
        if not hit.get("ok"):
            continue
        body = hit.get("text") or ""
        if sol and sol not in body.lower() and sol not in u.lower():
            # Avoid merging unrelated similarly named solicitations
            title_words = [w for w in str(row.get("title") or "").lower().split() if len(w) > 4][:3]
            if not title_words or sum(1 for w in title_words if w in body.lower()) < 2:
                continue
        text_bits.append(body[:10000])
        items.append(
            _evidence_item(
                row=row,
                source_url=hit.get("final_url") or u,
                source_type="public_web_recovery",
                authority=AUTH_SECONDARY,
                title=row.get("title"),
                document_type="WEB_RECOVERY",
                text=body[:15000],
                confidence="LOW",
                relevance="POSSIBLE",
                tier=TIER_3_WEB,
            )
        )
        for link in _extract_links(body, hit.get("final_url") or u):
            if link["kind"] == "attachment":
                docs.append({"url": link["url"], "title": link["url"].rsplit("/", 1)[-1], "source": "web_recovery"})

    return {
        "tier": TIER_3_WEB,
        "items": items,
        "attempted": True,
        "documents": docs,
        "fetched_text": "\n".join(text_bits),
        "queries": queries,
        "candidate_urls": found_urls[:10],
    }


def _tier4_openai_web(row: dict[str, Any], *, allow_paid: bool = True) -> dict[str, Any]:
    """Controlled OpenAI web search — Cost Governor / budget gated."""
    if not allow_paid:
        return {"tier": TIER_4_OPENAI_WEB, "items": [], "attempted": False, "reason": "paid_blocked"}
    if (os.environ.get("M3_EVIDENCE_OPENAI_WEB") or "true").strip().lower() in {"0", "false", "no"}:
        return {"tier": TIER_4_OPENAI_WEB, "items": [], "attempted": False, "reason": "disabled"}

    sol = str(row.get("solicitation_number") or row.get("external_id") or "")
    title = str(row.get("title") or "")
    agency = str(row.get("agency") or "")
    query_fp = _fp(f"{sol}|{title}|{agency}|evidence_pkg_v1")
    # Idempotent: skip identical paid search fingerprint
    prior = (row.get("openai_web_search") or {}).get("query_fingerprint")
    if prior == query_fp and (row.get("openai_web_search") or {}).get("result"):
        return {
            "tier": TIER_4_OPENAI_WEB,
            "items": [],
            "attempted": True,
            "reused": True,
            "query_fingerprint": query_fp,
            "result": (row.get("openai_web_search") or {}).get("result"),
        }

    try:
        from cost_governor import get_cost_governor

        gov = get_cost_governor()
        if hasattr(gov, "authorize"):
            auth = gov.authorize(
                {
                    "action_type": "DEEP_RESEARCH",
                    "estimated_cost_usd": 0.15,
                    "priority_tier": 3,
                    "voi_score": 0.7,
                    "opportunity_id": row.get("canonical_id"),
                }
            )
            if isinstance(auth, dict) and not auth.get("authorized", True):
                return {"tier": TIER_4_OPENAI_WEB, "items": [], "attempted": False, "reason": "cost_governor_blocked"}
    except Exception:
        pass

    try:
        from openai_runtime import create_response, text_part
        from ai_model_router import FunnelStage

        instructions = (
            "Find authoritative public procurement package URLs for this exact solicitation. "
            "Return JSON only: {\"package_urls\":[...],\"attachment_urls\":[...],\"notes\":str,"
            "\"auth_required\":bool,\"confidence\":\"HIGH|MEDIUM|LOW\"}. "
            "Do not invent URLs. Prefer government domains. Reject unrelated solicitations."
        )
        content = [
            text_part(
                json.dumps(
                    {
                        "solicitation_number": sol,
                        "title": title,
                        "agency": agency,
                        "known_url": detail_url(row),
                        "source_id": row.get("source_id"),
                    },
                    default=str,
                )
            )
        ]
        raw = create_response(
            task="m3_evidence_package_recovery",
            instructions=instructions,
            content=content,
            max_output_tokens=800,
            web_search=True,
            funnel_stage=FunnelStage.STAGE_3,
            automatic=True,
            notice_id=str(row.get("canonical_id") or sol or "")[:80] or None,
        )
        from openai_runtime import extract_json_object

        parsed = extract_json_object(raw) if raw else {}
        items = []
        docs = list(row.get("documents") or []) if isinstance(row.get("documents"), list) else []
        text_bits = []
        for u in list(parsed.get("package_urls") or [])[:5] + list(parsed.get("attachment_urls") or [])[:5]:
            if not isinstance(u, str) or not u.startswith("http"):
                continue
            hit = _http_get(u, source_id="openai_web_located")
            if hit.get("ok") and hit.get("text"):
                body = hit["text"]
                # Validate belongs to opportunity
                if sol and sol.lower() not in body.lower() and sol.lower() not in u.lower():
                    continue
                text_bits.append(body[:10000])
                items.append(
                    _evidence_item(
                        row=row,
                        source_url=hit.get("final_url") or u,
                        source_type="openai_web_search",
                        authority=AUTH_SECONDARY,
                        title=row.get("title"),
                        document_type="WEB_SEARCH_LOCATED",
                        text=body[:15000],
                        confidence=str(parsed.get("confidence") or "MEDIUM"),
                        relevance="CONFIRMED",
                        tier=TIER_4_OPENAI_WEB,
                    )
                )
                docs.append({"url": u, "title": u.rsplit("/", 1)[-1], "source": "openai_web_search", "text_preview": body[:2000]})
        return {
            "tier": TIER_4_OPENAI_WEB,
            "items": items,
            "attempted": True,
            "documents": docs,
            "fetched_text": "\n".join(text_bits),
            "query_fingerprint": query_fp,
            "result": parsed,
            "auth_required": bool(parsed.get("auth_required")),
            "paid": True,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("OpenAI web evidence recovery failed")
        return {"tier": TIER_4_OPENAI_WEB, "items": [], "attempted": True, "error": str(exc)[:300], "query_fingerprint": query_fp}


def _extract_line_hints(text: str) -> list[dict[str, Any]]:
    """Best-effort public line-item hints from recovered text — UNKNOWN when weak."""
    lines: list[dict[str, Any]] = []
    if not text:
        return lines
    # qty + unit patterns
    for m in re.finditer(
        r"(?P<item>.{8,80}?)\s+(?:qty\.?|quantity)\s*[:\s]*(?P<qty>\d[\d,]*)\s*(?P<unit>ea|each|lot|box|case|gal|lb|ton|bag|unit)?",
        text,
        re.I,
    ):
        lines.append(
            {
                "description": m.group("item").strip()[:120],
                "quantity": int(m.group("qty").replace(",", "")),
                "unit": (m.group("unit") or "EA").upper(),
                "confidence": "LOW",
                "source": "text_hint",
            }
        )
        if len(lines) >= 12:
            break
    # Part number hints
    if not lines:
        for m in re.finditer(r"\b(?:P/?N|Part\s*Number|Model|NSN|SKU)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-./]{3,})\b", text, re.I):
            lines.append(
                {
                    "description": f"Part {m.group(1)}",
                    "part_number": m.group(1),
                    "quantity": None,
                    "unit": "EA",
                    "confidence": "LOW",
                    "source": "part_hint",
                }
            )
            if len(lines) >= 8:
                break
    return lines


def acquire_evidence(
    row: dict[str, Any],
    *,
    allow_paid: bool = True,
    max_tier: str = TIER_4_OPENAI_WEB,
) -> dict[str, Any]:
    """Run progressive evidence ladder; return patched row fields + audit."""
    row = deepcopy(row)
    attempts: list[dict[str, Any]] = list(row.get("evidence_recovery_attempts") or [])
    recovered: list[dict[str, Any]] = list(row.get("recovered_evidence") or [])
    docs: list[dict[str, Any]] = list(row.get("documents") or []) if isinstance(row.get("documents"), list) else []
    text_corpus: list[str] = []
    paid_actions = 0
    tiers_order = [TIER_0_EXISTING, TIER_1_DIRECT, TIER_2_ALTERNATE, TIER_3_WEB, TIER_4_OPENAI_WEB]
    stop_at = tiers_order.index(max_tier) if max_tier in tiers_order else len(tiers_order) - 1

    def _enough() -> bool:
        return has_line_items(row) or len(text_corpus) > 2500

    # T0
    t0 = _tier0_existing(row)
    attempts.append({"tier": TIER_0_EXISTING, "at": _utc(), "items": len(t0["items"])})
    recovered.extend(t0["items"])
    for it in t0["items"]:
        if it.get("text_preview"):
            text_corpus.append(it["text_preview"])

    if stop_at >= 1 and not _enough():
        t1 = _tier1_direct(row)
        attempts.append(
            {
                "tier": TIER_1_DIRECT,
                "at": _utc(),
                "items": len(t1.get("items") or []),
                "http_status": t1.get("http_status"),
                "source_access_state": t1.get("source_access_state"),
            }
        )
        recovered.extend(t1.get("items") or [])
        if t1.get("documents"):
            docs = t1["documents"]
        if t1.get("fetched_text"):
            text_corpus.append(t1["fetched_text"])
        if t1.get("source_access_state"):
            row["source_access_state"] = t1["source_access_state"]
            if t1["source_access_state"] in {SA_AUTH_REQUIRED, SA_REGISTRATION_REQUIRED}:
                row["package_access"] = (
                    "REGISTRATION_REQUIRED"
                    if t1["source_access_state"] == SA_REGISTRATION_REQUIRED
                    else "AUTH_GATED"
                )
                row["auth_required_for_spec"] = True

    if stop_at >= 2 and not _enough() and row.get("source_access_state") not in {SA_BOT_PROTECTED}:
        t2 = _tier2_alternate(row)
        attempts.append(
            {
                "tier": TIER_2_ALTERNATE,
                "at": _utc(),
                "items": len(t2.get("items") or []),
                "urls_tried": t2.get("urls_tried"),
                "package_found_alternate": t2.get("package_found_alternate"),
            }
        )
        recovered.extend(t2.get("items") or [])
        if t2.get("documents"):
            docs = t2["documents"]
        if t2.get("fetched_text"):
            text_corpus.append(t2["fetched_text"])
        if t2.get("package_found_alternate"):
            row["evidence_notes"] = PACKAGE_FOUND_IN_ALTERNATE_SOURCE

    if stop_at >= 3 and not _enough() and row.get("source_access_state") not in {SA_AUTH_REQUIRED, SA_REGISTRATION_REQUIRED, SA_BOT_PROTECTED}:
        t3 = _tier3_public_web(row)
        attempts.append({"tier": TIER_3_WEB, "at": _utc(), "items": len(t3.get("items") or []), "queries": t3.get("queries")})
        recovered.extend(t3.get("items") or [])
        if t3.get("documents"):
            docs = t3["documents"]
        if t3.get("fetched_text"):
            text_corpus.append(t3["fetched_text"])

    if stop_at >= 4 and not _enough() and row.get("source_access_state") not in {SA_BOT_PROTECTED}:
        t4 = _tier4_openai_web(row, allow_paid=allow_paid)
        attempts.append(
            {
                "tier": TIER_4_OPENAI_WEB,
                "at": _utc(),
                "items": len(t4.get("items") or []),
                "attempted": t4.get("attempted"),
                "reason": t4.get("reason"),
                "paid": t4.get("paid"),
                "error": t4.get("error"),
            }
        )
        if t4.get("paid"):
            paid_actions += 1
        recovered.extend(t4.get("items") or [])
        if t4.get("documents"):
            docs = t4["documents"]
        if t4.get("fetched_text"):
            text_corpus.append(t4["fetched_text"])
        if t4.get("query_fingerprint"):
            row["openai_web_search"] = {
                "query_fingerprint": t4["query_fingerprint"],
                "result": t4.get("result"),
                "at": _utc(),
            }
        if t4.get("auth_required"):
            row["source_access_state"] = SA_AUTH_REQUIRED
            row["package_access"] = "AUTH_GATED"
            row["auth_required_for_spec"] = True

    combined = "\n".join(text_corpus)
    if combined and not has_line_items(row):
        hints = _extract_line_hints(combined)
        if hints:
            row["line_items"] = hints
            row["bom"] = hints
            row["requirements_insufficient"] = False
            row["line_items_confidence"] = "LOW"
            row["package_acquired"] = True

    if combined:
        row.setdefault("description", "")
        if len(combined) > len(str(row.get("description") or "")):
            row["evidence_text_excerpt"] = combined[:8000]
        row["documents"] = docs
        # Governing document map when possible
        try:
            from governing_documents import build_package_map

            pkg_map = build_package_map(
                solicitation_id=str(row.get("canonical_id") or row.get("solicitation_number") or "unknown"),
                documents=[
                    {
                        "document_id": d.get("url") or d.get("title") or f"doc-{i}",
                        "filename": d.get("title"),
                        "source_url": d.get("url"),
                        "extracted_text_available": bool(d.get("text_preview") or d.get("text")),
                        "governing": True,
                    }
                    for i, d in enumerate(docs)
                    if isinstance(d, dict)
                ]
                or [
                    {
                        "document_id": "recovered-corpus",
                        "filename": row.get("title"),
                        "source_url": detail_url(row),
                        "extracted_text_available": bool(combined),
                        "governing": True,
                    }
                ],
            )
            row["governing_documents"] = pkg_map
            row["solicitation_package_map"] = pkg_map
        except Exception:
            pass

    deal = classify_deal_type(row, evidence_text=combined[:12000])
    row["deal_type"] = deal.get("deal_type")
    row["deal_type_assessment"] = deal
    if deal.get("product_category"):
        row["product_category"] = (deal["product_category"] or {}).get("category")
        row["product_audit"] = deal["product_category"]

    # Reject / deprioritize when evidence proves non-product
    if deal.get("not_transactional_solicitation"):
        row["rejected"] = True
        row["stop_reason"] = "not_transactional_solicitation"
        row["lifecycle"] = "REJECTED"
    elif deal.get("deal_type") == DT_SERVICE and deal.get("confidence") == "HIGH":
        row["rejected"] = True
        row["stop_reason"] = "service_not_product_resale"
        row["lifecycle"] = "REJECTED"
    elif deal.get("deal_type") == DT_CONSTRUCTION and deal.get("confidence") == "HIGH":
        row["rejected"] = True
        row["stop_reason"] = "construction_not_product_resale"
        row["lifecycle"] = "REJECTED"

    if not row.get("source_access_state"):
        if detail_url(row) and not docs and not combined:
            row["source_access_state"] = SA_PUBLIC_METADATA_ONLY
        elif detail_url(row):
            row["source_access_state"] = SA_PUBLIC

    failure = classify_evidence_failure({**row, "evidence_recovery_attempts": attempts, "documents": docs})
    row["evidence_recovery_attempts"] = attempts[-20:]
    row["recovered_evidence"] = recovered[-40:]
    row["evidence_failure"] = failure
    row["evidence_acquisition"] = {
        "kind": "M3EvidenceAcquisitionResult",
        "at": _utc(),
        "tiers_attempted": [a.get("tier") for a in attempts],
        "recovered_count": len(recovered),
        "document_count": len(docs),
        "has_line_items": has_line_items(row),
        "paid_actions": paid_actions,
        "primary_failure": failure.get("primary_reason"),
        "deal_type": row.get("deal_type"),
        "source_access_state": row.get("source_access_state"),
        "improved": bool(has_line_items(row) or row.get("rejected") or len(combined) > 500),
    }
    # Allow research re-run after new evidence: clear prior research fingerprint when improved
    if row["evidence_acquisition"]["improved"]:
        row.pop("research_completed_fingerprint", None)
        row["research_queued"] = True
        row["evidence_invalidated_at"] = _utc()

    return {
        "row": row,
        "result": row["evidence_acquisition"],
        "failure": failure,
        "paid_actions": paid_actions,
    }


def evidence_access_summary(opportunities: list[dict[str, Any]]) -> dict[str, Any]:
    """Home EVIDENCE ACCESS aggregate from live pipeline rows."""
    deferred = 0
    public_recovery = 0
    packages_recovered = 0
    auth_blocked = 0
    web_pending = 0
    unresolved = 0
    rejected_evidence = 0
    advanced = 0
    for row in opportunities:
        lc = str(row.get("lifecycle") or "")
        if lc in {"REJECTED", "REJECTED_CHEAP_SCREEN"}:
            if (row.get("evidence_acquisition") or {}).get("improved"):
                rejected_evidence += 1
            continue
        if lc not in {"RESEARCH_QUEUED", "RESEARCH_IN_PROGRESS", "PACKAGE_REQUIRED", "CHEAP_SCREENED"}:
            if has_line_items(row) or lc not in {"DISCOVERED", "NORMALIZED"}:
                if has_line_items(row) or lc in {
                    "BOM_READY",
                    "REQUIREMENTS_PARSED",
                    "ECONOMICS_IN_PROGRESS",
                    "ECONOMICS_PRELIMINARY",
                    "ECONOMICS_ATTRACTIVE",
                    "READY_FOR_OPERATOR_ACTION",
                }:
                    advanced += 1
            continue
        deferred += 1
        ea = row.get("evidence_acquisition") or {}
        fail = (row.get("evidence_failure") or {}).get("primary_reason") or ""
        access = str(row.get("source_access_state") or "")
        if access in {SA_AUTH_REQUIRED, SA_REGISTRATION_REQUIRED} or fail in {AUTH_REQUIRED, REGISTRATION_REQUIRED}:
            auth_blocked += 1
        elif ea.get("has_line_items") or row.get("package_acquired"):
            packages_recovered += 1
        elif WEB_SEARCH_NOT_TRIED in ((row.get("evidence_failure") or {}).get("reasons") or []):
            web_pending += 1
            public_recovery += 1
        elif fail in {
            PACKAGE_URL_FOUND_NOT_FETCHED,
            PACKAGE_URL_MISSING,
            ATTACHMENT_NOT_FETCHED,
            ALTERNATE_ROUTE_NOT_TRIED,
            LISTING_ONLY,
            PUBLIC_METADATA_ONLY,
        }:
            public_recovery += 1
        else:
            unresolved += 1
    return {
        "kind": "M3EvidenceAccessSummary",
        "deferred": deferred,
        "public_recovery_candidates": public_recovery,
        "packages_recovered": packages_recovered,
        "auth_registration_blocked": auth_blocked,
        "web_research_pending": web_pending,
        "genuinely_unresolved": unresolved,
        "rejected_via_evidence": rejected_evidence,
        "advanced_with_evidence": advanced,
    }
