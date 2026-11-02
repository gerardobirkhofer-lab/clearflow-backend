"""
Authentication: email+password, then optional MFA (TOTP primary, SMS backup).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
import bcrypt
import jwt
from sqlalchemy import select

from app.core.auth import CurrentUser, get_current_user
from app.core.database import SharedSessionLocal
from app.models.local_auth_user import LocalAuthUser
from app.services import mfa as mfa_svc

router = APIRouter()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clearflow-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7
MFA_TOKEN_EXPIRE_MINUTES = 10


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def _create_access_token(user_id: int, email: str, tenant_id: uuid.UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "email": email,
        "tenant_id": str(tenant_id),
        "iat": now,
        "exp": now + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _create_mfa_token(user_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "email": email,
        "purpose": "mfa_pending",
        "iat": now,
        "exp": now + timedelta(minutes=MFA_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_mfa_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception as e:
        raise HTTPException(status_code=401, detail="MFA session expired — login again") from e
    if payload.get("purpose") != "mfa_pending":
        raise HTTPException(status_code=401, detail="Invalid MFA token")
    return payload


def _as_uuid(value) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _create_tenant_for_account(session, name: str, email: str) -> uuid.UUID:
    from app.models_orm import Tenant, TenantTier

    tenant_id = uuid.uuid4()
    label = (name or email.split("@")[0] or "account").strip()[:255]
    tenant = Tenant(
        id=tenant_id,
        name=label or "account",
        slug=f"acct-{tenant_id.hex[:16]}",
        timezone="Europe/Madrid",
        currency="EUR",
        is_active=True,
        subscription_plan="free",
        tier=TenantTier.STARTER,
    )
    session.add(tenant)
    await session.flush()
    return tenant_id


def _user_out(user: LocalAuthUser) -> dict:
    tenant_id = _as_uuid(user.tenant_id)
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "mfa": {
            "totp_enabled": bool(user.totp_enabled),
            "sms_backup_enabled": bool(user.sms_backup_enabled),
            "phone_masked": _mask_phone(user.phone_e164) if user.phone_e164 else None,
        },
    }


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    if len(phone) <= 4:
        return "****"
    return phone[:3] + "****" + phone[-2:]


def _mfa_methods(user: LocalAuthUser) -> list[str]:
    methods = []
    if user.totp_enabled and user.totp_secret:
        methods.append("totp")
    if user.sms_backup_enabled and user.phone_e164:
        methods.append("sms")
    return methods


@router.post("/register", status_code=201)
async def register(data: dict, request: Request):
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    full_name = data.get("full_name") or data.get("name") or email.split("@")[0]

    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email required")
    if len(password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")

    async with SharedSessionLocal() as session:
        existing = await session.execute(select(LocalAuthUser).where(LocalAuthUser.email == email))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered")

        tenant_id = await _create_tenant_for_account(session, full_name, email)
        user = LocalAuthUser(
            email=email,
            password_hash=_hash_password(password),
            name=full_name,
            role="self_owner",
            is_active=1,
            tenant_id=tenant_id,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    token = _create_access_token(user.id, email, _as_uuid(user.tenant_id))
    return {
        "token": token,
        "user": _user_out(user),
        "mfa_setup_recommended": True,
        "note": "Activa Authenticator (TOTP) en Seguridad. SMS queda como respaldo.",
    }


@router.post("/login")
async def login(data: dict):
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        raise HTTPException(status_code=422, detail="Email and password required")

    async with SharedSessionLocal() as session:
        result = await session.execute(select(LocalAuthUser).where(LocalAuthUser.email == email))
        user = result.scalar_one_or_none()
        if user is None or not _verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        tenant_id = _as_uuid(user.tenant_id)
        if tenant_id is None:
            tenant_id = await _create_tenant_for_account(session, user.name, user.email)
            user.tenant_id = tenant_id
            await session.commit()
            await session.refresh(user)
            tenant_id = _as_uuid(user.tenant_id)

        methods = _mfa_methods(user)
        if methods:
            mfa_token = _create_mfa_token(user.id, user.email)
            return {
                "mfa_required": True,
                "mfa_token": mfa_token,
                "methods": methods,
                "preferred_method": "totp" if "totp" in methods else methods[0],
                "phone_masked": _mask_phone(user.phone_e164),
                "user_preview": {"email": user.email, "name": user.name},
            }

    token = _create_access_token(user.id, user.email, tenant_id)
    return {"token": token, "user": _user_out(user), "mfa_required": False}


@router.post("/mfa/verify")
async def mfa_verify(data: dict):
    """Complete login after password: TOTP or SMS backup code."""
    mfa_token = data.get("mfa_token") or ""
    method = (data.get("method") or "totp").strip().lower()
    code = (data.get("code") or "").strip()
    if not mfa_token or not code:
        raise HTTPException(status_code=422, detail="mfa_token and code required")

    payload = _decode_mfa_token(mfa_token)
    user_id = int(payload["user_id"])

    async with SharedSessionLocal() as session:
        result = await session.execute(select(LocalAuthUser).where(LocalAuthUser.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")

        ok = False
        if method == "totp":
            if not user.totp_enabled or not user.totp_secret:
                raise HTTPException(status_code=400, detail="TOTP not enabled")
            ok = mfa_svc.verify_totp(user.totp_secret, code)
        elif method == "sms":
            if not user.sms_backup_enabled or not user.phone_e164:
                raise HTTPException(status_code=400, detail="SMS backup not enabled")
            ok = mfa_svc.verify_sms_otp(user.id, user.phone_e164, code)
        else:
            raise HTTPException(status_code=400, detail="method must be totp or sms")

        if not ok:
            raise HTTPException(status_code=401, detail="Invalid verification code")

        tenant_id = _as_uuid(user.tenant_id)
        if tenant_id is None:
            raise HTTPException(status_code=400, detail="Account missing tenant")

    token = _create_access_token(user.id, user.email, tenant_id)
    return {"token": token, "user": _user_out(user), "mfa_required": False}


@router.post("/mfa/sms/send")
async def mfa_sms_send(data: dict):
    """Send SMS OTP during login challenge (uses mfa_token) or while configuring (JWT)."""
    mfa_token = data.get("mfa_token")
    user_id = None
    phone = None

    if mfa_token:
        payload = _decode_mfa_token(mfa_token)
        user_id = int(payload["user_id"])
    else:
        raise HTTPException(
            status_code=422,
            detail="mfa_token required (login SMS). For setup use /mfa/sms/send-setup",
        )

    async with SharedSessionLocal() as session:
        result = await session.execute(select(LocalAuthUser).where(LocalAuthUser.id == user_id))
        user = result.scalar_one_or_none()
        if not user or not user.sms_backup_enabled or not user.phone_e164:
            raise HTTPException(status_code=400, detail="SMS backup not available")
        phone = user.phone_e164
        code = mfa_svc.create_sms_otp(user.id, phone)
        send = await mfa_svc.send_sms(
            phone, f"ClearFlow código: {code}. Válido 5 minutos."
        )
        if not send.get("success"):
            raise HTTPException(status_code=502, detail=send.get("error") or "SMS failed")

    out = {
        "message": "Código SMS enviado",
        "phone_masked": _mask_phone(phone),
        "provider": send.get("provider"),
    }
    if send.get("provider") == "stub":
        out["stub_code"] = code  # only when SMS stub enabled — remove in strict prod
        out["note"] = send.get("note")
    return out


@router.get("/mfa/status")
async def mfa_status(current_user: CurrentUser = Depends(get_current_user)):
    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(LocalAuthUser).where(LocalAuthUser.id == int(current_user.id))
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
    return {
        "totp_enabled": bool(user.totp_enabled),
        "sms_backup_enabled": bool(user.sms_backup_enabled),
        "phone_masked": _mask_phone(user.phone_e164),
        "totp_available": mfa_svc.totp_available(),
        "policy": {
            "primary": "totp",
            "backup": "sms",
            "password": "required",
        },
    }


@router.post("/mfa/totp/setup")
async def totp_setup(current_user: CurrentUser = Depends(get_current_user)):
    if not mfa_svc.totp_available():
        raise HTTPException(status_code=503, detail="pyotp not installed on server")
    secret = mfa_svc.generate_totp_secret()
    uri = mfa_svc.totp_provisioning_uri(secret, current_user.email)

    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(LocalAuthUser).where(LocalAuthUser.id == int(current_user.id))
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        # Keep pending secret until enable confirms a code
        user.totp_secret = secret
        user.totp_enabled = 0
        await session.commit()

    return {
        "secret": secret,
        "otpauth_url": uri,
        "issuer": mfa_svc.TOTP_ISSUER,
        "qr_hint": "Escanea el otpauth_url con Google Authenticator, Authy u 1Password",
        "next": "POST /auth/mfa/totp/enable with a 6-digit code from the app",
    }


@router.post("/mfa/totp/enable")
async def totp_enable(
    data: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    code = (data.get("code") or "").strip()
    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(LocalAuthUser).where(LocalAuthUser.id == int(current_user.id))
        )
        user = result.scalar_one_or_none()
        if not user or not user.totp_secret:
            raise HTTPException(status_code=400, detail="Run /mfa/totp/setup first")
        if not mfa_svc.verify_totp(user.totp_secret, code):
            raise HTTPException(status_code=400, detail="Invalid authenticator code")
        user.totp_enabled = 1
        await session.commit()
    return {
        "message": "Authenticator (TOTP) activado. Será obligatorio en el próximo login.",
        "totp_enabled": True,
    }


@router.post("/mfa/totp/disable")
async def totp_disable(
    data: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Require a valid TOTP (or password confirmation) before disabling."""
    code = (data.get("code") or "").strip()
    password = data.get("password") or ""
    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(LocalAuthUser).where(LocalAuthUser.id == int(current_user.id))
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        allowed = False
        if user.totp_secret and code and mfa_svc.verify_totp(user.totp_secret, code):
            allowed = True
        elif password and _verify_password(password, user.password_hash):
            allowed = True
        if not allowed:
            raise HTTPException(status_code=401, detail="Confirm with authenticator code or password")
        user.totp_enabled = 0
        user.totp_secret = None
        await session.commit()
    return {"message": "TOTP desactivado", "totp_enabled": False}


