# Route Planner V3 架构：3D 战略规划（V3-A）+ corridor-local 精化（V3-B）

> 状态：**V3-A 已实现**（战略搜索 + 硬约束内核 + provenance soft cost 向量 + refinement corridor proposal），并已完成三项正确性修复（admissible heuristic / 删除隐式 normalizer / expansion cap 语义）。
> **V3-B 已实现**（corridor-local 米制 fine grid + fine environment adapter + multi-cell stride 精化搜索 + refinement fingerprint/staleness）。
> 明确**未实现**：V3-C exact polygon / terrain / continuous clearance validation；V3-D validated route → operational adapter → CNS Assessment；Route–CNS 联合优化（未来项，不是 V3-D）；energy 模型。
> 本文件描述 V3 的目标架构与 V3-A/V3-B 的落地边界；凡标 **V3-A 现行** / **V3-B 现行** 的是已实现语义，标 **V3-C/D** 的是后续接口而非承诺。

---

## 0. 阶段路线图

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| V3-A | L8 × 高度 × heading 战略搜索、硬约束内核、provenance soft cost、candidate refinement corridor | 已实现 |
| V3-B | 在 V3-A corridor 的 support cells 米制窗口内构造局部 fine grid，做 corridor-local 3D 精化（多 cell stride、逐 traversed cell 检查） | 已实现 |
| V3-C | exact polygon membership、exact terrain profile clearance、沿整条轨迹的 continuous clearance 验证、连续曲率验证 | 未实现 |
| V3-D | validated route → operational adapter → 既有 P7/P8/P9/P10 CNS Assessment | 未实现 |
| 未来 | Route–CNS 联合优化（CNS 进入 cost/约束）；energy 模型 | 未来 backlog |

---

## 1. 为什么是「原生 3D」而不是「2D 航路后配高度」

V1/V2 是**二维战略水平**规划：先得到水平 path，再由 P7 `spatial_3d` 的 `route_altitude_profile` 事后配高度。这种分解在 V3 中被明确放弃，原因不是风格问题，而是它使安全与运动学约束无法在搜索中生效：

- 转弯能力依赖**航向**，而航向是路径的历史属性。2D 网格搜索的 state 只有 `grid_id`，转弯半径只能在生成后「检查」，无法在生成时「约束」。
- 地形/建筑净空依赖**高度与水平位置的联合**。事后配高度无法保证「配出来的高度在这条水平 path 上处处可用」，只能事后发现 breach。
- 因此 2D + 事后配高度的输出，其可行性是**逐阶段局部成立、整体不保证**。

**V3-A/V3-B 现行**：搜索 state 直接包含高度与航向，约束在 edge/primitive 生成时判定。

### 1.1 搜索 state

```
V3State = {
  grid_id,                  # V3-A: L8 MH/T 网格；V3-B: fine_cell_id（局部米制细网格）
  altitude_index,           # 高度层序号（显式 policy 离散）
  altitude_egm2008_m,       # 该高度的 canonical 垂向值
  heading_bin,              # 航向分箱（默认 8 箱 = 45°）
  x, y, z                   # z === altitude_egm2008_m
}
```

- 几何位置是 `x/y/z`；**canonical vertical reference 固定为 `egm2008_orthometric`**，复用项目既有语义（`domain/spatial_3d.resolve_egm2008_height`）。V3 不接受 AGL/WGS84 椭球高直接进入搜索。
- 添加 `heading_bin` 的唯一目的是让「转弯」成为 Markov 属性。
- **航向永远由运动几何推导**（edge/primitive 的实际方位角），不由调用方声明。V3-B 的方位角在**局部米制帧**内计算，帧的 CRS/origin/axis/mapping method 全部记录在 `LocalMetricPlanningFrame` 中。

**现行端点语义**：起点无历史，因此起点在**所有** heading bin 上都是等价的合法初始状态（代价 0）。终点是 **cell**（加高度，仅当问题显式要求 z）而不是 heading。V3-B additionally records how each endpoint was bound to a fine cell（`endpoint_binding`：显式 fine cell id / metric 坐标 / 经纬度最近 cell / 确定性 fallback），从不隐含假设。

---

## 2. Hard vs soft：分离的语义

### 2.1 Hard（不可违反，fail-closed）

**V3-A 现行**，由 `HardConstraintEvaluator` 分两向判定：

