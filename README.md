# CNS-PLANNER：低空航路规划与 CNS 设施规划工作台

CNS-PLANNER 是一个面向低空航路与 CNS（Communication / Navigation / Surveillance）工程分析的**本地工作台**，技术栈为 **Python + QGIS/GDAL + 原生 JavaScript ES Modules**，以可保存、可恢复、可审计的 schema-v2 项目为唯一权威状态。

它的产品目标不是"一次算出一个答案"，而是让每一步的输入等级、假设、证据与成熟度都可解释、可追溯：

> **缺失 ≠ 0，unknown ≠ pass，未评估 ≠ 通过。**
> 缺数据、工程假设、未评估、计算失败与领域不通过，在系统中是五种不同语义。

## 1. 当前定位与版本状态

- **冻结基线**：tag `phase4-final-freeze`，HEAD `30281644731602bf967ee44d866da470ecb4f2b0`。
- **当前阶段**：Phase 4 冻结版 Release Candidate。主流程（六步）与正式链已成型，本阶段只做预验收、收敛与文档对齐，**不引入新算法、不进入 Phase 5**。
- **正式工作流只有一套**：下面第 3 节的六步流程。`P1`–`P20` 之类的阶段编号只用于开发历史，**不是**用户工作流。
- 算法版本号（如 `layered_risk_aware_theta_star_v2 @ 2.0`）只出现在技术章节与审计视图，不出现在主界面叙事里。

## 2. 启动方法

Windows 下推荐双击 `启动地图.cmd`，或在项目根目录运行：

```powershell
python map_app.py
```

- 启动器自动查找已安装的 QGIS Python；非默认位置用 `CNS_QGIS_PYTHON` 指定（指向 `python-qgis*.bat`）。
- 就绪后打开 <http://127.0.0.1:8765>；日志位于 `outputs/map-server.log`。
- 停止：在启动窗口按 `Ctrl+C`。整棵进程树由 Windows Job Object 托管，窗口被强杀时 8765 也会由内核释放。
- 启动器复用 8765 上的服务前，会用 `/api/health` 校验 `service`、`ready`、`project_root` 与 `git_commit` 四项身份；版本过旧或不是本项目时**报错而不是静默复用**。

## 3. 当前 canonical 六步工作流

| 步 | 名称 | 业务目标 | 结果 |
|---|---|---|---|
| 1 | **数据准备** | 建立项目、数据来源、范围、坐标与必要输入清单 | 权威数据源、来源审计、工作区准备情况 |
| 2 | **环境与风险** | 建立规划网格、地形/建筑/铁塔/空域/要地环境、Population × Shelter 风险场，并按选定固定高度层派生 Planning Constraint Field | `environment`（含派生的 `planning_constraint_field`）、`risk_field` |
| 3 | **航路规划与发布** | 从 OD 与固定巡航高度生成候选，同时消费 soft Risk Field 与 hard Planning Constraint Field，再完成风险画像与独立连续验证，经人工确认发布 | `route_candidate` → `route_validation` → `operational_route` |
| 4 | **CNS需求** | 基于运行场景形成并确认 CNS 需求 | `required_cns` |
| 5 | **CNS能力与设施规划** | 三维覆盖、服务能力、服务走廊、走廊缺口、走廊站址方案（含监视雷达支线） | `coverage` → `service_capability` → `service_corridor` → `capability_gap` → `facility_plan` |
| 6 | **方案评审与报告** | 比较、选择、确认、应用方案并形成报告 | `plan_review` → `confirmed_plan` → `report` |

六步之间只有一条 production 主链；Advanced（时间线、保护包线、可靠性、安全、DAA）与 Research（V3 实验）不阻塞六步，也不得反向写入正式结果。

## 4. 航路正式链（Step 3）

```text
OD + 固定巡航高度
  ├──→ Population × Shelter Risk Field        （soft planning cost）
  └──→ Planning Constraint Field              （terrain/building/tower/airspace/critical-site 可行性）
  → LayeredRiskAwareThetaStarV2
  → RouteRiskProfile
  → 独立 continuous / native constraint validation
  → Layered Operational Adoption
  → Operational Route
```

关键约束：

