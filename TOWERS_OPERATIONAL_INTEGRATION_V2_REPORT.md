# Towers Operational Integration V2 Report

阶段目标（用户给定，只有三个）：

1. 前端把真实铁塔作为正式地图对象清晰展示；
2. CNS 设备规划优先利用真实铁塔进行共塔布设；
3. 铁塔高度在确有必要时进入航路障碍物/净空判断。

铁塔**不是**人口风险因子；本阶段**没有**修改 `0.8 / 0.1 / 0.1` Theta* objective，
没有修改 population×shelter / Risk Framework V2 / RouteRiskProfile 数学，
没有把 373 个铁塔自动转换成已有 C/N/S 设备，也没有杜撰任何铁塔上的设备参数。

---

## 1. HEAD / git status

| 项目 | 值 |
|---|---|
| `git rev-parse HEAD` | `f8ad33b894e79da14f1bad6237087ba21d853ca1` |
| `git log -1 --oneline` | `f8ad33b fix: unify route objective, grid metadata, and planning exposure` |
| `git status --short` | 上一阶段未提交修改（`M` 20 个文件、`??` 7 个）+ 本阶段新增/修改（见下表） |

本阶段严格遵守：**不** reset / stash / checkout / commit / push；未覆盖任何用户已有修改。
上一阶段的 towers 只读导入成果（`domain/towers.py`、`reference_data/towers.py`、
`web/js/map/tower_reference_layer.js`、`tests/test_towers_real_data.py`、
`tests/towers_map_layer.test.mjs`）在本阶段是**基线**，其中
`tests/test_towers_real_data.py` 只按 BUG-TOWERS-002 的修复更新了**失效断言**（§12）。

---

## 2. 当前 Towers V1 基线（审查结论，以 HEAD 代码为准）

* **TowerSite**（`cns_planner/domain/towers.py`）：只读站址事实，`SOURCE_ATTRIBUTE_FIELDS`
  只保留 `elevation_m / height_m / site_type / district / operator / address / remarks`，
  `FORBIDDEN_PERFORMANCE_FIELDS` 明确禁止通信性能字段。
* 导入链路：`/api/towers/import` → `WorkflowService.import_towers` →
  `ReferenceDataService.import_towers` → `state["towers"]`；`grid_attributes["towers"]`
  **永远为空**（铁塔不是网格属性）。
* 前端 V1：`towerLayer` 默认关闭；单点画**方形标记**；与起降点复用同一套
  `clusterPoints`；hover 提示与 click 详情只读。
* **本阶段开始时铁塔确实不参与任何规划**：不入 candidate site，不入 route feasibility，
  不入 Risk V2。唯一"参与"是 `invalidation_service.SOURCE_ATTRIBUTES` 里
  `"towers": ("towers",)` 这条**伪依赖**（BUG-TOWERS-002，见 §12）。

---

## 3. Tower 地图图标与 LOD

**图标（唯一来源）** —— `cns_planner/web/js/map/display_layers.js`：

* `TOWER_SYMBOL.segments`：10 段归一化折线，具备顶部天线 + 两层桁架 + 塔脚，左右严格镜像；
* `drawTowerSymbol(ctx,x,y,scale)`：canvas 路径（先白色 halo 再本体），**不使用 emoji、
  不使用外部网络图片、不引入 icon 包**；
* `towerSymbolSvg()`：同一份 `segments` 生成的 inline SVG，供统一图例使用。
  canvas 符号与图例符号**共用同一份几何定义**，不存在两套图标。
* 颜色沿用铁塔专用色 `#1f7a8c`，与起降场 `#1a8677`、参考航路点 `#783b69`、
  CNS facility `#7256a1/#e07a26` 均不相同。

**LOD（复用既有 `lod.js`，未新增第二套阈值）**：

| LOD | `ROUTE_STYLES[level].towerMarkerMode` | 行为 |
|---|---|---|
| overview | `cluster` | 只显示聚合点；聚合不住的孤立单塔**既不绘制也不命中** |
| medium | `cluster_isolated` | 仍以聚合为主；孤立塔画简化铁塔符号（scale 0.78、线宽 0.95） |
| detail | `single` | 每个真实铁塔都画独立铁塔图标（scale 1） |

* `towerSingleVisible(level)` 提供纯函数判定；`displayStyle()` 继续是唯一入口；
* detail 档 `CLUSTER_PIXEL_THRESHOLD.detail = 0` ⇒ 不聚合 ⇒ **373 个铁塔逐塔显示**
  （测试用 373 点 fixture 断言 `plan.towers.length === 373`）；
* 名称：`styles.nameMode` 对铁塔不适用 —— 铁塔循环里**没有任何 `placer.place`**，
  即永久不铺开 373 个名称标签，名称只在 hover/click 时出现。

**聚合**：继续用 `clusterPoints(...)`；聚合点显示 `N`（`drawAggregate`），
点击聚合点沿用既有"放大到该范围"机制（`entryExtent` + `fitScreenBox`）。

---

## 4. 图例集成

