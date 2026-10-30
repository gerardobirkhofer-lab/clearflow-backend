"""TrueLayer adapter — tube ready; wire live API when credentials exist."""
from __future__ import annotations

import os
from typing import Any

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


def _credentials() -> tuple[str, str]:
    client_id = os.getenv("TRUELAYER_CLIENT_ID", "").strip()
    client_secret = os.getenv("TRUELAYER_CLIENT_SECRET", "").strip()
    return client_id, client_secret


class TrueLayerProvider(OpenBankingProvider):
    name = "truelayer"
    display_name = "TrueLayer"

    def is_configured(self) -> bool:
        client_id, client_secret = _credentials()
        return bool(client_id and client_secret)

    def config_notes(self) -> str:
        return (
            "Set TRUELAYER_CLIENT_ID and TRUELAYER_CLIENT_SECRET. "
            "Sandbox: https://console.truelayer.com — production requires sales + KYB."
        )

    def _not_wired(self) -> None:
        raise OpenBankingError(
            "TrueLayer credentials detected but live adapter is not wired yet. "
            "Provider slot is reserved in the multi-provider tube.",
            status_code=501,
        )

    async def list_institutions(self, country: str = "ES") -> list[Institution]:
        if not self.is_configured():
            raise OpenBankingError(self.config_notes(), status_code=503)
        self._not_wired()
        return []

    async def create_link(
        self,
        *,
        institution_id: str,
        redirect_url: str,
        reference: str,
        access_valid_for_days: int = DEFAULT_ACCESS_DAYS,
        max_historical_days: int = DEFAULT_HISTORY_DAYS,
        user_language: str = "ES",
    ) -> LinkSession:
        if not self.is_configured():
            raise OpenBankingError(self.config_notes(), status_code=503)
        self._not_wired()
        raise AssertionError("unreachable")

    async def get_link_status(self, session_id: str) -> LinkStatus:
        if not self.is_configured():
            raise OpenBankingError(self.config_notes(), status_code=503)
        self._not_wired()
        raise AssertionError("unreachable")

    async def get_account_details(
        self,
        account_id: str,
        *,
        user_access_token: str | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured():
            raise OpenBankingError(self.config_notes(), status_code=503)
        self._not_wired()
        return {}

    async def get_account_transactions(
        self,
        account_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        user_access_token: str | None = None,
    ) -> list[NormalizedTransaction]:
        if not self.is_configured():
            raise OpenBankingError(self.config_notes(), status_code=503)
        self._not_wired()
        return []
