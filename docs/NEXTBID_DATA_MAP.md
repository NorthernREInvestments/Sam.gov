# NextBid Data Map (verified)

Status: permanent technical roadmap for M3. Only documented/verified behavior.  
Last verified against: NextBid public docs (`https://nextbid.us/docs`), MCP tool list, and M3 client `m3/research/nextbid_client.py` + stored paid responses.

Base URL: `https://api.nextbid.us/v1` (also `https://nextbid.us/v1`).  
Auth: `X-API-Key` or `Authorization: Bearer`.  
Metering: **1 credit per metered call** (REST and MCP share one meter). Free: usage, account, activity, tradelines, stats, pursuits/profile.

Implementation gate: **all metered calls** go through `m3.research.nextbid_controller.metered_request`.  
Emergency stop / owner authorization / acquisition decision required for spend.

---

## Operations

### `search` (discovery)

| Field | Value |
| --- | --- |
| HTTP | `GET /v1/opportunities` |
| Client | `NextBidClient.search` → endpoint name `search` |
| Params (verified) | `tradelines`, `q`, `states`, `agencies`, `sources`, `set_asides`, `open`, `due_within_days`, `deadline_from`, `deadline_to`, `sort`, `page`, `page_size` (client clamps 5–25) |
| Returns | `total`, `page`, `results[]`, `facets` |
| Credit cost | 1 |
| Does **not** | Acquire attachments or prove package completeness |
| Cache / store | Durable HTTP cache + inventory writers under `data_root/nextbid_inventory` |
| Call when | Discovering listings; never as substitute for document acquisition |

### `get_opportunity`

| Field | Value |
| --- | --- |
| HTTP | `GET /v1/opportunities/{nb_id}` |
| Client | `NextBidClient.get_opportunity` |
| Returns (verified fields include) | `nb_id`, `title`, `agency`, `solicitation_number`, `source`, `source_url`, `response_deadline`, `document_count`, `attachment_count`, `description`, `summary`, `ai_summary`, `contacts`, `amendments`, `documents`, lifecycle/status fields |
| Credit cost | 1 |
| Persistent store | `nextbid_paid_responses` (+ HTTP cache) |
| Critical rule | `document_count > 0` means attachments **may** exist — **not** that they were downloaded |
| Call when | First detail pull; if docs indicated, follow with inventory (when authorized) |

### `list_documents`

| Field | Value |
| --- | --- |
| HTTP | `GET /v1/opportunities/{nb_id}/documents` |
| Client | `NextBidClient.list_documents` |
| Returns (observed + docs) | Filed documents and attachment index. **Observed keys in M3 store include `source_documents`, `nextbid_documents`, `originals_available`** (not only `documents`/`files`) |
| Credit cost | 1 |
| Persistent store | Paid response store; inventory built by `m3.research.document_inventory` |
| Downstream | Inventory statuses DISCOVERED / DOWNLOAD_PENDING — **inventory ≠ contents** |
| Known failure | Prior parsers ignored `source_documents` → false empty inventories |
| Call when | Opportunity reports docs / attachments and local inventory missing; reuse store first |

### `list_files`

| Field | Value |
| --- | --- |
| HTTP | `GET /v1/opportunities/{nb_id}/files` |
| Client | `NextBidClient.list_files` |
| Returns | Provider-processed text artifacts: path, bytes, mtime, kind (`document0`, `normalized`, `requirements`, …) |
| Credit cost | 1 |
| Note | Processed files ≠ original PDFs; useful but not automatic completeness |

### `read_file`

| Field | Value |
| --- | --- |
| HTTP | `GET /v1/opportunities/{nb_id}/files/{path}` |
| Client | `NextBidClient.read_file` |
| Returns | Text/markdown/JSON artifact (docs: up to 5 MB) |
| Credit cost | 1 |
| Store | Paid responses + extracted text paths under full-text stores |

### `get_document_download`

| Field | Value |
| --- | --- |
| HTTP | `GET /v1/opportunities/{nb_id}/documents/{sha256}/download` |
| Client | `NextBidClient.get_document_download` |
| Params | `nb_id`, document SHA-256 from `list_documents` where stored |
| Returns | Time-limited object-store URL (`download.url`, `download.expires_at`) |
| Credit cost | 1 for the link request; byte fetch is separate HTTP to object store |
| Success only when | Bytes downloaded, validated (not HTML error), checksum checked, saved under `nextbid_original_documents/`, associated to solicitation |
| Expired URLs | Reuse **bytes** if present; do **not** reuse expired URL; regenerate only with authorized credit budget (`signed_url_expired`) |

### Account / free ops

| Op | Path | Metered |
| --- | --- | --- |
| usage | `/v1/usage` | Free |
| tradelines | `/v1/tradelines` | Free |
| activity / account | per docs | Free |

---

## Response field pitfalls (permanent)

1. Parse **`source_documents`**, **`nextbid_documents`**, **`originals_available`**, plus legacy `documents`/`files`/`items`/`results`.
2. Never insert manifest metadata into the evidence corpus as if it were document text.
3. Never treat `document_count` alone as package completeness.
4. Prefer product/spec filenames; demote “Specimen Contract” / sample agreements (`select_best_source_document`).

## Storage destinations

| Artifact | Location |
| --- | --- |
| Paid JSON bodies | `data_root/nextbid_paid_responses` |
| Document inventory | `data_root/document_inventories/{nb_id}.json` (via `document_inventory`) |
| Original bytes | `data_root/nextbid_original_documents/` |
| Full text | `data_root/solicitation_full_text/` |
| AI (cached) | `data_root/ai_doc_understanding/` |
| Revalidated AI | `data_root/ai_doc_understanding/revalidated/` |

## Error handling (required)

- Finite connect/read timeouts on all HTTP.
- HTTP 429: honor `Retry-After`; pause provider; no credit burn loops.
- Timeouts may still bill: keep local reservation; reconcile to provider `used` separately.
- No unbounded retries on metered endpoints.
