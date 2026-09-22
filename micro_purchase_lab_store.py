"""Durable Micro-Purchase Economics Lab store — AppSetting authoritative."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any  # noqa: F401 — used by quote-queue helpers
from uuid import uuid4

from application_clock import now_utc

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "micro_purchase_lab_store.json"
SETTINGS_KEY = "m3_micro_purchase_lab_v1"


def _utc() -> str:
    return now_utc().isoformat()


class MicroPurchaseLabStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self._tests: dict[str, dict[str, Any]] = {}
        self._quote_queue: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        data = None
        # Durable AppSetting is authoritative only for the default production path.
        if self.path == DEFAULT_PATH:
            data = self._read_durable()
        if not data and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                data = None
        self._tests = {}
        for row in (data or {}).get("tests") or []:
            if isinstance(row, dict) and row.get("id"):
                self._tests[row["id"]] = row
        # Backward compatible: older payloads lack quote_queue
        self._quote_queue = {}
        for row in (data or {}).get("quote_queue") or []:
            if isinstance(row, dict) and row.get("id"):
                self._quote_queue[row["id"]] = row

    def _read_durable(self) -> dict[str, Any] | None:
        try:
            from database import SessionLocal
            from models import AppSetting

            db = SessionLocal()
            try:
                row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
                if not row or not row.value:
                    return None
                data = json.loads(row.value) if isinstance(row.value, str) else row.value
                return data if isinstance(data, dict) else None
            finally:
                db.close()
        except Exception:
            return None

    def save(self) -> None:
        payload = {
            "kind": "MicroPurchaseLabStore",
            "updated_at": _utc(),
            "test_count": len(self._tests),
            "tests": sorted(self._tests.values(), key=lambda t: t.get("updated_at") or "", reverse=True),
            "quote_queue": sorted(
                self._quote_queue.values(), key=lambda t: t.get("updated_at") or "", reverse=True
            ),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        if self.path != DEFAULT_PATH:
            return
        try:
            from database import SessionLocal
            from models import AppSetting

            db = SessionLocal()
            try:
                row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
                raw = json.dumps(payload, default=str)
                if row is None:
                    db.add(AppSetting(key=SETTINGS_KEY, value=raw))
                else:
                    row.value = raw
                db.commit()
            finally:
                db.close()
        except Exception:
            pass

    def all(self) -> list[dict[str, Any]]:
        return [deepcopy(t) for t in sorted(self._tests.values(), key=lambda t: t.get("updated_at") or "", reverse=True)]

    def get(self, test_id: str) -> dict[str, Any] | None:
        row = self._tests.get(test_id)
        return deepcopy(row) if row else None

    def upsert(self, test: dict[str, Any]) -> dict[str, Any]:
        tid = str(test.get("id") or f"MPT-{uuid4().hex[:10]}")
        now = _utc()
        existing = self._tests.get(tid) or {}
        row = {**existing, **deepcopy(test), "id": tid}
        row.setdefault("created_at", existing.get("created_at") or now)
        row["updated_at"] = now
        if not existing:
            row["created_at"] = now
        self._tests[tid] = row
        self.save()
        return deepcopy(row)

    def delete(self, test_id: str) -> bool:
        if test_id not in self._tests:
            return False
        del self._tests[test_id]
        self.save()
        return True

    def duplicate(self, test_id: str) -> dict[str, Any] | None:
        src = self.get(test_id)
        if not src:
            return None
        src.pop("id", None)
        src["status"] = "NOT_TESTED"
        src["notes"] = (src.get("notes") or "") + "\n[duplicated]"
        return self.upsert(src)

    def all_quote_queue(self) -> list[dict[str, Any]]:
        return [deepcopy(t) for t in sorted(self._quote_queue.values(), key=lambda t: t.get("updated_at") or "", reverse=True)]

    def upsert_quote_queue_item(self, item: dict[str, Any]) -> dict[str, Any]:
        qid = str(item.get("id") or f"QQ-{uuid4().hex[:10]}")
        now = _utc()
        existing = self._quote_queue.get(qid) or {}
        row = {**existing, **deepcopy(item), "id": qid}
        row.setdefault("created_at", existing.get("created_at") or now)
        row["updated_at"] = now
        self._quote_queue[qid] = row
        self.save()
        return deepcopy(row)

    def update_quote_queue_status(self, item_id: str, status: str, **extra: Any) -> dict[str, Any] | None:
        row = self._quote_queue.get(item_id)
        if not row:
            return None
        row = dict(row)
        row["quote_status"] = status
        row.update(extra)
        row["updated_at"] = _utc()
        self._quote_queue[item_id] = row
        self.save()
        return deepcopy(row)

    def merge_quote_queue_from_test(self, test: dict[str, Any]) -> list[dict[str, Any]]:
        saved = []
        for item in test.get("quote_queue_items") or []:
            if not isinstance(item, dict):
                continue
            payload = dict(item)
            payload["test_id"] = test.get("id")
            saved.append(self.upsert_quote_queue_item(payload))
        return saved
