"""READ-ONLY Postgres Stage 3 inventory. No mutations. No external APIs. No secrets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import inspect, text

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


SENSITIVE_SETTING_PREFIXES = (
    "api_key",
    "openai",
    "secret",
    "password",
    "token",
    "credential",
)


def _safe_setting_keys(keys: list[str]) -> dict[str, int]:
    """Count settings by category — never dump values."""
    cats = {"ai_cache": 0, "ai_metrics": 0, "budget": 0, "other": 0, "sensitive_skipped": 0}
    for k in keys:
        low = k.lower()
        if any(p in low for p in SENSITIVE_SETTING_PREFIXES):
            cats["sensitive_skipped"] += 1
        elif low.startswith("ai_analysis") or len(k) == 64:
            cats["ai_cache"] += 1
        elif "metric" in low:
            cats["ai_metrics"] += 1
        elif "budget" in low or "usage" in low:
            cats["budget"] += 1
        else:
            cats["other"] += 1
    return cats


def main() -> int:
    from database import SessionLocal, engine
    from models import (
        AppSetting,
        AttachmentQueueItem,
        Contract,
        ContractAttachment,
        ContractInvoice,
        ContractSub,
        CsvOpportunity,
        Proposal,
        Sub,
        SubContact,
        SubcontractAgreement,
        SubPayment,
    )

    session = SessionLocal()
    inv = inspect(engine)
    try:
        tables = sorted(inv.get_table_names())
        row_counts: dict[str, int] = {}
        for t in tables:
            try:
                n = session.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
                row_counts[t] = int(n or 0)
            except Exception as exc:
                row_counts[t] = -1
                session.rollback()

        # Contract field population (Stage-3 relevant)
        c_total = session.query(Contract).count()
        field_stats = {}
        for col in (
            "estimated_value",
            "awarded_amount",
            "selected_sub_quote",
            "margin_percentage",
            "attachment_text",
            "analysis",
            "pricing_intel",
            "sam_raw",
            "square_footage",
            "price_per_sqft_per_year",
            "period_of_performance_start",
            "government_contract_number",
            "naics_code",
            "agency",
        ):
            nonnull = session.execute(
                text(f"SELECT COUNT(*) FROM gt_contracts WHERE {col} IS NOT NULL")
            ).scalar()
            field_stats[col] = {"nonnull": int(nonnull or 0), "total": c_total}

        # Quote-like rows
        sub_quotes = session.execute(
            text(
                "SELECT COUNT(*) FROM gt_sub_contacts WHERE quote_received = true OR quote_amount IS NOT NULL"
            )
        ).scalar()
        cs_quotes = session.execute(
            text("SELECT COUNT(*) FROM gt_contract_subs WHERE quote_amount IS NOT NULL")
        ).scalar()
        att_with_text = session.execute(
            text("SELECT COUNT(*) FROM gt_contract_attachments WHERE extracted_text IS NOT NULL")
        ).scalar()
        att_total = session.query(ContractAttachment).count()

        # pricing_intel / analysis shape samples (keys only)
        sample_pi_keys: set[str] = set()
        sample_an_keys: set[str] = set()
        usaspending_hits = 0
        stage_keys = {"stage0": 0, "stage1": 0, "stage2": 0, "funnel": 0}
        for row in session.query(Contract).filter(Contract.pricing_intel.isnot(None)).limit(80):
            pi = row.pricing_intel if isinstance(row.pricing_intel, dict) else {}
            sample_pi_keys.update(pi.keys())
            blob = json.dumps(pi).lower()
            if "usaspending" in blob or "award" in blob:
                usaspending_hits += 1
        for row in session.query(Contract).filter(Contract.analysis.isnot(None)).limit(80):
            an = row.analysis if isinstance(row.analysis, dict) else {}
            sample_an_keys.update(an.keys())
            low_keys = {str(k).lower() for k in an.keys()}
            if any("stage0" in k or k == "funnel_stage" for k in low_keys):
                stage_keys["stage0"] += 1
            if any("stage1" in k for k in low_keys):
                stage_keys["stage1"] += 1
            if any("stage2" in k for k in low_keys):
                stage_keys["stage2"] += 1
            if "funnel" in low_keys or any("funnel" in k for k in low_keys):
                stage_keys["funnel"] += 1

        setting_keys = [r.key for r in session.query(AppSetting.key).all()]
        setting_cats = _safe_setting_keys(setting_keys)

        # Freshness field presence across models (schema-level)
        freshness = {
            "gt_contracts": [
                "first_seen_at",
                "last_updated_at",
                "attachment_text_extracted_at",
                "award_date",
                "period_of_performance_start",
                "period_of_performance_end",
            ],
            "gt_contract_attachments": ["downloaded_at"],
            "gt_subs": ["date_first_found", "date_last_updated"],
            "gt_sub_contacts": ["quote_date", "insurance_expiration_date", "created_at", "updated_at"],
            "gt_contract_subs": ["quote_date", "date_added", "date_status_updated"],
            "gt_csv_opportunities": ["imported_at", "updated_at"],
            "gt_app_settings_cache_payload": ["cached_at (inside JSON value)"],
            "missing_globally": [
                "retrieved_at (generic)",
                "last_verified_at",
                "source_updated_at",
                "effective_date",
                "expiration_date (quotes)",
                "quote provenance / verification status enum",
            ],
        }

        report = {
            "READ_ONLY": True,
            "MUTATIONS": 0,
            "LIVE_API_REQUESTS": 0,
            "all_tables": tables,
            "row_counts": row_counts,
            "gt_model_counts": {
                "gt_contracts": session.query(Contract).count(),
                "gt_contract_attachments": att_total,
                "gt_contract_attachments_with_extracted_text": int(att_with_text or 0),
                "gt_subs": session.query(Sub).count(),
                "gt_sub_contacts": session.query(SubContact).count(),
                "gt_sub_contacts_with_quote": int(sub_quotes or 0),
                "gt_contract_subs": session.query(ContractSub).count(),
                "gt_contract_subs_with_quote": int(cs_quotes or 0),
                "gt_subcontract_agreements": session.query(SubcontractAgreement).count(),
                "gt_proposals": session.query(Proposal).count(),
                "gt_csv_opportunities": session.query(CsvOpportunity).count(),
                "gt_attachment_queue": session.query(AttachmentQueueItem).count(),
                "gt_contract_invoices": session.query(ContractInvoice).count(),
                "gt_sub_payments": session.query(SubPayment).count(),
                "gt_app_settings": session.query(AppSetting).count(),
            },
            "app_setting_categories": setting_cats,
            "contract_field_population": field_stats,
            "pricing_intel_sample_keys": sorted(sample_pi_keys),
            "analysis_sample_keys": sorted(sample_an_keys),
            "analysis_stage_key_hits_in_sample": stage_keys,
            "pricing_intel_awardish_sample_hits": usaspending_hits,
            "freshness_fields": freshness,
            "shared_watchlist_hint": "gs_watchlist (read-only sibling table if present)",
            "gs_watchlist_present": "gs_watchlist" in tables,
            "gs_watchlist_count": row_counts.get("gs_watchlist", 0),
        }
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
