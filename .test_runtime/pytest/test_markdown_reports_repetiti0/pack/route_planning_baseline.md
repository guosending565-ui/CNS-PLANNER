# 航路规划专家证据包 V2（合成 benchmark + 项目证据）

- 生成时间（UTC）：2026-09-19T07:24:02+00:00
- 代码版本：`ce7fe582327dc2020e3b69f4a38445f8879b365d`
- 工具版本：`route_planning_baseline.py@2.0` / schema `route-expert-evidence-2`
- Python：3.13.9（CPython） · 平台：Windows-11-10.0.26200-SP0
- 主机：DESKTOP-2HK3PTP · CPU：32 · 测地后端：`pyproj.Geod(ellps=WGS84)`
- 重复协议：runs=2 · warmup=1 · 统计=min, median, p95, max · seed=None
- 合成算例：确定性生成几何，无真实数据、无 QGIS、无网络。

> 本证据包为专家评审材料：只报告 manifest、输入、状态、质量指标、重复统计与失败原因，
> **不做自动排名、评分或算法推荐**。runtime 统计仅用于报告。

## 1. 当前规划器 Manifest

### `route_planner_v1@1.0`

- 名称：Route Planner V1
- 成熟度：`engineering_baseline` · 提供方：CNS-PLANNER
- 说明：在固定离散工作区中使用硬约束 BBOX 的确定性 A* 航路规划。
- 输入：`scenario_route`, `workspace_bbox`, `hard_constraints`, `grid_size`
- 输出：`operational_route`, `path`, `algorithm_id`, `algorithm_version`, `input_fingerprint`, `environment_risk`, `reason`
- 参数 schema：`{"additionalProperties": false, "properties": {"grid_size": {"default": 56, "minimum": 2, "type": "integer"}}, "type": "object"}`
- 假设：
  - 经纬度工作区离散为规则网格
  - grid_size 默认 56，实际输出即由该离散决定
  - 硬约束仅使用图层 BBOX，并与路径规划同源
- 局限：
  - 经纬度固定格：不按米制等距离散，格内代价不是真实米制长度
  - 非米制搜索：A* 代价为格步数，不是米制距离或风险代价
  - BBOX 硬约束：管制/禁飞区按外接矩形处理，不表达真实多边形边界
  - 无风险/高度/运动学：不读取 grid_risk，不做高度剖面、爬升或转弯约束

### `risk_aware_route_planner_v2@2.0`

- 名称：Risk-Aware Route Planner V2
- 成熟度：`engineering_baseline` · 提供方：CNS-PLANNER
- 说明：在现有 MH/T grid_id 邻接图上，以米制距离和既有相对网格风险执行确定性 A*。
- 输入：`scenario_route`, `grid.cells`, `grid_risk.cells`, `hard_constraints`
- 输出：`operational_route`, `grid_path`, `distance_and_risk_metrics`
- 参数 schema：`{"additionalProperties": false, "properties": {"max_relative_risk_index": {"maximum": 1, "minimum": 0, "type": ["number", "null"]}, "risk_component": {"default": "overall", "enum": ["overall", "ground", "air"]}, "risk_weight_lambda": {"default": 0, "minimum": 0, "type": "number"}, "unknown_penalty_index": {"maximum": 1, "minimum": 0, "type": ["number", "null"]}, "unknown_risk_policy": {"default": "block", "enum": ["block", "penalize"]}}, "type": "object"}`
- 假设：
  - RiskModelV1 分数仅作为 0..1 relative engineering index
  - risk_weight_lambda 非负时直线米制 heuristic 可采纳
  - 最大风险阈值仅为显式工程阈值
- 局限：
  - 不是事故概率、SORA GRC 或 TLS
  - 二维战略水平规划；输出为 MH/T 网格中心连成的二维航路，不计算 P7 高度、三维/四维风险，也不做路径平滑
  - 不在规划器内重算风险

## 2. 质量度量口径

- 距离：`ellipsoidal_geodesic_distance_m` · 椭球：WGS84
- 航向：`ellipsoidal_geodesic_azimuth_deg` · 约定：compass_degrees_clockwise_from_true_north
- 航向变化范围：[0.0, 180.0] · 容差：1e-09°
- 后端：`pyproj.Geod(ellps=WGS84)`

## 3. 用例状态总览