| 方向 | 判定内容 | reason 标识 |
| --- | --- | --- |
| `state_feasible` | allowed airspace（只有 `confirmed_allowed` 可行） | `airspace_not_confirmed_allowed` / `airspace_unknown` |
| | 高度带上下界 | `altitude_above_max` / `altitude_below_min` |
| | 地形净空（cell surface clearance floor） | `terrain_clearance_unresolved` / `below_terrain_clearance` |
| | 建筑净空（cell required vertical clearance） | `building_clearance_unresolved` / `below_building_clearance` |
| `transition_feasible` | 邻接/edge 空域连续性 | `neighbor_not_adjacent` |
| | 转弯能力（最小转弯半径弧长） | `turn_capability_unknown` / `turn_radius_exceeded` |
| | 爬升能力 | `climb_capability_unknown` / `climb_gradient_exceeded` |
| | 下降能力 | `descent_capability_unknown` / `descent_gradient_exceeded` |
| | 高度步长与纯垂直过渡 | `altitude_step_not_single_band` / `vertical_only_transition_not_modelled` |

**V3-B 现行**，在同一个 state/transition 分离上增加第三类判定：**traversed cell 检查**（见 §7）。

**fail-closed 规则**：任何安全数据 unknown/NoData 一律不可行，绝不当作「无约束」或「通过」。

- 地形：每个 cell 必须给出 `surface_clearance_egm2008_m`；缺少或 `data_status != passed` 即 blocked。
- 建筑：`data_status != passed` 即 blocked；`data_status == passed` 且无 clearance 表示该 cell **确认无建筑约束**（confirmed absence）。
- 空域：`confirmed_allowed` 才可行；`confirmed_restricted` 与 `unknown` 都不可行。

### 2.2 Soft（可权衡，必须显式 + 必须有 provenance）

**V3-A 现行（含正确性修复）**：

```
distance / population_risk / traffic_risk / building_exposure / energy
每项: raw / normalized / weight / contribution / unit / source / semantics / enabled / status
      + exposure_m / exposure_definition / normalized_index_statistics[count,min,max,mean]
      + source_resolution_m / mapping_method / upsampled_without_new_information
      + provenance_sources / provenance_note / edge_count
```

- **soft field 必须是带 provenance 的 normalized index ∈ [0,1]**。planner **不再包含任何隐式 normalizer**：旧的“人口除以 10000 / 交通除以 100”常数已删除，`normalize_synthetic_spec` 也拒绝 `population_per_cell` / `traffic_per_cell` 这类原始计数接口。
- **edge exposure = `length_m × (index_source + index_target) / 2`**，单位 `m_x_normalized_index`（长度积分，而不是 raw 计数相减）。
- scalar cost 精确语义：

```
scalar_cost = Σ_edges( length_m + Σ_channels( weight_c × exposure_m_c ) )
```

- weight 只要求**显式、finite、≥ 0**；**没有 weight 之和上限**（旧 `SOFT_WEIGHT_SUM_LIMIT = 1.0` 已删除），因为启发函数不再依赖 weight。
- 启用的 mapped channel（`population_risk` / `traffic_risk`）在每个 cell 上都必须有 `status=passed` 的 index；**缺 index 即 readiness blocked，绝不当 0 cost**。缺失字段的 cell 必须显式省略（不能写 `null`/0 冒充「已测量为 0」）。
- `building_exposure` 的 index 由「状态高度与该格 required clearance 的垂直余量」导出（`vertical_clearance_proximity_index_0_1`），是 documented engineering proxy，不是碰撞概率。
- `energy` 固定 `pending_model` / disabled。CNS 记录为 `integration_mode=post_route_assessment` 且 `excluded_from_search_cost=true`。

---

## 3. Policy 与 readiness：没有默认安全值

### 3.1 显式参数

**V3-A 现行**，`V3PlanningPolicy` 中以下参数**没有默认值**：

```
min/max_altitude_egm2008_m, vertical_step_m,
terrain_clearance_m, building_horizontal_clearance_m, building_vertical_clearance_m,
aircraft_min_turn_radius_m, max_climb_gradient, max_descent_gradient
```

缺任一 ⇒ `status != confirmed` ⇒ readiness 相应域 blocked ⇒ 直接返回 `not_ready`，**不会运行搜索**。

**V3-B 现行**，`v3_fine_refinement_policy`（ProjectState 独立容器）同样没有默认值：

