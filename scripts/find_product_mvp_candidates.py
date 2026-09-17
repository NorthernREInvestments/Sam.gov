"""READ-ONLY: rank local product solicitation candidates for PRODUCT MVP test.

NO OpenAI. NO SAM. NO web. NO USAspending.
"""

from __future__ import annotations
from application_clock import now_utc, today_local

import json
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main() -> int:
    from ai_analysis_cache import analysis_fingerprint, get_cached_analysis
    from ai_funnel import stage0_evaluate
    from ai_model_router import FunnelStage, ModelTier
    from ai_stage1 import STAGE1_SCHEMA_VERSION, STAGE1_TASK, compute_stage1_fingerprint
    from ai_stage2 import STAGE2_TASK, stage2_schema_fingerprint_component
    from database import SessionLocal
    from models import Contract, ContractAttachment
    from product_deal import FIT_CORE_PRODUCT, paid_work_priority_rank, resolve_core_fit

    session = SessionLocal()
    try:
        contracts = session.query(Contract).order_by(Contract.id.asc()).all()
        ranked: list[dict] = []
        today = today_local()

        for c in contracts:
            s0 = stage0_evaluate(c)
            fit = resolve_core_fit(stage0_classification=s0.get("classification"))
            title = (c.title or "")
            hay = f"{title}\n{(c.description or '')[:2000]}\n{(c.attachment_text or '')[:3000]}"
            product_kw = any(
                k in hay.lower()
                for k in (
                    "equipment",
                    "supply",
                    "supplies",
                    "hardware",
                    "laptop",
                    "computer",
                    "furniture",
                    "appliance",
                    "tool",
                    "parts",
                    "vehicle",
                    "generator",
                    "electronics",
                    "monitor",
                    "server",
                    "printer",
                    "safety",
                )
            )
            service_kw = any(
                k in hay.lower()
                for k in ("janitorial", "custodial", "groundskeeping", "mowing", "staffing", "services")
            )

            text_len = len(c.attachment_text or "")
            att_count = session.query(ContractAttachment).filter_by(contract_id=c.id).count()
            due = c.due_date
            expired = bool(due and due < today)
            status = (c.status or "").lower()
            status_dead = status in {"cancelled", "canceled", "expired", "inactive", "archived"}

            # Stage 1 / 2 cache presence (best-effort current fingerprints)
            s1_fp = None
            s1_hit = False
            s2_hit = False
            try:
                s1_computed = compute_stage1_fingerprint(c, stage0=s0)
                s1_fp = s1_computed.get("fingerprint")
                s1_hit = bool(get_cached_analysis(s1_fp) if s1_fp else None)
            except Exception:
                pass

            score = 0
            reasons: list[str] = []
            if fit["core_fit"] == FIT_CORE_PRODUCT:
                score += 100
                reasons.append("core_product_class")
            elif product_kw and not service_kw:
                score += 40
                reasons.append("product_keywords")
            if product_kw:
                score += 20
            if text_len >= 5000:
                score += 30
                reasons.append("rich_attachment_text")
            elif text_len >= 500:
                score += 15
            if att_count:
                score += 10
            if c.estimated_value:
                score += 10
            if due and not expired:
                score += 25
                reasons.append("deadline_open")
            if expired or status_dead:
                score -= 80
                reasons.append("expired_or_dead")
            if service_kw and fit["core_fit"] != FIT_CORE_PRODUCT:
                score -= 40
                reasons.append("service_keywords")
            if s0.get("decision") == "REJECT":
                score -= 50
                reasons.append("stage0_reject")

            ranked.append(
                {
                    "opportunity_id": c.id,
                    "notice_id": c.notice_id,
                    "solicitation": (c.sam_raw or {}).get("solicitationNumber")
                    if isinstance(c.sam_raw, dict)
                    else None,
                    "title": title[:120],
                    "stage0_classification": s0.get("classification"),
                    "core_fit": fit["core_fit"],
                    "product_purity": fit.get("product_purity"),
                    "paid_work_priority_rank": paid_work_priority_rank(fit),
                    "why_product": reasons,
                    "document_text_chars": text_len,
                    "attachment_rows": att_count,
                    "estimated_value": c.estimated_value,
                    "estimated_value_status": "ORM_FIELD_UNPROVEN_AS_FACT"
                    if c.estimated_value
                    else "UNKNOWN",
                    "due_date": due.isoformat() if due else None,
                    "expired": expired,
                    "status": c.status,
                    "stage0_decision": s0.get("decision"),
                    "stage1_cache_exists": s1_hit,
                    "stage1_fingerprint": s1_fp,
                    "stage2_cache_exists": s2_hit,  # filled below if we can cheaply probe
                    "candidate_score": score,
                    "quantity": "UNKNOWN",
                    "major_known_blocker": (
                        "expired_deadline"
                        if expired
                        else ("stage0_reject" if s0.get("decision") == "REJECT" else None)
                    ),
                }
            )

        ranked.sort(key=lambda r: (-r["candidate_score"], r["paid_work_priority_rank"], r["opportunity_id"]))
        top5 = ranked[:5]
        recommended = None
        for r in top5:
            if r["core_fit"] == FIT_CORE_PRODUCT and not r["expired"] and r["stage0_decision"] != "REJECT":
                recommended = r
                break
        # Do NOT recommend a SECONDARY_SERVICE opportunity as the product MVP candidate
        if recommended is None:
            for r in ranked:
                if r["core_fit"] == FIT_CORE_PRODUCT and not r["expired"] and r["stage0_decision"] != "REJECT":
                    recommended = r
                    break

        if recommended and recommended.get("core_fit") == FIT_CORE_PRODUCT and not recommended.get("expired"):
            next_state = "PRODUCT_MVP_CANDIDATE_READY"
        elif any(r.get("core_fit") == FIT_CORE_PRODUCT for r in ranked):
            next_state = "PRODUCT_MVP_NEEDS_LOCAL_FIXES"
        else:
            next_state = "NO_USABLE_LOCAL_PRODUCT_CANDIDATE"
            recommended = None

        report = {
            "LIVE_API_REQUESTS": 0,
            "MUTATIONS": 0,
            "candidates_scanned": len(ranked),
            "core_product_count": sum(1 for r in ranked if r["core_fit"] == FIT_CORE_PRODUCT),
            "top5": top5,
            "RECOMMENDED_PRODUCT_MVP_TEST_CANDIDATE": recommended,
            "next_state_hint": next_state,
            "note": (
                "Local gt_contracts corpus may be service-heavy; "
                "do not invent a product candidate"
            ),
        }
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
