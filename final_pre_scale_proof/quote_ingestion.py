"""Phases 11–13 — Quote ingestion fixtures, production gate, economics trigger."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from final_pre_scale_proof.models import (
    PRICE_ORIGIN_SUPPLIER_QUOTE,
    REAL_SUPPLIER_QUOTE,
    TEST_FIXTURE_ONLY,
)
from m3_data_root import data_path

REQUIRED_REAL_FIELDS = [
    "supplier_identity",
    "quote_date",
    "quote_number_or_reference",
    "exact_mpn_model",
    "qty",
    "uom",
    "unit_price",
    "extended_price",
    "freight_treatment",
    "validity",
    "source_artifact",
]


def build_fixtures() -> dict[str, Any]:
    """Synthetic LOCAL fixtures ONLY — clearly labeled TEST_FIXTURE_ONLY."""
    root = data_path("quote_ingestion_fixtures")
    root.mkdir(parents=True, exist_ok=True)

    # Email-text fixture
    email_path = root / "TEST_FIXTURE_ONLY_email_quote.txt"
    email_body = (
        "TEST_FIXTURE_ONLY — NOT A REAL SUPPLIER QUOTE\n"
        "From: quotes-test@example-supplier.test\n"
        "Quote #: TF-EMAIL-001\n"
        "Date: 2026-10-05\n"
        "Validity: 30 days\n\n"
        "Line 1: Cummins 2871452 | Qty 1 EA | Unit $42.00 | Ext $42.00\n"
        "Freight: $15.00 prepaid and add\n"
        "Payment: Net 30\n"
    )
    email_path.write_text(email_body, encoding="utf-8")

    # CSV fixture
    csv_path = root / "TEST_FIXTURE_ONLY_quote.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "origin",
                "supplier",
                "quote_number",
                "quote_date",
                "mpn",
                "qty",
                "uom",
                "unit_price",
                "extended_price",
                "freight",
                "validity",
            ]
        )
        w.writerow(
            [
                TEST_FIXTURE_ONLY,
                "Example Distributor LLC",
                "TF-CSV-001",
                "2026-10-05",
                "LK4430BF1U",
                "1",
                "EA",
                "1899.00",
                "1899.00",
                "PREPAID_AND_ADD",
                "30 days",
            ]
        )

    # XLSX-like TSV (avoid openpyxl dependency)
    xlsx_path = root / "TEST_FIXTURE_ONLY_quote.tsv"
    xlsx_path.write_text(
        "origin\tsupplier\tquote_number\tmpn\tqty\tuom\tunit_price\textended_price\n"
        f"{TEST_FIXTURE_ONLY}\tFixture Supply Co\tTF-XLS-001\t502-32PL-BT\t1\tEA\t1200.00\t1200.00\n",
        encoding="utf-8",
    )

    # Manual entry fixture (JSON form)
    manual_path = root / "TEST_FIXTURE_ONLY_manual_entry.json"
    manual_path.write_text(
        json.dumps(
            {
                "origin": TEST_FIXTURE_ONLY,
                "supplier_identity": "Manual Entry Fixture Supplier",
                "quote_date": "2026-10-05",
                "quote_number": "TF-MANUAL-001",
                "lines": [
                    {
                        "exact_mpn_model": "293",
                        "qty": 1,
                        "uom": "EA",
                        "unit_price": 850.0,
                        "extended_price": 850.0,
                    }
                ],
                "freight_treatment": "FOB_DESTINATION",
                "validity": "15 days",
                "source_artifact": str(manual_path.name),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Pseudo-PDF text stand-in (parser accepts .txt labeled pdf fixture)
    pdf_path = root / "TEST_FIXTURE_ONLY_quote_pdf.txt"
    pdf_path.write_text(
        "TEST_FIXTURE_ONLY PDF QUOTE STAND-IN\n"
        "Supplier: Fixture OEM Sales\n"
        "Quote Number: TF-PDF-001\n"
        "Date: 2026-10-05\n"
        "MPN: 204163 Qty: 1 EA Unit: $55.00 Extended: $55.00\n"
        "Freight: TBD\n"
        "Valid through: 2026-11-05\n",
        encoding="utf-8",
    )

    return {
        "label": TEST_FIXTURE_ONLY,
        "fixtures": {
            "email_text": str(email_path),
            "csv": str(csv_path),
            "spreadsheet_tsv": str(xlsx_path),
            "manual_entry": str(manual_path),
            "pdf_text_standin": str(pdf_path),
        },
    }


def parse_email_fixture(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert TEST_FIXTURE_ONLY in text
    return {
        "parser": "email_text",
        "origin": TEST_FIXTURE_ONLY,
        "accepted_as_production": False,
        "raw_preview": text[:200],
        "parsed_ok": "Quote #" in text and "Unit $" in text,
    }


def parse_csv_fixture(path: Path) -> dict[str, Any]:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert rows and rows[0].get("origin") == TEST_FIXTURE_ONLY
    return {
        "parser": "csv",
        "origin": TEST_FIXTURE_ONLY,
        "accepted_as_production": False,
        "rows": len(rows),
        "parsed_ok": bool(rows[0].get("unit_price")),
    }


def parse_spreadsheet_fixture(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert TEST_FIXTURE_ONLY in lines[1]
    return {
        "parser": "spreadsheet_tsv",
        "origin": TEST_FIXTURE_ONLY,
        "accepted_as_production": False,
        "rows": max(0, len(lines) - 1),
        "parsed_ok": len(lines) >= 2,
    }


def parse_manual_fixture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("origin") == TEST_FIXTURE_ONLY
    return {
        "parser": "manual_entry",
        "origin": TEST_FIXTURE_ONLY,
        "accepted_as_production": False,
        "parsed_ok": bool(data.get("lines")),
    }


def parse_pdf_standin(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert TEST_FIXTURE_ONLY in text
    return {
        "parser": "pdf_text_standin",
        "origin": TEST_FIXTURE_ONLY,
        "accepted_as_production": False,
        "parsed_ok": "Quote Number" in text,
    }


def production_gate(quote: dict[str, Any]) -> dict[str, Any]:
    """Real quote acceptance — fail closed unless REAL_SUPPLIER_QUOTE origin."""
    origin = quote.get("origin") or quote.get("price_origin_candidate")
    missing = []
    for f in REQUIRED_REAL_FIELDS:
        if quote.get(f) in (None, "", []):
            # allow quote_number optional if quote_number_or_reference present alternate
            if f == "quote_number_or_reference" and quote.get("quote_number"):
                continue
            missing.append(f)

    is_real = origin == REAL_SUPPLIER_QUOTE
    is_fixture = origin == TEST_FIXTURE_ONLY or str(quote.get("label") or "").startswith("TEST_FIXTURE")
    accept = is_real and not missing and not is_fixture
    return {
        "accepted": accept,
        "PRICE_ORIGIN": PRICE_ORIGIN_SUPPLIER_QUOTE if accept else None,
        "origin": origin,
        "missing_fields": missing,
        "blocked_reason": None
        if accept
        else (
            "TEST_FIXTURE_ONLY_CANNOT_ENTER_PRODUCTION_ECONOMICS"
            if is_fixture or origin == TEST_FIXTURE_ONLY
            else ("NOT_REAL_SUPPLIER_QUOTE" if not is_real else f"missing:{missing}")
        ),
    }


def run_ingestion_tests() -> dict[str, Any]:
    fixtures = build_fixtures()
    paths = {k: Path(v) for k, v in fixtures["fixtures"].items()}
    results = {
        "PDF parser": parse_pdf_standin(paths["pdf_text_standin"]),
        "Email parser": parse_email_fixture(paths["email_text"]),
        "Spreadsheet parser": parse_spreadsheet_fixture(paths["spreadsheet_tsv"]),
        "CSV parser": parse_csv_fixture(paths["csv"]),
        "Manual entry": parse_manual_fixture(paths["manual_entry"]),
    }

    # Prove fixtures cannot enter production economics
    fixture_quote = {
        "origin": TEST_FIXTURE_ONLY,
        "supplier_identity": "Fixture",
        "quote_date": "2026-10-05",
        "quote_number_or_reference": "TF-001",
        "exact_mpn_model": "X",
        "qty": 1,
        "uom": "EA",
        "unit_price": 1.0,
        "extended_price": 1.0,
        "freight_treatment": "TBD",
        "validity": "30d",
        "source_artifact": "fixture",
    }
    gate_fixture = production_gate(fixture_quote)

    real_ok = {
        "origin": REAL_SUPPLIER_QUOTE,
        "supplier_identity": "Acme Distributor",
        "quote_date": "2026-10-05",
        "quote_number_or_reference": "Q-999",
        "exact_mpn_model": "2871452",
        "qty": 1,
        "uom": "EA",
        "unit_price": 42.0,
        "extended_price": 42.0,
        "freight_treatment": "PREPAID_AND_ADD",
        "validity": "30 days",
        "source_artifact": "supplier_email.eml",
    }
    gate_real = production_gate(real_ok)

    parsers_ok = all(r.get("parsed_ok") for r in results.values())
    fixture_blocked = gate_fixture["accepted"] is False
    real_accepted = gate_real["accepted"] is True

    return {
        "fixtures": fixtures,
        "parser_results": results,
        "Production_gate": {
            "fixture_blocked": fixture_blocked,
            "fixture_gate": gate_fixture,
            "real_accepted": real_accepted,
            "real_gate": gate_real,
            "required_fields": REQUIRED_REAL_FIELDS,
            "PRICE_ORIGIN_on_accept": PRICE_ORIGIN_SUPPLIER_QUOTE,
        },
        "PASS_FAIL": "PASS" if parsers_ok and fixture_blocked and real_accepted else "FAIL",
        "economics_recompute_sequence": [
            "quote_received",
            "line_matched",
            "basket_cost_updated",
            "freight_updated",
            "financing_updated",
            "profit_recomputed",
            "execution_checked",
            "owner_notified_of_state_change",
        ],
        "automatic_recompute_wired": False,  # trigger defined; production wiring still PARTIAL
        "note": "Sequence specified and gate enforced; full auto-pipeline wiring marked in gap register",
    }
