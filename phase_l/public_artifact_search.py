"""Phase L.13 — public artifact search queries + multi-provider discovery."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from phase_l.public_artifact_index import get_cached_discovery, set_cached_discovery
from phase_l.public_artifact_types import BUILD, DEFAULT_QUERY_BUDGET


def normalize_solicitation(sid: str | None) -> str:
    return "".join(c for c in str(sid or "") if c.isalnum())


def build_artifact_queries(
    *,
    platform: str,
    solicitation: str,
    buyer: str | None = None,
    title: str | None = None,
    model: str | None = None,
    product_family: str | None = None,
) -> list[dict[str, str]]:
    """Deterministic exact discovery queries — discovery only, not evidence."""
    sid = str(solicitation or "").strip()
    if not sid:
        return []
    norm = normalize_solicitation(sid)
    plat_host = {
        "BidNet": "bidnetdirect.com",
        "OpenGov": "opengov.com",
        "Bonfire": "bonfirehub.com",
        "IonWave": "ionwave.net",
        "PlanetBids": "planetbids.com",
        "DemandStar": "demandstar.com",
        "Public Purchase": "publicpurchase.com",
        "Jaggaer/SciQuest": "jaggaer.com",
    }.get(platform, "")

    qs: list[dict[str, str]] = []

    def add(kind: str, q: str) -> None:
        qs.append({"kind": kind, "query": q})

    if plat_host:
        add("exact_solicitation_search", f'site:{plat_host} "{sid}"')
        if norm and norm != sid:
            add("exact_solicitation_norm", f'site:{plat_host} "{norm}"')
        add("public_print_search", f'site:{plat_host} "{sid}" print')
        add("public_print_pdf", f'site:{plat_host} "{sid}" "print-pdf"')
        add("public_attachment_search", f'site:{plat_host}/public/attachments "{sid}"')
        add("public_award_search", f'site:{plat_host} "{sid}" award')
        add("filetype_pdf", f'site:{plat_host} "{sid}" filetype:pdf')
        add("filetype_xlsx", f'site:{plat_host} "{sid}" (filetype:xlsx OR filetype:xls)')
    else:
        add("exact_solicitation_search", f'"{sid}" solicitation award')
        add("public_award_search", f'"{sid}" award OR "bid tab" OR tabulation')
        add("filetype_pdf", f'"{sid}" award filetype:pdf')

    if buyer:
        add("buyer_solicitation_search", f'"{buyer}" "{sid}"')
        if model:
            add("buyer_model_search", f'site:{plat_host} "{buyer}" "{model}"' if plat_host else f'"{buyer}" "{model}" award')
        if product_family:
            add(
                "buyer_family_award",
                f'site:{plat_host} "{buyer}" "{product_family}" award'
                if plat_host
                else f'"{buyer}" "{product_family}" award',
            )
        # Buyer-controlled domain copies
        add("buyer_domain_docs", f'"{buyer}" "{sid}" (award OR "bid tab" OR solicitation) filetype:pdf')

    if title and len(title) > 12:
        short = title[:60].strip()
        add("title_search", f'"{short}" "{sid}"' if sid else f'"{short}" award')

    # Deduplicate by query text, preserve order; honor budget
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in qs:
        q = item["query"]
        if q in seen:
            continue
        seen.add(q)
        out.append(item)
        if len(out) >= DEFAULT_QUERY_BUDGET:
            break
    return out


def discover_artifact_urls(
    *,
    platform: str,
    solicitation: str,
    queries: list[dict[str, str]] | None = None,
    buyer: str | None = None,
    title: str | None = None,
    model: str | None = None,
    authorize_live: bool = True,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Multi-provider search discovery. Returns URLs only — not evidence grades."""
    queries = queries or build_artifact_queries(
        platform=platform, solicitation=solicitation, buyer=buyer, title=title, model=model
    )
    attempts: list[dict[str, Any]] = []
    urls: list[str] = []
    providers_used: list[str] = []

    for item in queries:
        kind = item["kind"]
        query = item["query"]
        cached = get_cached_discovery(platform, solicitation, kind) if use_cache else None
        if cached and cached.get("urls") is not None:
            attempts.append(
                {
                    "kind": kind,
                    "query": query,
                    "provider": cached.get("provider") or "cache",
                    "cached": True,
                    "url_count": len(cached.get("urls") or []),
                    "artifact_found": bool(cached.get("urls")),
                }
            )
            for u in cached.get("urls") or []:
                if u not in urls:
                    urls.append(u)
            continue

        if not authorize_live:
            attempts.append({"kind": kind, "query": query, "skipped": "no_live", "url_count": 0})
            continue

        try:
            from phase_l.market_price import search_urls_with_fallback

            ser = search_urls_with_fallback(query, limit=6)
        except Exception as exc:
            attempts.append({"kind": kind, "query": query, "error": str(exc)[:120], "url_count": 0})
            continue

        found = list(ser.get("urls") or [])
        provider = ser.get("provider")
        if provider:
            providers_used.append(str(provider))
        attempts.append(
            {
                "kind": kind,
                "query": query,
                "provider": provider,
                "ok": ser.get("ok"),
                "url_count": len(found),
                "artifact_found": bool(found),
                "direct_url_found": bool(found),
                "exact_match_confidence": "HIGH" if solicitation in " ".join(found) else ("MEDIUM" if found else "LOW"),
            }
        )
        if use_cache:
            set_cached_discovery(
                platform, solicitation, kind, urls=found, provider=provider, query=query
            )
        for u in found:
            if u not in urls:
                urls.append(u)

    return {
        "kind": "PublicArtifactDiscoveryResult",
        "build": BUILD,
        "platform": platform,
        "solicitation": solicitation,
        "urls": urls,
        "attempts": attempts,
        "providers": list(dict.fromkeys(providers_used)),
        "query_count": len(attempts),
    }
