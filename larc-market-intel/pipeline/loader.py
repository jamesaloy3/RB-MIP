"""Loader: write a ParseResult into the database in a single transaction.

Idempotency contract:
  - Same publication_id (= deterministic from file content) → DELETE existing
    fact rows for that publication_id, then INSERT fresh.
  - Source file is registered in publications table with sha256 + bytes.
  - Re-loading the same file is a no-op in effect (rows are identical after
    DELETE+INSERT).
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from adapters.base import ParseResult, PublicationInfo


# Tables managed by the loader, in order of insertion.
# (table_name, attribute_name_in_ParseResult)
FACT_TABLES = [
    ("forecast_periods",        "forecast_periods"),
    ("convention_bookings",     "convention_bookings"),
    ("narratives",              "narratives"),
    ("transactions",            "transactions"),
    ("supply_pipeline",         "supply_pipeline"),
    ("green_street_grades",     "green_street_grades"),
    ("green_street_irr",        "green_street_irr"),
    ("gs_submarket_cap_rates",  "gs_submarket_cap_rates"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_many(conn: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    # Take column union from first row; require all rows have same keys (or ignore extras)
    cols = sorted(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    col_list = ",".join(cols)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    data = [tuple(r.get(c) for c in cols) for r in rows]
    conn.executemany(sql, data)
    return len(rows)


def upsert_publication(
    conn: sqlite3.Connection,
    pub: PublicationInfo,
    overall_market_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO publications (
                publication_id, provider_code, market_id, doc_type,
                publication_date, publication_period,
                source_filename, source_sha256, source_bytes,
                ingested_at, extractor_version
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(publication_id) DO UPDATE SET
                ingested_at = excluded.ingested_at,
                extractor_version = excluded.extractor_version,
                publication_date = excluded.publication_date,
                publication_period = excluded.publication_period,
                source_filename = excluded.source_filename,
                source_sha256 = excluded.source_sha256,
                source_bytes = excluded.source_bytes
        """,
        (
            pub.publication_id,
            pub.provider_code,
            overall_market_id,
            pub.doc_type,
            pub.publication_date,
            pub.publication_period,
            pub.source_filename,
            pub.source_sha256,
            pub.source_bytes,
            _now_iso(),
            pub.extractor_version,
        ),
    )


def delete_publication_facts(conn: sqlite3.Connection, publication_id: str) -> dict[str, int]:
    """DELETE all fact rows for a publication. Returns counts deleted per table."""
    counts: dict[str, int] = {}
    for table, _ in FACT_TABLES:
        cur = conn.execute(f"DELETE FROM {table} WHERE publication_id = ?", (publication_id,))
        if cur.rowcount > 0:
            counts[table] = cur.rowcount
    return counts


def load(
    conn: sqlite3.Connection,
    pub: PublicationInfo,
    result: ParseResult,
    overall_market_id: int | None = None,
) -> dict[str, int]:
    """Apply the ParseResult to the database in a single transaction.

    Returns a dict {table_name: rows_inserted}.
    """
    started = time.monotonic()
    inserted: dict[str, int] = {}

    try:
        conn.execute("BEGIN")

        upsert_publication(conn, pub, overall_market_id)
        delete_publication_facts(conn, pub.publication_id)

        for table, attr in FACT_TABLES:
            rows = getattr(result, attr)
            if not rows:
                continue
            # Inject publication_id into every row
            for r in rows:
                r["publication_id"] = pub.publication_id
            n = _insert_many(conn, table, rows)
            if n:
                inserted[table] = n

        # Persist warnings as info-level validation_warnings
        for w in result.warnings:
            conn.execute(
                "INSERT INTO validation_warnings (publication_id, severity, rule, message) "
                "VALUES (?, 'info', 'adapter_warning', ?)",
                (pub.publication_id, w),
            )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    total = sum(inserted.values())
    conn.execute(
        """INSERT INTO ingest_log
            (source_filename, source_sha256, provider_code, status,
             publication_id, records_loaded, duration_ms)
           VALUES (?, ?, ?, 'loaded', ?, ?, ?)""",
        (
            pub.source_filename,
            pub.source_sha256,
            pub.provider_code,
            pub.publication_id,
            total,
            duration_ms,
        ),
    )
    conn.commit()
    return inserted


def log_duplicate(conn: sqlite3.Connection, pub: PublicationInfo) -> None:
    conn.execute(
        """INSERT INTO ingest_log
            (source_filename, source_sha256, provider_code, status, publication_id)
           VALUES (?, ?, ?, 'duplicate', ?)""",
        (pub.source_filename, pub.source_sha256, pub.provider_code, pub.publication_id),
    )
    conn.commit()


def log_error(
    conn: sqlite3.Connection, pub: PublicationInfo | None, filename: str, error: str
) -> None:
    conn.execute(
        """INSERT INTO ingest_log
            (source_filename, source_sha256, provider_code, status, error_message)
           VALUES (?, ?, ?, 'error', ?)""",
        (
            filename,
            pub.source_sha256 if pub else None,
            pub.provider_code if pub else None,
            error[:1000],
        ),
    )
    conn.commit()


def is_already_loaded(conn: sqlite3.Connection, source_sha256: str) -> str | None:
    """If this file content has already been loaded, return its publication_id."""
    row = conn.execute(
        "SELECT publication_id FROM publications WHERE source_sha256 = ?",
        (source_sha256,),
    ).fetchone()
    return row["publication_id"] if row else None