* 图例容器仍是**唯一**的 `#legend`（`index.html`），本阶段只在其内部新增
  `<div id="businessLegend" hidden></div>`；
* 新模块 `cns_planner/web/js/workflow/map_legend.js` 渲染两个分组：
  * **在线底图**（QGIS 服务瓦片）；
  * **真实业务数据**：起降点、真实参考航路、真实航路点、**通信铁塔**；
* 四个符号都与地图绘制同源：基础形状走 `markerSymbolSvg()`（与 `drawMarker` 同一
  `MARKER_SHAPES` 词表），铁塔走 `towerSymbolSvg()`（与 `drawTowerSymbol` 同一
  `TOWER_SYMBOL`）；
* 铁塔一行额外显示数据状态（`373 个` / `未配置`）与"图层已打开 / 图层默认关闭"；
* 没有新建 towers 专用 legend 容器（测试断言 `id="towerLegend"` 不存在、
  `id="businessLegend"` 只出现一次）；
* 图例刷新：`shell.js::bindLayerControls` 的图层 onchange 钩子 + `renderWorkflow()`，
  即图层开关与项目状态刷新都会同步图例。

---

## 5. TowerColocationCandidate

新增 `cns_planner/domain/tower_colocation.py`（派生层，**不改** TowerSite 契约）：

* `default_tower_colocation_policy()`：
  `strategy = prefer_tower_colocation_fallback_to_plain_candidates`、
  `reuse_class = tower_colocation_host`、`service_origin_assumption = null`、
  `planning_host_use_confirmed = false`、`physical_mount_confirmed = false`、
  `requires_site_survey = true`、`confirmed = false`（legacy 别名，= 规划层）；
* `normalize_tower_colocation_policy()`：只允许
  `service_origin_assumption ∈ {null, "tower_top_agl_0"}`；只有
  `planning_host_use_confirmed && assumption != null` 才 `enabled = true`
  （**FIX-TOWER-SEM-001**：规划宿主确认 ≠ 物理安装确认；legacy
  `device_mount_confirmed` 仅作为输入别名读取，canonical 值恒为 `false`）；
* `tower_colocation_candidate()` 产出的条目**复用既有 CandidateSite 契约形状**
  （`site_id / name / coordinate / elevation_m / site_type / available_subsystems /
  usable / locked / source / metadata / vertical_profile / planning_profile`），
  因此不新建第二套 candidate contract；
* 宿主溯源全部落在 `metadata`（该键在 `normalize_candidate_site` 与 reopen 时被原样保留）：

```json
"metadata": {
  "candidate_origin": "tower_colocation",
  "host": {"host_type": "tower", "host_tower_id": "...", "host_tower_name": "...",
           "host_site_type": "...", "host_structure_height_m": 45.0,
           "site_position_available": true, "device_mount_confirmed": false},
  "planning_origin": {"origin": "tower_colocation", "host_tower_id": "...", "source": {...}},
  "obstacle_status": {"status": "...", "base_type": "...", "tower_top_orthometric_m": ...},
  "source": {"type": "file_row", "file_name": "towers.xlsx", "sheet": "...", "row": 3},
  "evidence": [...], "limitations": [...]
}
```

**宿主可用性语义**：`usable=true`（位置是真实事实）、`locked=false`；
`planning_profile.add_device_allowed` 与 `vertical_profile.confirmed` 只有在策略
显式确认后才为 `true` —— 未确认时规划侧按 `ineligible/pending` 处理（fail-closed），
绝不因为铁塔存在就假定可以挂设备。

派生集合存放在**独立** state key `state["tower_colocation_candidates"]`，
不与用户导入的 `candidate_sites` 混合。

---

## 6. CNS 共塔优先算法

规划侧**没有**新增任何权重或代价修正；优先级来自既有 tier **词典序**：

```python
# cns_planner/domain/site_planning.py
REUSE_TIERS = (
    "existing_cns_facility",     # 已有 CNS 设备（零建设，最高优先）
    "existing_shared_site",      # 已有共享站址
    "tower_colocation_host",     # ← 本阶段新增：真实铁塔共塔宿主
    "candidate_site",            # 普通候选站址
    "new_build_candidate",       # 新建候选
)
TOWER_COLOCATION_REUSE_CLASS = "tower_colocation_host"
```

* `cns_planner/domain/cns_inputs.py::REUSE_CLASSES` 同步（两处常量保持一致）；
* `_candidate_actions()`（P11/P16 共用）新增 `tower_colocation` 入参，把共塔候选
  与普通候选站走**同一条** action 生成路径，`action_type = add_device_to_explicit_site`、
  `facility_id = null`；
* `ReuseFirstSitePlannerV1.plan()` 外层按 `REUSE_TIERS` 逐 tier 贪心：tier 内的
  `marginal / cost` 排序与既有语义完全不变，因此共塔候选在**任何**普通候选站之前被
  尝试；共塔 tier 选完还有缺口时，后面两个 tier 照常补盲；
* `CorridorReuseFirstSitePlannerV2`（P16）用同一份 `REUSE_TIERS` 与同一个
  `_candidate_actions`，行为一致。

