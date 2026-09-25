"""Phase4-B5X：统一 compaction pipeline —— 把大型派生明细外置为 canonical artifact。

设计边界（严格保持）：

* compaction **不是业务计算**。它只做三件事：识别可外置的大字段 → 写 artifact →
  在 ProjectState 里替换为 ``artifact_ref`` / summary。它绝不改变 status、assessment、
  authority、assumptions、confirmation、algorithm selection 或任何业务值。
* ProjectState 只保存 authoritative inputs / policies / confirmations / active IDs /
  summary / fingerprint / ``artifact_ref`` / review & adoption records。逐 cell /
  逐 sample / 逐 voxel 的明细一律进 artifact。
* 旧项目（inline 大字段，或 B3X 的单个 ``result_index`` bundle）继续可读：
  读取路径提供兼容视图；只有在**重新计算或用户显式保存**时才写新格式。
* 旧格式读取失败绝不静默变成"0 / 全部可通行"：明细不可用时按
  ``artifact_unavailable`` 记录诊断，业务 status 不被 IO 失败冒充成领域 assessment failed。

外置位置由**声明式路径模板**描述（见 :data:`EXTERNAL_SCOPES`）：每个模板是一串
步进指令，``("field", name)`` 进入子字段、``("list",)`` 遍历列表、``("mapping",)``
遍历字典值。compaction 只清空模板指向的字段，其余（status / counts / fingerprint /
warnings / ids）原样留在 ProjectState。
"""

from __future__ import annotations

from copy import deepcopy

from .artifact_store import (
    ARTIFACT_SCHEMA_VERSION, ArtifactError, ArtifactStore, artifact_ref,
    is_artifact_ref,
)
from .project_repository import ProjectRepository


RESULT_INDEX_VERSION = 1
RESULT_DIRECTORY = ".cns-results"
ARTIFACT_MANIFEST_KEY = "artifact_manifest"
MANIFEST_SCHEMA_VERSION = 1

#: 派生缓存：不进入持久化文档（可由现有输入重建）。
_CACHE_KEYS = ("_population_shelter_cache", "_planning_exposure_cache")

FIELD = "field"
LIST = "list"
MAPPING = "mapping"
#: 外置后容器里该字段的处理方式：置空（``[]``/``{}``）或整键删除。
EMPTY = "empty"
DROP = "drop"


def _f(name):
    return (FIELD, name)


_LIST = (LIST,)
_MAPPING = (MAPPING,)


def _scope(
    logical_key, artifact_type, container_key, templates, *, producer=None,
    summary_keys=(), clear=EMPTY,
):
    return {
        "logical_key": logical_key,
        "artifact_type": artifact_type,
        "container_key": container_key,
        "templates": tuple(templates),
        "producer": dict(producer or {"service": None, "algorithm_id": None,
                                      "algorithm_version": None}),
        "summary_keys": tuple(summary_keys),
        "clear": clear,
    }


