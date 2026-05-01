"""Thin wrapper around the Anthropic SDK with three job-specific concerns:

  1. **Caching**: the system prompt + extraction schema is cached on the last
     system block. Across many PDF chunks in one ingestion, we pay the write
     once and read it back at ~10% of input cost on every subsequent call.
     Verify with the `cache_read_input_tokens` field on responses.

  2. **Token tracking**: every call writes a row to `llm_usage` (input,
     output, cache_creation, cache_read, model, purpose, duration, request_id,
     est. cost). Drives "what did this ingestion cost?" + budget enforcement.

  3. **Budget cap**: optional hard cap on total tokens per LLMClient instance.
     Once exceeded, raises BudgetExceededError before the next API call. Use
     one client per ingestion-run for per-ingestion budgets.

API-key resolution goes through `config/secrets.py` so swapping keys
(rotation, Azure Key Vault, per-env vars) is a config change, not a code
change.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from config.secrets import require_secret


# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens). Update when Anthropic changes prices.
# ---------------------------------------------------------------------------

_PRICING = {
    "claude-opus-4-7":   {"input": 5.00,  "output": 25.00},
    "claude-opus-4-6":   {"input": 5.00,  "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-5": {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":  {"input": 1.00,  "output": 5.00},
}
# Cache writes are billed at 1.25x base input; cache reads at ~0.1x.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """Best-effort cost estimate in USD. Returns 0.0 if model price unknown."""
    p = _PRICING.get(model)
    if not p:
        return 0.0
    return (
        input_tokens                  * p["input"]                       / 1_000_000
        + cache_creation_input_tokens * p["input"] * _CACHE_WRITE_MULT   / 1_000_000
        + cache_read_input_tokens     * p["input"] * _CACHE_READ_MULT    / 1_000_000
        + output_tokens               * p["output"]                      / 1_000_000
    )


# ---------------------------------------------------------------------------


class BudgetExceededError(RuntimeError):
    """Raised when a per-client token budget is exceeded."""


@dataclass
class UsageRecord:
    """One API call's usage. Persisted to llm_usage table."""

    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    duration_ms: int
    request_id: str | None
    publication_id: str | None
    estimated_cost_usd: float
    metadata: dict | None = None

    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass
class LLMResponse:
    """Parsed response wrapper — content + usage."""

    text: str
    parsed: Any | None  # parsed JSON if response was structured
    raw_message: Any
    usage: UsageRecord


# ---------------------------------------------------------------------------


