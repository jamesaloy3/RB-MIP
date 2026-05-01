# Claude Code Project Brief — Market Intelligence Platform

**Purpose of this document:** This is the single prompt you give Claude Code at the start of the project. Paste this as your first message. Claude Code will read it, ask clarifying questions (respond, or say "proceed with defaults"), and then scaffold the project.

**Important: read `docs/` before generating code.** The architectural decisions are specified there — do not re-derive them.

---

## Your role

You are building a **hospitality real-estate market intelligence platform** for a hotel asset manager. The platform ingests quarterly market reports from multiple providers (starting with **LARC** and **CoStar**), extracts both quantitative forecasts and qualitative narrative, stores them in a unified provider-agnostic database, and exposes them through a FastAPI backend + Next.js web app.

The architecture is already designed. Your job is to implement it faithfully, write tests, and only deviate from the spec when you find a concrete problem — in which case, stop and ask.

## Ground truth files (read these first, in order)

1. `docs/00_ARCHITECTURE.md` — component layout, folder structure
2. `docs/02_INGEST_WORKFLOW.md` — the file-drop-to-database pipeline
3. `docs/03_CANONICAL_FIELDS.md` — the LARC↔CoStar field mapping
4. `docs/05_APP.md` — UX and API contract
5. `schemas/canonical/*.schema.json` — JSON schemas (source of truth for data shape)
6. `schemas/provider_mappings/larc_field_map.yaml` — LARC-specific mapping
7. `schemas/provider_mappings/costar_field_map.yaml` — CoStar skeleton (fill in when first file lands)
8. `db/schema.sql` — DDL

## Non-negotiable design rules

1. **Provider-agnostic canonical schema.** Never write LARC-specific logic outside the `extractors/larc/` folder or the LARC mapping YAML. Same for CoStar. Anything touching `db/`, `api/`, or `app/` must treat provider as a dimension.

2. **Publication-versioned facts.** Every fact row is keyed by `publication_id`. Re-ingest of the same publication is a DELETE+INSERT, never an UPDATE. History is preserved by new publications, never by mutating old rows.

3. **Deterministic publication_id.** `sha1(provider|canonical_market|publication_date|doc_type)`. Dropping the same file twice must be a no-op at step 2 of the ingest workflow.

4. **Idempotent ingest.** Every step must be safely re-runnable. No "first run vs subsequent run" branches.

5. **No hardcoded market lists outside `db/seed_markets.py`.** Markets are data, not code.

6. **JSON Schema is the contract.** Every extractor output must validate against `schemas/canonical/` before DB insert. Validation failure = move to `_errors/`, do not insert.

7. **Use the Anthropic API for narrative extraction.** Structured output mode with the `narrative.schema.json` as the response schema. Do not try to regex-parse narrative prose.

8. **Decimals for percentages.** Always store 0.013 for 1.3%. Providers that ship differently get transformed in the extractor, not downstream.

9. **Tests alongside code.** Every extractor has a fixture (sample file in `tests/fixtures/`) and a golden JSON output. CI runs extraction and diffs against golden.

10. **Secrets via `.env`.** `ANTHROPIC_API_KEY`, `DATABASE_URL`, optional `SLACK_WEBHOOK_URL`. Never commit.

## Tech stack

- **Python 3.11+** — pipeline, extractors, API
- **FastAPI** — API layer
- **SQLAlchemy 2.0 + Alembic** — ORM and migrations
- **SQLite** for dev, **PostgreSQL 15+** for prod. Schema in `db/schema.sql` is ANSI; SQLAlchemy models abstract dialect differences.
- **pdfplumber + camelot-py** — PDF text and table extraction
- **Anthropic Python SDK** (`anthropic` package) — narrative and fallback table extraction. Use the current generally-available Claude model; default to `claude-sonnet-4-6` unless cost dictates otherwise.
- **pandas + openpyxl** — xlsx extraction
- **pydantic v2** — runtime types, aligned to JSON schemas via `datamodel-code-generator`
- **watchdog** — file watcher
- **jsonschema** — validation
- **pytest** — testing
- **Next.js 14 App Router + TypeScript + Tailwind + shadcn/ui + Recharts + TanStack Query** — frontend
- **Docker Compose** — local dev orchestration (watcher + api + db + app)

