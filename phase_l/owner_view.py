"""Phase L owner-facing presentation chips — Access / Competition / Economics / Source."""

from __future__ import annotations

from typing import Any


def _registration_action_label(action: str | None) -> str | None:
    if not action or action == "NONE":
        return None
    labels = {
        "REGISTER_NOW_RECURRING_BUYER": "REGISTER NOW — RECURRING BUYER",
        "REGISTER_BEFORE_BID": "REGISTER BEFORE BID",
        "REGISTER_NOW": "REGISTER NOW",
        "VERIFY_REGISTRATION_TIMING": "VERIFY REGISTRATION TIMING",
        "BLOCKED": "TRUE BLOCKER",
    }
    return labels.get(str(action), str(action).replace("_", " "))


def build_owner_view(opp: dict[str, Any]) -> dict[str, Any]:
    """Compact owner packet for UI — no redesign, just clear chips."""
    access = opp.get("access") if isinstance(opp.get("access"), dict) else {}
    comp = opp.get("competition") if isinstance(opp.get("competition"), dict) else {}
    econ = opp.get("economics") if isinstance(opp.get("economics"), dict) else {}
    reg_action = access.get("registration_action") or opp.get("registration_action")
    our = opp.get("our_bid_access") or access.get("our_bid_access")
    registration_actions: list[dict[str, Any]] = []
    if our == "YES" and reg_action and reg_action != "NONE":
        registration_actions.append(
            {
                "label": _registration_action_label(reg_action),
                "action": reg_action,
                "portal": opp.get("source_portal")
                or access.get("portal_source")
                or opp.get("source_id"),
            }
        )
    return {
        "kind": "PhaseLOwnerView",
        "access": {
            "can_we_bid": our,
            "why": opp.get("access_blocker") or access.get("access_blocker") or access.get("eligibility", {}).get("plain"),
            "vehicle_required": (opp.get("competition_access_type") or "")
            in {
                "VEHICLE_ONLY",
                "BPA_ONLY",
                "IDIQ_ONLY",
                "GWAC_ONLY",
                "MAS_ONLY",
                "SEWP_ONLY",
            },
            "set_aside": opp.get("set_aside"),
            "competition_access_type": opp.get("competition_access_type"),
            "registration_issue": bool(access.get("vendor_registration_required")),
            "can_compete": access.get("can_compete")
            if access.get("can_compete") is not None
            else our == "YES",
            "submission_readiness": access.get("submission_readiness") or opp.get("submission_readiness"),
            "registration_action": reg_action,
            "registration_gate_type": access.get("registration_gate_type")
            or opp.get("registration_gate_type"),
            "is_easy_registration": bool(
                access.get("is_easy_registration") or opp.get("is_easy_registration")
            ),
            "readiness_blocker": access.get("readiness_blocker") or opp.get("readiness_blocker"),
        },
        "registration_actions": registration_actions,
        "competition": {
            "historical_offers": opp.get("historical_offers_received")
            or comp.get("historical_offers_received"),
            "historical_competition_type": opp.get("historical_competition_type")
            or comp.get("historical_competition_type"),
            "effective_competition_signal": opp.get("effective_competition_signal")
            or comp.get("effective_competition_signal"),
            "open_comparable": comp.get("is_open_comparable"),
        },
        "economics": {
            "historical_unit_price": econ.get("historical_unit_price")
            or opp.get("historical_award_unit_price"),
            "public_retail_unit_price": econ.get("public_retail_unit_price")
            or opp.get("public_retail_price"),
            "quantity": econ.get("quantity") or opp.get("quantity"),
            "uom": econ.get("uom") or opp.get("uom"),
            "gross_retail_spread": econ.get("gross_retail_spread"),
            "financing_cost": econ.get("estimated_financing_cost") or opp.get("financing_cost"),
            "direct_costs": econ.get("estimated_direct_costs") or opp.get("direct_costs"),
            "expected_revenue": econ.get("expected_revenue") or opp.get("expected_revenue"),
            "expected_net_profit": econ.get("expected_net_profit") or opp.get("expected_net_profit"),
            "profit_tier": econ.get("profit_tier") or opp.get("profit_tier"),
        },
        "source": {
            "source_level": opp.get("source_level"),
            "source_portal": opp.get("source_portal"),
            "source_url": opp.get("source_url"),
            "last_live_verification_at": opp.get("last_live_verification_at"),
            "jurisdiction": opp.get("jurisdiction"),
        },
        "owner_decision": opp.get("owner_decision") or opp.get("actionable_state"),
    }


