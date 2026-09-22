# Phase 3.5 人工验收问题修复报告

> **范围声明**：本轮**只修复数据链路与前端显示**。
> 明确未改动：**Layered Risk-Aware Theta\* V2 核心算法**（搜索 / 启发函数 / objective / 终止判定）、
> **RiskProfile 数学模型**、**LayeredRouteValidation 与 Adoption 的核心流程与判据**。
> 建筑净空公式（`building_clearance.building_roof_elevation` / `evaluate_vertical_clearance`）
> 与"`unknown != 0`、`missing_data != 0`"的 fail-closed 语义完全不变。

| 项目 | 值 |
|---|---|
| 真实案例 | 舟山 L7 工作区（3528 格，由 L8 请求 coarsen 而来） |
| 后端测试 | **1312 passed, 6 skipped, 0 failed**（`python -m pytest tests -q`，8m35s） |
| 前端测试 | **228 passed, 0 failed**（9 个 `*.test.mjs`，原 218 + 本轮新增 10） |
| 数据源 | `舟山建筑数据.qgz` → `processed/zhoushan_buildings.gpkg`（538,228 footprint） |

---

## 0. 验收发现的问题与结论

| # | 现象 | 根因 | 结论 |
|---|---|---|---|
| 1 | 环境建模显示 FABDEM/GBA/L8 grid 全部"通过"，但**建筑环境映射 = unsupported，0/3528** | `WorkspaceGridService(preferred_level=7, max_cells=5000)` 把大工作区 coarsen 到 L7；`BuildingGridService.map()` 硬性要求 `level == 8`，于是整表 `unsupported` | **已修复**：层级无关的精确足迹聚合 |
| 2 | 专题人口显示正常，但环境结果的**人口映射显示"缺少数据"** | 前端只读 `value_status`；覆盖信息（full/partial/missing/outside）只在卡片注释里，且没有统一的结果载体 | **已修复**：统一 `PopulationMappingResult` + 覆盖统计展示 |
| 3 | 建筑 qgz/gpkg 路径**存在但状态异常** | `_validate_paths` 只接受 `.gpkg`；`.qgz` 工程被直接判为格式错误；且 `source_manifest` 会复用上一次的 schema/extent | **已修复**：统一 path resolver + 启动存在性检查 + 丢弃旧派生状态 |
| 4 | 建筑数据无法作为前端地图图层 | 前端没有建筑图层开关与渲染，浏览器也不能读 `.qgz` | **已修复**：qgz → 后端解析 → GeoJSON（按 bbox + LOD）→ 前端"建筑轮廓"图层（默认关闭） |

---

## 1. 任务 1：Building Environment Mapping

### 1.1 根因（不是数据损坏，也不是算法缺陷）

```text
WorkspaceGridService(preferred_level=7, max_cells=5000)
  → 舟山全域 L7 超出 5000 格 ⇒ 实际请求 L8 被 coarsen 到 L7（3528 格）
BuildingGridService.map()
  → if level != 8: return unsupported        # 既有硬约束
```

`zhoushan_building_grid_L8.gpkg` 是**预先按 L8 聚合好的事实表**，跨层级平均/插值是明确禁止的，
因此只要工作区网格不是恰好 L8，建筑事实就整表不可用。

### 1.2 修复：按当前层级的**精确几何聚合**

`docs/10-Phase3已知限制与待办.md` §1 记录的方向 1：直接消费原始 footprint，
对当前网格单元做真实面几何相交聚合。**语义与既有 L8 事实表生成脚本
（`building/scripts/build_mht_building_grid.py`）逐条对齐**，不引入第二套建筑数学：

| 既有 L8 事实表 | 本轮聚合（任意层级） |
|---|---|
| 计数 / 高度统计按**建筑质心**分配 | 同样按质心分配（一个建筑只计一次） |
| `height_valid = height_m notna & > 0` | 同样 `height_m is not None and > 0` |
| 米制面积用 `EPSG:32651`（UTM 51N） | 按工作区中心自动选 UTM 带（舟山 ⇒ `EPSG:32651`） |
| 覆盖度 = footprint ∩ cell 精确相交面积，ratio 裁剪到 `[0,1]` | 完全相同 |
| 只生成被建筑触及的 cell | 所有工作区 cell 都给出明确结果（命中 / 已知 0 / `outside_coverage`） |
| — | 额外记录 `area_crs_basis` / `count_allocation` / `height_valid_rule` / 跳过计数等 provenance |

