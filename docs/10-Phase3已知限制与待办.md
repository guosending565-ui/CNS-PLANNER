# Phase 3：真实舟山航路规划闭环稳定化 — 已知限制与待办

本文件记录 Phase 3 端到端验证过程中**确认**的问题、当前处理方式与后续需求。
与 `AI_DEV_CONTEXT.md` 的冻结边界一致：本轮只做 **bug 修复**、**真实数据适配（显式来源/CRS/工程确认参数）**
与 **性能/UI**，不改变任何路径规划算法语义。

---

## 1. 建筑事实只支持 L8 网格（本轮不修，已记录为后续需求）

### 现象

在舟山全域工作区（5344.78 km²）上，`grid_attributes.buildings` 恒为：

```json
{"status": "unsupported", "grid_level": 6, "count": 1900, "covered_count": 0,
 "message": "building_grid 当前仅允许 L8 直接映射；禁止跨层级平均或插值"}
```

因此 Layered Risk-Aware Theta* V2 的 coarse **战略垂向包线**（`coarse_strategic_vertical_envelope`）
拿不到任何建筑事实：`building_count` / `height_max_m` / `valid_height_fraction` 全部为 `unknown`。
规划本身仍可运行（`unknown` 不进入搜索、也不当 0），但**建筑约束实际未参与航路规划**。

### 根因（不是数据损坏）

```text
WorkspaceGridService(preferred_level=7, max_cells=5000)
  → 从请求层级逐级下降，直到格子数 ≤ 5000
  → 舟山全域 L7 超限 ⇒ 实际 level = 6（coarsened = true）

BuildingGridService.map()
  → if level != 8: return unsupported     # data/mapping/buildings.py
```

`zhoushan_building_grid_L8.gpkg` 是**预先按 L8 聚合好的事实表**，系统明确禁止跨层级平均/插值，
所以只要工作区网格层级不是 L8，建筑事实就整表不可用。

### 影响范围（回答"换工作区是不是也会用不了"）

**会，但可以规避。** L8 单格约 0.18 km²，在 `max_cells=5000` 约束下：

| 工作区面积 | 能否停在 L8 | 建筑事实 |
|---|---|---|
| ≳ 900 km²（含舟山全域 5344 km²） | 否，会被 coarsen 到 L6/L7 | 不可用 |
| ≲ 880 km²（例如桃花岛—函景湾走廊约 30×30 km） | 可以（需显式请求 level 8） | 可用 |

也就是说，**大工作区永远取不到建筑约束**，这与用户选择哪个工作区无关，只与面积/层级有关。

### 后续需求（用户 2026-09-21 明确）

> 希望以后不管什么级别的工作区，至少在需要建筑相关数据的时候可以直接调用。

即：建筑事实的获取不应被"必须恰好 L8"这一条硬约束绑死。后续可考虑的**最小语义安全**方向：

1. **按当前层级的精确几何聚合**：直接消费原始 footprint（`zhoushan_buildings.gpkg`），
   对当前网格单元做真实面几何相交聚合（`count` / `height_max` / `valid_height_fraction`）。
   这与 L8 事实表的聚合语义一致，**不是**跨层级平均或插值，因此不违反"禁止跨层级插值"的初衷。
2. **让工作区层级可显式指定为 L8**，并在超出 `max_cells` 时显式失败，而不是静默 coarsen。
3. 无论选哪条，都必须保持 `unknown ≠ 0`、`missing_data ≠ 0`，并在 provenance 中记录
   聚合方法、源几何与层级，绝不静默降级。

**本轮决定：不修改。** 作为已确认的已知限制与后续需求记录在此。

---

## 3. 大工作区下的服务端开销（已确认，本轮只记录 + 部分修复）

### 实测数据（真实舟山 L8 工作区）

| 指标 | 8505 格 L8 | 3161 格 L8 |
|---|---|---|
| 项目文件 | **97.7 MB** | — |
| `WorkflowSession` 载入 | 4.8 s | — |
| `session.save()` | 4.4 s | — |
| `WorkflowService.snapshot()` | **13.1 s** | — |
| snapshot JSON | **67.9 MB** | — |
| `layered_route_planner_service.result_snapshot()` | 1.7 s / 55.5 MB | — |

`GET /api/workflow` 与每个写操作的响应体都包含这个 snapshot。在 8505 格工作区上，
前端任何轮询都会产生 13 秒 CPU + 68 MB 序列化，并与 QGIS 主线程上正在执行的
Theta* 搜索竞争 GIL，最终把一次 `evaluate-real` 推到 **>1800 秒**（实测两次超时）。

**已定位但本轮未完成的改进方向**（属于"性能与 UI"边界，但涉及前端契约变更，故不在本轮
"最小修改"范围内）：

