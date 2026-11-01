"""
Contract math — Mode B building blocks.

Direct:  net = gross - percent - fixed (+ IVA on fees if contract says so)
Gross-up: given observed bank net, infer fee/gross from contract terms
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass
class ContractTerms:
    provider_name: str
    fee_percent: float = 0.0
    fee_fixed: float = 0.0
    monthly_fee: float = 0.0
    has_iva: bool = False
    iva_percent: float = 21.0
    payout_days: int = 2
    payout_destination: str = "bank"  # bank | wallet
    dispute_email: str | None = None

    @classmethod
    def from_provider(cls, p: Any) -> "ContractTerms":
        return cls(
            provider_name=getattr(p, "name", None) or getattr(p, "provider_name", "") or "unknown",
            fee_percent=float(getattr(p, "fee_percent", 0) or 0),
            fee_fixed=float(getattr(p, "fee_fixed", 0) or 0),
            monthly_fee=float(getattr(p, "monthly_fee", 0) or 0),
            has_iva=bool(getattr(p, "has_iva", False)),
            iva_percent=float(getattr(p, "iva_percent", 21) or 21),
            payout_days=int(
                getattr(p, "credit_delay_days", None)
                or getattr(p, "payout_days", None)
                or 2
            ),
            payout_destination=str(getattr(p, "payout_destination", None) or "bank"),
            dispute_email=getattr(p, "dispute_email", None),
        )


def fee_on_gross(gross: float, terms: ContractTerms) -> dict[str, float]:
    """Fee amount for a single gross sale under contract (before optional monthly)."""
    pct = max(0.0, float(terms.fee_percent))
    fixed = max(0.0, float(terms.fee_fixed))
    base = abs(float(gross)) * (pct / 100.0) + fixed
    iva = base * (float(terms.iva_percent) / 100.0) if terms.has_iva else 0.0
    total_fee = base + iva
    net = float(gross) - total_fee if float(gross) >= 0 else float(gross) + total_fee
    # For positive sales: net = gross - fees
    if float(gross) >= 0:
        net = float(gross) - total_fee
    else:
        net = float(gross) + total_fee
    return {
        "gross": float(gross),
        "fee_base": round(base, 4),
        "fee_iva": round(iva, 4),
        "fee_total": round(total_fee, 4),
        "net": round(net, 4),
    }


def expected_from_gross(gross: float, sale_date: datetime | None, terms: ContractTerms) -> dict:
    calc = fee_on_gross(gross, terms)
    deposit = None
    if sale_date is not None:
        deposit = sale_date + timedelta(days=max(0, int(terms.payout_days)))
    return {
        "strategy": "reconstruct_direct",
        "provider": terms.provider_name,
        "payout_destination": terms.payout_destination,
        "payout_days": terms.payout_days,
        "sale_date": sale_date.isoformat() if sale_date else None,
        "expected_deposit_date": deposit.isoformat() if deposit else None,
        "contract": {
            "fee_percent": terms.fee_percent,
            "fee_fixed": terms.fee_fixed,
            "has_iva": terms.has_iva,
            "iva_percent": terms.iva_percent,
        },
        **calc,
    }


def gross_up_from_net(net: float, terms: ContractTerms) -> dict:
    """
    Infer gross/fee when only bank net is observed.
    Assumes: net = gross - gross*(pct/100) - fixed - iva_on_fees
    With IVA on fees: fee_base = gross*p + f; fee = fee_base*(1+iva)
    net = gross - fee_base*(1+iva)
    => gross*(1 - p/100*(1+iva)) = net + f*(1+iva)
    """
    p = max(0.0, float(terms.fee_percent)) / 100.0
    f = max(0.0, float(terms.fee_fixed))
    iva_m = (1.0 + float(terms.iva_percent) / 100.0) if terms.has_iva else 1.0
    denom = 1.0 - p * iva_m
    if denom <= 0.01:
        return {
            "strategy": "reconstruct_gross_up",
            "error": "contract_fees_too_high_to_gross_up",
            "net_observed": float(net),
        }
    gross = (float(net) + f * iva_m) / denom
    calc = fee_on_gross(gross, terms)
    return {
        "strategy": "reconstruct_gross_up",
        "provider": terms.provider_name,
        "net_observed": float(net),
        "inferred_gross": round(gross, 4),
        "inferred_fee_total": calc["fee_total"],
        "check_net": calc["net"],
        "contract": {
            "fee_percent": terms.fee_percent,
            "fee_fixed": terms.fee_fixed,
            "has_iva": terms.has_iva,
            "iva_percent": terms.iva_percent,
        },
    }