#: 本轮统一外置的大型 canonical 派生明细（结果对象粒度）。
EXTERNAL_SCOPES = (
    _scope(
        "grid", "workspace.grid.cells", "grid",
        [(_f("cells"),)],
        producer={"service": "WorkspaceGridService", "algorithm_id": "workspace-grid",
                  "algorithm_version": "1"},
        summary_keys=("level", "crs", "bbox"),
    ),
    _scope(
        "grid_attributes", "grid_attribute.cells", "grid_attributes",
        [(_MAPPING, _f("cells"))],
        producer={"service": "grid attribution service", "algorithm_id": None,
                  "algorithm_version": None},
    ),
    _scope(
        "grid_risk", "risk.grid_risk.cells", "grid_risk",
        [(_f("cells"),)],
        producer={"service": "RiskService", "algorithm_id": "risk_model",
                  "algorithm_version": "1"},
        summary_keys=("input_fingerprint",),
    ),
    _scope(
        "grid_risk_v2", "risk.grid_risk_v2.cells", "grid_risk_v2",
        [(_f("cells"),)],
        producer={"service": "RiskFrameworkV2Service", "algorithm_id": "risk-framework-v2",
                  "algorithm_version": "2.0"},
        summary_keys=("input_fingerprint", "policy_fingerprint"),
    ),
    _scope(
        "layered_route_candidates", "layered_route.candidates",
        "layered_route_candidates",
        [(_f("items"),), (_f("masks"),)],
        producer={"service": "LayeredRoutePlannerService", "algorithm_id": None,
                  "algorithm_version": None},
        summary_keys=("active_candidate_id", "current_key"),
    ),
    _scope(
        "planning_constraint_fields", "planning_constraint_field.cells",
        "planning_constraint_fields",
        [(_f("items"), _LIST, _f("cells"))],
        producer={"service": "PlanningConstraintFieldService",
                  "algorithm_id": "planning-constraint-field", "algorithm_version": "1"},
        summary_keys=("policy_fingerprint",),
        clear=DROP,
    ),
    _scope(
        "coverage_3d", "coverage.coverage_3d.detail", "coverage_3d",
        [
            (_f("routes"), _LIST, _f("samples")),
            (_f("routes"), _LIST, _f("subsystems"), _LIST, _f("samples")),
            (_f("routes"), _LIST, _f("subsystems"), _LIST, _f("uncovered_segments"),
             _LIST, _f("path")),
        ],
        producer={"service": "Spatial3DService", "algorithm_id": "coverage_model",
                  "algorithm_version": "3d-v1"},
        summary_keys=("input_fingerprint", "route_count"),
    ),
    _scope(
        "cns_service_capability", "capability.service_capability.detail",
        "cns_service_capability",
        [
            (_f("routes"), _LIST, _f("samples")),
            (_f("routes"), _LIST, _f("subsystems"), _LIST, _f("samples")),
        ],
        producer={"service": "CNSServiceCapabilityService",
                  "algorithm_id": "cns-service-capability", "algorithm_version": "1"},
        summary_keys=("input_fingerprint", "route_count"),
    ),
    _scope(
        "cns_corridor_assessment", "corridor.assessment.detail",
        "cns_corridor_assessment",
        [
            (_f("routes"), _LIST, _f("voxels")),
            (_f("routes"), _LIST, _f("corridor_geometry"), _f("included_grid_ids")),
            (_f("routes"), _LIST, _f("subsystems"), _LIST, _f("deficit_voxel_ids")),
            (_f("routes"), _LIST, _f("subsystems"), _LIST, _f("unknown_voxel_ids")),
        ],
        producer={"service": "CorridorService", "algorithm_id": "cns-corridor",
                  "algorithm_version": "1"},
        summary_keys=("input_fingerprint", "corridor_geometry_fingerprint",
                      "route_count"),
    ),
    _scope(
        "cns_corridor_gap_assessment", "corridor.gap.detail",
        "cns_corridor_gap_assessment",
        [
            (_f("routes"), _LIST, _f("voxels")),
            (_f("routes"), _LIST, _f("subsystems"), _LIST,
             _f("continuous_deficit_segments"), _LIST, _f("voxel_ids")),
        ],
        producer={"service": "CorridorGapService", "algorithm_id": "cns-corridor-gap",
                  "algorithm_version": "1"},
        summary_keys=("input_fingerprint", "route_count"),
    ),
    _scope(
        "cns_corridor_site_plan", "corridor.site_plan.detail",
        "cns_corridor_site_plan",
        [
            (_f("final_hypothetical_evidence"),),
            (_f("candidate_impacts"),),
            (_f("iteration_trace"),),
        ],
        producer={"service": "CorridorSitePlanningService",
                  "algorithm_id": "corridor-site-planning", "algorithm_version": "2"},
        summary_keys=("input_fingerprint", "status", "stop_reason",
                      "target_voxel_count"),
    ),
    _scope(
        "cns_gap_analysis_v2", "gap.analysis_v2.detail", "cns_gap_analysis_v2",
        [
            (_f("cells"),),
            (_f("spatial_detail"),),
            (_f("routes"), _LIST, _f("subsystems"), _LIST, _f("samples")),
        ],
        producer={"service": "GapAnalysisV2Service", "algorithm_id": "cns-gap-analyzer",
                  "algorithm_version": "2"},
        summary_keys=("input_fingerprint",),
    ),
    _scope(
        "radar_surveillance_layout", "radar.layout.detail", "radar_surveillance_layout",
        [
            (_f("items"), _LIST, _f("samples")),
            (_f("items"), _LIST, _f("optimisation_samples")),
            (_f("items"), _LIST, _f("validation"), _f("samples")),
            (_f("items"), _LIST, _f("validation"), _f("coverage_profile"), _f("entries")),
            (_f("items"), _LIST, _f("selected_panels")),
            (_f("items"), _LIST, _f("uncovered_segments")),
            (_f("items"), _LIST, _f("under_redundant_segments")),
            (_f("items"), _LIST, _f("unknown_segments")),
            (_f("items"), _LIST, _f("refinement_rounds")),
            (_f("items"), _LIST, _f("presolve")),
            (_f("items"), _LIST, _f("stage_a")),
            (_f("items"), _LIST, _f("stage_b")),
        ],
        producer={"service": "RadarSurveillanceLayoutService",
                  "algorithm_id": "radar-surveillance-layout",
                  "algorithm_version": "1.1"},
        summary_keys=("count",),
    ),
    _scope(
        "route_planning_experiments", "experiment.route_planning.detail",
        "route_planning_experiments",
        [
            (_f("records"), _LIST, _f("result")),
            (_f("records"), _LIST, _f("context_basis")),
        ],
        producer={"service": "RoutePlanningExperimentService", "algorithm_id": None,
                  "algorithm_version": None},
    ),
)

