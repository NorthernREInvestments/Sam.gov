"""Durable M3 opportunity pipeline store — Postgres AppSetting authoritative on Railway.

Local JSON file is a cache only. Production restarts must reload from AppSetting.
"""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_lifecycle import derive_lifecycle, determine_next_action, readiness_summary
from solicitation_identity import fingerprint_record, identity_key

log = logging.getLogger("govtracker.m3_pipeline_store")

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "m3_pipeline_store.json"
CANONICAL_DURABLE_PATH = DEFAULT_PATH
PIPELINE_SETTINGS_KEY = "m3_pipeline_store_v1"
PIPELINE_DURABLE_POINTER_KEY = "m3_pipeline_durable_pointer_v1"
# AppSetting value larger than this uses file-primary durable pointer (avoids multi-MB DB round-trips)
DURABLE_INLINE_MAX_BYTES = 1_500_000



def _utc() -> str:
    return now_utc().isoformat()


def _evidence_hash(record: dict[str, Any]) -> str:
    return fingerprint_record(record)


def _payload_from_rows(rows: dict[str, dict[str, Any]], audit: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "M3PipelineStore",
        "updated_at": _utc(),
        "opportunity_count": len(rows),
        "opportunities": sorted(rows.values(), key=lambda r: r["canonical_id"]),
        "audit": audit[-500:],
    }