@dataclass
class LLMClient:
    """One client per ingestion run. Holds running totals for budget enforcement.

    Most callers should use `get_default_client(conn, ...)` rather than
    constructing directly.
    """

    model: str
    conn: sqlite3.Connection | None = None
    publication_id: str | None = None
    budget_tokens: int = 0          # 0 = unlimited
    api_key: str | None = None
    extra_headers: dict | None = None

    # internal
    _client: Any = field(default=None, init=False, repr=False)
    _running_totals: dict = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "estimated_cost_usd": 0.0,
        "calls": 0,
    }, init=False)

    def __post_init__(self) -> None:
        # Lazy import — anthropic is heavy and only needed when LLMClient is used
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "anthropic package not installed. `pip install anthropic`"
            ) from e
        api_key = self.api_key or require_secret("ANTHROPIC_API_KEY")
        # max_retries=8: gives 429s time to clear the 60s rate window. SDK uses
        # exponential backoff, so total wait can be ~2 minutes worst case.
        # Timeout=600s: large structured-extraction calls on Sonnet can take 3 min.
        self._client = anthropic.Anthropic(
            api_key=api_key, max_retries=8, timeout=600.0,
        )

    # ------------------------------------------------------------------

    def create(
        self,
        *,
        purpose: str,
        system_blocks: list[dict],
        messages: list[dict],
        max_tokens: int = 4096,
        response_schema: dict | None = None,
        cache_system: bool = True,
        metadata: dict | None = None,
    ) -> LLMResponse:
        """Issue a Messages.create call with caching + tracking.

        system_blocks: list of {"type": "text", "text": "..."} blocks. The
            last block automatically gets cache_control unless cache_system=False.
        response_schema: if provided, requests structured JSON output via
            output_config.format. Returned `parsed` is the loaded JSON.
        """
        self._enforce_budget()

        # Apply cache_control to the last system block
        sys_blocks = list(system_blocks)
        if cache_system and sys_blocks:
            last = dict(sys_blocks[-1])
            last["cache_control"] = {"type": "ephemeral"}
            sys_blocks[-1] = last

        kwargs: dict[str, Any] = {
            "model":      self.model,
            "max_tokens": max_tokens,
            "system":     sys_blocks,
            "messages":   messages,
        }
        if response_schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": response_schema},
            }
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        started = time.monotonic()
        try:
            message = self._client.messages.create(**kwargs)
        except Exception as e:
            self._log_error(purpose, str(e), metadata)
            raise
        duration_ms = int((time.monotonic() - started) * 1000)

        # Extract text from the response
        text_parts: list[str] = []
        for block in message.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        text = "".join(text_parts)

        parsed: Any | None = None
        if response_schema is not None and text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # Will be caught by validators upstream
                parsed = None

        usage_obj = message.usage
        record = UsageRecord(
            model=self.model,
            purpose=purpose,
            input_tokens=int(getattr(usage_obj, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage_obj, "output_tokens", 0) or 0),
            cache_creation_input_tokens=int(
                getattr(usage_obj, "cache_creation_input_tokens", 0) or 0
            ),
            cache_read_input_tokens=int(
                getattr(usage_obj, "cache_read_input_tokens", 0) or 0
            ),
            duration_ms=duration_ms,
            request_id=getattr(message, "_request_id", None),
            publication_id=self.publication_id,
            estimated_cost_usd=0.0,
            metadata=metadata,
        )
        record.estimated_cost_usd = estimate_cost_usd(
            self.model,
            record.input_tokens,
            record.output_tokens,
            record.cache_creation_input_tokens,
            record.cache_read_input_tokens,
        )

        self._update_totals(record)
        self._persist_usage(record, error=None)

        return LLMResponse(text=text, parsed=parsed, raw_message=message, usage=record)

    # ------------------------------------------------------------------

    def totals(self) -> dict:
        """Return a snapshot of running totals."""
        return dict(self._running_totals)

    # ------------------------------------------------------------------

    def _enforce_budget(self) -> None:
        if self.budget_tokens <= 0:
            return
        spent = (
            self._running_totals["input_tokens"]
            + self._running_totals["output_tokens"]
            + self._running_totals["cache_creation_input_tokens"]
            + self._running_totals["cache_read_input_tokens"]
        )
        if spent >= self.budget_tokens:
            raise BudgetExceededError(
                f"per-client budget {self.budget_tokens} tokens exceeded "
                f"(spent {spent} across {self._running_totals['calls']} calls)"
            )

    def _update_totals(self, r: UsageRecord) -> None:
        self._running_totals["input_tokens"] += r.input_tokens
        self._running_totals["output_tokens"] += r.output_tokens
        self._running_totals["cache_creation_input_tokens"] += r.cache_creation_input_tokens
        self._running_totals["cache_read_input_tokens"] += r.cache_read_input_tokens
        self._running_totals["estimated_cost_usd"] += r.estimated_cost_usd
        self._running_totals["calls"] += 1

    def _persist_usage(self, r: UsageRecord, error: str | None) -> None:
        if self.conn is None:
            return
        meta_json = json.dumps(r.metadata) if r.metadata else None
        try:
            self.conn.execute(
                """INSERT INTO llm_usage (
                    publication_id, purpose, model,
                    input_tokens, output_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens,
                    estimated_cost_usd, duration_ms, request_id,
                    error_message, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r.publication_id, r.purpose, r.model,
                    r.input_tokens, r.output_tokens,
                    r.cache_creation_input_tokens, r.cache_read_input_tokens,
                    r.estimated_cost_usd, r.duration_ms, r.request_id,
                    error, meta_json,
                ),
            )
        except sqlite3.Error:
            # Don't let usage logging break the main call
            pass

    def _log_error(self, purpose: str, message: str, metadata: dict | None) -> None:
        if self.conn is None:
            return
        meta_json = json.dumps(metadata) if metadata else None
        try:
            self.conn.execute(
                """INSERT INTO llm_usage (
                    publication_id, purpose, model,
                    error_message, metadata_json
                ) VALUES (?, ?, ?, ?, ?)""",
                (self.publication_id, purpose, self.model, message[:1000], meta_json),
            )
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------


def get_default_client(
    conn: sqlite3.Connection | None = None,
    publication_id: str | None = None,
    purpose_hint: str = "ingestion",
) -> LLMClient:
    """Construct an LLMClient using env-configured defaults.

    purpose_hint:
      - 'ingestion'  → reads LLM_INGESTION_MODEL (default claude-haiku-4-5)
      - 'summary'    → reads LLM_SUMMARY_MODEL  (default claude-sonnet-4-6)

    Resolution goes through `config.secrets.get_secret`, which checks env →
    .env → Azure Key Vault. Settings tucked into .env work without exporting.
    """
    from config.secrets import get_secret
    if purpose_hint == "summary":
        model = get_secret("LLM_SUMMARY_MODEL") or "claude-sonnet-4-6"
    else:
        model = get_secret("LLM_INGESTION_MODEL") or "claude-haiku-4-5"
    budget_str = get_secret("LLM_INGESTION_BUDGET_TOKENS") or "0"
    try:
        budget = int(budget_str)
    except ValueError:
        budget = 0
    return LLMClient(
        model=model,
        conn=conn,
        publication_id=publication_id,
        budget_tokens=budget,
    )