```
horizontal_crs           # local metric CRS；未配置 => blocked
resolution_source        # explicit_configuration | dtm_effective_resolution；未配置 => blocked
resolution_m             # explicit_configuration 时必须给出
max_stride_cells         # 默认 1（单 cell 步长），不放大任何能力
source / confirmed        # 无确认依据即 pending/blocked
```

“30 m” **不是**安全常数：fine resolution 只能来自显式配置或 DTM 有效分辨率，`FineGridSpec.resolution_source` 必须可追溯，`resolution_deviation_m` 记录请求值与实际值之差。

### 3.2 爬升/下降能力：rate 必须配 explicit speed

```
若提供 max_climb_gradient / max_descent_gradient        → 可直接判 edge
若只提供 max_climb_rate_mps                             → 必须同时有 explicit planning_speed_mps 才能换算
                                                           否则 kinematic readiness blocked
```

禁止猜速度，也禁止从 Aircraft 目录借用 cruise speed。

### 3.3 readiness 域

**V3-A 现行**：`airspace / terrain / building / policy / aircraft / cost_model` 各自 `ready | pending | blocked` 并给出 reasons。

**V3-B 现行**：`strategic_source / corridor / frame / airspace / terrain / building / policy / aircraft / cost_model / refinement_fingerprint`。

- `strategic_source`：只有**选定且 current 的 V3-A `strategic_candidate`**（且带 strategic fingerprint）才能精化；stale 候选、非 candidate 状态直接 blocked。
- `corridor`：必须存在且 `semantics == refinement_search_window_not_safety_corridor`。
- `frame`：metric bounds / local CRS / resolution source 必须齐备。
- `refinement_fingerprint`：`expected_refinement_fingerprint` 与当前 strategic/corridor/policy/source/frame/grid 不一致 ⇒ blocked（stale，必须重跑）。
- `blocked`：安全证据本身缺失或不可用；`pending`：证据可能存在但显式参数/确认缺失。两者都**阻止运行**。

### 3.4 运动模型的方法学声明

**V3-A 现行**：`motion_model_id = engineering_3d_motion_primitives_v3a`。

**V3-B 现行**：`model_id = engineering_3d_motion_primitives_v3b_corridor_refinement`，语义固定为

```
engineering_arc_length_proxy_min_turn_radius_times_heading_change_not_exact_curvature_v3c_will_validate_continuously
```

即：转弯仍是**工程弧长代理**（`R·|Δψ| ≤ stride_m`），**不声称 exact curvature**；连续曲率验证属于 V3-C。多 cell stride 只改变「一步走多远」，不放松任何能力上限。

---

## 4. 运动 primitive 与 edge 生成（V3-A）

**V3-A 现行**（`MotionPrimitiveProvider` + `TransitionValidator`）：

- 每个 state 生成 8 个水平 primitive（E/NE/N/NW/W/SW/S/SE）+ 2 个纯垂直（climb/descend，V3-A 判为不可行）+ 16 个水平+高度（±1 高度步）= 26 个。
- 每个 primitive 记录 `primitive_id / kind / grid_delta / altitude_delta_steps`；每个接受的 edge 记录 `length_m / climb_gradient / heading_change_deg / altitude_change_m / channel_indices / soft_penalty / scalar_cost`。
- edge 的几何测地长度与方位角每个环境只计算一次并缓存。
- **确定性 tie-break**：优先队列以 `(f, g, insertion_order, state)` 排序，等代价时按 state 字典序选择父节点。

---

## 5. L8 战略搜索与启发函数（V3-A 正确性修复）

**V3-A 现行**：

- 搜索层：L8 MH/T 水平网格 × 显式高度层 × heading bin。
- 启发函数：**纯 3D 几何距离**

```
h = 3D geodesic distance(state, goal)          # scale 恒为 1，与 weight 完全解耦
```

可采纳性论证（记录在 `heuristic_semantics.admissibility_argument`）：

1. 每条边的标量代价 = 边长 + Σ(weight × exposure)；
2. `weight` 有限且 ≥ 0，`exposure = 边长 × 端点 normalized index 均值`，index ∈ `[0,1]`；
3. 因此**每条边代价 ≥ 其 3D 几何边长**，`h` 不会高估真实代价 —— 与 weight 之和无关。

