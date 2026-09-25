# Phase4-B3X 验收报告：Neutral Infrastructure Extraction + Multi-Altitude Planning Constraint Field

生成时间：2026-09（本地会话）
HEAD：`c7b767ebbf1b55e56433f8d402387b03dc691522`（起点与终点一致，未 commit / 未 push）
工作树：全部改动留在工作树，未提交。P14 性能补丁候选四文件未被本批次触碰。

---

## 1. 结论速览

| 项 | 结果 |
|---|---|
| B3X_READY_TO_REVIEW | **true** |
| 定向测试（B3X 组合） | 284 passed / 1 skipped |
| full pytest | 第 1 次 1767 passed / 2 failed → 修复后第 2 次 **1769 passed / 7 skipped**（共 2 次，符合 §20） |
| P14 四文件 | 未修改（mtime 早于本批次所有 B3X 文件；SHA-256 见 §11） |
| 舟山 ALT-080 golden-case | 已用真实数据在**沙箱**中生成（24,300 格）；未写入用户工程 |

---

## 2. 交付物（修改 / 新增文件）

### 2.1 新增 production-neutral 模块

| 文件 | 作用 |
|---|---|
| `cns_planner/planning/grid_graph.py` | GridGraph 唯一实现（adjacency / center / diagonal guard / grid_id 解析） |
| `cns_planner/planning/__init__.py` | 再导出 `GridGraph` |
| `cns_planner/validation/continuous_validators.py` | 连续验证器唯一实现（terrain / building / tower / restricted area / airspace / geometry / kinematics） |
| `cns_planner/validation/continuous_contracts.py` | 连续验证契约（violation / interval / fingerprint 组件） |
| `cns_planner/validation/__init__.py` | 再导出 |
| `cns_planner/domain/planning_constraint_field.py` | 三态约束场契约 + 指纹 |
| `cns_planner/domain/restricted_area.py` | RestrictedArea / ProtectedSite 契约 |
| `cns_planner/gis/metric_crs.py` | 唯一米制 CRS resolver |
| `cns_planner/gis/planning_constraint_field_adapter.py` | 约束场 GIS 适配（点/面 → cell、连续验证证据投影） |
| `cns_planner/application/planning_constraint_field_service.py` | 生成 service + 持久化入口 |
| `tests/test_phase4_b3x_planning_constraints.py` | B3X 回归测试（22 项） |

### 2.2 修改（关键）

```text
cns_planner/api/router.py                                  # POST /api/planning-constraint-fields/evaluate
cns_planner/application/app_context.py                     # tower / restricted-area 连续验证真实证据
cns_planner/application/invalidation_service.py            # 约束场失效边
cns_planner/application/layered_operational_adoption_service.py
cns_planner/application/layered_route_planner_service.py   # 把约束场交给 Theta* V2
cns_planner/application/layered_route_validation_service.py# tower / restricted area 连续验证接入
cns_planner/application/project_state.py                   # 新节点 + 状态流
cns_planner/application/tower_obstacle_service.py
cns_planner/application/v3_operational_adoption_service.py
cns_planner/application/vertical_transition_validation_service.py
cns_planner/application/workflow_service.py
cns_planner/domain/building_clearance.py                   # height_status 判定语义
cns_planner/domain/layered_route.py                        # tower_top_not_confirmed 原因码 + 语义声明
cns_planner/domain/layered_route_validation.py
cns_planner/domain/tower_obstacle.py                       # resolved ≠ confirmed
cns_planner/domain/vertical_transition_validation.py
cns_planner/gis/building_clearance_adapter.py
cns_planner/gis/fine_environment_adapter.py                # _FabdemRasterBase 去重
cns_planner/gis/layered_feasibility_adapter.py             # tower fact confirmed 语义
cns_planner/layered_route_planner/planner.py               # 读 neutral GridGraph + tower confirmed 判定
cns_planner/layered_route_planner/theta_star_v2.py         # 读 neutral GridGraph + 约束场 gate
cns_planner/persistence/project_compaction.py              # 约束场 cell 走既有 sidecar
cns_planner/risk/route_profile.py                          # 读 neutral GridGraph
cns_planner/route_planner/risk_aware_v2.py                 # 删本地 GridGraph，改读 neutral
cns_planner/route_planner_v3/continuous_contracts.py       # 瘦身为兼容 re-export
cns_planner/route_planner_v3/continuous_validators.py      # 瘦身为兼容 re-export
cns_planner/route_planner_v3/fine_grid.py
tests/test_bug_tower_vref_001_terrain_vertical_reference_casing.py
tests/test_towers_operational_integration_v2.py
tests/test_layered_route_validation_adoption.py
tests/test_route_planner_v3_continuous.py
```

