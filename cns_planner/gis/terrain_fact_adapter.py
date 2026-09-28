"""Terrain cell fact 的**唯一** canonical 化入口（字段契约收口）。

为什么必须有这一层
------------------
真实项目里 terrain 有两套并存的字段契约，直接导致 Planning Constraint Field 全域
``unknown``：

* ``grid_attributes.terrain.cells[*]``（``data/mapping/terrain.py`` 产出）写的是
  canonical ``surface_elevation_max_m`` / ``surface_elevation_mean_m`` /
  ``surface_elevation_min_m``；
* coarse feasibility / PCF 却只读 ``surface_elevation_max_egm2008_m``。

字段名不同**不等于**垂向基准不同，但也**绝不等于**相同：只有 terrain **自身的**
dataset metadata 明确声明了 EGM2008 正高时，才允许把 canonical
``surface_elevation_max_m`` 投影为 ``surface_elevation_max_egm2008_m``。垂向基准
不明确时保持 ``unknown``。

硬边界
------
* 绝不用 AltitudeLayer 的 ``vertical_reference`` 反向兜底 terrain fact（那是巡航
  高度层的基准，不是地形数据的基准）；
* 绝不把 ``msl`` / ``ellipsoidal`` / 空值当成 EGM2008；
* 绝不把"读不到垂向声明"当成"垂向基准已确认"；
* 本模块只做契约规范化，不做任何空间运算、不读文件、不写状态。
"""

from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real

#: 内部 canonical 垂向基准（与 AltitudeLayer / TowerObstacleProfile 同一常量语义）。
EGM2008_ORTHOMETRIC = "egm2008_orthometric"

#: terrain dataset metadata 中被接受为"已明确声明 EGM2008 正高"的等价写法。
#: 只收录明确等价的形式；``msl`` / ``ellipsoidal`` / ``wgs84`` / 空值一律不在其中。
_EGM2008_TOKENS = frozenset({
    "egm2008_orthometric",
    "egm2008_orthometric_height",
    "egm2008 orthometric",
    "egm2008 orthometric height",
    "egm2008",
    "epsg:3855",
    "epsg::3855",
})

#: 可被当作"已解析"的 cell 状态。
_RESOLVED_STATUSES = ("passed", "resolved")

#: 允许出现 canonical 高程数值的字段（按优先级）。
_CANONICAL_MAX_FIELDS = (
    "surface_elevation_max_m",
    "max_elevation",
)


def _finite(value):
    return (
        isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))
    )


def _number(value):
    return float(value) if _finite(value) else None


def canonical_vertical_reference(value):
    """把一个 dataset metadata 取值规范成 canonical 垂向基准；不认识则 ``None``。

    这是**唯一**的规范化点：大小写、前后空白与连字符差异不代表垂向基准不同，
    但任何未收录的取值都返回 ``None``（绝不放宽为"任意非空字符串即可信"）。
    """

    text = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    text = " ".join(text.split())
    if not text:
        return None
    if text in _EGM2008_TOKENS:
        return EGM2008_ORTHOMETRIC
    # ``EPSG:3855`` 归一化后仍是 ``epsg:3855``（冒号不在替换集合里）。
    if text.replace(" ", "") in {token.replace(" ", "") for token in _EGM2008_TOKENS}:
        return EGM2008_ORTHOMETRIC
    return None


def terrain_dataset_metadata(attribute):
    """从 **terrain attribute 自身** 的 dataset metadata 解析垂向基准声明。

    ``attribute`` 是 ``state["grid_attributes"]["terrain"]``（或任何等价的 terrain
    dataset 记录）。读取顺序：

    1. 顶层显式 ``vertical_reference``（``data/mapping/terrain.py`` 新写入的产出端字段）；
    2. ``source_profile.crs.vertical`` / ``crs.vertical_name``（既有产品的产品契约声明）；
    3. ``source.vertical_reference``（栅格实测元数据，若有）。

    返回 ``{"vertical_reference", "vertical_reference_source",
    "vertical_reference_evidence", "surface_model", "dataset_status"}``；无法确认时
    ``vertical_reference`` 为 ``None``——调用方必须据此保持 ``unknown``。
    """

    raw = attribute if isinstance(attribute, dict) else {}
    evidence = []
    reference = None
    source = None

    explicit = raw.get("vertical_reference")
    if explicit not in (None, ""):
        reference = canonical_vertical_reference(explicit)
        source = "terrain_attribute_vertical_reference"
        evidence.append({
            "field": "vertical_reference",
            "observed": str(explicit),
            "accepted": reference is not None,
        })

    if reference is None:
        profile = raw.get("source_profile") if isinstance(raw.get("source_profile"), dict) else {}
        crs = profile.get("crs") if isinstance(profile.get("crs"), dict) else {}
        for field in ("vertical", "vertical_name"):
            observed = crs.get(field)
            if observed in (None, ""):
                continue
            candidate = canonical_vertical_reference(observed)
            evidence.append({
                "field": f"source_profile.crs.{field}",
                "observed": str(observed),
                "accepted": candidate is not None,
            })
            if candidate is not None:
                reference = candidate
                source = f"terrain_source_profile_crs_{field}"
                break

    if reference is None:
        raster = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        observed = raster.get("vertical_reference")
        if observed not in (None, ""):
            candidate = canonical_vertical_reference(observed)
            evidence.append({
                "field": "source.vertical_reference",
                "observed": str(observed),
                "accepted": candidate is not None,
            })
            if candidate is not None:
                reference = candidate
                source = "terrain_raster_metadata_vertical_reference"

    profile = raw.get("source_profile") if isinstance(raw.get("source_profile"), dict) else {}
    verification = profile.get("verification") if isinstance(profile.get("verification"), dict) else {}
    return {
        "vertical_reference": reference,
        "vertical_reference_source": source,
        "vertical_reference_evidence": {
            "scope": "terrain_dataset_metadata",
            "declarations": evidence,
            "verification_status": verification.get("status"),
            "file_identity": verification.get("file_identity"),
        },
        "surface_model": str(profile.get("provenance", {}).get("surface_model")
                            or raw.get("surface_model") or "") or None,
        "dataset_status": str(raw.get("status") or raw.get("quantity_status") or ""),
    }


