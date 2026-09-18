# Route Planner V3 架构：3D 战略规划（V3-A）+ corridor-local 精化（V3-B）+ 连续几何实现与源几何验证（V3-C）+ 运行化采用与 CNS 桥接（V3-D）

> 状态：**V3-A 已实现**（战略搜索 + 硬约束内核 + provenance soft cost 向量 + refinement corridor proposal），并已完成三项正确性修复（admissible heuristic / 删除隐式 normalizer / expansion cap 语义）。
> **Real-Data Enablement V1 当前语义**：空域与底图仅为 display-only reference layers，不参与 V3-A/B/C/D 可行性、readiness、fingerprint 或失效传播。V3-A 的 `configured_real_sources` 由注入的 GIS adapter 读取 verified FABDEM、buildings/building_grid 与现有 L8 soft risk；未知建筑高度保持 unresolved。
>
> **V3-B 已实现**（corridor-local 米制 fine grid + fine environment adapter + multi-cell stride 精化搜索 + refinement fingerprint/staleness）。
> **V3-C 已实现**（连续 3D 几何实现 + 源证据硬约束验证：analytic arc fillet、explicit curve chord error、native terrain raster、真实 building footprint、altitude 与 kinematics 重验证）；airspace 域固定为 `skipped/not_applicable`。
> **V3-D 已实现**（V3-C validated route → operational adoption → 复用既有 P7/P8/P9/P10 的 CNS Assessment bridge），见 §7B。
> 明确**未实现**：clothoid / continuous-curvature 过渡；Route–CNS 联合优化（未来项，不是 V3-D）；energy 模型。
> 本文件描述 V3 的目标架构与 V3-A/V3-B/V3-C/V3-D 的落地边界；凡标 **V3-A/B/C/D 现行** 的是已实现语义。

---

## 0. 阶段路线图

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| V3-A | L8 × 高度 × heading 战略搜索、硬约束内核、provenance soft cost、candidate refinement corridor | 已实现 |
| V3-B | 在 V3-A corridor 的 support cells 米制窗口内构造局部 fine grid，做 corridor-local 3D 精化（多 cell stride、逐 traversed cell 检查） | 已实现 |
| V3-C | 连续几何实现（C1 straight/arc、explicit chord error、realized vertical profile）+ 源证据硬约束验证（native terrain raster、真实 building footprint、altitude、kinematics）；`airspace` 域固定 `skipped/not_applicable` | 已实现 |
| V3-D | validated route → operational adoption（既有 `operational_routes` + `spatial_3d` 高度剖面接口）→ 复用既有 P7/P8/P9/P10 CNS Assessment bridge | 已实现 |
| 未来 | clothoid / continuous-curvature 过渡；Route–CNS 联合优化（CNS 进入 cost/约束）；energy 模型 | 未来 backlog |

**V3-C 的诚实边界**：vector predicate 对 **linearized representation（含显式 curve-error envelope）** 是精确的；圆弧本身是解析几何、折线是有界近似；terrain 是 **source-native raster evidence**，不声称真实世界地形在数学上连续精确。

**V3-D 的两条不可放松的边界**：

1. **V3-C validation 历史不可变**：`operational_route=false` / `cns_assessed=false` 永不回写 `true`；正式状态存放在新的 adoption / bundle 容器里；
2. **route safety 与 CNS 合规严格分离**：CNS 不满足 ⇒ `assessment_status=complete` + `requirement_verdict=does_not_meet`，而 V3-C 仍然是 `validated_route`；CNS gap 绝不改写成 route validation failed，route validated 也不等于 CNS 合规。

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
| `state_feasible` | 高度带上下界 | `altitude_above_max` / `altitude_below_min` |
| | 地形净空（cell surface clearance floor） | `terrain_clearance_unresolved` / `below_terrain_clearance` |
| | 建筑净空（cell required vertical clearance） | `building_clearance_unresolved` / `below_building_clearance` |
| `transition_feasible` | 邻接/edge 合法性 | `neighbor_not_adjacent` |
| | 转弯能力（最小转弯半径弧长） | `turn_capability_unknown` / `turn_radius_exceeded` |
| | 爬升能力 | `climb_capability_unknown` / `climb_gradient_exceeded` |
| | 下降能力 | `descent_capability_unknown` / `descent_gradient_exceeded` |
| | 高度步长与纯垂直过渡 | `altitude_step_not_single_band` / `vertical_only_transition_not_modelled` |

**active hard constraints 只有五类**：terrain clearance、building clearance、altitude bounds、turn、climb/descent。**airspace 不在其中**（Real-Data Enablement V1）。

**V3-B 现行**，在同一个 state/transition 分离上增加第三类判定：**traversed cell 检查**（见 §7）。

**fail-closed 规则**：任何安全数据 unknown/NoData 一律不可行，绝不当作「无约束」或「通过」。

- 地形：每个 cell 必须给出 `surface_clearance_egm2008_m`；缺少或 `data_status != passed` 即 blocked。
- 建筑：`data_status != passed` 即 blocked；`data_status == passed` 且无 clearance 表示该 cell **确认无建筑约束**（confirmed absence）。
- 空域：**display-only reference layer**。legacy `airspace.status` 可保留 `not_applicable/display_only` 供显示，`confirmed_allowed/confirmed_restricted/unknown` 一律不产生 cell/edge 可行性影响（禁止 `outside => blocked`、`unknown => unsafe`）。

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

