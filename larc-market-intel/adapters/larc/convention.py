"""LARC Aggregated Convention Center Data adapter.

Source: 'CC BIS Data' sheet in the Aggregated Convention Center xlsx.
Columns: Market | Published | Year | Period |
         Definite Room Nights | YoY Booking Pace | Pace Relative to 2019

Period values: 'A' (annual). May include '1'-'4' for quarters in future files.
Pace columns are already decimals (-0.114 = -11.4%) — pass through to_decimal_pct
which is idempotent for values already in decimal range.
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


class ConventionAdapter(BaseAdapter):
    PROVIDER_CODE = "LARC"
    DOC_TYPE = "larc_convention"
    EXTRACTOR_VERSION = "0.1.0"
    SHEET_NAME = "CC BIS Data"
    REQUIRED_COLUMNS = {"Market", "Published", "Year", "Period"}

    def parse(self, file_path: Path | str, pub: PublicationInfo) -> ParseResult:
        try:
            df = pd.read_excel(file_path, sheet_name=self.SHEET_NAME)
        except ValueError as e:
            raise AdapterError(f"sheet '{self.SHEET_NAME}' not found: {e}")
        df.columns = [str(c).strip() for c in df.columns]

        missing = self.REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise AdapterError(
                f"Convention file missing required columns: {sorted(missing)}"
            )

        # Override pub date from the file's Published column (authoritative)
        published_iso = self._derive_publication_date(df)
        if published_iso:
            pub.publication_date = published_iso
            y = int(published_iso[:4])
            m = int(published_iso[5:7])
            q = (m - 1) // 3 + 1
            pub.publication_period = f"{q}Q{y % 100:02d}"

        result = ParseResult()
        unresolved: dict[str, int] = {}

        for idx, row in df.iterrows():
            raw_market = row.get("Market")
            if raw_market is None or str(raw_market).strip() == "":
                continue
            try:
                market_id = self.resolve_market(str(raw_market))
            except UnknownMarketError:
                key = str(raw_market).strip()
                unresolved[key] = unresolved.get(key, 0) + 1
                continue

            year = self.to_int(row.get("Year"))
            if year is None:
                self.log_warning(f"row {idx}: missing Year for {raw_market}")
                continue

            period_raw = row.get("Period")
            period = self._normalize_period(period_raw)
            if period is None:
                self.log_warning(f"row {idx}: unknown Period '{period_raw}'")
                continue

            rec = {
                "market_id":            market_id,
                "year":                 year,
                "period":               period,
                "definite_room_nights": self.to_int(row.get("Definite Room Nights")),
                "yoy_booking_pace":     self.to_decimal_pct(row.get("YoY Booking Pace")),
                "pace_relative_to_2019": self.to_decimal_pct(row.get("Pace Relative to 2019")),
            }
            result.convention_bookings.append(rec)

        for name, count in unresolved.items():
            self.log_warning(f"market '{name}' not resolved ({count} rows skipped)")

        result.warnings = list(self.warnings)
        return result

    @staticmethod
    def _normalize_period(value) -> str | None:
        if value is None:
            return None
        s = str(value).strip().upper()
        if s in ("A", "ANNUAL", "Y", "YR"):
            return "A"
        if s in ("1", "2", "3", "4"):
            return s
        try:
            n = int(float(s))
            if 1 <= n <= 4:
                return str(n)
        except (TypeError, ValueError):
            pass
        return None

    def _derive_publication_date(self, df: pd.DataFrame) -> str | None:
        if "Published" not in df.columns:
            return None
        counts: dict[str, int] = {}
        for v in df["Published"].dropna():
            iso = self._normalize_published(v)
            if iso:
                counts[iso] = counts.get(iso, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: kv[1])[0]

    @staticmethod
    def _normalize_published(value) -> str | None:
        if value is None:
            return None
        if hasattr(value, "year") and hasattr(value, "month"):
            return f"{value.year:04d}-{value.month:02d}-01"
        s = str(value).strip()
        if pd.Series([s]).str.match(r"^\d{4}-\d{2}").iloc[0]:
            parts = s.split("-")
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-01"
        try:
            ts = pd.to_datetime(s, errors="coerce")
            if pd.notna(ts):
                return f"{ts.year:04d}-{ts.month:02d}-01"
        except Exception:
            pass
        return None
