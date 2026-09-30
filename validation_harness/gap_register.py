"""Persistent gap register across validation runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from validation_harness.models import GAP_STATUS_FIXED, GAP_STATUS_OPEN


class GapRegister:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._data: dict[str, Any] = {"kind": "ValidationGapRegister", "gaps": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("gaps"), dict):
                    self._data = loaded
            except Exception:
                pass

    def ingest(self, gaps: list[dict[str, Any]], *, run_id: str) -> None:
        now = now_utc().isoformat()
        seen_ids = set()
        for g in gaps or []:
            gid = str(g.get("gap_id") or f"{g.get('case_id')}:{g.get('field_path')}")
            seen_ids.add(gid)
            prev = self._data["gaps"].get(gid)
            if prev:
                prev["last_seen"] = now
                prev["last_run"] = run_id
                prev["actual"] = g.get("actual")
                prev["expected"] = g.get("expected")
                prev["severity"] = g.get("severity")
                prev["status"] = prev.get("status") or GAP_STATUS_OPEN
                if prev.get("status") == GAP_STATUS_FIXED:
                    prev["regression"] = True
                    prev["status"] = GAP_STATUS_OPEN
            else:
                self._data["gaps"][gid] = {
                    **g,
                    "gap_id": gid,
                    "first_seen": now,
                    "last_seen": now,
                    "last_run": run_id,
                    "status": GAP_STATUS_OPEN,
                    "regression": False,
                }
        # Mark previously open gaps not seen this run as still open (do not erase)
        self._data["updated_at"] = now
        self._data["last_run"] = run_id

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")
        return self.path

    def open_gaps(self) -> list[dict[str, Any]]:
        return [g for g in self._data.get("gaps", {}).values() if g.get("status") == GAP_STATUS_OPEN]
