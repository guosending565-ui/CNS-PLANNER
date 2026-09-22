"""Risk, traffic and grid-attribute application use cases."""

from copy import deepcopy

from ..domain.provenance import source_profile
from .project_state import assessment, empty_extension_attribute, empty_grid_attributes


class RiskService:
    MAPPED_ATTRIBUTES = ("population", "terrain")
    DISPLAY_ATTRIBUTES = ("airspace",)
    EXTENSION_ATTRIBUTES = (
        "buildings", "property_exposure", "infrastructure", "towers",
        "traffic", "conflict",
    )

    def __init__(
        self, session, risk_model, traffic_simulator, conflict_detector,
        traffic_grid_service, conflict_grid_service, invalidation, snapshot,
        algorithm_parameters=lambda: {},
    ):
        self.session = session
        self.risk_model = risk_model
        self.traffic_simulator = traffic_simulator
        self.conflict_detector = conflict_detector
        self.traffic_grid_service = traffic_grid_service
        self.conflict_grid_service = conflict_grid_service
        self.invalidation = invalidation
        self.snapshot = snapshot
        self.algorithm_parameters = algorithm_parameters

    def apply_grid_attributes(self, results):
        grid = self.session.state.get("grid")
        if not grid:
            raise ValueError("请先生成工作区标准网格")
        expected_ids = {cell["grid_id"] for cell in grid.get("cells", [])}
        clean, incoming = {}, results or {}
        current = self.session.state.get("grid_attributes") or empty_grid_attributes()
        for kind in self.MAPPED_ATTRIBUTES:
            result = deepcopy(incoming.get(kind))
            self._validate_attribute(kind, result, grid, expected_ids, required=True)
            clean[kind] = result
        for kind in self.DISPLAY_ATTRIBUTES:
            result = deepcopy(incoming.get(kind, current.get(kind)))
            if not isinstance(result, dict):
                result = empty_grid_attributes()[kind]
            self._validate_attribute(kind, result, grid, expected_ids, required=False)
            clean[kind] = result
        for kind in self.EXTENSION_ATTRIBUTES:
            result = deepcopy(incoming.get(kind, current.get(kind)))
            if not isinstance(result, dict):
                result = empty_extension_attribute(kind)
            self._validate_attribute(kind, result, grid, expected_ids, required=False)
            clean[kind] = result
        self.session.state["grid_attributes"] = clean
        profiles = self.session.state.setdefault("data_source_profiles", {})
        for kind in ("population", "terrain", "terrain_dtm"):
            if kind not in clean:
                continue
            if isinstance(clean[kind].get("source_profile"), dict):
                profiles[kind] = deepcopy(clean[kind]["source_profile"])
        self.apply_result(self.risk_model.evaluate(grid, clean, self.algorithm_parameters()))
        self.session.save()
        return self.snapshot()

    def apply_population_grid_attribute(self, result):
        """**仅**替换 ``grid_attributes.population`` 的定向 apply（population-only remap）。

        这是"仅重算人口映射"的落库入口，边界严格：

        * 只写 ``population`` 一个命名空间：``terrain`` / ``buildings`` / ``airspace`` /
          ``traffic`` / ``conflict`` / ``property_exposure`` 等一律原样保留；
        * 不调用 ``set_workspace``，不重新生成 ``grid``，不触碰 ``nodes`` /
          ``scenario_routes`` / ``operational_routes`` / ``workspace``；
        * 仍然验证结果与**当前** ``grid`` 的 level 与 ``grid_id`` 集合一致（``_validate_attribute``），
          级别或 id 不一致直接报错，绝不把别的网格的结果写进当前网格；
        * 人口属性变化按既有 invalidation（``grid_sources(["population"])``）使依赖它的
          Legacy Risk V1 / Risk Framework V2 / layered candidate 等变为 stale —— 与替换人口
          数据源的既有语义完全一致，不新增失效链；
        * 派生缓存 ``_population_shelter_cache`` 一并丢弃，由既有逻辑在下次读取时重建。

        失效传播放在写入**之前**：因此这里绝不会把刚刚写入的新结果又标成 stale。
        提交沿用既有 single-save 契约（调用方用 ``deferred_save`` 折叠为一次 commit）。
        """

        state = self.session.state
        grid = state.get("grid")
        if not grid:
            raise ValueError("请先生成工作区标准网格")
        expected_ids = {cell["grid_id"] for cell in grid.get("cells", [])}
        population = deepcopy(result)
        self._validate_attribute("population", population, grid, expected_ids, required=True)
        self.invalidation.grid_sources(["population"])
        attributes = state.setdefault("grid_attributes", empty_grid_attributes())
        attributes["population"] = population
        profiles = state.setdefault("data_source_profiles", {})
        if isinstance(population.get("source_profile"), dict):
            profiles["population"] = deepcopy(population["source_profile"])
        state.pop("_population_shelter_cache", None)
        self.session.save()
        return self.snapshot()

    def update_source_profiles(self, profiles):
        current = self.session.state.setdefault("data_source_profiles", {})
        for name in ("population", "terrain", "terrain_dtm"):
            if isinstance((profiles or {}).get(name), dict):
                current[name] = source_profile(profiles[name])

    @staticmethod
    def _validate_attribute(kind, result, grid, expected_ids, required):
        if not isinstance(result, dict):
            if required:
                raise ValueError(f"{kind} 网格映射结果缺失")
            return
        cells = result.get("cells") or {}
        if not required and result.get("status") == "not_calculated" and not cells:
            return
        if result.get("grid_level") != grid.get("level"):
            raise ValueError(f"{kind} 网格层级与当前标准网格不一致")
        if set(cells) != expected_ids:
            raise ValueError(f"{kind} 网格属性与当前 grid_id 不一致")

    def evaluate(self, parameters=None):
        state = self.session.state
        if parameters is None:
            parameters = self.algorithm_parameters()
        result = self.risk_model.evaluate(
            state.get("grid"), state.get("grid_attributes") or {}, parameters
        )
        self.apply_result(result)
        self.session.save()
        return self.snapshot()

    def run_traffic_simulation(self, parameters):
        state = self.session.state
        grid = state.get("grid")
        if not grid:
            raise ValueError("请先生成工作区标准网格")
        payload = parameters or {}
        simulation = self.traffic_simulator.simulate(payload.get("simulation") or payload)
        traffic = self.traffic_grid_service.map(grid, simulation, payload.get("traffic") or {})
        detection = self.conflict_detector.detect(
            simulation["trajectories"], payload.get("conflict") or {}
        )
        conflict = self.conflict_grid_service.map(
            grid, detection, simulation["simulation_seconds"],
            payload.get("conflict_grid") or {},
        )
        attributes = state.setdefault("grid_attributes", empty_grid_attributes())
        attributes["traffic"], attributes["conflict"] = traffic, conflict
        state["traffic_simulation"] = {**simulation, "conflict_detection": detection}
        risk_parameters = payload.get("risk")
        if risk_parameters is None:
            risk_parameters = self.algorithm_parameters() or (state.get("grid_risk") or {}).get("parameters")
        self.apply_result(self.risk_model.evaluate(grid, attributes, risk_parameters))
        self.session.save()
        return self.snapshot()

    def apply_result(self, result):
        if not isinstance(result, dict):
            raise ValueError("风险模型未返回有效结果")
        state = self.session.state
        grid = state.get("grid")
        expected_ids = {cell["grid_id"] for cell in (grid or {}).get("cells", [])}
        result_cells = result.get("cells") or {}
        if expected_ids and set(result_cells) != expected_ids:
            raise ValueError("风险结果与当前 grid_id 不一致")
        if any("geometry" in cell for cell in result_cells.values()):
            raise ValueError("风险结果不得复制基础网格 geometry")
        changed = state.get("grid_risk") != result
        state["grid_risk"] = deepcopy(result)
        status = result.get("status", "not_calculated")
        state["result_statuses"]["environment_risk"] = status
        state["risks"]["environment"] = assessment(
            status,
            f"{result.get('algorithm_id', 'risk-model')}@{result.get('algorithm_version', 'unknown')}",
        )
        if changed:
            self.invalidation.grid_risk_routes()

    def invalidate_grid_attributes(self, changed_sources):
        self.invalidation.grid_sources(changed_sources)
