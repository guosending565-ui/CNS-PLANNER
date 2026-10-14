# Round38-N1 导航监测仿真设计

## 目标与边界

本轮在不修改 Round37-T3D-3B 原工作树、正式舟山 ProjectState 和现有 C、RID、Radar、RTK 规划语义的前提下，新增两个只读能力：统一的 CNS 共享候选站址视图，以及消费该视图的导航监测关联仿真。仿真只表达站点与航路在给定半径下的二维空间关联和时间情景，不认证导航完整性性能，不产生已建成设施事实，也不写 ProjectState。

范围只包含 G0 成果保护、N1 后端、P17 最小缺陷修复、定向测试和交付。N2、FIG、SITE、FC30、T3D4 及正式 CNS 全流程不在本轮范围内。

## G0 与开发隔离

Round37 原工作树固定为 `feat/round37-t3d-3b-intrusion`、HEAD `7bd86d2d287d305461b17be933101546e3225720`，保留 9 个 tracked 修改和 5 个 untracked 文件。现有交付 patch 是 UTF-16LE，Git 无法解析，因此 G0 增补：

- UTF-8 无 BOM、可由 Git 解析的 tracked patch；
- 9 个 tracked 和 5 个 untracked 文件的逐字节源码快照；
- 每个文件的字节数、SHA256、类别和 live/backup 一致性清单；
- 对补丁执行 `git apply --check --reverse`，证明其精确描述当前 tracked 修改；
- G0 后再次确认原工作树的分支、HEAD 和 status 未变。

N1 在 `D:\Projects\_cns_workspaces\dev\round38_n1_navigation\CNS-PLANNER` 的 linked worktree 中开发，分支 `feat/round38-n1-navigation` 从上述提交直接派生，不复制 `projects/`、`outputs/` 或大型地理数据。

## 共享候选站址视图

新增 Application 层只读适配服务，输出稳定、确定性排序的候选资源：

1. `state.nodes` 中全部工程起降节点；
2. `reference_landing_sites.items` 中全部参考起降点；
3. `state.towers.items` / 既有 tower-colocation 派生事实中的全部真实通信站址。

稳定身份分别使用 `node:<node_id>`、`reference-landing-site:<reference_site_id>`、`tower-colocation:<tower_id>`。工程节点带有明确 `reference_site_id` 时，将对应参考记录作为该节点的关联来源，不另生成第二个物理候选；除此之外不按名称或坐标距离自动合并。

每条记录保留原始 ID、名称、坐标、坐标来源、坐标质量、source/representation CRS、站址类型、数据来源、明确关联和限制。视图为 C、N、RID、Radar、RTK 分别输出 applicability；缺少逐站证据时状态为 `not_evaluated` 或 `unqualified`，不得因进入视图自动变为可安装或已确认。

坐标缺失、越界、质量不足或 source CRS 未解析的参考记录仍保留，写明 `spatial_use_status` 和原因，但不参与空间求解。工程节点位于 workspace 内只证明数值落入边界，不构成 CRS 已确认的证据；参考导入节点只有具备可靠的坐标确认或转换证据（例如明确的 confirmed CRS 及可追溯转换记录）才能用于空间仿真。缺少该证据时仍保留工程节点及其关联参考记录的候选身份和 provenance，但 `spatial_use_status=blocked`，不得以节点坐标绕过参考来源 CRS 限制。铁塔只作为真实既有宿主候选，不改写成起降点或既有 CNS 设施，不创建新塔。

## 航路和风险身份

仿真只接受两类明确航路来源：current 的工程 demo adoption，或指定的 canonical operational route。两类来源分别解析和核对自身的 route authority、route ID、点数、CRS、adoption/currentness 身份、完整坐标序列几何指纹及风险身份，不以一类来源的身份字段补全另一类。请求若给出几何指纹，必须与选中航路严格相等；不允许仅凭 `route_id`、相同端点或历史名称复用其他版本几何的里程或风险。

风险画像只在以下条件同时成立时参与排序：profile 状态/currentness 有效；route/layer 相符；profile 的 candidate fingerprint 与当前 adoption 的 candidate fingerprint 相符；该 fingerprint 所绑定的路径身份与当前几何一致。任何一项缺失或冲突都令 `risk_priority_status` 为不可用，并列出原因。基础空间仿真继续运行，不生成虚假的 high-risk 分类。

缺失高程或完整 Route3DProfile 不阻止明确标注为二维的关联计算，但必须使三维/高度相关能力保持 `not_evaluated`，也不得借此复用其他几何的风险证据。

## 仿真算法

纯 Domain 算法接收已投影到明确米制 CRS 的折线、候选点、风险区间、速度证据和参数，不 import QGIS/GDAL、不读文件、不写状态。

默认参数为：

- `monitor_association_radius_m = 2000`
- `gap_tolerance_s = 10`
- `route_speed_mps = 15`
- `risk_priority_weight = 3`
- `max_optional_monitor_sites = 4`
- `force_endpoint_monitors = true`（固定约束，不是可关闭的策略开关）

所有参数连同来源进入结果指纹，并按各自语义校验：`monitor_association_radius_m`、`gap_tolerance_s`、`route_speed_mps` 和 `risk_priority_weight` 必须为有限正数；`max_optional_monitor_sites` 必须为有限非负整数，允许为 0；`force_endpoint_monitors` 省略时固定为 true，显式 false、null 或非布尔值均返回 `undetermined/invalid_parameters`。其他显式 null 或非法值同样失败，不猜替代值。

