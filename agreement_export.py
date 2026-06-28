"""Export subcontract agreements to PDF."""

from __future__ import annotations

import io
import re
from datetime import date
from typing import Any

from proposal_export import (
    COMPANY_SHORT,
    _css_escape,
    _fpdf_bytes,
    _html_to_plain,
    _sanitize_filename,
    _weasyprint_available,
    build_proposal_pdf_weasyprint,
)


def _today_str() -> str:
    return date.today().strftime("%B %d, %Y")


def _date_file() -> str:
    return date.today().strftime("%Y%m%d")


def agreement_filenames(solicitation_number: str, sub_name: str) -> dict[str, str]:
    sol = _sanitize_filename(solicitation_number)
    sub = _sanitize_filename(sub_name)[:40]
    d = _date_file()
    return {
        "pdf": f"{sol}_{sub}_SubcontractAgreement_{d}.pdf",
    }


def agreement_meta(agreement: Any) -> dict[str, Any]:
    config = agreement.config_json if isinstance(agreement.config_json, dict) else {}
    contract = config.get("contract") or {}
    sub = config.get("subcontractor") or {}
    sol = str(contract.get("solicitation_number") or "Agreement")
    sub_name = str(sub.get("legal_business_name") or sub.get("business_name") or "Sub")
    return {
        "solicitation_number": sol,
        "sub_name": sub_name,
        "filenames": agreement_filenames(sol, sub_name),
        "date_display": _today_str(),
    }


def _strip_outer_html(html: str) -> str:
    text = (html or "").strip()
    text = re.sub(r"^<!DOCTYPE[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</?(?:html|head|body)[^>]*>", "", text, flags=re.I)
    return text.strip()


def _agreement_print_html(html: str, meta: dict[str, Any]) -> str:
    sol = _css_escape(meta["solicitation_number"])
    date_display = _css_escape(meta["date_display"])
    sub_name = _css_escape(meta.get("sub_name") or "")
    body = _strip_outer_html(html)
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>
@page {{ size: letter; margin: 1in 1in 1.1in 1in;
  @top-left {{ content: '{COMPANY_SHORT}'; font-family: 'Times New Roman', Times, serif; font-size: 10pt; }}
  @top-right {{ content: '{sol}'; font-family: 'Times New Roman', Times, serif; font-size: 10pt; }}
  @bottom-center {{ content: counter(page); font-family: 'Times New Roman', Times, serif; font-size: 9pt; }}
  @bottom-right {{ content: '{date_display}'; font-family: 'Times New Roman', Times, serif; font-size: 9pt; }}
}}
body {{ font-family: 'Times New Roman', Times, serif; font-size: 12pt; line-height: 1.35; color: #111; }}
h1 {{ font-size: 16pt; text-align: center; margin: 0 0 16pt 0; }}
h2 {{ font-size: 13pt; font-weight: bold; margin: 18pt 0 8pt 0; text-transform: uppercase; }}
p {{ margin: 0 0 10pt 0; }}
.signature-block {{ margin-top: 24pt; }}
</style></head><body>
{body}
</body></html>"""


def build_agreement_pdf_fpdf(html: str, meta: dict[str, Any]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=72)
    pdf.set_margins(72, 72, 72)
    pdf.add_page()
    pdf.set_font("Times", "B", 10)
    pdf.cell(0, 8, COMPANY_SHORT, align="L")
    pdf.cell(0, 8, meta["solicitation_number"], align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Times", "", 12)
    body = _html_to_plain(_strip_outer_html(html))
    pdf.multi_cell(0, 6, body)
    pdf.set_y(-36)
    pdf.set_font("Times", "", 9)
    pdf.cell(0, 8, meta["date_display"], align="R")
    return _fpdf_bytes(pdf)


def build_agreement_pdf(agreement: Any) -> tuple[bytes, str]:
    """Returns (pdf_bytes, engine_used)."""
    html = agreement.agreement_html or ""
    if not html.strip():
        raise ValueError("Agreement has no content")
    meta = agreement_meta(agreement)
    print_html = _agreement_print_html(html, meta)
    if _weasyprint_available():
        try:
            return build_proposal_pdf_weasyprint(print_html), "weasyprint"
        except Exception:
            pass
    return build_agreement_pdf_fpdf(html, meta), "fpdf2"


def pdf_bytes_to_buffer(data: bytes) -> io.BytesIO:
    buf = io.BytesIO(data)
    buf.seek(0)
    return buf
