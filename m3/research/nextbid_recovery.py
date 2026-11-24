"""Full offline recovery of previously paid NextBid + free-source data (Build 002K).

ZERO metered NextBid calls.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.classify import classify_batch
from m3.research.commercial_criteria import STAGE_B, STAGE_C, evaluate_procurement_pipeline
from m3.research.nextbid_client import load_checkpoint
from m3.research.nextbid_inventory import upsert_inventory

EXCLUDE_TITLE_RE = re.compile(
    r"(?i)\b(construction|consulting|professional\s+services|software|saas|"
    r"maintenance\s+and\s+repair|lease\s+of|rental\s+of|IT\s+services|"
    r"design[- ]build|architectural)\b"
)
IT_RE = re.compile(r"(?i)\b(software|saas|cyber|microsoft\s*365|license)\b")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def collect_population() -> dict[str, Any]:
    """Gather all recoverable records from local artifacts.

    Includes durable inventory + paid search-cache listings so recovered IDs
    are not dropped when reprocessing (Build 002Q permanent repair).
    """
    from m3.research.nextbid_inventory import load_inventory

    root = Path(__file__).resolve().parents[2]
    docs = root / "docs"
    ck = load_checkpoint("nextbid_acquire_v1")
    state = ck.get("state") or {}
    enriched = list(state.get("enriched") or [])
    listings = list(state.get("listings") or [])
    seen = list(state.get("seen_nb_ids") or [])

    # Merge listings not in enriched
    by_id: dict[str, dict] = {}

    def _merge_row(row: dict) -> None:
        nb = str(row.get("nb_id") or "")
        if not nb:
            return
        prev = by_id.get(nb) or {}
        # Prefer longer description/document_text (never overwrite thick with thin)
        merged = {**prev}
        for k, v in row.items():
            if v is None:
                continue
            if k in {"description", "summary", "document_text"} and isinstance(v, str):
                old = str(merged.get(k) or "")
                if len(v) >= len(old):
                    merged[k] = v
            elif k not in merged or merged[k] in (None, "", [], {}):
                merged[k] = v
            else:
                merged[k] = v if v not in (None, "", [], {}) else merged[k]
        if merged.get("description") and not merged.get("document_text"):
            merged["document_text"] = merged["description"]
        elif merged.get("document_text") and not merged.get("description"):
            merged["description"] = merged["document_text"]
        # Normalize NextBid listing fields into canonical keys
        if not merged.get("deadline_at") and merged.get("response_deadline"):
            merged["deadline_at"] = merged["response_deadline"]
        if not merged.get("official_url"):
            merged["official_url"] = (
                merged.get("source_url")
                or merged.get("source_url_original")
                or merged.get("url")
            )
        if not merged.get("buyer_name"):
            merged["buyer_name"] = merged.get("agency") or merged.get("agency_department")
        life = str(merged.get("lifecycle_status") or "").lower()
        cur_st = str(merged.get("solicitation_status") or "").lower()
        if (not cur_st or cur_st in {"unknown", "null", "none"}) and life:
            if life in {"open", "active", "posted", "advertised"}:
                merged["solicitation_status"] = "open"
            elif life == "amended":
                merged["solicitation_status"] = "amended"
            elif life in {"closed", "expired", "inactive"}:
                merged["solicitation_status"] = "closed"
            elif life in {"cancelled", "canceled"}:
                merged["solicitation_status"] = "cancelled"
            elif life == "awarded":
                merged["solicitation_status"] = "historical_award"
        if not merged.get("package_status") and merged.get("document_set_status"):
            ds = str(merged["document_set_status"]).upper()
            if ds in {"COMPLETE", "PARTIAL"}:
                merged["package_status"] = ds
            elif ds == "AVAILABLE":
                merged["package_status"] = "COMPLETE"
        by_id[nb] = merged

    for row in listings + enriched:
        if isinstance(row, dict):
            _merge_row(row)

    # Durable inventory (includes IDs recovered from search cache)
    for nb, inv_row in load_inventory().items():
        _merge_row(
            {
                "nb_id": nb,
                "title": inv_row.get("title"),
                "solicitation_number": inv_row.get("solicitation_number"),
                "official_url": inv_row.get("official_url"),
                "source": inv_row.get("source"),
                "nextbid_source": inv_row.get("source"),
                "acquisition_status": inv_row.get("acquisition_status"),
                "processing_status": inv_row.get("processing_status"),
                "external_key": f"nextbid:{nb}",
            }
        )

    # Paid HTTP search-cache results (offline; no network)
    cache_http = get_settings().data_root() / "nextbid_cache" / "http"
    cache_listing_ids = 0
    if cache_http.exists():
        for p in cache_http.glob("*.json"):
            try:
                w = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            parts = w.get("parts") or []
            path = str(parts[0] if parts else "")
            if path.rstrip("/") != "/opportunities" and not path.endswith("/opportunities"):
                continue
            payload = w.get("payload") or {}
            for r in payload.get("results") or []:
                if isinstance(r, dict) and r.get("nb_id"):
                    _merge_row(r)
                    cache_listing_ids += 1

    prior = _load_json(docs / "BUILD_002I_VERIFIED_OPPORTUNITIES.json") or {}
    prior_ops = list(prior.get("opportunities") or [])
    j_acc = _load_json(docs / "BUILD_002J_ACCEPTANCE.json") or {}
    i_acc = _load_json(docs / "BUILD_002I_NEXTBID_ACCEPTANCE.json") or {}

    return {
        "by_id": by_id,
        "seen_count": len(seen),
        "enriched_count": len(enriched),
        "listings_count": len(listings),
        "unique_nextbid": len(by_id),
        "cache_listing_rows_merged": cache_listing_ids,
        "prior_free_source": prior_ops,
        "prior_free_count": len(prior_ops),
        "completed_queries": state.get("completed_queries") or [],
        "artifacts": {
            "checkpoint": True,
            "inventory": True,
            "http_search_cache": True,
            "build_002j_acceptance": bool(j_acc),
            "build_002i_nextbid_acceptance": bool(i_acc),
        },
        "collected_at": _utc(),
    }


def listing_prefilter(item: dict[str, Any]) -> str:
    """Return exclude|investigate|promote based on listing fields only.

    Build 002Q repair: do NOT hard-reject on isolated title words (IT, construction,
    maintenance, rental). Only exclude confirmed non-solicitations / awards / forecasts.
    Ambiguous titles stay in investigate for document-backed classification.
    """
    title = str(item.get("title") or "")
    url = str(item.get("official_url") or item.get("source_url_original") or "")
    blob = f"{title} {url}"
    # Confirmed non-solicitation / award / forecast only
    if re.search(r"(?i)notice[-_ ]of[-_ ]awards?|/awards?\b|award\s+results?\b", blob):
        return "exclude"
    if re.search(r"(?i)\b(procurement\s+forecast|buying\s+plan|purchasing\s+plan)\b", blob):
        return "exclude"
    if item.get("verified_individual_solicitation") is False and str(
        item.get("record_type") or ""
    ) in {
        "HISTORICAL_AWARD",
        "PROCUREMENT_FORECAST",
        "PURCHASING_PLAN",
        "NON_SOLICITATION",
        "PROCUREMENT_PORTAL",
    }:
        return "exclude"
    # Soft promote clear goods vocabulary — never a hard reject path
    if re.search(
        r"(?i)\b(suppl(y|ies)|equipment|furniture|pump|generator|trailer|ppe|"
        r"commodit|janitorial|tools?|appliances?|hardware|materials?|goods|"
        r"vehicles?|machinery|medical\s+equipment)\b",
        title,
    ):
        return "promote"
    # Title words that previously caused false excludes → investigate, don't exclude
    if IT_RE.search(title) or EXCLUDE_TITLE_RE.search(title):
        return "investigate"
    return "investigate"


def _restore_002i_fit_evidence(by_id: dict[str, dict]) -> dict[str, Any]:
    """Re-attach Build 002I Stage C quantity evidence lost in slim checkpoints."""
    root = Path(__file__).resolve().parents[2]
    i_acc = _load_json(root / "docs" / "BUILD_002I_NEXTBID_ACCEPTANCE.json") or {}
    fits = list(i_acc.get("strict_m3_fit_evidence") or [])
    restored = 0
    for fit in fits:
        nb = str(fit.get("nb_id") or "")
        if not nb or nb not in by_id:
            continue
        row = by_id[nb]
        if fit.get("quantity") and not row.get("quantity"):
            row["quantity"] = fit.get("quantity")
        if fit.get("quantity_unit") and not row.get("quantity_unit"):
            row["quantity_unit"] = fit.get("quantity_unit")
        if fit.get("quantity_semantic") and not row.get("quantity_semantic"):
            row["quantity_semantic"] = fit.get("quantity_semantic")
            restored += 1
        if fit.get("quantity_evidence") and not row.get("quantity_evidence"):
            row["quantity_evidence"] = fit.get("quantity_evidence")
        if fit.get("requirement_class") and (
            not row.get("requirement_class")
            or row.get("requirement_class") in {"pending_classification", "unknown", "unknown_mixed"}
        ):
            row["requirement_class"] = fit.get("requirement_class")
        row["build_002i_stage_c_evidence"] = {
            "stage": fit.get("stage"),
            "quantity": fit.get("quantity"),
            "quantity_semantic": fit.get("quantity_semantic"),
            "quantity_evidence": fit.get("quantity_evidence"),
        }
        by_id[nb] = row
    return {"restored_semantic_count": restored, "fit_evidence_rows": len(fits)}


def reprocess_full_population(*, artifact_name: str = "build002l_offline_recovery.json") -> dict[str, Any]:
    pop = collect_population()
    restore_meta = _restore_002i_fit_evidence(pop["by_id"])
    items = list(pop["by_id"].values())
    pre = Counter(listing_prefilter(i) for i in items)

    # Drop hard excludes from Stage C path but keep in inventory / registry
    work = []
    for it in items:
        disposition = listing_prefilter(it)
        it["listing_disposition"] = disposition
        if disposition == "exclude":
            it["requirement_class"] = it.get("requirement_class") or "not_applicable_non_solicitation"
        work.append(it)

    batch = classify_batch(work)
    classified = batch.get("items") if isinstance(batch, dict) else work

    fits: list[dict] = []
    stage_b: list[dict] = []
    stage_counts: Counter = Counter()
    qty_defined = 0
    unknown_kept = 0

    for it in classified or []:
        if it.get("listing_disposition") == "exclude":
            stage_counts["excluded_listing"] += 1
            # Keep excluded rows recoverable — do not destroy
            it["exclusion_reason"] = "listing_prefilter_exclude"
            continue
        pipe = evaluate_procurement_pipeline(it, document_text=it.get("document_text") or it.get("description"))
        for k, v in (pipe.get("enriched_fields") or {}).items():
            if v is not None:
                it[k] = v
        if pipe.get("quantity") is not None:
            it["quantity"] = pipe["quantity"]
        if pipe.get("quantity_semantic"):
            it["quantity_semantic"] = pipe["quantity_semantic"]
        if pipe.get("quantity_evidence"):
            it["quantity_evidence"] = pipe["quantity_evidence"]
        it["procurement_pipeline"] = pipe
        it["procurement_stage"] = pipe.get("stage") or "none"
        stage = pipe.get("stage") or "none"
        stage_counts[stage] += 1
        if pipe.get("unknown_flags"):
            unknown_kept += 1
        if pipe.get("quantity_defined"):
            qty_defined += 1
        if stage == STAGE_B:
            stage_b.append(it)
        if stage == STAGE_C and it.get("quantity") not in (None, "", "unknown"):
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
                    "quantity_evidence": it.get("quantity_evidence") or pipe.get("quantity_evidence"),
                    "requirement_class": it.get("requirement_class"),
                    "stage": STAGE_C,
                    "evidence": {
                        "nextbid_nb_id": it.get("nb_id"),
                        "source_url": it.get("source_url_original") or it.get("official_url"),
                        "pipeline_reasons": pipe.get("reasons"),
                        "quantity_evidence": it.get("quantity_evidence") or pipe.get("quantity_evidence"),
                        "recovery": "offline_full_population_002l",
                        "build_002i_evidence": it.get("build_002i_stage_c_evidence"),
                    },
                }
            )

    inv = upsert_inventory(classified or [])

    # Permanent registry ingest (file-backed; never deletes)
    from m3.research.procurement_registry import ingest_population

    registry = ingest_population(classified or [], actor="build_002l_offline_recovery")

    # Persist recovery artifact
    out_dir = get_settings().data_root() / "acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "recovered_at": _utc(),
        "unique_nextbid": pop["unique_nextbid"],
        "stage_counts": dict(stage_counts),
        "listing_prefilter": dict(pre),
        "strict_fits": fits,
        "stage_b_count": len(stage_b),
        "quantity_defined": qty_defined,
        "unknown_kept": unknown_kept,
        "prior_free_source_count": pop["prior_free_count"],
        "prior_free_source": pop["prior_free_source"],
        "inventory": inv,
        "registry": registry,
        "restore_002i_fit_evidence": restore_meta,
        "metered_calls": 0,
    }
    path = out_dir / artifact_name
    path.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
    # Keep 002K artifact name updated for compatibility
    if artifact_name != "build002k_offline_recovery.json":
        (out_dir / "build002k_offline_recovery.json").write_text(
            json.dumps({**artifact, "note": "compat_copy_from_002l"}, indent=2, default=str),
            encoding="utf-8",
        )

    return {
        **artifact,
        "population": {
            "seen_count": pop["seen_count"],
            "enriched_count": pop["enriched_count"],
            "listings_count": pop["listings_count"],
            "unique_nextbid": pop["unique_nextbid"],
            "completed_queries": pop["completed_queries"],
        },
        "artifact_path": str(path),
    }
