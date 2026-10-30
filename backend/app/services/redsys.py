"""
Redsys notification helpers (HMAC_SHA256_V1).

Redsys posts form fields:
  Ds_SignatureVersion, Ds_MerchantParameters (base64 JSON), Ds_Signature

Merchant secret key (clave de comercio) is base64. Signature is derived with
3DES(order) → HMAC-SHA256(merchant_parameters).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def decode_merchant_parameters(merchant_parameters_b64: str) -> dict[str, Any]:
    """Decode Ds_MerchantParameters → dict."""
    padded = merchant_parameters_b64 + "=" * (-len(merchant_parameters_b64) % 4)
    raw = base64.b64decode(padded)
    return json.loads(raw.decode("utf-8"))


def _normalize_signature(sig: str) -> str:
    """Redsys may use URL-safe base64 variants."""
    return sig.replace("-", "+").replace("_", "/")


def _order_diversified_key(merchant_key_b64: str, order: str) -> bytes:
    """3DES-CBC diversify merchant key with Ds_Order (Redsys HMAC_SHA256_V1)."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = base64.b64decode(merchant_key_b64)
    # TripleDES needs 16 or 24-byte key; Redsys keys are typically 16/24 after decode
    if len(key) not in (16, 24):
        # pad/truncate defensively
        key = (key + b"\0" * 24)[:24]

    order_bytes = order.encode("utf-8")
    # Zero-pad to multiple of 8
    if len(order_bytes) % 8 != 0:
        order_bytes = order_bytes + (b"\0" * (8 - len(order_bytes) % 8))

    cipher = Cipher(algorithms.TripleDES(key), modes.CBC(b"\0" * 8), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(order_bytes) + encryptor.finalize()


def verify_redsys_signature(
    merchant_parameters_b64: str,
    signature_b64: str,
    merchant_key_b64: str,
    order: str | None = None,
) -> bool:
    """
    Return True if Ds_Signature matches Ds_MerchantParameters for the merchant key.
    If cryptography is unavailable, returns False and logs.
    """
    try:
        params = decode_merchant_parameters(merchant_parameters_b64)
        order_val = order or str(params.get("Ds_Order") or "")
        if not order_val:
            return False

        divers_key = _order_diversified_key(merchant_key_b64, order_val)
        digest = hmac.new(
            divers_key,
            merchant_parameters_b64.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")

        provided = _normalize_signature(signature_b64.strip())
        # Compare allowing missing padding
        def _canon(s: str) -> str:
            s = _normalize_signature(s)
            return s + "=" * (-len(s) % 4)

        return hmac.compare_digest(_canon(expected), _canon(provided))
    except Exception as exc:  # noqa: BLE001 — keep webhook up even if crypto fails
        logger.warning("redsys_signature_verify_error: %s", exc)
        return False


def amount_from_redsys(params: dict[str, Any]) -> float:
    """Ds_Amount is in cents (integer string). Ds_Currency 978 = EUR."""
    raw = params.get("Ds_Amount") or "0"
    try:
        return float(str(raw)) / 100.0
    except (TypeError, ValueError):
        return 0.0


def is_authorized_response(params: dict[str, Any]) -> bool:
    """Ds_Response 0000-0099 = authorized."""
    resp = str(params.get("Ds_Response") or "")
    digits = re.sub(r"\D", "", resp)
    if not digits:
        return False
    try:
        code = int(digits)
    except ValueError:
        return False
    return 0 <= code <= 99


def summarize_operation(params: dict[str, Any]) -> dict[str, Any]:
    """Normalize Redsys params into a ClearFlow-friendly dict."""
    return {
        "order": params.get("Ds_Order"),
        "amount": amount_from_redsys(params),
        "currency": "EUR" if str(params.get("Ds_Currency") or "978") == "978" else str(params.get("Ds_Currency")),
        "merchant_code": params.get("Ds_MerchantCode"),
        "terminal": params.get("Ds_Terminal"),
        "response": params.get("Ds_Response"),
        "authorized": is_authorized_response(params),
        "auth_code": params.get("Ds_AuthorisationCode"),
        "date": params.get("Ds_Date"),
        "hour": params.get("Ds_Hour"),
        "transaction_type": params.get("Ds_TransactionType"),
        "card_brand": params.get("Ds_Card_Brand") or params.get("Ds_ProcessedPayMethod"),
    }