def _rows_from_payload(data: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in data.get("opportunities") or []:
        if isinstance(row, dict) and row.get("canonical_id"):
            rows[row["canonical_id"]] = row
    return rows, list(data.get("audit") or [])


def _read_durable_payload() -> dict[str, Any] | None:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            # Prefer file-primary pointer for large stores
            ptr_row = db.query(AppSetting).filter(AppSetting.key == PIPELINE_DURABLE_POINTER_KEY).one_or_none()
            if ptr_row and ptr_row.value:
                ptr = json.loads(ptr_row.value) if isinstance(ptr_row.value, str) else ptr_row.value
                if isinstance(ptr, dict) and ptr.get("mode") == "FILE_PRIMARY":
                    path = Path(str(ptr.get("path") or DEFAULT_PATH))
                    if not path.is_absolute():
                        path = Path(__file__).resolve().parent / path
                    if path.exists():
                        data = json.loads(path.read_text(encoding="utf-8"))
                        return data if isinstance(data, dict) else None
            row = db.query(AppSetting).filter(AppSetting.key == PIPELINE_SETTINGS_KEY).one_or_none()
            if not row or not row.value:
                return None
            data = json.loads(row.value) if isinstance(row.value, str) else row.value
            return data if isinstance(data, dict) else None
        finally:
            db.close()
    except Exception:
        log.exception("Failed reading durable M3 pipeline from AppSetting")
        return None


def _prefer_row(local: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    """Merge two opportunity rows without dropping discovery or research progress."""
    if not remote:
        return local
    if not local:
        return remote
    out = dict(remote)
    out.update({k: v for k, v in local.items() if v is not None})
    # Prefer non-empty collections / richer evidence
    for key in ("documents", "line_items", "bom", "source_references", "recovered_evidence", "operator_actions"):
        loc_v = local.get(key)
        rem_v = remote.get(key)
        if isinstance(loc_v, list) and isinstance(rem_v, list):
            out[key] = loc_v if len(loc_v) >= len(rem_v) else rem_v
        elif loc_v and not rem_v:
            out[key] = loc_v
        elif rem_v and not loc_v:
            out[key] = rem_v
    # Prefer richer intelligence packages (never drop research already computed)
    for key in (
        "commercial_intelligence",
        "supplier_intelligence",
        "supplier_price_research",
        "commercial_pricing",
        "public_pricing",
    ):
        loc_v = local.get(key)
        rem_v = remote.get(key)
        if isinstance(loc_v, dict) and loc_v and not (isinstance(rem_v, dict) and rem_v):
            out[key] = loc_v
        elif isinstance(rem_v, dict) and rem_v and not (isinstance(loc_v, dict) and loc_v):
            out[key] = rem_v
        elif isinstance(loc_v, dict) and isinstance(rem_v, dict) and loc_v and rem_v:
            # Keep local if it has newer generated_at / more keys
            loc_at = str(loc_v.get("generated_at") or "")
            rem_at = str(rem_v.get("generated_at") or "")
            out[key] = loc_v if loc_at >= rem_at or len(loc_v) >= len(rem_v) else rem_v
    # Lifecycle: don't regress from researched/advanced back to discovered-only
    loc_lc = str(local.get("lifecycle") or "")
    rem_lc = str(remote.get("lifecycle") or "")
    early = {"DISCOVERED", "CHEAP_SCREENED", "RESEARCH_QUEUED", "", "None"}
    if loc_lc not in early and rem_lc in early:
        out["lifecycle"] = loc_lc
        for k in ("research_queued", "research_in_progress", "stop_reason", "pending_next_action", "package_access"):
            if local.get(k) is not None:
                out[k] = local[k]
    elif rem_lc not in early and loc_lc in early:
        out["lifecycle"] = rem_lc
    # Timestamps: keep latest last_seen
    if str(local.get("last_seen_at") or "") >= str(remote.get("last_seen_at") or ""):
        out["last_seen_at"] = local.get("last_seen_at") or remote.get("last_seen_at")
    return out


def _write_durable_payload(payload: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        raw = json.dumps(payload, separators=(",", ":"), default=str)
        path = DEFAULT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        count = len(payload.get("opportunities") or [])
        use_pointer = len(raw.encode("utf-8")) > DURABLE_INLINE_MAX_BYTES

        db = SessionLocal()
        try:
            if use_pointer:
                ptr = {
                    "kind": "M3PipelineDurablePointer",
                    "mode": "FILE_PRIMARY",
                    "path": str(path),
                    "count": count,
                    "sha256": sha,
                    "updated_at": _utc(),
                }
                ptr_raw = json.dumps(ptr, default=str)
                prow = db.query(AppSetting).filter(AppSetting.key == PIPELINE_DURABLE_POINTER_KEY).one_or_none()
                if prow:
                    prow.value = ptr_raw
                else:
                    db.add(AppSetting(key=PIPELINE_DURABLE_POINTER_KEY, value=ptr_raw))
                stub = {
                    "kind": "M3PipelineStore",
                    "mode": "FILE_PRIMARY",
                    "count": count,
                    "sha256": sha,
                    "opportunities": [],
                    "updated_at": _utc(),
                }
                stub_raw = json.dumps(stub, default=str)
                row = db.query(AppSetting).filter(AppSetting.key == PIPELINE_SETTINGS_KEY).one_or_none()
                if row:
                    row.value = stub_raw
                else:
                    db.add(AppSetting(key=PIPELINE_SETTINGS_KEY, value=stub_raw))
            else:
                row = db.query(AppSetting).filter(AppSetting.key == PIPELINE_SETTINGS_KEY).one_or_none()
                if row:
                    row.value = raw
                else:
                    db.add(AppSetting(key=PIPELINE_SETTINGS_KEY, value=raw))
                prow = db.query(AppSetting).filter(AppSetting.key == PIPELINE_DURABLE_POINTER_KEY).one_or_none()
                if prow:
                    prow.value = json.dumps({"mode": "INLINE", "count": count, "updated_at": _utc()})
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("Failed writing durable M3 pipeline to AppSetting")
        return False


class M3PipelineStore:
    """Canonical opportunity pipeline — AppSetting durable, file is local cache."""

    def __init__(self, path: Path | None = None, *, durable: bool | None = None) -> None:
        self.path = path or DEFAULT_PATH
        # Production canonical path ⇒ AppSetting durable. Test tmp paths stay file-only.
        if durable is not None:
            self.durable = bool(durable)
        else:
            try:
                self.durable = self.path.resolve() == CANONICAL_DURABLE_PATH.resolve()
            except Exception:
                self.durable = path is None
        self._rows: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []
        self._load()

    def _apply_payload(self, data: dict[str, Any]) -> None:
        self._rows, self._audit = _rows_from_payload(data)

    def _load_file_payload(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _load(self) -> None:
        file_data = self._load_file_payload()
        db_data = _read_durable_payload() if self.durable else None

        file_count = len((file_data or {}).get("opportunities") or [])
        db_count = len((db_data or {}).get("opportunities") or [])

        # Prefer durable DB whenever it has equal/more opportunities (survives Railway restart)
        chosen: dict[str, Any] | None = None
        if db_data and db_count >= file_count and db_count > 0:
            chosen = db_data
        elif file_data and file_count > 0:
            chosen = file_data
        elif db_data and db_count > 0:
            chosen = db_data
        elif file_data:
            chosen = file_data

        if chosen:
            self._apply_payload(chosen)
            # Refresh ephemeral file cache from durable when DB won
            if self.durable and chosen is db_data:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_text(json.dumps(chosen, indent=2, default=str), encoding="utf-8")
                except Exception:
                    pass

    def save(self, *, durable_write: bool = True, skip_remote_merge: bool = False) -> Path:
        # Merge durable DB rows before write so concurrent research/discovery workers
        # cannot clobber opportunities the other worker just upserted.
        if self.durable and durable_write and not skip_remote_merge:
            try:
                db_data = _read_durable_payload()
                if db_data:
                    db_rows, db_audit = _rows_from_payload(db_data)
                    for cid, remote in db_rows.items():
                        local = self._rows.get(cid)
                        self._rows[cid] = _prefer_row(local, remote) if local else remote
                    if db_audit:
                        # Keep union of recent audit events
                        seen = {(a.get("at"), a.get("canonical_id"), a.get("event")) for a in self._audit if isinstance(a, dict)}
                        for a in db_audit:
                            key = (a.get("at"), a.get("canonical_id"), a.get("event")) if isinstance(a, dict) else None
                            if key and key not in seen:
                                self._audit.append(a)
            except Exception:
                log.exception("Durable merge-before-save failed; continuing with local rows")

        payload = _payload_from_rows(self._rows, self._audit)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if durable_write:
            self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        else:
            # Fast mid-batch checkpoint — compact JSON, no durable DB round-trip
            self.path.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")
        if self.durable and durable_write:
            ok = _write_durable_payload(payload)
            if not ok:
                log.error(
                    "Durable pipeline save failed or verify mismatch — file cache has %s opps",
                    len(self._rows),
                )
        return self.path

    def reload_from_durable(self) -> int:
        """Force reload from AppSetting (authoritative). Returns opportunity count."""
        if not self.durable:
            self._load()
            return len(self._rows)
        db_data = _read_durable_payload()
        if db_data:
            self._apply_payload(db_data)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(db_data, indent=2, default=str), encoding="utf-8")
            except Exception:
                pass
        else:
            self._load()
        return len(self._rows)

    def canonical_id_for(self, record: dict[str, Any]) -> str:
        return identity_key(record)

    def get(self, canonical_id: str) -> dict[str, Any] | None:
        row = self._rows.get(canonical_id)
        return deepcopy(row) if row else None

    def all(self) -> list[dict[str, Any]]:
        return [deepcopy(r) for r in sorted(self._rows.values(), key=lambda x: x["canonical_id"])]

    def upsert_from_discovery(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Idempotent ingest. Returns (row, created_new). Preserves evidence trail."""
        from m3_evidence_chain import (
            enrich_discovery_record_for_pipeline,
            ensure_evidence_chain_on_row,
            merge_preserve_evidence,
        )

        record = enrich_discovery_record_for_pipeline(
            record, discovery_run_id=record.get("discovery_run_id")
        )
        cid = self.canonical_id_for(record)
        existing = self._rows.get(cid)
        fp = _evidence_hash(record)
        if existing and existing.get("evidence_fingerprint") == fp:
            # unchanged — no duplicate work marker; still refresh evidence chain visibility
            existing["last_seen_at"] = _utc()
            refs = list(existing.get("source_references") or [])
            src = record.get("source_id")
            if src and not any(r.get("source_id") == src for r in refs):
                refs.append({"source_id": src, "url": record.get("detail_url")})
                existing["source_references"] = refs
            existing = merge_preserve_evidence(existing, record)
            existing = ensure_evidence_chain_on_row(existing)
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
                "raw_metadata": record.get("raw_metadata"),
                "discovery_evidence": record.get("discovery_evidence"),
                "discovery_run_id": record.get("discovery_run_id"),
                "discovered_at": record.get("discovered_at"),
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
            row = ensure_evidence_chain_on_row(row)
            row["lifecycle"] = derive_lifecycle(row)
            row["pending_next_action"] = determine_next_action(row)
            self._audit_event(cid, None, row["lifecycle"], "discovery_ingest", automated=True)
            self._rows[cid] = row
            return deepcopy(row), True

        # Material update — merge without regressing completed lifecycle unless invalidated
        # and WITHOUT discarding source URLs / document refs / raw metadata
        prev_lc = existing.get("lifecycle")
        existing = merge_preserve_evidence(existing, record)
        existing["last_seen_at"] = _utc()
        existing["evidence_fingerprint"] = fp
        for field in (
            "title",
            "agency",
            "deadline",
            "status",
            "description",
            "package_access",
            "product_classification",
            "product_category",
        ):
            if record.get(field) is not None:
                existing[field] = record[field]
        # Prefer incoming line_items only when present (never wipe with empty)
        if record.get("line_items") or record.get("bom"):
            existing["line_items"] = record.get("line_items") or record.get("bom")
        existing = ensure_evidence_chain_on_row(existing)
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
