"""Source-specific Socrata field inference and solicitation-dataset validation."""

from __future__ import annotations

import re
from typing import Any

from m3.research.http_util import fetch_json

# Ordered title candidates — first non-empty wins when config omits title_field
TITLE_CANDIDATES = (
    "title",
    "contract_title",
    "contracttitle",
    "contract_description",
    "project_description",
    "procurement_name",
    "type_of_notice_description",
    "description",
    "solicitation_title",
    "opportunity_title",
    "bid_title",
    "name",
    "commodity",
    "item_description",
    "work_description",
)

STATUS_CANDIDATES = ("status", "stagename", "contract_status", "bid_status", "opportunity_status")
AGENCY_CANDIDATES = ("agency_name", "agency", "department", "agencycode", "buyer", "department_name", "entity")
NUMBER_CANDIDATES = (
    "solicitation_number",
    "contract_number",
    "contractnumber",
    "contract_no",
    "number",
    "rampid",
    "pin",
    "tracking_number",
    "buying_plan_number",
    "solicitation",
    ":id",
)
DEADLINE_CANDIDATES = (
    "due_date",
    "end_date",
    "bid_opening_date",
    "closing_date",
    "closedate",
    "closingdate",
    "deadlinedate",
    "submission_deadline",
    "response_due",
)
POSTED_CANDIDATES = ("start_date", "posted_date", "bidpost", "issuancedate", "opendate", "date_created", "creation_date")
VALUE_CANDIDATES = (
    "contract_amount",
    "award_amount",
    "estimated_amount",
    "estimated_total_contract",
    "approximate_cost",
    "current_amount",
    "estimated_value",
)
CATEGORY_CANDIDATES = ("category", "commodity", "procurement_category", "type", "work_type")
URL_CANDIDATES = ("url", "document_link", "link", "bidurl", "documents_url", "detail_url", "opportunity_url")

# Datasets that are not active solicitation listings
NON_SOLICITATION_NAME_HINTS = re.compile(
    r"(?i)\b("
    r"charge ready|ev charging|expenditure|checkbook|payment|payroll|"
    r"vendor registration|certified vendor|lobbyist|campaign finance|"
    r"property assess|parcel|crime|311|traffic collision|"
    r"employee|salary|budget actuals|general ledger"
    r")\b"
)
SOLICITATION_NAME_HINTS = re.compile(
    r"(?i)\b("
    r"solicitation|bid opportunit|open bid|rfp|rfq|ifb|invitation to bid|"
    r"procurement opportunit|active bid|current bid|buying plan|"
    r"request for (proposal|quote|bid)|contract opportunit"
    r")\b"
)
AWARD_HINTS = re.compile(r"(?i)\b(award|awarded|tabulation|purchase order|po history|historical contract)\b")


def pick_first_key(row: dict, candidates: tuple[str, ...], preferred: str | None = None) -> str | None:
    if preferred and preferred in row and row[preferred] not in (None, ""):
        return preferred
    for k in candidates:
        if k in row and row[k] not in (None, ""):
            return k
    # case-insensitive
    lower_map = {str(k).lower(): k for k in row.keys()}
    if preferred and preferred.lower() in lower_map:
        return lower_map[preferred.lower()]
    for k in candidates:
        if k.lower() in lower_map and row[lower_map[k.lower()]] not in (None, ""):
            return lower_map[k.lower()]
    return None


def infer_field_map(sample_rows: list[dict]) -> dict[str, str]:
    """Infer best field names from sample rows."""
    keys_seen: set[str] = set()
    for r in sample_rows:
        if isinstance(r, dict):
            keys_seen.update(r.keys())
    # Prefer a row that has a long text-ish title candidate
    best_row = None
    for r in sample_rows:
        if not isinstance(r, dict):
            continue
        for cand in TITLE_CANDIDATES:
            v = r.get(cand)
            if isinstance(v, str) and len(v.strip()) >= 5:
                best_row = r
                break
        if best_row:
            break
    if not best_row:
        best_row = next((r for r in sample_rows if isinstance(r, dict)), {}) or {}

    mapping: dict[str, str] = {}
    pairs = {
        "title_field": TITLE_CANDIDATES,
        "status_field": STATUS_CANDIDATES,
        "agency_field": AGENCY_CANDIDATES,
        "number_field": NUMBER_CANDIDATES,
        "deadline_field": DEADLINE_CANDIDATES,
        "posted_field": POSTED_CANDIDATES,
        "value_field": VALUE_CANDIDATES,
        "category_field": CATEGORY_CANDIDATES,
        "url_field": URL_CANDIDATES,
    }
    for dest, cands in pairs.items():
        # Also try any key containing hint substrings
        key = pick_first_key(best_row, cands)
        if not key:
            for k in best_row.keys():
                kl = str(k).lower()
                if dest == "title_field":
                    # Avoid entity-name columns (vendor_name, charityname, authority_name)
                    if kl.endswith("_name") or kl in {
                        "name",
                        "vendor",
                        "supplier",
                        "authority_name",
                        "charityname",
                        "agencycode",
                    }:
                        continue
                    if any(x in kl for x in ("title", "description", "commodity", "solicitation")):
                        if isinstance(best_row[k], str) and len(best_row[k].strip()) >= 5:
                            key = k
                            break
        if key:
            mapping[dest] = key
    mapping["observed_keys"] = sorted(str(k) for k in keys_seen)[:40]  # type: ignore[assignment]
    return mapping


