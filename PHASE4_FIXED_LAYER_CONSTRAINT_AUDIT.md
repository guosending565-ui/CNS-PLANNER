# Phase4-B1.1 Fixed-Layer Constraint / Obstacle Architecture Audit

审计基线：Git `HEAD 126f089657dc65ca223a30d34cc44efdf83fa696`。本报告只审计当前工作树中的实现并提出 B3A 文件计划；未修改 production Python/JS、B1 新增代码或 P14 PERFORMANCE PATCH CANDIDATE。目标固定层 `ALT-080` 的含义是 **80 m EGM2008 orthometric**，不是 80 m AGL。

## 1 Current implementation map

| 关注点 | 当前实现 | 当前生效位置 | 审计结论 |
|---|---|---|---|
| 固定高度层 | `domain/altitude_layer_defaults.py` 定义 ALT-060/080/100/150/200；`domain/layered_route.py::resolve_cruise_altitude` 只把 confirmed EGM2008 层直接解析为 canonical 高度 | planning request/readiness、Layered planner、production validation | ALT-080 已是 80 m EGM2008 orthometric；AGL/WGS84 没有显式转换证据会 blocked，基础语义可复用 |
| Risk Field | `application/layered_route_planner_service.py::population_shelter_snapshot`；`risk/accessors_v2.py`；`layered_route_planner/theta_star_v2.py` | Theta* objective 的 `Population × Shelter` soft cost | 已与 terrain/building/tower clearance 分开；继续保持“风险只影响代价” |
| 搜索 feasibility mask | `gis/layered_feasibility_adapter.py` → `layered_route_planner/planner.py::build_layer_feasibility_mask` → `domain/layered_route.py::LayerFeasibilityMask` | terrain/building/tower 的 L8 coarse vertical envelope | 已有 `feasible/blocked/unknown`，缺高度不补 0；应迁移/兼容映射为 canonical `pass/blocked/unknown` Planning Constraint Field |
| Terrain search facts | `fine_environment_adapter.FabdemWindowTerrainSource.sample_cells` 经 `LayeredFeasibilityAdapter` | `terrain cell max + explicit clearance` hard gate | 使用 verified FABDEM/EGM2008、NoData fail-closed；属于 coarse mask，不是 continuous proof |
| Building search facts | `data/mapping/buildings.py`、`gis/building_footprint_aggregation.py` 的 L8 聚合，经 `LayeredFeasibilityAdapter` | `terrain cell max + height_max + confirmed clearance` hard gate | 能保守阻断，但不是 footprint/native ground 验证；`valid_height_fraction < 1` 为 unknown |
| Tower source/derived facts | `reference_data/towers.py`；`gis/tower_obstacle_adapter.py`；`domain/tower_obstacle.py`；`application/tower_obstacle_service.py` | `tower_obstacle_profiles` 进入 coarse mask；同源派生 `tower_colocation_candidates` | 双角色和“不复制源事实”方向已成立；但 search 把 `resolved` 当作足以 hard 判定，缺少独立 confirmed authority gate |
| Generic hard constraints | Theta* `_build_gate` 对 `hard_constraints[*].bbox` 做 cell bbox 相交 | search-time hard gate | 只支持松散 bbox；service 默认读未建模的 `state.route_constraints`，real entry point 不自动注入 GIS `data.hard_constraints`，来源链不闭合 |
| Regulatory | `domain/regulatory_constraints.py`；Theta* LOS 逐段调用 `evaluate_regulatory_intersection` | configured + confirmed geometry 可阻断 LOS；unresolved 相交 fail-closed | 可复用 geometry/vertical-scope/fingerprint 基础；不是完整 airspace/critical-site authority model |
| Airspace | `domain/airspace.py`、`gis/airspace_adapter.py`、`data/mapping/airspace_eligibility.py` | 当前正式 layered path 明确 `display_only`，不进 mask/search/fingerprint | 数据抽取/CRS/geometry health 可复用；现有 eligibility 不能冒充新的 Planning Constraint Field |
| Theta* gate | `theta_star_v2.py::line_of_sight/_build_gate` + `supercover.py` | 每条 LOS 对穿越 cell gate；blocked/unknown 均拒绝扩展 | blocked cells 不可扩展、unknown fail-closed 已实现；适合作为 Constraint Field consumer，不应继续自行拼装多套 constraint semantics |
| Continuous terrain/building | `route_planner_v3/continuous_validators.py` 被 `application/layered_route_validation_service.py` 生产复用；evidence 由 `application/app_context.py` + `gis/fine_environment_adapter.py` 组装 | post-planning production validation / Operational Adoption 前置 | terrain 与 building 已 source-native/real-footprint；tower、regulatory、airspace、critical-site 未连续复核 |
| State / snapshot / sidecar | `application/project_state.py` 保存 candidates/masks/validations/tower profiles；`application/workflow_service.py` 只 slim snapshot；`persistence/project_compaction.py` 外置 `grid_risk_v2.cells` 与 layered candidate items/masks | 保存项目与 UI snapshot | 已有 content-addressed sidecar 可扩展；尚无 `planning_constraint_field` summary/locator，continuous evidence 仍不是统一 artifact contract |

