"""Radar Surveillance Layout V1 — 领域契约、真实设备事实与 provenance。

本模块是 **additive** 的：它不修改任何既有契约（``GeometricCoverage3DV1`` 的
sphere/hemisphere 语义、``Route3DProfile``、``LayeredRouteValidation`` /
``LayeredOperationalAdoption``、``Theta* V2`` 全部保持原样），也不写任何既有结果容器。

正式名称
--------

**「80m固定高度航路方向性雷达几何初步划设方案」**

``model_scope = geometric_initial_radar_layout``

本模型只做**几何**布设：真实设备资料中的 RCS / Pd / Pfa、方位·俯仰·距离精度、
搜索/跟踪更新时间等字段全部作为**设备事实与 provenance** 保存，**不进入**几何布设
公式。以下内容一律 ``not_evaluated``：radar_equation、Pd_distance_curve、
terrain_LOS、diffraction、building_blocking、clutter、multipath、interference。

真实设备资料（只读来源，禁止修改源文件）
----------------------------------------

    D:\\aaa2026project\\UOM\\舟山\\基础数据\\设备性能\\
        低空智能网联系统相关设备信息-四创(2).docx

该路径是一个 **.docx 文件**（不是目录），SHA-256
``e0d9cc20f19b74d30df34548274fb98f04f36bb61b54531bda5db638f13f0b81``。
本轮只读取其中的 **中近程雷达Ⅰ型** 与 **中近程雷达Ⅱ型** 两个表格条目，不读取/不替代
同级目录下的其它设备资料。

固定几何参数
------------

资料中的探测距离是**不等式**（Ⅰ型 ``最小探测距离：≤120米``、``最大探测距离：≥3km``；
Ⅱ型 ``≤200米``、``≥5km``），而本轮布设模型按用户明确的固定几何参数使用：

* Radar-I：``min_slant_range_m = 120``、``max_slant_range_m = 3000``
* Radar-II：``min_slant_range_m = 200``、``max_slant_range_m = 5000``

两个值都**同时**保留为设备事实（``source_inequality`` / ``source_value``），因此模型
参数与来源证据可逐项对照，绝不是"把来源改掉"。
"""

from __future__ import annotations

from copy import deepcopy

SCHEMA_VERSION = "radar-surveillance-layout-v1"
MODEL_SCOPE = "geometric_initial_radar_layout"
ALGORITHM_ID = "radar_surveillance_layout"
ALGORITHM_VERSION = "1.0"
ALGORITHM_NAME = "Radar Surveillance Layout V1"
ALGORITHM_SEMANTICS = "80m固定高度航路方向性雷达几何初步划设方案"

#: 本模型绝不评估的物理/传播/环境项（逐项显式声明，不省略、不静默）。
NOT_EVALUATED = {
    name: "not_evaluated" for name in (
        "radar_equation", "Pd_distance_curve", "terrain_LOS", "diffraction",
        "building_blocking", "clutter", "multipath", "interference",
    )
}

#: 固定高度层：80 m，EGM2008 正高。
FIXED_ALTITUDE_LAYER_ID = "ALT-080"
FIXED_ALTITUDE_M = 80.0
VERTICAL_REFERENCE = "egm2008_orthometric"

#: 投影到米制平面使用的 CRS（舟山工程既有显式机制）。
METRIC_CRS = "EPSG:32651"

#: 每塔最多单面阵数量。
MAX_PANELS_PER_TOWER = 4

#: 单面阵水平波束。
AZIMUTH_BEAMWIDTH_DEG = 90.0
AZIMUTH_HALF_WIDTH_DEG = 45.0

#: 单面阵俯仰覆盖。
ELEVATION_CENTER_DEG = 22.5
ELEVATION_MIN_DEG = 0.0
ELEVATION_MAX_DEG = 45.0

#: 参考条件（资料原文条件，**不进入**几何公式）。
RCS_REFERENCE_M2 = 0.01
PD_REFERENCE = 0.8
PFA_REFERENCE = 1e-6

#: 型号枚举。
RADAR_TYPE_I = "radar_i"
RADAR_TYPE_II = "radar_ii"
RADAR_TYPES = (RADAR_TYPE_I, RADAR_TYPE_II)

RADAR_TYPE_LABELS = {
    RADAR_TYPE_I: "中近程雷达Ⅰ型",
    RADAR_TYPE_II: "中近程雷达Ⅱ型",
}

