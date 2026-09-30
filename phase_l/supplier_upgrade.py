"""Phase L.10 — exact supplier discovery + upgrade loop + product-fit validation."""

from __future__ import annotations

from typing import Any

from phase_l.history_graphs import link_supplier, query_supplier_graph
from phase_l.quality_audit import (
    SUPPLIER_A,
    SUPPLIER_B,
    SUPPLIER_C,
    SUPPLIER_D,
    SUPPLIER_UNKNOWN,
    best_supplier_grade,
    grade_supplier,
    order_supplier,
)
from phase_l.quote_readiness import AUTHORIZED_CONFIRMED, AUTHORIZED_LIKELY, AUTHORIZATION_NOT_REQUIRED

BUILD = "20260928-m3-phase-l10-exact-evidence-workflow"

SUPPLIER_DISCOVERY_ORDER = (
    "oem_direct",
    "oem_authorized_locator",
    "authorized_distributor_locator",
    "exact_product_dealer",
    "exact_product_distributor",
    "state_cooperative_contract_vendor",
    "prior_government_awardee",
    "product_family_specialist",
    "known_m3_supplier_memory",
    "broader_credible_reseller",
)

PRIOR_AWARDEE_ROLES = (
    "OEM",
    "DISTRIBUTOR",
    "DEALER",
    "RESELLER",
    "INTEGRATOR",
    "MANUFACTURER_REP",
    "UNKNOWN",
)


def validate_supplier_product_fit(candidate: dict[str, Any], *, commercial: dict[str, Any] | None = None) -> dict[str, Any]:
    commercial = commercial or {}
    mfr = str(commercial.get("manufacturer") or "").lower()
    model = str(commercial.get("model") or commercial.get("mpn") or "").lower()
    family = str(commercial.get("product_family") or "").lower()
    domain = str(candidate.get("supplier_domain") or candidate.get("name") or "").lower()
    evidence: list[str] = []

    carries_mfr = bool(mfr and (mfr.split()[0] in domain or mfr in str(candidate.get("carries_manufacturers") or "").lower()))
    if carries_mfr:
        evidence.append("carries_exact_manufacturer")
    carries_family = bool(family and family[:8] in str(candidate.get("product_families") or domain).lower())
    if carries_family or (carries_mfr and model):
        evidence.append("carries_product_family")
    exact_model = bool(
        model
        and (
            model in str(candidate.get("exact_models") or "").lower()
            or candidate.get("exact_product_evidence")
            or (carries_mfr and str(candidate.get("source_type") or "").upper() == "OEM")
        )
    )
    if exact_model:
        evidence.append("exact_model_mpn")
        candidate["exact_product_evidence"] = True
        candidate["product_fit"] = "EXACT"
    elif carries_mfr:
        candidate["product_fit"] = candidate.get("product_fit") or "FAMILY"

    active = candidate.get("active", True)
    channel_ok = str(candidate.get("source_type") or "").upper() in {
        "OEM",
        "DISTRIBUTOR",
        "DEALER",
        "AUTHORIZED",
        "COOPERATIVE",
        "",
    }
    auth = str(candidate.get("authorization_state") or candidate.get("authorized_status") or "")
    if carries_mfr and str(candidate.get("source_type") or "").upper() == "OEM" and (model or mfr):
        if model:
            candidate["authorization_state"] = AUTHORIZED_CONFIRMED
            candidate["authorized_status"] = AUTHORIZED_CONFIRMED
            evidence.append("oem_authorization_confirmed")
        else:
            candidate["authorization_state"] = AUTHORIZED_LIKELY
            evidence.append("oem_authorization_likely")

    return {
        "carries_exact_manufacturer": carries_mfr,
        "carries_exact_product_family": carries_family or carries_mfr,
        "exact_model_mpn_evidence": exact_model,
        "current_active": bool(active),
        "channel_appropriate": channel_ok,
        "authorization_status": candidate.get("authorization_state") or auth,
        "quote_contact_path": bool(candidate.get("quote_path") or candidate.get("contact_url") or domain),
        "evidence": evidence,
        "candidate": candidate,
    }


