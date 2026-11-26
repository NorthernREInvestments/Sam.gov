# BUILD 002O — OpenAI Cost Audit

**Spend date audited:** `2026-11-21`  
**Daily ceiling:** `$1.0 (PRESERVED — not raised)`  
**Cost basis:** application estimates used for enforcement (not OpenAI invoices)

## Official pricing (gpt-4o-mini)

- Input: **$0.15 / 1M tokens**
- Output: **$0.60 / 1M tokens**
- Cached input: **$0.075 / 1M tokens**
- Source: https://developers.openai.com/api/docs/pricing
- Application MODEL_RATES match official input/output rates.

## Why the $1 ceiling was reached after ~17 quantity calls

Shared $1 daily ceiling was consumed primarily by classification_recovery web_search calls ($0.8976, 71 events), not by quantity_extraction ($0.0098). Quantity extraction itself averaged well under $0.001 per call. Budget enforcement used actual committed application estimates; open reservations were $0 at audit time.

### Same-day spend by objective

```json
{
  "source_discovery_proof": {
    "events": 2,
    "committed_usd": 0.025,
    "reserved_usd": 0.0,
    "released_usd": 0.0,
    "input_tokens": 17382,
    "output_tokens": 64,
    "web_search_calls": 2,
    "statuses": {
      "committed": 2
    }
  },
  "supplier_recruitment": {
    "events": 1,
    "committed_usd": 0.0135,
    "reserved_usd": 0.0,
    "released_usd": 0.0,
    "input_tokens": 8890,
    "output_tokens": 1537,
    "web_search_calls": 1,
    "statuses": {
      "committed": 1
    }
  },
  "historical_awards": {
    "events": 2,
    "committed_usd": 0.0259,
    "reserved_usd": 0.0,
    "released_usd": 0.0,
    "input_tokens": 17724,
    "output_tokens": 1292,
    "web_search_calls": 2,
    "statuses": {
      "committed": 2
    }
  },
  "source_discovery": {
    "events": 2,
    "committed_usd": 0.025599999999999998,
    "reserved_usd": 0.0,
    "released_usd": 0.0,
    "input_tokens": 17828,
    "output_tokens": 920,
    "web_search_calls": 2,
    "statuses": {
      "committed": 2
    }
  },
  "classification_recovery": {
    "events": 71,
    "committed_usd": 0.8975999999999998,
    "reserved_usd": 0.0,
    "released_usd": 0.0,
    "input_tokens": 649573,
    "output_tokens": 9202,
    "web_search_calls": 71,
    "statuses": {
      "committed": 71
    }
  },
  "quantity_extraction": {
    "events": 21,
    "committed_usd": 0.0098,
    "reserved_usd": 0.0,
    "released_usd": 0.0,
    "input_tokens": 57202,
    "output_tokens": 2110,
    "web_search_calls": 0,
    "statuses": {
      "committed": 21
    }
  }
}
```

| Bucket | Committed USD |
|--------|--------------:|
| classification_recovery (web_search) | 0.8975999999999998 |
| quantity_extraction | 0.0098 |
| Open reservations at audit | 0.0 |

Quantity extraction smoke tests were ~$0.0001; production qty calls typically $0.0004–$0.0013.
The ceiling was reached through **actual committed spend on other shared-budget objectives**, not through stuck reservations.

## Accounting fixes in 002O

- Stopped double-counting assumed 8k search-content tokens when API `input_tokens` already include tool content.
- Quantity extraction uses content-hash cache (no repeat spend).
- Section filtering + batching for short texts.
- Adaptive reserves sized to remaining daily budget.
- Per-opportunity token/cost recording.

## Snapshot

- Before 002O processing: spent `0.9974` remaining `0.0026`
- After: spent `0.9999` remaining `0.0001`
- 002O quantity spend this run: `0.0025`
- Input/output tokens this run: `10206` / `1598`
- Cache hits: `0`
