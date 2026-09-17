# Route Planner V3-A 架构：3D 规划合同 + 硬约束内核 + L8 战略搜索

> 状态：**V3-A 已实现**（战略搜索 + 硬约束内核 + soft cost 向量 + refinement corridor proposal）。
> 明确**未实现**：30 m 局部精化、exact polygon/terrain 最终 validator、CNS joint optimization、energy 模型、真实数据 adapter。
> 本文件描述 V3 的目标架构与 V3-A 的落地边界；凡标 **V3-A 现行** 的是已实现语义，标 **V3-B/C/D** 的是后续接口而非承诺。

---

## 1. 为什么是「原生 3D」而不是「2D 航路后配高度」

V1/V2 是**二维战略水平**规划：先得到水平 path，再由 P7 `spatial_3d` 的 `route_altitude_profile` 事后配高度。这种分解在 V3 中被明确放弃，原因不是风格问题，而是它使安全与运动学约束无法在搜索中生效：

- 转弯能力依赖**航向**，而航向是路径的历史属性。2D 网格搜索的 state 只有 `grid_id`，转弯半径只能在生成后「检查」，无法在生成时「约束」。
- 地形/建筑净空依赖**高度与水平位置的联合**。事后配高度无法保证「配出来的高度在这条水平 path 上处处可用」，只能事后发现 breach。
- 因此 2D + 事后配高度的输出，其可行性是**逐阶段局部成立、整体不保证**。

**V3-A 现行**：搜索 state 直接包含高度与航向，约束在 edge 生成时判定。

### 1.1 搜索 state

```
V3State = {
  grid_id,                  # L8 MH/T 网格（水平离散）
  altitude_index,           # 高度层序号（显式 policy 离散）
  altitude_egm2008_m,       # 该高度的 canonical 垂向值
  heading_bin,              # 航向分箱（默认 8 箱 = 45°）
  x, y, z                   # z === altitude_egm2008_m
}
```

- 几何位置是 `x/y/z`；**canonical vertical reference 固定为 `egm2008_orthometric`**，复用项目既有语义（`domain/spatial_3d.resolve_egm2008_height`）。V3 不接受 AGL/WGS84 椭球高直接进入搜索。
- 添加 `heading_bin` 的唯一目的是让「转弯」成为 Markov 属性：状态携带航向，edge 才能判断「这一步的航向变化是否在能力之内」。
- **航向永远由运动几何推导**（edge 的实际方位角），不由调用方声明。motion primitive 不携带「我转了多少度」的字段，避免出现「声明了却没有真的转」的不一致。

**V3-A 现行**：起点无历史，因此起点在**所有** heading bin 上都是等价的合法初始状态（代价 0）。终点是 **cell**（加高度，仅当问题显式要求 z）而不是 heading —— heading 的职责是约束途中的转弯，不是隐含一个「终点必须朝某方向」的要求。

---

## 2. Hard vs soft：分离的语义

### 2.1 Hard（不可违反，fail-closed）

**V3-A 现行**，由 `V3HardConstraintEvaluator` 分两向判定：

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

**fail-closed 规则**：任何安全数据 unknown/NoData 一律不可行，绝不当作「无约束」或「通过」。

- 地形：每个 cell 必须给出 `surface_clearance_egm2008_m`；缺少或 `data_status != passed` 即 blocked。
- 建筑：`data_status != passed` 即 blocked；`data_status == passed` 且无 clearance 表示该 cell **确认无建筑约束**（confirmed absence），这是显式的数据语义而不是缺数据。
- 空域：`confirmed_allowed` 才可行；`confirmed_restricted` 与 `unknown` 都不可行。

### 2.2 Soft（可权衡，必须显式）

**V3-A 现行**：soft cost 始终输出**向量**，不是单一总数：

```
distance / population_risk / traffic_risk / building_exposure / energy
每项: raw / normalized / weight / contribution / unit / source / semantics / enabled / status
```

- 不同单位**禁止直接相加**。只有已明确 `normalization` 与非负 `weight` 的 component 才进入 scalar cost。
- 权重全部显式；V3 **不自动推荐**权重。未显式启用的 component 状态为 `disabled_no_explicit_weight`，其 raw 值仍如实报告，但不参与 scalar cost。
- `energy` 固定 `pending_model` / disabled：**V3-A 不发明 energy 公式**。
- CNS 记录为 `integration_mode=post_route_assessment` 且 `excluded_from_search_cost=true`。

scalar cost 的精确语义：

```
scalar_cost = distance_m + Σ(enabled weight_i × documented normalized_i)
```

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

### 3.2 爬升/下降能力：rate 必须配 explicit speed

