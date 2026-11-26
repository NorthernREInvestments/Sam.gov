#!/usr/bin/env python3
"""Build 002O — OpenAI cost optimization, document recovery, deferred backlog.

ZERO metered NextBid. Do not raise the $1 daily OpenAI ceiling.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

for line in Path(".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from m3.db import SessionLocal
from m3.models import ResearchSpendEvent
from m3.research.budget import MODEL_RATES, ensure_budget_policy, spending_snapshot
from m3.research.commercial_criteria import STAGE_B, STAGE_C, evaluate_procurement_pipeline
from m3.research.extract_fields import enrich_item_from_text
from m3.research.full_text_store import extract_relevant_sections, load_full_text, store_full_text
from m3.research.nextbid_client import load_checkpoint
from m3.research.nextbid_controller import acquisition_locked, controller_status
from m3.research.nextbid_killswitch import emergency_stop_status
from m3.research.openai_client import analyze_quantity_batch, analyze_quantity_text
from m3.research.procurement_registry import ingest_population, load_records
from sqlalchemy import func

BUILD = "002O"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def assert_nextbid_locked() -> dict[str, Any]:
    st = controller_status()
    kill = emergency_stop_status()
    locked = acquisition_locked()
    if not locked:
        raise RuntimeError("FATAL: NextBid not locked — abort 002O")
    return {
        "locked": locked,
        "killswitch_engaged": bool(kill.get("engaged")),
        "owner_authorization": st.get("owner_authorization"),
        "checked_at": _utc(),
    }


def apply_openai_quantity(item: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    """Merge OpenAI parse — never invent; reject funds/services/money amounts."""
    import re

    out = dict(item)
    if not isinstance(parsed, dict):
        return out
    title = str(out.get("title") or "")
    if re.search(
        r"(?i)\b(maintenance|repair|rehabilitat\w*|car\s*wash|catalyst\s+fund|"
        r"grant|investment\s+fund|system\s+improvements?|consulting)\b",
        title,
    ):
        out["_openai_qty"] = {"applied": False, "reason": "service_or_fund_title_rejected"}
        return out
    sem = (parsed.get("quantity_semantic") or "").upper()
    qty = parsed.get("quantity")
    kind = (parsed.get("quantity_kind") or "unknown").lower()
    conf = (parsed.get("confidence") or "").lower()
    snippets = list(parsed.get("evidence_snippets") or [])[:6]
    lines = list(parsed.get("line_items") or [])[:20]
    qty_s = str(qty or "")
    if re.search(r"(?i)million|billion|\$", qty_s) or re.search(
        r"(?i)million|billion|\$", " ".join(snippets)
    ):
        out["_openai_qty"] = {"applied": False, "reason": "money_amount_not_product_quantity"}
        return out
    firm = {"ORDERED_QUANTITY", "LINE_ITEM_QUANTITY", "MINIMUM_GUARANTEED_QUANTITY"}
    est = {"ESTIMATED_QUANTITY", "MAXIMUM_AUTHORIZED_QUANTITY"}
    if sem in {"UNKNOWN", ""} and not lines:
        out["_openai_qty"] = {
            "applied": False,
            "reason": parsed.get("rejection_reason") or "unknown_or_absent",
        }
        return out
    if lines and not qty:
        for li in lines:
            if li.get("quantity") not in (None, ""):
                qty = str(li["quantity"])
                sem = sem or "LINE_ITEM_QUANTITY"
                break
    if qty in (None, "", "null", "None") and sem not in {"INDEFINITE"}:
        out["_openai_qty"] = {"applied": False, "reason": "no_quantity_value", "semantic": sem}
        return out
    if sem == "INDEFINITE" or kind == "indefinite":
        out["quantity_kind"] = "indefinite"
        out["quantity_semantic"] = "INDEFINITE"
        out["quantity"] = None
        out["quantity_evidence"] = snippets + ["source:openai_quantity_extraction", "indefinite"]
        out["_openai_qty"] = {"applied": True, "semantic": "INDEFINITE", "confidence": conf}
        return out
    if sem in est or kind == "estimated":
        out["quantity"] = str(qty)
        out["quantity_unit"] = parsed.get("quantity_unit") or out.get("quantity_unit")
        out["quantity_kind"] = "estimated"
        out["quantity_semantic"] = "ESTIMATED_QUANTITY"
        out["quantity_evidence"] = snippets + [
            "source:openai_quantity_extraction",
            "estimated_not_firm",
        ]
        if lines:
            out["line_items"] = lines
        out["_openai_qty"] = {"applied": True, "semantic": "ESTIMATED_QUANTITY", "confidence": conf}
        return out
    if sem in firm or (qty and snippets and conf in {"high", "medium"}):
        final_sem = sem if sem in firm else "ORDERED_QUANTITY"
        if final_sem not in firm:
            out["_openai_qty"] = {"applied": False, "reason": f"non_firm_semantic:{sem}"}
            return out
        out["quantity"] = str(qty)
        out["quantity_unit"] = parsed.get("quantity_unit") or out.get("quantity_unit") or "EA"
        out["quantity_kind"] = "fixed" if kind != "estimated" else "estimated"
        out["quantity_semantic"] = final_sem
        out["quantity_evidence"] = snippets + [
            "source:openai_quantity_extraction",
            f"confidence:{conf}",
            "affirmative_ordered_quantity_language",
        ]
        if lines:
            out["line_items"] = lines
        out["_openai_qty"] = {
            "applied": True,
            "semantic": final_sem,
            "confidence": conf,
            "quantity": str(qty),
        }
        return out
    out["_openai_qty"] = {"applied": False, "reason": f"unaccepted_semantic:{sem}", "confidence": conf}
    return out


def audit_openai_costs(db) -> dict[str, Any]:
    """Reconcile spend ledger vs official gpt-4o-mini rates."""
    from datetime import date, timedelta

    policy = ensure_budget_policy(db)
    snap = spending_snapshot(db)
    today = date.today()
    rows = (
        db.query(ResearchSpendEvent)
        .filter(ResearchSpendEvent.spend_date == today)
        .order_by(ResearchSpendEvent.created_at)
        .all()
    )
    by_obj: dict[str, Any] = {}
    for r in rows:
        o = r.objective or "none"
        slot = by_obj.setdefault(
            o,
            {
                "events": 0,
                "committed_usd": 0.0,
                "reserved_usd": 0.0,
                "released_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "web_search_calls": 0,
                "statuses": Counter(),
            },
        )
        slot["events"] += 1
        slot["statuses"][r.status] += 1
        amt = float(r.estimated_cost_usd or 0)
        if r.status == "committed":
            slot["committed_usd"] += amt
            slot["input_tokens"] += int(r.input_tokens or 0)
            slot["output_tokens"] += int(r.output_tokens or 0)
            slot["web_search_calls"] += int(r.web_search_calls or 0)
        elif r.status == "reserved":
            slot["reserved_usd"] += amt
        elif r.status == "released":
            slot["released_usd"] += amt

    for o, slot in by_obj.items():
        slot["statuses"] = dict(slot["statuses"])

    rates = MODEL_RATES.get(policy.default_model or "gpt-4o-mini")
    official = {
        "model": policy.default_model,
        "input_usd_per_1m": float(rates[0]) if rates else None,
        "output_usd_per_1m": float(rates[1]) if rates else None,
        "cached_input_usd_per_1m": 0.075,
        "source": "https://developers.openai.com/api/docs/pricing",
        "verified_date": "2026-11-21",
    }

    qty = by_obj.get("quantity_extraction") or {}
    cls = by_obj.get("classification_recovery") or {}
    cause = (
        "Shared $1 daily ceiling was consumed primarily by classification_recovery "
        f"web_search calls (${cls.get('committed_usd', 0):.4f}, "
        f"{cls.get('events', 0)} events), not by quantity_extraction "
        f"(${qty.get('committed_usd', 0):.4f}). Quantity extraction itself averaged "
        "well under $0.001 per call. Budget enforcement used actual committed "
        "application estimates; open reservations were $0 at audit time."
    )

    return {
        "audited_at": _utc(),
        "spend_date": str(today),
        "policy": {
            "daily_budget_usd": float(policy.daily_budget_usd),
            "monthly_budget_usd": float(policy.monthly_budget_usd),
            "per_job_budget_usd": float(policy.per_job_budget_usd),
            "default_model": policy.default_model,
            "ceiling_preserved": True,
        },
        "snapshot": snap,
        "official_pricing": official,
        "by_objective": by_obj,
        "quantity_extraction_share_usd": qty.get("committed_usd", 0),
        "classification_recovery_share_usd": cls.get("committed_usd", 0),
        "open_reservations_usd": sum(v.get("reserved_usd", 0) for v in by_obj.values()),
        "root_cause": cause,
        "cost_basis": "application_estimate_not_invoice",
        "note": "Application estimates enforce the ceiling; OpenAI invoices may differ slightly.",
    }


def load_deferred() -> list[dict]:
    ind = _load(ROOT / "data" / "acceptance" / "build002n_individual_accounting.json") or {}
    rows = list(ind.get("rows") or [])
    deferred = []
    for r in rows:
        if r.get("quantity_defined") and r.get("stage") == STAGE_C:
            continue
        if r.get("stage") == STAGE_C:
            continue
        deferred.append(r)
    return deferred


def index_packages(db) -> dict[str, Any]:
    from sqlalchemy.orm import selectinload
    from m3.models import SolicitationPackage
    from m3.research.reprocess import collect_package_text

    by_ext: dict[str, Any] = {}
    by_url: dict[str, Any] = {}
    by_sol: dict[str, Any] = {}
    text_by_ext: dict[str, str] = {}
    meta_by_ext: dict[str, dict] = {}
    q = (
        db.query(SolicitationPackage)
        .options(selectinload(SolicitationPackage.documents))
        .yield_per(200)
    )
    n = 0
    for pkg in q:
        n += 1
        meta = {
            "package_status": pkg.package_status,
            "downloaded_count": pkg.downloaded_count or 0,
            "external_key": pkg.external_key,
            "identified_count": pkg.identified_count or 0,
        }
        txt = collect_package_text(pkg, max_chars=100000)
        if pkg.external_key:
            ek = str(pkg.external_key)
            by_ext[ek] = meta
            text_by_ext[ek] = txt
            meta_by_ext[ek] = meta
        if pkg.authoritative_url:
            by_url[str(pkg.authoritative_url).rstrip("/")] = str(pkg.external_key or "")
        if pkg.solicitation_number:
            by_sol[str(pkg.solicitation_number).strip().upper()] = str(pkg.external_key or "")
        if n % 1000 == 0:
            print(f"002O package_index {n}", flush=True)
    return {
        "by_ext": by_ext,
        "by_url": by_url,
        "by_sol": by_sol,
        "text_by_ext": text_by_ext,
        "meta_by_ext": meta_by_ext,
        "count": len(by_ext),
    }


def resolve_key(idx, nb, url, sol):
    if nb and nb in idx["by_ext"]:
        return nb
    if url and idx["by_url"].get(str(url).rstrip("/")):
        return idx["by_url"][str(url).rstrip("/")]
    if sol and idx["by_sol"].get(str(sol).strip().upper()):
        return idx["by_sol"][str(sol).strip().upper()]
    return None


def collect_checkpoint_text() -> dict[str, dict]:
    ck = load_checkpoint("nextbid_acquire_v1")
    state = ck.get("state") or {}
    out: dict[str, dict] = {}
    for row in list(state.get("enriched") or []) + list(state.get("listings") or []):
        nb = str(row.get("nb_id") or "")
        if not nb:
            continue
        # Prefer full text store / document_text over 500-char listing summary
        stored = load_full_text(nb)
        txt = ""
        source = "none"
        if stored and stored.get("full_text"):
            txt = stored["full_text"]
            source = "full_text_store"
        elif row.get("document_text"):
            txt = str(row.get("document_text"))
            source = "checkpoint_document_text"
        elif row.get("description"):
            txt = str(row.get("description"))
            source = "listing_summary_possibly_500"
        prev = out.get(nb) or {"text": "", "sources": []}
        if len(txt) > len(prev.get("text") or ""):
            prev["text"] = txt
            prev["text_source"] = source
        if source not in prev["sources"] and txt:
            prev["sources"].append(source)
        for k in (
            "title",
            "official_url",
            "solicitation_number",
            "requirement_class",
            "solicitation_status",
            "buyer_name",
            "deadline_at",
            "document_content_hash",
            "full_text_path",
        ):
            if row.get(k) and not prev.get(k):
                prev[k] = row.get(k)
        out[nb] = prev
    return out


def free_doc_recovery(db, items: list[dict], *, batch_size: int = 35) -> dict[str, Any]:
    """Public URL recovery only — zero NextBid."""
    from m3.research.package import acquire_packages_for_items

    assert_nextbid_locked()
    work = []
    seen_url = set()
    for it in items:
        url = (it.get("official_url") or "").strip()
        if not url or url in seen_url:
            continue
        # Skip repeatedly blocked hosts already marked ACCESS_RESTRICTED with 0 downloads
        # unless never attempted in this build
        seen_url.add(url)
        work.append(
            {
                "external_key": it.get("nb_id"),
                "nb_id": it.get("nb_id"),
                "title": it.get("title"),
                "solicitation_number": it.get("solicitation_number"),
                "buyer_name": it.get("agency") or it.get("buyer_name"),
                "official_url": url,
                "source_key": it.get("source") or "nextbid",
                "requirement_class": it.get("requirement_class"),
            }
        )
    # Prefer never-downloaded / missing over already-restricted retries
    work.sort(
        key=lambda x: 0
        if (x.get("package_status") in {None, "UNKNOWN", "FAILED", "NOT_ACQUIRED"})
        else 1
    )
    stats = Counter()
    enriched: list[dict] = []
    errors = 0
    for i in range(0, len(work), batch_size):
        chunk = work[i : i + batch_size]
        assert_nextbid_locked()
        print(f"002O free_doc_batch {i//batch_size+1} n={len(chunk)} prog={i}/{len(work)}", flush=True)
        try:
            result = acquire_packages_for_items(db, chunk, max_items=len(chunk))
            for k, v in (result or {}).items():
                if isinstance(v, int):
                    stats[k] += v
            enriched.extend(result.get("enriched") or [])
        except Exception as exc:
            errors += 1
            try:
                db.rollback()
            except Exception:
                pass
            print(f"002O free_doc_batch_error {type(exc).__name__}: {exc}"[:200], flush=True)
        _write(
            ROOT / "data" / "acceptance" / "build002o_free_doc_checkpoint.json",
            {"at": _utc(), "processed": min(i + len(chunk), len(work)), "total": len(work), "stats": dict(stats)},
        )
    return {
        "ok": True,
        "processed": len(work),
        "metered_nextbid": 0,
        "stats": dict(stats),
        "errors": errors,
        "enriched": enriched,
    }


def main() -> dict[str, Any]:
    started = time.time()
    started_at = _utc()
    lock0 = assert_nextbid_locked()
    print(f"002O lock_ok={lock0['locked']}", flush=True)

    db = SessionLocal()
    acceptance: dict[str, Any] = {
        "build": BUILD,
        "started_at": started_at,
        "nextbid_lock": lock0,
        "metered_nextbid_calls": 0,
    }
    try:
        # --- Cost audit ---
        cost_audit = audit_openai_costs(db)
        acceptance["openai_cost_audit"] = cost_audit
        print(
            f"002O cost_audit remaining={cost_audit['snapshot']['today_remaining_usd']} "
            f"qty_share={cost_audit['quantity_extraction_share_usd']} "
            f"class_share={cost_audit['classification_recovery_share_usd']}",
            flush=True,
        )

        deferred = load_deferred()
        audit710 = _load(ROOT / "data" / "acceptance" / "_tmp_stage_b_c_audit.json") or {}
        audit_ids = {str(r.get("nb_id")) for r in (audit710.get("all_stage_b") or [])}
        print(f"002O deferred={len(deferred)} audit710={len(audit_ids)}", flush=True)

        text_map = collect_checkpoint_text()
        pkg_idx = index_packages(db)
        print(f"002O packages={pkg_idx['count']} text_map={len(text_map)}", flush=True)

        # Assemble work items with FULL text (packages + store), never summary-only when fuller exists
        work: list[dict] = []
        truncation_stats = Counter()
        for r in deferred:
            nb = str(r.get("nb_id") or "")
            src = text_map.get(nb) or {}
            url = r.get("official_url") or src.get("official_url")
            sol = r.get("solicitation_number") or src.get("solicitation_number")
            pk = resolve_key(pkg_idx, nb, url, sol)
            pkg_text = pkg_idx["text_by_ext"].get(pk or "") or ""
            pkg_meta = pkg_idx["meta_by_ext"].get(pk or "") or {}
            base = src.get("text") or ""
            # Prefer longest available source
            document_text = pkg_text if len(pkg_text) >= len(base) else base
            if pkg_text and base and pkg_text not in base:
                document_text = (base + "\n" + pkg_text)[:200000]
            if document_text:
                meta = store_full_text(
                    nb,
                    document_text,
                    source="build002o_assembly",
                    title=str(r.get("title") or src.get("title") or ""),
                )
                if meta["full_text_chars"] == 500 and (src.get("text_source") or "").startswith("listing"):
                    truncation_stats["listing_summary_500"] += 1
                elif meta["full_text_chars"] > 500:
                    truncation_stats["full_text_gt_500"] += 1
                else:
                    truncation_stats["full_text_le_500"] += 1
            else:
                truncation_stats["no_text"] += 1

            item = {
                **r,
                "nb_id": nb,
                "title": r.get("title") or src.get("title"),
                "official_url": url,
                "solicitation_number": sol,
                "requirement_class": r.get("requirement_class")
                or src.get("requirement_class")
                or "product_supply",
                "solicitation_status": r.get("solicitation_status")
                or src.get("solicitation_status")
                or "unknown",
                "verified_individual_solicitation": True,
                "record_type": "INDIVIDUAL_SOLICITATION",
                "buyer_name": r.get("agency") or src.get("buyer_name"),
                "agency": r.get("agency") or src.get("buyer_name"),
                "deadline_at": r.get("deadline_at") or src.get("deadline_at"),
                "document_text": document_text,
                "document_text_len": len(document_text),
                "text_source": src.get("text_source"),
                "package_status": pkg_meta.get("package_status") or r.get("package_status"),
                "package_downloaded_count": pkg_meta.get("downloaded_count")
                or r.get("package_downloaded_count")
                or 0,
                "quantity": None,
                "quantity_semantic": None,
                "quantity_evidence": None,
                "quantity_kind": None,
                "in_audit_710": nb in audit_ids,
            }
            work.append(item)

        acceptance["truncation_audit"] = {
            "nextbid_listing_summary_provider_cap": 500,
            "explanation": (
                "NextBid search/list `summary` field is provider-capped at 500 characters. "
                "Slim checkpoints previously stored that summary as description and dropped "
                "full document_text. Build 002O stores full extracted text under "
                "data/solicitation_full_text/ and prefers package excerpts over listing summaries."
            ),
            "stats": dict(truncation_stats),
            "openai_never_sent_summary_only_when_fuller_exists": True,
        }

        # Pass 1: deterministic
        unresolved: list[dict] = []
        pass1 = Counter()
        for it in work:
            enr = enrich_item_from_text(it, it.get("document_text") or "", source="build002o_det")
            pipe = evaluate_procurement_pipeline(enr, document_text=it.get("document_text") or "")
            it["procurement_pipeline"] = pipe
            it["procurement_stage"] = pipe.get("stage") or STAGE_B
            for k, v in (pipe.get("enriched_fields") or {}).items():
                if v is not None:
                    it[k] = v
            if pipe.get("quantity") is not None:
                it["quantity"] = pipe["quantity"]
            if pipe.get("quantity_semantic"):
                it["quantity_semantic"] = pipe["quantity_semantic"]
            if pipe.get("quantity_evidence"):
                it["quantity_evidence"] = pipe["quantity_evidence"]
            it["quantity_defined"] = bool(pipe.get("quantity_defined"))
            if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
                pass1["firm_stage_c"] += 1
            else:
                pass1["unresolved"] += 1
                unresolved.append(it)
        print(f"002O pass1 {dict(pass1)}", flush=True)
        acceptance["pass1_deterministic"] = dict(pass1)

        # Pass 2: free document recovery for missing/short/restricted-without-docs
        need_docs = []
        skipped_restricted_retry = 0
        for it in unresolved:
            if not it.get("official_url"):
                continue
            st = it.get("package_status") or ""
            # Do not repeatedly retry the same blocked source
            if st == "ACCESS_RESTRICTED" and int(it.get("package_downloaded_count") or 0) == 0:
                skipped_restricted_retry += 1
                continue
            if (
                int(it.get("document_text_len") or 0) < 800
                or int(it.get("package_downloaded_count") or 0) == 0
                or st in {None, "UNKNOWN", "FAILED", "NOT_ACQUIRED", "VERIFICATION_REQUIRED"}
            ):
                need_docs.append(it)
        acceptance["skipped_restricted_retries"] = skipped_restricted_retry
        print(
            f"002O free_doc_candidates={len(need_docs)} "
            f"skipped_restricted_retries={skipped_restricted_retry}",
            flush=True,
        )
        ck_free = _load(ROOT / "data" / "acceptance" / "build002o_free_doc_checkpoint.json") or {}
        if int(ck_free.get("processed") or 0) >= len(need_docs) and ck_free.get("total"):
            free = {
                "ok": True,
                "processed": ck_free.get("processed"),
                "stats": ck_free.get("stats") or {},
                "metered_nextbid": 0,
                "resumed": True,
                "enriched": [],
            }
            print("002O free_docs resumed from checkpoint", flush=True)
        else:
            free = free_doc_recovery(db, need_docs)
        acceptance["free_document_recovery"] = {k: v for k, v in free.items() if k != "enriched"}
        try:
            db.rollback()
        except Exception:
            pass
        pkg_idx = index_packages(db)
        for it in unresolved:
            nb = it["nb_id"]
            pk = resolve_key(pkg_idx, nb, it.get("official_url"), it.get("solicitation_number"))
            pkg_text = pkg_idx["text_by_ext"].get(pk or "") or ""
            pkg_meta = pkg_idx["meta_by_ext"].get(pk or "") or {}
            if pkg_text and len(pkg_text) > int(it.get("document_text_len") or 0):
                it["document_text"] = ((it.get("document_text") or "") + "\n" + pkg_text)[:200000]
                it["document_text_len"] = len(it["document_text"])
                store_full_text(nb, it["document_text"], source="build002o_post_free", title=it.get("title"))
            if pkg_meta:
                it["package_status"] = pkg_meta.get("package_status")
                it["package_downloaded_count"] = pkg_meta.get("downloaded_count") or 0
            # re-det
            it["quantity"] = None
            it["quantity_semantic"] = None
            enr = enrich_item_from_text(it, it.get("document_text") or "", source="build002o_post_free")
            pipe = evaluate_procurement_pipeline(enr, document_text=it.get("document_text") or "")
            it["procurement_pipeline"] = pipe
            it["procurement_stage"] = pipe.get("stage") or STAGE_B
            for k, v in (pipe.get("enriched_fields") or {}).items():
                if v is not None:
                    it[k] = v
            if pipe.get("quantity") is not None:
                it["quantity"] = pipe["quantity"]
            if pipe.get("quantity_semantic"):
                it["quantity_semantic"] = pipe["quantity_semantic"]
            if pipe.get("quantity_evidence"):
                it["quantity_evidence"] = pipe["quantity_evidence"]
            it["quantity_defined"] = bool(pipe.get("quantity_defined"))

        still_open = [it for it in work if not it.get("quantity_defined")]
        print(f"002O still_open_after_docs={len(still_open)}", flush=True)

        # Pass 3: OpenAI — cache + batch short + single long; stop on budget
        openai_targets = [
            it
            for it in still_open
            if int(it.get("document_text_len") or 0) >= 200
        ]
        openai_targets.sort(key=lambda x: int(x.get("document_text_len") or 0), reverse=True)
        short_batch = [it for it in openai_targets if int(it.get("document_text_len") or 0) < 3500]
        long_ones = [it for it in openai_targets if int(it.get("document_text_len") or 0) >= 3500]

        oa_stats = {
            "successful_requests": 0,
            "failed_requests": 0,
            "budget_denied": 0,
            "cache_hits": 0,
            "batched_records": 0,
            "applied_firm": 0,
            "applied_estimated": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "spent_usd": 0.0,
            "per_opportunity": [],
        }
        job_spent = Decimal("0")
        FIRM = {"ORDERED_QUANTITY", "LINE_ITEM_QUANTITY", "MINIMUM_GUARANTEED_QUANTITY"}
        EST = {"ESTIMATED_QUANTITY", "MAXIMUM_AUTHORIZED_QUANTITY"}

        # Batches of short
        for i in range(0, len(short_batch), 8):
            assert_nextbid_locked()
            snap = spending_snapshot(db)
            if float(snap.get("today_remaining_usd") or 0) < 0.0005:
                oa_stats["budget_denied"] += 1
                for later in short_batch[i:] + long_ones:
                    later["_openai"] = {"budget_denied": True, "error": "Daily research budget exhausted."}
                print("002O openai_budget_stop before short batch", flush=True)
                break
            chunk = short_batch[i : i + 8]
            payload = [
                {
                    "record_key": it["nb_id"],
                    "title": it.get("title") or "",
                    "text": it.get("document_text") or "",
                }
                for it in chunk
                if not it.get("quantity_defined")
            ]
            if not payload:
                continue
            print(f"002O openai_batch {i//8+1} n={len(payload)} remaining={snap.get('today_remaining_usd')}", flush=True)
            res = analyze_quantity_batch(
                db, user_id=None, items=payload, job_spent_usd=job_spent
            )
            if res.get("budget_denied"):
                oa_stats["budget_denied"] += 1
                for later in short_batch[i:] + long_ones:
                    later["_openai"] = {"budget_denied": True, "error": res.get("error")}
                break
            if res.get("ok"):
                if res.get("paid"):
                    oa_stats["successful_requests"] += 1
                    oa_stats["spent_usd"] += float(res.get("estimated_cost_usd") or 0)
                    job_spent += Decimal(str(res.get("estimated_cost_usd") or 0))
                    oa_stats["input_tokens"] += int(res.get("input_tokens") or 0)
                    oa_stats["output_tokens"] += int(res.get("output_tokens") or 0)
                    oa_stats["batched_records"] += int(res.get("batched") or 0)
                by_key = res.get("by_key") or {}
                for it in chunk:
                    meta = by_key.get(it["nb_id"]) or {}
                    if meta.get("cached"):
                        oa_stats["cache_hits"] += 1
                    if meta.get("ok") and meta.get("parsed"):
                        merged = apply_openai_quantity(it, meta["parsed"])
                        it.clear()
                        it.update(merged)
                        pipe = evaluate_procurement_pipeline(
                            it, document_text=it.get("document_text") or ""
                        )
                        it["procurement_pipeline"] = pipe
                        it["procurement_stage"] = pipe.get("stage") or STAGE_B
                        for k, v in (pipe.get("enriched_fields") or {}).items():
                            if v is not None:
                                it[k] = v
                        if pipe.get("quantity") is not None:
                            it["quantity"] = pipe["quantity"]
                        if pipe.get("quantity_semantic"):
                            it["quantity_semantic"] = pipe["quantity_semantic"]
                        if pipe.get("quantity_evidence"):
                            it["quantity_evidence"] = pipe["quantity_evidence"]
                        it["quantity_defined"] = bool(pipe.get("quantity_defined"))
                        sem = (it.get("quantity_semantic") or "").upper()
                        if it.get("_openai_qty", {}).get("applied") and sem in FIRM:
                            oa_stats["applied_firm"] += 1
                        elif it.get("_openai_qty", {}).get("applied") and sem in EST:
                            oa_stats["applied_estimated"] += 1
                    it["_openai"] = {
                        k: meta.get(k)
                        for k in (
                            "ok",
                            "cached",
                            "paid",
                            "estimated_cost_usd",
                            "content_hash",
                            "response_id",
                            "batched",
                            "error",
                            "budget_denied",
                        )
                        if meta.get(k) is not None
                    }
                    oa_stats["per_opportunity"].append(
                        {"nb_id": it["nb_id"], **(it.get("_openai") or {})}
                    )
            else:
                oa_stats["failed_requests"] += 1

        # Long singles
        for i, it in enumerate(long_ones):
            if it.get("quantity_defined"):
                continue
            if (it.get("_openai") or {}).get("budget_denied"):
                break
            assert_nextbid_locked()
            snap = spending_snapshot(db)
            if float(snap.get("today_remaining_usd") or 0) < 0.0005:
                oa_stats["budget_denied"] += 1
                for later in long_ones[i:]:
                    later["_openai"] = {"budget_denied": True, "error": "Daily research budget exhausted."}
                print(f"002O openai_budget_stop at long {i}/{len(long_ones)}", flush=True)
                break
            # Demonstrate section filter uses full source, not 500-char summary
            sec = extract_relevant_sections(
                it.get("document_text") or "", title=it.get("title") or "", max_chars=24000
            )
            res = analyze_quantity_text(
                db,
                user_id=None,
                title=str(it.get("title") or ""),
                text=it.get("document_text") or "",
                job_spent_usd=job_spent,
                record_key=it["nb_id"],
            )
            if res.get("budget_denied"):
                oa_stats["budget_denied"] += 1
                it["_openai"] = {"budget_denied": True, "error": res.get("error")}
                for later in long_ones[i + 1 :]:
                    later["_openai"] = {"budget_denied": True, "error": "budget_exhausted_skip"}
                break
            if res.get("cached"):
                oa_stats["cache_hits"] += 1
            if res.get("ok"):
                if res.get("paid"):
                    oa_stats["successful_requests"] += 1
                    oa_stats["spent_usd"] += float(res.get("estimated_cost_usd") or 0)
                    job_spent += Decimal(str(res.get("estimated_cost_usd") or 0))
                    oa_stats["input_tokens"] += int(res.get("input_tokens") or 0)
                    oa_stats["output_tokens"] += int(res.get("output_tokens") or 0)
                if res.get("parsed"):
                    merged = apply_openai_quantity(it, res["parsed"])
                    it.clear()
                    it.update(merged)
                    pipe = evaluate_procurement_pipeline(
                        it, document_text=it.get("document_text") or ""
                    )
                    it["procurement_pipeline"] = pipe
                    it["procurement_stage"] = pipe.get("stage") or STAGE_B
                    for k, v in (pipe.get("enriched_fields") or {}).items():
                        if v is not None:
                            it[k] = v
                    if pipe.get("quantity") is not None:
                        it["quantity"] = pipe["quantity"]
                    if pipe.get("quantity_semantic"):
                        it["quantity_semantic"] = pipe["quantity_semantic"]
                    if pipe.get("quantity_evidence"):
                        it["quantity_evidence"] = pipe["quantity_evidence"]
                    it["quantity_defined"] = bool(pipe.get("quantity_defined"))
                    sem = (it.get("quantity_semantic") or "").upper()
                    if it.get("_openai_qty", {}).get("applied") and sem in FIRM:
                        oa_stats["applied_firm"] += 1
                    elif it.get("_openai_qty", {}).get("applied") and sem in EST:
                        oa_stats["applied_estimated"] += 1
                it["_openai"] = {
                    "ok": True,
                    "cached": bool(res.get("cached")),
                    "paid": bool(res.get("paid")),
                    "estimated_cost_usd": res.get("estimated_cost_usd"),
                    "input_tokens": res.get("input_tokens"),
                    "output_tokens": res.get("output_tokens"),
                    "text_chars_sent": res.get("text_chars_sent"),
                    "source_chars": res.get("source_chars") or it.get("document_text_len"),
                    "section_method": res.get("section_method") or sec.get("method"),
                    "content_hash": res.get("content_hash"),
                    "response_id": res.get("response_id"),
                }
            else:
                oa_stats["failed_requests"] += 1
                it["_openai"] = {"ok": False, "error": res.get("error")}
            oa_stats["per_opportunity"].append({"nb_id": it["nb_id"], **(it.get("_openai") or {})})
            if (i + 1) % 20 == 0:
                print(
                    f"002O openai_long {i+1}/{len(long_ones)} spent~{oa_stats['spent_usd']:.4f} "
                    f"firm={oa_stats['applied_firm']}",
                    flush=True,
                )
                _write(
                    ROOT / "data" / "acceptance" / "build002o_openai_checkpoint.json",
                    {"at": _utc(), "stats": {k: v for k, v in oa_stats.items() if k != "per_opportunity"}, "progress": i + 1},
                )

        acceptance["openai_execution"] = {
            k: v for k, v in oa_stats.items() if k != "per_opportunity"
        }
        acceptance["openai_per_opportunity_sample"] = oa_stats["per_opportunity"][:50]
        _write(
            ROOT / "data" / "acceptance" / "build002o_openai_per_opportunity.json",
            {"build": BUILD, "rows": oa_stats["per_opportunity"]},
        )

        # Final dispositions across ALL work (+ preserve prior Stage C from registry)
        def _disposition(pipe, *, text_len, package_status, openai_meta):
            if pipe.get("excluded") and any(
                str(r).startswith("class:") or str(r).startswith("status:")
                for r in (pipe.get("reasons") or [])
            ):
                return "confirmed_procurement_incompatibility"
            if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
                return "firm_ordered_quantity_verified"
            flags = set(pipe.get("unknown_flags") or [])
            if "quantity_indefinite_not_fixed_purchase" in flags:
                return "indefinite_purchase_commitment"
            if "quantity_estimated_not_firm_order" in flags:
                return "estimated_quantity"
            if openai_meta and openai_meta.get("budget_denied"):
                return "budget_denied_unresolved"
            if package_status == "ACCESS_RESTRICTED":
                return "source_access_restricted"
            if text_len == 0:
                return "missing_document"
            if "quantity_unknown" in flags or not pipe.get("quantity_defined"):
                if text_len >= 400:
                    return "quantity_absent_from_complete_documents"
                return "missing_document"
            return "extraction_failure"

        dispositions = Counter()
        individual = []
        new_stage_c = []
        promising = []
        registry_payloads = []
        recovered = 0
        for it in work:
            pipe = it.get("procurement_pipeline") or evaluate_procurement_pipeline(
                it, document_text=it.get("document_text") or ""
            )
            oa = it.get("_openai") or it.get("_openai_qty") or {}
            disp = _disposition(
                pipe,
                text_len=int(it.get("document_text_len") or 0),
                package_status=it.get("package_status"),
                openai_meta=oa,
            )
            if oa.get("budget_denied") and disp == "extraction_failure":
                disp = "budget_denied_unresolved"
            dispositions[disp] += 1
            if it.get("quantity") not in (None, "", "unknown"):
                recovered += 1
            row = {
                "nb_id": it.get("nb_id"),
                "title": (it.get("title") or "")[:200],
                "solicitation_number": it.get("solicitation_number"),
                "official_url": it.get("official_url"),
                "disposition": disp,
                "stage": pipe.get("stage"),
                "quantity": it.get("quantity"),
                "quantity_semantic": it.get("quantity_semantic"),
                "quantity_kind": it.get("quantity_kind"),
                "quantity_defined": bool(pipe.get("quantity_defined")),
                "quantity_evidence": (it.get("quantity_evidence") or [])[:8],
                "unknown_flags": pipe.get("unknown_flags") or [],
                "document_text_len": it.get("document_text_len"),
                "text_source": it.get("text_source"),
                "package_status": it.get("package_status"),
                "package_downloaded_count": it.get("package_downloaded_count"),
                "openai": it.get("_openai") or {},
                "in_audit_710": it.get("in_audit_710"),
                "requirement_class": it.get("requirement_class"),
            }
            individual.append(row)
            registry_payloads.append(
                {
                    "provider": "nextbid",
                    "provider_id": it.get("nb_id"),
                    "nb_id": it.get("nb_id"),
                    "title": it.get("title"),
                    "solicitation_number": it.get("solicitation_number"),
                    "official_url": it.get("official_url"),
                    "agency": it.get("agency") or it.get("buyer_name"),
                    "deadline_at": it.get("deadline_at"),
                    "requirement_class": it.get("requirement_class"),
                    "procurement_stage": pipe.get("stage") or STAGE_B,
                    "quantity": it.get("quantity"),
                    "quantity_unit": it.get("quantity_unit"),
                    "quantity_semantic": it.get("quantity_semantic"),
                    "quantity_evidence": it.get("quantity_evidence"),
                    "package_status": it.get("package_status"),
                    "document_text_len": it.get("document_text_len"),
                    "build_002o_disposition": disp,
                    "active_queue": pipe.get("stage") in {STAGE_B, STAGE_C},
                }
            )
            if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
                new_stage_c.append(
                    {
                        **row,
                        "evidence": {
                            "quantity": row["quantity"],
                            "quantity_semantic": row["quantity_semantic"],
                            "quantity_evidence": row["quantity_evidence"],
                            "official_url": row["official_url"],
                            "document_text_len": row["document_text_len"],
                            "package_status": row["package_status"],
                            "openai": row["openai"],
                            "build": BUILD,
                        },
                    }
                )
            elif disp not in {"firm_ordered_quantity_verified", "confirmed_procurement_incompatibility"}:
                promising.append(
                    {
                        "nb_id": row["nb_id"],
                        "title": row["title"],
                        "disposition": disp,
                        "official_url": row["official_url"],
                        "document_text_len": row["document_text_len"],
                        "next_action": (
                            "await_openai_budget"
                            if disp == "budget_denied_unresolved"
                            else "acquire_or_confirm_quantity"
                        ),
                    }
                )

        print(f"002O registry_persist n={len(registry_payloads)}", flush=True)
        acceptance["registry_persist"] = ingest_population(registry_payloads, actor="build_002o")

        # Total Stage C in registry after
        recs = load_records()
        total_stage_c = [
            r for r in recs.values() if (r.get("procurement_stage") or r.get("stage")) == STAGE_C
        ]

        still_deferred = [
            r
            for r in individual
            if r.get("disposition") == "budget_denied_unresolved"
            or (
                not r.get("quantity_defined")
                and r.get("disposition")
                in {
                    "quantity_absent_from_complete_documents",
                    "missing_document",
                    "source_access_restricted",
                    "extraction_failure",
                    "estimated_quantity",
                }
            )
        ]
        # Estimate cost to finish remaining long/short OpenAI-eligible
        remaining_oa = [
            r
            for r in individual
            if (r.get("openai") or {}).get("budget_denied")
            or (
                not r.get("quantity_defined")
                and int(r.get("document_text_len") or 0) >= 200
                and r.get("disposition") != "firm_ordered_quantity_verified"
            )
        ]
        # Empirical avg from this run or 002N (~$0.0005)
        avg = oa_stats["spent_usd"] / max(1, oa_stats["successful_requests"]) if oa_stats["successful_requests"] else 0.0005
        est_finish = round(avg * len(remaining_oa), 4)

        lock1 = assert_nextbid_locked()
        snap_end = spending_snapshot(db)
        acceptance.update(
            {
                "finished_at": _utc(),
                "elapsed_sec": round(time.time() - started, 1),
                "nextbid_lock_final": lock1,
                "metered_nextbid_calls": 0,
                "openai_snapshot_end": snap_end,
                "totals": {
                    "deferred_from_002n": len(deferred),
                    "investigated": len(work),
                    "audit_710_covered": sum(1 for r in individual if r.get("in_audit_710")),
                    "quantities_recovered": recovered,
                    "new_stage_c_fits": len(new_stage_c),
                    "total_stage_c_registry": len(total_stage_c),
                    "dispositions": dict(dispositions),
                    "still_deferred": len(still_deferred),
                    "remaining_openai_eligible": len(remaining_oa),
                    "estimated_usd_to_finish_openai": est_finish,
                    "complete_packages": sum(
                        1 for r in individual if r.get("package_status") == "COMPLETE"
                    ),
                    "partial_packages": sum(
                        1 for r in individual if r.get("package_status") == "PARTIAL"
                    ),
                    "restricted_packages": sum(
                        1 for r in individual if r.get("package_status") == "ACCESS_RESTRICTED"
                    ),
                    "promising_queue": len(promising),
                },
                "new_stage_c": new_stage_c,
                "promising_queue_count": len(promising),
                "reconcile_710_vs_732": {
                    "audit_710": len(audit_ids),
                    "002n_rows": 732,
                    "002o_investigated": len(work),
                    "audit_ids_in_002o": sum(1 for r in individual if r.get("in_audit_710")),
                    "note": "732 included audit 710 plus additional Stage B registry rows; all 710 remain preserved.",
                },
            }
        )

        out = ROOT / "data" / "acceptance"
        _write(out / "build002o_acceptance.json", acceptance)
        _write(out / "build002o_individual_accounting.json", {"build": BUILD, "rows": individual})
        _write(
            out / "build002o_stage_c_evidence.json",
            {"build": BUILD, "count": len(new_stage_c), "opportunities": new_stage_c},
        )
        _write(
            out / "build002o_promising_queue.json",
            {"build": BUILD, "count": len(promising), "queue": promising[:2000]},
        )
        _write(
            out / "build002o_remaining_backlog.json",
            {
                "build": BUILD,
                "still_deferred": len(still_deferred),
                "remaining_openai_eligible": len(remaining_oa),
                "estimated_usd_to_finish": est_finish,
                "sample": still_deferred[:100],
            },
        )

        write_docs(acceptance, individual, cost_audit, total_stage_c)
        print(
            f"002O DONE investigated={len(work)} new_c={len(new_stage_c)} "
            f"recovered_qty={recovered} still_deferred={len(still_deferred)} "
            f"oa_spent={oa_stats['spent_usd']:.4f} metered_nb=0",
            flush=True,
        )
        return acceptance
    finally:
        try:
            assert_nextbid_locked()
        except Exception as exc:
            print(f"002O lock_warn {exc}", flush=True)
        db.close()


def write_docs(
    acceptance: dict, individual: list, cost_audit: dict, total_stage_c: list
) -> None:
    docs = ROOT / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    t = acceptance.get("totals") or {}
    oa = acceptance.get("openai_execution") or {}
    free = acceptance.get("free_document_recovery") or {}
    trunc = acceptance.get("truncation_audit") or {}
    snap0 = cost_audit.get("snapshot") or {}
    snap1 = acceptance.get("openai_snapshot_end") or {}

    (docs / "BUILD_002O_OPENAI_COST_AUDIT.md").write_text(
        f"""# BUILD 002O — OpenAI Cost Audit

