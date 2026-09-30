"""L.17.3 reusable PlatformAdapters — config-driven multi-buyer ingestion.

Maps already-discovered platform jurisdictions → live opportunity harvest with
access-state classification, checkpoints, and failure isolation.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from application_clock import now_utc
from discovery.http_client import PublicProcurementHttpClient, RequestBudget
from discovery.jurisdiction_registry import REGISTRY_PATH
from discovery.live_fetchers import (
    BonfireLiveFetcher,
    IonWaveLiveFetcher,
    OpenGovLiveFetcher,
    PlanetBidsLiveFetcher,
    SimpleHtmlLiveFetcher,
)
from discovery.lower48 import (
    FREE_REGISTRATION_REQUIRED,
    NONFEDERAL_ACCESSIBLE_NOW,
    PORTAL_DISCOVERED_NOT_INTEGRATED,
)

BUILD = "20260928-m3-phase-l173-platform-adapters-accessible-now-growth"

# Platform access states (OpenGov §4 / general)
PUBLIC_LISTING_AUTOMATABLE = "PUBLIC_LISTING_AUTOMATABLE"
PUBLIC_LISTING_MANUAL = "PUBLIC_LISTING_MANUAL"
FREE_ACCOUNT_TO_BID = "FREE_ACCOUNT_TO_BID"
AUTH_REQUIRED_TO_VIEW = "AUTH_REQUIRED_TO_VIEW"
ANTI_BOT = "ANTI_BOT"
NO_CURRENT_OPPORTUNITIES = "NO_CURRENT_OPPORTUNITIES"
ADAPTER_FAILED = "ADAPTER_FAILED"

# Source activation (§14)
MAPPED_ONLY = "MAPPED_ONLY"
PUBLIC_DISCOVERY_READY = "PUBLIC_DISCOVERY_READY"
INGESTION_ACTIVE = "INGESTION_ACTIVE"
MANUAL_ONLY = "MANUAL_ONLY"
REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"
BLOCKED = "BLOCKED"

# Document access (§17)
DOCS_PUBLIC = "DOCS_PUBLIC"
DOCS_FREE_REGISTRATION = "DOCS_FREE_REGISTRATION"
DOCS_AUTH_REQUIRED = "DOCS_AUTH_REQUIRED"
DOCS_UNAVAILABLE = "DOCS_UNAVAILABLE"

# Crosswalk confidence (§26)
EXACT = "EXACT"
STRONG = "STRONG"
AMBIGUOUS = "AMBIGUOUS"
NO_MATCH = "NO_MATCH"

HIGH_LEVERAGE_PLATFORM_REGISTRATION = "HIGH_LEVERAGE_PLATFORM_REGISTRATION"

DATA = Path(__file__).resolve().parents[1] / "data"
CHECKPOINT_PATH = DATA / "l173_platform_checkpoint.json"

# Known productive agency mirrors when OpenGov CDN is Cloudflare-walled
OPENGOV_AGENCY_MIRRORS: dict[str, str] = {
    "AZ:CITY:0455000:phoenix_city": "https://solicitations.phoenix.gov/Solicitations",
    "MA:CITY:2507000:boston_city": "https://www.boston.gov/bid-listings",
    "IA:COUNTY:19113:linn_county": "https://www.linncountyiowa.gov/149/Purchasing",
    "VA:COUNTY:51107:loudoun_county": "https://www.loudoun.gov/929/Current-Solicitations",
    "WA:COUNTY:53033:king_county": "https://kingcounty.gov/depts/finance/procurement.aspx",
    "VA:COUNTY:51041:chesterfield_county": "https://www.chesterfield.gov/149/Purchasing",
    "MO:COUNTY:29095:jackson_county": "https://www.jacksongov.org/154/Purchasing",
    "GA:CITY:1369000:savannah_city": "https://www.savannahga.gov/137/Purchasing",
    "IL:CITY:1703012:aurora_city": "https://www.aurora-il.org/149/Purchasing",
    "KS:CITY:2079000:wichita_city": "https://www.wichita.gov/165/Purchasing",
    "VA:CITY:5157000:norfolk_city": "https://www.norfolk.gov/149/Purchasing",
    "TX:CITY:4858016:plano_city": "https://www.plano.gov/1475/Purchasing",
    "IL:COUNTY:17197:will_county": "https://www.willcountyillinois.com/County-Offices/Finance/Purchasing",
    "GA:COUNTY:13067:cobb_county": "https://www.cobbcounty.org/finance/purchasing",
}


def _utc() -> str:
    return now_utc().isoformat()


def load_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def load_checkpoint() -> dict[str, Any]:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {"platforms": {}, "updated_at": None, "build": BUILD}


def save_checkpoint(cp: dict[str, Any]) -> None:
    cp["updated_at"] = _utc()
    cp["build"] = BUILD
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(cp, indent=2, default=str), encoding="utf-8")


def extract_portal_slug(url: str) -> str | None:
    m = re.search(r"procurement\.opengov\.com/portal/([^/?#]+)", url or "", re.I)
    return m.group(1) if m else None


def jurisdictions_for_platform(reg: dict[str, Any], platform: str) -> list[dict[str, Any]]:
    return [
        j
        for j in (reg.get("jurisdictions") or {}).values()
        if str(j.get("procurement_platform") or "") == platform
    ]


def crosswalk_confidence(name_a: str, name_b: str) -> str:
    a = re.sub(r"[^a-z0-9]+", "", (name_a or "").lower())
    b = re.sub(r"[^a-z0-9]+", "", (name_b or "").lower())
    if not a or not b:
        return NO_MATCH
    if a == b:
        return EXACT
    if a in b or b in a:
        return STRONG
    # token overlap
    ta, tb = set(re.findall(r"[a-z0-9]+", (name_a or "").lower())), set(
        re.findall(r"[a-z0-9]+", (name_b or "").lower())
    )
    if ta and tb and len(ta & tb) / max(1, len(ta | tb)) >= 0.6:
        return STRONG
    if ta & tb:
        return AMBIGUOUS
    return NO_MATCH


def _is_cloudflare(body: str, status: int | None = None) -> bool:
    low = (body or "").lower()
    if status in {403, 503} and ("just a moment" in low or "cf-" in low or "cloudflare" in low):
        return True
    return "just a moment" in low and "cloudflare" in low


def _bid_board_child_urls(list_url: str, body: str) -> list[str]:
    """Find likely bid-board child links on purchasing landing pages."""
    out: list[str] = []
    for m in re.finditer(
        r'href=["\']([^"\']+)["\'][^>]*>([^<]{0,100})</a>',
        body or "",
        re.I,
    ):
        href, text = m.group(1), (m.group(2) or "").lower()
        blob = f"{href} {text}"
        if any(
            k in blob
            for k in (
                "solicitation",
                "bid-listing",
                "bidlisting",
                "current-bid",
                "open-bid",
                "opportunit",
                "/bids",
                "rfq",
                "rfp",
                "purchasing/bids",
            )
        ):
            if any(x in blob for x in ("career", "job", "employment", "login", "register")):
                continue
            full = urljoin(list_url, href)
            if "opengov.com" in full.lower():
                continue  # CDN usually Cloudflare
            if full not in out:
                out.append(full)
    # Phoenix-style /Solicitations sibling
    if "solicitations." in (list_url or "").lower() and "/Solicitations" not in list_url:
        out.insert(0, urljoin(list_url, "/Solicitations"))
    return out[:5]


def classify_fetch_result(
    *,
    status: int | None,
    body: str,
    n_opps: int,
    platform: str,
    list_url: str,
) -> tuple[str, str, str]:
    """Return (access_state, activation_state, docs_state)."""
    if _is_cloudflare(body, status):
        return ANTI_BOT, BLOCKED, DOCS_UNAVAILABLE
    if status in {401, 403}:
        return AUTH_REQUIRED_TO_VIEW, BLOCKED, DOCS_AUTH_REQUIRED
    if status and status >= 500:
        return ADAPTER_FAILED, MAPPED_ONLY, DOCS_UNAVAILABLE
    if n_opps > 0:
        docs = DOCS_PUBLIC
        if platform in {"BidNet", "PublicPurchase", "DemandStar"}:
            return FREE_ACCOUNT_TO_BID, INGESTION_ACTIVE, DOCS_FREE_REGISTRATION
        return PUBLIC_LISTING_AUTOMATABLE, INGESTION_ACTIVE, docs
    if status == 200 and body:
        # Page reachable but no parseable open bids
        if "login" in (body or "").lower() and "password" in (body or "").lower():
            return AUTH_REQUIRED_TO_VIEW, BLOCKED, DOCS_AUTH_REQUIRED
        return NO_CURRENT_OPPORTUNITIES, PUBLIC_DISCOVERY_READY, DOCS_PUBLIC
    return ADAPTER_FAILED, MAPPED_ONLY, DOCS_UNAVAILABLE


def _opp_to_row(
    opp: Any,
    *,
    platform: str,
    jurisdiction: dict[str, Any],
    list_url: str,
    access_state: str,
    docs_state: str,
) -> dict[str, Any]:
    d = opp.to_dict() if hasattr(opp, "to_dict") else dict(opp)
    jid = jurisdiction.get("jurisdiction_id")
    d["source_id"] = f"platform_{platform.lower()}_{jid}"
    d["source_portal"] = d["source_id"]
    d["platform_family"] = platform
    d["state_code"] = jurisdiction.get("state") or d.get("state_code")
    d["agency"] = d.get("agency") or jurisdiction.get("name")
    d["jurisdiction"] = jurisdiction.get("buyer_type") or "LOCAL"
    d["buyer_type"] = jurisdiction.get("buyer_type")
    d["jurisdiction_registry_id"] = jid
    d["source_url"] = list_url
    d["discovery_url"] = list_url
    if not d.get("detail_url"):
        d["detail_url"] = list_url
    d["our_bid_access"] = "YES"
    if access_state == FREE_ACCOUNT_TO_BID:
        d["registration_action"] = "REGISTER_BEFORE_BID"
    d["document_access"] = docs_state
    d["platform_access_state"] = access_state
    d["l173_platform_harvest"] = True
    d["authoritative_bid_location"] = {
        "detail_url": d.get("detail_url"),
        "solicitation_id": d.get("solicitation_number") or d.get("external_id"),
        "buyer": d.get("agency"),
        "submission_portal": d.get("detail_url") or list_url,
    }
    d["discovery_provenance"] = {
        "source_id": d["source_id"],
        "source_url": list_url,
        "platform": platform,
    }
    meta = dict(d.get("raw_metadata") or {})
    meta["platform_adapter"] = platform
    meta["jurisdiction_registry_id"] = jid
    meta["document_access"] = docs_state
    d["raw_metadata"] = meta
    return d


class PlatformAdapter:
    """Base config-driven platform adapter."""

    platform: str = "Generic"
    fetcher_factory: Any = SimpleHtmlLiveFetcher

    def resolve_list_url(self, jurisdiction: dict[str, Any]) -> str | None:
        jid = jurisdiction.get("jurisdiction_id")
        if jid and jid in OPENGOV_AGENCY_MIRRORS:
            return OPENGOV_AGENCY_MIRRORS[jid]
        return jurisdiction.get("bid_portal") or jurisdiction.get("procurement_page")

    def fetch_jurisdiction(
        self,
        client: PublicProcurementHttpClient,
        jurisdiction: dict[str, Any],
    ) -> dict[str, Any]:
        list_url = self.resolve_list_url(jurisdiction)
        jid = jurisdiction.get("jurisdiction_id")
        result: dict[str, Any] = {
            "jurisdiction_id": jid,
            "platform": self.platform,
            "list_url": list_url,
            "portal_slug": extract_portal_slug(list_url or ""),
            "rows": [],
            "access_state": MAPPED_ONLY,
            "activation_state": MAPPED_ONLY,
            "docs_state": DOCS_UNAVAILABLE,
            "error": None,
        }
        if not list_url:
            result["access_state"] = ADAPTER_FAILED
            result["error"] = "no_list_url"
            return result
        try:
            # Prefer agency mirrors / child bid boards when CDN would fail
            urls_to_try = [list_url]
            if "opengov.com" in list_url.lower():
                # CDN typically Cloudflare — short-circuit classification unless mirror exists
                result["access_state"] = ANTI_BOT
                result["activation_state"] = BLOCKED
                result["docs_state"] = DOCS_UNAVAILABLE
                result["error"] = "opengov_cdn_cloudflare"
                return result

            fetcher = self.fetcher_factory()
            source_id = f"l173_{self.platform.lower()}_{jid}"
            raw = fetcher.fetch_listing(client, list_url=list_url, source_id=source_id, max_pages=1)
            opps = list(raw.get("opportunities") or [])
            body = ""
            status = None
            try:
                resp = client.get(list_url, source_id=f"{source_id}_meta")
                body = resp.text or ""
                status = resp.status_code
            except Exception:
                body = ""
                status = None

            if not opps and body:
                for child in _bid_board_child_urls(list_url, body):
                    try:
                        raw2 = fetcher.fetch_listing(
                            client, list_url=child, source_id=f"{source_id}_child", max_pages=1
                        )
                        child_opps = list(raw2.get("opportunities") or [])
                        if child_opps:
                            opps = child_opps
                            list_url = child
                            result["list_url"] = child
                            result["followed_child"] = child
                            break
                        # try simplehtml fallback on child
                        raw3 = SimpleHtmlLiveFetcher().fetch_listing(
                            client, list_url=child, source_id=f"{source_id}_sh", max_pages=1
                        )
                        child_opps = list(raw3.get("opportunities") or [])
                        if child_opps:
                            opps = child_opps
                            list_url = child
                            result["list_url"] = child
                            result["followed_child"] = child
                            break
                    except Exception:
                        continue

            access, activation, docs = classify_fetch_result(
                status=status,
                body=body,
                n_opps=len(opps),
                platform=self.platform,
                list_url=list_url,
            )
            result["access_state"] = access
            result["activation_state"] = activation
            result["docs_state"] = docs
            result["opportunity_count"] = len(opps)
            rows = [
                _opp_to_row(
                    o,
                    platform=self.platform,
                    jurisdiction=jurisdiction,
                    list_url=list_url,
                    access_state=access,
                    docs_state=docs,
                )
                for o in opps
            ]
            result["rows"] = rows
        except Exception as e:
            result["access_state"] = ADAPTER_FAILED
            result["activation_state"] = MAPPED_ONLY
            result["error"] = str(e)[:240]
        return result


class OpenGovPlatformAdapter(PlatformAdapter):
    platform = "OpenGov"
    fetcher_factory = OpenGovLiveFetcher


class BonfirePlatformAdapter(PlatformAdapter):
    platform = "Bonfire"
    fetcher_factory = BonfireLiveFetcher


class PlanetBidsPlatformAdapter(PlatformAdapter):
    platform = "PlanetBids"
    fetcher_factory = PlanetBidsLiveFetcher


class IonWavePlatformAdapter(PlatformAdapter):
    platform = "IonWave"
    fetcher_factory = IonWaveLiveFetcher


class SimpleHtmlPlatformAdapter(PlatformAdapter):
    platform = "SimpleHTML"
    fetcher_factory = SimpleHtmlLiveFetcher


ADAPTERS: dict[str, type[PlatformAdapter]] = {
    "OpenGov": OpenGovPlatformAdapter,
    "Bonfire": BonfirePlatformAdapter,
    "PlanetBids": PlanetBidsPlatformAdapter,
    "IonWave": IonWavePlatformAdapter,
    "SimpleHTML": SimpleHtmlPlatformAdapter,
}


def run_platform_backfill(
    platform: str,
    *,
    max_jurisdictions: int | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Run adapter across all mapped jurisdictions for a platform (failure-isolated)."""
    reg = load_registry()
    juris = jurisdictions_for_platform(reg, platform)
    cp = load_checkpoint()
    plat_cp = cp.setdefault("platforms", {}).setdefault(
        platform, {"completed": {}, "updated_at": None}
    )
    completed = plat_cp.get("completed") or {}
    adapter_cls = ADAPTERS.get(platform)
    if not adapter_cls:
        return {"platform": platform, "error": "no_adapter", "mapped": len(juris)}

    adapter = adapter_cls()
    client = PublicProcurementHttpClient(
        authorize_live=True,
        budget=RequestBudget(max_total_requests=2000, max_requests_per_source=8),
    )

    targets = juris
    if resume:
        targets = [j for j in juris if j["jurisdiction_id"] not in completed]
    if max_jurisdictions is not None:
        targets = targets[:max_jurisdictions]

    states: Counter = Counter()
    activation: Counter = Counter()
    all_rows: list[dict[str, Any]] = []
    per_j: list[dict[str, Any]] = []
    tested = 0

    for j in targets:
        jid = j["jurisdiction_id"]
        try:
            res = adapter.fetch_jurisdiction(client, j)
        except Exception as e:
            res = {
                "jurisdiction_id": jid,
                "platform": platform,
                "access_state": ADAPTER_FAILED,
                "activation_state": MAPPED_ONLY,
                "docs_state": DOCS_UNAVAILABLE,
                "rows": [],
                "error": str(e)[:200],
            }
        tested += 1
        states[res.get("access_state") or ADAPTER_FAILED] += 1
        activation[res.get("activation_state") or MAPPED_ONLY] += 1
        all_rows.extend(res.get("rows") or [])
        summary = {k: res.get(k) for k in (
            "jurisdiction_id", "list_url", "access_state", "activation_state",
            "docs_state", "opportunity_count", "error", "followed_child", "portal_slug",
        )}
        summary["opportunity_count"] = len(res.get("rows") or [])
        per_j.append(summary)
        completed[jid] = {
            "access_state": res.get("access_state"),
            "activation_state": res.get("activation_state"),
            "opportunity_count": summary["opportunity_count"],
            "at": _utc(),
        }
        # Update registry activation fields
        target = (reg.get("jurisdictions") or {}).get(jid)
        if target:
            target["platform_access_state"] = res.get("access_state")
            target["source_activation"] = res.get("activation_state")
            target["document_access"] = res.get("docs_state")
            target["last_discovery_attempt"] = _utc()
            if res.get("activation_state") == INGESTION_ACTIVE:
                target["source_status"] = (
                    FREE_REGISTRATION_REQUIRED
                    if res.get("access_state") == FREE_ACCOUNT_TO_BID
                    else "AUTOMATED_STATIC"
                )
                target["last_successful_discovery"] = _utc()
                if res.get("list_url"):
                    target["bid_portal"] = res["list_url"]
            elif res.get("access_state") == ANTI_BOT:
                target["source_status"] = "AUTH_BLOCKED"
            elif res.get("access_state") == NO_CURRENT_OPPORTUNITIES:
                target["source_status"] = PORTAL_DISCOVERED_NOT_INTEGRATED

        if tested % 10 == 0:
            plat_cp["completed"] = completed
            plat_cp["updated_at"] = _utc()
            save_checkpoint(cp)
            REGISTRY_PATH.write_text(
                json.dumps(reg, separators=(",", ":"), default=str), encoding="utf-8"
            )

    plat_cp["completed"] = completed
    plat_cp["updated_at"] = _utc()
    save_checkpoint(cp)
    REGISTRY_PATH.write_text(json.dumps(reg, separators=(",", ":"), default=str), encoding="utf-8")

    active = sum(1 for s in completed.values() if s.get("activation_state") == INGESTION_ACTIVE)
    return {
        "kind": f"L173{platform}Results",
        "build": BUILD,
        "generated_at": _utc(),
        "platform": platform,
        "mapped": len(juris),
        "tested": tested,
        "previously_completed": len(completed) - tested,
        "access_state_counts": dict(states),
        "activation_state_counts": dict(activation),
        "ingestion_active": active,
        "live_rows": len(all_rows),
        "jurisdictions": per_j,
        "rows": all_rows,
        "AdapterYieldScore": round(
            (active * 10 + len(all_rows) * 2) / max(1, tested or 1), 2
        ),
    }


def platform_inventory(reg: dict[str, Any] | None = None) -> dict[str, Any]:
    reg = reg or load_registry()
    by: dict[str, list[str]] = defaultdict(list)
    for j in (reg.get("jurisdictions") or {}).values():
        p = j.get("procurement_platform")
        if p:
            by[str(p)].append(j["jurisdiction_id"])
    return {
        "kind": "L173PlatformAdapterInventory",
        "build": BUILD,
        "generated_at": _utc(),
        "platforms": [
            {
                "platform": p,
                "mapped_jurisdictions": len(ids),
                "adapter": p in ADAPTERS,
                "sample_ids": ids[:8],
            }
            for p, ids in sorted(by.items(), key=lambda x: -len(x[1]))
        ],
        "adapters_implemented": list(ADAPTERS.keys()),
        "opengov_agency_mirrors": OPENGOV_AGENCY_MIRRORS,
    }