```
若提供 max_climb_gradient / max_descent_gradient        → 可直接判 edge
若只提供 max_climb_rate_mps                             → 必须同时有 explicit planning_speed_mps 才能换算
                                                           否则 kinematic readiness blocked
```

禁止猜速度，也禁止从 Aircraft 目录借用 cruise speed。换算结果与来源都显式记录：

```
climb_gradient_source ∈ {
  policy_direct_climb_gradient,
  policy_max_climb_rate_mps_over_explicit_planning_speed_mps,
  aircraft_direct_climb_gradient,
  aircraft_max_climb_rate_mps_over_explicit_planning_speed_mps,
  ..._without_explicit_planning_speed_blocked,
  no_climb_capability_given
}
```

### 3.3 readiness 六域

**V3-A 现行**：`airspace / terrain / building / policy / aircraft / cost_model` 各自 `ready | pending | blocked` 并给出 reasons。

- `blocked`：安全证据本身缺失或不可用（unknown/NoData、无 confirmed allowed 空域、无 canonical 环境）。
- `pending`：证据可能存在，但显式 policy 参数或来源声明尚未提供/确认。
- 两者都**阻止运行**。

### 3.4 运动模型的方法学声明

**V3-A 现行**：`motion_model_id = engineering_3d_motion_primitives_v3a`，语义固定为

```
engineering_kinematic_limits_implemented_in_edge_generation_not_a_flight_dynamics_certification_model
```

即：这是**工程运动学基线**，不是飞行动力学/适航认证模型。不含 Dubins/Reeds-Shepp 弧、不含 bank angle、不含风场与能量管理。转弯半径采用显式弧长解释：航向变化 `dψ` 至少需要 `R·|dψ|` 的水平弧长，与该 edge 的水平步长比较。

---

## 4. 运动 primitive 与 edge 生成

**V3-A 现行**（`MotionPrimitiveProvider` + `TransitionValidator`）：

- 每个 state 生成 8 个水平 primitive（E/NE/N/NW/W/SW/S/SE）+ 2 个纯垂直（climb/descend，V3-A 判为不可行）+ 16 个水平+高度（±1 高度步）= 26 个。
- 每个 primitive 记录 `primitive_id / kind / grid_delta / altitude_delta_steps`；每个接受的 edge 记录 `length_m / climb_gradient / heading_change_deg / altitude_change_m`。
- 过急转向、过陡爬升下降在 **edge 生成阶段**直接不可行，而不是路径生成后检查。
- edge 的几何测地长度与方位角每个环境只计算一次并缓存，使结果与「搜索恰好展开了多少状态」无关（确定性的一部分）。

**确定性 tie-break**：A* 的优先队列以 `(f, g, insertion_order, state)` 排序，`state = (grid_id, altitude_index, heading_bin)` 为稳定字典序；等代价时按 state 字典序选择父节点。

---

## 5. L8 战略搜索与启发函数

**V3-A 现行**：

- 搜索层：L8 MH/T 水平网格 × 显式高度层 × heading bin。
- 启发函数：**3D 几何下界**

```
h = 3D geodesic distance(state, goal) × (1 + Σ enabled soft weights)
```

可采纳性论证（记录在 `heuristic_semantics.admissibility_argument`）：

1. 每条边的 normalized soft penalty ∈ `[0,1]` / m，且 weight 非负；
2. 因此标量边代价 ≥ 3D 边长；
3. soft weight 之和被显式限制（`SOFT_WEIGHT_SUM_LIMIT = 1.0`，超过即 `blocked`），使 worst case 下 `(1 + Σweight)` 仍是下界。

soft penalty 保持**非负**；启发函数不含飞行时间、能量或 CNS 项。

搜索统计完整记录：`expanded_states / generated_states / expanded_transitions / runtime_ms / expansion_cap / expansion_cap_reached / state_space_shape`。达到 `max_expanded_states` 时返回 `failed`（**不**声称成功）。

**hard-constraint rejection 统计**：`HardConstraintAudit` 以稳定 reason id 聚合**去重后的** state/edge 数量（每 reason 至多保留 200 个样本），并提供与搜索展开无关的 `environment_evidence`（cell 级证据缺口计数）。这让「为什么没有找到路径」可被审计，而不是只给一个 failed。

---

## 6. L8 → 30 m → exact 三阶段

**目标架构**（**V3-B 起**才实现后两阶段）：

```
L8 战略搜索（V3-A 现行）
  → CandidateRefinementCorridor（V3-A 现行 proposal）
      ├─ center_grid_ids          : 粗 state path 的 L8 cell 序列
      ├─ support_grid_ids         : 可配置 N-ring（Chebyshev）搜索窗口
      ├─ altitude_envelope        : 计划高度范围（可选显式 margin）
      └─ semantics = refinement_search_window_not_safety_corridor
  → 30 m 局部精化（V3-B，未实现）
  → exact polygon / terrain 最终判定（V3-B，未实现）
```