**V3-A 现行**：`terrain / building / policy / aircraft / cost_model` 各自 `ready | pending | blocked` 并给出 reasons。`airspace` 项保留为兼容字段，固定 `status=ready / applicability=not_applicable / layer_role=display_only_reference_layer`，不影响 overall。

**V3-B 现行**：`strategic_source / corridor / frame / terrain / building / policy / aircraft / cost_model / refinement_fingerprint`；`airspace` 只作兼容字段（`not_applicable/display_only`），**不**进入 `FINE_SOURCE_ROLES`、source_readiness、source_audit 或 fingerprint，也不再有 `no_confirmed_allowed_airspace_cells` blocker。

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
  → 连续几何实现 + 源几何硬约束验证（V3-C 现行，见 §7A）
  → validated route → operational adoption（既有 operational_routes + spatial_3d）
    → 复用既有 P7/P8/P9/P10 CNS Assessment（V3-D 现行，见 §7B）
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
| `FineCellEnvironment` | 逐 fine cell 的 terrain/building hard facts + upsampled soft fields + `source_audit`（airspace 仅作 display-only 兼容字段） |
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
- **DTM 有效分辨率必须做单位换算**：投影 CRS 按该 CRS 自己验证过的 linear unit 换算成 m（例如 US survey foot ≠ 1.0）；geographic CRS 的 affine pixel size 是**度**，绝不当 m——而是在 corridor/reference location 用 `Geod` 测相邻 pixel 中心的地面距离。记录 `native_pixel_size_x/y` + `native_pixel_size_unit`、`effective_resolution_m_x/y`、`method`（`projected_crs_verified_linear_unit` / `geographic_geodesic_adjacent_pixel_centres`）与 `reference_location`/`geodesic_backend`。`degree_values_never_reported_as_metres=true`。
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

### 7.5 Airspace（display-only reference layer，Real-Data Enablement V1）

- 空域**不再参与** V3-B 的任何可行性、readiness、source audit 或 fingerprint 判定。
- fine cell 不会因 current airspace `unknown`/`outside` 变为 blocked；不再有 `confirmed_allowed` cell-polygon 覆盖判定。
- 兼容字段可填 `not_applicable / display_only`；legacy `AirspacePolicy`（allowed/blocked/unknown + confirmed）仅为 backward compatibility 保留，**不参与** hard evaluator / fingerprint。
- 原始适飞空域图层仍作为**视觉参考**显示（前端 `id="air"` 开关），但不再有 “confirmed allowed overlay” 暗示规划可行性。
- 未来若需要法规约束，属于**独立扩展位** `regulatory_constraints`，不是把当前适飞空域重新解释成法规约束。

### 7.6 Soft facts：coarse → fine 的 provenance

- `population_risk` / `traffic_risk` 的 index **复用现有 RiskModel contributor 的 `normalized` 值**（`grid_risk.ground.contributors.{population,traffic}.normalized`），并记录 `risk_model.algorithm_id/version`；V3-B **不重新发明**人口/交通风险模型。
- coarse index 映射到 fine cell 时固定 `mapping_method = coarse_cell_index_upsampled_to_fine_cells`、`upsampled_without_new_information = true`、`coarse_source_cell_id = <parent cell>`，并在 cost vector 中给出 `provenance_note`（"coarse 数据映射到更细网格时不获得新的原始精度"）。
- 因此 fine grid **不声称比 coarse 源更高的原始精度**；`source_resolution_m` 如实记录 coarse 源分辨率。
- `building_exposure` 的 index 由 fine cell 自身的 required clearance 与状态高度导出（其 `mapping_method = vertical_clearance_proximity_index_0_1`，不是数据栅格）。

### 7.7 精化搜索：multi-cell stride 与 traversed cell

- state：`fine_cell_id × altitude_index × heading_bin`（与 V3-A 同构，canonical vertical reference 仍是 EGM2008）。
- **spatial `FineMotionPrimitive`**：沿 8 个网格方向生成 `stride = 1..max_stride_cells` 的水平 primitive，以及 ±1 高度层的 climb/descend 变体。**不机械复用单 cell 转弯**：fine cell 提供的弧长太短，单 cell 步长会拒绝几乎所有转弯。
- 每个 primitive 记录 `traversed_cell_ids`（Amanatides & Woo supercover，包含起点与终点 cell）与 `traversed_entry_fractions`（路径进度）。
- **supercover 的 boundary-touch 语义是保守的**：cell 只要被 segment **触及**（在角上或沿边，零长度接触）就算被 traversed。线段**恰好穿过 grid corner** 时，共享该点的三个 cell（两个正交邻格 + 对角格）都被报告，去重且 fraction 单调——**绝不会因为对角前进而漏掉正交邻格**。这比数学 supercover 更 inclusive，方向是安全的：中间障碍/地形尖峰无法被跨越。
- 转弯约束仍是既有工程约束 **`R · |Δψ| ≤ stride_m`**，并对每个 primitive 记录 `turn_arc_required_m` / `turn_arc_available_m`。
- 对**所有经过 cell**按路径进度**插值高度**执行 terrain / building 检查：

