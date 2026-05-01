"""LARC HotelBIS adapter — rolling-12-month + annual hotel fundamentals.

Source: single-sheet Excel.
Columns: Market | Published | Year | Period | Supply | Demand | Occupancy |
         ADR | RevPAR | Revenues | Wage Growth | Property Tax Growth |
         Hotel EBITDA Margin | Hotel EBITDA | Cap Rate |
         Hotel Value (Indexed to 2019)

Period encodes 1–4 for quarter-ending trailing-12-month snapshots, "A" for annual.
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


class HotelBISAdapter(BaseAdapter):
    PROVIDER_CODE = "LARC"
    DOC_TYPE = "larc_hotelbis"
    EXTRACTOR_VERSION = "0.1.0"

    REQUIRED_COLUMNS = {"Market", "Published", "Year", "Period"}
    METRIC_MAP: dict[str, str] = {
        # Excel column → forecast_periods column
        "Supply":                              "supply",
        "Demand":                              "demand",
        "Occupancy":                           "occupancy",
        "ADR":                                 "adr",
        "RevPAR":                              "revpar",
        "Revenues":                            "revenues",
        "Wage Growth":                         "wage_growth_pct",
        "Property Tax Growth":                 "property_tax_growth_pct",
        "Hotel EBITDA Margin":                 "hotel_ebitda_margin",
        "Hotel EBITDA":                        "hotel_ebitda",
        "Cap Rate":                            "cap_rate",
        "Hotel Value (Indexed to 2019)":       "hotel_value_index_2019",
    }
    # Columns where values are decimal rates (must pass through to_decimal_pct)
    PERCENT_COLUMNS = {
        "Occupancy",
        "Wage Growth",
        "Property Tax Growth",
        "Hotel EBITDA Margin",
        "Cap Rate",
    }

    def parse(self, file_path: Path | str, pub: PublicationInfo) -> ParseResult:
        df = pd.read_excel(file_path, sheet_name=0)
        df.columns = [str(c).strip() for c in df.columns]

        missing = self.REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise AdapterError(
                f"HotelBIS file missing required columns: {sorted(missing)}"
            )

        # Authoritative publication date comes from the file's Published column
        # (overrides router's filename guess). Take the most-common value across
        # rows in case of mixed dates.
        published_iso = self._derive_publication_date(df)
        if published_iso:
            pub.publication_date = published_iso
            # Update publication_period from the date
            y = int(published_iso[:4])
            m = int(published_iso[5:7])
            q = (m - 1) // 3 + 1
            pub.publication_period = f"{q}Q{y % 100:02d}"

        # Determine pub_year/pub_quarter from publication_period or publication_date
        pub_year, pub_quarter = self._publication_anchor(pub)

        result = ParseResult()
        unresolved: dict[str, int] = {}

        for idx, row in df.iterrows():
            try:
                rec = self._row_to_record(row, pub_year, pub_quarter)
            except UnknownMarketError as e:
                # Aggregate unresolved markets so we don't spam logs.
                key = str(row.get("Market", "")).strip() or "<empty>"
                unresolved[key] = unresolved.get(key, 0) + 1
                continue
            except _SkipRow as e:
                self.log_warning(f"row {idx}: {e}")
                continue
            if rec is not None:
                result.forecast_periods.append(rec)

        if unresolved:
            for name, count in unresolved.items():
                self.log_warning(
                    f"market '{name}' could not be resolved ({count} rows skipped)"
                )

        result.warnings = list(self.warnings)
        return result

    # ------------------------------------------------------------------

    def _derive_publication_date(self, df: pd.DataFrame) -> str | None:
        """Find the most common Published value, normalize to ISO YYYY-MM-01."""
        if "Published" not in df.columns:
            return None
        counts: dict[str, int] = {}
        for v in df["Published"].dropna():
            iso = self._normalize_published(v)
            if iso:
                counts[iso] = counts.get(iso, 0) + 1
        if not counts:
            return None
        # Pick the most common
        return max(counts.items(), key=lambda kv: kv[1])[0]

    @staticmethod
    def _normalize_published(value) -> str | None:
        """Accept '2026-03', '2026-03-01', '3/1/2026', datetime objects, etc."""
        if value is None:
            return None
        if hasattr(value, "year") and hasattr(value, "month"):
            return f"{value.year:04d}-{value.month:02d}-01"
        s = str(value).strip()
        # 'YYYY-MM' or 'YYYY-MM-DD'
        m = pd.Series([s]).str.match(r"^\d{4}-\d{2}").iloc[0]
        if m:
            parts = s.split("-")
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-01"
        # try generic parse
        try:
            ts = pd.to_datetime(s, errors="coerce")
            if pd.notna(ts):
                return f"{ts.year:04d}-{ts.month:02d}-01"
        except Exception:
            pass
        return None

    def _publication_anchor(self, pub: PublicationInfo) -> tuple[int, int]:
        """Return (pub_year, pub_quarter) used to flag is_forecast."""
        # Prefer publication_period like "1Q26", "4Q25"
        if pub.publication_period:
            p = pub.publication_period.upper().replace(" ", "")
            try:
                if "Q" in p:
                    q_str, y_str = p.split("Q")
                    q = int(q_str)
                    y = int(y_str)
                    if y < 100:
                        y += 2000
                    return y, q
            except ValueError:
                pass
        # Fallback: parse publication_date as ISO
        pd_iso = pub.publication_date  # 'YYYY-MM-DD'
        y = int(pd_iso[:4])
        m = int(pd_iso[5:7])
        q = (m - 1) // 3 + 1
        return y, q

    def _row_to_record(
        self, row: pd.Series, pub_year: int, pub_quarter: int
    ) -> dict | None:
        raw_market = row.get("Market")
        if raw_market is None or str(raw_market).strip() == "":
            raise _SkipRow("empty Market")
        market_id = self.resolve_market(str(raw_market))

        year = self.to_int(row.get("Year"))
        if year is None:
            raise _SkipRow("missing Year")

        period_raw = row.get("Period")
        period_str = str(period_raw).strip().upper() if period_raw is not None else ""
        if period_str in ("1", "2", "3", "4"):
            quarter = int(period_str)
            period_type = "rolling_12mo"
            is_forecast = (year, quarter) > (pub_year, pub_quarter)
        elif period_str in ("A", "ANNUAL", "Y", "YR", ""):
            quarter = None
            period_type = "annual"
            is_forecast = year > pub_year
        else:
            # Sometimes Period comes through as int 1.0
            try:
                q = int(float(period_str))
                if 1 <= q <= 4:
                    quarter = q
                    period_type = "rolling_12mo"
                    is_forecast = (year, quarter) > (pub_year, pub_quarter)
                else:
                    raise _SkipRow(f"unknown Period '{period_raw}'")
            except (TypeError, ValueError):
                raise _SkipRow(f"unknown Period '{period_raw}'")

        rec: dict = {
            "market_id":   market_id,
            "year":        year,
            "quarter":     quarter,
            "month":       None,
            "period_type": period_type,
            "is_forecast": 1 if is_forecast else 0,
            "scenario":    None,
        }

        for excel_col, db_col in self.METRIC_MAP.items():
            v = row.get(excel_col)
            if excel_col in self.PERCENT_COLUMNS:
                rec[db_col] = self.to_decimal_pct(v)
            else:
                rec[db_col] = self.to_float(v)

        return rec


class _SkipRow(Exception):
    """Internal: signals a row should be skipped, with a logged reason."""
