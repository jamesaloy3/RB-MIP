# Market Intelligence Platform — System Architecture

## Design Principles

1. **Provenance is mandatory.** Every number in the database carries source, file, sheet/page, as-of date, and confidence. Every AI-generated summary cites specific records.
2. **Hybrid schema.** Structured facts (hotel fundamentals, convention bookings, transactions) live in typed tables — easier to query, faster to chart. Unstructured facts (narrative text) live in a flexible `narratives` table with `section` labels. AI summaries have their own `summaries` + `summary_citations` tables.
3. **Piecemeal data is the default.** Markets with partial coverage are first-class. The UI always shows a data-availability panel before drawing conclusions.
4. **Provider-agnostic core.** All fact tables carry `provider_code`. Adding a fourth provider means one new adapter file, not a schema change.
5. **Local-first, Azure-deployable.** SQLite in persistent storage. No Docker, no message queue. FastAPI serves both API and frontend from a single process.

---

## Data Sources

| Provider | File type | Granularity | Key metrics |
|---|---|---|---|
| LARC HotelBIS | Excel (.xlsx) | Market × Quarter | Supply, Demand, Occ, ADR, RevPAR, EBITDA, Cap Rate, Value Index |
| LARC Convention | Excel (.xlsx) | Market × Year (+ quarter where available) | Definite Room Nights, YoY Pace, vs-2019 Pace |
| LARC Narrative | PDF | Market × Publication | Full narrative report (supply, demand, transactions, forecast revision) |
| CoStar STR | Excel (.xlsx) | Submarket × Month (rolling 12-mo) | ADR, Occupancy, Demand, Inventory, Delivered Rooms |
| CoStar Narrative | PDF | Submarket × Publication | Submarket intelligence reports |
| Green Street | Excel (.xlsx) | Market (10 sheets) | Fundamentals, Forecasts (5 scenarios), Asset Values, IRR, Grades, Submarket Cap Rates |

---

