"""National high-volume discovery funnel — cheap screen → progressive research backlog."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from discovery.classify import classify_discovery_opportunity
from national_discovery_constants import (
    CHG_NEW,
    CHG_UNCHANGED,
    INV_CHEAP_SCREENING,
    INV_REJECTED,
    INV_RESEARCH_QUEUED,
    STAGE_0,
    STAGE_1,
    STAGE_2,
    STAGE_3,
    STAGE_4,
    STAGE_5,
)
from solicitation_identity import SolicitationInventory, should_skip_unchanged

_OBVIOUS_REJECT = re.compile(
    r"\b(architectural\s+and\s+engineering|a/?e\s+services|professional\s+consulting|"
    r"staffing\s+services|janitorial\s+services|custodial|software\s+development|"
    r"custom\s+software|training\s+services|feasibility\s+study|grant\s+program|"
    r"real\s+estate\s+lease|employment\s+opportunity|job\s+opening|"
    r"grounds?\s+maintenance\s+services|master\s+agreement|indefinite\s+delivery|"
    r"cooperative\s+contract\s+vehicle|award\s+notice|intent\s+to\s+award)\b",
    re.I,
)
_PRODUCT_HINT = re.compile(
    r"\b(equipment|supplies|hardware|parts?|materials?|vehicles?|seed|computers?|"
    r"monitors?|furniture|tools?|pump|motor|tank|lift|blade|filter|cable|hose)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def stage1_ultra_cheap(record: dict[str, Any]) -> dict[str, Any]:
    title = str(record.get("title") or "")
    desc = str(record.get("description") or title)
    status = str(record.get("status") or record.get("deadline_status") or "").upper()
    if status in {"EXPIRED", "CANCELLED", "CANCELED", "AWARDED", "CLOSED"}:
        return {"survive": False, "reason": f"status_{status.lower()}", "stage": STAGE_1}
    if _OBVIOUS_REJECT.search(title) or _OBVIOUS_REJECT.search(desc[:500]):
        # Ambiguous product+service titles: preserve if strong product hint
        if _PRODUCT_HINT.search(title) and not re.search(
            r"\b(architectural|consulting|staffing|software\s+development|grant)\b", title, re.I
        ):
            return {"survive": True, "reason": "ambiguous_product_preserved", "stage": STAGE_1}
        return {"survive": False, "reason": "obvious_non_product", "stage": STAGE_1}
    classification = classify_discovery_opportunity(title=title, description=desc)
    klass = classification.get("classification")
    if klass in {"SERVICE", "CLEARLY_IRRELEVANT"}:
        return {"survive": False, "reason": f"class_{klass}", "stage": STAGE_1, "classification": klass}
    return {
        "survive": True,
        "reason": "plausible_tangible_or_unknown",
        "stage": STAGE_1,
        "classification": klass,
    }


def stage2_transaction_fit(record: dict[str, Any]) -> dict[str, Any]:
    title = str(record.get("title") or "")
    desc = str(record.get("description") or "")
    blob = f"{title} {desc}".lower()
    installation = bool(re.search(r"\b(install(?:ation)?|construction|surfacing)\b", blob))
    service_heavy = bool(re.search(r"\b(professional\s+services|consulting|staffing)\b", blob))
    productish = bool(_PRODUCT_HINT.search(blob)) or record.get("classification") in {
        "CORE_PRODUCT",
        "PRODUCT_PLUS_SERVICE",
        "UNKNOWN",
    }
    if service_heavy and not productish:
        return {"survive": False, "reason": "service_heavy", "stage": STAGE_2}
    if installation and not productish:
        return {"survive": False, "reason": "construction_without_product", "stage": STAGE_2}
    return {
        "survive": True,
        "reason": "transaction_fit_plausible",
        "stage": STAGE_2,
        "installation_burden": installation,
        "productish": productish,
    }


class ResearchBacklog:
    """Persistent progressive research queue — NO silent discard, NO result cap."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    def enqueue(self, identity_key: str, *, priority: float, stage: str, reason: str, record: dict[str, Any]) -> dict[str, Any]:
        existing = self._items.get(identity_key)
        if existing and existing.get("status") not in {"COMPLETED", "REJECTED"}:
            # Upgrade priority if stronger; never drop
            if priority > float(existing.get("priority") or 0):
                existing["priority"] = priority
                existing["reason"] = reason
            return existing
        item = {
            "queue_id": f"RQ-{uuid4().hex[:10]}",
            "identity_key": identity_key,
            "queued_at": _utc(),
            "priority": priority,
            "stage": stage,
            "reason": reason,
            "research_attempts": 0,
            "next_eligible_research_time": _utc(),
            "deadline": record.get("deadline") or record.get("response_deadline"),
            "status": "QUEUED",
            "title": record.get("title"),
            "solicitation_number": record.get("solicitation_number") or record.get("external_id"),
            "source_id": record.get("source_id"),
        }
        self._items[identity_key] = item
        return item

    def pop_batch(self, n: int) -> list[dict[str, Any]]:
        """Deadline-aware: urgent high-priority first; does not delete remainder."""
        ready = [i for i in self._items.values() if i.get("status") == "QUEUED"]

        def sort_key(i: dict[str, Any]) -> tuple:
            dl = str(i.get("deadline") or "9999")
            return (-float(i.get("priority") or 0), dl)

        ready.sort(key=sort_key)
        batch = ready[:n]
        for i in batch:
            i["status"] = "IN_RESEARCH"
            i["research_attempts"] = int(i.get("research_attempts") or 0) + 1
        return batch

    def complete(self, identity_key: str, *, reject: bool = False) -> None:
        item = self._items.get(identity_key)
        if item:
            item["status"] = "REJECTED" if reject else "COMPLETED"

    def queued(self) -> list[dict[str, Any]]:
        return [i for i in self._items.values() if i.get("status") in {"QUEUED", "IN_RESEARCH"}]

    def __len__(self) -> int:
        return len([i for i in self._items.values() if i.get("status") == "QUEUED"])