**修复说明（必须保留）**：旧实现使用 `h = 距离 × (1 + Σenabled weight)`，并用 `Σweight ≤ 1` 作为「保证下界」的理由。当任意 soft penalty 可以取 0 时（例如 `building_exposure` 在足够高度上恒为 0），该启发函数**并非 admissible**；两项（乘数与 weight 上限）均已删除，代价是 `SOFT_WEIGHT_SUM_LIMIT` / `POPULATION_NORMALIZATION_PEOPLE` / `TRAFFIC_NORMALIZATION_AIRCRAFT` 三个常量的移除。

检索统计完整记录：`expanded_states / generated_states / expanded_transitions / primitive_checks / runtime_ms / expansion_cap / expansion_cap_reached / search_complete / search_completeness / resource_limited / resource_limit / resource_limit_reason / optimality_proven / state_space_shape`。

**expansion cap ≠ failed**：达到 `max_expanded_states` 时返回 `status = search_incomplete` 且
`search_completeness = expansion_cap_reached_optimality_not_proven`、`optimality_proven = false`、`infeasibility_proven = false`、`semantics.resource_limited_not_infeasible = true`。若在 cap 之前已经生成可行 goal state，则保留该候选并显式标注「未证明最优」；否则给出空路径并明确说明这**不构成 infeasible 结论**。绝不返回 `failed`/infeasible。

`search_completeness` 的取值固定为：

```
queue_exhausted_optimality_proven | expansion_cap_reached_optimality_not_proven | search_did_not_run
```

**hard-constraint rejection 统计**：`HardConstraintAudit` 以稳定 reason id 聚合**去重后的** state/edge 数量（每 reason 至多保留 200 个样本）。V3-B 额外维护 `traversed_cell_rejections`。

---

## 6. L8 → corridor-local fine → exact 三阶段

**架构现状**：

```
L8 战略搜索（V3-A 现行）
  → CandidateRefinementCorridor（V3-A 现行 proposal）
      ├─ center_grid_ids          : 粗 state path 的 L8 cell 序列
      ├─ support_grid_ids         : 可配置 N-ring（Chebyshev）搜索窗口
      ├─ altitude_envelope        : 计划高度范围（可选显式 margin）
      └─ semantics = refinement_search_window_not_safety_corridor
  → corridor-local 米制 fine grid + 精化搜索（V3-B 现行）
  → exact polygon / terrain / continuous clearance 验证（V3-C，未实现）
```

必须明确的语义：

- corridor 是**下一阶段的搜索窗口**，**不是**安全走廊、不是 operation volume、不是法规边界。
- **N-ring 不是安全净空值**：它只扩大搜索空间，`ring_n=0` 时 support == center。
- `altitude_envelope` 只是**计划高度的包络**（`planned_altitude_extent_not_a_clearance_volume`）。V3-B **把它当作显式证据使用**：未声明 `explicit_margin_m` 时 envelope 就是计划高度范围，精化只能在计划高度上做水平精化（此时 fine search 不会擅自爬升/下降）；需要垂直精化时必须在 V3-A 运行时显式给出 `corridor_altitude_margin_m`。
- `refinement_cell_size_m` 由调用方显式给出；V3-A 只记录为下一阶段参数。

**V3-B 的诚实边界**：精化候选只在局部米制 fine grid 与离散高度层上被判为硬约束可行；它**没有**做连续空间/精确多边形的最终判定。V3-C 才做 exact validation。

---

## 7. V3-B：corridor-local 米制精化

### 7.1 阶段合同（`fine_contracts.py`）

| 契约 | 说明 |
| --- | --- |
| `LocalMetricPlanningFrame` | local metric CRS / CRS 来源 / origin / axis / resolution / metric bounds / `local_to_geographic`（method、authority、`display_only`、`interpolated_from_parent_cells`） |
| `FineGridSpec` | `resolution_m` + `resolution_source` + `requested_resolution_m` + `effective_source_resolution_m` + `resolution_deviation_m` + `nx/ny/cell_count` + `parent_grid_ids` + `mapping_method` + `not_a_safety_clearance` |
| `FineCellEnvironment` | 逐 fine cell 的 terrain/building/airspace hard facts + upsampled soft fields + `source_audit` |
| `V3RefinementProblem` | strategic candidate（含 fingerprint 与 applicability）+ corridor + policy + frame + fine grid + environment + `max_stride_cells` + `source_audit` + `expected_refinement_fingerprint` |
| `V3RefinementResult` | status / readiness / fingerprint / frame / fine_grid / source_audit / state_path / metric+geographic projection / cost vector / search + hard statistics / motion model / **final_validation_performed=false** / **operational_route=false** / `v3c_validation_pending=true` |
| `FineMotionPrimitive` | `primitive_id / kind / grid_delta_cells / stride_cells / stride_m / bearing_deg / altitude_delta_steps / traversed_cell_ids / traversed_entry_fractions / turn_model` |

