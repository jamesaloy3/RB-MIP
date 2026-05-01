# Application Specification

Stack: **Next.js 14 (App Router) + TypeScript + Tailwind + shadcn/ui + Recharts + TanStack Query**.
Backend: **FastAPI** (Python) over the canonical DB.

## Core UX principle

Provider is a **dimension**, not a navigation mode. Every view has a provider toggle (Single / LARC / CoStar / Both). When "Both" is selected, views show overlay or side-by-side comparison. This reinforces that LARC and CoStar are interchangeable sources of the same canonical data.

## Pages

### 1. Market Dashboard — `/markets/[slug]`
Top of page: sticky header with Market name + Provider toggle + Publication picker (latest → N prior publications).

Sections, top to bottom:

- **Headline Panel**: 5-year CAGRs (RevPAR, ADR, Value) with provider-colored bars. Rank badges ("16 / 62 markets"). Sentiment indicator.
- **Forecast Chart**: Multi-line chart (RevPAR, ADR, Occupancy index) historical + forecast. Shaded forecast region. Provider toggle overlays second line per metric. Downloadable CSV.
- **Quarterly Forecast Table**: The LARC-style quarterly table rendered from `forecast_periods` where `period_type='quarterly'`. Filter by date range. When both providers selected, alternating row colors per provider.
- **Narrative Reader** (collapsible): Organized by canonical section. Each passage shows source provider, publication date, and page refs. Entity tags are clickable — click "Denver CBD" to filter all narrative mentioning it.
- **Supply Pipeline**: Table + timeline visualization. Filter by phase, submarket, scale. "New since last publication" badge.
- **Transactions**: Last 24 months of comps, filters for price-per-key, property class. Link to LARC Score.
- **Cross-provider warnings**: If `validation_warnings.severity='warn'` for this publication, show yellow banner with drill-down.

### 2. Provider Comparison — `/compare/[slug]`
Full side-by-side view of LARC vs CoStar for a selected market. Three-column layout: metric | LARC | CoStar | Δ. Sortable by largest disagreement. Useful for "where do the providers diverge?" analysis.

### 3. Portfolio View — `/portfolio`
User's hotels tagged to a market. Each hotel row shows:
- Market's forecast RevPAR CAGR (from latest publication, LARC default)
- Portfolio-weighted average
- Overlay: hotel's own performance vs market forecast
- Triggers: flag hotels whose market has been revised down significantly in the latest publication

### 4. Narrative Search — `/search`
Full-text search across all narrative + transaction descriptions + pipeline. FTS5 backed. Filter by provider, market, section, date range. Shows snippets with highlighting. Every hit links back to source publication.

### 5. Forecast Revisions — `/revisions`
Time-series of how a market's 5-year RevPAR CAGR forecast has evolved across publications. Useful for calibrating provider accuracy — did LARC overshoot in 2024? Chart: x = publication date, y = forecasted 5-year CAGR. Separate line per provider. Tooltip shows what narrative section changed between revisions.

### 6. Admin / Ingest — `/admin/ingest`
Live ingest queue. Drag-and-drop upload (bypasses folder watcher for one-offs). Shows last 200 files with status. Click into any file to see the extracted canonical JSON diff vs what landed in DB.

### 7. Publication Library — `/library`
Browse raw source PDFs + xlsx files. Filter by provider / market / date. Click a publication to open the rendered view (page 1–3).

## API contract (FastAPI)

```
GET  /api/v1/markets
GET  /api/v1/markets/{slug}
GET  /api/v1/markets/{slug}/forecast
        ?provider=LARC|CoStar|both
        &publication_date=latest
        &period_type=annual|quarterly
        &metric=revpar,adr,occupancy
GET  /api/v1/markets/{slug}/narrative
        ?section=...&provider=...
GET  /api/v1/markets/{slug}/transactions
GET  /api/v1/markets/{slug}/pipeline
GET  /api/v1/markets/{slug}/revisions
        ?metric=revpar_cagr_5yr&provider=LARC
GET  /api/v1/search
        ?q=...&provider=...&section=...
POST /api/v1/ingest/upload     # multipart upload; same pipeline as folder watch
GET  /api/v1/admin/ingest-log
GET  /api/v1/providers
```

All GET endpoints return JSON. Large result sets paginate with `?limit=&cursor=`. CSV export: append `?format=csv`.

## Component library

- `<ForecastChart provider metric horizon />` — recharts wrapper, respects provider toggle
- `<NarrativeCard passage provider />` — renders one narrative passage with entity chips
- `<MarketSelector />` — combobox over `markets.canonical_name`
- `<ProviderToggle value onChange />` — three-state (LARC / CoStar / Both)
- `<PublicationPicker marketSlug />` — dropdown of publications for that market
- `<CAGRBadge value rank of />` — headline stat with rank

## Performance

- API responses cached 5 min (most data is publication-versioned and only changes quarterly)
- TanStack Query persistence to localStorage
- Forecast chart data precomputed into a view (`v_annual_forecast`) — no per-request aggregation
- Narrative search hits FTS5 index; returns top 50 ranked
