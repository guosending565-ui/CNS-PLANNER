# CNS 规划系统开发上下文

> 架构基线：2026-09-13（Asia/Shanghai）
> 当前目标是持续完善 CNS 规划工作台；结构重构不得顺带改变 V1 算法、风险公式、API 路径或项目业务结果。

## 1. 当前架构

系统采用本地 Python + QGIS/GDAL + 浏览器工作台，前端为原生 JavaScript ES Modules，无构建步骤。

```text
map_app.py / app.py
  → cns_planner.map_server（启动与兼容入口）
  → application.ApplicationContext
      ├─ api（HTTP、安全、路由、静态资源）
      ├─ application（六步用例与状态编排）
      ├─ gis（QGIS/GDAL/CRS/渲染/空间输入适配）
      ├─ data（Registry、Health、grid_id Mapping）
      ├─ algorithms / risk / simulation（纯标准输入输出）
      └─ persistence（原子 JSON I/O）
```

依赖方向为 API → Application → Domain/Algorithm；真实空间数据由 GIS 转换为标准业务输入。算法层不 import QGIS，不读取本机数据路径。`services/` 中与新边界重复的模块只保留兼容导入。

## 2. 六步业务流程

1. 项目与数据：项目创建、打开、另存和数据源设置。
2. 工作区与环境：workspace → MH/T grid → population/terrain/airspace/traffic/conflict → relative risk。
3. 航路设计：节点、场景航路、默认 RoutePlannerV1 或显式选择的 Risk-Aware Route Planner V2 运行航路。
4. 运行规则：飞行器、方向、高度、间隔和监视延迟。
5. 设备与布站：C/N/S 设备参数和 CoveragePlannerV1。
6. 确认与导出：统一 ResultStatus 复核，导出项目、航路和站址。

## 3. 已实现能力

- M1：workspace 生成/裁剪 MH/T 4063 标准网格，稳定 `grid_id`，保存恢复、API、Canvas 显示和点击。
- M2–M4：人口 GeoTIFF、GLO-30 DEM、QGIS 空域按 `grid_id` 映射；处理 CRS、NoData、无覆盖和定向失效。
- M5–M6：可替换 `RiskModel`；Ground、Airspace Constraint、Overall 相对风险，参数化权重/阈值、贡献解释和完整度。
- M7：可复现多机直线轨迹、逐格驻留时间、二维 CPA 潜在冲突、traffic/conflict 风险输入。
- CNS 核心输入：AircraftCNSProfileCatalog、RequiredCNS、DeviceCatalog、ExistingCNSFacility、CandidateSite 已纳入 schema v2；已有能力与任务需求严格分离。
- CNS Gap Analysis：V1 保持 RequiredCNS、机载能力及已有设施二维水平覆盖的既有输出；V2 独立合并 P7 三维几何、P8 静态能力与 P9 显式运行时间线，输出 planning/runtime/combined 评估、连续缺口段、contingency/unknown 暴露和稳定输入指纹。
- RoutePlannerV1：固定工作区离散、硬约束 BBOX、A*、geometry/关键节点/统计/指纹。
- Risk-Aware Route Planner V2：直接在 MH/T `grid_id` 邻接图上使用既有 `grid_risk` 相对工程指数执行米制 A*，保留完整 grid path、距离/风险暴露/绕行指标；Registry 默认仍为 V1。
- CoveragePlannerV1：C/N/S 主站、补盲、共址、未覆盖点/航段、统计/指纹。
- schema-v2 项目自动保存、打开、Save As 与数据源恢复；失败操作保留当前有效项目并清理临时文件。
- 本地 QGIS 渲染、原始人口/DEM 图层、在线瓦片、统一数据源中心、六步 ES Module 前端。

## 4. 重要文件职责

- `cns_planner/map_server.py`：QGIS 进程启动、ApplicationContext 组装、事件循环；旧测试/本地集成所需名字为兼容 facade。
- `cns_planner/application/app_context.py`：唯一运行上下文，持有 QGIS runtime、workflow、data、tile cache、repositories 和活动项目会话。
- `cns_planner/api/router.py`：保持 `/api/*` URL，只解析用例和响应，不依赖算法实现。
- `cns_planner/application/workflow_service.py`：六步 facade、步骤可进入性和 snapshot；具体变更委派给 Project/Workspace/Route/Operation/Risk/CNSPlanning/Export Service。
- `application/project_state.py`：schema-v2 空状态、兼容字段回填与 schema 校验。
- `domain/algorithm_manifest.py`、`algorithms/registry.py`：算法可解释元数据、精确注册/查询/实例化，包含受保护的既有 V1 与独立 3D 覆盖、静态能力、时间线和保护包络模型；factory/Python 实现路径不进入 ProjectState 或 API Manifest。
- `application/cns_input_service.py`：五类 CNS 规划输入的选择、导入、需求覆盖、保存与下游失效。
- `application/closed_loop_service.py` + `domain/closed_loop.py`：P12 working-copy 重跑编排、确定性 PlanApplication、Before/After 比较和事务式 Preview/Apply；不实现新的覆盖、能力、时间线或 Gap 公式。
- `route_planner/risk_aware_v2.py`：P13 纯 Python GridGraph、风险证据门控和 risk-aware A*；直接消费标准网格及网格风险，不依赖 QGIS、不重算 RiskModel。
- `domain/cns_corridor.py`、`algorithms/corridor/v1.py`、`application/corridor_service.py`：P14 route corridor 契约、纯 Python 水平/垂向离散、P7/P8 代表点复用及持久化用例。
- `domain/cns_planning_objectives.py`、`algorithms/corridor_gap/v1.py`、`application/corridor_gap_service.py`：P15 显式空间规划目标、P8 独立冗余证据复用、corridor voxel 分类和空间连续缺口投影。
- `catalogs/*`：JSON 飞行器能力与设备目录；`gis/cns_input_adapter.py`：JSON/CSV/Point GeoJSON 设施、站址标准化。
- `application/invalidation_service.py`：工作流、映射属性和风险失效的唯一权威实现。
- `application/review_service.py` + `domain/status.py`：结果状态聚合的唯一权威实现。
- `gis/source_loader.py`、`renderer.py`、`constraints.py`、`raster_adapter.py`、`airspace_adapter.py`：QGIS/GDAL 边界。
- `data/registry.py`、`health.py`、`data/mapping/*`：统一来源描述及标准网格属性映射。
- `algorithms/grid/service.py`、`route/v1.py`、`coverage/v1.py`、`risk/v1.py`：标准网格适配与受 characterization 保护的 V1 实现。
- `persistence/*_repository.py`：JSON 序列化和原子替换；不切换活动项目、不加载 QGIS、不做业务校验。
- `web/js/main.js`：前端组装；API、Store、地图、Sources、六个 Step 位于各自模块。`web/app.js` 是稳定入口。
- `legacy/`：Streamlit/schema-v1 原型；新版代码不得新增对它的依赖。

## 5. ProjectState、状态与失效

新版权威状态为 schema v2。`grid` 只保存基础 geometry/bbox；所有派生结果按 `grid_id` 关联，不复制 geometry：

