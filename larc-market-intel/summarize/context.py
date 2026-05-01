"""Build the data context for a (market[, submarket], quarter) summary.

Every observation we hand to Claude carries a `cite_id` of the form
`<source_table>:<row_id>` so that citation tokens emitted by the model
(e.g. `[obs:forecast_periods:42]`) can be validated against this exact list.
Anything not in this list is a fabrication and must be rejected.

The context spans:
  - Quarterly + monthly performance for the target period and prior 4 quarters
  - Annual context (current year and forecast horizon)
  - Forecast vintages (multiple snapshots of the same forecast year)
  - Convention bookings (annual, multi-year)
  - Narrative excerpts (most recent LARC + CoStar reports for the market)
  - Recent transactions
  - Supply pipeline
  - Green Street grades + IRR + submarket cap rates

Coverage report explicitly lists which providers contributed and which
metrics are missing. The model uses this to avoid claims it can't back up.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date


@dataclass
class ObservationBundle:
    """One row of source data, tagged for citation."""

    cite_id:        str           # 'forecast_periods:42'
    source_table:   str
    source_id:      int
    summary:        str           # one-line human-readable representation
    raw:            dict          # the underlying data (for the LLM context)
    provider:       str           # LARC / CoStar / GreenStreet
    period:         str           # '2026-Q1', '2025-09', 'A:2026', etc.


@dataclass
class DataCoverage:
    """What we have and what we're missing for this (market, submarket, period)."""

    market_canonical:    str
    submarket:           str | None
    target_quarter:      str         # '1Q26'
    target_year:         int
    target_q:            int

    providers_present:   list[str]   = field(default_factory=list)
    providers_missing:   list[str]   = field(default_factory=list)
    metrics_present:     list[str]   = field(default_factory=list)
    metrics_missing:     list[str]   = field(default_factory=list)
    counts_by_table:     dict[str, int] = field(default_factory=dict)
    notes:               list[str]   = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "market": self.market_canonical,
            "submarket": self.submarket,
            "target_quarter": self.target_quarter,
            "providers_present": self.providers_present,
            "providers_missing": self.providers_missing,
            "metrics_present": self.metrics_present,
            "metrics_missing": self.metrics_missing,
            "counts_by_table": self.counts_by_table,
            "notes": self.notes,
        }


@dataclass
class SummaryContext:
    """Everything the LLM needs to write a summary for one (market, submarket, period)."""

    market_id:     int
    market_name:   str
    submarket_id:  int | None
    submarket_name: str | None
    quarter:       str           # '1Q26'
    year:          int
    quarter_num:   int

    coverage:      DataCoverage
    observations:  list[ObservationBundle]

    # Source records grouped for the prompt — Claude reads them in this order
    quarterly_perf:        list[ObservationBundle] = field(default_factory=list)
    monthly_perf:          list[ObservationBundle] = field(default_factory=list)
    annual_context:        list[ObservationBundle] = field(default_factory=list)
    forecast_vintages:     list[ObservationBundle] = field(default_factory=list)
    convention:            list[ObservationBundle] = field(default_factory=list)
    narrative:             list[ObservationBundle] = field(default_factory=list)
    transactions:          list[ObservationBundle] = field(default_factory=list)
    supply_pipeline:       list[ObservationBundle] = field(default_factory=list)
    gs_grades:             list[ObservationBundle] = field(default_factory=list)
    gs_irr:                list[ObservationBundle] = field(default_factory=list)

    def cite_id_set(self) -> set[str]:
        return {o.cite_id for o in self.observations}

    def coverage_score(self) -> int:
        """Rough proxy for how well-covered this market/submarket is.
        Used by batch mode to decide if data is sufficient to summarize."""
        return len(self.observations)

    def total_records(self) -> int:
        return len(self.observations)


# ---------------------------------------------------------------------------


_QUARTER_RE = re.compile(r"^([1-4])Q(\d{2}|\d{4})$", re.IGNORECASE)