_SCOPES_BY_KEY = {scope["logical_key"]: scope for scope in EXTERNAL_SCOPES}

#: 每个外置 scope 的专用只读读取入口（前端/HTTP 按需拉取明细用）。
DETAIL_ENDPOINTS = {
    "grid": "/api/workspace/grid",
    "grid_attributes": "/api/workspace/grid/attributes",
    "grid_risk": None,
    "grid_risk_v2": "/api/grid-risk-v2",
    "layered_route_candidates": "/api/layered-route-candidates",
    "planning_constraint_fields": "/api/planning-constraint-field",
    "coverage_3d": "/api/coverage-3d",
    "cns_service_capability": "/api/cns-service-capability",
    "cns_corridor_assessment": "/api/cns-service-corridor",
    "cns_corridor_gap_assessment": "/api/cns-corridor-gap",
    "cns_corridor_site_plan": "/api/cns-corridor-site-plan",
    "cns_gap_analysis_v2": "/api/cns-gap-analysis-v2",
    "radar_surveillance_layout": "/api/radar-surveillance-layout",
    "route_planning_experiments": "/api/route-experiments",
}


def scope_keys():
    """全部已登记的外置 scope（供读取服务枚举）。"""

    return tuple(_SCOPES_BY_KEY)


def scope_endpoint(logical_key):
    return DETAIL_ENDPOINTS.get(str(logical_key))


def artifact_manifest_for(document):
    manifest = document.get(ARTIFACT_MANIFEST_KEY)
    return manifest if isinstance(manifest, dict) else None


