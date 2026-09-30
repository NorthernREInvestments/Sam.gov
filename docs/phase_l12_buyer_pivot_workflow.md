# Phase L.12 — Buyer Pivot Workflow

When procurement-platform history is blocked:

1. Classify access mode (`PLATFORM_HISTORY_BLOCKED`).
2. Extract buyer legal name, site seeds, solicitation #, model, award window.
3. **Do not stop** for BidNet anti-bot — use BidNet only for metadata, then pivot.
4. Load `BuyerHistoryPath` memory first.
5. Walk public evidence order:

   buyer procurement → historical solicitations → award/results → bid tabs →
   board/council → PO/check register → open data → contract register →
   state archive → cooperative history → exact solicitation search →
   exact product/buyer history → registration classification → manual-auth classification.

6. Exact solicitation variants **before** product-family search.
7. Line-item match on bid tabs; lot totals stay `LOT_VALUE` (no unit invention).
8. On success: persist path memory, prior awardee, recompute economics immediately.
9. On remaining block: emit `HISTORY_ACCESS_GAP_QUEUE` / registration priority — **never auto-register**.