每个站点通过逐航段“线段—圆”解析求交得到一个或多个里程区间。区间统一裁剪到 `[0, route_length]`，按稳定顺序合并重叠或接触区间，再取补集得到全部连续缺口。算法不得把关联半径描述为认证覆盖范围。

起点与终点各产生一个不可关闭的 mandatory planning requirement。只有站址身份、可靠坐标和 CRS/转换证据均有效时才生成 `simulation_planned` 设备；任一端点缺证时保留强制要求，并将整次仿真明确标记为 blocked，不虚构有效站址。现有设备仅在 service key、设备状态和稳定 site identity 精确匹配时复用；名称相似或坐标接近不足以认定同一设备，同一匹配设备也不得被起终点或候选选择重复计数。

可选站址来自其余可空间使用的起降点和 373 个既有铁塔。贪心分数为新增关联长度，加上 high-risk 新增关联长度乘以 `risk_priority_weight - 1`；风险不可用时退化为纯新增关联长度。并列时按起降点优先、再按稳定站址 ID 排序。选择达到上限、没有正收益或已满足目标时停止。之后逐个尝试删除对最终容忍结论和风险优先覆盖均无影响的冗余可选站。

## 缺口和时间判定

结果包含站点/设备、总关联长度与比例、合并区间、全部缺口、最长缺口、最长通过时间、满足/超出/无法判定分类，以及未满足原因。

时间优先使用完整且可靠的逐航段速度。若不存在该证据，则使用请求中的 `route_speed_mps`；省略时采用 15 m/s 工程仿真假设并明确标注来源。任何被缺口穿越的速度段若为 0、缺失、非有限或 authority 不可靠，该缺口 `duration_s = null`、`tolerance_status = undetermined`，不得判定通过。所有缺口使用同一个用户参数 `gap_tolerance_s`；风险只改变选址优先级，不改变阈值。

## P17 最小修复

根因是 navigation degradation 事件在 `duration_s=None`、`limit_s=None` 时仍写 `exceeds_limit=False`，而 `managed_gaps` 只检查该布尔值，导致无时间依据的事件进入接受链。`exceeds_limit` 必须采用 true/false/null 三态：只有时长和阈值依据均可靠时才产生 true 或 false，任一依据未知时必须为 null。

修复保留有精度证据支持的 `acceptable_degraded` 与 GNSS 回退说明，但 unknown-duration 事件不能进入 subsystem/route/top-level `managed_gaps`，不能贡献 `managed_gap_count`；下游汇总、disclosure、projected gate 和正式 Step6 均不得据此声称满足时间容忍。GNSS 回退的精度可接受性与时间容忍是两条独立证据链：前者可继续成立，后者缺证时必须 fail-closed。C/S 的数值阈值事件逻辑保持原样。

版本号采用 `2.3`：当前代码为 2.1，N0 已确认正式状态存在持久化 2.2，而相关源码、测试和审查材料中未发现 P17 2.3。实施时同步更新代码注册版本和 currentness/staleness 断言，并用旧持久化 2.2 反例证明不会误判 current。不得通过修改正式 ProjectState 消除 stale。

P16 hypothetical evidence / selection 的张力只做只读定向复核；除非它直接影响本缺陷接受链，否则不扩展修复。

## API 与状态边界

提供两个轻量只读调用：

- `GET /api/cns-shared-site-candidates`：候选视图、统计和不可用原因；
- `POST /api/navigation-monitoring-simulation`：显式路由选择和参数下的即时仿真。

POST 只表示带参数的计算请求，不取得 ProjectState mutation lock，不调用 `session.save()`，不触发 invalidation，不运行 P14～P17、P16、Radar 或 RTK 正式规划。响应携带 `writes_performed=0`、参数/航路/候选/风险指纹和仿真语义。

## 测试与验收

采用严格 TDD：每个行为先写失败测试并确认失败原因，再写最小实现。测试至少覆盖：

- 明确 reference 关联去重、未关联近邻不去重、稳定排序和身份；
- unresolved CRS/坏坐标记录保留但不参与空间求解；
- workspace 内的参考导入节点若无 CRS 确认/转换证据仍 blocked；
- 两端强制要求不可关闭、端点缺证令仿真 blocked、设备精确复用且不重复计数；
- 373 塔只读候选且不创建新塔；
- 线段圆区间、重叠合并、多个缺口、最长缺口和 150 m/10 s 数值；
- 分段速度、0/缺失速度的 undetermined；
- 参数逐项校验、`max_optional_monitor_sites=0`、禁用强制端点被拒绝，以及参数变化引起结果指纹变化；
- 风险优先、无风险退化、canonical operational route 与 demo adoption 各自身份核对、错误几何/候选 fingerprint、缺高程不误用；
- 可选站上限、剩余缺口和冗余剔除；
- P17 `exceeds_limit=null` 的 unknown duration 不进入完整接受链、汇总或 Step6，GNSS 回退精度结论仍成立，旧 2.2 结果保持 stale，C/S 行为不变；
- API 调用前后 state 深比较，正式舟山 ProjectState revision 468、SHA256 和数据源哈希不变，ArcGIS 请求 0。

只运行受影响的定向 pytest；不运行正式 CNS 全流程或全量昂贵回归。

