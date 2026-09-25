"""Phase4-B5X：ProjectState Compaction + Canonical Artifact Migration 定向测试。

覆盖 B5X 的 20 项硬要求（content-addressed 复用 / 原子发布失败 / 完整性 /
旧项目兼容 / compaction 幂等 / 各领域外置 / 快照瘦身 / bbox 按需读取 /
缺失语义 / 报告 manifest / runtime-only 不持久化 / dry-run GC）。
"""

from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.artifact_read_service import ArtifactReadService
from cns_planner.application.planning_constraint_field_service import (
    PlanningConstraintFieldService,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.persistence.artifact_store import (
    ARTIFACT_DIRECTORY, ARTIFACT_TEMP_DIRECTORY, ArtifactCorrupt, ArtifactPublishError,
    ArtifactStore, ArtifactUnavailable,
)
from cns_planner.persistence.project_compaction import (
    ARTIFACT_MANIFEST_KEY, artifact_inventory, artifact_references, compact_and_store,
    read_scope_artifact, restore_compacted_results, sync_artifact_refs_to_state,
)
from cns_planner.persistence.project_repository import ProjectRepository


DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"


def cells(count, *, payload="x" * 40):
    return {f"G{index}": {"grid_id": f"G{index}", "value": payload} for index in range(count)}


def constraint_field(cell_count=4, *, layer="ALT-080"):
    return {
        "schema_version": 1,
        "field_id": f"PCF-{layer}-0123456789abcdef",
        "status": "completed",
        "altitude_layer_id": layer,
        "nominal_altitude_m": 80.0,
        "vertical_reference": "egm2008_orthometric",
        "counts": {"total": cell_count, "pass": cell_count, "blocked": 0, "unknown": 0,
                   "blocked_by": {name: 0 for name in (
                       "terrain", "building", "tower", "airspace", "critical_site")}},
        "warnings": [],
        "constraint_field_fingerprint": "pcf-fixture",
        "cells": [
            {"grid_id": f"G{index}", "outcome": "pass", "blocked_by": [],
             "unknown_reasons": [], "evidence_refs": [],
             "bbox": [float(index), 0.0, float(index) + 1.0, 1.0]}
            for index in range(cell_count)
        ],
    }


def large_state(cell_count=64):
    big = cells(cell_count)
    return {
        "schema_version": 2,
        "project": {"name": "B5X"},
        "grid": {"status": "passed", "level": 8,
                 "cells": [{"grid_id": f"G{i}"} for i in range(cell_count)]},
        "grid_attributes": {
            "population": {"namespace": "population", "status": "passed", "cells": big},
        },
        # Risk V1 明细同样外置；规模固定（不随 fixture 格数增长），因此通用快照里
        # 保留 V1 逐 cell 不会让 B5X 的体积断言失真。
        "grid_risk": {"status": "passed", "input_fingerprint": "risk-fp", "cells": cells(4)},
        "grid_risk_v2": {"status": "passed", "input_fingerprint": "v2-fp", "cells": big},
        "layered_route_candidates": {
            "status": "passed", "count": 1, "active_candidate_id": "C1",
            "items": [{"candidate_id": "C1"}], "masks": {"R1@L1": big},
        },
        "planning_constraint_fields": {
            "schema_version": 1, "status": "passed", "count": 1,
            "items": [constraint_field(cell_count)],
        },
        "coverage_3d": {
            "status": "passed", "input_fingerprint": "cov-fp", "route_count": 1,
            "routes": [{
                "route_id": "R1", "samples": [{"grid_id": "G0"}] * 8,
                "subsystems": [{"subsystem": "P7", "covered_fraction": 0.5,
                                "samples": [{"grid_id": "G0", "covered": True}] * 8,
                                "uncovered_segments": [{"path": [[0, 0], [1, 1]]}]}],
            }],
        },
        "cns_service_capability": {
            "status": "passed", "input_fingerprint": "cap-fp",
            "routes": [{"route_id": "R1", "subsystems": [
                {"subsystem": "P7", "samples": [{"grid_id": "G0", "provider_evaluations": []}] * 8},
            ]}],
        },
        "cns_corridor_assessment": {
            "status": "passed", "input_fingerprint": "cor-fp", "route_count": 1,
            "routes": [{
                "route_id": "R1",
                "corridor_geometry": {"included_grid_ids": [f"G{i}" for i in range(cell_count)]},
                "voxels": [{"voxel_id": f"V{i}", "subsystems": [{"subsystem": "P7"}]}
                           for i in range(24)],
                "subsystems": [{"subsystem": "P7", "deficit_voxel_ids": ["V0", "V1"],
                                "unknown_voxel_ids": []}],
            }],
        },
        "cns_corridor_gap_assessment": {
            "status": "passed", "input_fingerprint": "gap-fp", "route_count": 1,
            "routes": [{"route_id": "R1", "voxels": [{"voxel_id": "V0"}] * 12,
                        "subsystems": [{"subsystem": "P7", "continuous_deficit_segments": [
                            {"voxel_ids": ["V0", "V1"]}]}]}],
        },
        "cns_corridor_site_plan": {
            "status": "passed", "input_fingerprint": "p16-fp", "stop_reason": "converged",
            "target_voxel_count": 3,
            "final_hypothetical_evidence": {"corridor": {"routes": []},
                                            "corridor_gap": {"routes": []}},
            "candidate_impacts": [{"candidate_id": "C1"}],
            "iteration_trace": [{"round": 1}],
        },
        "radar_surveillance_layout": {
            "schema_version": 1, "status": "proposal_ready", "count": 1,
            "items": [{
                "route_id": "R1", "samples": [{"grid_id": "G0"}] * 8,
                "optimisation_samples": [{"grid_id": "G0"}] * 4,
                "validation": {"samples": [{"grid_id": "G0"}] * 8,
                               "coverage_profile": {"entries": [{"segment": 1}] * 4}},
                "selected_panels": [{"panel_id": "P1", "display_geometry": {}}],
                "uncovered_segments": [{"start": 0}], "unknown_segments": [],
                "under_redundant_segments": [], "refinement_rounds": [{"round_index": 0}],
            }],
        },
        "route_planning_experiments": {
            "status": "passed", "count": 1,
            "records": [{"experiment_id": "E1", "result": {"path": [[0, 0], [1, 1]]},
                         "context_basis": {"hard_constraints_snapshot": {}}}],
        },
        "_population_shelter_cache": {"cells": big},
    }


def store_for(path):
    return ArtifactStore(path)


# ---- 1/2. content-addressed 复用与新内容新建 ---------------------------------

def test_same_content_reuses_one_artifact_and_different_content_creates_new(tmp_path):
    project = tmp_path / "project_state.json"
    store = store_for(project)
    payload = {"parts": {"cells": {"G0": {"grid_id": "G0"}}}}

    first = store.publish(payload, artifact_type="risk.grid_risk_v2.cells")
    second = store.publish(payload, artifact_type="risk.grid_risk_v2.cells")
    other = store.publish({"parts": {"cells": {"G0": {"grid_id": "G1"}}}},
                          artifact_type="risk.grid_risk_v2.cells")

    assert first["artifact_id"] == second["artifact_id"] == first["sha256"]
    assert first["created_at"] == second["created_at"], "同一 artifact 的创建时间必须稳定"
    assert other["artifact_id"] != first["artifact_id"]
    files = sorted(path.name for path in (tmp_path / ARTIFACT_DIRECTORY).glob("*.json.gz"))
    assert files == sorted([f"{first['artifact_id']}.json.gz", f"{other['artifact_id']}.json.gz"])
    assert Path(first["relative_path"]).is_absolute() is False
    assert str(tmp_path) not in first["relative_path"], "metadata 绝不携带绝对本机路径"


# ---- 3. atomic publish failure 不替换 active ref ----------------------------

def test_atomic_publish_failure_keeps_active_artifact_and_parks_temp(tmp_path, monkeypatch):
    project = tmp_path / "project_state.json"
    store = store_for(project)
    active = store.publish({"parts": {"cells": {"G0": 1}}}, artifact_type="coverage.coverage_3d.detail")

    def boom(source, target):
        raise PermissionError("simulated publish failure")

    monkeypatch.setattr(ArtifactStore, "_atomic_replace", staticmethod(boom))
    with pytest.raises(ArtifactPublishError) as excinfo:
        store.publish({"parts": {"cells": {"G1": 2}}}, artifact_type="coverage.coverage_3d.detail")
    assert excinfo.value.code == "artifact_publish_failed"

    monkeypatch.undo()
    # active artifact 完全没变，仍可读
    assert store.read(active) == {"parts": {"cells": {"G0": 1}}}
    assert store.exists(active["relative_path"])
    # 失败的临时文件留在可回收区（不静默删除）
    parked = list((tmp_path / ARTIFACT_TEMP_DIRECTORY).glob("*"))
    assert parked, "发布失败必须把临时文件留在可回收区"
    inventory = store.inventory({})
    assert inventory["categories"]["temp"]["count"] == len(parked)
    assert inventory["deletes"] == []


# ---- 4/5. hash mismatch 与 corrupt gzip 必须被检出 --------------------------

def test_hash_mismatch_and_corrupt_gzip_are_detected(tmp_path):
    project = tmp_path / "project_state.json"
    store = store_for(project)
    reference = store.publish({"parts": {"cells": {"G0": 1}}},
                              artifact_type="risk.grid_risk_v2.cells")
    artifact = tmp_path / reference["relative_path"]

    artifact.write_bytes(gzip.compress(b'{"tampered": true}', mtime=0))
    with pytest.raises(ArtifactCorrupt) as tampered:
        store.read(reference)
    assert tampered.value.code == "artifact_sha256_mismatch"

    corrupt = dict(reference)
    corrupt["sha256"] = corrupt["artifact_id"] = "0" * 64
    artifact.write_bytes(b"definitely not gzip")
    with pytest.raises(ArtifactCorrupt) as broken:
        store.read(corrupt)
    assert broken.value.code == "artifact_sha256_mismatch"

    # 内容与 metadata 一致但 gzip 损坏：必须报 corrupt，绝不返回空结果
    artifact.write_bytes(b"definitely not gzip")
    import hashlib

    digest = hashlib.sha256(b"definitely not gzip").hexdigest()
    mismatched = {**reference, "sha256": digest, "artifact_id": digest,
                  "relative_path": f"{ARTIFACT_DIRECTORY}/{digest}.json.gz"}
    Path(tmp_path / mismatched["relative_path"]).write_bytes(b"definitely not gzip")
    with pytest.raises(ArtifactCorrupt) as gz:
        store.read(mismatched)
    assert gz.value.code == "artifact_gzip_corrupt"


def test_unsupported_schema_and_missing_file_raise_unavailable(tmp_path):
    project = tmp_path / "project_state.json"
    store = store_for(project)
    reference = store.publish({"parts": {}}, artifact_type="x", schema_version=99)
    with pytest.raises(ArtifactUnavailable) as unsupported:
        store.read({**reference, "schema_version": 99}, expected_schema_version=1)
    assert unsupported.value.code == "artifact_schema_unsupported"

    with pytest.raises(ArtifactUnavailable) as missing:
        store.read({**reference, "relative_path": f"{ARTIFACT_DIRECTORY}/nope.json.gz"})
    assert missing.value.code == "artifact_missing"


# ---- 6/7. 旧项目可读 + 首次保存才迁移 ---------------------------------------

def test_old_inline_project_reads_verbatim_and_migrates_only_when_saved(tmp_path):
    project = tmp_path / "project_state.json"
    blank = WorkflowService(project, DEFAULTS)
    inline = deepcopy(blank.state)
    inline["project"]["name"] = "legacy-inline"
    inline["grid_risk_v2"] = {"status": "passed", "cells": cells(8)}
    assert ARTIFACT_MANIFEST_KEY not in inline and "result_index" not in inline
    ProjectRepository(project).save(inline)
    before = project.read_bytes()

    restored = restore_compacted_results(ProjectRepository(project).load(), project)
    assert restored == inline, "旧 inline 项目按原样读取，不改写任何字段"
    assert project.read_bytes() == before, "仅为了打开项目绝不修改用户项目文件"
    assert not (tmp_path / ARTIFACT_DIRECTORY).exists()

    service = WorkflowService(project, DEFAULTS)
    assert service.state["grid_risk_v2"]["cells"] == cells(8)
    service.set_project({"name": "legacy-inline-migrated"})
    saved = json.loads(project.read_text(encoding="utf-8"))
    assert "result_index" not in saved
    assert saved[ARTIFACT_MANIFEST_KEY]["refs"]["grid_risk_v2"]
    assert saved["grid_risk_v2"]["cells"] == {}
    assert saved["grid_risk_v2"]["status"] == "passed", "compaction 绝不改变业务 status"
    reopened = WorkflowService(project, DEFAULTS)
    assert reopened.state["grid_risk_v2"]["cells"] == cells(8)
    assert reopened.state["project"]["name"] == "legacy-inline-migrated"


def test_b3x_result_index_bundle_still_reads(tmp_path):
    """B3X 的单个 result bundle 是既有用户的真实项目文件：必须继续可读。"""

    project = tmp_path / "current_project.json"
    payload = {
        "grid_risk_v2_cells": cells(4),
        "layered_route_candidate_items": [{"candidate_id": "C1"}],
        "layered_route_candidate_masks": {"R1@L1": {"cells": cells(4)}},
        "planning_constraint_field_cells": {"PCF-A": [{"grid_id": "G0", "outcome": "pass"}]},
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    import hashlib

    digest = hashlib.sha256(compressed).hexdigest()
    relative = f"{ARTIFACT_DIRECTORY}/{digest}.json.gz"
    artifact = tmp_path / relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(compressed)
    document = {
        "schema_version": 2,
        "project": {"name": "b3x"},
        "grid_risk_v2": {"status": "passed", "cells": {}},
        "layered_route_candidates": {"status": "passed", "items": [], "masks": {}},
        "planning_constraint_fields": {
            "status": "passed", "count": 1,
            "items": [{"field_id": "PCF-A", "altitude_layer_id": "ALT-080", "cells": []}],
        },
        "result_index": {"schema_version": 1, "artifact": relative, "sha256": digest},
    }
    ProjectRepository(project).save(document)

    restored = restore_compacted_results(ProjectRepository(project).load(), project)
    assert restored["grid_risk_v2"]["cells"] == cells(4)
    assert restored["layered_route_candidates"]["masks"]["R1@L1"]["cells"] == cells(4)
    assert restored["planning_constraint_fields"]["items"][0]["cells"] == [
        {"grid_id": "G0", "outcome": "pass"}
    ]

    # 首次保存即迁移成新格式，业务语义不变。
    service = WorkflowService(project, DEFAULTS)
    service.save()
    saved = json.loads(project.read_text(encoding="utf-8"))
    assert "result_index" not in saved
    assert saved[ARTIFACT_MANIFEST_KEY]["migrated_from"]["sha256"] == digest


# ---- 8. compaction 幂等 -----------------------------------------------------

def test_compaction_is_idempotent_for_the_same_state(tmp_path):
    project = tmp_path / "project_state.json"
    state = large_state(16)
    first = compact_and_store(state, project)
    second = compact_and_store(state, project)
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == json.dumps(
        second, sort_keys=True, ensure_ascii=False
    )

    # 已迁移过的 state（带 artifact_ref / manifest）再 compact 一次也幂等
    migrated = deepcopy(state)
    sync_artifact_refs_to_state(migrated, first)
    third = compact_and_store(migrated, project)
    assert third[ARTIFACT_MANIFEST_KEY] == first[ARTIFACT_MANIFEST_KEY]


# ---- 9/10/11/12. 各类大型明细外置 ------------------------------------------

def test_large_details_are_externalized_for_every_canonical_result(tmp_path):
    project = tmp_path / "project_state.json"
    state = large_state(16)
    document = compact_and_store(state, project)

    assert document["grid"]["cells"] == []
    assert document["grid_attributes"]["population"]["cells"] == {}
    assert document["grid_risk"]["cells"] == {}
    assert document["grid_risk_v2"]["cells"] == {}
    assert document["layered_route_candidates"]["items"] == []
    assert document["layered_route_candidates"]["masks"] == {}
    assert document["planning_constraint_fields"]["items"][0].get("cells") is None
    assert document["coverage_3d"]["routes"][0]["samples"] == []
    assert document["coverage_3d"]["routes"][0]["subsystems"][0]["samples"] == []
    assert document["coverage_3d"]["routes"][0]["subsystems"][0]["uncovered_segments"][0]["path"] == []
    assert document["cns_service_capability"]["routes"][0]["subsystems"][0]["samples"] == []
    assert document["cns_corridor_assessment"]["routes"][0]["voxels"] == []
    assert document["cns_corridor_assessment"]["routes"][0]["corridor_geometry"]["included_grid_ids"] == []
    assert document["cns_corridor_assessment"]["routes"][0]["subsystems"][0]["deficit_voxel_ids"] == []
    assert document["cns_corridor_gap_assessment"]["routes"][0]["voxels"] == []
    assert document["cns_corridor_site_plan"]["final_hypothetical_evidence"] == {}
    assert document["cns_corridor_site_plan"]["candidate_impacts"] == []
    assert document["radar_surveillance_layout"]["items"][0]["samples"] == []
    assert document["radar_surveillance_layout"]["items"][0]["selected_panels"] == []
    assert document["radar_surveillance_layout"]["items"][0]["validation"]["samples"] == []
    assert document["route_planning_experiments"]["records"][0]["result"] == {}

    # 业务 summary / 指纹 / 计数仍然是权威事实（未被 compaction 改写）
    assert document["grid_risk_v2"]["status"] == "passed"
    assert document["grid_risk_v2"]["input_fingerprint"] == "v2-fp"
    assert document["coverage_3d"]["routes"][0]["subsystems"][0]["covered_fraction"] == 0.5
    assert document["cns_corridor_site_plan"]["stop_reason"] == "converged"
    assert document["radar_surveillance_layout"]["status"] == "proposal_ready"
    assert document["artifact_manifest"]["refs"].keys() >= {
        "grid", "grid_attributes", "grid_risk", "grid_risk_v2",
        "layered_route_candidates", "planning_constraint_fields", "coverage_3d",
        "cns_service_capability", "cns_corridor_assessment",
        "cns_corridor_gap_assessment", "cns_corridor_site_plan",
        "radar_surveillance_layout", "route_planning_experiments",
    }

    # 内存 state 未被就地改写（copy-on-write）
    assert state["grid_risk_v2"]["cells"] == cells(16)
    assert state["coverage_3d"]["routes"][0]["samples"]
    assert state["cns_corridor_assessment"]["routes"][0]["voxels"]

    ProjectRepository(project).save(document)
    restored = restore_compacted_results(ProjectRepository(project).load(), project)
    assert restored["coverage_3d"]["routes"][0]["samples"] == [{"grid_id": "G0"}] * 8
    assert len(restored["cns_corridor_assessment"]["routes"][0]["voxels"]) == 24
    assert restored["planning_constraint_fields"]["items"][0]["cells"][0]["grid_id"] == "G0"
    assert len(restored["radar_surveillance_layout"]["items"][0]["validation"]["samples"]) == 8
    assert restored["route_planning_experiments"]["records"][0]["result"]["path"]


# ---- 13/14. workflow snapshot 瘦身 -----------------------------------------

def _workflow_with_large_details(tmp_path, cell_count):
    service = WorkflowService(tmp_path / f"project-{cell_count}.json", DEFAULTS)
    service.state.update(large_state(cell_count))
    return service


def test_workflow_snapshot_carries_no_large_detail(tmp_path):
    service = _workflow_with_large_details(tmp_path, 32)
    snapshot = service.snapshot()

    assert "cells" not in snapshot["grid"]
    assert snapshot["grid"]["cell_count"] == 32
    assert snapshot["grid"]["detail_available"] is True
    assert snapshot["grid"]["detail_endpoint"] == "/api/workspace/grid"
    assert snapshot["planning_constraint_fields"]["items"][0].get("cells") is None
    assert snapshot["layered_route_candidates"]["masks_detail"] == "artifact"
    assert snapshot["layered_route_candidates"]["masks_count"] == 1
    assert snapshot["layered_route_candidates"]["active_candidate_id"] == "C1"
    assert snapshot["layered_route_candidates"]["items"][0]["candidate_id"] == "C1", (
        "候选记录是有界权威记录，保留在通用快照里"
    )
    assert snapshot["grid_risk_v2"]["cells_detail"] == "artifact"
    assert snapshot["coverage_3d"]["routes"][0]["samples_count"] == 8
    assert snapshot["coverage_3d"]["detail_available"] is True
    assert snapshot["cns_service_capability"]["routes"][0]["subsystems"][0]["samples_count"] == 8
    assert snapshot["cns_corridor_assessment"]["routes"][0]["voxels_count"] == 24
    assert snapshot["cns_corridor_assessment"]["routes"][0]["corridor_geometry"]["included_grid_ids_count"] == 32
    assert snapshot["cns_corridor_gap_assessment"]["routes"][0]["voxels_count"] == 12
    assert snapshot["cns_corridor_site_plan"]["final_hypothetical_evidence_detail"] == "artifact"
    assert snapshot["radar_surveillance_layout"].get("items", [{}])[0].get("samples") is None
    assert snapshot["route_planning_experiments"]["records"][0]["result_detail"] == "artifact"

    text = json.dumps(snapshot, ensure_ascii=False)
    for forbidden in ('"selected_panels": [{"panel_id"', '"corridor_gap": {"routes"'):
        assert forbidden not in text


def test_snapshot_size_does_not_grow_linearly_with_cell_count(tmp_path):
    small = json.dumps(_workflow_with_large_details(tmp_path, 200).snapshot(),
                       ensure_ascii=False).encode("utf-8")
    large = json.dumps(_workflow_with_large_details(tmp_path, 4000).snapshot(),
                       ensure_ascii=False).encode("utf-8")
    assert len(large) - len(small) < 20_000, (
        "workflow 快照不得随 artifact cell 数量线性膨胀："
        f"small={len(small)} large={len(large)}"
    )
    assert len(large) < len(small) * 1.25


# ---- 15. bbox lazy constraint read -----------------------------------------

def test_bbox_lazy_constraint_read_uses_artifact_without_hydration(tmp_path):
    project = tmp_path / "project_state.json"
    state = large_state(8)
    document = compact_and_store(state, project)
    ProjectRepository(project).save(document)

    class Session:
        def __init__(self, value, path):
            self.state = value
            self.store_path = path

    # state 中只剩 summary / artifact_ref：逐 cell 明细必须从 artifact 按需读取。
    persisted = ProjectRepository(project).load()
    service = PlanningConstraintFieldService(Session(persisted, project), None, None)
    result = service.field_map("ALT-080", "0,0,3,3")
    assert result["status"] == "passed"
    assert [item["grid_id"] for item in result["cells"]] == ["G0", "G1", "G2"]
    assert result["total_count"] == 8, "total_count 报告完整规模，不因视口过滤变小"
    assert result["bbox"] == [0.0, 0.0, 3.0, 3.0]
    assert all(set(item) == {"grid_id", "outcome", "blocked_by"} for item in result["cells"])
    assert persisted["planning_constraint_fields"]["items"][0].get("cells") is None


# ---- 16. missing artifact ≠ empty / pass ------------------------------------

def test_missing_artifact_is_never_reported_as_empty_or_pass(tmp_path):
    source = tmp_path / "source" / "project_state.json"
    document = compact_and_store(large_state(4), source)
    # 只把文档复制到新项目目录，**不**复制 artifact：模拟"明细文件缺失"。
    project = tmp_path / "copy" / "project_state.json"
    ProjectRepository(project).save(document)
    assert not (project.parent / ARTIFACT_DIRECTORY).exists()

    read = ArtifactReadService(type("S", (), {
        "state": ProjectRepository(project).load(), "store_path": project,
    })())
    with pytest.raises(ArtifactUnavailable) as missing:
        read.content("planning_constraint_fields")
    assert missing.value.code == "artifact_missing"

    with pytest.raises(ArtifactUnavailable):
        read_scope_artifact(read.session.state, project, "coverage_3d")

    response = ApiRouter._artifact_response(
        lambda: read.content("planning_constraint_fields")
    )
    assert response.status == 409
    assert response.data["code"] == "artifact_missing"
    assert "重新计算" in response.data["error"], "用户可见的必须是中文业务提示"
    assert "artifact_missing" not in response.data["error"], "技术码不得出现在用户可见文本里"

    restored = restore_compacted_results(ProjectRepository(project).load(), project)
    diagnostics = restored["artifact_diagnostics"]
    assert diagnostics and all(item["code"] == "artifact_missing" for item in diagnostics)
    assert restored["coverage_3d"]["detail_status"] == "artifact_unavailable"
    assert restored["coverage_3d"]["status"] == "passed", "IO 失败绝不被误写成领域 assessment failed"


# ---- 17. report manifest 保留 artifact refs ---------------------------------

def test_report_manifest_records_artifact_references(tmp_path):
    service = _workflow_with_large_details(tmp_path, 16)
    service.save()
    references = artifact_references(service.state)
    assert references, "保存后必须能枚举 canonical artifact 引用"
    for reference in references:
        assert reference["artifact_id"] == reference["sha256"]
        assert reference["relative_path"].startswith(f"{ARTIFACT_DIRECTORY}/")
        assert reference["content_encoding"] == "json.gz"
        assert reference["schema_version"] == 1
        assert reference["producer"] is not None
        assert not Path(reference["relative_path"]).is_absolute()

    preview = service.report_service.preview()
    assert preview["source_artifacts"], "报告预览必须登记引用的 artifact"
    assert {item["logical_key"] for item in preview["source_artifacts"]} == {
        item["logical_key"] for item in references
    }


# ---- 18. runtime-only compatibility 结果不进入 artifact store ---------------

def test_runtime_only_compatibility_results_are_not_persisted_or_stored(tmp_path):
    project = tmp_path / "project_state.json"
    service = WorkflowService(project, DEFAULTS)
    service.set_workspace([120.001, 30.001, 120.01, 30.01], {
        "status": "passed", "layers": [], "loaded_layer_count": 0, "covered_layer_count": 0,
    })
    service.state["compatibility"] = {
        "mode": "legacy-runtime-only",
        "results": {"legacy_routes": [{"route_id": "L1", "path": [[0, 0]] * 4}]},
    }
    service.save()

    saved = json.loads(project.read_text(encoding="utf-8"))
    compatibility = saved.get("compatibility") or {}
    assert "results" not in compatibility, "runtime-only 兼容结果绝不持久化"
    inventory = artifact_inventory(saved, project)
    stored = {
        entry["artifact_id"]
        for name in ("reachable", "permanent", "stale_referenced", "unreachable")
        for entry in inventory["categories"][name]["artifacts"]
    }
    assert "L1" not in json.dumps(sorted(stored)), "兼容试算结果绝不进 artifact store"


# ---- 19/20. dry-run GC 只列不删；confirmed / report artifact 不被误列 --------

def test_dry_run_gc_lists_but_never_deletes_and_protects_referenced_artifacts(tmp_path):
    project = tmp_path / "project_state.json"
    state = large_state(4)
    document = compact_and_store(state, project)
    store = store_for(project)

    orphan = store.publish({"parts": {}}, artifact_type="orphan")
    assert store.exists(orphan["relative_path"])
    temp = tmp_path / ARTIFACT_TEMP_DIRECTORY / "leftover.tmp"
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_bytes(b"leftover")

    live_id = document[ARTIFACT_MANIFEST_KEY]["refs"]["grid_risk_v2"]
    # 一个"不再被 manifest refs 引用，但被已确认方案引用"的 artifact
    confirmed_only = store.publish({"parts": {"cells": {"G0": 1}}},
                                   artifact_type="planning_constraint_field.cells")
    document["confirmed_cns_plan"] = {
        "status": "confirmed",
        "artifact_ref": {k: confirmed_only[k] for k in (
            "artifact_id", "artifact_type", "sha256", "content_encoding",
            "relative_path", "schema_version",
        )},
    }
    ProjectRepository(project).save(document)
    before = {path.name: path.stat().st_size
              for path in (tmp_path / ARTIFACT_DIRECTORY).glob("*.json.gz")}

    inventory = artifact_inventory(document, project)
    assert inventory["dry_run"] is True
    assert inventory["deletes"] == []
    categories = inventory["categories"]
    assert [item["artifact_id"] for item in categories["unreachable"]["artifacts"]] == [
        orphan["artifact_id"]
    ]
    assert {item["artifact_id"] for item in categories["permanent"]["artifacts"]} == {
        confirmed_only["artifact_id"]
    }
    assert live_id in {item["artifact_id"] for item in categories["reachable"]["artifacts"]}
    assert categories["temp"]["count"] == 1
    assert categories["temp"]["bytes"] == len(b"leftover")
    assert inventory["totals"]["artifact_count"] >= 3
    assert confirmed_only["artifact_id"] not in {
        item["artifact_id"] for item in categories["unreachable"]["artifacts"]
    }
    # 没有任何文件被删除，临时文件也仍在
    after = {path.name: path.stat().st_size
             for path in (tmp_path / ARTIFACT_DIRECTORY).glob("*.json.gz")}
    assert before == after
    assert temp.is_file()
    assert store.exists(orphan["relative_path"])


def test_stale_referenced_artifact_is_reported_but_kept(tmp_path):
    project = tmp_path / "project_state.json"
    store = store_for(project)
    stale = store.publish({"parts": {"cells": {"G0": 1}}}, artifact_type="x")
    document = {
        ARTIFACT_MANIFEST_KEY: {"schema_version": 1, "entries": {}, "refs": {}},
        "coverage_3d": {"status": "stale", "artifact_ref": {
            "artifact_id": stale["artifact_id"], "sha256": stale["sha256"],
            "relative_path": stale["relative_path"], "schema_version": 1,
        }},
    }
    inventory = artifact_inventory(document, project)
    assert [item["artifact_id"] for item in inventory["categories"]["stale_referenced"]["artifacts"]] == [
        stale["artifact_id"]
    ]
    assert inventory["categories"]["unreachable"]["count"] == 0
    assert store.exists(stale["relative_path"])


# ---- 只读性 / 状态不漂移 ----------------------------------------------------

def test_artifact_read_paths_never_mutate_project_state(tmp_path):
    service = _workflow_with_large_details(tmp_path, 8)
    service.save()
    before = deepcopy(service.state)
    service.artifact_summaries()
    service.artifact_manifest()
    service.artifact_inventory()
    service.artifact_content({"logical_key": "coverage_3d", "limit": 2})
    assert service.state == before, "B5X 的 artifact 读取路径必须完全只读"