1. `snapshot()` 不再内联逐 cell 的 feasibility mask，改为只保留逐 cell 摘要
   （`grid_id/status/reason/reason_code`），完整 mask 通过专用只读端点按需拉取；
2. 写操作（`*-evaluate-real`）返回**该操作自己的结果投影**，而不是整个 workflow snapshot；
3. 长 CPU 任务移出 GIL（子进程/线程池），使 HTTP 读请求不被饿死。

### 搜索预算（已确认的真实缺陷）

`layered_route_planner` 基线的 `max_expanded_labels = null`（无上限）。实测舟山案例走廊：

| 格数 | 找到路径所需扩展 | 耗时 | 20000 扩展上限下的结果 |
|---|---|---|---|
| 8505 (L8) | 46,620 | 97.6 s | `blocked`（"未找到 any-angle 可用路径"）|

即：**达到扩展上限时返回的是 `blocked`（语义上等于"无解"），而不是资源受限**。
`docs/route_planner_v3_architecture.md` 中 V3-A 对同类情形明确要求返回
`search_incomplete`（resource limited），Layered Theta* V2 与该语义不一致。
本轮**未修改算法**（冻结边界禁止改动 Theta* V2 搜索语义），只在工程验证中通过
`/api/algorithms/select` 显式设置一个足够大的搜索预算。

**最小修改方案（建议，尚未实施）**：在 Theta* V2 达到 `max_expanded_labels` 时返回
`search_incomplete` + `statistics.expanded_labels`，而不是 `blocked`；这不改变搜索、
启发函数、代价或任何 verdict 语义，只修正"资源耗尽 ≠ 无可行解"的报告语义。

---

## 4. Phase 3 修改记录（已实施）

| 文件 | 修改 | 原因 |
|---|---|---|
| `cns_planner/domain/population_nodata.py`（新增） | 显式「来源 NoData = 零人口」确认契约、normalize、fingerprint、allocation 构造 | 真实 WorldPop 海上 89.6% 为 NoData，Theta* V2 fail-closed 使跨海航线不可规划 |
| `cns_planner/gis/raster_adapter.py` | `read_population_count(bbox, nodata_semantics=None)`；范围内全 NoData 且已确认 ⇒ 已知 0；`outside_extent` 永不转换 | 同上 |
| `cns_planner/data/mapping/population.py` | 传递 policy、输出 `nodata_semantics` provenance、修正 `missing_count`/coverage 计数；**无有效观测覆盖**（`read_values` 为空）的格子按已知 0 处理 | 栅格边缘 87 个格子被极小面积重叠外推出 ~79 person/km² 的病态密度并阻塞搜索 |
| `cns_planner/gis/map_data.py` | `grid_attributes()` 从项目状态读取该确认并注入人口映射 | 确认必须显式、可审计、参与失效 |
| `cns_planner/application/project_state.py` | 新增 `population_nodata_policy` 键与 normalize（默认 `not_configured`） | 未确认时行为与历史完全一致 |
| `cns_planner/application/workspace_service.py` | policy 快照/设置（设置后定向 stale 人口属性及其下游） | 确认变更必须触发重算 |
| `cns_planner/application/workflow_service.py` | `population_nodata_policy` 委派；`deferred_save()` 单提交点上下文 | 见下 |
| `cns_planner/application/session.py` | `defer_save` / `commit_deferred()` | 一次用户操作（重算 workspace + 重算属性）必须**一次提交**，否则长任务期间 GET 会读到"工作区已换、属性未算"的中间态（实测 revision 4→8 时 `population.source=None`） |
| `cns_planner/api/router.py` | `/api/population-nodata-policy`（GET/POST）；workspace 动作使用 `deferred_save`；透传显式 `max_cells` | 同上 |
| `cns_planner/gis/layered_feasibility_adapter.py` | `layered_feasibility_source_status` 输出 `terrain`/`population`（含 `available`/`reason`），保留 `terrain_dtm` 兼容键 | 该函数只返回 `terrain_dtm`，而 readiness 与前端都读 `sources.terrain` ⇒ **FABDEM 永久误报"不可用"** |
| `cns_planner/gis/qgis_runtime.py` | 任务超时 90 s → 1800 s（可用 `CNS_QGIS_TASK_TIMEOUT_S` 调整）；超时抛出带明确说明的异常 | 真实数据规划 >90 s，旧上限让任务失败且 `str(TimeoutError())` 为空 ⇒ HTTP 400 无任何错误信息 |
| `cns_planner/algorithms/grid/service.py` | `max_cells` 可显式指定（默认仍是 5000，新增 `CNS_GRID_MAX_CELLS`），响应报告 `max_cells` | L8 是 building_grid 唯一可映射层级，默认上限会把 L8 静默 coarsen 到 L7 ⇒ 建筑事实整表不可用 |

