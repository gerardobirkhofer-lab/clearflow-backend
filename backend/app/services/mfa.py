"""
MFA helpers: TOTP (primary) + SMS OTP (backup).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from typing import Any

import httpx

# In-process SMS OTP store (fine for single instance; Redis later)
_SMS_CODES: dict[str, dict[str, Any]] = {}

TOTP_ISSUER = os.getenv("MFA_ISSUER", "ClearFlow")


def _try_import_pyotp():
    try:
        import pyotp  # type: ignore
        return pyotp
    except ImportError:
        return None


def totp_available() -> bool:
    return _try_import_pyotp() is not None


def generate_totp_secret() -> str:
    pyotp = _try_import_pyotp()
    if not pyotp:
        raise RuntimeError("pyotp not installed")
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    pyotp = _try_import_pyotp()
    if not pyotp:
        raise RuntimeError("pyotp not installed")
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=TOTP_ISSUER)


def verify_totp(secret: str, code: str) -> bool:
    pyotp = _try_import_pyotp()
    if not pyotp or not secret:
        return False
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    return bool(pyotp.TOTP(secret).verify(code, valid_window=1))


def normalize_phone(phone: str) -> str:
    p = "".join(c for c in (phone or "") if c.isdigit() or c == "+")
    if p.startswith("00"):
        p = "+" + p[2:]
    if p and not p.startswith("+"):
        # Assume Spain if local 9-digit
        if len(p) == 9:
            p = "+34" + p
        else:
            p = "+" + p
    return p


def _sms_key(user_id: int, phone: str) -> str:
    return f"{user_id}:{phone}"


def create_sms_otp(user_id: int, phone: str, *, ttl_seconds: int = 300) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    key = _sms_key(user_id, phone)
    _SMS_CODES[key] = {
        "hash": hashlib.sha256(code.encode()).hexdigest(),
        "exp": time.time() + ttl_seconds,
        "attempts": 0,
    }
    return code


def verify_sms_otp(user_id: int, phone: str, code: str) -> bool:
    key = _sms_key(user_id, phone)
    entry = _SMS_CODES.get(key)
    if not entry:
        return False
    if time.time() > float(entry["exp"]):
        _SMS_CODES.pop(key, None)
        return False
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    if entry["attempts"] > 5:
        _SMS_CODES.pop(key, None)
        return False
    ok = hashlib.sha256((code or "").strip().encode()).hexdigest() == entry["hash"]
    if ok:
        _SMS_CODES.pop(key, None)
    return ok


async def send_sms(phone: str, message: str) -> dict[str, Any]:
    """
    Send SMS via Twilio if configured; otherwise stub (dev logs code path).
    """
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_num = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    if sid and token and from_num:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                url,
                data={"To": phone, "From": from_num, "Body": message},
                auth=(sid, token),
            )
            if res.status_code >= 400:
                return {"success": False, "error": res.text[:300], "provider": "twilio"}
            return {"success": True, "provider": "twilio"}

    # Stub: allow MFA testing without Twilio
    allow_stub = os.getenv("SMS_OTP_STUB", "1") == "1"
    if allow_stub:
        return {
            "success": True,
            "provider": "stub",
            "note": "SMS stub activo (SMS_OTP_STUB=1). Configura Twilio en producción.",
        }
    return {"success": False, "error": "SMS provider not configured", "provider": "none"}