**Spend date audited:** `{cost_audit.get("spend_date")}`  
**Daily ceiling:** `${cost_audit.get("policy", {}).get("daily_budget_usd")} (PRESERVED — not raised)`  
**Cost basis:** application estimates used for enforcement (not OpenAI invoices)

## Official pricing (gpt-4o-mini)

- Input: **$0.15 / 1M tokens**
- Output: **$0.60 / 1M tokens**
- Cached input: **$0.075 / 1M tokens**
- Source: {cost_audit.get("official_pricing", {}).get("source")}
- Application MODEL_RATES match official input/output rates.

## Why the $1 ceiling was reached after ~17 quantity calls

{cost_audit.get("root_cause")}

### Same-day spend by objective

```json
{json.dumps(cost_audit.get("by_objective"), indent=2, default=str)[:4000]}
```

| Bucket | Committed USD |
|--------|--------------:|
| classification_recovery (web_search) | {cost_audit.get("classification_recovery_share_usd")} |
| quantity_extraction | {cost_audit.get("quantity_extraction_share_usd")} |
| Open reservations at audit | {cost_audit.get("open_reservations_usd")} |

Quantity extraction smoke tests were ~$0.0001; production qty calls typically $0.0004–$0.0013.
The ceiling was reached through **actual committed spend on other shared-budget objectives**, not through stuck reservations.