必须明确的语义：

- corridor 是**下一阶段的搜索窗口**，**不是**安全走廊、不是 operation volume、不是法规边界。
- **N-ring 不是安全净空值**：它只扩大搜索空间，`ring_n=0` 时 support == center。
- `altitude_envelope` 只是**计划高度的包络**（`planned_altitude_extent_not_a_clearance_volume`），不是 clearance volume。
- `refinement_cell_size_m` 由调用方显式给出（通常 30 m）；V3-A **不使用**它做计算，只记录为下一阶段参数。

**V3-A 的诚实边界**：V3-A 的候选路径只在 L8 网格分辨率与离散高度层上被判为硬约束可行；它**没有**做连续空间/精确多边形的最终判定。

---

## 7. CNS post-assessment

**V3-A 现行**：CNS 不进搜索。数据流固定为

```
Route Planning (V3-A)  →  CNS Assessment (既有 P7/P8/P9/P10 链路)
```

结果契约中固定记录：

```
cns_assessment.integration_mode = "post_route_assessment"
cns_assessment.excluded_from_search_cost = true
cns_assessment.evaluated = false
cost_vector.cns_integration 同上
next_stage = "V3-D_CNS_joint_optimization"
```

**V3-D（未实现）**才是 CNS 与航路联合优化的阶段。

---

## 8. 与经典 RCSP 的区别

| | 经典 RCSP（Resource-Constrained Shortest Path） | V3-A 现行 |
| --- | --- | --- |
| 资源 | 多维资源向量，代价与资源同时沿路径累积，靠 dominance/label-setting 剪枝 | **hard feasibility + additive soft cost**：硬约束只做可行/不可行两值判定（无「资源消耗」概念），soft cost 单独累加为标量 |
| 搜索 | 标签修正/Dijkstra 变体，标签集爆炸由 dominance 控制 | 分层状态 + A*（时间无关、静态障碍） |
| 目标 | 资源受限下的最优路径 | 硬可行域内的最小 scalar cost |
| 转弯/爬升 | 可作为资源（如累积转弯角） | 作为**转移可行性**（单步能力上限），不累积 |
| 权衡 | Pareto 前沿 | 本阶段不产出 Pareto 前沿；soft 向量只做**报告**，只有显式加权项进入标量 |

因此 V3-A **不是** RCSP 实现，也**不声称** Pareto 最优或能量最优。选择这条路线的原因是：当前阶段所有硬约束都是「有无证据 + 是否越界」型判定，而不是可累积资源；把这些硬套进 RCSP 会引入并不存在的可交换性假设。

后续若引入可累积资源（例如能量/时间窗），那一部分才适合 RCSP 语义，届时需要显式扩展本文件。

---

## 9. V3-A 输入 / 输出合同

### 9.1 JSON-safe 契约（`cns_planner/route_planner_v3/contracts.py`）

| 契约 | 说明 |
| --- | --- |
| `V3PlanningProblem` | 完整问题：start/goal、policy、aircraft limits、canonical environment、provenance |
| `V3PlanningPolicy` | 显式安全/规划参数 + `cost_model`；**无默认安全值** |
| `V3State` | `grid_id + altitude_index + altitude_egm2008_m + heading_bin + x/y/z` |
| `MotionPrimitive3D` | `primitive_id / kind / grid_delta / altitude_delta_steps` |
| `AircraftMotionLimits` | 显式运动能力，缺失即 pending |
| `V3CellEnvironment` | canonical 逐格环境（terrain floor / building clearance / airspace status / grid index） |
| `V3CostVector` | 多分量 soft cost + `cns_integration` |
| `V3StrategicResult` | 状态、readiness、path、corridor、cost、hard 统计、disclaimer |
| `CandidateRefinementCorridor` | refinement 搜索窗口 proposal |

所有契约带 schema version，可 `json.dumps(..., allow_nan=False)` 序列化。

### 9.2 结果状态（仅允许）

```
strategic_candidate / failed / missing_data / pending_confirmation / not_ready
```

**V3-A 结果永远不是** `final safe` / `validated operational route`：

- `operational_route = false`（normalizer 强制）
- `final_validation_performed = false`（normalizer 强制）
- `semantics.not_final_safe = true`，`semantics.not_validated_operational_route = true`
- `disclaimer` 固定文本随结果一起传递

### 9.3 算法边界

- **V3-A 现行**：算法只消费 canonical `V3CellEnvironment`。**算法层不 import QGIS/GDAL，不读文件**。
- V3-A **不提供**真实 terrain/building adapter；真实数据 readiness 显式为 `blocked`，需要 V3-B/C 阶段补齐 canonical 适配与 30 m/精确校验。