**新增回归证据**：`tests/test_building_level_independent_facts.py::test_real_aggregation_agrees_with_the_l8_fact_table`
在**真实数据**上逐格比对"聚合到 L8"与"预聚合事实表"的 `building_count` 与 `height_max_m`，
要求**零不一致**（该用例已通过）。

### 1.3 统一结果载体 `BuildingEnvironmentMappingResult`

`grid_attributes.buildings.environment_mapping`（additive 键）：

```json
{
  "status": "passed | partial | unsupported",
  "total_cells": 3528,
  "covered_cells": 3528,
  "unresolved_cells": 0,
  "coverage_ratio": 1.0,
  "mapping_basis": "exact_footprint_intersection_and_centroid_allocation",
  "grid_level": 7,
  "level_aligned": false,
  "level_independent_facts": true,
  "facts_available": true,
  "participates_in_planner": true,
  "mapping_status": "passed",
  "unresolved_reasons": {},
  "algorithm_id": "building-grid-footprint-aggregation",
  "algorithm_version": "1.0"
}
```

状态三态定义：

* `passed`：每一格都有明确建筑事实（含"源范围内确认无建筑"的已知 0 语义）；
* `partial`：部分格有事实、部分格无法判定（`unknown` 绝不当 0）；
* `unsupported`：一格都拿不到（无 footprint 源 / 无几何库 / 无空间索引）。

### 1.4 三条互不混淆的路径（`BuildingGridService.map`）

```text
level == 8 且 L8 事实表可用  → 既有"按 grid_id bbox 直接映射"（行为完全不变）
否则，且有原始 footprint 源 → 按当前层级精确聚合（层级无关事实）
否则                        → 既有 unsupported / missing_data（绝不伪造建筑事实）
```

`map()` 新增三个只读参数：`footprint_source` / `footprint_aggregator` / `footprint_layer_name`。
**既有调用方 `map(grid, source_path)` 的两个位置参数语义不变**，因此既有测试与行为完全保持。

**下游零改动**：`LayeredFeasibilityAdapter` 直接消费 `grid_attributes.buildings` 的
`building_count` / `height_max_m` / `valid_height_fraction`；聚合结果的 cell 结构与
L8 直接映射**键集合完全一致**，所以建筑约束现在能真正进入 Theta\* V2 的 coarse 战略垂向包线。

### 1.5 明确的边界

* **不改写源数据**：GeoPackage 全程 `mode=ro`，`.shp`/`.geojson` 只读；
* **不是跨层级平均/插值**：`metadata.cross_level_averaging = false`、`cross_level_interpolation = false`；
* **失败 fail-closed**：无空间索引 ⇒ 拒绝全表扫描并显式报错，逐格保持 `missing_data`；
* 几何库（shapely / pyproj）缺失时显式降级并给出原因，绝不伪造建筑事实。

---

## 2. 任务 2：Population Mapping 状态展示

### 2.1 统一输出

`grid_attributes.population` 新增（additive）：

```json
{
  "coverage_ratio": 0.9794,
  "full_cells": 2953,
  "partial_cells": 208,
  "missing_cells": 0,
  "population_mapping": {
    "status": "passed | partial | unsupported",
    "total_cells": 3161, "covered_cells": 3161, "unresolved_cells": 0,
    "coverage_ratio": 0.9794, "cell_coverage_ratio": 1.0,
    "full_cells": 2953, "partial_cells": 208, "missing_cells": 0,
    "outside_cells": 0, "nodata_only_cells": 0, "confirmed_zero_cells": 2322,
    "value_status": "passed", "coverage_status": "partial",
    "participates_in_planner": true
  }
}
```

