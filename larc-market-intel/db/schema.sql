-- ============================================================================
-- Market Intelligence Platform — SQLite Schema
-- WAL mode enabled at connection time: PRAGMA journal_mode=WAL;
-- ============================================================================

-- ----- Reference tables -----------------------------------------------------

CREATE TABLE IF NOT EXISTS providers (
    provider_code TEXT PRIMARY KEY,   -- 'LARC', 'CoStar', 'GreenStreet'
    display_name  TEXT NOT NULL,
    website       TEXT
);
INSERT OR IGNORE INTO providers VALUES
    ('LARC',        'Lodging Analytics Research & Consulting', 'https://larcanalytics.com'),
    ('CoStar',      'CoStar Group',                            'https://costar.com'),
    ('GreenStreet', 'Green Street Advisors',                   'https://greenstreetadvisors.com');

CREATE TABLE IF NOT EXISTS markets (
    market_id        INTEGER PRIMARY KEY,
    canonical_name   TEXT NOT NULL UNIQUE,  -- 'Denver'
    state            TEXT,                  -- 'CO'
    country          TEXT DEFAULT 'US',
    msa_code         TEXT,
    latitude         REAL,
    longitude        REAL,
    active           INTEGER DEFAULT 1      -- BOOLEAN stored as 0/1
);

CREATE TABLE IF NOT EXISTS market_aliases (
    alias_id      INTEGER PRIMARY KEY,
    market_id     INTEGER NOT NULL REFERENCES markets(market_id) ON DELETE CASCADE,
    alias         TEXT NOT NULL,
    provider_code TEXT REFERENCES providers(provider_code),
    UNIQUE(alias, provider_code)
);
CREATE INDEX IF NOT EXISTS idx_alias_lookup ON market_aliases(alias, provider_code);

CREATE TABLE IF NOT EXISTS submarkets (
    submarket_id   INTEGER PRIMARY KEY,
    market_id      INTEGER NOT NULL REFERENCES markets(market_id) ON DELETE CASCADE,
    canonical_name TEXT NOT NULL,     -- 'Denver CBD'
    short_code     TEXT,              -- 'CBD'
    UNIQUE(market_id, canonical_name)
);

CREATE TABLE IF NOT EXISTS submarket_aliases (
    id            INTEGER PRIMARY KEY,
    submarket_id  INTEGER NOT NULL REFERENCES submarkets(submarket_id) ON DELETE CASCADE,
    alias         TEXT NOT NULL,
    provider_code TEXT REFERENCES providers(provider_code),
    UNIQUE(alias, provider_code)
);

-- ----- Publications (one row per ingested file) ------------------------------

CREATE TABLE IF NOT EXISTS publications (
    publication_id     TEXT PRIMARY KEY,   -- sha1(provider|market|pub_date|doc_type)
    provider_code      TEXT NOT NULL REFERENCES providers(provider_code),
    market_id          INTEGER REFERENCES markets(market_id),  -- NULL for multi-market files
    doc_type           TEXT NOT NULL,
    -- doc_type values:
    --   'larc_hotelbis'       LARC HotelBIS quarterly data Excel
    --   'larc_convention'     LARC Aggregated Convention Center data Excel
    --   'larc_narrative'      LARC market intelligence PDF
    --   'costar_str'          CoStar STR AnalyticExport Excel
    --   'costar_narrative'    CoStar submarket PDF
    --   'greenstreet'         Green Street Fundamentals and Valuation Excel
    publication_date   TEXT NOT NULL,      -- ISO date string
    publication_period TEXT,               -- '1Q26', '4Q25', etc.
    source_filename    TEXT NOT NULL,
    source_sha256      TEXT NOT NULL UNIQUE,
    source_bytes       INTEGER,
    ingested_at        TEXT NOT NULL,      -- ISO timestamp
    extractor_version  TEXT NOT NULL,
    raw_json_path      TEXT               -- archival extracted JSON (optional)
);
CREATE INDEX IF NOT EXISTS idx_pub_provider_date
    ON publications(provider_code, publication_date DESC);