- `LayeredRiskAwareThetaStarV2` 是**唯一** production 航路规划器。
- 候选永远是 `provisional`；只有通过独立验证与 authority gate、并经用户确认发布后，才产生 `authoritative` 运行航路。
- 搜索 mask 与连续验证是两件事：搜索 `pass` **不构成**连续验证证据；两者不一致时 fail-closed。
- 候选航路不得直接写 `operational_routes`；发布必须校验候选、风险画像、Constraint Field lineage 与期望指纹。
- 固定巡航高度约束与起降场/进离场程序约束分开：本轮只覆盖 cruise altitude，terminal procedure 单列 deferred。

## 5. 高度层：数据驱动，不写死 80 m

- 高度层目录来自项目数据（`ALT-060` / `ALT-080` / `ALT-100` 以及自定义合法层都是**目录实例**，不是架构常量），可增删与确认。
- 垂向基准的 canonical 参考是 **EGM2008 正高**（orthometric）。`ALT-080` 的 canonical 含义是 **80 m EGM2008 正高**，不是 80 m AGL。
- 缺少垂向基准、转换证据或 AGL 依据时结果为 `unknown/blocked`，**绝不猜值或补 0**。
- 起飞/爬升/下降程序与固定巡航层是分离的对象；运行航路的 2D 几何与 EGM2008 高度不混写进第三维坐标（`egm2008_altitude_in_third_coordinate=false`）。

## 6. Planning Constraint Field：pass / blocked / unknown

- 每个单元按域给出 `pass` / `blocked` / `unknown`，域包括 `terrain`、`building`、`tower`、`airspace`、`critical_site`。
- 合成优先级为 `blocked > unknown > pass`，但**保留所有域 verdict**，不用 dominant verdict 丢证据。
- **fail-closed**：`blocked` 单元不可扩展；`unknown` 必须经显式 policy，production 默认 `block_search`，禁止 `unknown → pass`。
- 逐 cell 证据（高度、required floor、reason code、constraint id、protection geometry ref）只存 artifact；ProjectState 与 workflow 快照只留摘要、指纹与 locator。
- 硬规则示例：地形 `layer_orthometric_m < terrain_orthometric_m + clearance` 为 `blocked`；建筑 `top = resolved_ground + height` 后同样比较；缺高度/基准/clearance 为 `unknown`。
- display-only 图层**不能**冒充规划约束；"未提供要地数据"不得静默解释为"无禁飞区"。

## 7. CNS 正式链（Step 4 → Step 6）

```text
Required CNS（adopted，authoritative）
  → GeometricCoverage3D
  → CNSServiceCapability
  → Service Corridor
  → Corridor Gap
  → Corridor Site Planning
  → Plan Review / Report
```

- 推荐（recommendation）**不具权威性**，必须由用户显式 **Adopt** 后才成为 canonical `required_cns`；没有正式需求来源时使用 Engineering Required CNS Baseline，但同样要显式 Adopt 并保留 assumption 与报告披露。
- Coverage 是 `geometric_only`：不代表传播、探测、完整性或可用度满足要求；`not_evaluated` 项会逐项列出（propagation / line_of_sight / diffraction / interference / link_budget / sensor_detection_probability 等）。
- Service Capability 是静态技术能力判定，**不等同于**当前服务 `available`。
- 走廊空间单元与 evidence 强制外置为 artifact（完整明细可能很大），不允许内联进快照。

## 8. Existing CNS 基线与工程假设

既有 CNS 设施的事实状态必须显式声明，三选一：

| `knowledge_status` | 含义 |
|---|---|
| `not_declared` | 尚未声明（默认） |
| `confirmed_none` | 已确认现实中不存在既有设施 |
| `confirmed_present` | 已确认存在（必须能解析到设施或证据） |

规划模式 `planning_mode` 独立于事实状态：

- `factual`：按事实数据规划。
- `assume_empty_for_planning`：**按空既有设施工程基线**规划，只允许与 `not_declared` 组合，且必须携带显式、已确认、带 `report_disclosure` 的 assumption。它**不表示**现实中不存在 CNS 设施。

系统守卫：空的设施集合**不会**被推断为 `confirmed_none`；缺 assumption 的空基线声明会被拒绝；`confirmed_present` 无设施/证据会被拒绝；`confirmed_present + assume_empty_for_planning` 自相矛盾。重复声明同一基线是幂等的，并会把该字段旧的 active assumption 置为 `superseded` 而不是堆积矛盾声明。

