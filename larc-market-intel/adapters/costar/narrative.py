"""CoStar Hospitality Submarket Report (PDF) adapter.

Workflow mirrors the LARC narrative adapter, but:
  - reports are SUBMARKET-level, not market-level
  - the prompt and section taxonomy are CoStar-specific
  - filename pattern is '<Submarket>-Hospitality-Submarket-<YYYY-MM-DD>.pdf'
  - market name comes from page-1 ("<Submarket>\n<Market> - <ST> USA")
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
from adapters.larc.narrative import NARRATIVE_SCHEMA  # same shape
from llm import LLMClient, get_default_client


PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "costar_narrative_v1.md"


class CoStarNarrativeAdapter(BaseAdapter):
    PROVIDER_CODE = "CoStar"
    DOC_TYPE = "costar_narrative"
    EXTRACTOR_VERSION = "0.1.0"

    # 'Denver CBD-Hospitality-Submarket-2026-04-27.pdf' → 'Denver CBD'
    FILENAME_RE = re.compile(
        r"^(.+?)-Hospitality-Submarket-(\d{4})-(\d{2})-(\d{2})\.pdf$",
        re.IGNORECASE,
    )

    def __init__(self, market_resolver, llm_client: LLMClient | None = None):
        super().__init__(market_resolver)
        self._explicit_client = llm_client
        self._conn = getattr(market_resolver, "conn", None)

    def parse(self, file_path: Path | str, pub: PublicationInfo) -> ParseResult:
        file_path = Path(file_path)

        # --- 1. Extract text + identify market/submarket -------------------
        text, n_pages = self._extract_text(file_path)
        if not text.strip():
            raise AdapterError(f"PDF has no extractable text: {file_path.name}")

        submarket_name, market_name, pub_date = self._detect_geography(
            file_path.name, text
        )
        if not market_name or not submarket_name:
            raise AdapterError(
                f"could not determine market+submarket from {file_path.name}"
            )

        # Override pub date from filename (CoStar always carries it)
        if pub_date:
            pub.publication_date = pub_date
            y = int(pub_date[:4])
            m = int(pub_date[5:7])
            q = (m - 1) // 3 + 1
            pub.publication_period = f"{q}Q{y % 100:02d}"

        try:
            market_id = self.resolve_market(market_name)
        except UnknownMarketError as e:
            raise AdapterError(str(e)) from e
        submarket_id = self.resolve_submarket(market_id, submarket_name)

        # --- 2. Send to Claude ---------------------------------------------
        client = self._explicit_client or get_default_client(
            conn=self._conn,
            publication_id=pub.publication_id,
            purpose_hint="ingestion",
        )
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        user_msg = (
            f"Submarket: {submarket_name}\n"
            f"Market: {market_name}\n"
            f"Publication date: {pub.publication_date}\n"
            f"Pages: {n_pages}\n\n"
            f"--- BEGIN REPORT TEXT ---\n{text}\n--- END REPORT TEXT ---"
        )
        response = client.create(
            purpose="costar_narrative",
            system_blocks=[{"type": "text", "text": system_prompt}],
            messages=[{"role": "user", "content": user_msg}],
            response_schema=NARRATIVE_SCHEMA,
            max_tokens=16384,
            metadata={
                "market": market_name,
                "submarket": submarket_name,
                "filename": pub.source_filename,
            },
        )

        if response.parsed is None:
            raise AdapterError(
                f"Claude returned non-JSON output for {file_path.name}: "
                f"{response.text[:200]}"
            )

        # --- 3. Build ParseResult ------------------------------------------
        return self._build_result(response.parsed, market_id, submarket_id, pub)

    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(file_path: Path) -> tuple[str, int]:
        chunks: list[str] = []
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages):
                t = page.extract_text() or ""
                # Strip cover-page + footer boilerplate
                t = re.sub(r"^Hospitality Submarket Report\s*$", "", t, flags=re.M)
                t = re.sub(r"^© ?\d{4} CoStar Group.*$", "", t, flags=re.M)
                t = re.sub(r"^Realberry © \d{4}.*$", "", t, flags=re.M)
                chunks.append(f"[PAGE {i+1}]\n{t.strip()}")
            n_pages = len(pdf.pages)
        return "\n\n".join(chunks), n_pages

    def _detect_geography(
        self, filename: str, text: str
    ) -> tuple[str | None, str | None, str | None]:
        """Returns (submarket, market, pub_date_iso)."""
        m = self.FILENAME_RE.match(filename)
        submarket = m.group(1).strip() if m else None
        pub_date = (
            f"{m.group(2)}-{m.group(3)}-{m.group(4)}" if m else None
        )
        # Market: find a line that IS the market header. Two known shapes:
        #   '<Market> - <ST> USA'   e.g. 'Denver - CO USA', 'Austin - TX USA'
        #   '<Market> Area USA'      e.g. 'Colorado Area USA' (non-MSA aggregates)
        market = None
        head = text[:1500]
        m2 = re.search(
            r"^([A-Z][a-zA-Z\.]+(?:[ \t]+[A-Z][a-zA-Z\.]+)*)[ \t]+-[ \t]+([A-Z]{2})[ \t]+USA[ \t]*$",
            head,
            re.MULTILINE,
        )
        if m2:
            market = f"{m2.group(1).strip()} - {m2.group(2)} USA"
        else:
            m3 = re.search(
                r"^([A-Z][a-zA-Z\.]+(?:[ \t]+[A-Z][a-zA-Z\.]+)*[ \t]+Area)[ \t]+USA[ \t]*$",
                head,
                re.MULTILINE,
            )
            if m3:
                market = f"{m3.group(1).strip()} USA"
        return submarket, market, pub_date

    def _build_result(
        self, parsed: dict, market_id: int, submarket_id: int | None,
        pub: PublicationInfo
    ) -> ParseResult:
        result = ParseResult()

        for n in parsed.get("narratives", []) or []:
            section = (n.get("section") or "other").strip()
            text = (n.get("text") or "").strip()
            if not text:
                continue
            result.narratives.append({
                "market_id":        market_id,
                "section":          section,
                "subsection":       n.get("subsection"),
                "text":             text,
                "ordinal":          len(result.narratives),
                "sentiment":        n.get("sentiment"),
                "key_metrics_json": json.dumps(n.get("key_metrics") or []),
                "page_refs":        None,
                "entities_json":    None,
            })

        for t in parsed.get("transactions", []) or []:
            name = (t.get("property_name") or "").strip()
            if not name:
                continue
            sale_date = t.get("sale_date")
            sale_iso = self._normalize_date(sale_date)
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
                "notes":              t.get("notes"),
            })

        for s in parsed.get("supply_pipeline", []) or []:
            name = (s.get("hotel_name") or "").strip()
            if not name:
                continue
            opening = s.get("projected_opening")
            opening_iso = self._normalize_date(opening)
            result.supply_pipeline.append({
                "market_id":                  market_id,
                "hotel_name":                 name,
                "submarket":                  s.get("submarket"),
                "rooms":                      s.get("rooms"),
                "development_phase":          s.get("development_phase") or "proposed",
                "projected_opening":          opening,
                "projected_opening_date_iso": opening_iso,
                "brand_family":               s.get("brand_family"),
                "scale":                      s.get("scale"),
                "source":                     "costar_narrative",
            })

        result.warnings = list(self.warnings)
        return result

    @staticmethod
    def _normalize_date(s: str | None) -> str | None:
        if not s:
            return None
        s = s.strip()
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
        if m:
            return s
        m = re.match(r"^(\d{4})[-\s]*Q([1-4])$", s)
        if m:
            y, q = int(m.group(1)), int(m.group(2))
            return f"{y:04d}-{ {1:1,2:4,3:7,4:10}[q] :02d}-01"
        try:
            import pandas as pd
            ts = pd.to_datetime(s, errors="coerce")
            if pd.notna(ts):
                return ts.strftime("%Y-%m-%d")
        except Exception:
            pass
        return None
