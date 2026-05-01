"""Summary generation engine.

Workflow:
  1. build_context()   — pull observations from DB
  2. _build_user_msg() — format coverage + observations into a Claude-ready prompt
  3. client.create()   — single Claude call, system prompt cached
  4. parse_and_validate — strip citation tokens, reject fabrications
  5. persist_summary   — write detailed + summarized rows to summaries table,
                         plus per-citation rows in summary_citations

Versioning: every call inserts NEW rows (never overwrites). `generation_number`
auto-increments per (market_id, submarket_id, quarter, version_type).
`edited_text` stays NULL — the UI sets that when the user hand-edits.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from llm import LLMClient, get_default_client
from summarize.citations import CitationError, ParsedSummary, parse_and_validate
from summarize.context import SummaryContext, build_context


PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "summary_v1.md"
PROMPT_VERSION = "summary_v1"

# Hard ceilings (characters in rendered text, citations stripped)
DETAILED_MAX_CHARS = 2200
SUMMARIZED_MAX_CHARS = 600
# Targets (used in the regen feedback message)
DETAILED_TARGET_CHARS = 1800
SUMMARIZED_TARGET_CHARS = 450


SUMMARY_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "detailed":      {"type": "string"},
        "summarized":    {"type": "string"},
        "coverage_notes": {"type": "string"},
    },
    "required": ["detailed", "summarized", "coverage_notes"],
}


@dataclass
class GeneratedSummary:
    """Single (detailed | summarized) summary that has been validated and persisted."""

    summary_id:     int
    version_type:   str            # 'detailed' | 'summarized'
    rendered_text:  str            # citations stripped
    raw_text:       str            # with citation tokens
    parsed:         ParsedSummary  # citation map
    char_count:     int


@dataclass
class GenerationResult:
    """Output of generate_summary."""

    market_id:    int
    market_name:  str
    submarket_id: int | None
    submarket_name: str | None
    quarter:      str
    detailed:     GeneratedSummary
    summarized:   GeneratedSummary
    coverage_notes: str
    total_cost_usd: float
    cache_read_tokens: int


# ---------------------------------------------------------------------------


def generate_summary(
    conn: sqlite3.Connection,
    market_name: str,
    quarter: str,
    submarket_name: str | None = None,
    llm_client: LLMClient | None = None,
    historical_quarters: int = 4,
) -> GenerationResult:
    """End-to-end: build context → call Claude → validate → persist."""
    ctx = build_context(
        conn,
        market_name=market_name,
        quarter=quarter,
        submarket_name=submarket_name,
        historical_quarters=historical_quarters,
    )
    if ctx.total_records() == 0:
        raise ValueError(
            f"no observations found for {market_name}"
            f"{' / ' + submarket_name if submarket_name else ''} {quarter}"
        )

    client = llm_client or get_default_client(
        conn=conn, publication_id=None, purpose_hint="summary",
    )

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_msg = _build_user_msg(ctx)

    messages: list[dict] = [{"role": "user", "content": user_msg}]
    response = client.create(
        purpose=f"summary_{ctx.market_name}_{ctx.quarter}",
        system_blocks=[{"type": "text", "text": system_prompt}],
        messages=messages,
        response_schema=SUMMARY_SCHEMA,
        max_tokens=8192,
        metadata={
            "market":    ctx.market_name,
            "submarket": ctx.submarket_name,
            "quarter":   ctx.quarter,
            "n_obs":     ctx.total_records(),
        },
    )

    if response.parsed is None:
        raise RuntimeError(
            f"Claude returned non-JSON for {market_name} {quarter}: "
            f"{response.text[:200]}"
        )

    valid_ids = ctx.cite_id_set()
    detailed_parsed = parse_and_validate(response.parsed["detailed"], valid_ids)
    summarized_parsed = parse_and_validate(response.parsed["summarized"], valid_ids)

    # Regen-on-overlength: if either is over the cap, give the model one chance
    # to revise. Keeps the system prompt cached, so the regen call is cheap.
    overlength_msgs: list[str] = []
    if len(detailed_parsed.rendered_text) > DETAILED_MAX_CHARS:
        overlength_msgs.append(
            f"detailed was {len(detailed_parsed.rendered_text)} chars "
            f"(cap {DETAILED_MAX_CHARS}, target {DETAILED_TARGET_CHARS})"
        )
    if len(summarized_parsed.rendered_text) > SUMMARIZED_MAX_CHARS:
        overlength_msgs.append(
            f"summarized was {len(summarized_parsed.rendered_text)} chars "
            f"(cap {SUMMARIZED_MAX_CHARS}, target {SUMMARIZED_TARGET_CHARS})"
        )
    if overlength_msgs:
        messages.append({"role": "assistant", "content": json.dumps(response.parsed)})
        messages.append({
            "role": "user",
            "content": (
                "Your previous response is over the length cap: "
                + "; ".join(overlength_msgs)
                + ". Please regenerate, cutting content per the trim priority "
                "in the system prompt. Both versions must be under their caps. "
                "Keep all citations valid. Return JSON only."
            ),
        })
        response = client.create(
            purpose=f"summary_{ctx.market_name}_{ctx.quarter}_regen",
            system_blocks=[{"type": "text", "text": system_prompt}],
            messages=messages,
            response_schema=SUMMARY_SCHEMA,
            max_tokens=8192,
            metadata={"regen": True, "reason": "overlength"},
        )
        if response.parsed is None:
            raise RuntimeError(
                f"Regen also returned non-JSON: {response.text[:200]}"
            )
        detailed_parsed = parse_and_validate(response.parsed["detailed"], valid_ids)
        summarized_parsed = parse_and_validate(response.parsed["summarized"], valid_ids)

    # Reject if either summary fabricated citations
    if detailed_parsed.fabricated:
        raise CitationError(
            f"detailed summary cites unknown observations: "
            f"{detailed_parsed.invalid_tokens[:5]}{'...' if len(detailed_parsed.invalid_tokens) > 5 else ''}"
        )
    if summarized_parsed.fabricated:
        raise CitationError(
            f"summarized summary cites unknown observations: "
            f"{summarized_parsed.invalid_tokens[:5]}{'...' if len(summarized_parsed.invalid_tokens) > 5 else ''}"
        )

    # Persist
    detailed_obj = _persist(
        conn, ctx, "detailed", response.parsed["detailed"],
        detailed_parsed, client.model,
    )
    summarized_obj = _persist(
        conn, ctx, "summarized", response.parsed["summarized"],
        summarized_parsed, client.model,
    )

    return GenerationResult(
        market_id=ctx.market_id,
        market_name=ctx.market_name,
        submarket_id=ctx.submarket_id,
        submarket_name=ctx.submarket_name,
        quarter=ctx.quarter,
        detailed=detailed_obj,
        summarized=summarized_obj,
        coverage_notes=response.parsed.get("coverage_notes", ""),
        total_cost_usd=response.usage.estimated_cost_usd,
        cache_read_tokens=response.usage.cache_read_input_tokens,
    )


# ---------------------------------------------------------------------------


def _build_user_msg(ctx: SummaryContext) -> str:
    """Format the coverage report + observation buckets into a single user message.

    Each observation is rendered as `[cite_id]  human-readable summary` so the
    model can quote and cite directly without seeing JSON noise.
    """
    parts: list[str] = []

    # Header
    header = f"Market: {ctx.market_name}"
    if ctx.submarket_name:
        header += f"\nSubmarket: {ctx.submarket_name}"
    header += f"\nQuarter: {ctx.quarter}"
    parts.append(header)

    # Coverage report
    parts.append("\n=== Coverage report ===")
    parts.append(json.dumps(ctx.coverage.to_dict(), indent=2))

    # Observations grouped by category
    sections = [
        ("Quarterly performance",      ctx.quarterly_perf),
        ("Monthly rolling-12 (CoStar)", ctx.monthly_perf),
        ("Annual context + forecast horizon", ctx.annual_context),
        ("Forecast vintages (same year, multiple snapshots)", ctx.forecast_vintages),
        ("Convention center bookings", ctx.convention),
        ("Narrative passages",         ctx.narrative),
        ("Recent transactions",        ctx.transactions),
        ("Supply pipeline",            ctx.supply_pipeline),
        ("Green Street market grades", ctx.gs_grades),
        ("Green Street IRR model",     ctx.gs_irr),
    ]

    for title, bucket in sections:
        if not bucket:
            continue
        parts.append(f"\n=== {title} ({len(bucket)} records) ===")
        for o in bucket:
            parts.append(f"[{o.cite_id}]  {o.summary}")

    parts.append(
        "\n=== End of context ===\n\n"
        "Now write the detailed and summarized summaries per the system prompt. "
        "Return JSON only."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------


def _persist(
    conn: sqlite3.Connection,
    ctx: SummaryContext,
    version_type: str,
    raw_text: str,
    parsed: ParsedSummary,
    model: str,
) -> GeneratedSummary:
    """Insert one summary row + its citation rows. Returns GeneratedSummary."""
    # Determine generation_number (next sequence for this key)
    row = conn.execute(
        """SELECT COALESCE(MAX(generation_number), 0) + 1 AS next_n
             FROM summaries
            WHERE market_id = ?
              AND COALESCE(submarket_id, -1) = COALESCE(?, -1)
              AND quarter = ? AND version_type = ?""",
        (ctx.market_id, ctx.submarket_id, ctx.quarter, version_type),
    ).fetchone()
    gen_n = int(row[0]) if row else 1

    source_record_ids: dict[str, list[int]] = {}
    for o in ctx.observations:
        source_record_ids.setdefault(o.source_table, []).append(o.source_id)

    cur = conn.execute(
        """INSERT INTO summaries (
                market_id, submarket_id, quarter, version_type,
                generation_number, generated_text, prompt_version,
                model_used, generated_at, data_coverage_json,
                source_record_ids_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ctx.market_id, ctx.submarket_id, ctx.quarter, version_type,
            gen_n, raw_text, PROMPT_VERSION,
            model,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            json.dumps(ctx.coverage.to_dict()),
            json.dumps(source_record_ids),
        ),
    )
    summary_id = int(cur.lastrowid)

    # Citation rows
    for cit in parsed.citations:
        for cid in cit.cite_ids:
            tbl, _, rid = cid.partition(":")
            try:
                source_id = int(rid)
            except ValueError:
                continue
            conn.execute(
                """INSERT INTO summary_citations (
                       summary_id, citation_token, source_table, source_id,
                       char_start, char_end
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    summary_id, cit.citation_token, tbl, source_id,
                    cit.char_start, cit.char_end,
                ),
            )

    conn.commit()
    return GeneratedSummary(
        summary_id=summary_id,
        version_type=version_type,
        rendered_text=parsed.rendered_text,
        raw_text=raw_text,
        parsed=parsed,
        char_count=len(parsed.rendered_text),
    )
