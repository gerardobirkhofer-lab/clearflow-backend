"""
GoCardless Bank Account Data (ex-Nordigen) — Open Banking / PSD2 client.

Docs: https://developer.gocardless.com/bank-account-data/overview
Base: https://bankaccountdata.gocardless.com/api/v2/

Env (any pair works):
  GOCARDLESS_BANK_SECRET_ID + GOCARDLESS_BANK_SECRET_KEY
  or NORDIGEN_SECRET_ID + NORDIGEN_SECRET_KEY
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = os.getenv(
    "GOCARDLESS_BANK_API_URL",
    "https://bankaccountdata.gocardless.com/api/v2",
).rstrip("/")

DEFAULT_ACCESS_DAYS = int(os.getenv("OPEN_BANKING_ACCESS_DAYS", "90"))
DEFAULT_HISTORY_DAYS = int(os.getenv("OPEN_BANKING_HISTORY_DAYS", "90"))


def _secrets() -> tuple[str, str]:
    secret_id = (
        os.getenv("GOCARDLESS_BANK_SECRET_ID", "").strip()
        or os.getenv("NORDIGEN_SECRET_ID", "").strip()
    )
    secret_key = (
        os.getenv("GOCARDLESS_BANK_SECRET_KEY", "").strip()
        or os.getenv("NORDIGEN_SECRET_KEY", "").strip()
    )
    return secret_id, secret_key


def is_configured() -> bool:
    secret_id, secret_key = _secrets()
    return bool(secret_id and secret_key)


class OpenBankingError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class OpenBankingClient:
    """Thin async client for Bank Account Data API."""

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._access_expires_at: datetime | None = None

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        if (
            self._access_token
            and self._access_expires_at
            and datetime.now(timezone.utc) < self._access_expires_at - timedelta(seconds=60)
        ):
            return self._access_token

        secret_id, secret_key = _secrets()
        if not secret_id or not secret_key:
            raise OpenBankingError(
                "Open Banking not configured. Set GOCARDLESS_BANK_SECRET_ID and "
                "GOCARDLESS_BANK_SECRET_KEY (or NORDIGEN_* equivalents)."
            )

        resp = await client.post(
            f"{BASE_URL}/token/new/",
            json={"secret_id": secret_id, "secret_key": secret_key},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            raise OpenBankingError(
                f"Token request failed: {resp.status_code}",
                status_code=resp.status_code,
                payload=resp.text,
            )
        data = resp.json()
        self._access_token = data["access"]
        expires = int(data.get("access_expires") or 86400)
        self._access_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires)
        return self._access_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient() as client:
            token = await self._ensure_token(client)
            resp = await client.request(
                method,
                f"{BASE_URL}{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                json=json_body,
                params=params,
                timeout=45.0,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "open_banking_http_error method=%s path=%s status=%s body=%s",
                    method,
                    path,
                    resp.status_code,
                    resp.text[:500],
                )
                raise OpenBankingError(
                    f"Open Banking API error {resp.status_code} on {path}",
                    status_code=resp.status_code,
                    payload=resp.text,
                )
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    async def list_institutions(self, country: str = "ES") -> list[dict[str, Any]]:
        data = await self._request("GET", "/institutions/", params={"country": country.upper()})
        if not isinstance(data, list):
            return []
        # Normalize for frontend
        out = []
        for inst in data:
            out.append(
                {
                    "id": inst.get("id"),
                    "name": inst.get("name"),
                    "bic": inst.get("bic"),
                    "logo": inst.get("logo"),
                    "countries": inst.get("countries") or [],
                    "transaction_total_days": inst.get("transaction_total_days"),
                    "max_access_valid_for_days": inst.get("max_access_valid_for_days"),
                }
            )
        return out

    async def create_agreement(
        self,
        institution_id: str,
        *,
        access_valid_for_days: int = DEFAULT_ACCESS_DAYS,
        max_historical_days: int = DEFAULT_HISTORY_DAYS,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/agreements/enduser/",
            json_body={
                "institution_id": institution_id,
                "max_historical_days": max_historical_days,
                "access_valid_for_days": access_valid_for_days,
                "access_scope": ["balances", "details", "transactions"],
            },
        )

    async def create_requisition(
        self,
        *,
        redirect: str,
        institution_id: str,
        agreement_id: str,
        reference: str,
        user_language: str = "ES",
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/requisitions/",
            json_body={
                "redirect": redirect,
                "institution_id": institution_id,
                "agreement": agreement_id,
                "reference": reference,
                "user_language": user_language,
            },
        )

    async def get_requisition(self, requisition_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/requisitions/{requisition_id}/")

    async def get_account_details(self, account_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/accounts/{account_id}/details/")

    async def get_account_balances(self, account_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/accounts/{account_id}/balances/")

    async def get_account_transactions(
        self,
        account_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return await self._request(
            "GET",
            f"/accounts/{account_id}/transactions/",
            params=params or None,
        )


_client: OpenBankingClient | None = None


def get_open_banking_client() -> OpenBankingClient:
    global _client
    if _client is None:
        _client = OpenBankingClient()
    return _client
