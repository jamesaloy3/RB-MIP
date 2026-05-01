"""Market & submarket resolution.

Strategy: alias-first lookup, with auto-create of canonical markets when a raw
name is encountered for the first time during ingestion. The auto-created
market gets an alias for the source provider so future lookups hit the alias
table directly.

Canonical names follow LARC convention (shortest form): "Denver", "Austin",
"Boston", "Los Angeles", etc. CoStar's "Denver, CO" and Green Street's
"Denver" both alias to canonical "Denver".
"""
from __future__ import annotations

import re
import sqlite3
from typing import Callable


# Common provider-name → canonical normalization rules (applied before lookup).
# These run before alias table check, so they cover the common case without
# requiring explicit alias rows.
_NORMALIZE_RULES = [
    # CoStar: "Austin - TX USA" → "Austin"
    (re.compile(r"^(.+?)\s*-\s*[A-Z]{2}\s+USA\s*$"), r"\1"),
    # "Denver, CO" → "Denver"
    (re.compile(r"^(.+?),\s*[A-Z]{2}\s*$"), r"\1"),
    # "Colorado Area USA" → "Colorado Area"
    (re.compile(r"^(.+?)\s+USA\s*$"), r"\1"),
    # Strip trailing whitespace/punctuation
    (re.compile(r"\s+$"), r""),
]


def _normalize(raw_name: str) -> str:
    s = (raw_name or "").strip()
    for pattern, replacement in _NORMALIZE_RULES:
        s = pattern.sub(replacement, s).strip()
    return s


class MarketResolver:
    """Resolve provider market names to canonical market_ids.

    Holds an in-memory cache for the duration of one ingestion run. Cache is
    invalidated by reload() or by creating a new instance.
    """

    def __init__(self, conn: sqlite3.Connection, auto_create: bool = True):
        self.conn = conn
        self.auto_create = auto_create
        self._alias_cache: dict[tuple[str, str | None], int] = {}
        self._canonical_cache: dict[str, int] = {}
        self._reload()

    def _reload(self) -> None:
        self._alias_cache.clear()
        self._canonical_cache.clear()
        for row in self.conn.execute("SELECT market_id, canonical_name FROM markets"):
            self._canonical_cache[row["canonical_name"].lower()] = row["market_id"]
        for row in self.conn.execute(
            "SELECT alias, provider_code, market_id FROM market_aliases"
        ):
            self._alias_cache[(row["alias"].lower(), row["provider_code"])] = row["market_id"]

    # ------------------------------------------------------------------

    def resolve(self, raw_name: str, provider_code: str) -> int | None:
        """Look up a market_id for a (raw_name, provider) pair.

        Resolution order:
          1. exact provider-scoped alias
          2. provider-agnostic alias (provider_code IS NULL)
          3. normalized name → canonical market name
          4. auto_create: insert a new canonical market + alias, return new id
        """
        if not raw_name:
            return None
        raw = raw_name.strip()
        raw_lower = raw.lower()

        # 1. provider-scoped alias
        cached = self._alias_cache.get((raw_lower, provider_code))
        if cached is not None:
            return cached

        # 2. provider-agnostic alias
        cached = self._alias_cache.get((raw_lower, None))
        if cached is not None:
            return cached

        # 3. normalized name → canonical
        normalized = _normalize(raw)
        canonical_id = self._canonical_cache.get(normalized.lower())
        if canonical_id is not None:
            # Auto-add alias so future lookups skip normalization
            self._add_alias(raw, provider_code, canonical_id)
            return canonical_id

        # 4. auto-create
        if self.auto_create:
            new_id = self._create_canonical(normalized, raw, provider_code)
            return new_id

        return None

    # ------------------------------------------------------------------

    def _add_alias(self, alias: str, provider_code: str | None, market_id: int) -> None:
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO market_aliases (alias, provider_code, market_id) "
                "VALUES (?, ?, ?)",
                (alias, provider_code, market_id),
            )
            self._alias_cache[(alias.lower(), provider_code)] = market_id
        except sqlite3.IntegrityError:
            pass

    def _create_canonical(
        self, canonical_name: str, raw_alias: str, provider_code: str
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO markets (canonical_name) VALUES (?)",
            (canonical_name,),
        )
        market_id = int(cur.lastrowid)
        self._canonical_cache[canonical_name.lower()] = market_id
        # Register the original raw form as a provider-scoped alias
        if raw_alias != canonical_name:
            self._add_alias(raw_alias, provider_code, market_id)
        # Also register the canonical name itself as a provider-agnostic alias
        self._add_alias(canonical_name, None, market_id)
        return market_id

    # ------------------------------------------------------------------

    def resolve_submarket(
        self, market_id: int, raw_submarket: str, provider_code: str
    ) -> int | None:
        """Resolve a submarket name to submarket_id under a market. Auto-creates."""
        if not raw_submarket:
            return None
        raw = raw_submarket.strip()
        # alias check
        row = self.conn.execute(
            "SELECT submarket_id FROM submarket_aliases "
            "WHERE alias = ? AND (provider_code = ? OR provider_code IS NULL) "
            "AND submarket_id IN (SELECT submarket_id FROM submarkets WHERE market_id = ?)",
            (raw, provider_code, market_id),
        ).fetchone()
        if row:
            return row["submarket_id"]
        # canonical lookup
        row = self.conn.execute(
            "SELECT submarket_id FROM submarkets WHERE market_id = ? AND canonical_name = ?",
            (market_id, raw),
        ).fetchone()
        if row:
            return row["submarket_id"]
        if not self.auto_create:
            return None
        # create
        cur = self.conn.execute(
            "INSERT INTO submarkets (market_id, canonical_name) VALUES (?, ?)",
            (market_id, raw),
        )
        submarket_id = int(cur.lastrowid)
        return submarket_id


def make_resolver_callable(resolver: MarketResolver) -> Callable[[str, str], int | None]:
    """Return a callable suitable for BaseAdapter.market_resolver."""
    return lambda raw, provider: resolver.resolve(raw, provider)