**未触碰**（依约保护）：

* `cns_planner/algorithms/corridor/v1.py`、`algorithms/coverage/geometric_3d.py`、
  `algorithms/service_capability/v1.py`、`tests/test_cns_corridor.py`（P14 候选四文件）；
* `_dsh_prof/`（仅新增 `_dsh_prof/b3x/` 验收脚本，未改动既有内容）；
* 舟山旧检查临时 tools、qgis 临时文件、乱码临时文件；未 commit、未 push。

---

## 3. Stage A — GridGraph 收敛

* 新位置：`cns_planner/planning/grid_graph.py`（唯一 `class GridGraph` 定义）。
* 原定义已从 `cns_planner/route_planner/risk_aware_v2.py` 删除，改为 `from ..planning.grid_graph import GridGraph`。
* 全部消费方直接读 neutral 模块：

```text
cns_planner/route_planner/risk_aware_v2.py:15        (兼容 planner，再导出)
cns_planner/layered_route_planner/planner.py:44      (production V1)
cns_planner/layered_route_planner/theta_star_v2.py:72(production Theta* V2)
cns_planner/risk/route_profile.py:31                 (风险剖面)
```

* `production → archive planner` 的**反向 import 已不存在**：唯一仍 import 兼容 planner 的是
  `cns_planner/algorithms/registry.py`（注册表按算法 id 列出实现，属于既有注册语义，不是 GridGraph 依赖）。
* adjacency / coordinate mapping / neighbor semantics / supercover 输入语义全部保持；
  `GridGraph` 只含结构与查表，不含任何 route objective。
* 一致性问题由 `test_neutral_grid_graph_is_the_single_definition`（`is` 同一性）与既有
  planner characterization 测试共同锁定。

## 4. Stage B — 连续验证器收敛

* 新位置：`cns_planner/validation/continuous_validators.py`（1,392 行）与
  `cns_planner/validation/continuous_contracts.py`。
* `cns_planner/route_planner_v3/continuous_validators.py` / `continuous_contracts.py` 已瘦身为
  `from ..validation.xxx import *` 的**薄兼容 re-export**（V3 Research 与旧 import path 继续可用）。
* production 侧 canonical import：`application/layered_route_validation_service.py`、
  `application/project_state.py`、`application/v3_operational_adoption_service.py`、
  `domain/vertical_transition_validation.py` 全部 import `..validation.*`，不再 import V3 Research。
* 抽取未改变公式 / output schema / evidence 语义：由 `test_neutral_continuous_validator_is_the_canonical_implementation`
  以及既有 `test_route_planner_v3_continuous.py`、`test_layered_route_validation_adoption.py` 锁定。

## 5. 已确认的 5 个 obstacle 基础问题

| # | 问题 | 修复 |
|---|---|---|
| 1 | `validate_buildings` 不消费 `height_status` | 逐 footprint 判定：未解析的高度状态 ⇒ `building_height_status_unresolved`（unresolved，绝不 pass）；只有 `accepted/confirmed/measured/predicted/resolved/valid/verified` 才形成 roof |
| 2 | MultiPolygon 地面采样不完整 | 每个 polygon part 单独参与 route/buffer 距离与地面判断（`ground_elevation_by_part_egm2008_m[part_index]`），不再只取最大/第一个 part |
| 3 | metric CRS 硬编码 `EPSG:32651` | 新增唯一 `gis/metric_crs.py`：显式工程 CRS 优先（必须 projected + metre + 覆盖范围），否则仅当范围有限、紧凑且落在同一 UTM 带内才推导；无法确定 ⇒ `status=unknown`，绝不回退 32651 |
| 4 | `_FabdemRasterBase` 重复定义/自递归 | 现只保留单一实现；`test_fabdem_base_has_one_non_recursive_resolution_implementation` 断言 `effective_resolution_m` / `effective_resolution_detail` 各只定义一次且无自递归 |
| 5 | tower `resolved` 与 `confirmed` 混淆 | `TowerObstacleProfile` 新增 `confirmed` / `tower_top_status` / `confirmation_authority`；`resolved` 只是算法解析状态，未确认时 `tower_top_status="resolved_unconfirmed"`，并产生 `tower_obstacle_resolved_unconfirmed:<id>` warning |

