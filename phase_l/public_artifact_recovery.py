"""Phase L.13 — PUBLIC_ARTIFACT_RECOVERY (no CAPTCHA/auth bypass / no ID brute-force)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from phase_l.auth_access import (
    FREE_REGISTRATION_REQUIRED,
    PLATFORM_HISTORY_BLOCKED,
    PUBLIC_ANTI_BOT_BLOCKED,
    classify_access_mode,
)
from phase_l.auth_history_recovery import (
    extract_competition_from_bid_tab,
    match_bid_tab_line,
    registration_opportunity,
    history_access_registration_priority,
)
from phase_l.buyer_history_paths import discover_buyer_path_urls, record_successful_recovery
from phase_l.exact_history_recovery import grade_recovered_award
from phase_l.history_graphs import link_product_history, normalize_award_tabulation
from phase_l.platform_history import detect_platform
from phase_l.public_artifact_index import (
    apply_observed_pattern,
    classify_artifact_type,
    extract_public_links,
    observed_patterns_for,
    record_observed_url_pattern,
    upsert_artifact,
)
from phase_l.public_artifact_search import build_artifact_queries, discover_artifact_urls
from phase_l.public_artifact_types import (
    AUTHENTICATED_HISTORY_REQUIRED,
    AWARD_PDF,
    AWARD_PRINT_VIEW,
    BID_TAB,
    BIDNET_RECOVERY_ORDER,
    BOARD_DOCUMENT,
    BUILD,
    BUYER_ARTIFACT_RECOVERED,
    DEFAULT_FETCH_BUDGET,
    DEFAULT_QUERY_BUDGET,
    EVIDENCE_EXHAUSTED,
    FORBIDDEN_ACTIONS,
    FREE_REGISTRATION_STILL_REQUIRED,
    NO_PUBLIC_ARTIFACT_FOUND,
    PUBLIC_ARTIFACT_RECOVERED,
    PUBLIC_ARTIFACT_RECOVERY,
    PUBLIC_ATTACHMENT,
    PUBLIC_METADATA_ONLY,
    SOLICITATION_PRINT_VIEW,
    TABULATION,
)
from phase_l.quality_audit import GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C, GOV_VALUE_D
from phase_l.quote_economics import _f
from phase_l.supplier_upgrade import classify_prior_awardee_role


def _utc() -> str:
    return now_utc().isoformat()


def _norm(s: Any) -> str:
    return str(s or "").strip().upper()


def exact_artifact_link(
    *,
    url: str,
    solicitation: str,
    buyer: str | None = None,
    title: str | None = None,
    model: str | None = None,
    page_text: str | None = None,
) -> dict[str, Any]:
    """Strong identity linkage — reject keyword-only matches."""
    sid = str(solicitation or "").strip()
    blob = f"{url} {page_text or ''}".lower()
    basis: list[str] = []
    score = 0
    if sid and sid.lower() in blob:
        basis.append("solicitation_number_in_artifact")
        score += 4
    # normalized digit form
    digits = "".join(c for c in sid if c.isdigit())
    if digits and len(digits) >= 6 and digits in blob.replace("-", "").replace(" ", ""):
        if "solicitation_number_in_artifact" not in basis:
            basis.append("normalized_solicitation_digits")
            score += 3
    if buyer and _norm(buyer)[:8] and _norm(buyer)[:8].lower() in blob:
        basis.append("buyer_name")
        score += 2
    if title:
        tok = _norm(title)[:20]
        if tok and tok.lower() in blob:
            basis.append("title_token")
            score += 1
    if model and str(model).lower() in blob:
        basis.append("model")
        score += 2

    linked = score >= 4 or ("solicitation_number_in_artifact" in basis and score >= 3)
    return {
        "linked": linked,
        "score": score,
        "exact_match_basis": basis,
        "rejected_reason": None if linked else "insufficient_solicitation_identity",
    }


def reject_wrong_solicitation(url: str, solicitation: str, page_text: str | None = None) -> bool:
    """True when page clearly references a different solicitation id."""
    sid = str(solicitation or "").strip()
    if not sid:
        return False
    blob = f"{url} {page_text or ''}"
    if sid in blob:
        return False
    # Other long numeric IDs in BidNet URLs that don't match ours
    others = re.findall(r"/(\d{9,15})(?:/|\\?|$)", url)
    if others and sid not in others and all(o != sid for o in others):
        return True
    return False


class PublicArtifactRecoveryAdapter:
    """Platform-agnostic adapter — subclasses/patterns only from observed URLs."""

    platform = "generic"
    hosts: tuple[str, ...] = ()

    def seed_urls(self, row: dict[str, Any]) -> list[str]:
        seeds = []
        for k in ("original_solicitation_url", "source_url", "url", "original_posting_url"):
            u = row.get(k)
            if u and str(u) not in seeds:
                seeds.append(str(u))
        return seeds

    def instantiate_observed_patterns(self, solicitation: str) -> list[str]:
        out = []
        for p in observed_patterns_for(self.platform):
            u = apply_observed_pattern(p, solicitation)
            if u and u not in out:
                out.append(u)
        return out

    def prioritize_urls(self, urls: list[str]) -> list[str]:
        def rank(u: str) -> int:
            t = u.lower()
            score = 0
            if "award" in t:
                score += 5
            if "bid" in t and "tab" in t:
                score += 5
            if "print" in t:
                score += 4
            if ".pdf" in t:
                score += 3
            if "attachment" in t:
                score += 2
            if "abstract" in t:
                score += 2
            if any(h in t for h in self.hosts):
                score += 1
            return -score

        return sorted(dict.fromkeys(urls), key=rank)


class BidNetPublicArtifactAdapter(PublicArtifactRecoveryAdapter):
    platform = "BidNet"
    hosts = ("bidnetdirect.com",)

    def seed_urls(self, row: dict[str, Any]) -> list[str]:
        seeds = super().seed_urls(row)
        sid = str(row.get("solicitation_id") or row.get("solicitation_number") or row.get("notice_id") or "")
        # Only reuse observed abstract pattern when original URL already established that shape
        for u in list(seeds):
            if "bidnetdirect.com" in u and "/solicitations/" in u and sid and sid in u:
                # Print sibling only if path already contains public/supplier structure (observed family)
                if "/abstract" in u:
                    print_guess = u.replace("/abstract", "/print")
                    # Do NOT add invented print URL unless pattern was previously verified —
                    # record as candidate only via observed patterns or page-linked hrefs.
                    _ = print_guess  # intentional: no blind add
        seeds.extend(self.instantiate_observed_patterns(sid))
        return list(dict.fromkeys(seeds))


class OpenGovPublicArtifactAdapter(PublicArtifactRecoveryAdapter):
    platform = "OpenGov"
    hosts = ("opengov.com", "procurement.opengov.com")


class BonfirePublicArtifactAdapter(PublicArtifactRecoveryAdapter):
    platform = "Bonfire"
    hosts = ("bonfirehub.com",)


class IonWavePublicArtifactAdapter(PublicArtifactRecoveryAdapter):
    platform = "IonWave"
    hosts = ("ionwave.net",)


class PlanetBidsPublicArtifactAdapter(PublicArtifactRecoveryAdapter):
    platform = "PlanetBids"
    hosts = ("planetbids.com",)


class DemandStarPublicArtifactAdapter(PublicArtifactRecoveryAdapter):
    platform = "DemandStar"
    hosts = ("demandstar.com",)


class PublicPurchasePublicArtifactAdapter(PublicArtifactRecoveryAdapter):
    platform = "Public Purchase"
    hosts = ("publicpurchase.com",)


class JaggaerPublicArtifactAdapter(PublicArtifactRecoveryAdapter):
    platform = "Jaggaer/SciQuest"
    hosts = ("jaggaer.com", "sciquest.com")


ADAPTERS: dict[str, PublicArtifactRecoveryAdapter] = {
    "BidNet": BidNetPublicArtifactAdapter(),
    "OpenGov": OpenGovPublicArtifactAdapter(),
    "Bonfire": BonfirePublicArtifactAdapter(),
    "IonWave": IonWavePublicArtifactAdapter(),
    "PlanetBids": PlanetBidsPublicArtifactAdapter(),
    "DemandStar": DemandStarPublicArtifactAdapter(),
    "Public Purchase": PublicPurchasePublicArtifactAdapter(),
    "Jaggaer/SciQuest": JaggaerPublicArtifactAdapter(),
}


def get_adapter(platform: str) -> PublicArtifactRecoveryAdapter:
    return ADAPTERS.get(platform) or PublicArtifactRecoveryAdapter()


def _extract_award_fields(text: str) -> dict[str, Any]:
    vendor = None
    m = re.search(
        r"(?:awarded\s+to|award\s+vendor|successful\s+bidder|contractor)\s*[:\-]?\s*([A-Z][A-Za-z0-9 &.,'\-]{2,60})",
        text,
        re.I,
    )
    if m:
        vendor = m.group(1).strip()
    dollars = re.findall(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)", text[:120000])
    unit = float(dollars[0].replace(",", "")) if dollars else None
    date = None
    dm = re.search(r"(?:award\s+date|awarded\s+on|date\s+of\s+award)\s*[:\-]?\s*([0-9]{1,2}[/\-][0-9]{1,2}[/\-][0-9]{2,4})", text, re.I)
    if dm:
        date = dm.group(1)
    qty = None
    qm = re.search(r"(?:quantity|qty)\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if qm:
        qty = float(qm.group(1))
    return {"vendor": vendor, "unit_price": unit, "award_date": date, "quantity": qty, "dollars_found": len(dollars)}


def _parse_bid_tab_lines(text: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for m in re.finditer(
        r"(?P<vendor>[A-Z][A-Za-z0-9 &.,'\-]{2,40})\s+\$?\s*(?P<price>[0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)",
        text[:80000],
    ):
        lines.append(
            {
                "vendor": m.group("vendor").strip(),
                "unit_price": float(m.group("price").replace(",", "")),
                "bidder": m.group("vendor").strip(),
            }
        )
        if len(lines) >= 12:
            break
    return lines


def run_public_artifact_recovery(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    authorize_live: bool = False,
    max_queries: int = DEFAULT_QUERY_BUDGET,
    max_fetches: int = DEFAULT_FETCH_BUDGET,
    platform_blocked: bool = True,
) -> dict[str, Any]:
    """
    BLOCKED PLATFORM FRONTEND → public indexed artifacts → exact evidence.
    Never solves CAPTCHA, spoofs sessions, or brute-forces document IDs.
    """
    commercial = dict(commercial or {})
    platform = detect_platform(row)
    if "bidnet" in str(row.get("original_solicitation_url") or "").lower():
        platform = "BidNet"
    adapter = get_adapter(platform)
    buyer = str(row.get("agency") or row.get("buyer") or "")
    sid = str(row.get("solicitation_id") or row.get("solicitation_number") or row.get("notice_id") or "")
    title = str(row.get("title") or row.get("product") or "")
    model = str(commercial.get("model") or "")

    attempts: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    awards: list[dict[str, Any]] = []
    competition = None
    vendor_intel = None
    best_grade = GOV_VALUE_D
    best_gov = None
    best_rule = None
    recovery_source = None
    access_mode = PUBLIC_ANTI_BOT_BLOCKED if platform_blocked else None

    attempts.append(
        {
            "step": "trigger",
            "branch": PUBLIC_ARTIFACT_RECOVERY,
            "platform": platform,
            "platform_history_blocked": platform_blocked,
            "forbidden_actions": list(FORBIDDEN_ACTIONS),
            "no_id_brute_force": True,
            "no_captcha_bypass": True,
        }
    )

    # 1–5: BidNet / platform recovery order — seeds first, then bounded search
    queries = build_artifact_queries(
        platform=platform, solicitation=sid, buyer=buyer or None, title=title or None, model=model or None
    )[:max_queries]
    attempts.append({"step": "exact_solicitation_queries", "n": len(queries), "kinds": [q["kind"] for q in queries]})

    seed_first = adapter.prioritize_urls(list(adapter.seed_urls(row)) + adapter.instantiate_observed_patterns(sid))
    candidate_urls = list(seed_first)
    disc: dict[str, Any] = {"urls": [], "attempts": [], "providers": [], "query_count": 0}

    # Offline: use cache only. Live: fetch seeds first; search only if still Gov D after seeds.
    if not authorize_live:
        disc = discover_artifact_urls(
            platform=platform,
            solicitation=sid,
            queries=queries,
            buyer=buyer or None,
            title=title or None,
            model=model or None,
            authorize_live=False,
            use_cache=True,
        )
        attempts.append(
            {"step": "search_discovery", "url_count": len(disc.get("urls") or []), "providers": disc.get("providers")}
        )
        candidate_urls = adapter.prioritize_urls(seed_first + list(disc.get("urls") or []))
    else:
        attempts.append({"step": "search_deferred_until_after_seeds", "seeds": len(seed_first)})

    # 6: buyer public-record pivot seeds
    buyer_disc = discover_buyer_path_urls(buyer, solicitation=sid or None, model=model or None) if buyer else {"urls": []}
    attempts.append({"step": "buyer_public_record_pivot", "urls": (buyer_disc.get("urls") or [])[:6]})
    for bu in (buyer_disc.get("urls") or [])[:4]:
        if bu not in candidate_urls:
            candidate_urls.append(bu)

    fetches_used = 0
    metadata_only = False
    if authorize_live and candidate_urls:
        try:
            from phase_l.resilient_fetch import (
                FETCH_BOT_BLOCKED,
                FETCH_JS_EMPTY,
                FETCH_OK,
                FETCH_403,
                DomainCircuitBreaker,
                resilient_fetch,
            )

            breaker = DomainCircuitBreaker()
            for url in candidate_urls[: max(max_fetches * 2, max_fetches)]:
                if fetches_used >= max_fetches:
                    break
                if reject_wrong_solicitation(url, sid):
                    attempts.append({"step": "reject_wrong_solicitation", "url": url})
                    continue
                if not breaker.allow(url):
                    continue
                fr = resilient_fetch(url, breaker=breaker, retries=0, read_timeout=10.0)
                fetches_used += 1
                attempts.append({"step": "fetch_artifact", "url": url, "status": fr.status, "http": fr.status_code})

                if fr.status in {FETCH_BOT_BLOCKED, FETCH_403} or fr.status_code in {401, 403, 202}:
                    access_info = classify_access_mode(
                        fr.text[:500] if fr.text else "",
                        status=fr.status,
                        http_status=fr.status_code,
                        platform=platform,
                    )
                    access_mode = access_info["access_mode"]
                    continue
                if fr.status == FETCH_JS_EMPTY:
                    access_mode = access_mode or PUBLIC_ANTI_BOT_BLOCKED
                    continue
                if fr.status != FETCH_OK or not fr.text or len(fr.text) < 80:
                    continue

                text = fr.text
                link = exact_artifact_link(
                    url=url, solicitation=sid, buyer=buyer, title=title, model=model or None, page_text=text[:50000]
                )
                if not link["linked"]:
                    attempts.append({"step": "link_rejected", "url": url, "reason": link["rejected_reason"]})
                    continue

                atype = classify_artifact_type(url, title)
                host = urlparse(url).netloc.lower()
                # Enumerate public attachment links from page (observed only)
                page_links = extract_public_links(text, base_host=host)
                for pl in page_links:
                    if any(x in pl.lower() for x in ("attachment", "print", "award", "pdf", "bid")):
                        if pl not in candidate_urls and sid in pl:
                            candidate_urls.append(pl)

                rec = {
                    "platform": platform,
                    "buyer": buyer,
                    "solicitation_number": sid,
                    "artifact_url": url,
                    "artifact_type": atype,
                    "public_access_state": "PUBLIC",
                    "discovered_via": "search_or_seed",
                    "evidence_types_available": [],
                    "parsed_successfully": False,
                    "exact_match_basis": link["exact_match_basis"],
                    "source_health": "FETCH_OK",
                }
                record_observed_url_pattern(
                    platform,
                    url=url,
                    artifact_type=atype,
                    solicitation=sid,
                    discovery_method="public_fetch",
                    access_status="PUBLIC",
                )

                extracted = _extract_award_fields(text)
                bid_lines = _parse_bid_tab_lines(text) if atype in {BID_TAB, TABULATION, AWARD_PRINT_VIEW, AWARD_PDF} else []
                if bid_lines:
                    matched = match_bid_tab_line(bid_lines, commercial=commercial, title=title)
                    competition = extract_competition_from_bid_tab(bid_lines)
                    if matched:
                        extracted["unit_price"] = matched.get("unit_price") or extracted.get("unit_price")
                        extracted["vendor"] = matched.get("vendor") or extracted.get("vendor")
                        rec["evidence_types_available"].append("bid_tab_line")

                is_buyer_host = buyer and buyer.lower().split()[0] in host and "bidnet" not in host
                if extracted.get("unit_price") or extracted.get("vendor"):
                    aw = normalize_award_tabulation(
                        {
                            "buyer": buyer,
                            "vendor": extracted.get("vendor"),
                            "unit_price": extracted.get("unit_price"),
                            "quantity": extracted.get("quantity") or 1,
                            "award_date": extracted.get("award_date"),
                            "model": commercial.get("model"),
                            "manufacturer": commercial.get("manufacturer"),
                            "source": f"public_artifact:{atype}",
                            "item": title,
                            "solicitation_id": sid,
                            "line_matched": bool(bid_lines),
                        }
                    )
                    graded = grade_recovered_award(aw, row=row, commercial=commercial)
                    rec["parsed_successfully"] = bool(graded.get("gov"))
                    rec["evidence_types_available"].append("award_fields")
                    if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                        awards.append(aw)
                        best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")
                        recovery_source = "buyer_artifact" if is_buyer_host else "public_artifact"
                        if extracted.get("vendor"):
                            vendor_intel = {
                                "vendor": extracted["vendor"],
                                "role": classify_prior_awardee_role({"name": extracted["vendor"]}),
                            }
                        record_successful_recovery(buyer, evidence_type=atype, source_url=url)
                        link_product_history(
                            product_key=str(commercial.get("model") or title)[:80],
                            manufacturer=commercial.get("manufacturer"),
                            mpn_model=str(commercial.get("model") or ""),
                            buyer=buyer,
                            solicitation=sid,
                            award=aw,
                            vendor=extracted.get("vendor"),
                            price=extracted.get("unit_price"),
                            date=extracted.get("award_date"),
                        )
                    elif extracted.get("vendor") or atype in {
                        SOLICITATION_PRINT_VIEW,
                        PUBLIC_ATTACHMENT,
                        AWARD_PRINT_VIEW,
                    }:
                        metadata_only = True
                        rec["evidence_types_available"].append("metadata")
                else:
                    # Recovered public page but no price — metadata / attachment inventory
                    metadata_only = True
                    rec["evidence_types_available"].append("public_page")
                    if atype in {SOLICITATION_PRINT_VIEW, PUBLIC_ATTACHMENT, BOARD_DOCUMENT}:
                        rec["parsed_successfully"] = True

                upsert_artifact(rec)
                artifacts.append(rec)

                # Prefer stopping when strong exact evidence recovered
                if best_grade in {GOV_VALUE_A, GOV_VALUE_B}:
                    break
        except Exception as exc:
            attempts.append({"step": "live_fetch_error", "error": str(exc)[:160]})

        # If seeds didn't yield A/B/C, run bounded public search once
        if best_grade == GOV_VALUE_D and queries and max_queries > 0:
            disc = discover_artifact_urls(
                platform=platform,
                solicitation=sid,
                queries=queries[: min(3, max_queries)],
                buyer=buyer or None,
                title=title or None,
                model=model or None,
                authorize_live=True,
                use_cache=True,
            )
            attempts.append(
                {
                    "step": "search_discovery_after_seeds",
                    "url_count": len(disc.get("urls") or []),
                    "providers": disc.get("providers"),
                }
            )
            for u in disc.get("urls") or []:
                if u not in candidate_urls:
                    candidate_urls.append(u)
            # One extra fetch wave for newly discovered URLs
            extra = [u for u in candidate_urls if u not in seed_first][:max(0, max_fetches - fetches_used)]
            for url in extra:
                if fetches_used >= max_fetches:
                    break
                if reject_wrong_solicitation(url, sid):
                    continue
                if not breaker.allow(url):
                    continue
                fr = resilient_fetch(url, breaker=breaker, retries=0, read_timeout=8.0)
                fetches_used += 1
                attempts.append({"step": "fetch_search_hit", "url": url, "status": fr.status})
                if fr.status != FETCH_OK or not fr.text:
                    if fr.status in {FETCH_BOT_BLOCKED, FETCH_403}:
                        access_mode = classify_access_mode(
                            fr.text[:400] if fr.text else "",
                            status=fr.status,
                            http_status=fr.status_code,
                            platform=platform,
                        )["access_mode"]
                    continue
                link = exact_artifact_link(
                    url=url, solicitation=sid, buyer=buyer, title=title, model=model or None, page_text=fr.text[:40000]
                )
                if not link["linked"]:
                    continue
                atype = classify_artifact_type(url, title)
                rec = {
                    "platform": platform,
                    "buyer": buyer,
                    "solicitation_number": sid,
                    "artifact_url": url,
                    "artifact_type": atype,
                    "public_access_state": "PUBLIC",
                    "discovered_via": "search",
                    "evidence_types_available": ["public_page"],
                    "parsed_successfully": False,
                    "exact_match_basis": link["exact_match_basis"],
                    "source_health": "FETCH_OK",
                }
                metadata_only = True
                upsert_artifact(rec)
                artifacts.append(rec)
                record_observed_url_pattern(
                    platform,
                    url=url,
                    artifact_type=atype,
                    solicitation=sid,
                    discovery_method="search",
                    access_status="PUBLIC",
                )

    # Offline / unfetched path: seed URL + search cache counts as metadata if linked by ID
    if not artifacts and candidate_urls:
        for url in candidate_urls[:5]:
            if reject_wrong_solicitation(url, sid):
                continue
            link = exact_artifact_link(url=url, solicitation=sid, buyer=buyer, title=title, model=model or None)
            if link["linked"]:
                atype = classify_artifact_type(url, title)
                rec = {
                    "platform": platform,
                    "buyer": buyer,
                    "solicitation_number": sid,
                    "artifact_url": url,
                    "artifact_type": atype,
                    "public_access_state": "INDEXED_OR_SEEDED",
                    "discovered_via": "seed_or_index",
                    "evidence_types_available": ["url_metadata"],
                    "parsed_successfully": False,
                    "exact_match_basis": link["exact_match_basis"],
                    "source_health": "NOT_FETCHED" if not authorize_live else "NO_CONTENT",
                }
                upsert_artifact(rec)
                artifacts.append(rec)
                metadata_only = True
                record_observed_url_pattern(
                    platform,
                    url=url,
                    artifact_type=atype,
                    solicitation=sid,
                    discovery_method="seed_or_index",
                    access_status="INDEXED_OR_SEEDED",
                )

    # Outcome classification
    if awards and best_grade in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
        if recovery_source == "buyer_artifact":
            outcome = BUYER_ARTIFACT_RECOVERED
        else:
            outcome = PUBLIC_ARTIFACT_RECOVERED
    elif artifacts and metadata_only and not awards:
        outcome = PUBLIC_METADATA_ONLY
    elif not artifacts and platform == "BidNet":
        outcome = FREE_REGISTRATION_STILL_REQUIRED
        access_mode = FREE_REGISTRATION_REQUIRED
    elif not artifacts and access_mode in {PUBLIC_ANTI_BOT_BLOCKED, "VENDOR_ACCOUNT_REQUIRED"}:
        outcome = AUTHENTICATED_HISTORY_REQUIRED
    elif not artifacts:
        outcome = NO_PUBLIC_ARTIFACT_FOUND
    else:
        outcome = EVIDENCE_EXHAUSTED

    # Walk remaining order markers for audit
    for step in BIDNET_RECOVERY_ORDER:
        if not any(a.get("step") == step or step in str(a.get("step")) for a in attempts):
            attempts.append({"step": step, "noted": True})

    reg = None
    if outcome in {
        FREE_REGISTRATION_STILL_REQUIRED,
        AUTHENTICATED_HISTORY_REQUIRED,
        NO_PUBLIC_ARTIFACT_FOUND,
        EVIDENCE_EXHAUSTED,
        PUBLIC_METADATA_ONLY,
    }:
        if platform == "BidNet" or outcome in {FREE_REGISTRATION_STILL_REQUIRED, PUBLIC_METADATA_ONLY}:
            reg = registration_opportunity(
                platform=platform or "BidNet",
                buyer=buyer,
                access_mode=FREE_REGISTRATION_REQUIRED,
                registration_url="https://www.bidnetdirect.com/",
                blocked_opportunities=1,
            )
            reg["priority_score"] = history_access_registration_priority(reg)
            if outcome == PUBLIC_METADATA_ONLY:
                reg["note"] = "public_url_or_metadata_found_but_exact_award_still_gated"

    return {
        "kind": "PublicArtifactRecoveryResult",
        "build": BUILD,
        "branch": PUBLIC_ARTIFACT_RECOVERY,
        "platform": platform,
        "platform_state": PLATFORM_HISTORY_BLOCKED if platform_blocked else None,
        "outcome": outcome,
        "access_mode": access_mode,
        "grade_after": best_grade,
        "rule_id": best_rule,
        "gov": best_gov,
        "recovery_source": recovery_source,
        "artifacts": artifacts[:10],
        "artifacts_found": len(artifacts),
        "awards_found": len(awards),
        "awards": awards[:5],
        "competition": competition,
        "vendor_intel": vendor_intel,
        "registration_opportunity": reg,
        "attempts": attempts,
        "queries": queries,
        "candidate_url_count": len(candidate_urls),
        "fetches_used": fetches_used,
        "create_account": False,
        "auto_register": False,
        "captcha_bypass": False,
        "id_brute_force": False,
        "outreach": False,
        "timestamp": _utc(),
    }
