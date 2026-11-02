"""
Tenant document vault — ClearFlow-hosted default (local filesystem).
BYO cloud is a preference stub; connection comes later.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

VAULT_ROOT = Path(
    os.getenv("CLEARFLOW_VAULT_DIR")
    or os.getenv("UPLOADS_DIR")
    or "/tmp/clearflow-vault"
)


def ensure_vault_root() -> Path:
    VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    return VAULT_ROOT


def tenant_dir(tenant_id: str | uuid.UUID) -> Path:
    d = ensure_vault_root() / str(tenant_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_bytes(
    tenant_id: str | uuid.UUID,
    *,
    filename: str,
    data: bytes,
    category: str = "other",
) -> dict:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (filename or "file"))[:180]
    key_name = f"{uuid.uuid4().hex}_{safe}"
    rel_key = f"{tenant_id}/{category}/{key_name}"
    dest = ensure_vault_root() / str(tenant_id) / category
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / key_name
    path.write_bytes(data)
    return {
        "storage_backend": "clearflow_local",
        "storage_key": rel_key,
        "absolute_path": str(path),
        "size_bytes": len(data),
        "checksum_sha256": hashlib.sha256(data).hexdigest(),
    }


def resolve_path(storage_key: str) -> Path:
    path = ensure_vault_root() / storage_key
    # Prevent path traversal
    root = ensure_vault_root().resolve()
    resolved = path.resolve()
    if not str(resolved).startswith(str(root)):
        raise ValueError("invalid storage_key")
    return resolved


def read_bytes(storage_key: str) -> bytes:
    return resolve_path(storage_key).read_bytes()