---

## 6. Planning Constraint Field schema（`domain/planning_constraint_field.py`）

```text
schema_version = 1
field_id                 PCF-<altitude_layer_id>-<fingerprint 尾 16 位>
status                   completed | completed_with_warnings | not_calculated | stale
altitude_layer_id        任意 id（无枚举依赖）
nominal_altitude_m       number
vertical_reference       egm2008_orthometric
workspace_identity       {workspace_id, revision, bbox}
grid_identity            pcf-grid-<sha256>(cell id/bbox/level 排序后)
policy_fingerprint       pcf-policy-<sha256>(净空 + conditional policy ids + unknown policy)
source_fingerprints      {terrain_dtm, buildings, towers, restricted_areas}
constraint_field_fingerprint  pcf-<sha256>(层身份 + workspace + grid + policy + sources + cells)
unknown_policy           {allow_unknown_for_provisional, unknown_remains_unknown, operational_adoption_allowed=false}
counts                   {total, pass, blocked, unknown, blocked_by{terrain,building,tower,airspace,critical_site}}
warnings                 [{reason, unknown_constraint_count, provisional_traversal_allowed}]
artifact_ref             内容寻址 sidecar 引用（cell 明细不入 ProjectState）
cells                    [{grid_id, outcome, blocked_by[], unknown_reasons[], evidence_refs[]}]
risk_field_used = false / hard_constraints_are_not_risk_cost = true
```

* 三态严格：`blocked` 永不可通过；`pass` 可规划；`unknown` **绝不**自动等于 pass。
* 一个 cell 可同时有多个 blocker（`blocked_by` 是集合）。

## 7. RestrictedArea / ProtectedSite schema（`domain/restricted_area.py`）

```text
feature_id, name, category, domain(airspace|critical_site)
geometry (Point|Polygon|MultiPolygon GeoJSON) + geometry_crs
protection_geometry (Polygon|MultiPolygon|null)
planning_geometry  = protection_geometry ?? （Polygon/MultiPolygon 源几何）?? null
geometry_status    resolved | source_point_only_no_protection_geometry | unknown
constraint_type    hard_exclusion | conditional | advisory
lower_altitude_m / upper_altitude_m / vertical_reference / vertical_unbounded
vertical_status    resolved | unknown
source, evidence, confirmed（confirmed 需 source + evidence）
```

* `category="airport"` / `"bridge"` **不会**自动生成保护半径；没有 protection geometry 就只有 source point，
  `vertical_status` 不明时 `altitude_applicability` 返回 `unknown`（不默认"全高度禁飞"）。
* 领域数据集三态：`not_provided | confirmed_none | confirmed_present | unresolved`；
  **空列表不会被提升为 confirmed_none**，且 `confirmed_none/confirmed_present` 必须有 source + evidence。

## 8. per-altitude-layer 生成逻辑

* 纯函数入口：`generate_planning_constraint_field(altitude_layer, grid, terrain_by_cell, buildings_by_cell, tower_obstacle_profiles, restricted_areas, policies, source_fingerprints, workspace_identity)`；
  签名**不接受任何风险评分**（Risk Field / Constraint Field 彻底分离）。