def compact_and_store(state, project_path):
    """把大型派生明细写进 artifact，返回**待持久化**的 ProjectState 文档。

    返回的文档与 ``state`` 不共享任何被外置的容器：所有被改动的层级都重新构造，
    因此调用方内存中的 ``state`` 不会被就地改写。
    """

    document = {key: value for key, value in state.items() if key not in _CACHE_KEYS}
    store = ArtifactStore(project_path)
    previous_manifest = artifact_manifest_for(state) or {}
    previous_entries = (
        previous_manifest.get("entries") if isinstance(previous_manifest, dict) else {}
    )
    entries = {}
    refs = {}
    for scope in EXTERNAL_SCOPES:
        container = document.get(scope["container_key"])
        if not isinstance(container, dict):
            continue
        compacted, parts = _compact_container(scope, container)
        if compacted is None:
            continue
        if parts:
            metadata = store.publish(
                {"logical_key": scope["logical_key"], "parts": parts},
                artifact_type=scope["artifact_type"],
                producer=scope["producer"],
                input_fingerprint=_input_fingerprint(compacted),
                scope=_scope_descriptor(scope, compacted),
                summary=_summary(scope, compacted, parts),
                previous_entries=previous_entries,
            )
            entries[metadata["artifact_id"]] = metadata
            refs[scope["logical_key"]] = metadata["artifact_id"]
        document[scope["container_key"]] = compacted

    legacy = document.pop("result_index", None)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "entries": entries,
        "refs": refs,
    }
    if isinstance(legacy, dict) and legacy:
        # 旧项目首次落盘：记录"从 legacy bundle 迁移而来"，保留可审计性。
        manifest["migrated_from"] = {
            "schema_version": legacy.get("schema_version"),
            "artifact": legacy.get("artifact"),
            "sha256": legacy.get("sha256"),
        }
    document[ARTIFACT_MANIFEST_KEY] = manifest
    return document


def restore_compacted_results(document, project_path):
    """兼容读取：把 artifact 明细水合成可继续使用的 ProjectState 视图。

    * 新格式：按 ``artifact_manifest`` 逐 scope 还原；单个 artifact 不可用时只把
      该明细标记为不可用，其余结果照常还原，项目仍能打开。
    * 旧格式（B3X ``result_index`` 单 bundle）：走兼容读取路径。
    * 旧格式（完全 inline）：原样返回，不修改用户项目文件。
    """

    if not isinstance(document, dict):
        return document
    restored = deepcopy(document)
    manifest = artifact_manifest_for(restored)
    if manifest is None:
        legacy_index = restored.get("result_index")
        if isinstance(legacy_index, dict) and legacy_index.get("artifact"):
            return _restore_legacy_bundle(restored, legacy_index, project_path)
        # inline 项目：明细本来就在文档里。
        return restored

    store = ArtifactStore(project_path)
    diagnostics = []
    for scope in EXTERNAL_SCOPES:
        container = restored.get(scope["container_key"])
        if not isinstance(container, dict):
            continue
        reference = _container_ref(container) or _entry_ref(manifest, scope["logical_key"])
        if reference is None:
            continue
        try:
            payload = store.read(reference)
        except ArtifactError as exc:
            _mark_unavailable(scope, restored, exc)
            diagnostics.append({"scope": scope["logical_key"], **exc.as_dict()})
            continue
        _restore_parts(restored, scope, payload)
    restored[ARTIFACT_MANIFEST_KEY] = manifest
    if diagnostics:
        restored["artifact_diagnostics"] = diagnostics
    restored.pop("_population_shelter_cache", None)
    return restored


def read_scope_artifact(document, project_path, logical_key):
    """按 scope 读取 artifact payload；失败抛结构化异常（绝不返回半份结果）。"""

    scope = _SCOPES_BY_KEY.get(str(logical_key))
    if scope is None:
        raise ArtifactError(f"未登记的 artifact scope：{logical_key}")
    store = ArtifactStore(project_path)
    container = (document or {}).get(scope["container_key"])
    reference = _container_ref(container)
    manifest = artifact_manifest_for(document) or {}
    if reference is None:
        reference = _entry_ref(manifest, scope["logical_key"])
    if reference is None:
        legacy = (document or {}).get("result_index")
        if isinstance(legacy, dict) and legacy.get("artifact"):
            return _legacy_scope_payload(store, legacy, scope)
        raise ArtifactError("当前项目没有结果明细索引", code="artifact_ref_missing")
    return store.read(reference)