#: 每条航路采样点的地表分类。``unknown`` fail-closed：绝不自动按 sea 处理。
SURFACE_CLASSES = ("land", "sea", "unknown")

#: 要求的不同站址数量（land 需要 2 个独立站址；sea 需要 1 个；unknown fail-closed）。
REQUIRED_DISTINCT_SITE_COUNT = {"land": 2, "sea": 1, "unknown": None}

#: 单面阵覆盖判定的固定几何参数（米 / 度）。
RADAR_GEOMETRY_PARAMETERS = {
    RADAR_TYPE_I: {
        "radar_type": RADAR_TYPE_I,
        "label": RADAR_TYPE_LABELS[RADAR_TYPE_I],
        "min_slant_range_m": 120.0,
        "max_slant_range_m": 3000.0,
        "azimuth_beamwidth_deg": AZIMUTH_BEAMWIDTH_DEG,
        "azimuth_half_width_deg": AZIMUTH_HALF_WIDTH_DEG,
        "elevation_center_deg": ELEVATION_CENTER_DEG,
        "elevation_min_deg": ELEVATION_MIN_DEG,
        "elevation_max_deg": ELEVATION_MAX_DEG,
        "rcs_reference_m2": RCS_REFERENCE_M2,
        "pd_reference": PD_REFERENCE,
        "pfa_reference": PFA_REFERENCE,
        "source": "low_altitude_intelligent_networked_system_device_information_sichuang_2",
        "confirmed": True,
        "parameter_origin": "source_device_specification_fixed_geometry_preset",
    },
    RADAR_TYPE_II: {
        "radar_type": RADAR_TYPE_II,
        "label": RADAR_TYPE_LABELS[RADAR_TYPE_II],
        "min_slant_range_m": 200.0,
        "max_slant_range_m": 5000.0,
        "azimuth_beamwidth_deg": AZIMUTH_BEAMWIDTH_DEG,
        "azimuth_half_width_deg": AZIMUTH_HALF_WIDTH_DEG,
        "elevation_center_deg": ELEVATION_CENTER_DEG,
        "elevation_min_deg": ELEVATION_MIN_DEG,
        "elevation_max_deg": ELEVATION_MAX_DEG,
        "rcs_reference_m2": RCS_REFERENCE_M2,
        "pd_reference": PD_REFERENCE,
        "pfa_reference": PFA_REFERENCE,
        "source": "low_altitude_intelligent_networked_system_device_information_sichuang_2",
        "confirmed": True,
        "parameter_origin": "source_device_specification_fixed_geometry_preset",
    },
}

#: 真实资料 provenance（只读来源，源文件未被修改）。
DEVICE_SOURCE = {
    "source_id": "low_altitude_intelligent_networked_system_device_information_sichuang_2",
    "title": "低空智能网联系统相关设备信息-四创(2).docx",
    "absolute_path": (
        "D:\\aaa2026project\\UOM\\舟山\\基础数据\\设备性能\\"
        "低空智能网联系统相关设备信息-四创(2).docx"
    ),
    "source_kind": "docx_table",
    "source_type": "real",
    "read_only": True,
    "source_modified": False,
    "sha256": "e0d9cc20f19b74d30df34548274fb98f04f36bb61b54531bda5db638f13f0b81",
    "same_directory_not_used_for_substitution": [
        "低空智能网联系统相关设备信息-四创(1).docx",
        "低空智联网地面机载设备指标(1).docx",
        "5GA通感设备性能参数及可靠性指标(1).docx",
        "低空经济产品资料_低空飞行保障专网_卫星互联网_v1.1(1).docx",
        "低空经济产品资料_低空飞行保障专网_卫星互联网_应用案例v1.3(1).docx",
        "信通院_TDOA调研_天罡科技.xlsx",
        "信通院_气象调研表_中科光电(1).xlsx",
        "天奥科技低空业务资料2026(1).pdf",
        "成都天奥信息科技有限公司低空推荐产品案例(1).docx",
    ],
    "extraction_channel": "docx word/document.xml → 文本（表格行/单元格结构保留）",
    "sibling_device_entries_present_but_not_used": (
        "监视设施设备表中另有「远程雷达」「超近程雷达Ⅰ/Ⅱ/Ⅲ型」，"
        "以及导航设施设备与气象设备；本轮布设只使用中近程雷达Ⅰ型与Ⅱ型。"
    ),
    "source_modified_note": "本轮全程只读；未写入、未转换、未替换该来源文件。",
}