规划流程与用户给定一致：

```
CNS Gap → 可用真实 Tower Candidate → 优先尝试共塔补盲 → 仍存在 gap？
  ├─ 否 → 返回 tower-colocation 方案
  └─ 是 → 再加入普通 candidate site（fallback）
```

语义是 **prefer tower colocation**，不是 force。已有 CNS 设备复用仍然绝对优先
（`existing_cns_facility` 在共塔 tier 之前），因为那条路径不产生任何工程动作。

### 6.1 设备参数来源（未变）

设备型号与性能仍只来自 `state["device_catalog"]`（以及只读的
`equipment_reference_catalog`）。共塔候选**不携带**任何
`transmit_power / frequency / coverage_radius / capacity / MTBF / MTTR / cost`，
`available_subsystems` 保持空数组（不杜撰塔上能装 C/N/S 中的哪一个）。
`_apply_hypothetical_action` 仍然只写
`{device_id, subsystem, status, service_model:{status:"missing_data"}}`。

---

## 7. 普通 candidate fallback

* 共塔候选只有在 `tower_colocation_policy.enabled` 时才 eligible；
  未确认（默认）时它是 `ineligible`（"站点未明确允许增加设备"），
  普通候选站的 action 不受影响，仍可能被选中；
* 共塔候选**不能满足**全部 planning gap 时（`marginal <= 0` 或收益不足），
  普通候选站/新建候选继续补盲，`unresolved_segments` 照常报告剩余缺口；
* 允许"没有合适铁塔时新建站点"：`allow_arbitrary_new_coordinates` 仍为 `false`
  （不自动生成任意新坐标），新建候选仍必须来自显式候选站址数据。

---

## 8. TowerObstacleProfile

新增 `cns_planner/domain/tower_obstacle.py`（**派生层**，不改 TowerSite 契约）：

```
tower_id / name / longitude / latitude / site_type
base_type                : ground | rooftop | unknown
base_type_source         : site_type_rooftop_marker | site_type_ground_marker
                           | explicit_policy_default_base_type | site_type_missing
                           | site_type_unclassified
terrain_elevation_m      : FABDEM DTM（EGM2008 正高）
terrain_vertical_reference / terrain_status / terrain_reason
building_height_m / building_height_status / building_height_source / building_height_reason
tower_structure_height_m / tower_structure_height_source
tower_top_orthometric_m  : 派生塔顶（resolved 时非空）
horizontal_status        : source_coordinate_used_as_is
vertical_status          : egm2008_orthometric_resolved | tower_structure_height_missing
                           | terrain_elevation_unresolved | building_height_unresolved
                           | base_type_unknown
status                   : resolved | unresolved
source / evidence / limitations
```

集合契约 `state["tower_obstacle_profiles"]`：
`{status, collection_id, schema_version, semantics, not_a_population_risk_factor: true,
not_a_risk_framework_v2_input: true, count, resolved_count, unresolved_count, policy,
source, items{tower_id: profile}, warnings}`。

一次性计算入口：`POST /api/tower-obstacle-profiles/evaluate`
（`app_context.evaluate_tower_obstacle_profiles`，只在 QGIS 线程读取真实源）。

---

## 9. ground / rooftop 高度推导

| base_type | 公式 | 失败行为 |
|---|---|---|
| ground | `terrain_egm2008 + tower_structure_height_m` | 塔高缺失 → `tower_structure_height_missing`；地形不可用 → `terrain_elevation_unresolved` |
| rooftop | `terrain_egm2008 + building_height_at_tower + tower_structure_height_m` | 建筑高度未解析 → `building_height_unresolved`（**绝不当作 0**） |
| unknown | 不推导 | `base_type_unknown`（把未知当 ground 会低估楼面塔，属不安全方向） |

* 楼面识别特征：`楼面 / 屋顶 / 屋面 / rooftop / roof`（真实数据中的
  "楼面拉线塔 / 楼面抱杆 / 楼面增高架 / 楼面美化天线外罩" 全部命中）；
* 地面识别特征：明确写了 `地面 / 落地 / ground` 才算 ground；
* 未命中任何特征的 `site_type`（例如仅写"角钢塔"）⇒ `unknown`，
  唯一允许的覆盖方式是显式配置
  `tower_obstacle_policy.default_base_type = "ground" | "rooftop"`；
* 楼面塔建筑高度来源：`cns_planner/gis/tower_obstacle_adapter.py` 在**真实建筑足迹
  数据源**上按塔坐标做精确包含查询（`read_footprints` + shapely `intersects`），
  取命中足迹的最大高度（保守方向）；命中但高度字段缺失 → unresolved；
  一个足迹都没命中 → `tower_coordinate_matches_no_building_footprint`；
* 因此楼面塔的高度事实**不经过 L7/L8 建筑网格**，工作区降级不会让楼面塔塔顶被粗化。

---

## 10. 垂直基准处理