# Column-name signals that indicate awards / spending / vendor registries — not open bids
AWARD_COLUMN_HINTS = {
    "award_date",
    "amount_expended_for_fiscal_year",
    "amount_expended_to_date",
    "current_or_outstanding_balance",
    "vendor_name",
    "prime_vendor",
    "check_amount",
    "payment_amount",
    "expenditure",
    "charityname",
    "lobbyist",
}
SOLICITATION_COLUMN_HINTS = {
    "solicitation_number",
    "bid_opening_date",
    "closing_date",
    "closedate",
    "closingdate",
    "deadlinedate",
    "due_date",
    "stagename",
    "bidurl",
    "rampid",
    "type_of_notice",
    "solicitation_title",
    "contracttitle",
    "bid_status",
}


def classify_dataset_kind(name: str, sample_rows: list[dict], inferred: dict[str, str]) -> dict[str, Any]:
    """Decide if a dataset is usable as an active solicitation source."""
    name = name or ""
    keys_lower = set()
    for r in sample_rows[:5]:
        if isinstance(r, dict):
            keys_lower.update(str(k).lower() for k in r.keys())

    award_cols = keys_lower & AWARD_COLUMN_HINTS
    sol_cols = keys_lower & SOLICITATION_COLUMN_HINTS
    # Title field that is clearly a vendor/org name — not a solicitation title
    title_field = (inferred.get("title_field") or "").lower()
    if title_field in {"vendor_name", "prime_vendor", "charityname", "authority_name", "supplier_name"}:
        if not sol_cols:
            return {
                "kind": "vendor_or_award_register",
                "operational": False,
                "reason": f"title_field_is_entity_name:{title_field}",
            }

    if award_cols and not sol_cols:
        return {
            "kind": "historical_award_or_expenditure",
            "operational": False,
            "reason": f"award_expenditure_columns:{sorted(award_cols)[:6]}",
        }

    if NON_SOLICITATION_NAME_HINTS.search(name) and not SOLICITATION_NAME_HINTS.search(name):
        return {
            "kind": "non_solicitation",
            "operational": False,
            "reason": "name_indicates_non_solicitation_dataset",
        }
    if AWARD_HINTS.search(name) and not SOLICITATION_NAME_HINTS.search(name):
        return {
            "kind": "historical_award_or_po",
            "operational": False,
            "reason": "name_indicates_award_archive",
        }
    title_field = inferred.get("title_field")
    usable_titles = 0
    for r in sample_rows[:25]:
        if not isinstance(r, dict):
            continue
        t = r.get(title_field) if title_field else None
        if isinstance(t, str) and len(t.strip()) >= 5:
            usable_titles += 1
    if usable_titles == 0:
        return {
            "kind": "unmapped_or_empty_title",
            "operational": False,
            "reason": "no_usable_title_in_sample",
            "title_field": title_field,
        }
    # Heuristic: solicitation-like if name hints OR solicitation columns OR deadline/status fields
    if (
        SOLICITATION_NAME_HINTS.search(name)
        or sol_cols
        or inferred.get("deadline_field")
        or inferred.get("status_field")
    ):
        return {
            "kind": "active_solicitation_candidate",
            "operational": True,
            "reason": "schema_and_or_name_support_solicitations",
            "sample_titles_ok": usable_titles,
            "solicitation_columns": sorted(sol_cols)[:8],
        }
    # Buying plans / commodity lists may still be useful forecasts
    if re.search(r"(?i)buying plan|commodity|procurement", name):
        return {
            "kind": "procurement_plan_or_commodity",
            "operational": True,
            "reason": "procurement_plan_dataset",
            "is_forecast": True,
            "sample_titles_ok": usable_titles,
        }
    return {
        "kind": "uncertain_dataset",
        "operational": False,
        "reason": "insufficient_solicitation_signals",
        "sample_titles_ok": usable_titles,
    }


def validate_socrata_source(dataset_url: str, name: str = "") -> dict[str, Any]:
    """Fetch a small sample and return mapping + operational decision."""
    url = (dataset_url or "").split("?")[0]
    if not url:
        return {"ok": False, "operational": False, "reason": "missing_url"}
    try:
        status, final, data = fetch_json(f"{url}?$limit=25")
    except Exception as exc:
        return {"ok": False, "operational": False, "reason": f"{type(exc).__name__}:{exc}"}
    if status >= 400 or not isinstance(data, list) or not data:
        return {"ok": False, "operational": False, "reason": f"sample_status={status}", "status": status}
    rows = [r for r in data if isinstance(r, dict)]
    inferred = infer_field_map(rows)
    decision = classify_dataset_kind(name, rows, inferred)
    return {
        "ok": True,
        "operational": bool(decision.get("operational")),
        "inferred": {k: v for k, v in inferred.items() if k != "observed_keys"},
        "observed_keys": inferred.get("observed_keys"),
        "decision": decision,
        "sample_url": final,
        "sample_count": len(rows),
    }
