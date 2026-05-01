"""Summary generation CLI.

Examples:
    # Single market+submarket
    python summarize.py --market Denver --submarket "Denver CBD" --quarter 1Q26

    # Single market (no submarket)
    python summarize.py --market Denver --quarter 1Q26

    # Batch — all markets+submarkets that have at least N observations
    python summarize.py --quarter 1Q26 --all-markets --min-obs 30

    # Force re-generate (creates a new generation_number, doesn't overwrite)
    python summarize.py --market Denver --submarket "Denver CBD" --quarter 1Q26 --force

    # Show existing summaries for a market
    python summarize.py --market Denver --quarter 1Q26 --list
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from db.init import apply_schema, connect, get_db_path
from llm import BudgetExceededError, get_default_client
from summarize import (
    CitationError,
    GenerationResult,
    build_context,
    generate_summary,
)


def cmd_generate_one(
    conn: sqlite3.Connection,
    market: str,
    submarket: str | None,
    quarter: str,
    force: bool,
    skip_existing: bool,
) -> int:
    label = f"{market}" + (f" / {submarket}" if submarket else "")
    print(f"\n[{label}] {quarter}")

    if skip_existing and not force:
        existing = conn.execute(
            """SELECT id, generation_number, generated_at
                 FROM summaries
                WHERE market_id = (SELECT market_id FROM markets WHERE LOWER(canonical_name)=LOWER(?))
                  AND COALESCE(submarket_id, -1) = COALESCE(
                      (SELECT submarket_id FROM submarkets s
                        JOIN markets m ON s.market_id=m.market_id
                       WHERE LOWER(m.canonical_name)=LOWER(?)
                         AND LOWER(s.canonical_name)=LOWER(?)),
                      -1)
                  AND quarter = ? AND version_type = 'detailed'
                LIMIT 1""",
            (market, market, submarket or "", quarter),
        ).fetchone()
        if existing:
            print(f"  skip: existing summary id={existing[0]} gen#{existing[1]} from {existing[2]}")
            return 0

    try:
        result = generate_summary(
            conn, market_name=market, submarket_name=submarket, quarter=quarter,
        )
    except ValueError as e:
        print(f"  error: {e}")
        return 1
    except CitationError as e:
        print(f"  CITATION FAILURE: {e}")
        return 2
    except BudgetExceededError as e:
        print(f"  BUDGET EXCEEDED: {e}")
        return 3

    _print_result(result)
    return 0


def _print_result(r: GenerationResult) -> None:
    label = r.market_name + (f" / {r.submarket_name}" if r.submarket_name else "")
    print(f"  generated for {label} {r.quarter}")
    print(f"  cost ${r.total_cost_usd:.4f}, cache_read={r.cache_read_tokens:,} tokens")
    print(f"  detailed:   id={r.detailed.summary_id}  {r.detailed.char_count} chars  "
          f"{len(r.detailed.parsed.citations)} citations")
    print(f"  summarized: id={r.summarized.summary_id}  {r.summarized.char_count} chars  "
          f"{len(r.summarized.parsed.citations)} citations")
    if r.coverage_notes:
        print(f"  coverage notes: {r.coverage_notes}")


def cmd_list(
    conn: sqlite3.Connection,
    market: str | None,
    submarket: str | None,
    quarter: str | None,
) -> int:
    where = ["1=1"]
    args: list = []
    if market:
        where.append(
            "s.market_id = (SELECT market_id FROM markets WHERE LOWER(canonical_name)=LOWER(?))"
        )
        args.append(market)
    if submarket:
        where.append(
            """s.submarket_id = (SELECT submarket_id FROM submarkets sm
                                  JOIN markets m ON sm.market_id=m.market_id
                                 WHERE LOWER(m.canonical_name)=LOWER(?)
                                   AND LOWER(sm.canonical_name)=LOWER(?))"""
        )
        args += [market or "", submarket]
    if quarter:
        where.append("s.quarter = ?")
        args.append(quarter)
    sql = f"""
        SELECT s.id, m.canonical_name AS market, sm.canonical_name AS submarket,
               s.quarter, s.version_type, s.generation_number, s.generated_at,
               s.model_used, LENGTH(s.generated_text) AS chars,
               (SELECT COUNT(*) FROM summary_citations c WHERE c.summary_id = s.id) AS n_cites
          FROM summaries s
          JOIN markets m ON s.market_id = m.market_id
          LEFT JOIN submarkets sm ON s.submarket_id = sm.submarket_id
         WHERE {" AND ".join(where)}
         ORDER BY s.generated_at DESC, s.version_type
    """
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        print("(no summaries match)")
        return 0
    print(f"{'id':>4} {'market':16s} {'submarket':18s} {'qtr':6s} {'type':10s} {'gen#':>4} {'chars':>5} {'cites':>5} {'model':18s} when")
    for r in rows:
        print(f"{r['id']:>4} {(r['market'] or '')[:16]:16s} {(r['submarket'] or '')[:18]:18s} "
              f"{r['quarter']:6s} {r['version_type']:10s} {r['generation_number']:>4} "
              f"{r['chars']:>5} {r['n_cites']:>5} {r['model_used']:18s} {r['generated_at']}")
    return 0


def cmd_show(conn: sqlite3.Connection, summary_id: int) -> int:
    s = conn.execute(
        """SELECT s.*, m.canonical_name AS market, sm.canonical_name AS submarket
             FROM summaries s
             JOIN markets m ON s.market_id = m.market_id
             LEFT JOIN submarkets sm ON s.submarket_id = sm.submarket_id
            WHERE s.id = ?""",
        (summary_id,),
    ).fetchone()
    if not s:
        print(f"summary id={summary_id} not found")
        return 1
    label = s["market"] + (f" / {s['submarket']}" if s["submarket"] else "")
    print(f"=== Summary id={s['id']} | {label} | {s['quarter']} | {s['version_type']} | "
          f"gen#{s['generation_number']} | {s['model_used']} ===")
    print()
    print(s["generated_text"])
    print()

    cits = conn.execute(
        "SELECT citation_token, source_table, source_id, char_start "
        "FROM summary_citations WHERE summary_id=? ORDER BY char_start",
        (summary_id,),
    ).fetchall()
    print(f"\n--- {len(cits)} citations ---")
    for c in cits[:20]:
        print(f"  @ char {c['char_start']:>5}  {c['source_table']}:{c['source_id']}  "
              f"({c['citation_token']})")
    if len(cits) > 20:
        print(f"  ... ({len(cits) - 20} more)")
    return 0


def cmd_batch(
    conn: sqlite3.Connection,
    quarter: str,
    min_obs: int,
    skip_existing: bool,
    throttle_seconds: float = 0.0,
) -> int:
    """Iterate over every (market, submarket) pair that has data."""
    targets: list[tuple[str, str | None]] = []

    # Submarket-level targets — every submarket that has any data
    for r in conn.execute(
        """SELECT m.canonical_name AS market, s.canonical_name AS submarket
             FROM submarkets s JOIN markets m ON s.market_id=m.market_id
            ORDER BY m.canonical_name, s.canonical_name"""
    ):
        targets.append((r["market"], r["submarket"]))
    # Market-level targets — every market that has narrative or HotelBIS data
    # but NO submarket-level data (otherwise we'd duplicate)
    rows = conn.execute(
        """SELECT DISTINCT m.canonical_name AS market
             FROM markets m
             WHERE m.market_id IN (
                SELECT market_id FROM forecast_periods
                UNION SELECT market_id FROM narratives
             )
             AND m.market_id NOT IN (SELECT DISTINCT market_id FROM submarkets)
             ORDER BY m.canonical_name"""
    ).fetchall()
    for r in rows:
        targets.append((r["market"], None))

    print(f"\nBatch summary generation for {quarter}: {len(targets)} candidates")

    import time
    n_ok = n_skip = n_low = n_err = 0
    last_api_call_ts: float | None = None
    for market, submarket in targets:
        ctx = build_context(conn, market_name=market, submarket_name=submarket, quarter=quarter)
        if ctx.total_records() < min_obs:
            n_low += 1
            print(f"  [skip <min_obs] {market}/{submarket or ''}  ({ctx.total_records()} obs)")
            continue
        # Throttle between API calls to respect rate limits
        if throttle_seconds > 0 and last_api_call_ts is not None:
            elapsed = time.monotonic() - last_api_call_ts
            wait = throttle_seconds - elapsed
            if wait > 0:
                print(f"  [throttle] sleeping {wait:.0f}s before next call...")
                time.sleep(wait)
        rc = cmd_generate_one(
            conn, market=market, submarket=submarket, quarter=quarter,
            force=False, skip_existing=skip_existing,
        )
        last_api_call_ts = time.monotonic()
        if rc == 0: n_ok += 1
        else:       n_err += 1

    print(f"\nBatch complete: ok={n_ok}  skipped_low_data={n_low}  errors={n_err}")
    return 0 if n_err == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Summary generation CLI")
    p.add_argument("--db-path", default=None)
    p.add_argument("--market", help="canonical market name")
    p.add_argument("--submarket", default=None)
    p.add_argument("--quarter", help="e.g. 1Q26")
    p.add_argument("--all-markets", action="store_true",
                   help="batch mode: generate for all markets/submarkets with data")
    p.add_argument("--min-obs", type=int, default=10,
                   help="batch mode: skip if context has fewer observations than this")
    p.add_argument("--force", action="store_true",
                   help="force regenerate even if a summary exists")
    p.add_argument("--no-skip", action="store_true",
                   help="(batch) don't skip existing — regenerate everything")
    p.add_argument("--throttle", type=float, default=70.0,
                   help="(batch) seconds to wait between API calls; default 70s "
                        "to stay under tier-1 10K-TPM limits. Set to 0 if higher tier.")
    p.add_argument("--list", action="store_true",
                   help="list existing summaries (filterable by --market/--submarket/--quarter)")
    p.add_argument("--show", type=int, metavar="SUMMARY_ID",
                   help="print one summary's full text + citations")
    args = p.parse_args()

    db_path = get_db_path(args.db_path)
    conn = connect(db_path)
    apply_schema(conn)

    try:
        if args.show is not None:
            sys.exit(cmd_show(conn, args.show))
        if args.list:
            sys.exit(cmd_list(conn, args.market, args.submarket, args.quarter))
        if args.all_markets:
            if not args.quarter:
                p.error("--all-markets requires --quarter")
            sys.exit(cmd_batch(conn, args.quarter, args.min_obs,
                               skip_existing=not args.no_skip,
                               throttle_seconds=args.throttle))
        if args.market and args.quarter:
            sys.exit(cmd_generate_one(
                conn, market=args.market, submarket=args.submarket,
                quarter=args.quarter, force=args.force, skip_existing=False,
            ))
        p.print_help()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