结果状态**只允许**：

```
refined_candidate / failed / not_ready / missing_data / search_incomplete
```

`normalize_v3_refinement_result` 强制 `final_validation_performed=false`、`operational_route=false`、`v3c_validation_pending=true`，并固定 disclaimer 文本（"V3-B refined candidate…尚未执行 V3-C …"）。

### 7.2 fine grid 的构造范围与分辨率来源

- fine grid **只在 corridor support cells 的米制 bbox 内**构造（`mapping_method = corridor_support_parent_cells_to_local_metric_index_grid`）；不做全 workspace、全栅格重采样。
- 水平分辨率来自**显式配置**或 **DTM 有效分辨率**；两者都不可得 ⇒ `fine_resolution_unresolved` ⇒ blocked。
- fine cell 的 ID/row/column 为确定性 row-major（south-west origin）；经纬度位置由 adapter 的显式投影逆变换给出，或（synthetic）由显式声明的局部定义给出，并在 frame 中记录 `display_only` / `interpolated_from_parent_cells`。

### 7.3 Terrain hard facts

```
fine cell floor = max(相交该 cell 的 FABDEM 有效像元 EGM2008 高程) + explicit terrain_clearance_m
```

- **禁止** center sample / average / mean / bilinear 作为 hard floor；`normalize_fine_cell_environment` 直接拒绝这些 `sampling` 值，并固定记录 `sampling = intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance`。
- NoData / 无有效像元 ⇒ `data_status = unknown` ⇒ 不可行且 readiness blocked（**unknown 绝不是平地**）。
- 只读窗口：adapter 对 corridor 做**一次**只读 `ReadAsArray` 窗口读取，记录 `window_pixels / raster_pixels / read_fraction / resampled=false`；不修改源栅格、不做全图重采样。

### 7.4 Building hard facts

```
fine cell required floor = max( ground_max + height_m ) + explicit building_vertical_clearance_m
footprint 影响该 cell ⟺ footprint ring 与该 cell 矩形的距离 ≤ explicit building_horizontal_clearance_m
```

- 只通过**现有 GeoPackage 的 provider spatial index（RTree）** 用 `QgsFeatureRequest.setFilterRect` 查询 corridor 建筑；**缺少 spatial index ⇒ 整个 refinement blocked**（不允许退化为全表扫描）。
- 缺 `height_m` 或缺 DTM ground ⇒ 该 cell `data_status = unknown` ⇒ blocked。
- 语义固定为 **fine-grid conservative envelope**（`conservative_fine_grid_envelope_not_exact_polygon_clearance`），**不是** V3-C 的 exact polygon clearance，且**不修改源 geometry**。

### 7.5 Airspace

- 只消费 **confirmed `AirspacePolicy`**：parent L8 cell 必须在 confirmed `allowed_grid_ids` 中，**且** fine cell 中心位于 confirmed allowed polygon 内；中心落在 confirmed blocked polygon 内 ⇒ `confirmed_restricted`。
- unconfirmed / 无几何 / 不在 allowed polygon 内 ⇒ `unknown` ⇒ 不可行。
- **禁止**根据图层名称或颜色推断；adapter 的 audit 显式记录 `inferred_from_name_or_color = false`。
- 说明：fine 层是**中心点判定**，不是 full-cell coverage 证明；exact polygon membership 属于 V3-C。

### 7.6 Soft facts：coarse → fine 的 provenance

- `population_risk` / `traffic_risk` 的 index **复用现有 RiskModel contributor 的 `normalized` 值**（`grid_risk.ground.contributors.{population,traffic}.normalized`），并记录 `risk_model.algorithm_id/version`；V3-B **不重新发明**人口/交通风险模型。
- coarse index 映射到 fine cell 时固定 `mapping_method = coarse_cell_index_upsampled_to_fine_cells`、`upsampled_without_new_information = true`、`coarse_source_cell_id = <parent cell>`，并在 cost vector 中给出 `provenance_note`（"coarse 数据映射到更细网格时不获得新的原始精度"）。
- 因此 fine grid **不声称比 coarse 源更高的原始精度**；`source_resolution_m` 如实记录 coarse 源分辨率。
- `building_exposure` 的 index 由 fine cell 自身的 required clearance 与状态高度导出（其 `mapping_method = vertical_clearance_proximity_index_0_1`，不是数据栅格）。

