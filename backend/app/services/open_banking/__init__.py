"""
Open Banking multi-provider tube.

Select active vendor with:
  OPEN_BANKING_PROVIDER=gocardless|truelayer|tink   (default: gocardless)

Each adapter keeps its own credentials. The HTTP API stays the same.
"""
from __future__ import annotations

import os

from app.services.open_banking.base import (
    DEFAULT_ACCESS_DAYS,
    DEFAULT_HISTORY_DAYS,
    Institution,
    LinkSession,
    LinkStatus,
    NormalizedTransaction,
    OpenBankingError,
    OpenBankingProvider,
)
from app.services.open_banking.gocardless import GoCardlessBankProvider
from app.services.open_banking.tink import TinkProvider
from app.services.open_banking.truelayer import TrueLayerProvider

_PROVIDERS: dict[str, type[OpenBankingProvider]] = {
    "gocardless": GoCardlessBankProvider,
    "gocardless_bank": GoCardlessBankProvider,
    "gocardless_bank_account_data": GoCardlessBankProvider,
    "nordigen": GoCardlessBankProvider,
    "truelayer": TrueLayerProvider,
    "tink": TinkProvider,
}

_client: OpenBankingProvider | None = None


def available_providers() -> list[dict[str, object]]:
    seen: set[str] = set()
    out: list[dict[str, object]] = []
    for key, cls in _PROVIDERS.items():
        # Prefer canonical names only once
        if cls.name in seen:
            continue
        if key not in {cls.name}:
            continue
        seen.add(cls.name)
        instance = cls()
        out.append(
            {
                "id": cls.name,
                "display_name": cls.display_name,
                "configured": instance.is_configured(),
                "live": cls.name in {"gocardless", "tink"},
                "notes": instance.config_notes(),
            }
        )
    return out


def get_active_provider_name() -> str:
    raw = (os.getenv("OPEN_BANKING_PROVIDER") or "gocardless").strip().lower()
    return raw or "gocardless"


def get_open_banking_client(provider: str | None = None) -> OpenBankingProvider:
    """
    Return the active Open Banking adapter.
    Pass provider= to force a specific adapter (tests / admin).
    """
    global _client
    name = (provider or get_active_provider_name()).strip().lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        known = ", ".join(sorted({c.name for c in _PROVIDERS.values()}))
        raise OpenBankingError(
            f"Unknown OPEN_BANKING_PROVIDER '{name}'. Known: {known}",
            status_code=400,
        )

    if provider is not None:
        return cls()

    if _client is None or _client.name != cls.name:
        _client = cls()
    return _client


def is_configured(provider: str | None = None) -> bool:
    try:
        return get_open_banking_client(provider).is_configured()
    except OpenBankingError:
        return False


def reset_open_banking_client() -> None:
    """Clear cached client (tests / env reload)."""
    global _client
    _client = None


__all__ = [
    "DEFAULT_ACCESS_DAYS",
    "DEFAULT_HISTORY_DAYS",
    "Institution",
    "LinkSession",
    "LinkStatus",
    "NormalizedTransaction",
    "OpenBankingError",
    "OpenBankingProvider",
    "available_providers",
    "get_active_provider_name",
    "get_open_banking_client",
    "is_configured",
    "reset_open_banking_client",
]