当前 hard constraint 的实际作用层如下：

| Domain | Search mask / LOS | Post continuous/native validation | Operational Adoption 可独立证明 |
|---|---|---|---|
| terrain | 是 | 是 | 是，但须继续保持两层独立 |
| building | 是（coarse L8） | 是（footprint） | 是，但有 height status/MultiPolygon ground 风险 |
| tower | 是（cell bbox envelope） | 否 | 否 |
| payload bbox hard constraint | 是 | 否 | 否 |
| regulatory | 是（configured LOS） | 否 | 否 |
| airspace | 否，display-only | 否 | 否 |
| RestrictedArea / ProtectedSite | 不存在 | 不存在 | 否 |
| departure/arrival obstacle volume | 不在 cruise search | 另有 transition 基础但不是完整 terminal protection volume | 否，独立缺口 |

## 2 Reusable components

- `domain/layered_route.py::resolve_cruise_altitude`：canonical EGM2008 fixed-layer 解析与 fail-closed datum 行为。
- `fine_environment_adapter.FabdemWindowTerrainSource` / `NativeTerrainWindowSource`：只读 FABDEM、EGM2008 metadata、NoData 不补值、coarse/native 两级采样。
- `domain/building_clearance.py::building_roof_elevation/evaluate_vertical_clearance`：唯一 roof 与垂直 margin 公式。
- `domain/building_geometry_quality.py::prepare_footprint_polygons`：Polygon/MultiPolygon part 展开、in-memory `make_valid`、不可修复 geometry 保持 unknown。
- `gis/building_clearance_adapter.py::DtmFootprintSampler`：已有真正 footprint-mask raster sampling，可作为修复 production ground resolution 的参考/抽取来源；不能原样继承其固定 EPSG:32651 假设或 median policy。
- B3 抽取后的 neutral `validate_terrain` / `validate_buildings` 与 `MetricRoute`：可继续作为 continuous/native validator 基础，生产模块不应长期 import research package。
- `domain/tower_obstacle.py`、`domain/tower_colocation.py`、`application/tower_obstacle_service.py`：同一 Tower Source Facts 的双派生骨架、楼面塔 fail-closed、共塔候选与障碍角色分离。
- `domain/regulatory_constraints.py` 的 vertical scope、segment intersection、fingerprint；`gis/airspace_adapter.py` 的 CRS 转换、Polygon/MultiPolygon extraction 和 geometry health。
- `layered_route_planner/supercover.py`、Theta* `line_of_sight`：Constraint Field 的唯一 search consumer 入口；已有 unknown/non-pass 拒绝扩展语义。
- `persistence/project_compaction.py`：content-addressed gzip sidecar、sha256 校验与显式 restore 失败语义，可扩展到 Constraint Field/validation evidence。

