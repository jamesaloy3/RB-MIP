# Adapter Contract

Every adapter translates a raw source file into a `ParseResult` — a typed container of records ready for the loader to write to the database. Adapters never touch the database themselves.

---

## Interface

```python
from adapters.base import BaseAdapter, ParseResult

class MyProviderAdapter(BaseAdapter):

    PROVIDER_CODE = "MyProvider"   # must match providers table
    DOC_TYPE      = "my_doc_type"  # must match publications.doc_type enum

    def parse(self, file_path: str, publication_id: str, market_id: int | None) -> ParseResult:
        """
        Read file_path, return ParseResult.
        Raise AdapterError on unrecoverable parse failures.
        Log warnings (not raise) for soft failures (missing columns, sparse rows).
        """
        ...

    def metadata(self) -> dict:
        """
        Return a description of what this adapter produces.
        Used for documentation and the /api/admin/adapters endpoint.
        """
        return {
            "provider": self.PROVIDER_CODE,
            "doc_type": self.DOC_TYPE,
            "destination_tables": ["forecast_periods"],
            "markets": "all",      # or list of specific markets
            "period_types": ["quarterly", "annual"],
            "notes": "...",
        }
```

---

## ParseResult

```python
@dataclass
class ParseResult:
    # One dict per row; keys match column names in destination table.
    # Do NOT include publication_id or market_id — loader injects these.
    forecast_periods:     list[dict] = field(default_factory=list)
    convention_bookings:  list[dict] = field(default_factory=list)
    narratives:           list[dict] = field(default_factory=list)
    transactions:         list[dict] = field(default_factory=list)
    supply_pipeline:      list[dict] = field(default_factory=list)
    green_street_grades:  list[dict] = field(default_factory=list)
    green_street_irr:     list[dict] = field(default_factory=list)
    gs_submarket_cap_rates: list[dict] = field(default_factory=list)
    warnings:             list[str]  = field(default_factory=list)
```

The loader writes each non-empty list to its table in a single transaction. If any list fails validation, the transaction rolls back and the ingest_log entry is marked `error`.

---

## Adapter Registry

| Adapter module | Provider | doc_type | Source file | Destination tables |
|---|---|---|---|---|
| `larc/hotelbis.py` | LARC | `larc_hotelbis` | HotelBIS `.xlsx` — single sheet, flat rows | `forecast_periods` |
| `larc/convention.py` | LARC | `larc_convention` | Aggregated Convention Center `.xlsx` — `CC BIS Data` sheet | `convention_bookings` |
| `larc/narrative.py` | LARC | `larc_narrative` | LARC market intelligence PDF | `narratives`, `transactions`, `supply_pipeline` |
| `costar/str_data.py` | CoStar | `costar_str` | AnalyticExport `.xlsx` — `AnalyticExport` sheet | `forecast_periods` |
| `costar/narrative.py` | CoStar | `costar_narrative` | Submarket intelligence PDF | `narratives` |
| `greenstreet/fundamentals.py` | GreenStreet | `greenstreet` | Fundamentals and Valuation `.xlsx` — 10 sheets | `forecast_periods`, `green_street_grades`, `green_street_irr`, `gs_submarket_cap_rates` |

---

## Rules

1. **Nulls, not zeros.** Missing data is `None`, never `0`. A cell containing "N/A", empty string, or whitespace → `None`.

2. **Decimals, not percents.** All rates stored as decimals. `74.4%` → `0.744`. `1.3%` → `0.013`. Apply conversion in the adapter, not the loader. Check source units — CoStar may report occupancy as `74.4` (needing `/100`) while LARC may already store `0.744`.

3. **ISO dates.** All dates stored as `YYYY-MM-DD` strings. Conversions:
   - `"Dec-25"` → `"2025-12-01"`
   - `"1Q26"` → `"2026-01-01"`
   - `"2026-03"` → `"2026-03-01"`
   - `3/31/2005` (Green Street) → `"2005-03-31"`

4. **USD, not thousands.** If source column is `Price (000)`, multiply by 1000 in the adapter.

5. **Market name → canonical.** Adapters must NOT hard-code canonical market names. Instead, emit the raw market name string in a `raw_market_name` key and let the loader resolve it via `market_aliases`. If resolution fails, the loader raises `UnknownMarketError` and the file moves to `_errors/`.

