"""One-shot inventory KPI extractor for CoStar PDFs.

Pulls hotel count, room count, and segment mix from each CoStar Hospitality
Submarket Report and persists to `submarket_inventory`. This data anchors the
inventory opener pattern in the user's hand-written summaries
("X comprises N hotels with ~Y rooms, with Z% in Luxury/Upper Upscale").

Designed to be cheap and idempotent:
  - One Claude call per PDF (~$0.02 each on Sonnet 4.6)
  - System prompt is cached, so subsequent calls within the 5-min TTL are 90% cheaper
  - UPSERT — re-running on the same PDFs replaces the inventory row

Usage:
    python extract_inventory.py                     # all CoStar PDFs in standard locations
    python extract_inventory.py --file path/to.pdf  # single PDF
    python extract_inventory.py --list              # show what's already extracted
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import pdfplumber

from adapters.costar.narrative import CoStarNarrativeAdapter
from db.init import apply_schema, connect, get_db_path
from db.markets import MarketResolver
from llm import LLMClient, get_default_client


PROMPT_PATH = Path(__file__).parent / "prompts" / "inventory_v1.md"
DEFAULT_PDF_DIR = Path(__file__).parent.parent / "CoSTAR_Text"


INVENTORY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "hotel_count":              {"type": "integer"},
        "room_count":               {"type": "integer"},
        "luxury_upper_upscale_pct": {"type": "number"},
        "segment_mix": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "segment": {"type": "string"},
                    "share":   {"type": "number"},
                },
                "required": ["segment", "share"],
            },
        },
    },
    "required": [
        "hotel_count", "room_count", "luxury_upper_upscale_pct", "segment_mix",
    ],
}


# Filename → (submarket, market) — same regex as the CoStar narrative adapter
FILENAME_RE = CoStarNarrativeAdapter.FILENAME_RE


def extract_pdf_text(file_path: Path, max_pages: int = 6) -> str:
    """Read first N pages — inventory KPIs are always near the top."""
    chunks: list[str] = []
    with pdfplumber.open(file_path) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages]):
            t = page.extract_text() or ""
            t = re.sub(r"^Hospitality Submarket Report\s*$", "", t, flags=re.M)
            chunks.append(f"[PAGE {i+1}]\n{t.strip()}")
    return "\n\n".join(chunks)


def detect_submarket(filename: str) -> str | None:
    m = FILENAME_RE.match(filename)
    return m.group(1).strip() if m else None


def extract_one(
    conn: sqlite3.Connection,
    file_path: Path,
    client: LLMClient,
) -> dict | None:
    submarket_name = detect_submarket(file_path.name)
    if not submarket_name:
        print(f"  skip: cannot detect submarket from filename")
        return None
    submarket_row = conn.execute(
        "SELECT submarket_id FROM submarkets WHERE LOWER(canonical_name) = LOWER(?)",
        (submarket_name,),
    ).fetchone()
    if not submarket_row:
        print(f"  skip: submarket '{submarket_name}' not in DB")
        return None
    submarket_id = submarket_row[0]

    text = extract_pdf_text(file_path)
    if not text.strip():
        print(f"  skip: no extractable text")
        return None

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_msg = (
        f"Submarket: {submarket_name}\n\n"
        f"--- BEGIN REPORT EXCERPT ---\n{text}\n--- END REPORT EXCERPT ---"
    )
    response = client.create(
        purpose="costar_inventory",
        system_blocks=[{"type": "text", "text": system_prompt}],
        messages=[{"role": "user", "content": user_msg}],
        response_schema=INVENTORY_SCHEMA,
        max_tokens=1024,
        metadata={"submarket": submarket_name, "filename": file_path.name},
    )
    if response.parsed is None:
        print(f"  error: non-JSON response: {response.text[:120]}")
        return None

    p = response.parsed
    seg_json = json.dumps(p.get("segment_mix") or [])
    hotel_count = p.get("hotel_count") or None
    room_count = p.get("room_count") or None
    lux_pct = p.get("luxury_upper_upscale_pct") or None
    if not (hotel_count or room_count or lux_pct):
        print(f"  empty: no inventory data found in {file_path.name}")
        return None

    # Find latest costar_narrative publication for this submarket (for FK linkage)
    pub = conn.execute(
        """SELECT p.publication_id FROM publications p
            JOIN narratives n ON n.publication_id = p.publication_id
           WHERE p.doc_type = 'costar_narrative'
             AND n.market_id IN (SELECT market_id FROM submarkets WHERE submarket_id = ?)
           ORDER BY p.publication_date DESC LIMIT 1""",
        (submarket_id,),
    ).fetchone()
    pub_id = pub[0] if pub else None

    conn.execute(
        """INSERT INTO submarket_inventory (
                submarket_id, publication_id, hotel_count, room_count,
                luxury_upper_upscale_pct, segment_mix_json
           ) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(submarket_id) DO UPDATE SET
                publication_id = excluded.publication_id,
                hotel_count = excluded.hotel_count,
                room_count = excluded.room_count,
                luxury_upper_upscale_pct = excluded.luxury_upper_upscale_pct,
                segment_mix_json = excluded.segment_mix_json,
                extracted_at = datetime('now')""",
        (submarket_id, pub_id, hotel_count, room_count, lux_pct, seg_json),
    )
    conn.commit()

    return {
        "submarket": submarket_name,
        "hotel_count": hotel_count,
        "room_count": room_count,
        "luxury_upper_upscale_pct": lux_pct,
        "segment_count": len(p.get("segment_mix") or []),
        "cost_usd": response.usage.estimated_cost_usd,
        "cache_read": response.usage.cache_read_input_tokens,
    }


def cmd_extract_all(conn: sqlite3.Connection, pdf_dir: Path) -> int:
    pdfs = sorted(pdf_dir.glob("*Hospitality*Submarket*.pdf"))
    if not pdfs:
        print(f"no CoStar PDFs found in {pdf_dir}")
        return 1
    print(f"Found {len(pdfs)} CoStar PDFs in {pdf_dir}")

    client = get_default_client(conn=conn, purpose_hint="ingestion")
    total_cost = 0.0
    n_ok = n_err = 0
    for fp in pdfs:
        print(f"\n[{fp.name}]")
        try:
            r = extract_one(conn, fp, client)
            if r is None:
                continue
            n_ok += 1
            print(f"  hotels={r['hotel_count']}  rooms={r['room_count']}  "
                  f"lux/UU={(r['luxury_upper_upscale_pct'] or 0)*100:.0f}%  "
                  f"segments={r['segment_count']}  ${r['cost_usd']:.4f}")
            total_cost += r["cost_usd"]
        except Exception as e:
            print(f"  ERROR: {e!r}")
            n_err += 1
    print(f"\nDone: {n_ok} extracted, {n_err} errors, total ${total_cost:.4f}")
    return 0 if n_err == 0 else 1


def cmd_list(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """SELECT s.canonical_name AS submarket, m.canonical_name AS market,
                  i.hotel_count, i.room_count, i.luxury_upper_upscale_pct,
                  i.extracted_at, i.segment_mix_json
             FROM submarket_inventory i
             JOIN submarkets s ON i.submarket_id = s.submarket_id
             JOIN markets m ON s.market_id = m.market_id
             ORDER BY m.canonical_name, s.canonical_name""",
    ).fetchall()
    if not rows:
        print("(no inventory records yet)")
        return 0
    print(f"{'market':18s} {'submarket':28s} {'hotels':>6s} {'rooms':>7s} {'lux/UU':>7s}  segments  extracted")
    for r in rows:
        seg_n = len(json.loads(r[6] or "[]"))
        lp = (r[4] or 0) * 100
        print(f"{r[1][:18]:18s} {r[0][:28]:28s} "
              f"{r[2] or '-':>6} {r[3] or '-':>7} {lp:>6.0f}%  "
              f"{seg_n:>8}  {r[5]}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--file", help="single PDF path")
    p.add_argument("--pdf-dir", default=str(DEFAULT_PDF_DIR),
                   help=f"folder of CoStar PDFs (default: {DEFAULT_PDF_DIR})")
    p.add_argument("--list", action="store_true",
                   help="list extracted inventory records")
    args = p.parse_args()

    db = get_db_path()
    conn = connect(db)
    apply_schema(conn)

    if args.list:
        sys.exit(cmd_list(conn))

    if args.file:
        client = get_default_client(conn=conn, purpose_hint="ingestion")
        result = extract_one(conn, Path(args.file), client)
        if result:
            print(f"  hotels={result['hotel_count']}  rooms={result['room_count']}  "
                  f"lux/UU={(result['luxury_upper_upscale_pct'] or 0)*100:.0f}%")
        sys.exit(0 if result else 1)

    sys.exit(cmd_extract_all(conn, Path(args.pdf_dir)))


if __name__ == "__main__":
    main()