* 源数据 `elevation_m` 的垂直基准**未确认**，因此**永远不**用作 EGM2008 正高；
  只作为事实留档（`legacy_elevation_m` 与 click 详情里的"源数据海拔"）；
* 唯一可接受的塔基地形高程来自已确认的 FABDEM DTM：
  `terrain_source.usable()` + `vertical_reference == "egm2008_orthometric"`；
  基准未确认时整批 tower terrain fact 为 `unresolved`（复用既有 fail-closed 语义）；
* 楼面塔的建筑高度与地形高度相加前，两者都必须是 EGM2008 正高事实；
* 塔顶结果只用于**航路障碍物/净空**；`TowerObstacleProfile` 带
  `not_a_population_risk_factor: true` / `not_a_risk_framework_v2_input: true`。

---

## 11. Route tower clearance

### 11.1 事实装配（GIS 边界，`gis/layered_feasibility_adapter.py`）

* `tower_facts_by_cell(grid_cells, state)`：**以原始塔坐标为权威事实**，网格只充当
  "哪些 cell 受该塔影响"的加速索引；
* 影响范围 = 塔点按显式 `tower_clearance_policy.tower_horizontal_clearance_m`
  扩展后的 bbox（未配置时为 0，即只影响塔所在 cell）；
* 每个 cell 的 tower fact：
  `{data_status, tower_count, resolved_count, unresolved_count,
    tower_top_max_egm2008_m, reason}`；同格多塔取**最高塔顶**（保守）；
  只要格内有任何一塔高度未解析，`data_status = unknown`；
* 塔坐标在整条链路上**从未被取整、聚合或吸附到格心**（测试用"塔在格外的邻近格"
  与"水平净空扩展"两种情形锁定）。

### 11.2 判定（`layered_route_planner/planner.py::build_layer_feasibility_mask`）

顺序与 `COARSE_ENVELOPE_SEMANTICS` 一致：**terrain → building → tower**。

| 条件 | 结果 |
|---|---|
| 格内有塔，`tower_clearance_policy` 未确认 | `unknown / tower_clearance_not_configured` |
| 格内有塔，塔顶未解析 | `unknown / tower_height_unresolved` |
| `altitude < tower_top_max + tower_vertical_clearance_m` | `blocked / altitude_below_tower_clearance_floor` |
| 否则 | 保持 terrain/building 判定结果（`tower_status = passed`） |
| 格内无塔 | `not_applicable`（完全不影响该格原判） |

* `blocked` 只覆盖非 blocked 的原状态（不会把"地形更低"的 blocked 原因改写）；
* `unknown` 只覆盖 `feasible`（不会把 blocked 降级为 unknown）；
* mask 顶层新增：`tower_vertical_clearance_m / tower_horizontal_clearance_m /
  tower_clearance_policy_status / tower_clearance_source /
  tower_obstacle_profile_status / tower_cell_count / tower_unresolved_cell_count /
  tower_obstacle_semantics`；
* `mask_cell` 新增 `tower_count / tower_unresolved_count / tower_top_max_egm2008_m /
  tower_required_clearance_egm2008_m / tower_margin_m`；
* `input_fingerprint` 与 `mask_fingerprint` 显式登记 tower policy 与 tower facts
  （新依赖必须进指纹，否则"改了塔却复用旧 mask"）。

### 11.3 搜索期（`layered_route_planner/theta_star_v2.py`）

* `LOS_REJECTION_REASONS` 新增 `"tower"`；`_build_gate.classify` 把
  `altitude_below_tower_clearance_floor` 映射到 domain `tower`；
* **objective 与 cost 完全未改**：`USER_DEFINED_BASELINE_WEIGHTS` 仍是
  `{"risk": 0.8, "turn": 0.1, "distance": 0.1}`，`_ledger_total_cost` /
  `_objective_metrics` / `_extend_ledger` 未改动一行；
* 塔净空是 **hard constraint**（不可穿越），不是代价项。

### 11.4 horizontal clearance 与 policy

* 先查了既有 clearance policy：repo 中**不存在**可复用的 general horizontal
  clearance / corridor half-width / aircraft separation policy
  （`building_clearance_policy.horizontal_clearance_m` 只服务 V3-C 建筑走廊，
  `safety_policy` 只有时间维 separation）；
* 因此新增显式可配置 `state["tower_clearance_policy"]`：
  `{tower_vertical_clearance_m, tower_horizontal_clearance_m, source, confirmed, status}`，
  **两个净空都没有默认值**；
* 未配置时 `status = "not_configured"`，readiness 明确显示该状态，
  且任何含塔 cell 都是 `unknown`（fail-closed），不会因为"没配置"而放行；
* readiness 新增 `tower_clearance_policy` 段：
  `{status, confirmed, tower_vertical_clearance_m, tower_horizontal_clearance_m, source,
    tower_count, obstacle_profile_status, obstacle_profile_resolved_count,
    obstacle_profile_unresolved_count, mask_tower_cell_count,
    mask_tower_unresolved_cell_count, semantics: "obstacle_clearance_not_a_risk_factor",
    in_risk_framework_v2: false, in_theta_star_objective: false}`。

