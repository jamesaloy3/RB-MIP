# Market Intelligence Platform

Unified ingest + app for hospitality market research across providers (LARC, CoStar, extensible to HVS/PwC/STR).

## Quickstart

```bash
cp .env.example .env           # fill ANTHROPIC_API_KEY
docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec api python -m db.seed_markets
```

Drop files:
- LARC PDFs          → `ingest/larc/pdf/`
- LARC xlsx          → `ingest/larc/xlsx/`
- CoStar PDFs        → `ingest/costar/pdf/`
- CoStar xlsx        → `ingest/costar/xlsx/`

Open app: http://localhost:3000

## How it works

1. Watcher sees the file, hashes it, checks for duplicate ingest.
2. Router identifies provider + doc_type + market + publication_date.
3. Provider-specific extractor produces canonical JSON per `schemas/canonical/*.json`.
4. Validator enforces JSON schema + cross-field sanity + cross-source consistency.
5. Loader writes to canonical DB tables (DELETE+INSERT per publication).
6. App reads through FastAPI; provider is a filter dimension, not a mode.

See `docs/` for the full architecture, ingest workflow, and canonical field reference.

## Adding a new provider

1. Create `schemas/provider_mappings/<provider>_field_map.yaml` — mirror LARC as template.
2. Create `extractors/<provider>/xlsx_extractor.py` and `extractors/<provider>/pdf_extractor.py` inheriting from `extractors/base.py`.
3. Add `providers` row: `INSERT INTO providers VALUES ('<CODE>', '<name>', '<url>');`
4. Add market aliases if the provider uses different market names: insert into `market_aliases`.
5. Add tests: fixture file + golden JSON in `tests/fixtures/`.

No changes to `db/`, `api/`, or `app/` are required to onboard a new provider. That is the whole point.

## Tech

Python 3.11 • FastAPI • SQLAlchemy 2 • Alembic • pdfplumber • camelot-py • Anthropic SDK • Postgres
Next.js 14 • TypeScript • Tailwind • shadcn/ui • Recharts • TanStack Query
