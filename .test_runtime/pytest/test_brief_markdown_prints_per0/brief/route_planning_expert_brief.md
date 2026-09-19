# 航路规划专家咨询材料

## 项目 / 战略航路任务

CNS Planner 当前任务是战略二维水平 route；高度后续独立处理。战略规划不等于 DAA 战术避碰。专家只需提供算法/建模思路，不需要使用 CNS Planner。

## 当前数据与约束

- hard constraint 当前为 axis-aligned BBOX。
- V2 仅使用 confirmed allowed airspace；unknown 不得猜测。
- risk 为相对工程指数，不是概率或法规结论。
- 真实数据 readiness：**NOT_READY**；数据块 readiness=`partial`。

- NOT READY 原因：`pending_data_items`

| 条目 | 状态 | 原因 | 需要的人工动作 |
|---|---|---|---|
| **DATA-1** | `NOT_READY` | `source_crs_pending_confirmation` | 由数据方给出权威 CRS 证据并写入 reference source_crs（禁止猜测 WGS84/CGCS2000）。 |
| **DATA-2** | `NOT_READY` | `reference_routes_not_imported_from_xlsx_or_csv` | 人工把 .et 转换为 XLSX/CSV 后重新导入；系统不实现 ET parser。 |
| **DATA-3** | `retired` | `not_applicable_by_architecture_decision` | 无人工动作；已按架构决策退役，仅保留 display-only 兼容信息。 |

- reference landing sites：`1` 条 · source CRS 已确认 `False`
- reference routes：`0` 条 · format `None` · status `not_calculated`
- AirspacePolicy：`0` 条 · confirmed `0` · V2 readiness `not_applicable` (`display_only_airspace_not_used_for_route_constraints`)
- 米制度量可用：`False`（真实数据仍 NOT READY 的条目已逐项列出；禁止猜 CRS、禁止解析 ET；DATA-3 已按架构决策退役。）

## V1 / V2 机制与已知限制

### route_planner_v1@1.0

在固定离散工作区中使用硬约束 BBOX 的确定性 A* 航路规划。

- 经纬度固定格：不按米制等距离散，格内代价不是真实米制长度
- 非米制搜索：A* 代价为格步数，不是米制距离或风险代价
- BBOX 硬约束：管制/禁飞区按外接矩形处理，不表达真实多边形边界
- 无风险/高度/运动学：不读取 grid_risk，不做高度剖面、爬升或转弯约束

### risk_aware_route_planner_v2@2.0

在现有 MH/T grid_id 邻接图上，以米制距离和既有相对网格风险执行确定性 A*。

- 不是事故概率、SORA GRC 或 TLS
- 二维战略水平规划；输出为 MH/T 网格中心连成的二维航路，不计算 P7 高度、三维/四维风险，也不做路径平滑
- 不在规划器内重算风险

## Synthetic benchmark

- `open_space`：无障碍开阔空域，起终点对角穿越。 专家问题：开阔空间下应如何定义格网偏置、几何质量与可接受误差？
- `single_obstacle`：单一矩形硬约束位于直线路径中部。 专家问题：单一障碍绕行时，约束几何与路径质量应采用哪些验证指标？
- `concave_obstacle`：U 形（凹）硬约束组合，路径必须绕行。 专家问题：凹形约束应使用 polygon、栅格还是混合表达，并如何验证完整包含？
- `narrow_passage`：两道纵向硬约束之间保留窄通道。 专家问题：窄通道可达性应如何处理分辨率、净空与飞行器尺度？
- `disconnected_allowed_airspace`：合成 allowed airspace 被完全分割为互不连通的两块。 专家问题：不连通 allowed airspace 的不可达证据应如何表达与验证？
- `risk_tradeoff`：工作区中段的高相对风险带；V2 以两个显式 λ 观测“绕行 vs 风险暴露”的取舍，V1 不读取风险。 专家问题：距离与风险应采用加权和、约束、分层还是 Pareto 表达？
- `endpoint_near_boundary`：终点贴近工作区东北边界格。 专家问题：端点贴边时，搜索空间边界和端点连接的数值语义应如何定义？
- `malformed_constraint`：构造非法硬约束，验证 fail-closed：非法约束不得进入任何 planner。 专家问题：约束输入的最低验证契约和 fail-closed 边界应如何定义？
- `zigzag_open_grid_bias`：无障碍斜向 OD，用于暴露 8 邻域格网的阶梯与方向偏置。 专家问题：A* 格网偏置应采用 any-angle、后处理平滑还是运动学 planner 哪类思路？
- `bbox_overblocking_demo`：细长斜向假想约束以轴对齐 BBOX 输入，用于展示包络可能过度阻断。 专家问题：BBOX 与真实 polygon 可能不等价时，应如何选择约束几何表达和保守性？

## Lambda sensitivity

仅列事实，不推荐 λ。

