"""Round35-D 定向回归：陆域来源身份 / 指纹 / audit 登记 / 缓存签名的稳定性。

覆盖任务书 D1 的六条必测项：

1. 相同来源、策略和网格，多次计算指纹一致；
2. ``prepare()`` 前 / 后读取，指纹一致；
3. 审计登记完善前后，指纹一致；
4. 仅修改无关审计字段，指纹不变；
5. 真正修改掩膜内容 / 图层 / 30→100 m 参数，指纹改变；
6. 来源文件被替换后不会继续使用旧缓存。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.gis.land_mask_source import (
    STABLE_IDENTITY_FIELDS, land_mask_file_signature, land_mask_source_audit_facts,
    land_mask_source_identity,
)
from cns_planner.gis.radar_layout_adapter import LandMaskSource
from cns_planner.application.source_audit_service import SourceAuditService
from cns_planner.domain.source_audit import source_manifest

DEFAULTS = Path("cns_planner/config/defaults.json")


def _land_geojson(tmp_path, *, name="land.geojson", offset=(122.0, 30.0), size=0.002,
                  properties=None):
    west, south = offset
    east, north = west + size, south + size
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": dict(properties or {"name": "land"}),
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [west, south], [east, south], [east, north], [west, north],
                    [west, south],
                ]],
            },
        }],
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _grid(mapping):
    return {
        "status": "passed", "crs": "OGC:CRS84", "level": 8,
        "cells": [
            {"grid_id": grid_id, "bbox": list(bbox)} for grid_id, bbox in mapping.items()
        ],
    }


def _facts_for(tmp_path, land, *, name, buffer_m=30.0, audit=None, prepare_first=False):
    workflow = WorkflowService(tmp_path / name / "project.json", DEFAULTS)
    workflow.state["grid"] = _grid({"G-IN": (122.0, 30.0, 122.002, 30.002)})
    workflow.surface_classification_land_mask_path = lambda: str(land)
    if audit is not None:
        workflow.state["source_audits"] = {"status": "passed", "items": {"land_mask": audit}}
    workflow.set_surface_classification_policy(
        {"coastal_uncertainty_buffer_m": buffer_m, "confirmed": True}
    )
    if prepare_first:
        # 主动先 prepare 一次：模拟"describe() 晚于 prepare()"的调用时序。
        LandMaskSource(
            land, coastal_uncertainty_buffer_m=buffer_m,
        ).prepare()
    return workflow.update_surface_class_facts()


# --------------------------------------------------------------- identity（纯函数）


def test_identity_fields_are_exactly_the_stable_whitelist(tmp_path):
    land = _land_geojson(tmp_path)
    source = LandMaskSource(land)
    describe = source.describe()
    identity = land_mask_source_identity(describe, content_sha256="a" * 64)
    assert set(identity) == set(STABLE_IDENTITY_FIELDS)
    # 审计字段绝不进入身份载荷。
    for forbidden in ("layer_resolution", "source_crs", "polygon_count", "size_bytes",
                      "configured_path", "mtime_ns"):
        assert forbidden not in identity


def test_identity_is_identical_before_and_after_prepare(tmp_path):
    land = _land_geojson(tmp_path)
    source = LandMaskSource(land)
    before = source.describe()
    identity_before = land_mask_source_identity(before, content_sha256="b" * 64)

    source.prepare()
    after = source.describe()
    identity_after = land_mask_source_identity(after, content_sha256="b" * 64)

    assert identity_before == identity_after
    # 但审计事实确实反映了 prepare 的时序差异（这正是它们不能进身份的原因）。
    audit_before = land_mask_source_audit_facts(before, source_path=land)
    audit_after = land_mask_source_audit_facts(after, source_path=land)
    assert audit_before["layer_resolution"] != audit_after["layer_resolution"]
    assert audit_before["polygon_count"] is None
    assert audit_after["polygon_count"] == 1
    assert audit_after["source_crs"]
    assert audit_after["purpose"] == "audit_only_not_in_input_fingerprint"


def test_identity_ignores_audit_only_and_registration_completeness(tmp_path):
    land = _land_geojson(tmp_path)
    source = LandMaskSource(land)
    source.prepare()
    describe = source.describe()

    bare = land_mask_source_identity(describe, content_sha256="c" * 64)
    enriched = land_mask_source_identity(
        describe,
        audit_manifest={
            "role": "land_mask", "size_bytes": 2605056,
            "feature_count": 1368, "extent": [121.0, 29.0, 123.1616516, 31.5],
            "declared_crs": "EPSG:4326",
        },
        content_sha256="c" * 64,
    )
    assert bare == enriched, "充实审计登记不得改变来源身份"


def test_identity_changes_only_with_real_identity_inputs(tmp_path):
    land = _land_geojson(tmp_path)
    base = land_mask_source_identity(
        {"source_role": "land_mask", "source_type": "real", "layer_name": "land"},
        content_sha256="d" * 64,
    )
    assert base["identity_basis"] == "content_sha256"
    # 内容变化 ⇒ 变。
    assert land_mask_source_identity(
        {"source_role": "land_mask", "source_type": "real", "layer_name": "land"},
        content_sha256="e" * 64,
    ) != base
    # 图层身份变化 ⇒ 变。
    assert land_mask_source_identity(
        {"source_role": "land_mask", "source_type": "real", "layer_name": "other"},
        content_sha256="d" * 64,
    ) != base
    # 来源角色变化 ⇒ 变。
    assert land_mask_source_identity(
        {"source_role": "coastline", "source_type": "real", "layer_name": "land"},
        content_sha256="d" * 64,
    ) != base
    # 拿不到内容 hash ⇒ 显式降级标记（绝不假装是内容身份）。
    degraded = land_mask_source_identity({"layer_name": "land"})
    assert degraded["identity_basis"] == "declared_source_facts"
    assert degraded["content_sha256"] is None


# --------------------------------------------------------------- 文件签名（缓存）


def test_file_signature_detects_same_path_content_replacement(tmp_path):
    land = _land_geojson(tmp_path, name="mask.geojson")
    first = land_mask_file_signature(land)
    assert first is not None and first[0] > 0
    # 同一路径写入**不同内容**（长度不同 ⇒ size 必变，不依赖文件系统时间精度）。
    land.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {"name": "land", "edition": "rev-2"},
            "geometry": {"type": "Polygon", "coordinates": [[
                [122.0, 30.0], [122.003, 30.0], [122.003, 30.003], [122.0, 30.003],
                [122.0, 30.0]]]},
        }],
    }), encoding="utf-8")
    second = land_mask_file_signature(land)
    assert second != first, "同路径替换内容后签名必须变化，缓存才能失效"
    assert land_mask_file_signature(tmp_path / "missing.geojson") is None
    assert land_mask_file_signature(None) is None


# --------------------------------------------------------------- facts 指纹（生产链）


def test_surface_facts_fingerprint_is_stable_and_tracks_only_real_inputs(tmp_path):
    land = _land_geojson(tmp_path)
    first = _facts_for(tmp_path, land, name="a")
    second = _facts_for(tmp_path, land, name="b")
    assert first["input_fingerprint"] == second["input_fingerprint"], "相同输入必须同指纹"

    # prepare 时序不同（外部先 prepare 过）⇒ 指纹不变。
    late = _facts_for(tmp_path, land, name="c", prepare_first=True)
    assert late["input_fingerprint"] == first["input_fingerprint"], "describe 时序不得影响指纹"

    # 审计登记充实（含几何事实与 size）⇒ 指纹不变。
    audited = _facts_for(
        tmp_path, land, name="d",
        audit={
            "role": "land_mask", "size_bytes": 2605056, "feature_count": 1368,
            "extent": [121.0, 29.0, 123.1616516, 31.5], "declared_crs": "EPSG:4326",
        },
    )
    assert audited["input_fingerprint"] == first["input_fingerprint"]

    # 30 → 100 m 策略参数 ⇒ 指纹改变（真正的分类参数）。
    buffer_changed = _facts_for(tmp_path, land, name="e", buffer_m=100.0)
    assert buffer_changed["input_fingerprint"] != first["input_fingerprint"]

    # 掩膜内容真正变化（几何范围更大）⇒ 指纹改变。
    wider = _land_geojson(tmp_path, name="wider.geojson", size=0.004)
    content_changed = _facts_for(tmp_path, wider, name="f")
    assert content_changed["input_fingerprint"] != first["input_fingerprint"]

    # 已 prepare 的 describe 事实现在能落到 facts（polygon_count 纳入键集合）。
    assert first["land_mask"]["polygon_count"] == 1
    assert first["land_mask"]["source_audit_facts"]["polygon_count"] == 1
    assert first["land_mask"]["source_audit_facts"]["purpose"].startswith("audit_only")


# --------------------------------------------------------------- audit 登记补齐


def test_verify_path_probes_geometry_facts_and_never_inherits_stale_ones(tmp_path):
    """刷新登记时必须从**当前文件**补齐几何事实，且绝不继承旧来源的失效事实。"""

    land = _land_geojson(tmp_path, name="mask.geojson")
    service = SourceAuditService(session=None, invalidation=None, snapshot=None)
    merged = service._with_geometry_facts({}, land, probe_geometry=True)
    assert merged["feature_count"] == 1
    assert merged["declared_crs"]
    assert merged["extent"] and len(merged["extent"]) == 4
    assert merged["geometry_probe"]["read_only"] is True
    assert merged["geometry_probe"]["semantics"].startswith("probed_from_current_file")

    # 调用方显式提供的值优先，不被探测覆盖。
    explicit = service._with_geometry_facts(
        {"feature_count": 999, "declared_crs": "EPSG:4326"}, land, probe_geometry=True,
    )
    assert explicit["feature_count"] == 999

    # 旧来源的事实绝不继承：签名变化时 source_manifest 丢弃 previous 的派生事实……
    previous = {
        "size_bytes": 1, "mtime_ns": 1, "feature_count": 618,
        "extent": [121.8, 29.6, 122.7, 30.5], "declared_crs": "EPSG:4326",
        "verification": {"sha256": "0" * 64, "size_bytes": 1, "mtime_ns": 1},
    }
    manifest = source_manifest("land_mask", land, previous=previous)
    assert manifest["feature_count"] is None
    assert manifest["extent"] is None
    assert "source_signature_changed_previous_details_dropped" in manifest["reasons"]

    # ……并由**当前文件**的探测结果补齐（因此不再是旧来源的值）。
    refreshed = source_manifest(
        "land_mask", land, previous=previous,
        details=service._with_geometry_facts({}, land),
    )
    assert refreshed["feature_count"] == 1
    assert refreshed["extent"] != [121.8, 29.6, 122.7, 30.5]


def test_geometry_probe_failure_never_blocks_verification(tmp_path):
    service = SourceAuditService(session=None, invalidation=None, snapshot=None)
    merged = service._with_geometry_facts({}, tmp_path / "missing.gpkg", probe_geometry=True)
    assert merged["geometry_probe"]["status"] == "not_probed"
    assert merged.get("feature_count") is None
    # probe_geometry=False 时完全不做探测（保持既有行为）。
    assert "geometry_probe" not in service._with_geometry_facts(
        {}, tmp_path / "missing.gpkg", probe_geometry=False,
    )


def test_land_mask_source_identity_is_deterministic_across_calls(tmp_path):
    """同一份来源 + 同一策略 + 同一网格，多次计算的指纹必须逐位一致。"""

    land = _land_geojson(tmp_path)
    fingerprints = {
        _facts_for(tmp_path, land, name=f"run{index}")["input_fingerprint"]
        for index in range(3)
    }
    assert len(fingerprints) == 1


def test_stable_identity_fields_constant_is_used_by_payload():
    identity = land_mask_source_identity(None)
    assert set(identity) == set(STABLE_IDENTITY_FIELDS)
    with pytest.raises(TypeError):
        # 位置参数之外的 audit 事实必须显式命名，避免再次混入时序敏感字段。
        land_mask_source_identity(None, {}, "x")