#: 两型雷达的**真实设备字段**（保留来源原文与单位）。
#:
#: 每个字段：``raw``（来源原文）/ ``value``（解析值，可能为 None）/ ``unit`` /
#: ``quantity`` / ``source_row`` / ``source_cell`` / ``used_in_geometry``。
#: ``used_in_geometry`` 恒为 False —— 这些字段一律不进入几何布设公式。
def _device_fact(raw, value, unit, quantity, *, source_row, source_cell, condition=None):
    return {
        "raw": raw,
        "value": value,
        "unit": unit,
        "quantity": quantity,
        "source_row": source_row,
        "source_cell": source_cell,
        "condition": condition,
        "used_in_geometry": False,
    }


RADAR_DEVICE_FACTS = {
    RADAR_TYPE_I: {
        "radar_type": RADAR_TYPE_I,
        "label": RADAR_TYPE_LABELS[RADAR_TYPE_I],
        "table": "监视设施设备",
        "source_id": DEVICE_SOURCE["source_id"],
        "fields": {
            "function_unmanned_aircraft_reconnaissance": _device_fact(
                "1）具备无人机侦查功能；", True, None, "capability",
                source_row=30, source_cell="主要功能 1）",
            ),
            "function_realtime_parameter_and_status_display": _device_fact(
                "2）具备工作参数和状态实时显示功能。", True, None, "capability",
                source_row=31, source_cell="主要功能 2）",
            ),
            "min_detection_distance": _device_fact(
                "①最小探测距离：≤120米；", 120.0, "m", "detection_range",
                source_row=34, source_cell="性能指标 1）①",
                condition="RCS=0.01m²，Pf=10⁻⁶，Pd=0.8",
            ),
            "max_detection_distance": _device_fact(
                "②最大探测距离：≥3km；", 3000.0, "m", "detection_range",
                source_row=35, source_cell="性能指标 1）②",
                condition="RCS=0.01m²，Pf=10⁻⁶，Pd=0.8",
            ),
            "max_detection_height": _device_fact(
                "③最大探测高度：≥600米；", 600.0, "m", "detection_height",
                source_row=36, source_cell="性能指标 1）③",
                condition="RCS=0.01m²，Pf=10⁻⁶，Pd=0.8",
            ),
            "azimuth_measurement_accuracy": _device_fact(
                "①方位≤0.5°；", 0.5, "deg", "measurement_accuracy",
                source_row=38, source_cell="测量精度 ①",
            ),
            "elevation_measurement_accuracy": _device_fact(
                "②俯仰≤0.5°；", 0.5, "deg", "measurement_accuracy",
                source_row=39, source_cell="测量精度 ②",
            ),
            "range_measurement_accuracy": _device_fact(
                "③距离≤10m；", 10.0, "m", "measurement_accuracy",
                source_row=40, source_cell="测量精度 ③",
            ),
            "azimuth_coverage": _device_fact(
                "①方位覆盖范围：360°（四面阵）、±45°（单面阵）；",
                {"four_face_array_deg": 360.0, "single_face_array_deg": 90.0},
                "deg", "angular_coverage", source_row=42, source_cell="角度覆盖范围 ①",
            ),
            "elevation_coverage_max": _device_fact(
                "②俯仰最大覆盖范围：≥45°（仰角可调）；", 45.0, "deg",
                "angular_coverage", source_row=43, source_cell="角度覆盖范围 ②",
                condition="仰角可调",
            ),
            "search_update_interval": _device_fact(
                "①搜索≤2s；", 2.0, "s", "data_update_rate",
                source_row=45, source_cell="数据更新率 ①",
            ),
            "track_update_interval": _device_fact(
                "②跟踪≤1s。", 1.0, "s", "data_update_rate",
                source_row=46, source_cell="数据更新率 ②",
            ),
        },
        "geometry_parameter_mapping": {
            "min_slant_range_m": {
                "source_field": "min_detection_distance",
                "source_value": 120.0,
                "source_inequality": "≤120米",
                "model_value": 120.0,
                "note": (
                    "来源为不等式上界（≤120米）；本模型按用户明确的固定几何参数取 120 m，"
                    "来源原文同时保留，未修改来源。"
                ),
            },
            "max_slant_range_m": {
                "source_field": "max_detection_distance",
                "source_value": 3000.0,
                "source_inequality": "≥3km",
                "model_value": 3000.0,
                "note": (
                    "来源为不等式下界（≥3km）；本模型按用户明确的固定几何参数取 3000 m，"
                    "来源原文同时保留，未修改来源。"
                ),
            },
        },
    },
    RADAR_TYPE_II: {
        "radar_type": RADAR_TYPE_II,
        "label": RADAR_TYPE_LABELS[RADAR_TYPE_II],
        "table": "监视设施设备",
        "source_id": DEVICE_SOURCE["source_id"],
        "fields": {
            "function_unmanned_aircraft_detection_and_tracking": _device_fact(
                "1）具备无人机探测、跟踪功能；", True, None, "capability",
                source_row=50, source_cell="主要功能 1）",
            ),
            "function_realtime_parameter_and_status_display": _device_fact(
                "2）具备设备工作参数和状态实时显示功能。", True, None, "capability",
                source_row=51, source_cell="主要功能 2）",
            ),
            "min_detection_distance": _device_fact(
                "①最小探测距离：≤200米；", 200.0, "m", "detection_range",
                source_row=54, source_cell="性能指标 1）①",
                condition="民用微型无人机 RCS=0.01m²，Pf=10⁻⁶，探测概率 80%",
            ),
            "max_detection_distance": _device_fact(
                "②最大探测距离：≥5km；", 5000.0, "m", "detection_range",
                source_row=55, source_cell="性能指标 1）②",
                condition="民用微型无人机 RCS=0.01m²，Pf=10⁻⁶，探测概率 80%",
            ),
            "max_detection_height": _device_fact(
                "③最大探测高度：≥600米；", 600.0, "m", "detection_height",
                source_row=56, source_cell="性能指标 1）③",
                condition="民用微型无人机 RCS=0.01m²，Pf=10⁻⁶，探测概率 80%",
            ),
            "target_classification_accuracy": _device_fact(
                "④目标分类识别准确率≥80%；", 0.8, "ratio", "classification",
                source_row=57, source_cell="性能指标 1）④",
            ),
            "azimuth_measurement_accuracy": _device_fact(
                "①方位精度≤0.3°；", 0.3, "deg", "measurement_accuracy",
                source_row=59, source_cell="测量精度 ①",
            ),
            "elevation_measurement_accuracy_low": _device_fact(
                "②俯仰精度≤0.3°（仰角2°～6°间）", 0.3, "deg", "measurement_accuracy",
                source_row=60, source_cell="测量精度 ②", condition="仰角 2°～6°",
            ),
            "elevation_measurement_accuracy_high": _device_fact(
                "≤0.6°（仰角6°～30°间）；", 0.6, "deg", "measurement_accuracy",
                source_row=60, source_cell="测量精度 ②", condition="仰角 6°～30°",
            ),
            "range_measurement_accuracy": _device_fact(
                "③距离精度≤5m；", 5.0, "m", "measurement_accuracy",
                source_row=61, source_cell="测量精度 ③",
            ),
            "azimuth_coverage": _device_fact(
                "①方位覆盖范围：360°（四面阵）、±45°（单面阵）；",
                {"four_face_array_deg": 360.0, "single_face_array_deg": 90.0},
                "deg", "angular_coverage", source_row=63, source_cell="角度覆盖范围 ①",
            ),
            "elevation_coverage_max": _device_fact(
                "②俯仰最大覆盖范围：≥45°（仰角可调）；", 45.0, "deg",
                "angular_coverage", source_row=64, source_cell="角度覆盖范围 ②",
                condition="仰角可调",
            ),
            "search_update_interval": _device_fact(
                "①搜索≤2s；", 2.0, "s", "data_update_rate",
                source_row=66, source_cell="数据更新率 ①",
            ),
            "track_update_interval": _device_fact(
                "跟踪≤1s。", 1.0, "s", "data_update_rate",
                source_row=67, source_cell="数据更新率 ②",
            ),
        },
        "geometry_parameter_mapping": {
            "min_slant_range_m": {
                "source_field": "min_detection_distance",
                "source_value": 200.0,
                "source_inequality": "≤200米",
                "model_value": 200.0,
                "note": (
                    "来源为不等式上界（≤200米）；本模型按用户明确的固定几何参数取 200 m，"
                    "来源原文同时保留，未修改来源。"
                ),
            },
            "max_slant_range_m": {
                "source_field": "max_detection_distance",
                "source_value": 5000.0,
                "source_inequality": "≥5km",
                "model_value": 5000.0,
                "note": (
                    "来源为不等式下界（≥5km）；本模型按用户明确的固定几何参数取 5000 m，"
                    "来源原文同时保留，未修改来源。"
                ),
            },
        },
    },
}