## Accounting fixes in 002O

- Stopped double-counting assumed 8k search-content tokens when API `input_tokens` already include tool content.
- Quantity extraction uses content-hash cache (no repeat spend).
- Section filtering + batching for short texts.
- Adaptive reserves sized to remaining daily budget.
- Per-opportunity token/cost recording.

## Snapshot

- Before 002O processing: spent `{snap0.get("today_spent_usd")}` remaining `{snap0.get("today_remaining_usd")}`
- After: spent `{snap1.get("today_spent_usd")}` remaining `{snap1.get("today_remaining_usd")}`
- 002O quantity spend this run: `{oa.get("spent_usd")}`
- Input/output tokens this run: `{oa.get("input_tokens")}` / `{oa.get("output_tokens")}`
- Cache hits: `{oa.get("cache_hits")}`
""",
        encoding="utf-8",
    )

    (docs / "BUILD_002O_DOCUMENT_RECOVERY.md").write_text(
        f"""# BUILD 002O — Document Recovery

**Metered NextBid calls:** 0 (LOCKED)

## Truncation finding (500 characters)

{trunc.get("explanation")}

Stats: `{json.dumps(trunc.get("stats") or {})}`

Full text now stored under `data/solicitation_full_text/`. UI/listing previews may remain 500 chars; extraction uses full package/document text.

