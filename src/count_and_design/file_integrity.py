"""Canonical JSON hashing and atomic project-file writes."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any


_DOMAIN = b"count-and-design:file-integrity:v1\n"


class IntegrityStatus(str, Enum):
    VERIFIED = "verified"
    LEGACY_UNSIGNED = "legacy_unsigned"
    MODIFIED = "modified"


def _unsigned_payload(payload: dict[str, Any]) -> dict[str, Any]:
    unsigned = copy.deepcopy(payload)
    unsigned.pop("file_integrity", None)
    return unsigned


def canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(
        _unsigned_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _DOMAIN + encoded


def calculate_file_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


def attach_file_integrity(payload: dict[str, Any]) -> dict[str, Any]:
    signed = _unsigned_payload(payload)
    signed["file_integrity"] = {
        "version": 1,
        "algorithm": "sha256",
        "digest": calculate_file_digest(signed),
    }
    return signed


def verify_file_integrity(payload: dict[str, Any]) -> IntegrityStatus:
    metadata = payload.get("file_integrity")
    schema_version = payload.get("schema_version", 1)
    if metadata is None:
        return (
            IntegrityStatus.LEGACY_UNSIGNED
            if schema_version == 1
            else IntegrityStatus.MODIFIED
        )
    if not isinstance(metadata, dict):
        return IntegrityStatus.MODIFIED
    if metadata.get("version") != 1 or metadata.get("algorithm") != "sha256":
        return IntegrityStatus.MODIFIED
    digest = metadata.get("digest")
    if not isinstance(digest, str):
        return IntegrityStatus.MODIFIED
    expected = calculate_file_digest(payload)
    return (
        IntegrityStatus.VERIFIED
        if hmac.compare_digest(digest, expected)
        else IntegrityStatus.MODIFIED
    )


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return target