```
z(f) = z_source + (z_target - z_source) · f      # f = 该 cell 的 entry fraction
z(f) ≥ floor(cell) ；z(f) ≥ required_clearance(cell)
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
- **scope 感知**：`configured_real_sources` 的 `source_fingerprint` 覆盖受跟踪的 source audits（terrain/DTM/buildings）与现有 risk model 身份（**不含** airspace eligibility）；`canonical_synthetic` 的 `source_fingerprint` 只覆盖它真正依赖的 workspace/grid 与自身环境指纹——无关的真实源文件变化不会把 synthetic 演练误判为 stale。
- planner 内部另有 `refinement_fingerprint` readiness 域：`expected_refinement_fingerprint` 不匹配即 blocked（防止用旧问题描述重放）。

### 7.9 V3-B 的写入边界

- 只在 `route_planner_v3_experiments` 的记录下追加 `refinements[]`（每个 strategic candidate 至多保留 `MAX_V3_REFINEMENTS_PER_EXPERIMENT` 条），以及独立容器 `v3_fine_refinement_policy`。
- **不写** `operational_routes`、`algorithm_selection`、`spatial_3d`；不产生任何失效传播（不使 routes/grid/grid_risk/P7-P19 stale）。

---

## 7A. V3-C：连续几何实现 + 源几何硬约束验证

### 7A.1 输入边界

V3-C **只**在 **选定且 current 的 V3-B `refined_candidate`** 上运行。它消费该 refinement 已记录的：

- `metric_projection`（V3-B local metric frame 内的米制轨迹）与 `state_path`（逐状态 canonical EGM2008 高度）；
- `frame`（local metric CRS / origin / axis / resolution / `local_to_geographic`）；
- `refinement_fingerprint` 与 `source_audit`。

缺少、非 candidate、`current_applicability != current` 的 refinement ⇒ `not_ready`，**不运行**。V3-C 不读 V1/V2 route path，不做任何投影发明。

### 7A.2 连续几何实现（`continuous_geometry.py`）

在 V3-B local metric frame 内把 polyline 变成 **C1（position + heading）连续**几何：

```
straight → circular_arc(fillet) → straight → …
```

- 内角 `Δψ`；`R = aircraft_min_turn_radius_m`（显式，**V3-C 先取 R = Rmin**，绝不为了勉强通过而减小 R）；
- `t = R · tan(|Δψ|/2)`；
- 若 `t` 超出任一相邻可用 segment、相邻 fillet 重叠、或几何退化（切点构造与解析转角不一致、非有限坐标）⇒ `turn_realization_failed` + `replan_required`，**禁止减小 R**；
- 圆弧保存 **analytic geometry**：`center_metric / radius_m / start_angle_rad / end_angle_rad / signed_sweep_rad / arc_length_m / chord_length_m / tangent_points_metric / turn_direction / radius_verified_from_center`；
- **只保证 position + heading 连续**：曲率在 straight↔arc 处**可以跳变**；`continuous_curvature=false`、`c2=false`，禁止声明 continuous-curvature / C2。Clothoid 明确留 future。

### 7A.3 曲线 linearization 与显式 chord error

- `curve_chord_error_m` **必须显式给出，没有安全默认值**。缺失 ⇒ `blocked` ⇒ `not_ready`；`realize_continuous_route` 直接拒绝。
- 每段圆弧用**等角** sub-chord 线性化：`n = ceil(θ / (2·acos(1 − e/R)))`，即每段 sagitta `R·(1 − cos(θ/(2n))) ≤ e`。
- 保存解析几何 **和** linearized LineString，并给出 **`actual_max_chord_error_m`（实测）**：对解析圆弧密采样、取到折线的最大垂距。要求 `actual ≤ requested`，否则 `turn_realization_failed`（不静默放宽）。
- `semantics = arcs_are_analytic_circular_arcs_the_linestring_is_a_chord_bounded_approximation`；`not_the_mathematical_curve = true`。
- 所有 vector/raster validation 都考虑该**误差包络**（route uncertainty envelope = linearized LineString 按 `curve_chord_error_m` 做 offset）。**不得把采样折线称为数学 exact curve**。

### 7A.4 Vertical profile（`z(s)`）

统一为 **`z(s) = EGM2008 orthometric altitude` vs realized along-track distance**（`egm2008_orthometric_altitude_against_realized_along_track_distance`）：

- tangent point 高度由 V3-B 对应 segment 上**线性插值**给出（按 top-level 距离定位，最近 segment 胜出，避免恰好落在顶点时选错段）；
- arc 内按 **arc-length 线性插值**（`vertical_interpolation = linear_in_arc_length_between_tangent_point_altitudes`）；
- 每个 primitive 记录 `horizontal_length_m / length_3d_m / z_start / z_end / gradient`；
- 重新验证 **min/max altitude**（显式高度带）与 **max climb/descent gradient**；
- 明确**不写**旧 `spatial_3d` / `route_altitude_profile`。

### 7A.5 Airspace（display-only，不再是 validator）

- Real-Data Enablement V1 起 `airspace` **不是** active validation domain：real evidence adapter 不再读取 `ConfirmedAirspacePolicySource`。
- 兼容 `airspace` 域固定返回 `status=skipped`、`applicability=not_applicable`、`reason=display_only_airspace_not_used_for_route_constraints`，**不阻塞** `validated_route`，也不进入 `validation_fingerprint`。
- legacy `ExactAirspaceValidator` / `ConfirmedAirspacePolicySource` 类保留为 backward compatibility，但**不在证据链中**。
- active validation domains = **geometry / terrain / building / altitude / kinematics**；`_route_status`、`all_domains_passed`、`validated_route` 判据只要求 active domains passed。geometry / FABDEM / buildings / altitude / kinematics 仍严格 fail-closed。

### 7A.6 NativeTerrainValidator

- **直接检查 FABDEM native raster**（`NativeTerrainWindowSource`），**不用 fine-cell hard floor 替代最终验证**。
- 只读 corridor/path window（单次 `ReadAsArray`）；遍历 realized path **含 curve-error envelope** 涉及的 native pixels。
- 每个 pixel 保留 **source value / data_status / NoData / CRS**；**NoData ⇒ `unresolved`**，禁止 bilinear 填补、禁止当 0。
- 把 route 在该 pixel 影响 interval 上的 **minimum z** 与 `terrain elevation + explicit terrain_clearance` 比较。
- 语义固定 `source_native_raster_validation`；**不声称真实世界无限连续 terrain exactness**。
- pixel interval 用纯几何 helper `resolve_native_pixel_intervals` 计算（clip 到"该 pixel 中心最近"的区间，保守：min z over interval vs floor 仍精确；相邻 pixel 区间在边界处重叠，transition point 至少被一个 pixel 检查）。

### 7A.7 BuildingPolygonValidator

- **复用现有 BuildingClearance** 的 source、RTree/provider query 与 **EGM2008 roof/height 语义**：`domain/building_clearance.py` 的 `building_roof_elevation` / `evaluate_vertical_clearance` 是**唯一** roof 公式与垂直余量判定的实现，`BuildingClearanceV1` 与 V3-C 共用（不再出现第三套 roof 公式）。行为等价已由 characterization 测试锁定。
- 只查询 **route bbox + clearance** 候选（provider spatial index）。
- 真实 footprint 按 `explicit horizontal_clearance + curve_error` 做 **metric buffer**，与 route LineString 求 **affected intervals**。
- 每 interval 比较 **minimum z(s)** 与 **roof + vertical_clearance**。
- 缺 height / 缺 ground / invalid geometry ⇒ `unresolved`（**不 MakeValid、不改源 geometry**）。
- 保留 `building_id / source / roof / min horizontal & vertical margin / interval`。
- 记录 Shapely buffer approximation 参数（`quad_segs`）。

### 7A.8 KinematicValidator

- 验证 **analytic turn `R ≥ Rmin`**；
- 验证 **tangent heading continuity**（逐 primitive 边界 + arc 端点与转角一致性，解析检查）；
- 重新验证每个 **continuous primitive** 的 climb/descent gradient；
- 记录 `minimum_turn_radius_observed_m`、`max_climb/descent_gradient_observed`；
- 明确 **不把 V3-B 的 `R·Δψ` 弧长代理当作最终转弯验证**；
- **self-intersection 只作为 diagnostic**（`self_intersection_semantics = diagnostic_only_not_automatically_unsafe_unless_policy_explicitly_requires`），除非 policy 显式规定 `self_intersection_is_failure`。

### 7A.9 V3ContinuousValidator 汇总

```
geometry / terrain / building / altitude / kinematics      # ACTIVE_V3C_DOMAINS
airspace                                                    # display-only: 固定 skipped/not_applicable
```

- **全部 active domains passed** ⇒ `status = validated_route`（`airspace` 不参与该判据）；
- 任一 domain 有**确定违反** ⇒ `failed`（`replan_required=true` + 结构化证据）；
- 缺证据 ⇒ `unresolved`；
- source/refined candidate stale ⇒ `not_ready`；
- **资源上限**（`max_validation_samples` / `max_runtime_s`）⇒ `validation_incomplete`，**绝不当 failed**（`resource_limited_not_infeasible=true`）。

结果状态**只允许**：

```
validated_route / failed / unresolved / not_ready / validation_incomplete
```

**即使 `validated_route` 也强制** `operational_route=false`、`cns_assessed=false`（`normalize_v3_continuous_validation_result` 无条件重置），并固定 disclaimer（"V3-C continuous validated route…仍然不是 operational route，CNS 尚未评估"）。

`validation_fingerprint` 组件：

```
refinement_fingerprint | continuous_policy_fingerprint | curve_tolerance_fingerprint
| source_fingerprint | crs_fingerprint | validator_versions_fingerprint
```

任一变化 ⇒ `current_applicability = stale`，必须重跑。

### 7A.10 统一 violation interval

```
domain, reason_id, start/end_distance_m, start/end coordinate,
required, observed, margin, evidence
```

**不自动 repair / 不自动 replan**：失败只给出 `replan_required=true` 与结构化证据。

### 7A.11 写入边界与隔离

- 只在既有 `route_planner_v3_experiments` 记录的 refinement 下追加 `validations[]`（每条 refinement 至多保留 `MAX_V3C_VALIDATIONS_PER_REFINEMENT`），以及独立容器 `v3_continuous_validation_policy`。
- **不写** `operational_routes`、`algorithm_selection`、`spatial_3d`；不产生任何失效传播。
- V3-C 只消费 canonical `domain_evidence`（metric polygon / native pixel window / building footprint），**算法包不 import QGIS/GDAL、不读文件**；真实数据只经 `gis/fine_environment_adapter.py` 的 V3-C 类进入。

### 7A.12 接口

| 方法 | 路径 |
| --- | --- |
| GET | `/api/route-planner-v3/continuous-readiness` |
| GET | `/api/route-planner-v3-validations` |
| POST | `/api/route-planner-v3/validation-policy` |
| POST | `/api/route-planner-v3-validations/evaluate` |
| POST | `/api/route-planner-v3-validations/evaluate-real`（GIS-wired，需 QGIS/GDAL） |

Step 03 同时显示 V3-A 战略候选、V3-B refined candidate 与 **V3-C realized route**，并显示：domain status、最小 margin、turn radius、climb/descent、source/tolerance、violation intervals。面板**醒目标注**"V3-C validated route 仍不是 operational route；CNS 尚未评估"。

---

## 7B. V3-D：V3-C validated route → operational adoption → CNS Assessment bridge

V3-D **不再开发航路算法**。它把 current V3-C `validated_route` 以**显式、可追溯、事务式**方式发布到既有 `operational_routes` + `spatial_3d` 高度剖面接口，并复用既有 P7/P8/P9/P10 做 CNS Assessment。**Route 与 CNS 仍然串行，CNS 绝不反馈 V3 cost/search。**

### 7B.1 独立契约

`cns_planner/domain/v3_operational_adoption.py`：

```
V3OperationalProjection      # 投影：legacy route + locked profile + path metrics + compatibility
V3OperationalAdoption        # 一条正式采用记录（immutable 历史之外的正式状态）
V3OperationalAdoptionPreview # 只读 Preview（不写入任何 state）
V3CNSAssessmentBundle        # CNS 评估结果（完整度与需求满足度分离）
```

**V3-C validation 历史保持 immutable**：`operational_route=false`、`cns_assessed=false` 永不回写 `true`；正式状态只存在 adoption / bundle 中。

### 7B.2 Publish gate（`V3OperationalAdoptionService.publish_gate`）

只允许 selected validation 满足**全部**条件：

- `status = validated_route`；
- validation **current**（`continuous_validation_snapshot().current_applicability == current`）；
- source / refinement fingerprints current（refinement fingerprint 存在且 validation 未 stale）；
- `route_id` **对应当前 scenario route**，且投影后的 start/end 与该 scenario route 一致；start/end node identity 齐全；
- real configured source chain **ready**（`real_data_readiness().v3c.status == ready`，production 路径）；
- CRS transform 可用（不可用 ⇒ `crs_transform_unavailable`，**不猜 identity**）。

其他规则：

- **production Apply 禁止 `canonical_synthetic`**；`canonical_synthetic` 只能 Preview，并标注"仅测试，不可正式发布"；
- Preview **只读**：`operational_routes_untouched / spatial_3d_untouched / cns_not_run` 全部为 `true`；
- Apply 必须显式 `confirmed=true`，并提交 `expected_validation_fingerprint`；fingerprint 变化 ⇒ **拒绝**（避免 TOCTOU）；
- 支持 `validation_ids[]` **批量原子** Apply：任一失败则**全部不写**；
- Apply / Revoke 都是**事务式**：在 `deepcopy` 上完成全部变更，成功后才就地安装并保存一次；任何异常都保持原状态不变。

### 7B.3 V3 → legacy 兼容投影

`ContinuousRoute3D` 仍是 **authoritative source**：

- 使用其**已验证的 linearized metric geometry**，**不再 simplify**（`simplification_applied=false`）；
- 通过 recorded projected CRS 转到 **OGC:CRS84**；
- `operational_routes` 的 `path` **只保存二维 `[lon, lat]`**；**严禁把 EGM2008 正高写进 GeoJSON 第三坐标**（第三坐标会被误解释为 WGS84 ellipsoidal height）；高度**只**由 locked profile 承载；
- route 记录：`route_id / start / end / start_node_id / end_node_id / kind=operational / status=passed / path`，加轻量 provenance：`source_type=v3c_validated_route`、`validation_id`、`validation_fingerprint`、`refinement_id`、`refinement_fingerprint`、`curve_chord_error_m`、`horizontal_crs`、`crs_transform`、`vertical_reference=egm2008_orthometric`、`planner_family=route_planner_v3`、`cns_integration_mode=post_route_assessment`、`cns_excluded_from_search_cost=true`；
- **完整 analytic geometry 不复制进 `operational_routes`**。

### 7B.4 自动生成且锁定 RouteAltitudeProfile

- 从 V3-C 的 linearized point 取对应 `z(s)`；
- 转成 lon/lat 后，对**最终二维 path** 重算累计距离，得到 `waypoint_linear` profile：
  `vertical_reference=egm2008_orthometric`、`confirmed=true`、`source=v3c_validated_route`、`derived=true`、`locked=true`、`locked_by_adoption=true`；
- 保存 `v3_metric_length_m`、`legacy_geodesic_length_m`、`length_delta_m` 与 `distance_basis`；
- **path 与 profile 顶点顺序、距离基准完全一致**（profile 的 `distance_along_route_m` 就是 published path 的累计基线距离，逐顶点对应）；因此 P7 的 `route_profile_height(offset)` 与路径顶点不会漂移；
- **禁止要求用户重新手填 V3 高度**；
- `RouteAltitudeProfile` normalization 仅做 **additive** provenance/locked 字段兼容（旧项目与用户 profile 不带这些字段，默认 `derived=false`/`locked=false`）；
- `Spatial3DService.set_route_profile` 在 route 当前由 V3 adoption 管理且 locked 时**拒绝手工改高**；撤销 adoption 后才允许（V1/V2 行为完全不变）。

### 7B.5 Adoption state 与 Revoke

新增 `v3_operational_adoptions`（schema/backfill/additive）。每条保存：`adoption_id / route_id / validation ids + fingerprints / refinement fingerprint / projection fingerprint / applied_at / current_applicability / before-after 摘要 / provenance / CNS assessment link`。

- Apply 对对应 `route_id` 做 **upsert**，**不清空无关 `operational_routes`**；
- `route_id / start / end` 保持 scenario identity；
- API：Preview / Apply / Revoke；
- **Revoke 只在"仍由该 adoption 拥有"时**移除该 adoption 及其 derived profile/route——ownership 由 route provenance 的 `source_type + validation_id + validation_fingerprint` 与 profile 的 `source + locked_by_adoption + route_id` 判定；**不得误删其他 planner 后来生成的同 id route**。

### 7B.6 Invalidation

- **不对 Apply 调用 `workflow("route")`**（那会立即把刚发布的 route 标 stale）。新增专用 `v3_operational_route_published`：保持新 route 与 current V3 validation 不变，**只 stale downstream**：
  `coverage_3d → cns_service_capability → service_timeline → cns_gap_v2`，以及依赖 operational route 的 corridor/site/report/building/route-profile 展示结果；
- **Risk / grid / V3-A/B/C 不反向失效**（`environment_risk`、`grid_risk`、`route_planner_v3_experiments`、V3 policy 全部保持原状态）；
- `terrain_dtm` / `buildings` / V3 policy / source audit / validation fingerprint 变化 ⇒ **只**把相关 V3 adoption + adopted operational route 标 stale，并向下 stale CNS；**不得误伤 V1/V2 route**（`V3OperationalAdoptionService.stale_for_sources` 只遍历 adoption 拥有的 route_id）。`airspace`/`basemap` 已从 `stale_for_sources` 移除（返回 `not_applicable`，不 stale 任何 adoption/route）；当前适飞空域变化**不得** stale adoption / adopted route / P7–P10。
- **任何 CNS evaluation 前重新检查 adoption current**：不 current ⇒ `assessment_status=stale`，不给 verdict。

### 7B.7 CNS Assessment bridge

新增"对 V3 adopted route 运行现有 CNS 链"的 orchestrator，**严禁复制/改写 P7-P10 公式**。顺序复用现有服务：

```
P7  Spatial3DService.evaluate
 → P8  CNSServiceCapabilityService.evaluate
 → P9  OperationalTimingService.evaluate_timeline
 → P10 GapAnalysisV2Service.evaluate
