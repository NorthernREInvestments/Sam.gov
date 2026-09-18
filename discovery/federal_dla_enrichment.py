"""Progressive Federal/DLA enrichment — description → package refs → extraction → readiness.

Bounded, checkpointed, no DIBBS/auth bypass, no outreach.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.dla_product_extract import (
    classify_federal_product_cheap,
    compute_product_transaction_readiness,
    enrich_with_dla_structure,
)
from discovery.federal_description_recovery import apply_description_recovery, recover_description
from discovery.federal_package_refs import classify_technical_data_state, enumerate_package_references
from federal_dla_product_constants import (
    DESCRIPTION_PUBLIC_RECOVERED,
    ENRICHMENT_CHECKPOINT_KEY,
    ENRICHMENT_VERSION,
    READY_COMMERCIAL_RESEARCH,
    READY_HISTORICAL_RESEARCH,
    REF_SOLICITATION_DOCUMENT,
    REF_ATTACHMENT,
    REF_SAM_DESCRIPTION,
)

# Soft public retrieval for a few high-value package URLs (not bulk)
_FETCHABLE_REF_CLASSES = {REF_SOLICITATION_DOCUMENT, REF_ATTACHMENT, REF_SAM_DESCRIPTION}


def _utc() -> str:
    return now_utc().isoformat()


def _ckpt_path() -> Path:
    return Path(__file__).resolve().parent / "artifacts" / "m3_federal_dla_enrichment.json"


def load_enrichment_checkpoint() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == ENRICHMENT_CHECKPOINT_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value) if isinstance(row.value, str) else row.value
                if isinstance(data, dict):
                    return data
        finally:
            db.close()
    except Exception:
        pass
    if _ckpt_path().exists():
        try:
            return json.loads(_ckpt_path().read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {"kind": "M3FederalDlaEnrichment", "by_id": {}, "version": ENRICHMENT_VERSION}


def save_enrichment_checkpoint(payload: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(payload)
    out["kind"] = "M3FederalDlaEnrichment"
    out["version"] = ENRICHMENT_VERSION
    out["updated_at"] = _utc()
    raw = json.dumps(out, default=str)
    try:
        _ckpt_path().parent.mkdir(parents=True, exist_ok=True)
        _ckpt_path().write_text(raw, encoding="utf-8")
    except Exception:
        pass
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == ENRICHMENT_CHECKPOINT_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=ENRICHMENT_CHECKPOINT_KEY, value=raw))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
    return out


def enrichment_priority_score(row: dict[str, Any]) -> int:
    """Higher = enrich sooner. Progressive research — not all 25k."""
    score = 0
    sem = str(row.get("notice_semantic_class") or "")
    if sem == "BID_OR_QUOTE_READY" or row.get("bid_quote_ready"):
        score += 100
    if row.get("is_dla"):
        score += 40
    cls = str(row.get("federal_product_class") or row.get("product_classification") or "")
    if cls in {"FEDERAL_PRODUCT_LIKELY", "CORE_PRODUCT", "FEDERAL_PRODUCT_PLUS_MINOR_SERVICE"}:
        score += 30
    if row.get("exact_nsn") or row.get("exact_part_number"):
        score += 10
    # Prefer those with description URLs (recoverable value)
    desc = row.get("description")
    links = row.get("document_links") or []
    if isinstance(desc, str) and desc.startswith("http"):
        score += 25
    if any(isinstance(l, dict) and str(l.get("kind")) == "sam_description" for l in links):
        score += 25
    if sem in {"AWARD_OR_HISTORY", "MARKET_RESEARCH"}:
        score -= 50
    return score


def select_enrichment_candidates(
    opportunities: list[dict[str, Any]],
    *,
    limit: int = 100,
    prefer_dla: bool = True,
) -> list[dict[str, Any]]:
    scored = []
    for r in opportunities:
        if prefer_dla and not (
            r.get("is_dla")
            or r.get("bid_quote_ready")
            or str(r.get("notice_semantic_class")) == "BID_OR_QUOTE_READY"
        ):
            # still allow high product-likely federal
            if str(r.get("federal_product_class") or "") not in {
                "FEDERAL_PRODUCT_LIKELY",
                "CORE_PRODUCT",
            }:
                continue
        scored.append((enrichment_priority_score(r), r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def enrich_one_opportunity(
    row: dict[str, Any],
    *,
    authorize_live: bool = False,
    fetch_documents: bool = False,
    max_document_fetches: int = 2,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full enrichment path for one notice. Idempotent via content hashes."""
    prior = prior or {}
    out = dict(row)
    # 1) Description recovery
    desc_rec = recover_description(
        out,
        authorize_live=authorize_live,
        known_hash=prior.get("description_hash"),
    )
    out = apply_description_recovery(out, desc_rec)
    # 2) Package refs + tech-data state
    refs = enumerate_package_references(out)
    tech = classify_technical_data_state(out, refs)
    out["package_references"] = refs
    out["technical_data"] = tech
    # 3) Optional bounded public document retrieval
    docs_recovered = 0
    recovered_docs: list[dict[str, Any]] = []
    if fetch_documents and authorize_live and max_document_fetches > 0:
        from public_document_retriever import retrieve_public_document

        known_hashes = set(prior.get("document_hashes") or [])
        fetches = 0
        for ref in refs:
            if fetches >= max_document_fetches:
                break
            if ref.get("reference_class") not in _FETCHABLE_REF_CLASSES:
                continue
            url = str(ref.get("url") or "")
            # Skip DIBBS/PIEE hosts explicitly
            host = str(ref.get("host") or "")
            if any(x in host for x in ("dibbs", "piee", "eb.mil")):
                continue
            if ref.get("reference_class") == REF_SAM_DESCRIPTION and desc_rec.get("text"):
                continue  # already recovered as description
            result = retrieve_public_document(url, known_hashes=known_hashes)
            fetches += 1
            if result.get("ok") and result.get("hash"):
                known_hashes.add(result["hash"])
                docs_recovered += 1
                text = result.get("text") or ""
                recovered_docs.append(
                    {
                        "url": url,
                        "hash": result.get("hash"),
                        "http_status": result.get("http_status"),
                        "filename": result.get("filename"),
                        "text_preview": text[:2000],
                        "bytes": result.get("bytes"),
                    }
                )
                # Merge text into description blob for extraction if thin
                if text and (not out.get("description") or len(str(out.get("description"))) < 200):
                    out["description"] = (str(out.get("description") or "") + "\n" + text)[:50000]
            elif result.get("http_status") in {401, 403}:
                ref["reference_class"] = "AUTH_REQUIRED"
        out["recovered_documents"] = recovered_docs
        # Do not persist full document bodies in checkpoint — hashes + preview only
    # 4) Structure + identity + signals + readiness
    out = enrich_with_dla_structure(out)
    screen = classify_federal_product_cheap(out)
    out.update(screen)
    readiness = compute_product_transaction_readiness(
        out,
        struct=out.get("dla_product_structure"),
        description_state=out.get("description_state"),
        tech_state=(out.get("technical_data") or {}).get("technical_data_state"),
        refs_count=len(refs),
        docs_recovered=docs_recovered,
    )
    out["product_transaction_readiness"] = readiness
    out["readiness_state"] = readiness.get("readiness_state")
    # Amendment / change awareness vs prior enrichment snapshot
    prior_snap = {
        "quantity": prior.get("quantity"),
        "exact_nsn": prior.get("exact_nsn"),
        "exact_part_number": prior.get("exact_part_number"),
        "unit_of_issue": prior.get("unit_of_issue"),
        "deadline": prior.get("deadline"),
        "description_content_hash": prior.get("description_hash"),
        "readiness_state": prior.get("readiness_state"),
    }
    curr_snap = {
        "quantity": (out.get("dla_product_structure") or {}).get("quantity"),
        "exact_nsn": (out.get("dla_product_structure") or {}).get("nsn"),
        "exact_part_number": (out.get("dla_product_structure") or {}).get("part_number"),
        "unit_of_issue": (out.get("dla_product_structure") or {}).get("unit_of_issue"),
        "deadline": out.get("deadline"),
        "description_content_hash": desc_rec.get("content_hash"),
        "readiness_state": readiness.get("readiness_state"),
    }
    amendments = detect_amendment_changes(prior_snap, curr_snap) if prior else []
    if amendments:
        out["amendment_changes"] = amendments
        out["needs_reenrichment"] = False  # just re-enriched
        meta = dict(out.get("raw_metadata") or {})
        hist = list(meta.get("amendment_history") or [])
        hist.extend(amendments)
        meta["amendment_history"] = hist[-50:]
        out["raw_metadata"] = meta
    out["enrichment"] = {
        "version": ENRICHMENT_VERSION,
        "enriched_at": _utc(),
        "description_state": out.get("description_state"),
        "description_hash": desc_rec.get("content_hash") or prior.get("description_hash"),
        "refs_count": len(refs),
        "docs_recovered": docs_recovered,
        "document_hashes": [d.get("hash") for d in recovered_docs if d.get("hash")],
        "technical_data_state": (out.get("technical_data") or {}).get("technical_data_state"),
        "readiness_state": readiness.get("readiness_state"),
        "bypass_attempted": False,
        "amendment_changes": amendments,
        "quantity": curr_snap["quantity"],
        "exact_nsn": curr_snap["exact_nsn"],
        "exact_part_number": curr_snap["exact_part_number"],
        "unit_of_issue": curr_snap["unit_of_issue"],
        "deadline": curr_snap["deadline"],
    }
    return out