| case | planner run | status | deterministic | runtime median ms |
|---|---|---|---|---|
| `open_space` | `route_planner_v1` | `passed` | `True` | 0.257 |
|  | `risk_aware_route_planner_v2` | `passed` | `True` | 16.343 |
| `malformed_constraint` | `route_planner_v1` | `rejected_at_input_boundary` | `None` | — |
|  | `risk_aware_route_planner_v2` | `rejected_at_input_boundary` | `None` | — |

## 4. 逐用例输入与结果（含重复统计）

### `open_space` — 无障碍开阔空域，起终点对角穿越。

- 适用模式：`works_both`（不允许为通过而修改 planner：`False`）
- 工作区：`[0.0, 0.0, 0.1, 0.1]` level 7
- 网格：900 格 · 格尺寸（度）[0.0033333333333333335, 0.0033333333333333335]
- 场景航路：`BENCH-open_space` [0.01, 0.01] → [0.09, 0.09]
- 硬约束：0 项 `[]`
- 合成阻断盒：`[]`
- 合成风险带：`null`
- V2 参数变体：`[]`
- allowed airspace：`passed` / 900 格

**route_planner_v1** — `passed`

- 调用：`route_planner_v1@1.0` · fingerprint `0085a71c2652c62cb1873461a14da42afd39a2093a2ade4ddfb91c9e1897dfa1`
- 结果原因：A* 规划完成；正式风险代价模型待接入
- 质量指标：path_length_m=12552.275 · detour_factor=1.0000 · segment_count=1 · turn_count=0 · total_heading_change_deg=0.000 · max_heading_change_deg=0.000 · min_segment_m=12552.275
- 硬约束可行性：`accepted`（无硬约束）
- allowed 可行性：`not_applicable`（来自 planner status，未重新判定）
- 风险指标：`{}`（来源 `not_published_by_planner`，未重算风险）
- planner 自报 vs 实测（长度/绕行差）：— m / —
- 重复统计（runs=2，warmup=1）：min=0.228 · median=0.257 · p95=0.286 · max=0.286 ms（stdev=0.041，仅报告）
- 确定性：result/path fingerprint 稳定性 `True`（distinct result=1 · distinct path=1）· seed `None`
- result fingerprint：`fd26b6b31b8ee268c8e08dcbeac63d910e1906d458eac1d62de1fdeb3ada4b71` · path fingerprint：`1b284917de60de5616f5b108b33950851df63946fd6328029583940149645b61`
- 声明期望 `detour_factor<=1.02` → 观察结果 `True`（不用于评分）
- 声明期望 `turn_count==0` → 观察结果 `True`（不用于评分）

**risk_aware_route_planner_v2** — `passed` · 有效参数 `{"risk_component": "overall", "risk_weight_lambda": 0.0}`

- 调用：`risk_aware_route_planner_v2@2.0` · fingerprint `8e3e3575bf258e081ff00b806a8370ee52f68313cc36c3d5c41456961120f10c`
- 结果原因：Risk-aware A* 规划完成
- 质量指标：path_length_m=13075.286 · detour_factor=1.0417 · segment_count=26 · turn_count=25 · total_heading_change_deg=180.000 · max_heading_change_deg=180.000 · min_segment_m=261.506
- 硬约束可行性：`accepted`（无硬约束）
- allowed 可行性：`not_applicable`（来自 planner status，未重新判定）
- 风险指标：`{"risk_exposure_index_m": 655.2231321709688, "mean_risk_index": 0.04999999999999999, "max_risk_index": 0.05, "optimization_cost": 13104.46264341938, "risk_component": "overall", "risk_weight_lambda": 0.0}`（来源 `read_from_existing_planner_output`，未重算风险）
- planner 自报 vs 实测（长度/绕行差）：29.176 m / -0.000000
- 重复统计（runs=2，warmup=1）：min=14.717 · median=16.343 · p95=17.970 · max=17.970 ms（stdev=2.300，仅报告）
- 确定性：result/path fingerprint 稳定性 `True`（distinct result=1 · distinct path=1）· seed `None`
- result fingerprint：`0ab64a3f7233b7e2de9fdf2bb7806743f5ddde9789dd4eab2bc4120e67eb6ec4` · path fingerprint：`cfe1bdb876def0f8cf583f7a6be6544c483e45a70b8aca33d62bac90befc4f1c`
- 声明期望 `detour_factor<=1.02` → 观察结果 `False`（不用于评分）
- 声明期望 `turn_count==0` → 观察结果 `False`（不用于评分）
- planner 自报指标：`{"distance_m": 13104.46264341938, "risk_exposure_index_m": 655.2231321709688, "mean_risk_index": 0.04999999999999999, "max_risk_index": 0.05, "optimization_cost": 13104.46264341938, "straight_line_distance_m": 12580.284847725366, "detour_factor": 1.041666607874049, "risk_component": "overall", "risk_weight_lambda": 0.0}`

