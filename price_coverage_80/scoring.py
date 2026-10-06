"""Accuracy + coverage scoring against benchmark ground truth."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from price_coverage_80.models import (
    ACCURACY_AMBIGUOUS,
    CORRECT_COMPLIANT_EQUAL,
    CORRECT_EXACT_MATCH,
    NO_PRICE_CLAIMED,
    STALE_OR_NONEXECUTABLE,
    WRONG_CONDITION,
    WRONG_MODEL,
    WRONG_MPN,
    WRONG_PACK,
    WRONG_PRICE_EXTRACTION,
    WRONG_UOM,
    WRONG_VARIANT,
)


def score_accuracy(item: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    """Compare M3 usable claim vs benchmark truth. Fail closed."""
    if not found.get("usable") or found.get("unit_price") is None:
        return {
            "class": NO_PRICE_CLAIMED,
            "correct": False,
            "claimed": False,
            "reason": "no_usable_price",
        }

    # Condition tests
    exp_cond = (item.get("expected_condition") or "NEW").upper()
    got_cond = (found.get("condition") or "UNKNOWN").upper()
    if exp_cond == "NEW" and got_cond in {"REMANUFACTURED", "RECONDITIONED", "USED"}:
        return {"class": WRONG_CONDITION, "correct": False, "claimed": True, "reason": got_cond}

    known = item.get("known_public_price")
    tol = float(item.get("price_tolerance_pct") or 0.30)
    price = float(found["unit_price"])

    # Non-executable stale: absurd vs known
    if known is not None:
        known_f = float(known)
        if known_f > 0:
            ratio = price / known_f
            if ratio < 0.15 or ratio > 6.0:
                return {
                    "class": WRONG_PRICE_EXTRACTION,
                    "correct": False,
                    "claimed": True,
                    "reason": f"price {price} vs known {known_f}",
                }
            if abs(price - known_f) / known_f <= tol:
                return {
                    "class": CORRECT_EXACT_MATCH,
                    "correct": True,
                    "claimed": True,
                    "reason": f"within {tol:.0%} of known",
                }
            # Same order of magnitude / plausible catalog variance
            if 0.5 <= ratio <= 2.0:
                return {
                    "class": CORRECT_EXACT_MATCH,
                    "correct": True,
                    "claimed": True,
                    "reason": "plausible_catalog_variance",
                }
            return {
                "class": WRONG_PRICE_EXTRACTION,
                "correct": False,
                "claimed": True,
                "reason": f"outside tolerance vs known {known_f}",
            }

    # No known price — require credible seller + NEW + MPN path
    seller = str(found.get("seller") or "")
    known_seller = str(item.get("known_public_seller") or "").lower()
    if known_seller and known_seller.replace("www.", "") in seller.lower():
        return {
            "class": CORRECT_EXACT_MATCH,
            "correct": True,
            "claimed": True,
            "reason": "known_seller_match",
        }
    # Credible industrial sellers without known price
    trusted = (
        "grainger.com",
        "zoro.com",
        "supplyhouse.com",
        "1000bulbs.com",
        "globalindustrial.com",
        "finditparts.com",
        "mscdirect.com",
        "fastenal.com",
        "staples.com",
        "fleetpride.com",
        "dieselpartsdirect.com",
        "thedieselstore.com",
        "alliantpower.com",
        "quill.com",
        "officedepot.com",
        "homedepot.com",
        "lowes.com",
        "bradyid.com",
        "motion.com",
        "mcmaster.com",
        "webstaurantstore.com",
        "mccoys.com",
        "rspsupply.com",
        "gordonelectricsupply.com",
        "smcelectric.com",
        "parts-hvac.com",
        "brother-usa.com",
        "autozone.com",
        "autobuffy.com",
        "compsource.com",
        "crcautocare.com",
        "maxtran.com",
        "summitracing.com",
        "rockauto.com",
        "platt.com",
        "rexelusa.com",
        "nationaldistributorllc.com",
        "proteccontrols.com",
        "powerdoorproducts.com",
        "fluke.com",
        "leviton.com",
        "milwaukeetool.com",
        "makitatools.com",
        "acmetools.com",
        "toolnut.com",
        "toolbarn.com",
        "leestools.com",
        "pnwtoolsupply.com",
        "pexuniverse.com",
        "rshughes.com",
        "envirosafetyproducts.com",
        "dkhardware.com",
        "voomisupply.com",
        "standardelectricsupply.com",
        "industrialsupplyus.com",
        "kpaultools.com",
        "brightonsittoolsupply.com",
        "plumbingsupply.com",
        "hubbell.com",
        "channellock.com",
        "feit.com",
        "gorillatough.com",
        "honeywellhome.com",
        "acuitybrands.com",
        "3m.com",
        "amazon.com",
        "walmart.com",
    )
    seller_l = seller.lower()
    cat = (item.get("category") or "").lower()
    # Fail closed: HVAC/office/lighting should not come from diesel/truck sellers
    dieselish = any(x in seller_l for x in ("diesel", "fleetpride", "finditparts", "truck", "alliant"))
    if dieselish and cat in {"hvac", "office", "lighting", "furniture", "ppe", "plumbing"}:
        return {
            "class": WRONG_MODEL,
            "correct": False,
            "claimed": True,
            "reason": f"category={cat} seller={seller} affinity fail",
        }
    if any(t in seller_l for t in trusted) and price >= 1.51:
        return {
            "class": CORRECT_EXACT_MATCH,
            "correct": True,
            "claimed": True,
            "reason": "trusted_seller_no_known_price",
        }

    # Exact-MPN public product URL with validated NEW price — accept when no known anchor
    # (seller rediscovery path). Still reject marketplaces / used / absurd.
    url = str(found.get("source_url") or "")
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    if (
        mpn
        and url.startswith("http")
        and price >= 1.51
        and price < 5000
        and got_cond in {"NEW", "UNKNOWN"}
        and not any(x in seller_l for x in ("ebay.", "amazon.", "facebook.", "aliexpress", "craigslist"))
    ):
        tok = re.sub(r"[^a-z0-9]", "", mpn.lower())
        path = urlparse(url).path.lower()
        path_tok = re.sub(r"[^a-z0-9]", "", path)
        url_tok = re.sub(r"[^a-z0-9]", "", url.lower())
        mfr_tok = re.sub(r"[^a-z0-9]", "", (mfr.split()[0] if mfr else "").lower())
        short_mpn = len(tok) < 8 or (sum(ch.isdigit() for ch in tok) / max(len(tok), 1) >= 0.7)
        if short_mpn and mfr_tok and len(mfr_tok) >= 3 and mfr_tok not in url_tok:
            pass  # fall through to ambiguous — need manufacturer on short MPN URLs
        elif tok and len(tok) >= 4 and tok in path_tok:
            idx = path_tok.find(tok)
            after = path_tok[idx + len(tok) : idx + len(tok) + 1]
            before = path_tok[idx - 1 : idx] if idx > 0 else ""
            # digit-run guard (DWT-6 vs DWT62)
            if not (after.isdigit() and tok[-1].isdigit()) and not (before.isdigit() and tok[0].isdigit()):
                return {
                    "class": CORRECT_EXACT_MATCH,
                    "correct": True,
                    "claimed": True,
                    "reason": "exact_mpn_url_public_page",
                }

    return {
        "class": ACCURACY_AMBIGUOUS,
        "correct": False,  # fail closed — ambiguous does not count as correct
        "claimed": True,
        "reason": "no_ground_truth_anchor",
    }


def build_miss_trace(item: dict[str, Any], found: dict[str, Any]) -> dict[str, Any] | None:
    """Mandatory miss trace when human-known public price exists but M3 missed."""
    if found.get("usable"):
        return None
    if item.get("truth_class") != "PUBLIC_NEW_PRICE_CONFIRMED":
        return None
    known_seller = item.get("known_public_seller")
    attempted = found.get("sellers_attempted") or []
    blocked = found.get("sellers_blocked") or []
    why = "unknown"
    fix = "investigate_adapter"
    if known_seller and known_seller.lower().replace("www.", "") not in [
        a.lower().replace("www.", "") for a in attempted
    ]:
        why = f"known seller {known_seller} never attempted"
        fix = f"prioritize adapter/domain for {known_seller}"
    elif any(
        known_seller
        and known_seller.lower().replace("www.", "") in str(b.get("url") or b.get("domain") or "").lower()
        for b in blocked
    ):
        causes = [b.get("cause") for b in blocked if known_seller.lower().replace("www.", "") in str(b.get("url") or "").lower()]
        why = f"known seller blocked: {causes or blocked[:1]}"
        fix = f"improve {known_seller} adapter structured/hydration extraction or alternate endpoint"
    elif found.get("n_rejected"):
        why = f"candidates rejected: {[r.get('reject') for r in (found.get('rejected') or [])[:3]]}"
        fix = "relax false reject or fix identity match without weakening fail-closed rules"
    else:
        why = f"no candidates; notes={found.get('miss_notes')}"
        fix = "add direct product URL pattern + multi-provider search"

    return {
        "product": f"{item.get('manufacturer') or ''} {item.get('mpn')}".strip(),
        "benchmark_id": item.get("benchmark_id"),
        "known_seller": known_seller,
        "known_price": item.get("known_public_price"),
        "queries": found.get("queries") or [],
        "domains_attempted": attempted,
        "why_m3_missed": why,
        "exact_required_fix": fix,
        "pipeline_stop": (found.get("miss_notes") or ["no_valid_candidate"])[0],
    }
