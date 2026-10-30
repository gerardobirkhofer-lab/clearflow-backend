"""Tink Open Banking adapter (Transactions / AIS via Tink Link)."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

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

API_BASE = os.getenv("TINK_API_URL", "https://api.tink.com").rstrip("/")
LINK_BASE = os.getenv("TINK_LINK_URL", "https://link.tink.com").rstrip("/")

_LOCALE = {
    "ES": "es_ES",
    "PT": "pt_PT",
    "FR": "fr_FR",
    "DE": "de_DE",
    "IT": "it_IT",
    "NL": "nl_NL",
    "GB": "en_GB",
    "UK": "en_GB",
    "SE": "sv_SE",
    "EN": "en_US",
}

# Sandbox Demo Bank provider names (password flow) by market
_DEMO_PROVIDERS = {
    "ES": "es-demobank-password",
    "SE": "se-demobank-password",
    "GB": "uk-demobank-open-banking-redirect",
    "UK": "uk-demobank-open-banking-redirect",
    "DE": "de-demobank-password",
    "FR": "fr-demobank-password",
    "PT": "pt-demobank-password",
    "NL": "nl-demobank-open-banking-redirect",
    "IT": "it-demobank-open-banking-redirect",
}


def _credentials() -> tuple[str, str]:
    client_id = os.getenv("TINK_CLIENT_ID", "").strip()
    client_secret = os.getenv("TINK_CLIENT_SECRET", "").strip()
    return client_id, client_secret


def _demo_provider_for_market(market: str) -> str:
    return _DEMO_PROVIDERS.get(market.upper(), "es-demobank-password")


def _resolve_provider_id(institution_id: str, market: str) -> str | None:
    """Map ClearFlow institution ids to Tink input_provider."""
    if not institution_id:
        return None
    if institution_id in {"demo", "tink-demo-bank", "demobank"}:
        return _demo_provider_for_market(market)
    return institution_id

class TinkProvider(OpenBankingProvider):
    name = "tink"
    display_name = "Tink"

    def is_configured(self) -> bool:
        client_id, client_secret = _credentials()
        return bool(client_id and client_secret)

    def config_notes(self) -> str:
        return (
            "Set TINK_CLIENT_ID and TINK_CLIENT_SECRET. "
            "Sandbox: https://console.tink.com — production needs sales + KYB. "
            "Register redirect URI: {API}/api/v1/open-banking/callback"
        )

    async def list_institutions(self, country: str = "ES") -> list[Institution]:
        market = country.upper()
        if market == "UK":
            market = "GB"
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(f"{API_BASE}/api/v1/providers/{market}")
        if resp.status_code >= 400:
            raise OpenBankingError(
                f"Tink providers failed: {resp.status_code}",
                status_code=resp.status_code,
                payload=resp.text,
            )
        data = resp.json()
        providers = data.get("providers") if isinstance(data, dict) else data
        if not isinstance(providers, list):
            providers = []

        demo_id = _demo_provider_for_market(market)
        out: list[Institution] = [
            Institution(
                id=demo_id,
                name="Tink Demo Bank (sandbox)",
                bic=None,
                logo=None,
                countries=[market],
                transaction_total_days=90,
                max_access_valid_for_days=90,
            )
        ]
        # Keep legacy alias used in early tests
        if demo_id != "demo":
            out.insert(
                0,
                Institution(
                    id="demo",
                    name="Tink Demo Bank (sandbox alias)",
                    bic=None,
                    logo=None,
                    countries=[market],
                    transaction_total_days=90,
                    max_access_valid_for_days=90,
                ),
            )
        seen: set[str] = {"demo", demo_id}
        for p in providers:
            name = (p.get("name") or "").strip()
            if not name or name in seen:
                continue
            caps = p.get("capabilities") or []
            # Prefer AIS-capable providers
            if caps and not any(
                c in caps
                for c in (
                    "CHECKING_ACCOUNTS",
                    "CHECKING_TRANSACTIONS",
                    "SAVING_ACCOUNTS",
                    "CREDITCARD_ACCOUNTS",
                )
            ):
                continue
            seen.add(name)
            images = p.get("images") or {}
            out.append(
                Institution(
                    id=name,
                    name=p.get("displayName") or p.get("financialInstitutionName") or name,
                    bic=None,
                    logo=images.get("icon"),
                    countries=[p.get("market") or market],
                    transaction_total_days=None,
                    max_access_valid_for_days=DEFAULT_ACCESS_DAYS,
                )
            )
        return out

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
        client_id, _ = _credentials()
        if not client_id:
            raise OpenBankingError(self.config_notes(), status_code=503)

        market = (user_language or "ES").upper()
        # Prefer market from institution prefix (es-…) when possible
        if institution_id and institution_id not in {"demo", "tink-demo-bank"}:
            prefix = institution_id.split("-")[0].upper()
            if len(prefix) == 2:
                market = prefix
        locale = _LOCALE.get(market, _LOCALE.get(user_language.upper(), "es_ES"))

        market_code = "GB" if market == "UK" else market
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_url,
            "market": market_code,
            "locale": locale,
            "state": reference,
        }
        provider = _resolve_provider_id(institution_id, market_code)
        if provider:
            params["input_provider"] = provider

        link = f"{LINK_BASE}/1.0/transactions/connect-accounts/?{urlencode(params)}"
        return LinkSession(
            link=link,
            session_id=reference,
            access_valid_for_days=access_valid_for_days,
            agreement_id=None,
            raw={
                "market": market_code,
                "locale": locale,
                "redirect_uri": redirect_url,
                "input_provider": provider,
            },
        )

    async def get_link_status(self, session_id: str) -> LinkStatus:
        raise OpenBankingError(
            "Tink uses OAuth code callback. Wait for redirect with ?code=…&state=…",
            status_code=400,
        )

    async def complete_authorization(
        self,
        *,
        code: str,
        state: str,
        redirect_uri: str,
    ) -> LinkStatus:
        client_id, client_secret = _credentials()
        if not client_id or not client_secret:
            raise OpenBankingError(self.config_notes(), status_code=503)

        async with httpx.AsyncClient(timeout=45.0) as client:
            token_resp = await client.post(
                f"{API_BASE}/api/v1/oauth/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code >= 400:
                logger.warning("tink_token_exchange_failed status=%s body=%s", token_resp.status_code, token_resp.text[:500])
                raise OpenBankingError(
                    f"Tink token exchange failed: {token_resp.status_code}",
                    status_code=token_resp.status_code,
                    payload=token_resp.text,
                )
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                raise OpenBankingError("Tink did not return access_token", status_code=502)

            # Credentials may still be refreshing right after Link completes
            credentials_payload: Any = None
            for attempt in range(4):
                cred_resp = await client.get(
                    f"{API_BASE}/api/v1/credentials/list",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if cred_resp.status_code < 400:
                    credentials_payload = cred_resp.json()
                    creds = (
                        credentials_payload
                        if isinstance(credentials_payload, list)
                        else (credentials_payload or {}).get("credentials")
                        or (credentials_payload or {}).get("items")
                        or []
                    )
                    statuses = [
                        (c.get("status") or c.get("statusPayload") or "").upper()
                        for c in creds
                        if isinstance(c, dict)
                    ]
                    if not statuses or any(s in {"UPDATED", "AUTHENTICATED", "CREATED"} for s in statuses):
                        if attempt > 0 or any(s == "UPDATED" for s in statuses) or not statuses:
                            break
                    if all(s in {"UPDATING", "AUTHENTICATING"} for s in statuses if s):
                        await asyncio.sleep(1.5)
                        continue
                break

            accounts_resp = await client.get(
                f"{API_BASE}/data/v2/accounts",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if accounts_resp.status_code >= 400:
                # Legacy fallback
                accounts_resp = await client.get(
                    f"{API_BASE}/api/v1/accounts/list",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            if accounts_resp.status_code >= 400:
                raise OpenBankingError(
                    f"Tink accounts failed: {accounts_resp.status_code}",
                    status_code=accounts_resp.status_code,
                    payload=accounts_resp.text,
                )
            accounts_payload = accounts_resp.json() or {}
            if isinstance(accounts_payload, list):
                accounts_list = accounts_payload
            else:
                accounts_list = (
                    accounts_payload.get("accounts")
                    or accounts_payload.get("accountList")
                    or []
                )
            account_ids = [a.get("id") for a in accounts_list if isinstance(a, dict) and a.get("id")]
            if not account_ids:
                logger.warning(
                    "tink_accounts_empty state=%s accounts_body=%s credentials_body=%s",
                    state,
                    str(accounts_payload)[:500],
                    str(credentials_payload)[:500] if credentials_payload is not None else None,
                )

        institution_id = ""
        if state.startswith("cf:") and len(state.split(":")) >= 3:
            institution_id = state.split(":")[2]

        return LinkStatus(
            session_id=state or code[:32],
            status="CONNECTED" if account_ids else "PENDING",
            accounts=account_ids,
            institution_id=institution_id,
            reference=state,
            raw={
                "access_token": access_token,
                "token_type": token_data.get("token_type"),
                "expires_in": token_data.get("expires_in"),
                "scope": token_data.get("scope"),
                "accounts_count": len(account_ids),
                "token_obtained_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def get_account_details(
        self,
        account_id: str,
        *,
        user_access_token: str | None = None,
    ) -> dict[str, Any]:
        if not user_access_token:
            raise OpenBankingError("Tink requires user_access_token for account details", status_code=401)
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.get(
                f"{API_BASE}/data/v2/accounts/{account_id}",
                headers={"Authorization": f"Bearer {user_access_token}"},
            )
        if resp.status_code >= 400:
            # Fallback: list and find
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.get(
                    f"{API_BASE}/data/v2/accounts",
                    headers={"Authorization": f"Bearer {user_access_token}"},
                )
            if resp.status_code >= 400:
                raise OpenBankingError(
                    f"Tink account details failed: {resp.status_code}",
                    status_code=resp.status_code,
                    payload=resp.text,
                )
            for acct in (resp.json() or {}).get("accounts") or []:
                if acct.get("id") == account_id:
                    return {"account": self._normalize_account(acct)}
            return {"account": {"name": "Open Banking account"}}
        return {"account": self._normalize_account(resp.json() or {})}

    async def get_account_transactions(
        self,
        account_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        user_access_token: str | None = None,
    ) -> list[NormalizedTransaction]:
        if not user_access_token:
            raise OpenBankingError("Tink requires user_access_token for transactions", status_code=401)

        params: dict[str, str] = {"accountIdIn": account_id}
        # Tink v2 uses pageToken pagination; date filters vary by product version
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                f"{API_BASE}/data/v2/transactions",
                headers={"Authorization": f"Bearer {user_access_token}"},
                params=params,
            )
        if resp.status_code >= 400:
            raise OpenBankingError(
                f"Tink transactions failed: {resp.status_code}",
                status_code=resp.status_code,
                payload=resp.text,
            )
        payload = resp.json() or {}
        txs = payload.get("transactions") or []
        out: list[NormalizedTransaction] = []
        for tx in txs:
            # Optional client-side date filter
            booked = (
                (tx.get("dates") or {}).get("booked")
                or tx.get("bookedDate")
                or tx.get("date")
            )
            if date_from and booked and str(booked)[:10] < date_from:
                continue
            if date_to and booked and str(booked)[:10] > date_to:
                continue

            amount_obj = tx.get("amount") or {}
            value = amount_obj.get("value") if isinstance(amount_obj, dict) else amount_obj
            unscaled = None
            scale = 0
            currency = "EUR"
            if isinstance(value, dict):
                unscaled = value.get("unscaledValue")
                scale = int(value.get("scale") or 0)
                currency = value.get("currencyCode") or amount_obj.get("currencyCode") or "EUR"
            try:
                if unscaled is not None:
                    amount = float(unscaled) / (10 ** scale) if scale else float(unscaled)
                else:
                    amount = float(amount_obj.get("amount") or tx.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0

            # Tink amounts are often absolute with a separate type — prefer signed if present
            if tx.get("type") in {"WITHDRAWAL", "PAYMENT", "DEFAULT", "EXPENSE"} and amount > 0:
                # keep as provided; many feeds are already signed
                pass

            reference = (
                tx.get("id")
                or tx.get("transactionId")
                or tx.get("externalId")
                or f"{account_id}:{booked}:{amount}"
            )
            descriptions = tx.get("descriptions") or {}
            concept = (
                descriptions.get("display")
                or descriptions.get("original")
                or tx.get("description")
                or tx.get("merchantName")
                or "Open Banking movement"
            )
            out.append(
                NormalizedTransaction(
                    reference=str(reference),
                    amount=amount,
                    currency=str(currency),
                    booking_date=str(booked)[:10] if booked else None,
                    concept=str(concept),
                    raw=tx,
                )
            )
        return out

    @staticmethod
    def _normalize_account(acct: dict[str, Any]) -> dict[str, Any]:
        identifiers = acct.get("identifiers") or {}
        iban = None
        if isinstance(identifiers, dict):
            iban = (identifiers.get("iban") or {}).get("iban") if isinstance(identifiers.get("iban"), dict) else identifiers.get("iban")
        return {
            "iban": iban,
            "name": acct.get("name") or acct.get("displayName") or "Open Banking account",
            "ownerName": (acct.get("holderName") or acct.get("financialInstitution") or {}).get("name")
            if isinstance(acct.get("holderName"), dict)
            else acct.get("holderName"),
            "currency": (acct.get("balances") or {}).get("booked", {}).get("amount", {}).get("currencyCode")
            if isinstance(acct.get("balances"), dict)
            else acct.get("currencyCode") or "EUR",
        }