* 逐格判定：
  * terrain：`H < terrain_orthometric + clearance` ⇒ blocked；证据/净空/垂直基准未解析 ⇒ unknown（NoData 绝不当 0）。
  * building：`H < ground + height + clearance` ⇒ blocked；缺 ground / height / 有效 height_status ⇒ unknown；
    `building_count == 0` ⇒ 无垂直建筑约束；`ground` 可由地形回填（与既有 L8 建筑网格语义一致）。
  * tower：`confirmed` 才可能 blocked；`resolved_unconfirmed`/未解析 ⇒ unknown。
  * airspace / critical_site：仅 confirmed + 垂直范围适用 + 几何相交的 `hard_exclusion` 才 blocked；
    `conditional` 需显式 policy 列出 feature id，否则 unknown；`advisory` 永不 hard block。
* 高度层校验：`_altitude_layer` 要求显式 `altitude_layer_id` / `nominal_altitude_m` / `vertical_reference`，
  且层必须 confirmed；`next(...)` 选中层与请求层不一致时 Theta* 直接 `constraint_field_altitude_layer_mismatch`。

## 9. unknown policy

```text
allow_unknown_for_provisional  显式开关（默认 false）
unknown_remains_unknown        true
operational_adoption_allowed   false
```

* candidate 可在显式 policy 下穿越 unknown，但必须：`completion_status = completed_with_warnings`、
  `unknown_constraint_count > 0`、带 reason/evidence；maturity 永远 provisional。
* 经过 unknown 的候选**不能**获得 Operational Adoption：`LayeredOperationalAdoptionService._gate` 增加
  `candidate_traverses_unknown_constraints` 拒绝理由。
* 不存在 `unknown → pass` 的状态转换；正式发布前的连续验证必须把 REQUIRED 域解析为 passed/failed。

## 10. Theta* V2 接入

* `LayeredRiskAwareThetaStarV2.plan(..., constraint_field=..., unknown_constraint_policy=...)`。
* `_build_gate` 在有显式约束场时**以约束场为可行性权威**（不再二次套用 legacy mask，避免把允许的 provisional unknown 又变 blocked）；
* blocked cell：不可扩展、supercover LOS 穿越即拒绝、LOS rewiring 不成立；
* any-angle / LOS rewiring / supercover / objective 公式（risk 0.8 / turn 0.1 / distance 0.1）**未改动**；
* 约束场只增加 feasibility 输入，obstacle 不进入 risk cost。
* 版本/指纹：`algorithm_id` 未改名；candidate fingerprint 组件包含
  `planning_constraint_field_fingerprint` + `unknown_constraint_policy`（外加既有 layer/grid/objective/policy 组件）。
  全部 pass 且无新增 obstacle 时，path / optimization_cost / turn_statistics / distance 与无字段时完全一致，
  但 candidate fingerprint 必然变化。

## 11. continuous validation 新增内容

* `validate_towers`：以**真实塔点几何**（`point_metric`）与路线 buffer 做水平判定；塔顶 confirmed 才做垂向净空判定；
  影响 route 但塔顶未确认 ⇒ `outcome=unknown` 并阻止 authority gate。
* `validate_restricted_areas`：confirmed `hard_exclusion` 与正式路线在适用高度相交 ⇒ `failed`（不是 risk penalty）；
  `advisory` 不 block；未确认/垂直范围不明 ⇒ unresolved。
* 独立性声明：`independent_from_search_constraint_field = true`、
  `search_constraint_field_is_not_authority_evidence = true`（search feasibility 与 continuous authority 是两份独立证据，
  共享 source facts / policy 语义但不共享结论）。
* 真实源接线：`app_context._continuous_evidence_adapter` 注入 tower evidence（含 `point_metric`）与
  restricted-area protection geometry 的米制投影；无 confirmed 源时如实 `unresolved`。

## 12. artifact / 存储

* 复用既有内容寻址 sidecar（`persistence/project_compaction.py`，`.cns-results/<sha256>.json.gz`）：
  `planning_constraint_field_cells` 按 `field_id` 存入 sidecar；`result_index.planning_constraint_fields`
  只记 status / count / field_count / cell_count / fingerprints。
* `ProjectState` 实际保存：`planning_constraint_fields.{schema_version,status,count,items[]}`，
  其中每个 item 只保留层身份、status、counts、warnings、unknown_policy、policy/source/grid 指纹与 artifact_ref，
  **不含 cells**（`test_service_persists_summary_only_in_project_json_and_returns_artifact_ref` 断言）。