def read_result_artifact(document, project_path):
    """B3X/B4X 兼容入口：返回约束场逐 cell 明细 payload（``..._cells`` 形状）。

    只读，不改写任何状态；索引缺失 / 版本不符 / 文件缺失 / 指纹不符 / 内容损坏
    一律抛异常，由调用方降级为"明细不可用"。
    """

    payload = read_scope_artifact(document, project_path, "planning_constraint_fields")
    if isinstance(payload, dict) and "planning_constraint_field_cells" in payload:
        return payload
    parts = payload.get("parts") if isinstance(payload, dict) else None
    cells = {}
    if isinstance(parts, dict):
        items = (((document or {}).get("planning_constraint_fields") or {}).get("items")
                 or [])
        for index, item in enumerate(items):
            field_id = str((item or {}).get("field_id") or f"field-{index}")
            cells[field_id] = parts.get(f"items.[{index}].cells") or []
    return {"planning_constraint_field_cells": cells}


def artifact_inventory(document, project_path, *, extra_references=()):
    """dry-run GC 盘点：只列 count / bytes / 候选 ID 与路径，绝不删除。"""

    return ArtifactStore(project_path).inventory(
        document, extra_references=extra_references
    )


def artifact_references(document):
    """当前 ProjectState 引用的 canonical artifact 清单（只读、只含相对路径）。

    报告 manifest / 审计记录使用它登记"这份结果引用了哪些 artifact 与指纹"，
    因此生成报告不需要把任何大型明细重新塞回 ProjectState。
    """

    manifest = artifact_manifest_for(document) or {}
    entries = manifest.get("entries") if isinstance(manifest.get("entries"), dict) else {}
    refs = manifest.get("refs") if isinstance(manifest.get("refs"), dict) else {}
    by_id = {value: key for key, value in refs.items()}
    listed = []
    for artifact_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        reference = artifact_ref(entry)
        reference["logical_key"] = by_id.get(artifact_id)
        reference["detail_endpoint"] = scope_endpoint(by_id.get(artifact_id))
        listed.append(reference)
    return sorted(listed, key=lambda item: str(item.get("logical_key")))


def sync_artifact_refs_to_state(state, document):
    """把落盘 manifest 写回内存 state（``artifact_manifest`` + 迁移 legacy 索引）。

    只写 manifest 这一个 additive 顶层键：逐 cell 明细**不**被触碰（内存视图继续
    水合可用），各结果容器也**不**被塞入额外字段 —— artifact 引用统一由 manifest
    的 ``refs`` / ``entries`` 表达，避免污染 ``grid_attributes`` 这类"命名空间 →
    属性"映射的业务键空间。这样"保存后的内存状态"与"重新打开后的内存状态"在
    索引信息上完全一致，旧项目首次保存时同时完成 legacy → 新格式的迁移。
    """

    if not isinstance(state, dict) or not isinstance(document, dict):
        return
    state.pop("result_index", None)
    state[ARTIFACT_MANIFEST_KEY] = deepcopy(artifact_manifest_for(document) or {})


# ---- 内部：模板遍历 ---------------------------------------------------------


def _collect(node, segments, path, out):
    """按模板收集 ``(key, holder, name)``：holder[name] 即被外置的字段。"""

    if not segments:
        return
    head = segments[0]
    rest = segments[1:]
    if head == _LIST:
        if isinstance(node, list):
            for index, item in enumerate(node):
                _collect(item, rest, [*path, f"[{index}]"], out)
        return
    if head == _MAPPING:
        if isinstance(node, dict):
            for key, item in node.items():
                _collect(item, rest, [*path, str(key)], out)
        return
    if head[0] != FIELD or not isinstance(node, dict):
        return
    name = head[1]
    if not rest:
        out.append((".".join([*path, name]), node, name))
        return
    _collect(node.get(name), rest, [*path, name], out)