计数以 `cells` 为**唯一权威来源**重新统计：历史记录里 `full_count` 与 `coverage_summary.full`
可能互相矛盾（真实项目实测 `full_count=2953` 而 `coverage_summary.full=631`），
按载体重算可以避免把旧状态带上界面。

### 2.2 前端

* 人口映射卡：**不再**因为 `value_status=missing_data` 就只显示"缺少数据"；
  现在显示统一载体的三态 + 覆盖统计
  `已覆盖 6 / 10 格（60%） · full 4 / partial 2 / missing 3 / outside 1`；
* 建筑环境映射卡：显示 `environment_mapping` 三态 + `已覆盖 C / T 格（P%） · 未判定 U 格 · <依据>`；
* 旧快照（无统一载体）继续用既有计数字段做**展示换算**，不解构历史数据；
* 顺带修正 `workspaceMappingSummary` 里硬编码的 `建筑：missing_data`（改为读真实映射状态）。

---

## 3. 任务 3：统一数据源路径校验

### 3.1 新的 `cns_planner/gis/path_resolver.py`

* 每个来源角色的**允许格式表**（单一事实来源，`file_browser` 与加载校验共用）：

  | 角色 | 格式 |
  |---|---|
  | `basemap` | `.qgz` / `.qgs` |
  | `population` / `terrain` / `terrain_dtm` | `.tif` / `.tiff` |
  | `buildings` / `building_grid` | `.gpkg` / `.shp` / `.geojson` / `.json` / `.qgz` / `.qgs` |
  | 参考起降点 / 参考航线 | 既有约定 |

* `resolve_source_path(value, role=…)` 返回
  `{path, resolved, exists, is_file, is_directory, suffix, format_supported, status, reason}`，
  status ∈ `not_configured` / `path_missing` / `not_a_file` / `unsupported_format` / `ok`；
* `check_source_paths(paths)` 在**每次 `metadata()`** 都重新 `exists` + `is_file`，
  通过 `/api/state` 的 `path_checks` 暴露给数据源中心；**不存在缓存**。

### 3.2 `.qgz` 作为建筑来源

`cns_planner/gis/qgis_project_layers.py`（纯标准库 ZIP + XML）把 QGIS 工程解析成图层清单：

```text
舟山建筑数据.qgz
  → project.qgs（XML）
  → ./processed/zhoushan_buildings.gpkg|layername=buildings   （建筑单体，Polygon）
  → ./processed/zhoushan_building_grid_L8.gpkg|layername=…    （聚合事实表，Polygon）
  → ../FABDEM/…tif（栅格）、天地图 XYZ（远程）
```

* 相对路径按**工程所在目录**解析成绝对路径；
* 远程/服务型 datasource 绝不当作本地文件；
* 选层是**启发式排序**（单体足迹优先于 `building_grid` 聚合表），并记录 `selection_reason`；
* `resolve_vector_layer_source()` 对 `.qgz` / `.gpkg` / `.shp` / `.geojson` 返回统一结构或
  明确的中文失败原因。

`source_loader._validate_paths()` 现在对建筑类来源同时检查"路径存在"与"能否解析出可用图层"，
`.qgz` 里没有可用 Polygon 图层也会在加载阶段说清楚（而不是事后报 `unsupported`）。

### 3.3 不再缓存旧状态

| 位置 | 修复 |
|---|---|
| `domain/source_audit.source_manifest` | 文件签名（size/mtime）变化时**丢弃上一次的 schema / extent / feature_count / geometry_health**，只保留验证记录用于 `needs_revalidation` |
| `MapData.load` | 每次加载清空 `_vector_role_cache`，矢量角色解析结果不跨加载复用 |
| `MapData.metadata` | `path_checks` 与 `building_sources` 每次重新计算 |

---

## 4. 任务 4：建筑轮廓图层

### 4.1 数据链路