### 11.5 L7/L8 降级

* `PLANNER_CAPABILITY` 里"工作区被 coarsen 时建筑约束实际不参与"的事实**未被改动**；
* 塔不受该影响：塔事实按**源坐标**生成，与工作区层级无关；
  唯一与水平净空有关的是显式 policy 值，不是网格层级。

---

## 12. Invalidation DAG

### 12.1 修复 BUG-TOWERS-002（根因）

`application/invalidation_service.py`：

```python
# 之前：towers 被映射到一个永远为空的网格属性命名空间
"obstacles": ("towers",), "towers": ("towers",),
# 现在：铁塔不是网格属性、不是风险输入
"obstacles": (), "towers": (),
```

于是 `grid_sources({"towers"})` 不再进入 `risk()` / `risk_v2()` / `mark_active_report_stale`
那条分支 —— `grid_risk` / `grid_risk_v2` / `environment_risk` **不再**因为铁塔变化而 stale。

### 12.2 新增定向失效入口

```python
def tower_data_changed(self, reason="tower_data_changed", *, include_derived=False):
    ...
    self.layered_route(reason)          # layered candidate/mask（塔净空进入 feasibility）
                                        # → 既有语义连带 route_risk_profiles /
                                        #   layered_route_validations / Safety Evidence
    self.cns_site_plan()                # 共塔宿主候选变化 ⇒ P11 提案过时
    self.cns_corridor_site_plan()       # P16 同样消费共塔候选
    mark_active_report_stale(state, reason)
```

`include_derived=True` 时额外把 `tower_obstacle_profiles` /
`tower_colocation_candidates` 标 stale（只有**源**变化才需要）。

调用点：

* `grid_sources({"towers"|"obstacles"})` → `include_derived=True`；
* `WorkflowService.import_towers()` → `include_derived=True`
  （补上"/api/towers/import 是失效盲区"的缺口）；
* `configure_reference_sources` 的 towers 首次恢复 → `include_derived=True`；
* `TowerObstacleService.evaluate()` 重算后 → `include_derived=False`
  （刚算出来的派生事实保持 current，只失效下游）。

**不**受影响：`grid_risk` / `grid_risk_v2` / `environment_risk`
（铁塔不是 population×shelter 或 Risk Framework V2 的输入）。
RouteRiskProfile 会随候选航路变化而 stale —— 那是"路线变了"的既有语义，
不是"因为铁塔而重算风险网格"。

---

## 13. 前端交互

* **塔 click 详情**（`tower_reference_layer.js`）：源字段
  （tower_id / name / 经纬度 / 源数据海拔 / 塔身高度 / district / site_type /
  来源文件 · sheet · 行）+ **规划采用塔顶高程** + **CNS 共塔候选：是/否 + 关联 candidate_id**；
  * 塔顶信息来自已落库的 `flow.tower_obstacle_profiles.items[tower_id]`；
    unresolved 时明确显示"未解析 + vertical_status"，**绝不显示编造的数字**；
  * 源数据海拔只写"源数据海拔"，**不声明**它是 EGM2008 航路高度。
* **CNS 共塔候选展示**（`step05_cns.js`）：候选站址页新增
  "共塔候选（真实铁塔宿主）"小节（Tower ID / Tower Name / Tower Type /
  设备挂载是否确认 / 塔顶高程 / 位置可用性），并有"从真实铁塔派生宿主候选"按钮
  （`/api/tower-obstacle-profiles/evaluate`）；P11 结果里的候选 action 与选中 action
  标出"共塔候选"徽章，汇总行区分
  已有站点 / 共享站址 / 共塔候选 / 普通候选 / 新建候选。
* **地图关联**：共塔候选画成"候选色的铁塔符号"（`CNS_COLORS.candidate`），
  与塔图层同位置但可区分；**不画任何永久连接线**；
  点击共塔候选 → 设置 `towerHighlight`（纯 UI 状态）→ 宿主铁塔画高亮环
  （`TOWER_HIGHLIGHT_COLOR`）；overview 档也会保留被高亮的那一个塔，避免"点了候选看不到塔"。
* **默认图层状态不变**：`towerLayer` 仍默认关闭；全页仍只有 `online` 默认勾选。

---

## 14. 修改文件

### 新增（7）

| 文件 | 作用 |
|---|---|
| `cns_planner/domain/tower_obstacle.py` | TowerObstacleProfile 契约 + 纯函数推导 + clearance/obstacle policy |
| `cns_planner/domain/tower_colocation.py` | TowerColocationCandidate 契约 + 共塔 policy + 派生 |
| `cns_planner/gis/tower_obstacle_adapter.py` | FABDEM 逐塔采样 + 真实建筑足迹包含查询 |
| `cns_planner/application/tower_obstacle_service.py` | 一次动作派生两类事实 + 定向失效 |
| `cns_planner/web/js/workflow/map_legend.js` | 统一图例的"在线底图 / 真实业务数据"分组 |
| `tests/test_towers_operational_integration_v2.py` | 后端 38 项断言 |
| `tests/towers_operational_v2_frontend.test.mjs` | 前端 12 项断言 |

