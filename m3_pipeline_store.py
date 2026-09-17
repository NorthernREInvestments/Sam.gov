"""Durable M3 opportunity pipeline store — restart-safe, idempotent."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_lifecycle import derive_lifecycle, determine_next_action, readiness_summary
from solicitation_identity import fingerprint_record, identity_key

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "m3_pipeline_store.json"


def _utc() -> str:
    return now_utc().isoformat()


def _evidence_hash(record: dict[str, Any]) -> str:
    return fingerprint_record(record)


class M3PipelineStore:
    """JSON-backed durable store for canonical opportunity pipeline state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self._rows: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for row in data.get("opportunities") or []:
                if isinstance(row, dict) and row.get("canonical_id"):
                    self._rows[row["canonical_id"]] = row
            self._audit = list(data.get("audit") or [])

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "M3PipelineStore",
            "updated_at": _utc(),
            "opportunity_count": len(self._rows),
            "opportunities": sorted(self._rows.values(), key=lambda r: r["canonical_id"]),
            "audit": self._audit[-500:],
        }
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return self.path

    def canonical_id_for(self, record: dict[str, Any]) -> str:
        return identity_key(record)

    def get(self, canonical_id: str) -> dict[str, Any] | None:
        row = self._rows.get(canonical_id)
        return deepcopy(row) if row else None

    def all(self) -> list[dict[str, Any]]:
        return [deepcopy(r) for r in sorted(self._rows.values(), key=lambda x: x["canonical_id"])]

    def upsert_from_discovery(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Idempotent ingest. Returns (row, created_new)."""
        cid = self.canonical_id_for(record)
        existing = self._rows.get(cid)
        fp = _evidence_hash(record)
        if existing and existing.get("evidence_fingerprint") == fp:
            # unchanged — no duplicate work marker
            existing["last_seen_at"] = _utc()
            refs = list(existing.get("source_references") or [])
            src = record.get("source_id")
            if src and not any(r.get("source_id") == src for r in refs):
                refs.append({"source_id": src, "url": record.get("detail_url")})
                existing["source_references"] = refs
            self._rows[cid] = existing
            return deepcopy(existing), False

        if existing is None:
            row = {
                "canonical_id": cid,
                "created_at": _utc(),
                "title": record.get("title"),
                "agency": record.get("agency") or record.get("buyer"),
                "solicitation_number": record.get("solicitation_number") or record.get("external_id"),
                "source_id": record.get("source_id"),
                "detail_url": record.get("detail_url"),
                "deadline": record.get("deadline") or record.get("deadline_raw") or record.get("response_deadline"),
                "status": record.get("status") or "OPEN",
                "description": record.get("description"),
                "product_classification": record.get("product_classification"),
                "package_access": record.get("package_access") or record.get("document_access"),
                "source_references": [
                    {"source_id": record.get("source_id"), "url": record.get("detail_url")}
                ]
                if record.get("source_id")
                else [],
                "pipeline_stage": None,
                "operator_readiness": None,
                "stop_reason": None,
                "research_queued": False,
                "research_in_progress": False,
                "paid_research_charged": False,
                "paid_research_charge_id": None,
                "line_items": record.get("line_items") or record.get("bom"),
                "documents": record.get("documents"),
                "subsystem": {},
                "pending_next_action": None,
                "invalidation": {},
                "operator_actions": [],
                "evidence_fingerprint": fp,
                "last_seen_at": _utc(),
                "normalized": True,
                "external_id": record.get("external_id"),
                "deal_id": record.get("deal_id") or cid,
            }
            for k, v in record.items():
                if k not in row and v is not None:
                    row.setdefault(k, v)
            row["lifecycle"] = derive_lifecycle(row)
            row["pending_next_action"] = determine_next_action(row)
            self._audit_event(cid, None, row["lifecycle"], "discovery_ingest", automated=True)
            self._rows[cid] = row
            return deepcopy(row), True

        # Material update — merge without regressing completed lifecycle unless invalidated
        prev_lc = existing.get("lifecycle")
        existing["last_seen_at"] = _utc()
        existing["evidence_fingerprint"] = fp
        for field in (
            "title",
            "agency",
            "deadline",
            "status",
            "description",
            "detail_url",
            "package_access",
            "product_classification",
            "line_items",
            "documents",
            "product_category",
        ):
            if record.get(field) is not None:
                existing[field] = record[field]
        existing["lifecycle"] = derive_lifecycle(existing)
        existing["pending_next_action"] = determine_next_action(existing)
        if existing["lifecycle"] != prev_lc:
            self._audit_event(cid, prev_lc, existing["lifecycle"], "evidence_update", automated=True)
        self._rows[cid] = existing
        return deepcopy(existing), False

    def apply_pipeline_result(self, canonical_id: str, result: dict[str, Any]) -> dict[str, Any]:
        row = self._rows.get(canonical_id)
        if not row:
            raise KeyError(canonical_id)
        prev = row.get("lifecycle")
        deal = result.get("deal") or {}
        for k in (
            "pipeline_stage",
            "operator_readiness",
            "stop_reason",
            "line_items",
            "bom",
            "economics",
            "transaction_economics",
            "funding_status",
            "funding_requirement",
            "compliance",
            "compliance_blockers",
            "expected_actual_profit",
            "working_capital_required",
            "deadline_evaluation",
            "package_acquired",
            "auth_required_for_spec",
            "requirements_insufficient",
            "rejected",
            "deal_type",
            "fit",
            "product_classification",
            "product_category",
        ):
            if deal.get(k) is not None:
                row[k] = deal[k]
            elif result.get(k) is not None and k in {
                "pipeline_stage",
                "operator_readiness",
                "stop_reason",
            }:
                row[k] = result[k]
        if result.get("actions"):
            # merge operator actions by action_id
            existing_ids = {a.get("action_id") for a in row.get("operator_actions") or []}
            for a in result["actions"]:
                if a.get("action_id") not in existing_ids:
                    row.setdefault("operator_actions", []).append(a)
        row["subsystem"] = {
            **(row.get("subsystem") or {}),
            "executable_pipeline": {
                "stage": result.get("stage"),
                "stages_run": result.get("stages_run"),
                "metrics": result.get("metrics"),
            },
        }
        for extra in (
            "bid_compliance",
            "bid_pricing",
            "commercial_verification_plan",
            "draft_bid_package",
            "draft_bid_ready",
            "ready_for_operator",
            "package_access",
            "research_queued",
            "research_in_progress",
            "cheap_screen_survive",
            "economics_supported",
        ):
            if result.get(extra) is not None:
                row[extra] = result[extra]
            if deal.get(extra) is not None:
                row[extra] = deal[extra]
        row["lifecycle"] = derive_lifecycle(row)
        row["pending_next_action"] = determine_next_action(row)
        row["readiness_summary"] = readiness_summary(row)
        row["updated_at"] = _utc()
        self._audit_event(
            canonical_id,
            prev,
            row["lifecycle"],
            result.get("stop_reason") or "pipeline_advance",
            automated=True,
            evidence={"stage": result.get("stage")},
        )
        self._rows[canonical_id] = row
        return deepcopy(row)

    def mark_paid_research(self, canonical_id: str, charge_id: str) -> None:
        row = self._rows[canonical_id]
        if row.get("paid_research_charged") and row.get("paid_research_charge_id") == charge_id:
            return  # idempotent — no duplicate charge
        row["paid_research_charged"] = True
        row["paid_research_charge_id"] = charge_id

    def invalidate(self, canonical_id: str, *, change_type: str, affected: list[str]) -> dict[str, Any]:
        row = self._rows[canonical_id]
        prev = row.get("lifecycle")
        inv = dict(row.get("invalidation") or {})
        inv["last_change"] = change_type
        inv["affected"] = affected
        inv["at"] = _utc()
        row["invalidation"] = inv
        # Targeted clears
        if "bom" in affected or "quantity" in affected:
            row.pop("transaction_economics", None)
            row.pop("economics", None)
            row.pop("expected_actual_profit", None)
            row.pop("bid_pricing", None)
            row.pop("funding_requirement", None)
            row["economics_supported"] = False
        if "spec" in affected:
            row.pop("product_match", None)
            row.pop("bid_compliance", None)
        if "deadline" in affected:
            row.pop("deadline_evaluation", None)
        if change_type in {"CANCELLED", "AWARDED", "CLOSED"}:
            row["status"] = change_type
            row["research_in_progress"] = False
            row["research_queued"] = False
        row["lifecycle"] = derive_lifecycle(row)
        row["pending_next_action"] = determine_next_action(row)
        self._audit_event(canonical_id, prev, row["lifecycle"], f"invalidation:{change_type}", automated=True)
        self._rows[canonical_id] = row
        return deepcopy(row)

    def operator_queue(self) -> list[dict[str, Any]]:
        out = []
        for row in self.all():
            nxt = row.get("pending_next_action") or determine_next_action(row)
            if nxt.get("next_action") in {
                "WAIT_OPERATOR",
                "WAIT_COMMERCIAL_VERIFICATION",
                "WAIT_FUNDING_VERIFICATION",
                "WAIT_COMPLIANCE_RESOLUTION",
                "READY_FOR_OPERATOR_ACTION",
            }:
                out.append(
                    {
                        "canonical_id": row["canonical_id"],
                        "title": row.get("title"),
                        "buyer": row.get("agency"),
                        "deadline": row.get("deadline"),
                        "lifecycle": row.get("lifecycle"),
                        "next_action": nxt,
                        "operator_actions": row.get("operator_actions") or [],
                        "priority": 10 if "FUNDING" in str(nxt.get("next_action")) else 40,
                    }
                )
            for a in row.get("operator_actions") or []:
                if a.get("status") == "OPEN":
                    out.append({**a, "canonical_id": row["canonical_id"], "from_action_queue": True})
        out.sort(key=lambda x: (x.get("priority") or 50, str(x.get("deadline") or "9999")))
        return out

    def _audit_event(
        self,
        canonical_id: str,
        previous: str | None,
        new: str,
        reason: str,
        *,
        automated: bool = True,
        evidence: dict[str, Any] | None = None,
        cost: float | None = None,
    ) -> None:
        self._audit.append(
            {
                "canonical_id": canonical_id,
                "previous_state": previous,
                "new_state": new,
                "reason": reason,
                "trigger": "automated" if automated else "operator",
                "evidence": evidence or {},
                "timestamp": _utc(),
                "cost": cost,
            }
        )

    def audit_for(self, canonical_id: str) -> list[dict[str, Any]]:
        return [a for a in self._audit if a.get("canonical_id") == canonical_id]