* 未新建第二套大型持久化框架；B5 前置缺口：restricted-area / tower 的 confirmed 源尚未进入项目数据源注册表。

## 13. 失效（invalidation）

* `planning_constraint_field(reason)`：约束场自身 stale（状态 + `result_statuses`），并沿 `layered_route` 传播。
* 触发边：`workspace/spatial_3d` 变化、`terrain_dtm/buildings` 源变化、`airspace` 源变化、
  `building_clearance_policy_changed`、`tower_clearance_policy_changed`、
  `layered_route_feasibility_policy_changed`、`regulatory_constraints_changed`、约束场指纹变化。
* 链条：constraint field stale → route candidate stale → route validation → operational route → 下游；
  只补新节点所需的最小失效边，未重写整套引擎。

## 14. API

* `POST /api/planning-constraint-fields/evaluate`（`workflow.generate_planning_constraint_field(payload)`，payload 带 `altitude_layer_id`）。
* `GET` 摘要走既有 workflow snapshot 通道（`workflow.planning_constraint_fields(altitude_layer_id)`）。
* 无 `/80m-obstacle-layer` 之类写死 endpoint；HTTP 默认只返回 summary / artifact_ref / status / counts / warnings，不回传全部 cells。

---

## 15. 测试

### 15.1 B3X 定向组合（一次）

```text
pytest -q tests/test_phase4_b3x_planning_constraints.py \
         tests/test_layered_route_validation_adoption.py \
         tests/test_layered_theta_star_v2.py tests/test_layered_route_planner.py \
         tests/test_layered_planner_baseline.py tests/test_route_planner_v3_continuous.py \
         tests/test_building_level_independent_facts.py tests/test_fabdem_footprint_ground_sampling.py \
         tests/test_risk_aware_route_planner_v2.py
→ 284 passed, 1 skipped
```

### 15.2 §19 关键回归覆盖对照

| # | 要求 | 覆盖 |
|---|---|---|
| 1 | GridGraph 抽取前后一致 | `test_neutral_grid_graph_is_the_single_definition` + 既有 planner characterization |
| 2 | 连续验证器抽取前后一致 | `test_neutral_continuous_validator_is_the_canonical_implementation` + V3 characterization |
| 3 | height_status unknown 不 pass | `test_building_height_status_unknown_never_passes` |
| 4 | MultiPolygon 后续 part 参与判定 | `test_multipolygon_later_part_ground_can_fail_clearance` |
| 5 | 非舟山 metric CRS 不用 32651 | `test_metric_crs_resolver_is_extent_aware_and_never_falls_back_to_32651` |
| 6 | `_FabdemRasterBase` 无 recursion/shadowing | `test_fabdem_base_has_one_non_recursive_resolution_implementation` |
| 7 | resolved 未确认塔 ⇒ unknown | `test_resolved_but_unconfirmed_tower_is_unknown_not_blocked_or_passed` |
| 8 | confirmed 塔可 block | `test_confirmed_tower_blocks_field_and_continuous_route` |
| 9 | 无保护几何的点不产生禁飞圆 | `test_restricted_point_without_protection_geometry_does_not_invent_radius` + `test_empty_restricted_list_is_not_silently_promoted_to_confirmed_none` |
| 10 | confirmed hard polygon 可 block | `test_confirmed_hard_polygon_blocks_field_and_independent_continuous_validation` |
| 11 | advisory 不 hard block | `test_advisory_polygon_never_hard_blocks` |
| 12 | Terrain NoData ≠ 0 | `test_terrain_nodata_is_unknown_never_zero` |
| 13 | blocked cell 不可穿越 | `test_theta_star_and_supercover_never_cross_blocked_constraint_cells` |
| 14 | supercover/LOS 不穿 blocked cell | `test_los_traversal_never_crosses_a_blocked_or_uncovered_cell`（blocked / unknown / provisional 三态） |
| 15 | unknown provisional 有 warning | `test_unknown_provisional_policy_warns_and_operational_adoption_rejects` |
| 16 | unknown candidate 不能 Operational Adopt | 同上（`candidate_traverses_unknown_constraints`） |
| 17 | all-pass fixture 与旧 characterization 一致 | `test_all_pass_field_preserves_path_cost_but_changes_candidate_fingerprint` |
| 18 | ALT-060/080/100/自定义层参数化 | `test_multiple_and_custom_altitude_layers_are_parameterized_and_fingerprinted`（含 ALT-135） |
| 19 | constraint field 改变 ⇒ candidate fingerprint 改变 | `test_all_pass_field_preserves_path_cost_but_changes_candidate_fingerprint` |
| 20 | altitude layer 改变 ⇒ constraint fingerprint 改变 | `test_altitude_layer_change_changes_constraint_field_fingerprint_and_outcome` |