```text
QGIS 工程 / GeoPackage / Shapefile / GeoJSON
  → 后端解析（gis/qgis_project_layers.py：qgz → 图层 + 绝对路径）
  → GET /api/building-footprints?bbox=…&limit=…&tolerance=…
      · RTree 空间索引按视图 bbox 查询
      · shapely 简化（容差由前端按缩放级别给出）
      · 返回 GeoJSON FeatureCollection（含 count / truncated / source）
  → 前端“建筑轮廓”图层（canvas 绘制，只读）
```

浏览器**从不读取 `.qgz`**；端点只读，不写任何项目状态。

### 4.2 前端图层

* 图层抽屉"环境"组新增 **建筑轮廓（QGIS 建筑数据）**，**默认关闭**（无 `checked`）；
* LOD 分档（`map/building_footprint_layer.js`）：

  | 档位 | 判定（每像素米数） | 行为 |
  |---|---|---|
  | `hidden` | `res > 30` | **完全不请求**，状态行提示"放大后显示建筑轮廓" |
  | `coarse` | `8 < res ≤ 30` | 请求上限 600 个要素，较大简化容差，低透明度 |
  | `full` | `res ≤ 8` | 请求上限 3000 个要素，容差 `res × 1.2 / 111320` 度 |

* 视图移动不足 180 像素时不重复请求；后端不可用/加载失败时状态行直接给出原因，
  绝不显示假数据；
* 装配（`attachBuildingFootprintLayer`）全部在模块内完成，`main.js` 只保留一行接线
  （`tests/test_architecture.py` 要求入口文件 ≤ 450 行）。

---

## 4b. 任务 5：AltitudeLayer 目录的初始化与恢复（Phase 3.5 二轮验收）

### 4b.1 现象与根因

```text
Step02 环境建模（Terrain / Building mapping / Population / Airspace）全部 PASS
  → 进入 Step03 航路规划，scenario route R0005 N017→N018
  → altitude layer catalog = not_configured · 共 0 层
  → resolve_cruise_altitude() = blocked · altitude_layer_missing
  → readiness blockers 含 altitude_layer_not_found，Theta* V2 无法执行
```

根因**不是算法缺陷，而是配置生命周期缺陷**：`spatial_3d.altitude_layers` 只有
`POST /api/spatial-3d/altitude-layer`（用户手工逐条写入）一条产生路径。工作区首次设置
与项目恢复都不会补建目录，`empty_spatial_3d()` 直接给出 `altitude_layers: []`，
于是任何"没有手工配过高度层"的项目在 Step03 必然 `altitude_layer_missing`。

### 4b.2 修复：工程默认高度层（catalog 条目，不是默认选择）

新增 `cns_planner/domain/altitude_layer_defaults.py`：

| id | nominal | lower–upper | vertical_reference | confirmed |
|---|---|---|---|---|
| `ALT-060` | 60 m | 40–70 m | `egm2008_orthometric` | true |
| `ALT-080` | 80 m | 70–90 m | `egm2008_orthometric` | true |
| `ALT-100` | 100 m | 90–125 m | `egm2008_orthometric` | true |
| `ALT-150` | 150 m | 125–175 m | `egm2008_orthometric` | true |
| `ALT-200` | 200 m | 175–250 m | `egm2008_orthometric` | true |

> `lower` / `upper` 取相邻 nominal 的中点（首层下界 = nominal − 20 m，末层按对称半带宽 +50 m），
> 各层**互不重叠**；它们只是工程默认的带边界（可被工程师显式改写），不改变任何 nominal。

* `source` = `工程默认高度层（软件基线 ALT-060/080/100/150/200；非项目实测来源，可由工程师改写）`
  —— 显式、可追溯、**不是** `未记录`；
* 条目一律走既有 `normalize_altitude_layer` 合同（显式 nominal + 显式 datum + 显式 source），
  不新增第二套高度层数学，也不放宽任何 `confirmed` 判据。

**两处生命周期接入（幂等，且只补 catalog 条目）**：

| 时机 | 位置 | 行为 |
|---|---|---|
| 工作区确认（新建/重设工作区） | `WorkspaceService.set_workspace` | 补建默认高度层 + `refresh_spatial_status` + 提交一次 |
| 项目恢复（打开 / 启动加载 / 另存后重载） | `WorkflowSession._load` | 补建默认高度层 + `resync_operating_layer_statuses` + `refresh_spatial_status`，**不写盘**，只置 `pending_save` |