def radar_geometry_parameters(radar_type):
    """Return a deep copy of the fixed geometry parameters of one radar type."""

    key = str(radar_type or "")
    if key not in RADAR_GEOMETRY_PARAMETERS:
        raise ValueError(f"不支持的雷达型号：{radar_type}")
    return deepcopy(RADAR_GEOMETRY_PARAMETERS[key])


def radar_device_facts(radar_type):
    """Return a deep copy of the real equipment facts of one radar type."""

    key = str(radar_type or "")
    if key not in RADAR_DEVICE_FACTS:
        raise ValueError(f"不支持的雷达型号：{radar_type}")
    return deepcopy(RADAR_DEVICE_FACTS[key])


def device_provenance():
    """The read-only provenance block of the two real radar types."""

    return {
        "source": deepcopy(DEVICE_SOURCE),
        "device_facts": {
            radar_type: {
                "label": RADAR_DEVICE_FACTS[radar_type]["label"],
                "field_count": len(RADAR_DEVICE_FACTS[radar_type]["fields"]),
                "fields": {
                    name: {
                        "raw": fact["raw"],
                        "value": fact["value"],
                        "unit": fact["unit"],
                        "quantity": fact["quantity"],
                        "source_row": fact["source_row"],
                        "condition": fact["condition"],
                        "used_in_geometry": False,
                    }
                    for name, fact in RADAR_DEVICE_FACTS[radar_type]["fields"].items()
                },
                "geometry_parameter_mapping": deepcopy(
                    RADAR_DEVICE_FACTS[radar_type]["geometry_parameter_mapping"]
                ),
            }
            for radar_type in RADAR_TYPES
        },
        "fields_used_in_geometry": [
            "device_type_identity",
            "single_face_array_azimuth_coverage_±45deg",
            "elevation_coverage_max_45deg",
            "min_max_detection_distance_as_fixed_geometry_preset",
        ],
        "fields_recorded_but_not_used_in_geometry": [
            "rcs_reference_m2", "pd_reference", "pfa_reference",
            "azimuth_measurement_accuracy", "elevation_measurement_accuracy",
            "range_measurement_accuracy", "search_update_interval",
            "track_update_interval", "max_detection_height",
            "target_classification_accuracy",
        ],
        "source_modified": False,
    }