CREATE INDEX IF NOT EXISTS idx_pub_market
    ON publications(market_id, publication_date DESC);

-- ----- Core fact table: hotel fundamentals ----------------------------------
-- Covers: LARC HotelBIS (quarterly + annual)
--         CoStar STR AnalyticExport (monthly rolling-12)
--         Green Street Baseline Forecast + scenario sheets (quarterly)
--         Green Street Baseline Fundamentals (annual historical)
--         Green Street Asset Values (quarterly cap rates)

CREATE TABLE IF NOT EXISTS forecast_periods (
    id                           INTEGER PRIMARY KEY,
    publication_id               TEXT NOT NULL REFERENCES publications(publication_id) ON DELETE CASCADE,
    market_id                    INTEGER NOT NULL REFERENCES markets(market_id),
    submarket_id                 INTEGER REFERENCES submarkets(submarket_id),

    -- Time dimension
    year                         INTEGER NOT NULL,
    quarter                      INTEGER,           -- NULL for annual or rolling-12
    month                        INTEGER,           -- populated for CoStar monthly data
    period_type                  TEXT NOT NULL,     -- 'quarterly' | 'annual' | 'rolling_12mo'
    is_forecast                  INTEGER NOT NULL,  -- 0=actual, 1=forecast

    -- Scenario (Green Street only; NULL for actuals and LARC/CoStar)
    scenario                     TEXT,
    -- NULL | 'baseline' | 'strong_growth' | 'moderate_recession'
    -- | 'protracted_slump' | 'near_term_strong'

    -- Hotel fundamentals (LARC HotelBIS + CoStar STR)
    supply                       REAL,              -- room nights
    supply_growth_pct            REAL,              -- decimal: 0.013 = 1.3%
    supply_index                 REAL,              -- Green Street supply index
    demand                       REAL,              -- room nights
    demand_growth_pct            REAL,
    occupancy                    REAL,              -- decimal: 0.743 = 74.3%
    occupancy_growth_pct         REAL,
    adr                          REAL,              -- USD
    adr_growth_pct               REAL,
    revpar                       REAL,              -- USD
    revpar_growth_pct            REAL,
    revenues                     REAL,              -- USD

    -- LARC-specific expense metrics
    wage_growth_pct              REAL,
    property_tax_growth_pct      REAL,
    expense_growth_pct           REAL,

    -- EBITDA / NOI
    hotel_ebitda                 REAL,              -- USD (LARC label)
    hotel_ebitda_margin          REAL,              -- decimal
    hotel_ebitda_growth_pct      REAL,
    hotel_ebitda_margin_chg_bps  REAL,
    noi_index                    REAL,              -- Green Street NOI index
    noi_growth_pct               REAL,              -- Green Street NOI growth
    ncf_growth_pct               REAL,              -- Green Street Net Cash Flow growth

    -- Investment / valuation
    cap_rate                     REAL,              -- decimal
    cap_rate_change_bps          REAL,
    hotel_value_index_2019       REAL,              -- LARC: indexed to 2019=100
    hotel_value_change_pct       REAL,
    cppi_index                   REAL,              -- Green Street Commercial Property Price Index

    -- Green Street effective rent (their RevPAR proxy)
    effective_rent               REAL,
    effective_rent_growth_pct    REAL,
    m_revpaf_growth_pct          REAL,             -- Green Street M-RevPAF Growth

    source_notes                 TEXT,

    UNIQUE(publication_id, market_id, submarket_id, year, quarter, month, period_type, scenario)
);
CREATE INDEX IF NOT EXISTS idx_fp_market_year   ON forecast_periods(market_id, year, quarter);
CREATE INDEX IF NOT EXISTS idx_fp_pub           ON forecast_periods(publication_id);
CREATE INDEX IF NOT EXISTS idx_fp_scenario      ON forecast_periods(scenario, market_id, year);

-- ----- Convention center bookings (LARC CC BIS Data sheet) ------------------

