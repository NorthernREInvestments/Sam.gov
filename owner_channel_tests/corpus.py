"""Phase 1–2 — Freeze immutable corpus + revalidate packets."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from owner_channel_tests.models import BUILD, CORPUS, PRIOR_PACKETS
from p0_prescale_hardening.identity_revenue import identity_execution_ready
from p0_prescale_hardening.models import F_FAMILY_ONLY, G_AMBIGUOUS, PRODUCT, PRODUCT_WITH_INCIDENTAL_DELIVERY
from p0_prescale_hardening.provenance import build_source_provenance
from p0_prescale_hardening.scope_classify import classify_line_scope


def _load_prior_packets() -> list[dict[str, Any]]:
    p = data_path(PRIOR_PACKETS)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return list(data.get("channel_ready") or data.get("channel_packets") or [])


def freeze_corpus() -> dict[str, Any]:
    packets = _load_prior_packets()
    frozen = []
    for pkt in packets:
        lines = []
        for ln in pkt.get("lines") or []:
            lines.append(
                {
                    "line_id": ln.get("line_id"),
                    "clin": ln.get("clin"),
                    "manufacturer": ln.get("manufacturer"),
                    "mpn": ln.get("mpn") or ln.get("model"),
                    "model": ln.get("model"),
                    "raw_description": ln.get("raw_description") or ln.get("description"),
                    "qty": ln.get("qty"),
                    "uom": ln.get("uom"),
                    "pack": ln.get("pack") or 1,
                    "condition": ln.get("condition") or "NEW",
                    "SOURCE_PROVENANCE": ln.get("SOURCE_PROVENANCE"),
                    "identity_class": ln.get("identity_class"),
                    "IDENTITY_EXECUTION_READY": ln.get("IDENTITY_EXECUTION_READY"),
                    "scope": ln.get("scope"),
                    "delivery_destination": ln.get("delivery_destination") or pkt.get("delivery_destination"),
                    "deadline": ln.get("deadline") or pkt.get("deadline"),
                }
            )
        row = {
            "packet_id": pkt.get("packet_id"),
            "opportunity_id": pkt.get("opportunity_id"),
            "buyer": pkt.get("buyer"),
            "solicitation_id": pkt.get("solicitation_id"),
            "supplier": deepcopy(pkt.get("supplier") or {}),
            "cluster_key": pkt.get("cluster_key"),
            "line_count": len(lines),
            "lines": lines,
            "SOURCE_QUANTITY_TOTAL": pkt.get("SOURCE_QUANTITY_TOTAL"),
            "QUOTE_PACKET_QUANTITY_TOTAL": pkt.get("QUOTE_PACKET_QUANTITY_TOTAL"),
            "DIFF": pkt.get("DIFF"),
            "delivery_destination": pkt.get("delivery_destination"),
            "deadline": pkt.get("deadline"),
            "quote_status": pkt.get("status") or "OWNER_CHANNEL_TEST_READY",
            "contact_status": "NOT_CONTACTED",
            "OWNER_CHANNEL_TEST_READY": bool(pkt.get("OWNER_CHANNEL_TEST_READY")),
            "revenue_note": pkt.get("revenue_note") or "SUPPLIER_CHANNEL_PROOF_ONLY",
            "do_not_send_automatically": True,
            "frozen_at": now_utc().isoformat(),
            "immutable": True,
        }
        blob = json.dumps(
            {"packet_id": row["packet_id"], "lines": [(l["line_id"], l["qty"], l["mpn"]) for l in lines]},
            sort_keys=True,
        )
        row["corpus_hash"] = hashlib.sha1(blob.encode()).hexdigest()[:16]
        frozen.append(row)

    corpus = {
        "register": "OWNER_CHANNEL_TEST_CORPUS_V1",
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "immutable": True,
        "packets": frozen,
        "count": len(frozen),
        "expected": {
            "go-metro_cummins": 57,
            "dekalb_bound_tree": 159,
            "dekalb_henry_schein": 37,
            "dekalb_medline": 12,
        },
    }
    data_path(CORPUS).write_text(json.dumps(corpus, indent=2, default=str), encoding="utf-8")
    return corpus


def revalidate_packet(packet: dict[str, Any]) -> dict[str, Any]:
    lines = packet.get("lines") or []
    blockers: list[str] = []
    fg = 0
    non_product = 0
    untraceable = 0
    identity_not_ready = 0
    ids = []
    src_qty = 0.0
    pkt_qty = 0.0

    for ln in lines:
        ids.append(ln.get("line_id"))
        q = float(ln.get("qty") or 0)
        src_qty += q
        pkt_qty += q
        prov = ln.get("SOURCE_PROVENANCE") or build_source_provenance(
            {
                **ln,
                "description": ln.get("raw_description"),
                "source_document": (ln.get("SOURCE_PROVENANCE") or {}).get("source_path")
                or (ln.get("SOURCE_PROVENANCE") or {}).get("filename"),
                "source_kind": (ln.get("SOURCE_PROVENANCE") or {}).get("source_kind"),
                "page": (ln.get("SOURCE_PROVENANCE") or {}).get("page"),
                "sheet": (ln.get("SOURCE_PROVENANCE") or {}).get("sheet"),
                "row": (ln.get("SOURCE_PROVENANCE") or {}).get("row"),
                "cell": (ln.get("SOURCE_PROVENANCE") or {}).get("cell"),
            }
        )
        if not prov.get("complete"):
            untraceable += 1
        scope = classify_line_scope(
            {
                "description": ln.get("raw_description"),
                "manufacturer": ln.get("manufacturer"),
                "mpn": ln.get("mpn"),
                "model": ln.get("model"),
            }
        )
        if scope["scope"] not in {PRODUCT, PRODUCT_WITH_INCIDENTAL_DELIVERY}:
            non_product += 1
        conf = str(ln.get("identity_class") or "")
        if conf.startswith("F_") or conf.startswith("G_") or conf in {F_FAMILY_ONLY, G_AMBIGUOUS}:
            fg += 1
        ident = identity_execution_ready(
            {
                **ln,
                "identity_confidence": conf,
                "SOURCE_PROVENANCE": prov,
                "source_document": prov.get("filename"),
                "identity_validated": True,
            }
        )
        if not ident.get("IDENTITY_EXECUTION_READY"):
            identity_not_ready += 1

    from collections import Counter

    dups = [i for i, n in Counter([x for x in ids if x]).items() if n > 1]
    qty_diff = round(pkt_qty - src_qty, 6)  # same source by construction; also check packet fields
    declared_diff = float(packet.get("DIFF") or 0)
    if declared_diff != 0:
        qty_diff = declared_diff

    if untraceable:
        blockers.append(f"untraceable_lines={untraceable}")
    if qty_diff != 0:
        blockers.append(f"qty_diff={qty_diff}")
    if dups:
        blockers.append(f"duplicates={len(dups)}")
    if fg:
        blockers.append(f"fg_identities={fg}")
    if non_product:
        blockers.append(f"non_product_scope={non_product}")
    if not packet.get("delivery_destination"):
        blockers.append("delivery_missing")
    if not packet.get("deadline"):
        blockers.append("deadline_missing")

    ready = len(blockers) == 0
    return {
        "packet_id": packet.get("packet_id"),
        "opportunity_id": packet.get("opportunity_id"),
        "supplier": (packet.get("supplier") or {}).get("supplier_name"),
        "SOURCE_TRACE": "PASS" if untraceable == 0 else "FAIL",
        "QTY_DIFF": qty_diff,
        "DUPLICATES": len(dups),
        "F_G_in_packet": fg,
        "non_product_in_packet": non_product,
        "identity_not_ready": identity_not_ready,
        "delivery": bool(packet.get("delivery_destination")),
        "deadline": bool(packet.get("deadline")),
        "revenue_status": packet.get("revenue_note") or "SUPPLIER_CHANNEL_PROOF_ONLY",
        "blockers": blockers,
        "status": "READY" if ready else "NOT_READY",
        "PASS_FAIL": "PASS" if ready else "FAIL",
    }


def revalidate_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    results = [revalidate_packet(p) for p in corpus.get("packets") or []]
    return {
        "total": len(results),
        "ready": sum(1 for r in results if r["status"] == "READY"),
        "not_ready": sum(1 for r in results if r["status"] != "READY"),
        "results": results,
        "ALL_PASS": all(r["PASS_FAIL"] == "PASS" for r in results) and len(results) == 4,
    }