### 15.3 full pytest

```text
第 1 次：2 failed, 1767 passed, 7 skipped in 727.05s
  失败项（与本批次 tower 语义直接相关，属本轮 regression）：
    tests/test_bug_tower_vref_001_terrain_vertical_reference_casing.py::test_collection_only_ground_and_rooftop_towers_become_resolved
    tests/test_towers_operational_integration_v2.py::test_tower_facts_use_the_source_point_geometry_and_horizontal_clearance
第 2 次（修复 + 全部生产代码改动之后）：1769 passed, 7 skipped in 709.46s
```

第 2 次是 §20 允许的最后一次 full pytest：它已覆盖本批次**全部生产代码变更**
（tower 语义修复、`FEASIBILITY_REASON_CODES`、planner 塔分支、`domain/layered_route.py`、`gis/layered_feasibility_adapter.py`）。
第 2 次启动之后只再改了**测试文件与文档**（新增 3 个 B3X 用例 + 2 处旧断言更新 + 本报告），
这些测试文件由下方向导组合单独验证：

```text
pytest -q tests/test_phase4_b3x_planning_constraints.py tests/test_towers_operational_integration_v2.py \
         tests/test_bug_tower_vref_001_terrain_vertical_reference_casing.py tests/test_towers_real_data.py \
         tests/test_layered_route_planner.py tests/test_layered_theta_star_v2.py \
         tests/test_layered_route_validation_adoption.py tests/test_building_environment_clearance.py \
         tests/test_building_geometry_quality.py
→ 263 passed
```

两处失败的根因与修复（B3X §6.5 要求 `resolved ≠ confirmed`，但当时把"数值语义"和"权威语义"混在了一起）：

1. `gis/layered_feasibility_adapter.py` 把 `resolved` 定义成 `status == resolved and confirmed is True`，
   导致未确认塔的**已解析塔顶数值**也不输出 —— 位置索引测试（与确认状态无关）随之失败。
   修复：`resolved` 恢复为纯数值语义；新增 `confirmed` / `confirmed_count` / `unconfirmed_tower_ids`；
   未确认时 `data_status="unknown"`、`reason="tower_top_not_confirmed"`（数值只作诊断）。
2. `layered_route_planner/planner.py` 的塔净空分支新增未确认原因码（`tower_top_not_confirmed`，
   已加入 `FEASIBILITY_REASON_CODES` 与 `COARSE_ENVELOPE_SEMANTICS`），未确认塔顶仍然 fail-closed（该格 unknown），
   绝不产生 hard block。
3. 两处旧断言按新语义更新（warning 列表新增 `tower_obstacle_resolved_unconfirmed:` 条目；
   tower fact 断言改为"数值 145.0 保留 + data_status unknown + reason tower_top_not_confirmed"）。

### 15.4 frontend tests

未运行。本批次未修改任何前端（JS/CSS/HTML）文件，依 §20 不跑 frontend suite。

---

## 16. 舟山 R0003 / ALT-080 golden-case

### 16.1 前置事实（用户工程当前状态）

`projects/current_project.json` 当前是**未初始化工程**：

```text
grid.cells = 0            workspace = null
spatial_3d.altitude_layers = []            grid_attributes(towers/terrain/buildings) = not_calculated
layered_route_feasibility_policy.status = "blocked"（terrain_vertical_clearance_m = null，"no_default_clearance"）
building_clearance_policy.status = "pending_confirmation"（horizontal/vertical = null）
tower_clearance_policy.status = "not_configured"           tower_obstacle_profiles.status = "not_calculated"
planning_constraint_fields = null
projects/map_sources.json 无 towers 键；无 airspace/要地数据源
```