CREATE TABLE IF NOT EXISTS convention_bookings (
    id                     INTEGER PRIMARY KEY,
    publication_id         TEXT NOT NULL REFERENCES publications(publication_id) ON DELETE CASCADE,
    market_id              INTEGER NOT NULL REFERENCES markets(market_id),
    year                   INTEGER NOT NULL,
    period                 TEXT NOT NULL,      -- 'A'=annual, '1'–'4'=quarter
    definite_room_nights   INTEGER,
    yoy_booking_pace       REAL,               -- decimal; NULL if not available
    pace_relative_to_2019  REAL,               -- decimal; NULL if not available
    UNIQUE(publication_id, market_id, year, period)
);
CREATE INDEX IF NOT EXISTS idx_conv_market_year ON convention_bookings(market_id, year);

-- ----- Narratives (LARC + CoStar PDFs) --------------------------------------

CREATE TABLE IF NOT EXISTS narratives (
    id               INTEGER PRIMARY KEY,
    publication_id   TEXT NOT NULL REFERENCES publications(publication_id) ON DELETE CASCADE,
    market_id        INTEGER NOT NULL REFERENCES markets(market_id),
    submarket_id     INTEGER REFERENCES submarkets(submarket_id),
    section          TEXT NOT NULL,     -- canonical section enum (see 03_CANONICAL_FIELDS.md)
    subsection       TEXT,
    text             TEXT NOT NULL,
    ordinal          INTEGER NOT NULL DEFAULT 0,
    page_refs        TEXT,              -- JSON array of page numbers
    entities_json    TEXT,             -- JSON: [{type, value, normalized_value}]
    key_metrics_json TEXT,             -- JSON: [{metric, value, unit, ranking}]
    sentiment        TEXT              -- 'positive' | 'neutral' | 'negative' | NULL
);
CREATE INDEX IF NOT EXISTS idx_narr_market_section ON narratives(market_id, section);
CREATE INDEX IF NOT EXISTS idx_narr_pub            ON narratives(publication_id);
CREATE VIRTUAL TABLE IF NOT EXISTS narratives_fts USING fts5(
    text, section, content='narratives', content_rowid='id'
);

-- ----- Transactions (LARC PDF: Recent Transactions section) -----------------