## 9. Radar（Step 5 支线）

- 归属 Step 5 surveillance planning，`KEEP_PRODUCTION`，但默认是 **OPTIONAL** 支线，**不阻塞**核心六步。
- 仅当 adopted Required CNS 或 surveillance policy 明确要求时，该分支及其必要输入才成为 REQUIRED。
- 保持 **proposal-only**：只生成划设方案，不写正式权威容器。

## 10. ProjectState、artifact 与 sidecar

- 唯一权威状态为 schema-v2 `ProjectState`（原子 JSON 持久化 + `revision` 乐观锁）。所有写路径要求同源 + 会话 token + 匹配 revision。
- 大结果（逐 cell 风险、约束场、走廊单元、覆盖采样等）外置为 canonical artifact（gzip + sha256），ProjectState 与 workflow 快照只保留摘要、指纹与 locator；`GET /api/artifacts*` 提供只读读取、清单与 bounded 内容。
- 失效语义：上游权威输入、策略、确认、active id 或 artifact 指纹变化时，下游转 `stale`；`stale` 只可查看，不可继续 Apply 或生成新权威结果。`report` 是失效链末端，历史报告文件保留，但 manifest 会标记其相对当前项目为 stale。
- 打开未知 schema、损坏 JSON 或数据源加载失败时保留当前有效项目；Save As 失败不会切换活动项目，也不遗留 `.tmp`。

## 11. Heavy Task：持久、可观察、可取消

飞行走廊这类重计算走异步任务，而不是长时间占住同步请求与 `mutation_lock`：

- 提交：业务 endpoint 带 `async: true`（或直接 `POST /api/tasks`）→ HTTP `202` + `task_id`。
- 观察：`GET /api/tasks/<task_id>` 提供 `progress` 与 `heartbeat`；`GET /api/tasks` 列出任务。
- 取消：`POST /api/tasks/cancel` 或 `POST /api/tasks/<task_id>/cancel`。
- 输入一致性：提交时冻结 **immutable snapshot**（输入事实 + 算法选择），worker 变化检测与 publish 阶段都用同一份快照比对指纹；输入变化则任务不发布。
- 发布：只有通过指纹校验的结果才写回 ProjectState，并驱动下游失效链。
- **性能准入**：提交时记录准入策略版本，超包线/超天花板的请求按策略拒绝，**hard ceiling 不可 override**。

## 12. 已验证性能包线

当前只声明已被实测验证的范围，**不宣称无限规模**：

| 常量 | 值 | 说明 |
|---|---|---|
| `VALIDATED_EVALUATION_LIMIT` | `3_000_000` | 约等于实测 2.5e6 次评估 / 2.6 GB 那一档 |
| `SAFETY_EVALUATION_CEILING` | `10_000_000` | 约等于实测 5.2 GB 那一档；超过即拒绝 |

- 包线内行为不变（响应会附加只读的复杂度估算）。
- 仅超过已验证包线时按策略处理；超过安全天花板时拒绝，**不存在跳过准入的产品路径**。

## 13. Compatibility / Research 边界

- **legacy 只读兼容**：`RoutePlannerV1`、`RiskAwareRoutePlannerV2`、`LayeredRoutePlannerV1`、`CoveragePlannerV1`(2D)、`CNSGapAnalyzerV1`、`ReuseFirstSitePlannerV1` 等不再是 production 默认，只用于旧项目读取与对照，且**没有**正式权威容器写权。
- **Research/History**：`RoutePlannerV3` A/B/C/D 属于研究实验，其中 V3-D 不得写 `operational_routes` 或其他 production 权威容器；V3 相关入口在高级/研究区域，**不属于 production**。
- `CNSGapAnalyzerV2`（非 corridor gap）属于 Advanced/Compatibility 的旧区间缺口分析，不写 canonical `capability_gap`。
- ClosedLoopService 是 Advanced 的 working-copy 试算，不直接 Apply `confirmed_plan`。

## 14. 当前算法注册（production 默认选型）

