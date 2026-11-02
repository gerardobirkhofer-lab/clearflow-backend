"""
Tenant document vault — ClearFlow local default + BYO S3-compatible.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

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
    backend: str = "clearflow_local",
    s3_cfg: dict[str, Any] | None = None,
) -> dict:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (filename or "file"))[:180]
    key_name = f"{uuid.uuid4().hex}_{safe}"
    rel_key = f"{tenant_id}/{category}/{key_name}"
    checksum = hashlib.sha256(data).hexdigest()

    if backend == "s3":
        _s3_put(rel_key, data, s3_cfg or {})
        return {
            "storage_backend": "s3",
            "storage_key": rel_key,
            "absolute_path": None,
            "size_bytes": len(data),
            "checksum_sha256": checksum,
        }

    dest = ensure_vault_root() / str(tenant_id) / category
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / key_name
    path.write_bytes(data)
    return {
        "storage_backend": "clearflow_local",
        "storage_key": rel_key,
        "absolute_path": str(path),
        "size_bytes": len(data),
        "checksum_sha256": checksum,
    }


def resolve_path(storage_key: str) -> Path:
    path = ensure_vault_root() / storage_key
    root = ensure_vault_root().resolve()
    resolved = path.resolve()
    if not str(resolved).startswith(str(root)):
        raise ValueError("invalid storage_key")
    return resolved


def read_bytes(
    storage_key: str,
    *,
    backend: str = "clearflow_local",
    s3_cfg: dict[str, Any] | None = None,
) -> bytes:
    if backend == "s3":
        return _s3_get(storage_key, s3_cfg or {})
    return resolve_path(storage_key).read_bytes()


def _minio_client(cfg: dict[str, Any]):
    try:
        from minio import Minio
    except ImportError as e:
        raise RuntimeError(
            "minio package required for BYO S3. Add minio to dependencies."
        ) from e
    endpoint = (cfg.get("endpoint") or "").replace("https://", "").replace("http://", "")
    secure = str(cfg.get("secure", True)).lower() != "false"
    if "://" in (cfg.get("endpoint") or ""):
        secure = (cfg.get("endpoint") or "").startswith("https")
    return Minio(
        endpoint,
        access_key=cfg["access_key"],
        secret_key=cfg["secret_key"],
        secure=secure,
        region=cfg.get("region") or None,
    )


def _s3_put(key: str, data: bytes, cfg: dict[str, Any]) -> None:
    from io import BytesIO

    client = _minio_client(cfg)
    bucket = cfg["bucket"]
    found = client.bucket_exists(bucket)
    if not found:
        client.make_bucket(bucket, location=cfg.get("region") or None)
    client.put_object(
        bucket,
        key,
        BytesIO(data),
        length=len(data),
        content_type="application/octet-stream",
    )


def _s3_get(key: str, cfg: dict[str, Any]) -> bytes:
    client = _minio_client(cfg)
    resp = client.get_object(cfg["bucket"], key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()
