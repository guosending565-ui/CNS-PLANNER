# CNS 规划系统开发上下文

> 架构基线：2026-09-16（Asia/Shanghai）
> 当前目标是持续完善 CNS 规划工作台；结构重构不得顺带改变 V1 算法、风险公式、API 路径或项目业务结果。
>
> **Real-Data Enablement V1 决策**：现有适飞空域/AirspacePolicy/airspace_eligibility 是 `display_only_reference_layer`。DATA-3 已退役并标记 `retired/not_applicable_by_architecture_decision`；它不是 route eligibility、risk evidence 或 regulatory constraint，不参与规划、readiness、fingerprint、staleness 或 CNS。

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
      ├─ reference_data（只读来源事实；与规划输入隔离）
      ├─ algorithms / risk / simulation（纯标准输入输出）
      └─ persistence（原子 JSON I/O）
```

依赖方向为 API → Application → Domain/Algorithm；真实空间数据由 GIS 转换为标准业务输入。算法层不 import QGIS，不读取本机数据路径。`services/` 中与新边界重复的模块只保留兼容导入。

## 2. 六步业务流程

1. 项目与数据：项目创建、打开、另存和数据源设置。
2. 工作区与环境：workspace → MH/T grid → population/terrain/traffic/conflict → relative risk；airspace 仅显示。
3. 航路设计：节点、场景航路（显式 起点→终点，或兼容的 all-pairs）、默认 RoutePlannerV1 或显式选择的 Risk-Aware Route Planner V2 运行航路。
4. 运行规则：飞行器、方向、高度、间隔和监视延迟。
5. 设备与布站：C/N/S 设备参数和 CoveragePlannerV1。
6. 确认与导出：统一 ResultStatus 复核，导出项目、航路和站址。

## 3. 已实现能力

- M1：workspace 生成/裁剪 MH/T 4063 标准网格，稳定 `grid_id`，保存恢复、API、Canvas 显示和点击。
- M2–M4：人口 GeoTIFF、GLO-30 DEM、QGIS 空域按 `grid_id` 映射；处理 CRS、NoData、无覆盖和定向失效。
- M5–M6：可替换 `RiskModel`；Ground、Operational Air、Overall 相对风险，参数化权重/阈值、贡献解释和完整度；legacy `airspace_constraint` 固定为 not_applicable/display_only。
- M7：可复现多机直线轨迹、逐格驻留时间、二维 CPA 潜在冲突、traffic/conflict 风险输入。
- CNS 核心输入：AircraftCNSProfileCatalog、RequiredCNS、DeviceCatalog、ExistingCNSFacility、CandidateSite 已纳入 schema v2；已有能力与任务需求严格分离。
- CNS Gap Analysis：V1 保持 RequiredCNS、机载能力及已有设施二维水平覆盖的既有输出；V2 独立合并 P7 三维几何、P8 静态能力与 P9 显式运行时间线，输出 planning/runtime/combined 评估、连续缺口段、contingency/unknown 暴露和稳定输入指纹。
- RoutePlannerV1：固定工作区离散、硬约束 BBOX、A*、geometry/关键节点/统计/指纹；硬约束在 Application 输入边界 fail-closed 校验。
- Risk-Aware Route Planner V2：直接在 MH/T `grid_id` 邻接图上使用既有 `grid_risk` 相对工程指数执行米制 A*，保留完整 grid path、距离/风险暴露/绕行指标；Registry 默认仍为 V1。
- Route Planner V3-A（实验，未注册进 Registry）：原生 3D `grid_id + altitude_index + heading_bin` 状态、hard-constraint edge 生成、provenance soft cost 向量、L8 战略 A* 与 candidate refinement corridor；结果只进独立实验容器，见 `docs/route_planner_v3_architecture.md`。已修正三项语义：h = 纯 3D 几何距离；soft field 只接受带 provenance 的 normalized index（删除 10000/100 隐式 normalizer 与 Σweight≤1 假设）；expansion cap 返回 `search_incomplete`（resource limited）而不是 failed/infeasible。
- Route Planner V3-A/B（实验，未注册进 Registry）：V3-A 支持 canonical synthetic 与 injected `configured_real_sources`；真实 adapter 位于 GIS 边界，消费 verified FABDEM、buildings/building_grid L8 facts 与 grid_risk。V3-B 在 corridor-local fine grid 上严格检查 terrain/building/altitude/kinematics；airspace 均为 display-only/not-applicable。
- Route Planner V3-C（实验，未注册进 Registry）：active validation domains 为 geometry/terrain/building/altitude/kinematics；兼容 airspace domain 固定 skipped/not_applicable。结果只有 validated_route/failed/unresolved/not_ready/validation_incomplete，**即使 validated_route 也强制 operational_route=false、cns_assessed=false**。
- CoveragePlannerV1：C/N/S 主站、补盲、共址、未覆盖点/航段、统计/指纹。
- schema-v2 项目自动保存、打开、Save As 与数据源恢复；失败操作保留当前有效项目并清理临时文件。
- 本地 QGIS 渲染、原始人口/DEM 图层、在线瓦片、统一数据源中心、六步 ES Module 前端。
- 真实数据基线：`reference_landing_sites` 与 `flow.nodes` 分离，Step 03 仅在用户“加入项目”后创建带 provenance 的 node；`equipment_reference_catalog` 与算法 `DeviceCatalog` 分离，Step 05 只读展示来源事实，不自动进入 Coverage/Site Planner。

## 4. 重要文件职责

- `cns_planner/map_server.py`：QGIS 进程启动、ApplicationContext 组装、事件循环；旧测试/本地集成所需名字为兼容 facade。
- `cns_planner/application/app_context.py`：唯一运行上下文，持有 QGIS runtime、workflow、data、tile cache、repositories 和活动项目会话。
- `cns_planner/api/router.py`：保持 `/api/*` URL，只解析用例和响应，不依赖算法实现。
- `cns_planner/application/workflow_service.py`：六步 facade、步骤可进入性和 snapshot；具体变更委派给 Project/Workspace/Route/Operation/Risk/CNSPlanning/Export Service。
- `application/project_state.py`：schema-v2 空状态、兼容字段回填与 schema 校验。
- `domain/algorithm_manifest.py`、`algorithms/registry.py`：算法可解释元数据、精确注册/查询/实例化，包含受保护的既有 V1 与独立 3D 覆盖、静态能力、时间线和保护包络模型；factory/Python 实现路径不进入 ProjectState 或 API Manifest。
- `application/cns_input_service.py`：五类 CNS 规划输入的选择、导入、需求覆盖、保存与下游失效。
- `reference_data/landing_sites.py`、`reference_data/equipment_catalog.py`、`application/reference_data_service.py`：真实起降点 XLSX/CSV 导入、坐标质量/重复候选/来源记录、规范设备事实目录加载，以及 reference→node 显式采用边界；不实现规划算法映射。
- `application/closed_loop_service.py` + `domain/closed_loop.py`：P12 working-copy 重跑编排、确定性 PlanApplication、Before/After 比较和事务式 Preview/Apply；不实现新的覆盖、能力、时间线或 Gap 公式。
- `route_planner/risk_aware_v2.py`：P13 纯 Python GridGraph、风险证据门控和 risk-aware A*；直接消费标准网格及网格风险，不依赖 QGIS、不重算 RiskModel。
- `route_planner_v3/*`：V3-A/V3-B/V3-C 独立 3D 规划包（V3-A: contracts/motion/hard_constraints/cost/planner/corridor/readiness/synthetic；V3-B: fine_contracts/fine_grid/fine_search/fine_synthetic；V3-C: continuous_contracts/continuous_geometry/continuous_validators/continuous_validation/continuous_raster_window/continuous_synthetic）；不 import QGIS/GDAL、不读文件、不写 `operational_routes`，只消费 canonical `V3CellEnvironment` / `FineCellEnvironment` / V3-C domain evidence；未注册进 Registry。soft cost 的每个 channel 都是带 provenance 的 normalized index `[0,1]`（`exposure_m = length_m × (index_source+index_target)/2`），planner 内**没有**隐式 normalizer，启发函数是纯 3D 几何距离；达到 `max_expanded_states` 返回 `search_incomplete`。V3-C 只产出 `validated_route/failed/unresolved/not_ready/validation_incomplete`，且 `validated_route` 也强制 `operational_route=false` / `cns_assessed=false`。
- `domain/v3_operational_adoption.py`：V3-D 契约（`V3OperationalProjection` / `V3OperationalAdoption` / `V3OperationalAdoptionPreview` / `V3CNSAssessmentBundle`）与 normalizer/fingerprint。V3-C validation 历史 immutable；正式状态只在 adoption/bundle。**EGM2008 正高绝不写入 GeoJSON 第三坐标**。
- `application/v3_operational_adoption_service.py`：V3-D 唯一写入者。publish gate（validated_route + current + fingerprints + route_id 对应 scenario route + real source chain ready + CRS transform 可用；production 禁止 synthetic；Apply 需 `confirmed=true` + `expected_validation_fingerprint` 防 TOCTOU；batch atomic；事务式 deepcopy 后就地安装）；V3→legacy 二维 `[lon,lat]` 投影（不 simplify、不复制 analytic geometry）；自动生成且 locked 的 EGM2008 derived profile（含 `v3_metric_length_m`/`legacy_geodesic_length_m`/`length_delta_m`/`distance_basis`，path 与 profile 同顶点顺序同距离基准）；`v3_operational_route_published` 专用失效传播（只 stale downstream，不 stale 刚发布的 route，不反向失效 Risk/grid/V3）；`stale_for_sources` 只 stale V3 adoptees（不误伤 V1/V2）；Revoke 仅在 ownership 仍属于该 adoption 时移除 route/profile；CNS bridge 只编排既有 P7→P8→P9→P10（不复制公式），prerequisites 缺失 ⇒ incomplete + blocking reason。
- `gis/fine_environment_adapter.py`：V3-B/V3-C 的 GIS/GDAL 边界。V3-B：FABDEM 单次只读窗口取相交有效像元最大值 + GPKG provider RTree 建筑查询 + confirmed AirspacePolicy **fine-cell polygon** 判定；水平分辨率只能来自显式配置或 DTM 有效分辨率（投影 CRS 按自身 verified linear unit 换算，geographic CRS 用 `Geod` 测相邻像元中心地面距离，**禁止 degree-as-meter**），禁止写死 30 m。V3-C：`NativeTerrainWindowSource`（native 像元窗口，NoData 不填补）、`RouteCorridorBuildingSource`（route bbox + clearance 的 RTree 查询）、`ConfirmedAirspacePolicySource`（confirmed polygon → 米制）。数据未配置/未确认时显式 blocked/unresolved，不构造假环境；算法包不依赖它。
- `application/route_planner_v3_service.py`：V3-A/V3-B/V3-C 实验编排与独立容器（`route_planner_v3_experiments` 记录下的 `refinements[]`/`validations[]`、`v3_planning_policy`、`v3_fine_refinement_policy`、`v3_continuous_validation_policy`）、readiness 报告、refinement/validation staleness 与 `record_summary` 有界摘要；真实源 readiness 由 ApplicationContext 注入（只报告、不读数据）。
- `domain/building_clearance.py`：唯一 roof/垂直余量语义（`building_roof_elevation` = ground + height、`evaluate_vertical_clearance` = `minimum_z − (roof + vertical_clearance)`）；`BuildingClearanceV1` 与 V3-C `BuildingPolygonValidator` 共用，不出现第三套 roof 公式。
- `docs/route_planner_v3_architecture.md`：V3 目标架构与 V3-A/V3-B/V3-C/V3-D 边界；V3-D 见 §7B；路线图为 V3-A 战略 → V3-B corridor-local 精化 → V3-C 连续几何实现 + 源几何硬约束验证 → V3-D validated route → operational adoption → CNS Assessment，clothoid 与 Route–CNS 联合优化列为未来项。
- `domain/cns_corridor.py`、`algorithms/corridor/v1.py`、`application/corridor_service.py`：P14 route corridor 契约、纯 Python 水平/垂向离散、P7/P8 代表点复用及持久化用例。
- `domain/cns_planning_objectives.py`、`algorithms/corridor_gap/v1.py`、`application/corridor_gap_service.py`：P15 显式空间规划目标、P8 独立冗余证据复用、corridor voxel 分类和空间连续缺口投影。
- `domain/corridor_site_planning.py`、`site_planner/corridor_reuse_first_v2.py`、`application/corridor_site_planning_service.py`：P16 confirmed voxel target/action 契约、确定性 reuse-first 排序与 Application 累计 P14→P15 what-if 编排。
- `domain/requirement_policy.py`、`algorithms/requirements/*`、`application/requirement_recommendation_service.py`：P17 运行上下文/显式 Policy 契约、RequiredCNS recommendation 与显式 Adopt 编排。
- `catalogs/*`：JSON 飞行器能力与设备目录；`gis/cns_input_adapter.py`：JSON/CSV/Point GeoJSON 设施、站址标准化。
- `application/invalidation_service.py`：工作流、映射属性和风险失效的唯一权威实现。
- `application/constraint_validation.py`：硬约束输入的 fail-closed 校验（dict + 4 项有限 bbox + west<east/south<north），在 planner 之前拒绝畸形输入；不是 planner 的一部分，也不重解释几何。
- `cns_planner/benchmark/`：`fixtures.py` 确定性 synthetic 算例、`quality.py` 独立 RouteQualityEvaluator（只读 planner 输出，不重算风险、不排名）；不得被 planner 反向依赖。
- `tools/route_planning_baseline.py`：专家评审证据包生成器（JSON + Markdown，写入被忽略的 `outputs/`），只报告不评分。
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
reference_landing_sites（来源事实；CRS pending；选择后才创建 node）
equipment_reference_catalog（来源事实；不自动映射 DeviceCatalog）
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
corridor_site_planning_policy / cns_corridor_site_plan（P16 proposal-only 走廊感知 reuse-first 站址提案）
cns_operation_context / cns_requirement_policies（P17 项目默认+航路覆盖运行上下文与可追溯显式规则）
required_cns_recommendation / required_cns_adoption（P17 proposal 与显式采用 provenance；不替代正式 required_cns）
v3_planning_policy（V3-A 显式规划/安全参数；无默认安全值，未确认即 pending）
v3_fine_refinement_policy（V3-B 显式 fine 配置：local metric CRS、resolution_source、resolution_m、max_stride_cells；无默认分辨率）
v3_continuous_validation_policy（V3-C 显式验证配置：curve_chord_error_m **无默认值**、max_validation_samples、max_runtime_s、use_curve_error_envelope、fail_closed_on_unknown）
route_planner_v3_experiments（V3-A/V3-B/V3-C 独立实验容器：记录/环境 spec/policy/result/verdicts + refinements[] + 每条 refinement 下的 validations[]；不是 operational route）
v3_operational_adoptions（V3-D 正式采用记录：adoption_id/route_id/validation ids+fingerprints/refinement fingerprint/projection fingerprint/applied_at/current_applicability/before-after/provenance/CNS link；`published`/`stale`/`revoked`）
v3_cns_assessment_bundle（V3-D CNS 评估结果：assessment_status=not_started|incomplete|complete|stale、requirement_verdict=meets|does_not_meet|unknown、stage_results{P7,P8,P9,P10}、fingerprints、route/adoption/validation ids）
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
- current P14 corridor、RequiredCNS 或 `cns_planning_objectives` 变化只向下使 `cns_corridor_gap_assessment` stale；P15 不反向影响 Route/P7-P14/P10-P12。P15.1 已明确排除 P8 `stage=provider_type_compatibility` 中间门控，只有真正 provider service evaluation 才能计入 qualified provider。
- current P14/P15、RequiredCNS、ExistingCNS、CandidateSite、DeviceCatalog、P16 policy 或相关 P14/P15/site-planner 有效参数变化会使 `cns_corridor_site_plan` stale。P16 proposal 不反向使 P7-P15/P10-P12 stale；P12 Apply 通过 ExistingCNS→P14→P15→P16 单向传递。
- operation context、requirement policy、requirement-model 选择/参数或 route 集合变化只使 `required_cns_recommendation` stale。Evaluate 不改变正式 RequiredCNS 或 P8-P16；手工 RequiredCNS 编辑只显示 recommendation diverged。只有无 conflict 且 current 的显式 Adopt 才通过既有 `required_cns` 失效链一次性更新下游。
- V3-A/V3-B/V3-C 写入 `route_planner_v3_experiments` / `v3_planning_policy` / `v3_fine_refinement_policy` / `v3_continuous_validation_policy` **不产生任何失效传播**：不使 routes、grid、grid_risk 或 P7-P19 结果 stale；V3 自身也不读取或改写 `operational_routes`、`algorithm_selection`、`spatial_3d`。
- V3-D Apply **不使用** `workflow("route")`（那会立即 stale 刚发布的 route）。专用 `v3_operational_route_published` 只 stale downstream（`coverage_3d → cns_service_capability → service_timeline → cns_gap_v2` 以及依赖 operational route 的 corridor/site/report/building/route-profile），保持新 route 与 current V3 validation 不变；Risk/grid/V3-A/B/C 不反向失效。`terrain_dtm`/`buildings`/`AirspacePolicy`/V3 policy/source audit/validation fingerprint 变化只把相关 V3 adoption + adopted route 标 stale 并向下 stale CNS，**不误伤 V1/V2 route**（`SourceAuditService` 通过注入的 `v3_adoption_invalidator` 调用）。
- V3-B refinement 与 V3-C/D validation/adoption 的 stale 都是**自身证据链**判定，不进入既有失效传播：refinement 依赖 strategic candidate / corridor / policy / source audits / local frame / fine grid；validation 依赖 refinement fingerprint / continuous policy / curve tolerance / source audits / CRS-transform / validator versions；adoption 依赖 validation fingerprints / refinement fingerprint / source chain / adopted route 状态。任一变化 ⇒ stale，必须重跑。
- 不支持 schema、损坏 JSON、数据源加载失败不会替换当前项目；Save As 失败不切换 active project。

## 6. 算法外部契约

- RoutePlannerV1 与 CoveragePlannerV1 保留公开输入输出、`status`、`algorithm_name/version`、`input_fingerprint`、geometry/站址/统计和固定输入确定性。
- RiskModel 接口固定为 `evaluate(grid, grid_attributes, parameters) -> risk_result`。RiskModelV1 输出为 `relative_index`，不是事故或碰撞概率。
- P2 Algorithm Registry 使用精确 `(algorithm_type, algorithm_id, version)` 键，当前注册：`risk-model-v1-relative-index@1.1`、`route_planner_v1@1.0`、`risk_aware_route_planner_v2@2.0`、`coverage_planner_v1@1.0`、`cns_gap_analysis_v1@1.0`、`cns_gap_analysis_v2@2.0`、`coverage_model/geometric_coverage_3d_v1@1.0`、`service_model/cns_service_capability_v1@1.0`、`timeline_model/route_service_timeline_v1@1.0`、`protection_model/tactical_protection_envelope_v1@1.0`、`site_planner/reuse_first_site_planner_v1@1.0`、`site_planner/corridor_reuse_first_site_planner_v2@2.0`、`corridor_model/cns_service_corridor_v1@1.0`、`corridor_gap_analyzer/cns_corridor_gap_v1@1.0`。`route_planner`、`cns_gap_analyzer` 与 `site_planner` 默认仍选择各自 V1；找不到精确版本直接报错，禁止回退。
- `requirement_model` 注册 `manual_required_cns_v1@1.0` 与 `operational_context_required_cns_v2@2.0`，默认始终为 manual V1；旧项目不自动迁移、也不自动采用 recommendation。
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

P16 Corridor-aware Reuse-first CNS Site Planner V2：

- `site_planner/corridor_reuse_first_site_planner_v2@2.0` 已注册，但 `site_planner` 默认选择继续为 P11 V1。ProjectState additive 保存独立 `corridor_site_planning_policy` 与 `cns_corridor_site_plan`，不覆盖 P11/P12。
- 自动目标严格来自 current P15 confirmed target voxel；unknown/objective unknown 与非站基导航不触发地面建站。每项保存 required/current/remaining provider requirement unit 和 `discretized_volume_proxy_m3`。
- Application 在 deep-copied ExistingCNS 上累计应用已选 action，并对每个剩余候选重新运行当前 P14→P15；Planner 只做确定性 tier 内排序和结果组装。最终全部 selected actions 再执行一次 combined P14→P15，作为权威 proposal evidence；正式 ExistingCNS/P14/P15 不写回。
- 核心收益为 `sum(discretized_volume_proxy_m3 * unit_gain)`，语义固定为 `confirmed_requirement_unit_volume_gain` planning proxy，不是风险或概率。k>1 只使用 P15 confirmed independent provider count；相同 independence group 不重复增加 unit。
- reuse tier 固定为 existing CNS facility → existing shared site → candidate site → new-build candidate。仅同一已确认 cost unit 内比较收益/成本；其他情况使用 action-count proxy，不生成货币成本。confirmed objectives 全满足可提前停止；未配置目标则运行到没有正 confirmed marginal gain。
- Proposal 固定 `proposal_only=true/requires_user_confirmation_and_apply=true`，并保存 target、candidate impact、iteration trace、before/after service/redundancy/continuous-deficit/objectives、残余与 unknown evidence。API 为 `GET /api/cns-corridor-site-plan` 与 `POST /api/cns-corridor-site-plan/evaluate`；Step 5/6 显示并将 proposed 点与 Existing/Candidate 区分。
- 明确不评估 common-cause/shared power/shared backhaul/tower/site failure propagation；共址不自动惩罚或证明独立性。P16 完成后里程碑为 **M2: 3D CNS Spatial Infrastructure Planning Baseline**。

P16.1 Proposal 状态语义：

- baseline 无 confirmed target 且无 unknown evidence，或未选动作时所有 confirmed objective 已满足：`status=no_action_required`、`selected_actions=[]`、before=after、`result_status=passed`。
- 有 confirmed target 但没有 eligible positive-gain action：`no_eligible_proposal/failed`。只有 unknown/evidence 缺失：`evidence_required/pending_confirmation`；不得伪装 success 或 missing_data。

P17 Operational Context → RequiredCNS Recommendation：

- `cns_operation_context` 按项目默认+route override 保存 operation mode、airspace、UAS 数量、交通混合和有人机密度；每个字段独立保存 value/source/confirmed/status，未确认上下文不参加 confirmed rule match。
- `cns_requirement_policies` 默认为空，不含 ICAO/EASA/JARUS 或工程默认数值。规则只支持白名单字段及 `all_of + eq/in/contains`，禁止 eval；每项保存 source type、reference/clause、确认状态及现有 P3 RequiredCNS partial canonical contract。
- V2 仅合并 confirmed matched policy。不同 leaf 可合并；同 leaf 同值合并多来源 provenance；不同值产生 conflict，禁止“更严格优先”或法规等级推断。missing/unconfirmed applicability 保持 pending，不输出需求。
- Recommendation 保存 context snapshot、matched/not-matched/unknown rules、逐字段 provenance、conflict/missing evidence、current-vs-recommended diff、route 结果和确定性 fingerprint；固定 `proposal_only/requires_user_adoption`。
- Recommendation 不读取 Aircraft capability、ExistingCNS、P7-P16 或 runtime 来放宽需求。显式 Adopt 前校验 recommendation/context/policy/route/current fingerprints；conflict、pending 或 stale/diverged 均拒绝。采用后保留 algorithm/context/policy/field provenance，并复用唯一 RequiredCNS normalization/invalidation。
- API：`GET/POST /api/cns-operation-context`、`GET/POST /api/cns-requirement-policies`、`GET /api/cns-required-recommendation`、`POST /api/cns-required-recommendation/evaluate|adopt`。Step 1 展示 requirement model；Step 4 展示 Context/Policy/Matched Rules/diff/provenance/Adopt；Step 6 汇总 requirement basis。
- 正式语义：**operational-context-driven requirement recommendation, not automatic regulatory compliance**。

P18 CNS Plan Review, Variant Comparison & Controlled Apply V1：

- P18 是 Application 决策/配置管理层，不注册新算法；ProjectState additive 保存 `cns_plan_review` 与不可原地修改的 `confirmed_cns_plan` 历史快照。正式语义为 **human-reviewed plan decision and controlled application, not automatic optimal-plan selection**。
- Initialize 固定当前 RequiredCNS、routes、Existing/Candidate/Device、P14/P15/P16 与相关算法输入 fingerprint，生成 0-action baseline 及当前 P16 auto proposal。`variant_id` 只由 review baseline fingerprint 与排序后的 action IDs 决定，名称/notes 不改变 identity。
- User variant 只能 include/exclude 当前 P16 CandidateAction；评价在 deep copy ExistingCNS 上一次应用完整 action set，并复用当前 P14→P15 生成 authoritative hypothetical evidence。正式设施和 P14/P15 零污染，不累加 P16 历史 impact。
- Comparison Matrix 仅展示 route/C/N/S 的 service/redundancy/unknown volume proxy、continuous-deficit、objectives、reuse counts 与按 cost unit 分组的显式费用；`automatic_overall_score/rank=null`，不同费用单位不合计。
- Confirm 门禁要求 confirmed objectives 全满足、无 confirmed regression、无关键 unknown；未配置 objectives 只允许显式 `confirm_without_objectives` acknowledgement。Select 不改设施，Confirm 只冻结 decision/variant/actions/Before-After/Requirement basis/provenance，Confirm 不等于 Apply。
- Apply 重校验 review baseline/action fingerprints，在事务 working copy 中复用 P7→P10 和 P14→P15。P14/P15 必须与 confirmed Preview fingerprint 一致，P10 新 confirmed regression 或 satisfied→unknown 会回滚。成功只保存一次并以 planning origin 幂等安装；0-action baseline 可 no-op applied。
- API：`GET /api/cns-plan-review`，以及 `POST /api/cns-plan-review/initialize|variant|evaluate|select|confirm|apply`。Step 6 提供 Variant cards、无隐藏评分的比较矩阵与 Select→Confirm→Apply；地图切换仅显示 proposed overlay。

P19 CNS Planning Report, Audit & Export V1：

- P19 是只读报告与交付层，不注册算法、不重算 P1-P18。schema-v2 additive 保存 `cns_planning_reports={status,active_report_id,records[]}`；历史记录和产物只追加，active report 在源输入变化后标记 `stale_current_project`，仍可下载审计。
- 纯 `ReportBuilder` 从冻结、清洗后的 ProjectState 快照生成唯一 canonical `ReportDataModel`。HTML、PDF 与 ZIP 均消费同一模型；正式报告要求 current `confirmed_cns_plan` 为 `confirmed|applied`，无确认方案只能零持久化 draft preview。
- `ReportRecord` 保存确定性 `report_id`、schema/template version、source plan/status/fingerprint、ReportDataModel fingerprint、生成时间、current applicability 与白名单相对 artifact paths。相同 plan/source/template 重复生成幂等返回既有记录，任何阶段失败均不写 final record 或半成品目录。
- 中文 standalone HTML 内嵌 CSS、A4 print CSS 与纯 SVG 航路/设施/C/N/S 统计示意，不依赖 CDN、在线底图、QGIS 截图或第三方 chart。PDF 只允许从 exact HTML 使用可注入 renderer 生成；生产默认 Playwright Chromium，首次需执行 `python -m playwright install chromium`，不可用时显式返回 `pdf_renderer_unavailable`。
- 数据包固定包含 `report.html/report.pdf/report.json/routes.geojson/facilities.geojson/algorithms.json/provenance.json/manifest-sha256.txt`；manifest 校验各 payload SHA-256，语义仅为 checksum manifest，不宣称 BagIt。provenance 记录 P17→P14→P15→P16→P18→P19，明确 `w3c_prov_inspired_not_full_prov_compliance`。
- sanitizer 永久遮盖 token/password/secret/api_key/authorization，并将本机绝对路径降为 basename；所有用户文本 HTML escape。final facilities 来自 ExistingCNS 与 P18 confirmed/applied plan，禁止读取 legacy CoverageV1 stations。
- API：`GET /api/cns-planning-report`、`POST /api/cns-planning-report/preview|generate`、`GET /api/cns-planning-report/artifact?report_id=&kind=`。artifact 只允许 ReportRecord 中 html/pdf/package/json 白名单相对路径，并拒绝绝对路径和 traversal。
- UI 原则固定为：**内部 schema/API/algorithm/status 英文契约稳定，面向普通用户中文优先**。集中维护状态与来源类型映射；unknown 显示“证据不足/尚无法判断”，real/synthetic/manual 显示“真实数据/模拟数据/人工录入”，技术 ID 保留作为次级审计信息。

## 7. 数据源扩展

统一定义至少包含 `id/name/category/type/formats/required/health/coverage/source_metadata`，并新增 `source_mode/source_type = real | synthetic | manual`。需要进入计算的数据源通过轻量 `SourceProfile` 保存 `source_id/name/version/quantity/unit/resolution/crs/verification/provenance`；数值边界可使用 `QuantityValue(value/quantity/unit/source_unit/conversion/source/confirmed/status)`，不依赖大型单位或 PROV 库。

P1 数据语义契约：

- WorldPop R2025A（alpha）源量固定为 `population_count_per_source_pixel`、`person/source_pixel`、WGS84/EPSG:4326、约 3 arc-second。目标 MH/T 网格输出 `population_count_people` 与 `population_density_people_km2`，密度分母为该格实际球面面积。
- count 重映射方法为 `area_weighted_source_pixel_overlap`：按源像元/目标格交叠面积分配并守恒求和，不使用 bilinear 人数；保存 mapping method、假设、源分辨率和覆盖率。部分覆盖或 NoData 的 `quantity_status=missing_data`，不得作为 0 或 passed。
- Copernicus GLO-30 固定为 DSM、`surface_elevation`、m、水平 WGS84-G1150/EPSG:4326、垂直 EGM2008/EPSG:3855、1 arc-second。
- schema v2 以 additive/backfill 方式新增 `data_source_profiles`。人口旧字段 `value_sum/value_mean/value_min/value_max/value_unit/unit_status/interpretation`、DEM 旧字段 `mean_elevation/min_elevation/max_elevation/elevation_unit` 暂时保留，保障 RiskModelV1 和旧项目外部语义；权威新字段与旧字段不得混称。

当前 Registry 已覆盖 basemap、airspace、population、terrain、buildings、property exposure、obstacles、infrastructure、towers、traffic、existing CNS、candidate sites 等。

真实参考数据基线：

- `reference_landing_sites` 从本地配置指向的 XLSX/CSV 导入，保存原始单元格、标准 `[lon, lat]` 数值、`parsed/estimated/uncertain/invalid`、warnings、source(file/sheet/row)、稳定 ID 与疑似重复候选。舟山源表未声明 CRS，因此集合和每条记录固定 `crs_status=pending_confirmation`；`.et` 只返回 `requires_xlsx_or_csv_conversion`，不维护专用解析器。
- Step 03 只展示工作区内参考点并提供搜索/区域/类型筛选；只有用户点击“加入项目”才调用现有 RouteService 创建 node，并保留 `reference_site_id/provenance`。参考集合不会批量进入 nodes，旧手工地图加点结构不变。
- `equipment_reference_catalog` 是一次性整理的规范 JSON 来源事实模型，允许字段缺失，保存 source/evidence/conditions/reliability 与 `planning_mapping.status`。当前目录与 `DeviceCatalog` 严格分离，所有 34 条记录均为 `not_mapped`；不得根据缺失信息补造规划 radius、MTBF、MTTR、cost 或 capacity。
- A/B 本机绝对路径只允许保存在本地/项目 `data_sources` 配置，Python 模块、规范 JSON 与测试不得硬编码。设备运行时不解析 DOCX/PDF/XLSX。

新增来源原则上仅增加：Registry 定义 + GIS Adapter + Mapping Service；不得在 MapData.metadata、Workflow 或 HTTP handler 中补丁式拼接。

## 8. 测试基线

环境：Python 3.13.9，pytest 8.4.2。命令：

```powershell
python -m pytest -q -rs
node --test tests/grid_theme.test.js tests/frontend_modules.test.mjs
node --check cns_planner/web/app.js
node --check cns_planner/web/js/main.js
```

> 本机 DSH 沙箱会对 pytest `--basetemp` 子树施加拒绝 ACL，导致大量伪 setup error 与退出崩溃；在受限沙箱内运行全量测试时请显式给出可写 basetemp，例如
> `python -m pytest -q -rs -p no:cacheprovider --basetemp=outputs/pytest_tmp`。
>
> 2026-09 复测结论（更精确）：真正的原因是**沙箱拒绝写入以 mode `0o700` 创建的目录**——pytest 的 basetemp 与每个 per-test 目录、以及 `tempfile.mkdtemp`
> 都用 `0o700`；同时系统临时目录（`%TEMP%`）位于 workspace 之外也不可写。因此仅给 `--basetemp` 仍会失败（`Path.mkdir` 的 0o700 ACL），且
> `tempfile.mkdtemp` 会在 `%TEMP%` 下失败。受限沙箱内的完整可用形式是：
> `$env:PYTHONPATH='outputs'; $env:TMP=$env:TEMP=$env:TMPDIR='<workspace>\outputs\sys_tmp'; python -m pytest -q -rs -p no:cacheprovider -p dsh_tmp_plugin --basetemp=outputs/pytest_tmp`
> 其中 `outputs/dsh_tmp_plugin.py` 是 gitignored 的沙箱兼容 shim，只把 `0o700` 改成宽松 mode（不改测试语义与产品行为）。
> Node 测试同理：`node --test <file>` 在沙箱内会因 piped stdio EPERM 失败，改用 `node <file>`（`node:test` 在进程内运行）。

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

P16 完整基线：**316 passed, 6 skipped, 1 known failed**；P11/P12/P14/P15/P16/Registry 定向回归 **82 passed**；P15.1 门禁回归 **37 passed**；Node **14 passed, 0 failed**，`main.js/step05_cns.js/step06_review.js` 语法检查通过。新增覆盖 P8 type compatibility 门控不计 qualified provider、单服务缺口、k=2 累计联合冗余、相同 independence group 不重复收益、unknown/非站基 N target 隔离、reuse tier、objective stop/no-objective stop、不同 cost unit proxy、regression/unknown 门控、最终 combined what-if、proposal 零污染、Registry 默认 V1/V2 精确绑定、API/schema-v2 backfill/保存恢复与单向失效。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P16 未修改生产空域代码。

P16.1 前置门禁：**51 passed**。P17 完整基线：**334 passed, 6 skipped, 1 known failed**；P17/P16/P14/P15/P8/Registry/RequiredCNS 定向回归 **130 passed**；Node **15 passed, 0 failed**，`app.js/main.js/step01_project.js/step04_operation.js/step06_review.js` 语法检查通过。新增覆盖 manual V1 默认/backfill、confirmed exact match、route override、纯 synthetic policy 的 VLOS/BVLOS 与 single/multi 差异、missing/unconfirmed context、空/未确认 policy、字段 merge/同值 provenance/conflict、不完整 recommendation 门控、零污染、外部能力隔离、显式 Adopt/既有失效链、divergence/stale、确定性 fingerprint/diff、Registry/API/schema-v2 保存恢复及 UI。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P17 未修改生产空域代码。

P18 完整基线：**342 passed, 6 skipped, 1 known failed**；P18/P12/P14-P17 定向回归 **72 passed**；Node 前端 **16 passed, 0 failed**，`app.js/main.js/step06_review.js` 语法检查通过。新增覆盖 baseline+auto 初始化、action-set 确定性 identity、P14→P15 authoritative preview 零污染、无隐藏 score/rank、confirmed objectives 与无目标显式 acknowledgement 门禁、no-action baseline、confirmed immutable/clone、Select/Confirm/Apply 分层、输入 stale 拒绝、异常 rollback、成功单次 commit、P14/P15 current、P16/P11/P12/report 定向 stale、幂等 Apply、schema-v2 backfill、API 与 Step 6。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P18 未修改生产空域代码。

P19 完整基线：**351 passed, 6 skipped, 1 known failed**；P19/P18/ProjectRepository/Persistence 定向回归 **32 passed**；Node 前端 **17 passed, 0 failed**，`app.js/main.js/source_center.js/step06_review.js` 语法检查及 `git diff --check` 通过。新增覆盖 draft/final 门禁、confirmed/applied 标签、canonical 模型章节与 unknown 语义、确定性 ID/幂等、schema-v2 backfill/保存恢复、HTML escape、secret/path redaction、standalone HTML/inline SVG、HTML/PDF 同源、FakePDF 成功/失败回滚、final facility 隔离、ZIP 完整性与 SHA-256、provenance、历史报告 stale 保留、artifact traversal、API 与中文状态/来源映射。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题；P19 未修改生产空域代码。

真实数据基线接入完整结果：**357 passed, 6 skipped, 1 known failed**；reference-data 定向 **6 passed**；Node 前端 **18 passed, 0 failed**，新增 Step 03/05/main 语法检查通过。测试覆盖 XLSX/DMS/estimated/uncertain/invalid、CRS pending、稳定 ID、疑似重复不合并、ET 拒绝、reference→node 显式采用、旧手工 node 结构、设备缺字段和 DeviceCatalog 隔离。唯一失败仍为既有 QgsSpatialIndex 测试替身签名问题，本轮未修改空域生产代码。

真实航路/适飞空域硬约束阶段完整结果：**371 passed, 6 skipped**；Node 前端 **20 passed, 0 failed**；Python compile、`app.js/main.js/step03_routes.js` 及新增地图模块语法检查、`git diff --check` 通过。既有 `QgsSpatialIndex` 测试替身签名已用兼容构造处理，不改变真实 QGIS 空间索引语义。新增 `reference_routes`/独立 route points（CSV/XLSX/GeoJSON；按 route_number 分组、sequence 排序、完整保留中间点、稳定 ID、reference_only），并与 nodes/scenario/operational 严格隔离。当前本机目录没有“舟山16条航线点位核对表.csv”等已转换航线文件，仅有 `.et`，因此真实航线/航路点导入数为 **0/0**，状态明确为 `requires_xlsx_or_csv_conversion`。

Step 03 新增真实参考航线、真实航路点、参考起降点三个独立开关和来源事实选中面板；reference/scenario/operational 使用不同样式，并提供参考/运行长度并列对比、无自动评分。原 QGIS 图层改标为“空域源图层（非政策结论）”，`confirmed allowed` 适飞几何独立显示，避免按颜色/图层名猜测政策；真实参考坐标继续明确 `pending_confirmation`，不暗示已确认 WGS84。

空域模型新增 AirspaceFeature 与 AirspacePolicy。只有 `confirmed=true && route_eligibility=allowed` 的 Polygon/MultiPolygon 进入 JSON-safe `airspace_eligibility`；V2 无 confirmed allowed 时为 missing_data，端点不在允许区、允许区不连通或无完整合法路径时为 failed。网格仅作搜索索引：cell 必须完整覆盖，中心边、对角 guard 与真实端点连接均受完整几何覆盖约束；hard constraints 优先，risk 只在 allowed graph 内优化。geometry/policy fingerprint 进入运行航路 provenance，变化会使 operational routes 及下游结果 stale；旧 schema-v2 项目自动 backfill。当前自动恢复项目尚无工作区、无已确认 AirspacePolicy，项目状态计数为 allowed/blocked/unknown = **0/0/0**，不能生成 V2 运行航路。

FABDEM/GBA 建筑阶段完整结果：**389 passed, 6 skipped**；Node 前端 **21 passed, 0 failed**；Python compile 与全部新增/修改 JS 语法检查通过。真实源内容已核验：FABDEM V1.2 DTM 为 7200×10800、Float32、NoData=-9999、EPSG:4326、1 arc-second，文件元数据明确 `EGM2008_orthometric`；GBA buildings 为 538,228 个 MULTIPOLYGON、EPSG:4326、带 RTree，字段包含 `id/source/height_m/height_var/height_status`；L8 building grid 为 143,013 个 POLYGON、EPSG:4326，聚合字段与本阶段契约一致。真实 L8 小工作区直接映射验证为 **324/324** 格覆盖。

建筑环境链为 `building_grid → BuildingGridService → grid_attributes.buildings → density/P95/max themes → optional RiskModelV1 building_exposure`。只允许 L8 通过 cell bounds 精确直映，不读取预处理 `grid_key` 作为正式编码；非 L8 显式 unsupported，不做高度平均/插值；范围外为 missing_data，范围内无记录才是 0。RiskModelV1 的 `building_additive_multiplier=0` 与建筑 completeness weight=0 保持不变。

三维建筑净空链为 `FABDEM footprint-mask median ground + GBA height_m → LoD1 prism → indexed route corridor candidates → polygon buffer/route continuous intersection → canonical EGM2008 vertical comparison → breach/safe/unknown intervals`。`GLO-30 DSM` 继续用于原有地形链，明确禁止用于屋顶公式；`height_var` 只保留为原始不确定性字段。新 `building_clearance_policy`/`building_clearance_assessment` 与 CNS Safety Event、grid risk 独立，未确认参数或垂向/高度/DTM 未解析时永不输出 confirmed safe。运行查询使用 provider spatial index，状态只保存候选/关键建筑及 breach 证据，不序列化全量单体。

数据源 `terrain_dtm/buildings/building_grid` 已进入默认/项目路径、保存恢复、统一设置、浏览器、registry/profile/health 与 mtime 失效；workspace/grid、三类数据源、运行航路、spatial_3d/高度剖面、policy 变化均定向使建筑结果 stale。Step 02 提供 L8 显式选择、三类专题与状态；Step 03 提供参数来源/confirmed、执行、汇总、critical buildings 和地图 breach 图层；P19 新增“建筑环境与建筑净空安全”章节与限制声明。当前命令行环境的 QGIS PyQt DLL 仍无法装载，因此真实 QGIS 几何/GUI HTTP 集成需在正常 QGIS 启动器进程手工验收；GDAL 与真实 GeoPackage/DTM 内容、RTree 及 L8 映射已在本机验证。

空间数据可视化闭环与人口语义修正结果：**395 passed, 6 skipped**；Node 前端 **24 passed, 0 failed**；Python compile、相关 JS syntax 与作用域 `git diff --check` 通过。reference overlay 绘制已与 workflow step 解耦：三个全局开关在所有步骤生效，只有 Step 03 保留点击详情、筛选与加入项目交互；侧栏和 Data Source Center 显示航线/航路点/起降点 count、status 与 0 条原因。本机当前项目实际为起降点 **98 / passed**、参考航线/航路点 **0/0 / requires_xlsx_or_csv_conversion**，源目录没有已转换航线文件。

WorldPop 原始语义保持 `people_per_pixel` / `person/source_pixel`，映射仍为 source-pixel 与目标格的面积权重 overlap、无插值。新人口结果将 `value_status` 与 `coverage_status=full/partial/nodata_only/outside_extent` 分离，保存有效覆盖面积、覆盖比例、源像元数与 quality flags；partial 保留实际 count，以有效覆盖面积计算 canonical density，不向未覆盖区域外推。RiskModelV1 优先使用 `population_density_people_km2` 并用 coverage fraction 降低 completeness，`value_mean` 仅为旧项目 fallback；building multiplier 仍为 0。真实 WorldPop L8 只读冒烟框 `[122.0,29.9,122.02,29.92]` 共 324 格：full/partial/missing/outside = **4/19/301/0**，其中 nodata-only 301。

航路三维安全剖面与 Protection Budget V1 结果：**402 passed, 6 skipped**；Node 前端 **23 passed, 0 failed**；Python compile、全部 JS syntax 与 `git diff --check` 通过。新增只读 `route_vertical_profiles`：仅对 passed operational route 使用 confirmed altitude profile，并直接从带 `EGM2008_orthometric` 元数据确认的 FABDEM DTM 按自适应间距只读采样；默认最多 500 点，保存首尾点、实际间距、ground/flight EGM2008、AGL 与 ground clearance。样本只服务 SVG 展示，建筑区间、breach 与 critical buildings 原样引用 `BuildingClearanceV1` 的精确结论，不从采样重判安全。

route/path、高度剖面、FABDEM 路径/mtime/vertical metadata、building assessment/policy 均进入 fingerprint/provenance 与定向 stale 链；旧 schema-v2 自动 backfill。Step 03 提供航路下拉、FABDEM/flight/roof+required-clearance/breach 纵剖面、hover 数值与二维位置标记，并明确 visualization-only。Step 04 将六个 response-time 分量与 `t_pre/d_reaction/maneuver_distance/uncertainty_distance/d_protect` 做成只读预算图，明确 `engineering protection budget / regulatory well-clear not evaluated`；未增加任何航路 buffer、保护圆或 ConflictDetector 状态机。当前自动恢复项目没有 operational route/confirmed altitude profile，故实际 profile 为 **not_calculated / 0 samples**；Protection 为 **not_calculated**。WorldPop canonical density 另补 `density_support_area_m2` 与 `density_semantics=observed_covered_area_density`，未改变既有数值。

3D Encounter + DAA 告警/响应状态机 V1 完整结果：**410 passed, 6 skipped**；Node 前端 **24 passed, 0 failed**；Python compile、全部 JS syntax 与 `git diff --check` 通过。新增独立 `EncounterTrack`、`EncounterPolicy`、`ManeuverCapabilityProfile`、`ManeuverCommand` 与 `encounter_3d_assessment`，保留原二维 `ConflictDetector` 和 legacy protection encounter 输入。轨迹仅接受明确 EGM2008 orthometric 高度；局部 ENU 按重叠时间段计算水平/垂直/斜距、水平 CPA、阈值进入/退出与 engineering predicted conflict，平行/静止、无时间重叠、缺高度和 pending policy 均有显式保守状态。所有结果固定声明 `regulatory_well_clear=not_evaluated`，不生成法规 hazard zone。

`DAAEventStateMachineV1` 强制保存逐步 transition 的 time/reason/input evidence，覆盖正常 `NO_TRAFFIC→…→CLEARED` 及 `LOST_TRACK/ALERT_DELIVERY_FAILED/COMMAND_UNAVAILABLE/MANEUVER_UNRESOLVED`。S 只门控 detect/track，C 只门控 warning/command delivery，N 只影响 ownship state confidence；这些工程事件不写入 SafetyEvent/UE。Protection Budget 作为响应截止约束并记录 actual-vs-budget；V1 只模拟显式 confirmed command，不生成“最佳”避让。Step 04 新增 DAA Encounter Lab 的轨迹/CPA、距离、C/N/S、状态时间线与播放滑块。track/policy/service timeline/protection/capability/command 进入 fingerprint/stale，旧 schema-v2 自动 backfill，报告只加入 engineering summary。RouteVerticalProfileV1 同时输出 `profile_geometry_status` 与 `clearance_evidence_status`，旧 `status` 保留兼容。

当前里程碑：**interactive CNS planning product delivery baseline complete**；下一步先做 synthetic/manual end-to-end validation。

Route Planner V3-A 正确性修复 + V3-B corridor-local 精化 完整结果：**712 passed, 6 skipped, 0 failed**；Node 前端 **46 passed, 0 failed**；Python compile、全部 JS syntax 与 `git diff --check` 通过；V1/V2 characterization（`test_v1_algorithm_characterization`、`test_route_planner_v3.py` 内的 V1 契约、`test_risk_aware_route_planner_v2`）保持不变。

V3-A 三项正确性修复（必须保留，均有回归测试）：

1. **启发函数**：改为 `h = 纯 3D 几何距离`（scale 恒为 1，与 weight 解耦）。旧 `h = 距离 × (1 + Σweight)` 在 soft penalty 可为 0 时并非 admissible，已删除；新增 `test_heuristic_matches_dijkstra_optimal_cost_case_by_case` 对 6 类算例（含 open/plateau/buildings/高权重/零 penalty 捷径）逐例比对 `h=0` 的最优 cost 与距离。
2. **soft field**：删除 planner 内隐式 normalizer（`POPULATION_NORMALIZATION_PEOPLE=10000` / `TRAFFIC_NORMALIZATION_AIRCRAFT=100`）与 `SOFT_WEIGHT_SUM_LIMIT=1.0`；soft channel 只接受带 provenance 的 normalized index `[0,1]`，`edge exposure = length_m × (index_source+index_target)/2`，`scalar = Σ(边长 + Σ weight×exposure)`；weight 只需显式、finite、≥0；启用的 channel 缺 index 即 readiness blocked（`test_raw_counts_and_out_of_range_indices_are_rejected_not_normalized`、`test_enabled_soft_channel_without_a_provenance_index_blocks_the_run`、`test_no_hidden_normalizer_constants_and_no_weight_sum_cap`）。
3. **expansion cap**：达到 `max_expanded_states` 返回 `search_incomplete`（`search_completeness=expansion_cap_reached_optimality_not_proven`、`resource_limited=true`、`optimality_proven/infeasibility_proven=false`），不再返回 failed/infeasible；cap 前已找到的可行候选保留但显式标注未证明最优（`test_expansion_cap_is_resource_limited_never_failed_or_infeasible`）。

V3-B 关键边界：corridor-local fine grid（只在 support cells 米制窗口内、分辨率必须可追溯、禁止写死 30 m）；terrain hard floor = 相交有效像元最大值 + explicit clearance（禁止 center/average，`test_forbidden_terrain_sampling_methods_are_rejected`）；building = buffered footprint 保守包络（required floor = ground+height+vertical_clearance，缺 height/DTM blocked）；airspace 只消费 confirmed policy（`inferred_from_name_or_color=false`）；coarse soft index 只以 `upsampled_without_new_information=true` 复制且复用 RiskModel contributor normalized；multi-cell stride 记录 `traversed_cell_ids`/`traversed_entry_fractions` 并按插值高度逐格检查（`test_intermediate_obstacle_cannot_be_skipped_by_a_multi_cell_stride`、`test_climb_is_distributed_along_the_primitive_and_never_exceeds_the_gradient`）；转弯仍为 `R·|Δψ| ≤ stride_m` 弧长代理（V3-C 才做连续曲率验证）；结果只允许 refined_candidate/failed/not_ready/missing_data/search_incomplete 且强制 `final_validation_performed=false`/`operational_route=false`/`v3c_validation_pending=true`；refinement staleness 由 strategic/corridor/policy/source/frame/fine-grid fingerprint 判定（`test_v3b_refinement_becomes_stale_when_policy_or_sources_change`）。

## 8.6 Route Planner V3-C：连续 3D 几何实现 + 源几何硬约束验证（本轮）

基线 commit `d7de7b2eed3d58e2b2e3476d69608bd427d25615`。架构与完整语义见 `docs/route_planner_v3_architecture.md` §7A。

**明确不变（硬边界）**：未修改 V1/V2 搜索核心、代价公式或输出契约；V1 的 56×56 经纬度近似网格、BBOX 硬约束、`RoutePlannerV1.plan` 返回 dict 及其 `input_fingerprint` 由 characterization 测试锁定并保持不变；未修改 `spatial_3d` 语义（V3-C 高度**不写入** `route_altitude_profile`）；未注册进 `algorithm_registry`（默认 route planner 仍为 `route_planner_v1`）；V3-C 结果**不写入** `operational_routes`、`algorithm_selection` 或 `spatial_3d`；未发明 clothoid/energy；未 MakeValid、未修改任何真实 source geometry；未把 unknown 当 safe。

### 0. V3-B 三项技术债修复（必须保留，均有回归测试）

1. **DTM 有效分辨率的单位换算**（`FabdemWindowTerrainSource` / `_FabdemRasterBase.effective_resolution_detail`）：GT(1)/GT(5) 不再直接当米。projected CRS ⇒ 按该 CRS **自己验证过的 linear unit** 换算（metre=1.0；US survey foot 用其 factor；unit 不可读 ⇒ blocked）；geographic CRS ⇒ 原始值是**度**，改为在 corridor/reference location 用 `Geod`（无 pyproj 时是有标签的球面 haversine）测**相邻 pixel 中心**的地面距离。记录 `native_pixel_size_x/y` + `native_pixel_size_unit`、`effective_resolution_m_x/y`、`method`（`projected_crs_verified_linear_unit` / `geographic_geodesic_adjacent_pixel_centres` / `unresolved`）、`reference_location`/`reference_pixel`/`geodesic_backend`，并固定 `degree_values_never_reported_as_metres=true`。**禁止 degree-as-meter**（`test_geographic_raster_resolution_is_measured_in_metres_never_degrees`、`test_projected_raster_resolution_uses_the_crs_linear_unit_not_a_bare_one`、`test_unresolvable_raster_units_block_the_resolution_instead_of_assuming_metres`）。修好过程中同时发现并修正了相邻像元偏移量必须为 **1 pixel**（而非 pixel size 的数值）的问题。
2. **fine airspace 用 fine-cell polygon 而非 cell centre**（`ConfirmedAirspacePolygonSource`）：必须 **covered_by confirmed allowed union**（完整 cell polygon 落在**单个** confirmed allowed polygon 内、允许区边界不得穿入该 cell）**且与 confirmed blocked 无面积交集**；mixed/boundary/unconfirmed ⇒ `unknown`（fail-closed）。`query_mode = confirmed_policy_fine_cell_polygon_covered_by_allowed_union_and_disjoint_from_blocked_union`（`test_fine_airspace_cell_polygon_test_is_not_a_centre_only_test`、`test_fine_airspace_cell_fully_inside_the_allowed_polygon_is_confirmed_allowed`、`test_fine_airspace_blocked_intersection_wins_over_allowed_coverage`）。
3. **supercover 的 corner-crossing 保守语义**（`fine_search.supercover_line`）：线段**恰好穿过 grid corner** 时按保守语义包含**两个正交邻格 + 对角格**并**去重**，fraction 单调（`SUPERCOVER_BOUNDARY_SEMANTICS = conservative_boundary_touch_included_corner_crossing_yields_both_orthogonal_neighbours_and_the_diagonal_cell_without_duplicates`）。corner 判定来自 `t_max_row == t_max_column`（即线段同时穿过列方向格线与行方向格线），而不是"对角步"，因此 `(0,0)→(3,1)` 这类**掠角**线段也会被保守补齐。新增 `corner_crossing_points()` 审计 helper 与 corner obstacle 测试（`test_supercover_line_lists_every_cell_the_segment_touches`、`test_supercover_corner_obstacle_is_not_skipped`、`test_supercover_corner_obstacle_is_included_through_the_public_helper`），并在架构文档 §7.7 明确 boundary-touch 语义。

### 1. V3-C 独立 contracts（`continuous_contracts.py`）

`ContinuousRoute3D` / `ContinuousPrimitive3D`（straight / circular_arc）/ `TurnRealization` / `V3ValidationPolicy` / `ConstraintViolationInterval` / `DomainValidationResult` / `V3ContinuousValidationResult`，全部 JSON-safe、带 schema version。

- **输入只能是 selected + current 的 V3-B `refined_candidate`**（消费其 `metric_projection` / `state_path` / `frame` / `refinement_fingerprint`）；其他状态 ⇒ `not_ready` 且不运行。
- 结果 status **只允许** `validated_route / failed / unresolved / not_ready / validation_incomplete`。
- **即使 `validated_route` 也强制 `operational_route=false`、`cns_assessed=false`**（normalizer 无条件重置，任何构造路径都无法放松）。
- `curve_chord_error_m` **没有默认值**：缺失 ⇒ policy `blocked` ⇒ `not_ready`。
- `effective_v3c_policy` 把显式 V3-B planning policy（altitude band / clearances / Rmin / gradient limits）与显式 V3-C validation policy 按 provenance 合并；payload 显式给出的值优先，`confirmed:false` 不会被 stored policy 升级。

### 2. ContinuousGeometryRealizer（`continuous_geometry.py`）

在 V3-B local metric frame 内把 refined polyline 变成 **C1（position + heading）连续**几何，内部转弯用 explicit `aircraft_min_turn_radius_m` 的**解析圆弧 fillet**：

- `Δψ`；`R >= Rmin`，V3-C 先取 `R = Rmin`；`t = R·tan(|Δψ|/2)`；
- 若 `t` 超出任一相邻可用 segment、相邻 fillet overlap、几何退化（切点构造与解析转角不一致、非有限坐标）⇒ `turn_realization_failed` + `replan_required`，**禁止减小 R**（`test_segment_too_short_for_the_minimum_turn_radius_fails_and_never_reduces_the_radius`、`test_adjacent_fillets_that_overlap_fail_instead_of_shrinking_the_radius`）；
- 保存 analytic arc `center/radius/start/end angle/signed sweep/tangent points/arc length/chord length/radius_verified_from_center`；
- **只保证 position + heading 连续**：曲率在 straight↔arc 处可跳变；`continuous_curvature=false`、`c2=false`、`clothoid=future_work_not_implemented`；**禁止声明 continuous-curvature / C2**。

### 3. 曲线 linearization 的 explicit chord error

- 等角 sub-chord 线性化：`n = ceil(θ / (2·acos(1 − e/R)))`，每段 sagitta `R(1−cos(θ/(2n))) ≤ e`；
- 保存 analytic geometry **与** linearized LineString，并给出**实测** `actual_max_chord_error_m`（对解析圆弧密采样取到折线的最大垂距）；超出 requested ⇒ `turn_realization_failed`，不静默放宽；
- `not_the_mathematical_curve=true`；所有 vector/raster validation 都考虑该 **curve-error envelope**（route uncertainty envelope）；**不得把采样折线称为数学 exact curve**（`test_arc_chord_error_bound_is_measured_and_inside_the_explicit_tolerance`、`test_tighter_chord_tolerance_produces_more_segments_and_a_smaller_measured_bound`、`test_missing_or_nonpositive_curve_chord_error_is_refused`）。

### 4. Vertical profile

统一 `z(s) = EGM2008 orthometric altitude vs realized along-track distance`：tangent point 高度由 V3-B 对应 segment 线性插值（按 top-level 距离定位、最近 segment 胜出），arc 内按 arc-length 线性插值；每 primitive 记录 horizontal/3D length、`z_start/z_end`、`gradient`；重新验证 min/max altitude 与 max climb/descent gradient。**不写**旧 `spatial_3d` / `route_altitude_profile`（`test_climb_along_the_arc_is_interpolated_on_the_realized_distance`、`test_realized_altitude_bounds_are_revalidated_against_the_explicit_band`）。

### 5. ExactAirspaceValidator / 6. NativeTerrainValidator / 7. BuildingPolygonValidator / 8. KinematicValidator

- **Airspace**：只消费 confirmed 真实 polygon；realized route + curve-error envelope 转到米制后要求 **envelope 完全被 allowed union 覆盖且与 blocked union 不相交**；unconfirmed/unknown ⇒ `unresolved`（fail-closed）；有 confirmed lower/upper altitude 时按 intersection interval 检查 `z(s)`，没有垂向证据时明确 `horizontal_only_policy_evidence`，不猜高度；记录 `buffer_approximation`（Shapely `quad_segs`）。**V3-C 承担最终 route-level vector 验证**（V3-B 的 fine-cell 判定只是离散前置过滤）——`test_tiny_blocked_polygon_inside_a_v3b_centre_test_blind_spot_is_caught` 用 10 m×30 m blocked polygon 证明 V3-B centre-test 盲区被 V3-C 抓到。
- **Terrain**：**直接检查 FABDEM native raster**（`NativeTerrainWindowSource` 一次只读窗口），不用 fine-cell hard floor 替代最终验证；遍历 realized path（含 envelope）涉及的 native pixels；每 pixel 保留 source value / data_status / NoData / CRS；**NoData ⇒ unresolved**，**禁止 bilinear 填补、禁止当 0**；每 pixel 比较 route 在该影响 interval 的 min z 与 `terrain elevation + explicit terrain_clearance`；语义固定 `source_native_raster_validation`，**不声称真实世界无限连续 terrain exactness**（`test_native_dtm_single_pixel_spike_fails_the_realized_route`、`test_native_dtm_nodata_is_unresolved_and_never_filled_or_zeroed`、`test_terrain_uses_every_native_pixel_the_route_envelope_touches`）。pixel interval 由纯几何 helper `continuous_raster_window.resolve_native_pixel_intervals` 计算（clip 到"该 pixel 中心最近"的区间；相邻 pixel 区间在边界重叠，transition point 至少被一个 pixel 检查）。
- **Building**：复用现有 BuildingClearance 的 source / RTree provider query / EGM2008 roof+height 语义——`domain/building_clearance.py` 的 `building_roof_elevation` 与 `evaluate_vertical_clearance` 是**唯一** roof/垂直余量实现，`BuildingClearanceV1` 与 V3-C 共用（**不复制第三套 roof 公式**；行为等价由既有 characterization 锁定）；只查询 route bbox + clearance 候选；真实 footprint 按 `explicit horizontal_clearance + curve_error` 做 metric buffer 与 route LineString 求 affected intervals；每 interval 比较 min z(s) 与 `roof + vertical_clearance`；缺 height / 缺 ground / invalid geometry ⇒ `unresolved`（**不 MakeValid、不改源 geometry**）；保留 `building_id/source/roof/min horizontal+vertical margin/interval`；记录 buffer approximation 参数（`test_building_overflight_with_vertical_clearance_passes`、`test_building_side_clearance_failure_is_a_vertical_violation`、`test_missing_building_height_is_unresolved_never_safe`）。
- **Kinematics**：验证 analytic turn `R >= Rmin` 与 tangent heading continuity（逐 primitive 边界 + arc 端点与转角一致性）；重新验证每 continuous primitive 的 climb/descent gradient；记录 `minimum_turn_radius_observed_m` / `max_climb/descent_gradient_observed`；**不把 V3-B 的 `R·Δψ` 弧长代理当最终转弯验证**；**self-intersection 只作为 diagnostic**，除非 policy 显式规定 `self_intersection_is_failure`（`test_kinematic_validator_rejects_a_radius_below_the_minimum`、`test_kinematic_validator_revalidates_every_continuous_primitive_gradient`、`test_self_intersection_is_a_diagnostic_unless_the_policy_requires_a_failure`）。

### 9. V3ContinuousValidator 汇总

`geometry / airspace / terrain / building / altitude / kinematics` 全部 passed 才 `validated_route`；有确定违反 ⇒ `failed`；缺证据 ⇒ `unresolved`；source/refined candidate stale ⇒ `not_ready`；资源上限（`max_validation_samples` / `max_runtime_s`）⇒ `validation_incomplete`，**绝不当 failed**。`validation_fingerprint` 组件固定为 `refinement_fingerprint / continuous_policy_fingerprint / curve_tolerance_fingerprint / source_fingerprint / crs_fingerprint / validator_versions_fingerprint`。

### 10. violation interval 统一

`domain, reason_id, start/end_distance_m, start/end coordinate, required, observed, margin, evidence`。**不自动 repair / 不自动 replan**：失败只给 `replan_required=true` 与结构化证据。

### 11. Step 03 / 地图

Step 03 同时可见 **V3-A / V3-B / V3-C**：realized route、validated/failed/unresolved intervals、domain status、min margins、turn radius、climb/descent、source/tolerance，并**醒目标注"V3-C validated route 仍不是 operational route；CNS 尚未评估"**。V3-C 面板由独立 model 供数（状态独立），未写入 `spatial_3d`、也未复用 `route_altitude_profile` 绘图。地图 overlay 用 V3-B state path 的 (metric, geographic) 对拟合仿射/尺度映射来放置 realized 折线（不可拟合时不猜、不画），并用颜色区分 validated / failed / unresolved 与 violation interval 标记。

### 12. 本轮验证

新增 `tests/test_route_planner_v3_continuous.py`（80 项）：90° fillet 解析几何、right-hand fillet、segment 太短 / adjacent fillet overlap、heading tangent continuity、arc chord error bound（含更紧 tolerance 产生更多分段）、缺/非法 chord error 与 Rmin 拒绝、climb along arc、altitude bounds（below/above/missing）、tiny blocked polygon 落在 V3-B centre 盲区、allowed boundary、unconfirmed ⇒ unresolved、confirmed 垂向 band 检查、horizontal_only_policy_evidence、native DTM spike fail、DTM NoData ⇒ unresolved、每 native pixel 保留 source/CRS、missing clearance ⇒ unresolved、building overflight pass / side clearance fail / missing height / missing ground / invalid geometry ⇒ unresolved、far footprint 不构成约束、kinematics R / gradient / 缺失 limits / self-intersection diagnostic、geometry domain failure 与 chord/radius 异常、validated_route 需全部 domain passed、`operational_route=false`/`cns_assessed=false`（含 normalizer 对全部 status 的强制）、违反 ⇒ failed 不自动修路、缺证据 ⇒ unresolved、stale ⇒ not_ready、资源上限 ⇒ validation_incomplete（含 wall-clock 降级）、fingerprint 逐组件 stale 检测与 validator versions、project state/service 端到端（容器 backfill、零污染写入、保存恢复、确定性幂等、tolerance 变化 stale、未确认 policy 与无可选 refinement 拒绝、configured_real_sources 无 adapter ⇒ unresolved 不造数据、snapshot 投影）、以及 **V3-B 三项技术债回归**与 V1/V2 未注册/未修改。

本轮完整结果：全量 pytest **795 passed, 6 skipped, 0 failed**（6 项跳过仍是 `tests/test_map_http.py` 的真实 QGIS HTTP 集成）；Node 前端 **50 passed, 0 failed**（grid_theme **3 passed**）；`python -m compileall cns_planner tools`、`app.js/main.js/step03_routes.js/route_planner_v3_overlay.js/renderer.js/grid_theme.js/tiles.js` 语法检查与 `git diff --check` 全部通过。

## 8.7 Route Planner V3-D：V3-C validated route → operational adoption → CNS Assessment bridge（本轮）

基线 commit `9fe1d50457482a371e431710eee3009d5c884bd3`。架构与完整语义见 `docs/route_planner_v3_architecture.md` §7B。

**本轮目标不是开发航路算法**：把 current V3-C `validated_route` 以显式、可追溯、事务式方式发布到既有 `operational_routes` + `spatial_3d` 高度剖面接口，并复用既有 P7/P8/P9/P10 做 CNS Assessment。Route 与 CNS 仍然**串行**，CNS 绝不反馈 V3 cost。

**明确不变（硬边界）**：未修改 V3-A/B/C 规划与 validator 算法；未自动 replan；CNS 未进入 route cost；未注册 V3 为 `algorithm_registry` 默认 planner（仍为 `route_planner_v1`）；未发明 energy/clothoid；未修改 V1/V2（其搜索核心、代价公式与 characterization 输出由既有测试锁定保持不变）；synthetic route **不写**正式 `operational_routes`；EGM2008 正高**不写** GeoJSON 第三坐标；CNS 不满足**不等同** route unsafe。

### 0. 清理（只做结构清理，不改行为）

删除 `ApplicationContext._continuous_evidence_adapter` 中 `return build_evidence` 之后**不可达的 V3-B adapter 重复代码块**（与 `evaluate_route_planner_v3_refinement` 重复、且因 `return` 永不执行）。活跃方法集合不变（`configure_route_planner_v3_sources` / `evaluate_route_planner_v3_refinement` / `evaluate_route_planner_v3_continuous_validation` / `_fine_environment_adapter` / `_continuous_evidence_adapter`），编译与既有测试全通过。

### 1. 独立契约与服务

`domain/v3_operational_adoption.py`：`V3OperationalProjection`、`V3OperationalAdoption`、`V3OperationalAdoptionPreview`、`V3CNSAssessmentBundle` + normalizer / fingerprint / 语义常量。

`application/v3_operational_adoption_service.py`：`V3OperationalAdoptionService`（唯一写入者）。

- **V3-C validation 历史 immutable**：`operational_route=false`、`cns_assessed=false` 永不回写 `true`；正式状态只在新的 adoption/bundle 容器。测试对**全部** V3-C status 断言两个字段恒为 false。
- `operational_adoption` 走 `v3_operational_adoptions`，`cns bundle` 走 `v3_cns_assessment_bundle`，两者 schema/backfill/additive，旧项目无这两个键也能正常打开。

### 2. Publish gate

只允许 selected validation 满足全部条件：`status=validated_route`；validation **current**；source/refinement fingerprints current；`route_id` 对应当前 scenario route 且投影端点与之一致；real configured source chain ready；CRS transform 可用（不可用 ⇒ `crs_transform_unavailable`，不猜 identity）。

- **production Apply 禁止 `canonical_synthetic`**；Preview 可用 synthetic（标注"仅测试，不可正式发布"）且 `operational_routes_untouched/spatial_3d_untouched/cns_not_run` 全为 true；
- Apply 必须显式 `confirmed=true` 并提交 `expected_validation_fingerprint`；fingerprint 变化 ⇒ 拒绝（**TOCTOU**）；
- 支持 `validation_ids[]` 批量 **atomic** Apply：任一失败则全部不写；
- Apply/Revoke **事务式**：在 `deepcopy` 上完成全部变更，成功后就地安装（`_install_in_place`，因为整个服务图按引用持有 `session.state`）并保存一次；异常保持原状态。

### 3. V3→legacy 兼容投影

`ContinuousRoute3D` 仍是 authoritative source：用其**已验证 linearized metric geometry**（`simplification_applied=false`），经 recorded projected CRS 转 **OGC:CRS84**；`path` **只二维 `[lon,lat]`**；**EGM2008 正高绝不写入第三坐标**（会被误读为 WGS84 ellipsoidal height），高度只由 locked profile 承载。route 记录含 `route_id/start/end/start_node_id/end_node_id/kind=operational/status=passed/path` 与轻量 provenance（`source_type=v3c_validated_route`、validation id+fingerprint、refinement id+fingerprint、`curve_chord_error_m`、`horizontal_crs`、`crs_transform`、`vertical_reference=egm2008_orthometric`、`planner_family=route_planner_v3`、`cns_integration_mode=post_route_assessment`、`cns_excluded_from_search_cost=true`）。**完整 analytic geometry 不复制进 `operational_routes`**。

### 4. 自动生成且锁定 RouteAltitudeProfile

从 V3-C 每个 linearized point 取对应 `z(s)`；转 lon/lat 后对最终二维 path 重算累计基线距离；生成 `waypoint_linear` profile：`vertical_reference=egm2008_orthometric`、`confirmed=true`、`source=v3c_validated_route`、`derived=true`、`locked=true`、`locked_by_adoption=true`；保存 `v3_metric_length_m`/`legacy_geodesic_length_m`/`length_delta_m`/`distance_basis`。**path 与 profile 同一顶点顺序、同一距离基准**（profile 的 `distance_along_route_m` 就是 published path 的累计基线距离，逐顶点对应）。**禁止要求用户重新手填 V3 高度**。`RouteAltitudeProfile` normalizer **只做 additive** provenance/locked 兼容（旧/用户 profile 默认 `derived=false`/`locked=false`）；`Spatial3DService.set_route_profile` 在 route 由 V3 adoption 管理且 locked 时拒绝手工改高，撤销 adoption 后允许。**V1/V2 行为完全不变**（专项测试验证普通用户 profile 不受影响）。

### 5. Adoption state / Revoke

`v3_operational_adoptions` 每条保存 `adoption_id/route_id/validation ids+fingerprints/refinement fingerprint/projection fingerprint/applied_at/current_applicability/before-after 摘要/provenance/CNS link`。Apply 只对对应 `route_id` **upsert**，不清空无关 `operational_routes`；`route_id/start/end` 保持 scenario identity。**Revoke 只在 ownership 仍属该 adoption 时**移除其 derived profile/route（ownership 由 route provenance 的 `source_type + validation_id + validation_fingerprint` 与 profile 的 `source + locked_by_adoption + route_id` 判定）——专项测试用"另一 planner 后来生成同 id route"证明**不误删**。

### 6. Invalidation

**不对 Apply 调用 `workflow("route")`**。专用 `v3_operational_route_published`：保持新 route 与 current V3 validation 不变，只 stale downstream：`coverage_3d → cns_service_capability → service_timeline → cns_gap_v2` 以及 `cns_site_plan/closed_loop_assessment/building_clearance/route_vertical_profiles/cns_corridor_*/report`。Risk/grid/V3-A/B/C **不反向失效**（专项测试断言 `environment_risk`、`v3_planning_policy`、`route_planner_v3_experiments`、V3 validation staleness 均保持不变）。`terrain_dtm`/`buildings`/`AirspacePolicy` 变化经 `SourceAuditService` 注入的 `v3_adoption_invalidator` 只 stale V3 adoptees 并向下 stale CNS；**不误伤 V1/V2 route**。任何 CNS evaluation 前重新检查 adoption current，不 current ⇒ `assessment_status=stale` 且不给 verdict。

### 7. CNS Assessment bridge

新增 orchestrator，**严禁复制/改写 P7-P10 公式**，顺序复用 `Spatial3DService.evaluate → CNSServiceCapabilityService.evaluate → OperationalTimingService.evaluate_timeline → GapAnalysisV2Service.evaluate`。每阶段先检查**真实 prerequisites**（`required_cns` 从 scoped `project_default`/`route_overrides` 读取而不是容器顶层；另有 route profile、selected aircraft profile、existing facilities、timing），缺参数/required_cns/aircraft/timing ⇒ `incomplete` + blocking reason，**禁止造默认**。每个 `evaluate()` 会持久化自己的 canonical 结果并返回 workflow snapshot，因此 stage entry **从持久化容器读回**；阶段 route 级状态按**必选分系统**计算（项目不要求的分系统可以合法 `missing_data`，不使评估显得 incomplete，也不污染 verdict）。

### 8. CNS 结果语义

`assessment_status = not_started|incomplete|complete|stale`；`requirement_verdict = meets|does_not_meet|unknown`；`stage_results{P7,P8,P9,P10}` + fingerprints + ids。**"评估完成但 CNS 不满足" = `assessment_status=complete` + `requirement_verdict=does_not_meet`，且 V3 validation 仍 `validated_route`**（专项测试断言）。normalizer 强制 `route_validation_unchanged=true` 且从不回写 validation；**不得**把 CNS gap 改成 route validation failed。

### 9. P7 兼容性

P7 消费的 `altitude_profile` 就是 V3 投影（`source=v3c_validated_route`、`vertical_reference=egm2008_orthometric`）；同一 linearized route 端点一致；profile 在所有导出顶点高度一致；P7 采样得到的 EGM2008 高度与 derived profile 插值**逐顶点一致（容差 1e-9）**；无 AGL/WGS84 混用（`crs_mixing=false`，且断言采样里不出现 ellipsoidal）。记录 `compatibility_projection_semantics` 与 `projection_fingerprint`。

### 10-11. 前端与报告

Step 03 在 V3-C 下新增"发布为运行分析航路"：Preview（显示 route/profile/provenance/downstream invalidation，synthetic 显示"仅测试，不可正式发布"）→ 显式确认 Apply；状态链显示 V3-A strategic → V3-B refined → V3-C validated → V3-D published。

Step 04 新增 **V3 CNS Assessment summary**：Route validation / Operational publication / P7 geometry / P8 capability / P9 timeline / P10 gap / Assessment completeness / Requirement verdict——**route safety 与 CNS compliance 分列，不合并为一个总状态**。

报告新增 `v3_validated_route_provenance` 段（validated route id/fingerprint、adoption、horizontal/vertical representation、curve error、P7-P10 statuses、completeness vs verdict），并明确 Route Planning → CNS Assessment、CNS excluded from V3 search cost。`report_source_snapshot` 白名单 additive 加入两个新容器；报告 `source_fingerprint`/`report_id` 仍按内容确定性计算。

### 12. 本轮验证

新增 `tests/test_route_planner_v3_operational_adoption.py`（55 项）：容器 backfill/round-trip；gate 对 validated/current/route identity/source chain/CRS 的正反用例；failed/unresolved/not_ready/stale 拒绝；synthetic production apply 拒绝 + synthetic preview 零写入；缺 `confirmed` 拒绝；fingerprint TOCTOU 正反用例；batch atomic；unknown evidence source / unknown validation id；metric→CRS84 端点转换；二维 path 无 z（含 GeoJSON export 逐点长度）；route identity 保持；upsert 不清空无关 route；derived profile 的 locked/confirmed/EGM2008 与 metric/geodesic 长度字段；path/profile 顶点顺序与距离基准逐顶点校验；profile 高度取自 V3-C realized vertical；locked profile 拒绝手工改高（撤销后允许）；V1 普通 profile 不受影响；profile normalizer additive 兼容；revoke ownership（含"同 id 外来 route 不删"）；重复 Apply 幂等；Apply 不让自己 stale；只 stale downstream 且 Risk/grid/V3 不变；source change 只 stale V3 adoptees；source audit 变更传播；stale adoption 评估为 stale 而非 verdict；bridge 缺 prerequisites ⇒ incomplete + blocking；P7/P8/P9 真实复用（含 `algorithm_id` 与持久化容器复用）；缺 timing ⇒ incomplete；**CNS 不满足 ⇒ complete + does_not_meet 且 route 仍 validated**；bundle 链接与持久化；assess 需要已发布 adoption；bundle normalizer 强制分离语义；vocabulary 封闭；P7 高度 parity；compatibility 语义与 fingerprint；JSON-safe；publish status 正反；V1/V2 未注册未修改；报告 V3 provenance 不合并 verdict；adoption normalizer 固定表示边界。

前端新增 6 项（Node）：V3-D adoption model（gate/projection/adoption state/stage chain/三条 label）、Preview 面板（route/profile/provenance/downstream/TOCTOU 防护/locked/二维 path）、fingerprint TOCTOU helper、vocabulary 封闭、CNS summary 分列（含未发布禁用态）。

本轮完整结果：全量 pytest **850 passed, 6 skipped, 0 failed**（6 项跳过仍是 `tests/test_map_http.py` 的真实 QGIS HTTP 集成）；Node 前端 **55 passed, 0 failed**（含 grid_theme 3 项）；`python -m compileall cns_planner tools`、`app.js/main.js/step03_routes.js/step04_operation.js/route_planner_v3_overlay.js/renderer.js/grid_theme.js/tiles.js` 语法检查与 `git diff --check` 全部通过。

**人工走查结论（outputs/ 下 ignored harness，不属于测试套件）**：synthetic V3-C validated route 的投影得到 11 个顶点的二维 `[lon,lat]` path（端点与 scenario route 一致）、`v3_metric_length_m=2855.13` 与 `legacy_geodesic_length_m=2863.19`（`length_delta_m=8.06`）、locked profile 11 个顶点且 `z=100 m`；Apply 后 `operational_routes=[R0001 passed operational]`、下游状态 stale 而 `routes` 仍 passed；手工改高被拒；未满足需求时 `assessment_status=complete` + `requirement_verdict=does_not_meet` 而 V3-C 仍 `validated_route`；缺 timing 时 `incomplete`；Revoke 移除 route+profile 并保留外来 route。

## 8.8 V3 Real-Data Enablement V1：Airspace Decoupling + readiness fixes + V3-A configured_real_sources（本轮）

基线 commit `ab8f4e9587db10db1ee801f39312ff424e12f466`。本轮**不**开发 V3-E、不补任何 V3 safety/aircraft/risk 默认值、不要求人工测试、不改真实项目数据、不 push。

**已发现问题的修复**：`cns_planner/application/route_planner_v3_service.py::real_data_readiness()` 在 `terrain_vertical_reference` 与 `building_grid_source` 之间存在残留裸 `...`（main commit diff 同样存在），导致 `SyntaxError`。已修复；因此此前“850 pytest passed”**不能**作为当前 HEAD 的测试结论，本轮重新全量执行。

**架构决策（本轮生效）**：适飞空域 / `AirspacePolicy` / `airspace_eligibility` 自本轮起只有一种语义——`display_only_reference_layer`。

- 不参与 risk、V1/V2/V3 feasibility/search/readiness、allowed/blocked graph、candidate corridor、V3-B、V3-C gate、V3-D adoption、route comparison、CNS assessment、fingerprint/staleness/invalidation。
- 旧 schema/API 仅为 backward compatibility 保留，并标记 `deprecated/display_only/not_used_for_planning`。
- 禁止 `outside => blocked`、`unknown AirspacePolicy => unsafe`、图层名/颜色 => allowed/blocked。
- 未来 `regulatory_constraints` 是**独立扩展位**；本轮不把当前适飞空域重新解释成法规约束。

**逐层解耦**：

- **V1/V2**：`RiskAwareRoutePlannerV2` 删除 AirspacePolicy allowed-cell/edge gate，`airspace_eligibility` 仍可作为兼容参数接收但**不进入** fingerprint/blocked/missing_data，搜索空间 = 工作区有效标准网格 + 真正 explicit hard constraints；无 AirspacePolicy 仍可运行，`grid/risk/distance/adjacency/corner-guard/cost` 行为与路径保持不变。`RiskModelV1` 的 overall 公式未改写，`airspace` 从 active inputs、required mapped attrs、source_versions/fingerprint、availability、normalization/invalidation 中移除；legacy `airspace_constraint` 固定输出 `not_applicable`。
- **V3 hard constraints / readiness**：`HardConstraintEvaluator` 不再因 unknown/restricted airspace 拒绝 cell；active hard constraints 仅 terrain clearance、building clearance、altitude bounds、turn、climb/descent。`evaluate_v3_readiness` 的 airspace 项固定 `status=ready / applicability=not_applicable / layer_role=display_only_reference_layer`，不影响 overall。
- **V3-A**：`V3_ENVIRONMENT_SOURCES` 扩展为 `canonical_synthetic + configured_real_sources`。`route_planner_v3` 算法包**不**读取 QGIS/GDAL/files；真实数据由 GIS 边界的新适配器 `cns_planner/gis/v3_environment_adapter.py::V3RealEnvironmentAdapter` 注入，构造 canonical `V3CellEnvironment`。数据来源：当前 L8 grid、verified FABDEM `terrain_dtm`、verified buildings/building_grid L8 facts、`grid_risk` soft fields、source audit/provenance。**不**消费 airspace。`building_count > valid_height_count` 时保留 unresolved，绝不把未知建筑高度当 0；terrain 高程缺失时不猜 clearance。
- **readiness 修复**：terrain vertical reference 从 canonical `terrain_dtm` source profile/audit 解析（`crs.observed_vertical=EGM2008_orthometric` + `verified_from_raster_metadata` 被正确识别）；`building_grid` 从 `source_audits.items.building_grid` 的 canonical identity/status 获取。因此真实 FABDEM/buildings/building_grid 可表达 `verified/ready`，不再 null/false。real source adapter readiness 可为 ready，但 V3-P1 未确认时 `policy_readiness`/`aircraft_readiness` 必须仍 blocked，`configured_real_sources` **不得**通过 `allow_unconfirmed_policy` 绕过。
- **V3-B**：`FineEnvironmentAdapter`、`FINE_SOURCE_ROLES`、source_readiness/source_audit/fingerprint 不再要求 AirspacePolicy，删除 `no_confirmed_allowed_airspace_cells` blocker；fine cell 不因 current airspace unknown/outside 变 blocked。
- **V3-C**：real evidence adapter 不再读取 `ConfirmedAirspacePolicySource`（该类保留为 legacy 兼容，**未删除**）。`airspace` domain 返回 `status=skipped / applicability=not_applicable / reason=display_only_airspace_not_used_for_route_constraints`。`ACTIVE_V3C_DOMAINS = geometry, terrain, building, altitude, kinematics`；`_route_status`、`all_domains_passed`、`validated_route` 只要求 active domains passed；`validation_fingerprint_components` 的 policy/source 分量排除 `airspace_allow_touching_blocked_boundary`、`airspace`、`airspace_policy`。geometry/FABDEM/buildings/altitude/kinematics 仍严格 fail-closed。
- **V3-D**：production source chain/readiness/fingerprint 不要求 AirspacePolicy；`stale_for_sources` 移除 airspace 及仅因旧 airspace 链加入的 basemap dependency，`["airspace","basemap"]` 返回 `not_applicable` 且不 stale 任何 adoption/adopted route。`validated_route != operational_route` 与 `CNS unmet != route unsafe` 未改变。
- **Invalidation / Source Center / Step 03**：切断 `airspace_policy -> routes/CNS` dependency（`services/invalidation.py` 中 `airspace_policy: ()`）；`InvalidationService.SOURCE_ATTRIBUTES` 中 `airspace`/`basemap` 为空集；`SourceAuditService._source_changed` 对 `airspace`/`basemap` 只更新显示健康与 legacy policy `needs_revalidation`，不触发 `grid_sources`、不触发 V3 adoption invalidator。legacy AirspacePolicy backfill/API 保留，但修改 policy 不再 stale planning。前端删除 Step 03 的 AirspacePolicy editor/`airspacePolicyPanel`、`data-save-airspace-policy`、confirmed allowed count、V2 airspace readiness warning、`airspace_risk` 栅格主题与 `map/airspace_policy_overlay.js`（confirmed-allowed 叠加层）；保留**原始适飞空域参考图层**与开关（`id="air"`，标注“仅显示，不参与路线约束”），不再用 “confirmed allowed overlay” 暗示规划可行性。
- **DATA-3**：`retired/not_applicable_by_architecture_decision`，不计入 `pending_items`/blocking；DATA-1、DATA-2 语义不变。

**新增/调整测试**：新增 `tests/test_airspace_decoupling_v1.py`（无 AirspacePolicy 的 V2 可运行、risk value/fingerprint 不受 airspace 影响、V3 hard constraint 不因 airspace 拒绝、V3-C airspace `skipped/not_applicable` 不阻塞 `validated_route`、V3-D `stale_for_sources(["airspace","basemap"])` 不 stale、V3-A real adapter 保留 unresolved 建筑高度、V3-A real readiness/`allow_unconfirmed_policy` 绕过被拒、V3-B 无 airspace role/blocker、real source adapter 不构造 ConfirmedAirspace*、airspace/basemap 源变化不 stale risk/routes）；`tests/test_route_planner_v3_continuous.py` 改用 `ACTIVE_V3C_DOMAINS` 并新增 fingerprint 排除 display-only airspace 的回归；`tests/test_route_planning_diagnostics.py` 断言 DATA-3 `retired`；`tests/frontend_modules.test.mjs` 断言 Step 03 无 AirspacePolicy editor/confirmed-allowed 语义、Source Center 无 AirspacePolicy 动作且叠加层模块已删除。

**本轮完整结果**：全量 pytest **866 passed, 6 skipped, 0 failed**（6 项跳过仍是 `tests/test_map_http.py` 的真实 QGIS HTTP 集成）；Node **58 passed, 0 failed**（`tests/frontend_modules.test.mjs` 55 项 + `tests/grid_theme.test.js` 3 项）；`python -m compileall cns_planner tools tests`、全部改动 JS 的 `node --check` 与 `git diff --check` 全部通过。**未修改真实项目数据，未 push。**

> 沙箱说明：本轮全量 pytest 与 Node 测试在 DSH workspace-write 沙箱内执行。受限沙箱会拒绝写入 mode `0o700` 的目录（pytest basetemp/per-test 目录与 `tempfile.mkdtemp` 都是该 mode），因此使用 gitignored 的 `outputs/dsh_tmp_plugin.py` shim，并改用 `node <file>`（`node --test <file>` 因 piped stdio EPERM 失败）。这是环境工件，不是代码缺陷。



## 8.1 航路规划基础治理 + 专家评审基线（本轮）

本轮目标是为航路规划专家评审准备**可信 baseline**，不是继续扩算法能力。基线 commit `33752b6759d992db39c639085a05e5c291945a38`。

**明确不变（硬边界）**：未修改 V1/V2 路径搜索核心、代价公式或既有输出契约；未把 BBOX 硬约束改为 polygon；未决定垂直间隔；未实现 Theta*/RRT/Dubins/V3；未修改 RiskModel、AirspacePolicy 规则或 BuildingClearance；未新增 DAA/Gap/设备/安全 UE 能力。V1 的 56×56 经纬度近似网格、图层 BBOX 硬约束、`RoutePlannerV1.plan` 返回 dict 及其 `input_fingerprint` 均由 characterization 测试锁定并保持不变。

清理项：

- 删除无人使用且签名错误的 `domain/route.py` `RoutePlanner` Protocol（全仓库无 import）。
- 删除 V1 中未被引用的 `RoutePlan` dataclass；V1 dict 输出与 `input_fingerprint` 完全不变。
- `.gitignore` 新增 `.pytest_tmp/`，并用 `git rm -r --cached --ignore-unmatch .pytest_tmp` 取消跟踪已提交的 pytest 临时产物。
- `docs/03-架构与数据字典.md` 开头标注为 **schema-v1 历史设计稿**，当前以 `AI_DEV_CONTEXT.md` 与 `docs/CNS_TECHNICAL_BASELINE.md` 为准。

Hard constraint 必须 fail-closed：

- 新增 `cns_planner/application/constraint_validation.py`：在 Application 输入边界统一校验，要求每项为 dict/映射且 `bbox` 为 4 项、**finite**、`west < east`、`south < north`；返回归一化副本（float bbox），否则抛出带索引/图层名与可操作建议的 `ValueError`。
- `RouteService.generate_operational` 在调用任何 planner **之前**校验；V1/V2 内部搜索语义未改动，非法 bbox 绝不被静默当作无约束、也绝不被丢弃后继续规划。API 层沿用既有 HTTP 400 错误响应。
- 边界语义区分明确：`None`/`[]` 表示**确实没有硬约束**；畸形输入是错误，不是空约束。

Planner manifest 与真实输出对齐（不改算法输出）：

- V1 manifest：参数 schema 明确 `grid_size` **默认 56**、`minimum 2`、`additionalProperties=false`；outputs 收敛为真实返回键；limitations 明确四条——**经纬度固定格 / 非米制搜索 / BBOX 硬约束 / 无风险·高度·运动学**。
- V2 manifest：inputs 补 `airspace_eligibility`；保留二维战略水平规划、MH/T 网格中心、无 smoothing 等 limitations。
- Step 03 新增通用“**当前规划器**”卡片，直接从 `algorithm_catalog` manifest 显示 id/version/maturity/description/inputs/assumptions/limitations 与**有效参数**（schema 默认值 + 当前选择，标注来源）。前端不硬编码任何算法限制；选择与 catalog 无精确匹配时只显示“无精确匹配的 Manifest”，不推断限制。V1/V2 都会显示。

RouteVerticalProfile 假过滤语义修复：

- 现状：前端传 `route_id`，service 完全忽略——即“看起来能按航路过滤，实际总是全量”。
- 修复：不再发送无效 `route_id`；`RouteVerticalProfileService.evaluate` 对具体 `route_id` 请求**显式拒绝**（`ValueError`），只接受 `None`/`""`/`"all"` 全量范围。
- state 仍是**全量结果**（每条 current operational route 一个 profile）；前端下拉只切换图表显示对象并在 UI 中写明“刷新全部剖面（全量评估）/ 当前全部运行航路”，不制造按单条路由过滤的假象。

显式 OD 航路场景能力：

- 新增 `RouteService.generate_scenario_od(start_node_id, end_node_id, direction)`：只创建用户显式指定的这一条（或这一对双向）场景航路，**不会**因参考点数量自动生成全连接。
- 原 `generate_scenario(direction)` all-pairs API 保持兼容（3 节点仍生成 6 条双向路由），旧项目不受影响。
- API 新增 `/api/workflow/scenario-od`；Step 03 优先提供“起点 → 终点 → 创建航路”，旧 all-pairs 按钮保留并标注“兼容”。
- 与既有 pair 生成器一致：显式 OD 会替换当前场景航路集合、复用仍存方向的 `route_id`、退役被移除方向，并清空运行航路（需重新生成）。

独立 RouteQualityEvaluator 与 benchmark：

- `cns_planner/benchmark/quality.py`：**独立于 planner** 的质量度量，绝不写入 V1/V2 结果。指标含 `path_length_m`、`detour_factor`、`segment_count`、`turn_count`、`total_heading_change_deg`、`max_heading_change_deg`、`min_segment_m`，全部由已发布 polyline 实测（罗盘航向、0°=北）。
- V2 已发布的 `distance_m`/`risk_exposure_index_m`/`mean`/`max`/`optimization_cost` 原样读取为 `planner_reported`，**不重算风险**（`risk_recomputed=false`）；并给出 planner 自报 vs 实测的差值，供专家核对。
- constraint/allowed feasibility 使用既有权威结果：硬约束复用同一 validator；allowed 只读 planner `status`，不重新判定。
- `runtime_ms` 仅用于报告，明确不参与任何质量判定。
- 输出 `verdicts={automatically_ranked:false, automatically_scored:false, preferred_algorithm:null}`。

可复现 synthetic benchmark fixtures：

- `cns_planner/benchmark/fixtures.py` 提供 8 个确定性算例：`open_space`、`single_obstacle`、`concave_obstacle`、`narrow_passage`、`disconnected_allowed_airspace`、`risk_tradeoff`、`endpoint_near_boundary`、`malformed_constraint`。全部为生成几何，无真实数据、无 QGIS、无网络。
- 每个 case 声明 `applicability`：不能运行的 planner 明确报 `not_applicable` / `missing_prerequisite` / `rejected_at_input_boundary`，**不得为了让 case 通过而修改 planner**（`planner_changes_allowed=false`）。
- V2 使用合成 MH/T L7 网格（0.1°×0.1°、900 格）、合成 `grid_risk`、合成 confirmed allowed 外壳与硬约束阻断盒；`risk_tradeoff` 用两个显式 λ（0 与 8）观测长度/风险暴露取舍，λ=8 时路由变长约 1.48 km、风险暴露下降约 77%。

证据包 `tools/route_planning_baseline.py`：

- 运行 benchmark 并输出 `outputs/route_baseline/route_planning_baseline.json` + `.md`（`outputs/` 已被忽略）。
- 内容包含 planner manifest/limitations、case 输入摘要、status、质量指标、失败原因、有效参数、runtime_ms（仅报告）。
- 它是给专家的证据包：**不自动排名/评分/推荐算法**，并在文末列明本轮明确未做的事。

Step 03 并列查看：

- 新增 V1/V2 结果并列面板，只做长度/几何与 V2 自报风险指数的**事实并列**，显式声明“本面板不判定更好、不排名、不评分、不推荐算法”。
- 真实参考航线存在时仍只做长度/几何并列，不作优劣结论。
- 并列面板说明 V1 顶点更少源于其对共线点的简化、V2 保留完整 grid path（无 smoothing），属**输出契约差异**而非质量结论。

本轮测试：全量 pytest **459 passed, 6 skipped**（6 项跳过仍是 `tests/test_map_http.py` 的真实 QGIS HTTP 集成）；Node **33 passed, 0 failed**；`python -m compileall cns_planner tools`、`app.js`/`main.js`/`step03_routes.js`/`route_vertical_profile.js` 语法检查与 `git diff --check` 全部通过。新增 `tests/test_route_planning_governance.py`（25 项：fail-closed、OD、manifest、清理回归）与 `tests/test_route_planning_benchmark.py`（22 项：evaluator、fixtures、证据包、不调用 inapplicable planner）。

**仍需专家决策（本轮不决定）**：V1 是否改用米制/等距格或保留经纬度固定格（B2）；BBOX 硬约束是否升级为 polygon/精确几何（B3）；垂直间隔与高度层规则（B4）；是否引入 Theta*/RRT/Dubins/V3 或路径平滑；`risk_weight_lambda` 与最大相对风险阈值是否存在工程/运行依据；benchmark 是否需要真实数据与更严格质量门限。

## 8.2 航路规划专家证据闭环 V2（本轮）

基线 commit `58a3b96f3ff08e178dcfb9dd279fc68f44fb108a`。本轮把上一轮的 baseline 升级为**证据闭环**：实验与当前正式结果彻底分离、度量改为测地口径、reference 数据 CRS 语义 fail-closed、新增只读 reference 比较、AirspacePolicy/数据就绪可见。

**明确不变（硬边界）**：未修改 V1/V2 路径搜索核心、代价公式、图邻接或输出契约；未实现 V3/Theta*/RRT/Dubins；未继续 DAA/Gap/CNS 设备；未实现 ET parser；未按图层颜色/名称推断 suitability；未自动关联 reference route；未产生 similarity score/排名/优劣结论。

### 1. 实验与当前正式结果分离

- `operational_routes` 仍然**只**表示当前正式 planner 结果，未新增任何写入者。
- 新增 additive `route_planning_experiments`（`domain/experiment.py` + `application/route_experiment_service.py`）：每条记录保存 `experiment_id`、scenario/input fingerprints、planner id/version/manifest/effective params、result snapshot、quality、runtime、provenance。
- 运行 V1/V2 比较**不切换 `algorithm_selection`**、**不覆盖 `operational_routes`**、不改变 `result_statuses["routes"]`；record 内固定 `operational_routes_untouched=true` / `algorithm_selection_untouched=true`。
- 实验身份 `EXP-<12hex>` 只由 scenario fingerprint + planner 版本集合 + 有效参数决定（名称/时间不参与），同输入重跑幂等；集合上限 20 条并保留最新。
- `current_applicability` 在读取时按当前 scenario fingerprint 计算：输入变化后记录标记 `stale_scenario_inputs`，但**不删除**冻结证据。
- 实验复用 `RouteService.planner_context()` / `plan_routes()` 的**同一**输入视图与 dispatch（该重构行为等价，characterization 测试锁定）；因此 experiment 与正式路径不会因参数口径不同而不可比。实验仅复用当前 selection 的参数，且只读。
- API：`GET /api/route-experiments`、`POST /api/route-experiments/evaluate|delete`。
- Step 03 新增“规划器比较实验”面板，可同时列出 V1/V2 实验结果并明确 **experiment ≠ current operational route**。

### 2. Benchmark 重复运行语义

- `tools/route_planning_baseline.py` 升级为 `route-expert-evidence-2`，支持 `--runs N`（默认 5）与 `--warmup N`（默认 1）、`--project PATH`。
- 每次重复记录 runtime 与 result/path fingerprint；输出 `min/median/p95/max`（另附 mean/stdev）、`deterministic_consistency`、distinct fingerprint 数、`hostname/python/平台/CPU/git` 与有效参数；seed 字段为未来随机 planner 预留（当前 V1/V2 确定性、保持 `null`）。
- 统计学口径参考 OMPL benchmark 语义（重复 + 预热 + 分位数），**不引入、不依赖 OMPL**；所有统计字段固定 `used_for_ranking=false`/`used_for_scoring=false`。

### 3. RouteQualityEvaluator 度量口径修正

- 新增 `benchmark/geodesy.py`：距离/方位改用 `pyproj.Geod(ellps="WGS84")` **测地**计算，彻底移除 `atan2(delta_lon, delta_lat)` 的度空间航向。
- 航向约定为罗盘方位（0°=真北，顺时针）；heading change 归一化到 `[0,180]`，容差 `1e-9` 并随结果输出；无 pyproj 时回退到有标签的球面 haversine 且在 `metric_semantics.backend` 明示。
- 结果输出 `metric_semantics`（distance/bearing/backend/ellipsoid/单位/约定/容差）、`heading_method`、`assumed_input_crs=EPSG:4326` 与 `vertex_dedup_tolerance_m`。
- 未改变 planner 结果；`planner_reported` 与 `planner_reported_vs_measured` 继续并列，供专家核对两套口径差异（V1 固定度网格 / V2 项目米制距离 vs 本次测地度量）。

### 4. Reference CRS 语义（fail-closed）

- 新增 `domain/reference_crs.py`：分离 `source_crs` 与 `representation_crs`，各自保存 `value/status/axis_order/confirmed|declared_by_format/source/evidence`。
- `crs_status=pending` 时：坐标仍可按源数值临时显示（保留 `source_numeric_path`），但**正式 `length_m=null`**、`length_status=blocked_unresolved_source_crs`、`metric_geometry_similarity_available=false`，并输出禁用原因与 warning。
- CSV/XLSX **绝不**被猜成 WGS84/CGCS2000（`representation_crs.value=null`）；GeoJSON 按 RFC 7946 记录 `representation_crs=OGC:CRS84`（`status=declared`、`declared_by_format=true`），但**不据此证明原始 source CRS**，仍保持 source 未确认、length 禁用。
- 只有 `source_crs.status=confirmed && confirmed=true && value` 才启用米制度量；仅有值但未确认仍被拒绝（`source_crs_value_not_confirmed`）。
- 旧项目 additive backfill：`crs_status` 降级为可见 evidence（`legacy_crs_status_field`），旧 `length_m` 移入 `legacy_length_m`，正式 `length_m` 置 null；空集合回填**幂等**，保证项目 round-trip 稳定。

### 5. 显式 reference route 关联

- 新增 `domain/reference_route_link.py` + `application/reference_link_service.py`：`ReferenceRouteLink` 把 `reference_route_id` 与 scenario route/OD 关联，**必须 `confirmed=true` 且带 source**，否则拒绝。
- 端点距离候选只在 reference source CRS 已确认后给出，且永远 `requires_user_confirmation=true; automatic_association=false`；CRS 未确认时返回 `blocked` + 明确原因，不造候选。
- API：`GET /api/reference-route-links`、`POST /api/reference-route-links/create|delete`、`GET /api/reference-endpoint-candidates`；Step 03 支持“参考航线 ↔ 当前 OD”选择与逐候选确认。

### 6. 只读 RouteReferenceComparator

- 新增 `benchmark/reference_comparison.py`：仅在 **CRS 已确认 + link 已确认** 时计算 reference/planned 测地长度、`length_delta_m`、`length_ratio`、`start/end_offset_m`、`Hausdorff distance_m`、`discrete Fréchet distance_m`。
- Hausdorff/Fréchet 先投影到记录明确的**局部米制 CRS**（以数据为中心的 Transverse Mercator，输出 proj4 串、中心经纬度、units=m、backend），再按记录 `sample_spacing_m`（默认 25 m）densify/resample；输出投影、方法、间距与样本数。
- 前置条件缺失时返回 `status=not_ready` 与结构化 reasons，绝不半算；`similarity_score/ranking/verdict` 恒为 `null`。
- 结果只读派生（不持久化），随 experiment snapshot 的 `reference_comparisons` 返回，并区分 `comparisons` 与 `blocked`。

### 7. AirspacePolicy readiness

- Step 03 新增只读“AirspacePolicy 就绪总览”：逐条 feature_id/category/source/route_eligibility allowed|blocked|unknown/confirmed，以及 allowed/blocked/unknown/confirmed counts 与 **V2 readiness 原因**。
- 明确 `never_inferred_from_layer_name_or_color=true`：绝不按颜色/名称推断 suitability。
- policy 修改继续沿既有 `set_airspace_policies` → airspace eligibility/route stale 链失效（本轮未新增失效通道，仅补测试）。

### 8. 数据就绪面板

- 新增 `GET /api/data-readiness` 与 Step 03 面板：参考起降点 CRS、参考航线 CRS/格式/数量、AirspacePolicy 完整度、reference route link 与 experiment 数、ET 政策。
- ET 继续 `requires_xlsx_or_csv_conversion`，`et_parser=null`，**未实现 ET parser**。

### 9. 专家证据包升级

- `tools/route_planning_baseline.py` 统一为一个入口，输出 `outputs/route_baseline/route_planning_baseline.json` + `.md`（ignored），包含：合成 benchmark 重复统计、planner manifests、route experiments（项目级）、真实 reference comparisons（若 ready）、data readiness、已知局限、待专家决策（D1/D2/D3）与“明确未做的事”。
- 项目证据缺失或未确认 CRS 时明确 `NOT READY` + 原因（`not_supplied`/`not_found`/`blocked`），**不造数据**。

本轮测试：全量 pytest **528 passed, 6 skipped**；Node **37 passed, 0 failed**；`compileall`、JS syntax 与 `git diff --check` 全部通过。新增 `tests/test_route_experiments_and_reference_crs.py`（38 项）与 `tests/test_reference_comparison_and_readiness.py`（31 项），覆盖实验隔离、重复统计/确定性、测地航向、CRS pending/confirmed、显式 link、米制 Hausdorff/Fréchet、policy readiness 与 stale、旧 schema backfill、前端双 planner 实验显示。

**仍需人工决策（D1/D2/D3，本轮不决定）**：
- **D1**：BBOX 硬约束是否升级为 polygon/精确几何边界；
- **D2**：垂直间隔与高度层规则如何确定；
- **D3**：V1 是否改用米制/等距格，以及是否引入 Theta*/RRT/Dubins/V3 或路径平滑。
**仍需人工动作（数据侧）**：舟山起降点/航线坐标 CRS 必须由人工确认并写入 `source_crs`（本轮已提供证据字段与入口，未自动推断）；`.et` 必须人工转 XLSX/CSV；AirspacePolicy 需逐 feature 显式确认后才能解除 V2 readiness blocked；reference route ↔ OD 关联需人工逐条确认。

## 8.3 航路规划问题定义 + 专家咨询诊断基线（本轮）

基线 commit `e5cfec8afef77b44ed8f5a009222bf7ce6bae26d`。本轮只增加问题定义、独立诊断、敏感性工具、专家咨询材料与 Step 03 只读展示；**V1/V2 的搜索核心、8 邻域、代价、risk lambda 语义、hard constraint 形式和输出契约全部冻结**，未实现 V3/Theta*/RRT/Dubins/smoothing。

- `docs/route_planning_problem_definition.md` 固化战略水平 route 的输入、输出、硬条件、当前目标、已确认假设与未决问题，并严格区分 Fact / Constraint / Policy / Objective；战略规划不等于 DAA 战术避碰。
- `RoutePlanningDiagnostics` 保持在 benchmark 层并只读取 published result：新增 vertex/segment、总/最大 heading change、min segment、`zigzag_index=sum(abs heading change)/(180×内部顶点数)`、MH/T 水平/垂直/对角步数与八方向直方图、grid level/cell size、V2 已发布 risk、hard constraint/allowed 状态与 runtime。所有 verdict 均不评分、不排名、不推荐。
- synthetic benchmark 在原 8 case 上新增 `zigzag_open_grid_bias` 与 `bbox_overblocking_demo`；后者只说明现有 BBOX 包络与假想 source polygon 可能不同，不改变 planner，也不声称 polygon 是最终方案。10 个 case 均带 `expert_question`。
- `tools/route_planning_diagnostics.py` 对冻结 V2 做 λ=`0,0.5,1,2,4,8` 与 MH/T L6/L7/L8 敏感性观测；输出 status/length/detour/turn/heading/risk/runtime/path fingerprint，禁止自动推荐参数。
- experiment 适用性改为 scenario fingerprint + planner-context fingerprint 双检查；workspace/grid/grid risk/airspace eligibility/hard constraints 变化会标 `stale_context_inputs`。缺 context fingerprint 的旧证据标 `unknown_legacy_context_inputs`，不得误报 current。多航路 `{results:[...]}` reference comparison 按 `route_id` 精确取 path。
- Step 03 新增只读“航路规划诊断”，展示当前 planner、几何、grid behavior、risk、约束输入、manifest limitations 与历史 experiment diagnostics；没有专家算法执行/导入入口。
- `tools/route_planning_expert_brief.py` 生成 ignored `outputs/route_expert_brief/route_planning_expert_brief.{md,json}`，固定包含任务、约束、V1/V2 限制、benchmark、λ/grid sensitivity、典型问题、真实数据 readiness 与 P1–P8。

统一未决项命名：

- **DATA-1 = reference CRS**：舟山参考点/线源 CRS 待权威确认，禁止猜 WGS84/CGCS2000。
- **DATA-2 = ET→XLSX/CSV**：`.et` 需人工转换，系统只返回 `requires_xlsx_or_csv_conversion`。
- **DATA-3 = AirspacePolicy（已退役）**：状态固定为 `retired/not_applicable_by_architecture_decision`。当前适飞空域是 `display_only_reference_layer`，不是 route eligibility、risk evidence 或 regulatory constraint。legacy schema/API 仅为 backward compatibility 保留；禁止从颜色/名称推断，未来法规约束是**独立扩展位**。
- **EXPERT-1 = constraint geometry**：BBOX/polygon/raster/混合表达。
- **EXPERT-2 = vertical/altitude**：2D+独立高度或 3D 联合规划。
- **EXPERT-3 = state space/algorithm**：MH/T 搜索空间/风险索引与算法类别。
- **EXPERT-4 = kinematics**：转弯半径、航向、爬升进入搜索或后处理。
- **EXPERT-5 = objective/risk cost**：距离+风险的加权和/约束/分层/Pareto 表达。

真实项目数据当前允许保持 **NOT READY**：本轮未猜 CRS、未解析 ET。专家 brief 的“真实数据 readiness”不再复用 project-evidence 的读取状态（那只是“项目能否读出”），而是由 `real_data_verdict()` 独立给出 DATA-1/2 逐项 verdict + 需要的人工动作，并把 DATA-3 固定为 `retired`（不计入 pending/blocking）。当前真实项目为 `NOT_READY`（DATA-1/2 pending，DATA-3 retired），`metric_measurement_enabled=false`。

合成 allowed-airspace fixture 改为直接按 MH/T `grid_id` 整数索引构造邻接（`_adjacency_edges`），不再调用 `AirspaceEligibilityService.build`：后者对候选边做 pairwise 全覆盖判定，是 O(n²)，在 MH/T L8（8100 格）下由 57 s 主导整轮工具运行。改动只影响**合成 fixture 的构造方式**，allowed 集合与邻接语义等价（全部 case 的 path_length/detour/turns/status 与改动前逐位一致），而**生产** airspace eligibility 规则未改动，遍历规则仍由 planner 自身 `diagonal_guards` 在执行时保证。效果：`build_pack` 0.4–0.7 s，`build_diagnostics` 57 s → 3 s。

本轮测试：全量 pytest **550 passed, 6 skipped**；Node **38 passed, 0 failed**；`compileall`、JS syntax 与 `git diff --check` 全部通过。`tests/test_route_planning_diagnostics.py` 覆盖 zigzag 定义、八方向直方图与真实 grid path 一致性、方向偏置回归锁、λ sweep 取舍与“无推荐”保证、grid sensitivity、DATA-1/2/3 verdict 与 NOT READY 路径、brief 固定章节/P1–P8/观测事实。

brief 新增 `observed_findings`（OBS-LAMBDA / OBS-GRID / OBS-DIRECTION-BIAS），只陈述冻结 planner 实际观测：λ 增大时用更长路径换更低 risk exposure（λ=0→4，9428.6 m/8477.8 → 10902.9 m/1935.8，4 条不同路径）；网格 L6→L8 路径缩短 1058.9 m、zigzag 降至 0.087 倍；斜向 OD 上 8 邻域只使用 E/NE 两个方向（其余 6 个方向未出现）。这些观测用于支撑 P5/P6/P7，本身不构成结论或推荐。

## 8.4 真实数据可信化与规划输入就绪 V1（本轮）

> **已被 8.8 取代（DATA-3 部分）**：本节描述的是 DATA-3 = AirspacePolicy 仍然 active 的历史状态。自 V3 Real-Data Enablement V1 起 DATA-3 已退役，AirspacePolicy 不再进入 readiness/route eligibility；Data Source Center 与 Step 03 的 AirspacePolicy editor 已删除。其余 DATA-1/2 结论仍然有效。

基线 commit `9f2af8007e1216b02e1d2675f9865dba61e1c153`。本轮只推进 DATA-1/2/3 与来源治理；V1/V2 搜索、lambda、grid adjacency、hard-constraint 模型与所有需专家决策的算法项保持冻结。

- 新增 JSON-safe `SourceAssetManifest/SourceAudit`：稳定 `source_id` 只依赖 role，绝对路径只由本机 `map_sources.json` 解析，manifest 仅保存 `local_path_ref`、文件名、size/mtime、schema/count/extent、declared/confirmed CRS、geometry health、evidence/provenance 和版本验证。普通 snapshot 仅 stat；只有用户点击“验证数据源”才流式计算 SHA-256。
- 已验证文件的 size/mtime 或 SHA 变化会标记 `source_changed/needs_revalidation`，使 reference CRS/米制比较或 airspace policy/eligibility/operational route 沿现有依赖链 stale；不自动重算、不以同名文件代表同版本。
- DATA-1 新增人工 CRS 确认 API/UI：`pyproj.CRS` 只验证用户输入，有 source/evidence 才能保存；需转换时固定 `Transformer.from_crs(source, "OGC:CRS84", always_xy=True)`，保留 source numeric coordinate/path 和转换 provenance。GeoJSON RFC 7946 仍只证明 representation CRS，不自动确认 source CRS。
- DATA-2 新增 converted route `ImportPreview`：报告 route/point 数、有效/无效坐标、航线编号冲突、sequence gap、columns/bounds/warnings/source audit。配置 CSV/XLSX/GeoJSON 不会替换 `reference_routes`，只有用户确认同一 preview fingerprint 才替换；预览后文件改变则拒绝。`.et` 仍只返回 `requires_xlsx_or_csv_conversion`。
- DATA-3 新增 AirspacePolicy editor：逐 feature 展示来源几何事实、geometry health、allowed/blocked/unknown/confirmed/source/evidence；单项或“用户明确勾选”的批量保存都要求 source+evidence。旧 project 中无 evidence 的未修改 policy 可 backfill 保留，但不能因此阻断新单项编辑。仍禁止根据图层名/颜色/样式推断。
- geometry health 为只读检查，记录 null/empty/invalid/unsupported/count/extent/CRS，不 MakeValid。空域几何有无效项时在 intersections/eligibility 前 fail-closed；reference GeoJSON 预览中 blocked geometry 不允许确认导入。
- 统一最小 provenance 字段为 `source_entity / processing_activity / derived_entity / derived_from / method / timestamp / note`；ET→converted 可在预览时记录 conversion method/evidence，但系统不执行 ET 转换。
- Data Source Center 新增 configured/identity/schema/CRS/geometry/version/overall 审计状态及“验证数据源 / 确认 CRS / 预览并导入航线 / 编辑 AirspacePolicy”入口，不显示内部 P 阶段编号。
- `tools/data_readiness_report.py` 生成 ignored `outputs/data_readiness/data_readiness.{json,md}`，只读汇总 source audit/hash/version/CRS/geometry/import/policy/DATA-1/2/3，不造数据、不自动 hash、不猜 CRS、不解析 ET、不确认 policy。

当前本机报告：参考起降点 98 条，geometry health 98/0 null/0 empty/0 invalid/0 unsupported，但 source CRS 待确认；航线配置仍为 `.et`，目录中未发现已转换 CSV/XLSX/GeoJSON，因此 reference routes=0；建筑单体 538,228 个、建筑网格 143,013 个，Shapely 只读拓扑扫描均为 0 null/empty/invalid/unsupported；当前持久化项目尚无新版 airspace geometry health，需重跑工作区网格映射；AirspacePolicy=0，confirmed allowed=0。DATA-1/2/3 全部 blocked，仍需用户提供 CRS 证据、转换 ET 并预览确认、逐 feature 确认 policy。

本轮验证：全量 pytest **566 passed, 6 skipped**；Node **39 passed, 0 failed**；`compileall`、Source Center/Step03 JS syntax 与 `git diff --check` 全部通过。默认 pytest 环境的 `.pytest_cache` 因当前 Windows 中文路径/权限只产生一条 cache warning，不影响测试结果。

## 8.5 Route Planner V3-A：3D 规划合同 + 硬约束内核 + L8 战略搜索（本轮）

基线 commit `7ff52258b5a7d6ca18c0ead751926a79442e4446`。本轮新建**独立** `cns_planner/route_planner_v3` 包与独立实验容器；架构与完整语义见 `docs/route_planner_v3_architecture.md`。

**明确不变（硬边界）**：未修改 V1/V2 搜索核心、代价公式或输出契约；未修改 `spatial_3d` 语义，**V3 高度不写入 `route_altitude_profile`**；未实现 30 m 局部精化、exact polygon/terrain 最终 validator、CNS joint optimization、energy 模型；未把 V3 注册进 `algorithm_registry`（默认 route planner 仍为 `route_planner_v1`）；V3-A 结果**不写入** `operational_routes`。

### 1. 原生 3D + heading 的搜索状态

- `V3State = {grid_id, altitude_index, altitude_egm2008_m, heading_bin, x, y, z}`；`z` 恒等于 `altitude_egm2008_m`，canonical 垂向基准固定 `egm2008_orthometric`（复用既有 P7 语义，不接受 AGL/WGS84 椭球高直接进搜索）。
- `heading_bin`（默认 8 箱）存在的唯一目的是让**转弯成为 Markov 属性**：状态带航向，edge 才能判断这一步的航向变化是否在能力内。
- 航向**永远由运动几何推导**（edge 实际方位角），不由调用方声明；primitive 不含「我转了多少度」字段。
- 起点无历史 ⇒ 起点在**所有** heading bin 上等价合法（代价 0）；终点是 cell（+高度，仅当问题显式给 z），不是 heading。

### 2. Hard / soft 分离

- `HardConstraintEvaluator.state_feasible`：allowed airspace（只有 `confirmed_allowed`）、高度带、terrain clearance、building clearance。
- `HardConstraintEvaluator.transition_feasible`：邻接、转弯弧长、爬升/下降梯度、高度步长。
- **fail-closed**：terrain 缺 floor 或 `data_status != passed` ⇒ blocked；building `data_status != passed` ⇒ blocked（`data_status=passed` 且无 clearance 表示**确认无建筑约束**）；airspace unknown/restricted 都不可行。
- `HardConstraintAudit` 以稳定 reason id 聚合**去重后的** state/edge 拒绝数（每 reason 至多 200 样本），并另给与搜索展开无关的 `environment_evidence`（cell 级证据缺口计数）。
- soft cost 永远输出**向量**：`distance / population_risk / traffic_risk / building_exposure / energy`，每项保存 `raw/normalized/weight/contribution/unit/source/semantics/enabled/status`。不同单位不直接相加；**只有显式 normalization + 非负 weight 的项**进入 scalar cost；`energy` 固定 `pending_model`/disabled（不发明公式）；CNS 记录 `integration_mode=post_route_assessment` 且 `excluded_from_search_cost=true`。

### 3. 无默认安全值

- 无默认值参数：`min/max_altitude_egm2008_m`、`vertical_step_m`、`terrain_clearance_m`、`building_horizontal/vertical_clearance_m`、`aircraft_min_turn_radius_m`、`max_climb_gradient`、`max_descent_gradient`。缺任一 ⇒ readiness blocked ⇒ 返回 `not_ready`，**不运行搜索**。
- 爬升/下降：有 `max_climb_gradient`/`max_descent_gradient` 可直接判 edge；只有 `max_climb_rate_mps` 时**必须**同时有 explicit `planning_speed_mps` 才能换算，否则 kinematic readiness blocked（禁止猜速度、禁止借用 Aircraft cruise speed）。换算来源逐项记录在 `climb_gradient_source/descent_gradient_source/planning_speed_source`。
- readiness 六域 `airspace / terrain / building / policy / aircraft / cost_model` 各自 `ready|pending|blocked` + reasons；两者都阻止运行。

### 4. 运动 primitive 与 edge 生成

- `MotionPrimitiveProvider`：每 state 8 水平 + 2 纯垂直 + 16 水平×高度 = 26 个 primitive；`TransitionValidator` 在 **edge 生成阶段**判定转弯/爬升/下降，而不是路径生成后检查。
- 转弯半径采用显式弧长解释：`R·|dψ|` 与该 edge 水平步长比较；纯垂直过渡与「无水平位移却要改变航向」直接不可行。
- 运动模型固定声明 `engineering_3d_motion_primitives_v3a` / `engineering_kinematic_limits_implemented_in_edge_generation_not_a_flight_dynamics_certification_model`（**不是**飞行动力学认证模型；无 Dubins/bank/风/能量）。
- 每个 edge 记录 `primitive_id/length_m/climb_gradient/heading_change_deg`；测地边长与方位角每个环境只算一次并缓存，结果与展开量无关。

### 5. L8 战略搜索

- `V3StrategicPlanner` 在 L8 网格 × 显式高度层 × heading bin 上跑**确定性 A***（`(f, g, insertion_order, state)` 排序，等代价按 state 字典序选父）。
- 启发函数：**3D 几何下界** `h = 3D geodesic distance × (1 + Σ enabled soft weights)`。可采纳性由「normalized penalty ∈ [0,1]/m + weight 非负 + soft weight 之和被限制在 1.0」保证；超出即 `blocked`，不静默允许。
- 统计完整记录 `expanded_states/generated_states/expanded_transitions/runtime_ms/expansion_cap/expansion_cap_reached/state_space_shape`；达 `max_expanded_states` 返回 `failed`，不声称成功。
- 结果状态只允许 `strategic_candidate / failed / missing_data / pending_confirmation / not_ready`；normalizer 强制 `operational_route=false`、`final_validation_performed=false`，并携带固定 `disclaimer`。

### 6. CandidateRefinementCorridor 与三阶段

- 由粗 state path 生成 `center_grid_ids` + 可配置 Chebyshev N-ring `support_grid_ids` + `altitude_envelope`；语义固定 `refinement_search_window_not_safety_corridor`，`n_ring_is_not_a_safety_clearance=true`，`ring_n=0` 时 support == center。
- **N-ring 不是安全净空值**；`altitude_envelope` 只是计划高度包络（`planned_altitude_extent_not_a_clearance_volume`）；`refinement_cell_size_m` 由调用方显式给出（通常 30 m），V3-A 不使用它做计算。
- 三阶段固定为 `L8 战略（现行） → 30 m 局部精化（V3-B 未实现） → exact polygon/terrain 最终判定（V3-B 未实现）`。

### 7. CNS 与经典 RCSP 的区别

- CNS 第一阶段**不进搜索**：`Route Planning → CNS Assessment`，`next_stage = V3-D_CNS_joint_optimization`。
- V3-A 是 **hard-feasibility + additive soft cost**，不是 RCSP：硬约束只做可行/不可行两值判定（不累积资源），soft 只累加为显式加权标量；转弯/爬升是**单步转移可行性**而非可累积资源；本阶段不产出 Pareto 前沿，也不声称最优性。区别与理由见架构文档第 8 节。

### 8. 隔离、容器与接口

- V3 结果独立保存于 additive `route_planner_v3_experiments`（记录/环境 spec/policy/result/provenance/verdicts）与 `v3_planning_policy`；**不写 `operational_routes`**、不切换 `algorithm_selection`、不改 `spatial_3d`。写 V3 容器不产生任何 stale 传播。
- 环境只接受 canonical `V3CellEnvironment`；算法层不 import QGIS/GDAL、不读文件。V3-A **无真实数据 adapter**：`real_data_readiness.status=blocked` + `adapter_status=not_implemented_v3a` + `required_before_real_run` 列表。
- V3-A 只对 canonical/synthetic 环境运行；合成环境的 terrain/building 值全部来自显式 spec（不从文件或图层推断），`source_type=synthetic`。
- API：`GET /api/route-planner-v3-experiments`、`GET /api/route-planner-v3/readiness`、`POST /api/route-planner-v3/policy`、`POST /api/route-planner-v3-experiments/evaluate|delete`。Workflow snapshot 只带**有界摘要**（`record_summary`）+ `detail_endpoint`，完整 state path 走 detail 接口。
- Step 03 新增**只读/实验面板**：架构、readiness、显式参数、strategic candidate、3D state 数、hard-constraint rejection 统计、cost breakdown、corridor；地图新增 V3-A candidate **2D 投影** 图层开关（corridor 画成搜索窗口，高度/heading 只在详情显示），本轮不做复杂 3D 渲染。

### 9. 本轮验证

- 新增 `tests/test_route_planner_v3.py`（55 项：契约 JSON-safe/versioned、无默认安全参数、rate→gradient 需 explicit speed、energy 不可启用、soft weight 边界、环境契约、结果语义强制、primitive/turn/gradient 判定、state/transition 分离、fail-closed、open 3D / required climb / terrain barrier / building obstacle / altitude floor·ceiling / turn radius / climb·descent rejection / airspace blocked / unknown 数据 / soft tradeoff / invalid policy / 确定性 fingerprint / corridor N-ring / 绝不称 final / V1 V2 特征不变）。
- 新增 `tests/test_route_planner_v3_service.py`（24 项：容器 backfill 与保存恢复、evaluation 不写 `operational_routes`/不切 planner/不改 `spatial_3d`、项目网格不被替换、实验身份确定性、删除、unready policy 拒绝、policy 持久化与 per-run 完整覆盖、corridor 参数、scenario route 绑定、readiness 六域与 rate-only blocked、snapshot 摘要边界、API 五个端点、未注册进 Registry）。
- `tests/frontend_modules.test.mjs` 新增 4 项（V3 panel model 的 candidate 实验语义/高度·heading/cost 向量、readiness 六域与 adapter verdict、panel 文案与 disclaimer、地图 overlay 只投影 2D candidate + refinement window）。
- `main.js` 去掉空行后仍满足 `test_architecture.py` 的「入口保持 thin」约束（≤450 行）。

本轮完整结果：全量 pytest **645 passed, 6 skipped**（6 项跳过仍是 `tests/test_map_http.py` 的真实 QGIS HTTP 集成，既有 `QgsSpatialIndex` 测试替身问题已在前序阶段修复，本轮无 known failure）；Node 前端 **43 passed, 0 failed**；`python -m compileall cns_planner tools`、`app.js/main.js/step03_routes.js/route_planner_v3_overlay.js/grid_theme.js/tiles.js` 语法检查与 `git diff --check` 全部通过。

> 本机 DSH 沙箱提示：workspace-write 模式下 pytest 的 `tmp_path`/`--basetemp` 会被拒绝 ACL（`PermissionError: [WinError 5]`），Node `--test` 子进程 piped stdio 会被拒（`EPERM`）。本轮全量 pytest 与 Node 测试使用系统 temp `--basetemp` 并在放宽沙箱下执行；这不是代码缺陷。

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
6.1 航路实验（`route_planning_experiments`）的 runtime 是单进程 wall-clock，不是跨机器性能基准；同名实验重跑会更新 `created_at`（身份不变），当前不保留历史重复运行明细。reference comparison 为读取时派生、不持久化，因此其脚本/证据只在 experiment 记录存在时可复现。
6.2 reference 数据的 `source_crs` 目前**全部**为 `pending_confirmation`（含舟山源表），所以 reference `length_m` 与米制相似度默认禁用；AirspacePolicy 为空，V2 readiness 保持 `blocked`。这些是数据/人工确认缺口，不是代码缺陷。
7. RiskModelV1 已优先使用 canonical `population_density_people_km2`；`value_mean` 仅为旧项目兼容 fallback，并以 `raw_semantics=legacy_source_value_mean` 明示。当前仍是相对工程指数，不是绝对人口风险。
8. 数据源产品契约已确认，但当前具体 GeoTIFF 文件身份仍记录为 `configured_assumption`；尚未通过 checksum/manifest 验证其确为对应 WorldPop/GLO-30 产品。
9. 原 M7 `ConflictDetector` 仍是兼容的二维恒速直线 CPA；新增 EncounterAssessment3DV1 处理显式 EGM2008 三维轨迹与工程阈值，但位置/速度 uncertainty 在 V1 只记录、不膨胀阈值，也不评价正式法规 well-clear。
10. CSS 已按加载职责拆分，但 `base.css` 保留历史压缩规则；未来视觉改版时再格式化和去重，避免本轮改变级联结果。
11. WorldPop SourceProfile 保留兼容字段 `quantity=population_count_per_source_pixel`、`unit=person/source_pixel`，并新增 `support=source_pixel` / `source_semantics=people_per_pixel` 与 canonical target quantities；不得把 partial 未覆盖区当作零人口或进行外推。

12. `QgsSpatialIndex` 同时兼容真实 QGIS 的空构造+`addFeature` 与轻量测试替身的 features 构造；当前全量 pytest 无失败。真实 QGIS HTTP/GUI 集成仍由默认测试环境跳过。
13. `OperationService` 为保持旧工作流/API 语义，仍在顶层 `aircraft` 输出 legacy `lambda_per_hour=1/mtbf_h`；Step 4 已标注其不是 P4 ReliabilitySpec 推断。新安全计算只能使用显式声明模型的分系统 ReliabilitySpec。

14. P6 只支持由离散 EventObservation 驱动的定性 C+S/C+N/N+S 功能耦合；尚无完整航路 ServiceTimeline、耦合概率、common-cause、BN/DBN/Petri、FTA 图形编辑、认证工作流或正式 safety objective 校核。

15. P8 仅提供静态技术能力工程基线：尚未实现 P.526/Fresnel/terrain diffraction、3GPP SINR/channel、GNSS constellation/DOP/RAIM、radar equation/Pd curve 或 SitePlanner。

16. P9 时间线仍只支持恒定地速和显式离散场景；保护包络仍是代数工程基线。DAA V1 可对给定转弯/水平加速度/垂直速度 command 做简化运动学重评估，但不自动求解动作，不含正式 Well-Clear、DAA Detection Volume、风场安全余度或 Monte Carlo。

17. GapV2 是上游证据的保守区间合并，不做传播/性能/ServiceState 重算，不把 unknown 当 gap，也不触发 SafetyEvent；protection margin 仅支持已有 confirmed 监视探测距离的工程差值，尚未形成 GapV2→Safety/站址方案闭环。

18. P11 是单 action 正收益的 reuse-first 提案器，不求解多 action 联合后才能满足的独立冗余，不含风险/人口/severity 权重，也不是费用优化器。P12 可组合 apply 并重跑 P7-P10，但结果仍受 P7/P8 工程模型与输入证据完整度约束。
19. P12 没有项目级 audit log、撤销已提交 application 或多方案分支合并；事务边界依赖当前单项目单进程 repository 原子写入。真实数据验证、并发提交控制和人工审批流留待后续阶段。
20. P13 是 MH/T cell-centroid 的二维八邻域 A*；没有 Theta*/LOS smoothing、连续空间最短路、动态/四维风险或高度相关风险。硬约束仍来自现有 bbox 输入，风险仍受 RiskModelV1 相对指数和数据完整度限制。
21. P14 使用保守 cell 纳入、代表性 voxel probe 和离散 volume proxy；不是精确 buffer/mesh，也不保证 voxel 全体满足。尚未评估走廊冗余、韧性、runtime outage、真实传播或法规空间合规。
22. P15 只做静态空间 service/redundancy 缺口与显式目标判定；不建模 common-cause、共享供电/回传、塔站失效传播或概率 continuity/availability。共址关系当前既不奖励也不惩罚。
23. P16 仍是离散 voxel/volume-proxy 的 proposal baseline；没有真实传播、设施施工约束、异构成本归一、全局整数优化、共因失效或 proposal apply/rollback。最终方案仍需用户确认与后续应用闭环。
24. P17 Policy Engine 只支持显式项目规则、白名单字段和无优先级确定合并；尚无权威 policy catalog 签名/版本治理、法规适用性法律判断、复杂逻辑或审批审计。Recommendation 只能由用户确认后采用。
25. P18 目前提供单项目、单进程的人工 Variant 审查与原子 Apply；尚无多人审批签名、撤销已应用计划、持久化 audit event stream 或跨进程并发提交锁。Comparison Matrix 有意不提供自动综合评分/排名。
26. P19 PDF 依赖本机 Playwright Chromium，未安装时正式生成会原子失败并返回可操作提示；当前报告 checksum manifest 不等同完整 BagIt、数字签名或不可抵赖审计，HTML/SVG 地图也仅为无底图工程示意。
27. 舟山起降点源表未明确 CRS，当前 `[lon, lat]` 只按源数值临时展示并保持 `pending_confirmation`；正式空间分析前必须获得 CRS 证据。两份 `.et` 需人工转换为 XLSX/CSV；5GA/低空智联网资料的厂商（包括是否为“54所”）仍待来源确认，不得猜测。
28. RouteVerticalProfileV1 是显示用离散采样，不是新的净空裁决器；真实剖面仍依赖 passed operational route、confirmed 高度剖面、带明确 EGM2008 元数据的 FABDEM 与当前有效 BuildingClearanceV1。QGIS GUI/HTTP 真实航路 hover 与建筑区间需在正常 QGIS 启动器进程验收。
29. EncounterAssessment3DV1 的局部 ENU 与分段线性插值适用于短距离工程仿真；ManeuverCommand 是简化运动学且只验证显式能力上限。当前项目未配置真实 confirmed encounter tracks/policy/capability/command，因此默认结果保持 `not_calculated / NO_TRAFFIC`；不得将 synthetic 测试的 `CLEARED` 视为真实运行安全结论。
30. V3-A 只在 L8 网格分辨率与显式离散高度层上判定硬约束可行性，**没有** 30 m 局部精化与 exact polygon/terrain 最终判定；运动模型是工程运动学基线（无 bank/风/能量，非适航认证模型）；空域自 V3 Real-Data Enablement V1 起是 `display_only_reference_layer`，**不参与**可行性判定（legacy `airspace.status` 只作显示，硬约束仅 terrain clearance、building clearance、altitude bounds、turn、climb/descent）；环境支持 canonical synthetic 与 injected `configured_real_sources`；soft cost 只做报告、不产出 Pareto 前沿，也不声称最优性；endpoint 绑定最近 cell 中心而非精确多边形包含判定。以上限制随结果 `disclaimer` 一起交付，禁止把 V3-A 结果读作 final safe / operational route。
31. V3-A `max_expanded_states` 是显式工程上限：达到上限返回 `search_incomplete`（resource limited，不是 failed/infeasible）而不是「尽量近似」；L8×高度×heading 的朴素状态空间随工作区线性增长（324 cell × 7 高度 × 8 heading ≈ 1.8 万状态），大工作区需要重新评估上限与索引策略，本轮未做性能优化承诺。

32. V3-C 只保证 **C1（position + heading）连续**：曲率在 straight↔arc 处可跳变，`continuous_curvature=false`/`c2=false`，clothoid 未实现（future）。vector predicate 对 **linearized representation（含显式 `curve_chord_error_m` envelope）** 精确，**不对数学曲线**；terrain 是 **native-raster evidence**，不声称真实世界地形数学连续精确。building 仍是 LoD1 prism（ground + 预测 height）语义；kinematics 仍是工程基线（无 bank/风/能量/飞行动力学）。圆弧 linearization 的 `curve_chord_error_m` 与 `max_validation_samples`/`max_runtime_s` 都是显式工程参数，缺一即 blocked/not_ready 或 validation_incomplete。

33. V3-C `validated_route` **不是** operational route（`operational_route=false`、`cns_assessed=false` 无条件强制），也不自动修复/自动 replan。**V3-D 已实现** operational adoption + 既有 P7/P8/P9/P10 CNS Assessment bridge；但 `operational_route`/`cns_assessed` 仍**只在 V3-C validation 内恒为 false**（历史 immutable），"已采用"与"CNS 已评估"的事实读取自 `v3_operational_adoptions` 与 `v3_cns_assessment_bundle`。Route–CNS 联合优化（CNS 进入 cost/约束）继续留在 future backlog。

34. V3-C/V3-D 的真实数据路径仍依赖本机 QGIS/GDAL 与已确认来源：`NativeTerrainWindowSource` 需要带 `egm2008_orthometric` 元数据的 FABDEM、`RouteCorridorBuildingSource` 需要带 spatial index 的 buildings GeoPackage。自 V3 Real-Data Enablement V1 起 V3-C **不再**要求 confirmed AirspacePolicy（`ConfirmedAirspacePolicySource` 保留为 legacy 兼容类，不再进入证据链）：`airspace` domain 固定返回 `status=skipped / applicability=not_applicable / reason=display_only_airspace_not_used_for_route_constraints`，active domains 仅 geometry/terrain/building/altitude/kinematics。当前自动恢复项目无 current V3-B refined candidate 与 confirmed V3-C policy，因此 V3-C 真实结果为 `not_ready`、V3-D production gate 为 `configured_real_source_chain_not_ready`，不是代码缺陷；V3-C/V3-D 的 QGIS/GDAL 端到端仍需在正常 QGIS 启动器进程手工验收。

35. V3-C 的 airspace/building geometry 层依赖 Shapely（buffer/intersection/within/covers）；缺失时相关 domain 报 `unresolved` 而不是伪造通过。terrain pixel interval 与 route 几何是纯 Python，不依赖 Shapely。Shapely `quad_segs` 只影响 cap 的圆弧离散，buffer offset 距离本身 exact；该参数已随 domain evidence 记录。

36. V3-D production Apply 需要 **real configured source chain ready**（`real_data_readiness().v3c.status == ready`）。该判定读取本机 `map_sources.json` 解析出的路径、terrain vertical reference 与 building/building_grid source audit；**不再**读取 AirspacePolicy。`airspace` 无论存在与否都返回 `not_applicable/display_only`，因此 production gate 不会因适飞空域缺失或变化而 blocked/stale。仓库与测试**不硬编码**本机路径。`V3OperationalAdoptionService.require_production_sources` 是**仅本机 harness 可用**的测试 seam（构造函数参数，**不可由 HTTP payload 设置**），生产部署保持默认 `true`。测试用注入的 affine transform 代替 pyproj/QGIS（接口为 `to_geographic` + `describe`）；真实路径用 `_library_transform`（pyproj `Transformer.from_crs(authority, "OGC:CRS84", always_xy=True)`），CRS 不可解析即 fail-closed 为 `crs_transform_unavailable`。

37. V3-D 的 `route_altitude_profile` 距离基准是 published 二维 path 的**累计基线距离**（`cumulative_2d_baseline_path_distance_matching_path_vertex_order`），与 meter 无关；`v3_metric_length_m`/`legacy_geodesic_length_m` 都是米，`length_delta_m` 记录表示差异供审计。**不要**把 `profile_length_m` 当作米去和 `v3_metric_length_m` 比较——两者基准不同且已在 `path_metrics.length_semantics` 中写明。

38. V3-D CNS bridge 的**完整度**只看 requested stages 是否都运行且必选分系统都有确定判定；**需求满足度**单独由 required 分系统的 P7/P8/P9/P10 route 级状态推导。项目**未要求**的分系统可以合法 `missing_data`（例如只要求 C 时的 N/S），不会让评估 incomplete，也不会污染 verdict；反之缺 evidence 的必选分系统 ⇒ incomplete + blocking reason。P8 在有单一已确认地面提供者但缺 provider independence 证据时仍返回 evidence-limited 状态（非 meets），这是既有 fail-closed 语义，V3-D 不改写它。


## 11. 下一阶段计划

1. 确认舟山起降点/航线坐标 CRS，将“区县航线统计表（包括企业）总表260304.et”或权威“舟山16条航线点位核对表”转换为 XLSX/CSV/GeoJSON，并补齐 5GA/低空智联网资料的明确厂商来源证据；确认前保持 reference-only/unknown。**不再**需要逐 feature 确认 AirspacePolicy（DATA-3 已退役）。
2. V3-D 已实现（validated route → operational adoption → 复用既有 P7/P8/P9/P10 CNS Assessment；`operational_route`/`cns_assessed` 在 V3-C validation 内仍恒为 false，"已采用/CNS 已评估"读取自 adoption/bundle 容器）。下一步是在正常 QGIS 启动器进程中用**真实舟山来源**做 V3-D 端到端验收（见下节"仍需真实端到端验证的问题"）。
3. 真实数据 canonical adapter（terrain surface clearance floor、building required vertical clearance、grid risk soft fields）与来源审计在 V3-A/V3-B/V3-C 已实现（GIS 边界，`cns_planner/gis/v3_environment_adapter.py`），V3-D 的 operational adoption 与 CNS bridge 亦已就绪；仍需在正常 QGIS 启动器进程手工验收真实航路的 V3-A/V3-C/V3-D 端到端结果。空域不进入该链路。
4. V3 后续（非 V3-D）：clothoid / continuous-curvature 过渡；Route–CNS 联合优化（CNS 进入 cost/约束）；energy 模型——三者都必须先用显式 policy 定义归一化与权重，禁止静默进入 cost。
5. 完成 synthetic/manual end-to-end validation，验证从需求推荐、三维走廊、冗余目标、站址提案、人工确认/应用到 P19 交付包的完整闭环。
6. P20：Synthetic Data Generator，为可复现端到端场景提供显式模拟数据与来源标记。
7. 设计 GapV2 到 P5/P6 Safety Event 的显式、可确认映射，仍禁止 Gap 自动等同 SafetyEvent。
8. 在正常 QGIS 桌面启动器进程中手工验证真实航路的建筑 polygon/DTM mask 净空结果与 V3-C native-raster 验证，确认项目水平/垂直阈值来源；财产/基础设施仍待后续真实映射。
9. 补 ApplicationContext 并发事务、schema migrations、项目 manifest/audit、application rollback 和真实 QGIS 集成 CI/验收脚本。