## Component Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  UPLOAD LAYER (Azure Web App)                                 │
│                                                               │
│  Browser drag-and-drop  ──►  POST /api/upload                 │
│  (or folder watch in dev)     ├── validates file type         │
│                               ├── computes sha256 (dedup)     │
│                               └── enqueues to pipeline        │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  PIPELINE (Python, runs in background thread)                 │
│                                                               │
│  Router → identifies: provider, doc_type, market, pub_date    │
│       ↓                                                       │
│  Adapter (provider + doc_type specific)                       │
│       ├── Excel adapters: pandas, deterministic               │
│       └── PDF adapters: pdfplumber → OCR fallback             │
│                          (pytesseract if text layer absent)   │
│       ↓                                                       │
│  Validator                                                    │
│       ├── cross-field: RevPAR ≈ ADR × Occ (±2%)              │
│       ├── market name → canonical alias resolution            │
│       └── logs warnings, does NOT block on soft failures      │
│       ↓                                                       │
│  Loader (single SQLite transaction)                           │
│       ├── UPSERT publications                                 │
│       ├── DELETE + INSERT fact rows for this publication_id   │
│       └── logs to ingest_log                                  │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  STORAGE (SQLite, WAL mode)                                   │
│                                                               │
│  /home/data/market_intel.db   (Azure persistent storage)      │
│  /home/uploads/               (source files, organized by     │
│      larc/hotelbis/           provider after processing)      │
│      larc/convention/                                         │
│      larc/narrative/                                          │
│      costar/str/                                              │
│      costar/narrative/                                        │
│      greenstreet/                                             │
│                                                               │
│  Key tables:                                                  │
│    providers, markets, market_aliases, submarkets             │
│    publications, ingest_log                                   │
│    forecast_periods   ← LARC HotelBIS, CoStar STR, GS        │
│    convention_bookings← LARC CC BIS                           │
│    narratives         ← LARC + CoStar PDFs                    │
│    transactions       ← LARC PDF                              │
│    supply_pipeline    ← LARC PDF                              │
│    green_street_grades← GS Market Grades sheet                │
│    green_street_irr   ← GS Risk-Adjusted IRRs sheet           │
│    gs_submarket_cap_rates ← GS Submarket and Zip Cap Rates    │
│    summaries          ← AI-generated, versioned               │
│    summary_citations  ← token → source record mapping         │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  API LAYER (FastAPI, Python 3.11)                             │
│                                                               │
│  GET  /api/markets                   — list + coverage scores │
│  GET  /api/markets/{id}/coverage     — data availability grid │
│  GET  /api/markets/{id}/forecast     — forecast_periods       │
│  GET  /api/markets/{id}/convention   — convention_bookings    │
│  GET  /api/markets/{id}/narrative    — narratives by section  │
│  GET  /api/markets/{id}/summaries    — AI summaries + edits   │
│  GET  /api/markets/{id}/pipeline     — supply pipeline        │
│  GET  /api/markets/{id}/transactions — recent sales           │
│  POST /api/upload                    — file ingestion trigger │
│  POST /api/summaries/generate        — trigger AI generation  │
│  PATCH /api/summaries/{id}/edit      — save hand-edit         │
│  GET  /api/export/flat-csv           — Power BI flat export   │
│  GET  /api/admin/ingest-log          — pipeline status        │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  FRONTEND (Alpine.js + HTMX, served by FastAPI)              │
│                                                               │
│  No build step. Static files in /app/static/                  │
│  Views:                                                       │
│    1. Market Browser    — searchable list, coverage badges    │
│    2. Market Detail     — forecast charts, summaries, data    │
│                           availability panel, citation hover  │
│    3. Forecast Vintage  — how GS/LARC projections evolved     │
│    4. Coverage Dashboard— heat map: markets × providers       │
│    5. Upload / Ingest   — drag-and-drop + status log          │
│    6. Export            — flat CSV, Power Query template      │
└──────────────────────────────────────────────────────────────┘
```

---

## Adapter Pattern

Each provider+doc_type combination has one adapter module in `adapters/`. Every adapter implements the same interface (see `adapters/README.md`). The pipeline calls `adapter.parse(file_path)` and receives a typed `ParseResult` containing lists of records for each destination table. The adapter never writes to the database — that is the loader's job.

```
adapters/
  base.py                   # Abstract base class + ParseResult dataclass
  larc/
    hotelbis.py             # HotelBIS Excel → forecast_periods rows
    convention.py           # CC BIS Excel → convention_bookings rows
    narrative.py            # PDF → narratives + transactions + supply_pipeline rows
  costar/
    str_data.py             # AnalyticExport Excel → forecast_periods rows
    narrative.py            # Submarket PDF → narratives rows
  greenstreet/
    fundamentals.py         # Multi-sheet xlsx → forecast_periods + gs_* rows
```

### Green Street adapter notes (multi-sheet)

The Green Street file is NOT one sheet per market. It is one sheet per metric type. The `fundamentals.py` adapter reads all relevant sheets in one pass:

| Sheet | Destination | Notes |
|---|---|---|
| Baseline Forecast | `forecast_periods` (scenario='baseline') | Quarterly, 2005–2030+. Headers at row 5. |
| Exceptionally Strong Growth | `forecast_periods` (scenario='strong_growth') | Same structure |
| Moderate Recession | `forecast_periods` (scenario='moderate_recession') | Same structure |
| Protracted Slump | `forecast_periods` (scenario='protracted_slump') | Same structure |
| Stronger Near-Term Growth | `forecast_periods` (scenario='near_term_strong') | Same structure |
| Baseline Fundamentals | `forecast_periods` (annual, historical) | Wide format: years as columns, metric sections as row blocks |
| Asset Values | `forecast_periods` (cap_rate, quarterly) | Wide format: dates as columns |
| Market Grades | `green_street_grades` | Headers at row 4 |
| Risk-Adjusted IRRs | `green_street_irr` | Headers at row 4 |
| Submarket and Zip Cap Rates | `gs_submarket_cap_rates` | Headers at row 4, 4,900+ rows |

All sheets have a title block in rows 1–3 and actual column headers at row 4 (Baseline Forecast/scenario sheets: row 5). The adapter skips these header rows using `skiprows` in pandas.

---

## Summary Generation

```
summarize.py  (market_id, quarter)
       │
       ├── 1. Query all fact tables for this market × quarter
       │       + 4 prior quarters for context
       │       + full-year context
       │       + GS scenario forecasts for next 2 years
       │
       ├── 2. Build data availability report
       │       (which providers × metrics × periods exist)
       │
       ├── 3. Load prompt template from prompts/summary_v1.md
       │       Inject: data payload + availability report + few-shot examples
       │
       ├── 4. Call Anthropic API
       │       Model: from ANTHROPIC_MODEL env var (default claude-sonnet-4-6)
       │       Output: structured JSON { detailed: "...", short: "..." }
       │       Every cited number must use token [obs:TABLE:ID]
       │
       ├── 5. Parse + validate citations
       │       Reject any [obs:TABLE:ID] where ID was not in the context payload
       │
       └── 6. Write to summaries table (new row, never overwrite)
               Write citation map to summary_citations
