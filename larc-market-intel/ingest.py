"""Ingestion CLI.

Examples:
    python ingest.py --file LARC_Data/1Q26_HotelBIS\\ Data\\ File.For\\ Distribution.xlsx
    python ingest.py --file path/to/file --doc-type larc_hotelbis
    python ingest.py --file path/to/file --force
    python ingest.py --status
    python ingest.py --status --provider LARC
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from db.init import apply_schema, connect, get_db_path
from pipeline.orchestrator import ingest_file


def cmd_ingest(args: argparse.Namespace) -> int:
    db_path = get_db_path(args.db_path)
    conn = connect(db_path)
    apply_schema(conn)

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"error: file not found: {file_path}", file=sys.stderr)
        return 1

    print(f"Ingesting: {file_path.name}")
    print(f"  DB: {db_path}")
    try:
        outcome = ingest_file(
            conn,
            file_path,
            override_doc_type=args.doc_type,
            force_reload=args.force,
        )
    except Exception as e:
        print(f"  ✗ FAILED: {e!r}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print(f"  provider:  {outcome.provider_code}")
    print(f"  doc_type:  {outcome.doc_type}")
    print(f"  pub_id:    {outcome.publication_id}")
    if outcome.duplicate:
        print("  status:    DUPLICATE (already loaded; use --force to re-ingest)")
        return 0

    total = sum(outcome.inserted.values())
    print(f"  status:    LOADED ({total} records)")
    for table, n in outcome.inserted.items():
        print(f"    {table}: {n}")
    if outcome.warnings:
        print(f"  warnings:  {len(outcome.warnings)}")
        for w in outcome.warnings[:10]:
            print(f"    - {w}")
        if len(outcome.warnings) > 10:
            print(f"    ... ({len(outcome.warnings) - 10} more)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db_path = get_db_path(args.db_path)
    if not db_path.exists():
        print(f"DB does not exist yet: {db_path}")
        return 0
    conn = connect(db_path)

    print(f"Database: {db_path}")
    print()
    print("Publications by provider:")
    rows = conn.execute(
        """SELECT provider_code, doc_type, COUNT(*) AS n,
                  MAX(publication_date) AS latest
             FROM publications
            GROUP BY provider_code, doc_type
            ORDER BY provider_code, doc_type"""
    ).fetchall()
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  {r['provider_code']:12s} {r['doc_type']:20s} count={r['n']:4d}  latest={r['latest']}")

    print()
    print("Coverage matrix (markets x provider):")
    cov = conn.execute(
        """SELECT canonical_name, state,
                  larc_hotelbis_latest, larc_convention_latest, larc_narrative_latest,
                  costar_str_latest, costar_narrative_latest, greenstreet_latest,
                  forecast_period_rows, convention_rows, narrative_rows
             FROM v_market_coverage
            ORDER BY forecast_period_rows DESC, canonical_name"""
    ).fetchall()
    if not cov:
        print("  (no markets)")
    else:
        header = f"  {'Market':28s} {'State':5s} {'HotelBIS':10s} {'CC':10s} {'LARC pdf':10s} {'CoStar':10s} {'CoStar pdf':12s} {'GS':10s} {'fp':>5s} {'cc':>4s} {'nar':>4s}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for r in cov:
            print(
                f"  {(r['canonical_name'] or '')[:28]:28s} "
                f"{(r['state'] or ''):5s} "
                f"{(r['larc_hotelbis_latest'] or '-')[:10]:10s} "
                f"{(r['larc_convention_latest'] or '-')[:10]:10s} "
                f"{(r['larc_narrative_latest'] or '-')[:10]:10s} "
                f"{(r['costar_str_latest'] or '-')[:10]:10s} "
                f"{(r['costar_narrative_latest'] or '-')[:12]:12s} "
                f"{(r['greenstreet_latest'] or '-')[:10]:10s} "
                f"{r['forecast_period_rows']:>5d} "
                f"{r['convention_rows']:>4d} "
                f"{r['narrative_rows']:>4d}"
            )

    print()
    print("LLM usage:")
    rows = conn.execute(
        """SELECT purpose, model, COUNT(*) AS n_calls,
                  SUM(input_tokens)                  AS in_tok,
                  SUM(output_tokens)                 AS out_tok,
                  SUM(cache_creation_input_tokens)   AS cache_w,
                  SUM(cache_read_input_tokens)       AS cache_r,
                  SUM(estimated_cost_usd)            AS cost,
                  SUM(CASE WHEN error_message IS NOT NULL THEN 1 ELSE 0 END) AS errors
             FROM llm_usage GROUP BY purpose, model ORDER BY purpose, model"""
    ).fetchall()
    if not rows:
        print("  (none)")
    for r in rows:
        cache_hit_pct = (r["cache_r"] / max(r["in_tok"] + r["cache_r"], 1) * 100) if r["cache_r"] else 0
        print(
            f"  {r['purpose']:20s} {r['model']:20s} calls={r['n_calls']:>3}  "
            f"in={r['in_tok']:>7,}  out={r['out_tok']:>6,}  "
            f"cache_w={r['cache_w']:>6,}  cache_r={r['cache_r']:>6,}  "
            f"hit%={cache_hit_pct:>4.0f}  ${r['cost'] or 0:.4f}  err={r['errors']}"
        )
    grand = conn.execute("SELECT SUM(estimated_cost_usd) FROM llm_usage").fetchone()[0]
    if grand:
        print(f"  {'TOTAL':45s} ${grand:.4f}")

    print()
    print("Recent ingest log (last 10):")
    log = conn.execute(
        """SELECT seen_at, source_filename, status, records_loaded, error_message
             FROM ingest_log
            ORDER BY seen_at DESC
            LIMIT 10"""
    ).fetchall()
    for r in log:
        msg = r["error_message"] or ""
        print(
            f"  {r['seen_at']}  {r['status']:9s}  rows={r['records_loaded'] or '-':>5}  "
            f"{r['source_filename'][:50]}  {msg[:60]}"
        )
    conn.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Market Intelligence ingestion CLI")
    parser.add_argument("--db-path", default=None, help="override DB path")
    parser.add_argument("--file", help="path to source file to ingest")
    parser.add_argument("--doc-type", default=None,
                        help="explicit doc_type (override filename matching)")
    parser.add_argument("--force", action="store_true",
                        help="re-ingest even if file content already loaded")
    parser.add_argument("--status", action="store_true",
                        help="print database status and coverage")
    parser.add_argument("--provider", default=None,
                        help="filter --status output by provider")
    args = parser.parse_args()

    if args.status:
        sys.exit(cmd_status(args))
    elif args.file:
        sys.exit(cmd_ingest(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