### 7.7 精化搜索：multi-cell stride 与 traversed cell

- state：`fine_cell_id × altitude_index × heading_bin`（与 V3-A 同构，canonical vertical reference 仍是 EGM2008）。
- **spatial `FineMotionPrimitive`**：沿 8 个网格方向生成 `stride = 1..max_stride_cells` 的水平 primitive，以及 ±1 高度层的 climb/descend 变体。**不机械复用单 cell 转弯**：fine cell 提供的弧长太短，单 cell 步长会拒绝几乎所有转弯。
- 每个 primitive 记录 `traversed_cell_ids`（Amanatides & Woo supercover，包含起点与终点 cell）与 `traversed_entry_fractions`（路径进度）。
- 转弯约束仍是既有工程约束 **`R · |Δψ| ≤ stride_m`**，并对每个 primitive 记录 `turn_arc_required_m` / `turn_arc_available_m`。
- 对**所有经过 cell**按路径进度**插值高度**执行 airspace / terrain / building 检查：

```
z(f) = z_source + (z_target - z_source) · f      # f = 该 cell 的 entry fraction
z(f) ≥ floor(cell) ；z(f) ≥ required_clearance(cell) ；airspace(cell) == confirmed_allowed
```

  因为「每个 cell 的出口」就是「下一个 cell 的入口」，逐 cell 的入口检查加上目标 state 检查即可覆盖整段 stride；**中间障碍与地形尖峰无法被 stride 跨越**（`hard_constraint_summary.intermediate_obstacles_cannot_be_skipped_by_a_stride = true`）。
- 高度层受 corridor `altitude_envelope` 限制（见 §6），并在 traversed 检查中记录 `traversed_cell_outside_corridor_altitude_envelope`。
- cost 复用同一 provenance soft cost 模型（`scalar = Σ( stride_m + Σ weight × exposure_m )`），启发函数仍是纯 3D 几何距离。
- cap 语义与 V3-A 相同：`search_incomplete` / resource limited，不是 failed/infeasible。

### 7.8 refinement fingerprint 与 stale

```
refinement_fingerprint = f(strategic_fingerprint, corridor, policy, source audits, frame, fine grid)
```

`evidence_components` 的六个分量：`strategic_fingerprint / corridor_fingerprint / policy_fingerprint / source_fingerprint / frame_fingerprint / fine_grid_fingerprint`。

- **源 / corridor / policy 变化 ⇒ stale**：`refinement_snapshot()` 现场重算并逐分量比较，`current_applicability = current | stale`，并列出 `changed_components` 与原因；stale 的 refinement 必须重跑。
- **scope 感知**：`configured_real_sources` 的 `source_fingerprint` 覆盖受跟踪的 source audits（terrain/DTM/buildings）、confirmed airspace eligibility 与现有 risk model 身份；`canonical_synthetic` 的 `source_fingerprint` 只覆盖它真正依赖的 workspace/grid 与自身环境指纹——无关的真实源文件变化不会把 synthetic 演练误判为 stale。
- planner 内部另有 `refinement_fingerprint` readiness 域：`expected_refinement_fingerprint` 不匹配即 blocked（防止用旧问题描述重放）。

### 7.9 V3-B 的写入边界

- 只在 `route_planner_v3_experiments` 的记录下追加 `refinements[]`（每个 strategic candidate 至多保留 `MAX_V3_REFINEMENTS_PER_EXPERIMENT` 条），以及独立容器 `v3_fine_refinement_policy`。
- **不写** `operational_routes`、`algorithm_selection`、`spatial_3d`；不产生任何失效传播（不使 routes/grid/grid_risk/P7-P19 stale）。

---

## 8. 与经典 RCSP 的区别