```

- 每阶段先检查其**真实 prerequisites**：`spatial_3d.route_altitude_profiles`、confirmed `required_cns`（read from the scoped `project_default` / `route_overrides`，不是容器顶层）、selected aircraft profile、`existing_cns_facilities.items`；缺参数/required_cns/aircraft/timing ⇒ `incomplete` + blocking reason，**禁止造默认**；
- 允许请求 `stages`，默认按 ready 程度顺序运行；
- 每个 `evaluate()` 会持久化自己的 canonical 结果并返回 workflow snapshot，因此 stage entry **从持久化容器读回**；
- 阶段 route 级状态按**必选分系统**计算（P7 覆盖 / P8 能力 / P9 时间线 / P10 缺口都过滤到 required 的 C/N/S）：项目不要求的分系统可以合法地 `missing_data`，不应让评估显得 incomplete，也不应污染 requirement verdict。

### 7B.8 CNS 结果语义

`V3CNSAssessmentBundle` 至少包含：`assessment_status = not_started|incomplete|complete|stale`、`requirement_verdict = meets|does_not_meet|unknown`、`stage_results{P7,P8,P9,P10}`、input/output fingerprints、`route/adoption/validation ids`。

- **"评估完成但 CNS 不满足"必须是**：`assessment_status=complete` **且** `requirement_verdict=does_not_meet`，且 **V3 validation 仍 `validated_route`**；
- **不得**把 CNS gap/coverage failure 改成 route validation failed（normalizer 强制 `route_validation_unchanged=true`，且从不回写 validation）；
- 完整度与需求满足度是两个独立维度，前端**不得用一个总红/绿状态混淆 route safety 与 CNS compliance**。

### 7B.9 写入边界

- 只写 `route_planner_v3_experiments` 之外的新增容器：`v3_operational_adoptions`、`v3_cns_assessment_bundle`，以及既有 `operational_routes` / `spatial_3d.route_altitude_profiles`（**仅** adopted route_id）；
- **不注册**进 `algorithm_registry`；默认 route planner 仍为 `route_planner_v1`；
- 算法包与 domain 层不 import QGIS/GDAL；真实 CRS transform 由 composition root 注入 resolver。

### 7B.10 接口

| 方法 | 路径 |
| --- | --- |
| GET | `/api/route-planner-v3-operational-adoptions` |
| GET | `/api/route-planner-v3-operational-publish` |
| GET | `/api/v3-cns-assessment` |
| POST | `/api/route-planner-v3-operational-adoptions/preview` |
| POST | `/api/route-planner-v3-operational-adoptions/apply` |
| POST | `/api/route-planner-v3-operational-adoptions/revoke` |
| POST | `/api/v3-cns-assessment/evaluate` |

Step 03 在 V3-C 下新增"发布为运行分析航路"：Preview → 显示 route/profile/provenance/downstream invalidation → 显式确认 Apply；状态链显示 **V3-A strategic → V3-B refined → V3-C validated → V3-D published**；synthetic 必须显示"仅测试，不可正式发布"。

Step 04 新增 **V3 CNS Assessment summary**：Route validation / Operational publication / P7 geometry / P8 capability / P9 timeline / P10 gap / Assessment completeness / Requirement verdict——**route validation 与 CNS 结果分列，不合并**。

报告新增 `v3_validated_route_provenance` 段：validated route id/fingerprint、operational adoption、horizontal/vertical representation、curve error、P7-P10 statuses、assessment completeness vs requirement verdict，并明确 **Route Planning → CNS Assessment**、**CNS excluded from V3 search cost**。

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
- 真实数据只通过 **GIS 边界** 进入：V3-A 用 `gis/v3_environment_adapter.py::V3RealEnvironmentAdapter`（FABDEM 窗口只读 + 现有 L8 buildings/building_grid facts + `grid_risk` soft fields + source audit），V3-B/V3-C 用 `gis/fine_environment_adapter.py`（FABDEM 窗口只读 + GPKG RTree 建筑查询）；数据未配置/未确认时**显式返回 blocked**，不构造假环境。**不消费 airspace**。

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
2. **V3-B 未做最终判定**：fine grid 只做保守包络与逐 cell 插值检查，没有 exact polygon membership、exact terrain profile、沿整条轨迹的 continuous clearance、连续曲率验证。**这些由 V3-C 完成**。
3. **运动模型是工程基线**：不是飞行动力学认证模型（无 bank/风/能量）；V3-B 的转弯仍是弧长代理（V3-C 用解析圆弧重新验证 R 与 tangent heading，但仍不是飞行动力学模型）。
4. **cell 级保守语义**：地形/建筑按 cell 的 clearance floor 判定，未表达 cell 内几何细节。
5. **空域是 display-only reference layer**：不参与 cell/edge 可行性、readiness、fingerprint 或 stale；legacy `confirmed_allowed / confirmed_restricted / unknown` 只作显示兼容值。未来法规约束是独立扩展位。
6. **环境可以是合成或真实**：synthetic 路径只用于确定性演练；`configured_real_sources` 需要配置并确认 FABDEM/建筑来源（**不需要**空域来源）。
7. **无 Pareto / 无多目标最优**：soft 向量只报告，标量只是显式加权和。
8. **endpoint 绑定**：V3-A 绑定最近 cell 中心；V3-B 记录显式绑定方法；V3-C 从已记录的 metric 轨迹实现几何，仍不是精确多边形包含判定。
9. **无时间维**：不含时间窗、动态障碍、四维（4D）规划。
10. **energy 缺席**：`pending_model` / disabled。
11. **V3-B 垂直自由度受 corridor envelope 限制**：未声明 margin 时只能做水平精化（见 §6）。

V3-C 追加局限：

12. **只保证 C1**：position + heading 连续；曲率在 straight↔arc 处可跳变，**不是 continuous-curvature / C2**；clothoid 未实现（future）。
13. **折线是有界近似**：vector predicate 对 linearized representation（含显式 `curve_chord_error_m` envelope）精确，**不代表对数学曲线的 exact membership**。
14. **terrain 是 native-raster evidence**：逐 native pixel 判定，**不声称真实世界地形在数学上连续或精确**；NoData ⇒ `unresolved`。
15. **building 仍是 LoD1 prism 语义**：roof = ground + 预测 height（复用既有 BuildingClearance 公式），不是真实屋顶几何/遮挡建模。
16. **kinematics 仍是工程基线**：解析 R 与 tangent heading + 显式 gradient 上限；无 bank/风/能量/飞行动力学。
17. **`validated_route` 不是 operational route**：在 V3-C validation 记录内 `operational_route=false`、`cns_assessed=false` **无条件成立且历史 immutable**；"已采用"与"CNS 已评估"的事实读取自 V3-D 的 `v3_operational_adoptions` / `v3_cns_assessment_bundle`（见 §7B），而不是回写 validation。
18. **不自动修复**：任何违反只给出结构化证据与 `replan_required=true`，**不自动修路、不自动 replan**。

V3-D 追加局限：

19. **操作采用依赖真实来源就绪**：production Apply 需要 `real_data_readiness().v3c.status == ready`（FABDEM + buildings GeoPackage + confirmed V3 policy，**不需要** AirspacePolicy）；当前自动恢复项目为 blocked，因此生产不能从 synthetic 演练直接发布。
20. **表示是有损但显式**：published path 是二维 `[lon,lat]`，V3 metric 几何经 recorded projected CRS 转换后**不再 simplify**，但表示变化本身会引入米制/测地长度差异（记录为 `length_delta_m`）；完整 analytic geometry 不进入 `operational_routes`。
21. **CNS 桥接不产生 route 安全结论**：bridge 只编排既有 P7-P10；`does_not_meet` 是需求不满足，**不是** route unsafe；反之 `validated_route` 也**不是** CNS 合规。
22. **profile 距离基准不是米**：`profile_length_m` 是二维基线累计距离，仅用于与 path 顶点对齐；米制长度见 `v3_metric_length_m`/`legacy_geodesic_length_m`。
23. **不注册为默认 planner**：V3-D 未进入 `algorithm_registry`；默认 route planner 仍为 `route_planner_v1`，V1/V2 行为未改动。

---

## 12. 未来接口

| 阶段 | 目标 | 已就位的接口 |
| --- | --- | --- |
| 未来 | clothoid / continuous-curvature 过渡 | V3-C `ContinuousPrimitive3D`（straight/circular_arc）已预留 arc 解析字段与 `clothoid=future_work_not_implemented` 语义 |
| 未来 | Route–CNS 联合优化（CNS 进入 cost/约束） | 当前明确**禁止**：CNS 只在 `post_route_assessment` 阶段运行，`cns_excluded_from_search_cost=true`；若要进入 cost，必须先用显式 policy 定义归一化与权重 |
| 未来 | energy 模型 | 显式加权 component 接口；V3 阶段明确标注 energy pending |

任何后续阶段都必须继续满足：**没有默认安全值、unknown fail-closed、CNS/energy 不静默进入 cost、coarse→fine 不声称新精度、route safety 与 CNS 合规不合并**。

---

## 13. 相关代码

```
cns_planner/route_planner_v3/
  contracts.py             # V3-A JSON-safe 契约、normalizer、readiness 汇总语义、disclaimer
  motion.py                # MotionPrimitiveProvider、TransitionValidator、kinematic_readiness
  hard_constraints.py      # HardConstraintEvaluator（state/transition 分离）、HardConstraintAudit
  cost.py                  # SoftCostModel（无隐式 normalizer）、length-integrated exposure、cost vector
  planner.py               # V3StrategicPlanner：L8 × 高度 × heading 的确定性 A*（h = 纯几何距离）
  corridor.py              # CandidateRefinementCorridor builder + ring_neighbors
  readiness.py             # 六域 readiness 与 overall 聚合（V3-A/V3-B 共用 policy/aircraft/cost 判定；airspace 固定 not_applicable）
  synthetic.py             # canonical synthetic V3CellEnvironment（显式 spec + normalized index）
  fine_contracts.py        # V3-B 契约、fine refinement policy、fingerprint 与 applicability
  fine_grid.py             # 纯几何：local frame、fine grid、soft 映射、terrain 聚合、建筑保守包络
  fine_search.py           # V3RefinementPlanner：multi-cell stride primitive + traversed cell 检查（保守 supercover）
  fine_synthetic.py        # V3-B synthetic fine environment builder（显式 spec，非真实数据）
  continuous_contracts.py  # V3-C 契约：ContinuousRoute3D/ContinuousPrimitive3D/TurnRealization/
                           #   V3ValidationPolicy/ConstraintViolationInterval/DomainValidationResult/
                           #   V3ContinuousValidationResult、fingerprint、disclaimer
  continuous_geometry.py   # V3-C 纯几何：C1 straight/arc fillet 实现 + explicit curve chord error linearization
  continuous_validators.py # V3-C 纯验证：geometry / terrain / building / altitude / kinematics（airspace 固定 skipped）
  continuous_validation.py # V3ContinuousValidator：domain 汇总、状态映射、fingerprint、资源上限
  continuous_raster_window.py # 纯几何：route envelope × native pixel 的 limiting interval
  continuous_synthetic.py  # V3-C synthetic domain evidence builder（显式 spec，非真实数据）