### 修改（22）

| 文件 | 改动 |
|---|---|
| `cns_planner/domain/site_planning.py` | `REUSE_TIERS` 插入 `tower_colocation_host`；新增常量；`CandidateAction` 补 `planning_profile/host/planning_origin` |
| `cns_planner/domain/cns_inputs.py` | `REUSE_CLASSES` 同步 |
| `cns_planner/domain/layered_route.py` | 3 个 tower reason code；mask 语义/默认字段；`mask_cell` tower 字段；`mask_fingerprint` tower 成分 |
| `cns_planner/gis/layered_feasibility_adapter.py` | tower facts（`TOWER_FACT_KEYS` / `tower_facts_by_cell` / `_tower_fact` / 水平净空换算），adapter 1.1 |
| `cns_planner/layered_route_planner/planner.py` | tower clearance 判定链、mask 顶层字段、`_tower_profile_status`、input fingerprint |
| `cns_planner/layered_route_planner/theta_star_v2.py` | `LOS_REJECTION_REASONS += tower`；gate domain 映射（objective 未动） |
| `cns_planner/application/site_planning_service.py` | `_candidate_actions(..., tower_colocation)`；`_action` 透出 host/planning_origin |
| `cns_planner/application/corridor_site_planning_service.py` | 同上（P16） |
| `cns_planner/application/invalidation_service.py` | 修复 towers 伪依赖；新增 `tower_data_changed` |
| `cns_planner/application/project_state.py` | 5 个新 state key 的默认值/归一化/result_statuses 兜底 |
| `cns_planner/application/workflow_service.py` | 服务接线、4 个快照方法、快照共享键、import_towers 失效 |
| `cns_planner/application/app_context.py` | `evaluate_tower_obstacle_profiles`（QGIS 线程） |
| `cns_planner/application/layered_route_planner_service.py` | 传入 tower policy；readiness 新增 tower 段 |
| `cns_planner/api/router.py` | 3 个 GET + 1 个 POST |
| `cns_planner/web/index.html` | `#businessLegend` |
| `cns_planner/web/css/map.css` | 图例分组/行/符号样式 |
| `cns_planner/web/js/map/lod.js` | `TOWER_MARKER_MODES`、每档 `towerMarkerMode/towerSymbolScale`、`towerSingleVisible` |
| `cns_planner/web/js/map/display_layers.js` | 铁塔矢量符号（canvas + SVG）、共塔候选绘制/命中、高亮环、plan 字段 |
| `cns_planner/web/js/map/tower_reference_layer.js` | 塔顶/共塔详情、`towerDetailContext`、`candidateClick`、模块内高亮状态 |
| `cns_planner/web/js/workflow/step05_cns.js` | 共塔候选面板、徽章、`reuseClassLabel` |
| `cns_planner/web/js/main.js` | 图例刷新、`towerHighlight` 接线、click 优先级、保持 450 行轻入口 |
| `cns_planner/web/js/shell.js` | 图层开关 → 图例刷新钩子 |
| `tests/test_towers_real_data.py` | BUG-TOWERS-002 失效断言改为新行为（唯一被修改的既有测试） |

---

## 15. 新增测试

### 后端 `tests/test_towers_operational_integration_v2.py`（42 项）

* TowerObstacleProfile：地面塔、楼面塔、楼面缺建筑高度、部分覆盖、地形基准未确认、
  塔高缺失、未知塔型 + 显式策略覆盖、逐塔独立、clearance policy 无默认值、
  persistence round-trip；
* TowerColocationCandidate：宿主溯源、不杜撰设备参数、未确认策略保持 pending、
  塔顶未解析不确认 service origin、persistence 保留宿主关联；
* 共塔优先：tier 顺序、eligible action 的 host 保留、planner 选中共塔、
  共塔不可用时 fallback 普通候选、绝不生成 existing facility；
  **端到端（走真实 P11 `evaluate_cns_site_plan`，含 P7/P8 what-if）**：
  共塔候选被首个选中且 `candidate_site` 计数为 0；共塔策略未确认时普通候选站被选中；
* 航路塔净空：无塔格不受影响、低于塔顶 floor 被 blocked、高于则 feasible、
  未确认策略 unknown、塔顶未解析 unknown、已有 blocked 不被降级、
  reason code 登记、水平净空扩展、塔坐标不被粗化；
* 边界：objective 权重未变、`towers` 不在 `FACTOR_INPUT_ATTRIBUTES`、
  `risk/*` + `domain/route_risk_profile.py` + `domain/population_shelter.py` 源码**不引用** towers；
* API/快照：4 个端点已注册、3 个快照投影契约、派生层以只读共享引用进入快照；
* 失效：铁塔变化不 stale grid_risk/grid_risk_v2、定向 stale 派生事实与下游、
  重算只 stale 下游、terrain 变化仍 stale grid_risk_v2、save/reopen 保留、
  normalize 回填、非法 policy 被拒。