```text
grid_attributes.population
grid_attributes.terrain
grid_attributes.airspace
grid_attributes.traffic
grid_attributes.conflict
data_source_profiles（population / terrain 的版本、quantity、unit、resolution、CRS、verification、provenance）
algorithm_selection（每类仅保存 algorithm_type / algorithm_id / version / parameters）
grid_attributes.buildings / property_exposure / infrastructure / towers（扩展入口）
grid_risk
aircraft_profiles / selected_aircraft_profile_id
required_cns（project_default + route_overrides）
device_catalog
existing_cns_facilities / candidate_sites
cns_gap_analysis（按 route_id / subsystem 保存，不复制航路）
safety_policy（FailureCondition / UnacceptableEvent / FaultTree / FMEA / FunctionalDependency / CoupledCondition / CoupledUE）
safety_assessment（预留结果容器；P5 preview 不持久化）
spatial_3d（altitude_layers / route_altitude_profiles / site_vertical_profiles；不保存全量 voxel）
coverage_3d（按 route/subsystem 保存 3D 几何覆盖结果）
cns_service_capability（按 route/subsystem/sample 保存静态技术能力判定，不覆盖 coverage_3d）
operational_timing（route_motion_profiles / service_scenarios / response_time_budgets / encounter_scenarios）
service_timeline（按 route/subsystem 保存显式场景驱动的连续运行状态区间）
protection_envelope（独立保存工程战术保护距离结果）
cns_gap_analysis_v2（独立保存 planning/runtime/combined Gap V2；不覆盖 cns_gap_analysis）
site_planning_policy / cns_site_plan（P11 proposal-only 规划输入与提案）
closed_loop_assessment（P12 baseline/planned 重跑证据、比较、provenance 与 commit 状态）
cns_corridor_policy / cns_corridor_assessment（P14 显式走廊配置与独立 voxel/volume-proxy 结果）
cns_planning_objectives / cns_corridor_gap_assessment（P15 显式规划目标与静态 service/redundancy/continuous-deficit 结果）
```

- workspace/grid 变化：所有网格属性、traffic/conflict 和 risk 失效或重算。
- population 变化只失效 population + risk；terrain 同理；QGIS/airspace 变化只失效 airspace + risk。
- traffic simulation 变化失效 traffic、conflict、risk。
- Aircraft/RequiredCNS/Device/ExistingCNS/CandidateSite 变化仅使相应下游 routes、coverage、technical risk、report stale；不改写已保存的 V1 算法结果结构。
- workspace、operational route、运行规则/选定机型、RequiredCNS、DeviceCatalog 或 ExistingCNS 变化会使 cns_gap_analysis stale；CandidateSite 不是 Gap Analysis 输入。
- 缺失/NoData/未知不得转换成零风险或通过。
- safety_policy 变化只使 safety_assessment、technical_risk、report stale；不得使 workspace/grid/routes/coverage/cns_gap stale。
- DEM、航路、已有设施、设备及 P7 垂向/几何配置变化定向使 `coverage_3d` stale；单独修改高度层/航路高度剖面不得反向使 grid/routes/CoverageV1/GapV1 stale。
- `coverage_3d`、RequiredCNS、选定 Aircraft Profile、DeviceCatalog/ExistingCNS 或 service-model 选择变化会定向使 `cns_service_capability` stale；不得反向使 grid/routes/CoverageV1/GapV1 stale。
- route/altitude/P8 capability/RequiredCNS/Aircraft/motion/service scenario 变化会定向使 `service_timeline` stale；response budget/encounter scenario 变化仅使 `protection_envelope` stale。两者均不反向使 grid/routes/CoverageV1/GapV1 stale。
- RequiredCNS、Aircraft、`coverage_3d`、`cns_service_capability` 或 `service_timeline` 变化会定向使 `cns_gap_analysis_v2` stale；`protection_envelope` 仅在 Gap V2 显式启用 protection margin 时使其 stale。该链路不反向影响 grid/routes/CoverageV1/GapV1/P7/P8/P9。
- Gap V2、ExistingCNS、CandidateSite、DeviceCatalog、site policy 或相关算法选择变化会使 `cns_site_plan` stale，并继续使 `closed_loop_assessment` stale。P12 Preview 只保存 assessment；Apply 成功后提交 planned ExistingCNS 与 P7-P10 结果，并只向下游使 CoverageV1、GapV1、site plan、technical risk、report stale，不反向影响 grid/routes/grid_risk。
- route/path、altitude profile/layers、grid/terrain、RequiredCNS、Aircraft、ExistingCNS、DeviceCatalog、corridor policy 及 P7/P8 有效算法参数变化会使 `cns_corridor_assessment` stale；corridor 变化不反向使 route/P7-P12 stale。P12 Apply 因正式 ExistingCNS 改变只额外 stale corridor，不破坏刚提交的 P7-P10。
- current P14 corridor、RequiredCNS 或 `cns_planning_objectives` 变化只向下使 `cns_corridor_gap_assessment` stale；P15 不反向影响 Route/P7-P14/P10-P12。`cns_corridor_gap_result` 已保留面向 P16 SitePlannerV2 的单向依赖边界。
- 不支持 schema、损坏 JSON、数据源加载失败不会替换当前项目；Save As 失败不切换 active project。

## 6. 算法外部契约

- RoutePlannerV1 与 CoveragePlannerV1 保留公开输入输出、`status`、`algorithm_name/version`、`input_fingerprint`、geometry/站址/统计和固定输入确定性。
- RiskModel 接口固定为 `evaluate(grid, grid_attributes, parameters) -> risk_result`。RiskModelV1 输出为 `relative_index`，不是事故或碰撞概率。
- P2 Algorithm Registry 使用精确 `(algorithm_type, algorithm_id, version)` 键，当前注册：`risk-model-v1-relative-index@1.1`、`route_planner_v1@1.0`、`risk_aware_route_planner_v2@2.0`、`coverage_planner_v1@1.0`、`cns_gap_analysis_v1@1.0`、`cns_gap_analysis_v2@2.0`、`coverage_model/geometric_coverage_3d_v1@1.0`、`service_model/cns_service_capability_v1@1.0`、`timeline_model/route_service_timeline_v1@1.0`、`protection_model/tactical_protection_envelope_v1@1.0`、`site_planner/reuse_first_site_planner_v1@1.0`、`corridor_model/cns_service_corridor_v1@1.0`、`corridor_gap_analyzer/cns_corridor_gap_v1@1.0`。`route_planner` 与 `cns_gap_analyzer` 默认仍选择各自 V1；找不到精确版本直接报错，禁止回退。
- `AlgorithmManifest` 固定包含 `name/provider/maturity/description/inputs/outputs/parameter_schema/assumptions/limitations/references`；Registry 内部 factory 不序列化，ProjectState 只保存选择与参数。
- Workflow 启动时从 Registry 解析选择，再把算法对象注入相应 Application Service；业务 Service 不依赖 Registry。
- 未来 RoutePlanner：`plan(start, end, grid, risk, constraints) -> RouteResult`。
- 未来 CNSSitePlanner：`plan(route, required_cns, candidate_sites, device_catalog, parameters) -> CNSPlanResult`。
- `DeviceCatalog`（地面设备型号/性能）与 `AircraftCNSProfileCatalog`（机载已有能力/默认需求）必须分开；已有能力不得等同需求。