## Build order

Do these in strict sequence. Do not skip ahead. Commit after each phase with a clear message.

### Phase 0 — Bootstrap (1 commit)
- Initialize repo, pyproject.toml, package.json for `app/`
- Docker Compose with 4 services: `db` (postgres), `watcher`, `api`, `app`
- Alembic initialized, empty migration
- `.env.example` with every variable
- README with quickstart

### Phase 1 — Schema and DB (1 commit)
- Translate `db/schema.sql` into SQLAlchemy 2.0 models in `db/models.py`
- First Alembic migration creates all tables
- `db/seed_markets.py` populates `markets` from a canonical CSV (seed with all 76 markets from the LARC HotelBIS file, sourced at `tests/fixtures/HotelBIS_Data_File.xlsx` — extract distinct `Market` values, infer state from a lookup table you generate)
- Tests: `test_models_create_all`, `test_seed_markets_idempotent`

### Phase 2 — JSON Schemas → Pydantic (1 commit)
- Run `datamodel-code-generator` to produce `pipeline/models_canonical.py` from `schemas/canonical/*.schema.json`
- Manually verify generated models; commit both schemas and generated code
- Tests: round-trip JSON → pydantic → JSON for each schema

### Phase 3 — LARC xlsx extractor (1 commit)
- `extractors/larc/xlsx_extractor.py`
- Input: path to HotelBIS-format xlsx
- Reads `schemas/provider_mappings/larc_field_map.yaml`
- Loops over distinct markets, emits one canonical JSON bundle per market:
  - 1 Publication
  - N ForecastPeriod (quarterly + annual where Period='A')
- Deterministic `publication_id`
- Fixture: `tests/fixtures/HotelBIS_sample.xlsx` (real file, distributed with the brief)
- Golden: `tests/fixtures/golden/larc_xlsx_denver.json`
- Test extracts Denver, validates against JSON schema, diffs against golden

### Phase 4 — Ingest pipeline core (1 commit)
- `pipeline/router.py` — detect provider from path, doc_type from extension, pull market + date from filename with regex + fallback
- `pipeline/orchestrator.py` — extract → validate → load. Transactional.
- `pipeline/validator.py` — jsonschema + cross-field sanity (RevPAR ≈ ADR×Occ, EBITDA margin)
- `pipeline/watcher.py` — watchdog-based, debounced, queued
- Tests: `test_roundtrip_xlsx` drops file in fixtures folder, runs orchestrator, asserts DB state

### Phase 5 — LARC PDF extractor (2 commits)
Commit A: **tables** (camelot-py). Extract:
- Page 2 annual forecast table → ForecastPeriod[period_type=annual]
- Page 2 quarterly forecast table → ForecastPeriod[period_type=quarterly]
- Page 12 supply pipeline (Select Hotel Developments) → SupplyPipelineItem
- Page 15 Recent Hotel Transactions → Transaction
- Page 25 submarket historical indices → SubmarketIndex[index_type=submarket]
- Page 26 property class indices → SubmarketIndex[index_type=property_class]
- Page 27 Forecast Revision Summary

Commit B: **narrative** (Anthropic API). For each page text block:
- Identify section via heading pattern match (see larc_field_map.yaml `pdf.sections`)
- Send section text + canonical section enum + `narrative.schema.json` to Claude as structured-output tool call
- Extract passages, entity tags, key_metrics_referenced, sentiment
- Post-validate with jsonschema
- Fixture: `tests/fixtures/LARC_DENVER_1Q26.pdf` (real report distributed with the brief)
- Golden: `tests/fixtures/golden/larc_pdf_denver.json` — the first run's output is committed as golden; subsequent runs diff against it. Regressions require reviewing the diff and either fixing extractor or updating golden.

### Phase 6 — Cross-validation (1 commit)
- Post-load step: if both xlsx and PDF publications exist for same `(provider, market, publication_date_month)`, compare annual forecast values between them
- Discrepancies > 0.5% → insert into `validation_warnings`
- Narrative-claims validation: regex-find "X% CAGR" in narrative, verify against computed CAGR from forecast_periods
- Narrative ranking claims ("ranks 16th of 62") → populate `forecast_summaries.rank_*` fields and warn if inconsistent with data
- Test: feed matching xlsx + PDF, assert no warnings; mutate PDF value, assert warning created

