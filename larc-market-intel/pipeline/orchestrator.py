"""Orchestrator: route → adapter.parse → loader.load."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from db.markets import MarketResolver
from pipeline import loader, router


@dataclass
class IngestOutcome:
    publication_id: str
    provider_code: str
    doc_type: str
    inserted: dict[str, int]
    warnings: list[str]
    duplicate: bool = False


def ingest_file(
    conn: sqlite3.Connection,
    file_path: Path | str,
    override_doc_type: str | None = None,
    force_reload: bool = False,
) -> IngestOutcome:
    file_path = Path(file_path)

    pub = router.route(file_path, override_doc_type=override_doc_type)

    # Idempotency: if same file content already loaded, skip unless --force
    if not force_reload:
        existing = loader.is_already_loaded(conn, pub.source_sha256)
        if existing == pub.publication_id:
            loader.log_duplicate(conn, pub)
            return IngestOutcome(
                publication_id=pub.publication_id,
                provider_code=pub.provider_code,
                doc_type=pub.doc_type,
                inserted={},
                warnings=[],
                duplicate=True,
            )

    # Build market resolver + adapter
    resolver = MarketResolver(conn, auto_create=True)
    adapter_cls = router.load_adapter_class(pub.doc_type)
    adapter = adapter_cls(market_resolver=resolver)

    # Parse
    try:
        result = adapter.parse(file_path, pub)
    except Exception as e:
        loader.log_error(conn, pub, file_path.name, f"adapter: {e!r}")
        raise

    # Load (idempotent — DELETE+INSERT on publication_id)
    try:
        inserted = loader.load(conn, pub, result)
    except Exception as e:
        loader.log_error(conn, pub, file_path.name, f"loader: {e!r}")
        raise

    return IngestOutcome(
        publication_id=pub.publication_id,
        provider_code=pub.provider_code,
        doc_type=pub.doc_type,
        inserted=inserted,
        warnings=result.warnings,
    )