def device_summary():
    """前端「两型雷达真实资料摘要」最小投影。"""

    return {
        "source": {
            "title": DEVICE_SOURCE["title"],
            "absolute_path": DEVICE_SOURCE["absolute_path"],
            "sha256": DEVICE_SOURCE["sha256"],
            "read_only": True,
            "source_modified": False,
        },
        "types": [
            {
                "radar_type": radar_type,
                "label": RADAR_DEVICE_FACTS[radar_type]["label"],
                "min_slant_range_m": RADAR_GEOMETRY_PARAMETERS[radar_type]["min_slant_range_m"],
                "max_slant_range_m": RADAR_GEOMETRY_PARAMETERS[radar_type]["max_slant_range_m"],
                "source_min_detection_distance": RADAR_DEVICE_FACTS[radar_type]["fields"][
                    "min_detection_distance"
                ]["raw"],
                "source_max_detection_distance": RADAR_DEVICE_FACTS[radar_type]["fields"][
                    "max_detection_distance"
                ]["raw"],
                "max_detection_height_raw": RADAR_DEVICE_FACTS[radar_type]["fields"][
                    "max_detection_height"
                ]["raw"],
                "azimuth_coverage_raw": RADAR_DEVICE_FACTS[radar_type]["fields"][
                    "azimuth_coverage"
                ]["raw"],
                "elevation_coverage_raw": RADAR_DEVICE_FACTS[radar_type]["fields"][
                    "elevation_coverage_max"
                ]["raw"],
                "azimuth_measurement_accuracy_raw": RADAR_DEVICE_FACTS[radar_type]["fields"][
                    "azimuth_measurement_accuracy"
                ]["raw"],
                "range_measurement_accuracy_raw": RADAR_DEVICE_FACTS[radar_type]["fields"][
                    "range_measurement_accuracy"
                ]["raw"],
                "search_update_interval_raw": RADAR_DEVICE_FACTS[radar_type]["fields"][
                    "search_update_interval"
                ]["raw"],
                "track_update_interval_raw": RADAR_DEVICE_FACTS[radar_type]["fields"][
                    "track_update_interval"
                ]["raw"],
                "rcs_reference_m2": RCS_REFERENCE_M2,
                "pd_reference": PD_REFERENCE,
                "pfa_reference": PFA_REFERENCE,
                "used_in_geometry": False,
            }
            for radar_type in RADAR_TYPES
        ],
        "not_evaluated": deepcopy(NOT_EVALUATED),
    }