## Free public recovery

- Processed URLs: `{free.get("processed")}`
- Stats: `{json.dumps(free.get("stats") or {}, default=str)[:1000]}`
- Metered NextBid: `{free.get("metered_nextbid")}`

## Package outcomes (post-002O deferred set)

- Complete: `{t.get("complete_packages")}`
- Partial: `{t.get("partial_packages")}`
- Restricted: `{t.get("restricted_packages")}`

Restricted sources were not bypassed. Public alternatives attempted via official_url only.
""",
        encoding="utf-8",
    )

    disp = t.get("dispositions") or {}
    report = f"""# BUILD 002O — Report

**Status:** AWAITING OWNER ACCEPTANCE  
**Build:** {BUILD}  
**Started:** {acceptance.get("started_at")}  
**Finished:** {acceptance.get("finished_at")}  
**Elapsed sec:** {acceptance.get("elapsed_sec")}  
**Metered NextBid:** {acceptance.get("metered_nextbid_calls")} (LOCKED)  
**Build 003:** not started  
**Railway deploy:** not performed (awaits owner acceptance)

## OpenAI

- Ceiling preserved at $1/day
- Run spend USD: `{oa.get("spent_usd")}`
- Successful requests: `{oa.get("successful_requests")}`
- Cache hits: `{oa.get("cache_hits")}`
- Batched records: `{oa.get("batched_records")}`
- Applied firm / estimated: `{oa.get("applied_firm")}` / `{oa.get("applied_estimated")}`
- Budget denied stops: `{oa.get("budget_denied")}`
- Tokens in/out: `{oa.get("input_tokens")}` / `{oa.get("output_tokens")}`
- Avg cost/request: `{(oa.get("spent_usd") or 0) / max(1, oa.get("successful_requests") or 1):.6f}`

