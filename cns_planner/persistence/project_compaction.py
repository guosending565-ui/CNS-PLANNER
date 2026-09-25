"""Compact derived project results into a content-addressed sidecar."""

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path

from .project_repository import ProjectRepository


RESULT_INDEX_VERSION = 1
RESULT_DIRECTORY = ".cns-results"


def compact_and_store(state, project_path):
    """Return the small persisted document after durably storing large results."""

    document = {
        key: value for key, value in state.items()
        if key not in ("_population_shelter_cache", "_planning_exposure_cache")
    }
    risk = state.get("grid_risk_v2") if isinstance(state.get("grid_risk_v2"), dict) else {}
    candidates = (
        state.get("layered_route_candidates")
        if isinstance(state.get("layered_route_candidates"), dict) else {}
    )
    constraint_fields = (
        state.get("planning_constraint_fields")
        if isinstance(state.get("planning_constraint_fields"), dict) else {}
    )
    payload = {
        "grid_risk_v2_cells": risk.get("cells") or {},
        "layered_route_candidate_items": candidates.get("items") or [],
        "layered_route_candidate_masks": candidates.get("masks") or {},
        "planning_constraint_field_cells": {
            str(item.get("field_id")): item.get("cells") or []
            for item in constraint_fields.get("items") or []
            if isinstance(item, dict) and item.get("field_id")
        },
    }
    raw = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    digest = hashlib.sha256(compressed).hexdigest()
    relative = Path(RESULT_DIRECTORY) / f"{digest}.json.gz"
    artifact_path = Path(project_path).parent / relative
    if not artifact_path.is_file():
        ProjectRepository(artifact_path).save_bytes(compressed)

    compact_risk = {key: value for key, value in risk.items() if key != "cells"}
    compact_risk["cells"] = {}
    compact_candidates = {
        key: value for key, value in candidates.items() if key not in ("items", "masks")
    }
    compact_candidates["items"] = []
    compact_candidates["masks"] = {}
    document["grid_risk_v2"] = compact_risk
    document["layered_route_candidates"] = compact_candidates
    compact_fields = {
        key: value for key, value in constraint_fields.items() if key != "items"
    }
    compact_fields["items"] = [
        {key: value for key, value in item.items() if key != "cells"}
        for item in constraint_fields.get("items") or [] if isinstance(item, dict)
    ]
    document["planning_constraint_fields"] = compact_fields
    document["result_index"] = {
        "schema_version": RESULT_INDEX_VERSION,
        "artifact": relative.as_posix(),
        "sha256": digest,
        "grid_risk_v2": {
            "status": risk.get("status"),
            "input_fingerprint": risk.get("input_fingerprint"),
            "policy_fingerprint": risk.get("policy_fingerprint"),
            "cell_count": len(payload["grid_risk_v2_cells"]),
        },
        "layered_route_candidates": {
            "status": candidates.get("status"),
            "active_candidate_id": candidates.get("active_candidate_id"),
            "count": candidates.get("count", len(payload["layered_route_candidate_items"])),
            "item_count": len(payload["layered_route_candidate_items"]),
            "mask_count": len(payload["layered_route_candidate_masks"]),
            "fingerprints": [
                item.get("fingerprint") or item.get("input_fingerprint")
                for item in payload["layered_route_candidate_items"]
                if isinstance(item, dict)
                and (item.get("fingerprint") or item.get("input_fingerprint"))
            ],
        },
        "planning_constraint_fields": {
            "status": constraint_fields.get("status"),
            "count": constraint_fields.get("count", len(compact_fields["items"])),
            "field_count": len(payload["planning_constraint_field_cells"]),
            "cell_count": sum(
                len(items) for items in payload["planning_constraint_field_cells"].values()
            ),
            "fingerprints": [
                item.get("constraint_field_fingerprint")
                for item in constraint_fields.get("items") or []
                if isinstance(item, dict) and item.get("constraint_field_fingerprint")
            ],
        },
    }
    return document


def restore_compacted_results(document, project_path):
    """Rehydrate large derived results referenced by a persisted result index."""

    index = document.get("result_index") if isinstance(document, dict) else None
    if not isinstance(index, dict) or not index.get("artifact"):
        return document
    if index.get("schema_version") != RESULT_INDEX_VERSION:
        raise ValueError("不支持的项目结果索引版本")
    artifact_path = ProjectRepository._result_artifact_path(document, project_path)
    compressed = artifact_path.read_bytes()
    if hashlib.sha256(compressed).hexdigest() != index.get("sha256"):
        raise ValueError("项目结果文件指纹不匹配")
    payload = _decode_artifact(compressed)

    restored = deepcopy(document)
    risk = restored.setdefault("grid_risk_v2", {})
    risk["cells"] = payload.get("grid_risk_v2_cells") or {}
    candidates = restored.setdefault("layered_route_candidates", {})
    candidates["items"] = payload.get("layered_route_candidate_items") or []
    candidates["masks"] = payload.get("layered_route_candidate_masks") or {}
    fields = restored.setdefault("planning_constraint_fields", {})
    cell_payload = payload.get("planning_constraint_field_cells") or {}
    fields["items"] = [
        {
            **item,
            "cells": cell_payload.get(str(item.get("field_id"))) or [],
        }
        for item in fields.get("items") or [] if isinstance(item, dict)
    ]
    restored.pop("_population_shelter_cache", None)
    return restored


def read_result_artifact(document, project_path):
    """只读读取项目结果 sidecar 的原始 payload（**不改写**任何状态）。

    B4X 的只读展示读取路径（``GET /api/planning-constraint-field*``）使用它把
    「内存里没有逐 cell 明细」的工程补齐成可展示结果。契约与
    :func:`restore_compacted_results` 完全一致：
      * 索引缺失 / 版本不符 / 文件缺失 / 指纹不符 / 内容损坏一律**抛异常**，
        由调用方决定如何降级（绝不返回半份 payload 假装成功）；
      * 文件系统路径不暴露给 HTTP 层：调用方只拿到解析后的 dict。
    """

    index = document.get("result_index") if isinstance(document, dict) else None
    if not isinstance(index, dict) or not index.get("artifact"):
        raise ValueError("当前项目没有结果索引")
    if index.get("schema_version") != RESULT_INDEX_VERSION:
        raise ValueError("不支持的项目结果索引版本")
    artifact_path = ProjectRepository._result_artifact_path(document, project_path)
    compressed = artifact_path.read_bytes()
    if hashlib.sha256(compressed).hexdigest() != index.get("sha256"):
        raise ValueError("项目结果文件指纹不匹配")
    return _decode_artifact(compressed)


def _decode_artifact(compressed: bytes) -> dict:
    try:
        return json.loads(gzip.decompress(compressed).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("项目结果文件损坏") from exc
