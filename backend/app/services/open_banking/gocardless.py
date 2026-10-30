"""GoCardless Bank Account Data (ex-Nordigen) adapter."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

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

logger = logging.getLogger(__name__)

BASE_URL = os.getenv(
    "GOCARDLESS_BANK_API_URL",
    "https://bankaccountdata.gocardless.com/api/v2",
).rstrip("/")


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


class GoCardlessBankProvider(OpenBankingProvider):
    name = "gocardless"
    display_name = "GoCardless Bank Account Data"

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._access_expires_at: datetime | None = None

    def is_configured(self) -> bool:
        secret_id, secret_key = _secrets()
        return bool(secret_id and secret_key)

    def config_notes(self) -> str:
        return (
            "Set GOCARDLESS_BANK_SECRET_ID and GOCARDLESS_BANK_SECRET_KEY "
            "(or NORDIGEN_SECRET_ID / NORDIGEN_SECRET_KEY)."
        )

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        if (
            self._access_token
            and self._access_expires_at
            and datetime.now(timezone.utc) < self._access_expires_at - timedelta(seconds=60)
        ):
            return self._access_token

        secret_id, secret_key = _secrets()
        if not secret_id or not secret_key:
            raise OpenBankingError(self.config_notes())

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
                    "open_banking_http_error provider=gocardless method=%s path=%s status=%s body=%s",
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

    async def list_institutions(self, country: str = "ES") -> list[Institution]:
        data = await self._request("GET", "/institutions/", params={"country": country.upper()})
        if not isinstance(data, list):
            return []
        return [
            Institution(
                id=inst.get("id") or "",
                name=inst.get("name") or "",
                bic=inst.get("bic"),
                logo=inst.get("logo"),
                countries=inst.get("countries") or [],
                transaction_total_days=inst.get("transaction_total_days"),
                max_access_valid_for_days=inst.get("max_access_valid_for_days"),
            )
            for inst in data
            if inst.get("id")
        ]

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
        agreement = await self._request(
            "POST",
            "/agreements/enduser/",
            json_body={
                "institution_id": institution_id,
                "max_historical_days": max_historical_days,
                "access_valid_for_days": access_valid_for_days,
                "access_scope": ["balances", "details", "transactions"],
            },
        )
        agreement_id = agreement["id"]
        access_days = int(agreement.get("access_valid_for_days") or access_valid_for_days)

        requisition = await self._request(
            "POST",
            "/requisitions/",
            json_body={
                "redirect": redirect_url,
                "institution_id": institution_id,
                "agreement": agreement_id,
                "reference": reference,
                "user_language": user_language,
            },
        )
        session_id = requisition.get("id")
        link = requisition.get("link")
        if not session_id or not link:
            raise OpenBankingError("GoCardless did not return a link", status_code=502)

        return LinkSession(
            link=link,
            session_id=session_id,
            access_valid_for_days=access_days,
            agreement_id=agreement_id,
            raw={"agreement": agreement, "requisition": requisition},
        )

    async def get_link_status(self, session_id: str) -> LinkStatus:
        requisition = await self._request("GET", f"/requisitions/{session_id}/")
        return LinkStatus(
            session_id=session_id,
            status=(requisition.get("status") or "").upper(),
            accounts=list(requisition.get("accounts") or []),
            institution_id=requisition.get("institution_id") or "",
            reference=requisition.get("reference") or "",
            raw=requisition,
        )

    async def get_account_details(
        self,
        account_id: str,
        *,
        user_access_token: str | None = None,
    ) -> dict[str, Any]:
        return await self._request("GET", f"/accounts/{account_id}/details/")

    async def get_account_transactions(
        self,
        account_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        user_access_token: str | None = None,
    ) -> list[NormalizedTransaction]:
        params: dict[str, Any] = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        payload = await self._request(
            "GET",
            f"/accounts/{account_id}/transactions/",
            params=params or None,
        )
        booked = ((payload or {}).get("transactions") or {}).get("booked") or []
        pending = ((payload or {}).get("transactions") or {}).get("pending") or []
        out: list[NormalizedTransaction] = []
        for tx in list(booked) + list(pending):
            reference = (
                tx.get("transactionId")
                or tx.get("internalTransactionId")
                or tx.get("entryReference")
            )
            amount_obj = tx.get("transactionAmount") or {}
            try:
                amount = float(amount_obj.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            if not reference:
                reference = (
                    f"{account_id}:{tx.get('bookingDate')}:{amount}:"
                    f"{(tx.get('remittanceInformationUnstructured') or '')[:40]}"
                )
            concept = (
                tx.get("remittanceInformationUnstructured")
                or tx.get("additionalInformation")
                or tx.get("creditorName")
                or tx.get("debtorName")
                or "Open Banking movement"
            )
            out.append(
                NormalizedTransaction(
                    reference=str(reference),
                    amount=amount,
                    currency=str(amount_obj.get("currency") or "EUR"),
                    booking_date=tx.get("bookingDate") or tx.get("valueDate"),
                    concept=str(concept),
                    raw=tx,
                )
            )
        return out