P3 CNS Taxonomy & Performance Contract：

- AircraftCNSProfile（机载能力）、RequiredCNS（运行需求）和 CNSDevice（地面设备能力）共用轻量 C/N/S taxonomy，但分别保存，互不推导或替代。
- 每个分系统采用 `type + performance + contingency + source + confirmed + confirmation_status`；未知分类使用允许的 `unknown` 或 `null`，未知性能保持 `null/pending_confirmation`，不填入法规或安全阈值。
- C 类型包含 `service_type/technology/network_scope/interfaces`，性能包含秒制时延、失链/中断、可用度与冗余；N 类型包含导航 technology，性能包含水平/垂直误差、完整性、告警/降级时间、可用度与冗余；S 类型包含 target cooperation、sensor mode、technology，性能包含探测距离/概率、更新/航迹丢失/告警时间、可用度与冗余。
- canonical 时间单位统一为秒；schema-v2 继续保留并同步 V1 别名：`max_latency_s <-> latency_ms`、`max_horizontal_error_m <-> accuracy_m`、`integrity_required <-> integrity`、`max_update_interval_s <-> update_interval_s`、`min_redundancy <-> redundancy`。同时提供不一致检测，禁止静默选择其中一个值。
- availability/probability 校验为 0..1；冗余为正整数；时间和距离非负。Catalog 恢复时执行 additive contract backfill，旧 `capabilities/radius_m/mtbf_h` 等字段原样保留。

P4 CNS Reliability & Effective Service State：

- `ReliabilitySpec` 是独立统计属性：保存 `model/mtbf_h/mttr_h/failure_rate_per_h/availability/availability_type/empirical_failure_probability/failure_mode/operating_conditions/reference_conditions/confidence/sample_size/source/confirmed/status`。未知不补默认值；demo MTBF 回填为 `source=demo, confirmed=false, model=unknown`。
- Aircraft 顶层 legacy `mtbf_h` 不下沉为 C/N/S reliability；每个机载 capability 独立保存 `reliability` 和 machine-readable `fallbacks[]`。P3 `contingency` 文本继续保留，但不作为已确认 fallback。
- 指数可靠度只在明确 `model=constant_rate_exponential` 时计算 `lambda=1/MTBF`、`R(t)=exp(-lambda*t)`、`Pf=1-R`；MTBF 与 failure rate 冲突直接报错。`MTBF/(MTBF+MTTR)` 只输出 inherent availability，operational/empirical availability 独立保存，禁止混合聚合。
- `ExternalServiceSnapshot` 单独表达实时 `available/degraded/unavailable/unknown`、性能、持续时间、冗余与来源；不得抽样 ReliabilitySpec 随机生成实时 lost。
- 有效服务状态为 `available/available_degraded/contingency/lost/unknown`，RequiredCNS `required=false` 例外返回 `not_applicable`。只有 confirmed、性能满足且未超过 bridge/需求允许时限的 fallback 才能进入 contingency；输出 `reasons/evidence/fallback_used`。
- `POST /api/cns/service-state/evaluate` 为无持久化纯计算 preview。Application 为 GapV1 构造不含 `reliability/fallbacks` 的兼容视图，使这些 P4 字段既不参与公式也不改变 V1 输入指纹；GapV1 实现未修改。

P5 CNS Safety Event / FHA / FTA / FMEA Baseline：

- schema-v2 additive/backfill 新增 safety_policy 与预留 safety_assessment。内置 FC-C-01/FC-N-01/FC-S-01 loss FailureCondition 及对应 UE-C-01/UE-N-01/UE-S-01 项目模板；模板统一 severity=unknown/source=project_template/confirmed=false/status=pending_confirmation，不填安全等级或概率目标。
- ServiceState → FailureCondition → UnacceptableEvent 为三个独立层级：P4 lost 可触发 loss FC；available/available_degraded/contingency 默认不触发 loss FC；未确认 UE 即使关联 FC triggered 也只能输出 unknown，禁止自动判为 unacceptable/catastrophic。
- FHA/FailureCondition 保存 function、failure_mode、运行条件/飞行阶段、local/system/operation effect、severity、mitigations、safety_objective 与来源/确认状态。Step 4 明示这是 ARP4761A/FAA-inspired engineering assessment，不是认证结论。
- FaultTree 使用 JSON-safe top_event/and/or/basic_event/reference 节点。定性状态独立求值；只有每个叶概率均已知且相应 AND/OR gate 明确 independence_confirmed=true 时计算 AND 乘积与 OR 1-product(1-p)。依赖未确认时 probability=null/status=pending_dependency，从不假定独立。
- FMEA 仅保存 failure mode 到 FC/UE 的 traceability、effects、detection、mitigation 与来源状态，不计算或保存 RPN。引用在 safety policy 规范化时校验。
- GET/POST /api/cns/safety-policy 持久化策略；POST /api/cns/events/evaluate 与 POST /api/cns/fault-tree/evaluate 为纯计算 preview，不写入 safety_assessment，也不把 risks.technical 变为定量概率或 passed。

P6 CNS Functional Dependency & Coupled Safety Event Analysis V1：

- safety_policy additive 增加 functional_dependencies、coupled_conditions、coupled_unacceptable_events；旧 schema-v2 项目加载时自动回填。FunctionalDependency 保存 dependency_type、C/N/S participants、可选 stages/order/time constraints、operational condition、来源与确认状态，不内建任何通用 N→S 或 S→C 关系。
- 内置 FD/CC/CUE-CS-01、FD/CC/CUE-CN-01、FD/CC/CUE-NS-01 三组模板，分别表达战术冲突缓解信息链、导航恢复对通信的条件依赖、导航信息对监视/DAA 的条件影响。全部 severity=unknown、source=project_template、confirmed=false，只是项目研究假设。
- EventObservation 使用 JSON-safe ref/subsystem/status/start_s/end_s/source。CoupledCondition 支持 all_of、sequence、overlap；有时间规则而缺少时间数据时返回 unknown，顺序错误、超过间隔或重叠不足返回 not_triggered，运行上下文不匹配返回 not_applicable。
- 层级固定为 ServiceState/P5 FC → CoupledCondition → CoupledUE。Dependency、Condition 与 CoupledUE 必须分别确认；未确认 CoupledUE 始终 unknown，不推断 unacceptable/catastrophic。
- P6 所有 coupled 输出 probability=null/probability_status=not_calculated；EventObservation 的额外 probability 不参与计算，CoupledUE policy 拒绝概率输入。P5 FaultTree 的 independence-confirmed 概率规则保持不变。
- POST /api/cns/coupled-events/evaluate 是无持久化纯 preview；Step 4 新增 Functional Coupling 摘要、EventObservation/OperationalContext 输入和预览。

