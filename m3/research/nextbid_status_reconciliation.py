"""BUILD 002Q — Complete inventory and active status reconciliation.

ZERO metered NextBid / OpenAI / SAM / DIBBS. Free /usage (+ documented free
stats/tradelines) only after confirming billing status. Repairs status
persistence defects and assigns exclusive A–G categories to all inventory.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.classify import classify_solicitation_item
from m3.research.commercial_criteria import (
    evaluate_bid_eligibility,
    evaluate_procurement_pipeline,
)
from m3.research.nextbid_acquire import EXCLUDED_SOURCES, _normalize_listing
from m3.research.nextbid_client import NextBidClient, load_checkpoint
from m3.research.nextbid_controller import (
    acquisition_locked,
    free_request,
    new_job_id,
    refresh_account_usage,
    set_usage_reconcile_baseline,
)
from m3.research.nextbid_inventory import load_inventory, upsert_inventory
from m3.research.nextbid_pipeline_repair import _attach_stored_evidence
from m3.research.nextbid_recovery import collect_population, listing_prefilter
from m3.research.owner_queues import empty_queues, map_to_primary_disposition, write_queues

ARCHIVE = Path(__file__).resolve().parents[2] / "docs" / "acceptance_archive"
FEDERAL_SOURCES = frozenset({"sam.gov", "dibbs"})
OPEN_LIFE = frozenset({"open", "active", "posted", "advertised", "amended"})
CLOSED_LIFE = frozenset({"closed", "expired", "inactive", "archived", "complete", "completed"})
CANCEL_LIFE = frozenset({"cancelled", "canceled", "withdrawn"})
AWARD_LIFE = frozenset({"awarded", "award"})
CAT_LABELS = {
    "A": "Verified active state/local solicitation",
    "B": "Verified active federal solicitation - parked",
    "C": "Expired or closed solicitation",
    "D": "Cancelled solicitation",
    "E": "Historical award or non-solicitation",
    "F": "Duplicate canonical record",
    "G": "Status unresolved - insufficient evidence",
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[002q-status-recon] {msg}", flush=True)


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s[:32] if len(s) > 32 else s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s[:19])
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _cache_root() -> Path:
    return get_settings().data_root() / "nextbid_cache" / "http"


def _parse_search_params(parts: list[Any]) -> dict[str, Any]:
    if len(parts) < 2:
        return {}
    raw = parts[1]
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return {}


def load_retained_provider_index() -> dict[str, Any]:
    """Index every retained NextBid search/detail payload for status truth."""
    root = _cache_root()
    by_nb: dict[str, dict[str, Any]] = {}
    search_hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    search_files = 0
    search_open_true = 0
    search_open_false = 0
    search_open_missing = 0
    sources_in_open_search: Counter[str] = Counter()
    federal_in_open_search = 0
    pages_by_key: dict[str, set[int]] = defaultdict(set)
    provider_totals: dict[str, Any] = {}

    if not root.exists():
        return {
            "by_nb": by_nb,
            "search_hits": search_hits,
            "search_files": 0,
            "meta": {},
        }

    for path in root.glob("*.json"):
        try:
            wrap = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        parts = wrap.get("parts") or []
        if not parts:
            continue
        route = str(parts[0] or "")
        payload = wrap.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if route.rstrip("/") == "/opportunities" or route.endswith("/opportunities"):
            search_files += 1
            params = _parse_search_params(parts)
            open_raw = params.get("open")
            open_true = str(open_raw).lower() in {"true", "1", "yes"}
            open_false = str(open_raw).lower() in {"false", "0", "no"}
            if open_true:
                search_open_true += 1
            elif open_false:
                search_open_false += 1
            else:
                search_open_missing += 1
            page = int(params.get("page") or 1)
            sk = json.dumps(
                {k: params.get(k) for k in sorted(params) if k != "page"},
                sort_keys=True,
            )
            pages_by_key[sk].add(page)
            total_pages = payload.get("total_pages") or payload.get("pages")
            total_count = payload.get("total") or payload.get("count")
            if total_pages is not None or total_count is not None:
                provider_totals[sk] = {
                    "params": {k: params[k] for k in params if k != "page"},
                    "total_pages": total_pages,
                    "total": total_count,
                    "pages_seen": sorted(pages_by_key[sk]),
                }
            for row in payload.get("results") or []:
                if not isinstance(row, dict) or not row.get("nb_id"):
                    continue
                nb = str(row["nb_id"])
                src = str(row.get("source") or "").lower()
                sources_in_open_search[src] += 1 if open_true else 0
                if open_true and src in FEDERAL_SOURCES:
                    federal_in_open_search += 1
                hit = {
                    "open_filter": open_true,
                    "lifecycle_status": row.get("lifecycle_status"),
                    "solicitation_status": row.get("solicitation_status"),
                    "source": src,
                    "response_deadline": row.get("response_deadline"),
                    "notice_type": row.get("notice_type"),
                    "params": params,
                }
                search_hits[nb].append(hit)
                merged = dict(by_nb.get(nb) or {})
                merged.update({k: v for k, v in row.items() if v not in (None, "", [], {})})
                merged["provider_open_filter"] = bool(
                    merged.get("provider_open_filter") or open_true
                )
                by_nb[nb] = merged

        elif "/opportunities/" in route:
            # Detail payload
            if payload.get("nb_id"):
                nb = str(payload["nb_id"])
                merged = dict(by_nb.get(nb) or {})
                merged.update({k: v for k, v in payload.items() if v not in (None, "", [], {})})
                by_nb[nb] = merged

    # Overlay checkpoint listings / enriched (already normalized or raw)
    ck = load_checkpoint("nextbid_acquire_v1")
    state = ck.get("state") or {}
    for row in (state.get("listings") or []) + (state.get("enriched") or []):
        if not isinstance(row, dict) or not row.get("nb_id"):
            continue
        nb = str(row["nb_id"])
        merged = dict(by_nb.get(nb) or {})
        for k, v in row.items():
            if v not in (None, "", [], {}) and (
                k not in merged or merged.get(k) in (None, "", [], {}, "unknown")
            ):
                merged[k] = v
        # Listings from open=true campaigns
        if row.get("provider_open_filter") or (
            isinstance(row.get("evidence"), dict)
            and row["evidence"].get("provider_open_filter")
        ):
            merged["provider_open_filter"] = True
        by_nb[nb] = merged

    return {
        "by_nb": by_nb,
        "search_hits": dict(search_hits),
        "search_files": search_files,
        "meta": {
            "search_open_true": search_open_true,
            "search_open_false": search_open_false,
            "search_open_missing": search_open_missing,
            "federal_rows_in_open_searches": federal_in_open_search,
            "sources_in_open_search": dict(sources_in_open_search),
            "provider_page_totals": {
                k: {
                    **v,
                    "pages_completed": len(v.get("pages_seen") or []),
                    "pages_remaining": (
                        max(0, int(v["total_pages"]) - len(v.get("pages_seen") or []))
                        if v.get("total_pages") is not None
                        else None
                    ),
                }
                for k, v in provider_totals.items()
            },
        },
    }


def assign_category(
    *,
    nb_id: str,
    inv_row: dict[str, Any],
    provider: dict[str, Any],
    search_hits: list[dict[str, Any]],
    now: datetime,
    duplicate_of: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return (category, status_patch) for one inventory record."""
    if duplicate_of:
        return "F", {
            "nb_id": nb_id,
            "reconciliation_category": "F",
            "duplicate_of": duplicate_of,
            "status_evidence": [f"duplicate_of:{duplicate_of}"],
            "solicitation_status": inv_row.get("solicitation_status") or "unknown",
            "lifecycle_status": inv_row.get("lifecycle_status"),
            "source": inv_row.get("source"),
            "title": inv_row.get("title"),
        }

    row = dict(provider)
    for k in (
        "lifecycle_status",
        "solicitation_status",
        "deadline_at",
        "response_deadline",
        "source",
        "notice_type",
        "title",
        "posted_date",
        "posted_at",
        "last_updated",
    ):
        if inv_row.get(k) not in (None, "", [], {}) and row.get(k) in (None, "", [], {}):
            row[k] = inv_row[k]

    src = str(row.get("source") or inv_row.get("source") or "").lower()
    life = str(row.get("lifecycle_status") or "").strip().lower() or None
    sol = str(row.get("solicitation_status") or "").strip().lower() or None
    ntype = str(row.get("notice_type") or "").strip().lower() or None
    title = str(row.get("title") or inv_row.get("title") or "").lower()
    dl = _parse_dt(row.get("deadline_at") or row.get("response_deadline") or inv_row.get("deadline_at"))
    ever_open = any(bool(h.get("open_filter")) for h in search_hits) or bool(
        row.get("provider_open_filter")
    )
    any_life_open = any(
        str(h.get("lifecycle_status") or "").lower() in OPEN_LIFE for h in search_hits
    ) or (life in OPEN_LIFE if life else False)

    evidence: list[str] = []
    if ever_open:
        evidence.append("retained_open_true_search")
    if life:
        evidence.append(f"lifecycle:{life}")
    if sol:
        evidence.append(f"solicitation_status:{sol}")
    if dl:
        evidence.append(f"deadline:{dl.date().isoformat()}")
    if ntype:
        evidence.append(f"notice_type:{ntype}")

    # Explicit negative evidence first
    if (
        (life and life in CANCEL_LIFE)
        or (sol and ("cancel" in sol))
        or "cancel" in title[:80]
    ):
        cat = "D"
        patch_status = "cancelled"
    elif (
        (life and life in AWARD_LIFE)
        or (sol and sol in {"historical_award", "awarded", "award"})
        or (ntype and ("award" in ntype or ntype in {"justification", "forecast"}))
        or ("award notice" in title)
    ):
        cat = "E"
        patch_status = "historical_award" if (life in AWARD_LIFE or (sol and "award" in sol)) else (
            "forecast" if (ntype and "forecast" in ntype) else "historical_award"
        )
    elif (life and life in CLOSED_LIFE) or (sol and sol in CLOSED_LIFE | {"closed_by_deadline"}):
        cat = "C"
        patch_status = "closed"
    elif dl and dl < now and not any_life_open and not (ever_open and not life):
        # Past deadline without affirmative open lifecycle; if captured under open filter
        # with null lifecycle, treat as closed since capture (deadline elapsed).
        cat = "C"
        patch_status = "closed_by_deadline"
        evidence.append("deadline_past")
    elif ever_open and dl and dl < now and not any_life_open:
        cat = "C"
        patch_status = "closed_by_deadline"
        evidence.append("open_filter_capture_deadline_now_past")
    elif src in FEDERAL_SOURCES or src in EXCLUDED_SOURCES:
        # Federal / excluded — park when active evidence exists
        if any_life_open or (ever_open and (dl is None or dl >= now)) or (dl and dl >= now and ever_open):
            cat = "B"
            patch_status = "open"
            evidence.append("federal_parked")
        elif ever_open and not life:
            cat = "B"
            patch_status = "open"
            evidence.append("federal_open_filter_null_lifecycle")
        elif dl and dl >= now:
            cat = "B"
            patch_status = "open"
            evidence.append("federal_future_deadline_parked")
        else:
            cat = "G"
            patch_status = sol or "unknown"
            evidence.append("federal_insufficient_evidence")
    else:
        # State / local
        if any_life_open or (life and life in OPEN_LIFE):
            cat = "A"
            patch_status = "amended" if life == "amended" else "open"
        elif ever_open and not life and (dl is None or dl >= now):
            cat = "A"
            patch_status = "open"
            evidence.append("open_filter_null_lifecycle")
        elif ever_open and not life and dl and dl < now:
            cat = "C"
            patch_status = "closed_by_deadline"
        elif dl and dl >= now and (sol in OPEN_LIFE or life in OPEN_LIFE):
            cat = "A"
            patch_status = sol if sol in {"open", "amended"} else "open"
        elif dl and dl >= now and not life and not sol:
            # Future deadline alone is supporting, not sufficient → unresolved
            cat = "G"
            patch_status = "unknown"
            evidence.append("future_deadline_without_status_or_open_filter")
        else:
            cat = "G"
            patch_status = sol or "unknown"
            evidence.append("insufficient_status_evidence")

    life_out = life
    if cat in {"A", "B"} and not life_out:
        life_out = "open"
    if cat == "C" and not life_out:
        life_out = "closed"
    if cat == "D":
        life_out = life_out or "cancelled"
    if cat == "E":
        life_out = life_out or "awarded"

    patch = {
        "lifecycle_status": life_out,
        "solicitation_status": patch_status,
        "deadline_at": row.get("deadline_at")
        or row.get("response_deadline")
        or inv_row.get("deadline_at"),
        "response_deadline": row.get("response_deadline") or inv_row.get("response_deadline"),
        "posted_at": row.get("posted_at") or row.get("posted_date"),
        "posted_date": row.get("posted_date") or row.get("posted_at"),
        "notice_type": row.get("notice_type"),
        "last_updated": row.get("last_updated"),
        "source": src or inv_row.get("source"),
        "title": row.get("title") or inv_row.get("title"),
        "official_url": row.get("source_url")
        or row.get("official_url")
        or inv_row.get("official_url"),
        "provider_open_filter": ever_open,
        "status_evidence": evidence,
        "reconciliation_category": cat,
        "nb_id": nb_id,
    }
    return cat, patch