- λ=0.0: status=passed, length=9428.567 m, risk_exposure=8477.803200490252, turns=2, runtime=17.959 ms
- λ=0.5: status=passed, length=10036.353 m, risk_exposure=2283.9016745366816, turns=6, runtime=54.861 ms
- λ=1.0: status=passed, length=10036.353 m, risk_exposure=2283.9016745366816, turns=6, runtime=54.614 ms
- λ=2.0: status=passed, length=10469.621 m, risk_exposure=2044.6158246426505, turns=6, runtime=77.450 ms
- λ=4.0: status=passed, length=10902.890 m, risk_exposure=1935.829004661576, turns=4, runtime=122.962 ms
- λ=8.0: status=passed, length=10902.890 m, risk_exposure=1935.829004661576, turns=4, runtime=137.720 ms

## Grid sensitivity

仅列分辨率依赖，不推荐 level。

- L6: cells=36, status=passed, length=12139.409 m, turns=4, zigzag=0.196924
- L7: cells=900, status=passed, length=11327.900 m, turns=15, zigzag=0.049957
- L8: cells=8100, status=passed, length=11080.523 m, turns=42, zigzag=0.017109

## 观测事实（只描述，不推荐）

### OBS-LAMBDA

λ 从 0 增大时，V2 用更长路径换取更低 risk exposure，并在某个 λ 之后收敛到同一路径。

> 只描述取舍曲线；没有工程依据判定哪个 λ 正确，本材料不推荐取值。

### OBS-GRID

同一 OD 在不同 MH/T level 下路径几何不同：网格越细，路径越贴近目标方位、zigzag_index 越低，但步数与顶点数越多。

| level | cells | length_m | detour | turns | zigzag | E/NE |
|---|---|---|---|---|---|---|
| L6 | 36 | 12139.4 | 1.1900 | 4 | 0.196924 | 3/2 |
| L7 | 900 | 11327.9 | 1.1104 | 15 | 0.049957 | 11/13 |
| L8 | 8100 | 11080.5 | 1.0862 | 42 | 0.017109 | 32/40 |

> 只描述分辨率依赖；本材料不推荐 level，也不声明哪条路径更好。

### OBS-DIRECTION-BIAS

在斜向 OD 上，8 邻域网格路径只使用了部分方向（本例仅 E/NE），其余方向未出现；这是网格离散与 equally-cost 走法的直接结果，而不是路径被平滑或优化过。

- 使用方向：E, NE
- 未出现方向：N, NW, S, SE, SW, W

> 只陈述方向分布事实。是否为偏置、是否需要用 any-angle/平滑/运动学方法消除，属于待专家回答的问题（P6），本材料不代答。

## 典型问题

- 8-neighbour grid direction bias and zigzag geometry
- path geometry changes with MH/T grid resolution
- BBOX envelope may overblock relative to a source polygon
- risk lambda changes length/exposure tradeoff without an approved calibration basis
- reference CRS and AirspacePolicy can keep real-data evidence NOT READY

## 真实数据 readiness

整体：**NOT_READY** · 数据块 readiness `partial`
- **DATA-1** `NOT_READY`：source_crs_pending_confirmation → 由数据方给出权威 CRS 证据并写入 reference source_crs（禁止猜测 WGS84/CGCS2000）。
- **DATA-2** `NOT_READY`：reference_routes_not_imported_from_xlsx_or_csv → 人工把 .et 转换为 XLSX/CSV 后重新导入；系统不实现 ET parser。
- **DATA-3** `retired`：not_applicable_by_architecture_decision → 无人工动作；已按架构决策退役，仅保留 display-only 兼容信息。

## 待专家回答问题

- **P1** 战略航路应采用 2D 水平规划 + 独立高度，还是三维联合规划？
- **P2** MH/T 网格应作为搜索空间，还是仅作为风险索引？
- **P3** 约束几何应采用 polygon、栅格还是混合表达？
- **P4** 转弯半径、航向、爬升等运动学条件应进入搜索还是后处理？
- **P5** 距离 + 风险应采用加权和、约束、分层还是 Pareto 表达？
- **P6** A* 格网偏置应采用 any-angle、平滑还是运动学 planner 哪类思路？
- **P7** 网格分辨率依赖应如何处理、报告和验证？
- **P8** 应如何定义验证指标，以及真实航线应作为何种用途的证据？

## DATA / EXPERT 未决项

- **DATA-1** reference CRS：舟山参考点/线坐标系待权威确认。
- **DATA-2** ET→XLSX/CSV：ET 必须人工转换，系统不解析。
- **DATA-3** retired/not_applicable_by_architecture_decision：当前空域只作为显示参考图层。
- **EXPERT-1** constraint geometry：BBOX/polygon/raster/混合表达。
- **EXPERT-2** vertical/altitude：二维+独立高度或三维联合规划。
- **EXPERT-3** state space/algorithm：搜索状态空间与算法类别。
- **EXPERT-4** kinematics：运动学约束进入搜索或后处理。
- **EXPERT-5** objective/risk cost：距离与风险的目标表达。

本材料只陈述问题和现有证据，不代专家回答，不自动评分、排名或推荐参数。