P7 3D Spatial Model & Geometric CNS Coverage Baseline：

- canonical 垂向基准为 `egm2008_orthometric`；`agl` 必须使用 P1 DEM 单格 `surface_elevation_mean_m` 转换，NoData 保持 `missing_data`；`wgs84_ellipsoidal` 未提供明确 geoid undulation/转换时为 `unresolved`，不得与正高直接混用。
- `AltitudeLayer`、`RouteAltitudeProfile`、`Route3DSample` 为 JSON-safe 契约；`VoxelRef` 仅按需构造 `grid_id@altitude_layer_id`，ProjectState 禁止预生成或保存全 workspace voxel。
- ExistingCNSFacility、CandidateSite、CNSDevice additive 保存 `vertical_profile`；legacy `elevation_m` 只进入 `legacy_elevation_m`，不自动解释垂向基准。Device additive 保存 `coverage_geometry`；legacy `radius_m` 仅形成未确认的 `legacy_engineering_assumption/geometric_only`，显式 GNSS 等非站基技术不自动生成球覆盖。
- `GeometricCoverage3DV1` 与 CoveragePlannerV1 分离，三维距离为 `hypot(horizontal_distance, vertical_delta)`，支持 sphere/hemisphere。`sample_spacing_m` 为显式工程采样参数；覆盖长度按真实航路里程区间累计而非样本数量。
- 结果始终标记 `model_scope=geometric_only`；propagation/LOS/diffraction/interference/link_budget/sensor_Pd 均为 `not_evaluated`，不得据此调用 P4 ServiceState 或宣称真实 CNS 性能。
- API：`GET /api/spatial-3d`、`GET /api/coverage-3d`、`POST /api/spatial-3d/altitude-layers`、`POST /api/spatial-3d/route-profile`、`POST /api/coverage-3d/evaluate`。Step 2/3/5 提供最小高度层、航路高度剖面和几何覆盖配置/结果界面。

P8 Technology-Aware CNS Service Capability V1：

- `ServiceModelSpec` 是 JSON-safe 静态服务模型契约，支持 `free_space_link_budget/declared_performance/external_service/unsupported`，并保存 version、technology、applicability、parameters、source、confirmed/status、assumptions、limitations 和 references；旧 radius/demo 数据回填为未确认 `unsupported/missing_data`，不得提升为物理模型。
- `CNSServiceCapabilityV1` 固定按 P7 geometry gate → provider 技术/类型 → Aircraft capability/interface → confirmed service model → RequiredCNS performance 的顺序判定；sample 状态仅为 `meets_under_model/does_not_meet_under_model/unknown/not_applicable/unsupported_model`，语义是静态能力，不是 P4 当前服务状态或 availability。
- 通信自由空间基线使用 P7 三维斜距和 ITU-R P.525：计算 path loss、received power、available/required/link margin；距离小于等于零直接报错。4G/5G 只标为 `free_space_reference/reference_only`，不代表 3GPP channel 或真实蜂窝覆盖；LOS、绕射、干扰、负载、切换均保持 `not_evaluated`。
- N 使用已确认机载或 declared/external performance 比较；GNSS/INS/hybrid 不自动生成地面球覆盖。S 显式比较 cooperation/sensor mode/technology 与已确认 Pd、更新、时延等性能；缺失关键证据返回 unknown。
- provider multiplicity 与 independent redundancy 分离，只有每个有效 provider 都具有确认的独立性及不同 independence group 才能声明独立冗余。route 汇总按真实里程区间分别累计 meets/fail/unknown，unknown 不并入 fail。
- API：`GET /api/cns-service-capability`、`POST /api/cns-service-capability/evaluate`。Step 5 在 P7 几何覆盖后显示 C/N/S 静态能力比例、scope/model、链路裕度或原因及 not-evaluated 声明。

P9 Route Service Timeline & Tactical Protection Envelope V1：

- `operational_timing` 是 additive JSON-safe 配置，分别保存 `route_motion_profiles/service_scenarios/response_time_budgets/encounter_scenarios`。P9 仅实现 confirmed `constant_ground_speed_mps`；P7 `distance_along_route_m` 确定映射为 `time_from_start_s=distance/speed`，缺失或未确认速度保持未知，不借用 Aircraft cruise speed。
- `ServiceScenarioEvent` 保存 id、C/N/S、start/end、external state、type/performance/redundancy、source/confirmed；同一分系统重叠事件直接拒绝，不猜优先级，也不从 ReliabilitySpec/MTBF 随机生成中断。
- `RouteServiceTimelineV1` 只有在存在 confirmed 显式事件时调用 P4 ServiceState；P8 `meets_under_model` 只作为静态证据，绝不自动变成 `available`。输出连续 interval、时间/航路偏移、fallback、reasons/evidence，并分别按状态累计持续时间和真实里程，场景空档独立保留 `unknown`。
- `ResponseTimeBudget` 的 detect/track/processing/decision/communication/aircraft_reaction 每项独立保存 value_s/source/confirmed；`EncounterScenario` 显式保存 relative closing speed、maneuver distance、uncertainty distance，不以 ownship speed 替代。
- `TacticalProtectionEnvelopeV1` 计算 `T_pre=sum(response components)`、`D_reaction=relative_closing_speed*T_pre`、`D_protect=D_reaction+maneuver_distance+uncertainty_distance`。缺任何关键 confirmed 输入返回 unknown/null；结果固定声明 `engineering_tactical_protection_envelope`，法规 Well-Clear 和正式 DAA Detection Volume 均为 `not_evaluated`。
- API：`GET/POST /api/operational-timing`、`GET /api/service-timeline`、`POST /api/service-timeline/evaluate`、`GET /api/protection-envelope`、`POST /api/protection-envelope/evaluate`。Step 3/4/5 提供最小配置和结果界面；P9 不自动触发 P5/P6 事件。

P10 3D / Performance / Runtime-Aware CNS Gap Analysis V2：

- `CNSGapAnalyzerV2` 注册为 `cns_gap_analyzer/cns_gap_analysis_v2@2.0`，但 Registry 默认选择仍是 GapV1。ProjectState 使用独立 `cns_gap_analysis_v2`，V1 `cns_gap_analysis`、实现、API、输出与指纹均不改变。
- V2 只消费 RequiredCNS、P7 `coverage_3d`、P8 `cns_service_capability`、P9 `service_timeline` 和可选 `protection_envelope`，不重新执行几何覆盖、性能匹配、Reliability 抽样或 ServiceState 判定。P8 meet/fail/unknown 与 P9 available/degraded/contingency/lost/unknown 分别形成 planning 与 operational assessment。
- unified breakpoints 由航路首尾、P8 sample distance 和 P9 interval offset 构成。只有相邻 P8 sample 状态相同的中间区间才能继承该状态；状态变化区间保持 unknown。`GapSegmentV2` 保存 route/subsystem、里程/时间边界、planning/runtime/combined 状态、结构化 cause、完整 reasons/evidence、contingency exposure 与保守 remediation scope。
- combined 状态固定为 `satisfied/satisfied_by_contingency/confirmed_gap/unknown/not_applicable`。明确 planning fail 或 runtime lost 为 confirmed gap；contingency 单独统计且不是 gap；无明确 gap 但证据未知时为 unknown。长度/时间按连续区间守恒统计，并输出最大连续缺口。
- protection margin 默认关闭。显式开启后，仅在 P9 protection passed 且 P8 命中 provider 的 DeviceCatalog 有 confirmed 实际监视探测距离时计算 `available_detection_range-d_protect`；缺证据为 unknown，负值才形成 `protection_margin_gap`。始终标记 engineering only，Well-Clear/正式 DAA 合规未评估。
- API：`GET/POST /api/cns-gap-analysis-v2`。Step 5 在 P7/P8/P9 后显示 C/N/S planning、runtime lost、contingency、unknown、最大连续 gap 和 segment evidence；Unknown 明确表示证据不足。Gap V2 不自动触发 P5/P6 Safety Event。