---

## 10. 与既有系统的隔离

**V3-A 现行**：

- 不修改 V1/V2 搜索/输出；不修改 `spatial_3d` 语义；V3 高度**不写入** `route_altitude_profile`。
- 不注册 `algorithm_registry`：V3-A 不是可被 `algorithm_selection` 选中的 planner，默认 route planner 仍为 `route_planner_v1`。
- V3 结果**不写入** `operational_routes`，也不写入 `default planner`；独立容器：

```
route_planner_v3_experiments   # 记录、环境 spec、policy、result、provenance、verdicts
v3_planning_policy             # 显式 policy（可能为 pending）
```

- 诊断/实验路径不产生任何失效传播：写 V3 容器不使 routes / P7-P19 结果 stale。

### 10.1 接口

| 方法 | 路径 |
| --- | --- |
| GET | `/api/route-planner-v3-experiments` |
| GET | `/api/route-planner-v3/readiness` |
| POST | `/api/route-planner-v3/policy` |
| POST | `/api/route-planner-v3-experiments/evaluate` |
| POST | `/api/route-planner-v3-experiments/delete` |

Step 03 只新增**只读/实验面板**：架构、readiness、参数、strategic candidate、3D state 数、cost breakdown、corridor。地图可画 candidate 的 **2D 投影**（高度/heading 在详情显示），本轮不做复杂 3D 渲染。

---

## 11. V3-A 局限（必须随结果一起阅读）

1. **分辨率未精化**：候选只在 L8 网格分辨率与离散高度层上可行；H 未做 30 m 局部精化。
2. **未做最终判定**：没有 exact polygon / terrain validator，因此「战略可行」不等于「连续空间可行」。
3. **运动模型是工程基线**：不是飞行动力学认证模型（无 bank/风/能量）。
4. **cell 级保守语义**：地形/建筑按 cell 的 clearance floor 判定，未表达 cell 内几何细节。
5. **空域只有两值**：`confirmed_allowed / confirmed_restricted / unknown`；不支持分层高度限制的时变空域。
6. **环境为合成**：V3-A 只提供 canonical synthetic environment；真实数据 adapter 未实现。
7. **无 Pareto / 无多目标最优**：soft 向量只报告，标量只是显式加权和。
8. **endpoint 绑定**：起点/终点绑定到最近 cell 中心（战略网格绑定），不是精确多边形包含判定；未显式给 z 时终点不设高度要求（取最小代价可行高度）。
9. **无时间维**：不含时间窗、动态障碍、四维（4D）规划。
10. **energy 缺席**：`pending_model` / disabled，不参与任何计算。

---

## 12. V3-B / V3-C / V3-D 后续接口

| 阶段 | 目标 | 已就位的接口 |
| --- | --- | --- |
| **V3-B** | 30 m 局部精化 + exact polygon/terrain 最终 validator | `CandidateRefinementCorridor`（center/support/altitude envelope/`refinement_cell_size_m`）；`not_implemented_in_v3a` 显式列出这两项 |
| **V3-C** | 真实数据 canonical adapter（terrain surface floor、building required clearance、confirmed airspace）与来源审计 | `V3CellEnvironment` 契约与 `data_status` fail-closed 语义；`real_data_readiness.required_before_real_run` |
| **V3-D** | CNS 联合优化（CNS 进入 cost/约束） | `cns_integration.integration_mode`（当前 `post_route_assessment`）、`next_stage = V3-D_CNS_joint_optimization`；`V3CostVector` 已支持新增显式加权 component |

任何后续阶段都必须继续满足：**没有默认安全值、unknown fail-closed、结果不称 final、CNS/energy 不静默进入 cost**。

---

## 13. 相关代码

```
cns_planner/route_planner_v3/
  contracts.py         # JSON-safe 契约、normalizer、readiness 汇总语义、disclaimer
  motion.py            # MotionPrimitiveProvider、TransitionValidator、kinematic_readiness
  hard_constraints.py  # HardConstraintEvaluator（state/transition 分离）、HardConstraintAudit
  cost.py              # SoftCostModel、build_cost_vector、building_exposure_normalized
  planner.py           # V3StrategicPlanner：L8 × 高度 × heading 的确定性 A*
  corridor.py          # CandidateRefinementCorridor builder + ring_neighbors
  readiness.py         # 六域 readiness 与 overall 聚合
  synthetic.py         # canonical synthetic V3CellEnvironment（显式 spec，非真实数据）
cns_planner/application/route_planner_v3_service.py   # 独立实验容器与编排（不写 operational_routes）
cns_planner/web/js/map/route_planner_v3_overlay.js    # candidate 2D 投影 + corridor 搜索窗口
docs/route_planner_v3_architecture.md                 # 本文件
```