class NationalDiscoveryFunnel:
    def __init__(self, inventory: SolicitationInventory | None = None) -> None:
        self.inventory = inventory or SolicitationInventory()
        self.backlog = ResearchBacklog()
        self.survivors: dict[str, dict[str, Any]] = {}  # ALL survivors retained
        self.metrics = {
            "input_records": 0,
            "stage0_unique": 0,
            "unchanged_skipped": 0,
            "duplicates_merged": 0,
            "stage1_survivors": 0,
            "stage1_rejected": 0,
            "stage2_survivors": 0,
            "stage2_rejected": 0,
            "research_queued": 0,
            "new_records": 0,
            "changed_records": 0,
        }

    def ingest_batch(self, records: list[dict[str, Any]], *, deep_research_budget: int = 50) -> dict[str, Any]:
        """
        Process arbitrarily large batches. deep_research_budget only limits *immediate*
        expensive research — all Stage-2 survivors remain queued.
        """
        self.metrics["input_records"] += len(records)
        for rec in records:
            ident, change = self.inventory.upsert(rec)
            key = ident["identity_key"]
            if change == CHG_UNCHANGED and should_skip_unchanged(ident, change):
                self.metrics["unchanged_skipped"] += 1
                continue
            if change == CHG_NEW:
                self.metrics["new_records"] += 1
            elif change != CHG_UNCHANGED:
                self.metrics["changed_records"] += 1
            if len(ident.get("source_references") or []) > 1:
                self.metrics["duplicates_merged"] += 1

            self.metrics["stage0_unique"] = len(self.inventory)
            s1 = stage1_ultra_cheap({**rec, **ident})
            if not s1["survive"]:
                self.metrics["stage1_rejected"] += 1
                continue
            self.metrics["stage1_survivors"] += 1

            s2 = stage2_transaction_fit({**rec, **ident, "classification": s1.get("classification")})
            if not s2["survive"]:
                self.metrics["stage2_rejected"] += 1
                continue
            self.metrics["stage2_survivors"] += 1

            # Retain forever in survivors map — ranking never deletes
            priority = rough_priority(rec, s1, s2)
            self.survivors[key] = {
                "identity": ident,
                "record": {k: rec.get(k) for k in rec if k not in {"raw_html", "body"}},
                "stage1": s1,
                "stage2": s2,
                "change_state": change,
                "inventory_state": INV_RESEARCH_QUEUED,
                "priority": priority,
            }
            self.backlog.enqueue(
                key,
                priority=priority,
                stage=STAGE_3,
                reason="stage2_survivor",
                record=rec,
            )
            self.metrics["research_queued"] = len(self.backlog)
            self.inventory.mark_processed(key)

        immediate = self.backlog.pop_batch(deep_research_budget)
        return {
            "metrics": dict(self.metrics),
            "survivor_count": len(self.survivors),
            "backlog_queued": len(self.backlog),
            "immediate_research_batch": len(immediate),
            "immediate_keys": [i["identity_key"] for i in immediate],
            "no_result_cap": True,
            "note": "All Stage-2 survivors retained; deep research is progressive only",
        }


def rough_priority(record: dict[str, Any], s1: dict[str, Any], s2: dict[str, Any]) -> float:
    score = 50.0
    if s1.get("classification") == "CORE_PRODUCT":
        score += 20
    if s2.get("productish"):
        score += 10
    if s2.get("installation_burden"):
        score -= 15
    title = str(record.get("title") or "")
    if _PRODUCT_HINT.search(title):
        score += 5
    # Deadline urgency bump without discarding long-deadline deals
    return score