def parse_quarter(q: str) -> tuple[int, int]:
    """Accept '1Q26', '1Q2026' → (year, quarter_num)."""
    m = _QUARTER_RE.match(q.strip())
    if not m:
        raise ValueError(f"unparseable quarter '{q}'; use form like '1Q26'")
    qn = int(m.group(1))
    y = int(m.group(2))
    if y < 100:
        y += 2000
    return y, qn


def _q_iso_start(year: int, q: int) -> str:
    """First day of the quarter."""
    m = {1: 1, 2: 4, 3: 7, 4: 10}[q]
    return f"{year:04d}-{m:02d}-01"


def _q_iso_end_month(year: int, q: int) -> tuple[int, int]:
    """Last month of the quarter (inclusive)."""
    m = {1: 3, 2: 6, 3: 9, 4: 12}[q]
    return year, m


# ---------------------------------------------------------------------------


def build_context(
    conn: sqlite3.Connection,
    market_name: str,
    quarter: str,
    submarket_name: str | None = None,
    historical_quarters: int = 4,
) -> SummaryContext:
    """Pull everything Claude needs for a single (market, submarket, quarter) summary.

    Resolution:
      - market_name → market_id via market_aliases (case-insensitive exact match,
        then canonical name match)
      - submarket_name (if provided) → submarket_id under that market
    """
    conn.row_factory = sqlite3.Row
    target_year, target_q = parse_quarter(quarter)

    # --- Resolve market and (optional) submarket ---
    market_row = conn.execute(
        "SELECT m.market_id, m.canonical_name FROM markets m "
        "WHERE LOWER(m.canonical_name) = LOWER(?) "
        "   OR m.market_id = (SELECT market_id FROM market_aliases "
        "                     WHERE LOWER(alias)=LOWER(?) LIMIT 1) "
        "LIMIT 1",
        (market_name, market_name),
    ).fetchone()
    if not market_row:
        raise ValueError(f"market '{market_name}' not found in DB")
    market_id = market_row["market_id"]
    market_canonical = market_row["canonical_name"]

    submarket_id: int | None = None
    submarket_canonical: str | None = None
    if submarket_name:
        smrow = conn.execute(
            "SELECT submarket_id, canonical_name FROM submarkets "
            "WHERE market_id = ? AND LOWER(canonical_name) = LOWER(?) LIMIT 1",
            (market_id, submarket_name),
        ).fetchone()
        if smrow:
            submarket_id = smrow["submarket_id"]
            submarket_canonical = smrow["canonical_name"]
        else:
            # Don't fail — proceed at market level with submarket name preserved
            submarket_canonical = submarket_name

    ctx = SummaryContext(
        market_id=market_id,
        market_name=market_canonical,
        submarket_id=submarket_id,
        submarket_name=submarket_canonical,
        quarter=quarter,
        year=target_year,
        quarter_num=target_q,
        coverage=DataCoverage(
            market_canonical=market_canonical,
            submarket=submarket_canonical,
            target_quarter=quarter,
            target_year=target_year,
            target_q=target_q,
        ),
        observations=[],
    )

    # --- Fetch all observation buckets ---
    _fetch_quarterly_performance(conn, ctx, historical_quarters)
    _fetch_monthly_performance(conn, ctx)
    _fetch_annual_context(conn, ctx)
    _fetch_forecast_vintages(conn, ctx)
    _fetch_convention(conn, ctx)
    _fetch_narrative(conn, ctx)
    _fetch_transactions(conn, ctx)
    _fetch_supply_pipeline(conn, ctx)
    _fetch_gs_grades(conn, ctx)
    _fetch_gs_irr(conn, ctx)

    # Aggregate observations + coverage
    all_buckets = [
        ctx.quarterly_perf, ctx.monthly_perf, ctx.annual_context,
        ctx.forecast_vintages, ctx.convention, ctx.narrative,
        ctx.transactions, ctx.supply_pipeline,
        ctx.gs_grades, ctx.gs_irr,
    ]
    for b in all_buckets:
        ctx.observations.extend(b)

    _populate_coverage(conn, ctx)
    return ctx


# ---------------------------------------------------------------------------
# Per-table fetchers
# ---------------------------------------------------------------------------


