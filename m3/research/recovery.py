"""Progressive classification recovery (Levels 1–4) for the full uncertain population."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from m3.research.classify import PRODUCT_CLASSES, classify_requirement, classify_solicitation_item
from m3.research.documents import fetch_and_extract, find_attachment_urls
from m3.research.http_util import fetch_text

RESOLVED_NON_PRODUCT = {
    "services",
    "professional_services",
    "maintenance_repair_services",
    "construction",
    "software_only",
}
RESOLVED_PRODUCT = PRODUCT_CLASSES | {"mixed_product_service"}


def _is_uncertain(item: dict[str, Any]) -> bool:
    req = (item.get("requirement_class") or "").lower()
    res = (item.get("uncertain_resolution") or "").lower()
    return req in {"unknown_mixed", "unknown", "pending_classification", ""} or res in {
        "requires_further_verification",
        "",
    }


def _level1_classify(item: dict[str, Any]) -> dict[str, Any]:
    """Use only existing normalized fields."""
    out = classify_solicitation_item(item)
    out["recovery_level"] = 1
    out["recovery_evidence"] = list(out.get("classification_evidence") or [])
    return out


def _still_uncertain(item: dict[str, Any]) -> bool:
    req = (item.get("requirement_class") or "").lower()
    if req in RESOLVED_PRODUCT | RESOLVED_NON_PRODUCT:
        # Forecast / closed stay tracked but are "resolved" for backlog purposes if class is clear
        return False
    return _is_uncertain(item)


def _level2_detail(item: dict[str, Any], *, cache_get, cache_put) -> dict[str, Any]:
    url = item.get("official_url") or ""
    path_l = url.split("?")[0].lower()
    if not url.startswith("http") or ".json" in path_l:
        item = dict(item)
        item["recovery_level"] = max(int(item.get("recovery_level") or 1), 2)
        item["recovery_blockers"] = list(item.get("recovery_blockers") or []) + ["no_usable_detail_url"]
        return item
    # Document URLs belong in Level 3 — do not fetch PDFs as HTML
    if any(path_l.endswith(ext) for ext in (".pdf", ".docx", ".xlsx", ".xls", ".csv", ".doc", ".zip")):
        out = dict(item)
        out["attachment_candidates"] = [url] + list(item.get("attachment_candidates") or [])
        out["recovery_level"] = 2
        out["recovery_blockers"] = list(out.get("recovery_blockers") or []) + ["detail_is_document_deferred_to_l3"]
        return out
    ck = f"detail_html:{(url or '')[:180]}"
    cached = cache_get(ck) if cache_get else None
    html = None
    final = url
    if cached and cached.get("text"):
        html = cached["text"]
        final = cached.get("url") or url
    else:
        try:
            status, final, html = fetch_text(url, timeout=8)
            if status >= 400 or not html:
                item = dict(item)
                item["recovery_level"] = 2
                item["recovery_blockers"] = list(item.get("recovery_blockers") or []) + [f"detail_http_{status}"]
                return item
            # Detect PDF/binary mislabeled as HTML
            if isinstance(html, str) and (html.startswith("%PDF") or "\x00" in html[:500]):
                out = dict(item)
                out["attachment_candidates"] = [final] + list(item.get("attachment_candidates") or [])
                out["recovery_level"] = 2
                out["recovery_blockers"] = list(out.get("recovery_blockers") or []) + ["detail_binary_deferred_to_l3"]
                return out
            if cache_put:
                from m3.research.documents import extract_html_text as _eh

                cache_put(ck, "detail_html", {"text": _eh(html)[:100000], "url": final}, hours=72)
        except Exception as exc:
            item = dict(item)
            item["recovery_level"] = 2
            item["recovery_blockers"] = list(item.get("recovery_blockers") or []) + [f"detail_error:{type(exc).__name__}"]
            return item

    from m3.research.documents import extract_html_text

    text = extract_html_text(html)
    out = dict(item)
    out["description"] = (out.get("description") or "") or text[:1500]
    out["document_text"] = text[:6000]
    out["_document_searchable"] = True
    out["attachment_candidates"] = find_attachment_urls(html, final)
    clf = classify_requirement(
        str(out.get("title") or ""),
        category_raw=(out.get("evidence") or {}).get("category_raw") if isinstance(out.get("evidence"), dict) else out.get("product_category"),
        procurement_type=out.get("procurement_type"),
        description=out.get("description"),
        document_text=out["document_text"],
        line_items_text=out.get("line_items_text"),
    )
    out["requirement_class"] = clf["requirement_class"]
    out["classification_evidence"] = list(clf.get("classification_evidence") or []) + ["level2_detail_page"]
    out["include_in_product_resale"] = clf.get("include_in_product_resale")
    out["evidence_sources"] = clf.get("evidence_sources")
    out["recovery_level"] = 2
    out["recovery_evidence"] = out["classification_evidence"]
    return out


def _level3_attachments(item: dict[str, Any], *, cache_get, cache_put, max_attachments: int = 2) -> dict[str, Any]:
    urls = list(item.get("attachment_candidates") or [])
    # Also try official_url if it looks like a document
    ou = item.get("official_url") or ""
    if ou and any(ou.lower().split("?")[0].endswith(ext) for ext in (".pdf", ".docx", ".xlsx", ".csv", ".txt")):
        urls = [ou] + urls
    texts: list[str] = []
    blockers: list[str] = []
    used: list[str] = []
    for u in urls[:max_attachments]:
        ck = f"doc_extract_body:{u[:160]}"
        cached = cache_get(ck) if cache_get else None
        if cached and cached.get("ok") and cached.get("text"):
            texts.append(cached["text"])
            used.append(u)
            continue
        if cached and cached.get("ok") is False:
            blockers.append(cached.get("reason") or "cached_failure")
            continue
        extracted = fetch_and_extract(u)
        if cache_put:
            cache_put(
                extracted.get("cache_key") or ck,
                "document_extract",
                {
                    "ok": extracted.get("ok"),
                    "text": (extracted.get("text") or "")[:12000],
                    "reason": extracted.get("reason"),
                    "format": extracted.get("format"),
                    "url": extracted.get("url"),
                },
                hours=168,
            )
        if extracted.get("ok") and extracted.get("text"):
            texts.append(extracted["text"])
            used.append(extracted.get("url") or u)
        else:
            blockers.append(extracted.get("reason") or "attachment_inaccessible")
    out = dict(item)
    out["recovery_level"] = 3
    if not texts:
        out["recovery_blockers"] = list(out.get("recovery_blockers") or []) + (blockers or ["no_attachments_extracted"])
        # Explicit: inaccessible documents are not successful investigations
        out["attachments_investigated"] = False
        return out
    combined = " ".join(texts)[:10000]
    out["document_text"] = ((out.get("document_text") or "") + " " + combined)[:12000]
    out["_document_searchable"] = True
    out["attachments_investigated"] = True
    out["attachments_used"] = used
    clf = classify_requirement(
        str(out.get("title") or ""),
        category_raw=(out.get("evidence") or {}).get("category_raw") if isinstance(out.get("evidence"), dict) else out.get("product_category"),
        procurement_type=out.get("procurement_type"),
        description=out.get("description"),
        document_text=out["document_text"],
        line_items_text=out.get("line_items_text"),
    )
    out["requirement_class"] = clf["requirement_class"]
    out["classification_evidence"] = list(clf.get("classification_evidence") or []) + [
        "level3_attachments",
        f"attachments={len(used)}",
    ]
    out["include_in_product_resale"] = clf.get("include_in_product_resale")
    out["recovery_evidence"] = out["classification_evidence"]
    if blockers:
        out["recovery_blockers"] = list(out.get("recovery_blockers") or []) + blockers
    return out


def _level4_openai(
    db: Session,
    item: dict[str, Any],
    *,
    user_id,
    research_run_id,
    job_spent: Decimal,
) -> tuple[dict[str, Any], Decimal]:
    """Selective AI only when deterministic levels failed."""
    from m3.research.openai_client import web_research

    title = (item.get("title") or "")[:300]
    snippet = (item.get("document_text") or item.get("description") or "")[:2500]
    schema = (
        '{"requirement_class":"product_supply|product_with_incidental_services|'
        'product_with_significant_installation|product_rental_leasing|software_only|'
        'professional_services|maintenance_repair_services|construction|mixed_product_service|'
        'services|unknown_mixed","confidence":"low|medium|high","rationale":"","evidence_quotes":[""]}'
    )
    prompt = (
        "Classify this U.S. government solicitation requirement for purchasing type. "
        "Decide what is actually being purchased. Do not invent facts not present in the text. "
        "If evidence is insufficient, return unknown_mixed.\n"
        f"Title: {title}\n"
        f"Evidence text: {snippet or '(none)'}\n"
        "Prefer product_supply when the buyer is acquiring tangible goods/materials/equipment. "
        "Prefer services/maintenance_repair_services/construction when labor is the primary purchase."
    )
    ai = web_research(
        db,
        user_id=user_id,
        objective="classification_recovery",
        prompt=prompt,
        research_run_id=research_run_id,
        json_schema_hint=schema,
        job_spent_usd=job_spent,
    )
    out = dict(item)
    out["recovery_level"] = 4
    cost = Decimal(str(ai.get("estimated_cost_usd") or 0))
    if not ai.get("ok"):
        out["recovery_blockers"] = list(out.get("recovery_blockers") or []) + [
            f"openai:{ai.get('error') or 'denied_or_failed'}"
        ]
        out["uncertain_resolution"] = "requires_further_verification"
        return out, cost
    parsed = ai.get("parsed") or {}
    cls = str(parsed.get("requirement_class") or "unknown_mixed").strip().lower()
    allowed = RESOLVED_PRODUCT | RESOLVED_NON_PRODUCT | {"unknown_mixed", "services"}
    if cls not in allowed:
        cls = "unknown_mixed"
    conf = str(parsed.get("confidence") or "low").lower()
    # Do not force verified product on low-confidence AI
    if cls in RESOLVED_PRODUCT and conf == "low":
        out["requirement_class"] = "unknown_mixed"
        out["uncertain_resolution"] = "requires_further_verification"
        out["classification_evidence"] = [
            "level4_openai_low_confidence_not_forced",
            str(parsed.get("rationale") or "")[:200],
        ]
    else:
        out["requirement_class"] = cls
        out["uncertain_resolution"] = cls if cls != "unknown_mixed" else "requires_further_verification"
        out["classification_evidence"] = [
            "level4_openai",
            f"confidence={conf}",
            str(parsed.get("rationale") or "")[:240],
        ] + list(parsed.get("evidence_quotes") or [])[:3]
        out["include_in_product_resale"] = cls in RESOLVED_PRODUCT | {"unknown_mixed", "mixed_product_service"}
    out["recovery_evidence"] = out["classification_evidence"]
    out["openai_classification"] = True
    return out, cost


def recover_uncertain_population(
    db: Session,
    items: list[dict[str, Any]],
    *,
    user_id=None,
    research_run_id=None,
    enable_openai: bool = True,
    max_level2: int | None = None,
    max_level3: int | None = None,
    max_level4: int = 25,
    job_spent_usd: Decimal | None = None,
    cache_get=None,
    cache_put=None,
) -> dict[str, Any]:
    """Run L1→L4 recovery over the full uncertain population (no small-sample default)."""
    spent = Decimal(str(job_spent_usd or 0))
    if max_level2 is None and os.getenv("M3_RECOVERY_MAX_LEVEL2"):
        max_level2 = int(os.environ["M3_RECOVERY_MAX_LEVEL2"])
    if max_level3 is None and os.getenv("M3_RECOVERY_MAX_LEVEL3"):
        max_level3 = int(os.environ["M3_RECOVERY_MAX_LEVEL3"])
    level2_budget = max_level2 if max_level2 is not None else 50_000
    level3_budget = max_level3 if max_level3 is not None else 10_000
    out_items: list[dict] = []
    stats = {
        "investigated": 0,
        "resolved_product": 0,
        "resolved_product_incidental": 0,
        "resolved_product_installation": 0,
        "resolved_non_product": 0,
        "resolved_other": 0,
        "remaining_uncertain": 0,
        "level1_resolved": 0,
        "level2_attempted": 0,
        "level2_resolved": 0,
        "level3_attempted": 0,
        "level3_resolved": 0,
        "level4_attempted": 0,
        "level4_resolved": 0,
        "openai_spent_usd": 0.0,
        "unresolved_reasons": {},
        "skipped_already_classified": 0,
    }

    uncertain_idxs: list[int] = []
    for i, it in enumerate(items):
        if _is_uncertain(it):
            uncertain_idxs.append(i)
        else:
            stats["skipped_already_classified"] += 1

    # Work on copies
    working = [dict(it) for it in items]

    for idx in uncertain_idxs:
        stats["investigated"] += 1
        cur = _level1_classify(working[idx])
        if not _still_uncertain(cur):
            stats["level1_resolved"] += 1
            working[idx] = cur
            continue
        working[idx] = cur

    # Level 2 — all remaining uncertain; dedupe by official_url to avoid repeat fetches
    remaining = [i for i in uncertain_idxs if _still_uncertain(working[i])][:level2_budget]
    url_to_idxs: dict[str, list[int]] = {}
    for idx in remaining:
        u = (working[idx].get("official_url") or "").strip()
        url_to_idxs.setdefault(u or f"__noid_{idx}", []).append(idx)
    unique_idxs = [idxs[0] for idxs in url_to_idxs.values()]
    stats["level2_attempted"] = len(unique_idxs)
    stats["level2_url_groups"] = len(url_to_idxs)
    if unique_idxs:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _l2(i: int) -> tuple[int, dict]:
            try:
                return i, _level2_detail(working[i], cache_get=cache_get, cache_put=cache_put)
            except Exception as exc:
                cur = dict(working[i])
                cur["recovery_level"] = 2
                cur["recovery_blockers"] = list(cur.get("recovery_blockers") or []) + [
                    f"level2_exception:{type(exc).__name__}"
                ]
                return i, cur

        workers = min(8, max(2, int(os.getenv("M3_RECOVERY_L2_WORKERS", "8"))))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_l2, idx) for idx in unique_idxs]
            for fut in as_completed(futs):
                idx, cur = fut.result()
                done += 1
                if done % 100 == 0:
                    print(f"recovery_l2_progress {done}/{len(unique_idxs)}", flush=True)
                # Propagate result to all items sharing the same URL
                u = (working[idx].get("official_url") or "").strip() or f"__noid_{idx}"
                if not _still_uncertain(cur):
                    stats["level2_resolved"] += len(url_to_idxs.get(u, [idx]))
                for j in url_to_idxs.get(u, [idx]):
                    merged = dict(working[j])
                    for k in (
                        "requirement_class",
                        "classification_evidence",
                        "include_in_product_resale",
                        "document_text",
                        "description",
                        "attachment_candidates",
                        "recovery_level",
                        "recovery_evidence",
                        "recovery_blockers",
                        "_document_searchable",
                        "uncertain_resolution",
                    ):
                        if k in cur:
                            merged[k] = cur[k]
                    working[j] = merged

    # Level 3 — attachments for still-uncertain with candidates or document URLs
    remaining = [i for i in uncertain_idxs if _still_uncertain(working[i])]
    l3_n = 0
    for idx in remaining:
        if l3_n >= level3_budget:
            break
        it = working[idx]
        if not (it.get("attachment_candidates") or (
            (it.get("official_url") or "").lower().split("?")[0].endswith((".pdf", ".docx", ".xlsx", ".csv"))
        )):
            continue
        l3_n += 1
        stats["level3_attempted"] += 1
        cur = _level3_attachments(it, cache_get=cache_get, cache_put=cache_put)
        if not _still_uncertain(cur):
            stats["level3_resolved"] += 1
        working[idx] = cur

    # Level 4 — selective OpenAI for remaining with some evidence text
    if enable_openai:
        remaining = [i for i in uncertain_idxs if _still_uncertain(working[i])]
        l4 = 0
        for idx in remaining:
            if l4 >= max_level4:
                break
            it = working[idx]
            evidence_len = len(it.get("document_text") or "") + len(it.get("description") or "")
            if evidence_len < 80:
                # Prefer records with evidence; skip empty titles-only for paid AI
                continue
            l4 += 1
            stats["level4_attempted"] += 1
            cur, cost = _level4_openai(
                db, it, user_id=user_id, research_run_id=research_run_id, job_spent=spent
            )
            spent += cost
            stats["openai_spent_usd"] = float(spent)
            if not _still_uncertain(cur):
                stats["level4_resolved"] += 1
            working[idx] = cur

    # Tally only among originally uncertain indices
    for idx in uncertain_idxs:
        it = working[idx]
        req = (it.get("requirement_class") or "").lower()
        if req == "product_with_incidental_services":
            stats["resolved_product_incidental"] += 1
            it["uncertain_resolution"] = req
        elif req == "product_with_significant_installation":
            stats["resolved_product_installation"] += 1
            it["uncertain_resolution"] = req
        elif req in RESOLVED_PRODUCT:
            stats["resolved_product"] += 1
            it["uncertain_resolution"] = req
        elif req in RESOLVED_NON_PRODUCT:
            stats["resolved_non_product"] += 1
            it["uncertain_resolution"] = req
        elif req in {"forecast"} or (it.get("solicitation_status") or "") == "forecast":
            stats["resolved_other"] += 1
            it["uncertain_resolution"] = "forecast"
        elif _still_uncertain(it) or _is_uncertain(it):
            stats["remaining_uncertain"] += 1
            it["uncertain_resolution"] = "requires_further_verification"
            reason = (it.get("recovery_blockers") or it.get("classification_evidence") or ["insufficient_evidence"])[-1]
            key = str(reason)[:100]
            stats["unresolved_reasons"][key] = stats["unresolved_reasons"].get(key, 0) + 1
        else:
            stats["resolved_other"] += 1
            it["uncertain_resolution"] = req

    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    stats["openai_spent_usd"] = float(spent - Decimal(str(job_spent_usd or 0)))
    return {"items": working, "stats": stats, "job_spent_usd": float(spent)}
