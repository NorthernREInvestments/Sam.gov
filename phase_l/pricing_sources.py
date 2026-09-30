"""Phase L.2.5 — PricingSourceAdapter + PDF/XLSX/HTML bid-tab parsers.

Strict identity match required before accepting any dollar amount.
Does not weaken L.2.2 product-page verification for retailer HTML.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from application_clock import now_utc
from phase_l.convergence import (
    AUTHORIZED_DISTRIBUTOR,
    COOPERATIVE_CONTRACT,
    DEALER_ADVERTISED,
    OEM_MSRP,
    PRICE_ACCESS_CONDITIONAL,
    PRICE_ACCESS_NO,
    PRICE_ACCESS_UNKNOWN,
    PRICE_ACCESS_YES,
    PUBLIC_CATALOG,
    PUBLIC_GOV_CHANNEL,
    PUBLIC_RETAIL,
    STATE_TERM_CONTRACT,
    classify_price_access,
)

# Roles
HISTORICAL_GOV_PRICE = "HISTORICAL_GOV_PRICE"
CURRENT_MARKET_PRICE = "CURRENT_MARKET_PRICE"
CURRENT_ACQUISITION_PRICE = "CURRENT_ACQUISITION_PRICE"
GOVERNMENT_CHANNEL_PRICE = "GOVERNMENT_CHANNEL_PRICE"
COMPARABLE_ONLY = "COMPARABLE_ONLY"

# Document types
DOC_HTML = "HTML"
DOC_PDF = "PDF"
DOC_XLSX = "XLSX"
DOC_CSV = "CSV"
DOC_JSON = "JSON"
DOC_BID_TAB = "BID_TAB"
DOC_CONTRACT_CATALOG = "CONTRACT_CATALOG"
DOC_BOARD_PACKET = "BOARD_PACKET"

_DOLLAR = re.compile(
    r"\$\s*("
    r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"
    r"|\d{1,3}(?:,\d{3})*\.\d{2}"
    r"|\d{4,8}(?:\.\d{1,2})?"
    r")"
)
_USED = re.compile(r"\b(used|refurbished|salvage|auction|rental|lease\s+only)\b", re.I)
_MONTHLY = re.compile(r"\b(per\s+mo|/mo|monthly|down\s+payment)\b", re.I)
_AWARD_HINT = re.compile(r"\b(award(ed)?|bid\s+tab|tabulation|purchase\s+order|resolution|approved)\b", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def _norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(s or "").upper())


def _num(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


@dataclass
class PriceEvidenceRecord:
    source_type: str
    source_url: str | None = None
    document_type: str = DOC_HTML
    seller_or_buyer: str | None = None
    product_identity: str | None = None
    observed_mpn: str | None = None
    observed_model: str | None = None
    price: float | None = None
    currency: str = "USD"
    quantity: float | None = None
    uom: str | None = "EA"
    effective_date: str | None = None
    expiration_date: str | None = None
    price_access: str = PRICE_ACCESS_UNKNOWN
    acquisition_price_type: str | None = None
    roles: list[str] = field(default_factory=list)
    confidence: str = "MEDIUM"
    evidence_location: str | None = None
    evidence_text: str | None = None
    economics_eligible_as_acquisition: bool = False
    economics_eligible_as_history: bool = False
    rejection_reason: str | None = None
    fetched_at: str = field(default_factory=_utc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def identity_keys(search_id: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for k in (
        search_id.get("primary_mpn"),
        search_id.get("model"),
        search_id.get("sku"),
        search_id.get("manufacturer"),
        *(search_id.get("mpn_variants") or [])[:4],
    ):
        n = _norm(k)
        if n and len(n) >= 3 and n not in keys:
            keys.append(n)
    # Also keep display forms for nearby text
    for k in (search_id.get("model"), search_id.get("primary_mpn")):
        if k and str(k) not in keys:
            keys.append(str(k))
    return keys


def text_has_identity(blob: str, search_id: dict[str, Any]) -> tuple[bool, str | None]:
    """Require exact model/MPN token (normalized) in nearby text."""
    if not blob:
        return False, None
    blob_n = _norm(blob)
    mpn = search_id.get("primary_mpn")
    model = search_id.get("model")
    if mpn and _norm(mpn) and _norm(mpn) in blob_n:
        return True, str(mpn)
    if model:
        mn = _norm(model)
        if len(mn) >= 4 and mn in blob_n:
            return True, str(model)
        # multi-token model: require all significant tokens
        toks = [t for t in re.findall(r"[A-Za-z0-9]{3,}", str(model)) if t.upper() not in {"THE", "AND"}]
        if toks and all(_norm(t) in blob_n for t in toks):
            return True, str(model)
    return False, None


def parse_html_bid_tab(
    html: str,
    *,
    search_id: dict[str, Any],
    source_url: str,
    role_hint: str = HISTORICAL_GOV_PRICE,
) -> list[PriceEvidenceRecord]:
    """Extract award/bid-tab rows from HTML tables when identity matches."""
    if not html:
        return []
    out: list[PriceEvidenceRecord] = []
    # Simple table rows
    for tm in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.I | re.S):
        row_html = tm.group(1)
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.I | re.S)
        if len(cells) < 2:
            continue
        plain_cells = [re.sub(r"<[^>]+>", " ", c) for c in cells]
        row_text = " | ".join(plain_cells)
        ok, matched = text_has_identity(row_text, search_id)
        if not ok:
            continue
        if _USED.search(row_text) or _MONTHLY.search(row_text):
            continue
        # Prefer award column / last money column
        prices = []
        for c in plain_cells:
            for m in _DOLLAR.finditer(c):
                p = _num(m.group(1))
                if p and 10 <= p <= 5_000_000:
                    prices.append(p)
        if not prices:
            continue
        # Heuristic: if "award" present use first after award token else last price
        price = prices[-1]
        awarded = bool(re.search(r"\bawarded?\b|\bselected\b|\bwinner\b", row_text, re.I))
        vendor = None
        for c in plain_cells:
            if re.search(r"[A-Za-z]{3,}", c) and not _DOLLAR.search(c):
                vendor = re.sub(r"\s+", " ", c).strip()[:80]
                break
        access = classify_price_access(
            price_type=PUBLIC_GOV_CHANNEL if role_hint == HISTORICAL_GOV_PRICE else PUBLIC_RETAIL,
            url=source_url,
            evidence_text=row_text,
        )
        rec = PriceEvidenceRecord(
            source_type="BID_TAB_HTML",
            source_url=source_url,
            document_type=DOC_BID_TAB,
            seller_or_buyer=vendor,
            product_identity=matched,
            observed_model=matched if search_id.get("model") else None,
            observed_mpn=matched if search_id.get("primary_mpn") else None,
            price=price,
            roles=[role_hint],
            confidence="HIGH" if awarded else "MEDIUM",
            evidence_location="html_table_row",
            evidence_text=row_text[:240],
            price_access=PRICE_ACCESS_NO if role_hint == HISTORICAL_GOV_PRICE else access["price_access"],
            acquisition_price_type=PUBLIC_GOV_CHANNEL if role_hint == HISTORICAL_GOV_PRICE else PUBLIC_RETAIL,
            economics_eligible_as_history=role_hint == HISTORICAL_GOV_PRICE,
            economics_eligible_as_acquisition=False if role_hint == HISTORICAL_GOV_PRICE else access["economics_eligible"],
        )
        out.append(rec)
        if len(out) >= 8:
            break
    return out


def parse_council_award_text(
    text: str,
    *,
    search_id: dict[str, Any],
    source_url: str,
) -> list[PriceEvidenceRecord]:
    """Board/council packet snippets: vendor + model + amount."""
    if not text:
        return []
    out: list[PriceEvidenceRecord] = []
    # Window around identity match
    keys = [search_id.get("model"), search_id.get("primary_mpn"), search_id.get("manufacturer")]
    keys = [k for k in keys if k]
    for key in keys[:3]:
        for m in re.finditer(re.escape(str(key)), text, re.I):
            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 220)
            window = text[start:end]
            if not _AWARD_HINT.search(window) and "$" not in window:
                continue
            if _USED.search(window):
                continue
            ok, matched = text_has_identity(window, search_id)
            if not ok:
                continue
            prices = [_num(x.group(1)) for x in _DOLLAR.finditer(window)]
            prices = [p for p in prices if p and 50 <= p <= 5_000_000]
            if not prices:
                continue
            # Prefer larger amount as award total; derive unit if qty nearby
            price = max(prices)
            qty = None
            qm = re.search(r"\b(\d{1,4})\s*(?:EA|each|units?|vehicles?|machines?)\b", window, re.I)
            if qm:
                try:
                    qty = float(qm.group(1))
                    if qty > 1 and price / qty >= 50:
                        price = round(price / qty, 2)
                except ValueError:
                    pass
            out.append(
                PriceEvidenceRecord(
                    source_type="BOARD_COUNCIL",
                    source_url=source_url,
                    document_type=DOC_BOARD_PACKET,
                    product_identity=matched,
                    price=price,
                    quantity=qty,
                    roles=[HISTORICAL_GOV_PRICE],
                    confidence="HIGH" if _AWARD_HINT.search(window) else "MEDIUM",
                    evidence_location="council_window",
                    evidence_text=re.sub(r"\s+", " ", window)[:240],
                    price_access=PRICE_ACCESS_NO,
                    acquisition_price_type=PUBLIC_GOV_CHANNEL,
                    economics_eligible_as_history=True,
                    economics_eligible_as_acquisition=False,
                )
            )
            if len(out) >= 5:
                return out
    return out


def extract_pdf_text(data: bytes) -> str:
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            parts = []
            # Cap pages hard — board packets can be huge
            for page in pdf.pages[:12]:
                parts.append(page.extract_text() or "")
            return "\n".join(parts)[:200000]
    except Exception:
        return ""


def parse_pdf_price_rows(
    text: str,
    *,
    search_id: dict[str, Any],
    source_url: str,
    role_hint: str = CURRENT_ACQUISITION_PRICE,
) -> list[PriceEvidenceRecord]:
    """Exact-row PDF pricing: identity must appear on same/neighboring line as price."""
    if not text:
        return []
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]
    out: list[PriceEvidenceRecord] = []
    for i, ln in enumerate(lines):
        window = " ".join(lines[max(0, i - 1) : i + 2])
        ok, matched = text_has_identity(window, search_id)
        if not ok:
            continue
        if _USED.search(window) or _MONTHLY.search(window):
            continue
        prices = [_num(m.group(1)) for m in _DOLLAR.finditer(window)]
        prices = [p for p in prices if p and 5 <= p <= 5_000_000]
        if not prices:
            continue
        # Prefer price on the same line as identity
        same = [_num(m.group(1)) for m in _DOLLAR.finditer(ln)]
        same = [p for p in same if p and 5 <= p <= 5_000_000]
        price = same[-1] if same else prices[-1]
        is_hist = role_hint == HISTORICAL_GOV_PRICE or bool(_AWARD_HINT.search(window))
        is_coop = bool(re.search(r"sourcewell|omnia|naspo|contract\s+price", window, re.I))
        if is_hist:
            ptype = PUBLIC_GOV_CHANNEL
            roles = [HISTORICAL_GOV_PRICE]
            acq_ok = False
            hist_ok = True
            access = PRICE_ACCESS_NO
        elif is_coop:
            ptype = COOPERATIVE_CONTRACT
            roles = [GOVERNMENT_CHANNEL_PRICE, CURRENT_MARKET_PRICE]
            gate = classify_price_access(price_type=ptype, url=source_url, evidence_text=window)
            acq_ok = gate["economics_eligible"]
            hist_ok = False
            access = gate["price_access"]
        else:
            ptype = PUBLIC_CATALOG
            roles = [role_hint]
            gate = classify_price_access(price_type=PUBLIC_RETAIL, url=source_url, evidence_text=window)
            acq_ok = gate["economics_eligible"]
            hist_ok = False
            access = gate["price_access"]
        out.append(
            PriceEvidenceRecord(
                source_type="PDF_PRICE",
                source_url=source_url,
                document_type=DOC_PDF,
                product_identity=matched,
                price=price,
                roles=roles,
                confidence="HIGH",
                evidence_location=f"pdf_line_{i}",
                evidence_text=window[:240],
                price_access=access,
                acquisition_price_type=ptype,
                economics_eligible_as_acquisition=acq_ok,
                economics_eligible_as_history=hist_ok,
            )
        )
        if len(out) >= 8:
            break
    return out


def parse_spreadsheet_prices(
    data: bytes,
    *,
    search_id: dict[str, Any],
    source_url: str,
    filename_hint: str = "",
) -> list[PriceEvidenceRecord]:
    """XLSX/CSV exact-row match on manufacturer/model/SKU columns."""
    name = (filename_hint or source_url or "").lower()
    rows: list[list[Any]] = []
    doc = DOC_CSV
    try:
        if name.endswith(".xlsx") or name.endswith(".xlsm") or data[:2] == b"PK":
            doc = DOC_XLSX
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                rows.append(list(row))
                if i > 5000:
                    break
            wb.close()
        else:
            text = data.decode("utf-8", errors="ignore")
            reader = csv.reader(io.StringIO(text))
            rows = [list(r) for i, r in enumerate(reader) if i < 5000]
    except Exception:
        return []

    if not rows:
        return []
    header = [str(c or "").strip().lower() for c in rows[0]]
    col = {h: i for i, h in enumerate(header)}

    def find_col(*names: str) -> int | None:
        for n in names:
            for h, i in col.items():
                if n in h:
                    return i
        return None

    i_model = find_col("model", "item", "description", "product")
    i_mfr = find_col("manufacturer", "mfr", "brand", "vendor")
    i_sku = find_col("sku", "mpn", "part", "item #", "item_number")
    i_price = find_col("contract price", "unit price", "price", "msrp", "net")
    i_msrp = find_col("msrp", "list price")
    if i_price is None and i_msrp is not None:
        i_price = i_msrp

    out: list[PriceEvidenceRecord] = []
    for ridx, row in enumerate(rows[1:], start=2):
        cells = [str(c or "") for c in row]
        blob = " ".join(cells)
        ok, matched = text_has_identity(blob, search_id)
        if not ok:
            continue
        price = None
        if i_price is not None and i_price < len(row):
            price = _num(row[i_price])
        if price is None:
            for c in cells:
                for m in _DOLLAR.finditer(c):
                    price = _num(m.group(1))
            if price is None:
                continue
        if price < 5 or price > 5_000_000:
            continue
        is_msrp = i_msrp is not None and i_price == i_msrp
        is_contract = any("contract" in h for h in header)
        if is_contract:
            ptype = COOPERATIVE_CONTRACT if "coop" in name or "sourcewell" in name else STATE_TERM_CONTRACT
            gate = classify_price_access(price_type=ptype, url=source_url, evidence_text=blob)
            roles = [GOVERNMENT_CHANNEL_PRICE, CURRENT_MARKET_PRICE]
        elif is_msrp:
            ptype = OEM_MSRP
            gate = {"price_access": PRICE_ACCESS_YES, "economics_eligible": True}
            roles = [CURRENT_MARKET_PRICE]
        else:
            ptype = PUBLIC_CATALOG
            gate = {"price_access": PRICE_ACCESS_YES, "economics_eligible": True}
            roles = [CURRENT_ACQUISITION_PRICE]
        out.append(
            PriceEvidenceRecord(
                source_type="SPREADSHEET",
                source_url=source_url,
                document_type=doc,
                product_identity=matched,
                observed_model=str(row[i_model]) if i_model is not None and i_model < len(row) else matched,
                observed_mpn=str(row[i_sku]) if i_sku is not None and i_sku < len(row) else None,
                seller_or_buyer=str(row[i_mfr]) if i_mfr is not None and i_mfr < len(row) else None,
                price=float(price),
                roles=roles,
                confidence="HIGH",
                evidence_location=f"row_{ridx}",
                evidence_text=blob[:240],
                price_access=gate["price_access"],
                acquisition_price_type=ptype,
                economics_eligible_as_acquisition=bool(gate.get("economics_eligible")) and ptype != STATE_TERM_CONTRACT,
                economics_eligible_as_history=False,
            )
        )
        if len(out) >= 10:
            break
    return out


def classify_url_source(url: str) -> str:
    low = (url or "").lower()
    if low.endswith(".pdf"):
        return "PDF"
    if low.endswith(".xlsx") or low.endswith(".xls") or low.endswith(".csv"):
        return "SPREADSHEET"
    if any(x in low for x in ("sourcewell", "omnia", "naspo", "buyboard")):
        return "COOPERATIVE"
    if any(x in low for x in ("council", "board", "agenda", "minutes", "resolution")):
        return "BOARD"
    if any(x in low for x in ("bid", "tabulation", "award", "purchasing")):
        return "BID_TAB"
    if ".gov" in low:
        return "GOV_PORTAL"
    if any(x in low for x in ("dealer", "inventory", "ford.com", "bobcat.com")):
        return "DEALER_OEM"
    return "WEB"


class PricingSourceAdapter:
    """Thin adapter interface — subclasses/handlers produce PriceEvidenceRecord lists."""

    name: str = "base"

    def search_queries(self, search_id: dict[str, Any], row: dict[str, Any]) -> list[str]:
        return []

    def parse(
        self,
        *,
        url: str,
        content: bytes | str,
        content_type: str,
        search_id: dict[str, Any],
        row: dict[str, Any],
    ) -> list[PriceEvidenceRecord]:
        return []


def parse_content_auto(
    *,
    url: str,
    content: bytes | str,
    content_type: str = "",
    search_id: dict[str, Any],
    prefer_history: bool = False,
) -> list[PriceEvidenceRecord]:
    """Route content to the right parser."""
    ctype = (content_type or "").lower()
    low = (url or "").lower()
    role = HISTORICAL_GOV_PRICE if prefer_history else CURRENT_ACQUISITION_PRICE
    records: list[PriceEvidenceRecord] = []

    if isinstance(content, bytes):
        if "pdf" in ctype or low.endswith(".pdf") or content[:4] == b"%PDF":
            text = extract_pdf_text(content)
            records.extend(
                parse_pdf_price_rows(text, search_id=search_id, source_url=url, role_hint=role)
            )
            if prefer_history or _AWARD_HINT.search(text[:5000] if text else ""):
                records.extend(parse_council_award_text(text, search_id=search_id, source_url=url))
            return records
        if (
            "sheet" in ctype
            or "excel" in ctype
            or "csv" in ctype
            or low.endswith((".xlsx", ".xls", ".csv"))
            or content[:2] == b"PK"
        ):
            return parse_spreadsheet_prices(content, search_id=search_id, source_url=url, filename_hint=low)
        try:
            text = content.decode("utf-8", errors="ignore")
        except Exception:
            return []
    else:
        text = str(content)

    kind = classify_url_source(url)
    if kind in {"BID_TAB", "GOV_PORTAL"} or prefer_history:
        records.extend(parse_html_bid_tab(text, search_id=search_id, source_url=url, role_hint=HISTORICAL_GOV_PRICE))
        records.extend(parse_council_award_text(text, search_id=search_id, source_url=url))
    if kind == "BOARD":
        records.extend(parse_council_award_text(text, search_id=search_id, source_url=url))
    if kind in {"COOPERATIVE", "DEALER_OEM", "WEB"} and not prefer_history:
        # HTML dealer/OEM: extract only if identity + price on product-ish page
        ok, matched = text_has_identity(text[:20000], search_id)
        if ok and not _USED.search(text[:8000]):
            prices = [_num(m.group(1)) for m in _DOLLAR.finditer(text[:15000])]
            prices = [p for p in prices if p and 100 <= p <= 5_000_000]
            if prices:
                # median-ish to avoid footer noise
                prices.sort()
                price = prices[len(prices) // 2]
                ptype = DEALER_ADVERTISED if kind == "DEALER_OEM" else (
                    COOPERATIVE_CONTRACT if kind == "COOPERATIVE" else OEM_MSRP if "msrp" in text[:5000].lower() else PUBLIC_RETAIL
                )
                gate = classify_price_access(price_type=ptype, url=url, evidence_text=text[:1000])
                records.append(
                    PriceEvidenceRecord(
                        source_type=kind,
                        source_url=url,
                        document_type=DOC_HTML,
                        product_identity=matched,
                        price=price,
                        roles=[CURRENT_ACQUISITION_PRICE if gate["economics_eligible"] else GOVERNMENT_CHANNEL_PRICE],
                        confidence="MEDIUM",
                        evidence_location="html_identity_price",
                        evidence_text=f"{matched} ~ ${price}",
                        price_access=gate["price_access"],
                        acquisition_price_type=ptype,
                        economics_eligible_as_acquisition=gate["economics_eligible"] and ptype != COOPERATIVE_CONTRACT,
                        economics_eligible_as_history=False,
                        rejection_reason=None if gate["economics_eligible"] else "price_access_blocked",
                    )
                )
    return records