@router.post("/mfa/sms/setup")
async def sms_setup(
    data: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    phone = mfa_svc.normalize_phone(data.get("phone") or "")
    if len(phone) < 8:
        raise HTTPException(status_code=422, detail="Valid phone required (E.164)")
    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(LocalAuthUser).where(LocalAuthUser.id == int(current_user.id))
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.phone_e164 = phone
        # Not enabled until code confirmed
        user.sms_backup_enabled = 0
        await session.commit()
        code = mfa_svc.create_sms_otp(user.id, phone)
        send = await mfa_svc.send_sms(phone, f"ClearFlow: confirma SMS backup con {code}")
        if not send.get("success"):
            raise HTTPException(status_code=502, detail=send.get("error") or "SMS failed")
    out = {
        "message": "Código enviado. Confirma con /mfa/sms/enable",
        "phone_masked": _mask_phone(phone),
        "provider": send.get("provider"),
    }
    if send.get("provider") == "stub":
        out["stub_code"] = code
    return out


@router.post("/mfa/sms/enable")
async def sms_enable(
    data: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    code = (data.get("code") or "").strip()
    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(LocalAuthUser).where(LocalAuthUser.id == int(current_user.id))
        )
        user = result.scalar_one_or_none()
        if not user or not user.phone_e164:
            raise HTTPException(status_code=400, detail="Run /mfa/sms/setup first")
        if not mfa_svc.verify_sms_otp(user.id, user.phone_e164, code):
            raise HTTPException(status_code=400, detail="Invalid SMS code")
        user.sms_backup_enabled = 1
        await session.commit()
    return {
        "message": "SMS de respaldo activado (solo si no puedes usar Authenticator).",
        "sms_backup_enabled": True,
        "phone_masked": _mask_phone(user.phone_e164),
    }


@router.post("/mfa/sms/disable")
async def sms_disable(current_user: CurrentUser = Depends(get_current_user)):
    async with SharedSessionLocal() as session:
        result = await session.execute(
            select(LocalAuthUser).where(LocalAuthUser.id == int(current_user.id))
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.sms_backup_enabled = 0
        user.phone_e164 = None
        await session.commit()
    return {"message": "SMS backup desactivado", "sms_backup_enabled": False}
