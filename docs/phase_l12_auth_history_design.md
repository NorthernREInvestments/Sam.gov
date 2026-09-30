# Phase L.12 — Auth-Walled History Recovery Design

**Build:** `20260928-m3-phase-l12-auth-walled-history-recovery`

## Problem

Platform award/history failure (BidNet anti-bot, auth-walled tabs) was treated like “no history.” That stopped recovery too early and left Gov D dominant.

## Hard rule

`PLATFORM_HISTORY_BLOCKED` ≠ `HISTORY_NOT_AVAILABLE`.

Platform block **must** trigger buyer-specific public-record recovery before declaring evidence unavailable.

## Access taxonomy (exact — never collapse to AUTH_REQUIRED)

| Mode | Meaning |
|------|---------|
| `PUBLIC_ANTI_BOT_BLOCKED` | Public URL blocked by bot challenge |
| `FREE_REGISTRATION_REQUIRED` | Free vendor/signup unlocks history |
| `VENDOR_ACCOUNT_REQUIRED` | Paid/network vendor account |
| `BUYER_SPECIFIC_ACCOUNT_REQUIRED` | Buyer portal login |
| `CAPTCHA_PRESENT` | Human challenge |
| `PRIVATE_RESTRICTED` | Not public by policy |
| `NO_PUBLIC_HISTORY_FEATURE` | Platform has no public awards |
| `UNKNOWN_ACCESS_MODE` | Auth-like signal, unclear mode |

## Architecture

1. `auth_access.classify_access_mode` — sole classifier
2. `buyer_history_paths` — `BuyerHistoryPath` memory + discovery seeds
3. `auth_history_recovery.run_auth_walled_history_recovery` — exhaustion order + registration intel
4. `l12_rescue.run_phase_l12_auth_history` — Stage 3 + Gov D corpus, no row cap
5. L.11 `exact_history_recovery` retained for memory/graph; `classify_auth_wall` maps to L.12

## Stop rules

No Phase M. No account creation. No login/CAPTCHA. No outreach/quotes/bids. Evidence grades unchanged.
