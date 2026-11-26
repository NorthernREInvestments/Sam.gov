"""Demote expired opportunities from actionable owner queues. Never deletes evidence."""

from __future__ import annotations

import json
from typing import Any

from m3.research.commercial_criteria import evaluate_deadline_gate
from m3.research.owner_queues import QUEUE_KEYS, queues_root, write_queues


ACTIONABLE = {
    "verified_bid_opportunities",
    "verified_procurement_fits",
    "qualified_commercial_evaluation",
    "bids_ready_for_owner_approval",
    "awaiting_supplier_quotations",
    "promising_awaiting_verification",
    "verified_product_information_needed",
}


def demote_expired_from_owner_queues() -> dict[str, Any]:
    root = queues_root()
    queues: dict[str, list] = {k: [] for k in QUEUE_KEYS}
    moved: list[dict[str, Any]] = []
    kept = 0

    for qname in QUEUE_KEYS:
        path = root / f"{qname}.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = list(payload.get("rows") or [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            dl = evaluate_deadline_gate(row)
            if dl.get("expired") and qname in ACTIONABLE:
                hist = dict(row)
                hist["disposition"] = "closed_expired_non_solicitation"
                hist["unresolved"] = True
                hist["demoted_reason"] = dl.get("evidence")
                hist["open_status_label"] = dl.get("label")
                queues["closed_expired_non_solicitation"].append(hist)
                queues["archived_or_closed"].append(dict(hist))
                moved.append(
                    {
                        "nb_id": hist.get("nb_id") or hist.get("external_key"),
                        "from_queue": qname,
                        "deadline": dl.get("deadline_at"),
                        "evidence": dl.get("evidence"),
                    }
                )
            else:
                queues[qname].append(row)
                kept += 1

    summary = write_queues(queues, build="deadline-repair")
    return {
        "demoted_expired": len(moved),
        "kept": kept,
        "moved": moved[:100],
        "queue_summary": summary,
        "evidence_deleted": False,
    }
