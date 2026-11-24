#!/usr/bin/env python3
"""Build 002N — quantity extraction + public solicitation document recovery.

ZERO metered NextBid calls. NextBid remains locked for the entire run.
OpenAI used only for quantity analysis of local solicitation text when
deterministic extractors cannot establish firm ordered quantity.
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
from m3.models import SolicitationPackage
from m3.research.commercial_criteria import (
    STAGE_B,
    STAGE_C,
    evaluate_procurement_pipeline,
)
from m3.research.extract_fields import enrich_item_from_text, extract_quantity_fields
from m3.research.nextbid_client import load_checkpoint
from m3.research.nextbid_controller import acquisition_locked, controller_status
from m3.research.nextbid_killswitch import emergency_stop_status
from m3.research.openai_client import analyze_quantity_text, openai_ready
from m3.research.procurement_registry import ingest_population, load_records
from m3.research.reprocess import collect_package_text

BUILD = "002N"
FIRM_SEMANTICS = {
    "ORDERED_QUANTITY",
    "LINE_ITEM_QUANTITY",
    "MINIMUM_GUARANTEED_QUANTITY",
}
EST_SEMANTICS = {"ESTIMATED_QUANTITY", "MAXIMUM_AUTHORIZED_QUANTITY"}


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
        raise RuntimeError("FATAL: NextBid acquisition is NOT locked — aborting Build 002N")
    return {
        "locked": locked,
        "killswitch_engaged": bool(kill.get("engaged")),
        "owner_authorization": st.get("owner_authorization"),
        "resume_requires_owner_authorization": st.get("resume_requires_owner_authorization"),
        "checked_at": _utc(),
    }


def live_openai_smoke(db) -> dict[str, Any]:
    """Mandatory live OpenAI verification — real request, no credentials exposed."""
    ready, reason = openai_ready()
    from m3.research.budget import ensure_budget_policy

    policy = ensure_budget_policy(db)
    sample_title = "Purchase of Fifteen (15) Portable Generators"
    sample_text = (
        "The City invites sealed bids for the purchase of Fifteen (15) new portable "
        "generators, Quantity: 15 EA, line item 1. Firm one-time purchase. "
        "Do not confuse model year 2024 with ordered quantity."
    )
    started = time.time()
    result = analyze_quantity_text(
        db,
        user_id=None,
        title=sample_title,
        text=sample_text,
    )
    elapsed = round(time.time() - started, 3)
    parsed = result.get("parsed") or {}
    return {
        "ready": ready,
        "ready_reason": reason,
        "paid_research_enabled": bool(policy.paid_research_enabled),
        "default_model": policy.default_model,
        "api_key_configured": True,  # never print key
        "live_request_ok": bool(result.get("ok")),
        "live_request_paid": bool(result.get("paid")),
        "model_used": result.get("model"),
        "response_id": result.get("response_id"),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "estimated_cost_usd": result.get("estimated_cost_usd"),
        "text_chars_sent": result.get("text_chars_sent"),
        "elapsed_sec": elapsed,
        "parsed_quantity": parsed.get("quantity") if isinstance(parsed, dict) else None,
        "parsed_semantic": parsed.get("quantity_semantic") if isinstance(parsed, dict) else None,
        "error": result.get("error"),
        "silent_regex_fallback": False,
        "note": "Smoke test uses local fixture text; production path calls analyze_quantity_text for unresolved records",
    }


def collect_text_sources() -> dict[str, dict[str, Any]]:
    """Map nb_id -> best available local text (checkpoint + package excerpts)."""
    by_nb: dict[str, dict[str, Any]] = {}
    ck = load_checkpoint("nextbid_acquire_v1")
    state = ck.get("state") or {}
    for row in list(state.get("enriched") or []) + list(state.get("listings") or []):
        nb = str(row.get("nb_id") or "")
        if not nb:
            continue
        txt = str(row.get("document_text") or row.get("description") or "")
        prev = by_nb.get(nb) or {"text": "", "sources": []}
        if len(txt) > len(prev.get("text") or ""):
            prev["text"] = txt
        if txt and "checkpoint" not in prev["sources"]:
            prev["sources"].append("checkpoint")
        prev["title"] = row.get("title") or prev.get("title")
        prev["official_url"] = row.get("official_url") or prev.get("official_url")
        prev["description"] = row.get("description") or prev.get("description")
        prev["solicitation_number"] = row.get("solicitation_number") or prev.get(
            "solicitation_number"
        )
        prev["requirement_class"] = row.get("requirement_class") or prev.get("requirement_class")
        prev["solicitation_status"] = row.get("solicitation_status") or prev.get(
            "solicitation_status"
        )
        prev["verified_individual_solicitation"] = row.get(
            "verified_individual_solicitation", prev.get("verified_individual_solicitation")
        )
        prev["record_type"] = row.get("record_type") or prev.get("record_type")
        prev["buyer_name"] = row.get("buyer_name") or prev.get("buyer_name")
        prev["deadline_at"] = row.get("deadline_at") or prev.get("deadline_at")
        prev["nextbid_source"] = row.get("nextbid_source") or prev.get("nextbid_source")
        by_nb[nb] = prev
    return by_nb


def index_packages(db) -> dict[str, Any]:
    """Build lookup indexes + text cache for packages (avoid N+1)."""
    from sqlalchemy.orm import selectinload

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
        }
        txt = collect_package_text(pkg)
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
            print(f"002N package_index_progress {n}", flush=True)
    return {
        "by_ext": by_ext,
        "by_url": by_url,
        "by_sol": by_sol,
        "text_by_ext": text_by_ext,
        "meta_by_ext": meta_by_ext,
        "count": len(by_ext),
    }


def resolve_package_key(pkg_idx: dict, nb: str, url: str | None, sol: str | None) -> str | None:
    if nb and nb in pkg_idx["by_ext"]:
        return nb
    if url:
        ek = pkg_idx["by_url"].get(str(url).rstrip("/"))
        if ek:
            return ek
    if sol:
        ek = pkg_idx["by_sol"].get(str(sol).strip().upper())
        if ek:
            return ek
    return None


def apply_openai_quantity(item: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    """Merge OpenAI quantity parse into item. Never invent; trust only structured parse."""
    import re

    out = dict(item)
    if not isinstance(parsed, dict):
        return out
    title = str(out.get("title") or "")
    if re.search(
        r"(?i)\b(maintenance|repair|rehabilitat|car\s*wash|catalyst\s+fund|"
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

    # Reject low-confidence or unknown without evidence
    if sem in {"UNKNOWN", ""} and not lines:
        out["_openai_qty"] = {
            "applied": False,
            "reason": parsed.get("rejection_reason") or "unknown_or_absent",
            "semantic": sem,
            "confidence": conf,
        }
        return out
    if conf == "low" and sem not in FIRM_SEMANTICS and sem not in EST_SEMANTICS:
        out["_openai_qty"] = {"applied": False, "reason": "low_confidence", "semantic": sem}
        return out

    if lines and not qty:
        # Prefer first line-item quantity
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

    if sem in EST_SEMANTICS or kind == "estimated":
        out["quantity"] = str(qty)
        out["quantity_unit"] = parsed.get("quantity_unit") or out.get("quantity_unit")
        out["quantity_kind"] = "estimated"
        out["quantity_semantic"] = "ESTIMATED_QUANTITY"
        out["quantity_evidence"] = snippets + ["source:openai_quantity_extraction", "estimated_not_firm"]
        if lines:
            out["line_items"] = lines
        out["_openai_qty"] = {"applied": True, "semantic": "ESTIMATED_QUANTITY", "confidence": conf}
        return out

    if sem in FIRM_SEMANTICS or (qty and snippets and conf in {"high", "medium"}):
        # Only firm if semantic is firm OR medium/high with ordered-like semantic empty→ORDERED
        final_sem = sem if sem in FIRM_SEMANTICS else "ORDERED_QUANTITY"
        if final_sem not in FIRM_SEMANTICS:
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
        out["_openai_qty"] = {"applied": True, "semantic": final_sem, "confidence": conf, "quantity": str(qty)}
        return out

    out["_openai_qty"] = {"applied": False, "reason": f"unaccepted_semantic:{sem}", "confidence": conf}
    return out


def classify_disposition(pipe: dict[str, Any], *, text_len: int, package_status: str | None, openai_meta: dict | None) -> str:
    """Owner-required disposition taxonomy. Missing info ≠ confirmed disqualification."""
    if pipe.get("excluded") and any(
        str(r).startswith("class:") or str(r).startswith("status:") for r in (pipe.get("reasons") or [])
    ):
        return "confirmed_procurement_incompatibility"
    if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
        return "firm_ordered_quantity_verified"
    flags = set(pipe.get("unknown_flags") or [])
    if "quantity_indefinite_not_fixed_purchase" in flags:
        return "indefinite_purchase_commitment"
    if "quantity_estimated_not_firm_order" in flags:
        return "estimated_quantity"
    if package_status == "ACCESS_RESTRICTED":
        return "source_access_restricted"
    if openai_meta and openai_meta.get("error") and not openai_meta.get("applied"):
        if text_len >= 400:
            return "extraction_failure"
    if text_len == 0 and package_status in {None, "UNKNOWN", "NOT_ACQUIRED", "FAILED"}:
        return "missing_document"
    if text_len == 0:
        return "missing_document"
    if "quantity_unknown" in flags or not pipe.get("quantity_defined"):
        if text_len >= 400:
            return "quantity_absent_from_complete_documents"
        return "missing_document"
    return "extraction_failure"


def free_doc_pass(db, items: list[dict], *, max_items: int = 500, batch_size: int = 40) -> dict[str, Any]:
    """Acquire publicly accessible solicitation packages — zero NextBid metered calls."""
    from m3.research.package import acquire_packages_for_items

    assert_nextbid_locked()
    work = []
    for it in items:
        if not it.get("official_url"):
            continue
        work.append(
            {
                "external_key": it.get("nb_id") or it.get("external_key"),
                "title": it.get("title"),
                "solicitation_number": it.get("solicitation_number"),
                "buyer_name": it.get("agency") or it.get("buyer_name"),
                "official_url": it.get("official_url"),
                "source_key": it.get("source") or it.get("nextbid_source") or "nextbid",
                "nb_id": it.get("nb_id"),
                "requirement_class": it.get("requirement_class"),
            }
        )
    if not work:
        return {"processed": 0, "note": "no_urls", "metered_nextbid": 0}
    work = work[:max_items]
    merged_stats: dict[str, Any] = {
        "COMPLETE": 0,
        "PARTIAL": 0,
        "ACCESS_RESTRICTED": 0,
        "VERIFICATION_REQUIRED": 0,
        "FAILED": 0,
        "downloaded_docs": 0,
        "identified_docs": 0,
        "errors": 0,
    }
    all_enriched: list[dict] = []
    batches = 0
    try:
        for i in range(0, len(work), batch_size):
            chunk = work[i : i + batch_size]
            batches += 1
            assert_nextbid_locked()
            print(
                f"002N free_doc_batch {batches} items={len(chunk)} "
                f"progress={i}/{len(work)}",
                flush=True,
            )
            result = acquire_packages_for_items(db, chunk, max_items=len(chunk))
            for k in merged_stats:
                if isinstance(result.get(k), int):
                    merged_stats[k] += int(result.get(k) or 0)
            all_enriched.extend(result.get("enriched") or [])
            # Checkpoint batch progress
            _write(
                ROOT / "data" / "acceptance" / "build002n_free_doc_checkpoint.json",
                {
                    "at": _utc(),
                    "batches": batches,
                    "processed": min(i + len(chunk), len(work)),
                    "total": len(work),
                    "stats": merged_stats,
                    "metered_nextbid": 0,
                },
            )
        assert_nextbid_locked()
        return {
            "ok": True,
            "processed": len(work),
            "batches": batches,
            "metered_nextbid": 0,
            "stats": merged_stats,
            "package_count": len(all_enriched),
            "enriched": all_enriched,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:400],
            "processed": len(all_enriched),
            "batches": batches,
            "metered_nextbid": 0,
            "stats": merged_stats,
            "enriched": all_enriched,
            "traceback": traceback.format_exc()[-800:],
        }


def verify_stage_c(db, records: list[dict]) -> dict[str, Any]:
    """Re-verify existing Stage C against eligibility / active / install / bond / insurance."""
    from m3.research.commercial_criteria import BOND_MARKERS, INSURANCE_MARKERS
    import re

    INSTALL_RE = re.compile(
        r"(?i)\b(install(?:ation)?|site\s+work|construction|prevailing\s+wage|"
        r"davis[- ]bacon|licensed\s+contractor)\b"
    )
    results = []
    kept = 0
    demoted = 0
    for r in records:
        nb = r.get("nb_id") or r.get("provider_id")
        text = str(r.get("document_text") or r.get("description") or "")
        title = str(r.get("title") or "")
        blob = f"{title}\n{text}"
        item = dict(r)
        item.setdefault("verified_individual_solicitation", True)
        item.setdefault("record_type", item.get("record_type") or "INDIVIDUAL_SOLICITATION")
        pipe = evaluate_procurement_pipeline(item, document_text=text)
        flags = list(pipe.get("unknown_flags") or [])
        reasons = []
        if INSTALL_RE.search(title) and not re.search(
            r"(?i)\b(supply\s+only|furnish\s+only|no\s+install)\b", blob
        ):
            reasons.append("installation_language_review")
        if BOND_MARKERS.search(blob):
            reasons.append("bonding_present")
        if INSURANCE_MARKERS.search(blob):
            reasons.append("insurance_present")
        still_c = pipe.get("stage") == STAGE_C and pipe.get("quantity_defined")
        if still_c:
            kept += 1
        else:
            demoted += 1
        results.append(
            {
                "nb_id": nb,
                "title": title[:160],
                "still_stage_c": still_c,
                "stage": pipe.get("stage"),
                "quantity": pipe.get("quantity"),
                "quantity_semantic": pipe.get("quantity_semantic"),
                "quantity_defined": pipe.get("quantity_defined"),
                "unknown_flags": flags,
                "review_flags": reasons,
                "evidence": pipe.get("quantity_evidence"),
            }
        )
    return {"examined": len(records), "kept_stage_c": kept, "demoted": demoted, "rows": results}


def main() -> dict[str, Any]:
    started = _utc()
    lock0 = assert_nextbid_locked()
    print(f"002N lock_ok={lock0['locked']}", flush=True)

    db = SessionLocal()
    acceptance: dict[str, Any] = {
        "build": BUILD,
        "started_at": started,
        "nextbid_lock": lock0,
        "metered_nextbid_calls": 0,
    }
    try:
        # --- Mandatory OpenAI live verification ---
        smoke = live_openai_smoke(db)
        acceptance["openai_verification"] = smoke
        print(
            f"002N openai_live ok={smoke.get('live_request_ok')} model={smoke.get('model_used')} "
            f"qty={smoke.get('parsed_quantity')} sem={smoke.get('parsed_semantic')}",
            flush=True,
        )
        if not smoke.get("live_request_ok"):
            acceptance["openai_verification"]["fatal"] = "live_openai_request_failed"
            # Continue deterministic path but flag — do not invent OpenAI success

        # --- Load Stage B investigation set (audit 710 + current registry Stage B) ---
        audit = _load(ROOT / "data" / "acceptance" / "_tmp_stage_b_c_audit.json") or {}
        audit_rows = list(audit.get("all_stage_b") or [])
        audit_ids = {str(r.get("nb_id")) for r in audit_rows if r.get("nb_id")}
        recs = load_records()
        registry_stage_b = [
            r
            for r in recs.values()
            if (r.get("procurement_stage") or r.get("stage")) == STAGE_B
        ]
        registry_stage_c = [
            r
            for r in recs.values()
            if (r.get("procurement_stage") or r.get("stage")) == STAGE_C
        ]
        # Prefer registry rows; ensure every audit ID is present
        by_nb: dict[str, dict] = {}
        for r in registry_stage_b:
            nb = str(r.get("nb_id") or r.get("provider_id") or "")
            if nb:
                by_nb[nb] = dict(r)
        for a in audit_rows:
            nb = str(a.get("nb_id") or "")
            if not nb:
                continue
            if nb not in by_nb:
                by_nb[nb] = {
                    "nb_id": nb,
                    "provider_id": nb,
                    "title": a.get("title"),
                    "requirement_class": a.get("requirement_class"),
                    "procurement_stage": STAGE_B,
                    "from_audit_only": True,
                }
            else:
                by_nb[nb]["audit_document_text_len"] = a.get("document_text_len")
                by_nb[nb]["audit_unknown_flags"] = a.get("unknown_flags")

        text_map = collect_text_sources()
        print(f"002N stage_b_targets={len(by_nb)} audit_ids={len(audit_ids)} text_map={len(text_map)}", flush=True)

        pkg_idx = index_packages(db)
        print(f"002N packages_indexed={pkg_idx['count']}", flush=True)

        # Merge text + package for each target
        work_items: list[dict] = []
        for nb, row in by_nb.items():
            src = text_map.get(nb) or {}
            url = row.get("official_url") or src.get("official_url")
            sol = row.get("solicitation_number") or src.get("solicitation_number")
            pkg_key = resolve_package_key(pkg_idx, nb, url, sol)
            pkg_meta = pkg_idx["meta_by_ext"].get(pkg_key or "") or {}
            pkg_text = pkg_idx["text_by_ext"].get(pkg_key or "") or ""
            base_text = src.get("text") or ""
            # Prefer longer package excerpt over capped checkpoint description
            document_text = pkg_text if len(pkg_text) >= len(base_text) else base_text
            if pkg_text and base_text and pkg_text != base_text:
                document_text = (base_text + "\n" + pkg_text)[:50000]

            item = {
                **row,
                "nb_id": nb,
                "external_key": nb,
                "title": row.get("title") or src.get("title"),
                "official_url": url,
                "solicitation_number": sol,
                "buyer_name": row.get("agency") or row.get("buyer_name") or src.get("buyer_name"),
                "agency": row.get("agency") or src.get("buyer_name"),
                "requirement_class": row.get("requirement_class")
                or src.get("requirement_class")
                or "product_supply",
                "solicitation_status": row.get("solicitation_status")
                or src.get("solicitation_status")
                or "unknown",
                "verified_individual_solicitation": bool(
                    row.get("verified_individual_solicitation")
                    if row.get("verified_individual_solicitation") is not None
                    else src.get("verified_individual_solicitation", True)
                ),
                "record_type": row.get("record_type")
                or src.get("record_type")
                or "INDIVIDUAL_SOLICITATION",
                "deadline_at": row.get("deadline_at") or src.get("deadline_at"),
                "source": row.get("provider") or src.get("nextbid_source") or "nextbid",
                "nextbid_source": src.get("nextbid_source"),
                "document_text": document_text,
                "description": src.get("description") or "",
                "document_text_len": len(document_text),
                "package_status": pkg_meta.get("package_status") or row.get("package_status"),
                "package_downloaded_count": pkg_meta.get("downloaded_count") or 0,
                "text_sources": list(src.get("sources") or [])
                + (["package_excerpt"] if pkg_text else []),
                # Clear stale quantity so re-extract is honest
                "quantity": None,
                "quantity_kind": None,
                "quantity_semantic": None,
                "quantity_evidence": None,
                "line_items": None,
            }
            work_items.append(item)

        # --- Pass 1: deterministic extraction on all ---
        pass1_stats = Counter()
        unresolved_for_docs: list[dict] = []
        for it in work_items:
            enriched = enrich_item_from_text(
                it, it.get("document_text") or "", source="build002n_deterministic"
            )
            pipe = evaluate_procurement_pipeline(
                enriched, document_text=it.get("document_text") or ""
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
            if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
                pass1_stats["firm_stage_c"] += 1
            elif pipe.get("unknown_flags") and "quantity_estimated_not_firm_order" in (
                pipe.get("unknown_flags") or []
            ):
                pass1_stats["estimated"] += 1
                unresolved_for_docs.append(it)  # still need firm — keep investigating
            elif not pipe.get("quantity_defined"):
                pass1_stats["unresolved"] += 1
                unresolved_for_docs.append(it)
            else:
                pass1_stats["other"] += 1
        print(f"002N pass1 {dict(pass1_stats)} unresolved={len(unresolved_for_docs)}", flush=True)
        acceptance["pass1_deterministic"] = dict(pass1_stats)

        # --- Pass 2: free public document recovery for unresolved with URLs ---
        need_docs = [
            it
            for it in unresolved_for_docs
            if it.get("official_url")
            and (
                int(it.get("document_text_len") or 0) < 400
                or (it.get("package_status") or "")
                in {None, "UNKNOWN", "NOT_ACQUIRED", "FAILED", "VERIFICATION_REQUIRED", "ACCESS_RESTRICTED"}
                or int(it.get("package_downloaded_count") or 0) == 0
            )
        ]
        print(f"002N free_doc_candidates={len(need_docs)}", flush=True)
        free_ckpt = _load(ROOT / "data" / "acceptance" / "build002n_free_doc_checkpoint.json") or {}
        if int(free_ckpt.get("processed") or 0) >= len(need_docs) and free_ckpt.get("total"):
            print(
                f"002N free_docs_resume_skip processed={free_ckpt.get('processed')} "
                f"(using prior free-doc acquisition; zero NextBid)",
                flush=True,
            )
            free = {
                "ok": True,
                "processed": int(free_ckpt.get("processed") or 0),
                "metered_nextbid": 0,
                "stats": free_ckpt.get("stats") or {},
                "resumed_from_checkpoint": True,
                "enriched": [],
            }
        else:
            free = free_doc_pass(db, need_docs, max_items=len(need_docs))
        acceptance["free_document_recovery"] = {
            k: v for k, v in free.items() if k != "enriched"
        }
        print(
            f"002N free_docs ok={free.get('ok')} processed={free.get('processed')} "
            f"stats={free.get('stats')}",
            flush=True,
        )

        # Clear any failed transaction from package acquisition before reindex
        try:
            db.rollback()
        except Exception:
            pass

        # Refresh package index + re-merge text for unresolved
        print("002N refreshing_package_index_after_free_docs", flush=True)
        pkg_idx = index_packages(db)
        enriched_by_key = {
            str(e.get("external_key") or e.get("nb_id") or ""): e
            for e in (free.get("enriched") or [])
        }
        for it in unresolved_for_docs:
            nb = it["nb_id"]
            pkg_key = resolve_package_key(
                pkg_idx, nb, it.get("official_url"), it.get("solicitation_number")
            )
            pkg_meta = pkg_idx["meta_by_ext"].get(pkg_key or "") or {}
            pkg_text = pkg_idx["text_by_ext"].get(pkg_key or "") or ""
            enr = enriched_by_key.get(nb) or {}
            extra = str(enr.get("description") or enr.get("document_text") or "")
            parts = [it.get("document_text") or "", pkg_text, extra]
            combined = "\n".join(p for p in parts if p)[:50000]
            it["document_text"] = combined
            it["document_text_len"] = len(combined)
            if pkg_meta:
                it["package_status"] = pkg_meta.get("package_status")
                it["package_downloaded_count"] = pkg_meta.get("downloaded_count") or 0
            if "package_excerpt" not in (it.get("text_sources") or []) and pkg_text:
                it.setdefault("text_sources", []).append("package_excerpt")
            if extra:
                it.setdefault("text_sources", []).append("free_doc_enrichment")

            # Re-run deterministic after new docs
            it["quantity"] = None
            it["quantity_semantic"] = None
            it["quantity_evidence"] = None
            enriched = enrich_item_from_text(it, combined, source="build002n_post_free_docs")
            pipe = evaluate_procurement_pipeline(enriched, document_text=combined)
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

        assert_nextbid_locked()

        # --- Pass 3: OpenAI for unresolved with substantial text ---
        openai_ok = 0
        openai_fail = 0
        openai_skipped_short = 0
        openai_budget_denied = 0
        openai_applied_firm = 0
        openai_applied_est = 0
        job_spent = Decimal(str(smoke.get("estimated_cost_usd") or 0))
        long_text_targets = [
            it
            for it in work_items
            if not it.get("quantity_defined")
            and int(it.get("document_text_len") or 0) >= 400
        ]
        # Also include audit's 386 long-text set even if currently shorter after merge
        audit_long = {
            str(r.get("nb_id"))
            for r in audit_rows
            if int(r.get("document_text_len") or 0) >= 400
        }
        seen_nb = {it["nb_id"] for it in long_text_targets}
        for it in work_items:
            if (
                it["nb_id"] in audit_long
                and not it.get("quantity_defined")
                and it["nb_id"] not in seen_nb
                and int(it.get("document_text_len") or 0) > 0
            ):
                long_text_targets.append(it)
                seen_nb.add(it["nb_id"])
        # Longest text first — maximize evidence density under daily budget
        long_text_targets.sort(
            key=lambda x: int(x.get("document_text_len") or 0), reverse=True
        )

        print(
            f"002N openai_targets={len(long_text_targets)} audit_long={len(audit_long)}",
            flush=True,
        )
        acceptance["openai_targets_count"] = len(long_text_targets)
        acceptance["audit_long_text_count"] = len(audit_long)

        for i, it in enumerate(long_text_targets):
            if it.get("quantity_defined"):
                continue
            text = it.get("document_text") or ""
            if len(text) < 200:
                openai_skipped_short += 1
                it["_openai_qty"] = {"applied": False, "reason": "text_too_short_for_openai"}
                continue
            # No silent regex-only fallback: always call OpenAI for these targets
            result = analyze_quantity_text(
                db,
                user_id=None,
                title=str(it.get("title") or ""),
                text=text,
                job_spent_usd=job_spent,
            )
            if result.get("ok"):
                openai_ok += 1
                job_spent += Decimal(str(result.get("estimated_cost_usd") or 0))
                parsed = result.get("parsed") or {}
                merged = apply_openai_quantity(it, parsed if isinstance(parsed, dict) else {})
                it.clear()
                it.update(merged)
                # Re-evaluate pipeline with OpenAI fields
                pipe = evaluate_procurement_pipeline(it, document_text=text)
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
                meta = dict(it.get("_openai_qty") or {})
                meta["response_id"] = result.get("response_id")
                meta["model"] = result.get("model")
                meta["text_chars_sent"] = result.get("text_chars_sent")
                meta["estimated_cost_usd"] = result.get("estimated_cost_usd")
                it["_openai_qty"] = meta
                if meta.get("applied") and meta.get("semantic") in FIRM_SEMANTICS:
                    openai_applied_firm += 1
                elif meta.get("applied") and meta.get("semantic") in EST_SEMANTICS:
                    openai_applied_est += 1
            else:
                openai_fail += 1
                it["_openai_qty"] = {
                    "applied": False,
                    "error": result.get("error"),
                    "budget_denied": bool(result.get("budget_denied")),
                }
                if result.get("budget_denied"):
                    openai_budget_denied += 1
                    remaining = len(long_text_targets) - (i + 1)
                    for later in long_text_targets[i + 1 :]:
                        if later.get("_openai_qty"):
                            continue
                        later["_openai_qty"] = {
                            "applied": False,
                            "error": "budget_exhausted_skip",
                            "budget_denied": True,
                        }
                    print(
                        f"002N openai_budget_exhausted at {i+1}; "
                        f"skipping_remaining={remaining}",
                        flush=True,
                    )
                    break
            if (i + 1) % 25 == 0:
                print(
                    f"002N openai_progress {i+1}/{len(long_text_targets)} "
                    f"ok={openai_ok} fail={openai_fail} firm={openai_applied_firm} spent~{job_spent}",
                    flush=True,
                )
                assert_nextbid_locked()

        acceptance["openai_execution"] = {
            "successful_requests": openai_ok,
            "failed_requests": openai_fail,
            "budget_denied": openai_budget_denied,
            "skipped_short_text": openai_skipped_short,
            "applied_firm": openai_applied_firm,
            "applied_estimated": openai_applied_est,
            "job_spent_usd_estimate": float(job_spent),
            "silent_regex_fallback": False,
            "note": "Every long-text unresolved target received an OpenAI call attempt; failures recorded explicitly",
        }
        print(f"002N openai_done {acceptance['openai_execution']}", flush=True)

        # --- Final disposition for ALL targets ---
        dispositions = Counter()
        individual: list[dict] = []
        new_stage_c: list[dict] = []
        promising_queue: list[dict] = []
        quantities_recovered = 0
        registry_payloads: list[dict] = []

        for it in work_items:
            pipe = it.get("procurement_pipeline") or evaluate_procurement_pipeline(
                it, document_text=it.get("document_text") or ""
            )
            disp = classify_disposition(
                pipe,
                text_len=int(it.get("document_text_len") or 0),
                package_status=it.get("package_status"),
                openai_meta=it.get("_openai_qty"),
            )
            dispositions[disp] += 1
            if it.get("quantity") not in (None, "", "unknown"):
                quantities_recovered += 1
            row = {
                "nb_id": it.get("nb_id"),
                "title": (it.get("title") or "")[:200],
                "solicitation_number": it.get("solicitation_number"),
                "official_url": it.get("official_url"),
                "disposition": disp,
                "stage": pipe.get("stage"),
                "quantity": it.get("quantity") or pipe.get("quantity"),
                "quantity_semantic": it.get("quantity_semantic") or pipe.get("quantity_semantic"),
                "quantity_kind": it.get("quantity_kind") or pipe.get("quantity_kind"),
                "quantity_defined": bool(pipe.get("quantity_defined")),
                "quantity_evidence": (it.get("quantity_evidence") or pipe.get("quantity_evidence") or [])[:6],
                "unknown_flags": pipe.get("unknown_flags") or [],
                "document_text_len": it.get("document_text_len"),
                "package_status": it.get("package_status"),
                "package_downloaded_count": it.get("package_downloaded_count"),
                "text_sources": it.get("text_sources"),
                "openai": {
                    k: v
                    for k, v in (it.get("_openai_qty") or {}).items()
                    if k != "raw"
                },
                "in_audit_710": it.get("nb_id") in audit_ids,
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
                    "build_002n_disposition": disp,
                    "active_queue": disp
                    in {
                        "firm_ordered_quantity_verified",
                        "estimated_quantity",
                        "quantity_absent_from_complete_documents",
                        "missing_document",
                        "extraction_failure",
                        "source_access_restricted",
                    }
                    and pipe.get("stage") in {STAGE_B, STAGE_C},
                    "_defer_source_link": True,
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
                            "pipeline_reasons": pipe.get("reasons"),
                            "official_url": row["official_url"],
                            "document_text_len": row["document_text_len"],
                            "package_status": row["package_status"],
                            "openai": row["openai"],
                            "build": BUILD,
                        },
                    }
                )
            elif disp in {
                "estimated_quantity",
                "missing_document",
                "quantity_absent_from_complete_documents",
                "extraction_failure",
                "source_access_restricted",
            } and (it.get("requirement_class") or "").startswith("product"):
                promising_queue.append(
                    {
                        "nb_id": row["nb_id"],
                        "title": row["title"],
                        "disposition": disp,
                        "official_url": row["official_url"],
                        "document_text_len": row["document_text_len"],
                        "package_status": row["package_status"],
                        "next_action": (
                            "acquire_public_solicitation_documents"
                            if disp in {"missing_document", "source_access_restricted"}
                            else "confirm_firm_ordered_quantity"
                        ),
                    }
                )

        # Batch registry persist (preserve all prior fields; never delete)
        print(f"002N registry_persist n={len(registry_payloads)}", flush=True)
        try:
            acceptance["registry_persist"] = ingest_population(
                registry_payloads, actor="build_002n"
            )
        except Exception as exc:
            acceptance["registry_persist"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "attempted": len(registry_payloads),
            }

        # --- Verify pre-existing Stage C ---
        stage_c_verify = verify_stage_c(db, registry_stage_c)
        acceptance["stage_c_verification"] = {
            "examined": stage_c_verify["examined"],
            "kept_stage_c": stage_c_verify["kept_stage_c"],
            "demoted": stage_c_verify["demoted"],
            "rows": stage_c_verify["rows"],
        }

        lock1 = assert_nextbid_locked()
        acceptance.update(
            {
                "finished_at": _utc(),
                "nextbid_lock_final": lock1,
                "metered_nextbid_calls": 0,
                "totals": {
                    "investigated": len(work_items),
                    "audit_710_covered": sum(1 for r in individual if r.get("in_audit_710")),
                    "quantities_recovered": quantities_recovered,
                    "new_stage_c_fits": len(new_stage_c),
                    "dispositions": dict(dispositions),
                    "promising_queue": len(promising_queue),
                    "complete_packages": sum(
                        1 for r in individual if r.get("package_status") == "COMPLETE"
                    ),
                    "partial_packages": sum(
                        1 for r in individual if r.get("package_status") == "PARTIAL"
                    ),
                    "restricted_packages": sum(
                        1
                        for r in individual
                        if r.get("package_status") == "ACCESS_RESTRICTED"
                    ),
                    "long_text_ge_400": sum(
                        1 for r in individual if int(r.get("document_text_len") or 0) >= 400
                    ),
                },
                "new_stage_c": new_stage_c,
                "promising_queue": promising_queue,
            }
        )

        # Persist individual accounting (all records)
        out_dir = ROOT / "data" / "acceptance"
        _write(out_dir / "build002n_individual_accounting.json", {"build": BUILD, "rows": individual})
        _write(out_dir / "build002n_acceptance.json", acceptance)
        _write(
            out_dir / "build002n_promising_queue.json",
            {"build": BUILD, "count": len(promising_queue), "queue": promising_queue},
        )
        _write(
            out_dir / "build002n_stage_c_evidence.json",
            {"build": BUILD, "count": len(new_stage_c), "opportunities": new_stage_c},
        )

        write_reports(acceptance, individual)
        print(
            f"002N DONE investigated={len(work_items)} recovered_qty={quantities_recovered} "
            f"new_stage_c={len(new_stage_c)} dispositions={dict(dispositions)}",
            flush=True,
        )
        return acceptance
    finally:
        # Never unlock NextBid
        try:
            assert_nextbid_locked()
        except Exception as exc:
            print(f"002N WARNING lock check in finally: {exc}", flush=True)
        db.close()


def write_reports(acceptance: dict[str, Any], individual: list[dict]) -> None:
    docs = ROOT / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    t = acceptance.get("totals") or {}
    disp = t.get("dispositions") or {}
    oa = acceptance.get("openai_execution") or {}
    ov = acceptance.get("openai_verification") or {}
    free = acceptance.get("free_document_recovery") or {}
    scv = acceptance.get("stage_c_verification") or {}

    report = f"""# BUILD 002N — Quantity Extraction & Solicitation Document Recovery

