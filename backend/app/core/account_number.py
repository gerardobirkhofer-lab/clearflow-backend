"""Check the account number for the country the client chose.

Spain and any other IBAN country use the IBAN length and checksum.
Argentina uses the 22-digit CBU or CVU and its two check digits.
A country we do not know yet is stored, and marked as not checked.
"""
from __future__ import annotations

from app.core.iban import IBAN_LENGTHS, compact_iban, iban_ok

_CBU_BANK = (7, 1, 3, 9, 7, 1, 3)
_CBU_ACCOUNT = (3, 9, 7, 1, 3, 9, 7, 1, 3, 9, 7, 1, 3)


class AccountNumberError(Exception):
    def __init__(self, detail: str):
        self.detail = detail


def _cbu_digit(number: str, weights: tuple[int, ...]) -> int:
    total = sum(int(digit) * weight for digit, weight in zip(number, weights))
    return (10 - (total % 10)) % 10


def cbu_ok(value: str) -> bool:
    compact = compact_iban(value)
    if len(compact) != 22 or not compact.isdigit():
        return False
    bank_ok = _cbu_digit(compact[:7], _CBU_BANK) == int(compact[7])
    account_ok = _cbu_digit(compact[8:21], _CBU_ACCOUNT) == int(compact[21])
    return bank_ok and account_ok


def _known_iban_prefix(compact: str) -> bool:
    return len(compact) >= 2 and compact[:2].isalpha() and compact[:2] in IBAN_LENGTHS


def normalize_account(country: str | None, raw: str) -> dict:
    """Return the compact number, the country it belongs to, and whether it was checked."""
    chosen = (country or "ES").strip().upper()
    if chosen not in {"ES", "AR", "OTHER"}:
        raise AccountNumberError("Account country is not supported")
    compact = compact_iban(raw)
    if chosen == "ES":
        if not compact.startswith("ES") or not iban_ok(compact):
            raise AccountNumberError("IBAN does not check out")
        return {"number": compact, "country": "ES", "checked": True}
    if chosen == "AR":
        if not cbu_ok(compact):
            raise AccountNumberError("CBU does not check out")
        return {"number": compact, "country": "AR", "checked": True}
    if len(compact) > 34:
        raise AccountNumberError("Account number is too long")
    if _known_iban_prefix(compact):
        if not iban_ok(compact):
            raise AccountNumberError("IBAN does not check out")
        return {"number": compact, "country": compact[:2], "checked": True}
    if cbu_ok(compact):
        return {"number": compact, "country": "AR", "checked": True}
    if len(compact) < 4 or not compact.isalnum():
        raise AccountNumberError("Account number does not check out")
    return {"number": compact, "country": "XX", "checked": False}