### `malformed_constraint` — 构造非法硬约束，验证 fail-closed：非法约束不得进入任何 planner。

- 适用模式：`input_guard`（不允许为通过而修改 planner：`False`）
- 工作区：`[0.0, 0.0, 0.1, 0.1]` level 7
- 网格：None 格 · 格尺寸（度）None
- 场景航路：`BENCH-malformed_constraint` [0.01, 0.05] → [0.09, 0.05]
- 硬约束：1 项 `[{"name": "合成非法硬约束（经度反转）", "bbox": [0.06, 0.02, 0.04, 0.08]}]`
- 合成阻断盒：`[]`
- 合成风险带：`null`
- V2 参数变体：`[]`
- allowed airspace：`None` / 0 格
- 输入边界拒绝：hard_constraints[0]（合成非法硬约束（经度反转）） 的 bbox 经度范围无效（west=0.06 必须小于 east=0.04）；禁止把非法输入当作无约束继续规划。请在数据源中修正该图层的范围，或从硬约束候选图层中移除后再生成运行航路。

**route_planner_v1** — `rejected_at_input_boundary`

- planner 未被调用：非法硬约束在 Application 输入边界被拒绝，planner 未被调用。

**risk_aware_route_planner_v2** — `rejected_at_input_boundary` · 有效参数 `{"risk_component": "overall", "risk_weight_lambda": 0.0}`

- planner 未被调用：非法硬约束在 Application 输入边界被拒绝，planner 未被调用。


## 5. 项目级证据（route experiments / reference comparison / data readiness）

- 状态：**NOT READY** — `not_supplied` / `no_project_path_argument`
- 未提供 --project，跳过项目级真实数据证据；本文件其余部分仍是合成算例证据。

## 6. 复现

```powershell
# 合成 benchmark（重复统计）
python tools/route_planning_baseline.py --runs 2 --warmup 1

# 追加项目级证据（experiments / reference comparison / data readiness）
python tools/route_planning_baseline.py --project <project.json>
```

生成文件：`.test_runtime/pytest/test_markdown_reports_repetiti0/pack/route_planning_baseline.json` 与 `.test_runtime/pytest/test_markdown_reports_repetiti0/pack/route_planning_baseline.md`。

## 7. 已知局限与待专家决策

### 已知局限

- 合成算例只覆盖几何/硬约束/风险带语义，不代表真实空域复杂度。
- V1 为固定 56×56 经纬度网格 + BBOX 硬约束，非米制搜索，且不读取风险/高度/运动学。
- V2 为二维战略水平规划，输出 MH/T 网格中心二维航路，不做平滑，不含高度。
- 重复统计只描述同一进程内的 wall-clock 分布，不是跨机器性能基准。
- 未提供 `--project` 或项目缺少参考航线/确认 CRS 时，真实数据证据为 NOT READY。

### 待专家决策（本轮不决定）

- D1：BS 是否复用航空器尺寸/速度参数，还是保持独立 BBOX；
- D2：BBOX 硬约束是否升级为 polygon/精确几何边界；
- D3：垂直间隔与高度层规则如何确定；
- 是否引入 Theta*/RRT/Dubins/V3 或路径平滑；
- V2 `risk_weight_lambda` 与最大相对风险阈值是否存在工程/运行依据；
- reference route 与 OD 的关联口径（端点距离阈值、是否要求同一来源批次）。

## 8. 明确未做的事

- 未修改 V1/V2 路径搜索核心、代价公式、邻接或输出契约；未为了让任何 case 通过而调整 planner。
- 未新选算法，未实现 Theta*/RRT/Dubins/V3，未决定垂直间隔或 BBOX→polygon 变更。
- 未修改 RiskModel、AirspacePolicy 规则或 BuildingClearance。
- 未排名、未评分、未推荐算法。
- 未实现 ET parser；ET 仍要求人工转换为 XLSX/CSV。
