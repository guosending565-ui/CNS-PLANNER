# Phase 3.5 工程稳定化开发报告

> 范围声明：本轮**只做工程稳定化**，不新增算法、不重构架构。
> 明确未改动：**Theta\* V2 搜索逻辑**、**heuristic**、**RouteRiskProfile 数学**、
> **Validation 判据**、**Adoption gate**、**固定高度层语义**。
> 唯一与"算法"沾边的改动是**终态 status 的报告语义**（`blocked` → `no_path` /
> `search_incomplete` / `invalid_input`），它不改变搜索、代价、启发函数或任何判定结果。

| 项目 | 值 |
|---|---|
| 案例 | 桃花岛 `RLS-FA13218F1A7F` → 函景湾 `RLS-30E29238CD3D` |
| 工作区 | `[122.268, 29.835, 122.300, 29.955]`，显式 L8 |
| 高度层 | `ALT-ZS-300-EGM2008`（300 m EGM2008 orthometric，confirmed） |
| 后端测试 | **pytest 1273 passed, 6 skipped**（全量）；新增 3 个测试文件共 33 项 |
| 前端测试 | **node 221 passed**（原 219 + 本轮新增 2） |

---

## 0. 结论摘要

| 任务 | 状态 | 关键产出 |
|---|---|---|
| 1. 建筑几何质量 | **已完成** | 定位并修复提取链路根因；`make_valid` 内存修复；quality report |
| 2. 规划失败状态 | **已完成** | `no_path` / `search_incomplete` / `invalid_input`；预算耗尽不再等于 blocked |
| 3. 层级硬编码 | **已完成（只声明）** | `available_levels` / `preferred_level` 能力声明；L8 行为不变 |
| 4. 真实舟山 golden case | **已完成** | `tests/test_zhoushan_golden_case_e2e.py`（9 项） |
| 5. 开发报告 | **本文件** | — |

---

## 1. 建筑数据处理链路（任务 1）

### 1.1 根因（实测定位，不是源数据损坏）

只读探测真实 GPKG 得到的事实：

| 探测 | 结果 |
|---|---|
| `zhoushan_buildings.gpkg` 全量（538,228 个 footprint，WGS84） | `shapely.is_valid` **全部 True**，无空几何、无孔洞、无 MultiPolygon |
| 走廊范围内（1481 个）经 `QgsGeometry.transform` → EPSG:32651 | **1481/1481 变成 MultiPolygon**（每个仍只有 1 个部件） |
| 既有 `_metric_ring_of()`（只调 `asPolygon()`） | 对 MultiPolygon 抛 `TypeError` → 被 `except` 吞掉 → `ring_metric = []` |
| 连续验证器 | `len(ring) < 3` ⇒ `building_footprint_geometry_invalid` ⇒ 建筑域 `unresolved` |

因此真实舟山案例的建筑 `unresolved` 是**提取链路缺陷**：投影后几何类型改变未被识别。
`unknown != safe` 的 fail-closed 行为本身是正确的（不允许发布），但结论被错误地归因于
"源数据几何质量"。

### 1.2 修复（最小修改，源数据只读）

```text
GeoPackage(只读 SQLite/ogr)
  → QgsGeometry.transform(米制 CRS)
  → _metric_rings_of()            # 新增：逐部件外环（asPolygon / asMultiPolygon）
  → domain/building_geometry_quality.py
        assess_footprint_geometry()   # Ring → shapely Polygon → make_valid（必要时）
        prepare_footprint_polygons()  # 返回**全部**有效部件
  → validate_buildings() / building_domain_result()  # 逐部件净空判定
  → evidence.building_quality_report                 # 只读质量报告
```

关键语义：

* **不修改原始 gpkg**：全程 `mode=ro` + 内存修复，`source_modified=False`；
* **优先 `shapely.make_valid()`**：修复结果必须自身有效且面积为正才被采用；
* **修复失败保留 unknown 语义**：`quality="invalid"` → 继续 `unresolved`，
  绝不当作"没有建筑"或"已验证"；
* **多部件不漏判**：一个 footprint 的每个部件都单独参与净空判定
  （只取最大部件可能隐藏较小部件上的穿透）；
