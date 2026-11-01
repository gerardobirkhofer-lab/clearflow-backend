"""
Shared ingest helpers: fingerprints (idempotency), row classification, fee fields.

Why CSV still matters even with Open Banking / webhooks live:
  - Tink AIS → bank account movements only
  - Redsys/Stripe webhooks → payment events (often incomplete vs settlement packs)
  - POS sales detail, batch liquidations (gross/fee/net), wallet ledgers → almost
    always arrive as CSV/API reports from the provider portal until those connectors
    are fully wired per merchant.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any


def fingerprint(
    tenant_id: Any,
    *,
    channel: str,
    reference: str | None = None,
    amount: float | None = None,
    tx_date: datetime | None = None,
    concept: str | None = None,
    filename: str | None = None,
    provider_name: str | None = None,
) -> str:
    """Stable id so re-uploading the same logical row does not duplicate."""
    tid = str(tenant_id)
    ref = (reference or "").strip().upper()
    if ref:
        raw = f"{tid}|{channel}|ref|{ref}|{provider_name or ''}"
    else:
        date_s = tx_date.isoformat() if tx_date else ""
        raw = (
            f"{tid}|{channel}|row|{filename or ''}|"
            f"{date_s}|{amount}|{(concept or '').strip().lower()}|{provider_name or ''}"
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def classify_kind(
    *,
    channel: str,
    filename: str | None = None,
    concept: str | None = None,
    headers: list[str] | None = None,
) -> str:
    """
    sale_detail | settlement_batch | wallet_ledger | bank_movement | unknown
    """
    blob = " ".join(
        [
            channel or "",
            (filename or "").lower(),
            (concept or "").lower(),
            " ".join(headers or []),
        ]
    )
    if channel in ("open_banking", "bank_csv"):
        if any(k in blob for k in ("wallet", "saldo stripe", "balance ledger")):
            return "wallet_ledger"
        return "bank_movement"
    if any(
        k in blob
        for k in (
            "liquidacion",
            "liquidación",
            "settlement",
            "payout",
            "batch",
            "abono",
            "remesa",
        )
    ):
        return "settlement_batch"
    if any(k in blob for k in ("wallet", "ledger", "saldo disponible")):
        return "wallet_ledger"
    if channel == "provider_csv":
        return "sale_detail"
    return "unknown"


def extract_fee_fields(clean_row: dict[str, str], parse_amount) -> dict[str, float | None]:
    """Pull gross / fee / net when the CSV has them."""
    gross = parse_amount(
        _first(clean_row, ["bruto", "gross", "importe bruto", "amount_gross", "gross_amount"])
    )
    fee = parse_amount(
        _first(
            clean_row,
            ["fee", "comision", "comisión", "fees", "commission", "gasto", "coste fee"],
        )
    )
    net = parse_amount(
        _first(
            clean_row,
            ["liquido", "líquido", "net", "neto", "importe neto", "net_amount", "payout"],
        )
    )
    # Treat 0 from empty as None when source field missing
    def _nz(val, keys):
        if any(k in clean_row and str(clean_row.get(k) or "").strip() for k in keys):
            return val
        return None if val == 0.0 else val

    return {
        "gross_amount": _nz(gross, ["bruto", "gross", "importe bruto", "amount_gross", "gross_amount"]),
        "fee_amount": _nz(
            fee, ["fee", "comision", "comisión", "fees", "commission", "gasto", "coste fee"]
        ),
        "net_amount": _nz(
            net, ["liquido", "líquido", "net", "neto", "importe neto", "net_amount", "payout"]
        ),
    }


def settlement_id_from_row(clean_row: dict[str, str]) -> str | None:
    val = _first(
        clean_row,
        [
            "settlement_id",
            "liquidacion_id",
            "liquidación_id",
            "batch_id",
            "payout_id",
            "id liquidacion",
            "id liquidación",
        ],
    )
    return val or None


def _first(row: dict[str, str], keys: list[str]) -> str:
    for k in keys:
        if k in row and row[k] is not None and str(row[k]).strip():
            return str(row[k]).strip()
    # fuzzy: key contains
    for rk, rv in row.items():
        for k in keys:
            if k in (rk or "") and rv is not None and str(rv).strip():
                return str(rv).strip()
    return ""


def intake_channels_doc() -> dict:
    """Product truth for UI/API: what each port actually delivers."""
    return {
        "principle": (
            "Open Banking and webhooks do not replace settlement/POS packs. "
            "CSV (or provider report APIs) fill the gaps until those feeds are complete."
        ),
        "channels": [
            {
                "id": "open_banking",
                "status": "live_sandbox_tink",
                "delivers": ["bank_account_movements"],
                "does_not_deliver": [
                    "pos_sale_lines",
                    "settlement_batch_gross_fee_net",
                    "wallet_ledger_detail",
                ],
            },
            {
                "id": "webhook_redsys",
                "status": "live_limited_tenant_mapping",
                "delivers": ["tpv_payment_notifications"],
                "does_not_deliver": ["full_settlement_pack", "historical_backfill"],
            },
            {
                "id": "webhook_stripe",
                "status": "live",
                "delivers": ["charge_payout_events"],
                "does_not_deliver": ["complete_fee_breakdown_unless_expanded"],
            },
            {
                "id": "csv_bank",
                "status": "live",
                "delivers": ["bank_movements_backup_or_history"],
                "why": "Backup when OB consent expired; historical import; banks without OB yet",
            },
            {
                "id": "csv_provider",
                "status": "live",
                "delivers": [
                    "sale_detail",
                    "settlement_batches",
                    "fee_breakdown",
                    "wallet_exports",
                ],
                "why": "Required for 3-level reconciliation until portal APIs are wired per merchant",
            },
        ],
    }
