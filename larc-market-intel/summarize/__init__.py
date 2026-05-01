"""Summary generation: pulls observations from the DB, calls Claude, validates citations."""
from summarize.citations import (
    Citation,
    CitationError,
    ParsedSummary,
    parse_and_validate,
    render_with_inline_markers,
)
from summarize.context import (
    DataCoverage,
    ObservationBundle,
    SummaryContext,
    build_context,
    parse_quarter,
)
from summarize.engine import (
    GeneratedSummary,
    GenerationResult,
    PROMPT_VERSION,
    SUMMARY_SCHEMA,
    generate_summary,
)

__all__ = [
    # context
    "DataCoverage", "ObservationBundle", "SummaryContext",
    "build_context", "parse_quarter",
    # citations
    "Citation", "CitationError", "ParsedSummary",
    "parse_and_validate", "render_with_inline_markers",
    # engine
    "GeneratedSummary", "GenerationResult", "PROMPT_VERSION",
    "SUMMARY_SCHEMA", "generate_summary",
]