* **保持接口兼容**：`ring_metric` 仍是"代表环"（最大部件），新增 `ring_parts_metric`
  携带全部部件；synth 证据链路同步透传。

### 1.3 修改文件与原因

| 文件 | 修改 | 原因 |
|---|---|---|
| `cns_planner/domain/building_geometry_quality.py`（**新增**） | 几何质量检查、`make_valid` 修复、多部件展开、质量报告累加 | 单一实现，供 GIS 边界与两个验证器共用；避免三套重复逻辑 |
| `cns_planner/gis/fine_environment_adapter.py` | `_metric_rings_of()` 逐部件提取；`RouteCorridorBuildingSource.query_route` 走质量门；`_ring_metric` 复用代表环逻辑 | **根因修复**：投影后的 MultiPolygon 不再被丢弃 |
| `cns_planner/route_planner_v3/continuous_validators.py` | `validate_buildings` 改为 `prepare_footprint_polygons` + 逐部件循环 + `building_quality_report` | cruise 连续验证恢复真实建筑判定 |
| `cns_planner/domain/vertical_transition_validation.py` | 同上（`building_domain_result`） | climb/descent 垂直过渡验证共用同一质量门 |
| `cns_planner/route_planner_v3/continuous_synthetic.py` | 透传 `ring_parts_metric` | 合成证据支持多部件 footprint（测试可达） |
| `cns_planner/application/layered_route_validation_service.py` | provenance 记录新验证器与报告标志 | 审计可追溯 |
| `cns_planner/web/js/workflow/layered_route_validation.js` | 展示 `building_quality_report`（passed/repaired/invalid + make_valid 统计） | 用户能看到"为什么 unresolved" |
| `tools/building_geometry_quality_report.py`（**新增**） | 只读质量报告生成器（gpkg → JSON/Markdown） | 独立输出 building quality report |
| `tools/building_geometry_corridor_probe.py`（**新增**） | QGIS 走廊建筑查询探针 | 复现根因与验证修复 |

### 1.4 真实数据质量报告（已生成）

```powershell
python tools/building_geometry_quality_report.py `
  --source ".../zhoushan_buildings.gpkg" --metric-crs EPSG:32651 `
  --bbox 122.268 29.835 122.300 29.955 `
  --out outputs/building_geometry_quality_corridor.json `
  --markdown outputs/building_geometry_quality_corridor.md
```

结果：**检查 1481 个 footprint → passed 1481 · repaired 0 · invalid 0**。
即走廊内真实 footprint 在米制下**全部有效**，进一步证实原 `unresolved` 由提取链路造成。

### 1.5 行为变化（必须知悉）

1. 建筑域 `evidence` 中的质量报告键是 **`building_quality_report`**（旧键
   `geometry_quality` 仍在，指向同一对象，向后兼容）；
2. 无法解释的 footprint 的 reason id 由 `building_footprint_geometry_invalid` 改为
   **`building_footprint_quality_unresolved`**（内部细节保留在
   `internal_geometry_quality_status` / `internal_geometry_quality`）；
3. 无效但**可修复**的几何（自交等）现在会被 `make_valid` 修复后**参与判定**
   （旧行为是直接 `unresolved`）。这是本次唯一"结论可能改变"的点，仅作用于
   *源几何无效* 的 footprint；无法修复者仍 fail-closed。

---

## 2. 路径规划失败状态（任务 2）

### 2.1 新状态机

`cns_planner/domain/layered_route.py` 新增显式契约：

| 终态 | 语义 | 触发条件 |
|---|---|---|
| `success`（线上拼写 `candidate`） | 搜索完成且找到航路 | A\*/Theta\* 返回路径 |
| `no_path` | **搜索完整跑完**但没有可行路径 | 端点不可遍历 / 无 feasible cell / 完整搜索未找到 |
| `search_incomplete` | **搜索预算耗尽**，可达性与最优性**均未证明** | 达到 `max_expanded_labels` / `max_expanded_states` |
| `invalid_input` | 输入本身不可解释 | 网格/掩码缺失、端点或 route_id 缺失、端点不在网格内 |

前置条件未满足仍走 `not_ready` / `missing_data` / `stale`；`blocked` 保留为历史拼写
（仅用于"显式硬约束阻断端点"）。每个终态结果同时带：