因此 **无法**在用户当前工程上直接生成 ALT-080 约束场：工作区网格、高度层目录与三个净空阈值都未确认，
按项目语义（禁止安全默认值）这属于设计性阻断，不是代码缺陷。

### 16.2 实际做法：沙箱真实数据验收（不写用户工程）

脚本：`_dsh_prof/b3x/zhoushan_alt080_golden_case.py`（只读真实源 + 纯函数生成，输出到 `_dsh_prof/b3x/`）

```text
sandbox 工作区（显式给定）  122.10–122.30 E / 29.95–30.10 N ⇒ L8 恰好 24,300 格（180 × 135）
  （先试过 121.90–122.45 / 29.70–30.15，L8 需 18 万格 > max_cells=60000，被既有资源守卫显式 blocked）
地形   Zhoushan_FABDEM_DTM_30m.tif（EGM2008 正高，逐格只读 window，NoData 保持 unknown）
建筑   zhoushan_building_grid_L8.gpkg（L8 预聚合事实表；9 位小数 bbox 匹配 + 覆盖范围外 missing_data）
铁塔   项目未注册 towers 源 ⇒ not_provided（不伪造 confirmed）
要地/空域 无 confirmed 源 ⇒ unresolved（不猜机场保护半径）
```

### 16.3 结果 A：只用项目已有默认值（terrain/tower 净空无默认值 ⇒ null）

```text
altitude_layer_id   ALT-080
nominal_altitude_m  80.0
vertical_reference  egm2008_orthometric
grid_level          8
total / pass / blocked / unknown = 24300 / 0 / 0 / 24300
blocked_by          terrain 0, building 0, tower 0, airspace 0, critical_site 0
status              completed_with_warnings
fingerprint         pcf-215b07d26c0e074b088536097d80d8465150fabce0fbf7fdbc18455abd925818
```

unknown 原因：`terrain:terrain_evidence_or_clearance_unresolved`、`building:building_ground_height_or_status_unresolved`、
`tower:tower_dataset_unresolved`、`airspace:restricted_area_dataset_unresolved`、
`critical_site:protected_site_dataset_unresolved`。
→ 未配置净空时**没有任何格子**被静默判为 pass，正是 §10 期望的保守行为。

### 16.4 结果 B：显式净空参数（sandbox 验收用，非项目规则）

`--terrain-clearance 30 --building-clearance 30 --tower-clearance 10`
（30 m 取自项目 `config/defaults.json::engineering_parameters.vertical_clearance_m.value` 的既有工程默认语义；
调用方显式传入，脚本在报告中标注 `explicit_cli_argument`）

```text
altitude_layer_id    ALT-080
nominal_altitude_m   80.0
vertical_reference   egm2008_orthometric
grid_level           8（L8 = 0.0011111°）
total cells          24300
pass cells           0
blocked cells        9918      （terrain 9496、building 1090；部分格同时命中两类）
unknown cells        14382
by blocker type      terrain 9496, building 1090, tower 0, airspace 0, critical_site 0
status               completed_with_warnings
field_id             PCF-ALT-080-afe0b4cffc5b877c
fingerprint          pcf-9b08b88ff9e5ccaba6e41a2538bb90b90917348ad49da2f83025a9f292675d85
wall time            grid 0.477 s / terrain 3.292 s / building 0.208 s / field 1.263 s / total 5.241 s
artifact             _dsh_prof/b3x/case_explicit_clearances.json（含 counts、逐类 unknown 直方图、blocked 样例、指纹）
```

unknown 直方图（14382 格全部只因**未提供的权威数据**）：

```text
tower:tower_dataset_unresolved            24300
airspace:restricted_area_dataset_unresolved 24300
critical_site:protected_site_dataset_unresolved 24300
```

解释（均为如实结果，未做任何修饰）：

* `pass = 0` 的**唯一**原因就是上面三个 domain 在整个工作区都没有 confirmed 数据源；
  建筑域在修正"覆盖范围内无事实行 = 无建筑"的既有映射语义后，unknown 已降到 0（`zero_cell` 16284 格，覆盖范围外 0 格）。
