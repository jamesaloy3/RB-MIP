"""Base adapter contract — see adapters/README.md for the full spec."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class AdapterError(Exception):
    """Raised when an adapter cannot produce useful output."""


class UnknownMarketError(AdapterError):
    """Raised when a market name cannot be resolved to a canonical market."""


@dataclass
class ParseResult:
    """Container of records produced by an adapter, keyed by destination table.

    Each entry is a list of dicts. Dict keys must match the destination table's
    column names. Do NOT include `publication_id` — the loader injects it.
    `market_id` and `submarket_id` should be populated by the adapter via the
    market resolver. If unresolvable, the adapter should raise UnknownMarketError.
    """

    forecast_periods:        list[dict] = field(default_factory=list)
    convention_bookings:     list[dict] = field(default_factory=list)
    narratives:              list[dict] = field(default_factory=list)
    transactions:            list[dict] = field(default_factory=list)
    supply_pipeline:         list[dict] = field(default_factory=list)
    green_street_grades:     list[dict] = field(default_factory=list)
    green_street_irr:        list[dict] = field(default_factory=list)
    gs_submarket_cap_rates:  list[dict] = field(default_factory=list)
    warnings:                list[str]  = field(default_factory=list)

    def total_records(self) -> int:
        return sum(
            len(getattr(self, name))
            for name in (
                "forecast_periods",
                "convention_bookings",
                "narratives",
                "transactions",
                "supply_pipeline",
                "green_street_grades",
                "green_street_irr",
                "gs_submarket_cap_rates",
            )
        )

    def summary(self) -> dict[str, int]:
        return {
            name: len(getattr(self, name))
            for name in (
                "forecast_periods",
                "convention_bookings",
                "narratives",
                "transactions",
                "supply_pipeline",
                "green_street_grades",
                "green_street_irr",
                "gs_submarket_cap_rates",
            )
            if len(getattr(self, name)) > 0
        }


@dataclass
class PublicationInfo:
    """Information about the publication being ingested.

    Constructed by the router and passed to the adapter.
    """

    publication_id:    str
    provider_code:     str
    doc_type:          str
    publication_date:  str        # ISO YYYY-MM-DD
    publication_period: str | None  # '1Q26', '4Q25', etc. — may be None
    source_filename:   str
    source_sha256:     str
    source_bytes:      int
    extractor_version: str = "0.1.0"


class BaseAdapter(ABC):
    """Abstract base class for all provider adapters."""

    PROVIDER_CODE: str = ""    # subclass overrides
    DOC_TYPE:      str = ""
    EXTRACTOR_VERSION: str = "0.1.0"

    def __init__(self, market_resolver):
        """market_resolver: a MarketResolver-like object exposing
        .resolve(name, provider) and .resolve_submarket(market_id, name, provider).
        For backward compat, may be a callable(name, provider) -> int | None.
        """
        self.market_resolver = market_resolver
        self.warnings: list[str] = []

    @abstractmethod
    def parse(self, file_path: Path | str, pub: PublicationInfo) -> ParseResult:
        """Parse the source file. Return ParseResult with empty publication_id
        on every record (the loader injects it)."""

    def metadata(self) -> dict:
        return {
            "provider": self.PROVIDER_CODE,
            "doc_type": self.DOC_TYPE,
            "extractor_version": self.EXTRACTOR_VERSION,
        }

    # ------------------------------------------------------------------
    # Helpers for subclasses
    # ------------------------------------------------------------------

    def log_warning(self, message: str) -> None:
        self.warnings.append(message)

    def resolve_market(self, raw_name: str) -> int:
        """Resolve a raw market name to a canonical market_id. Raises if missing."""
        if raw_name is None or str(raw_name).strip() == "":
            raise UnknownMarketError("empty market name")
        name = str(raw_name).strip()
        if hasattr(self.market_resolver, "resolve"):
            market_id = self.market_resolver.resolve(name, self.PROVIDER_CODE)
        else:
            market_id = self.market_resolver(name, self.PROVIDER_CODE)
        if market_id is None:
            raise UnknownMarketError(
                f"unresolved market '{raw_name}' for provider {self.PROVIDER_CODE}"
            )
        return market_id

    def resolve_submarket(self, market_id: int, raw_name: str) -> int | None:
        """Resolve raw submarket name → submarket_id. Returns None if not resolvable."""
        if raw_name is None or str(raw_name).strip() == "":
            return None
        if hasattr(self.market_resolver, "resolve_submarket"):
            return self.market_resolver.resolve_submarket(
                market_id, str(raw_name).strip(), self.PROVIDER_CODE
            )
        return None

    @staticmethod
    def _is_missing(value) -> bool:
        """True if value should be treated as missing (None, NaN, empty, N/A)."""
        if value is None:
            return True
        if isinstance(value, float):
            # NaN check without importing math
            return value != value
        if isinstance(value, str):
            return value.strip() in ("", "N/A", "n/a", "-", "--", "NA", "nan", "NaN")
        return False

    @staticmethod
    def to_decimal_pct(value) -> float | None:
        """Normalize a percentage to decimal form.

        Excel may store 74.4% as 0.744 (formatted) or 74.4 (raw number).
        Heuristic: any value with absolute >1.5 is divided by 100.
        Empty/NaN/N/A → None.
        """
        if BaseAdapter._is_missing(value):
            return None
        if isinstance(value, str):
            s = value.strip().rstrip("%").replace(",", "")
            try:
                value = float(s)
            except ValueError:
                return None
        if isinstance(value, (int, float)):
            v = float(value)
            if v != v:        # NaN
                return None
            if abs(v) > 1.5:
                v = v / 100.0
            return v
        return None

    @staticmethod
    def to_float(value) -> float | None:
        """Coerce to float. Empty/NaN/N/A → None."""
        if BaseAdapter._is_missing(value):
            return None
        if isinstance(value, str):
            s = value.strip().replace(",", "").replace("$", "")
            try:
                return float(s)
            except ValueError:
                return None
        if isinstance(value, (int, float)):
            v = float(value)
            return None if v != v else v
        return None

    @staticmethod
    def to_int(value) -> int | None:
        f = BaseAdapter.to_float(value)
        return int(f) if f is not None else None
