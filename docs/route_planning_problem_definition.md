# 战略航路规划问题定义（专家咨询基线）

## 1. 范围与边界

本问题是低空运行前的**战略水平航路规划**：给定 OD 与已确认约束，生成一条可审计的二维水平 route。高度剖面属于后续独立步骤，不由本问题自动求解。

战略规划不等于 DAA 战术避碰。DAA 处理运行中的 encounter、CPA、告警与响应；不得用战略 route 的可行性替代战术冲突处置，也不得反向把 DAA 工程阈值当作战略空域边界。

## 2. Fact / Constraint / Policy / Objective

| 类别 | 定义 | 当前实例 | 不得混淆为 |
|---|---|---|---|
| Fact | 有来源、可追溯的客观输入 | OD 坐标、MH/T cells、terrain/building、CNS/aircraft facts、reference route | 自动允许/禁止规则或优化偏好 |
| Constraint | 候选 route 必须满足的硬条件 | confirmed allowed airspace 内完整连通、端点合法、hard-constraint BBOX 不相交 | 风险权重或“更好”评分 |
| Policy | 把事实解释为规划资格/阈值的显式确认规则 | AirspacePolicy.allowed/blocked/unknown + confirmed | 由颜色、图层名或经验猜测出的适飞性 |
| Objective | 在可行集合内比较候选路径的量 | V1 路径代价；V2 距离 + `risk_weight_lambda × risk exposure` | 法规安全结论或硬约束 |

## 3. 输入

- OD：已确认的起点、终点与 canonical `[lon, lat]`。
- allowed airspace：AirspaceFeature 几何和显式 AirspacePolicy；只有 confirmed allowed 可供 V2 运行。
- hard constraints：当前契约是命名 BBOX 列表，输入边界 fail-closed。
- risk：RiskModelV1 的相对工程指数及完整度；不是事故概率、TLS 或 SORA GRC。
- terrain/building：可作为独立高度与净空证据；当前不进入 V1/V2 水平搜索核心。
- 可选 aircraft/CNS facts：只在有明确来源和语义时作为后续可行性证据；当前不进入 V1/V2 搜索核心。
- project context：workspace、MH/T grid、grid risk、airspace eligibility 及 hard constraints，均须进入证据指纹。

## 4. 输出

- 一条战略二维水平 route（或结构化 missing/failed 原因）。
- planner id/version、有效参数、输入指纹、path/grid path 及已有风险量。
- 后续独立高度配置和净空评估所需的 route identity。

输出不包含自动平滑、运动学可飞性保证、法规 well-clear、DAA 动作或专家推荐。

## 5. 必须满足的硬条件

1. 输入结构合法；畸形约束必须在 Application 边界拒绝。
2. V2 必须存在 confirmed allowed airspace，且端点与每条搜索 edge 完整位于 allowed geometry 内。
3. allowed graph 必须连通；两个适飞岛之间不得跨越非适飞区域。
4. hard constraints 优先阻断；risk 只能在已允许图内优化。
5. 不得在缺空域数据时静默回退到旧 A*。
6. reference/scenario routes 可以作为事实或候选存在于 allowed 外；只有 operational route 受运行硬约束。

## 6. 当前机制与优化量（冻结事实）

- V1：56×56 经纬度规则格（manifest 默认值）、8 邻域 A*、BBOX hard constraints、共线点简化；不读取 risk、height 或 kinematics。
- V2：MH/T cell-centroid 8 邻域 A*；每条 edge 受预计算 allowed graph 限制；优化量为已有距离与相对风险加权和，`risk_weight_lambda` 语义保持不变；输出完整 grid path，无 smoothing。
- 本轮诊断只读取 planner 已发布结果，不写回结果、不评分、不排名、不推荐参数。

## 7. 已确认假设

- planned route canonical 坐标表示为 WGS84 lon/lat；未确认 CRS 的 reference 数据不得据此做米制度量。
- V1/V2 当前只解决二维战略水平 route；高度为独立配置和评估。
- MH/T grid 是 V2 当前搜索图，同时承载 risk 索引；这只是现状，不代表最终专家结论。
- BBOX 是当前 hard-constraint 契约；BBOX 与真实 polygon 可能不同。
- risk 指标是相对工程指数；不得解释为绝对事故概率或法规合规性。

## 8. 数据未决项

- **DATA-1 — reference CRS**：舟山 reference landing/routes 源坐标 CRS 仍待权威确认，禁止猜测 WGS84/CGCS2000。
- **DATA-2 — ET→XLSX/CSV**：`.et` 只返回 `requires_xlsx_or_csv_conversion`，需人工转换，禁止开发 ET parser 或造数据。
- **DATA-3 — AirspacePolicy**：每个 AirspaceFeature 的 allowed/blocked/unknown 与 confirmed 状态需有明确来源；禁止从图层名、颜色或样式推断。

## 9. 专家建模未决项

- **EXPERT-1 — constraint geometry**：BBOX、polygon、raster 或混合约束的表达和验证边界。
- **EXPERT-2 — vertical/altitude**：2D+独立高度与三维联合规划的边界、垂向规则和验证方法。
- **EXPERT-3 — state space/algorithm**：MH/T 作为搜索空间还是风险索引，以及 any-angle/连续/运动学算法类别。
- **EXPERT-4 — kinematics**：转弯半径、航向、爬升等应进入搜索还是后处理。
- **EXPERT-5 — objective/risk cost**：距离+风险采用加权和、约束、分层或 Pareto，以及如何校准和验证。

这些条目是咨询问题，不在本文件中代专家作答。