### 前端 `tests/towers_operational_v2_frontend.test.mjs`（13 项）

符号为本地矢量且左右镜像、图例与地图共用同一几何、图例位于唯一 `#legend` 容器内、
铁塔图例行使用同一符号、detail 逐塔 373 / overview 仅聚合、单塔分支画铁塔符号而不退回方形标记、
LOD 复用 displayStyle 且无第二套阈值、
铁塔不铺开标签且 hover/click 保留、默认图层不变、共塔候选可命中、
点击候选高亮宿主塔且不发请求、塔 click 详情（源字段 + 塔顶 + 共塔关联 + unresolved 不编数字）、
Step05 共塔徽章与复用类别区分。

### 与用户 30 项要求的对应

| 要求 | 覆盖 |
|---|---|
| 1–5 MAP-TOWER-ICON | 前端测试 1–4 + 既有 `tests/towers_map_layer.test.mjs`（6 项） |
| 6–12 CNS-TOWER-COLOCATION | 后端 §"共塔优先" 9 项 + persistence |
| 13–19 ROUTE-TOWER-CLEARANCE | 后端 mask/facts 7 项 |
| 20–21 objective / RRP 未改 | `test_theta_v2_objective_weights_are_untouched` + 既有 `test_layered_theta_star_v2.py::test_objective_zero_point_eight...` + `test_route_risk_profile.py` 全绿 |
| 22–25 INVALIDATION | 后端 4 项 + 更新后的 `test_towers_real_data.py` |
| 26–30 FRONTEND | 前端 5 项 |

---

## 16. 全量回归

见本节末尾的实测结果（Python `pytest tests` 全量 + 18 个前端 `.test.mjs` 逐文件执行）。

**前端（18 个文件，逐文件执行）**

```
bug_shelter_ui_001_confirmed_contract.test.mjs   pass=9  fail=0
bug_ui_pop_002_coverage_scopes.test.mjs          pass=8  fail=0
building_mapping_frontend.test.mjs               pass=10 fail=0
frontend_modules.test.mjs                        pass=83 fail=0
grid_details_summary.test.mjs                    pass=7  fail=0
layered_route_map_evidence.test.mjs              pass=15 fail=0
layered_validation_adoption_frontend.test.mjs    pass=31 fail=0
map_default_layers.test.mjs                      pass=4  fail=0
map_lod_clustering.test.mjs                      pass=19 fail=0
map_measure_tool.test.mjs                        pass=12 fail=0
phase35_feasibility_policy_save_chain.test.mjs   pass=7  fail=0
phase35_stabilization_frontend.test.mjs          pass=2  fail=0
population_nodata_frontend.test.mjs              pass=11 fail=0
route_risk_profile_frontend.test.mjs             pass=14 fail=0
route_safety_evidence_frontend.test.mjs          pass=8  fail=0
towers_map_layer.test.mjs                        pass=6  fail=0
towers_operational_v2_frontend.test.mjs          pass=13 fail=0
workbench_shell.test.mjs                         pass=48 fail=0
```

合计 **307 项前端断言、18 个文件、0 失败**（`FILES_WITH_FAILURES=0`）。

```text
$ python -m pytest tests -q -p no:cacheprovider
1481 passed, 6 skipped in 484.42s (0:08:04)     [exit code: 0]
```

对比：本阶段开始前的同一条命令为 `1474 passed, 6 skipped`（并含本阶段之前
towers V1 的既有测试）。回归中修改过的唯一既有测试是
`tests/test_towers_real_data.py::test_towers_source_is_a_tracked_source_for_directed_derived_invalidation`
（BUG-TOWERS-002 的锁定断言按修复后的行为更新，见 §12），
其余 22 个前端文件与全部 Python 测试**零修改**通过。

回归期间唯一观察到的噪声是 pytest 会话结束时对系统临时目录的清理
（`PermissionError: [WinError 5]`）——那是 DSH 文件沙箱对 `%TEMP%` 的限制，
与测试结果无关（已由 `tests/conftest.py` 的 tmp_path 替身覆盖测试自身临时目录）。

---

## 17. 未修改内容

以下内容在本阶段**一行未改**（并有测试锁定）：

* Theta* V2 any-angle 与 parent LOS rewiring（`theta_star_v2.py` 的搜索/代价/objective 部分）；
* `0.8 / 0.1 / 0.1` objective 权重与公式（`domain/layered_theta_v2.py` 未改）；
* population × shelter（`domain/population_shelter.py`、`risk/*` 未改）；
* Risk Framework V2 数学（`risk/factors_v2.py`、`risk/domains_v2.py`、`risk/model_v2.py` 未改）；
* RouteRiskProfile 数学（`risk/route_profile.py`、`domain/route_risk_profile.py` 未改）；
* AirspacePolicy（`domain/safety_policy.py`、`data/mapping/airspace*` 未改）；
* Operational Adoption 核心语义（各 `*_adoption_service.py` 未改）；
* 原始 towers Excel（未入库、未修改）；
* `domain/towers.py` 与 `reference_data/towers.py` 的 TowerSite 契约（未改）；
* 既有 `towerLayer` 默认关闭策略（未改）。