| | 经典 RCSP（Resource-Constrained Shortest Path） | V3-A/V3-B 现行 |
| --- | --- | --- |
| 资源 | 多维资源向量，代价与资源同时沿路径累积，靠 dominance/label-setting 剪枝 | **hard feasibility + additive soft cost**：硬约束只做可行/不可行两值判定（无「资源消耗」概念），soft cost 单独累加为标量 |
| 搜索 | 标签修正/Dijkstra 变体，标签集爆炸由 dominance 控制 | 分层状态 + A*（时间无关、静态障碍） |
| 目标 | 资源受限下的最优路径 | 硬可行域内的最小 scalar cost |
| 转弯/爬升 | 可作为资源（如累积转弯角） | 作为**转移可行性**（单步能力上限），不累积 |
| 权衡 | Pareto 前沿 | 本阶段不产出 Pareto 前沿；soft 向量只做**报告**，只有显式加权项进入标量 |

因此 V3 **不是** RCSP 实现，也**不声称** Pareto 最优或能量最优。

---

## 9. 输入 / 输出合同

### 9.1 JSON-safe 契约

`cns_planner/route_planner_v3/contracts.py`（V3-A）与 `fine_contracts.py`（V3-B）见 §7.1。所有契约带 schema version，可 `json.dumps(..., allow_nan=False)` 序列化。

### 9.2 结果状态（仅允许）

```
V3-A: strategic_candidate / failed / missing_data / pending_confirmation / not_ready / search_incomplete
V3-B: refined_candidate  / failed / not_ready / missing_data / search_incomplete
```

**V3 结果永远不是** `final safe` / `validated operational route`：

- `operational_route = false`（两个 normalizer 都强制）
- `final_validation_performed = false`（两个 normalizer 都强制）
- V3-B 另有 `v3c_validation_pending = true`
- `semantics.not_final_safe = true`，`semantics.not_validated_operational_route = true`
- `disclaimer` 固定文本随结果一起传递

### 9.3 算法边界

- **算法层（`route_planner_v3/*`）不 import QGIS/GDAL，不读文件**，只消费 canonical `V3CellEnvironment` / `FineCellEnvironment`。V3-B 的 fine grid / 投影 / 建筑包络 / 地形聚合都是纯 Python 纯函数（`fine_grid.py`）。
- 真实数据只通过 **GIS 边界** `gis/fine_environment_adapter.py` 进入：FABDEM 窗口只读 + GPKG RTree 建筑查询 + confirmed AirspacePolicy；数据未配置/未确认时**显式返回 blocked**，不构造假环境。

---

## 10. 与既有系统的隔离

**V3-A/V3-B 现行**：

- 不修改 V1/V2 搜索/输出；不修改 `spatial_3d` 语义；V3 高度**不写入** `route_altitude_profile`。
- 不注册 `algorithm_registry`：V3 不是可被 `algorithm_selection` 选中的 planner，默认 route planner 仍为 `route_planner_v1`。
- 独立容器：

```
route_planner_v3_experiments   # 记录/环境 spec/policy/result/verdicts + refinements[]
v3_planning_policy             # V3-A 显式 policy（可能为 pending/blocked）
v3_fine_refinement_policy      # V3-B 显式 fine configuration（无默认分辨率）
```

- 诊断/实验路径不产生任何失效传播。

### 10.1 接口

| 方法 | 路径 |
| --- | --- |
| GET | `/api/route-planner-v3-experiments` |
| GET | `/api/route-planner-v3/readiness` |
| POST | `/api/route-planner-v3/policy` |
| POST | `/api/route-planner-v3-experiments/evaluate` |
| POST | `/api/route-planner-v3-experiments/delete` |
| GET | `/api/route-planner-v3/refinement-readiness` |
| GET | `/api/route-planner-v3-refinements` |
| POST | `/api/route-planner-v3/fine-policy` |
| POST | `/api/route-planner-v3-refinements/evaluate` |
| POST | `/api/route-planner-v3-refinements/evaluate-real`（GIS-wired，需 QGIS/GDAL） |

Step 03 面板同时显示 **V3-A coarse candidate** 与 **V3-B refined candidate 的 2D 投影**，并给出 fine resolution / cell count / expanded states / hard rejection / cost breakdown（含 exposure 与 provenance）/ data provenance；醒目标注 **“refined candidate，未执行 V3-C 连续几何验证”**。地图叠加用同一条虚线（coarse）与实线（refined）区分，corridor 仍只作为搜索窗口绘制。

---

## 11. 局限（必须随结果一起阅读）

