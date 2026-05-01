"""LARC Market Intelligence Report (PDF) adapter.

Workflow:
  1. Pull text via pdfplumber. LARC PDFs have an embedded text layer; OCR is
     not required.
  2. Identify the market name from the filename + first-page header.
  3. Send text + extraction schema to Claude. System prompt is cached, so
     repeat ingestions across many LARC PDFs in a single run get ~90% off
     the system-prompt portion of input cost.
  4. Validate the parsed JSON, populate ParseResult records keyed to the
     canonical narrative / transactions / supply_pipeline tables.

Token tracking: every Claude call writes to llm_usage. To audit a run:

    SELECT publication_id, purpose, model,
           SUM(input_tokens), SUM(output_tokens), SUM(estimated_cost_usd)
      FROM llm_usage GROUP BY publication_id, purpose;
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pdfplumber

from adapters.base import (
    AdapterError,
    BaseAdapter,
    ParseResult,
    PublicationInfo,
    UnknownMarketError,
)
from llm import LLMClient, get_default_client


PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "larc_narrative_v1.md"

# JSON schema for the structured extraction.
NARRATIVE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "report_market_name": {"type": "string"},
        "narratives": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "section":    {"type": "string"},
                    # Use empty string "" when no subsection (avoids union-type budget)
                    "subsection": {"type": "string"},
                    "text":       {"type": "string"},
                    "sentiment":  {
                        "type": "string",
                        "enum": ["positive", "negative", "neutral", "mixed", ""],
                    },
                    "key_metrics": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "metric":  {"type": "string"},
                                # Bake unit + ranking INTO value rather than separate
                                # fields ("16 of 62 markets", "5.4% YoY")
                                "value":   {"type": "string"},
                            },
                            "required": ["metric", "value"],
                        },
                    },
                },
                "required": ["section", "subsection", "text", "sentiment", "key_metrics"],
            },
        },
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "property_name":      {"type": "string"},
                    "sale_date":          {"type": ["string", "null"]},
                    "submarket":          {"type": ["string", "null"]},
                    "units":              {"type": ["integer", "null"]},
                    "price_total_usd":    {"type": ["number",  "null"]},
                    "price_per_unit_usd": {"type": ["number",  "null"]},
                    "buyer":              {"type": ["string", "null"]},
                    "seller":             {"type": ["string", "null"]},
                    # LARC Score: emit as integer; absent = 0 (no separate null)
                    "larc_score":         {"type": "integer"},
                    "notes":              {"type": ["string", "null"]},
                },
                "required": [
                    "property_name", "sale_date", "submarket", "units",
                    "price_total_usd", "price_per_unit_usd", "buyer", "seller",
                    "larc_score", "notes",
                ],
            },
        },
        "supply_pipeline": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "hotel_name":        {"type": "string"},
                    "submarket":         {"type": ["string", "null"]},
                    "rooms":             {"type": ["integer", "null"]},
                    "development_phase": {
                        "type": "string",
                        "enum": ["planning", "under_construction",
                                 "recently_opened", "proposed", "closed"],
                    },
                    "projected_opening": {"type": ["string", "null"]},
                    "brand_family":      {"type": ["string", "null"]},
                    "scale":             {"type": ["string", "null"]},
                },
                "required": [
                    "hotel_name", "submarket", "rooms", "development_phase",
                    "projected_opening", "brand_family", "scale",
                ],
            },
        },
    },
    "required": ["report_market_name", "narratives", "transactions", "supply_pipeline"],
}


class NarrativeAdapter(BaseAdapter):
    PROVIDER_CODE = "LARC"
    DOC_TYPE = "larc_narrative"
    EXTRACTOR_VERSION = "0.1.0"

    # Filename markets used as a fallback when the first page doesn't reveal it.
    FILENAME_MARKET_RE = re.compile(
        r"LARC[_\s]+([A-Z][A-Z\s]+?)(?:[_\s]+\d?Q\d{2,4})?\.pdf$",
        re.IGNORECASE,
    )

    # LARC PDFs sometimes use abbreviations in filenames. Map to canonical
    # before resolving via the alias table.
    FILENAME_ABBREV_MAP: dict[str, str] = {
        "LA":      "Los Angeles",
        "NYC":     "New York",
        "DFW":     "Dallas",
        "DC":      "Washington",
        "SF":      "San Francisco",
        "PHL":     "Philadelphia",
        "ATL":     "Atlanta",
        "ORD":     "Chicago",
    }

    def __init__(self, market_resolver, llm_client: LLMClient | None = None):
        super().__init__(market_resolver)
        self._explicit_client = llm_client
        # MarketResolver carries the connection; pull from there for usage tracking
        self._conn = getattr(market_resolver, "conn", None)

    def parse(self, file_path: Path | str, pub: PublicationInfo) -> ParseResult:
        file_path = Path(file_path)

        # --- 1. Extract text + identify market -----------------------------
        text, n_pages = self._extract_text(file_path)
        if not text.strip():
            raise AdapterError(f"PDF has no extractable text: {file_path.name}")

        market_name = self._detect_market(file_path.name, text)
        if not market_name:
            raise AdapterError(
                f"could not determine market from filename or content: {file_path.name}"
            )
        try:
            market_id = self.resolve_market(market_name)
        except UnknownMarketError as e:
            raise AdapterError(str(e)) from e

        # --- 2. Send to Claude ---------------------------------------------
        client = self._explicit_client or get_default_client(
            conn=self._conn,
            publication_id=pub.publication_id,
            purpose_hint="ingestion",
        )
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

        user_msg = (
            f"Report market: {market_name}\n"
            f"Publication period: {pub.publication_period or '(unknown)'}\n"
            f"Source file: {pub.source_filename}\n"
            f"Pages: {n_pages}\n\n"
            f"--- BEGIN REPORT TEXT ---\n{text}\n--- END REPORT TEXT ---"
        )

        response = client.create(
            purpose="larc_narrative",
            system_blocks=[{"type": "text", "text": system_prompt}],
            messages=[{"role": "user", "content": user_msg}],
            response_schema=NARRATIVE_SCHEMA,
            max_tokens=16384,
            metadata={"market": market_name, "filename": pub.source_filename},
        )

        if response.parsed is None:
            raise AdapterError(
                f"Claude returned non-JSON output for {file_path.name}: "
                f"{response.text[:200]}"
            )

        # --- 3. Convert to ParseResult records -----------------------------
        return self._build_result(response.parsed, market_id, pub)

    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(file_path: Path) -> tuple[str, int]:
        """Read all pages and join. Light cleanup of repeating headers/footers."""
        chunks: list[str] = []
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages):
                t = page.extract_text() or ""
                t = re.sub(r"^\s*Market Intelligence Report\s*$", "", t, flags=re.M)
                t = re.sub(r"www\.larcanalytics\.com.*$", "", t, flags=re.M)
                t = re.sub(r"^\s*LODGING ANALYTICS RESEARCH & CONSULTING\s*$", "",
                           t, flags=re.M)
                chunks.append(f"[PAGE {i+1}]\n{t.strip()}")
            n_pages = len(pdf.pages)
        return "\n\n".join(chunks), n_pages

    def _detect_market(self, filename: str, text: str) -> str | None:
        """Filename pattern first; fallback to first-page header."""
        m = self.FILENAME_MARKET_RE.search(filename)
        if m:
            raw = m.group(1).strip()
            # Check abbreviation map BEFORE titlecasing
            if raw.upper() in self.FILENAME_ABBREV_MAP:
                return self.FILENAME_ABBREV_MAP[raw.upper()]
            return raw.title()
        # Fallback: scan first 500 chars for "LODGING ANALYTICS ... <Market>, ST"
        head = text[:1500]
        m2 = re.search(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*),\s*[A-Z]{2}\b", head)
        if m2:
            return m2.group(1)
        return None

    def _build_result(
        self, parsed: dict, market_id: int, pub: PublicationInfo
    ) -> ParseResult:
        result = ParseResult()

        for n in parsed.get("narratives", []) or []:
            section = (n.get("section") or "other").strip()
            text = (n.get("text") or "").strip()
            if not text:
                continue
            # Convert sentinels back to NULL where the schema uses ""
            sentiment = n.get("sentiment") or None
            subsection = n.get("subsection") or None
            result.narratives.append({
                "market_id":        market_id,
                "section":          section,
                "subsection":       subsection,
                "text":             text,
                "ordinal":          len(result.narratives),
                "sentiment":        sentiment,
                "key_metrics_json": json.dumps(n.get("key_metrics") or []),
                "page_refs":        None,
                "entities_json":    None,
            })

        for t in parsed.get("transactions", []) or []:
            name = (t.get("property_name") or "").strip()
            if not name:
                continue
            sale_date = t.get("sale_date")
            sale_iso = self._normalize_sale_date(sale_date)
            # LARC reports include a "LARC Score" per transaction. Schema doesn't
            # have a column for it (yet), so prepend to notes for round-trip.
            larc_score = t.get("larc_score") or 0   # 0 = sentinel for "not stated"
            base_notes = (t.get("notes") or "").strip()
            if larc_score:
                score_str = f"LARC Score: {larc_score}"
                notes = f"{score_str}. {base_notes}" if base_notes else score_str
            else:
                notes = base_notes or None
            result.transactions.append({
                "market_id":          market_id,
                "property_name":      name,
                "sale_date":          sale_date,
                "sale_date_iso":      sale_iso,
                "submarket":          t.get("submarket"),
                "units":              t.get("units"),
                "price_total_usd":    t.get("price_total_usd"),
                "price_per_unit_usd": t.get("price_per_unit_usd"),
                "buyer":              t.get("buyer"),
                "seller":             t.get("seller"),
                "notes":              notes,
            })

        for s in parsed.get("supply_pipeline", []) or []:
            name = (s.get("hotel_name") or "").strip()
            if not name:
                continue
            phase = s.get("development_phase")
            opening = s.get("projected_opening")
            opening_iso = self._normalize_opening(opening)
            result.supply_pipeline.append({
                "market_id":                  market_id,
                "hotel_name":                 name,
                "submarket":                  s.get("submarket"),
                "rooms":                      s.get("rooms"),
                "development_phase":          phase or "proposed",
                "projected_opening":          opening,
                "projected_opening_date_iso": opening_iso,
                "brand_family":               s.get("brand_family"),
                "scale":                      s.get("scale"),
                "source":                     "larc_narrative",
            })

        result.warnings = list(self.warnings)
        return result

    @staticmethod
    def _normalize_sale_date(s: str | None) -> str | None:
        """Best-effort 'YYYY-MM-DD' from 'Q3 2025', '2025-Q3', 'Sep 2025', etc."""
        if not s:
            return None
        s = s.strip()
        m = re.match(r"^(\d{4})[-\s]*Q([1-4])$", s)
        if m:
            y, q = int(m.group(1)), int(m.group(2))
            month = {1: 1, 2: 4, 3: 7, 4: 10}[q]
            return f"{y:04d}-{month:02d}-01"
        m = re.match(r"^Q([1-4])\s+(\d{4})$", s, re.IGNORECASE)
        if m:
            q, y = int(m.group(1)), int(m.group(2))
            month = {1: 1, 2: 4, 3: 7, 4: 10}[q]
            return f"{y:04d}-{month:02d}-01"
        try:
            import pandas as pd
            ts = pd.to_datetime(s, errors="coerce")
            if pd.notna(ts):
                return ts.strftime("%Y-%m-%d")
        except Exception:
            pass
        return None

    @staticmethod
    def _normalize_opening(s: str | None) -> str | None:
        if not s:
            return None
        return NarrativeAdapter._normalize_sale_date(s)