判定条件（`should_initialize_default_altitude_layers`）：项目**已有 workspace 与 grid cells**、
`altitude_layer_defaults_initialized` 未置位、且目录为空。因此：

* 空项目 / 已 `clear_workspace` 的项目保持空目录（进入 Step02 之前不会被塞入默认层）；
* 目录**首次**补建或被**显式删除**时把标记置位（`mark_altitude_layer_catalog_managed`），
  用户刻意删空的目录在之后的项目恢复中**保持为空**，由前端显式提示，绝不静默填满。

**明确的边界（绝不放宽高度检查）**：

1. 补建的是 **catalog**（"工程里有哪些可选巡航高度层"），**不是**为任何航路选择高度层：
   planning request 的 `altitude_layer_id` 仍必须由用户显式选择，
   `parameter_status = "no_default_altitude_layer"` 与 `altitude_layer_not_explicitly_selected`
   语义完全不变；
2. 不触碰 `route_operating_layers` / `departure_arrival_procedures` / planning request /
   feasibility·cost policy / candidate / validation / adoption；
3. 未改 Theta* V2 搜索、RouteRiskProfile、LayeredRouteValidation、OperationalAdoption；
4. **恢复保持只读**：打开旧项目不改写 `project_state.json`（既有持久化特征化用例继续通过），
   差异登记为 `pending_save`，由下一次真实提交固化。

### 4b.3 Step03 前端

* `layered_theta_v2.js` ① 规划请求区块：`altitude layer catalog` 行保持显示后端原值
  （`configured/not_configured · 共 N 层`）；当 `layers.length === 0` 时追加**明确提示**
  `高度层目录为空（共 0 层）：没有可选巡航高度层 …请先在工作区/垂直配置面板补建 AltitudeLayer`；
* `layered_route_planner.js` 高度层下拉：目录为空时选项文案为
  `高度层目录为空（not_configured · 共 0 层）：没有可选高度层`，不再使用含糊的"尚未配置"；
* 提示文案中列出的可选层为 `ALT-060/ALT-080/ALT-100/ALT-150/ALT-200`；
* 提示**不改变**门控：运行按钮仍只由后端 `theta_star_v2.blockers` 决定（目录为空 ⇒ 保持 disabled）。

### 4b.4 回归证据

新增 `tests/test_altitude_layer_lifecycle.py`（14 项）：默认层合同、初始化判定与幂等、
workspace 生命周期、项目恢复（含"旧项目只读补建"）、显式删除后保持空目录、
planning request 仍需显式选择、`altitude_layer_missing/not_found` blocker 解除、API 面。

前端新增 2 项（`tests/frontend_modules.test.mjs`）：下拉逐层读取恢复后的 catalog 且不自动选中；
目录为空时的明确提示 + 按钮保持 disabled。

---

## 5. 测试结果

| 套件 | 结果 |
|---|---|
| `python -m pytest tests -q` | **1312 passed, 6 skipped, 0 failed**（8m35s，1 warning） |
| `node tests/*.test.mjs`（9 个文件） | **228 passed, 0 failed** |
| 其中本轮新增 | 后端 `test_building_level_independent_facts.py` **24 项**；前端 `building_mapping_frontend.test.mjs` **10 项** |
| 真实数据用例 | 4 项（`qgz` 解析、聚合↔L8 事实表逐格一致性、L7 工作区可用性、轮廓 GeoJSON） |

> 6 项 skip 是 `tests/test_map_http.py` 的真实 QGIS HTTP 集成（历史行为，需要
> `CNS_MAP_TESTS=1` 与运行中的地图服务）。
>
> 沙箱说明：DSH workspace-write 下 `node --test` 因 piped stdio 被拒（EPERM），按仓库既有
> 约定改用 `node <file>`；pytest 退出阶段的 `PermissionError` 来自临时目录清理，与用例结果无关。

