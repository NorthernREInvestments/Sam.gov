#!/usr/bin/env python3
"""Build 002P — complete procurement backlog with one-time +$1 OpenAI authorization.

NEXTBID LOCKED. Zero metered NextBid. No web_search. Restore $1 daily ceiling after.
"""

from __future__ import annotations

import json
import os
import sys
import time
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
from m3.research.budget import ensure_budget_policy, spending_snapshot
from m3.research.commercial_criteria import STAGE_B, STAGE_C, evaluate_procurement_pipeline
from m3.research.extract_fields import enrich_item_from_text
from m3.research.full_text_store import extract_relevant_sections, load_full_text, store_full_text
from m3.research.nextbid_client import load_checkpoint
from m3.research.nextbid_controller import acquisition_locked, controller_status
from m3.research.nextbid_killswitch import emergency_stop_status
from m3.research.openai_client import analyze_quantity_batch, analyze_quantity_text
from m3.research.procurement_registry import ingest_population, load_records
from sqlalchemy import func

BUILD = "002P"
ADDITIONAL_USD = Decimal("1.00")
NORMAL_DAILY = Decimal("1.00")
NORMAL_PER_JOB = Decimal("0.50")
FIRM = {"ORDERED_QUANTITY", "LINE_ITEM_QUANTITY", "MINIMUM_GUARANTEED_QUANTITY"}
EST = {"ESTIMATED_QUANTITY", "MAXIMUM_AUTHORIZED_QUANTITY"}


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
    locked = acquisition_locked()
    if not locked:
        raise RuntimeError("FATAL: NextBid not locked — abort 002P")
    return {
        "locked": locked,
        "killswitch": emergency_stop_status(),
        "owner_authorization": controller_status().get("owner_authorization"),
        "checked_at": _utc(),
    }