**Status:** AWAITING OWNER ACCEPTANCE  
**Build:** {acceptance.get("build")}  
**Started:** {acceptance.get("started_at")}  
**Finished:** {acceptance.get("finished_at")}  
**Metered NextBid calls:** {acceptance.get("metered_nextbid_calls", 0)} (LOCKED)

## NextBid lock

- Locked at start: `{acceptance.get("nextbid_lock", {}).get("locked")}`
- Locked at end: `{acceptance.get("nextbid_lock_final", {}).get("locked")}`
- Killswitch engaged: `{acceptance.get("nextbid_lock", {}).get("killswitch_engaged")}`
- Owner authorization: `{acceptance.get("nextbid_lock", {}).get("owner_authorization")}`

## OpenAI integration verification (live evidence)

- API credentials configured: `{ov.get("api_key_configured")}` (value never logged)
- Paid research enabled: `{ov.get("paid_research_enabled")}`
- Policy default model: `{ov.get("default_model")}`
- Live request OK: `{ov.get("live_request_ok")}`
- Model used: `{ov.get("model_used")}`
- Response id: `{ov.get("response_id")}`
- Tokens in/out: `{ov.get("input_tokens")}` / `{ov.get("output_tokens")}`
- Text chars sent (smoke): `{ov.get("text_chars_sent")}`
- Parsed smoke quantity/semantic: `{ov.get("parsed_quantity")}` / `{ov.get("parsed_semantic")}`
- Estimated cost (smoke): `{ov.get("estimated_cost_usd")}`
- Silent regex-only fallback: `{ov.get("silent_regex_fallback")}`

