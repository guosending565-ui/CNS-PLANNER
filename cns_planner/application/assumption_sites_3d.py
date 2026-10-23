"""P16-V2 选址结果的**只读**三维工程假设投影。

边界（硬约定）：

* 数据来源是**已完成 V2 任务的 staged result**（``/api/tasks`` 同源记录），
  不是 ``ProjectState``；本模块不读取、不写入任何 canonical 容器；
* 只暴露 V2 已选动作的坐标、子系统、设备与复用类别；V2 结果本身没有逐站
  地面正高，因此这里**不猜**原点高程，前端只能按地形贴地并明确标注"假设"；
* 语义固定为 ``engineering_assumption_only_not_formal_state``：
  ``formal_equipment=False``、``canonical_write=False``，与正式 CNS 设备分层；
* 没有成功的 V2 任务时返回 ``unavailable``，绝不回退到 V1 正式方案冒充 V2。
"""

from __future__ import annotations

from ..tasks.task_specs import DEFAULT_V2_SCENARIO_ID, P16_V2_TASK_TYPE

SCHEMA_VERSION = "visualization-3d-assumption-sites/v1"
SEMANTICS = "engineering_assumption_only_not_formal_state"
DISCLOSURE = (
    "P16-V2 选址是工程假设预算，不是正式设备、不是 P16-V1 结果，也不写入 ProjectState；"
    "三维层只按任务 staged 结果叠加，关闭后与正式设备图层完全独立。"
)


def _tasks(context):
    service = getattr(context, "heavy_tasks", None)
    if service is None:
        return []
    records = service.list(status="succeeded", limit=50)
    return records if isinstance(records, list) else []


def _latest_v2(records):
    rows = [
        item for item in records
        if isinstance(item, dict) and item.get("task_type") == P16_V2_TASK_TYPE
    ]
    if not rows:
        return None
    return max(rows, key=lambda item: str(item.get("finished_at") or ""))


def _mount_height(scenario_id):
    """场景声明的统一挂高（版本化配置，缺失则如实返回 None）。"""

    try:
        from .engineering_assumption_scenario_service import (
            EngineeringAssumptionScenarioService,
        )

        service = EngineeringAssumptionScenarioService(
            state_provider=lambda: {},
            standardized_facilities_provider=lambda: {},
            landing_sites_provider=lambda: {},
            tower_colocation_provider=lambda: {},
        )
        scenario = service.load_scenario(scenario_id or DEFAULT_V2_SCENARIO_ID)
        value = ((scenario.get("assumptions") or {}).get("mount_height_agl_m"))
        return float(value) if value is not None else None
    except Exception:  # noqa: BLE001 - 只读展示，配置不可读时不猜挂高
        return None


def assumption_sites_payload(context):
    """返回 V2 假设站址的三维只读投影（不触碰 ProjectState）。"""

    base = {
        "schema_version": SCHEMA_VERSION,
        "semantics": SEMANTICS,
        "disclosure": DISCLOSURE,
        "assumption_only": True,
        "formal_equipment": False,
        "canonical_write": False,
        "source": "p16_v2_task_staged_result",
        "read_only": True,
        "writes_performed": 0,
    }
    tasks = _tasks(context)
    task = _latest_v2(tasks)
    if task is None:
        return {
            **base, "status": "unavailable", "reason": "no_succeeded_v2_task",
            "count": 0, "sites": [],
        }
    summary = task.get("result_summary") or {}
    actions = [
        item for item in (summary.get("selected_actions") or [])
        if isinstance(item, dict) and item.get("coordinate")
    ]
    if not actions:
        return {
            **base, "status": "unavailable", "reason": "v2_result_has_no_selected_actions",
            "source_task_id": task.get("task_id"), "count": 0, "sites": [],
        }

    scenario_id = str((summary.get("inputs") or {}).get("scenario_id") or DEFAULT_V2_SCENARIO_ID)
    mount = _mount_height(scenario_id)
    grouped = {}
    for action in actions:
        key = str(action.get("distinct_site_id") or action.get("site_id") or action.get("action_id"))
        entry = grouped.setdefault(key, {
            "site_id": action.get("site_id") or key,
            "distinct_site_id": action.get("distinct_site_id") or key,
            "name": action.get("name"),
            "coordinate": list(action.get("coordinate") or [])[:2],
            "subsystems": [],
            "devices": [],
            "reuse_classes": [],
            "action_ids": [],
            "formal_equipment": False,
            "assumption_only": True,
        })
        for field, bucket in (
            ("subsystem", "subsystems"), ("device_id", "devices"),
            ("reuse_class", "reuse_classes"),
        ):
            value = action.get(field)
            if value and value not in entry[bucket]:
                entry[bucket].append(value)
        action_id = action.get("action_id")
        if action_id and action_id not in entry["action_ids"]:
            entry["action_ids"].append(action_id)

    return {
        **base,
        "status": "available",
        "reason": None,
        "source_task_id": task.get("task_id"),
        "source_task_finished_at": task.get("finished_at"),
        "scenario_id": scenario_id,
        "mount_height_agl_m": mount,
        "origin_status": (
            "engineering_assumed_ground_plus_mount" if mount is not None
            else "origin_height_unknown_not_guessed"
        ),
        "count": len(grouped),
        "action_count": len(actions),
        "sites": sorted(grouped.values(), key=lambda item: item["distinct_site_id"]),
    }


__all__ = ["DISCLOSURE", "SEMANTICS", "assumption_sites_payload"]