复用原则：共享 canonical formulas、schemas 与 pure geometry helpers；search mask 与 continuous validation 不共享 verdict，不允许 continuous validator 读取 mask 的 `pass` 作为证据。

## 3 Missing/unsafe behaviors

1. **缺 canonical Planning Constraint Field。** 当前 `LayerFeasibilityMask` 隶属 candidate collection，domain 只覆盖 terrain/building/tower；没有 environment-derived、per-layer、可独立寻址的 constraint artifact，也没有 airspace/critical-site domain。
2. **正式 hard-constraint source chain 不闭合。** `LayeredRoutePlannerService.evaluate` 默认读取 `state.route_constraints`，但 blank ProjectState 没有该 canonical key；`evaluate-real` 只注入 FABDEM adapter，不注入 `MapData.hard_constraints`。payload bbox 也未形成 RestrictedArea/authority schema。
3. **`validate_buildings` 不消费 `height_status`。** `RouteCorridorBuildingSource` 虽读取并输出 `height_status`，validator 只检查 numeric `height_m` + ground；`unknown/pending/unacceptable` 状态只要数值存在就可能被当作 resolved。
4. **MultiPolygon ground sampling 不完整。** `RouteCorridorBuildingSource.query_route` 保留所有 parts 用于水平/垂直相交，但只把 `parts_metric[0]` 传给 `sample_footprint_ground`；其它受影响 part 没有各自 ground evidence。
5. **当前 production ground sampler 不是 footprint mask。** `_FabdemRasterBase.sample_footprint_ground` 读取代表 ring 的 bbox 并取最大有效值，没有按 polygon/multipolygon mask 筛选；可能吸收 footprint 外高地而过度阻断，也不能提供逐 part/NoData coverage 证明。另一个 `DtmFootprintSampler` 已实现 rasterized footprint mask，形成重复实现。
6. **horizontal CRS 契约不统一。** production `RouteCorridorBuildingSource` 可接收显式 CRS，`app_context` 也要求 payload 提供；但 `QgisGpkgBuildingSource`、`QgisBuildingClearanceAdapter` 仍硬编码 EPSG:32651，前端还提供固定 suggested CRS。离开舟山/UTM 51N 存在错误米制距离风险；必须验证 projected/metre/area of use，而不是只验证 CRS 可构造。
7. **`_FabdemRasterBase` 存在重复方法遮蔽。** `effective_resolution_m` 和 `effective_resolution_detail` 各定义两次；前一版 `effective_resolution_detail` 还会自递归，但当前被后一版静默覆盖。当前运行取最后定义，不等于安全：重构、移动或 backport 可重新暴露递归/行为漂移。
8. **terrain evidence 可静默跳过无 interval pixel。** `validate_terrain` 对缺 `start/end` 的 pixel 直接 `continue`，没有转为 unresolved；evidence adapter 正常应补 interval，但 validator 边界仍不够 fail-closed。
9. **tower 的 resolved 不等于 confirmed。** coarse mask 只检查 profile `status == resolved`。profile 可基于 site-type keyword、源 `height_m` 和 GBA predicted building height形成 top，没有独立 `tower_top_confirmed/evidence authority`；这不满足“只有确认 tower top 才能 hard 判定”。tower 也没有 post continuous/native validator。
10. **regulatory type 语义过宽且模型不足。** 当前 evaluator 对所有 confirmed、相交且垂向命中的 item 都加入 blocked，没有按 `constraint_type` 区分 hard/conditional/advisory；geometry 仅 polygon/bbox，不支持 ProtectedSite point → confirmed protection geometry 的正规转换。
11. **airspace 与 regulatory 并行但未统一。** display-only airspace extraction、unused eligibility、additive regulatory interface、payload bbox hard constraints 是三条独立路径，存在重复 geometry/政策语义；尚无一个由 environment 产生的 canonical constraint artifact。
12. **continuous authority gate 域不完整。** `LayeredRouteValidationService` 只运行 terrain/building，并明确 airspace not applicable；tower、regulatory、critical-site 与 generic hard constraints 只在 search-time 生效。search mask pass 因而可能被误当作它们的最终证据。
13. **building applicability 当前是无条件。** validation readiness 总是要求 confirmed horizontal/vertical policy 与 verified building source，没有 canonical `required/not_applicable + evidence` 决策；这与目标中的显式 applicability contract 不一致。
14. **保存模型只做部分外置。** workflow snapshot 已 slim，但 ProjectState runtime 和 sidecar index 仍以 `layered_route_candidates.masks` 为中心；没有 Constraint Field 独立版本、domain counts、warnings、locator 和 source/policy lineage。
15. **无 critical-site 数据没有正式 warning/gate。** 当前系统无法区分“未提供要地数据”“已确认没有要地”“已有 confirmed protection geometry”，可能被 UI/调用方误读成空约束。

