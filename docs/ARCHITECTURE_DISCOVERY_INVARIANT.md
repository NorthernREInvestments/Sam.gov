# Permanent Architectural Invariant — Discovery vs Classification

**Build:** 20261121-m3-2-build-002b  
**Status:** Permanent — supersedes any adapter-level product suitability gates.

## Processing sequence (mandatory)

1. **Unrestricted solicitation discovery** — retrieve every accessible solicitation record from authorized free sources (and budget-controlled OpenAI *portal* discovery). No product/keyword/category/industry/value/set-aside/profitability suitability decisions.
2. **Normalization and deduplication** — identity, amendments, cross-posts.
3. **Centralized requirement classification** — classify what is being purchased; independent of owner preferences.
4. **Evidence-based classification verification** — detail pages, line items, documents when available.
5. **User-selected filtering** — keywords, categories, value bands, geography, product-resale display mode.
6. **Commercial opportunity evaluation** — sourcing, pricing, financing, gates (later builds).

## Forbidden in Stage 1 (discovery adapters)

- `_is_productish` / `PRODUCT_HINTS` / `include_in_product_resale` as retrieval gates
- Commodity-only or category prefer/exclude lists that drop solicitations
- Service/construction title exclusions at fetch time
- Keyword or product-vocabulary matching as a discovery gate
- Requirement-class restrictions during retrieval

## Permitted in Stage 1

- Reliable source-level *solicitation status* filters (open / amended / active) when the source API supports them
- Schema validation that a dataset is a solicitation listing (vs awards/expenditures/vendor registries)
- Pagination, rate limits, timeouts, loop detection
- Mapping title/number/buyer/dates/URLs for normalization

## Display vs discovery

The Opportunities UI may default to showing tangible product-resale classifications. Toggling that filter must not change the underlying discovered population stored on the research run.