### 5.1 真实数据端到端复现（本轮实测）

```text
工作区：[122.27, 29.84, 122.41, 30.12]，请求 L8 → coarsen 到 L7，3528 格

修复前：BuildingGridService().map(grid, building_grid_L8.gpkg)
        status = unsupported        covered = 0 / 3528        environment_mapping = unsupported

修复后：BuildingGridService().map(grid, building_grid_L8.gpkg,
                                 footprint_source=zhoushan_buildings.gpkg,
                                 footprint_aggregator=aggregate_footprint_facts)
        status = passed             covered = 3528 / 3528      用时 0.55 s
        environment_mapping = {
          status: passed, total_cells: 3528, covered_cells: 3528, unresolved_cells: 0,
          coverage_ratio: 1.0, grid_level: 7, level_aligned: false,
          level_independent_facts: true, participates_in_planner: true,
          mapping_basis: exact_footprint_intersection_and_centroid_allocation
        }
        851 格拿到建筑事实，最高建筑 88.82 m

任务 3：resolve_vector_layer_source(舟山建筑数据.qgz, role="buildings")
        → ok=True, source=qgis_project, layer_name="buildings"
        → 解析到 processed/zhoushan_buildings.gpkg（path_checks 无问题）
```

## 6. 修改文件清单

### 新增

| 文件 | 用途 |
|---|---|
| `cns_planner/gis/building_footprint_aggregation.py` | 层级无关建筑事实精确聚合 + 建筑轮廓 GeoJSON 读取 |
| `cns_planner/gis/path_resolver.py` | 统一来源路径解析 / 存在性 / 格式校验 |
| `cns_planner/gis/qgis_project_layers.py` | `.qgz` / `.qgs` → 图层清单与数据源解析 |
| `cns_planner/web/js/map/building_footprint_layer.js` | 建筑轮廓图层状态机 + LOD + 绘制 + 装配 |
| `tests/test_building_level_independent_facts.py` | 任务 1/2/3/4 后端契约与真实数据回归（24 项） |
| `tests/building_mapping_frontend.test.mjs` | 映射卡与建筑轮廓图层前端契约（10 项） |
| `cns_planner/domain/altitude_layer_defaults.py` | 任务 5：工程默认高度层（ALT-060/080/100/150/200）+ 幂等初始化判定 + 显式管理标记 |
| `tests/test_altitude_layer_lifecycle.py` | 任务 5：workspace 初始化 / 项目恢复 / catalog / planning request / API（14 项） |

### 修改

