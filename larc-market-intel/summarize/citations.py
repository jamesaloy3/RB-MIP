"""Parse and validate `[obs:source_table:row_id]` citation tokens.

The model is instructed to attach a token after every numerical claim. We:
  1. Extract every token (with character offset) before stripping them
  2. Reject any token that doesn't match a cite_id supplied in the context
  3. Return both the rendered text (citations stripped) and the citation map
     (for the UI to render hover-tooltips and click-throughs)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Matches: [obs:forecast_periods:709] or [obs:forecast_periods:709,convention_bookings:103]
_CITE_RE = re.compile(
    r"\[obs:(?P<body>[a-zA-Z_]+:\d+(?:\s*,\s*[a-zA-Z_]+:\d+)*)\]"
)
# Within a body, individual cite_ids
_INNER_RE = re.compile(r"([a-zA-Z_]+):(\d+)")


@dataclass
class Citation:
    """One citation occurrence in the rendered text."""

    citation_token: str           # the full '[obs:...]' token as the model wrote it
    cite_ids:       list[str]     # parsed cite_ids: ['forecast_periods:709', ...]
    char_start:     int           # offset in RENDERED text (after token stripping)
    char_end:       int           # offset of end (== char_start; tokens are zero-width after strip)


@dataclass
class ParsedSummary:
    """Result of parse_and_validate."""

    rendered_text:  str               # text with citation tokens removed
    citations:      list[Citation]
    invalid_tokens: list[str]         # cite_ids that didn't match any supplied observation
    fabricated:     bool              # True if any invalid_tokens

    def cite_id_set(self) -> set[str]:
        ids: set[str] = set()
        for c in self.citations:
            ids.update(c.cite_ids)
        return ids


class CitationError(Exception):
    """Raised when citations cannot be parsed or are fabricated."""


def parse_and_validate(text: str, valid_cite_ids: set[str]) -> ParsedSummary:
    """Strip citation tokens, return rendered text + a list of Citation objects.

    `valid_cite_ids` is the set of cite_ids that were supplied in the context.
    Any token referencing an ID not in this set is recorded in `invalid_tokens`.
    """
    citations: list[Citation] = []
    invalid: list[str] = []
    rendered = []
    cursor = 0
    last_end = 0

    for m in _CITE_RE.finditer(text):
        # Append everything before this token to rendered
        rendered.append(text[last_end:m.start()])
        cursor += m.start() - last_end

        body = m.group("body")
        cite_ids: list[str] = []
        for inner in _INNER_RE.finditer(body):
            cid = f"{inner.group(1)}:{inner.group(2)}"
            cite_ids.append(cid)
            if cid not in valid_cite_ids:
                invalid.append(cid)

        citations.append(Citation(
            citation_token=m.group(0),
            cite_ids=cite_ids,
            char_start=cursor,
            char_end=cursor,    # token is dropped, so start==end in rendered output
        ))

        last_end = m.end()

    # Append trailing text
    rendered.append(text[last_end:])
    rendered_text = "".join(rendered).strip()

    return ParsedSummary(
        rendered_text=rendered_text,
        citations=citations,
        invalid_tokens=invalid,
        fabricated=bool(invalid),
    )


def render_with_inline_markers(text: str, marker: str = "[ref]") -> str:
    """Replace every `[obs:...]` token with a generic marker, for log printouts."""
    return _CITE_RE.sub(marker, text)