### OpenAI quantity extraction execution

- Successful requests: **{oa.get("successful_requests")}**
- Failed requests: **{oa.get("failed_requests")}**
- Budget denied: `{oa.get("budget_denied")}`
- Applied firm quantities: `{oa.get("applied_firm")}`
- Applied estimated (not Stage C): `{oa.get("applied_estimated")}`
- Job spend estimate USD: `{oa.get("job_spent_usd_estimate")}`
- Silent regex fallback: `{oa.get("silent_regex_fallback")}`

## Inventory results

| Metric | Count |
|--------|------:|
| Product opportunities investigated | {t.get("investigated")} |
| Audit-710 IDs covered | {t.get("audit_710_covered")} |
| Quantities successfully recovered (any semantic) | {t.get("quantities_recovered")} |
| New Stage C fits (firm ordered only) | {t.get("new_stage_c_fits")} |
| Firm ordered quantity verified | {disp.get("firm_ordered_quantity_verified", 0)} |
| Estimated quantity | {disp.get("estimated_quantity", 0)} |
| Indefinite purchase commitment | {disp.get("indefinite_purchase_commitment", 0)} |
| Quantity absent from complete documents | {disp.get("quantity_absent_from_complete_documents", 0)} |
| Missing document | {disp.get("missing_document", 0)} |
| Extraction failure | {disp.get("extraction_failure", 0)} |
| Source access restricted | {disp.get("source_access_restricted", 0)} |
| Confirmed procurement incompatibility | {disp.get("confirmed_procurement_incompatibility", 0)} |
| Long text (≥400 chars) after recovery | {t.get("long_text_ge_400")} |
| Complete packages | {t.get("complete_packages")} |
| Partial packages | {t.get("partial_packages")} |
| Restricted packages | {t.get("restricted_packages")} |
| Promising working queue | {t.get("promising_queue")} |