# ------------------------------------------------------------------------------ policy


def default_radar_mount_assumption():
    """雷达原点挂高策略。"无证据" ⇒ 无值（``None``），绝不写死虚假塔高/安装高度。

    前端可以提交统一的**工程示例参数**；此时必须保存
    ``source`` / ``confirmed=false`` / ``parameter_origin=engineering_assumption``。
    """

    return {
        "radar_mount_height_m": None,
        "mount_height_basis": None,
        "source": None,
        "confirmed": False,
        "parameter_origin": "not_configured",
        "status": "not_configured",
    }


MOUNT_HEIGHT_ORIGINS = (
    "not_configured",
    "engineering_assumption",
    "source_device_installation_height",
    "source_tower_record",
    "user_confirmed",
)


def normalize_radar_mount_assumption(value):
    payload = value if isinstance(value, dict) else {}
    result = default_radar_mount_assumption()
    raw = payload.get("radar_mount_height_m")
    height = None
    if raw not in (None, ""):
        try:
            height = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("radar_mount_height_m 必须是数值") from exc
        if height != height or abs(height) == float("inf"):
            raise ValueError("radar_mount_height_m 必须是有限数值")
        if height < 0:
            raise ValueError("radar_mount_height_m 不能为负")
    origin = str(payload.get("parameter_origin") or "")
    confirmed = payload.get("confirmed") is True
    if height is None:
        result["status"] = "not_configured"
        return result
    if origin not in MOUNT_HEIGHT_ORIGINS or origin == "not_configured":
        # 未声明来源 ⇒ 只能按工程假设处理，绝不冒充已确认真实数据。
        origin = "engineering_assumption"
    if origin != "user_confirmed":
        confirmed = False
    result.update({
        "radar_mount_height_m": height,
        "mount_height_basis": str(payload.get("mount_height_basis") or "radar_above_tower_top"),
        "source": str(payload.get("source") or "user_supplied_engineering_example_parameter"),
        "confirmed": confirmed,
        "parameter_origin": origin,
        "status": (
            "confirmed" if confirmed and origin == "user_confirmed"
            else "pending_confirmation"
        ),
    })
    return result


__all__ = [
    "ALGORITHM_ID", "ALGORITHM_NAME", "ALGORITHM_SEMANTICS", "ALGORITHM_VERSION",
    "AZIMUTH_BEAMWIDTH_DEG", "AZIMUTH_HALF_WIDTH_DEG",
    "DEVICE_SOURCE", "ELEVATION_CENTER_DEG", "ELEVATION_MAX_DEG", "ELEVATION_MIN_DEG",
    "FIXED_ALTITUDE_LAYER_ID", "FIXED_ALTITUDE_M", "MAX_PANELS_PER_TOWER",
    "METRIC_CRS", "MODEL_SCOPE", "MOUNT_HEIGHT_ORIGINS", "NOT_EVALUATED",
    "PD_REFERENCE", "PFA_REFERENCE", "RADAR_DEVICE_FACTS", "RADAR_GEOMETRY_PARAMETERS",
    "RADAR_TYPES", "RADAR_TYPE_I", "RADAR_TYPE_II", "RADAR_TYPE_LABELS",
    "RCS_REFERENCE_M2", "REQUIRED_DISTINCT_SITE_COUNT", "SCHEMA_VERSION",
    "SURFACE_CLASSES", "VERTICAL_REFERENCE",
    "default_radar_mount_assumption", "device_provenance", "device_summary",
    "normalize_radar_mount_assumption", "radar_device_facts", "radar_geometry_parameters",
]
