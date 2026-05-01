"""CoStar STR AnalyticExport adapter.

Source: 'AnalyticExport' sheet. Monthly rolling-12-month data per submarket.
Period column is a date (end of month). Last Processed Month tells us the
publication cutoff (anything after = forecast).

Geography Name format: "<Market> - <ST> USA - <Submarket>"
                  or:  "<Market> USA - <Submarket>"  (e.g. "Colorado Area USA")
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from adapters.base import (
    AdapterError,
    BaseAdapter,
    ParseResult,
    PublicationInfo,
    UnknownMarketError,
)


class CoStarSTRAdapter(BaseAdapter):
    PROVIDER_CODE = "CoStar"
    DOC_TYPE = "costar_str"
    EXTRACTOR_VERSION = "0.1.0"
    SHEET_NAME = "AnalyticExport"

    REQUIRED_COLUMNS = {
        "Period", "Market", "Submarket",
        "12 Mo ADR", "12 Mo Occupancy", "12 Mo RevPAR",
    }

    METRIC_MAP: dict[str, str] = {
        "12 Mo ADR":              "adr",
        "12 Mo ADR Chg":          "adr_growth_pct",
        "12 Mo Occupancy":        "occupancy",
        "12 Mo Occupancy Chg":    "occupancy_growth_pct",
        "12 Mo RevPAR":           "revpar",
        "12 Mo RevPAR Chg":       "revpar_growth_pct",
        "12 Mo Demand":           "demand",
        "12 Mo Demand Chg":       "demand_growth_pct",
        "12 Mo Supply":           "supply",
        "12 Mo Supply Chg":       "supply_growth_pct",
        "12 Mo Revenue":          "revenues",
    }
    PERCENT_COLUMNS = {
        "12 Mo ADR Chg", "12 Mo Occupancy", "12 Mo Occupancy Chg",
        "12 Mo RevPAR Chg", "12 Mo Demand Chg", "12 Mo Supply Chg",
    }

    def parse(self, file_path: Path | str, pub: PublicationInfo) -> ParseResult:
        try:
            df = pd.read_excel(file_path, sheet_name=self.SHEET_NAME)
        except ValueError as e:
            raise AdapterError(f"sheet '{self.SHEET_NAME}' not found: {e}")
        df.columns = [str(c).strip() for c in df.columns]

        missing = self.REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise AdapterError(
                f"CoStar STR file missing required columns: {sorted(missing)}"
            )

        # Publication cutoff comes from 'Last Processed Month' (e.g. '3/31/2026')
        cutoff_iso = self._derive_cutoff(df)
        if cutoff_iso:
            pub.publication_date = cutoff_iso
            y = int(cutoff_iso[:4])
            m = int(cutoff_iso[5:7])
            q = (m - 1) // 3 + 1
            pub.publication_period = f"{q}Q{y % 100:02d}"
        cutoff_year, cutoff_month = (int(cutoff_iso[:4]), int(cutoff_iso[5:7])) \
            if cutoff_iso else (None, None)

        result = ParseResult()
        unresolved_markets: dict[str, int] = {}
        unresolved_submarkets: dict[str, int] = {}

        for idx, row in df.iterrows():
            raw_market = row.get("Market")
            raw_submarket = row.get("Submarket")
            if BaseAdapter._is_missing(raw_market):
                continue
            try:
                market_id = self.resolve_market(str(raw_market))
            except UnknownMarketError:
                unresolved_markets[str(raw_market)] = (
                    unresolved_markets.get(str(raw_market), 0) + 1
                )
                continue

            submarket_id = None
            if not BaseAdapter._is_missing(raw_submarket):
                submarket_id = self.resolve_submarket(market_id, str(raw_submarket))

            year, month = self._period_year_month(row.get("Period"))
            if year is None or month is None:
                self.log_warning(f"row {idx}: unparseable Period '{row.get('Period')}'")
                continue

            if cutoff_year is not None:
                is_forecast = (year, month) > (cutoff_year, cutoff_month)
            else:
                is_forecast = False

            rec = {
                "market_id":   market_id,
                "submarket_id": submarket_id,
                "year":        year,
                "quarter":     None,
                "month":       month,
                "period_type": "rolling_12mo",
                "is_forecast": 1 if is_forecast else 0,
                "scenario":    None,
            }
            for excel_col, db_col in self.METRIC_MAP.items():
                v = row.get(excel_col)
                if excel_col in self.PERCENT_COLUMNS:
                    rec[db_col] = self.to_decimal_pct(v)
                else:
                    rec[db_col] = self.to_float(v)

            result.forecast_periods.append(rec)

        for name, count in unresolved_markets.items():
            self.log_warning(f"market '{name}' not resolved ({count} rows skipped)")

        result.warnings = list(self.warnings)
        return result

    @staticmethod
    def _period_year_month(value) -> tuple[int | None, int | None]:
        if value is None:
            return None, None
        if hasattr(value, "year") and hasattr(value, "month"):
            return int(value.year), int(value.month)
        try:
            ts = pd.to_datetime(str(value), errors="coerce")
            if pd.notna(ts):
                return int(ts.year), int(ts.month)
        except Exception:
            pass
        return None, None

    @staticmethod
    def _derive_cutoff(df: pd.DataFrame) -> str | None:
        if "Last Processed Month" not in df.columns:
            return None
        for v in df["Last Processed Month"].dropna().head(5):
            if hasattr(v, "year"):
                return f"{v.year:04d}-{v.month:02d}-01"
            try:
                ts = pd.to_datetime(str(v), errors="coerce")
                if pd.notna(ts):
                    return f"{ts.year:04d}-{ts.month:02d}-01"
            except Exception:
                continue
        return None