P11 Reuse-first CNS Site Planner V1：

- `site_planner/reuse_first_site_planner_v1@1.0` 是 proposal-only 、确定性 reuse-first weighted greedy set-cover 基线。ProjectState additive 保存 `site_planning_policy` 与 `cns_site_plan`；结果固定 `proposal_only=true` 和 `requires_closed_loop_validation=true`，不修改 ExistingCNS，不声明 Gap 已消除。
- ExistingCNSFacility/CandidateSite 新增显式 `planning_profile`：`reuse_class/add_device_allowed/planning_cost/cost_unit/source/confirmed/status`。缺失数据不根据 `site_type` 或位置猜测建设能力和费用；CandidateAction 只来自已有设施或显式 CandidateSite 坐标。
- Application `SitePlanningService` 负责构造临时设施 clone，依次复用当前 P7 GeometricCoverage3DV1、P8 CNSServiceCapabilityV1 与 GapV2 planning assessment 做 per-action what-if。临时结果不持久化；只有正的 confirmed planning-gap reduction 才是 eligible gain。
- target 仅包含 GapV2 `planning_status=confirmed_gap` 且有可能由地面服务改善的 segment；unknown、runtime-only lost 和 contingency 默认不规划。复用 tier 顺序固定为 existing CNS facility → existing shared site → candidate site → new-build candidate，已覆盖区间不重复计益。
- confirmed explicit cost 使用 marginal gap/cost；缺 cost 时只用 action-count proxy，不伪造货币。provider 数、共址和多个 action 不自动视为 independent redundancy；单 action 无收益但可能需联合求解时保留 residual 并标记 `requires_joint_optimization`。
- P11 planning metadata 在正常 P7/P8/GapV1 调用前从兼容输入视图剔除，保护已有算法指纹。API 为 `GET/POST /api/cns-site-plan`；Step 5 显示 target、CandidateAction what-if 收益、selected proposal、remaining gap 及 P12 闭环复核声明。

P12 Closed-Loop Plan Application & Reassessment V1：

- P12 是 Application 层的 engineering closed-loop verification，不注册新算法，也不声明真实 CNS 模型 validation、认证结论或安全改善。ProjectState additive 保存独立 `closed_loop_assessment`。
- Preview 在 deep-copied baseline/planned working state 中使用相同实现与有效参数重跑 P7→P8→P9→P10；planned 副本一次应用 P11 全部 selected actions。Preview 正式状态除 assessment 外不变，P11 估计收益不替代真实重跑结果。
- `PlanApplication` 以 site-plan fingerprint、baseline fingerprint 和 action IDs 确定性生成 identity，记录 applied facility/device refs 与 planning provenance。既有设施只追加设备；Candidate/New-build 使用显式站址坐标转换为确定性 ExistingCNSFacility，且不修改 DeviceCatalog/CandidateSite。
- Before/After 分别比较 planning/combined gap、unknown、contingency、runtime lost、最大连续 gap 与 gap count。Gap→Unknown 不计 resolved；有新 confirmed regression 为 regression；confirmed planning gap 实际下降且无 regression 才为 validated_improvement。
- Apply 只接受当前 validated Preview，并重新校验 baseline/site-plan/assessment fingerprint。工作副本或重跑任一步失败则内存与磁盘正式状态不变；成功时单次保存 planned ExistingCNS 与 P7-P10 结果。相同 application/action 通过 `planning_origin` 幂等去重。
- API 为 `GET /api/cns-closed-loop`、`POST /api/cns-closed-loop/evaluate`、`POST /api/cns-closed-loop/apply`；Step 5 显示 Before/After/Delta、predicted/actual、residual、regression 和 Preview/Apply 边界。

P13 Risk-Aware Route Planner V2：

- `route_planner/risk_aware_route_planner_v2@2.0` 是 additive 可选算法；默认 route planner 及旧项目 backfill 继续使用 `route_planner_v1@1.0`。V1 实现、调用、输出和指纹未修改。
- `GridGraph` 直接消费当前 MH/T `grid.cells`，优先使用显式 row/column 或稳定 MHT `grid_id` 索引，否则按 bbox 边/角邻接；使用八邻域且禁止对角穿过被阻断角点。起终点使用半开边界映射到包含网格。
- 节点/边距离复用项目米制 `distance_m`。对边定义 `r_edge=(r_i+r_j)/2`、`risk_exposure=d_m*r_edge`、`edge_cost=d_m*(1+lambda*r_edge)`；`lambda>=0` 时到目标的米制直线距离为 admissible heuristic。
- `risk_component=overall|ground|air` 只读取对应 `status=passed` 的 `score`。unknown 默认 block；penalize 必须显式给 0..1 penalty。可选最大风险仅是工程相对指数阈值。missing/stale 不变成 0，硬约束始终独立且不可进入。
- 输出保留真实起终点和 centroid path，并完整保存 `grid_path`、距离、risk exposure、mean/max risk、optimization cost、直线距离、detour、参数、风险来源/fingerprint；语义固定为 `relative_engineering_index_not_probability` 和二维战略水平规划。
- Step 1 通过 Registry Manifest 展示 V2；Step 3 仅在选中 V2 时显示 lambda/component/unknown/threshold 与 Distance/Risk exposure/Mean/Max/Detour。继续复用既有算法选择和运行航路 API。
- 只有当前 route planner 精确选择 V2 时，`grid_risk` 变化才使 routes 及 P7-P12 下游 stale；V1 选择时不新增 route 对 grid risk 的依赖。算法/参数选择变化沿既有 route dependency 失效。

P14 CNS Service Requirement Corridor & 3D Volume Assessment V1：

