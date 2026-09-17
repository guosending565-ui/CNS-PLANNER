"""JSON-safe source identity, version evidence and minimal provenance."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

SOURCE_AUDIT_SCHEMA_VERSION = 1


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_id_for(role):
    digest = sha256(str(role).strip().lower().encode()).hexdigest()[:16].upper()
    return f"SRC-{digest}"


def local_path_ref(role):
    return f"local_map_sources:{str(role).strip()}"


def provenance_record(*, source_entity, processing_activity, derived_entity,
                      derived_from=None, method, note=None, timestamp=None):
    return {
        "source_entity": source_entity,
        "processing_activity": processing_activity,
        "derived_entity": derived_entity,
        "derived_from": list(derived_from or [source_entity]),
        "method": method,
        "timestamp": timestamp or utc_now(),
        "note": note,
    }


def empty_source_audits():
    return {
        "status": "not_calculated",
        "schema_version": SOURCE_AUDIT_SCHEMA_VERSION,
        "count": 0,
        "items": {},
    }


def _quick_signature(path):
    source = Path(path) if path else None
    if source is None or not source.is_file():
        return None
    stat = source.stat()
    return {"size_bytes": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _content_fingerprint(role, size_bytes, mtime_ns, sha256_value):
    return sha256(json.dumps({
        "role": role, "size_bytes": size_bytes, "mtime_ns": mtime_ns,
        "sha256": sha256_value,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_manifest(role, path, *, previous=None, details=None, verified_sha256=None,
                    verified_at=None):
    """Build a manifest without retaining an absolute path.

    Unless ``verified_sha256`` is supplied, this performs stat-only checks.  Thus
    ordinary snapshots never hash large files.
    """

    previous = previous if isinstance(previous, dict) else {}
    details = details if isinstance(details, dict) else {}
    signature = _quick_signature(path)
    source = Path(path) if path else None
    reasons = []
    if source is None:
        status = "not_configured"
        reasons.append("source_not_configured")
    elif source.exists() and not source.is_file():
        status = "invalid_source_path"
        reasons.append("source_must_be_concrete_file")
    elif signature is None:
        status = "missing"
        reasons.append("configured_file_missing")
    else:
        verified = previous.get("verification") or {}
        recorded = {
            "size_bytes": verified.get("size_bytes"), "mtime_ns": verified.get("mtime_ns"),
        }
        changed = bool(
            verified.get("sha256") and recorded != signature
        )
        if verified_sha256:
            status = "verified"
        elif changed:
            status = "needs_revalidation"
            reasons.extend(["source_changed", "needs_revalidation"])
        elif verified.get("sha256"):
            status = "verified"
        else:
            status = "configured_unverified"
            reasons.append("explicit_verification_required")
    size = signature.get("size_bytes") if signature else None
    mtime = signature.get("mtime_ns") if signature else None
    verification = deepcopy(previous.get("verification") or {})
    if verified_sha256:
        verification = {
            "sha256": str(verified_sha256), "size_bytes": size, "mtime_ns": mtime,
            "verified_at": verified_at or utc_now(), "method": "sha256_full_file_stream",
        }
    geometry_health = deepcopy(details.get("geometry_health") or previous.get("geometry_health")) or {
        "status": "not_calculated", "feature_count": details.get("feature_count"),
        "null": None, "empty": None, "invalid": None, "unsupported": None,
    }
    source_id = source_id_for(role)
    provenance = deepcopy(details.get("provenance", previous.get("provenance")))
    if not provenance:
        provenance = provenance_record(
            source_entity=source_id,
            processing_activity="source_asset_registration",
            derived_entity=source_id,
            method="local_file_stat" if signature else "local_source_configuration",
            note="绝对路径仅由本机 source registry 解析，不参与稳定身份。",
        )
    manifest = {
        "source_id": source_id,
        "role": str(role),
        "format": (source.suffix.lower().lstrip(".") if source else None),
        "file_name": source.name if source else None,
        "local_path_ref": local_path_ref(role),
        "size": size,
        "mtime": mtime,
        "size_bytes": size,
        "mtime_ns": mtime,
        "sha256": verification.get("sha256"),
        "schema": deepcopy(details.get("schema", previous.get("schema"))),
        "feature_count": details.get("feature_count", previous.get("feature_count")),
        "row_count": details.get("row_count", previous.get("row_count")),
        "extent": deepcopy(details.get("extent", previous.get("extent"))),
        "declared_crs": deepcopy(details.get("declared_crs", previous.get("declared_crs"))),
        "confirmed_crs": deepcopy(details.get("confirmed_crs", previous.get("confirmed_crs"))),
        "geometry_health": geometry_health,
        "provenance": provenance,
        "evidence": deepcopy(details.get("evidence", previous.get("evidence") or [])),
        "verification": verification,
        "verified_at": verification.get("verified_at"),
        "status": status,
        "reasons": reasons,
    }
    manifest["version_fingerprint"] = _content_fingerprint(
        role, size, mtime, verification.get("sha256") if status == "verified" else None,
    ) if signature else None
    return manifest


def sha256_file(path, chunk_size=4 * 1024 * 1024):
    source = Path(path)
    if not source.is_file():
        raise ValueError("数据源必须是存在的具体文件")
    digest = sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_source_audits(value):
    result = empty_source_audits()
    if value is None:
        return result
    if not isinstance(value, dict):
        raise ValueError("source_audits 必须是对象")
    raw_items = value.get("items") or {}
    if isinstance(raw_items, list):
        raw_items = {str(item.get("role")): item for item in raw_items if isinstance(item, dict)}
    if not isinstance(raw_items, dict):
        raise ValueError("source_audits.items 必须是对象")
    items = {}
    for role, item in raw_items.items():
        if not isinstance(item, dict):
            continue
        entry = deepcopy(item)
        entry["role"] = str(entry.get("role") or role)
        entry["source_id"] = source_id_for(entry["role"])
        entry["local_path_ref"] = local_path_ref(entry["role"])
        entry.pop("path", None)
        entry.pop("local_path", None)
        entry.setdefault("status", "configured_unverified")
        entry.setdefault("reasons", ["legacy_audit_requires_verification"])
        entry.setdefault("verification", {})
        entry.setdefault("geometry_health", {"status": "not_calculated"})
        items[entry["role"]] = entry
    result["items"] = items
    result["count"] = len(items)
    result["status"] = "passed" if items else "not_calculated"
    return result