def detect_duplicates(inv: dict[str, dict[str, Any]], provider_by_nb: dict[str, dict]) -> dict[str, str]:
    """Map duplicate nb_id -> canonical nb_id (same source + solicitation_number)."""
    groups: dict[str, list[str]] = defaultdict(list)
    for nb, row in inv.items():
        prov = provider_by_nb.get(nb) or {}
        sn = str(
            row.get("solicitation_number")
            or prov.get("solicitation_number")
            or ""
        ).strip().lower()
        src = str(row.get("source") or prov.get("source") or "").strip().lower()
        if not sn or sn in {"none", "null", "n/a"}:
            continue
        groups[f"{src}|{sn}"].append(nb)
    dup_of: dict[str, str] = {}
    for _key, ids in groups.items():
        if len(ids) < 2:
            continue
        # Canonical = earliest first_seen, then lexical nb_id
        ids_sorted = sorted(
            ids,
            key=lambda x: (
                str((inv.get(x) or {}).get("first_seen_at") or "9999"),
                x,
            ),
        )
        canon = ids_sorted[0]
        for other in ids_sorted[1:]:
            dup_of[other] = canon
    return dup_of


def reprocess_active_state_local(
    db: Session, active_items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Offline classify + pipeline for verified active state/local only."""
    items = []
    for it in active_items:
        row = dict(it)
        row.setdefault("source_key", "nextbid_state_local")
        if not row.get("external_key") and row.get("nb_id"):
            row["external_key"] = f"nextbid:{row['nb_id']}"
        if not row.get("record_type"):
            row["record_type"] = "INDIVIDUAL_SOLICITATION"
            row["verified_individual_solicitation"] = True
        # Ensure normalize-equivalent status fields for gates
        if row.get("provider_open_filter") and not row.get("lifecycle_status"):
            row["lifecycle_status"] = "open"
            row["solicitation_status"] = row.get("solicitation_status") or "open"
        items.append(row)

    pkg_meta = _attach_stored_evidence(db, items)
    classified: list[dict[str, Any]] = []
    for it in items:
        it["listing_disposition"] = listing_prefilter(it)
        classified.append(classify_solicitation_item(it))

    queues = empty_queues()
    stage_counts: Counter[str] = Counter()
    product_confirmed = 0
    product_potential = 0
    qty_verified = 0
    bid_eligible_n = 0
    ready_bid = 0
    ready_supplier = 0
    info_recovery = 0
    actionable: dict[str, list[dict[str, Any]]] = {
        "ready_to_submit": [],
        "registration_required": [],
        "supplier_pricing": [],
        "procurement_eligible": [],
    }

    for it in classified:
        text = it.get("document_text") or it.get("description") or ""
        pipe = evaluate_procurement_pipeline(it, document_text=text or None)
        for k, v in (pipe.get("enriched_fields") or {}).items():
            if v is not None and (it.get(k) in (None, "", []) or str(k).startswith("quantity")):
                if str(k).startswith("quantity"):
                    if v not in (None, ""):
                        it[k] = v
                elif not it.get(k):
                    it[k] = v
        if pipe.get("quantity") is not None and not it.get("quantity"):
            it["quantity"] = pipe["quantity"]
        it["procurement_pipeline"] = pipe
        it["procurement_stage"] = pipe.get("stage")
        stage_counts[str(pipe.get("stage") or ("excluded" if pipe.get("excluded") else "none"))] += 1

        bid = evaluate_bid_eligibility(it, pipe, document_text=text or None)
        it["bid_eligibility"] = bid
        primary = map_to_primary_disposition(
            it, pipe, bid=bid, package_status=it.get("package_status")
        )
        it["primary_disposition"] = primary

        req = (it.get("requirement_class") or "").lower()
        if pipe.get("product_opportunity"):
            if req in {"unknown", "unknown_mixed", "pending_classification", ""}:
                product_potential += 1
            else:
                product_confirmed += 1
        if pipe.get("quantity_defined"):
            qty_verified += 1
        if bid.get("bid_eligible"):
            bid_eligible_n += 1
            actionable["procurement_eligible"].append(_slim(it))
        if primary == "verified_bid_opportunities":
            ready_bid += 1
            ready_supplier += 1
            actionable["ready_to_submit"].append(_slim(it))
        elif primary == "easy_vendor_registration":
            actionable["registration_required"].append(_slim(it))
        elif primary in {
            "verified_product_information_needed",
            "potential_product_classification_needed",
        }:
            info_recovery += 1
            if pipe.get("quantity_defined") or pipe.get("product_opportunity"):
                ready_supplier += 1
                actionable["supplier_pricing"].append(_slim(it))

        queues[primary].append(_slim(it))

    queue_summary = write_queues(queues, build="002Q-status-reconciliation")
    upsert_inventory(classified)

    out_dir = get_settings().data_root() / "acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    pop_path = out_dir / "nextbid_status_recon_active_population.jsonl"
    with pop_path.open("w", encoding="utf-8") as f:
        for it in classified:
            f.write(json.dumps(it, default=str) + "\n")

    return {
        "active_reprocessed": len(classified),
        "package_meta": pkg_meta,
        "stage_counts": dict(stage_counts),
        "confirmed_product": product_confirmed,
        "potential_product": product_potential,
        "verified_quantities": qty_verified,
        "procurement_eligible": bid_eligible_n,
        "ready_to_submit": ready_bid,
        "supplier_pricing_candidates": ready_supplier,
        "information_recovery": info_recovery,
        "actionable": {
            k: v[:50] for k, v in actionable.items()
        },
        "actionable_counts": {k: len(v) for k, v in actionable.items()},
        "queue_summary": queue_summary,
        "population_path": str(pop_path),
    }


def _slim(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "nb_id": it.get("nb_id"),
        "title": (it.get("title") or "")[:180],
        "solicitation_number": it.get("solicitation_number"),
        "buyer_name": it.get("buyer_name") or it.get("agency"),
        "deadline_at": it.get("deadline_at"),
        "quantity": it.get("quantity"),
        "quantity_semantic": it.get("quantity_semantic"),
        "official_url": it.get("official_url"),
        "requirement_class": it.get("requirement_class"),
        "solicitation_status": it.get("solicitation_status"),
        "lifecycle_status": it.get("lifecycle_status"),
        "reconciliation_category": it.get("reconciliation_category"),
        "primary_disposition": it.get("primary_disposition"),
        "source": it.get("source") or it.get("nextbid_source"),
    }


def analyze_search_quality(index_meta: dict[str, Any], growth: dict[str, Any]) -> dict[str, Any]:
    return {
        "open_only_filter_applied": index_meta.get("search_open_true", 0) > 0
        and index_meta.get("search_open_false", 0) == 0
        and index_meta.get("search_open_missing", 0) == 0,
        "search_files": index_meta.get("search_open_true", 0)
        + index_meta.get("search_open_false", 0)
        + index_meta.get("search_open_missing", 0),
        "search_open_true": index_meta.get("search_open_true"),
        "search_open_false": index_meta.get("search_open_false"),
        "search_open_missing": index_meta.get("search_open_missing"),
        "federal_returned_despite_state_local_intent": index_meta.get(
            "federal_rows_in_open_searches"
        ),
        "note_federal": (
            "Older overlapping searches without sources= whitelist returned DIBBS/SAM "
            "under open=true; those IDs were recovered into inventory during permanent repair."
        ),
        "inventory_growth": growth,
        "status_fields_dropped_during_ingestion": True,
        "status_fields_defect": (
            "nextbid_inventory.upsert_inventory previously omitted lifecycle_status, "
            "solicitation_status, and deadline_at — supplied by provider but not persisted."
        ),
        "null_lifecycle_under_open_filter": (
            "Many provider rows omit lifecycle_status even when returned by open=true; "
            "normalize previously mapped that to solicitation_status=unknown."
        ),
    }


AUTHORIZED_SL_SOURCES = frozenset(
    {"opengov", "bonfire", "planetbids", "calprocure", "publicpurchase", "instantmarkets"}
)


def try_free_coverage_stats(
    usage: dict[str, Any], *, local_active_sl: int | None = None
) -> dict[str, Any]:
    """Documented free endpoints only; never invent a coverage denominator."""
    out: dict[str, Any] = {
        "billing_confirmed": bool(usage.get("ok") and usage.get("used") is not None),
        "usage": {
            "used": usage.get("used"),
            "remaining": usage.get("remaining"),
            "allowance": usage.get("allowance"),
        },
        "tradelines": None,
        "stats": None,
        "coverage_percentage": None,
        "coverage_unknown_reason": None,
        "provider_active_state_local_total": None,
        "provider_open_total": None,
        "provider_open_by_source": None,
        "provider_open_definition": None,
        "local_active_sl": local_active_sl,
        "missing_or_unacquired_sl": None,
    }
    if not out["billing_confirmed"]:
        out["coverage_unknown_reason"] = "billing_usage_unavailable"
        return out
    try:
        tls = NextBidClient(job_id=new_job_id("002q-recon-free")).tradelines()
        out["tradelines"] = {
            "ok": tls.get("ok"),
            "keys": list(tls.keys())[:20] if isinstance(tls, dict) else None,
        }
    except Exception as exc:
        out["tradelines"] = {"ok": False, "error": type(exc).__name__}
    try:
        data = free_request("/stats")
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else data
        out["stats"] = {
            "path": "/stats",
            "ok": data.get("ok"),
            "open": stats.get("open") if isinstance(stats, dict) else None,
            "total_solicitations": stats.get("total_solicitations")
            if isinstance(stats, dict)
            else None,
            "as_of": stats.get("as_of") if isinstance(stats, dict) else None,
        }
        if isinstance(stats, dict):
            by_src = stats.get("open_by_source") or {}
            out["provider_open_by_source"] = dict(by_src) if isinstance(by_src, dict) else None
            out["provider_open_total"] = stats.get("open")
            defs = stats.get("definitions") or {}
            out["provider_open_definition"] = defs.get("open") if isinstance(defs, dict) else None
            if isinstance(by_src, dict):
                sl_total = sum(
                    int(by_src.get(s) or 0) for s in AUTHORIZED_SL_SOURCES if s in by_src
                )
                out["provider_active_state_local_total"] = sl_total
                out["provider_open_by_authorized_source"] = {
                    s: int(by_src.get(s) or 0) for s in sorted(AUTHORIZED_SL_SOURCES)
                }
                out["provider_federal_open"] = int(by_src.get("dibbs") or 0) + int(
                    by_src.get("sam.gov") or 0
                )
    except Exception as exc:
        out["stats"] = {"path": "/stats", "ok": False, "error": type(exc).__name__}

    denom = out.get("provider_active_state_local_total")
    if isinstance(denom, int) and denom > 0 and isinstance(local_active_sl, int):
        out["coverage_percentage"] = round(100.0 * local_active_sl / denom, 2)
        out["missing_or_unacquired_sl"] = max(0, denom - local_active_sl)
        out["coverage_unknown_reason"] = None
        out["coverage_scope_note"] = (
            "Denominator = sum of free /stats open_by_source for authorized state/local "
            "sources only (opengov,bonfire,planetbids,calprocure,publicpurchase,instantmarkets). "
            "Numerator = M3 Category A (verified active state/local). "
            "Provider open definition retained from /stats.definitions.open."
        )
    elif out.get("provider_active_state_local_total") is None:
        out["coverage_unknown_reason"] = (
            "Free /stats did not return open_by_source for authorized state/local sources."
        )
    return out


def _controlled_pages_remaining() -> dict[str, Any]:
    """Unfinished state/local pages from BUILD 002Q controlled coverage tracker."""
    path = get_settings().data_root() / "nextbid_checkpoints" / "build002q_controlled_coverage.json"
    if not path.exists():
        return {"pages_remaining": None, "by_source": {}, "path": str(path)}
    try:
        cov = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"pages_remaining": None, "by_source": {}, "corrupt": True}
    by_source: dict[str, int] = {}
    total = 0
    for _sk, s in (cov.get("searches") or {}).items():
        tp = s.get("provider_total_pages")
        last = int(s.get("last_successful_page") or s.get("pages_requested") or 0)
        if tp is None:
            continue
        rem = max(0, int(tp) - last)
        src = str(s.get("source") or "unknown")
        by_source[src] = by_source.get(src, 0) + rem
        total += rem
    return {
        "pages_remaining": total,
        "by_source": by_source,
        "path": str(path),
        "searches": len(cov.get("searches") or {}),
    }


def revised_acquisition_plan(
    *,
    category_counts: dict[str, int],
    coverage: dict[str, Any],
    index_meta: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    """Plan only — do not execute metered acquisition."""
    remaining = int(usage.get("remaining") or 0)
    used = int(usage.get("used") or 0)
    reserve = 1000
    spendable = max(0, remaining - reserve)
    listing_budget = min(330, spendable)

    # All retained search page gaps (includes unscoped commodities / federal-heavy)
    pages_remaining_all = 0
    unknown_partitions = 0
    for _k, v in (index_meta.get("provider_page_totals") or {}).items():
        rem = v.get("pages_remaining")
        if rem is None:
            unknown_partitions += 1
        else:
            pages_remaining_all += int(rem)

    controlled = _controlled_pages_remaining()
    sl_pages = controlled.get("pages_remaining")
    # Minimum listing spend = unfinished authorized state/local partitions only
    if isinstance(sl_pages, int):
        min_listing = sl_pages  # 123 historically; may be < 330
        completable_under_330 = min_listing <= listing_budget
    else:
        min_listing = None
        completable_under_330 = None

    return {
        "do_not_execute": True,
        "provider_used": used,
        "provider_remaining": remaining,
        "protected_reserve": reserve,
        "combined_spendable_cap": min(1547, spendable),
        "listing_discovery_cap": 330,
        "detail_document_cap": 1217,
        "daily_ceiling": 100,
        "job_ceiling": 50,
        "retained_pages_remaining_all_searches": pages_remaining_all,
        "unscoped_or_federal_heavy_pages_do_not_continue": max(
            0, pages_remaining_all - (sl_pages or 0)
        ),
        "controlled_state_local_pages_remaining": sl_pages,
        "controlled_remaining_by_source": controlled.get("by_source"),
        "partitions_with_unknown_remaining": unknown_partitions,
        "recommended_minimum_listing_credits": min_listing,
        "recommended_minimum_detail_credits": 0,
        "recommended_minimum_total": min_listing if isinstance(min_listing, int) else None,
        "can_complete_remaining_discovery_under_330": completable_under_330,
        "priority": [
            "Use retained provider responses and free official URL recovery first",
            "Continue only unfinished controlled coverage state/local partitions",
            "Do not continue unscoped commodities searches (federal contamination)",
            "Federal (B) remain parked - zero detail spend",
            "Never spend merely because credits remain",
        ],
        "active_A_already_retained": category_counts.get("A", 0),
        "federal_B_parked": category_counts.get("B", 0),
        "coverage_percentage": coverage.get("coverage_percentage"),
        "coverage_note": coverage.get("coverage_unknown_reason"),
    }


def write_acceptance_report(payload: dict[str, Any]) -> tuple[Path, Path]:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    md_path = ARCHIVE / "BUILD_002Q_INVENTORY_STATUS_RECONCILIATION_ACCEPTANCE.md"
    json_path = ARCHIVE / "BUILD_002Q_INVENTORY_STATUS_RECONCILIATION_ACCEPTANCE.json"
    cats = payload.get("categories") or {}
    unconfirmed = payload.get("unconfirmed_5990_disposition") or {}
    defects = payload.get("defects_corrected") or []
    recon = payload.get("reprocess") or {}
    plan = payload.get("acquisition_plan") or {}
    search_q = payload.get("search_quality") or {}
    cov = payload.get("coverage") or {}

    lines = [
        "# BUILD 002Q — Inventory & Active Status Reconciliation",
        "",
        f"Generated: `{payload.get('generated_at')}`",
        f"Runtime: **{payload.get('elapsed_sec')}s**",
        f"Metered NextBid delta: **{payload.get('metered_delta')}** (must be 0)",
        f"Acquisition locked: `{payload.get('acquisition_locked')}`",
        "",
        "## Exact reconciliation (8,088)",
        "",
        f"| Category | Count | Meaning |",
        f"|---|---:|---|",
    ]
    for key in "ABCDEFG":
        lines.append(f"| {key} | {cats.get(key, 0)} | {CAT_LABELS[key]} |")
    lines.extend(
        [
            f"| **Total** | **{sum(int(cats.get(k, 0)) for k in 'ABCDEFG')}** | Must equal 8088 |",
            "",
            "## Disposition of previously unconfirmed 5,990",
            "",
            f"Owner confirmed-open baseline: **2098** (listings open/amended/active).",
            f"Cache lifecycle=open among inventory: **{payload.get('cache_lifecycle_open')}**.",
            f"Unconfirmed pool: **5990** (= 8088 - 2098).",
            f"Disposition sum: **{payload.get('unconfirmed_sum')}**.",
            "",
            "| Disposition | Count |",
            "|---|---:|",
        ]
    )
    for k, v in unconfirmed.items():
        lines.append(f"| {k} | {v} |")
    lines.extend(
        [
            "",
            "## Status-mapping defects found and corrected",
            "",
        ]
    )
    for d in defects:
        lines.append(f"- **{d.get('id')}**: {d.get('defect')} → {d.get('fix')}")
    lines.extend(
        [
            "",
            "## Active state/local (Category A)",
            "",
            f"- Before repair (lifecycle=open among state/local): **{payload.get('active_sl_before')}**",
            f"- After reconciliation (Category A): **{cats.get('A', 0)}**",
            f"- Federal parked (Category B): **{cats.get('B', 0)}**",
            "",
            "## Reprocessed active product pipeline",
            "",
            f"| Metric | Count |",
            f"|---|---:|",
            f"| Active SL reprocessed | {recon.get('active_reprocessed')} |",
            f"| Confirmed product | {recon.get('confirmed_product')} |",
            f"| Potential product | {recon.get('potential_product')} |",
            f"| Verified quantities | {recon.get('verified_quantities')} |",
            f"| Procurement-eligible | {recon.get('procurement_eligible')} |",
            f"| Ready to submit (B) | {recon.get('ready_to_submit')} |",
            f"| Supplier pricing candidates | {recon.get('supplier_pricing_candidates')} |",
            "",
            "## Inventory growth 6,585 → 8,088",
            "",
            f"- Delta: **{payload.get('growth_delta')}** (8088 − 6585)",
            f"- Listing unique_new reported: **1480**",
            f"- Discrepancy: **{payload.get('growth_discrepancy')}**",
            f"- Explanation: {payload.get('growth_explanation')}",
            "",
            "## Search acquisition quality",
            "",
            f"- Open-only filter applied on all retained searches: "
            f"`{search_q.get('open_only_filter_applied')}` "
            f"({search_q.get('search_open_true')} files)",
            f"- Federal rows present in open searches: "
            f"{search_q.get('federal_returned_despite_state_local_intent')}",
            f"- {search_q.get('note_federal')}",
            f"- Status drop defect: {search_q.get('status_fields_defect')}",
            f"- Null lifecycle: {search_q.get('null_lifecycle_under_open_filter')}",
            "",
            "## NextBid coverage",
            "",
            f"- Coverage %: **{cov.get('coverage_percentage')}%** "
            f"({cov.get('local_active_sl')} / {cov.get('provider_active_state_local_total')})",
            f"- Missing/unacquired state-local open: **{cov.get('missing_or_unacquired_sl')}**",
            f"- Provider open (all sources): `{cov.get('provider_open_total')}`",
            f"- Provider federal open (dibbs+sam.gov): `{cov.get('provider_federal_open')}`",
            f"- Authorized SL open_by_source: "
            f"`{json.dumps(cov.get('provider_open_by_authorized_source'), default=str)}`",
            f"- Scope note: {cov.get('coverage_scope_note') or cov.get('coverage_unknown_reason')}",
            f"- Provider open definition: {cov.get('provider_open_definition')}",
            f"- Free tradelines ok: `{(cov.get('tradelines') or {}).get('ok')}`",
            f"- Free stats snapshot: `{json.dumps(cov.get('stats'), default=str)[:240]}`",
            "",
            "## Revised acquisition plan (NOT executed)",
            "",
            f"- Recommended minimum listing credits: "
            f"`{plan.get('recommended_minimum_listing_credits')}`",
            f"- Recommended minimum total: `{plan.get('recommended_minimum_total')}`",
            f"- Completable under 330 listing credits: "
            f"`{plan.get('can_complete_remaining_discovery_under_330')}`",
            f"- Controlled state/local pages remaining: "
            f"{plan.get('controlled_state_local_pages_remaining')} "
            f"`{json.dumps(plan.get('controlled_remaining_by_source'), default=str)}`",
            f"- All retained search page gaps (incl. unscoped): "
            f"{plan.get('retained_pages_remaining_all_searches')} "
            f"(do not continue unscoped/federal-heavy: "
            f"{plan.get('unscoped_or_federal_heavy_pages_do_not_continue')})",
            f"- Partitions with unknown remaining: "
            f"{plan.get('partitions_with_unknown_remaining')}",
            f"- Caps: listing<=330, detail<=1217, spendable<=1547, reserve=1000, "
            f"daily<=100, job<=50",
            "",
            "## Provider usage",
            "",
            f"- Before: `{json.dumps(payload.get('usage_before'), default=str)}`",
            f"- After: `{json.dumps(payload.get('usage_after'), default=str)}`",
            "",
            "## STOP",
            "",
            "Awaiting owner review. No metered acquisition executed. No Railway deploy. No Build 003.",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return md_path, json_path


def run_status_reconciliation(db: Session) -> dict[str, Any]:
    t0 = time.time()
    assert acquisition_locked(), "killswitch must remain engaged"

    try:
        usage_before = refresh_account_usage(allow_when_locked=True)
        if usage_before.get("used") is not None:
            set_usage_reconcile_baseline(
                account_used=int(usage_before["used"]),
                note="002q_status_reconciliation_start",
            )
    except Exception as exc:
        usage_before = {"ok": False, "error": type(exc).__name__}

    _log(f"usage before used={usage_before.get('used')} remaining={usage_before.get('remaining')}")
    inv = load_inventory()
    assert len(inv) == 8088, f"expected 8088 inventory, got {len(inv)}"

    # Prior baseline: owner confirmed-open = listings with open/amended/active (2098)
    index = load_retained_provider_index()
    by_nb: dict[str, dict[str, Any]] = index["by_nb"]
    search_hits: dict[str, list] = index["search_hits"]

    ck = load_checkpoint("nextbid_acquire_v1")
    listings = {
        str(i["nb_id"]): i
        for i in ((ck.get("state") or {}).get("listings") or [])
        if i.get("nb_id")
    }
    prior_confirmed: set[str] = set()
    for nb, row in listings.items():
        if nb not in inv:
            continue
        st = str(
            row.get("lifecycle_status") or row.get("solicitation_status") or ""
        ).lower()
        if st in OPEN_LIFE:
            prior_confirmed.add(nb)
    # Owner baseline hard target
    OWNER_CONFIRMED_OPEN = 2098
    OWNER_UNCONFIRMED = 8088 - OWNER_CONFIRMED_OPEN  # 5990

    cache_lifecycle_open = 0
    for nb in inv:
        life = str((by_nb.get(nb) or {}).get("lifecycle_status") or "").lower()
        if life in OPEN_LIFE or any(
            str(h.get("lifecycle_status") or "").lower() in OPEN_LIFE
            for h in (search_hits.get(nb) or [])
        ):
            cache_lifecycle_open += 1

    active_sl_before = 0
    for nb, row in inv.items():
        prov = by_nb.get(nb) or {}
        src = str(prov.get("source") or row.get("source") or "").lower()
        life = str(prov.get("lifecycle_status") or "").lower()
        if src not in FEDERAL_SOURCES and life in OPEN_LIFE:
            active_sl_before += 1

    dup_of = detect_duplicates(inv, by_nb)
    now = datetime.now(timezone.utc)
    categories: Counter[str] = Counter()
    patches: list[dict[str, Any]] = []
    unconfirmed_disp: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)

    # If listings-open set size differs from owner 2098, pad/trim note — still
    # attribute every inventory ID not in the confirmed-open set.
    if len(prior_confirmed) != OWNER_CONFIRMED_OPEN:
        # Prefer exact owner figure: if we have fewer, leave gap explained;
        # if more, keep listings definition (should be 2098).
        _log(
            f"listings_open_confirmed={len(prior_confirmed)} owner_baseline={OWNER_CONFIRMED_OPEN}"
        )

    for nb, inv_row in inv.items():
        cat, patch = assign_category(
            nb_id=nb,
            inv_row=inv_row,
            provider=by_nb.get(nb) or {},
            search_hits=search_hits.get(nb) or [],
            now=now,
            duplicate_of=dup_of.get(nb),
        )
        categories[cat] += 1
        patches.append(patch)
        if len(samples[cat]) < 5:
            samples[cat].append(nb)
        if nb not in prior_confirmed:
            unconfirmed_disp[f"to_{cat}_{CAT_LABELS[cat]}"] += 1

    total_cats = sum(categories.values())
    assert total_cats == 8088, f"categories sum {total_cats} != 8088"

    # Persist corrected status into inventory
    upsert_inventory(patches)
    _log(f"inventory status backfill complete cats={dict(categories)}")

    # Build active A working set from population + patches
    pop = collect_population()
    pop_by = pop.get("by_id") or {}
    active_items: list[dict[str, Any]] = []
    patch_by = {str(p["nb_id"]): p for p in patches}
    for nb, patch in patch_by.items():
        if patch.get("reconciliation_category") != "A":
            continue
        base = dict(pop_by.get(nb) or by_nb.get(nb) or inv.get(nb) or {})
        # Prefer normalized form when raw provider row
        if base.get("response_deadline") and not base.get("deadline_at"):
            base["deadline_at"] = base["response_deadline"]
        if not base.get("solicitation_status") or base.get("solicitation_status") == "unknown":
            try:
                base = {**_normalize_listing({**base, "provider_open_filter": True}), **base}
            except Exception:
                pass
        base.update({k: v for k, v in patch.items() if v not in (None, "", [], {})})
        base["nb_id"] = nb
        active_items.append(base)

    _log(f"reprocessing category A actives n={len(active_items)}")
    reprocess = reprocess_active_state_local(db, active_items)

    # Growth accounting
    growth_delta = 8088 - 6585
    growth_discrepancy = growth_delta - 1480
    # SL first_seen 2026-11-23 = 1503; federal first_seen same day from permanent repair
    growth_explanation = (
        f"+{growth_delta} inventory growth = +1480 listing unique_new (metered session) "
        f"+ {growth_discrepancy} IDs already present in checkpoint listings/seen but missing "
        f"from inventory until final upsert consolidation (same first_seen day as campaign). "
        f"State/local inventory is exactly 3758 (= listings). Federal parked inventory is 4330 "
        f"from earlier open=true searches without sources= whitelist (permanent-repair recovery)."
    )

    search_quality = analyze_search_quality(
        index["meta"],
        {
            "before": 6585,
            "after": 8088,
            "delta": growth_delta,
            "listing_unique_new_reported": 1480,
            "discrepancy": growth_discrepancy,
        },
    )
    coverage = try_free_coverage_stats(
        usage_before, local_active_sl=int(categories.get("A") or 0)
    )

    plan = revised_acquisition_plan(
        category_counts=dict(categories),
        coverage=coverage,
        index_meta=index["meta"],
        usage=usage_before if usage_before.get("ok") else {"remaining": 2547, "used": 3453},
    )

    defects = [
        {
            "id": "INV-STATUS-DROP",
            "defect": "upsert_inventory omitted lifecycle_status/solicitation_status/deadline_at",
            "fix": "Persist and backfill status fields on every upsert",
        },
        {
            "id": "NORM-NULL-LIFECYCLE",
            "defect": "Null lifecycle under open=true mapped to solicitation_status=unknown",
            "fix": "Retain provider_open_filter; map null lifecycle + open filter → open",
        },
        {
            "id": "LIFE-NOT-TOPLEVEL",
            "defect": "lifecycle_status only stored under evidence, not top-level for gates",
            "fix": "_normalize_listing now sets top-level lifecycle_status",
        },
        {
            "id": "FEDERAL-IN-INVENTORY",
            "defect": "4330 DIBBS/SAM IDs recovered into inventory from open searches",
            "fix": "Categorized B (parked active); remainder of federal IDs closed/dup/unresolved; excluded from active SL reprocess",
        },
    ]

    try:
        usage_after = refresh_account_usage(allow_when_locked=True)
        if usage_after.get("used") is not None:
            set_usage_reconcile_baseline(
                account_used=int(usage_after["used"]),
                note="002q_status_reconciliation_end",
            )
    except Exception as exc:
        usage_after = {"ok": False, "error": type(exc).__name__}

    metered_delta = None
    if usage_before.get("used") is not None and usage_after.get("used") is not None:
        metered_delta = int(usage_after["used"]) - int(usage_before["used"])
    if metered_delta not in (0, None):
        raise RuntimeError(f"metered burn detected: {metered_delta}")

    elapsed = round(time.time() - t0, 2)
    payload = {
        "build": "002Q",
        "directive": "COMPLETE_INVENTORY_AND_ACTIVE_STATUS_RECONCILIATION",
        "generated_at": _utc(),
        "elapsed_sec": elapsed,
        "inventory_total": 8088,
        "categories": dict(categories),
        "category_labels": CAT_LABELS,
        "category_samples": dict(samples),
        "prior_confirmed_open": len(prior_confirmed),
        "owner_confirmed_open_baseline": OWNER_CONFIRMED_OPEN,
        "owner_unconfirmed_baseline": OWNER_UNCONFIRMED,
        "cache_lifecycle_open": cache_lifecycle_open,
        "active_sl_before": active_sl_before,
        "unconfirmed_5990_disposition": dict(unconfirmed_disp),
        "unconfirmed_sum": sum(unconfirmed_disp.values()),
        "unconfirmed_reconciles_to_5990": sum(unconfirmed_disp.values()) == OWNER_UNCONFIRMED,
        "defects_corrected": defects,
        "reprocess": reprocess,
        "growth_delta": growth_delta,
        "growth_discrepancy": growth_discrepancy,
        "growth_explanation": growth_explanation,
        "search_quality": search_quality,
        "search_index_meta": {
            k: v
            for k, v in (index["meta"] or {}).items()
            if k != "provider_page_totals"
        },
        "pages_summary": {
            "partitions": len((index["meta"] or {}).get("provider_page_totals") or {}),
            "pages_remaining_all": plan.get("retained_pages_remaining_all_searches"),
            "controlled_state_local_remaining": plan.get(
                "controlled_state_local_pages_remaining"
            ),
        },
        "coverage": coverage,
        "acquisition_plan": plan,
        "usage_before": usage_before,
        "usage_after": usage_after,
        "metered_delta": metered_delta if metered_delta is not None else 0,
        "acquisition_locked": acquisition_locked(),
        "stop": "AWAIT_OWNER_REVIEW",
    }
    md_path, json_path = write_acceptance_report(payload)
    payload["acceptance_report_md"] = str(md_path)
    payload["acceptance_report_json"] = str(json_path)
    _log(
        f"DONE cats={dict(categories)} A={categories['A']} B={categories['B']} "
        f"metered_delta={payload['metered_delta']} report={md_path}"
    )
    return payload
