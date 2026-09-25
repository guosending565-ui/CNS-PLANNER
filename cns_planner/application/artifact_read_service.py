"""Phase4-B5X：Application 层统一 artifact 只读读取服务。

对外只暴露三种读取能力（对应 B5X 第 8 节的"read summary / read content /
read bounded subset"）：

* :meth:`summaries` / :meth:`summary` —— 项目当前引用的 canonical artifact 摘要。
  只需 metadata，**不解压**任何 artifact。
* :meth:`content` —— 受界读取（route_id / subsystem / grid_ids / bbox / offset /
  limit）。解压后只把筛选出的子集交给调用方，绝不把整份 artifact 直接转给前端。
* :meth:`inventory` —— dry-run GC 清单（只列不删）。

失败语义（B5X 第 12 节）：缺失 / 指纹不符 / gzip 损坏 / 版本不支持一律抛
结构化 :class:`~cns_planner.persistence.artifact_store.ArtifactError`，由 HTTP 层
翻译成"明细不可用 / 数据损坏，请重新计算"。**绝不**静默返回空结果，也绝不把它
当作 0 或"全部可通行"。
"""

from __future__ import annotations

from copy import deepcopy

from ..persistence.artifact_store import (
    ArtifactError, ArtifactStore, ArtifactUnavailable,
)
from ..persistence.project_compaction import (
    artifact_inventory, artifact_manifest_for, scope_endpoint, scope_keys,
)
from ..persistence.project_compaction import read_scope_artifact


#: 逐 cell / 逐 sample / 逐 voxel 明细的候选字段名（bounded 读取时用于展平）。
_RECORD_FIELDS = (
    "cells", "samples", "voxels", "evidence", "panels", "selected_panels",
    "entries", "records", "items", "segments",
)