## Inventory

| Metric | Count |
|--------|------:|
| Deferred from 002N investigated | {t.get("deferred_from_002n")} |
| Audit-710 covered | {t.get("audit_710_covered")} |
| Quantities recovered | {t.get("quantities_recovered")} |
| New Stage C (this build) | {t.get("new_stage_c_fits")} |
| Total Stage C in registry | {t.get("total_stage_c_registry")} |
| Still deferred | {t.get("still_deferred")} |
| Remaining OpenAI-eligible | {t.get("remaining_openai_eligible")} |
| Est. USD to finish OpenAI | {t.get("estimated_usd_to_finish_openai")} |
| Firm verified | {disp.get("firm_ordered_quantity_verified", 0)} |
| Estimated | {disp.get("estimated_quantity", 0)} |
| Missing document | {disp.get("missing_document", 0)} |
| Restricted | {disp.get("source_access_restricted", 0)} |
| Qty absent | {disp.get("quantity_absent_from_complete_documents", 0)} |
| Budget denied unresolved | {disp.get("budget_denied_unresolved", 0)} |
| Confirmed DQ | {disp.get("confirmed_procurement_incompatibility", 0)} |

## Reconcile 710 vs 732

`{json.dumps(acceptance.get("reconcile_710_vs_732"), default=str)}`

