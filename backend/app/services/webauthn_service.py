"""
Passkeys / WebAuthn (platform authenticators → Face ID / Touch ID on supported devices).
Requires `webauthn` package.
"""
from __future__ import annotations

import os
from typing import Any

_RP_ID = os.getenv("WEBAUTHN_RP_ID", "localhost")
_RP_NAME = os.getenv("WEBAUTHN_RP_NAME", "ClearFlow")
_ORIGIN = os.getenv("WEBAUTHN_ORIGIN", "http://localhost:5173")

# pending challenges: key -> {challenge, user_id, type}
_CHALLENGES: dict[str, dict[str, Any]] = {}


def webauthn_available() -> bool:
    try:
        import webauthn  # noqa: F401
        return True
    except ImportError:
        return False


def rp_config() -> dict[str, str]:
    return {"rp_id": _RP_ID, "rp_name": _RP_NAME, "origin": _ORIGIN}


def begin_registration(user_id: int, email: str, existing_credential_ids: list[bytes]) -> dict:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
    import json

    exclude = [
        PublicKeyCredentialDescriptor(id=cid)
        for cid in existing_credential_ids
        if cid
    ]
    options = generate_registration_options(
        rp_id=_RP_ID,
        rp_name=_RP_NAME,
        user_id=str(user_id).encode(),
        user_name=email,
        user_display_name=email,
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    _CHALLENGES[f"reg:{user_id}"] = {"challenge": options.challenge, "type": "reg"}
    return json.loads(options_to_json(options))


def complete_registration(user_id: int, credential: dict) -> dict:
    from webauthn import verify_registration_response
    from webauthn.helpers import base64url_to_bytes

    pending = _CHALLENGES.pop(f"reg:{user_id}", None)
    if not pending:
        raise ValueError("No registration challenge — call begin first")
    verification = verify_registration_response(
        credential=credential,
        expected_challenge=pending["challenge"],
        expected_rp_id=_RP_ID,
        expected_origin=_ORIGIN,
    )
    return {
        "credential_id": verification.credential_id,
        "public_key": verification.credential_public_key,
        "sign_count": verification.sign_count,
        "credential_device_type": str(getattr(verification, "credential_device_type", "") or ""),
        "credential_backed_up": bool(getattr(verification, "credential_backed_up", False)),
    }


def begin_authentication(user_id: int, credential_ids: list[bytes]) -> dict:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )
    import json

    allow = [PublicKeyCredentialDescriptor(id=cid) for cid in credential_ids if cid]
    options = generate_authentication_options(
        rp_id=_RP_ID,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    _CHALLENGES[f"auth:{user_id}"] = {"challenge": options.challenge, "type": "auth"}
    return json.loads(options_to_json(options))


def complete_authentication(
    user_id: int,
    credential: dict,
    *,
    public_key: bytes,
    sign_count: int,
) -> int:
    from webauthn import verify_authentication_response

    pending = _CHALLENGES.pop(f"auth:{user_id}", None)
    if not pending:
        raise ValueError("No authentication challenge")
    verification = verify_authentication_response(
        credential=credential,
        expected_challenge=pending["challenge"],
        expected_rp_id=_RP_ID,
        expected_origin=_ORIGIN,
        credential_public_key=public_key,
        credential_current_sign_count=sign_count,
    )
    return int(verification.new_sign_count)