```text
risk_model             / risk-model-v1-relative-index              @ 1.1
layered_route_planner  / layered_risk_aware_theta_star_v2          @ 2.0
coverage_model         / geometric_coverage_3d_v1                  @ 1.0
service_model          / cns_service_capability_v1                 @ 1.0
timeline_model         / route_service_timeline_v1                 @ 1.0
protection_model       / tactical_protection_envelope_v1           @ 1.0
site_planner           / corridor_reuse_first_site_planner_v2      @ 2.0
corridor_model         / cns_service_corridor_v1                   @ 1.0
corridor_gap_analyzer  / cns_corridor_gap_v1                       @ 1.0
requirement_model      / operational_context_required_cns_v2       @ 2.0
```

注册表按 `(algorithm_type, algorithm_id, version)` 精确解析；找不到版本时**不静默 fallback**。业务 Service 依赖算法对象而不是 Registry，Registry 只在 Application/Composition 层负责解析与实例化。

## 15. 架构

```text
cns_planner/
  api/                 HTTP 路由、传输、同源/会话/乐观锁门禁
  application/         六步用例编排、失效传播、写权限（production write authority）
  domain/              schema-v2 与各领域契约（含 constraint field、CNS 基线、成熟度）
  gis/                 QGIS/GDAL 适配、CRS、栅格/矢量读取、建筑与地形事实
  data/                数据源 Registry、SourceProfile、网格映射
  algorithms/          production 算法实现
    route/             LayeredRiskAwareThetaStarV2 相关
    corridor/          Service Corridor（含性能准入常量）
    corridor_gap/      Corridor Gap
    coverage/          GeometricCoverage3D
    service_capability/CNSServiceCapability
    radar_layout/      Radar 监视布站（OPTIONAL 支线）
    ...                以及 terrain/building/tower 约束相关原语
  tasks/               Heavy task：immutable snapshot、worker、任务存储
  persistence/         原子 JSON repository 与项目压缩/artifact 引用
  reporting/           报告构建、HTML/PDF 渲染
  reference_data/      参考起降点、航路、铁塔等只读参考数据
  web/                 HTML/CSS/ES Modules 前端（六步工作台）
  compatibility/       旧项目/旧算法只读兼容
  legacy/              Streamlit/schema-v1 原型（非领域模型）
```

架构原则是 **API → Application → Domain/Algorithm**：GIS Adapter 把真实空间数据转成领域输入，算法不直接依赖 QGIS 对象或本机路径。

## 16. 测试与人工验收

后端（固定解释器 `D:\tools\anaconda\python.exe`）：

```powershell
D:\tools\anaconda\python.exe -m pytest -q -p no:cacheprovider
```

前端（在仓库根目录，Node 直接运行测试文件）：

```powershell
node tests/frontend_modules.test.mjs
node --test tests/*.test.mjs
node --check cns_planner/web/app.js
node --check cns_planner/web/js/main.js
```

真实 QGIS HTTP 集成（需要 8765 上的服务）：`tests/test_map_http.py` 默认跳过，设置 `CNS_MAP_TESTS=1` 后运行。

**人工验收入口**（六步依次走通）：

1. 准备项目与数据源 → 检查必要输入清单与来源审计；
2. 定义工作区与网格 → 生成/核对风险场与所选高度层的 Planning Constraint Field；
3. 选 OD 与固定巡航高度 → 生成正式候选 → 风险画像 → 连续验证 → 确认发布；
4. 形成并 Adopt Required CNS；
5. 声明 Existing CNS 基线 → 三维覆盖 → 服务能力 → 服务走廊 → 走廊缺口 → 站址方案（Radar 按需）；
6. 方案评审 select → confirm → apply → 生成报告与交付包。

每一步之间建议执行一次保存/重开，确认 active id、authoritative 结果与 artifact 引用都保持不变。

## 17. 相关文档

- `PHASE4_TARGET_ARCHITECTURE.md`：Phase 4 目标架构（六步契约、canonical DAG、成熟度与 authority gate、模块分区）。
- `AI_DEV_CONTEXT.md`：当前测试基线与开发上下文。
- `docs/`：历史验收报告与阶段性记录（`docs/14-Phase4-B4X验收报告.md` 等）。
- `docs/15-Phase4-Final-Preacceptance.md`：Phase 4 冻结版预验收报告。