cns_planner/gis/v3_environment_adapter.py     # V3-A GIS 边界：V3RealEnvironmentAdapter（configured_real_sources）
cns_planner/gis/fine_environment_adapter.py   # V3-B/V3-C GIS/GDAL 边界
                           #   V3-B: FabdemWindowTerrainSource / QgisGpkgBuildingSource
                           #   V3-C: NativeTerrainWindowSource / RouteCorridorBuildingSource
                           #   legacy 兼容（不在证据链中）：ConfirmedAirspacePolygonSource / ConfirmedAirspacePolicySource
                           #   + _FabdemRasterBase（DTM 有效分辨率的单位换算与 provenance）
cns_planner/domain/v3_operational_adoption.py      # V3-D 契约：Projection/Adoption/Preview/CNSAssessmentBundle
cns_planner/application/v3_operational_adoption_service.py  # V3-D：publish gate、二维投影、locked profile、
                           #   operational adoption 容器、专用失效传播、Revoke ownership、P7-P10 CNS bridge
cns_planner/domain/building_clearance.py      # 共享 roof/垂直余量语义（BuildingClearanceV1 与 V3-C 共用）
cns_planner/application/route_planner_v3_service.py  # 独立实验容器与 V3-A/V3-B/V3-C 编排（不写 operational_routes）
cns_planner/web/js/workflow/step03_routes.js         # Step 03 V3-A/V3-B/V3-C/V3-D 面板
cns_planner/web/js/workflow/step04_operation.js      # Step 04 V3 CNS Assessment summary（safety/compliance 分列）
cns_planner/web/js/map/route_planner_v3_overlay.js   # coarse + refined + realized 投影、corridor、violation intervals
cns_planner/reporting/builder.py                     # P19 报告 v3_validated_route_provenance 段
docs/route_planner_v3_architecture.md                # 本文件
```