def _compact_container(scope, container):
    """返回 (已外置的新容器, parts)；没有可外置内容时返回 (None, None)。

    新容器是 **copy-on-write** 的：只有被清空字段所在的层级被重新构造，其余
    子树共享只读引用。因此既不会就地改写调用方的 ``state``，也不会为几十 MB 的
    corridor 明细多做一次整树深拷贝。
    """

    matches = []
    for template in scope["templates"]:
        _collect(container, template, [], matches)
    if not matches:
        return None, None
    parts = {}
    for key, holder, name in matches:
        value = holder.get(name)
        if _has_detail(value):
            parts[key] = value
    if not parts:
        return None, None
    compacted = container
    for template in scope["templates"]:
        compacted, _ = _rewrite(compacted, template, parts, (), scope["clear"])
    compacted = dict(compacted)
    return compacted, parts


def _rewrite(node, segments, parts, prefix, clear=EMPTY):
    """copy-on-write 清空：返回 ``(新节点, 是否改动)``。"""

    if not segments:
        return node, False
    head = segments[0]
    rest = segments[1:]
    if head == _LIST:
        if not isinstance(node, list):
            return node, False
        changed = False
        copied = list(node)
        for index, item in enumerate(node):
            child, child_changed = _rewrite(
                item, rest, parts, (*prefix, f"[{index}]"), clear
            )
            if child_changed:
                copied[index] = child
                changed = True
        return (copied, True) if changed else (node, False)
    if head == _MAPPING:
        if not isinstance(node, dict):
            return node, False
        changed = False
        copied = dict(node)
        for name, item in node.items():
            child, child_changed = _rewrite(
                item, rest, parts, (*prefix, str(name)), clear
            )
            if child_changed:
                copied[name] = child
                changed = True
        return (copied, True) if changed else (node, False)
    if head[0] != FIELD or not isinstance(node, dict):
        return node, False
    name = head[1]
    if not rest:
        if ".".join([*prefix, name]) not in parts:
            return node, False
        copied = dict(node)
        if clear == DROP:
            copied.pop(name, None)
        else:
            copied[name] = [] if isinstance(node.get(name), list) else {}
        return copied, True
    child, child_changed = _rewrite(
        node.get(name), rest, parts, (*prefix, name), clear
    )
    if not child_changed:
        return node, False
    copied = dict(node)
    copied[name] = child
    return copied, True


def _restore_parts(document, scope, payload):
    container = document.get(scope["container_key"])
    if not isinstance(container, dict):
        return
    parts = payload.get("parts") if isinstance(payload, dict) else None
    if not isinstance(parts, dict):
        return
    matches = []
    for template in scope["templates"]:
        _collect(container, template, [], matches)
    for key, holder, name in matches:
        if key in parts:
            holder[name] = parts[key]


def _source_diagnostics(container):
    diagnostics = container.get("detail_diagnostics")
    return deepcopy(diagnostics) if isinstance(diagnostics, dict) else None


def _has_detail(value):
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return False


# ---- 内部：summary / 引用 ----------------------------------------------------


def _summary(scope, container, parts):
    summary = {"logical_key": scope["logical_key"], "detail_available": True}
    lengths = [len(value) for value in parts.values() if hasattr(value, "__len__")]
    summary["detail_count"] = sum(lengths)
    first = next(iter(scope["templates"]))[-1]
    if isinstance(first, tuple) and first[0] == FIELD and len(scope["templates"]) == 1:
        summary["cell_count"] = lengths[0] if lengths else 0
    for key in scope["summary_keys"]:
        if container.get(key) is not None:
            summary[key] = deepcopy(container[key])
    counts = container.get("counts")
    if counts is None:
        counts = container.get("count")
    if counts is not None:
        summary["counts"] = deepcopy(counts)
    return summary


def _scope_descriptor(scope, container):
    descriptor = {"logical_key": scope["logical_key"]}
    for key in ("altitude_layer_id", "nominal_altitude_m", "vertical_reference",
                "route_id", "workspace_id", "field_id", "candidate_id", "status"):
        if container.get(key) is not None:
            descriptor[key] = deepcopy(container[key])
    return descriptor


def _input_fingerprint(container):
    for key in ("input_fingerprint", "constraint_field_fingerprint", "fingerprint",
                "policy_fingerprint"):
        value = container.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _container_ref(container):
    if not isinstance(container, dict):
        return None
    reference = container.get("artifact_ref")
    if is_artifact_ref(reference):
        return reference
    items = container.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and is_artifact_ref(item.get("artifact_ref")):
                return item["artifact_ref"]
    return None