def _cell_declared_reference(fact):
    """cell 级垂向基准声明（优先于 dataset 级）。"""

    for field in ("vertical_reference", "surface_elevation_vertical_reference"):
        observed = fact.get(field)
        if observed in (None, ""):
            continue
        candidate = canonical_vertical_reference(observed)
        if candidate is not None:
            return candidate, f"terrain_cell_{field}", {
                "field": field, "observed": str(observed), "scope": "terrain_cell",
            }
    return None, None, None


def canonical_terrain_fact(raw, *, dataset_metadata=None):
    """把一个 terrain cell fact 规范成 canonical EGM2008 事实。

    返回的 ``surface_elevation_max_egm2008_m`` **只在**下列两种情况下有值：

    * cell 自己带 canonical ``surface_elevation_max_egm2008_m``（字段名即契约）；
    * cell 带 canonical ``surface_elevation_max_m``，且**垂向基准已由 terrain 自身
      证据确认为 EGM2008**（cell 级声明优先，其次 dataset metadata）。

    其余情况一律 ``data_status = "unknown"`` 且高程为 ``None``，并给出可审计的
    ``reason``；调用方**不得**用其它来源（例如巡航高度层的基准）补齐。
    """

    fact = raw if isinstance(raw, dict) else {}
    meta = dataset_metadata if isinstance(dataset_metadata, dict) else {}
    status = str(fact.get("data_status") or fact.get("status") or "")

    declared = _number(fact.get("surface_elevation_max_egm2008_m"))
    canonical = None
    for field in _CANONICAL_MAX_FIELDS:
        canonical = _number(fact.get(field))
        if canonical is not None:
            break

    if declared is not None:
        reference = EGM2008_ORTHOMETRIC
        reference_source = "egm2008_canonical_field_name"
        reference_evidence = {
            "scope": "terrain_cell_canonical_field",
            "field": "surface_elevation_max_egm2008_m",
        }
        elevation, projection = declared, "source_declared_canonical_field"
    else:
        cell_reference, cell_source, cell_evidence = _cell_declared_reference(fact)
        if cell_reference is not None:
            reference, reference_source, reference_evidence = (
                cell_reference, cell_source, cell_evidence,
            )
        else:
            reference = meta.get("vertical_reference")
            reference_source = meta.get("vertical_reference_source")
            reference_evidence = deepcopy(meta.get("vertical_reference_evidence"))
        if canonical is not None and reference == EGM2008_ORTHOMETRIC:
            elevation, projection = (
                canonical, "terrain_canonical_surface_elevation_max_m_projected_to_egm2008",
            )
        else:
            elevation, projection = None, None

    reason = None
    if elevation is None:
        if canonical is None and declared is None:
            reason = "terrain_elevation_value_missing"
        elif reference != EGM2008_ORTHOMETRIC:
            reason = "terrain_vertical_reference_not_confirmed_as_egm2008"
        else:
            reason = "terrain_elevation_value_missing"
    elif status not in _RESOLVED_STATUSES:
        reason = str(fact.get("reason") or "terrain_data_status_not_resolved")
    resolved = elevation is not None and status in _RESOLVED_STATUSES

    return {
        "data_status": "passed" if resolved else "unknown",
        "surface_elevation_max_egm2008_m": elevation if resolved else None,
        # 源 canonical 数值原样保留（诊断/审计用，绝不改语义）。
        "surface_elevation_max_m": canonical,
        "surface_elevation_mean_m": _number(fact.get("surface_elevation_mean_m")),
        "surface_elevation_min_m": _number(fact.get("surface_elevation_min_m")),
        "vertical_reference": reference,
        "vertical_reference_source": reference_source,
        "vertical_reference_evidence": reference_evidence,
        "vertical_projection": projection if resolved else None,
        "surface_model": meta.get("surface_model"),
        "cell_status": status or None,
        "valid_pixel_count": fact.get("valid_sample_count", fact.get("valid_pixel_count")),
        "nodata_pixel_count": fact.get("nodata_pixel_count"),
        "sampling": fact.get("sampling"),
        "source": deepcopy(fact.get("source")),
        "reason": None if resolved else reason,
    }


def canonical_terrain_facts(attribute):
    """``grid_attributes.terrain`` → ``{grid_id: canonical fact}``（一次解析 dataset 元数据）。"""

    raw = attribute if isinstance(attribute, dict) else {}
    cells = raw.get("cells")
    if not isinstance(cells, dict):
        return {}
    metadata = terrain_dataset_metadata(raw)
    return {
        str(grid_id): canonical_terrain_fact(fact, dataset_metadata=metadata)
        for grid_id, fact in cells.items()
    }


__all__ = [
    "EGM2008_ORTHOMETRIC", "canonical_terrain_fact", "canonical_terrain_facts",
    "canonical_vertical_reference", "terrain_dataset_metadata",
]
