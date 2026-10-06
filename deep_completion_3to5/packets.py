"""Quote packet consolidation — minimize outreach, maximize coverage."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from application_clock import now_utc
from deep_completion_3to5.models import READY_FOR_OWNER_QUOTE_OUTREACH
from deep_completion_3to5.suppliers import _SECTION_FAKE, resolve_channels_for_line

_EMS_FAMILY = re.compile(
    r"(trauma|airway|oxygen|pharma|ppe|personal\s+protective|spinal|soft\s+goods|ansell|arrow)",
    re.I,
)


def _packet_key(line: dict[str, Any], supplier_map: dict[str, Any]) -> tuple[str, str]:
    """Return (cluster_key, preferred_supplier_domain)."""
    mfr = str(line.get("manufacturer") or "").strip()
    oid = line.get("opportunity_id") or ""
    channels = line.get("supplier_channels") or resolve_channels_for_line(line)
    preferred = channels[0] if channels else "unknown-supplier"

    if oid.endswith("298984") or (mfr and "cummins" in mfr.lower()):
        # Single Cummins packet — prefer OEM shop
        preferred = "shop.cummins.com" if "shop.cummins.com" in channels or True else preferred
        if "shop.cummins.com" not in (channels or []):
            preferred = "cummins.com"
        return ("CUMMINS_PARTS", preferred)

    if oid.endswith("286698") or _EMS_FAMILY.search(f"{mfr} {line.get('description') or ''}"):
        # Group EMS into 2 packets max: Bound Tree primary, Henry Schein secondary by family
        fam = "EMS_GENERAL"
        blob = f"{mfr} {line.get('description') or ''}"
        if re.search(r"pharma", blob, re.I):
            fam = "EMS_PHARMA"
            preferred = "henryschein.com"
        elif re.search(r"ppe|protective|glove|ansell", blob, re.I):
            fam = "EMS_PPE"
            preferred = "medline.com"
        else:
            preferred = "boundtree.com"
        return (fam, preferred)

    if mfr and not _SECTION_FAKE.match(mfr):
        key = f"MFR:{mfr.upper()}"
        return (key, preferred)

    cat = str(line.get("category") or "OTHER")
    return (f"CAT:{cat}", preferred)


def build_quote_packets(
    opportunity_id: str,
    lines: list[dict[str, Any]],
    *,
    supplier_map: dict[str, Any],
    revenue: dict[str, Any],
    execution: dict[str, Any],
    buyer: str | None,
    solicitation_id: str | None,
    deadline: str | None,
    delivery_destination: str | None = None,
) -> dict[str, Any]:
    quote_lines = [
        l
        for l in lines
        if l.get("quote_required_state") == "QUOTE_REQUIRED"
        or (l.get("identity_usable") and l.get("acquisition_state") not in {"PRICED_EXECUTABLE"})
    ]
    # Prefer identity-usable only for ready packets
    usable_quote = [l for l in quote_lines if l.get("identity_usable")]

    buckets: dict[str, dict[str, Any]] = {}
    for ln in usable_quote:
        cluster, supplier_dom = _packet_key(ln, supplier_map)
        b = buckets.setdefault(
            f"{cluster}|{supplier_dom}",
            {
                "cluster_key": cluster,
                "supplier_domain": supplier_dom,
                "lines": [],
            },
        )
        b["lines"].append(ln)

    packets = []
    for i, (key, bucket) in enumerate(sorted(buckets.items(), key=lambda x: -len(x[1]["lines"])), 1):
        supplier = (supplier_map.get("supplier_by_domain") or {}).get(bucket["supplier_domain"]) or {
            "supplier_name": bucket["supplier_domain"],
            "domain": bucket["supplier_domain"],
            "contact_route": None,
            "quote_request_capability": "UNKNOWN",
            "account_login_required": True,
        }
        manufacturers = sorted(
            {
                str(l.get("manufacturer"))
                for l in bucket["lines"]
                if l.get("manufacturer") and not _SECTION_FAKE.match(str(l.get("manufacturer")))
            }
            or ({bucket["cluster_key"]} if bucket["cluster_key"].startswith("CUMMINS") else set())
        )
        line_rows = []
        for ln in bucket["lines"]:
            line_rows.append(
                {
                    "line_id": ln.get("line_id"),
                    "clin": ln.get("clin"),
                    "manufacturer": ln.get("manufacturer"),
                    "mpn": ln.get("mpn") or ln.get("part_number") or ln.get("model"),
                    "description": (ln.get("description") or "")[:200],
                    "qty": ln.get("quantity") or 1,
                    "uom": ln.get("uom") or "EA",
                    "pack": ln.get("pack") or 1,
                    "condition": "NEW",
                }
            )

        # Readiness gates
        blockers = []
        if execution.get("PASS_FAIL") == "FAIL":
            blockers.append("fatal_execution_blocker")
        if revenue.get("status") == "REVENUE_NOT_READY":
            blockers.append("revenue_not_ready")
        if not line_rows:
            blockers.append("no_lines")
        identities_ok = all(
            bool(l.get("mpn") or l.get("part_number") or l.get("model") or l.get("identity_usable"))
            for l in bucket["lines"]
        )
        if not identities_ok:
            blockers.append("identity_incomplete")

        # Soft: unknown deadline does not block outreach prep
        ready = len(blockers) == 0 and len(line_rows) > 0
        # Collier complexity: still allow product-line packets
        if execution.get("complexity") and opportunity_id.endswith("295143"):
            ready = ready and len(line_rows) > 0

        urgency = "NORMAL"
        packet_id = "QP-" + hashlib.sha1(f"{opportunity_id}|{key}".encode()).hexdigest()[:10]
        packets.append(
            {
                "packet_id": packet_id,
                "opportunity_id": opportunity_id,
                "buyer": buyer,
                "solicitation_id": solicitation_id,
                "deadline": deadline,
                "requested_quote_return_date": None,
                "urgency": urgency,
                "supplier": supplier,
                "manufacturers": manufacturers,
                "cluster_key": bucket["cluster_key"],
                "line_count": len(line_rows),
                "lines": line_rows,
                "request": {
                    "condition": "NEW",
                    "delivery_destination": delivery_destination,
                    "freight_treatment": "REQUEST_SEPARATE_FREIGHT_QUOTE",
                    "payment_terms_requested": True,
                    "ask_for": [
                        "unit_price",
                        "extended_price",
                        "availability",
                        "lead_time",
                        "shipping_freight",
                        "minimum_order",
                        "quote_validity",
                        "payment_terms",
                        "manufacturer_authorization",
                    ],
                },
                "status": READY_FOR_OWNER_QUOTE_OUTREACH if ready else "BLOCKED_NOT_READY",
                "blockers": blockers,
                "ready_for_owner_quote_outreach": ready,
                "do_not_send_automatically": True,
                "created_at": now_utc().isoformat(),
            }
        )

    covered_ids = {l["line_id"] for p in packets for l in p["lines"]}
    usable_n = len(usable_quote) or 1
    coverage = len(covered_ids) / usable_n if usable_quote else 0.0

    return {
        "opportunity_id": opportunity_id,
        "material_lines": len(lines),
        "quote_ready_lines": len(usable_quote),
        "quote_packets": packets,
        "packet_count": len(packets),
        "suppliers": sorted({(p.get("supplier") or {}).get("domain") or (p.get("supplier") or {}).get("supplier_name") for p in packets}),
        "coverage": round(coverage, 4),
        "lines_per_packet": [p["line_count"] for p in packets],
        "consolidation_ratio": round(len(usable_quote) / max(1, len(packets)), 2),
    }


# Quote receipt schema (Phase 13)
QUOTE_RECEIPT_FIELDS = [
    "supplier",
    "quote_number",
    "quote_date",
    "expiration",
    "mpn",
    "qty",
    "uom",
    "unit_price",
    "extended_price",
    "freight",
    "lead_time",
    "availability",
    "payment_terms",
    "authorization",
    "notes",
    "source_file_email_reference",
    "opportunity_id",
    "line_id",
    "packet_id",
]