CREATE TABLE IF NOT EXISTS transactions (
    id                 INTEGER PRIMARY KEY,
    publication_id     TEXT NOT NULL REFERENCES publications(publication_id) ON DELETE CASCADE,
    market_id          INTEGER NOT NULL REFERENCES markets(market_id),
    property_name      TEXT NOT NULL,
    sale_date          TEXT,           -- as written in the report
    sale_date_iso      TEXT,           -- ISO date
    submarket          TEXT,
    units              INTEGER,
    price_total_usd    REAL,
    price_per_unit_usd REAL,
    buyer              TEXT,
    seller             TEXT,
    notes              TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_market_date ON transactions(market_id, sale_date_iso DESC);

-- ----- Supply pipeline (LARC PDF: Lodging Supply section) -------------------

CREATE TABLE IF NOT EXISTS supply_pipeline (
    id                         INTEGER PRIMARY KEY,
    publication_id             TEXT NOT NULL REFERENCES publications(publication_id) ON DELETE CASCADE,
    market_id                  INTEGER NOT NULL REFERENCES markets(market_id),
    hotel_name                 TEXT NOT NULL,
    submarket                  TEXT,
    rooms                      INTEGER,
    development_phase          TEXT NOT NULL,  -- 'planning' | 'under_construction' | 'recently_opened'
    projected_opening          TEXT,           -- as written
    projected_opening_date_iso TEXT,
    brand_family               TEXT,
    scale                      TEXT,
    source                     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipe_market_phase ON supply_pipeline(market_id, development_phase);

-- ----- Green Street: Market Grades ------------------------------------------

CREATE TABLE IF NOT EXISTS green_street_grades (
    id                        INTEGER PRIMARY KEY,
    publication_id            TEXT NOT NULL REFERENCES publications(publication_id) ON DELETE CASCADE,
    market_id                 INTEGER NOT NULL REFERENCES markets(market_id),
    gs_market_id              INTEGER,          -- Green Street's internal Market ID
    tourism_score             INTEGER,
    business_orientation_score INTEGER,
    str_regulation_score      INTEGER,
    supply_barriers_score     INTEGER,
    desirability_score        INTEGER,
    business_friendliness_score INTEGER,
    human_capital_score       INTEGER,
    median_hhi                REAL,
    college_degree_pct        REAL,
    university_score          INTEGER,
    climate_event_risk_score  INTEGER,
    analyst_adjustment        TEXT,
    fiscal_health_score       INTEGER,
    UNIQUE(publication_id, market_id)
);

-- ----- Green Street: Risk-Adjusted IRRs -------------------------------------

CREATE TABLE IF NOT EXISTS green_street_irr (
    id                  INTEGER PRIMARY KEY,
    publication_id      TEXT NOT NULL REFERENCES publications(publication_id) ON DELETE CASCADE,
    market_id           INTEGER NOT NULL REFERENCES markets(market_id),
    gs_market_id        INTEGER,
    nominal_cap_rate    REAL,
    capex_pct           REAL,
    economic_cap_rate   REAL,
    intermediate_noi_growth REAL,
    long_term_noi_growth REAL,
    unlevered_irr       REAL,
    risk_adjustment     REAL,
    risk_adjusted_irr   REAL,
    UNIQUE(publication_id, market_id)
);

-- ----- Green Street: Submarket and Zip Cap Rates ----------------------------

CREATE TABLE IF NOT EXISTS gs_submarket_cap_rates (
    id                        INTEGER PRIMARY KEY,
    publication_id            TEXT NOT NULL REFERENCES publications(publication_id) ON DELETE CASCADE,
    market_id                 INTEGER NOT NULL REFERENCES markets(market_id),
    gs_market_id              INTEGER,
    gs_submarket_id           INTEGER,
    submarket_name            TEXT,
    zip_code                  TEXT,
    cap_rate_market           REAL,
    cap_rate_submarket        REAL,
    cap_rate_zip              REAL
);
CREATE INDEX IF NOT EXISTS idx_gs_cap_market ON gs_submarket_cap_rates(market_id);

-- ----- AI-generated summaries -----------------------------------------------

CREATE TABLE IF NOT EXISTS summaries (
    id                      INTEGER PRIMARY KEY,
    market_id               INTEGER NOT NULL REFERENCES markets(market_id),
    quarter                 TEXT NOT NULL,      -- '1Q26', '2Q25', etc.
    version_type            TEXT NOT NULL,      -- 'detailed' | 'short'
    generation_number       INTEGER NOT NULL DEFAULT 1,
    generated_text          TEXT NOT NULL,      -- AI output (never overwritten)
    edited_text             TEXT,               -- hand-edited version; NULL if untouched
    prompt_version          TEXT NOT NULL,      -- 'summary_v1'
    model_used              TEXT NOT NULL,
    generated_at            TEXT NOT NULL,      -- ISO timestamp
    data_coverage_json      TEXT,               -- snapshot of what data existed at generation time
    source_record_ids_json  TEXT,               -- JSON: {forecast_periods:[1,2,3], convention_bookings:[4]}
    submarket_id            INTEGER REFERENCES submarkets(submarket_id),
    UNIQUE(market_id, submarket_id, quarter, version_type, generation_number)
);
CREATE INDEX IF NOT EXISTS idx_summ_market_quarter ON summaries(market_id, quarter);
CREATE INDEX IF NOT EXISTS idx_summ_submarket ON summaries(submarket_id) WHERE submarket_id IS NOT NULL;

-- Citation map: links [obs:TABLE:ID] tokens to source records
CREATE TABLE IF NOT EXISTS summary_citations (
    id             INTEGER PRIMARY KEY,
    summary_id     INTEGER NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
    citation_token TEXT NOT NULL,     -- '[obs:forecast_periods:123]'
    source_table   TEXT NOT NULL,     -- 'forecast_periods' | 'convention_bookings' | ...
    source_id      INTEGER NOT NULL,
    char_start     INTEGER,           -- character offset in generated_text
    char_end       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cite_summary ON summary_citations(summary_id);

-- ----- Operational tables ---------------------------------------------------

CREATE TABLE IF NOT EXISTS ingest_log (
    id              INTEGER PRIMARY KEY,
    seen_at         TEXT NOT NULL DEFAULT (datetime('now')),
    source_filename TEXT NOT NULL,
    source_sha256   TEXT,
    provider_code   TEXT,
    status          TEXT NOT NULL,
    -- 'queued' | 'extracting' | 'validating' | 'loaded' | 'duplicate' | 'error'
    publication_id  TEXT,
    records_loaded  INTEGER,
    duration_ms     INTEGER,
    error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_status ON ingest_log(status, seen_at DESC);

CREATE TABLE IF NOT EXISTS validation_warnings (
    id             INTEGER PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(publication_id) ON DELETE CASCADE,
    severity       TEXT NOT NULL,   -- 'info' | 'warn' | 'error'
    rule           TEXT NOT NULL,
    message        TEXT NOT NULL,
    context_json   TEXT,
    resolved       INTEGER DEFAULT 0
);

-- ----- Submarket inventory (CoStar headline KPIs) --------------------------
-- Captures the structural facts that anchor the typical user opener:
-- "Denver CBD comprises 82 hotels with ~15,000 rooms, with 64% in Luxury / Upper Upscale".
-- Populated by a one-shot extractor over CoStar PDFs (extract_inventory.py).
-- One row per submarket; UPDATE on re-extraction.
CREATE TABLE IF NOT EXISTS submarket_inventory (
    id                  INTEGER PRIMARY KEY,
    submarket_id        INTEGER NOT NULL REFERENCES submarkets(submarket_id),
    publication_id      TEXT REFERENCES publications(publication_id),
    hotel_count         INTEGER,
    room_count          INTEGER,
    luxury_upper_upscale_pct REAL,    -- combined % of inventory in Luxury+Upper Upscale
    segment_mix_json    TEXT,          -- {"Luxury": 0.10, "Upper Upscale": 0.54, ...}
    extracted_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(submarket_id)
);

-- ----- LLM usage tracking --------------------------------------------------
-- One row per Claude API call made by the ingestion or summarization layers.
-- Lets us answer "how many tokens did this ingestion burn?", "what's our cache
-- hit rate?", and supports per-ingestion budget caps.
CREATE TABLE IF NOT EXISTS llm_usage (
    id                              INTEGER PRIMARY KEY,
    timestamp                       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    publication_id                  TEXT,         -- nullable: not all calls tied to a pub
    purpose                         TEXT NOT NULL,
    -- e.g. 'larc_narrative_section', 'larc_narrative_transactions',
    --      'costar_narrative_section', 'summarize_market_period'
    model                           TEXT NOT NULL,
    input_tokens                    INTEGER NOT NULL DEFAULT 0,
    output_tokens                   INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens         INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd              REAL,
    duration_ms                     INTEGER,
    request_id                      TEXT,         -- Anthropic _request_id
    error_message                   TEXT,         -- if call failed
    metadata_json                   TEXT          -- arbitrary per-call context
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_pub      ON llm_usage(publication_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_timestamp ON llm_usage(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_purpose  ON llm_usage(purpose);

-- ----- Convenience views ----------------------------------------------------

-- Latest publication per (provider, market, doc_type)
CREATE VIEW IF NOT EXISTS v_latest_publications AS
SELECT p.*
FROM publications p
INNER JOIN (
    SELECT provider_code, market_id, doc_type, MAX(publication_date) AS max_date
    FROM publications
    GROUP BY provider_code, market_id, doc_type
) latest
  ON p.provider_code   = latest.provider_code
 AND p.market_id       = latest.market_id
 AND p.doc_type        = latest.doc_type
 AND p.publication_date = latest.max_date;

-- Annual forecast view — provider-agnostic, baseline/actuals only
CREATE VIEW IF NOT EXISTS v_annual_forecast AS
SELECT
    p.provider_code,
    p.publication_date,
    m.canonical_name AS market,
    fp.year,
    fp.is_forecast,
    fp.scenario,
    fp.occupancy,
    fp.adr,
    fp.revpar,
    fp.hotel_ebitda,
    fp.hotel_ebitda_margin,
    fp.cap_rate,
    fp.hotel_value_index_2019,
    fp.effective_rent,
    fp.m_revpaf_growth_pct
FROM forecast_periods fp
JOIN publications p ON fp.publication_id = p.publication_id
JOIN markets m      ON fp.market_id      = m.market_id
WHERE fp.period_type = 'annual'
  AND (fp.scenario IS NULL OR fp.scenario = 'baseline');

-- Per-market × doc_type coverage, derived from fact tables (works for both
-- multi-market and single-market publications)
CREATE VIEW IF NOT EXISTS v_market_doctype_coverage AS
-- forecast_periods covers: larc_hotelbis, costar_str, greenstreet
SELECT
    fp.market_id,
    p.doc_type,
    MAX(p.publication_date) AS latest_date,
    COUNT(*)                 AS row_count
FROM forecast_periods fp
JOIN publications p ON fp.publication_id = p.publication_id
GROUP BY fp.market_id, p.doc_type
UNION ALL
-- convention_bookings: larc_convention only
SELECT cb.market_id, p.doc_type, MAX(p.publication_date), COUNT(*)
FROM convention_bookings cb
JOIN publications p ON cb.publication_id = p.publication_id
GROUP BY cb.market_id, p.doc_type
UNION ALL
-- narratives: larc_narrative + costar_narrative
SELECT n.market_id, p.doc_type, MAX(p.publication_date), COUNT(*)
FROM narratives n
JOIN publications p ON n.publication_id = p.publication_id
GROUP BY n.market_id, p.doc_type
UNION ALL
-- gs_grades / gs_irr / gs_submarket_cap_rates → all 'greenstreet' doc_type
SELECT g.market_id, p.doc_type, MAX(p.publication_date), COUNT(*)
FROM green_street_grades g
JOIN publications p ON g.publication_id = p.publication_id
GROUP BY g.market_id, p.doc_type
UNION ALL
SELECT g.market_id, p.doc_type, MAX(p.publication_date), COUNT(*)
FROM green_street_irr g
JOIN publications p ON g.publication_id = p.publication_id
GROUP BY g.market_id, p.doc_type
UNION ALL
SELECT g.market_id, p.doc_type, MAX(p.publication_date), COUNT(*)
FROM gs_submarket_cap_rates g
JOIN publications p ON g.publication_id = p.publication_id
GROUP BY g.market_id, p.doc_type;

-- Wide coverage view (one row per market). Drives UI availability panel.
CREATE VIEW IF NOT EXISTS v_market_coverage AS
SELECT
    m.market_id,
    m.canonical_name,
    m.state,
    MAX(CASE WHEN c.doc_type = 'larc_hotelbis'    THEN c.latest_date END) AS larc_hotelbis_latest,
    MAX(CASE WHEN c.doc_type = 'larc_convention'  THEN c.latest_date END) AS larc_convention_latest,
    MAX(CASE WHEN c.doc_type = 'larc_narrative'   THEN c.latest_date END) AS larc_narrative_latest,
    MAX(CASE WHEN c.doc_type = 'costar_str'       THEN c.latest_date END) AS costar_str_latest,
    MAX(CASE WHEN c.doc_type = 'costar_narrative' THEN c.latest_date END) AS costar_narrative_latest,
    MAX(CASE WHEN c.doc_type = 'greenstreet'      THEN c.latest_date END) AS greenstreet_latest,
    (SELECT COUNT(*) FROM forecast_periods    fp WHERE fp.market_id = m.market_id) AS forecast_period_rows,
    (SELECT COUNT(*) FROM convention_bookings cb WHERE cb.market_id = m.market_id) AS convention_rows,
    (SELECT COUNT(*) FROM narratives          n  WHERE n.market_id  = m.market_id) AS narrative_rows,
    (SELECT COUNT(*) FROM summaries           s  WHERE s.market_id  = m.market_id) AS summary_count
FROM markets m
LEFT JOIN v_market_doctype_coverage c ON c.market_id = m.market_id
GROUP BY m.market_id, m.canonical_name, m.state;
