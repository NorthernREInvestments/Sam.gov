"""NextBid client — thin wrapper over centralized controller (Build 002J).

Metered calls cannot bypass nextbid_controller.metered_request.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.nextbid_controller import (
    NextBidBudgetExhausted,
    NextBidControllerError,
    NextBidUnauthorized,
    acquisition_locked,
    controller_status,
    free_request,
    metered_request,
    new_job_id,
    refresh_account_usage,
)
from m3.research.nextbid_killswitch import emergency_stop_status

# Re-exports for callers
NextBidConfigError = NextBidUnauthorized
NextBidQuotaError = NextBidBudgetExhausted


class NextBidBudget:
    """Compatibility shim — real enforcement is in nextbid_controller."""

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self.account_remaining: int | None = None
        self.account_used: int | None = None
        self.account_allowance: int | None = None
        self.stopped_reason: str | None = None
        self.by_endpoint: dict[str, int] = {}

    def snapshot(self) -> dict[str, Any]:
        st = controller_status()
        return {
            "controller": st,
            "by_endpoint": dict(self.by_endpoint),
            "account_remaining": self.account_remaining,
            "account_used": self.account_used,
            "account_allowance": self.account_allowance,
            "stopped_reason": self.stopped_reason,
        }

    def can_spend(self, bucket: str, units: int = 1) -> bool:
        if acquisition_locked():
            return False
        st = controller_status()
        day_used = int((st.get("budget_state") or {}).get("day_used") or 0)
        daily = int((st.get("limits") or {}).get("daily") or 0)
        return day_used + units <= daily

    def update_account(self, *, remaining: int | None, used: int | None, allowance: int | None) -> None:
        if remaining is not None:
            self.account_remaining = int(remaining)
        if used is not None:
            self.account_used = int(used)
        if allowance is not None:
            self.account_allowance = int(allowance)

    def record(self, bucket: str, endpoint: str, units: int = 1) -> None:
        self.by_endpoint[endpoint] = self.by_endpoint.get(endpoint, 0) + units


class NextBidClient:
    def __init__(self, budget: NextBidBudget | None = None, job_id: str | None = None) -> None:
        settings = get_settings()
        if not (settings.nextbid_api_key or "").strip():
            raise NextBidConfigError(
                "NEXTBID_API_KEY is not configured. Set it securely; do not commit the key."
            )
        self.budget = budget or NextBidBudget()
        self.job_id = job_id or new_job_id("nb")

    def close(self) -> None:
        return None

    def __enter__(self) -> "NextBidClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def usage(self) -> dict[str, Any]:
        data = refresh_account_usage(allow_when_locked=True)
        self.budget.update_account(
            remaining=data.get("remaining"),
            used=data.get("used"),
            allowance=data.get("allowance"),
        )
        return {"ok": data.get("ok"), "usage": data}

    def tradelines(self) -> dict[str, Any]:
        return free_request("/tradelines")

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if "page_size" in clean:
            clean["page_size"] = max(5, min(25, int(clean["page_size"])))
        else:
            clean["page_size"] = 25
        data = metered_request(
            "GET",
            "/opportunities",
            job_id=self.job_id,
            endpoint="search",
            params=clean,
            max_retries=0,
        )
        self.budget.record("discovery", "search", 1)
        return data

    def get_opportunity(self, nb_id: str) -> dict[str, Any]:
        data = metered_request(
            "GET",
            f"/opportunities/{nb_id}",
            job_id=self.job_id,
            endpoint="get_opportunity",
            nb_id=nb_id,
            max_retries=0,
        )
        self.budget.record("detail", "get_opportunity", 1)
        return data

    def list_documents(self, nb_id: str) -> dict[str, Any]:
        data = metered_request(
            "GET",
            f"/opportunities/{nb_id}/documents",
            job_id=self.job_id,
            endpoint="list_documents",
            nb_id=nb_id,
            max_retries=0,
        )
        self.budget.record("documents", "list_documents", 1)
        return data

    def list_files(self, nb_id: str) -> dict[str, Any]:
        data = metered_request(
            "GET",
            f"/opportunities/{nb_id}/files",
            job_id=self.job_id,
            endpoint="list_files",
            nb_id=nb_id,
            max_retries=0,
        )
        self.budget.record("documents", "list_files", 1)
        return data

    def read_file(self, nb_id: str, path: str) -> dict[str, Any]:
        data = metered_request(
            "GET",
            f"/opportunities/{nb_id}/files/{path}",
            job_id=self.job_id,
            endpoint="read_file",
            nb_id=nb_id,
            max_retries=0,
        )
        self.budget.record("documents", "read_file", 1)
        return data

    def get_document_download(self, nb_id: str, document_hash: str) -> dict[str, Any]:
        """Metered: temporary object-store download URL for a known document SHA-256."""
        h = str(document_hash or "").strip()
        data = metered_request(
            "GET",
            f"/opportunities/{nb_id}/documents/{h}/download",
            job_id=self.job_id,
            endpoint="get_document_download",
            nb_id=nb_id,
            max_retries=0,
        )
        self.budget.record("documents", "get_document_download", 1)
        return data


def checkpoint_path(name: str = "nextbid_acquire_v1") -> Path:
    root = get_settings().data_root() / "nextbid_checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{name}.json"


def load_checkpoint(name: str = "nextbid_acquire_v1") -> dict[str, Any]:
    path = checkpoint_path(name)
    if not path.exists():
        return {"version": 1, "updated_at": None, "state": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "updated_at": None, "state": {}, "corrupt": True}


def save_checkpoint(state: dict[str, Any], name: str = "nextbid_acquire_v1") -> Path:
    path = checkpoint_path(name)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "emergency_stop": emergency_stop_status(),
        "controller": controller_status(),
    }
    raw = json.dumps(payload, indent=2, default=str)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    try:
        tmp.replace(path)
    except PermissionError:
        # OneDrive / Windows lock: write directly, then best-effort cleanup
        path.write_text(raw, encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    return path
