"""Domain yield ranking + circuit-breaker suppression."""

from __future__ import annotations

import json
import time
from typing import Any

from m3_data_root import data_path
from seller_rediscovery.models import (
    BOT_WALL_STREAK_LIMIT,
    BUILD,
    DOMAIN_YIELD,
    LOW_YIELD_BLOCKED,
    SEED_TIER_A,
    SEED_TIER_D,
    TIER_A,
    TIER_B,
    TIER_C,
    TIER_D,
    ZERO_YIELD_ATTEMPT_LIMIT,
)


def _norm(domain: str) -> str:
    return (domain or "").lower().replace("www.", "").strip()


def _empty_row(domain: str) -> dict[str, Any]:
    return {
        "domain": domain,
        "exact_urls_attempted": 0,
        "successful_prices": 0,
        "validated_prices": 0,
        "bot_wall": 0,
        "call_for_price": 0,
        "http_requests": 0,
        "browser_renders": 0,
        "consecutive_bot_walls": 0,
        "by_route": {},
        "status": "ACTIVE",
        "tier": TIER_D,
        "success_rate": 0.0,
        "dominant_route": None,
        "avg_http_per_attempt": 0.0,
        "suppressed_reason": None,
    }


def load_yield() -> dict[str, Any]:
    p = data_path(DOMAIN_YIELD)
    if not p.exists():
        payload = {"build": BUILD, "domains": {}, "updated_at": None}
        for d in SEED_TIER_A:
            row = _empty_row(d)
            row["tier"] = TIER_A
            row["seed_signal"] = "prior_strong"
            payload["domains"][d] = row
        for d in SEED_TIER_D:
            row = _empty_row(d)
            row["tier"] = TIER_D
            row["seed_signal"] = "prior_poor"
            payload["domains"][d] = row
        return payload
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"build": BUILD, "domains": {}, "updated_at": None}


def save_yield(payload: dict[str, Any]) -> None:
    p = data_path(DOMAIN_YIELD)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload["build"] = BUILD
    payload["updated_at"] = time.time()
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _recompute_tier(row: dict[str, Any]) -> None:
    att = max(int(row.get("exact_urls_attempted") or 0), 0)
    val = int(row.get("validated_prices") or 0)
    rate = (val / att) if att else 0.0
    row["success_rate"] = round(rate, 4)
    if row.get("status") == LOW_YIELD_BLOCKED:
        row["tier"] = TIER_D
        return
    if att == 0:
        # keep seed signal if present
        if row.get("seed_signal") == "prior_strong":
            row["tier"] = TIER_A
        elif row.get("seed_signal") == "prior_poor":
            row["tier"] = TIER_D
        else:
            row["tier"] = TIER_C
        return
    if rate >= 0.60 and att >= 2:
        row["tier"] = TIER_A
    elif rate >= 0.25 and att >= 2:
        row["tier"] = TIER_B
    elif rate > 0:
        row["tier"] = TIER_C
    elif att >= ZERO_YIELD_ATTEMPT_LIMIT:
        # Confirmed zero-yield after enough attempts
        row["tier"] = TIER_D
    else:
        # Low sample / unknown — keep as C so rediscovery can still try
        row["tier"] = TIER_C
    # dominant route
    by = row.get("by_route") or {}
    if by:
        best = max(by.items(), key=lambda kv: int((kv[1] or {}).get("validated") or 0))
        row["dominant_route"] = best[0] if int((best[1] or {}).get("validated") or 0) else None
    http = int(row.get("http_requests") or 0)
    row["avg_http_per_attempt"] = round(http / max(att, 1), 3)