def render_owner_view_html(view: dict[str, Any]) -> str:
    """Minimal HTML fragment for deal room injection."""
    a = view.get("access") or {}
    c = view.get("competition") or {}
    e = view.get("economics") or {}
    s = view.get("source") or {}
    reg_items = view.get("registration_actions") or []
    reg_html = ""
    if reg_items:
        lis = "".join(
            f"<li><strong>{r.get('label') or r.get('action')}</strong>"
            f" — {r.get('portal') or 'portal'}</li>"
            for r in reg_items
        )
        reg_html = f"<h3>Registration actions</h3><ul class=\"phase-l-reg-actions\">{lis}</ul>"
    return f"""
<section class="phase-l-owner-view" data-phase="L">
  <h3>Phase L — Access</h3>
  <dl>
    <div><dt>Can we bid?</dt><dd>{a.get('can_we_bid') or '—'}</dd></div>
    <div><dt>Submission ready?</dt><dd>{a.get('submission_readiness') or '—'}</dd></div>
    <div><dt>Why</dt><dd>{a.get('why') or '—'}</dd></div>
    <div><dt>Vehicle required?</dt><dd>{'yes' if a.get('vehicle_required') else 'no'}</dd></div>
    <div><dt>Set-aside</dt><dd>{a.get('set_aside') or '—'}</dd></div>
    <div><dt>Access type</dt><dd>{a.get('competition_access_type') or '—'}</dd></div>
    <div><dt>Easy registration?</dt><dd>{'yes' if a.get('is_easy_registration') else 'no'}</dd></div>
  </dl>
  {reg_html}
  <h3>Competition</h3>
  <dl>
    <div><dt>Historical offers</dt><dd>{c.get('historical_offers') if c.get('historical_offers') is not None else '—'}</dd></div>
    <div><dt>Competition type</dt><dd>{c.get('historical_competition_type') or '—'}</dd></div>
    <div><dt>Effective signal</dt><dd>{c.get('effective_competition_signal') or '—'}</dd></div>
  </dl>
  <h3>Economics</h3>
  <dl>
    <div><dt>Hist unit</dt><dd>{e.get('historical_unit_price') if e.get('historical_unit_price') is not None else '—'}</dd></div>
    <div><dt>Retail unit</dt><dd>{e.get('public_retail_unit_price') if e.get('public_retail_unit_price') is not None else '—'}</dd></div>
    <div><dt>Qty</dt><dd>{e.get('quantity') or '—'} {e.get('uom') or ''}</dd></div>
    <div><dt>Gross spread</dt><dd>{e.get('gross_retail_spread') if e.get('gross_retail_spread') is not None else '—'}</dd></div>
    <div><dt>Financing</dt><dd>{e.get('financing_cost') if e.get('financing_cost') is not None else '—'}</dd></div>
    <div><dt>Direct costs</dt><dd>{e.get('direct_costs') if e.get('direct_costs') is not None else '—'}</dd></div>
    <div><dt>Expected net</dt><dd>{e.get('expected_net_profit') if e.get('expected_net_profit') is not None else '—'}</dd></div>
    <div><dt>Profit tier</dt><dd>{e.get('profit_tier') or '—'}</dd></div>
  </dl>
  <h3>Source</h3>
  <dl>
    <div><dt>Level</dt><dd>{s.get('source_level') or '—'}</dd></div>
    <div><dt>Portal</dt><dd>{s.get('source_portal') or '—'}</dd></div>
    <div><dt>URL</dt><dd>{s.get('source_url') or '—'}</dd></div>
    <div><dt>Verified</dt><dd>{s.get('last_live_verification_at') or '—'}</dd></div>
  </dl>
  <p class="phase-l-decision"><strong>Owner decision:</strong> {view.get('owner_decision') or '—'}</p>
</section>
""".strip()
