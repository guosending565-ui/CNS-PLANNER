"""FC30 —— **canonical 机载档案**（P17 连续服务可接受性评估的机载事实基线）。

本模块只承载**事实与出处**，不做任何评估，也不写任何状态。

设计约束（Round 2.5）
---------------------

1. 每个量都必须带 ``source_type`` / ``source`` / ``statement`` —— 与
   :mod:`cns_planner.domain.planning_evidence` 同一套来源语义（外加本轮的
   ``external_reference``）。**绝不**把内置的工程基线冒充成厂家事实或法规事实。
2. ``3 s`` 是 **FC30 设备故障保护（failsafe）触发门限**，即"遥控信号丢失超过 3 s
   触发 Failsafe RTH"这一**设备行为事实**。它**不是**任何法规阈值，报告与前端
   必须逐字保留该定位（见 :data:`FC30_FAILSAFE_TRIGGER_SEMANTICS`）。
3. 本体不声明任何"RTK 断 X 秒即失败"的判定；P17 的导航判定只看
   :mod:`cns_planner.domain.cns_continuous_service` 的显式状态机。
4. 事实清单落在 ``metadata.fc30_facts`` 上：它随档案一起保存、重开后逐字保留，
   且**不**与既有 CNS 子系统契约（type / performance / fallbacks）争夺字段。
"""

from __future__ import annotations

from copy import deepcopy

from .cns_inputs import normalize_aircraft_profile

#: 内置 canonical 档案 id。
FC30_AIRCRAFT_ID = "FC30"

#: 3 s 的**权威定位**：设备 failsafe 触发事实，不是法规阈值。
FC30_FAILSAFE_TRIGGER_SEMANTICS = "device_failsafe_trigger_fact_not_regulatory_threshold"

#: FC30 的**声明事实**。每一项都是"出处 + 事实"的成对记录，不含判定。
#:
#: ``value`` 为 ``None`` 表示"本条事实的具体数值未提供"（例如恢复时长是一个区间而
#: 不是一个确定值），此时必须保留 ``statement`` 说明取值形态。
FC30_DECLARED_FACTS = (
    {
        "fact_id": "fc30.route_speed",
        "parameter": "route_speed_mps",
        "value": 15.0,
        "unit": "m/s",
        "source_type": "confirmed_source_fact",
        "source": "FC30 机载档案（canonical，随本仓库冻结）",
        "statement": "FC30 航路（巡航）速度为 15 m/s；作为规划基线使用。",
        "semantics": "planning_baseline_cruise_speed",
    },
    {
        "fact_id": "fc30.max_horizontal_speed",
        "parameter": "max_horizontal_speed_mps",
        "value": 20.0,
        "unit": "m/s",
        "source_type": "confirmed_source_fact",
        "source": "FC30 机载档案（canonical，随本仓库冻结）",
        "statement": "FC30 最大水平速度为 20 m/s；仅作为机型包线事实登记。",
        "semantics": "airframe_envelope_fact",
    },
    {
        "fact_id": "fc30.mtow",
        "parameter": "mtow_kg",
        "value": 95.0,
        "unit": "kg",
        "source_type": "confirmed_source_fact",
        "source": "FC30 机载档案（canonical，随本仓库冻结）",
        "statement": "FC30 最大起飞重量（MTOW）为 95 kg。",
        "semantics": "airframe_envelope_fact",
    },
    {
        "fact_id": "fc30.navigation.fallback.rtk_to_gnss",
        "parameter": "rtk_loss_fallback",
        "value": "gnss",
        "unit": None,
        "source_type": "confirmed_source_fact",
        "source": "FC30 机载档案（canonical，随本仓库冻结）",
        "statement": "RTK 丢失时 FC30 回退到 GNSS 定位（RTK loss → GNSS fallback）。",
        "semantics": "declared_navigation_fallback_chain",
    },
    {
        "fact_id": "fc30.navigation.fallback.gnss_loss",
        "parameter": "gnss_loss_fallback",
        "value": "atti_descend_asap",
        "unit": None,
        "source_type": "confirmed_source_fact",
        "source": "FC30 机载档案（canonical，随本仓库冻结）",
        "statement": (
            "GNSS 也失效时 FC30 进入 ATTI（姿态）模式并尽快降落；"
            "该状态**不**提供航路保持（route containment）能力。"
        ),
        "semantics": "declared_navigation_fallback_chain",
    },
    {
        "fact_id": "fc30.failsafe.rc_loss_rth",
        "parameter": "rc_loss_failsafe_trigger_s",
        "value": 3.0,
        "unit": "s",
        "source_type": "confirmed_source_fact",
        "source": (
            "FC30 机载档案（canonical）：Failsafe RTH 配置下，遥控信号丢失超过 3 s "
            "触发自动返航。"
        ),
        "statement": "在 Failsafe RTH 已配置的前提下，遥控（RC）信号丢失超过 3 s 触发 RTH。",
        "semantics": FC30_FAILSAFE_TRIGGER_SEMANTICS,
        "not_a_regulatory_threshold": True,
    },
)