def _fmt_pct(v, signed: bool = False) -> str:
    """Format a decimal as a percentage. signed=True for change/growth fields."""
    if v is None:
        return "n/a"
    return f"{v*100:+.1f}%" if signed else f"{v*100:.1f}%"


def _fmt_chg(v) -> str:
    """Always-signed percentage (for change/growth values)."""
    return _fmt_pct(v, signed=True)


def _fmt_usd(v) -> str:
    return f"${v:,.2f}" if v is not None else "n/a"


def _fmt_int(v) -> str:
    return f"{int(v):,}" if v is not None else "n/a"


def _fetch_quarterly_performance(
    conn: sqlite3.Connection, ctx: SummaryContext, historical_quarters: int
) -> None:
    """Quarterly + rolling-12mo data. LARC HotelBIS is at market level;
    CoStar STR is at submarket level — both go in here."""
    # Build a list of (year, quarter) tuples covering target + N quarters back
    periods: list[tuple[int, int]] = []
    y, q = ctx.year, ctx.quarter_num
    for _ in range(historical_quarters + 1):
        periods.append((y, q))
        q -= 1
        if q == 0:
            q = 4
            y -= 1

    # market-level quarterly (LARC HotelBIS, GreenStreet baseline scenario)
    place = ",".join(["(?,?)"] * len(periods))
    args = [ctx.market_id]
    for yr, qn in periods:
        args += [yr, qn]
    rows = conn.execute(
        f"""SELECT fp.id, fp.year, fp.quarter, fp.is_forecast, fp.scenario,
                   fp.occupancy, fp.adr, fp.revpar, fp.supply_growth_pct,
                   fp.demand_growth_pct, fp.cap_rate, fp.hotel_ebitda_margin,
                   fp.hotel_value_index_2019, p.provider_code, p.publication_date
              FROM forecast_periods fp
              JOIN publications p ON fp.publication_id = p.publication_id
             WHERE fp.market_id = ? AND fp.submarket_id IS NULL
               AND fp.period_type = 'quarterly'
               AND (fp.year, fp.quarter) IN ({place})
               AND (fp.scenario IS NULL OR fp.scenario = 'baseline')
             ORDER BY p.provider_code, fp.year DESC, fp.quarter DESC""",
        args,
    ).fetchall()
    for r in rows:
        period = f"{r['year']}-Q{r['quarter']}"
        prov = r["provider_code"]
        scen = f" ({r['scenario']})" if r["scenario"] else ""
        fcst = " [forecast]" if r["is_forecast"] else ""
        summary = (
            f"{prov} {ctx.market_name} {period}{scen}{fcst}: "
            f"Occ={_fmt_pct(r['occupancy'])}, ADR={_fmt_usd(r['adr'])}, "
            f"RevPAR={_fmt_usd(r['revpar'])}, "
            f"Cap={_fmt_pct(r['cap_rate'])}, "
            f"EBITDAm={_fmt_pct(r['hotel_ebitda_margin'])}"
        )
        ctx.quarterly_perf.append(_obs(
            "forecast_periods", r["id"], summary,
            dict(r), prov, period,
        ))

    # submarket-level (CoStar STR rolling-12mo) — only if we resolved a submarket
    if ctx.submarket_id:
        rows = conn.execute(
            """SELECT fp.id, fp.year, fp.month, fp.is_forecast,
                      fp.occupancy, fp.adr, fp.revpar, fp.supply, fp.demand,
                      fp.demand_growth_pct, fp.supply_growth_pct,
                      p.provider_code, p.publication_date
                 FROM forecast_periods fp
                 JOIN publications p ON fp.publication_id = p.publication_id
                WHERE fp.submarket_id = ? AND fp.period_type = 'rolling_12mo'
                  AND fp.year = ? AND fp.month BETWEEN ? AND ?
                ORDER BY fp.year DESC, fp.month DESC""",
            (
                ctx.submarket_id, ctx.year,
                {1: 1, 2: 4, 3: 7, 4: 10}[ctx.quarter_num],
                {1: 3, 2: 6, 3: 9, 4: 12}[ctx.quarter_num],
            ),
        ).fetchall()
        for r in rows:
            period = f"{r['year']}-{r['month']:02d} (rolling-12)"
            prov = r["provider_code"]
            # Derive approximate room count from 12mo supply (room-nights / 365)
            supply_rn = r["supply"]
            room_count_approx = (
                f"~{int(round(supply_rn / 365 / 100) * 100):,} rooms"
                if supply_rn else "n/a"
            )
            summary = (
                f"{prov} {ctx.submarket_name} {period}: "
                f"Occ={_fmt_pct(r['occupancy'])}, ADR={_fmt_usd(r['adr'])}, "
                f"RevPAR={_fmt_usd(r['revpar'])}, "
                f"12moSupply={_fmt_int(supply_rn)} room-nights "
                f"(implied {room_count_approx}), "
                f"Demand_chg={_fmt_chg(r['demand_growth_pct'])}, "
                f"Supply_chg={_fmt_chg(r['supply_growth_pct'])}"
            )
            ctx.quarterly_perf.append(_obs(
                "forecast_periods", r["id"], summary,
                dict(r), prov, period,
            ))