| 文件 | 修改 | 原因 |
|---|---|---|
| `cns_planner/data/mapping/buildings.py` | 三条映射路径 + `environment_mapping` 归一化 + 能力声明 | 任务 1 |
| `cns_planner/data/mapping/population.py` | `population_mapping` + `coverage_ratio` / `full_cells` / `partial_cells` / `missing_cells` | 任务 2 |
| `cns_planner/gis/map_data.py` | 矢量角色解析与缓存失效、聚合注入、`path_checks` / `building_sources` 输出 | 任务 1/3 |
| `cns_planner/gis/source_loader.py` | 统一路径校验、`.qgz/.shp/.geojson` 建筑来源描述 | 任务 3 |
| `cns_planner/domain/source_audit.py` | 签名变化时丢弃旧派生状态 | 任务 3 |
| `cns_planner/application/app_context.py` | `fields` 两种形态兼容（dict / list） | 任务 3 |
| `cns_planner/api/router.py` | `GET /api/building-footprints` | 任务 4 |
| `cns_planner/api/file_browser.py` | 扩展名表统一来自 `SOURCE_FORMATS` | 任务 3 |
| `cns_planner/web/index.html` | 建筑轮廓图层开关（默认关闭）+ 建筑来源提示 | 任务 3/4 |
| `cns_planner/web/js/main.js` | 建筑轮廓接线（LOD 同步 / 绘制 / 数据源变化重置） | 任务 4 |
| `cns_planner/web/js/map/display_layers.js` | `drawBuildingFootprints` 绘制钩子（网格之上、航路之下） | 任务 4 |
| `cns_planner/web/js/workflow/step02_workspace.js` | 映射卡改为覆盖统计 + 统一载体 | 任务 2 |
| `cns_planner/web/js/workflow/common.js` | `partial` / `unsupported` 文案 | 任务 2 |
| `cns_planner/web/js/sources/source_center.js` | 路径校验与建筑来源解析结论展示 | 任务 3 |
| `tests/workbench_shell.test.mjs` | 映射卡文案断言更新（**预期变化**，见 §6） | 任务 2 |
| `cns_planner/application/project_state.py` | `altitude_layer_defaults_initialized` 键（`blank_project` 默认 false + `normalize_project` 回填），**不在 normalize 中写入高度层** | 任务 5 |
| `cns_planner/application/session.py` | `WorkflowSession._load` 恢复时按需补建目录并登记 `pending_save`（保持只读） | 任务 5 |
| `cns_planner/application/workspace_service.py` | `set_workspace` 确认工作区后补建默认目录 + `refresh_spatial_status` | 任务 5 |
| `cns_planner/application/route_operating_layer_service.py` | `delete_altitude_layer` 置位"已显式管理"标记（删空的目录不再被补回） | 任务 5 |
| `cns_planner/web/js/workflow/layered_theta_v2.js` | 目录为空的明确提示文案常量 + ① 规划请求区块提示 | 任务 5 |
| `cns_planner/web/js/workflow/layered_route_planner.js` | 高度层下拉的空目录文案改为明确提示（仍不自动选择） | 任务 5 |
| `tests/frontend_modules.test.mjs` | 新增 2 项：下拉读取恢复后的 catalog / 空目录明确提示（**新增，无既有断言改动**） | 任务 5 |

---

## 7. 断言更新清单（诚实披露）

| 文件 | 更新 | 理由 |
|---|---|---|
| `tests/workbench_shell.test.mjs` | 人口映射 note 断言：`full 0 / partial 1 / missing 2 / outside 4 格` → `已覆盖 1 / 7 格（14%） · full 0 / partial 1 / missing 2 / outside 4` | 用户明确要求"不要简单显示缺少数据，改为显示覆盖统计" |
| `tests/workbench_shell.test.mjs` | 建筑环境映射 note 断言：`已覆盖 7 / 10 格` → `已覆盖 7 / 10 格（70%） · 未判定 3 格 · 既有建筑网格映射` | 同上；并显式给出映射依据 |
| `tests/test_grid_level_capability.py` | **未修改**：`available_levels=[8]`、`cross_level_aggregation_allowed=false`、非 L8 且无 footprint 源时仍 `unsupported` | 既有硬约束与语义保留；聚合是新增加的能力分支 |
| `tests/test_altitude_layer_lifecycle.py` 等高度层相关用例 | **未修改**：`altitude_layer_not_explicitly_selected`、`no_default_altitude_layer`、`altitude_layer_pending_confirmation`、显式删除后空目录等既有断言全部保留并继续通过 | 任务 5 只补 catalog 生命周期，未放宽任何高度检查 |

---

## 8. 复现命令

```powershell
# 全量后端
python -m pytest tests -q

# 本轮聚焦套件
python -m pytest tests/test_building_level_independent_facts.py -q
python -m pytest tests/test_architecture.py tests/test_grid_level_capability.py -q

# 前端（沙箱内用 node <file>，不用 node --test）
node tests/building_mapping_frontend.test.mjs
node tests/workbench_shell.test.mjs
node tests/frontend_modules.test.mjs
```

真实数据用例（`tests/test_building_level_independent_facts.py` 中的 4 项）依赖：

```text
D:\aaa2026project\UOM\舟山\规划系统\building\舟山建筑数据.qgz
D:\aaa2026project\UOM\舟山\规划系统\building\processed\zhoushan_buildings.gpkg
D:\aaa2026project\UOM\舟山\规划系统\building\processed\zhoushan_building_grid_L8.gpkg
```

文件缺失时这 4 项自动 skip（不会假装通过）。
