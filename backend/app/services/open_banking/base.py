"""
Open Banking multi-provider tube.

ClearFlow talks to this interface. Adapters (GoCardless, TrueLayer, Tink, …)
map each vendor API into the same shapes.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


DEFAULT_ACCESS_DAYS = int(os.getenv("OPEN_BANKING_ACCESS_DAYS", "90"))
DEFAULT_HISTORY_DAYS = int(os.getenv("OPEN_BANKING_HISTORY_DAYS", "90"))


class OpenBankingError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass
class Institution:
    id: str
    name: str
    bic: str | None = None
    logo: str | None = None
    countries: list[str] = field(default_factory=list)
    transaction_total_days: Any = None
    max_access_valid_for_days: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "bic": self.bic,
            "logo": self.logo,
            "countries": self.countries,
            "transaction_total_days": self.transaction_total_days,
            "max_access_valid_for_days": self.max_access_valid_for_days,
        }


@dataclass
class LinkSession:
    """Result of starting a bank consent flow."""

    link: str
    session_id: str  # requisition_id / auth_id / etc.
    access_valid_for_days: int
    agreement_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LinkStatus:
    """Status after the user returns from the bank."""

    session_id: str
    status: str
    accounts: list[str]
    institution_id: str
    reference: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedTransaction:
    """Vendor-agnostic bank movement for ClearFlow persistence."""

    reference: str
    amount: float
    currency: str
    booking_date: str | None
    concept: str
    raw: dict[str, Any] = field(default_factory=dict)


class OpenBankingProvider(ABC):
    """One adapter per Open Banking vendor."""

    name: str
    display_name: str

    @abstractmethod
    def is_configured(self) -> bool:
        ...

    def config_notes(self) -> str:
        return f"Configure credentials for provider '{self.name}'."

    @abstractmethod
    async def list_institutions(self, country: str = "ES") -> list[Institution]:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def get_link_status(self, session_id: str) -> LinkStatus:
        ...

    async def complete_authorization(
        self,
        *,
        code: str,
        state: str,
        redirect_uri: str,
    ) -> LinkStatus:
        """OAuth-code providers (Tink, TrueLayer). Default: not supported."""
        raise OpenBankingError(
            f"{self.name} does not use authorization-code callback",
            status_code=501,
        )

    @abstractmethod
    async def get_account_details(
        self,
        account_id: str,
        *,
        user_access_token: str | None = None,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    async def get_account_transactions(
        self,
        account_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        user_access_token: str | None = None,
    ) -> list[NormalizedTransaction]:
        ...