```json
{
  "terminal_status": "search_incomplete",
  "terminal_status_semantics": "search_budget_exhausted_reachability_not_proven",
  "blocking_reasons": [{
    "reason_code": "search_budget_exhausted",
    "resource_limit": "max_expanded_labels",
    "reachability_proven": false,
    "optimality_proven": false
  }]
}
```

### 2.2 修改文件与原因

| 文件 | 修改 | 原因 |
|---|---|---|
| `cns_planner/domain/layered_route.py` | 新增 `PLANNING_TERMINAL_STATUSES` / `PLANNING_TERMINAL_SEMANTICS` / `terminal_reason_code()` / `annotate_terminal_status()`；`CANDIDATE_STATUSES` 扩充 | 唯一权威状态词汇与 reason code；所有 planner 共用 |
| `cns_planner/layered_route_planner/theta_star_v2.py` | 终态判定（cap→`search_incomplete`，完整搜索→`no_path`）；缺失输入→`invalid_input`；`blocked()` 语义化；`SEARCH_SEMANTICS` 声明 | **禁止把搜索预算不足解释为空域不可行** |
| `cns_planner/layered_route_planner/planner.py` | 同上（Legacy V1 基线保持一致） | 两个 planner 状态语义统一，避免下游分歧 |
| `cns_planner/application/layered_route_planner_service.py` | stale 集合纳入新终态 | 输入变化必须能 stale `no_path` / `search_incomplete` / `invalid_input` 记录 |
| `cns_planner/web/js/workflow/layered_theta_v2.js` | `candidateModel` 暴露 `terminalStatus` / `resourceLimited` / `reachabilityProven`；blocker 徽标不再一律显示 `blocked` | 前端不得把预算耗尽显示成"不可行" |

### 2.3 未改动（冻结边界）

* **搜索逻辑**：`_theta_star` / `_astar` 的扩展、relax、LOS rewiring、corner guard 全部未动；
* **heuristic**：未动；
* **客观函数/代价**：未动；
* 达到 cap 时的**返回值本身**（无路径）也未动，只改 status 标签与语义声明。

### 2.4 测试

* `tests/test_layered_theta_star_v2.py`：新增 4 项（cap→`search_incomplete` 且
  `reachability_proven=false`；完整搜索→`no_path` 且 `reachability_proven=true`；
  `invalid_input`；词汇表暴露）；
* `tests/test_layered_route_planner.py`：2 项断言更新为 `no_path` / `search_incomplete`；
* `tests/test_risk_v2_canonical_integration.py`：1 项断言更新为 `no_path`；
* `tests/test_zhoushan_golden_case_e2e.py`：真实走廊预算耗尽 → `search_incomplete`
  且后续 profile/validation/adoption 无法越过前置门。

---

## 3. 建筑层级硬编码检查（任务 3）

### 3.1 发现的 L8 强绑定位置（只记录，不改行为）

| 位置 | 绑定 |
|---|---|
| `cns_planner/data/mapping/buildings.py:32` | `if level != 8: return unsupported`（**硬绑定，本轮不改**） |
| `cns_planner/gis/layered_feasibility_adapter.py` | building 事实直接消费 `grid_attributes.buildings`（L8 聚合） |
| `cns_planner/layered_route_planner/planner.py`、`theta_star_v2.py` | 文档/语义声明为 "MH/T L8"；实际按**当前工作区层级**搜索 |
| `cns_planner/algorithms/grid/service.py` | `preferred_level=7` + `max_cells=5000` ⇒ 大工作区静默 coarsen 到 L6/L7 |
| `cns_planner/application/layered_route_planner_service.py` | 未配置 adapter 时 `feasibility_adapter_not_configured` |

**语义落差**：生产默认工作区层级是 L7，而建筑事实表只能映射 L8 ⇒ 大工作区上建筑约束
实际不参与包线（`unknown` 不当 0，但也不参与）。这是已记录的后续需求，本轮**不改变**。

### 3.2 新增能力声明接口