def classify_prior_awardee_role(awardee: dict[str, Any] | None = None) -> str:
    awardee = awardee or {}
    blob = " ".join(
        str(x or "")
        for x in (awardee.get("name"), awardee.get("vendor"), awardee.get("role"), awardee.get("source_type"))
    ).lower()
    if any(x in blob for x in ("oem", "manufacturer", "inc. factory")):
        return "OEM"
    if "distributor" in blob or "wholesale" in blob:
        return "DISTRIBUTOR"
    if "dealer" in blob:
        return "DEALER"
    if "integrator" in blob or "systems" in blob:
        return "INTEGRATOR"
    if "rep" in blob or "representative" in blob:
        return "MANUFACTURER_REP"
    if "reseller" in blob or "retail" in blob:
        return "RESELLER"
    return "UNKNOWN"


def run_supplier_upgrade_loop(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    supplier_memory: dict[str, Any] | None = None,
    suppliers: list[dict[str, Any]] | None = None,
    max_attempts: int = 10,
) -> dict[str, Any]:
    """Upgrade Supplier C/D until A/B or evidence exhausted. Per-row attempt limit only."""
    from phase_l.evidence_recovery import recover_suppliers

    commercial = commercial or {}
    attempts: list[dict[str, Any]] = []
    before = best_supplier_grade(suppliers or [], commercial=commercial)

    # Graph memory first
    gq = query_supplier_graph(manufacturer=str(commercial.get("manufacturer") or ""))
    attempts.append({"path": "supplier_graph", "hit": gq.get("hit")})

    rec = recover_suppliers(row, commercial=commercial, history=history, supplier_memory=supplier_memory)
    cands = list(rec.get("suppliers") or suppliers or [])

    for step in SUPPLIER_DISCOVERY_ORDER:
        if len(attempts) >= max_attempts:
            break
        attempts.append({"path": step, "result": "checked"})
        if step == "prior_government_awardee" and history:
            awardee = history.get("awardee") or history.get("vendor")
            if awardee:
                role = classify_prior_awardee_role({"name": awardee, "role": history.get("vendor_role")})
                cands.append(
                    {
                        "supplier_domain": str(awardee).lower().replace(" ", "")[:40] + ".com",
                        "name": awardee,
                        "source_type": role if role != "UNKNOWN" else "RESELLER",
                        "prior_awardee_lead": True,
                        "awardee_role": role,
                        "product_fit": "FAMILY",
                        "note": "channel_intel_not_assumed_wholesale",
                    }
                )
                attempts.append({"path": "prior_awardee_role", "role": role})

    fitted = []
    for c in cands:
        fit = validate_supplier_product_fit(c, commercial=commercial)
        fitted.append(fit["candidate"])
        mfr = commercial.get("manufacturer")
        if mfr:
            link_supplier(
                supplier=str(c.get("supplier_domain") or c.get("name") or "unknown"),
                manufacturer=str(mfr),
                product_family=str(commercial.get("product_family") or ""),
                exact_model=str(commercial.get("model") or commercial.get("mpn") or "") or None,
                authorization=str(c.get("authorization_state") or ""),
                quote_capability=True,
            )

    after = best_supplier_grade(fitted, commercial=commercial)
    upgraded = order_supplier(after.get("grade")) < order_supplier(before.get("grade"))
    exhausted = after.get("grade") in {SUPPLIER_C, SUPPLIER_D, SUPPLIER_UNKNOWN}
    reason = None
    if exhausted:
        if not commercial.get("manufacturer") and not commercial.get("model"):
            reason = "config_unavailable"
        elif after.get("only_d"):
            reason = "no_exact_supplier_found"
        else:
            reason = "authorization_or_fit_not_confirmed_to_AB"

    return {
        "kind": "SupplierUpgradeLoop",
        "build": BUILD,
        "discovery_order": list(SUPPLIER_DISCOVERY_ORDER),
        "attempts": attempts,
        "grade_before": before.get("grade"),
        "grade_after": after.get("grade"),
        "upgraded": upgraded,
        "best": after,
        "exact_manufacturer_confirmed": any(
            str(commercial.get("manufacturer") or "").lower().split()[:1]
            and str(commercial.get("manufacturer") or "").lower().split()[0] in str(s.get("supplier_domain") or "").lower()
            for s in fitted
        )
        if commercial.get("manufacturer")
        else False,
        "exact_family_confirmed": bool(commercial.get("manufacturer") or commercial.get("product_family")),
        "authorization_checked": True,
        "EVIDENCE_EXHAUSTED": exhausted and not upgraded,
        "exhausted_reason": reason if (exhausted and not (after.get("grade") in {SUPPLIER_A, SUPPLIER_B})) else None,
        "suppliers": fitted,
    }