新增测试：`tests/test_population_nodata_semantics.py`（9 项）、`tests/test_grid_level_ceiling.py`（5 项）。

---

## 5. 真实舟山案例闭环验证结果（2026-09-21）

**案例**：起点 桃花岛无人机起降点 `RLS-FA13218F1A7F` `[122.2841666667, 29.8430555556]`
→ 终点 函景湾无人机起降场 `RLS-30E29238CD3D` `[122.2763888889, 29.9452777778]`
（与真实航线 `RLR-83B454FC48A2` 同一 OD，可对照）

**工作区**：`[122.268, 29.835, 122.300, 29.955]`，显式请求 **L8**，3161 格（`coarsened=false`）。
L8 是 `building_grid` 事实唯一可映射的层级；L7 下 `buildings` 全为 `unsupported_grid_level`
⇒ feasibility mask `feasible=0` ⇒ 规划在语义上不可能成立。默认 `max_cells=5000` 在该工作区
即可停在 L8（无需提额）。

| 环节 | 结果 | 耗时 |
|---|---|---|
| 数据加载 / 工作区重算 | `population status=passed`（confirmed_zero=2322）、`buildings status=passed`（covered=3161） | 34.5 s |
| 固定高度层 | `ALT-ZS-080-EGM2008` → 80 m EGM2008 orthometric confirmed | — |
| Theta* V2 readiness | `theta_star_v2.status=passed`，`resolved_cell_count=3161/3161` | — |
| **Layered Theta* V2 规划** | **`candidate`**：`LRC2-R0013-ALT-ZS-080-EGM2008-layeredcandv1-71`，距离 **11506.65 m**（直线 11391.31 m，绕行 1.0101），5 个顶点，扩展 21027 | **46.1 s** |
| RouteRiskProfile | `status=passed`，`current_applicability=current`；三个 domain 的 classification 均为 `not_configured`（无默认阈值，符合设计） | 11.1 s |
| LayeredRouteValidation | 执行成功：`LRV-765D413D987F`，`status=unresolved`，`status_reason=unresolved_domain_evidence` | 40.5 s |
| └ terrain 域 | **`passed`**，461 个证据项，最小垂向余量 **40.25 m** | — |
| └ building 域 | **`unresolved`**：源数据 `zhoushan_buildings.gpkg` 中存在几何无效的 footprint（`building_footprint_geometry_invalid`，例如 `building_id=4tCet`） | — |
| Operational Adoption | **正确拒绝发布**（publish gate 要求 `validated_route`，当前为 `unresolved`） | 3.5 s |

**结论**：管道完整、语义正确。规划与地形验证在真实数据上通过；建筑域的 `unresolved`
是**源数据几何质量问题**（不是系统缺陷），系统按 `unknown != safe` 正确 fail-closed，
从而不允许发布运行航路。

### 本轮修复的 7 个真实缺陷

1. **population 海上 NoData 使跨海航线不可规划**（方案 A：显式 NoData 语义确认）
2. **栅格边缘格子被极小面积重叠外推出病态密度**（2e-7 人 → 79 person/km²）并阻塞搜索
3. **`layered_feasibility_source_status` 键名不匹配** ⇒ FABDEM 永久误报"不可用"
4. **QGIS 任务硬超时 90 s** 且 `str(TimeoutError())` 为空 ⇒ 真实数据规划失败且无错误信息
5. **`evaluate_layered_route_candidate` 双层 `qgis.call` 自死锁**（本轮最关键）
   —— router 已编组到 QGIS 线程，`ApplicationContext` 内又包一层，QGIS 主线程给自己排任务并
   `future.result()` 等待，请求永不返回；线程转储确认。修复后 `evaluate-real` 由 **1800 s 超时 → 46 s 成功**
6. **`NativeTerrainWindowSource` 缺少 `_transform_to_raster_crs`** ⇒ production candidate
   validation 100% 报 `400 'NativeTerrainWindowSource' object has no attribute ...`；
   该方法只定义在兄弟类上，已上移到共享基类 `_FabdemRasterBase`
7. **`max_cells` 不可显式指定** ⇒ L8 被静默 coarsen 掉、建筑事实整表不可用

### 仍然存在的（未修）问题

* §1 建筑事实的层级无关获取（用户已明确为后续需求）；
* §3 大工作区下 `snapshot()` 13 s / 68 MB 的服务端开销；
* §3 `max_expanded_labels` 达到上限时返回 `blocked` 而非 `search_incomplete`。


