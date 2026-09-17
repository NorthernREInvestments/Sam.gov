"""M3 1.0 thin end-to-end orchestrator — extends ExecutableDealPipeline; does not replace engines."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from deadline_runtime import evaluate_deadline
from executable_deal_pipeline import ExecutableDealPipeline
from financing_verification import (
    build_transaction_funding_requirement,
    evaluate_funding_gate,
)
from m3_lifecycle import (
    NA_AUTO_CONTINUE,
    NA_NO_ACTION_REJECTED,
    NA_QUEUE_RESEARCH,
    NA_READY_FOR_OPERATOR,
    NA_WAIT_BUDGET,
    NA_WAIT_COMMERCIAL,
    NA_WAIT_FUNDING,
    NA_WAIT_OPERATOR,
    NA_WAIT_PACKAGE,
    derive_lifecycle,
    determine_next_action,
    readiness_summary,
)
from m3_pipeline_store import M3PipelineStore
from national_discovery_funnel import stage1_ultra_cheap
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, is_development_no_outreach, set_operating_mode
from product_category_yield import classify_product_category
from product_false_positive_audit import audit_survivor

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"


def _utc() -> str:
    return now_utc().isoformat()


def _classify_package_access(record: dict[str, Any]) -> str:
    raw = str(record.get("package_access") or record.get("document_access") or "").upper()
    if raw in {
        "PUBLIC_DIRECT",
        "PUBLIC_DETAIL_PAGE",
        "AUTH_GATED",
        "REGISTRATION_REQUIRED",
        "UNAVAILABLE",
        "UNKNOWN",
    }:
        return raw
    meta = record.get("raw_metadata") or {}
    if isinstance(meta, dict):
        d = str(meta.get("document_access") or "").upper()
        if d:
            return d
        if meta.get("public_metadata_only"):
            return "AUTH_GATED"
    if record.get("auth_required_for_spec"):
        return "AUTH_GATED"
    if record.get("documents") or record.get("line_items"):
        return "PUBLIC_DETAIL_PAGE"
    return "UNKNOWN"


class M3EndToEndOrchestrator:
    """
    Thin durable orchestration:
    discover/normalize record → cheap screen → research queue → ExecutableDealPipeline
    → optional compliance/pricing/commercial plan/draft → readiness + operator queue.
    """

    def __init__(
        self,
        *,
        store: M3PipelineStore | None = None,
        pipeline: ExecutableDealPipeline | None = None,
        cost_governor: Any | None = None,
    ) -> None:
        set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
        self.store = store or M3PipelineStore()
        self.pipeline = pipeline or ExecutableDealPipeline()
        self.cost_governor = cost_governor
        self.metrics = {
            "ingested": 0,
            "duplicates_skipped": 0,
            "cheap_rejected": 0,
            "research_queued": 0,
            "pipeline_runs": 0,
            "auto_advanced": 0,
            "paid_blocked": 0,
            "external_actions_executed": 0,
            "supplier_contacts": 0,
            "financier_contacts": 0,
            "agency_contacts": 0,
            "registrations": 0,
            "signatures": 0,
            "bids_submitted": 0,
            "purchases": 0,
            "real_external_spend": 0.0,
            "evidence_acquisitions": 0,
        }

    def ingest_discovery_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Normalize + dedupe + cheap screen + queue if survivor."""
        row, created = self.store.upsert_from_discovery(record)
        self.metrics["ingested"] += 1
        if not created:
            self.metrics["duplicates_skipped"] += 1
            self.store.save()
            return {
                "canonical_id": row["canonical_id"],
                "created": False,
                "lifecycle": row.get("lifecycle"),
                "next_action": row.get("pending_next_action"),
                "duplicate": True,
            }

        # Deadline
        if row.get("deadline"):
            dl = evaluate_deadline(
                response_deadline=row["deadline"],
                local_timezone=record.get("timezone") or "America/Chicago",
            )
            row["deadline_evaluation"] = {
                "status": dl.get("deadline_status") or dl.get("status"),
                "calendar_days_remaining": dl.get("calendar_days_remaining"),
            }
            st = str(row["deadline_evaluation"]["status"] or "").upper()
            if st in {"EXPIRED", "TOO_LATE"} or (
                isinstance(dl.get("calendar_days_remaining"), (int, float)) and dl["calendar_days_remaining"] < 0
            ):
                row["rejected"] = True
                row["stop_reason"] = "deadline_expired"
                row["lifecycle"] = derive_lifecycle(row)
                row["pending_next_action"] = determine_next_action(row)
                self.store._rows[row["canonical_id"]] = row
                self.store.save()
                return {
                    "canonical_id": row["canonical_id"],
                    "created": True,
                    "lifecycle": row["lifecycle"],
                    "next_action": row["pending_next_action"],
                    "survived": False,
                    "stop_reason": "deadline_expired",
                }

        s1 = stage1_ultra_cheap(
            {
                "title": row.get("title") or "",
                "description": row.get("description") or "",
                "status": row.get("status") or "OPEN",
            }
        )
        row["cheap_screen_survive"] = bool(s1.get("survive"))
        row["cheap_screen"] = s1
        row["product_classification"] = s1.get("classification") or row.get("product_classification")
        cat = classify_product_category(str(row.get("title") or ""), str(row.get("description") or ""))
        row["product_category"] = cat["category"]
        row["product_audit"] = audit_survivor(row)

        if not s1.get("survive"):
            row["rejected_cheap_screen"] = True
            row["lifecycle"] = derive_lifecycle(row)
            row["pending_next_action"] = determine_next_action(row)
            self.store._rows[row["canonical_id"]] = row
            self.metrics["cheap_rejected"] += 1
            self.store.save()
            return {
                "canonical_id": row["canonical_id"],
                "created": True,
                "lifecycle": row["lifecycle"],
                "next_action": row["pending_next_action"],
                "survived": False,
            }

        row["research_queued"] = True
        row["package_access"] = _classify_package_access({**record, **row})
        row["lifecycle"] = derive_lifecycle(row)
        row["pending_next_action"] = determine_next_action(row)
        self.store._rows[row["canonical_id"]] = row
        self.metrics["research_queued"] += 1
        self.store.save()
        return {
            "canonical_id": row["canonical_id"],
            "created": True,
            "lifecycle": row["lifecycle"],
            "next_action": row["pending_next_action"],
            "survived": True,
        }

    def _budget_blocks_paid(self, *, estimated_cost: float = 1.0) -> bool:
        if self.cost_governor is None:
            return False
        try:
            from cost_governor import PaidActionRequest

            # Prefer authorize API if present
            req = {
                "action_type": "DEEP_RESEARCH",
                "estimated_cost_usd": estimated_cost,
                "priority_tier": 4,
                "voi_score": 0.5,
            }
            if hasattr(self.cost_governor, "authorize"):
                res = self.cost_governor.authorize(req)
                if isinstance(res, dict):
                    return not bool(res.get("authorized", True))
            return False
        except Exception:  # noqa: BLE001
            return False

    def advance(self, canonical_id: str, *, max_auto_steps: int = 8) -> dict[str, Any]:
        """Advance opportunity through free/authorized stages until stop boundary."""
        row = self.store.get(canonical_id)
        if not row:
            return {"error": "missing", "canonical_id": canonical_id}

        steps: list[dict[str, Any]] = []
        for _ in range(max_auto_steps):
            nxt = determine_next_action(row, budget_blocked=self._budget_blocks_paid())
            action = nxt["next_action"]
            steps.append({"next_action": action, "lifecycle": nxt["lifecycle"], "reason": nxt.get("reason")})

            if action in {
                NA_NO_ACTION_REJECTED,
                "NO_ACTION_CLOSED",
                "NO_ACTION_CANCELLED",
                NA_READY_FOR_OPERATOR,
                NA_WAIT_OPERATOR,
                NA_WAIT_COMMERCIAL,
                NA_WAIT_FUNDING,
                "WAIT_COMPLIANCE_RESOLUTION",
                NA_WAIT_BUDGET,
            }:
                break

            if action == NA_WAIT_PACKAGE or (
                row.get("package_access") in {"AUTH_GATED", "REGISTRATION_REQUIRED"}
                and not row.get("line_items")
            ):
                # Preserve gated — do not reject
                row["package_access"] = row.get("package_access") or "AUTH_GATED"
                row["stop_reason"] = "PACKAGE_ACCESS_GATED"
                row["auth_required_for_spec"] = True
                # Still try pipeline with insufficient requirements path
                result = self._run_executable(row)
                row = self.store.apply_pipeline_result(canonical_id, result)
                self.metrics["pipeline_runs"] += 1
                self.metrics["auto_advanced"] += 1
                break

            if action in {NA_QUEUE_RESEARCH, NA_AUTO_CONTINUE}:
                row["research_in_progress"] = True
                row["research_queued"] = True
                # Progressive evidence acquisition before executable pipeline
                if not (row.get("line_items") or row.get("bom")):
                    try:
                        from m3_evidence_acquisition import acquire_evidence

                        allow_paid = not self._budget_blocks_paid(estimated_cost=0.15)
                        acq = acquire_evidence(row, allow_paid=allow_paid)
                        row = acq["row"]
                        self.store._rows[canonical_id] = row
                        self.store.save()
                        self.metrics["evidence_acquisitions"] = int(self.metrics.get("evidence_acquisitions") or 0) + 1
                        if row.get("rejected"):
                            row["lifecycle"] = derive_lifecycle(row)
                            row["pending_next_action"] = determine_next_action(row)
                            self.store._rows[canonical_id] = row
                            self.store.save()
                            steps.append(
                                {
                                    "next_action": "NO_ACTION_REJECTED",
                                    "lifecycle": row.get("lifecycle"),
                                    "reason": row.get("stop_reason"),
                                }
                            )
                            break
                    except Exception as exc:  # noqa: BLE001
                        row["subsystem_failures"] = {
                            **(row.get("subsystem_failures") or {}),
                            "evidence_acquisition": str(exc)[:200],
                        }
                # Attempt public package fill when accessible and BOM empty
                if not (row.get("line_items") or row.get("bom")) and row.get("package_access") not in {
                    "AUTH_GATED",
                    "REGISTRATION_REQUIRED",
                }:
                    row = self._try_public_package(row)
                # Reclassify product with any richer evidence
                if row.get("description") or row.get("line_items") or row.get("evidence_text_excerpt"):
                    cat = classify_product_category(
                        str(row.get("title") or ""),
                        str(row.get("description") or row.get("evidence_text_excerpt") or ""),
                    )
                    row["product_category"] = cat["category"]
                    row["product_audit"] = audit_survivor(row)

                # Commercial panel (wholesale vs public — no false reject)
                try:
                    from m3_commercial_intelligence import build_commercial_research_panel

                    row["commercial_research"] = build_commercial_research_panel(row)
                except Exception:
                    pass

                result = self._run_executable(row)
                row = self.store.apply_pipeline_result(canonical_id, result)
                self.metrics["pipeline_runs"] += 1
                self.metrics["auto_advanced"] += 1

                # Post-pipeline free stages when bid-prep reached or economics known
                row = self._post_pipeline_stages(row)
                self.store._rows[canonical_id] = row
                self.store.apply_pipeline_result(canonical_id, {"deal": row, "stage": row.get("pipeline_stage")})
                row = self.store.get(canonical_id) or row
                continue

            break

        self.store.save()
        final = self.store.get(canonical_id) or row
        return {
            "canonical_id": canonical_id,
            "lifecycle": final.get("lifecycle"),
            "next_action": final.get("pending_next_action"),
            "readiness": final.get("readiness_summary") or readiness_summary(final),
            "steps": steps,
            "operator_actions": final.get("operator_actions") or [],
            "stop_reason": final.get("stop_reason"),
            "DEVELOPMENT_NO_OUTREACH": is_development_no_outreach(),
            "external_action_counts": {
                "supplier_contacts": self.metrics["supplier_contacts"],
                "financier_contacts": self.metrics["financier_contacts"],
                "agency_contacts": self.metrics["agency_contacts"],
                "registrations": self.metrics["registrations"],
                "signatures": self.metrics["signatures"],
                "bids_submitted": self.metrics["bids_submitted"],
                "purchases": self.metrics["purchases"],
            },
        }

    def _try_public_package(self, row: dict[str, Any]) -> dict[str, Any]:
        """Fill BOM from existing public packet/locator — no auth, no outreach."""
        row = deepcopy(row)
        sol = str(row.get("solicitation_number") or row.get("external_id") or "")
        # Reuse known transactional packet if present (public Iowa-style research artifacts)
        if sol:
            packet_path = ARTIFACTS / "transactional_procurement_packets" / f"{sol}.json"
            if packet_path.exists():
                try:
                    import json

                    packet = json.loads(packet_path.read_text(encoding="utf-8"))
                    req = packet.get("requirement") or {}
                    lines = req.get("line_items") or packet.get("line_items") or []
                    if lines:
                        row["line_items"] = lines
                        row["package_acquired"] = True
                        row["package_access"] = "PUBLIC_DETAIL_PAGE"
                        row["documents"] = packet.get("documents") or row.get("documents")
                        return row
                except Exception:  # noqa: BLE001
                    pass
        try:
            from document_locator import locate_procurement_documents

            located = locate_procurement_documents(
                source_url=row.get("detail_url"),
                solicitation_id=sol or None,
                agency=row.get("agency"),
                title=row.get("title"),
                known_document_links=row.get("documents") if isinstance(row.get("documents"), list) else None,
            )
            docs = located.get("documents") or located.get("candidates") or []
            if docs:
                row["documents"] = docs
                row["package_acquired"] = True
                row.setdefault("package_access", "PUBLIC_DETAIL_PAGE")
        except Exception as exc:  # noqa: BLE001
            row["subsystem_failures"] = {
                **(row.get("subsystem_failures") or {}),
                "public_package": str(exc)[:200],
            }
        return row

    def _run_executable(self, row: dict[str, Any]) -> dict[str, Any]:
        opp = {
            "deal_id": row.get("deal_id") or row["canonical_id"],
            "title": row.get("title"),
            "description": row.get("description") or row.get("title"),
            "solicitation_number": row.get("solicitation_number"),
            "agency": row.get("agency"),
            "deadline": row.get("deadline"),
            "bid_deadline": row.get("deadline"),
            "product_classification": row.get("product_classification") or "UNKNOWN",
            "line_items": row.get("line_items") or row.get("bom") or [],
            "requirements_insufficient": not bool(row.get("line_items") or row.get("bom")),
            "auth_required_for_spec": row.get("package_access") in {"AUTH_GATED", "REGISTRATION_REQUIRED"},
            "portal": row.get("detail_url"),
            "state": row.get("jurisdiction") or row.get("state"),
            "jurisdiction": row.get("jurisdiction"),
            "documents": row.get("documents"),
            "compliance_blockers": row.get("compliance_blockers") or [],
            "compliance_resolved": row.get("compliance_resolved") or [],
            # Pass through any fixture economics evidence
            **{
                k: row[k]
                for k in row
                if k
                in {
                    "supplier_unit_cost",
                    "acquisition_cost",
                    "freight_cost",
                    "financing_cost",
                    "transaction_expenses",
                    "risk_allowance",
                    "government_revenue",
                    "revenue",
                    "formal_quote",
                    "funding_deal_specific_confirmed",
                    "pg_operator_approved",
                    "funding_status",
                    "bom",
                    "working_capital_required",
                    "timezone",
                    "supplier_quote",
                    "public_price_total",
                    "freight_amount",
                    "freight_maturity",
                    "freight",
                    "proposed_bid",
                    "financing_cost_amount",
                    "supplier_candidates",
                    "preferred_supplier",
                    "quantities_from_solicitation",
                    "product_category",
                    "synthetic_fixture",
                    "label",
                }
                and row.get(k) is not None
            },
        }
        # Map BOM from fixture-friendly shape
        if not opp["line_items"] and row.get("bom_lines"):
            opp["line_items"] = row["bom_lines"]
            opp["requirements_insufficient"] = False
        return self.pipeline.run(opp)

    def _post_pipeline_stages(self, row: dict[str, Any]) -> dict[str, Any]:
        """Compliance / pricing / commercial plan / draft when evidence permits — no outreach."""
        row = deepcopy(row)
        readiness = str(row.get("operator_readiness") or "")
        stop = str(row.get("stop_reason") or "")

        # Funding requirement when acquisition known
        acq_cost = row.get("acquisition_cost")
        if acq_cost is not None and not row.get("funding_requirement"):
            try:
                row["funding_requirement"] = build_transaction_funding_requirement(
                    acquisition_cost=float(acq_cost),
                    freight=float(row.get("freight_cost") or 0),
                    maximum_financing_cost=float(row.get("financing_cost") or 0) or None,
                    bid_revenue=float(row["government_revenue"]) if row.get("government_revenue") is not None else None,
                    minimum_target_profit_after_financing=10000.0,
                )
            except Exception as exc:  # noqa: BLE001
                row["funding_requirement"] = {
                    "capital_amount": row.get("working_capital_required"),
                    "status": "UNKNOWN",
                    "note": f"verification_required:{str(exc)[:80]}",
                }

        # Financing gate — UNKNOWN ≠ reject
        if row.get("financier_assessments"):
            try:
                gate = evaluate_funding_gate(
                    funding_requirement=row.get("funding_requirement"),
                    compatibility=row.get("financier_assessments") or [],
                    economically_attractive=bool(row.get("economics_supported")),
                )
                row["funding_gate"] = gate
                if gate.get("state") == "TRANSACTION_FUNDING_EXHAUSTED":
                    row["stop_reason"] = "TRANSACTION_FUNDING_EXHAUSTED"
                elif str(gate.get("state") or "").upper() in {"UNKNOWN", "VERIFICATION_REQUIRED", ""}:
                    row.setdefault("funding_status", "FUNDING_VERIFICATION_REQUIRED")
            except Exception:  # noqa: BLE001
                row.setdefault("funding_status", "FUNDING_VERIFICATION_REQUIRED")

        # Bid compliance when governing docs present
        docs = row.get("governing_documents") or row.get("document_texts") or {}
        if docs and not row.get("bid_compliance"):
            try:
                from bid_compliance_engine import analyze_bid_compliance

                row["bid_compliance"] = analyze_bid_compliance(
                    solicitation_id=row["canonical_id"],
                    documents=row.get("documents") if isinstance(row.get("documents"), list) else None,
                    document_texts=docs if isinstance(docs, dict) else {},
                )
                unresolved = (row["bid_compliance"] or {}).get("mandatory_unresolved") or (
                    row["bid_compliance"] or {}
                ).get("unresolved")
                if unresolved:
                    row["compliance_blockers"] = list(unresolved)
                    row["stop_reason"] = "COMPLIANCE_REVIEW_REQUIRED"
                    row["operator_readiness"] = "COMPLIANCE_REVIEW_REQUIRED"
            except Exception as exc:  # noqa: BLE001
                row["subsystem_failures"] = {
                    **(row.get("subsystem_failures") or {}),
                    "bid_compliance": str(exc)[:200],
                }

        # Pricing only when acquisition evidence exists — never fabricate
        acq = row.get("acquisition_cost") or row.get("supplier_unit_cost")
        if acq is not None and not row.get("bid_pricing"):
            try:
                from bid_pricing_engine import analyze_bid_pricing

                row["bid_pricing"] = analyze_bid_pricing(
                    solicitation_id=row["canonical_id"],
                    line_items=row.get("line_items") or row.get("bom") or [],
                    freight_cost=float(row.get("freight_cost") or 0) or None,
                    financing_cost=float(row.get("financing_cost") or 0) or None,
                    transaction_expense=float(row.get("transaction_expenses") or 0),
                    risk_allowance=float(row.get("risk_allowance") or 0) or None,
                )
            except Exception as exc:  # noqa: BLE001
                row["subsystem_failures"] = {
                    **(row.get("subsystem_failures") or {}),
                    "bid_pricing": str(exc)[:200],
                }
        elif acq is None and readiness in {"READY_FOR_BID_PREPARATION", "QUOTE_REQUIRED", "ECONOMICS_REVIEW"}:
            row["bid_price_status"] = "BID_PRICE_REQUIRES_COMMERCIAL_VERIFICATION"
            row["commercial_verification_required"] = True

        # Commercial verification plan (future actions only)
        if not row.get("commercial_verification_plan") and readiness not in {"REJECTED"}:
            try:
                from commercial_verification_plan import build_commercial_verification_plan

                plan = build_commercial_verification_plan(
                    opportunity_id=row["canonical_id"],
                    acquisition_estimate=float(acq) if acq is not None else None,
                    acquisition_confidence="ESTIMATE" if acq is not None else "UNKNOWN",
                    freight_estimate=float(row["freight_cost"]) if row.get("freight_cost") is not None else None,
                    financing_required=True,
                    financing_amount=float(row["working_capital_required"])
                    if row.get("working_capital_required") is not None
                    else None,
                    financing_confidence="UNKNOWN",
                )
                row["commercial_verification_plan"] = plan
                for item in (plan.get("items") or plan.get("verification_items") or plan.get("targets") or [])[:20]:
                    if isinstance(item, dict):
                        item["action_timing"] = "FUTURE_ACTION_IF_PURSUED"
                        item["executed"] = False
            except Exception as exc:  # noqa: BLE001
                row["subsystem_failures"] = {
                    **(row.get("subsystem_failures") or {}),
                    "commercial_verification_plan": str(exc)[:200],
                }

        # Draft bid when bid-prep and not blocked on unknown acquisition
        if readiness == "READY_FOR_BID_PREPARATION" and not row.get("draft_bid_package"):
            if row.get("bid_price_status") == "BID_PRICE_REQUIRES_COMMERCIAL_VERIFICATION" or acq is None:
                row["draft_bid_ready"] = False
                row["ready_for_operator"] = True
                row["stop_reason"] = row.get("stop_reason") or "COMMERCIAL_VERIFICATION_REQUIRED"
            else:
                try:
                    from draft_bid_assembly import assemble_draft_bid_package

                    draft = assemble_draft_bid_package(
                        solicitation_id=row["canonical_id"],
                        solicitation_number=row.get("solicitation_number"),
                        line_pricing=row.get("bid_pricing"),
                        compliance_matrix=(row.get("bid_compliance") or {}).get("matrix"),
                        commercial_targets=row.get("commercial_verification_plan"),
                    )
                    row["draft_bid_package"] = draft
                    row["draft_bid_ready"] = True
                    row["ready_for_operator"] = True
                    assert is_development_no_outreach()
                except Exception as exc:  # noqa: BLE001
                    row["subsystem_failures"] = {
                        **(row.get("subsystem_failures") or {}),
                        "draft_bid": str(exc)[:200],
                    }
                    row["ready_for_operator"] = True

        self.metrics["external_actions_executed"] = 0
        return row

    def run_from_discovery_batch(self, records: list[dict[str, Any]], *, advance: bool = True) -> dict[str, Any]:
        results = []
        for r in records:
            ing = self.ingest_discovery_record(r)
            if advance and ing.get("survived"):
                adv = self.advance(ing["canonical_id"])
                results.append({**ing, "advance": adv})
            else:
                results.append(ing)
        self.store.save()
        return {
            "kind": "M3EndToEndBatch",
            "count": len(results),
            "metrics": self.metrics,
            "results": results,
            "operator_queue": self.store.operator_queue(),
            "DEVELOPMENT_NO_OUTREACH": True,
        }

    def resume_after_restart(self, canonical_id: str) -> dict[str, Any]:
        """Reload durable state and continue — no duplicate paid charge."""
        # Force reload from disk
        self.store._load()
        row = self.store.get(canonical_id)
        if not row:
            return {"error": "missing_after_restart"}
        return self.advance(canonical_id)


