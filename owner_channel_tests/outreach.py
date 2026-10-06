"""Phases 5–6 — Owner quote request text + supplier-friendly exports."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from m3_data_root import data_path
from owner_channel_tests.models import BUILD, EXPORTS_DIR, OUTREACH
from owner_channel_tests.supplier_contacts import contact_for_domain


def build_quote_request(packet: dict[str, Any], *, contact: dict[str, Any] | None = None) -> dict[str, Any]:
    supplier = (packet.get("supplier") or {}).get("supplier_name") or "Supplier"
    domain = (packet.get("supplier") or {}).get("domain") or ""
    contact = contact or contact_for_domain(domain)
    buyer = packet.get("buyer") or ""
    sol = packet.get("solicitation_id") or ""
    dest = packet.get("delivery_destination") or "[confirm delivery address]"
    deadline = packet.get("deadline") or "[confirm return date]"
    n = packet.get("line_count") or len(packet.get("lines") or [])

    subject = f"Quote request — {buyer} solicitation {sol} ({n} line items, NEW)"
    body = (
        f"Hello {supplier} team,\n\n"
        f"Please provide a firm quote for the attached / listed NEW products for "
        f"solicitation {sol} ({buyer}).\n\n"
        f"Requested quote return: {deadline}\n"
        f"Delivery destination: {dest}\n"
        f"Condition: NEW only\n\n"
        f"For each line, please include:\n"
        f"- Unit price\n"
        f"- Extended price\n"
        f"- Availability\n"
        f"- Lead time\n"
        f"- Freight (separate line preferred)\n"
        f"- Payment terms\n"
        f"- Quote validity / expiration\n\n"
        f"A line schedule (Manufacturer / MPN / Qty / UOM) is included with this request.\n\n"
        f"Thank you,\n"
    )
    return {
        "packet_id": packet.get("packet_id"),
        "opportunity_id": packet.get("opportunity_id"),
        "supplier": supplier,
        "domain": domain,
        "subject": subject,
        "body": body,
        "contact_route_class": contact.get("contact_route_class"),
        "contact_page": contact.get("sales_quote_contact_page"),
        "phone": contact.get("sales_phone_public"),
        "account_requirement": contact.get("account_requirement"),
        "do_not_send_automatically": True,
        "READY": True,
    }


def export_packet_files(packet: dict[str, Any]) -> dict[str, str]:
    root = data_path(EXPORTS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    pid = packet.get("packet_id") or "packet"
    csv_path = root / f"{pid}_lines.csv"
    txt_path = root / f"{pid}_summary.txt"
    json_path = root / f"{pid}_packet.json"

    fields = [
        "Line",
        "Manufacturer",
        "MPN/Model",
        "Description",
        "Qty",
        "UOM",
        "Pack",
        "Condition",
        "Delivery Destination",
        "Notes",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, ln in enumerate(packet.get("lines") or [], 1):
            w.writerow(
                {
                    "Line": i,
                    "Manufacturer": ln.get("manufacturer") or "",
                    "MPN/Model": ln.get("mpn") or ln.get("model") or "",
                    "Description": (ln.get("raw_description") or "")[:200],
                    "Qty": ln.get("qty"),
                    "UOM": ln.get("uom") or "EA",
                    "Pack": ln.get("pack") or 1,
                    "Condition": ln.get("condition") or "NEW",
                    "Delivery Destination": packet.get("delivery_destination") or "",
                    "Notes": "",
                }
            )

    req = build_quote_request(packet)
    summary = (
        f"{req['subject']}\n\n{req['body']}\n"
        f"--- LINE COUNT: {packet.get('line_count')} ---\n"
        f"Export CSV: {csv_path.name}\n"
        f"DO NOT AUTO-SEND\n"
    )
    txt_path.write_text(summary, encoding="utf-8")
    json_path.write_text(json.dumps(packet, indent=2, default=str), encoding="utf-8")

    # XLSX-compatible TSV twin
    tsv_path = root / f"{pid}_lines.tsv"
    with tsv_path.open("w", encoding="utf-8", newline="") as f:
        f.write("\t".join(fields) + "\n")
        for i, ln in enumerate(packet.get("lines") or [], 1):
            row = [
                str(i),
                str(ln.get("manufacturer") or ""),
                str(ln.get("mpn") or ln.get("model") or ""),
                (ln.get("raw_description") or "")[:200].replace("\t", " "),
                str(ln.get("qty") or ""),
                str(ln.get("uom") or "EA"),
                str(ln.get("pack") or 1),
                str(ln.get("condition") or "NEW"),
                str(packet.get("delivery_destination") or ""),
                "",
            ]
            f.write("\t".join(row) + "\n")

    return {
        "csv": str(csv_path),
        "tsv": str(tsv_path),
        "summary_txt": str(txt_path),
        "packet_json": str(json_path),
    }


def build_all_outreach(corpus: dict[str, Any], contacts: dict[str, Any]) -> dict[str, Any]:
    by_domain = {(s.get("domain") or ""): s for s in contacts.get("suppliers") or []}
    items = []
    for pkt in corpus.get("packets") or []:
        domain = (pkt.get("supplier") or {}).get("domain") or ""
        contact = by_domain.get(domain) or contact_for_domain(domain)
        req = build_quote_request(pkt, contact=contact)
        exports = export_packet_files(pkt)
        items.append({**req, "exports": exports, "lines": pkt.get("line_count")})
    payload = {"build": BUILD, "requests": items, "do_not_send_automatically": True}
    data_path(OUTREACH).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload
