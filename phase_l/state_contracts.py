"""Phase L.2.9 — StateContractPriceAdapter + priority state term-contract discovery."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

from phase_l.pricing_sources import (
    CURRENT_ACQUISITION_PRICE,
    GOVERNMENT_CHANNEL_PRICE,
    HISTORICAL_GOV_PRICE,
    PriceEvidenceRecord,
    parse_content_auto,
)
from phase_l.resilient_fetch import (
    FETCH_403,
    FETCH_429,
    FETCH_BOT_BLOCKED,
    FETCH_JS_EMPTY,
    FETCH_OK,
    DomainCircuitBreaker,
    resilient_fetch,
)
from phase_l.source_roles import (
    CURRENT_GOV_CONTRACT_PRICE,
    HISTORICAL_GOV_PRICE as ROLE_HIST,
    SOURCE_AUTH_REQUIRED,
    SOURCE_RESTRICTED,
)

# Priority state portals — public search/landing URLs (no credentials)
STATE_CONTRACT_PORTALS: list[dict[str, Any]] = [
    {
        "state": "WA",
        "name": "Washington DES Contracts",
        "search_url": "https://apps.des.wa.gov/DESContracts/Home/ContractSearch?searchText={q}",
        "alt_url": "https://des.wa.gov/sell-goods-services/bidding-opportunities?search={q}",
        "priority": 1,
    },
    {
        "state": "TX",
        "name": "Texas SmartBuy",
        "search_url": "https://www.txsmartbuy.com/contracts?search={q}",
        "priority": 2,
    },
    {
        "state": "NC",
        "name": "North Carolina eProcurement",
        "search_url": "https://evp.nc.gov/solicitations/?search={q}",
        "priority": 3,
    },
    {
        "state": "VA",
        "name": "Virginia eVA",
        "search_url": "https://eva.virginia.gov/pages/eva-public-access.htm?q={q}",
        "priority": 3,
    },
    {
        "state": "FL",
        "name": "Florida MyFloridaMarketPlace",
        "search_url": "https://vendor.myfloridamarketplace.com/search/bids?keywords={q}",
        "priority": 3,
    },
    {
        "state": "GA",
        "name": "Team Georgia Marketplace",
        "search_url": "https://ssl.doas.state.ga.us/PRSapp/PR_index.jsp?search={q}",
        "priority": 4,
    },
    {
        "state": "PA",
        "name": "Pennsylvania eMarketplace",
        "search_url": "https://www.emarketplace.state.pa.us/Search.aspx?q={q}",
        "priority": 4,
    },
    {
        "state": "IL",
        "name": "Illinois BidBuy",
        "search_url": "https://www.bidbuy.illinois.gov/bso/view/search/external/advancedSearchBid.xhtml?keyword={q}",
        "priority": 4,
    },
    {
        "state": "OH",
        "name": "OhioBuys",
        "search_url": "https://ohiobuys.ohio.gov/page.aspx/en/rfp/request_browse_public?q={q}",
        "priority": 4,
    },
    {
        "state": "MI",
        "name": "Michigan SIGMA",
        "search_url": "https://sigma.michigan.gov/webapp/PRDVSS2X1/AltSelfService?q={q}",
        "priority": 5,
    },
    {
        "state": "AZ",
        "name": "Arizona Procurement Portal",
        "search_url": "https://app.az.gov/page.aspx/en/rfp/request_browse_public?q={q}",
        "priority": 5,
    },
    {
        "state": "CA",
        "name": "California eProcure",
        "search_url": "https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx?q={q}",
        "priority": 5,
    },
    {
        "state": "IA",
        "name": "Iowa Bid Opportunities",
        "search_url": "https://bidopportunities.iowa.gov/?q={q}",
        "priority": 2,
    },
    {
        "state": "MT",
        "name": "Montana eMACS",
        "search_url": "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana&q={q}",
        "priority": 2,
    },
]

_DOC_HREF = re.compile(
    r'href=["\']([^"\']+\.(?:pdf|xlsx|xls|csv)[^"\']*)["\']',
    re.I,
)
_LOGIN = re.compile(r"\b(sign\s*in|log\s*in|authentication\s+required|create\s+an\s+account)\b", re.I)
_RESTRICTED = re.compile(r"\b(\.gov|\.mil)\s+email\s+required|federal\s+employees?\s+only\b", re.I)


class StateContractPriceAdapter:
    """Discover + parse publicly linked state term-contract pricing artifacts."""

    family = "state term contracts"

    def discovery_urls(self, search_id: dict[str, Any], *, limit: int = 6) -> list[dict[str, Any]]:
        key = search_id.get("primary_mpn") or search_id.get("model") or search_id.get("sku")
        if not key:
            return []
        q = quote_plus(str(key))
        portals = sorted(STATE_CONTRACT_PORTALS, key=lambda p: p.get("priority", 99))
        out = []
        for p in portals[:limit]:
            url = (p.get("search_url") or "").format(q=q)
            out.append(
                {
                    "url": url,
                    "state": p["state"],
                    "name": p["name"],
                    "source_family": self.family,
                    "roles": [CURRENT_GOV_CONTRACT_PRICE, ROLE_HIST],
                }
            )
        return out

    def extract_pricing_document_links(self, html: str, *, base_url: str) -> list[str]:
        from phase_l.product_page_resolution import normalize_url

        if not html:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for m in _DOC_HREF.finditer(html):
            u = normalize_url(m.group(1), base=base_url)
            if not u or u in seen:
                continue
            seen.add(u)
            low = u.lower()
            score = 0
            if any(x in low for x in ("price", "pricing", "schedule", "catalog", "msrp", "award")):
                score += 20
            if score or low.endswith((".pdf", ".xlsx", ".csv", ".xls")):
                out.append(u)
        return out[:12]

    def research(
        self,
        *,
        search_id: dict[str, Any],
        breaker: DomainCircuitBreaker,
        max_portals: int = 3,
        max_docs: int = 3,
    ) -> dict[str, Any]:
        tele = {
            "attempts": 0,
            "fetch_ok": 0,
            "blocks": 0,
            "auth_required": 0,
            "restricted": 0,
            "docs_found": 0,
            "exact_hits": 0,
            "price_hits": 0,
            "history_hits": 0,
        }
        records: list[dict[str, Any]] = []
        status_flags: list[str] = []

        for portal in self.discovery_urls(search_id, limit=max_portals):
            url = portal["url"]
            if not breaker.allow(url):
                continue
            fr = resilient_fetch(url, breaker=breaker, retries=0)
            tele["attempts"] += 1
            if fr.status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED, FETCH_JS_EMPTY}:
                tele["blocks"] += 1
                continue
            if fr.status != FETCH_OK:
                continue
            tele["fetch_ok"] += 1
            text = fr.text or ""
            if _RESTRICTED.search(text):
                tele["restricted"] += 1
                status_flags.append(SOURCE_RESTRICTED)
                continue
            if _LOGIN.search(text) and len(text) < 8000:
                tele["auth_required"] += 1
                status_flags.append(SOURCE_AUTH_REQUIRED)
                continue

            # Parse landing page itself
            parsed = parse_content_auto(
                url=url,
                content=fr.content or text.encode("utf-8", errors="ignore"),
                content_type=fr.content_type,
                search_id=search_id,
                prefer_history=False,
            )
            for rec in parsed or []:
                tele["exact_hits"] += 1
                d = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
                d["source_family"] = self.family
                d["state"] = portal["state"]
                d["roles"] = [CURRENT_GOV_CONTRACT_PRICE]
                if d.get("price"):
                    tele["price_hits"] += 1
                    d["acquisition_price_type"] = d.get("acquisition_price_type") or GOVERNMENT_CHANNEL_PRICE
                records.append(d)

            # Follow linked pricing documents
            docs = self.extract_pricing_document_links(text, base_url=url)
            tele["docs_found"] += len(docs)
            for doc_url in docs[:max_docs]:
                if not breaker.allow(doc_url):
                    continue
                dfr = resilient_fetch(doc_url, breaker=breaker, retries=0)
                tele["attempts"] += 1
                if dfr.status != FETCH_OK:
                    if dfr.status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED}:
                        tele["blocks"] += 1
                    continue
                tele["fetch_ok"] += 1
                dparsed = parse_content_auto(
                    url=doc_url,
                    content=dfr.content,
                    content_type=dfr.content_type,
                    search_id=search_id,
                    prefer_history=True,
                )
                for rec in dparsed or []:
                    tele["exact_hits"] += 1
                    d = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
                    d["source_family"] = self.family
                    d["state"] = portal["state"]
                    roles = [CURRENT_GOV_CONTRACT_PRICE]
                    if d.get("source_type") == HISTORICAL_GOV_PRICE:
                        roles.append(ROLE_HIST)
                        tele["history_hits"] += 1
                    d["roles"] = roles
                    if d.get("price"):
                        tele["price_hits"] += 1
                    records.append(d)
                if tele["price_hits"] >= 3:
                    break
            if tele["price_hits"] >= 3:
                break

        return {
            "kind": "StateContractPriceResearch",
            "family": self.family,
            "records": records,
            "telemetry": tele,
            "status_flags": list(dict.fromkeys(status_flags)),
        }


def research_state_contracts(
    *,
    search_id: dict[str, Any],
    breaker: DomainCircuitBreaker,
    max_portals: int = 3,
    max_docs: int = 3,
) -> dict[str, Any]:
    return StateContractPriceAdapter().research(
        search_id=search_id, breaker=breaker, max_portals=max_portals, max_docs=max_docs
    )