---

## 18. 已知限制

1. **未知塔型不生成净空 floor**：真实源里的 `site_type` 若既不含"楼面/楼顶/屋顶"也不含
   "地面/落地"，base_type 为 `unknown` ⇒ 该塔 obstacle `unresolved` ⇒
   **塔顶高度未解析时不生成具体 tower clearance floor；相关空间保持 unknown，
   并在路径搜索中 fail-closed，不得作为已验证安全可通行区域**。
   要么补源数据分类，要么显式配置 `tower_obstacle_policy.default_base_type`。
2. **楼面塔依赖建筑源**：未配置建筑数据源、建筑源 CRS 不可判定、或塔坐标不落在任何
   足迹内时，楼面塔同样不生成 tower clearance floor（保持 unknown + fail-closed，绝不补 0）。
3. **净空 policy 无默认值**：未配置 `tower_clearance_policy` 时，任何含塔 cell 都是
   `unknown`，航路无法穿越这些 cell。这是有意为之（不给没有依据的业务值）。
4. **mask 是 cell 级粗包络**：同一 cell 内只要有塔，整格按"格内最高塔顶 + 显式垂直净空"
   判定（比点更保守）。对候选 route corridor 的逐点精确塔净空仍需连续验证阶段完成
   （与既有"exact footprint / horizontal clearance 留给连续验证"的分工一致）。
5. **水平净空是 bbox 近似**：以塔为中心的经纬度 bbox（米→度按纬度近似换算），
   即 `tower_horizontal_clearance: explicit_policy_bbox_envelope` /
   `coarse_bbox_envelope_not_exact_radial_clearance`，**不是**精确圆形/欧氏水平净空
   （精确 corridor clearance 仍留给连续验证阶段）。
6. **共塔候选使用塔顶作为服务原点假设**：只有在显式确认
   `service_origin_assumption = "tower_top_agl_0"` 时才确认 service origin；
   真实设备挂高仍需用户在 adoption 阶段确认，本阶段不推断挂高。
7. **规划宿主 ≠ 物理安装确认**（FIX-TOWER-SEM-001）：全局策略只确认
   `planning_host_use_confirmed`；`physical_mount_confirmed` 恒为 `false`、
   `requires_site_survey` 恒为 `true`，`subsystem_mount_status` 恒为 `unverified`
   （没有逐塔分系统安装证据，需现场勘察）。
7. **P16 同步接入**：共塔 tier 同时进入 corridor site planner 的 tier 词典序，
   但其 what-if/目标语义完全未改。
8. **点击优先级**：共塔候选与宿主铁塔坐标完全重合，click 优先给候选详情并高亮塔；
   塔自身详情在候选站图层关闭时或 hover 时可见。
9. **未新增 report/export 章节**：铁塔派生事实随 `flow` 快照与 `project_state.json`
   持久化，报告暂未单列塔净空章节。

---

## 19. 人工验收步骤

1. 启动地图服务，打开页面，确认**默认只勾选在线底图**，铁塔图层未勾选。
2. 展开图例（左下"图层图例"），确认出现"在线底图 / 真实业务数据"两组，
   其中"通信铁塔"使用塔架矢量符号（非方块、非 emoji），并显示"图层默认关闭"。
3. 打开"图层 → 通信铁塔站址"：overview 只看到聚合点（带数量）；
   滚轮放大到 detail 档，确认每个真实铁塔显示为铁塔图标，且**没有任何名称标签铺开**；
   hover 出现简短提示，click 出现详情（含源数据海拔 / 塔身高度 / 来源行）。
4. 数据源中心点"导入铁塔站址"（373 条），确认导入后风险与覆盖结果**没有**变成 stale。
5. Step05 → 候选站址 → 点"从真实铁塔派生宿主候选"：
   * 确认共塔候选列表出现（Tower ID / Name / Type / 塔顶高程 / 设备挂载未确认）；
   * 若未配置 FABDEM DTM 会明确报错；若塔型无法判定则塔顶显示"未解析"。
6. Step05 → 走廊站址优化/Reuse-first：确认提案里共塔候选排在普通候选之前，
   并且共塔策略未确认时**不会**被选中（普通候选站照常补盲）。
7. Step03 → 分层候选：配置 `tower_clearance_policy`（垂直 + 水平净空）后运行，
   确认含塔 cell 在巡航高度低于"塔顶 + 垂直净空"时为 blocked（reason
   `altitude_below_tower_clearance_floor`）；未配置时该 cell 为 unknown
   （`tower_clearance_not_configured`），readiness 显示 `not_configured`。
8. 在地图上点击一个共塔候选，确认宿主铁塔被高亮（只画一圈），
   并且没有出现从候选到铁塔的永久连接线。
9. 保存项目并重新打开，确认共塔候选的 `host_tower_id` 关联与塔顶事实仍在。
10. 确认 `git status`：本阶段改动全部为工作区修改，**没有** commit / push。
