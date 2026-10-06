"""Phases 19–20 — Supplier intelligence memory for future routing."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from owner_channel_tests.models import SUPPLIER_MEMORY


def update_supplier_memory(
    *,
    supplier_key: str,
    supplier_name: str,
    domain: str,
    manufacturers: list[str],
    categories: list[str],
    response_state: str | None = None,
    terms: str | None = None,
    freight: str | None = None,
    account_requirement: str | None = None,
    contact_path: str | None = None,
    response_time_hours: float | None = None,
    mpn_families: list[str] | None = None,
    quality_notes: str | None = None,
) -> dict[str, Any]:
    store = {}
    p = data_path(SUPPLIER_MEMORY)
    if p.exists():
        store = json.loads(p.read_text(encoding="utf-8"))
    by = store.setdefault("by_supplier", {})
    row = by.get(supplier_key) or {
        "supplier_key": supplier_key,
        "supplier_name": supplier_name,
        "domain": domain,
        "interactions": 0,
    }
    row["interactions"] = int(row.get("interactions") or 0) + 1
    row["manufacturers"] = sorted(set((row.get("manufacturers") or []) + manufacturers))
    row["categories"] = sorted(set((row.get("categories") or []) + categories))
    if response_state:
        row["last_response_state"] = response_state
    if terms:
        row["payment_terms"] = terms
    if freight:
        row["freight_behavior"] = freight
    if account_requirement:
        row["account_requirement"] = account_requirement
    if contact_path:
        row["contact_path"] = contact_path
    if response_time_hours is not None:
        times = list(row.get("response_times_hours") or [])
        times.append(response_time_hours)
        row["response_times_hours"] = times[-20:]
        row["avg_response_hours"] = round(sum(times) / len(times), 2)
    if mpn_families:
        row["mpn_families"] = sorted(set((row.get("mpn_families") or []) + mpn_families))
    if quality_notes:
        row["quality_notes"] = quality_notes
    row["reusable"] = True
    row["updated_at"] = now_utc().isoformat()
    by[supplier_key] = row
    store["updated_at"] = now_utc().isoformat()
    data_path(SUPPLIER_MEMORY).write_text(json.dumps(store, indent=2), encoding="utf-8")
    return row


def seed_memory_from_contacts(contacts: dict[str, Any], corpus: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for s in contacts.get("suppliers") or []:
        # manufacturers from packets for this domain
        mfrs: set[str] = set()
        cats: set[str] = set()
        for pkt in corpus.get("packets") or []:
            if (pkt.get("supplier") or {}).get("domain") == s.get("domain"):
                for ln in pkt.get("lines") or []:
                    if ln.get("manufacturer"):
                        mfrs.add(str(ln["manufacturer"]))
                cluster = str(pkt.get("cluster_key") or "")
                if cluster:
                    cats.add(cluster)
        row = update_supplier_memory(
            supplier_key=s.get("supplier_key") or s.get("domain"),
            supplier_name=s.get("supplier_name") or "",
            domain=s.get("domain") or "",
            manufacturers=sorted(mfrs)[:40],
            categories=sorted(cats),
            response_state="NOT_CONTACTED",
            account_requirement=s.get("account_requirement"),
            contact_path=s.get("sales_quote_contact_page"),
            quality_notes="Seeded from owner-channel corpus — no private contact dump",
        )
        rows.append(row)
    return rows