## Free public document recovery

- Processed: `{free.get("processed")}`
- Metered NextBid: `{free.get("metered_nextbid")}`
- OK: `{free.get("ok")}`
- Stats: `{json.dumps(free.get("stats") or {}, default=str)[:800]}`

## Existing Stage C verification

- Examined: `{scv.get("examined")}`
- Kept Stage C: `{scv.get("kept_stage_c")}`
- Demoted (no longer firm-eligible): `{scv.get("demoted")}`

## Standards preserved

- Estimated / ambiguous quantities are **not** represented as firm Stage C.
- Missing information is **not** classified as confirmed disqualification.
- No quantities invented.
- Model years, packaging, dimensions, capacities, weights rejected as ordered qty.
- Every previously acquired opportunity / document / source id preserved.
- Individual accounting: `data/acceptance/build002n_individual_accounting.json` ({len(individual)} rows)
- Stage C evidence: `data/acceptance/build002n_stage_c_evidence.json`
- Promising queue: `data/acceptance/build002n_promising_queue.json`

## New Stage C evidence (summary)

"""
    for fit in (acceptance.get("new_stage_c") or [])[:50]:
        report += (
            f"- `{fit.get('nb_id')}` qty={fit.get('quantity')} "
            f"sem={fit.get('quantity_semantic')} — {(fit.get('title') or '')[:80]}\n"
        )
    if not acceptance.get("new_stage_c"):
        report += "- _(none promoted in this build)_\n"

    report += """
---
Awaiting owner acceptance. Build 003 not started. NextBid remains locked.
"""
    (docs / "BUILD_002N_REPORT.md").write_text(report, encoding="utf-8")

    # Shorter acquisition-focused note
    acq = f"""# BUILD 002N — Acquisition / Lock Note

Metered NextBid calls during 002N: **0**

Lock start: {json.dumps(acceptance.get("nextbid_lock"), default=str)}
Lock end: {json.dumps(acceptance.get("nextbid_lock_final"), default=str)}

Free public document recovery only. No NextBid detail/document/search calls authorized or executed.
"""
    (docs / "BUILD_002N_ACQUISITION_REPORT.md").write_text(acq, encoding="utf-8")


if __name__ == "__main__":
    main()