上述 1、3、4、6、9、10、12、15 是 B3A authority blockers；2、5、7、8、11、13、14 是必须同批或前置关闭的安全/架构债务。

## 4 Canonical Constraint Field schema

```json
{
  "schema_version": "planning-constraint-field-v1",
  "artifact_type": "planning_constraint_field",
  "constraint_field_id": "PCF-...",
  "status": "ready | incomplete | stale | failed",
  "derived_from_node": "environment",
  "grid": {"grid_id": "...", "grid_level": "L8", "geometry_crs": "OGC:CRS84"},
  "altitude_layer": {
    "altitude_layer_id": "ALT-080",
    "layer_orthometric_m": 80.0,
    "vertical_reference": "egm2008_orthometric"
  },
  "unknown_policy": {
    "search": "block_search",
    "authority": "block_adoption",
    "source": "production_default",
    "confirmed": true
  },
  "domain_order": ["terrain", "building", "tower", "airspace", "critical_site"],
  "counts": {"pass": 0, "blocked": 0, "unknown": 0},
  "domain_counts": {},
  "source_fingerprints": {},
  "policy_fingerprints": {},
  "input_fingerprint": "...",
  "field_fingerprint": "...",
  "warnings": [],
  "cells_artifact": {"locator": ".cns-results/...", "sha256": "...", "schema_version": 1}
}
```

Artifact 中的 cell 最小形态：

```json
{
  "grid_id": "...",
  "state": "pass | blocked | unknown",
  "dominant_reason_code": null,
  "domain_results": {
    "terrain": {"state": "pass", "required_floor_orthometric_m": 0.0, "evidence_ref": "..."},
    "building": {"state": "unknown", "required_floor_orthometric_m": null, "evidence_ref": "..."},
    "tower": {"state": "pass", "required_floor_orthometric_m": 0.0, "evidence_ref": "..."},
    "airspace": {"state": "pass", "constraint_ids": [], "evidence_ref": "..."},
    "critical_site": {"state": "unknown", "constraint_ids": [], "evidence_ref": "..."}
  }
}
```

合成规则严格为 `blocked > unknown > pass`。`dominant_reason_code` 只用于快速显示，不能删掉其它 domain results。`unknown_policy.search=block_search` 是 production 默认；任何未来显式放行 unknown 的 policy 也只能生成 provisional research/diagnostic result，不能把 cell 改写为 pass，不能通过 adoption。

## 5 fixed-layer obstacle formulas

令 `H = layer_orthometric_m`，所有参与比较的高度都必须是 EGM2008 orthometric metres。

Terrain：

```text
terrain_required_floor_m = terrain_orthometric_m + terrain_vertical_clearance_m
blocked iff H < terrain_required_floor_m
pass iff H >= terrain_required_floor_m
unknown iff terrain height / datum / clearance is unresolved
```

Building：

