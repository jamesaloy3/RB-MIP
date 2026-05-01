"""Compare AI-generated 1Q26 summaries to the user's hand-written examples.

Reads the user's `Market_Summaries_2026Q1.xlsx` for the gold examples, joins
to summaries in the DB on (submarket name, version_type), and prints a
side-by-side diff with character-count and citation-count deltas.

Usage:
    python scripts/compare_summaries.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "market_intel.db"
GOLD_XLSX = Path(__file__).parent.parent.parent / "Market_Summaries_2026Q1.xlsx"


# Map from user's xlsx submarket names → DB canonical names
_NAME_MAP = {
    "Denver CBD":         ("Denver", "Denver CBD"),
    "Denver Airport East": ("Denver", "Denver Airport/East"),
    "Austin CBD":         ("Austin", "Austin CBD"),
    "Boston CBD":         ("Boston", "Boston CBD/Airport"),
    "Disneyland":         ("Orange County", "Disneyland"),
    "Fort Collins":       ("Colorado Area", "Fort Collins Area"),
    "Loveland":           ("Colorado Area", "Loveland Area"),
    "Santa Monica":       ("Los Angeles", "Santa Monica/Marina Del Rey"),
    "Irvine":             ("Orange County", "Irvine"),     # may not be in DB
    "Portland CBD":       ("Portland", "Portland CBD"),    # may not be in DB
}


def _strip_citations(text: str) -> str:
    return re.sub(r"\[obs:[^\]]+\]", "", text or "").strip()


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return 1
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    df = pd.read_excel(GOLD_XLSX, sheet_name=0)
    df["Quarter"] = df["Quarter"].astype(str)
    detailed_gold = {
        r["Market"]: r["Summary"]
        for _, r in df[df["Quarter"].str.contains("1Q26 Detailed", case=False)].iterrows()
    }
    summarized_gold = {
        r["Market"]: r["Summary"]
        for _, r in df[df["Quarter"].str.contains("1Q26 Summarized", case=False)].iterrows()
    }

    rows_total = 0
    matched = 0
    for user_name, gold_text in detailed_gold.items():
        if user_name not in _NAME_MAP:
            continue
        market_name, submarket_name = _NAME_MAP[user_name]
        rows_total += 1

        # Pick the latest detailed + summarized for this submarket
        det = conn.execute(
            """SELECT s.id, s.generated_text, s.generation_number, s.generated_at, s.model_used
                 FROM summaries s
                 JOIN markets m ON s.market_id = m.market_id
                 LEFT JOIN submarkets sm ON s.submarket_id = sm.submarket_id
                WHERE m.canonical_name = ? AND COALESCE(sm.canonical_name, '') = ?
                  AND s.quarter = '1Q26' AND s.version_type = 'detailed'
                ORDER BY s.generation_number DESC LIMIT 1""",
            (market_name, submarket_name),
        ).fetchone()
        sumz = conn.execute(
            """SELECT s.id, s.generated_text FROM summaries s
                 JOIN markets m ON s.market_id = m.market_id
                 LEFT JOIN submarkets sm ON s.submarket_id = sm.submarket_id
                WHERE m.canonical_name = ? AND COALESCE(sm.canonical_name, '') = ?
                  AND s.quarter = '1Q26' AND s.version_type = 'summarized'
                ORDER BY s.generation_number DESC LIMIT 1""",
            (market_name, submarket_name),
        ).fetchone()

        if not det:
            print(f"\n=== {user_name} — NOT GENERATED ===")
            continue

        matched += 1
        gen_det = _strip_citations(det["generated_text"])
        gen_sum = _strip_citations(sumz["generated_text"]) if sumz else ""
        gold_sum = summarized_gold.get(user_name, "")

        n_cites_det = conn.execute(
            "SELECT COUNT(*) FROM summary_citations WHERE summary_id=?", (det["id"],)
        ).fetchone()[0]
        n_cites_sum = conn.execute(
            "SELECT COUNT(*) FROM summary_citations WHERE summary_id=?", (sumz["id"],)
        ).fetchone()[0] if sumz else 0

        print(f"\n{'='*78}")
        print(f"{user_name}  →  DB: {market_name} / {submarket_name}")
        print(f"{'='*78}")
        print(f"DETAILED:    gold={len(gold_text):>5} chars   |   "
              f"generated={len(gen_det):>5} chars  ({n_cites_det} citations)  "
              f"gen#{det['generation_number']}  ({det['model_used']})")
        print(f"SUMMARIZED:  gold={len(gold_sum):>5} chars   |   "
              f"generated={len(gen_sum):>5} chars  ({n_cites_sum} citations)")
        print()
        print("--- GOLD (detailed) ---")
        print(gold_text)
        print()
        print("--- GENERATED (detailed, citations stripped) ---")
        print(gen_det)
        print()
        print("--- GOLD (summarized) ---")
        print(gold_sum)
        print()
        print("--- GENERATED (summarized, citations stripped) ---")
        print(gen_sum)

    print(f"\n\n=== Matched {matched}/{rows_total} hand-written 1Q26 examples ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