### Phase 7 — FastAPI backend (1–2 commits)
- `api/main.py` mounts `api/routes/markets.py`, `api/routes/search.py`, `api/routes/admin.py`
- Implement every endpoint in `docs/05_APP.md` §"API contract"
- Use SQLAlchemy queries, not raw SQL, except for FTS5
- Pydantic response models that mirror canonical schemas
- Tests: each endpoint hit with DB pre-seeded from phase 3 fixtures, snapshot response

### Phase 8 — Frontend app (2–3 commits)
- Commit A: chrome + `MarketSelector` + `ProviderToggle` + `PublicationPicker`, one Market Dashboard page working end-to-end with real data
- Commit B: Forecast Chart, Quarterly Forecast Table, Narrative Reader
- Commit C: Supply Pipeline, Transactions, Comparison page, Revisions page
- Components in `app/components/`, feature pages in `app/app/(routes)/`
- TanStack Query with 5-minute staleTime; useQueryClient-driven invalidation on websocket ingest events (stretch)

### Phase 9 — CoStar (stub → filled later)
- Create `extractors/costar/xlsx_extractor.py` and `extractors/costar/pdf_extractor.py` as mirrors of LARC structure
- Both should be functional the moment a real CoStar file is delivered — they read `costar_field_map.yaml`, which is filled in at that point
- Until then: the extractors raise `NotImplementedError("Awaiting first CoStar file")` but `router.py` correctly recognizes CoStar files and routes them there
- Test: drop a mock CoStar file with known structure, assert correct routing and error surfacing

### Phase 10 — Operational polish (1 commit)
- `/admin/ingest` page fully wired (drag-drop upload, live log tail via SSE)
- Slack webhook on error (optional, env-gated)
- `scripts/backfill.py` walks historical folder, processes in parallel with bounded worker pool
- `scripts/reextract.py` re-runs extractor on all `_processed/` files of a given provider — for when extractor is upgraded

## Clarifying questions to ask before starting

Before writing any code, ask the user for decisions on:

1. **Deployment target** — local docker-compose only, or also production (AWS/Azure/GCP)? If prod, which?
2. **Auth** — does the app need login? If yes, which provider (Auth0, Okta, Azure AD, Supabase)?
3. **Portfolio data** — will the user provide a list of their hotels + market assignments now, or should the Portfolio View be stubbed with demo data?
4. **Historical backfill** — how many prior quarterly LARC reports are available? Dates? Same for CoStar.
5. **Delivery cadence** — quarterly publications arrive in batches (e.g., 60 PDFs same week). Should the watcher throttle API calls to Claude, and at what concurrency?
6. **CoStar file format** — is an example available? If yes, attach before Phase 9.

If the user says "proceed with defaults," use: local docker-compose only, no auth, Portfolio View stubbed with 5 demo hotels, no historical backfill (start fresh), concurrency of 4 on Claude API, CoStar extractor stubs with `NotImplementedError`.

## Testing discipline

- Every extractor has at least one golden-file test.
- Every API endpoint has at least one snapshot test.
- Every validator rule has a positive and negative case.
- No merge without all tests green.
- Run `pytest -q` and `npm test` locally before each commit.

## When to stop and ask

- A fixture doesn't match what the LARC report actually produces (numbers look wrong, columns misaligned): **stop, show me the discrepancy**.
- A schema field would need to change to accommodate reality: **propose the change with rationale**, do not silently add a field.
- A canonical section enum needs a new value: same — propose, don't add.
- Claude API structured output fails consistently on a section: **stop and show me the failing input**, don't fall back to regex.

## Definition of done

- `docker compose up` starts everything.
- Dropping `LARC_DENVER_1Q26.pdf` into `ingest/larc/pdf/` results in a fully populated Market Dashboard at `/markets/denver` within 2 minutes, with narrative rendered by section and forecast chart populated through 2030.
- Dropping the HotelBIS xlsx processes all 76 markets, populating `markets` and `forecast_periods` for each.
- `pytest -q` passes 100%.
- README documents the full flow, including "how to add a new provider" as a recipe (copy mapping yaml, write extractor class, add tests; nothing else).