* 80 m 正高是本岛的强约束面：9496 格地形正高 ≥ 50 m 被 terrain 硬阻断，1090 格屋面 + 净空高于 80 m 被 building 硬阻断。
* 未生成 `ProjectState` 记录、未写入 `.cns-results`、未改动用户工程：本案例是**只读真实数据 + 纯函数生成**的沙箱验收。

### 16.5 若要成为用户工程内的正式 golden-case，唯一前置条件

需要在用户工程中显式完成（而不是由代码猜测）：确认工作区（L8 网格）、确认 ALT-080 高度层、
确认 `terrain_vertical_clearance_m` / 建筑水平+垂直净空、注册 towers 源并确认塔顶（`tower_top_confirmation`），
以及提供 confirmed 的要地/空域保护几何。缺任何一项都会如实停在 unknown（这正是本轮的设计目标）。

---

## 17. 已知 B5/B4 前置缺口（本轮不修）

1. **塔顶确认渠道**：`tower_top_confirmation` / `tower_top_confirmed` 目前只能由源数据字段推导，
   项目没有工程录入/审核入口 —— 因此真实塔顶在当前工程内无法进入 confirmed 状态（如实为 unknown）。
2. **要地/空域 confirmed 源**：项目数据源注册表没有该角色，`restricted_areas` 只能由调用方显式传入
   （已实现"无配置 ⇒ unresolved，绝不猜半径"）。
3. **约束场 cell 的 HTTP 粒度**：默认只返回摘要 + artifact_ref，前端如需可视化需要 B4 之后的新 overlay 读取 sidecar。
4. **起降场周边障碍 / 高度层区域扫掠**：依 §13 明确 deferred，本轮未实现。

---

## 18. 最终验收对照（§22）

| 项 | 结论 |
|---|---|
| A production 不再依赖 archive planner 定义的 GridGraph | ✅ 唯一实现 `planning/grid_graph.py` |
| B production 连续验证不再依赖 V3 Research 模块 | ✅ 只 import `..validation.*`（V3 侧反向薄 re-export） |
| C 约束场按任意 altitude layer 参数化 | ✅ 无 80 m / ALT-080 硬编码，含自定义层测试 |
| D terrain/building/tower/restricted area 均可贡献三态 | ✅ 单测 + 真实数据 golden-case |
| E Theta* V2 不穿 blocked constraint | ✅ 扩展、supercover、rewiring 全部受 gate 约束 |
| F Risk Field 与 Constraint Field 语义分离 | ✅ 生成入口不接受风险评分；obstacle 不进 objective |
| G unknown 不被伪装 pass | ✅ 三态 + provisional policy + operational adoption 拒绝 |
| H Operational Adoption 仍由 independent continuous validation 做最终 gate | ✅ 两份独立证据 |
| I 无 80 m hardcode | ✅ |
| J P14 SHA 完全未变 | ✅ 见 §11（mtime + SHA-256） |

---

## 19. P14 四文件 SHA-256（本批次接手时 = 结束时）

```text
5AA3559E4B84C19D513BA6546A1A5435E4CC1C9401DCBB759FBA666E96920867  cns_planner/algorithms/corridor/v1.py
045826BFEB8B7C74885F8DAF65DB67B26C8F3E60219989A59F124542E94E32B8  cns_planner/algorithms/coverage/geometric_3d.py
79F9C56A97955847B74A5B6EDE244BEFCB8DC77CAF4739DAAD2C2B769BA57C28  cns_planner/algorithms/service_capability/v1.py
BFC08B5FC25B608FC035754714F127DED6E024F854AF6CA67A73ED2E8436A20C  tests/test_cns_corridor.py
```

（四文件 mtime 均为 2026-09-24 15:18–15:46，早于本批次所有 B3X 文件（2026-09-25 17:50 起），
交叉证明本批次没有触碰它们。`git diff --check` 无输出、exit 0。）

---

## 20. B3X_READY_TO_REVIEW

**true** —— 无 blocker。上述 §17 的缺口是下一批次（B4/B5）范围，不影响 B3X 的十条验收结论。
