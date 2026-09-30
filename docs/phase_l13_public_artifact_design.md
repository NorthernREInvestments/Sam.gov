# Phase L.13 — Public Artifact Recovery Design

**Build:** `20260928-m3-phase-l13-public-artifact-recovery`

## Problem

Interactive procurement frontends (especially BidNet) are often anti-bot / CAPTCHA / JS-shell blocked. That is **not** proof that no public artifacts exist.

## Method

`BLOCKED PLATFORM FRONTEND`
→ exact solicitation identity
→ public search/index discovery
→ direct artifact URL
→ fetch & parse artifact
→ exact evidence / Gov upgrade
→ only then registration / exhausted

## Allowed vs forbidden

**Allowed:** public search, direct public URLs, print views, PDFs, attachments, buyer copies, indexed metadata.

**Forbidden:** CAPTCHA solve, session spoof, cookie theft, auth bypass, ID brute-force, hidden endpoint enumeration, proxy evasion, account creation.

## Canonical branch

`CanonicalOpportunityWorkflow.PUBLIC_ARTIFACT_RECOVERY` runs after platform/history block and **before** `REGISTRATION_REQUIRED` / final `EVIDENCE_EXHAUSTED`.

## Modules

- `public_artifact_types` — types, outcomes, safety constants
- `public_artifact_index` — `PublicArtifactIndex`, observed URL patterns, discovery cache
- `public_artifact_search` — exact queries + multi-provider discovery
- `public_artifact_recovery` — adapters (BidNet + peers) + recovery loop
- `l13_rescue` — full 82-gap corpus pass
