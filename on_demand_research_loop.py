"""Bounded OnDemandResearchLoop — search → retrieve temp → analyze → extract → release."""

from __future__ import annotations

from typing import Any

from application_clock import CLOCK_HISTORICAL_SIMULATION, clock_mode, knowledge_cutoff_at
from deal_research_record import deal_research_record
from document_locator import locate_procurement_documents
from information_need_router import (
    NEED_FINANCING_INFORMATION,
    NEED_SPECIFICATION,
    InformationSourceRouter,
)
from procurement_source_knowledge import ProcurementSourceKnowledgeBase
from reusable_knowledge import ReusableKnowledgeStore, buyer_fact
from temporary_retrieval import TemporaryRetrievalStore
from temporal_evidence_api import TemporalEvidenceStore, get_evidence_available_as_of
from temporal_evidence_firewall import make_temporal_evidence
from historical_case_constants import ROLE_PRE_BID_REQUIREMENT

# Stop conditions
STOP_DEAL_REJECTED = "DEAL_REJECTED"
STOP_BID_READY = "BID_READY"
STOP_OPERATOR_ACTION = "OPERATOR_ACTION_REQUIRED"
STOP_AUTH_REQUIRED = "AUTH_REQUIRED"
STOP_QUOTE_REQUIRED = "QUOTE_REQUIRED"
STOP_FUNDING_VERIFICATION = "FUNDING_VERIFICATION_REQUIRED"
STOP_NO_MORE_PUBLIC = "NO_MORE_PUBLIC_EVIDENCE"
STOP_BUDGET = "REQUEST_BUDGET_REACHED"
STOP_MAX_ITER = "MAX_ITERATIONS"