- `corridor_model/cns_service_corridor_v1@1.0` 是 additive 工程评估；ProjectState 独立保存 `cns_corridor_policy` 与 `cns_corridor_assessment`。它不是 JARUS Operational Volume、U-space Surveillance Volume 或法规批准空间，也不改变中心线 GapV2。
- 每条航路的 `CNSCorridorSpec` 显式保存水平半宽、上下垂向余量、source/confirmed/status；缺失或未确认保持 pending_confirmation，不提供监管或工程默认宽度。
- 水平离散按 cell center 到航路最近米制距离与 cell half diagonal 做保守网格纳入，语义固定为 `conservative_grid_cell_inclusion_not_exact_buffer`。垂向严格复用 RouteAltitudeProfile、terrain、AltitudeLayer 和 `resolve_egm2008_height`；AGL 无 DEM 或 WGS84 椭球高无 geoid 证据保持 unknown。
- voxel 继续使用 lazy `grid_id@altitude_layer_id`。代表点取 cell center 与 layer/corridor overlap 中点，语义为 `representative_voxel_probe_not_entire_voxel_guarantee`；体积仅为 cell 米制面积近似乘 overlap 厚度的 `discretized_volume_proxy_not_exact_corridor_volume`。
- P7 暴露并继续复用同一纯 geometry point helper；P8 暴露并继续复用同一 static capability point helper。P14 按 P7 geometry → P8 technology/aircraft/service-model/performance/redundancy 判定，unknown/unsupported 不转为 deficit 或 passed，且只使用 ExistingCNS，CandidateSite/P11 proposal 不参与。
- API 为 `GET /api/cns-service-corridor`、`POST /api/cns-service-corridor/evaluate`。Step 4 配置 route corridor spec；Step 5 展示 C/N/S satisfied/confirmed-deficit/unknown voxel 数与 volume proxy 比例。

P15 CNS Spatial Planning Objectives, Redundancy & Continuous-Deficit Assessment V1：

- `corridor_gap_analyzer/cns_corridor_gap_v1@1.0` 只消费 current P14 `cns_corridor_assessment`、RequiredCNS 与独立 `cns_planning_objectives`；不重算 P7/P8，不读取 CandidateSite/P11 proposal 或 P9 runtime。
- 每个 voxel/subsystem 保留 P14 service status，并从 P14 `provider_evaluations` 统计 confirmed `meets_under_model` provider。独立数量直接复用 P8 helper：任一合格 provider 缺 confirmed independence/group 即为 null，不从 facility/site 数量推断；共塔不附加惩罚。
- C/S 及站基 N 的冗余按 RequiredCNS `min_redundancy` 判断。非站基 GNSS/GNSS-RTK/INS/visual/hybrid 保持 P14 服务语义，地面 provider 冗余标为 `not_applicable_to_site_provider_redundancy`，不因无地面 N 站产生 gap。
- combined 仅在 service 或 redundancy 明确失败时为 confirmed gap；unknown 证据单独保留且不进入 confirmed target。volume proxy 按 satisfied/deficit/unknown 守恒分类。
- confirmed deficit voxel 按 `nearest_route_offset ± cell_half_diagonal` 裁剪并确定性合并，语义固定为 `conservative_longitudinal_projection_of_corridor_voxel_deficits`。正式术语为 **spatial continuous-deficit**，不是 runtime outage、正式 ICAO continuity 或 availability probability。
- PlanningObjective 与 RequiredCNS 分离；五类阈值均保存 value/operator/source/confirmed/status，默认完全不配置。输出 `met/not_met/unknown/not_applicable` 及聚合 `objectives_not_configured/objectives_not_met/objectives_unknown/objectives_met`。
- 明确不评估 common-cause、shared-power、shared-backhaul、tower failure 或 site-failure propagation；不计算概率。API 为 `GET/POST /api/cns-planning-objectives`、`GET /api/cns-corridor-gap`、`POST /api/cns-corridor-gap/evaluate`；Step 5 提供最小目标配置及 service/redundancy/continuous-deficit/Actual-Pass 展示。

## 7. 数据源扩展

统一定义至少包含 `id/name/category/type/formats/required/health/coverage/source_metadata`，并新增 `source_mode/source_type = real | synthetic | manual`。需要进入计算的数据源通过轻量 `SourceProfile` 保存 `source_id/name/version/quantity/unit/resolution/crs/verification/provenance`；数值边界可使用 `QuantityValue(value/quantity/unit/source_unit/conversion/source/confirmed/status)`，不依赖大型单位或 PROV 库。

P1 数据语义契约：

- WorldPop R2025A（alpha）源量固定为 `population_count_per_source_pixel`、`person/source_pixel`、WGS84/EPSG:4326、约 3 arc-second。目标 MH/T 网格输出 `population_count_people` 与 `population_density_people_km2`，密度分母为该格实际球面面积。
- count 重映射方法为 `area_weighted_source_pixel_overlap`：按源像元/目标格交叠面积分配并守恒求和，不使用 bilinear 人数；保存 mapping method、假设、源分辨率和覆盖率。部分覆盖或 NoData 的 `quantity_status=missing_data`，不得作为 0 或 passed。
- Copernicus GLO-30 固定为 DSM、`surface_elevation`、m、水平 WGS84-G1150/EPSG:4326、垂直 EGM2008/EPSG:3855、1 arc-second。
- schema v2 以 additive/backfill 方式新增 `data_source_profiles`。人口旧字段 `value_sum/value_mean/value_min/value_max/value_unit/unit_status/interpretation`、DEM 旧字段 `mean_elevation/min_elevation/max_elevation/elevation_unit` 暂时保留，保障 RiskModelV1 和旧项目外部语义；权威新字段与旧字段不得混称。

当前 Registry 已覆盖 basemap、airspace、population、terrain、buildings、property exposure、obstacles、infrastructure、towers、traffic、existing CNS、candidate sites 等。

新增来源原则上仅增加：Registry 定义 + GIS Adapter + Mapping Service；不得在 MapData.metadata、Workflow 或 HTTP handler 中补丁式拼接。

## 8. 测试基线

环境：Python 3.13.9，pytest 8.4.2。命令：

```powershell
python -m pytest -q -rs
node --test tests/grid_theme.test.js tests/frontend_modules.test.mjs
node --check cns_planner/web/app.js
node --check cns_planner/web/js/main.js
```

P2 完整运行结果：**150 passed, 6 skipped, 1 failed**；Node 前端纯函数 **9 passed, 0 failed**。6 项跳过均为 `tests/test_map_http.py` 的真实 QGIS 服务集成测试。唯一失败仍是 P1 前已存在的 `test_qgis_adapter_transforms_crs_filters_workspace_and_uses_spatial_index`：测试替身要求 `QgsSpatialIndex(features)`，当前 airspace adapter 使用真实 QGIS 支持的空构造后 `addFeature`；P2 未修改空域生产代码。

真实 QGIS 3.44.14 初始化与 ApplicationContext 冒烟已通过：加载 31 个本地图层、schema v2、1 个 Aircraft Profile 和 6 个 Device Catalog 条目。

测试保护：Route/Coverage characterization、MH/T 网格、raster/airspace/traffic/conflict 映射、RiskModel、ProjectState、persistence/repository、安全失败语义、Workflow、tile cache、API URL 与前端网格纯函数。原 4 个持久化 xfail 已修复并转为普通通过测试。

CNS Gap Analysis 本轮定向基线：41 passed；覆盖解析长度、missing/pending/not_applicable、航路级需求覆盖、冗余、保存恢复、旧项目回填、输入失效及 API/UI 接线。

