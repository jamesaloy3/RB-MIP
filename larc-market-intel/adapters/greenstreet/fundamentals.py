"""Green Street Fundamentals & Valuation Data adapter.

Source: 10-sheet xlsx. This adapter handles 6 sheets:
  - Baseline Forecast            -> forecast_periods (scenario='baseline')
  - Exceptionally Strong Growth  -> forecast_periods (scenario='strong_growth')
  - Moderate Recession           -> forecast_periods (scenario='moderate_recession')
  - Protracted Slump             -> forecast_periods (scenario='protracted_slump')
  - Stronger Near-Term Growth    -> forecast_periods (scenario='near_term_strong')
  - Market Grades                -> green_street_grades
  - Risk-Adjusted IRRs           -> green_street_irr
  - Submarket and Zip Cap Rates  -> gs_submarket_cap_rates

Skipped (TODO — wide multi-section format):
  - Baseline Fundamentals (annual historical, wide)
  - Asset Values         (quarterly cap rates, wide)

Publication date: Green Street files don't carry a date column. Falls back to
the router's value (filename or file mtime).
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from adapters.base import (
    AdapterError,
    BaseAdapter,
    ParseResult,
    PublicationInfo,
    UnknownMarketError,
)


SCENARIO_SHEETS = {
    "Baseline Forecast":             "baseline",
    "Exceptionally Strong Growth":   "strong_growth",
    "Moderate Recession":            "moderate_recession",
    "Protracted Slump":              "protracted_slump",
    "Stronger Near-Term Growth":     "near_term_strong",
}

# Forecast / scenario sheets: header at row 5 (0-indexed = 4)
FORECAST_HEADER = 4
# Other tabular sheets: header at row 4 (0-indexed = 3)
TAB_HEADER = 3

# Forecast sheet column → forecast_periods column
FORECAST_METRIC_MAP: dict[str, str] = {
    "Demand Growth":                              "demand_growth_pct",
    "Occupancy":                                  "occupancy",
    "Effective Rent":                             "effective_rent",
    "Effective Rent Growth":                      "effective_rent_growth_pct",
    "M-RevPAF Growth":                            "m_revpaf_growth_pct",
    "Net Operating Income (NOI) Index":           "noi_index",
    "Net Operating Income (NOI) Growth":          "noi_growth_pct",
    "Nominal Cap Rate":                           "cap_rate",
    "Supply Growth":                              "supply_growth_pct",
    "Supply Index":                               "supply_index",
    "Net Cash Flow (NCF) Growth":                 "ncf_growth_pct",
    "Commercial Property Price Index (CPPI)":     "cppi_index",
}
FORECAST_PERCENT_COLUMNS = {
    "Demand Growth", "Occupancy", "Effective Rent Growth", "M-RevPAF Growth",
    "Net Operating Income (NOI) Growth", "Nominal Cap Rate", "Supply Growth",
    "Net Cash Flow (NCF) Growth",
}


class GreenStreetAdapter(BaseAdapter):
    PROVIDER_CODE = "GreenStreet"
    DOC_TYPE = "greenstreet"
    EXTRACTOR_VERSION = "0.1.0"

    def parse(self, file_path: Path | str, pub: PublicationInfo) -> ParseResult:
        file_path = Path(file_path)
        # Publication date: prefer router-parsed (from filename like 1Q26),
        # else file mtime, else router default (today). The router's parsed
        # publication_period will be non-None only if filename carried a quarter.
        if not pub.publication_period:
            try:
                from datetime import datetime
                mtime = os.path.getmtime(file_path)
                dt = datetime.fromtimestamp(mtime)
                pub.publication_date = f"{dt.year:04d}-{dt.month:02d}-01"
                q = (dt.month - 1) // 3 + 1
                pub.publication_period = f"{q}Q{dt.year % 100:02d}"
                self.log_warning(
                    f"GS file has no date in filename or columns; using file mtime "
                    f"({pub.publication_date}). Rename file to include '1Q26' to override."
                )
            except OSError:
                pass

        # Cache pub anchor for is_forecast comparison in forecast sheets
        self._pub_year = int(pub.publication_date[:4])
        self._pub_quarter = ((int(pub.publication_date[5:7]) - 1) // 3) + 1

        result = ParseResult()
        xl = pd.ExcelFile(file_path)
        sheets = set(xl.sheet_names)

        # 1. Forecast / scenario sheets
        for sheet, scenario in SCENARIO_SHEETS.items():
            if sheet in sheets:
                self._parse_forecast_sheet(xl, sheet, scenario, result)
            else:
                self.log_warning(f"sheet '{sheet}' not present in file")

        # 2. Market Grades
        if "Market Grades" in sheets:
            self._parse_market_grades(xl, result)

        # 3. Risk-Adjusted IRRs
        if "Risk-Adjusted IRRs" in sheets:
            self._parse_risk_adjusted_irrs(xl, result)

        # 4. Submarket and Zip Cap Rates
        if "Submarket and Zip Cap Rates" in sheets:
            self._parse_submarket_cap_rates(xl, result)

        # TODO: Baseline Fundamentals and Asset Values (wide multi-section)

        result.warnings = list(self.warnings)
        return result

    # ------------------------------------------------------------------
    # Forecast / scenario sheets
    # ------------------------------------------------------------------

    def _parse_forecast_sheet(
        self, xl: pd.ExcelFile, sheet_name: str, scenario: str, result: ParseResult
    ) -> None:
        df = pd.read_excel(xl, sheet_name=sheet_name, header=FORECAST_HEADER)
        df.columns = [str(c).strip() for c in df.columns]

        if "Date" not in df.columns or "Market" not in df.columns:
            self.log_warning(f"sheet '{sheet_name}' missing Date or Market column")
            return

        unresolved: dict[str, int] = {}
        n_loaded = 0
        for idx, row in df.iterrows():
            raw_market = row.get("Market")
            if BaseAdapter._is_missing(raw_market):
                continue
            try:
                market_id = self.resolve_market(str(raw_market))
            except UnknownMarketError:
                unresolved[str(raw_market)] = unresolved.get(str(raw_market), 0) + 1
                continue

            date_val = row.get("Date")
            year, quarter = self._date_to_year_quarter(date_val)
            if year is None:
                continue

            is_forecast = (year, quarter) > (self._pub_year, self._pub_quarter)

            rec = {
                "market_id":   market_id,
                "submarket_id": None,
                "year":        year,
                "quarter":     quarter,
                "month":       None,
                "period_type": "quarterly",
                "is_forecast": 1 if is_forecast else 0,
                "scenario":    scenario,
            }
            for excel_col, db_col in FORECAST_METRIC_MAP.items():
                v = row.get(excel_col)
                if excel_col in FORECAST_PERCENT_COLUMNS:
                    rec[db_col] = self.to_decimal_pct(v)
                else:
                    rec[db_col] = self.to_float(v)
            result.forecast_periods.append(rec)
            n_loaded += 1

        if unresolved:
            for name, count in unresolved.items():
                self.log_warning(
                    f"[{sheet_name}] market '{name}' not resolved ({count} rows)"
                )

    @staticmethod
    def _date_to_year_quarter(value) -> tuple[int | None, int | None]:
        if value is None:
            return None, None
        if hasattr(value, "year") and hasattr(value, "month"):
            year = int(value.year)
            quarter = (int(value.month) - 1) // 3 + 1
            return year, quarter
        try:
            ts = pd.to_datetime(str(value), errors="coerce")
            if pd.notna(ts):
                return int(ts.year), (int(ts.month) - 1) // 3 + 1
        except Exception:
            pass
        return None, None

    # ------------------------------------------------------------------
    # Market Grades
    # ------------------------------------------------------------------

    def _parse_market_grades(self, xl: pd.ExcelFile, result: ParseResult) -> None:
        df = pd.read_excel(xl, sheet_name="Market Grades", header=TAB_HEADER)
        df.columns = [str(c).strip() for c in df.columns]
        # Some columns repeat after the right-side block — drop *.1 / Unnamed
        df = df.loc[:, ~df.columns.str.match(r"^(Unnamed:|.+\.1$)")]

        unresolved: dict[str, int] = {}
        for idx, row in df.iterrows():
            raw_market = row.get("Market")
            if BaseAdapter._is_missing(raw_market):
                continue
            try:
                market_id = self.resolve_market(str(raw_market))
            except UnknownMarketError:
                unresolved[str(raw_market)] = unresolved.get(str(raw_market), 0) + 1
                continue

            rec = {
                "market_id":               market_id,
                "gs_market_id":            self.to_int(row.get("Market ID")),
                "tourism_score":           self.to_int(row.get("Tourism")),
                "business_orientation_score": self.to_int(row.get("Business Orientation")),
                "str_regulation_score":    self.to_int(row.get("Short-Term Rental Regulation")),
                "supply_barriers_score":   self.to_int(row.get("Supply Barriers")),
                "desirability_score":      self.to_int(row.get("Desirability")),
                "business_friendliness_score": self.to_int(row.get("Business Friendliness")),
                "human_capital_score":     self.to_int(row.get("Human Capital")),
                "median_hhi":              self.to_float(row.get("Median HHI")),
                "college_degree_pct":      self.to_decimal_pct(row.get("College Degree %")),
                "university_score":        self.to_int(row.get("University Score")),
                "climate_event_risk_score": self.to_int(row.get("Climate Event Risk")),
                "analyst_adjustment":      str(row.get("Analyst Adjustment"))
                                            if not BaseAdapter._is_missing(row.get("Analyst Adjustment"))
                                            else None,
                "fiscal_health_score":     self.to_int(row.get("Fiscal Health")),
            }
            result.green_street_grades.append(rec)

        for name, count in unresolved.items():
            self.log_warning(f"[Market Grades] market '{name}' not resolved ({count})")

    # ------------------------------------------------------------------
    # Risk-Adjusted IRRs
    # ------------------------------------------------------------------

    def _parse_risk_adjusted_irrs(self, xl: pd.ExcelFile, result: ParseResult) -> None:
        df = pd.read_excel(xl, sheet_name="Risk-Adjusted IRRs", header=TAB_HEADER)
        df.columns = [str(c).strip() for c in df.columns]

        unresolved: dict[str, int] = {}
        for idx, row in df.iterrows():
            raw_market = row.get("Market")
            if BaseAdapter._is_missing(raw_market):
                continue
            try:
                market_id = self.resolve_market(str(raw_market))
            except UnknownMarketError:
                unresolved[str(raw_market)] = unresolved.get(str(raw_market), 0) + 1
                continue

            rec = {
                "market_id":               market_id,
                "gs_market_id":            self.to_int(row.get("Market ID")),
                "nominal_cap_rate":        self.to_decimal_pct(row.get("Nominal Cap Rate")),
                "capex_pct":               self.to_decimal_pct(row.get("Cap-Ex")),
                "economic_cap_rate":       self.to_decimal_pct(row.get("Economic Cap Rate")),
                "intermediate_noi_growth": self.to_decimal_pct(row.get("Intermediate-Term NOI Growth")),
                "long_term_noi_growth":    self.to_decimal_pct(row.get("Long-Term NOI Growth")),
                "unlevered_irr":           self.to_decimal_pct(row.get("Unlevered IRR")),
                "risk_adjustment":         self.to_decimal_pct(row.get("Risk Adjustments")),
                "risk_adjusted_irr":       self.to_decimal_pct(row.get("Risk-Adjusted IRR")),
            }
            result.green_street_irr.append(rec)

        for name, count in unresolved.items():
            self.log_warning(f"[IRRs] market '{name}' not resolved ({count})")

    # ------------------------------------------------------------------
    # Submarket and Zip Cap Rates
    # ------------------------------------------------------------------

    def _parse_submarket_cap_rates(self, xl: pd.ExcelFile, result: ParseResult) -> None:
        df = pd.read_excel(xl, sheet_name="Submarket and Zip Cap Rates", header=TAB_HEADER)
        df.columns = [str(c).strip() for c in df.columns]

        unresolved: dict[str, int] = {}
        for idx, row in df.iterrows():
            raw_market = row.get("Market")
            if BaseAdapter._is_missing(raw_market):
                continue
            try:
                market_id = self.resolve_market(str(raw_market))
            except UnknownMarketError:
                unresolved[str(raw_market)] = unresolved.get(str(raw_market), 0) + 1
                continue

            zip_raw = row.get("Zip Code")
            zip_str = None
            if not BaseAdapter._is_missing(zip_raw):
                z = self.to_int(zip_raw)
                zip_str = f"{z:05d}" if z is not None else str(zip_raw).strip()

            rec = {
                "market_id":          market_id,
                "gs_market_id":       self.to_int(row.get("Market ID")),
                "gs_submarket_id":    self.to_int(row.get("Submarket ID")),
                "submarket_name":     str(row.get("Submarket"))
                                       if not BaseAdapter._is_missing(row.get("Submarket"))
                                       else None,
                "zip_code":           zip_str,
                "cap_rate_market":    self.to_decimal_pct(row.get("Nominal Cap Rate - Market")),
                "cap_rate_submarket": self.to_decimal_pct(row.get("Nominal Cap Rate - Submarket")),
                "cap_rate_zip":       self.to_decimal_pct(row.get("Nominal Cap Rate - Zip Code")),
            }
            result.gs_submarket_cap_rates.append(rec)

        if unresolved:
            top = sorted(unresolved.items(), key=lambda kv: -kv[1])[:5]
            self.log_warning(
                f"[Submarket Cap Rates] {len(unresolved)} unresolved markets; "
                f"top: {top}"
            )