class ArtifactReadService:
    """canonical artifact 的唯一只读入口（不修改任何项目状态）。"""

    def __init__(self, session):
        self.session = session

    # ---- summary ------------------------------------------------------------

    def manifest(self):
        manifest = artifact_manifest_for(self.session.state) or {}
        return {
            "schema_version": manifest.get("schema_version"),
            "artifact_count": len(manifest.get("entries") or {}),
            "refs": deepcopy(manifest.get("refs") or {}),
            "migrated_from": deepcopy(manifest.get("migrated_from")),
        }

    def summaries(self):
        """每个已登记 scope 的 artifact 摘要（含"尚未外置"的显式状态）。"""

        state = self.session.state
        manifest = artifact_manifest_for(state) or {}
        entries = manifest.get("entries") if isinstance(manifest.get("entries"), dict) else {}
        refs = manifest.get("refs") if isinstance(manifest.get("refs"), dict) else {}
        store = self._store()
        items = []
        for logical_key in scope_keys():
            artifact_id = refs.get(logical_key)
            entry = entries.get(artifact_id) if artifact_id else None
            if not isinstance(entry, dict):
                items.append({
                    "logical_key": logical_key,
                    "status": "not_externalized",
                    "detail_endpoint": scope_endpoint(logical_key),
                    "summary": self._container_summary(state, logical_key),
                })
                continue
            items.append({
                "logical_key": logical_key,
                "status": "available",
                "artifact_id": entry.get("artifact_id"),
                "artifact_type": entry.get("artifact_type"),
                "schema_version": entry.get("schema_version"),
                "sha256": entry.get("sha256"),
                "content_encoding": entry.get("content_encoding"),
                "relative_path": entry.get("relative_path"),
                "size_bytes": entry.get("size_bytes"),
                "created_at": entry.get("created_at"),
                "producer": deepcopy(entry.get("producer")),
                "input_fingerprint": entry.get("input_fingerprint"),
                "scope": deepcopy(entry.get("scope")),
                "summary": deepcopy(entry.get("summary")),
                "detail_endpoint": scope_endpoint(logical_key),
                "readable": store.exists(str(entry.get("relative_path") or "")),
            })
        return {"schema_version": 1, "count": len(items), "items": items}

    def summary(self, logical_key):
        for item in self.summaries()["items"]:
            if item["logical_key"] == str(logical_key):
                return item
        raise ArtifactUnavailable(
            "未登记的结果明细类型", code="artifact_scope_unknown",
            detail={"logical_key": logical_key},
        )

    @staticmethod
    def _container_summary(state, logical_key):
        from ..persistence.project_compaction import EXTERNAL_SCOPES

        scope = next(
            (item for item in EXTERNAL_SCOPES if item["logical_key"] == logical_key), None
        )
        if scope is None:
            return {}
        container = state.get(scope["container_key"])
        if not isinstance(container, dict):
            return {}
        summary = {
            key: deepcopy(container[key])
            for key in ("status", "count", "input_fingerprint", "policy_fingerprint",
                        "artifact_ref", "detail_status")
            if container.get(key) is not None
        }
        return summary

    # ---- bounded content ----------------------------------------------------

    def content(
        self, logical_key, *, route_id=None, subsystem=None, grid_ids=None,
        bbox=None, offset=0, limit=None,
    ):
        """受界读取：解压后只返回筛选 + 分页后的子集。"""

        payload = read_scope_artifact(self.session.state, self.session.store_path, logical_key)
        parts = payload.get("parts") if isinstance(payload, dict) else None
        if not isinstance(parts, dict):
            parts = {}
        selected = self._select_parts(parts, route_id=route_id, subsystem=subsystem)
        wanted = _normalize_grid_ids(grid_ids)
        box = _normalize_bbox(bbox)
        flattened = []
        for key, value in selected.items():
            flattened.extend(_records(key, value))
        filtered = [
            record for record in flattened
            if _record_matches(record, wanted=wanted, box=box)
        ]
        total = len(filtered)
        start = max(0, int(offset or 0))
        end = None if limit in (None, "") else start + max(0, int(limit))
        window = filtered[start:end]
        return {
            "schema_version": 1,
            "logical_key": str(logical_key),
            "status": "passed",
            "artifact_ref": self._reference(logical_key),
            "detail_endpoint": scope_endpoint(logical_key),
            "filters": {
                "route_id": route_id, "subsystem": subsystem,
                "grid_ids": sorted(wanted) if wanted else None, "bbox": box,
            },
            "part_count": len(selected),
            "total_count": total,
            "offset": start,
            "limit": None if end is None else max(0, int(limit)),
            "returned_count": len(window),
            "truncated": end is not None and end < total,
            "records": window,
        }

    # ---- dry-run GC ---------------------------------------------------------

    def inventory(self):
        return artifact_inventory(self.session.state, self.session.store_path)

    # ---- 内部 ---------------------------------------------------------------

    def _store(self):
        return ArtifactStore(self.session.store_path)

    def _reference(self, logical_key):
        manifest = artifact_manifest_for(self.session.state) or {}
        entries = manifest.get("entries") if isinstance(manifest.get("entries"), dict) else {}
        refs = manifest.get("refs") if isinstance(manifest.get("refs"), dict) else {}
        artifact_id = refs.get(str(logical_key))
        entry = entries.get(artifact_id) if artifact_id else None
        if not isinstance(entry, dict):
            return None
        from ..persistence.artifact_store import artifact_ref

        reference = artifact_ref(entry)
        reference["logical_key"] = str(logical_key)
        return reference

    @staticmethod
    def _select_parts(parts, *, route_id=None, subsystem=None):
        selected = {}
        for key, value in parts.items():
            text = str(key)
            if route_id and str(route_id) not in text:
                continue
            if subsystem and str(subsystem) not in text:
                continue
            selected[text] = value
        return selected


def _records(key, value):
    """把 part 值展平成记录列表（list → 逐项；dict → 逐项并补 key）。"""

    if isinstance(value, list):
        return [
            {"part": key, "index": index, "record": deepcopy(item)}
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return [
            {"part": key, "index": name, "record": deepcopy(item)}
            for name, item in value.items()
        ]
    return []


def _record_matches(entry, *, wanted, box):
    record = entry.get("record")
    if wanted:
        grid_id = str((record or {}).get("grid_id") or entry.get("index") or "")
        if grid_id not in wanted:
            return False
    if box is not None and isinstance(record, dict):
        cell = record.get("bbox")
        if isinstance(cell, (list, tuple)) and len(cell) == 4:
            try:
                west, south, east, north = (float(item) for item in cell)
            except (TypeError, ValueError):
                return True
            if east <= box[0] or west >= box[2] or north <= box[1] or south >= box[3]:
                return False
    return True


def _normalize_grid_ids(value):
    if value in (None, ""):
        return set()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return set()
    return {str(item).strip() for item in items if str(item).strip()}


def _normalize_bbox(value):
    """非法 / 反向 / 越界 bbox 一律忽略（返回 None），绝不猜一个范围。"""

    if value in (None, ""):
        return None
    raw = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    numbers = []
    for item in raw:
        try:
            number = float(item)
        except (TypeError, ValueError):
            return None
        if number != number or number in (float("inf"), float("-inf")):
            return None
        numbers.append(number)
    west, south, east, north = numbers
    if west > east or south > north:
        return None
    return numbers


__all__ = ["ArtifactReadService"]