Algorithm Registry 定向基线：44 passed；覆盖四个 V1 Manifest、精确版本无 fallback、schema-v2 backfill/保存恢复、未知选择显式失败、Dummy 真正换实例、同选择 no-op、四类定向失效、API、Step 1 和 V1 characterization。

P3 完整基线：**161 passed, 6 skipped, 1 known failed**；Node 前端纯函数/Step 4 **11 passed, 0 failed**，`step04_operation.js` 语法检查通过。新增覆盖 legacy backfill、canonical/V1 alias 双向转换与冲突、C/N/S 枚举和数值校验、Aircraft/Device 分离兼容、route override、保存恢复、Step 4 语义及四个 V1 characterization。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题，P3 未修改生产空域代码。

P4 完整基线：**175 passed, 6 skipped, 1 known failed**；Node **11 passed, 0 failed**，`app.js/main.js/step04_operation.js` 语法检查通过。新增覆盖 ReliabilitySpec 校验/来源、显式指数公式、inherent 与 operational availability 分离、MTBF/lambda 冲突、demo backfill、Aircraft 分系统可靠性/保存恢复、fallback 的 available/degraded/contingency/lost/unknown/not_applicable 转换、bridge 超时、纯 preview API、P4 字段不改变 GapV1 指纹及 V1 characterization。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题，P4 未修改生产空域代码。

P5 完整基线：**191 passed, 6 skipped, 1 known failed**；P5 定向测试（含 P4、CNS 输入/Gap、repository、architecture 与四个 V1 characterization）**77 passed**；Node **11 passed, 0 failed**，main.js/step04_operation.js 语法检查通过。新增覆盖 FC/UE 模板与枚举、lost/contingency 分层、未确认 UE、FTA AND/OR 与独立性门控、FMEA 引用、schema-v2 backfill/保存恢复、定向失效、API 纯 preview 和 Step 4 声明。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题，P5 未修改生产空域代码。

P6 完整基线：**206 passed, 6 skipped, 1 known failed**；P4/P5/P6、architecture 与四个 V1 characterization 定向回归 **54 passed**；Node **11 passed, 0 failed**，main.js/step04_operation.js 语法检查通过。新增覆盖 P6 backfill/引用校验、EventObservation、all_of/sequence/overlap、时间缺失/顺序/超时/重叠、运行适用性、未确认 dependency/CoupledUE、禁止耦合概率、保存恢复、定向失效和纯 preview API。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题，P6 未修改生产空域代码。

P7 完整基线：**219 passed, 6 skipped, 1 known failed**；P7 与 Registry/Gap/V1 定向回归 **42 passed**；Node **12 passed, 0 failed**。新增覆盖垂向基准、AGL/DEM NoData/ellipsoid 拒绝、lazy voxel、constant/waypoint-linear contract、route3D、sphere/hemisphere 边界、legacy radius 假设、GNSS 不自动建球、真实航路长度统计、Registry、API、保存恢复与定向失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P7 未修改生产空域代码。

P8 完整基线：**231 passed, 6 skipped, 1 known failed**；P8 能力模型定向测试 **11 passed**；Node **12 passed, 0 failed**。新增覆盖 ServiceModelSpec、geometry gate、P.525、技术/性能、独立冗余、长度汇总、Registry、保存恢复、API 与单向失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P8 未修改生产空域代码。

P9 完整基线：**242 passed, 6 skipped, 1 known failed**；P9/P4/P7/P8/Registry/Safety/V1 定向回归 **100 passed**；Node **12 passed, 0 failed**，Step 3/4/5 与主入口语法检查通过。新增覆盖 distance→time、非法/未确认速度、P8 meet≠available、显式场景与 P4/fallback、事件重叠、duration/length/unknown、响应预算、缺失参数、显式 relative speed、保护距离 fixture、Registry、schema-v2 backfill、保存恢复、API 和定向失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P9 未修改生产空域代码。

P10 完整基线：**249 passed, 6 skipped, 1 known failed**；P10/GapV1/P7/P8/P9/Registry/Project/architecture 定向回归 **85 passed**；Node **12 passed, 0 failed**，`app.js/main.js/step05_cns.js` 语法检查通过。新增覆盖 planning fail、runtime lost、contingency/unknown 分离、P8 状态跃迁保守 unknown、统一断点与相邻合并、长度/时间守恒、最大连续 gap、可选 protection margin、Registry 默认 V1、schema-v2 backfill、保存恢复、API 与单向失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P10 未修改生产空域代码。

P11 完整基线：**260 passed, 6 skipped, 1 known failed**；P11/P7/P8/P10/Registry/CNS inputs 定向回归 **64 passed**；Node **12 passed, 0 failed**，`app.js/main.js/step05_cns.js` 语法检查通过。新增覆盖 planning profile/backfill、target 过滤、locked/unusable/unsupported/缺失证据门控、真实 P7/P8 what-if、reuse tier、重叠收益去重、cost proxy、deterministic tie-break、独立冗余保守残留、上游不变/指纹兼容、Registry、schema-v2 保存恢复、API 与定向失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P11 未修改生产空域代码。

P12 完整基线：**268 passed, 6 skipped, 1 known failed**；P12/P11/P7-P10/GapV1/Registry/Persistence/architecture 定向回归 **104 passed**；Node **12 passed, 0 failed**，`app.js/main.js/step05_cns.js` 语法检查通过。新增覆盖 Preview 零污染、全部 selected actions 组合真实重跑、Apply 单次提交与幂等保存恢复、predicted-vs-actual、Gap→Unknown、regression/no-effect/inconclusive、显式 runtime loss 保持、stale assessment 拒绝、重跑异常 rollback、API、schema-v2 backfill 与定向失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P12 未修改生产空域代码。

P13 完整基线：**278 passed, 6 skipped, 1 known failed**；P13/Registry/V1/P7-P12/Workflow/Risk/Persistence/architecture 定向回归 **114 passed**；Node **13 passed, 0 failed**，`main.js/step03_routes.js` 语法检查通过。新增覆盖 MH/T grid_id 与 bbox fallback 邻接、lambda=0 最短路、lambda>0 风险绕行、距离-风险权衡、边风险暴露公式、unknown block/显式 penalize、工程阈值、硬约束/对角 corner、missing/stale risk、确定性 tie、米制距离、Dijkstra 最优性参照、Registry 默认 V1、Workflow 接线与 V2-only 条件失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P13 未修改生产空域代码。

P14 完整基线：**288 passed, 6 skipped, 1 known failed**；P14/P7/P8/Registry 定向回归 **48 passed**；Node **13 passed, 0 failed**，`app.js/main.js/step04_operation.js/step05_cns.js` 语法检查通过。新增覆盖中心线满足但走廊边缘缺口、水平保守纳入、垂向 layer overlap、waypoint-linear 高度、AGL DEM NoData/WGS84 geoid 缺失、未确认 service model、confirmed fail、GNSS 非站基导航、CandidateSite 隔离、volume proxy 守恒、确定性指纹、P7/P8 point helper、Registry/API/schema-v2 backfill/保存恢复、P12 Apply 与单向失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P14 未修改生产空域代码。

