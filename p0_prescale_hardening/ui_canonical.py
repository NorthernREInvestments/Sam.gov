"""P0-1 — Canonical operator UI path (backend projection + legacy suppression)."""

from __future__ import annotations

from typing import Any

from p0_prescale_hardening.models import CANONICAL_STAGES, BUILD


def canonical_operator_shell() -> dict[str, Any]:
    return {
        "build": BUILD,
        "canonical_entry": "/ops",
        "canonical_stages": CANONICAL_STAGES,
        "legacy_paths": {
            "/": "REDIRECT → /ops",
            "/index.html": "REDIRECT → /ops (legacy only with ?legacy=1 advanced research)",
            "/index.html?legacy=1": "WRAP — advanced research only; no parallel profit workflow",
            "/supplier_call_desk.html": "REMOVED from operator Settings; advanced-only if needed",
            "/api/m3/mobile/*": "WRAP — read-only/advanced; not primary operator path",
            "/api/m3/pipeline/next-action": "WRAP → canonical next-action engine",
            "m3-mobile.js deal room": "REDIRECT messaging to /ops#/deal",
        },
        "primary_next_action_engine": "p0_prescale_hardening.next_action.determine_canonical_next_action",
        "owner_ui_status_bridge": "phase_l.owner_ui_status.map_funnel_to_owner_status",
        "parallel_operator_workflows": False,
        "duplicate_profit_pages": False,
        "one_opportunity_detail_workflow": True,
        "teachable_minutes_target": "15-30",
    }


def attach_canonical_deal_panel(deal: dict[str, Any], *, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge canonical funnel panel into /api/ui/deals/{id} payload."""
    out = dict(deal)
    shell = canonical_operator_shell()
    snap = snapshot or {}
    out["canonical_funnel"] = {
        "stages": CANONICAL_STAGES,
        "current_stage": snap.get("canonical_stage") or "OPPORTUNITY",
        "next_action": (snap.get("next_action") or {}).get("primary_next_action"),
        "next_action_detail": snap.get("next_action"),
        "material_line_status": snap.get("material_line_status"),
        "quote_packet_status": snap.get("quote_packet_status"),
        "revenue_status": snap.get("revenue_status"),
        "basket_status": snap.get("basket_status"),
        "economics_status": snap.get("economics_status"),
        "execution_risk": snap.get("execution_risk"),
        "do_not_send_automatically": True,
    }
    out["ui_path"] = "CANONICAL_OPERATOR"
    out["legacy_suppressed"] = True
    out["p0_build"] = BUILD
    out["operator_shell"] = {
        "entry": shell["canonical_entry"],
        "teachable_target": shell["teachable_minutes_target"],
    }
    # Prefer canonical next action on the primary button when present
    if snap.get("next_action"):
        nba = snap["next_action"]
        out["next"] = {
            **(out.get("next") or {}),
            "action": nba.get("primary_next_action"),
            "label": nba.get("label"),
            "reason": nba.get("reason"),
            "canonical": True,
        }
    return out