def detect_amendment_changes(prior: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare enrichment snapshots; return material field deltas with invalidation hints."""
    tracked = [
        ("quantity", "economics"),
        ("exact_nsn", "product_identity"),
        ("exact_part_number", "product_identity"),
        ("unit_of_issue", "transaction_structure"),
        ("deadline", "pursuit_viability"),
        ("description_content_hash", "package_extraction"),
        ("readiness_state", None),
    ]
    changes: list[dict[str, Any]] = []
    for field, invalidate in tracked:
        old = prior.get(field)
        new = current.get(field)
        if old is None and new is None:
            continue
        if old != new:
            changes.append(
                {
                    "state": "AMENDMENT_DETECTED",
                    "field": field,
                    "old_value": old,
                    "new_value": new,
                    "materiality": "HIGH" if invalidate else "MEDIUM",
                    "invalidates": invalidate,
                    "timestamp": _utc(),
                }
            )
    return changes


def run_federal_dla_enrichment_campaign(
    opportunities: list[dict[str, Any]],
    *,
    authorize_live: bool = False,
    limit: int = 75,
    fetch_documents: bool = True,
    max_document_fetches_per_opp: int = 2,
    prefer_dla: bool = True,
) -> dict[str, Any]:
    """ONE controlled enrichment campaign with durable checkpoint."""
    ckpt = load_enrichment_checkpoint()
    by_id: dict[str, Any] = dict(ckpt.get("by_id") or {})
    candidates = select_enrichment_candidates(opportunities, limit=limit, prefer_dla=prefer_dla)
    enriched_rows: list[dict[str, Any]] = []
    metrics = {
        "sample_size": 0,
        "descriptions_recovered": 0,
        "description_inline": 0,
        "description_blocked": 0,
        "packages_discovered": 0,
        "packages_recovered": 0,
        "exact_nsn": 0,
        "exact_pn": 0,
        "cage_oem": 0,
        "quantity": 0,
        "uoi": 0,
        "approved_source": 0,
        "fat": 0,
        "packaging": 0,
        "inspection": 0,
        "submission_path": 0,
        "commercial_research_ready": 0,
        "historical_research_ready": 0,
        "dibbs_refs": 0,
        "tdmt_refs": 0,
        "skipped_unchanged_descriptions": 0,
    }
    evidence_chains: list[dict[str, Any]] = []

    for i, row in enumerate(candidates):
        eid = str(row.get("notice_id") or row.get("external_id") or "")
        prior = by_id.get(eid) or {}
        if i % 5 == 0:
            print(f"  enrich {i+1}/{len(candidates)} id={eid[:24]}", flush=True)
        enriched = enrich_one_opportunity(
            row,
            authorize_live=authorize_live,
            fetch_documents=fetch_documents,
            max_document_fetches=max_document_fetches_per_opp,
            prior=prior,
        )
        metrics["sample_size"] += 1
        st = str(enriched.get("description_state") or "")
        if st in {DESCRIPTION_PUBLIC_RECOVERED, "DESCRIPTION_INLINE"}:
            metrics["descriptions_recovered"] += 1
        if st == "DESCRIPTION_INLINE":
            metrics["description_inline"] += 1
        if st in {"DESCRIPTION_AUTH_REQUIRED", "DESCRIPTION_BOT_BLOCKED"}:
            metrics["description_blocked"] += 1
        if (enriched.get("enrichment") or {}).get("skipped_unchanged") or (
            enriched.get("raw_metadata") or {}
        ).get("description_recovery", {}).get("skipped_unchanged"):
            metrics["skipped_unchanged_descriptions"] += 1
        refs = enriched.get("package_references") or []
        if refs:
            metrics["packages_discovered"] += 1
        docs_n = int((enriched.get("enrichment") or {}).get("docs_recovered") or 0)
        if docs_n > 0:
            metrics["packages_recovered"] += 1
        struct = enriched.get("dla_product_structure") or {}
        if struct.get("has_exact_nsn"):
            metrics["exact_nsn"] += 1
        if struct.get("has_exact_pn"):
            metrics["exact_pn"] += 1
        if struct.get("has_cage") or (struct.get("fields") or {}).get("oem"):
            metrics["cage_oem"] += 1
        if struct.get("has_quantity"):
            metrics["quantity"] += 1
        if struct.get("has_uoi"):
            metrics["uoi"] += 1
        if struct.get("approved_source_signal"):
            metrics["approved_source"] += 1
        if struct.get("fat_required_signal"):
            metrics["fat"] += 1
        if struct.get("packaging_signal"):
            metrics["packaging"] += 1
        if struct.get("inspection_signal"):
            metrics["inspection"] += 1
        sigs = enriched.get("dla_research_signals") or []
        if any(s.startswith("SUBMISSION_PATH_") for s in sigs):
            metrics["submission_path"] += 1
        ready = enriched.get("product_transaction_readiness") or {}
        if ready.get("commercial_research_ready") or ready.get("readiness_state") == READY_COMMERCIAL_RESEARCH:
            metrics["commercial_research_ready"] += 1
        if ready.get("historical_research_ready") or ready.get("readiness_state") == READY_HISTORICAL_RESEARCH:
            metrics["historical_research_ready"] += 1
        tech = enriched.get("technical_data") or {}
        if tech.get("dibbs_reference"):
            metrics["dibbs_refs"] += 1
        if tech.get("tdmt_reference"):
            metrics["tdmt_refs"] += 1

        by_id[eid] = {
            "description_hash": (enriched.get("enrichment") or {}).get("description_hash"),
            "document_hashes": (enriched.get("enrichment") or {}).get("document_hashes") or [],
            "readiness_state": enriched.get("readiness_state"),
            "enriched_at": _utc(),
            "version": ENRICHMENT_VERSION,
        }
        enriched_rows.append(enriched)
        if len(evidence_chains) < 12:
            evidence_chains.append(
                {
                    "external_id": eid,
                    "solicitation_number": enriched.get("solicitation_number"),
                    "title": (enriched.get("title") or "")[:120],
                    "description_state": enriched.get("description_state"),
                    "refs": len(refs),
                    "docs_recovered": docs_n,
                    "nsn": struct.get("nsn"),
                    "pn": struct.get("part_number"),
                    "qty": struct.get("quantity"),
                    "uoi": struct.get("unit_of_issue"),
                    "tech_state": tech.get("technical_data_state"),
                    "readiness": enriched.get("readiness_state"),
                    "signals": sigs[:8],
                    "provenance": {
                        "description_url": (enriched.get("raw_metadata") or {})
                        .get("description_recovery", {})
                        .get("source_url"),
                        "content_hash": (enriched.get("enrichment") or {}).get("description_hash"),
                    },
                }
            )

    ckpt_out = save_enrichment_checkpoint({"by_id": by_id, "last_campaign_metrics": metrics})
    n = max(1, metrics["sample_size"])
    return {
        "kind": "FEDERAL_DLA_ENRICHMENT_CAMPAIGN",
        "version": ENRICHMENT_VERSION,
        "executed": True,
        "authorize_live": authorize_live,
        "metrics": metrics,
        "rates": {
            "description_recovery_rate": round(metrics["descriptions_recovered"] / n, 3),
            "package_reference_rate": round(metrics["packages_discovered"] / n, 3),
            "package_recovered_rate": round(metrics["packages_recovered"] / n, 3),
            "nsn_rate": round(metrics["exact_nsn"] / n, 3),
            "pn_rate": round(metrics["exact_pn"] / n, 3),
            "quantity_rate": round(metrics["quantity"] / n, 3),
            "commercial_ready_rate": round(metrics["commercial_research_ready"] / n, 3),
        },
        "evidence_chains": evidence_chains,
        "opportunities": enriched_rows,
        "checkpoint_ids": len(ckpt_out.get("by_id") or {}),
        "anti_bot_bypass": 0,
        "DEVELOPMENT_NO_OUTREACH": True,
        "note": "Bounded sample — do not extrapolate as full-universe percentages without measurement",
    }
