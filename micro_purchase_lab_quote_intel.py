"""Internal supplier quote intelligence — learned from REAL quotes only.

ESTIMATED_FROM_OUR_QUOTE_HISTORY never equals VERIFIED acquisition price.
Estimated bands must never produce BID_CANDIDATE.
"""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from micro_purchase_lab_economics import D

BUILD_TAG = "20260922-m3-micro-lab-automation-1"
SETTINGS_KEY = "m3_micro_purchase_quote_intel_v1"
DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "micro_purchase_quote_intel.json"

MIN_QUOTE_SAMPLE = 3
LABEL_ESTIMATED = "ESTIMATED_FROM_OUR_QUOTE_HISTORY"
LABEL_ESTIMATE_REQUIRED = "ESTIMATE — ACTUAL QUOTE REQUIRED"


def _utc() -> str:
    return now_utc().isoformat()


class QuoteIntelligenceStore:
    """Durable store for real supplier quote observations + aggregates."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self._quotes: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        data = None
        if self.path == DEFAULT_PATH:
            data = self._read_durable()
        if not data and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                data = None
        self._quotes = [q for q in (data or {}).get("quotes") or [] if isinstance(q, dict)]

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
            "kind": "MicroPurchaseQuoteIntelligence",
            "build": BUILD_TAG,
            "updated_at": _utc(),
            "quote_count": len(self._quotes),
            "quotes": self._quotes,
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
        return deepcopy(self._quotes)

    def record(self, observation: dict[str, Any]) -> dict[str, Any]:
        row = dict(observation)
        row.setdefault("id", f"SQI-{uuid4().hex[:10]}")
        row.setdefault("recorded_at", _utc())
        row["label"] = "ACTUAL_SUPPLIER_QUOTE"
        self._quotes.append(row)
        self.save()
        return deepcopy(row)


_STORE: QuoteIntelligenceStore | None = None


def get_quote_intel_store() -> QuoteIntelligenceStore:
    global _STORE
    if _STORE is None:
        _STORE = QuoteIntelligenceStore()
    return _STORE


def reset_quote_intel_store_for_tests(path: Path | None = None) -> QuoteIntelligenceStore:
    global _STORE
    _STORE = QuoteIntelligenceStore(path=path)
    return _STORE


def record_actual_supplier_quote(
    *,
    test: dict[str, Any],
    quote: dict[str, Any],
) -> dict[str, Any]:
    """Persist one real quote as internal pricing intelligence."""
    public = None
    for m in test.get("current_market_prices") or []:
        p = D(m.get("unit_price") or m.get("price"))
        if p is not None:
            public = p
            break
    quoted = D(quote.get("quoted_unit_cost") or quote.get("unit_price"))
    discount_pct = None
    if public is not None and quoted is not None and public > 0:
        discount_pct = ((public - quoted) / public * Decimal("100")).quantize(Decimal("0.01"))

    obs = {
        "supplier": quote.get("supplier") or "UNKNOWN",
        "manufacturer": test.get("manufacturer"),
        "product": test.get("product"),
        "part_number": test.get("part_number"),
        "nsn": test.get("nsn"),
        "category": test.get("category") or _infer_category(test),
        "public_price_at_quote_time": str(public) if public is not None else None,
        "quoted_price": str(quoted) if quoted is not None else None,
        "quote_discount_vs_public_pct": str(discount_pct) if discount_pct is not None else None,
        "quantity": quote.get("quantity") or test.get("quantity"),
        "quote_date": quote.get("quote_date") or _utc()[:10],
        "pricing_type": quote.get("pricing_type") or "RESELLER",
        "special_bid_status": quote.get("special_bid") or quote.get("special_bid_status"),
        "terms": quote.get("payment_terms"),
        "freight": quote.get("freight"),
        "account_type": quote.get("account_type"),
        "executable_status": "EXECUTABLE"
        if not (
            quote.get("personal_guarantee_required")
            or quote.get("requires_full_prepayment")
            or quote.get("personal_credit_required")
        )
        else "EXECUTION_CONSTRAINT",
        "test_id": test.get("id"),
        "solicitation": test.get("solicitation"),
    }
    return get_quote_intel_store().record(obs)


def _infer_category(test: dict[str, Any]) -> str:
    blob = " ".join(str(x or "") for x in (test.get("product"), test.get("manufacturer"), test.get("agency"))).lower()
    if any(x in blob for x in ("dell", "lenovo", "cisco", "laptop", "server", "switch")):
        return "IT_HARDWARE"
    if any(x in blob for x in ("valve", "bearing", "electrical", "industrial", "nsn", "bushing")):
        return "INDUSTRIAL"
    return "GENERAL"


def supplier_discount_history(
    supplier: str,
    *,
    category: str | None = None,
    min_sample: int = MIN_QUOTE_SAMPLE,
) -> dict[str, Any]:
    needle = str(supplier or "").strip().lower()
    rows = []
    for q in get_quote_intel_store().all():
        if str(q.get("supplier") or "").strip().lower() != needle:
            continue
        if category and str(q.get("category") or "").upper() != str(category).upper():
            continue
        d = D(q.get("quote_discount_vs_public_pct"))
        if d is None:
            continue
        rows.append({"discount_pct": d, "quote_date": q.get("quote_date"), "id": q.get("id")})

    if len(rows) < min_sample:
        return {
            "available": False,
            "label": LABEL_ESTIMATED,
            "observation_count": len(rows),
            "min_sample_required": min_sample,
            "median_discount_pct": None,
            "min_discount_pct": None,
            "max_discount_pct": None,
            "recency": None,
            "note": f"Need at least {min_sample} actual quotes before estimating",
        }

    discounts = sorted(r["discount_pct"] for r in rows)
    med = discounts[len(discounts) // 2]
    dated = sorted((r.get("quote_date") or "" for r in rows), reverse=True)
    return {
        "available": True,
        "label": LABEL_ESTIMATED,
        "never_verified": True,
        "observation_count": len(rows),
        "min_sample_required": min_sample,
        "median_discount_pct": str(med),
        "min_discount_pct": str(min(discounts)),
        "max_discount_pct": str(max(discounts)),
        "recency": dated[0] if dated else None,
        "supplier": supplier,
        "category": category,
    }


def estimated_supplier_cost_band(
    *,
    public_price: Any,
    supplier: str,
    category: str | None = None,
    min_sample: int = MIN_QUOTE_SAMPLE,
) -> dict[str, Any]:
    """Provisional acquisition estimate for prioritization only."""
    public = D(public_price)
    hist = supplier_discount_history(supplier, category=category, min_sample=min_sample)
    if not hist.get("available") or public is None or public <= 0:
        return {
            "available": False,
            "label": LABEL_ESTIMATE_REQUIRED,
            "estimation_label": LABEL_ESTIMATED,
            "can_support_bid_candidate": False,
            "history": hist,
        }

    med = D(hist["median_discount_pct"]) or Decimal("0")
    lo_d = D(hist["min_discount_pct"]) or med
    hi_d = D(hist["max_discount_pct"]) or med
    # Higher discount → lower cost. Band from max-discount (low cost) to min-discount (high cost).
    low_cost = (public * (Decimal("1") - (hi_d / Decimal("100")))).quantize(Decimal("0.01"))
    high_cost = (public * (Decimal("1") - (lo_d / Decimal("100")))).quantize(Decimal("0.01"))
    mid_cost = (public * (Decimal("1") - (med / Decimal("100")))).quantize(Decimal("0.01"))
    return {
        "available": True,
        "label": LABEL_ESTIMATE_REQUIRED,
        "estimation_label": LABEL_ESTIMATED,
        "can_support_bid_candidate": False,
        "public_market_price": str(public),
        "median_discount_pct": str(med),
        "estimated_low": str(min(low_cost, high_cost)),
        "estimated_high": str(max(low_cost, high_cost)),
        "estimated_mid": str(mid_cost),
        "band_label": f"~${min(low_cost, high_cost)}–${max(low_cost, high_cost)}",
        "observation_count": hist["observation_count"],
        "supplier": supplier,
        "history": hist,
        "note": "Prioritization only — ACTUAL QUOTE REQUIRED for economics / BID_CANDIDATE",
    }


def estimated_supplier_cost_band_for_test(test: dict[str, Any]) -> dict[str, Any]:
    public = None
    for m in test.get("current_market_prices") or []:
        public = D(m.get("unit_price") or m.get("price"))
        if public is not None:
            break
    if public is None:
        public = D((test.get("recommended_current_market") or {}).get("unit_price"))
    targets = test.get("recommended_quote_targets") or test.get("supplier_candidates") or []
    supplier = (targets[0].get("supplier") if targets else None) or "MSC Industrial"
    return estimated_supplier_cost_band(
        public_price=public,
        supplier=str(supplier),
        category=_infer_category(test),
    )


def prior_quote_score_for_supplier(supplier: str) -> int:
    """Higher is better for ranking (used as negative sort key)."""
    hist = supplier_discount_history(supplier, min_sample=1)
    if not hist.get("observation_count"):
        return 0
    n = int(hist["observation_count"])
    med = D(hist.get("median_discount_pct")) or Decimal("0")
    return int(n * 10 + med)


def supplier_performance_stats(*, min_sample: int = 1) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for q in get_quote_intel_store().all():
        s = str(q.get("supplier") or "UNKNOWN")
        by.setdefault(s, []).append(q)
    out = []
    for supplier, rows in sorted(by.items()):
        discounts = [D(r.get("quote_discount_vs_public_pct")) for r in rows]
        discounts = [d for d in discounts if d is not None]
        executable = sum(1 for r in rows if r.get("executable_status") == "EXECUTABLE")
        med = None
        if discounts:
            sd = sorted(discounts)
            med = str(sd[len(sd) // 2])
        out.append(
            {
                "supplier": supplier,
                "quotes_requested": len(rows),  # approximate from received intel
                "quotes_returned": len(rows),
                "median_discount_pct": med,
                "observation_count": len(rows),
                "executable_quote_count": executable,
                "statistically_meaningful": len(rows) >= MIN_QUOTE_SAMPLE,
                "min_sample": MIN_QUOTE_SAMPLE,
            }
        )
    return [r for r in out if r["observation_count"] >= min_sample]


def ingest_quotes_from_test(test: dict[str, Any]) -> list[dict[str, Any]]:
    """When operator saves actual quotes, record each into intelligence store."""
    recorded = []
    for q in test.get("supplier_quotes") or []:
        if not isinstance(q, dict):
            continue
        if not (q.get("quoted_unit_cost") or q.get("unit_price")):
            continue
        if q.get("_intel_recorded"):
            continue
        obs = record_actual_supplier_quote(test=test, quote=q)
        q["_intel_recorded"] = True
        q["_intel_id"] = obs.get("id")
        recorded.append(obs)
    return recorded