1. **V3-A 未精化**：候选只在 L8 网格分辨率与离散高度层上可行。
2. **V3-B 未做最终判定**：fine grid 只做保守包络与逐 cell 插值检查，没有 exact polygon membership、exact terrain profile、沿整条轨迹的 continuous clearance、连续曲率验证。
3. **运动模型是工程基线**：不是飞行动力学认证模型（无 bank/风/能量）；V3-B 的转弯仍是弧长代理。
4. **cell 级保守语义**：地形/建筑按 cell 的 clearance floor 判定，未表达 cell 内几何细节。
5. **空域只有两值**（外加 unknown）：`confirmed_allowed / confirmed_restricted / unknown`；fine 层是中心点判定。
6. **环境可以是合成**：synthetic 路径只用于确定性演练；真实数据需要配置并确认 FABDEM/建筑/空域来源。
7. **无 Pareto / 无多目标最优**：soft 向量只报告，标量只是显式加权和。
8. **endpoint 绑定**：V3-A 绑定最近 cell 中心；V3-B 记录显式绑定方法，仍不是精确多边形包含判定。
9. **无时间维**：不含时间窗、动态障碍、四维（4D）规划。
10. **energy 缺席**：`pending_model` / disabled。
11. **V3-B 垂直自由度受 corridor envelope 限制**：未声明 margin 时只能做水平精化（见 §6）。

---

## 12. V3-C / V3-D 后续接口

| 阶段 | 目标 | 已就位的接口 |
| --- | --- | --- |
| **V3-C** | exact polygon membership、exact terrain profile clearance、continuous clearance（沿整条轨迹）、连续曲率验证 | `V3RefinementResult.metrics/state_path`（含 `traversed_cell_ids` 与 `traversed_entry_fractions`）、`semantics.v3c_pending`、`frame`（投影与度量帧）、`source_audit`（fingerprint） |
| **V3-D** | validated route → operational adapter → 既有 P7/P8/P9/P10 CNS Assessment | `cns_assessment.integration_mode = post_route_assessment`、`next_stage = V3-D_validated_route_operational_adapter_and_cns_assessment`、`V3CostVector` 可扩展 component |
| 未来 | Route–CNS 联合优化（CNS 进入 cost/约束）、energy 模型 | 显式加权 component 接口；V3 阶段明确标注 energy pending |

任何后续阶段都必须继续满足：**没有默认安全值、unknown fail-closed、结果不称 final、CNS/energy 不静默进入 cost、coarse→fine 不声称新精度**。

---

## 13. 相关代码

```
cns_planner/route_planner_v3/
  contracts.py         # V3-A JSON-safe 契约、normalizer、readiness 汇总语义、disclaimer
  motion.py            # MotionPrimitiveProvider、TransitionValidator、kinematic_readiness
  hard_constraints.py  # HardConstraintEvaluator（state/transition 分离）、HardConstraintAudit
  cost.py              # SoftCostModel（无隐式 normalizer）、length-integrated exposure、cost vector
  planner.py           # V3StrategicPlanner：L8 × 高度 × heading 的确定性 A*（h = 纯几何距离）
  corridor.py          # CandidateRefinementCorridor builder + ring_neighbors
  readiness.py         # 六域 readiness 与 overall 聚合（V3-A/V3-B 共用 policy/aircraft/cost 判定）
  synthetic.py         # canonical synthetic V3CellEnvironment（显式 spec + normalized index）
  fine_contracts.py    # V3-B 契约、fine refinement policy、fingerprint 与 applicability
  fine_grid.py         # 纯几何：local frame、fine grid、airspace/soft 映射、terrain 聚合、建筑保守包络
  fine_search.py       # V3RefinementPlanner：multi-cell stride primitive + traversed cell 检查
  fine_synthetic.py    # V3-B synthetic fine environment builder（显式 spec，非真实数据）
cns_planner/gis/fine_environment_adapter.py   # V3-B GIS/GDAL 边界（FABDEM 窗口 + GPKG RTree + confirmed airspace）
cns_planner/application/route_planner_v3_service.py  # 独立实验容器与 V3-A/V3-B 编排（不写 operational_routes）
cns_planner/web/js/workflow/step03_routes.js         # Step 03 V3-A/V3-B 面板
cns_planner/web/js/map/route_planner_v3_overlay.js   # coarse + refined 2D 投影 + corridor 搜索窗口
docs/route_planner_v3_architecture.md                # 本文件
```
