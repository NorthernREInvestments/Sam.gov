# Proposed metered allocation — M3 master build (awaiting owner approval)

Status: **NOT APPROVED. Metered acquisition remains locked.** NextBid emergency stop engaged,
`NEXTBID_ENABLED=false`, `NEXTBID_EMERGENCY_STOP=true`, no `owner_authorization.json`,
`approved_allocation_calls` = 0. SAM `sam_live_enabled=false`, allocation 0.

## Current account position (local ledger, last provider sync 2026-11-23 04:09 UTC)

| Item | Value |
|---|---|
| Provider allowance | 6,000 |
| Provider used | 3,453 |
| Provider remaining | 2,547 |
| Monthly reserve (untouchable) | 1,000 |
| Spendable above reserve | 1,547 |
| Stale reservation from interrupted call (2026-11-22) | 1 |

The remaining balance must be re-verified via the free `/v1/usage` endpoint immediately before
any job; the job aborts if provider remaining differs from the ledger.

## Proposal: NextBid, 100 calls total (two jobs of at most 50), no SAM calls

| Job | Max calls | Endpoints | Purpose |
|---|---|---|---|
| NB-DOC-1 | 50 | `get_opportunity`, `list_documents`, `list_files`, `read_file` | Unlock solicitation packages for the highest-value targets that free direct retrieval could not obtain |
| NB-DOC-2 | 50 | same | Second wave only if NB-DOC-1 yields at least 3 records that become quote-ready or supplier-research-ready |

- `search` (discovery) is **excluded**: the owner directive forbids spending on repeated discovery.
- Expected yield: about 3–4 calls per package, so 12–16 packages per job and at most 25–30 in total.
- Account remaining after the full allocation: about 2,447 (reserve floor intact).
- Daily ceiling 100 means both jobs can run on one calendar day only if no other metered use occurs.

## Target selection (computed offline from the staged inventory)

Candidate pool: 260 NextBid records that are EXPLICIT_PRODUCT, non-federal, have a deadline at
least 7 days out, no document evidence, and no service/construction/installation heading
(121 due in 8–14 days, 76 in 15–30 days, 63 later).

Each call is spent only when all hold:
1. Free direct retrieval from the official URL was attempted first and failed or yielded no package.
2. Deadline still ≥7 days at job time; status not awarded/cancelled; not a duplicate.
3. Product identity or quantities are missing and a package is expected to supply them.
4. Eligibility not already blocked (bonding, licensing, local-only, registration requiring owner).
5. Ranked by: multi-line/quantity signals, deadline window 8–30 days, estimated value tier.

Never spent on: expired/historical, services, construction, blocked, duplicates, unchanged
documents (durable-store hit short-circuits), discovery, or tests.

## Controls verified before proposal (mocked, zero real calls)

- Emergency stop locks regardless of authorization scope (prior scoped bypass removed).
- `approved_allocation_calls` absent or 0 blocks the first request.
- 51st call in a job, 101st call in a day (across new jobs), and reserve breach all blocked.
- Provider credit units are counted against the daily ceiling independently of request count.
- Counters persist on disk (day, month key, allocation); no reset on restart or new job.
- Cross-process file lock plus in-process lock around every budget read-modify-write; 160
  concurrent workers produce exactly 100 successful reservations.
- SAM guard: 10/day hard ceiling, zero allocation blocks the first call, disabled by config.
- Recurring schedule remains disabled; its per-run plan is 10 discovery + 40 detail (≤50/job).

## To approve

The owner records approval by writing an authorization with an explicit
`approved_allocation_calls` (e.g. 50 or 100), clearing the emergency stop for the job window
only, and re-engaging it afterwards. Approval of this bounded job does not activate any
recurring schedule.
