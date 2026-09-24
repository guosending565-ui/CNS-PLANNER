"""Workspace and standard-grid use cases."""

import math
from copy import deepcopy

from ..algorithms.grid.service import OperationalGridBlockedError
from ..risk.v1 import RiskModelV1
from ..domain.altitude_layer_defaults import ensure_default_altitude_layers
from ..domain.layered_route import default_layered_route_request
from ..domain.population_nodata import (
    POLICY_KEY, default_population_nodata_policy, normalize_population_nodata_policy,
)
from .production_write_authority import drop_runtime_compatibility_result
from .project_state import assessment, empty_grid_attributes
from .route_operating_layer_service import refresh_spatial_status


class WorkspaceService:
    """工作区与标准网格用例。

    GRID-L8-UNIFICATION：正式业务的 canonical 空间索引只有一个层级 —— MH/T 4063.1 **L8**。

    * 正式入口调用 ``WorkspaceGridService.generate_operational()``：恰好 L8、**禁止 silent
      coarsening**、超出 ``max_cells``（软件资源保护上限）时明确 blocked；
    * 层级参数只接受 ``None`` / ``8``：界面也不再暴露 L6/L7，"用户选 L8、实际静默 L7"
      这种模糊状态被彻底取消；
    * L6/L7 的**底层算法**仍在 ``WorkspaceGridService.generate()`` 中保留（legacy / unit
      test / diagnostic），但不再是正式工作流的降级目标。
    """

    def __init__(self, session, grid_service, invalidation, snapshot):
        self.session = session
        self.grid_service = grid_service
        self.invalidation = invalidation
        self.snapshot = snapshot

    def set_workspace(self, bbox, health, preferred_grid_level=None, max_cells=None):
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("工作区必须包含西、南、东、北四个坐标")
        values = [float(value) for value in bbox]
        west, south, east, north = values
        if not (-180 <= west < east <= 180 and -85 < south < north < 85):
            raise ValueError("工作区范围无效")
        # 层级与网格必须在**写入任何状态之前**确定：正式入口只接受 canonical L8（L6/L7 由
        # generate_operational 明确拒绝），超限即阻断，因此不存在"工作区已经保存、网格却悄悄
        # 退到 L7"的中间态，也不存在伪 passed。
        grid = self.grid_service.generate_operational(
            values, level=preferred_grid_level, max_cells=max_cells
        )
        blocked = str(grid.get("status") or "") == "blocked"
        width = math.radians(east - west) * 6371008.8 * math.cos(
            math.radians((south + north) / 2)
        )
        height = math.radians(north - south) * 6371008.8
        state = self.session.state
        self.invalidation.workflow("workspace")
        workspace_status = "blocked" if blocked else "passed"
        workspace = {
            "bbox": values, "area_km2": round(width * height / 1_000_000, 3),
            "health": health, "status": workspace_status,
        }
        if blocked:
            # blocked 是**明确的工程阻断**（不是失败后的降级）：如实记录原因、需求格数与上限，
            # 界面据此提示"缩小工作区或显式提高资源上限"，绝不显示伪通过。
            workspace.update({
                "blocked_code": grid.get("blocked_code"),
                "blocked_reason": grid.get("blocked_message"),
                "required_cells": grid.get("required_cells"),
                "max_cells": grid.get("max_cells"),
                "canonical_level": grid.get("canonical_level"),
            })
        state["workspace"] = workspace
        state["grid"] = grid
        state["grid_attributes"] = empty_grid_attributes()
        state["grid_risk"] = RiskModelV1.empty()
        state["traffic_simulation"] = None
        state["risks"]["environment"] = assessment(
            "not_calculated", "等待当前网格属性风险评估"
        )
        state["result_statuses"]["workspace"] = workspace_status
        state["result_statuses"]["grid"] = workspace_status
        state["result_statuses"]["environment_risk"] = "not_calculated"
        if not blocked:
            # 工作区（工程范围）确认后，若该项目从未初始化过巡航高度层目录，补建工程默认高度层
            # （ALT-060/080/100/150/200，EGM2008 正高）。这只补 **catalog 条目**，不为任何航路选择高度层：
            # planning request 仍需用户显式选择 AltitudeLayer。
            # 网格被阻断时不补建：正式业务流程在 L8 空间索引成立之前不继续。
            if ensure_default_altitude_layers(state):
                refresh_spatial_status(state)
        self.session.save()
        if blocked:
            # 状态已经如实落库（blocked），异常只负责让本次 API 调用返回可读错误并停止后续步骤。
            raise OperationalGridBlockedError(grid.get("blocked_message"), grid.get("error"))
        return self.snapshot()


    # ------------------------------------------------- population NoData semantics

    def population_nodata_policy_snapshot(self):
        """The stored explicit confirmation (never invented, default = not configured)."""

        return deepcopy(
            self.session.state.get(POLICY_KEY) or default_population_nodata_policy()
        )

    def set_population_nodata_policy(self, payload):
        """Confirm (or withdraw) the population source NoData semantics.

        A confirmation only changes *how source NoData inside the raster footprint is
        interpreted*; it never touches an algorithm, a threshold, a validation verdict or an
        adoption.  Because the population grid attribute is derived, the change stales that
        attribute (and only its own downstream) so it must be recomputed explicitly.
        """

        raw = payload.get(POLICY_KEY, payload) if isinstance(payload, dict) else payload
        candidate = normalize_population_nodata_policy(raw)
        state = self.session.state
        current = normalize_population_nodata_policy(state.get(POLICY_KEY))
        if candidate == current:
            return self.snapshot()
        state[POLICY_KEY] = candidate
        self.invalidation.grid_sources(["population"])
        self.invalidation.layered_route("population_nodata_policy_changed")
        # The derived per-grid shelter field caches the population factor: drop it so the
        # next read rebuilds from the recomputed attribute.
        state.pop("_population_shelter_cache", None)
        self.session.save()
        return self.snapshot()

    def clear_workspace(self):
        """清除工作区：删除项目节点、场景航路与运行航路，并让依赖它们的结果失效。

        BUG-WORKSPACE-CLEAR-001/002：清工作区确实会删除 ``nodes`` / ``scenario_routes`` /
        ``operational_routes``（这是既有业务语义，不改变）。但显式 ``layered_route_planning_request``
        过去会**原样保留**，继续指向已经被删除的 scenario route 或 OD 节点 —— 于是 readiness
        只报 ``scenario_route_not_found``（一个悬空引用），而不是如实报告"规划请求尚未配置"。

        现在清工作区后请求被重置为 ``default_layered_route_request()``（未确认、无 route/node
        引用、无高度层选择）；依赖它的 layered 候选 / mask、RouteRiskProfile、layered
        validations、operational adoptions（及其拥有的运行航路状态）与 transition / Safety
        Evidence 全部按既有 invalidation 语义被标为 stale —— 既不留悬空引用，也绝不把任何
        结果伪装成 current。
        """

        state = self.session.state
        self.invalidation.workflow("workspace")
        state.update({
            "workspace": None, "grid": None,
            "grid_attributes": empty_grid_attributes(),
            "grid_risk": RiskModelV1.empty(), "traffic_simulation": None,
            "nodes": [], "scenario_routes": [], "operational_routes": [],
            "coverage": None,
            # 悬空引用清零：重置为默认（pending_confirmation）请求，不伪造 confirmed。
            "layered_route_planning_request": default_layered_route_request(),
        })
        state["risks"]["environment"] = assessment(
            "not_calculated", "GRC 环境/航路规划风险接口"
        )
        # 旧算法兼容试算也属于被清除的派生航路：清工作区时一并回收，避免兼容副本
        # 残留成"仍然存在的航路"。canonical ``operational_routes`` 的语义不变。
        drop_runtime_compatibility_result(self.session, "operational_routes")
        state["result_statuses"].update({
            "workspace": "not_calculated", "grid": "not_calculated",
            "environment_risk": "not_calculated",
        })
        # 定向失效 layered 依赖链（候选/mask → RouteRiskProfile → validations → adoptions →
        # transition validation / Safety Evidence）；不触碰与其无关的 CNS 上游结果。
        self.invalidation.layered_route("workspace_cleared")
        self.session.save()
        return self.snapshot()
