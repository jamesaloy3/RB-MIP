# Ingest Workflow

## Trigger → Landed in DB: Full Path

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 1. FILE DROP                                                             │
│    User drops file into one of:                                          │
│      ingest/larc/pdf/     ingest/larc/xlsx/                              │
│      ingest/costar/pdf/   ingest/costar/xlsx/                            │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 2. WATCHER (watchdog lib, Python)                                        │
│    - Fires on file-close (not file-create) → avoids partial reads        │
│    - Debounces 3 seconds to ensure upload complete                       │
│    - Computes sha256 → checks publications table for duplicate           │
│      - If dup: skip, log 'already ingested: {publication_id}'            │
│      - Else: enqueue to orchestrator                                     │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 3. ROUTER                                                                │
│    Inputs: file path, file bytes (first 4KB)                             │
│    Outputs: {provider, doc_type, market, publication_date}               │
│                                                                          │
│    Resolution logic:                                                     │
│    - provider   ← folder name (ingest/<provider>/...)                    │
│    - doc_type   ← folder name (pdf = market_intelligence_report,         │
│                                xlsx = data_file)                         │
│    - market     ← filename regex (e.g., LARC_DENVER_*.pdf → Denver)      │
│                   fallback: first-page OCR for PDF, 'Market' column      │
│                   distinct values for xlsx                               │
│    - pub_date   ← filename regex (_1Q26, _2026-03)                       │
│                   fallback: xlsx 'Published' col, PDF footer text        │
│                                                                          │
│    publication_id = sha1(provider|market|pub_date|doc_type)              │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 4. EXTRACTOR (provider + type specific)                                  │
│                                                                          │
│    a. LARC xlsx:                                                         │
│       - pandas read → one DataFrame                                      │
│       - For every (market) group, emit ForecastPeriod records            │
│       - Filter to rows matching the file's market OR emit all-markets    │
│         data — xlsx files cover 76 markets, so each yields 76 pubs       │
│       - Deterministic, zero LLM calls                                    │
│                                                                          │
│    b. CoStar xlsx: same pattern, different column map                    │
│                                                                          │
│    c. LARC pdf:                                                          │
│       HYBRID EXTRACTION                                                  │
│       - Text layer via pdfplumber                                        │
│       - Tables via camelot-py (lattice mode for structured, stream for   │
│         loose). Validate extracted tables against forecast_period schema │
│       - For tables that camelot fails on (sensitivity matrices, forecast │
│         revision), fall back to Claude API with structured JSON output   │
│       - For narrative text: chunk by detected section heading, send each │
│         chunk to Claude API with a strict JSON schema for Narrative[],   │
│         including entity tagging and metric extraction                   │
│       - Output: one canonical JSON doc per canonical table               │
│                                                                          │
│    d. CoStar pdf: same pattern                                           │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 5. VALIDATOR                                                             │
│    For each emitted record:                                              │
│      - JSON Schema validation (strict, draft 2020-12)                    │
│      - Cross-field sanity:                                               │
│         * RevPAR ≈ ADR × Occupancy (tolerance 2%)                        │
│         * EBITDA margin = EBITDA / Revenues (tolerance 1%)               │
│         * Year monotonic within publication                              │
│      - Cross-source sanity (when both xlsx + pdf present for same pub):  │
│         * annual RevPAR from xlsx ≈ PDF table value (tolerance 0.5%)     │
│      - Market canonicalization: 'Denver, CO' → 'Denver' via alias table  │
│                                                                          │
│    On validation failure:                                                │
│      - Move source file to ingest/_errors/                               │
│      - Write .log with validation errors next to moved file              │
│      - Do NOT insert partial data                                        │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 6. LOAD                                                                  │
│    Single transaction:                                                   │
│      - UPSERT publications (by publication_id)                           │
│      - DELETE existing rows for this publication_id from fact tables     │
│      - INSERT new rows                                                   │
│      - Triggers: recompute forecast_summary denorm row                   │
│                                                                          │
│    After commit:                                                         │
│      - Move source file to ingest/_processed/<provider>/<yyyy>/<mm>/     │
│      - Append to audit log (ingest_log table)                            │
│      - Emit event to websocket channel for live-updating app UI          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Idempotency

Every step is idempotent:
- `publication_id` is deterministic, so re-dropping the same file is a no-op (detected at step 2).
- If the extractor is upgraded (`extractor_version` bumps), a full re-ingest is triggered by moving files back from `_processed/` to the source folder. The DELETE+INSERT pattern in step 6 ensures clean overwrites.

## Backfill Mode

`python -m pipeline.backfill --provider larc --folder /path/to/historical_reports`

Walks a directory recursively and processes every file through the same pipeline. Used once at project start to load historical LARC and CoStar archives.

## Validation Cross-Checks (Quant ↔ Qual)

After loading, a post-load validator runs to flag inconsistencies between extracted quant and narrative:

| Narrative claim | Quant check |
|---|---|
| "RevPAR will increase at a 3.3% CAGR over next five years" | Compute CAGR from forecast_period 2025→2030; must be within 0.1% |
| "ranks 16th of 62 markets" | Requires all 62 markets in same publication batch; rank from data |
| "cap rates end 2030 expanding 5 bps more than national" | Verify against forecast_period cap_rate series |
| "Residence Inn Denver Downtown closed in December" | Should NOT appear in supply_pipeline; may appear in closures log |

Mismatches go to a `validation_warnings` table and surface as yellow flags in the app. They do not block ingest.

## Error Handling

| Failure | Action |
|---|---|
| File corrupt / unreadable | Move to _errors/, log stack trace |
| Market name unresolvable | Move to _errors/, require user to add alias in `markets` table |
| JSON Schema validation fails | Move to _errors/, dump schema errors per record |
| Partial table extraction (e.g., missing 2027 quarter) | Insert what's available, write warning to `ingest_log` |
| Claude API timeout | Retry 3× with exponential backoff; then _errors/ |

## Observability

- `ingest_log` table: every file ever seen, status, timings, record counts
- Dashboard page `/admin/ingest` in the app shows queue, recent successes, errors
- Optional Slack webhook on errors (configurable in `.env`)