class OnDemandResearchLoop:
    def __init__(
        self,
        *,
        knowledge: ProcurementSourceKnowledgeBase | None = None,
        temp_store: TemporaryRetrievalStore,
        reusable: ReusableKnowledgeStore | None = None,
        max_http: int = 15,
        max_iterations: int = 8,
    ) -> None:
        self.knowledge = knowledge or ProcurementSourceKnowledgeBase()
        self.router = InformationSourceRouter(self.knowledge)
        self.temp = temp_store
        self.reusable = reusable or ReusableKnowledgeStore()
        self.max_http = max_http
        self.max_iterations = max_iterations
        self._attempted_keys: set[str] = set()
        self.history: list[dict[str, Any]] = []

    def run(
        self,
        deal_state: dict[str, Any],
        *,
        opportunity: dict[str, Any],
        known_document_links: list[dict[str, Any]] | None = None,
        client: Any = None,
        fetch: bool = True,
        historical_evidence_store: TemporalEvidenceStore | None = None,
        benchmark_hidden_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        state = dict(deal_state)
        stop = None
        iterations = 0

        while iterations < self.max_iterations:
            iterations += 1
            if state.get("rejected"):
                stop = STOP_DEAL_REJECTED
                break
            if state.get("bid_ready"):
                stop = STOP_BID_READY
                break
            if self.temp.stats["requests_attempted"] >= self.max_http:
                stop = STOP_BUDGET
                break

            needs = self.router.missing_needs_from_deal_state(state)
            if not needs:
                stop = STOP_NO_MORE_PUBLIC
                break

            need = needs[0]
            # Defer funding until economics mature
            if need["need_type"] == NEED_FINANCING_INFORMATION and not state.get("cost_established"):
                self.history.append({"skipped": NEED_FINANCING_INFORMATION, "reason": "economics_not_mature"})
                # mark to avoid spin
                state["funding_deferred"] = True
                if len(needs) == 1:
                    stop = STOP_QUOTE_REQUIRED
                    break
                need = needs[1] if len(needs) > 1 else need

            key = f"{need['need_type']}:{opportunity.get('solicitation_number')}"
            if key in self._attempted_keys:
                self.history.append({"deduped": key})
                state.setdefault("attempted_needs", []).append(need["need_type"])
                # prevent infinite loop
                if need["need_type"] == NEED_SPECIFICATION:
                    state["requirements_complete"] = state.get("requirements_complete") or False
                    state["spec_exhausted"] = True
                break
            self._attempted_keys.add(key)

            loc = locate_procurement_documents(
                source=opportunity.get("source"),
                source_url=opportunity.get("source_url"),
                solicitation_id=opportunity.get("solicitation_number"),
                agency=opportunity.get("agency"),
                title=opportunity.get("title"),
                known_metadata=opportunity,
                known_document_links=known_document_links,
                knowledge=self.knowledge,
                router=self.router,
                need_type=need["need_type"],
                benchmark_hidden_urls=benchmark_hidden_urls,
            )
            self.history.append({"need": need, "location_status": loc["overall_status"], "n_candidates": len(loc["candidates"])})

            if loc["overall_status"] == "FOUND_AUTH_REQUIRED":
                stop = STOP_AUTH_REQUIRED
                state["portal_blockers"] = ["AUTH_REQUIRED"]
                state["operator_next_action"] = "download_authorized_attachment"
                break

            public_docs = [c for c in loc["candidates"] if c.get("status") == "FOUND_PUBLIC" and c.get("url")]
            if not public_docs and loc["overall_status"] in {"NOT_FOUND", "UNKNOWN"}:
                if need["need_type"] == NEED_SPECIFICATION:
                    state["spec_exhausted"] = True
                stop = STOP_NO_MORE_PUBLIC
                break

            for cand in public_docs[:2]:
                url = cand["url"]
                if not fetch:
                    self.temp.discover(url, meta={"document_type": cand.get("document_type")})
                    continue
                # Historical mode: register through temporal firewall; don't treat retrieval date as availability
                if clock_mode() == CLOCK_HISTORICAL_SIMULATION and historical_evidence_store is not None:
                    cutoff = knowledge_cutoff_at()
                    if cutoff is None:
                        stop = STOP_NO_MORE_PUBLIC
                        break
                    # Without defensible historical availability, do not analyze as pre-bid
                    pub = cand.get("temporal_metadata", {}).get("publication_date")
                    te = make_temporal_evidence(
                        title=str(cand.get("document_type")),
                        evidence_role=ROLE_PRE_BID_REQUIREMENT,
                        knowledge_cutoff_at=cutoff,
                        source_publication_date=pub if pub and pub != "UNKNOWN" else None,
                        retrieval_date=None,  # set after retrieve
                        historical_availability_date=pub if pub and pub != "UNKNOWN" else None,
                        payload={"url": url, "document_type": cand.get("document_type")},
                        case_id=opportunity.get("solicitation_number"),
                    )
                    historical_evidence_store.add(te)
                    allowed = get_evidence_available_as_of(
                        historical_evidence_store,
                        cutoff,
                        for_pre_bid_decision=True,
                        case_id=opportunity.get("solicitation_number"),
                    )
                    if not any(e.get("evidence_id") == te["evidence_id"] for e in allowed):
                        self.history.append({"temporal_block": url, "reason": te.get("reason")})
                        continue

                item = self.temp.retrieve(url, client=client, meta={"document_type": cand.get("document_type")})
                if item.get("access") in {"AUTH_REQUIRED", "ACCESS_DENIED"}:
                    stop = STOP_AUTH_REQUIRED
                    break
                if item.get("lifecycle") == "TEMP_RETRIEVED":
                    self.temp.mark_analyzed(item["item_id"])
                    # Extract compact knowledge — not bulk body into deal
                    extracted = {
                        "document_type": cand.get("document_type"),
                        "url_reference": url,
                        "sha256": item.get("sha256"),
                        "bytes": item.get("bytes"),
                    }
                    self.temp.mark_knowledge_extracted(item["item_id"], extracted)
                    state.setdefault("document_manifest_summary", []).append(
                        {
                            "document_type": cand.get("document_type"),
                            "url": url.split("?")[0],  # reference without necessarily storing signed forever
                            "sha256": item.get("sha256"),
                            "signed_query_was_preserved_at_fetch": "X-Amz-" in url,
                        }
                    )
                    if need["need_type"] == NEED_SPECIFICATION:
                        state["requirements_complete"] = False  # partial unless BOM engine says otherwise
                        state["requirements_partial"] = True
                        state["has_spec_evidence"] = True
                    # Learn reusable SciQuest quirk when signed URL works
                    if "X-Amz-" in url and item.get("http_status") == 200:
                        self.knowledge.record_search_learning(
                            source_id="iowa_sciquest_jaggaer",
                            learning_type="signed_url_preserve",
                            pattern="preserve_X-Amz_query_string",
                            success=True,
                            case_specific_url=False,
                        )
                    # Buyer intelligence compact
                    if opportunity.get("agency"):
                        try:
                            self.reusable.add_buyer(
                                buyer_fact(
                                    str(opportunity["agency"]),
                                    portal=opportunity.get("source"),
                                    source_recipe_id="iowa_sciquest_jaggaer"
                                    if "iowa" in str(opportunity.get("agency")).lower()
                                    else None,
                                    source="on_demand_research_loop",
                                    common_categories=[opportunity.get("title") or ""][:1],
                                )
                            )
                        except ValueError:
                            pass

            if stop:
                break

            # If we got something but still missing supplier/cost → quote path
            if state.get("has_spec_evidence") and not state.get("supplier_identified"):
                stop = STOP_QUOTE_REQUIRED
                state["operator_next_action"] = "request_supplier_quote"
                break

        if stop is None:
            stop = STOP_MAX_ITER if iterations >= self.max_iterations else STOP_NO_MORE_PUBLIC

        record = deal_research_record(
            opportunity_id=opportunity.get("opportunity_id"),
            solicitation_number=opportunity.get("solicitation_number"),
            agency=opportunity.get("agency"),
            source=opportunity.get("source"),
            source_url=opportunity.get("source_url"),
            deadline=opportunity.get("deadline"),
            document_manifest_summary=state.get("document_manifest_summary"),
            requirement_summary={
                "partial": state.get("requirements_partial"),
                "complete": state.get("requirements_complete"),
                "has_spec_evidence": state.get("has_spec_evidence"),
            },
            supplier_quote_status="QUOTE_REQUIRED" if stop == STOP_QUOTE_REQUIRED else None,
            portal_blockers=state.get("portal_blockers"),
            operator_next_action=state.get("operator_next_action"),
            notes=f"stop={stop}; iterations={iterations}",
            provenance=[{"clock_mode": clock_mode(), "history_len": len(self.history)}],
        )

        return {
            "stop_reason": stop,
            "iterations": iterations,
            "deal_state": state,
            "deal_record": record,
            "history": self.history,
            "temp_stats": dict(self.temp.stats),
            "reusable_counts": self.reusable.counts(),
            "benchmark_answer_key_used": False,
        }