def _entry_ref(manifest, logical_key):
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    refs = manifest.get("refs") if isinstance(manifest, dict) else None
    if not isinstance(entries, dict) or not isinstance(refs, dict):
        return None
    artifact_id = refs.get(logical_key)
    if artifact_id and artifact_id in entries:
        return entries[artifact_id]
    return None


def _mark_unavailable(scope, document, error):
    """明细不可用：只标记 detail 通道，绝不改写业务 status / assessment。"""

    container = document.get(scope["container_key"])
    if not isinstance(container, dict):
        return
    container["detail_status"] = "artifact_unavailable"
    container["detail_error"] = error.as_dict()
    container.pop("artifact_ref", None)


# ---- 旧格式兼容读取 ---------------------------------------------------------


def _restore_legacy_bundle(document, index, project_path):
    """B3X 单 bundle 反向兼容：旧的四个 payload 键按原语义还原。"""

    store = ArtifactStore(project_path)
    try:
        payload = store.read(_legacy_reference(index))
    except ArtifactError as exc:
        document["artifact_diagnostics"] = [
            {"scope": "legacy_result_bundle", **exc.as_dict()}
        ]
        return document
    risk = document.setdefault("grid_risk_v2", {})
    if isinstance(risk, dict):
        risk["cells"] = payload.get("grid_risk_v2_cells") or {}
    candidates = document.setdefault("layered_route_candidates", {})
    if isinstance(candidates, dict):
        candidates["items"] = payload.get("layered_route_candidate_items") or []
        candidates["masks"] = payload.get("layered_route_candidate_masks") or {}
    fields = document.setdefault("planning_constraint_fields", {})
    if isinstance(fields, dict):
        cell_payload = payload.get("planning_constraint_field_cells") or {}
        fields["items"] = [
            {**item, "cells": cell_payload.get(str(item.get("field_id"))) or []}
            for item in fields.get("items") or [] if isinstance(item, dict)
        ]
    document.pop("_population_shelter_cache", None)
    return document


def _legacy_reference(index):
    return {
        "artifact_id": str(index.get("sha256") or ""),
        "artifact_type": "legacy.project_result_bundle",
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "sha256": str(index.get("sha256") or ""),
        "content_encoding": "json.gz",
        "relative_path": str(index.get("artifact") or ""),
        "size_bytes": None,
        "created_at": None,
        "producer": {"service": None, "algorithm_id": None, "algorithm_version": None},
        "input_fingerprint": None,
        "scope": {"logical_key": "legacy_result_bundle"},
        "status": "published",
        "summary": {},
        "legacy": True,
    }


def _legacy_scope_payload(store, index, scope):
    payload = store.read(_legacy_reference(index))
    if scope["logical_key"] == "planning_constraint_fields":
        return payload
    if scope["logical_key"] == "grid_risk_v2":
        return {"parts": {"cells": payload.get("grid_risk_v2_cells") or {}}}
    if scope["logical_key"] == "layered_route_candidates":
        return {"parts": {
            "items": payload.get("layered_route_candidate_items") or [],
            "masks": payload.get("layered_route_candidate_masks") or {},
        }}
    raise ArtifactError(
        f"legacy 结果 bundle 不含该明细：{scope['logical_key']}",
        code="artifact_scope_not_in_legacy_bundle",
    )


__all__ = [
    "ARTIFACT_MANIFEST_KEY", "DETAIL_ENDPOINTS", "EXTERNAL_SCOPES",
    "MANIFEST_SCHEMA_VERSION", "RESULT_DIRECTORY", "RESULT_INDEX_VERSION",
    "artifact_inventory", "artifact_manifest_for", "artifact_references",
    "compact_and_store", "read_result_artifact", "read_scope_artifact",
    "restore_compacted_results", "scope_endpoint", "scope_keys",
    "sync_artifact_refs_to_state",
]
