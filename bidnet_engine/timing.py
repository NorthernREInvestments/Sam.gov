"""Per-stage timing instrumentation."""

from __future__ import annotations

import statistics
import time
from contextlib import contextmanager
from typing import Any, Iterator


class StageTimer:
    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = {}
        self.class_samples: dict[str, list[float]] = {
            "NETWORK_WAIT": [],
            "BROWSER_WAIT": [],
            "CPU": [],
            "DATABASE": [],
            "RATE_LIMIT_SLEEP": [],
            "SERIALIZATION": [],
            "OTHER": [],
        }
        self._map = {
            "auth": "BROWSER_WAIT",
            "detail_fetch": "NETWORK_WAIT",
            "detail_parse": "CPU",
            "official_portal": "NETWORK_WAIT",
            "package_lookup": "CPU",
            "document_inventory": "CPU",
            "document_download": "NETWORK_WAIT",
            "document_hash": "CPU",
            "eligibility": "CPU",
            "lines": "CPU",
            "identity": "CPU",
            "revenue": "CPU",
            "pricing": "NETWORK_WAIT",
            "quote_routing": "CPU",
            "db_write": "DATABASE",
            "total": "OTHER",
        }

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self.samples.setdefault(stage, []).append(elapsed)
            bucket = self._map.get(stage, "OTHER")
            self.class_samples[bucket].append(elapsed)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"stages": {}, "time_classes": {}, "primary_sink": None}
        totals: dict[str, float] = {}
        for stage, vals in self.samples.items():
            if not vals:
                continue
            sorted_vals = sorted(vals)
            n = len(sorted_vals)

            def pct(p: float) -> float:
                idx = min(n - 1, max(0, int(round((p / 100.0) * (n - 1)))))
                return round(sorted_vals[idx], 4)

            out["stages"][stage] = {
                "n": n,
                "mean": round(statistics.fmean(vals), 4),
                "median": round(statistics.median(vals), 4),
                "p90": pct(90),
                "p95": pct(95),
                "sum": round(sum(vals), 4),
            }
            totals[stage] = sum(vals)
        for name, vals in self.class_samples.items():
            out["time_classes"][name] = round(sum(vals), 4) if vals else 0.0
        if totals:
            out["primary_sink"] = max(totals, key=totals.get)  # type: ignore[arg-type]
        return out