def fc30_aircraft_profile() -> dict:
    """FC30 的 canonical 机载档案（``normalize_aircraft_profile`` 的合法输入）。

    与 ``cns_planner/config/aircraft_profiles.json`` 中的 FC30 条目**逐字一致**
    （有回归测试守护），因此"内置事实"与"项目可选档案"不会出现第二种真值。
    """

    return normalize_aircraft_profile({
        "aircraft_id": FC30_AIRCRAFT_ID,
        "name": "FC30",
        "manufacturer": "FC30",
        "model": "FC30",
        "cruise_speed_mps": 15.0,
        "max_speed_mps": 20.0,
        "max_horizontal_speed_mps": 20.0,
        "mtow_kg": 95.0,
        "communication": {
            "status": "confirmed",
            "capabilities": [],
            "type": {
                "service_type": "command_control",
                "technology": "dedicated_radio",
            },
            #: P17 的 C 全失联 / 冗余退化阈值**另有**独立的显式工程基线参数
            #: （``cns_continuous_service_policy``）；这里登记的是设备侧的失联门限事实。
            "performance": {"lost_link_threshold_s": 3.0},
            "confirmed": True,
            "confirmation_status": "confirmed",
            "source": "FC30 机载档案（canonical）",
        },
        "navigation": {
            "status": "confirmed",
            "capabilities": [],
            "type": {"technology": "gnss_rtk"},
            "performance": {"max_horizontal_error_m": None, "min_redundancy": 1},
            "confirmed": True,
            "confirmation_status": "confirmed",
            "source": "FC30 机载档案（canonical）",
        },
        "surveillance": {
            "status": "confirmed",
            "capabilities": [],
            "type": {"technology": "adsb", "target_cooperation": "cooperative"},
            "performance": {},
            "confirmed": True,
            "confirmation_status": "confirmed",
            "source": "FC30 机载档案（canonical）",
        },
        "source": (
            "FC30 机载档案（canonical）：航路速度 / 最大水平速度 / MTOW / "
            "导航回退链 / failsafe RTH"
        ),
        "metadata": {
            "authority": "canonical_aircraft_profile",
            "profile_class": "fc30_canonical",
            "confirmed": True,
            "parameter_origin": "fc30_canonical_profile_round2_5",
            "failsafe_trigger_semantics": FC30_FAILSAFE_TRIGGER_SEMANTICS,
            "not_regulatory_threshold": True,
            "fc30_facts": [deepcopy(item) for item in FC30_DECLARED_FACTS],
            "fc30_navigation_fallbacks": [
                {
                    "from": "rtk",
                    "to": "gnss",
                    "capability": "route_containment_depends_on_gnss_accuracy",
                    "source": "FC30 机载档案（canonical）",
                    "source_type": "confirmed_source_fact",
                },
                {
                    "from": "gnss",
                    "to": "atti_descend_asap",
                    "capability": "no_route_containment",
                    "source": "FC30 机载档案（canonical）",
                    "source_type": "confirmed_source_fact",
                },
            ],
            "fc30_controllability": {
                "rc_link_loss_failsafe": "rth",
                "rc_link_loss_failsafe_trigger_s": 3.0,
                "rc_link_loss_failsafe_trigger_semantics": FC30_FAILSAFE_TRIGGER_SEMANTICS,
                "source": "FC30 机载档案 failsafe 配置事实",
                "source_type": "confirmed_source_fact",
                "not_a_regulatory_threshold": True,
            },
        },
    })


def fc30_declared_facts() -> list[dict]:
    """只读的 FC30 事实清单（含逐条 provenance），供前端 / 报告披露。"""

    return [deepcopy(item) for item in FC30_DECLARED_FACTS]


def fc30_planning_speeds(profile: dict | None = None) -> dict:
    """P17 使用的速度基线（route_speed / max_horizontal_speed）。

    只**读取**档案事实；缺失时返回 ``None``（调用方必须保持 unknown，绝不猜测）。
    也接受一个通用机载档案（不限于 FC30），以便评估器对任意选定机型工作。
    """

    item = profile if isinstance(profile, dict) else {}
    route_speed = item.get("cruise_speed_mps")
    max_speed = item.get("max_horizontal_speed_mps", item.get("max_speed_mps"))
    return {
        "route_speed_mps": float(route_speed) if route_speed not in (None, "") else None,
        "max_horizontal_speed_mps": (
            float(max_speed) if max_speed not in (None, "") else None
        ),
    }


__all__ = [
    "FC30_AIRCRAFT_ID", "FC30_DECLARED_FACTS", "FC30_FAILSAFE_TRIGGER_SEMANTICS",
    "fc30_aircraft_profile", "fc30_declared_facts", "fc30_planning_speeds",
]