def golden_path_fixture_opportunity() -> dict[str, Any]:
    """SYNTHETIC/FIXTURE complete product-resale solicitation — never a live deal."""
    return {
        "title": "SYNTHETIC FIXTURE — Purchase of 50 Dell Latitude Laptops",
        "description": "Firm-fixed-price supply of laptop computers; brand or equal. Delivery to agency warehouse. No installation services.",
        "solicitation_number": "M3-FIX-GOLDEN-001",
        "external_id": "M3-FIX-GOLDEN-001",
        "agency": "Fixture City Procurement",
        "source_id": "fixture_synthetic",
        "detail_url": "https://example.invalid/fixture/M3-FIX-GOLDEN-001",
        "deadline": "2099-12-15T17:00:00",
        "status": "OPEN",
        "product_classification": "CORE_PRODUCT",
        "product_category": "IT_COMPUTERS",
        "package_access": "PUBLIC_DIRECT",
        "documents": [{"url": "https://example.invalid/fixture/spec.pdf", "kind": "specification"}],
        "line_items": [
            {
                "line_number": 1,
                "description": "Dell Latitude 5540 or equal laptop",
                "quantity": 50,
                "unit": "EA",
                "manufacturer": "Dell",
                "brand_or_equal": True,
            }
        ],
        "bom_lines": [
            {
                "line_number": 1,
                "description": "Dell Latitude 5540 or equal laptop",
                "quantity": 50,
                "unit": "EA",
                "manufacturer": "Dell",
            }
        ],
        "quantities_from_solicitation": True,
        "supplier_candidates": [
            {"name": "Fixture IT Supply Co", "supplier": "Fixture IT Supply Co", "source": "SYNTHETIC_FIXTURE"}
        ],
        "preferred_supplier": "Fixture IT Supply Co",
        # Fixture-provided commercial evidence (NOT live outreach)
        "supplier_quote": {
            "quoted_total": 39000.0,
            "freight_included": False,
            "notes": "SYNTHETIC_FIXTURE_EVIDENCE — NON-LIVE",
        },
        "public_price_total": None,
        "freight_amount": 1200.0,
        "freight_maturity": "ESTIMATE",
        "acquisition_cost": 39000.0,
        "supplier_unit_cost": 780.0,
        "freight_cost": 1200.0,
        "financing_cost": 800.0,
        "transaction_expenses": 200.0,
        "risk_allowance": 500.0,
        "government_revenue": 52000.0,
        "revenue": 52000.0,
        "proposed_bid": 52000.0,
        "working_capital_required": 41000.0,
        "financing_cost_amount": 800.0,
        "formal_quote": True,
        "funding_deal_specific_confirmed": True,
        "funding_status": "VERIFIED_PRE_BID_PATH",
        "pg_operator_approved": True,
        "synthetic_fixture": True,
        "label": "SYNTHETIC_FIXTURE",
        "timezone": "America/Chicago",
    }