def _fetch_monthly_performance(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    """12 most-recent monthly rows for the submarket (CoStar STR), pre-target."""
    if not ctx.submarket_id:
        return
    target_iso = _q_iso_start(ctx.year, ctx.quarter_num)
    rows = conn.execute(
        """SELECT fp.id, fp.year, fp.month, fp.adr, fp.revpar, fp.occupancy,
                  fp.revpar_growth_pct, p.provider_code
             FROM forecast_periods fp
             JOIN publications p ON fp.publication_id = p.publication_id
            WHERE fp.submarket_id = ? AND fp.period_type = 'rolling_12mo'
              AND fp.year * 100 + fp.month <= ? * 100 + ?
            ORDER BY fp.year DESC, fp.month DESC LIMIT 12""",
        (ctx.submarket_id, ctx.year, {1: 3, 2: 6, 3: 9, 4: 12}[ctx.quarter_num]),
    ).fetchall()
    for r in rows:
        period = f"{r['year']}-{r['month']:02d}"
        summary = (
            f"{r['provider_code']} {ctx.submarket_name} {period} (T12mo): "
            f"RevPAR={_fmt_usd(r['revpar'])} "
            f"({_fmt_chg(r['revpar_growth_pct'])} YoY), "
            f"Occ={_fmt_pct(r['occupancy'])}"
        )
        ctx.monthly_perf.append(_obs(
            "forecast_periods", r["id"], summary, dict(r),
            r["provider_code"], period,
        ))


def _fetch_annual_context(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    """Annual rows for current year + forecast horizon (next 5 years)."""
    rows = conn.execute(
        """SELECT fp.id, fp.year, fp.is_forecast, fp.occupancy, fp.adr, fp.revpar,
                  fp.cap_rate, fp.hotel_ebitda_margin, fp.hotel_value_index_2019,
                  fp.hotel_value_change_pct, p.provider_code, fp.scenario
             FROM forecast_periods fp
             JOIN publications p ON fp.publication_id = p.publication_id
            WHERE fp.market_id = ? AND fp.submarket_id IS NULL
              AND fp.period_type = 'annual'
              AND fp.year BETWEEN ? AND ?
              AND (fp.scenario IS NULL OR fp.scenario = 'baseline')
            ORDER BY p.provider_code, fp.year""",
        (ctx.market_id, ctx.year - 1, ctx.year + 5),
    ).fetchall()
    for r in rows:
        period = f"FY{r['year']}"
        scen = f" ({r['scenario']})" if r["scenario"] else ""
        fcst = " [forecast]" if r["is_forecast"] else ""
        summary = (
            f"{r['provider_code']} {ctx.market_name} {period}{scen}{fcst}: "
            f"Occ={_fmt_pct(r['occupancy'])}, ADR={_fmt_usd(r['adr'])}, "
            f"RevPAR={_fmt_usd(r['revpar'])}, "
            f"Cap={_fmt_pct(r['cap_rate'])}, "
            f"Value_chg={_fmt_chg(r['hotel_value_change_pct'])}"
        )
        ctx.annual_context.append(_obs(
            "forecast_periods", r["id"], summary, dict(r),
            r["provider_code"], period,
        ))


def _fetch_forecast_vintages(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    """Multiple forecast publications for the same year — shows how outlook evolved."""
    rows = conn.execute(
        """SELECT fp.id, p.publication_date, p.provider_code, p.publication_period,
                  fp.year, fp.revpar, fp.hotel_value_change_pct, fp.occupancy
             FROM forecast_periods fp
             JOIN publications p ON fp.publication_id = p.publication_id
            WHERE fp.market_id = ? AND fp.submarket_id IS NULL
              AND fp.period_type = 'annual' AND fp.is_forecast = 1
              AND fp.year IN (?, ?, ?)
              AND (fp.scenario IS NULL OR fp.scenario = 'baseline')
            ORDER BY fp.year, p.publication_date""",
        (ctx.market_id, ctx.year, ctx.year + 1, ctx.year + 5),
    ).fetchall()
    # Only emit if we have >1 vintage for the same year (otherwise it's noise)
    by_year: dict[int, list] = {}
    for r in rows:
        by_year.setdefault(r["year"], []).append(r)
    for yr, items in by_year.items():
        if len(items) < 2:
            continue
        for r in items:
            period = f"FY{yr} forecast as of {r['publication_period'] or r['publication_date']}"
            summary = (
                f"{r['provider_code']} {ctx.market_name} {period}: "
                f"RevPAR={_fmt_usd(r['revpar'])}, "
                f"Occ={_fmt_pct(r['occupancy'])}, "
                f"Value_chg={_fmt_chg(r['hotel_value_change_pct'])}"
            )
            ctx.forecast_vintages.append(_obs(
                "forecast_periods", r["id"], summary, dict(r),
                r["provider_code"], period,
            ))


def _fetch_convention(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    rows = conn.execute(
        """SELECT cb.id, cb.year, cb.period, cb.definite_room_nights,
                  cb.yoy_booking_pace, cb.pace_relative_to_2019,
                  p.provider_code
             FROM convention_bookings cb
             JOIN publications p ON cb.publication_id = p.publication_id
            WHERE cb.market_id = ?
              AND cb.year BETWEEN ? AND ?
            ORDER BY cb.year""",
        (ctx.market_id, ctx.year - 1, ctx.year + 4),
    ).fetchall()
    for r in rows:
        period = f"{r['year']}-{r['period']}"
        summary = (
            f"{r['provider_code']} {ctx.market_name} convention {period}: "
            f"DRN={_fmt_int(r['definite_room_nights'])}, "
            f"YoY={_fmt_chg(r['yoy_booking_pace'])}, "
            f"vs2019={_fmt_chg(r['pace_relative_to_2019'])}"
        )
        ctx.convention.append(_obs(
            "convention_bookings", r["id"], summary, dict(r),
            r["provider_code"], period,
        ))


def _fetch_narrative(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    """Most recent narrative passages, prioritized: prefer submarket-tagged
    publications when available."""
    rows = conn.execute(
        """SELECT n.id, n.section, n.subsection, n.text, n.sentiment,
                  n.key_metrics_json, p.provider_code, p.publication_date,
                  p.publication_period
             FROM narratives n
             JOIN publications p ON n.publication_id = p.publication_id
            WHERE n.market_id = ?
              AND p.publication_date >= date(?, '-9 months')
            ORDER BY p.publication_date DESC, n.ordinal""",
        (ctx.market_id, _q_iso_start(ctx.year, ctx.quarter_num)),
    ).fetchall()
    seen_sections: set[tuple[str, str]] = set()
    for r in rows:
        # Dedupe by (provider, section) — keep most recent
        key = (r["provider_code"], r["section"])
        if key in seen_sections:
            continue
        seen_sections.add(key)
        period = r["publication_period"] or r["publication_date"]
        text_excerpt = (r["text"] or "")[:1500]
        summary = (
            f"{r['provider_code']} narrative [{r['section']}"
            f"{('/' + r['subsection']) if r['subsection'] else ''}] {period} "
            f"(sentiment: {r['sentiment'] or 'neutral'}): {text_excerpt}"
        )
        ctx.narrative.append(_obs(
            "narratives", r["id"], summary,
            {**dict(r), "text": text_excerpt},
            r["provider_code"], period,
        ))


def _fetch_transactions(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    rows = conn.execute(
        """SELECT t.id, t.property_name, t.sale_date, t.units, t.price_total_usd,
                  t.price_per_unit_usd, t.buyer, t.seller, t.notes,
                  t.submarket, p.provider_code
             FROM transactions t
             JOIN publications p ON t.publication_id = p.publication_id
            WHERE t.market_id = ?
              AND p.publication_date >= date(?, '-12 months')
            ORDER BY t.sale_date_iso DESC NULLS LAST LIMIT 20""",
        (ctx.market_id, _q_iso_start(ctx.year, ctx.quarter_num)),
    ).fetchall()
    for r in rows:
        date_disp = r["sale_date"] or "?"
        summary = (
            f"{r['provider_code']} sale {date_disp}: {r['property_name']} "
            f"({_fmt_int(r['units'])} keys) {_fmt_usd(r['price_total_usd'])} "
            f"= {_fmt_usd(r['price_per_unit_usd'])}/key, buyer: {r['buyer'] or '?'}"
            f"{'. ' + r['notes'] if r['notes'] else ''}"
        )
        ctx.transactions.append(_obs(
            "transactions", r["id"], summary, dict(r),
            r["provider_code"], date_disp,
        ))


def _fetch_supply_pipeline(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    rows = conn.execute(
        """SELECT sp.id, sp.hotel_name, sp.submarket, sp.rooms,
                  sp.development_phase, sp.projected_opening, sp.brand_family,
                  sp.scale, p.provider_code
             FROM supply_pipeline sp
             JOIN publications p ON sp.publication_id = p.publication_id
            WHERE sp.market_id = ?
              AND p.publication_date >= date(?, '-9 months')
            ORDER BY sp.development_phase, sp.projected_opening_date_iso NULLS LAST""",
        (ctx.market_id, _q_iso_start(ctx.year, ctx.quarter_num)),
    ).fetchall()
    for r in rows:
        summary = (
            f"{r['provider_code']} pipeline: {r['hotel_name']} "
            f"({_fmt_int(r['rooms'])} rooms, {r['development_phase']}) "
            f"opening {r['projected_opening'] or '?'}, "
            f"submarket: {r['submarket'] or '?'}, brand: {r['brand_family'] or '?'}"
        )
        ctx.supply_pipeline.append(_obs(
            "supply_pipeline", r["id"], summary, dict(r),
            r["provider_code"], r["projected_opening"] or "tbd",
        ))


def _fetch_gs_grades(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    row = conn.execute(
        """SELECT g.id, g.tourism_score, g.business_orientation_score,
                  g.supply_barriers_score, g.desirability_score,
                  g.business_friendliness_score, g.median_hhi,
                  g.college_degree_pct, g.fiscal_health_score
             FROM green_street_grades g
            WHERE g.market_id = ? LIMIT 1""",
        (ctx.market_id,),
    ).fetchone()
    if not row:
        return
    summary = (
        f"GreenStreet {ctx.market_name} grades: "
        f"Tourism={row['tourism_score']}, "
        f"BusinessOrientation={row['business_orientation_score']}, "
        f"SupplyBarriers={row['supply_barriers_score']}, "
        f"Desirability={row['desirability_score']}, "
        f"FiscalHealth={row['fiscal_health_score']}, "
        f"MedianHHI={_fmt_usd(row['median_hhi'])}, "
        f"CollegeDeg={_fmt_pct(row['college_degree_pct'])}"
    )
    ctx.gs_grades.append(_obs(
        "green_street_grades", row["id"], summary, dict(row),
        "GreenStreet", "static",
    ))


def _fetch_gs_irr(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    row = conn.execute(
        """SELECT g.id, g.nominal_cap_rate, g.economic_cap_rate,
                  g.intermediate_noi_growth, g.long_term_noi_growth,
                  g.unlevered_irr, g.risk_adjusted_irr
             FROM green_street_irr g
            WHERE g.market_id = ? LIMIT 1""",
        (ctx.market_id,),
    ).fetchone()
    if not row:
        return
    summary = (
        f"GreenStreet {ctx.market_name} IRR model: "
        f"NominalCap={_fmt_pct(row['nominal_cap_rate'])}, "
        f"EconCap={_fmt_pct(row['economic_cap_rate'])}, "
        f"NOIgrowth(intermed)={_fmt_pct(row['intermediate_noi_growth'])}, "
        f"NOIgrowth(LT)={_fmt_pct(row['long_term_noi_growth'])}, "
        f"UnleveredIRR={_fmt_pct(row['unlevered_irr'])}, "
        f"RiskAdjIRR={_fmt_pct(row['risk_adjusted_irr'])}"
    )
    ctx.gs_irr.append(_obs(
        "green_street_irr", row["id"], summary, dict(row),
        "GreenStreet", "static",
    ))


# ---------------------------------------------------------------------------
# Coverage classifier
# ---------------------------------------------------------------------------


def _populate_coverage(conn: sqlite3.Connection, ctx: SummaryContext) -> None:
    """Fill in DataCoverage based on what we found."""
    cov = ctx.coverage
    counts: dict[str, int] = {}
    providers: set[str] = set()
    metrics: set[str] = set()
    for o in ctx.observations:
        counts[o.source_table] = counts.get(o.source_table, 0) + 1
        providers.add(o.provider)
        if "occupancy" in o.raw and o.raw.get("occupancy") is not None:
            metrics.add("occupancy")
        if "adr" in o.raw and o.raw.get("adr") is not None:
            metrics.add("adr")
        if "revpar" in o.raw and o.raw.get("revpar") is not None:
            metrics.add("revpar")
        if "cap_rate" in o.raw and o.raw.get("cap_rate") is not None:
            metrics.add("cap_rate")
        if "hotel_ebitda_margin" in o.raw and o.raw.get("hotel_ebitda_margin") is not None:
            metrics.add("hotel_ebitda_margin")
        if o.source_table == "convention_bookings":
            metrics.add("convention_pace")
        if o.source_table == "transactions":
            metrics.add("transactions")
        if o.source_table == "supply_pipeline":
            metrics.add("supply_pipeline")

    cov.counts_by_table = counts
    cov.providers_present = sorted(providers)
    expected = ["LARC", "CoStar", "GreenStreet"]
    cov.providers_missing = [p for p in expected if p not in providers]
    cov.metrics_present = sorted(metrics)
    expected_metrics = [
        "occupancy", "adr", "revpar", "cap_rate", "hotel_ebitda_margin",
        "convention_pace", "transactions", "supply_pipeline",
    ]
    cov.metrics_missing = [m for m in expected_metrics if m not in metrics]

    # Heuristic notes for the prompt
    if not ctx.quarterly_perf:
        cov.notes.append("No quarterly performance data for the target period.")
    if not ctx.narrative:
        cov.notes.append("No recent narrative reports — analysis is data-only.")
    if "convention_pace" not in metrics:
        cov.notes.append("No convention center data for this market.")
    if "GreenStreet" not in providers:
        cov.notes.append("No Green Street market grades / IRR available.")


def _obs(table: str, row_id: int, summary: str, raw: dict,
         provider: str, period: str) -> ObservationBundle:
    """Build an ObservationBundle. Strips non-JSON-serializable date fields."""
    safe_raw = {k: (v if not isinstance(v, (date,)) else v.isoformat())
                for k, v in raw.items()}
    return ObservationBundle(
        cite_id=f"{table}:{row_id}",
        source_table=table,
        source_id=row_id,
        summary=summary,
        raw=safe_raw,
        provider=provider,
        period=period,
    )