```text
building_top_orthometric_m = resolved_ground_orthometric_m + building_height_m
building_required_floor_m = building_top_orthometric_m + building_vertical_clearance_m
blocked iff H < building_required_floor_m
pass iff H >= building_required_floor_m
unknown iff ground / height / acceptable height_status / geometry / CRS / clearance is unresolved
```

Tower：

```text
tower_required_floor_m = confirmed_tower_top_orthometric_m + tower_vertical_clearance_m
blocked iff H < tower_required_floor_m within confirmed horizontal protection/clearance geometry
pass iff H >= tower_required_floor_m and all required evidence is confirmed
unknown iff tower top / base datum / rooftop base / mounting semantics / clearance geometry is unresolved
```

边界比较使用严格 `<`；恰好等于 required floor 为 pass。NoData、空字符串、NaN、缺字段和未知 datum 都不能转成 0。coarse cell 应取对该 cell/clearance envelope 保守的 source-native maxima；continuous validation 则在实际 route corridor/native geometry 上重算，不能复用 coarse floor verdict。

## 6 RestrictedArea model

推荐统一 `RestrictedArea` / `ProtectedSite` source-fact contract：

```json
{
  "feature_id": "...",
  "name": "...",
  "category": "...",
  "geometry": {"type": "Point | Polygon | MultiPolygon", "coordinates": []},
  "geometry_crs": "OGC:CRS84",
  "constraint_type": "hard_exclusion | conditional | advisory",
  "lower_altitude_m": null,
  "upper_altitude_m": null,
  "vertical_reference": "egm2008_orthometric | agl | unknown",
  "source": {},
  "evidence": [],
  "confirmed": false,
  "protection_geometry": null,
  "protection_geometry_crs": null,
  "protection_basis": null
}
```

规则：

- source geometry 可以是 point/polygon/multipolygon；Constraint Field 只消费最终 confirmed `protection_geometry`。
- point 不自带半径。只有 source 明示范围或显式 confirmed policy 才能生成 protection geometry；不得按 `category` 猜 radius。
- confirmed `hard_exclusion` 且 fixed layer 落入已解析垂向范围时，intersecting cells 直接 blocked。
- `conditional` 必须交给具名 policy resolver；未解析条件为 unknown，不得自动 blocked 或 pass。`advisory` 进入 warning/evidence，不进入 hard mask，除非另有显式 policy 提升。
- dataset state 至少区分 `not_provided / confirmed_none / confirmed_present / unresolved`。`not_provided` 可在 policy 允许时生成 provisional candidate，但必须 `critical_site_not_evaluated`；Operational Adoption 是否 blocked 由 airspace/regulatory authority policy 明确决定。
- `confirmed_none` 只能来自有 authority/source/evidence 的显式声明，不能从空 items 推断。

## 7 tower dual-role model

```text
Tower Source Facts (single authoritative collection)
  ├──→ tower_obstacle_profiles
  │      → Step 2 environment / Planning Constraint Field
  │      → Step 3 search + independent continuous tower validation
  └──→ tower_colocation_candidates
         → Step 5 CNS facility planning
```

`Tower Source Facts` 不复制到两个输入集合；两个派生物都记录 `tower_source_fingerprint`、`tower_id` 和自身 policy fingerprint。`tower_obstacle_profiles.status=resolved` 只说明数学上可算，不能等同 authority confirmed。B3A 应增加显式 `tower_top_status = confirmed | unresolved`、`tower_top_orthometric_m`、base type/datum/building-base evidence、confirmed-by/source/evidence lineage。

地面塔可在 terrain EGM2008 + confirmed structure height + confirmed base semantics 全部成立时 confirmed；楼面塔还必须有 confirmed ground/building-top/base relation。仅有 source `elevation_m`、关键词分类、predicted building height、未知 mounting point 或模糊“楼面/基站”语义时保持 unknown。共塔 candidate 可以继续存在，但 `physical_mount_confirmed=false`，不得反向提高障碍物 profile 的确认等级。

