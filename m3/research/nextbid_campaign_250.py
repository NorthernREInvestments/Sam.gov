"""Owner-authorized single-use 250-credit product acquisition campaign.

Lane A (existing inventory) ≤150 planning / Lane B (new discovery) ≤100 planning.
Absolute hard cap 250. Checkpoints ~25 / 75 / 150 / 250. Always restores killswitch.
Does NOT enable recurring daily acquisition or federal sources.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.nextbid_client import NextBidClient
from m3.research.nextbid_controller import (
    CAMPAIGN_250_ALLOWED_ENDPOINTS,
    CAMPAIGN_250_CREDIT_CAP,
    CAMPAIGN_250_SCOPE,
    NextBidBudgetExhausted,
    NextBidUnauthorized,
    acquisition_locked,
    append_ledger,
    controller_status,
    load_budget_state,
    new_job_id,
    refresh_account_usage,
    revoke_owner_authorization,
    write_owner_authorization,
)
from m3.research.nextbid_inventory import load_inventory, upsert_inventory
from m3.research.nextbid_killswitch import clear_emergency_stop, engage_emergency_stop
from m3.research.nextbid_live_pilot import (
    FEDERAL_SKIP,
    _execute_proposed,
    _ingest_response,
    _is_physical_product_candidate,
    _process_retained,
    _select_lane_a_targets,
    publish_owner_queues,
)
from m3.research.nextbid_procurement_pipeline import plan_cheapest_retrieval
from m3.research.full_text_store import store_full_text

LANE_A_PLAN = 150
LANE_B_PLAN = 100
CAMPAIGN_ID = "campaign_250_product_acquisition_20261125"
CAMPAIGN_NOTE = "owner_campaign_250_product_acquisition_single_use_20261125"
CHECKPOINTS = (25, 75, 150, 250)
RESERVED_FLOOR = 1000


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[campaign-250] {msg}", flush=True)


def _report_dir() -> Path:
    p = Path("TEMP") / "CAMPAIGN_250"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _campaign_state_path() -> Path:
    p = get_settings().data_root() / "nextbid_ledger"
    p.mkdir(parents=True, exist_ok=True)
    return p / "campaign_250_state.json"


def _worker_lock_path() -> Path:
    return get_settings().data_root() / "nextbid_ledger" / "campaign_250.worker.lock"


def _load_campaign_state() -> dict[str, Any]:
    path = _campaign_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"corrupt": True}


def _save_campaign_state(state: dict[str, Any]) -> None:
    path = _campaign_state_path()
    payload = json.dumps(state, indent=2, default=str)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    for attempt in range(8):
        try:
            tmp.replace(path)
            return
        except (PermissionError, OSError):
            time.sleep(0.05 * (attempt + 1))
    path.write_text(payload, encoding="utf-8")
    tmp.unlink(missing_ok=True)


def _acquire_worker_lock() -> Path:
    lock = _worker_lock_path()
    if lock.exists():
        age = time.time() - lock.stat().st_mtime
        if age < 6 * 3600:
            raise RuntimeError(f"campaign_worker_already_active:{lock}")
        lock.unlink(missing_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "at": _utc(), "campaign_id": CAMPAIGN_ID}), encoding="utf-8")
    return lock


def _release_worker_lock(lock: Path | None) -> None:
    if lock and lock.exists():
        lock.unlink(missing_ok=True)


def authorize_and_unlock_campaign() -> dict[str, Any]:
    prior = _load_campaign_state()
    if prior.get("status") == "completed" and prior.get("campaign_id") == CAMPAIGN_ID and int(prior.get("credits_spent") or 0) > 0:
        raise RuntimeError("campaign_250_already_completed_single_use")
    if prior.get("status") == "running":
        raise RuntimeError("campaign_250_already_running")

    state = load_budget_state()
    day_used = int(state.get("day_used") or 0)
    # Scoped daily exception: prior same-day pilot burn must not shrink the 250 allowance.
    daily_override = day_used + CAMPAIGN_250_CREDIT_CAP
    try:
        write_owner_authorization(
            authorized=True,
            approved_allocation_calls=CAMPAIGN_250_CREDIT_CAP,
            max_additional_calls=CAMPAIGN_250_CREDIT_CAP,
            note=CAMPAIGN_NOTE,
            scope=CAMPAIGN_250_SCOPE,
            test_call_cap=CAMPAIGN_250_CREDIT_CAP,
            allowed_endpoints=sorted(CAMPAIGN_250_ALLOWED_ENDPOINTS),
            daily_limit_override=daily_override,
            reset_test_used=True,
        )
        os.environ["NEXTBID_ENABLED"] = "true"
        os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
        get_settings.cache_clear()
        clear_emergency_stop(owner_authorized=True)
        assert acquisition_locked() is False, "campaign unlock failed"
        _save_campaign_state(
            {
                "campaign_id": CAMPAIGN_ID,
                "status": "running",
                "started_at": _utc(),
                "credits_spent": 0,
                "lane_a_spent": 0,
                "lane_b_spent": 0,
                "checkpoints_hit": [],
                "stop_reason": None,
                "daily_limit_override": daily_override,
                "day_used_at_start": day_used,
                "max_credits": CAMPAIGN_250_CREDIT_CAP,
                "reserve_floor": RESERVED_FLOOR,
            }
        )
        return controller_status()
    except Exception:
        revoke_owner_authorization()
        os.environ["NEXTBID_ENABLED"] = "false"
        os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
        get_settings.cache_clear()
        engage_emergency_stop(reason="campaign_250_authorize_failed")
        raise


def relock_campaign(*, reason: str) -> None:
    engage_emergency_stop(reason=reason)
    revoke_owner_authorization()
    os.environ["NEXTBID_ENABLED"] = "false"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    get_settings.cache_clear()
    st = _load_campaign_state()
    st["status"] = "completed" if "complete" in reason or reason.endswith(":ok") else "stopped"
    st["finished_at"] = _utc()
    st["stop_reason"] = reason
    st["authorization_revoked"] = True
    st["killswitch_locked"] = True
    _save_campaign_state(st)


def _metrics_snapshot(
    *,
    credits: int,
    screened: int,
    docs_retrieved: int,
    docs_reused: int,
    product_confirmed: int,
    with_qty_spec: int,
    quote_ready: int,
    duplicates_avoided: int,
    errors: int,
) -> dict[str, Any]:
    useful = max(quote_ready, with_qty_spec)
    return {
        "credits_consumed": credits,
        "listings_screened": screened,
        "original_documents_retrieved": docs_retrieved,
        "documents_reused": docs_reused,
        "confirmed_product_opportunities": product_confirmed,
        "opportunities_with_qty_spec": with_qty_spec,
        "supplier_quote_ready_additions": quote_ready,
        "duplicate_requests_avoided": duplicates_avoided,
        "provider_errors": errors,
        "credits_per_useful": (credits / useful) if useful else None,
    }


def _checkpoint_continue(metrics: dict[str, Any], *, checkpoint: int) -> tuple[bool, str]:
    credits = int(metrics.get("credits_consumed") or 0)
    useful = int(metrics.get("supplier_quote_ready_additions") or 0) + int(
        metrics.get("opportunities_with_qty_spec") or 0
    )
    errors = int(metrics.get("provider_errors") or 0)
    if errors >= 3:
        return False, f"provider_errors:{errors}"
    if checkpoint >= 75 and useful == 0 and credits >= 40:
        return False, "no_meaningful_requirements_after_75"
    if checkpoint >= 150 and useful < 2:
        return False, "insufficient_yield_at_150"
    cpp = metrics.get("credits_per_useful")
    if checkpoint >= 75 and cpp is not None and cpp > 80 and useful < 3:
        return False, f"poor_credits_per_useful:{cpp}"
    return True, "ok"


class CampaignBudget:
    def __init__(self, total_cap: int):
        self.total_cap = total_cap
        self.spent = 0
        self.lane_a = 0
        self.lane_b = 0
        self.checkpoints_hit: list[int] = []
        self.stop_reason: str | None = None
        self.screened = 0
        self.docs_retrieved = 0
        self.docs_reused = 0
        self.product_confirmed = 0
        self.with_qty_spec = 0
        self.quote_ready = 0
        self.duplicates_avoided = 0
        self.errors = 0
        self.cards: list[dict[str, Any]] = []
        self.pre_existing_quote_ids: set[str] = set()

    def remaining(self) -> int:
        return max(0, self.total_cap - self.spent)

    def can_spend(self, n: int = 1) -> bool:
        return self.spent + n <= self.total_cap and self.stop_reason is None

    def record_spend(self, n: int, *, lane: str) -> None:
        self.spent += n
        if lane == "A":
            self.lane_a += n
        else:
            self.lane_b += n
        st = _load_campaign_state()
        st["credits_spent"] = self.spent
        st["lane_a_spent"] = self.lane_a
        st["lane_b_spent"] = self.lane_b
        _save_campaign_state(st)
        self._maybe_checkpoint()

    def _metrics(self) -> dict[str, Any]:
        return _metrics_snapshot(
            credits=self.spent,
            screened=self.screened,
            docs_retrieved=self.docs_retrieved,
            docs_reused=self.docs_reused,
            product_confirmed=self.product_confirmed,
            with_qty_spec=self.with_qty_spec,
            quote_ready=self.quote_ready,
            duplicates_avoided=self.duplicates_avoided,
            errors=self.errors,
        )

    def _maybe_checkpoint(self) -> None:
        for cp in CHECKPOINTS:
            if self.spent >= cp and cp not in self.checkpoints_hit:
                self.checkpoints_hit.append(cp)
                metrics = self._metrics()
                ok, reason = _checkpoint_continue(metrics, checkpoint=cp)
                append_ledger(
                    {
                        "op": "campaign_250_checkpoint",
                        "campaign_id": CAMPAIGN_ID,
                        "checkpoint": cp,
                        "continue": ok,
                        "reason": reason,
                        "metrics": metrics,
                    }
                )
                _log(f"CHECKPOINT {cp}: continue={ok} reason={reason} metrics={metrics}")
                report = _report_dir() / f"checkpoint_{cp}.json"
                report.write_text(json.dumps({"checkpoint": cp, "ok": ok, "reason": reason, "metrics": metrics}, indent=2), encoding="utf-8")
                st = _load_campaign_state()
                st["checkpoints_hit"] = list(self.checkpoints_hit)
                st["last_checkpoint_metrics"] = metrics
                _save_campaign_state(st)
                if not ok:
                    self.stop_reason = reason
                    break

    def note_card(self, card: dict[str, Any]) -> None:
        self.cards.append(card)
        lines = card.get("line_items") or []
        if card.get("supplier_quote_ready"):
            self.quote_ready += 1
        if any(li.get("quantity") is not None for li in lines):
            self.with_qty_spec += 1
        if lines:
            self.product_confirmed += 1


def run_lane_a(*, client: NextBidClient, budget: CampaignBudget, credit_cap: int) -> dict[str, Any]:
    used = 0
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    targets = _select_lane_a_targets(limit=max(40, credit_cap))
    for target in targets:
        if budget.stop_reason or used >= credit_cap or not budget.can_spend(1):
            break
        nb = target["nb_id"]
        plan = plan_cheapest_retrieval(nb)
        proposed = plan.get("proposed_metered_call")
        if not proposed:
            budget.docs_reused += 1
            budget.duplicates_avoided += 1
            card = _process_retained(nb, target["row"], lane="A_offline_reuse")
            results.append(card)
            budget.note_card(card)
            skipped.append({"nb_id": nb, "reason": plan.get("stop_without_call") or "no_call"})
            continue
        if proposed.get("endpoint") not in {"get_opportunity", "read_file", "list_documents", "list_files"}:
            skipped.append({"nb_id": nb, "reason": f"endpoint_not_justified:{proposed.get('endpoint')}"})
            continue
        cost = int(proposed.get("max_credit_cost") or 1)
        if used + cost > credit_cap or not budget.can_spend(cost):
            skipped.append({"nb_id": nb, "reason": "would_exceed_lane_or_campaign_cap"})
            continue
        _log(f"LANE A {proposed['endpoint']} {nb} (lane {used + cost}/{credit_cap}, total {budget.spent + cost}/{budget.total_cap})")
        try:
            out = _execute_proposed(client, proposed)
            used += cost
            budget.record_spend(cost, lane="A")
            if proposed["endpoint"] in {"read_file", "get_document_download"}:
                budget.docs_retrieved += 1
            _ingest_response(nb, proposed["endpoint"], out["response"], target["row"])
            card = _process_retained(nb, target["row"], lane="A")
            card["metered_call"] = {
                "endpoint": proposed["endpoint"],
                "path": proposed.get("path"),
                "credits": cost,
            }
            results.append(card)
            budget.note_card(card)
        except (NextBidBudgetExhausted, NextBidUnauthorized) as exc:
            skipped.append({"nb_id": nb, "reason": str(exc)})
            budget.stop_reason = str(exc)
            break
        except Exception as exc:
            budget.errors += 1
            skipped.append({"nb_id": nb, "reason": f"{type(exc).__name__}:{exc}"})
            if budget.errors >= 3:
                budget.stop_reason = f"lane_a_errors:{budget.errors}"
                break
    return {"lane": "A", "credits_used": used, "cap": credit_cap, "results": results, "skipped": skipped}


def run_lane_b(*, client: NextBidClient, budget: CampaignBudget, credit_cap: int) -> dict[str, Any]:
    used = 0
    screened: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    product_hits: list[dict[str, Any]] = []
    if credit_cap <= 0 or budget.stop_reason:
        return {
            "lane": "B",
            "credits_used": 0,
            "cap": credit_cap,
            "screened": [],
            "results": [],
            "skipped": ["no_budget_or_stopped"],
        }

    inv = load_inventory()
    page = 1
    max_search_pages = min(8, credit_cap)  # leave room for details
    while used < credit_cap and page <= max_search_pages and budget.can_spend(1) and not budget.stop_reason:
        params = {
            "tradelines": "commodities",
            "sources": "opengov,bonfire,planetbids,calprocure,publicpurchase,instantmarkets",
            "open": "true",
            "page": page,
            "page_size": 25,
        }
        _log(f"LANE B search page={page} page_size=25 (lane {used + 1}/{credit_cap})")
        try:
            data = client.search(params)
            used += 1
            budget.record_spend(1, lane="B")
        except Exception as exc:
            budget.errors += 1
            skipped.append({"reason": f"search_failed:{type(exc).__name__}:{exc}"})
            if budget.errors >= 3:
                budget.stop_reason = f"lane_b_search_errors:{budget.errors}"
            break

        items = []
        if isinstance(data, dict):
            raw = data.get("data") if isinstance(data.get("data"), (list, dict)) else data
            if isinstance(raw, dict):
                items = raw.get("opportunities") or raw.get("items") or raw.get("results") or []
            elif isinstance(raw, list):
                items = raw
            if not items and isinstance(data.get("opportunities"), list):
                items = data["opportunities"]
        if not items:
            skipped.append({"page": page, "reason": "empty_search_page"})
            break

        page_new = 0
        for it in items or []:
            if not isinstance(it, dict):
                continue
            nb = str(it.get("nb_id") or it.get("id") or "")
            title = str(it.get("title") or "")
            src = str(it.get("source") or "").lower()
            screened.append({"nb_id": nb, "title": title[:120], "source": src, "page": page})
            budget.screened += 1
            if not nb or src in FEDERAL_SKIP:
                continue
            if nb in inv:
                budget.duplicates_avoided += 1
                continue
            if not _is_physical_product_candidate(title, str(it.get("description") or "")):
                continue
            upsert_inventory(
                [
                    {
                        "nb_id": nb,
                        "title": title,
                        "source": it.get("source"),
                        "state": it.get("state"),
                        "buyer": it.get("buyer") or it.get("agency"),
                        "deadline_at": it.get("response_deadline") or it.get("deadline_at"),
                        "solicitation_number": it.get("solicitation_number") or it.get("solicitationNumber"),
                        "official_url": it.get("url") or it.get("official_url"),
                        "description": it.get("description") or it.get("summary"),
                    }
                ]
            )
            inv[nb] = inv.get(nb) or {"nb_id": nb}
            desc = str(it.get("description") or it.get("summary") or "")
            if len(desc) >= 80:
                store_full_text(nb, desc, source="campaign_250_search_listing", title=title)
            product_hits.append({"nb_id": nb, "title": title, "item": it, "desc_chars": len(desc)})
            page_new += 1
        page += 1
        if page_new == 0 and page > 2:
            # Additional search pages not yielding new product hits — stop searching
            break

    for hit in product_hits:
        if used >= credit_cap or not budget.can_spend(1) or budget.stop_reason:
            break
        nb = hit["nb_id"]
        if hit["desc_chars"] >= 500:
            row = load_inventory().get(nb) or hit["item"]
            card = _process_retained(nb, row, lane="B_listing_only")
            results.append(card)
            budget.note_card(card)
            budget.docs_reused += 1
            continue
        # Prefer get_opportunity once; do not chain list_documents/read_file automatically
        _log(f"LANE B get_opportunity {nb} (lane {used + 1}/{credit_cap})")
        try:
            resp = client.get_opportunity(nb)
            used += 1
            budget.record_spend(1, lane="B")
            row = load_inventory().get(nb) or hit["item"]
            _ingest_response(nb, "get_opportunity", resp, row)
            card = _process_retained(nb, load_inventory().get(nb) or row, lane="B")
            card["metered_call"] = {"endpoint": "get_opportunity", "credits": 1}
            results.append(card)
            budget.note_card(card)
        except (NextBidBudgetExhausted, NextBidUnauthorized) as exc:
            skipped.append({"nb_id": nb, "reason": str(exc)})
            budget.stop_reason = str(exc)
            break
        except Exception as exc:
            budget.errors += 1
            skipped.append({"nb_id": nb, "reason": f"{type(exc).__name__}:{exc}"})
            if budget.errors >= 3:
                budget.stop_reason = f"lane_b_detail_errors:{budget.errors}"
                break

    return {
        "lane": "B",
        "credits_used": used,
        "cap": credit_cap,
        "listings_returned_pages": page - 1,
        "product_hits": len(product_hits),
        "screened": screened,
        "results": results,
        "skipped": skipped,
    }


def _seed_preexisting_quote_ids() -> set[str]:
    ids: set[str] = set()
    root = get_settings().staged_inventory_root() / "owner"
    for name in ("supplier_pricing_live.jsonl", "supplier_quote_ready_offline.jsonl", "pricing_now_offline.jsonl"):
        path = root / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            nb = row.get("nb_id")
            if nb:
                ids.add(str(nb))
    qpath = get_settings().data_root() / "procurement_queues" / "awaiting_supplier_quotations.json"
    if qpath.exists():
        try:
            payload = json.loads(qpath.read_text(encoding="utf-8"))
            for r in payload.get("rows") or []:
                if r.get("nb_id"):
                    ids.add(str(r["nb_id"]))
        except Exception:
            pass
    return ids


def run_campaign_250() -> dict[str, Any]:
    """Execute the owner-authorized 250-credit single-use campaign and relock."""
    t0 = datetime.now(timezone.utc)
    report_dir = _report_dir()
    lock: Path | None = None
    usage_before = refresh_account_usage(allow_when_locked=True)
    before = {
        "remaining": usage_before.get("remaining"),
        "used": usage_before.get("used"),
        "allowance": usage_before.get("allowance"),
    }
    assert int(before["remaining"] or 0) - CAMPAIGN_250_CREDIT_CAP >= RESERVED_FLOOR, "reserve would be breached"
    assert acquisition_locked(), "must start locked"
    job_id = new_job_id("campaign_250")
    client = NextBidClient(job_id=job_id)
    lane_a: dict[str, Any] = {}
    lane_b: dict[str, Any] = {}
    error: str | None = None
    unlocked = False
    budget = CampaignBudget(CAMPAIGN_250_CREDIT_CAP)
    budget.pre_existing_quote_ids = _seed_preexisting_quote_ids()
    try:
        lock = _acquire_worker_lock()
        st = authorize_and_unlock_campaign()
        unlocked = True
        append_ledger(
            {
                "op": "campaign_250_start",
                "campaign_id": CAMPAIGN_ID,
                "job_id": job_id,
                "allocation": CAMPAIGN_250_CREDIT_CAP,
                "lane_a_plan": LANE_A_PLAN,
                "lane_b_plan": LANE_B_PLAN,
                "usage_before": before,
                "controller": {"limits": st.get("limits"), "budget_state": st.get("budget_state")},
            }
        )
        # Lane A first (existing inventory recovery)
        lane_a_cap = min(LANE_A_PLAN, budget.remaining())
        lane_a = run_lane_a(client=client, budget=budget, credit_cap=lane_a_cap)
        # Flexible reassignment: unused Lane A may feed Lane B under absolute 250
        lane_b_cap = min(LANE_B_PLAN + max(0, LANE_A_PLAN - int(lane_a.get("credits_used") or 0)), budget.remaining())
        if not budget.stop_reason and lane_b_cap > 0:
            lane_b = run_lane_b(client=client, budget=budget, credit_cap=lane_b_cap)
        # If Lane B under-spent and Lane A still has justified targets, reassign leftover
        leftover = budget.remaining()
        if not budget.stop_reason and leftover >= 5 and int(lane_a.get("credits_used") or 0) < LANE_A_PLAN:
            extra = run_lane_a(client=client, budget=budget, credit_cap=min(leftover, 40))
            lane_a["credits_used"] = int(lane_a.get("credits_used") or 0) + int(extra.get("credits_used") or 0)
            lane_a.setdefault("results", []).extend(extra.get("results") or [])
            lane_a["reassigned_extra"] = extra.get("credits_used")
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
        _log(f"CAMPAIGN ERROR {error}")
        budget.stop_reason = budget.stop_reason or error
    finally:
        if unlocked or not acquisition_locked():
            relock_campaign(reason=f"campaign_250_complete:{error or budget.stop_reason or 'ok'}")
        _release_worker_lock(lock)
        assert acquisition_locked(), "killswitch must be restored"

    usage_after = refresh_account_usage(allow_when_locked=True)
    after = {
        "remaining": usage_after.get("remaining"),
        "used": usage_after.get("used"),
        "allowance": usage_after.get("allowance"),
    }
    credits_consumed = None
    if before.get("remaining") is not None and after.get("remaining") is not None:
        credits_consumed = int(before["remaining"]) - int(after["remaining"])

    cards = list(lane_a.get("results") or []) + list(lane_b.get("results") or [])
    # Prefer campaign cards already noted on budget (includes offline reuse)
    if budget.cards:
        by_id = {c.get("nb_id"): c for c in budget.cards if c.get("nb_id")}
        for c in cards:
            if c.get("nb_id"):
                by_id[c["nb_id"]] = c
        cards = list(by_id.values())

    queue_summary = publish_owner_queues(cards, build="campaign_250")
    # Retag queue write build via republish already done inside publish; write campaign tag file
    (get_settings().staged_inventory_root() / "owner" / "campaign_250_cards.jsonl").write_text(
        "".join(json.dumps(c, default=str) + "\n" for c in cards),
        encoding="utf-8",
    )

    newly_quote = [
        c
        for c in cards
        if c.get("supplier_quote_ready") and str(c.get("nb_id")) not in budget.pre_existing_quote_ids
    ]
    pre_existing_still = [
        c
        for c in cards
        if c.get("supplier_quote_ready") and str(c.get("nb_id")) in budget.pre_existing_quote_ids
    ]
    partial = [c for c in cards if not c.get("supplier_quote_ready")]

    shortlist = []
    for c in sorted(
        [x for x in cards if x.get("supplier_quote_ready") or (x.get("complete_line_items") or 0) >= 1],
        key=lambda x: (0 if x.get("supplier_quote_ready") else 1, str(x.get("deadline_at") or "9999")),
    )[:40]:
        lines = c.get("line_items") or []
        shortlist.append(
            {
                "nb_id": c.get("nb_id"),
                "title": c.get("title"),
                "agency": c.get("agency"),
                "solicitation_number": c.get("solicitation_number"),
                "products": "; ".join((li.get("description") or "")[:80] for li in lines[:3]) or "UNKNOWN",
                "qty_uom": "; ".join(
                    f"{li.get('quantity', 'UNKNOWN')} {li.get('unit_of_measure') or 'UNKNOWN'}" for li in lines[:3]
                )
                or "UNKNOWN",
                "deadline": c.get("deadline_at") or "UNKNOWN",
                "requirements": c.get("requirements_state") or "UNKNOWN",
                "supplier_quote": "READY" if c.get("supplier_quote_ready") else "PARTIAL/NO",
                "next_action": c.get("next_action"),
                "official_url": c.get("official_url"),
                "newly_qualified": str(c.get("nb_id")) not in budget.pre_existing_quote_ids
                and bool(c.get("supplier_quote_ready")),
            }
        )

    report = {
        "ok": error is None and acquisition_locked(),
        "error": error,
        "campaign_id": CAMPAIGN_ID,
        "job_id": job_id,
        "started_at": t0.isoformat(),
        "finished_at": _utc(),
        "usage_before": before,
        "usage_after": after,
        "credits_consumed_account": credits_consumed,
        "credits_spent_local": budget.spent,
        "lane_a_credits": budget.lane_a,
        "lane_b_credits": budget.lane_b,
        "checkpoints_hit": budget.checkpoints_hit,
        "stop_reason": budget.stop_reason,
        "metrics": budget._metrics(),
        "lane_a": {k: v for k, v in lane_a.items() if k != "results"}
        | {"result_count": len(lane_a.get("results") or [])},
        "lane_b": {k: v for k, v in lane_b.items() if k not in {"results", "screened"}}
        | {
            "result_count": len(lane_b.get("results") or []),
            "screened_count": len(lane_b.get("screened") or []),
        },
        "queue_summary": queue_summary,
        "newly_quote_ready": len(newly_quote),
        "pre_existing_quote_ready_seen": len(pre_existing_still),
        "partial_count": len(partial),
        "shortlist": shortlist,
        "acquisition_locked": acquisition_locked(),
        "authorization_revoked": True,
        "scheduler_not_activated": True,
        "reserve_intact": int(after.get("remaining") or 0) >= RESERVED_FLOOR,
        "protected_reserve_remaining": max(0, int(after.get("remaining") or 0) - RESERVED_FLOOR),
    }
    (report_dir / "CAMPAIGN_REPORT.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_lines = [
        "# Campaign 250 — Final Report",
        "",
        f"- Campaign ID: `{CAMPAIGN_ID}`",
        f"- Credits consumed (account): **{credits_consumed}**",
        f"- Lane A: {budget.lane_a} · Lane B: {budget.lane_b}",
        f"- Checkpoints: {budget.checkpoints_hit}",
        f"- Stop reason: {budget.stop_reason or 'completed'}",
        f"- Newly quote-ready: {len(newly_quote)}",
        f"- Pre-existing quote-ready still listed: {len(pre_existing_still)}",
        f"- Partial / needs info: {len(partial)}",
        f"- Killswitch locked: {acquisition_locked()}",
        f"- Scheduler activated: NO",
        "",
        "## Shortlist",
        "",
        "| Solicitation | Products | Qty/UOM | Deadline | Requirements | Supplier Quote | Next Action |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in shortlist:
        md_lines.append(
            f"| {s.get('nb_id')} / {(s.get('title') or '')[:40]} | {(s.get('products') or '')[:60]} | "
            f"{(s.get('qty_uom') or '')[:40]} | {s.get('deadline')} | {s.get('requirements')} | "
            f"{s.get('supplier_quote')} | {(s.get('next_action') or '')[:50]} |"
        )
    (report_dir / "CAMPAIGN_REPORT.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    append_ledger({"op": "campaign_250_finish", "campaign_id": CAMPAIGN_ID, "report_ok": report["ok"], **{k: report[k] for k in ("credits_consumed_account", "stop_reason", "newly_quote_ready")}})
    return report
