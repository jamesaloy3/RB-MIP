"""Router: decide which adapter to use for a given file.

Inputs: file_path (and optionally an explicit --provider override from CLI).
Outputs: PublicationInfo (with adapter class resolvable via registry).

Resolution rules:
  1. Explicit override from CLI > everything.
  2. Filename pattern matching against config/providers.yaml.
  3. Folder convention: ingest/<provider>/<doctype>/file → infer.
  4. Fallback: raise RouterError.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from datetime import datetime
from pathlib import Path

import yaml

from adapters.base import BaseAdapter, PublicationInfo


CONFIG_PATH = Path(__file__).parent.parent / "config" / "providers.yaml"


class RouterError(Exception):
    pass


# Registry maps doc_type → (adapter module path, class name).
# Adapters are imported lazily so a missing optional dep doesn't break unrelated paths.
_ADAPTER_REGISTRY: dict[str, tuple[str, str]] = {
    "larc_hotelbis":     ("adapters.larc.hotelbis",          "HotelBISAdapter"),
    "larc_convention":   ("adapters.larc.convention",        "ConventionAdapter"),
    "larc_narrative":    ("adapters.larc.narrative",         "NarrativeAdapter"),
    "costar_str":        ("adapters.costar.str_data",        "CoStarSTRAdapter"),
    "costar_narrative":  ("adapters.costar.narrative",       "CoStarNarrativeAdapter"),
    "greenstreet":       ("adapters.greenstreet.fundamentals", "GreenStreetAdapter"),
}


def load_adapter_class(doc_type: str) -> type[BaseAdapter]:
    if doc_type not in _ADAPTER_REGISTRY:
        raise RouterError(f"no adapter registered for doc_type '{doc_type}'")
    module_path, class_name = _ADAPTER_REGISTRY[doc_type]
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def load_provider_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _match_doc_type(file_path: Path, cfg: dict) -> tuple[str, str] | None:
    """Match file by name pattern. Returns (provider_code, doc_type) or None."""
    name = file_path.name
    for provider in cfg["providers"]:
        for dt in provider["doc_types"]:
            for pattern in dt.get("file_patterns", []):
                if fnmatch.fnmatch(name, pattern):
                    return provider["code"], dt["doc_type"]
    return None


def _sha256(file_path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _publication_id(sha256_hex: str, provider_code: str, doc_type: str) -> str:
    """Stable publication_id derived from file content + provider + doc_type."""
    h = hashlib.sha1(f"{provider_code}|{doc_type}|{sha256_hex}".encode()).hexdigest()
    return h[:24]


_PERIOD_PATTERNS = [
    # Note: avoid trailing \b — underscore is a word character so \b fails
    # between "26" and "_". Use a negative lookahead instead.
    re.compile(r"(?<![0-9])([1-4])Q[\s._]?(\d{2,4})(?![0-9])", re.IGNORECASE),
    re.compile(r"(?<![0-9])(\d{4})[-_.]?Q([1-4])(?![0-9])", re.IGNORECASE),
]


def _parse_period_from_filename(name: str) -> tuple[str | None, str | None]:
    """Return (publication_period, publication_date_iso) inferred from filename."""
    for pat in _PERIOD_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        g1, g2 = m.group(1), m.group(2)
        # Determine which is quarter, which is year
        if len(g1) <= 1 and int(g1) in (1, 2, 3, 4):
            q, y = int(g1), int(g2)
        else:
            y, q = int(g1), int(g2)
        if y < 100:
            y += 2000
        period = f"{q}Q{y % 100:02d}"
        # publication_date = first day of the quarter end month
        end_month = {1: 3, 2: 6, 3: 9, 4: 12}[q]
        date_iso = f"{y:04d}-{end_month:02d}-01"
        return period, date_iso
    # Try YYYY-MM-DD
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", name)
    if m:
        y, mo, d = m.groups()
        return None, f"{y}-{mo}-{d}"
    return None, None


def route(file_path: Path | str, override_doc_type: str | None = None) -> PublicationInfo:
    """Inspect a file and produce a PublicationInfo describing what it is."""
    file_path = Path(file_path)
    if not file_path.exists():
        raise RouterError(f"file not found: {file_path}")

    cfg = load_provider_config()

    if override_doc_type:
        # Find provider for this doc_type
        provider_code = None
        for provider in cfg["providers"]:
            for dt in provider["doc_types"]:
                if dt["doc_type"] == override_doc_type:
                    provider_code = provider["code"]
                    break
            if provider_code:
                break
        if not provider_code:
            raise RouterError(f"unknown doc_type override: {override_doc_type}")
        doc_type = override_doc_type
    else:
        match = _match_doc_type(file_path, cfg)
        if not match:
            raise RouterError(
                f"could not determine provider/doc_type for {file_path.name}. "
                "Use --doc-type to override or add the file to config/providers.yaml."
            )
        provider_code, doc_type = match

    sha256_hex, size_bytes = _sha256(file_path)
    pub_id = _publication_id(sha256_hex, provider_code, doc_type)
    period, date_iso = _parse_period_from_filename(file_path.name)
    if not date_iso:
        # Fallback: today's date
        date_iso = datetime.now().strftime("%Y-%m-%d")

    return PublicationInfo(
        publication_id=pub_id,
        provider_code=provider_code,
        doc_type=doc_type,
        publication_date=date_iso,
        publication_period=period,
        source_filename=file_path.name,
        source_sha256=sha256_hex,
        source_bytes=size_bytes,
    )