## 8 search vs continuous validation contract

| Contract | A. Search feasibility mask | B. Continuous/native validation |
|---|---|---|
| 目的 | 快速剪枝、禁止明显不可行 cell/LOS | 为 Operational Adoption 提供最终 route-corridor authority evidence |
| 几何 | grid cell + conservative envelope + supercover | 实际 route centreline/corridor、native terrain pixels、真实 footprint/tower/protection geometry |
| 输入 | Planning Constraint Field artifact | 原始/权威 source + policy + route geometry；只引用 search lineage，不读取 search verdict 作为证据 |
| unknown | production 默认不可扩展 | 任何适用域 unknown 阻断 adoption |
| blocked/failed | blocked cell 不可扩展 | penetration/violation 为 failed，阻断 adoption |
| pass | 只表示该离散搜索单元可扩展 | 各适用域独立 passed，才可能 authority-eligible |
| 指纹 | field/source/policy/layer/grid | candidate/path/source/policy/validator/CRS；与 search fingerprint 分开 |

Adoption contract：search field current 且 path 未穿越 blocked/unknown；continuous terrain/building/tower/airspace/critical-site 各 applicable domain passed；任何 stale、unknown、failed、fingerprint mismatch 或 search/continuous disagreement 均 fail-closed。连续验证可以复用 canonical formula/helper，但必须重新读取 native evidence 并独立计算结果。

## 9 terminal procedure deferred scope

本 B3A 只实现 fixed-cruise altitude constraint。起飞、爬升、下降、进近和着陆的障碍保护另定义为 `Departure/Arrival Procedure Validation`：未来消费 terminal protection volume、climb/descent trajectory、terrain/building/tower penetration 和程序 clearance policy。

当前 `vertical_transition_validation_service.py`、`domain/vertical_transition_validation.py`、`route_3d_profile_service.py` 可作为证据/几何基础，但不能被描述为完整 terminal procedure authority。不得把 terminal volume 塞入 Theta* fixed-layer mask；也不得用 cruise pass 推导 departure/arrival pass。

## 10 implementation/file plan

B3A 必须位于 B3 neutral dependency extraction 之后、B4 frontend convergence 之前。

建议新增：

- `cns_planner/domain/planning_constraint_field.py`：schema、三态、domain composition、fingerprints、summary。
- `cns_planner/domain/restricted_area.py`：RestrictedArea/ProtectedSite、dataset knowledge state、protection geometry contract。
- `cns_planner/application/planning_constraint_field_service.py`：environment-derived per-layer materialization、readiness、staleness、artifact publication。
- `cns_planner/gis/planning_constraint_field_adapter.py`：terrain/building/tower/airspace/critical-site 事实转 canonical domain evidence；不放业务 verdict 到 GIS boundary。
- B3 抽取目标（名称可在 B3 固化）`cns_planner/validation/continuous_validators.py`：neutral production validators；B3A 增加 tower/protection geometry validators。

建议修改：

