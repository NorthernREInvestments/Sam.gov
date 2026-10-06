"""Checkpointed BidNet recovery batch runner — staged 100 → 500 → 2000 → full."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from bidnet_recovery.recover import (
    is_bidnet_rec,
    recover_one,
    recovery_priority_tier,
    _client,
)
from bidnet_recovery.states import (
    DETAIL_RECOVERED,
    DOCUMENTS_RECOVERED,
    ECONOMICS_READY,
    EXPIRED,
    GOV_VALUE_FOUND,
    NOT_PRODUCT,
    PRODUCT_IDENTIFIED,
    PUBLIC_COST_FOUND,
    RECOVERY_BLOCKED,
)

log = logging.getLogger("govtracker.bidnet_recovery.batch")

CHECKPOINT = "m3_bidnet_recovery_checkpoint.json"
REPORT = "m3_bidnet_recovery_last_report.json"


def _paths():
    from m3_data_root import data_path

    return data_path(CHECKPOINT), data_path(REPORT)


def _load_ck() -> dict[str, Any]:
    path, _ = _paths()
    if not path.exists():
        return {"kind": "BidNetRecoveryCheckpoint", "processed_ids": [], "cursor": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "BidNetRecoveryCheckpoint", "processed_ids": [], "cursor": 0}


def _save_ck(payload: dict[str, Any]) -> None:
    path, _ = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def bidnet_recovery_funnel(store: dict[str, Any] | None = None) -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import load_store
    from phase_l.owner_ui_service import _is_available_rec

    store = store if store is not None else load_store()
    counts: Counter = Counter()
    blockers: Counter = Counter()
    class_changes: Counter = Counter()
    profit: Counter = Counter()
    total = 0
    for rec in store.values():
        if not isinstance(rec, dict) or not is_bidnet_rec(rec):
            continue
        if not _is_available_rec(rec) and str((rec.get("bidnet_recovery") or {}).get("state")) not in {
            EXPIRED,
            NOT_PRODUCT,
        }:
            # still count BidNet rows that were live at discovery
            pass
        total += 1
        br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
        state = br.get("state") or "DISCOVERED"
        counts[state] += 1
        for b in br.get("blockers") or []:
            blockers[str(b)] += 1
        if br.get("reclassified"):
            class_changes[f"{br.get('class_before')}->{br.get('class_after')}"] += 1
        pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
        if pf.get("profit_status"):
            profit[str(pf["profit_status"])] += 1
        if "PROFITABLE_AT_PUBLIC_RETAIL" in (pf.get("proof_signals") or []):
            profit["PROFITABLE_AT_PUBLIC_RETAIL"] += 1

    def _pct(n: int, d: int) -> float:
        return round(100.0 * n / d, 2) if d else 0.0

    discovered = total
    # Cumulative: at-or-beyond each stage
    detail = sum(
        counts.get(s, 0)
        for s in (
            DETAIL_RECOVERED,
            DOCUMENTS_RECOVERED,
            PRODUCT_IDENTIFIED,
            GOV_VALUE_FOUND,
            PUBLIC_COST_FOUND,
            ECONOMICS_READY,
        )
    )
    docs = sum(
        counts.get(s, 0)
        for s in (
            DOCUMENTS_RECOVERED,
            GOV_VALUE_FOUND,
            PUBLIC_COST_FOUND,
            ECONOMICS_READY,
        )
    )
    # Also count PRODUCT_IDENTIFIED+ only if documents actually attached
    docs_with_files = 0
    product = sum(
        counts.get(s, 0)
        for s in (
            PRODUCT_IDENTIFIED,
            GOV_VALUE_FOUND,
            PUBLIC_COST_FOUND,
            ECONOMICS_READY,
        )
    )
    gov = counts.get(GOV_VALUE_FOUND, 0) + counts.get(ECONOMICS_READY, 0)
    cost = counts.get(PUBLIC_COST_FOUND, 0) + counts.get(ECONOMICS_READY, 0)
    ready = counts.get(ECONOMICS_READY, 0)

    # Recount docs from actual attachments on BidNet rows that reached product+
    for rec in store.values():
        if not isinstance(rec, dict) or not is_bidnet_rec(rec):
            continue
        br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
        docs_meta = rec.get("attachments_metadata") or []
        real_docs = [
            d
            for d in docs_meta
            if isinstance(d, dict)
            and d.get("document_type") in {"attachment", "linked"}
            and "abstract" not in str(d.get("document_url") or d.get("url") or "").lower()
        ]
        if real_docs and br.get("state") not in {None, "DISCOVERED"}:
            docs_with_files += 1
    docs = docs_with_files

    return {
        "kind": "BidNetRecoveryFunnel",
        "generated_at": now_utc().isoformat(),
        "BidNet_discovered": discovered,
        "Detail_recovered": detail,
        "Documents_recovered": docs,
        "Product_identified": product,
        "Gov_value_found": gov,
        "Public_cost_found": cost,
        "Economics_ready": ready,
        "Profitable": profit.get("PROVEN_PROFITABLE", 0) + profit.get("LIKELY_PROFITABLE", 0),
        "state_counts": dict(counts),
        "blockers": dict(blockers.most_common(20)),
        "class_changes": dict(class_changes.most_common(30)),
        "profit_statuses": dict(profit),
        "conversion_pct": {
            "detail_of_discovered": _pct(detail, discovered),
            "documents_of_detail": _pct(docs, detail),
            "product_of_detail": _pct(product, detail),
            "gov_of_product": _pct(gov, product),
            "cost_of_product": _pct(cost, product),
            "ready_of_product": _pct(ready, product),
            "profitable_of_ready": _pct(
                profit.get("PROVEN_PROFITABLE", 0) + profit.get("LIKELY_PROFITABLE", 0), ready
            ),
        },
    }


def _select_bidnet_candidates(
    store: dict[str, Any],
    *,
    done: set[str],
    force: bool,
    recovery_batch_size: int,
    backlog_batch_size: int,
    min_tier: int,
) -> list[tuple[int, str, dict[str, Any]]]:
    """Incremental priority: new/updated/imminent/missing-docs first, then backlog budget."""
    from phase_l.owner_ui_service import _is_available_rec

    priority: list[tuple[int, str, dict[str, Any]]] = []
    backlog: list[tuple[int, str, dict[str, Any]]] = []
    for cid, rec in store.items():
        if not isinstance(rec, dict) or not is_bidnet_rec(rec):
            continue
        if not _is_available_rec(rec):
            continue
        if not force and cid in done:
            br = rec.get("bidnet_recovery") or {}
            # Re-queue auth-blocked / document-missing product rows
            blockers = set(br.get("blockers") or [])
            if br.get("economics_dead") or "PACKAGE_UNAVAILABLE_FREE" in blockers or rec.get("economics_dead"):
                continue
            if br.get("state") and br.get("state") != "DISCOVERED":
                if "AUTH_REQUIRED" not in blockers and br.get("state") not in {
                    "DETAIL_RECOVERED",
                    "PRODUCT_IDENTIFIED",
                }:
                    continue
                if br.get("state") in {"ECONOMICS_READY", "NOT_PRODUCT", "EXPIRED"}:
                    continue
        tier = recovery_priority_tier(rec)
        if tier > min_tier and tier != 5:
            continue
        if tier <= 2:
            priority.append((tier, cid, rec))
        elif tier == 5:
            backlog.append((tier, cid, rec))
        elif tier <= min_tier:
            priority.append((tier, cid, rec))

    priority.sort(key=lambda x: (x[0], x[1]))
    backlog.sort(key=lambda x: (x[0], x[1]))
    selected = priority[: max(0, int(recovery_batch_size))]
    remaining_slots = max(0, int(recovery_batch_size) + int(backlog_batch_size) - len(selected))
    selected.extend(backlog[:remaining_slots])
    return selected


def run_bidnet_recovery(
    *,
    limit: int = 100,
    batch_size: int = 25,
    resume: bool = True,
    persist: bool = True,
    force: bool = False,
    min_tier: int = 5,
    run_id: str | None = None,
    use_auth: bool | None = None,
    recovery_batch_size: int | None = None,
    backlog_batch_size: int | None = None,
    target_ids: list[str] | None = None,
    require_auth_blocker: bool = False,
) -> dict[str, Any]:
    """Recover BidNet detail for up to `limit` prioritized records.

    When auth is enabled and credentials exist, uses BidNetAuthenticatedClient first.
    Auth challenge/failure stops the authenticated portion cleanly (no fake success).
    """
    from phase_l.l23_full_population_funnel import load_store, save_store
    from m3_canonical_discovery_bridge import available_count

    run_id = run_id or f"BNR-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    store = load_store()
    before_live = available_count(store)
    ck = _load_ck() if resume else {"kind": "BidNetRecoveryCheckpoint", "processed_ids": [], "cursor": 0}
    done = set(ck.get("processed_ids") or [])

    # Batch budgets
    try:
        from bidnet_auth.config import load_bidnet_auth_config

        acfg = load_bidnet_auth_config()
    except Exception:
        acfg = None

    rec_budget = int(
        recovery_batch_size
        if recovery_batch_size is not None
        else (acfg.recovery_batch_size if acfg else limit)
    )
    back_budget = int(
        backlog_batch_size
        if backlog_batch_size is not None
        else (acfg.backlog_batch_size if acfg else 0)
    )
    # Respect legacy `limit` as hard cap
    hard_cap = max(0, int(limit))
    if hard_cap and rec_budget + back_budget > hard_cap:
        # Prefer priority slots within the hard cap
        rec_budget = min(rec_budget, hard_cap)
        back_budget = max(0, hard_cap - rec_budget)

    should_auth_pref = use_auth is True or (use_auth is None and bool(acfg and acfg.can_authenticate))
    if target_ids:
        selected = []
        for cid in target_ids:
            rec = store.get(cid)
            if isinstance(rec, dict) and is_bidnet_rec(rec):
                selected.append((1, cid, rec))
        selected = selected[: max(0, hard_cap or len(selected))]
    elif require_auth_blocker or should_auth_pref:
        # Prefer previously AUTH_REQUIRED / DOCUMENTS_NOT_AVAILABLE BidNet rows
        auth_pref: list[tuple[int, str, dict[str, Any]]] = []
        for cid, rec in store.items():
            if not isinstance(rec, dict) or not is_bidnet_rec(rec):
                continue
            br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
            blockers = set(br.get("blockers") or [])
            if "AUTH_REQUIRED" not in blockers and "DOCUMENTS_NOT_AVAILABLE" not in blockers:
                continue
            # Free chase already exhausted — no BidNet membership path
            if br.get("economics_dead") or "PACKAGE_UNAVAILABLE_FREE" in blockers or rec.get("economics_dead"):
                continue
            if br.get("state") in {"ECONOMICS_READY", "EXPIRED", "NOT_PRODUCT"}:
                continue
            auth_pref.append((recovery_priority_tier(rec), cid, rec))
        auth_pref.sort(key=lambda x: (x[0], x[1]))
        selected = auth_pref[: max(0, hard_cap or (rec_budget + back_budget))]
        if not selected:
            selected = _select_bidnet_candidates(
                store,
                done=done,
                force=force,
                recovery_batch_size=rec_budget,
                backlog_batch_size=back_budget,
                min_tier=min_tier,
            )
    else:
        selected = _select_bidnet_candidates(
            store,
            done=done,
            force=force,
            recovery_batch_size=rec_budget,
            backlog_batch_size=back_budget,
            min_tier=min_tier,
        )

    auth_info: dict[str, Any] | None = None
    auth_client = None
    auth_stop: str | None = None
    should_auth = use_auth
    if should_auth is None:
        should_auth = bool(acfg and acfg.can_authenticate)

    if should_auth:
        try:
            from bidnet_auth import BidNetAuthenticatedClient

            auth_client = BidNetAuthenticatedClient()
            auth_result = auth_client.ensure_authenticated()
            auth_info = auth_result.to_dict()
            if not auth_result.authenticated:
                auth_stop = auth_result.status
                log.warning(
                    "BidNet auth blocked recovery status=%s msg=%s",
                    auth_result.status,
                    auth_result.message,
                )
                # Do not silently fall back to anonymous as "authenticated success"
                report = {
                    "kind": "BidNetRecoveryBatchReport",
                    "run_id": run_id,
                    "started_at": started,
                    "completed_at": now_utc().isoformat(),
                    "limit": limit,
                    "selected": len(selected),
                    "processed": 0,
                    "auth": auth_info,
                    "auth_stop": auth_stop,
                    "stats": {"attempted": 0, "auth_blocked": 1},
                    "funnel": bidnet_recovery_funnel(store),
                    "sample": [],
                    "SAM_API_CALLS": 0,
                    "note": (
                        f"Authenticated BidNet recovery stopped: {auth_stop}. "
                        "Anonymous fallback not used for this authenticated run."
                    ),
                }
                try:
                    auth_client.close()
                except Exception:
                    pass
                if persist:
                    _, rpath = _paths()
                    rpath.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
                return report
        except Exception as exc:
            auth_stop = "AUTH_FAILED"
            auth_info = {
                "status": "AUTH_FAILED",
                "authenticated": False,
                "message": f"auth_client_error:{type(exc).__name__}",
            }
            log.exception("BidNet auth client failed")
            report = {
                "kind": "BidNetRecoveryBatchReport",
                "run_id": run_id,
                "started_at": started,
                "completed_at": now_utc().isoformat(),
                "limit": limit,
                "selected": len(selected),
                "processed": 0,
                "auth": auth_info,
                "auth_stop": auth_stop,
                "stats": {"attempted": 0, "auth_blocked": 1},
                "funnel": bidnet_recovery_funnel(store),
                "sample": [],
                "SAM_API_CALLS": 0,
                "note": "Authenticated BidNet recovery failed before any records were processed.",
            }
            if persist:
                _, rpath = _paths()
                rpath.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            return report

    client = None if auth_client and getattr(auth_client, "is_authenticated", False) else _client()
    stats: Counter = Counter()
    class_delta: Counter = Counter()
    results_sample: list[dict[str, Any]] = []
    processed = 0

    try:
        for i, (tier, cid, rec) in enumerate(selected):
            out = recover_one(
                cid,
                rec,
                client=client,
                auth_client=auth_client if auth_client and auth_client.is_authenticated else None,
                fetch_live=True,
            )
            if out.get("auth_challenge"):
                auth_stop = "AUTH_CHALLENGE"
                stats["auth_challenge"] += 1
                log.warning("BidNet AUTH_CHALLENGE mid-batch — stopping authenticated recovery")
                break
            processed += 1
            done.add(cid)
            stats["attempted"] += 1
            stats[f"state:{out.get('state')}"] += 1
            if out.get("deadline_recovered"):
                stats["deadlines_recovered"] += 1
            if out.get("documents"):
                stats["documents_recovered"] += 1
                stats["document_urls"] += int(out.get("documents") or 0)
            if out.get("improved"):
                stats["detail_recovered"] += 1
            if out.get("issuing_org"):
                stats["issuing_org_recovered"] += 1
            if out.get("solicitation_number"):
                stats["solicitation_number_recovered"] += 1
            if out.get("agency_source_url"):
                stats["source_url_recovered"] += 1
            if out.get("authenticated"):
                stats["authenticated_fetches"] += 1
            if out.get("reclassified"):
                stats["reclassified"] += 1
                class_delta[f"{out.get('class_before')}->{out.get('class_after')}"] += 1
            if out.get("gov_value"):
                stats["gov_value_found"] += 1
            if out.get("public_cost"):
                stats["public_cost_found"] += 1
            if out.get("economics_ready"):
                stats["economics_ready"] += 1
            if out.get("auth_wall"):
                stats["auth_wall"] += 1
            if out.get("expired"):
                stats["expired_removed"] += 1
            if out.get("state") == RECOVERY_BLOCKED:
                stats["recovery_blocked"] += 1
            if out.get("state") == NOT_PRODUCT:
                stats["not_product"] += 1
            if len(results_sample) < 25:
                results_sample.append({"id": cid, "tier": tier, "title": (rec.get("title") or "")[:80], **out})

            if persist and processed % batch_size == 0:
                ck = {
                    "kind": "BidNetRecoveryCheckpoint",
                    "run_id": run_id,
                    "processed_ids": list(done)[-100000:],
                    "updated_at": now_utc().isoformat(),
                    "stats_partial": dict(stats),
                }
                _save_ck(ck)
                save_store(store)
                log.info("BidNet recovery checkpoint n=%s", processed)
    finally:
        if auth_client is not None:
            try:
                auth_client.close()
            except Exception:
                pass

    after_live = available_count(store)
    funnel = bidnet_recovery_funnel(store)

    # Top evidence-backed by profit (no guesses)
    top_ops = []
    for cid, rec in store.items():
        if not isinstance(rec, dict) or not is_bidnet_rec(rec):
            continue
        pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
        if pf.get("profit_status") not in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT"}:
            continue
        top_ops.append(
            {
                "id": cid,
                "title": (rec.get("title") or "")[:100],
                "status": pf.get("profit_status"),
                "post_financing_profit": pf.get("post_financing_profit") or pf.get("expected_profit"),
                "recovery_state": (rec.get("bidnet_recovery") or {}).get("state"),
            }
        )
    top_ops.sort(key=lambda x: -(float(x.get("post_financing_profit") or 0)))

    report = {
        "kind": "BidNetRecoveryBatchReport",
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "limit": limit,
        "recovery_batch_size": rec_budget,
        "backlog_batch_size": back_budget,
        "selected": len(selected),
        "processed": processed,
        "before_canonical_live": before_live,
        "after_canonical_live": after_live,
        "auth": auth_info,
        "auth_stop": auth_stop,
        "stats": dict(stats),
        "class_changes": dict(class_delta),
        "funnel": funnel,
        "sample": results_sample,
        "top_evidence_backed": top_ops[:15],
        "SAM_API_CALLS": 0,
        "note": (
            "BidNet is discovery-index only. AUTH_REQUIRED triggers free package chase "
            "(agency / OpenGov / public detail). No BidNet membership. "
            "No free package → PACKAGE_UNAVAILABLE_FREE / economics-dead."
        ),
    }

    if persist:
        save_store(store)
        _save_ck(
            {
                "kind": "BidNetRecoveryCheckpoint",
                "run_id": run_id,
                "processed_ids": list(done)[-100000:],
                "updated_at": now_utc().isoformat(),
                "last_report": {
                    "processed": processed,
                    "detail_recovered": stats.get("detail_recovered"),
                    "deadlines_recovered": stats.get("deadlines_recovered"),
                    "auth_status": (auth_info or {}).get("status"),
                },
            }
        )
        _, rpath = _paths()
        rpath.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return report
