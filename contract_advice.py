"""Ensure contract pursue/avoid advice is stored on analysis."""

from __future__ import annotations

from typing import Any

from claude_client import generate_contract_advice, normalize_contract_advice


def get_contract_advice(contract: Any) -> dict[str, Any] | None:
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    return normalize_contract_advice(analysis.get("contract_advice"))


def ensure_contract_advice(session, contract: Any, *, force: bool = False) -> dict[str, Any] | None:
    """Generate Claude advice when missing. Requires plain-English summary."""
    existing = get_contract_advice(contract)
    if existing and not force:
        return existing

    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    summary = analysis.get("plain_english_summary") or analysis.get("executive_summary")
    if not summary or not str(summary).strip():
        return existing

    advice = generate_contract_advice(contract, session)
    analysis = dict(analysis)
    analysis["contract_advice"] = advice
    contract.analysis = analysis
    session.commit()
    return advice