6. **Idempotency.** The loader handles deduplication at the `publication_id` level (DELETE + INSERT). Adapters do not need to check for existing records.

7. **Logging.** Call `self.log_warning(message)` for soft issues (skipped rows, missing optional columns). These accumulate in `ParseResult.warnings` and land in `validation_warnings`. Raise `AdapterError` only for hard failures where no useful output can be produced.

8. **# UNVERIFIED marker.** If an adapter was written without a real sample file, add `# UNVERIFIED — needs real sample` at the top of the file and list the assumptions made. Remove once verified against real data.

---

## LARC HotelBIS adapter notes

Source: single sheet, flat table. Columns:
`Market | Published | Year | Period | Supply | Demand | Occupancy | ADR | RevPAR | Revenues | Wage Growth | Property Tax Growth | Hotel EBITDA Margin | Hotel EBITDA | Cap Rate | Hotel Value (Indexed to 2019)`

- `Period` is `1`–`4` for quarters, `A` for annual. Map: `{'1':1, '2':2, '3':3, '4':4, 'A':None}` for the `quarter` column; `period_type` = `'quarterly'` or `'annual'`.
- `Published` column (e.g. `2026-03`) is the `publication_date`. The file covers all markets; emit one publication record per unique (Market, Published) pair.
- `is_forecast`: any period where `Year > current_year` OR where LARC convention says "F" suffix — check column for mixed actuals/forecasts.
- `Hotel Value (Indexed to 2019)` → `hotel_value_index_2019`; frequently NULL in earlier years.

## LARC Convention adapter notes

Source: `CC BIS Data` sheet (skip Sheet1 which is a presentation pivot).
Columns: `Market | Published | Year | Period | Definite Room Nights | YoY Booking Pace | Pace Relative to 2019`

- `Period` is `A` for annual, or `1`–`4` for quarter.
- `YoY Booking Pace` and `Pace Relative to 2019` are frequently NULL — store as NULL, not 0.

## CoStar STR adapter notes

Source: `AnalyticExport` sheet.
Key columns: `Period | Geography Name | Market | Submarket | 12 Mo ADR | 12 Mo Occupancy | 12 Mo Demand | 12 Mo Inventory Growth | ...`

- `Period` is `"Apr 2000"` style → parse to year + month; `period_type = 'rolling_12mo'`.
- `Geography Name` is compound: `"Austin - TX USA - Austin CBD"` → split on ` - ` for market + submarket.
- Submarket must be resolved via `submarket_aliases` before loading.
- Many metric columns are blank for early periods — store as NULL.

## Green Street adapter notes

Source: 10-sheet workbook. All sheets have a title block in rows 1–3; actual headers at row 4 (forecast/scenario sheets: row 5). Use `pd.read_excel(..., header=N, skiprows=...)` accordingly.

**Forecast / scenario sheets** (`Baseline Forecast`, `Exceptionally Strong Growth`, etc.):
- Columns: `Date | Scenario | Market | Demand Growth | Occupancy | Effective Rent | Effective Rent Growth | M-RevPAF Growth | NOI Index | NOI Growth | Nominal Cap Rate | Supply Growth | Supply Index | NCF Growth | CPPI`
- `Date` is `3/31/2005` style → ISO date; derive `year` and `quarter` from the month.
- `Scenario` column value in the data row determines `scenario` field.
- `period_type = 'quarterly'`; infer `is_forecast` from whether date > publication_date.

**Baseline Fundamentals** (annual historical, wide format):
- Row structure: metric-name rows (e.g. "U.S. M-RevPAF Growth") followed by market data rows. Identify market rows by non-empty Market ID column (col B) and text market name (col C).
- Year columns start at col D. Transpose to long form: one row per (market, year, metric).
- `period_type = 'annual'`, `scenario = NULL` (historical).

**Asset Values** (quarterly cap rates, wide format):
- Similar wide format: dates as column headers, market rows. Multiple metric sections separated by blank rows.
- Parse section header rows to identify which metric applies to the block below.

**Market Grades**, **Risk-Adjusted IRRs**, **Submarket and Zip Cap Rates**: standard tabular, header at row 4. Each → their respective dedicated tables.