def _load_002o():
    import importlib.util

    path = ROOT / "scripts" / "build002o_run.py"
    spec = importlib.util.spec_from_file_location("build002o_run_mod", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_O2 = None


def _o2():
    global _O2
    if _O2 is None:
        _O2 = _load_002o()
    return _O2


def apply_openai_quantity(item: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    return _o2().apply_openai_quantity(item, parsed)


def committed_since(db, *, after_ts: datetime | None, objectives: set[str]) -> Decimal:
    q = db.query(ResearchSpendEvent).filter(
        ResearchSpendEvent.status == "committed",
        ResearchSpendEvent.objective.in_(list(objectives)),
    )
    if after_ts is not None:
        q = q.filter(ResearchSpendEvent.created_at >= after_ts)
    total = Decimal("0")
    for r in q.all():
        total += Decimal(str(r.estimated_cost_usd or 0))
    return total


def authorize_additional_budget(db) -> dict[str, Any]:
    """Temporarily expand daily/per-job headroom by exactly +$1 for tonight only."""
    policy = ensure_budget_policy(db)
    snap = spending_snapshot(db)
    spent = Decimal(str(snap["today_spent_usd"]))
    original = {
        "daily_budget_usd": float(policy.daily_budget_usd),
        "per_job_budget_usd": float(policy.per_job_budget_usd),
        "per_user_daily_budget_usd": float(policy.per_user_daily_budget_usd),
    }
    # Headroom = current spent + $1 additional (never a permanent ceiling raise)
    new_daily = float(spent + ADDITIONAL_USD)
    policy.daily_budget_usd = new_daily
    policy.per_job_budget_usd = float(ADDITIONAL_USD)  # job may consume the full +$1
    # Keep per-user aligned so it cannot block the authorized additional dollar
    if float(policy.per_user_daily_budget_usd or 0) < new_daily:
        policy.per_user_daily_budget_usd = new_daily
    db.add(policy)
    db.commit()
    db.refresh(policy)
    auth = {
        "authorized_at": _utc(),
        "scope": "build_002p_one_time",
        "max_additional_usd": float(ADDITIONAL_USD),
        "original_policy": original,
        "temporary_daily_budget_usd": new_daily,
        "temporary_per_job_budget_usd": float(ADDITIONAL_USD),
        "spent_at_authorization": float(spent),
        "web_search_disabled_for_run": True,
        "note": "Owner-authorized one-time +$1 for quantity_extraction only; restore after run",
    }
    _write(ROOT / "data" / "acceptance" / "build002p_openai_authorization.json", auth)
    return auth


def restore_normal_ceiling(db, original: dict[str, Any]) -> dict[str, Any]:
    policy = ensure_budget_policy(db)
    policy.daily_budget_usd = float(original.get("daily_budget_usd") or NORMAL_DAILY)
    policy.per_job_budget_usd = float(original.get("per_job_budget_usd") or NORMAL_PER_JOB)
    policy.per_user_daily_budget_usd = float(
        original.get("per_user_daily_budget_usd") or NORMAL_DAILY
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    out = {
        "restored_at": _utc(),
        "daily_budget_usd": float(policy.daily_budget_usd),
        "per_job_budget_usd": float(policy.per_job_budget_usd),
        "per_user_daily_budget_usd": float(policy.per_user_daily_budget_usd),
    }
    _write(ROOT / "data" / "acceptance" / "build002p_ceiling_restored.json", out)
    return out


def index_packages(db):
    return _o2().index_packages(db)


def resolve_key(idx, nb, url, sol):
    return _o2().resolve_key(idx, nb, url, sol)


def collect_text_map() -> dict[str, dict]:
    return _o2().collect_checkpoint_text()


def load_all_deferred() -> list[dict]:
    """All unresolved from 002O individual accounting + any remaining backlog."""
    ind = _load(ROOT / "data" / "acceptance" / "build002o_individual_accounting.json") or {}
    rows = list(ind.get("rows") or [])
    # Also include 002N rows that were Stage C (retain) via separate path — deferred only here
    deferred = []
    for r in rows:
        if r.get("stage") == STAGE_C and r.get("quantity_defined"):
            continue
        deferred.append(r)
    return deferred


def disposition(pipe, *, text_len, package_status, openai_meta, deadline_closed=False):
    if deadline_closed:
        return "archived_or_closed"
    if pipe.get("excluded") and any(
        str(x).startswith("class:") or str(x).startswith("status:")
        for x in (pipe.get("reasons") or [])
    ):
        return "confirmed_disqualification"
    if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
        return "verified_procurement_fit"
    flags = set(pipe.get("unknown_flags") or [])
    if "quantity_indefinite_not_fixed_purchase" in flags:
        return "confirmed_indefinite"
    if "quantity_estimated_not_firm_order" in flags:
        return "estimated_quantity"
    if openai_meta and openai_meta.get("budget_denied"):
        return "budget_denied_unresolved"
    if package_status == "ACCESS_RESTRICTED":
        return "restricted_document"
    if text_len == 0:
        return "missing_document"
    if not pipe.get("quantity_defined"):
        return "promising_awaiting_verification" if text_len >= 200 else "missing_document"
    return "promising_awaiting_verification"


def sync_queues_to_db(queues: dict[str, list]) -> dict[str, Any]:
    """Persist queue membership onto ProcurementRecord.active_queue + raw_snapshot."""
    from m3.bootstrap import init_database
    from m3.models import ProcurementRecord

    init_database()
    db = SessionLocal()
    updated = 0
    try:
        # Map nb_id -> queue name
        by_nb: dict[str, str] = {}
        for qname, rows in queues.items():
            for r in rows:
                nb = str(r.get("nb_id") or "")
                if nb:
                    by_nb[nb] = qname
        recs = db.query(ProcurementRecord).all()
        for prev in recs:
            nb = prev.nb_id or prev.provider_id
            qname = by_nb.get(str(nb or ""))
            snap = dict(prev.raw_snapshot or {})
            if qname:
                snap["owner_queue"] = qname
                prev.raw_snapshot = snap
                # Only verified fits + promising stay in active processing queue
                prev.active_queue = qname in {
                    "verified_procurement_fits",
                    "promising_awaiting_verification",
                    "estimated_quantity",
                }
                if qname in {"archived_or_closed", "confirmed_disqualification"}:
                    prev.archived = True
                    prev.active_queue = False
                updated += 1
                db.add(prev)
        db.commit()
        return {"ok": True, "updated": updated}
    except Exception as exc:
        db.rollback()
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        db.close()


def main() -> dict[str, Any]:
    t0 = time.time()
    lock0 = assert_nextbid_locked()
    print(f"002P lock_ok={lock0['locked']}", flush=True)

    db = SessionLocal()
    original_policy = None
    acceptance: dict[str, Any] = {
        "build": BUILD,
        "started_at": _utc(),
        "nextbid_lock": lock0,
        "metered_nextbid_calls": 0,
        "web_search_used": False,
        "max_additional_openai_usd": float(ADDITIONAL_USD),
    }
    baseline_ts = datetime.now(timezone.utc)
    try:
        auth = authorize_additional_budget(db)
        original_policy = auth["original_policy"]
        acceptance["openai_authorization"] = auth
        snap0 = spending_snapshot(db)
        acceptance["openai_snapshot_start"] = snap0
        print(
            f"002P auth additional=$1 temp_daily={auth['temporary_daily_budget_usd']} "
            f"remain={snap0['today_remaining_usd']}",
            flush=True,
        )

        deferred = load_all_deferred()
        # Ensure prior Stage C from registry are retained in reconcile later
        print(f"002P deferred={len(deferred)}", flush=True)

        text_map = collect_text_map()
        pkg_idx = index_packages(db)
        print(f"002P packages={pkg_idx['count']}", flush=True)

        work: list[dict] = []
        for r in deferred:
            nb = str(r.get("nb_id") or "")
            src = text_map.get(nb) or {}
            url = r.get("official_url") or src.get("official_url")
            sol = r.get("solicitation_number") or src.get("solicitation_number")
            pk = resolve_key(pkg_idx, nb, url, sol)
            pkg_text = pkg_idx["text_by_ext"].get(pk or "") or ""
            pkg_meta = pkg_idx["meta_by_ext"].get(pk or "") or {}
            stored = load_full_text(nb)
            base = (stored or {}).get("full_text") or src.get("text") or ""
            document_text = pkg_text if len(pkg_text) >= len(base) else base
            if pkg_text and base and pkg_text not in document_text:
                document_text = (document_text + "\n" + pkg_text)[:200000]
            if document_text:
                store_full_text(nb, document_text, source="build002p", title=r.get("title"))
            work.append(
                {
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
                    "package_status": pkg_meta.get("package_status") or r.get("package_status"),
                    "package_downloaded_count": pkg_meta.get("downloaded_count")
                    or r.get("package_downloaded_count")
                    or 0,
                    "quantity": None,
                    "quantity_semantic": None,
                    "quantity_evidence": None,
                    "quantity_kind": None,
                }
            )

        # Deterministic first
        for it in work:
            enr = enrich_item_from_text(it, it.get("document_text") or "", source="build002p_det")
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

        openai_targets = [
            it
            for it in work
            if not it.get("quantity_defined") and int(it.get("document_text_len") or 0) >= 200
        ]
        openai_targets.sort(key=lambda x: int(x.get("document_text_len") or 0), reverse=True)
        short = [it for it in openai_targets if int(it.get("document_text_len") or 0) < 3500]
        long = [it for it in openai_targets if int(it.get("document_text_len") or 0) >= 3500]
        print(
            f"002P openai_targets={len(openai_targets)} short={len(short)} long={len(long)}",
            flush=True,
        )

        oa = {
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
            "web_search_calls": 0,
            "per_opportunity": [],
        }
        job_spent = Decimal("0")
        stop = False

        def additional_spent() -> Decimal:
            return committed_since(
                db,
                after_ts=baseline_ts,
                objectives={"quantity_extraction", "quantity_extraction_batch"},
            )

        def budget_ok() -> bool:
            spent = additional_spent()
            oa["spent_usd"] = float(spent)
            if spent >= ADDITIONAL_USD - Decimal("0.0004"):
                return False
            snap = spending_snapshot(db)
            return float(snap.get("today_remaining_usd") or 0) >= 0.0005

        # Short batches — NO web_search
        for i in range(0, len(short), 10):
            assert_nextbid_locked()
            if not budget_ok():
                oa["budget_denied"] += 1
                for later in short[i:] + long:
                    later["_openai"] = {
                        "budget_denied": True,
                        "error": "additional_$1_exhausted",
                    }
                print(f"002P budget_stop short@{i} spent={oa['spent_usd']}", flush=True)
                stop = True
                break
            chunk = [it for it in short[i : i + 10] if not it.get("quantity_defined")]
            if not chunk:
                continue
            payload = [
                {
                    "record_key": it["nb_id"],
                    "title": it.get("title") or "",
                    "text": it.get("document_text") or "",
                }
                for it in chunk
            ]
            print(
                f"002P batch {i//10+1} n={len(payload)} add_spent={oa['spent_usd']:.4f}",
                flush=True,
            )
            res = analyze_quantity_batch(
                db, user_id=None, items=payload, job_spent_usd=job_spent
            )
            if res.get("budget_denied"):
                oa["budget_denied"] += 1
                for later in short[i:] + long:
                    later["_openai"] = {"budget_denied": True, "error": res.get("error")}
                stop = True
                break
            if res.get("ok"):
                if res.get("paid"):
                    oa["successful_requests"] += 1
                    job_spent += Decimal(str(res.get("estimated_cost_usd") or 0))
                    oa["input_tokens"] += int(res.get("input_tokens") or 0)
                    oa["output_tokens"] += int(res.get("output_tokens") or 0)
                    oa["batched_records"] += int(res.get("batched") or 0)
                by_key = res.get("by_key") or {}
                for it in chunk:
                    meta = by_key.get(it["nb_id"]) or {}
                    if meta.get("cached"):
                        oa["cache_hits"] += 1
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
                            oa["applied_firm"] += 1
                        elif it.get("_openai_qty", {}).get("applied") and sem in EST:
                            oa["applied_estimated"] += 1
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
                            "budget_denied",
                            "error",
                        )
                        if meta.get(k) is not None
                    }
                    oa["per_opportunity"].append({"nb_id": it["nb_id"], **(it.get("_openai") or {})})
            else:
                oa["failed_requests"] += 1
            oa["spent_usd"] = float(additional_spent())
            if (i // 10) % 5 == 0:
                _write(
                    ROOT / "data" / "acceptance" / "build002p_openai_checkpoint.json",
                    {
                        "at": _utc(),
                        "progress_short": i + len(chunk),
                        "spent_usd": oa["spent_usd"],
                        "stats": {k: v for k, v in oa.items() if k != "per_opportunity"},
                    },
                )

        # Long singles
        if not stop:
            for i, it in enumerate(long):
                if it.get("quantity_defined"):
                    continue
                assert_nextbid_locked()
                if not budget_ok():
                    oa["budget_denied"] += 1
                    for later in long[i:]:
                        later["_openai"] = {
                            "budget_denied": True,
                            "error": "additional_$1_exhausted",
                        }
                    print(f"002P budget_stop long@{i} spent={oa['spent_usd']}", flush=True)
                    break
                # Section filter — never 500-char summary only when fuller text exists
                extract_relevant_sections(
                    it.get("document_text") or "",
                    title=it.get("title") or "",
                    max_chars=24000,
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
                    oa["budget_denied"] += 1
                    it["_openai"] = {"budget_denied": True, "error": res.get("error")}
                    for later in long[i + 1 :]:
                        later["_openai"] = {
                            "budget_denied": True,
                            "error": "budget_exhausted_skip",
                        }
                    break
                if res.get("cached"):
                    oa["cache_hits"] += 1
                if res.get("ok"):
                    if res.get("paid"):
                        oa["successful_requests"] += 1
                        job_spent += Decimal(str(res.get("estimated_cost_usd") or 0))
                        oa["input_tokens"] += int(res.get("input_tokens") or 0)
                        oa["output_tokens"] += int(res.get("output_tokens") or 0)
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
                            oa["applied_firm"] += 1
                        elif it.get("_openai_qty", {}).get("applied") and sem in EST:
                            oa["applied_estimated"] += 1
                    it["_openai"] = {
                        "ok": True,
                        "cached": bool(res.get("cached")),
                        "paid": bool(res.get("paid")),
                        "estimated_cost_usd": res.get("estimated_cost_usd"),
                        "input_tokens": res.get("input_tokens"),
                        "output_tokens": res.get("output_tokens"),
                        "text_chars_sent": res.get("text_chars_sent"),
                        "source_chars": res.get("source_chars") or it.get("document_text_len"),
                        "section_method": res.get("section_method"),
                        "response_id": res.get("response_id"),
                        "web_search_calls": 0,
                    }
                else:
                    oa["failed_requests"] += 1
                    it["_openai"] = {"ok": False, "error": res.get("error")}
                oa["per_opportunity"].append({"nb_id": it["nb_id"], **(it.get("_openai") or {})})
                oa["spent_usd"] = float(additional_spent())
                if (i + 1) % 25 == 0:
                    print(
                        f"002P long {i+1}/{len(long)} spent={oa['spent_usd']:.4f} "
                        f"firm={oa['applied_firm']} cache={oa['cache_hits']}",
                        flush=True,
                    )
                    _write(
                        ROOT / "data" / "acceptance" / "build002p_openai_checkpoint.json",
                        {
                            "at": _utc(),
                            "progress_long": i + 1,
                            "spent_usd": oa["spent_usd"],
                            "stats": {k: v for k, v in oa.items() if k != "per_opportunity"},
                        },
                    )

        oa["spent_usd"] = float(additional_spent())
        acceptance["openai_execution"] = {k: v for k, v in oa.items() if k != "per_opportunity"}
        _write(
            ROOT / "data" / "acceptance" / "build002p_openai_per_opportunity.json",
            {"build": BUILD, "rows": oa["per_opportunity"]},
        )
        print(f"002P openai_done spent={oa['spent_usd']:.4f} {acceptance['openai_execution']}", flush=True)

        # Final accounting for ALL deferred + reconcile prior Stage C
        individual = []
        queues: dict[str, list] = {
            "verified_procurement_fits": [],
            "promising_awaiting_verification": [],
            "missing_document": [],
            "restricted_document": [],
            "estimated_quantity": [],
            "confirmed_disqualification": [],
            "confirmed_indefinite": [],
            "archived_or_closed": [],
            "budget_denied_unresolved": [],
        }
        registry_payloads = []
        new_stage_c = []
        recovered = 0
        disp_counts = Counter()

        for it in work:
            pipe = it.get("procurement_pipeline") or evaluate_procurement_pipeline(
                it, document_text=it.get("document_text") or ""
            )
            # Closed/cancelled status → archive queue
            status = (it.get("solicitation_status") or "").lower()
            closed = status in {"closed", "cancelled", "historical_award", "closed_by_deadline"}
            oa_meta = it.get("_openai") or {}
            disp = disposition(
                pipe,
                text_len=int(it.get("document_text_len") or 0),
                package_status=it.get("package_status"),
                openai_meta=oa_meta,
                deadline_closed=closed,
            )
            disp_counts[disp] += 1
            if it.get("quantity") not in (None, "", "unknown"):
                recovered += 1
            row = {
                "nb_id": it.get("nb_id"),
                "title": (it.get("title") or "")[:200],
                "solicitation_number": it.get("solicitation_number"),
                "official_url": it.get("official_url"),
                "disposition": disp,
                "queue": disp
                if disp
                in queues
                else (
                    "promising_awaiting_verification"
                    if disp == "promising_awaiting_verification"
                    else disp
                ),
                "stage": pipe.get("stage"),
                "quantity": it.get("quantity"),
                "quantity_semantic": it.get("quantity_semantic"),
                "quantity_kind": it.get("quantity_kind"),
                "quantity_defined": bool(pipe.get("quantity_defined")),
                "quantity_evidence": (it.get("quantity_evidence") or [])[:8],
                "unknown_flags": pipe.get("unknown_flags") or [],
                "document_text_len": it.get("document_text_len"),
                "package_status": it.get("package_status"),
                "package_downloaded_count": it.get("package_downloaded_count"),
                "openai": oa_meta,
                "requirement_class": it.get("requirement_class"),
            }
            # Map disposition to queue key
            qkey = {
                "verified_procurement_fit": "verified_procurement_fits",
                "promising_awaiting_verification": "promising_awaiting_verification",
                "missing_document": "missing_document",
                "restricted_document": "restricted_document",
                "estimated_quantity": "estimated_quantity",
                "confirmed_disqualification": "confirmed_disqualification",
                "confirmed_indefinite": "confirmed_indefinite",
                "archived_or_closed": "archived_or_closed",
                "budget_denied_unresolved": "budget_denied_unresolved",
            }.get(disp, "promising_awaiting_verification")
            row["queue"] = qkey
            queues[qkey].append(row)
            individual.append(row)

            is_fit = pipe.get("stage") == STAGE_C and pipe.get("quantity_defined")
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
                    "procurement_stage": STAGE_C if is_fit else (pipe.get("stage") or STAGE_B),
                    "quantity": it.get("quantity") if is_fit or it.get("quantity") else None,
                    "quantity_unit": it.get("quantity_unit"),
                    "quantity_semantic": it.get("quantity_semantic"),
                    "quantity_evidence": it.get("quantity_evidence"),
                    "package_status": it.get("package_status"),
                    "document_text_len": it.get("document_text_len"),
                    "build_002p_disposition": disp,
                    "owner_queue": qkey,
                    "active_queue": qkey
                    in {
                        "verified_procurement_fits",
                        "promising_awaiting_verification",
                        "estimated_quantity",
                    },
                    "archived": qkey in {"archived_or_closed", "confirmed_disqualification"},
                }
            )
            if is_fit:
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

        print(f"002P registry_persist n={len(registry_payloads)}", flush=True)
        acceptance["registry_persist"] = ingest_population(registry_payloads, actor="build_002p")

        # Reconcile ALL Stage C uniquely from registry (prior + new)
        recs = load_records()
        stage_c_by_nb: dict[str, dict] = {}
        for r in recs.values():
            if (r.get("procurement_stage") or r.get("stage")) != STAGE_C:
                continue
            nb = str(r.get("nb_id") or r.get("provider_id") or "")
            if not nb or not r.get("quantity"):
                continue
            # Prefer richer evidence
            prev = stage_c_by_nb.get(nb)
            if not prev or len(str(r.get("quantity_evidence") or "")) >= len(
                str(prev.get("quantity_evidence") or "")
            ):
                stage_c_by_nb[nb] = r

        unique_stage_c = []
        for nb, r in sorted(stage_c_by_nb.items()):
            unique_stage_c.append(
                {
                    "nb_id": nb,
                    "title": (r.get("title") or "")[:200],
                    "quantity": r.get("quantity"),
                    "quantity_semantic": r.get("quantity_semantic"),
                    "quantity_evidence": r.get("quantity_evidence"),
                    "official_url": r.get("official_url"),
                    "solicitation_number": r.get("solicitation_number"),
                    "package_status": r.get("package_status"),
                    "requirement_class": r.get("requirement_class"),
                }
            )

        # Merge prior Stage C into verified queue if not already in individual
        seen_fit = {r["nb_id"] for r in queues["verified_procurement_fits"]}
        for fit in unique_stage_c:
            if fit["nb_id"] not in seen_fit:
                queues["verified_procurement_fits"].append(
                    {**fit, "disposition": "verified_procurement_fit", "queue": "verified_procurement_fits", "retained_prior": True}
                )

        # Dedupe verified queue by nb_id
        deduped = {}
        for r in queues["verified_procurement_fits"]:
            deduped[str(r.get("nb_id"))] = r
        queues["verified_procurement_fits"] = list(deduped.values())

        # Write queue artifacts for software access
        queues_dir = ROOT / "data" / "procurement_queues"
        queues_dir.mkdir(parents=True, exist_ok=True)
        queue_summary = {}
        for qname, rows in queues.items():
            _write(queues_dir / f"{qname}.json", {"queue": qname, "count": len(rows), "rows": rows})
            queue_summary[qname] = len(rows)
        _write(
            queues_dir / "index.json",
            {
                "build": BUILD,
                "updated_at": _utc(),
                "queues": queue_summary,
                "note": "Unresolved never listed as verified_procurement_fits",
            },
        )
        acceptance["queue_sync_db"] = sync_queues_to_db(queues)

        # Document accessibility sample check
        from m3.models import PackageDocument, SolicitationPackage

        pkg_n = db.query(func.count(SolicitationPackage.id)).scalar() or 0
        doc_n = db.query(func.count(PackageDocument.id)).scalar() or 0
        with_excerpt = (
            db.query(func.count(PackageDocument.id))
            .filter(PackageDocument.extracted_excerpt.isnot(None))
            .filter(PackageDocument.extracted_excerpt != "")
            .scalar()
            or 0
        )

        # API/UI verification — import app health + queue index readable
        ui_verify = {"queue_index_exists": (queues_dir / "index.json").exists()}
        try:
            from app import app as fastapi_app

            ui_verify["app_import_ok"] = True
            ui_verify["routes_sample"] = [
                getattr(r, "path", None)
                for r in fastapi_app.routes
                if getattr(r, "path", "").startswith(("/healthz", "/opportunities", "/api/m3"))
            ][:20]
        except Exception as exc:
            ui_verify["app_import_ok"] = False
            ui_verify["error"] = f"{type(exc).__name__}: {exc}"[:200]

        lock1 = assert_nextbid_locked()
        acceptance.update(
            {
                "finished_at": _utc(),
                "elapsed_sec": round(time.time() - t0, 1),
                "nextbid_lock_final": lock1,
                "metered_nextbid_calls": 0,
                "openai_snapshot_end": spending_snapshot(db),
                "openai_additional_spent_usd": oa["spent_usd"],
                "totals": {
                    "deferred_investigated": len(work),
                    "openai_eligible_targeted": len(openai_targets),
                    "quantities_recovered": recovered,
                    "newly_verified_quantities": sum(
                        1
                        for r in individual
                        if r.get("quantity") and (r.get("openai") or {}).get("ok")
                    ),
                    "new_stage_c_fits": len(new_stage_c),
                    "unique_verified_stage_c": len(unique_stage_c),
                    "dispositions": dict(disp_counts),
                    "queues": queue_summary,
                    "complete_packages": sum(
                        1 for r in individual if r.get("package_status") == "COMPLETE"
                    ),
                    "partial_packages": sum(
                        1 for r in individual if r.get("package_status") == "PARTIAL"
                    ),
                    "restricted_packages": sum(
                        1 for r in individual if r.get("package_status") == "ACCESS_RESTRICTED"
                    ),
                    "remaining_unresolved": sum(
                        queue_summary.get(k, 0)
                        for k in (
                            "promising_awaiting_verification",
                            "missing_document",
                            "restricted_document",
                            "estimated_quantity",
                            "budget_denied_unresolved",
                        )
                    ),
                },
                "new_stage_c": new_stage_c,
                "unique_stage_c": unique_stage_c,
                "document_accessibility": {
                    "packages": pkg_n,
                    "documents": doc_n,
                    "with_extracted_excerpt": with_excerpt,
                },
                "ui_api_verification": ui_verify,
            }
        )

        out = ROOT / "data" / "acceptance"
        _write(out / "build002p_acceptance.json", acceptance)
        _write(out / "build002p_individual_accounting.json", {"build": BUILD, "rows": individual})
        _write(
            out / "build002p_stage_c_evidence.json",
            {
                "build": BUILD,
                "new_count": len(new_stage_c),
                "unique_count": len(unique_stage_c),
                "new_opportunities": new_stage_c,
                "unique_opportunities": unique_stage_c,
            },
        )

        write_reports(acceptance)
        # Prepare deploy marker (do not deploy)
        _write(
            out / "build002p_deploy_ready.json",
            {
                "build": BUILD,
                "build_id": "20261122-m3-2-build-002p",
                "ready_for_railway": True,
                "deployed": False,
                "awaits_owner_acceptance": True,
                "nextbid_locked": True,
                "prepared_at": _utc(),
            },
        )
        print(
            f"002P DONE spent={oa['spent_usd']:.4f} new_c={len(new_stage_c)} "
            f"unique_c={len(unique_stage_c)} unresolved={acceptance['totals']['remaining_unresolved']} "
            f"metered_nb=0",
            flush=True,
        )
        return acceptance
    finally:
        # ALWAYS restore normal ceiling
        try:
            if original_policy is not None:
                restored = restore_normal_ceiling(db, original_policy)
                acceptance["ceiling_restored"] = restored
                print(f"002P ceiling_restored {restored}", flush=True)
                # Persist restoration onto acceptance if already written
                try:
                    prev = _load(ROOT / "data" / "acceptance" / "build002p_acceptance.json") or {}
                    prev["ceiling_restored"] = restored
                    _write(ROOT / "data" / "acceptance" / "build002p_acceptance.json", prev)
                except Exception:
                    pass
        except Exception as exc:
            print(f"002P CRITICAL ceiling_restore_failed {exc}", flush=True)
        try:
            assert_nextbid_locked()
        except Exception as exc:
            print(f"002P lock_warn {exc}", flush=True)
        db.close()


def write_reports(acceptance: dict) -> None:
    docs = ROOT / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    t = acceptance.get("totals") or {}
    oa = acceptance.get("openai_execution") or {}
    q = t.get("queues") or {}
    report = f"""# BUILD 002P — Complete Procurement Backlog

**Status:** AWAITING OWNER ACCEPTANCE (before Railway deploy)  
**Build:** {BUILD}  
**Started:** {acceptance.get("started_at")}  
**Finished:** {acceptance.get("finished_at")}  
**Elapsed sec:** {acceptance.get("elapsed_sec")}  
**Metered NextBid:** {acceptance.get("metered_nextbid_calls")} (LOCKED)  
**Web search used:** {acceptance.get("web_search_used")}  
**Build 003:** not started  

## OpenAI one-time authorization

- Max additional authorized: **$1.00**
- Actual additional spent: **${acceptance.get("openai_additional_spent_usd")}**
- Successful requests: `{oa.get("successful_requests")}`
- Cache hits: `{oa.get("cache_hits")}`
- Batched records: `{oa.get("batched_records")}`
- Applied firm / estimated: `{oa.get("applied_firm")}` / `{oa.get("applied_estimated")}`
- Tokens in/out: `{oa.get("input_tokens")}` / `{oa.get("output_tokens")}`
- Budget denied stops: `{oa.get("budget_denied")}`
- Ceiling restored: `{json.dumps(acceptance.get("ceiling_restored"), default=str)}`

## Results

| Metric | Count |
|--------|------:|
| Deferred investigated | {t.get("deferred_investigated")} |
| OpenAI-eligible targeted | {t.get("openai_eligible_targeted")} |
| Quantities recovered | {t.get("quantities_recovered")} |
| New Stage C fits | {t.get("new_stage_c_fits")} |
| Unique verified Stage C | {t.get("unique_verified_stage_c")} |
| Remaining unresolved | {t.get("remaining_unresolved")} |
| Complete packages | {t.get("complete_packages")} |
| Partial packages | {t.get("partial_packages")} |
| Restricted packages | {t.get("restricted_packages")} |

## Queues (`data/procurement_queues/`)

| Queue | Count |
|-------|------:|
| Verified fits | {q.get("verified_procurement_fits", 0)} |
| Promising awaiting verification | {q.get("promising_awaiting_verification", 0)} |
| Missing document | {q.get("missing_document", 0)} |
| Restricted document | {q.get("restricted_document", 0)} |
| Estimated quantity | {q.get("estimated_quantity", 0)} |
| Confirmed DQ | {q.get("confirmed_disqualification", 0)} |
| Indefinite | {q.get("confirmed_indefinite", 0)} |
| Archived/closed | {q.get("archived_or_closed", 0)} |
| Budget denied unresolved | {q.get("budget_denied_unresolved", 0)} |

Unresolved opportunities are **not** listed as verified fits.

## New Stage C (this build)

"""
    for fit in (acceptance.get("new_stage_c") or [])[:60]:
        report += (
            f"- `{fit.get('nb_id')}` qty={fit.get('quantity')} "
            f"sem={fit.get('quantity_semantic')} — {(fit.get('title') or '')[:90]}\n"
        )
    if not acceptance.get("new_stage_c"):
        report += "- _(none)_\n"
    report += f"""
## Unique Stage C total: {t.get("unique_verified_stage_c")}

Deploy ready marker: `data/acceptance/build002p_deploy_ready.json` (deployed=false).  
Await owner acceptance before Railway deployment. NextBid remains locked.
"""
    (docs / "BUILD_002P_REPORT.md").write_text(report, encoding="utf-8")
    _write(
        docs / "BUILD_002P_ACCEPTANCE.json",
        {
            "build": BUILD,
            "status": "AWAITING_OWNER_ACCEPTANCE",
            "metered_nextbid_calls": 0,
            "web_search_used": False,
            "openai_additional_spent_usd": acceptance.get("openai_additional_spent_usd"),
            "max_additional_authorized_usd": 1.0,
            "ceiling_restored": acceptance.get("ceiling_restored"),
            "totals": t,
            "openai_execution": oa,
            "railway_deployed": False,
            "build_003_started": False,
            "finished_at": acceptance.get("finished_at"),
        },
    )


if __name__ == "__main__":
    main()
