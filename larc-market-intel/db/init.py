"""Initialize the SQLite database from schema.sql.

Usage:
    python -m db.init                    # create at default path
    python -m db.init --db-path /tmp/x.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_db_path(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("DB_PATH")
    if env:
        return Path(env)
    # Default: ./data/market_intel.db locally; /home/data/market_intel.db on Azure
    if Path("/home").exists() and os.environ.get("WEBSITE_INSTANCE_ID"):
        return Path("/home/data/market_intel.db")
    return Path(__file__).parent.parent / "data" / "market_intel.db"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None → autocommit mode; we manage transactions explicitly
    # via BEGIN/COMMIT in the loader.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args()

    db_path = get_db_path(args.db_path)
    print(f"Initializing database at: {db_path}")
    conn = connect(db_path)
    apply_schema(conn)
    n_tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    print(f"  ✓ {n_tables} tables present")
    conn.close()


if __name__ == "__main__":
    main()