| 文件 | 新增 | 说明 |
|---|---|---|
| `cns_planner/data/mapping/buildings.py` | `BuildingGridService.capabilities(source_path=…, declared_level=…)` | `available_levels=[8]`、`preferred_level=8`、`declared_level_usable`、`unusable_reason`、`cross_level_*_allowed=false`、`future_work` |
| `cns_planner/algorithms/grid/service.py` | `WorkspaceGridService.capabilities(preferred_level=…, max_cells=…)`；`empty()` 带 `capabilities` | `available_levels=1..16`、`preferred_level`、coarsen 语义与 `coarsened` 键 |
| `cns_planner/layered_route_planner/planner.py`、`theta_star_v2.py` | `PLANNER_CAPABILITY` | 层级绑定、`building_fact_level=8` |
| `cns_planner/application/layered_route_planner_service.py` | readiness 新增 `capabilities{planner,building_grid,workspace_grid}` | 前端/测试无需再猜层级 |

**行为不变**：`map()` 的 `level != 8 ⇒ unsupported`、`generate()` 的 coarsen 逻辑、
所有既有返回值均未改动；`capabilities()` 是纯只读声明。

---

## 4. 真实舟山 golden case e2e（任务 4）

`tests/test_zhoushan_golden_case_e2e.py`（9 项）：

| 测试 | 覆盖 |
|---|---|
| `test_golden_case_closes_the_loop_and_publishes_a_real_od_route` | workspace → L8 grid → 高度层 → Theta\* V2 → RouteRiskProfile → LayeredRouteValidation → Adoption → save/reload 全链路 |
| `test_golden_case_building_geometry_failure_blocks_publication` | 无法修复的 footprint ⇒ `unresolved` ⇒ publish gate 拒绝 |
| `test_golden_case_terrain_nodata_stays_unresolved_and_never_publishes` | NoData 不当作净空通过 |
| `test_golden_case_search_budget_exhaustion_is_not_infeasibility` | 预算耗尽 ≠ 不可行 |
| `test_golden_case_grid_is_l8_and_building_facts_are_level_bound` | 显式 L8 + 能力声明 |
| `test_layered_planner_readiness_exposes_level_capability_declarations` | readiness 三份能力声明、只读 |
| `test_golden_case_coarse_grid_at_l6_cannot_use_l8_building_facts` | coarsen 后建筑事实整表 `unsupported`，unknown 不当 0 |
| `test_real_building_gpkg_geometry_quality_report_for_the_golden_corridor` | **真实 GPKG**（缺数据自动 skip）：走廊质量报告 invalid=0 |
| `test_real_building_footprints_survive_the_metric_quality_gate` | **真实 footprint** 经质量门后仍为有效多边形（根因回归） |

**证据边界（诚实声明）**：默认路径不打开 QGIS/GDAL，FABDEM 与 footprint 证据为**注入的
合成证据**（与真实数据同 schema、同 CRS 语义、同量级数值：真实建筑高度实测
`27.912586212158203 m`、真实 10 顶点 footprint 形状、真实 O.D. 与工作区）。它验证**工程
管道、状态语义、失效链与持久化**；真实栅格读取由 QGIS 集成测试与
`tools/building_geometry_quality_report.py` 负责（后者已在真实 GPKG 上执行）。

---

## 5. 全量测试结果

| 套件 | 结果 |
|---|---|
| `python -m pytest tests -q` | **1273 passed, 6 skipped, 0 failed**（8m11s） |
| 本轮新增/重跑的聚焦套件 | `test_building_geometry_quality.py`(15) + `test_grid_level_capability.py`(9) + `test_zhoushan_golden_case_e2e.py`(9) = **33 passed** |
| `node <file>`（9 个前端测试文件） | **221 passed, 0 failed** |
| `node --check`（改动的 JS） | 通过 |

> 6 项 skip 是 `tests/test_map_http.py` 的真实 QGIS HTTP 集成（历史行为，需要
> `CNS_MAP_TESTS=1` 与运行中的地图服务）。
>
> 沙箱说明：DSH workspace-write 下 `node --test` 因 piped stdio 被拒（EPERM），按仓库既有
> 约定改用 `node <file>`；pytest 退出阶段的 `PermissionError` 来自临时目录清理，与用例结果
> 无关（报告已显示 0 failed）。

### 5.1 断言更新清单（诚实披露）

