"""Phase L.2.9 — PublicBoardPurchaseAdapter + open-data purchase discovery."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote_plus

from phase_l.pricing_sources import (
    HISTORICAL_GOV_PRICE,
    parse_content_auto,
    parse_council_award_text,
)
from phase_l.resilient_fetch import (
    FETCH_403,
    FETCH_429,
    FETCH_BOT_BLOCKED,
    FETCH_OK,
    DomainCircuitBreaker,
    resilient_fetch,
)
from phase_l.source_roles import HISTORICAL_GOV_PRICE as ROLE_HIST
from phase_l.source_roles import PRODUCT_IDENTITY, RECURRING_BUY

_DOC_HREF = re.compile(r'href=["\']([^"\']+\.(?:pdf|xlsx|xls|csv)[^"\']*)["\']', re.I)


class PublicBoardPurchaseAdapter:
    """City/county/school board packets that often name manufacturer, model, vendor, unit price."""

    family = "government board records"

    def discovery_queries(self, search_id: dict[str, Any], row: dict[str, Any] | None = None) -> list[str]:
        key = search_id.get("model") or search_id.get("primary_mpn") or search_id.get("sku")
        if not key:
            return []
        mfr = search_id.get("manufacturer") or ""
        buyer = ""
        if row:
            buyer = str(row.get("agency") or row.get("buyer") or "")[:40]
        base = f'"{mfr}" "{key}"' if mfr else f'"{key}"'
        out = [
            f"{base} agenda purchase approve",
            f"{base} council resolution award",
            f"{base} board packet bid",
            f"{base} school board purchase",
            f"{base} fleet replacement minutes",
            f"{base} filetype:pdf award",
        ]
        if buyer:
            out.insert(0, f'"{buyer}" {base} purchase')
        return out[:8]

    def discovery_urls(self, search_id: dict[str, Any]) -> list[str]:
        """Direct public board-doc search seeds (no login)."""
        key = search_id.get("model") or search_id.get("primary_mpn")
        if not key:
            return []
        q = quote_plus(str(key))
        return [
            f"https://www.google.com/search?q={quote_plus(str(key)+' council agenda purchase filetype:pdf')}",
            f"https://html.duckduckgo.com/html/?q={quote_plus(str(key)+' board packet award price')}",
        ]

    def research(
        self,
        *,
        search_id: dict[str, Any],
        row: dict[str, Any] | None = None,
        breaker: DomainCircuitBreaker,
        seed_urls: list[str] | None = None,
        max_fetches: int = 4,
    ) -> dict[str, Any]:
        from phase_l.market_price import search_urls_with_fallback
        from phase_l.product_page_resolution import normalize_url

        tele = {"attempts": 0, "fetch_ok": 0, "blocks": 0, "exact_hits": 0, "history_hits": 0, "docs": 0}
        records: list[dict[str, Any]] = []
        urls: list[str] = list(seed_urls or [])

        # Indexed discovery of board PDFs
        for q in self.discovery_queries(search_id, row)[:2]:
            try:
                ser = search_urls_with_fallback(q, limit=4)
                for u in ser.get("urls") or []:
                    if u not in urls:
                        urls.append(u)
            except Exception:
                pass

        for url in urls[:max_fetches]:
            if not breaker.allow(url):
                continue
            # Skip pure search-engine roots without docs
            if "google.com/search" in url and "filetype" not in url:
                continue
            fr = resilient_fetch(url, breaker=breaker, retries=0)
            tele["attempts"] += 1
            if fr.status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED}:
                tele["blocks"] += 1
                continue
            if fr.status != FETCH_OK:
                continue
            tele["fetch_ok"] += 1
            content = fr.content or (fr.text or "").encode("utf-8", errors="ignore")
            parsed = parse_content_auto(
                url=url,
                content=content,
                content_type=fr.content_type,
                search_id=search_id,
                prefer_history=True,
            )
            for rec in parsed or []:
                tele["exact_hits"] += 1
                d = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
                d["source_family"] = self.family
                d["roles"] = [ROLE_HIST, PRODUCT_IDENTITY, RECURRING_BUY]
                if d.get("price"):
                    tele["history_hits"] += 1
                records.append(d)
            # Also try council text parser on HTML
            if fr.text and "pdf" not in (fr.content_type or "").lower():
                for rec in parse_council_award_text(fr.text, search_id=search_id, source_url=url):
                    tele["exact_hits"] += 1
                    d = rec.to_dict()
                    d["source_family"] = self.family
                    d["roles"] = [ROLE_HIST, PRODUCT_IDENTITY]
                    if d.get("price"):
                        tele["history_hits"] += 1
                    records.append(d)
                # Follow linked PDFs from agenda index pages
                for m in _DOC_HREF.finditer(fr.text):
                    du = normalize_url(m.group(1), base=url)
                    if not du or du in urls:
                        continue
                    urls.append(du)
                    tele["docs"] += 1

        return {
            "kind": "PublicBoardPurchaseResearch",
            "family": self.family,
            "records": records,
            "telemetry": tele,
        }


class OpenDataPurchaseAdapter:
    """Socrata-style open-data discovery for PO / expenditure / contract datasets."""

    family = "open-data datasets"

    # Public discovery catalog search (no API key required for many portals)
    SOCRATA_DISCOVERY = "https://api.us.socrata.com/api/catalog/v1?q={q}&only=datasets&limit=5"

    def research(
        self,
        *,
        search_id: dict[str, Any],
        breaker: DomainCircuitBreaker,
        max_datasets: int = 3,
    ) -> dict[str, Any]:
        key = search_id.get("primary_mpn") or search_id.get("model")
        tele = {"attempts": 0, "fetch_ok": 0, "blocks": 0, "datasets": 0, "history_hits": 0, "exact_hits": 0}
        records: list[dict[str, Any]] = []
        if not key:
            return {"kind": "OpenDataPurchaseResearch", "family": self.family, "records": [], "telemetry": tele}

        # Search for procurement-related datasets mentioning product
        queries = [
            f"{key} purchase order",
            f"{key} expenditure",
            f"{key} vendor payment",
        ]
        dataset_urls: list[str] = []
        for q in queries[:2]:
            catalog = self.SOCRATA_DISCOVERY.format(q=quote_plus(q))
            if not breaker.allow(catalog):
                continue
            fr = resilient_fetch(catalog, breaker=breaker, retries=0, read_timeout=8.0)
            tele["attempts"] += 1
            if fr.status != FETCH_OK:
                if fr.status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED}:
                    tele["blocks"] += 1
                continue
            tele["fetch_ok"] += 1
            try:
                data = json.loads(fr.text or "{}")
            except Exception:
                continue
            for res in (data.get("results") or [])[:max_datasets]:
                link = (res.get("link") or "")
                pers = ((res.get("resource") or {}).get("page_views") and None)
                # Prefer resource API endpoint when present
                resource = res.get("resource") or {}
                rid = resource.get("id")
                domain = None
                meta = res.get("metadata") or {}
                domain = meta.get("domain")
                if domain and rid:
                    api = f"https://{domain}/resource/{rid}.json?$limit=50&$q={quote_plus(str(key))}"
                    dataset_urls.append(api)
                elif link:
                    dataset_urls.append(link)
                tele["datasets"] += 1
                _ = pers  # silence lint

        for api_url in dataset_urls[:max_datasets]:
            if not breaker.allow(api_url):
                continue
            fr = resilient_fetch(api_url, breaker=breaker, retries=0, read_timeout=8.0)
            tele["attempts"] += 1
            if fr.status != FETCH_OK:
                continue
            tele["fetch_ok"] += 1
            try:
                rows = json.loads(fr.text or "[]")
            except Exception:
                continue
            if not isinstance(rows, list):
                continue
            from phase_l.pricing_sources import text_has_identity, _num

            _DOLLAR = __import__("re").compile(
                r"\$\s*("
                r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"
                r"|\d{1,3}(?:,\d{3})*\.\d{2}"
                r"|\d{4,8}(?:\.\d{1,2})?"
                r")"
            )

            for row in rows[:40]:
                blob = " ".join(str(v) for v in row.values() if v is not None)
                hit, observed = text_has_identity(blob, search_id)
                if not hit:
                    continue
                tele["exact_hits"] += 1
                price = None
                for k, v in row.items():
                    lk = str(k).lower()
                    if any(x in lk for x in ("unit", "price", "amount", "cost", "total")):
                        price = _num(v)
                        if price is None and isinstance(v, str):
                            m = _DOLLAR.search(v)
                            if m:
                                price = float(m.group(1).replace(",", ""))
                        if price:
                            break
                if not price or price <= 0:
                    continue
                tele["history_hits"] += 1
                records.append(
                    {
                        "source_type": HISTORICAL_GOV_PRICE,
                        "source_url": api_url,
                        "document_type": "JSON",
                        "product_identity": observed,
                        "price": float(price),
                        "source_family": self.family,
                        "roles": [ROLE_HIST, RECURRING_BUY],
                        "confidence": "MEDIUM",
                        "economics_eligible_as_history": True,
                        "economics_eligible_as_acquisition": False,
                        "evidence_text": blob[:240],
                    }
                )

        return {
            "kind": "OpenDataPurchaseResearch",
            "family": self.family,
            "records": records,
            "telemetry": tele,
        }


def research_board_purchases(
    *,
    search_id: dict[str, Any],
    row: dict[str, Any] | None = None,
    breaker: DomainCircuitBreaker,
    max_fetches: int = 4,
) -> dict[str, Any]:
    return PublicBoardPurchaseAdapter().research(
        search_id=search_id, row=row, breaker=breaker, max_fetches=max_fetches
    )


def research_open_data_purchases(
    *,
    search_id: dict[str, Any],
    breaker: DomainCircuitBreaker,
    max_datasets: int = 3,
) -> dict[str, Any]:
    return OpenDataPurchaseAdapter().research(
        search_id=search_id, breaker=breaker, max_datasets=max_datasets
    )