def note_attempt(
    domain: str,
    *,
    validated: bool,
    route: str | None = None,
    bot_wall: bool = False,
    call_for_price: bool = False,
    http_requests: int = 1,
    browser_renders: int = 0,
    price_found: bool = False,
) -> dict[str, Any]:
    payload = load_yield()
    d = _norm(domain)
    if not d:
        return payload
    row = payload.setdefault("domains", {}).setdefault(d, _empty_row(d))
    row["exact_urls_attempted"] = int(row.get("exact_urls_attempted") or 0) + 1
    row["http_requests"] = int(row.get("http_requests") or 0) + int(http_requests or 0)
    row["browser_renders"] = int(row.get("browser_renders") or 0) + int(browser_renders or 0)
    if price_found:
        row["successful_prices"] = int(row.get("successful_prices") or 0) + 1
    if validated:
        row["validated_prices"] = int(row.get("validated_prices") or 0) + 1
        row["consecutive_bot_walls"] = 0
    if bot_wall:
        row["bot_wall"] = int(row.get("bot_wall") or 0) + 1
        row["consecutive_bot_walls"] = int(row.get("consecutive_bot_walls") or 0) + 1
    else:
        if not validated:
            # non-bot miss resets streak only when we got a real page
            pass
        else:
            row["consecutive_bot_walls"] = 0
    if call_for_price:
        row["call_for_price"] = int(row.get("call_for_price") or 0) + 1
    if route:
        br = row.setdefault("by_route", {}).setdefault(route, {"attempts": 0, "validated": 0})
        br["attempts"] = int(br.get("attempts") or 0) + 1
        if validated:
            br["validated"] = int(br.get("validated") or 0) + 1

    # Circuit breaker
    if row.get("status") != LOW_YIELD_BLOCKED:
        streak = int(row.get("consecutive_bot_walls") or 0)
        att = int(row.get("exact_urls_attempted") or 0)
        val = int(row.get("validated_prices") or 0)
        if streak >= BOT_WALL_STREAK_LIMIT:
            row["status"] = LOW_YIELD_BLOCKED
            row["suppressed_reason"] = f"{streak}_consecutive_bot_walls"
        elif att >= ZERO_YIELD_ATTEMPT_LIMIT and val == 0:
            row["status"] = LOW_YIELD_BLOCKED
            row["suppressed_reason"] = f"{att}_attempts_0_validated"

    _recompute_tier(row)
    save_yield(payload)
    return row


def is_suppressed(domain: str) -> bool:
    payload = load_yield()
    row = (payload.get("domains") or {}).get(_norm(domain)) or {}
    return row.get("status") == LOW_YIELD_BLOCKED


def domain_tier(domain: str) -> str:
    payload = load_yield()
    row = (payload.get("domains") or {}).get(_norm(domain)) or {}
    if not row:
        if _norm(domain) in SEED_TIER_A:
            return TIER_A
        if _norm(domain) in SEED_TIER_D:
            return TIER_D
        return TIER_C
    return str(row.get("tier") or TIER_C)


def tier_rank(domain: str) -> int:
    """Lower is better."""
    t = domain_tier(domain)
    return {TIER_A: 0, TIER_B: 1, TIER_C: 2, TIER_D: 3}.get(t, 2)


def should_attempt(domain: str) -> bool:
    if is_suppressed(domain):
        return False
    # Soft-skip seed Tier D unless rediscovery specifically found verified URL
    # (caller may override via force)
    return True


def rank_urls(urls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort candidate URL rows by domain tier then identity confidence."""
    from seller_rediscovery.models import PREFERRED_OPEN_DISTRIBUTORS, SEED_TIER_A

    preferred = set(SEED_TIER_A) | set(PREFERRED_OPEN_DISTRIBUTORS)

    def key(row: dict[str, Any]) -> tuple:
        d = _norm(str(row.get("domain") or row.get("seller") or ""))
        conf = str(row.get("identity_confidence") or "")
        conf_rank = 0 if conf == "EXACT_PRODUCT_VERIFIED" else (1 if "UNVERIFIED" in conf else 2)
        pref_rank = 0 if d in preferred or any(d.endswith(p) for p in preferred) else 1
        return (tier_rank(d), pref_rank, conf_rank, d)

    return sorted(urls, key=key)


def yield_snapshot(limit: int = 40) -> list[dict[str, Any]]:
    payload = load_yield()
    rows = []
    for domain, row in (payload.get("domains") or {}).items():
        att = max(int(row.get("exact_urls_attempted") or 0), 1)
        rows.append(
            {
                "domain": domain,
                "attempts": row.get("exact_urls_attempted", 0),
                "valid_recoveries": row.get("validated_prices", 0),
                "success_pct": round(100.0 * int(row.get("validated_prices") or 0) / att, 1),
                "blocked_pct": round(100.0 * int(row.get("bot_wall") or 0) / att, 1),
                "avg_http_per_attempt": row.get("avg_http_per_attempt", 0),
                "tier": row.get("tier"),
                "status": row.get("status"),
                "dominant_route": row.get("dominant_route"),
                "suppressed_reason": row.get("suppressed_reason"),
            }
        )
    rows.sort(key=lambda r: (-r["valid_recoveries"], -r["success_pct"], r["domain"]))
    return rows[:limit]


def suppressed_domains() -> list[dict[str, Any]]:
    payload = load_yield()
    out = []
    for domain, row in (payload.get("domains") or {}).items():
        if row.get("status") == LOW_YIELD_BLOCKED:
            out.append(
                {
                    "domain": domain,
                    "reason": row.get("suppressed_reason"),
                    "attempts": row.get("exact_urls_attempted"),
                    "validated": row.get("validated_prices"),
                    "bot_wall": row.get("bot_wall"),
                }
            )
    return out