| 文件 | 更新 | 理由 |
|---|---|---|
| `tests/test_layered_route_planner.py` | `blocked` → `no_path`；cap 用例 → `search_incomplete` + 新 reason code | 终态语义修正的**预期**变化 |
| `tests/test_layered_theta_star_v2.py` | 同上 + 新增 4 项终态测试 | 同上 |
| `tests/test_risk_v2_canonical_integration.py` | `blocked` → `no_path`，新增 `search_incomplete is False` | 同上 |
| `tests/test_route_planner_v3_continuous.py` | 旧"self-touching 不修复"用例改为**共线不可修复**用例；新增 2 项（自交修复后判定、多部件逐部件判定）；键名改 `building_quality_report` | 任务 1 的语义修正 |
| `tests/test_vertical_transition_validation.py` | 同上（改名 + 新增 1 项修复后判定） | 同上 |

---

## 6. 未解决问题（明确不修，且不得静默并入）

1. **层级无关的建筑事实获取**（原 Phase 3 §1）：工作区被 coarsen 到大区域时，
   `building_grid` 整表 `unsupported`，建筑约束实际不参与战略包线。本轮只增加能力声明，
   未改变行为。可选方向仍为：按当前层级对原始 footprint 做**精确几何聚合**，或允许显式
   指定 L8 并在超限时显式失败。任何方案都必须保持 `unknown != 0` 与聚合 provenance。
2. **大工作区服务端开销**（原 Phase 3 §3）：8505 格 L8 下 `snapshot()` 13 s / 68 MB；
   建议改为按需拉取 mask、写操作只返回自身投影、长 CPU 任务移出 GIL。
   这属于"性能与 UI"，但涉及前端契约变更，不在本轮最小修改范围。
3. **`LayeredRouteValidation` 的 evidence 上限截断**：`max_evidence_items` 仍可能把大
   建筑集合截断为 `validation_incomplete`（语义正确，但真实大走廊上体验受限）。
4. **`WorkflowService` 收敛的 `total_seconds` 依赖默认工作区层级（L7）**：真实案例必须
   显式请求 L8 才能拿到建筑事实；前端未阻止用户在大工作区上"以为建筑已参与"。
   能力声明已可读出该落差，但 UI 提示尚未强制。
5. **`building_quality_report` 键名迁移**：旧键 `geometry_quality` 仍同时写入以保证兼容，
   未来可择机移除（需要一次显式的契约变更回合）。

---

## 7. 开发原则执行情况

| 原则 | 执行 |
|---|---|
| 最小修改 | 未新增算法；未改搜索/启发/数学/判据/gate；改动集中在提取链路、终态报告与只读声明 |
| 不重构架构 | 无模块搬迁、无接口重命名（除 1 处 reason id 与 1 个 additive 键）、无依赖方向变化 |
| 不新增算法 | 唯一"新数学"是 `shapely.make_valid()` 调用与逐部件展开，属几何质量修复 |
| 每完成一个功能立即测试 | 任务 2 → 跑 layered 套件；任务 1 → 跑 building/continuous 套件；任务 3 → 跑能力声明测试；任务 4 → 跑 golden case |
| 发现需修改核心算法则停止报告 | **未发生**：根因在 GIS 提取链路与状态报告，不在 Theta\* V2 / RouteRiskProfile / Validation 判据 |

## 8. 复现命令

```powershell
# 全量后端
python -m pytest tests -q

# 前端（沙箱内用 node <file>，不用 node --test）
node tests/phase35_stabilization_frontend.test.mjs
node tests/layered_validation_adoption_frontend.test.mjs
node tests/layered_route_map_evidence.test.mjs

# 真实建筑几何质量报告（只读）
python tools/building_geometry_quality_report.py `
  --source "D:/aaa2026project/UOM/舟山/规划系统/building/processed/zhoushan_buildings.gpkg" `
  --metric-crs EPSG:32651 --bbox 122.268 29.835 122.300 29.955 `
  --out outputs/building_geometry_quality_corridor.json `
  --markdown outputs/building_geometry_quality_corridor.md

# 真实走廊建筑提取（需要 QGIS python）
& "C:\Program Files\QGIS 3.44.14\bin\python-qgis-ltr.bat" tools/building_geometry_corridor_probe.py
```
