"""Transaction learning foundation — structured business memory (not ML)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from operating_mode import is_controlled_verification, is_development_no_outreach, mode_snapshot

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "transaction_learning_store.json"

QUOTE_FRESHNESS_DAYS_DEFAULT = 14


def _utc() -> str:
    return now_utc().isoformat()


def _empty_record(canonical_id: str, **seed: Any) -> dict[str, Any]:
    return {
        "kind": "M3TransactionLearningRecord",
        "record_id": f"TL-{uuid4().hex[:10]}",
        "canonical_id": canonical_id,
        "created_at": _utc(),
        "updated_at": _utc(),
        "discovery": {
            "source": seed.get("source") or "UNKNOWN",
            "platform": seed.get("platform") or "UNKNOWN",
            "buyer": seed.get("buyer") or "UNKNOWN",
            "category": seed.get("category") or "UNKNOWN",
            "why_discovered": seed.get("why_discovered") or "UNKNOWN",
        },
        "qualification": {
            "why_pursued": seed.get("why_pursued") or "UNKNOWN",
            "why_rejected": seed.get("why_rejected"),
            "evidence": seed.get("qualification_evidence") or [],
        },
        "pricing": {
            "estimated_acquisition_cost": seed.get("estimated_acquisition_cost", "UNKNOWN"),
            "actual_acquisition_cost": "UNKNOWN",
            "variance": "UNKNOWN",
        },
        "freight": {
            "estimated_freight": seed.get("estimated_freight", "UNKNOWN"),
            "actual_freight": "UNKNOWN",
            "variance": "UNKNOWN",
        },
        "financing": {
            "funding_path": "UNKNOWN",
            "result": "UNKNOWN",
            "terms": "UNKNOWN",
            "timeline": "UNKNOWN",
            "state": "UNKNOWN",  # UNKNOWN | COMPATIBLE | INCOMPATIBLE | VERIFIED
            "unknown_is_not_rejection": True,
        },
        "execution": {
            "problems_encountered": [],
            "delays": [],
            "missing_information": [],
        },
        "outcome": {
            "status": "IN_PROGRESS",  # IN_PROGRESS | WON | LOST | ABANDONED | NOT_BID
            "won_lost": "UNKNOWN",
            "revenue": "UNKNOWN",
            "actual_profit": "UNKNOWN",
            "timeline": "UNKNOWN",
        },
        "supplier_verifications": [],
        "financing_verifications": [],
        "operator_authorizations": [],
        "DEVELOPMENT_NO_OUTREACH_at_create": is_development_no_outreach(),
        "controlled_at_create": is_controlled_verification(),
    }


def _variance(est: Any, act: Any) -> Any:
    try:
        e = float(est)
        a = float(act)
        return round(a - e, 2)
    except (TypeError, ValueError):
        return "UNKNOWN"


class TransactionLearningStore:
    """Durable structured memory for pursued opportunities."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for r in data.get("records") or []:
                if isinstance(r, dict) and r.get("record_id"):
                    self._records[r["record_id"]] = r

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "M3TransactionLearningStore",
            "updated_at": _utc(),
            "record_count": len(self._records),
            "records": sorted(self._records.values(), key=lambda r: r.get("created_at") or ""),
        }
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return self.path

    def start_pursuit(self, canonical_id: str, **seed: Any) -> dict[str, Any]:
        for r in self._records.values():
            if r.get("canonical_id") == canonical_id and (r.get("outcome") or {}).get("status") == "IN_PROGRESS":
                return deepcopy(r)
        rec = _empty_record(canonical_id, **seed)
        self._records[rec["record_id"]] = rec
        self.save()
        return deepcopy(rec)

    def get(self, record_id: str) -> dict[str, Any] | None:
        r = self._records.get(record_id)
        return deepcopy(r) if r else None

    def by_opportunity(self, canonical_id: str) -> list[dict[str, Any]]:
        return [deepcopy(r) for r in self._records.values() if r.get("canonical_id") == canonical_id]

    def all(self) -> list[dict[str, Any]]:
        return [deepcopy(r) for r in sorted(self._records.values(), key=lambda x: x.get("created_at") or "")]

    def update_section(self, record_id: str, section: str, patch: dict[str, Any]) -> dict[str, Any]:
        rec = self._records.get(record_id)
        if not rec:
            return {"error": "not_found"}
        if section not in rec or not isinstance(rec[section], dict):
            return {"error": "invalid_section"}
        rec[section].update(patch or {})
        rec["updated_at"] = _utc()
        # auto variance
        if section == "pricing":
            rec["pricing"]["variance"] = _variance(
                rec["pricing"].get("estimated_acquisition_cost"),
                rec["pricing"].get("actual_acquisition_cost"),
            )
        if section == "freight":
            rec["freight"]["variance"] = _variance(
                rec["freight"].get("estimated_freight"),
                rec["freight"].get("actual_freight"),
            )
        self.save()
        return deepcopy(rec)

    def record_supplier_verification(
        self,
        record_id: str,
        *,
        supplier: str,
        product: str,
        quote_date: str | None = None,
        price: Any = "UNKNOWN",
        availability: str = "UNKNOWN",
        lead_time: str = "UNKNOWN",
        warranty: str = "UNKNOWN",
        notes: str | None = None,
        authorized_by: str | None = None,
        freshness_days: int = QUOTE_FRESHNESS_DAYS_DEFAULT,
    ) -> dict[str, Any]:
        rec = self._records.get(record_id)
        if not rec:
            return {"error": "not_found"}
        if not authorized_by:
            return {"error": "authorization_required", "message": "Supplier verification requires operator authorization"}
        if is_development_no_outreach() and not is_controlled_verification():
            return {
                "error": "blocked_by_operating_mode",
                "message": "Supplier verification recording requires CONTROLLED_REAL_WORLD_VERIFICATION",
                "mode": mode_snapshot(),
            }
        entry = {
            "verification_id": f"SV-{uuid4().hex[:8]}",
            "supplier": supplier,
            "product": product,
            "quote_date": quote_date or _utc()[:10],
            "price": price if price is not None else "UNKNOWN",
            "availability": availability,
            "lead_time": lead_time,
            "warranty": warranty,
            "notes": notes,
            "authorized_by": authorized_by,
            "recorded_at": _utc(),
            "freshness_days": freshness_days,
            "assumed_permanent": False,
            "network_transmitted": False,
        }
        rec.setdefault("supplier_verifications", []).append(entry)
        rec.setdefault("operator_authorizations", []).append(
            {"type": "SUPPLIER_VERIFICATION", "by": authorized_by, "at": _utc()}
        )
        if price not in {None, "UNKNOWN", ""}:
            try:
                rec["pricing"]["actual_acquisition_cost"] = float(price)
                rec["pricing"]["variance"] = _variance(
                    rec["pricing"].get("estimated_acquisition_cost"),
                    rec["pricing"].get("actual_acquisition_cost"),
                )
            except (TypeError, ValueError):
                pass
        rec["updated_at"] = _utc()
        self.save()
        return deepcopy(rec)

    def record_financing_verification(
        self,
        record_id: str,
        *,
        financing_path: str,
        requirements: Any = None,
        result: str = "UNKNOWN",
        evidence: Any = None,
        transaction_structure: str | None = None,
        terms: str | None = None,
        timeline: str | None = None,
        authorized_by: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Preserve UNKNOWN separately from INCOMPATIBLE."""
        rec = self._records.get(record_id)
        if not rec:
            return {"error": "not_found"}
        if not authorized_by:
            return {"error": "authorization_required", "message": "Financing verification requires operator authorization"}
        if is_development_no_outreach() and not is_controlled_verification():
            return {
                "error": "blocked_by_operating_mode",
                "message": "Financing verification recording requires CONTROLLED_REAL_WORLD_VERIFICATION",
                "mode": mode_snapshot(),
            }
        st = (state or result or "UNKNOWN").upper()
        if st not in {"UNKNOWN", "COMPATIBLE", "INCOMPATIBLE", "VERIFIED", "PENDING"}:
            st = "UNKNOWN"
        # Never coerce UNKNOWN → INCOMPATIBLE
        if result and str(result).upper() == "UNKNOWN":
            st = "UNKNOWN"
        entry = {
            "verification_id": f"FV-{uuid4().hex[:8]}",
            "financing_path": financing_path,
            "requirements": requirements if requirements is not None else "UNKNOWN",
            "result": result if result is not None else "UNKNOWN",
            "state": st,
            "evidence": evidence if evidence is not None else "UNKNOWN",
            "transaction_structure": transaction_structure or "UNKNOWN",
            "terms": terms or "UNKNOWN",
            "timeline": timeline or "UNKNOWN",
            "authorized_by": authorized_by,
            "recorded_at": _utc(),
            "unknown_is_not_rejection": True,
            "network_transmitted": False,
            "financing_application_submitted": False,
        }
        rec.setdefault("financing_verifications", []).append(entry)
        rec["financing"].update(
            {
                "funding_path": financing_path,
                "result": entry["result"],
                "terms": entry["terms"],
                "timeline": entry["timeline"],
                "state": st,
                "unknown_is_not_rejection": True,
            }
        )
        rec.setdefault("operator_authorizations", []).append(
            {"type": "FINANCING_VERIFICATION", "by": authorized_by, "at": _utc()}
        )
        rec["updated_at"] = _utc()
        self.save()
        return deepcopy(rec)

    def record_outcome(
        self,
        record_id: str,
        *,
        status: str,
        revenue: Any = "UNKNOWN",
        actual_profit: Any = "UNKNOWN",
        timeline: str | None = None,
        problems: list[str] | None = None,
        delays: list[str] | None = None,
        missing_information: list[str] | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        rec = self._records.get(record_id)
        if not rec:
            return {"error": "not_found"}
        st = str(status or "UNKNOWN").upper()
        won_lost = "UNKNOWN"
        if st in {"WON", "LOST", "ABANDONED", "NOT_BID", "IN_PROGRESS"}:
            if st == "WON":
                won_lost = "WON"
            elif st == "LOST":
                won_lost = "LOST"
        else:
            st = "IN_PROGRESS"
        rec["outcome"] = {
            "status": st,
            "won_lost": won_lost,
            "revenue": revenue if revenue is not None else "UNKNOWN",
            "actual_profit": actual_profit if actual_profit is not None else "UNKNOWN",
            "timeline": timeline or "UNKNOWN",
            "recorded_by": operator_id or "operator",
            "recorded_at": _utc(),
        }
        if problems:
            rec["execution"]["problems_encountered"] = list(problems)
        if delays:
            rec["execution"]["delays"] = list(delays)
        if missing_information:
            rec["execution"]["missing_information"] = list(missing_information)
        rec["updated_at"] = _utc()
        self.save()
        return deepcopy(rec)


_STORE: TransactionLearningStore | None = None


def get_transaction_learning_store(path: Path | None = None) -> TransactionLearningStore:
    global _STORE
    if path is not None:
        return TransactionLearningStore(path=path)
    if _STORE is None:
        _STORE = TransactionLearningStore()
    return _STORE


def reset_transaction_learning_store(path: Path | None = None) -> TransactionLearningStore:
    global _STORE
    _STORE = TransactionLearningStore(path=path) if path else TransactionLearningStore()
    return _STORE