```

The prompt template in `prompts/summary_v1.md` is parameterized with Jinja2 placeholders. Changing the prompt without touching code: edit the file, bump the version string at the top, re-run generation.

---

## Data Availability Model

Every market gets a **coverage score** computed on demand from the database. The score drives the UI (sorting in Market Browser, color coding in Coverage Dashboard).

Coverage dimensions:
- `has_larc_hotelbis` — bool, most recent publication date
- `has_larc_convention` — bool
- `has_larc_narrative` — bool
- `has_costar_str` — bool, most recent publication date
- `has_costar_narrative` — bool
- `has_greenstreet` — bool
- `obs_count_total` — total rows across all fact tables
- `most_recent_data` — MAX(publication_date) across all sources
- `quarters_covered` — list of (year, quarter) pairs with ≥1 observation

"No data" (NULL in the database) is always visually distinct from "zero" in the UI.

---

## Azure Deployment

```
Azure Web App (Linux, Python 3.11)
  /home/data/market_intel.db       — SQLite, WAL mode
  /home/uploads/                   — source files (persisted via Azure Files mount)
  /app/                            — application code (deployed from git)
    main.py                        — FastAPI app entry point
    static/                        — Alpine.js + HTMX frontend assets
    adapters/
    pipeline/
    summarize.py

Environment variables (Azure App Settings):
  ANTHROPIC_API_KEY
  ANTHROPIC_MODEL          (default: claude-sonnet-4-6)
  DB_PATH                  (default: /home/data/market_intel.db)
  UPLOAD_ROOT              (default: /home/uploads)
  PROMPT_VERSION           (default: summary_v1)
```

Deployment: push to main branch → Azure Web App deployment slot picks up via GitHub Actions or Azure deployment center.

For development: same code, SQLite at `./data/market_intel.db`, uploads at `./uploads/`.

---

## Folder Structure

```
larc-market-intel/
├── adapters/
│   ├── README.md               # Adapter contract
│   ├── base.py
│   ├── larc/
│   │   ├── hotelbis.py
│   │   ├── convention.py
│   │   └── narrative.py
│   ├── costar/
│   │   ├── str_data.py
│   │   └── narrative.py
│   └── greenstreet/
│       └── fundamentals.py
├── pipeline/
│   ├── router.py
│   ├── orchestrator.py
│   └── validator.py
├── api/
│   ├── main.py
│   └── routes/
│       ├── markets.py
│       ├── summaries.py
│       ├── upload.py
│       └── export.py
├── static/                     # Alpine.js + HTMX frontend
│   ├── index.html
│   ├── app.js
│   └── style.css
├── db/
│   ├── schema.sql
│   └── seed_markets.py
├── prompts/
│   └── summary_v1.md
├── config/
│   └── providers.yaml
├── tests/
│   ├── fixtures/
│   └── test_adapters/
├── docs/
│   ├── 00_ARCHITECTURE.md      # this file
│   ├── 02_INGEST_WORKFLOW.md
│   ├── 03_CANONICAL_FIELDS.md
│   └── 05_APP.md
├── summarize.py
├── ingest.py                   # CLI: python ingest.py --provider larc-hotelbis --file ...
├── requirements.txt
└── README.md
```