P15 完整基线：**302 passed, 6 skipped, 1 known failed**；P15/P14/P8/Registry/P12 定向回归 **58 passed**；Node **13 passed, 0 failed**，`app.js/main.js/step05_cns.js/step06_review.js` 语法检查通过。新增覆盖一/二 provider 冗余、独立性缺失、confirmed 独立数量不足、非站基 GNSS、unknown target 隔离、连续投影 merge/route clipping/确定性、volume 守恒、objectives met/not-met/unknown/not-configured、无默认阈值、stale/missing P14 拒绝、无 common-cause 推断、Registry/API/schema-v2 backfill/保存恢复、P12 Apply 和单向失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P15 未修改生产空域代码。

当前里程碑：**CNS-PLANNER v1.0 research baseline / ready for synthetic end-to-end validation**。

## 9. 架构原则

- 入口只组装；API 只处理传输；Application 负责编排；Domain 维护状态语义；GIS 隔离空间运行时；Algorithm 只计算；Persistence 只可靠读写。
- 旧导入路径可保留兼容 facade，但新代码必须使用规范路径。
- 不机械创建空 Service；按稳定职责和依赖方向拆分。
- 状态、来源、参数、算法版本和输入版本必须可审计。
- 前端业务状态与地图临时状态分离；高成本 grid_id 关联在状态变化时缓存，不在每次 paint 重算。
- 业务演进前先维护 characterization；重构不得改变 V1 输出。

## 10. 当前技术债

1. `risk/v1.py` 仍较长，Ground/Operational Air/Airspace Constraint/Overall 可在公式升级时按组件拆分；本次不为缩短文件改变公式。
2. HTTP 使用线程服务器，ApplicationContext 内的项目切换和 Workflow mutation 尚无完整的跨请求事务锁。
3. 项目包仍是 `project_state.json + data_sources.json`，尚未实现 manifest、迁移链、audit 与可选数据封装。
4. `services/` 仍保留一组旧导入路径兼容 facade；待外部脚本完成迁移后可在主版本升级中删除。
5. 默认测试跳过真实 QGIS HTTP；需在有 QGIS 与真实本机数据时执行集成套件。
6. V1 航路使用 56×56 经纬度近似网格和图层 BBOX 硬约束；CoverageV1 使用 demo/default 设备参数，均非最终工程模型。
7. RiskModelV1 为保持外部语义仍读取人口兼容字段 `value_mean`；P1 新的守恒人数/密度已独立保存并用于前端专题，后续模型版本才能显式切换到权威 quantity，不能在 V1 中暗改。
8. 数据源产品契约已确认，但当前具体 GeoTIFF 文件身份仍记录为 `configured_assumption`；尚未通过 checksum/manifest 验证其确为对应 WorldPop/GLO-30 产品。
9. M7 是二维恒速直线轨迹与局部平面 CPA，未处理垂直间隔、动力学、不确定性及正式安全阈值。
10. CSS 已按加载职责拆分，但 `base.css` 保留历史压缩规则；未来视觉改版时再格式化和去重，避免本轮改变级联结果。
11. WorldPop SourceProfile 当前使用 `quantity=population_count_per_source_pixel`、`unit=person/source_pixel` 表达官方 people-per-pixel 语义；长期应规范为 `quantity=population_count`、`unit=person`，并独立使用 `support=source_pixel` / `source_semantics=people_per_pixel` 表达空间支撑。当前阶段不得为此破坏 P1 兼容字段和人口映射结果。

12. 当前全量 pytest 存在 1 个已知基线失败：`test_qgis_adapter_transforms_crs_filters_workspace_and_uses_spatial_index`。原因是测试替身仅支持 `QgsSpatialIndex(features)`，而生产实现采用真实 QGIS 支持的空构造后 `addFeature`；后续非相关阶段不得通过修改生产空域逻辑来“修绿”该测试。
13. `OperationService` 为保持旧工作流/API 语义，仍在顶层 `aircraft` 输出 legacy `lambda_per_hour=1/mtbf_h`；Step 4 已标注其不是 P4 ReliabilitySpec 推断。新安全计算只能使用显式声明模型的分系统 ReliabilitySpec。

14. P6 只支持由离散 EventObservation 驱动的定性 C+S/C+N/N+S 功能耦合；尚无完整航路 ServiceTimeline、耦合概率、common-cause、BN/DBN/Petri、FTA 图形编辑、认证工作流或正式 safety objective 校核。

15. P8 仅提供静态技术能力工程基线：尚未实现 P.526/Fresnel/terrain diffraction、3GPP SINR/channel、GNSS constellation/DOP/RAIM、radar equation/Pd curve 或 SitePlanner。

16. P9 时间线仅支持恒定地速和显式离散场景；保护包络是代数工程基线，尚未实现 waypoint-linear motion、飞机动力学/转弯、正式 Well-Clear、DAA Detection Volume 或 Monte Carlo。

17. GapV2 是上游证据的保守区间合并，不做传播/性能/ServiceState 重算，不把 unknown 当 gap，也不触发 SafetyEvent；protection margin 仅支持已有 confirmed 监视探测距离的工程差值，尚未形成 GapV2→Safety/站址方案闭环。

18. P11 是单 action 正收益的 reuse-first 提案器，不求解多 action 联合后才能满足的独立冗余，不含风险/人口/severity 权重，也不是费用优化器。P12 可组合 apply 并重跑 P7-P10，但结果仍受 P7/P8 工程模型与输入证据完整度约束。
19. P12 没有项目级 audit log、撤销已提交 application 或多方案分支合并；事务边界依赖当前单项目单进程 repository 原子写入。真实数据验证、并发提交控制和人工审批流留待后续阶段。
20. P13 是 MH/T cell-centroid 的二维八邻域 A*；没有 Theta*/LOS smoothing、连续空间最短路、动态/四维风险或高度相关风险。硬约束仍来自现有 bbox 输入，风险仍受 RiskModelV1 相对指数和数据完整度限制。
21. P14 使用保守 cell 纳入、代表性 voxel probe 和离散 volume proxy；不是精确 buffer/mesh，也不保证 voxel 全体满足。尚未评估走廊冗余、韧性、runtime outage、真实传播或法规空间合规。
22. P15 只做静态空间 service/redundancy 缺口与显式目标判定；不建模 common-cause、共享供电/回传、塔站失效传播或概率 continuity/availability。共址关系当前既不奖励也不惩罚。

## 11. 下一阶段计划

1. P16：Corridor-aware Reuse-first CNS SitePlanner V2，只消费 P15 confirmed targets，并通过 P14/P15 closed-loop what-if 证明空间收益。
2. 设计 GapV2 到 P5/P6 Safety Event 的显式、可确认映射，仍禁止 Gap 自动等同 SafetyEvent。
3. 接入建筑/财产/基础设施真实映射，保持 `grid_attributes` 原始属性与 `grid_risk` 派生结果分离。
4. 补 ApplicationContext 并发事务、schema migrations、项目 manifest/audit、application rollback 和真实 QGIS 集成 CI/验收脚本。