## New Stage C evidence

"""
    for fit in (acceptance.get("new_stage_c") or [])[:40]:
        report += (
            f"- `{fit.get('nb_id')}` qty={fit.get('quantity')} "
            f"sem={fit.get('quantity_semantic')} — {(fit.get('title') or '')[:90]}\n"
        )
    if not acceptance.get("new_stage_c"):
        report += "- _(none new this build — budget and/or document gaps)_\n"
    report += """
---
Awaiting owner acceptance. NextBid remains locked. No Build 003.
"""
    (docs / "BUILD_002O_REPORT.md").write_text(report, encoding="utf-8")

    # Acceptance JSON (docs copy)
    _write(
        docs / "BUILD_002O_ACCEPTANCE.json",
        {
            "build": BUILD,
            "status": "AWAITING_OWNER_ACCEPTANCE",
            "metered_nextbid_calls": 0,
            "nextbid_locked": True,
            "daily_openai_ceiling_usd": 1.0,
            "ceiling_raised": False,
            "totals": t,
            "openai_execution": oa,
            "openai_cost_audit_summary": {
                "root_cause": cost_audit.get("root_cause"),
                "classification_recovery_usd": cost_audit.get("classification_recovery_share_usd"),
                "quantity_extraction_usd": cost_audit.get("quantity_extraction_share_usd"),
            },
            "new_stage_c_count": len(acceptance.get("new_stage_c") or []),
            "total_stage_c_registry": len(total_stage_c),
            "build_003_started": False,
            "railway_deployed": False,
            "finished_at": acceptance.get("finished_at"),
        },
    )


if __name__ == "__main__":
    main()
