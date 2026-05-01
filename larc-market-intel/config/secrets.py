"""Secret resolution for the platform.

Strategy: providers are checked in order, first hit wins. To rotate or change
the API key source, change the environment variable or swap the default
secret file — no code changes.

Resolution order (default):
  1. Process environment variable (e.g. ANTHROPIC_API_KEY)
  2. .env file in the repo root (loaded once on first call)
  3. Azure Key Vault — only if AZURE_KEY_VAULT_URL is set, lazily imported

You can swap providers by setting SECRETS_PROVIDERS=env,keyvault (or any
comma-separated subset) — useful in Azure Web Apps where you may want to
disable the .env file fallback entirely.

Public API:
    get_secret(name, default=None) -> str | None
    require_secret(name) -> str        # raises if missing

Example:
    from config.secrets import require_secret
    api_key = require_secret("ANTHROPIC_API_KEY")
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

_DOTENV_PATH = Path(__file__).parent.parent / ".env"
_dotenv_cache: dict[str, str] | None = None
_keyvault_client = None  # lazily initialized


class SecretNotFoundError(KeyError):
    """Raised by require_secret() when a secret is not available from any provider."""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def _from_env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v else None


def _load_dotenv() -> dict[str, str]:
    """Parse .env once. Format: KEY=VALUE, # comments allowed, no quoting required."""
    global _dotenv_cache
    if _dotenv_cache is not None:
        return _dotenv_cache
    cache: dict[str, str] = {}
    if _DOTENV_PATH.exists():
        for line in _DOTENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            # Strip optional surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            cache[key.strip()] = value
    _dotenv_cache = cache
    return cache


def _from_dotenv(name: str) -> str | None:
    return _load_dotenv().get(name) or None


def _from_keyvault(name: str) -> str | None:
    """Azure Key Vault provider. Activated by setting AZURE_KEY_VAULT_URL.

    Secret naming: Azure Key Vault names are dash-separated and case-insensitive,
    so ANTHROPIC_API_KEY is looked up as 'anthropic-api-key'.
    """
    global _keyvault_client
    vault_url = os.environ.get("AZURE_KEY_VAULT_URL")
    if not vault_url:
        return None
    if _keyvault_client is None:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError:
            # azure-keyvault-secrets is an optional dep; skip silently if not installed
            return None
        _keyvault_client = SecretClient(
            vault_url=vault_url,
            credential=DefaultAzureCredential(),
        )
    secret_name = name.lower().replace("_", "-")
    try:
        return _keyvault_client.get_secret(secret_name).value
    except Exception:
        return None


_PROVIDER_REGISTRY: dict[str, Callable[[str], str | None]] = {
    "env":      _from_env,
    "dotenv":   _from_dotenv,
    "keyvault": _from_keyvault,
}


def _active_providers() -> list[Callable[[str], str | None]]:
    """Read SECRETS_PROVIDERS env var to pick provider order. Defaults to all three."""
    spec = os.environ.get("SECRETS_PROVIDERS", "env,dotenv,keyvault")
    names = [n.strip().lower() for n in spec.split(",") if n.strip()]
    out: list[Callable[[str], str | None]] = []
    for name in names:
        if name in _PROVIDER_REGISTRY:
            out.append(_PROVIDER_REGISTRY[name])
    return out or [_from_env]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_secret(name: str, default: str | None = None) -> str | None:
    """Resolve a secret by name. Returns default if no provider has it."""
    for provider in _active_providers():
        v = provider(name)
        if v:
            return v
    return default


def require_secret(name: str) -> str:
    """Resolve a secret or raise SecretNotFoundError."""
    v = get_secret(name)
    if not v:
        providers = os.environ.get("SECRETS_PROVIDERS", "env,dotenv,keyvault")
        raise SecretNotFoundError(
            f"secret '{name}' not found in any provider ({providers}). "
            f"Set the env var, add to .env, or configure Azure Key Vault."
        )
    return v


def reset_cache() -> None:
    """Drop cached .env state. Useful in tests or after rotating .env."""
    global _dotenv_cache, _keyvault_client
    _dotenv_cache = None
    _keyvault_client = None