- `cns_planner/domain/layered_route.py`、`layered_route_planner/planner.py`：把旧 mask 兼容读取映射到 Constraint Field，停止产生第二套 formula。
- `cns_planner/layered_route_planner/theta_star_v2.py`：只消费 canonical field/gate；Risk Field 继续只做 soft cost。
- `cns_planner/application/layered_route_planner_service.py`、`application/app_context.py`：闭合 source chain、按 field ID/fingerprint 调用，不从 payload/state 临时拼 bbox hard constraints。
- `cns_planner/gis/fine_environment_adapter.py`：删除 `_FabdemRasterBase` 重复定义；统一显式 metric CRS 验证；逐 MultiPolygon part/footprint-mask ground sampling；保留 NoData fail-closed。
- `cns_planner/gis/building_clearance_adapter.py`、`domain/building_clearance.py`：抽取唯一 footprint ground resolver，消费 height status，移除固定 EPSG:32651 假设。
- `cns_planner/domain/tower_obstacle.py`、`gis/tower_obstacle_adapter.py`、`application/tower_obstacle_service.py`：区分 resolved/confirmed，补 authority lineage 与 continuous evidence。
- `cns_planner/domain/regulatory_constraints.py`、`domain/airspace.py`、`gis/airspace_adapter.py`、`data/mapping/airspace_eligibility.py`：统一到 RestrictedArea/protection geometry adapter，保留 display-only 视图但不让它冒充 authority。
- `cns_planner/application/layered_route_validation_service.py`、`domain/layered_route_validation.py`：加入 tower/airspace/critical-site independent domains 与 applicability，保留 search/continuous 两套指纹。
- `cns_planner/application/project_state.py`、`application/workflow_service.py`、`application/invalidation_service.py`：只存 field/validation summary、active IDs、fingerprints、warnings、locator；建立声明式 invalidation edges。
- `cns_planner/persistence/project_compaction.py`、`application/project_directory_service.py`：Constraint Field cells 与 continuous evidence content-addressed sidecar、校验/恢复/Save As。
- `cns_planner/api/router.py`、`algorithms/registry.py`：read/evaluate API、manifest 输入契约与 exactly-one writer guard；B4 再接 UI。

不得修改 P14 的四个候选文件；不得在 B3A 新增 canonical workflow node、改 B1 assumptions/existing CNS、绕过 production writer authority，或实现 terminal protection volume。

## 11 tests/acceptance criteria

1. ALT-080 解析为 `80.0 + egm2008_orthometric`；AGL/ellipsoidal/unknown 无显式转换证据不得进入 field。
2. Terrain table tests：`H < floor` blocked、`H == floor` pass、缺 height/datum/clearance unknown；NoData/NaN/None 永不变 0。
3. Building table tests：ground + height + clearance；消费 `height_status`；Polygon/MultiPolygon 每个受影响 part 独立 ground sample；footprint-mask NoData coverage；invalid/unrepairable geometry unknown。
4. CRS tests：自动/显式选择适用 projected metre CRS；非 metre、错误 area-of-use、未知 source CRS fail explicit；非舟山 fixture 证明不依赖 EPSG:32651。
5. `_FabdemRasterBase` 只有一组 resolution methods；projected unit conversion 与 geographic geodesic resolution 回归通过；缺 interval native pixel 为 unresolved。
6. Tower tests：同一 source fingerprint 双派生；resolved-but-unconfirmed、rooftop/base/mounting ambiguous 为 unknown；confirmed top 才能 blocked/pass；continuous tower validator 可发现 search mask 后的 route-corridor violation。
7. RestrictedArea tests：point/polygon/multipolygon；point 无 source/policy radius 不生成 protection geometry；confirmed hard exclusion blocked；conditional unresolved unknown；advisory 不自动 hard block；空 items 不自动 confirmed_none。
8. Theta* tests：blocked 不扩展；unknown 默认不扩展；不存在 unknown→pass；Risk Field 数值变化只改 soft objective，Constraint Field verdict 不进入 risk cost。
9. Search-vs-continuous tests：两层分别计算与分别指纹；mask pass 不能替代 native validation；任一 applicable domain unknown/failed、stale 或 mismatch 阻断 adoption。
10. Persistence tests：Constraint Field/continuous details 不内联 ProjectState/workflow snapshot；locator + sha256 restore；sidecar 缺失/损坏/schema mismatch fail explicit；Save As 保持可达。
11. Critical-site no-data tests：只在显式 policy 允许时生成 provisional candidate，必须带 `critical_site_not_evaluated`；UI/API/报告不得表述“无禁飞区”。
12. Regression tests：既有 terrain/building/tower mask、Theta* golden、route risk profile、layered validation/adoption 在兼容输入下语义不回退；B3A 不触碰 terminal validation verdict、六步 workflow、13-node DAG、B1 authority contracts 或 P14 patch files。
