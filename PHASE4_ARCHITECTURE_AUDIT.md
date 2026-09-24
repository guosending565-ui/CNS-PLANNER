# PHASE4-A 架构审计报告：从「多历史版本共存」收敛为 production CNS 规划产品

| 项目 | 值 |
|---|---|
| 项目根 | `C:\Users\yiding\Documents\ChatGPT\CNS规划系统` |
| 审计基线 HEAD | `82b5b09da310e2332bc51a7890180eceb0de113c`（`82b5b09`，`fix: render radar sectors at physical coverage range`） |
| 分支 | `main`（跟踪 `origin/main`，无领先/落后） |
| 报告类型 | 只读架构审计（Phase4-A）。**未修改任何被审计代码，未 commit/push/reset，未继续 P14 优化。** |
| 工作树其他 AI 修改 | 记录而不触碰（见 §0.3） |
| 审计方法 | 模块 → imports → Application service → API 路由 → ProjectState 键 → Workflow snapshot → Frontend → Tests 八点交叉验证；未经引用图谱验证的模块一律不下"可删除"结论 |

---

## 0. 审计口径与只读声明

### 0.1 分类词表（本报告统一使用）

| 分类 | 判据 |
|---|---|
| **KEEP_PRODUCTION** | 位于目标正式业务链上，且满足"有 Application 写入者 + 有 API 或主链调用 + 有持久化容器 + 有测试"中的至少三项；即使无独立 API/前端，只要被生产链唯一依赖也判 KEEP |
| **MERGE** | 有实现价值，但与另一模块职责重叠或仅为兼容转发；应合并进保留方，合并后删除原文件 |
| **OPTIONAL** | 功能完整且可运行，但不在正式主链默认路径上；用户显式选择才参与，允许长期保留 |
| **ADVANCED** | 实验/进阶能力，完整实现且通常有 API+前端+测试，但**不属于**正式主链；应在 UI 与文档中显式隔离 |
| **ARCHIVE** | 已被新实现取代，但仍被注册表/服务兜底/前端分流/已保存项目引用；应冻结为"只读兼容源"，停止新增功能 |
| **DELETE_CANDIDATE** | 无 API、无前端、无持久化、无生产 import（或仅空壳契约 / 仅被测试的同义性断言引用）；删除前必须同步测试 |

### 0.2 readiness 档位口径（Phase4 目标词表）

用户目标链要求三档缺失行为：`blocked` / `ready_with_assumptions` / `not_evaluated`。

**实测结论：当前代码库中 `ready_with_assumptions` 字符串命中数为 0**（全 `*.py` grep）。现有 readiness 实际只有 `ready` / `partial` / `blocked` / `not_calculated`、以及 `not_ready`（主链 planner 专用）四个状态词。"可假设"的语义目前散落在各 result 容器的 `assumptions` / `confirmed=false` / `assumable` 字段里，没有统一档位。这是 Phase4-B 必须补齐的第一项产品语义。

### 0.3 工作树现状（记录，不触碰）

审计开始时与结束时 `git status --porcelain=v1` 完全一致：

```
 M cns_planner/algorithms/corridor/v1.py
 M cns_planner/algorithms/coverage/geometric_3d.py
 M cns_planner/algorithms/service_capability/v1.py
 M tests/test_cns_corridor.py
?? _dsh_prev_corridor.diff
?? _dsh_prev_others.diff
?? _dsh_prof/
?? "ersyidingDocumentsChatGPTCNS规划系统"
?? qgis_polygon_inventory.json
```

- 上述 4 个已修改文件为**其他 AI 的工作成果**，本审计只读取其内容，未做任何回退或续改。
- `_dsh_prof/` 是包含一份完整源码副本（`_dsh_prof/ref/cns_planner/...`，363 文件 / ≈9.99 MB）的临时目录；`_dsh_*.diff`、`qgis_polygon_inventory.json`、`ersyiding...` 为临时产物。
- 工作区另有约 30 个 `_*` / `tmp*` / `pytest-*` 临时目录（部分因权限无法枚举）。这些属于**仓库卫生问题**，不进入分类表，列入 §9 阶段 0。

---

## 1. 当前系统功能地图

### 1.1 分层架构（实测，与 `AI_DEV_CONTEXT.md` §1 一致）

```text
map_app.py / app.py / launcher_process.py
  → cns_planner.map_server
  → application.ApplicationContext          (app_context.py, 40 KB, 唯一运行上下文)
      ├─ api/            HTTP 分发（router.py 单一 ApiRouter.get/post 大分发，无 per-route handler）
      ├─ application/    47 个模块：用例编排 + 状态 + 失效
      ├─ domain/         57 个模块：schema-v2 数据契约与语义
      ├─ gis/            21 个模块：QGIS/GDAL/CRS/渲染/空间适配
      ├─ data/           Registry + Health + grid_id 映射
      ├─ reference_data/ 只读来源事实（与规划输入隔离）
      ├─ algorithms/     13 个 algorithm_type 的注册实现
      ├─ risk/ safety/ simulation/ gap/ site_planner/
      ├─ route_planner/ layered_route_planner/ route_planner_v3/
      ├─ persistence/    原子 JSON + 内容寻址 sidecar
      └─ web/            原生 ES Modules 前端（无构建步骤）
```

依赖方向 `API → Application → Domain/Algorithm` 在实测中基本成立。**唯一的例外**：`api/router.py:7-10` 直接 import 4 个 `safety/*` 模块并在路由内联调用（见 §3.4、§4.4）。

### 1.2 模块功能地图（按目录，仅列有产品含义者）

| 层 | 模块组 | 功能 | 目标链归属 |
|---|---|---|---|
| api | `router.py`（212 条路由）、`server.py`（含 `X-CNS-Revision` 乐观并发 409）、`file_browser.py`、`security.py` | HTTP 传输与静态资源 | 全链 |
| application | `app_context.py` | QGIS 运行时、项目会话、全部 GIS 适配注入 | 全链 |
| application | `workflow_service.py`（1424 行 / 85 KB） | 六步 facade + snapshot 投影（slim 策略）+ 服务装配（38 个 service） | 全链 |
| application | `project_state.py`（728 行） | schema-v2 空状态、回填、规范化、legacy 迁移（reuse tier） | 全链 |
| application | `invalidation_service.py`（641 行） | 唯一失效 authority（43 个定向方法 + 注入式 invalidator） | 全链 |
| application | `workspace_service` / `route_service` / `risk_service` / `risk_v2_service` / `cns_input_service` | 工作区、场景/运行航路、Risk V1/V2、CNS 五类输入 | 步 1–3 |
| application | `layered_route_planner_service`(65 KB) / `route_risk_profile_service`(20 KB) / `layered_route_validation_service`(29 KB) / `layered_operational_adoption_service`(26 KB) / `route_3d_profile_service`(50 KB) / `vertical_transition_validation_service`(51 KB) / `route_safety_evidence_service`(70 KB) | **正式航路主链的 7 个阶段服务** | **步 3 主链** |
| application | `spatial_3d_service` / `route_operating_layer_service` / `cns_service_capability_service` / `operational_timing_service` / `building_clearance_service` / `route_vertical_profile_service` | 三维几何覆盖、巡航高度层与程序、静态服务能力、时间线/保护、建筑净空、剖面采样 | 步 3–5 |
| application | `corridor_service` / `corridor_gap_service` / `corridor_site_planning_service` / `site_planning_service` / `closed_loop_service` / `plan_review_service` / `report_service` / `export_service` | 服务走廊 → 走廊缺口 → 站址提案 → 闭环复核 → 方案评审 → 报告导出 | 步 5–6 |
| application | `requirement_recommendation_service` / `safety_policy_service` / `tower_obstacle_service` / `radar_surveillance_layout_service`(99 KB) / `encounter_3d_service` / `route_experiment_service` / `route_planner_v3_service`(91 KB) / `v3_operational_adoption_service`(82 KB) | CNS 需求建议、安全策略、铁塔派生层、雷达划设、DAA、规划实验、V3-A/B/C、V3-D | 步 4–5 + 实验区 |
| application | `reference_data_service` / `reference_link_service` / `source_audit_service` / `project_directory_service` / `project_service` / `operation_service` | 参考数据、就绪诊断、来源审计、项目目录、运行规则 | 步 1–2 |
| domain | 57 个契约模块 | schema-v2 数据契约、语义边界（AGL/EGM2008/NoData/fail-closed）、指纹与 normalizer | 全链 |
| algorithms | `registry.py`（19 条 manifest / 13 个 type） | 精确注册、查询、实例化、默认选择与归一化 | 全链治理 |
| risk | `v1.py`（Risk V1 相对指数）、`model_v2.py`+`factors_v2`+`domains_v2`+`normalization`+`accessors_v2`（Risk Framework V2）、`route_exposure.py`、`route_profile.py` | 相对工程风险、因子→域分层、航路风险画像计算 | 步 2–3 |
| safety | `service_state.py` / `event_evaluator.py` / `fault_tree.py` / `coupling.py` / `reliability.py` | ServiceState、FHA/FTA/FMEA、C/N/S 功能耦合、可靠性（**未接入产品**） | 步 4 |
| simulation | `traffic_simulator.py` / `conflict_detector.py` | 可复现多机轨迹、二维恒速 CPA | 步 2 |
| gap | `v1.py`（1-D 航路长度缺口）、`v2.py`（P7+P8+P9 保守区间合并） | CNS 缺口分析 | 步 5 |
| site_planner | `reuse_first_v1.py`（planning-segment 目标）、`corridor_reuse_first_v2.py`（P15 voxel 目标，Application 重跑 what-if） | reuse-first 站址提案 | 步 5 |
| layered_route_planner | `planner.py`（V1 A\*）、`theta_star_v2.py`（88 KB，默认）、`supercover.py` | **正式航路主链的规划核心** | **步 3 主链** |
| route_planner | `risk_aware_v2.py`（含被全家族复用的 `GridGraph`） | 二维风险感知 A\* | 步 3 legacy |
| route_planner_v3 | 24 个文件，V3-A 战略 / V3-B corridor 精化 / V3-C 连续几何验证 / V3-D 运营采纳 | 连续几何 3D 规划链 | 实验区（V3-D 有生产写入语义） |
| persistence | `project_repository`（原子写 + sha256）、`project_compaction`（内容寻址 sidecar）、`data_source_repository` | 可靠 I/O | 全链 |
| services | 11 个极薄兼容 facade（`invalidation.py` 除外，它承载真实 `DEPENDENTS` 依赖图） | 旧导入路径兼容 | 全链（技术债） |
| legacy | `project_v1.py` / `storage.py` / `ui_app.py` + 根目录 `legacy_app.py` | Streamlit/schema-v1 原型 | 已隔离 |
| web/js | `main.js`(35 KB) + `shell.js` + 6 个 step（合计 ≈418 KB）+ 18 个 workflow 子模块 + 22 个 map 模块 + css | 六步工作台 + 地图 | 全链 |

### 1.3 六步业务流现状（`index.html:33-38` + 各 step `shell()`）

| # | 标题 | rail-note | 模块 | 规模 |
|---|---|---|---|---|
| 01 | 项目准备 | 项目、路径与算法选择 | `step01_project.js:61` | 6.5 KB |
| 02 | 环境建模 | 工作区、网格与风险框架 | `step02_workspace.js:591` | 40 KB |
| 03 | 航路规划 | OD、分层候选与运行航路 | `step03_routes.js:1740` | **165.5 KB / 1907 行** |
| 04 | 运行规则 | 能力、需求与运行约束 | `step04_operation.js:264` | 41 KB |
| 05 | CNS规划 | 覆盖、缺口与布站提案 | `step05_cns.js:428` | 52 KB |
| 06 | 方案评审 | 比较、确认与报告 | `step06_review.js:451` | 36 KB |

每步固定三个一级标签：**操作 / 结果 / 高级**（`workbench.js:30-34`）。**不存在第 7 步**（全量 grep `data-step` / `setStep(` / `Step07` 均只命中 1–6）。

隐藏/实验入口（不在左侧导航中）：顶栏导出与报告菜单（`index.html:16`，与 step06 重复）、CNS 图层组（`index.html:81`，`currentStep>=5` 才显示）、V3-A 战略候选与 coarse 掩码图层（`index.html:74-76`，默认关闭）、step03 高级区并列的两套实验面板。

### 1.4 算法注册表现状（`algorithms/registry.py`）

`ALGORITHM_TYPES` 13 项：`risk_model`、`route_planner`、`layered_route_planner`、`coverage_planner`、`cns_gap_analyzer`、`coverage_model`、`service_model`、`timeline_model`、`protection_model`、`site_planner`、`corridor_model`、`corridor_gap_analyzer`、`requirement_model`。

| algorithm_type | 注册实现 | 版本 | 默认 selection |
|---|---|---|---|
| risk_model | RiskModelV1 | 1.1 | ✅ |
| route_planner | RoutePlannerV1 | 1.0 | ✅ |
| route_planner | RiskAwareRoutePlannerV2 | 2.0 | — |
| layered_route_planner | LayeredRoutePlannerV1 | 1.0 | —（legacy/baseline） |
| layered_route_planner | **LayeredRiskAwareThetaStarV2** | 2.0 | ✅ |
| coverage_planner | CoveragePlannerV1 | 1.0 | ✅（maturity = `demo`） |
| cns_gap_analyzer | CNSGapAnalyzerV1 | 1.0 | ✅ |
| cns_gap_analyzer | CNSGapAnalyzerV2 | 2.0 | — |
| coverage_model | GeometricCoverage3DV1 | 1.0 | ✅ |
| service_model | CNSServiceCapabilityV1 | 1.0 | ✅ |
| timeline_model | RouteServiceTimelineV1 | 1.0 | ✅ |
| protection_model | TacticalProtectionEnvelopeV1 | 1.0 | ✅ |
| site_planner | ReuseFirstSitePlannerV1 | 1.0 | ✅ |
| site_planner | CorridorReuseFirstSitePlannerV2 | 2.0 | — |
| corridor_model | CNSServiceCorridorV1 | 1.0 | ✅ |
| corridor_gap_analyzer | CNSCorridorGapAnalyzerV1 | 1.0 | ✅ |
| requirement_model | ManualRequiredCNSV1 | 1.0 | ✅（maturity = `stable_compatibility`） |
| requirement_model | OperationalContextRequiredCNSV2 | 2.0 | — |

**双轨治理事实（重要）**：

1. **6 类算法完全游离注册表**，只能硬编码在 `workflow_service.py` 中：`GridRiskModelV2`(:363)、`BuildingClearanceV1`(:468)、`EncounterAssessment3DV1`(:474)、`DAAEventStateMachineV1`(由 v1 内部使用)、`radar_surveillance_layout`(:383)、`RouteVerticalProfileV1`(:471)。它们不出现在 `GET /api/algorithms`，无法通过 `/api/algorithms/select` 切换，版本升级也无法走 manifest 契约。
2. **前端算法下拉只有 9/13 个 type**（`step01_project.js:12`），缺 `layered_route_planner`、`site_planner`、`corridor_model`、`corridor_gap_analyzer` 四类——即分层规划器与 P14/P15/P16 三条链无法从 Step01 切换。
3. **RoutePlannerV3 A/B/C/D 全部 `registered_in_algorithm_registry = False`**，但 V3-D 会真实写入 `operational_routes` 与 `spatial_3d.route_altitude_profiles`（`v3_operational_adoption_service.py:768-769`）。这是"未注册却可生产发布"的旁路（§4.2.5）。

---

## 2. production 主链

### 2.1 正式航路主链：目标 → 现有实现映射

目标链（用户给定）：

```text
Population × Shelter
  → fixed cruise altitude layer
  → Layered Risk-Aware Theta* V2
  → RouteRiskProfile
  → terrain/building validation
  → Operational Adoption
  → Operational Route
```

**结论：这条链在当前代码中已经完全存在且是默认实现，不需要重写，只需要"收敛与显名"。** 逐段映射如下：

| 目标环节 | 现有实现 | 写入容器 | API | 前端 | 测试 |
|---|---|---|---|---|---|
| Population × Shelter | `grid_attributes.population`（WorldPop canonical density）+ `shelter_coefficient_policy` + 按需派生 `population_shelter` 场 | `shelter_coefficient_policy`；派生缓存 `_population_shelter_cache`（**不持久化**） | `GET/POST /api/shelter-coefficient-policy`、`/api/population-shelter`、`/api/population-nodata-policy` | `layered_theta_v2.js` `①b` 段 | `test_layered_theta_star_v2.py`、`test_bug_shelter_ui_001_shelter_policy_projection.py` |
| fixed cruise altitude layer | `spatial_3d.altitude_layers`（ALT-060/080/100/150/200 目录）+ `layered_route_planning_request`（显式 selected layer） | `layered_route_planning_request` | `GET/POST /api/layered-route-planning-request`、`/api/spatial-3d/altitude-layer*` | `route_operating_layer.js`、`step03` 操作→高度与程序 | `test_altitude_layer_lifecycle.py`、`test_route_operating_layer.py` |
| Layered Risk-Aware Theta* V2 | `layered_route_planner/theta_star_v2.py` + `supercover.py`；`default_algorithm_selection()["layered_route_planner"]` 默认项 | `layered_route_candidates` | `POST /api/layered-route-candidates/evaluate-real`、`GET /api/layered-route-candidates` | `layered_theta_v2.js`(76 KB) | `test_layered_theta_star_v2.py`(62 KB)、`test_layered_planner_baseline.py` |
| RouteRiskProfile | `risk/route_profile.py` + `domain/route_risk_profile.py` + `route_risk_profile_service.py` | `route_risk_profiles` + `route_risk_profile_policy` | `POST /api/route-risk-profiles/evaluate`、`GET /api/route-risk-profile{s,-policy,/readiness}` | `route_risk_profile.js`(56 KB) | `test_route_risk_profile.py`(54 KB)、`route_risk_profile_frontend.test.mjs`(66 KB) |
| terrain/building validation | **两段合成**：① 巡航段 `layered_route_validation_service`（复用 V3-C `continuous_validators.validate_terrain/validate_buildings`）② 爬升/下降段 `vertical_transition_validation_service`（源生 3D 几何 + FABDEM 原生像元 + 真实 building footprint） | `layered_route_validations`、`vertical_transition_validations` | `POST /api/layered-route-validations/evaluate-real`、`/api/vertical-transition-validations/evaluate-real` | `layered_route_validation.js`(27 KB)、`route3d_profile.js` | `test_layered_route_validation_adoption.py`(35 KB)、`test_vertical_transition_validation.py`(65 KB) |
| Operational Adoption | `layered_operational_adoption_service.py`（`published`/`stale`/`revoked`，Apply 需 `confirmed=true` + fingerprint 防 TOCTOU） | `layered_operational_adoptions` | `POST /api/layered-operational-adoptions/{preview,apply,revoke}` | `layered_operational_adoption.js`(50 KB) | `test_layered_route_validation_adoption.py` |
| Operational Route | `operational_routes`（`provenance.source_type = layered_operational_adoption_v1`） | `operational_routes` | 同上（apply 时写入） | `step03` 操作→运行航路 | `test_bug_route_003_runtime_rebind.py` 等 |

**链上辅助（不改变主链语义）**：
- `route_3d_profile_service.py` → `spatial_3d.route_3d_profiles`（distance-parametric climb/cruise/descent 薄层派生，供覆盖与剖面消费）。
- `route_safety_evidence_service.py` → `route_safety_evidence_v2`（只读聚合四个 domain，**不合成 Safety Score**）。
- `building_clearance_service.py` → `building_clearance_assessment`（唯一 roof/垂直余量语义）。

### 2.2 CNS 需求 → 能力评估 → 设施规划主链

目标是新业务链的第 4、5 步：

```text
CNS 需求（RequiredCNS）
  → CNS 能力评估（三维几何覆盖 → 静态技术能力 → 运行时间线 → 保护包络）
  → 服务走廊 → 走廊缺口 → 设施规划提案 → 闭环复核
```

| 环节 | 现有实现 | 容器 | 目标链归属 |
|---|---|---|---|
| CNS 需求（权威） | `required_cns`（project_default + route_overrides），`ManualRequiredCNSV1` 为默认（纯透传） | `required_cns` | 步 4 权威输入 |
| CNS 需求（建议） | `OperationalContextRequiredCNSV2` + `requirement_recommendation_service`，**只有用户显式 Adopt 才进入正式需求** | `cns_operation_context`、`cns_requirement_policies`、`required_cns_recommendation`、`required_cns_adoption` | 步 4 建议层 |
| 三维几何覆盖 | `GeometricCoverage3DV1`（EGM2008 正高、球/半球、仅几何） | `coverage_3d` | 步 5 |
| 静态服务能力 | `CNSServiceCapabilityV1`（P7 门控后的技术/接口/ServiceModel/RequiredCNS 匹配） | `cns_service_capability` | 步 5 |
| 运行时间线 | `RouteServiceTimelineV1`（恒定地速 + 显式场景） | `service_timeline` | 步 5 |
| 保护包络 | `TacticalProtectionEnvelopeV1`（代数工程基线） | `protection_envelope` | 步 5 |
| 服务走廊 | `CNSServiceCorridorV1`（保守 grid-cell 纳入 + 代表点 voxel probe） | `cns_corridor_assessment` + `cns_corridor_policy` | 步 5 |
| 走廊缺口 | `CNSCorridorGapAnalyzerV1`（P8 独立冗余 + 空间连续缺口 + 显式规划目标） | `cns_corridor_gap_assessment` + `cns_planning_objectives` | 步 5 |
| 设施规划提案 | `CorridorReuseFirstSitePlannerV2`（Application 累计重跑 P14→P15） | `cns_corridor_site_plan` + `corridor_site_planning_policy` | 步 5 |
| 闭环复核 | `ClosedLoopService`（working-copy 重跑 + Before/After + 事务式 Apply） | `closed_loop_assessment` | 步 5→6 |
| 雷达监视划设 | `algorithms/radar_layout/*`（MILP + HiGHS 双阶段）+ `radar_surveillance_layout_service` | `radar_surveillance_layout` + `radar_surveillance_policy` | 步 5（新增支线） |

### 2.3 方案评审与报告主链

```text
人工比较 → 选择 Variant → Confirm → 事务式 Apply → 报告与交付包
```

| 环节 | 现有实现 | 容器 |
|---|---|---|
| 评审与 Variant | `PlanReviewService` + `domain/plan_review.py`（Select ≠ Confirm ≠ Apply） | `cns_plan_review`、`confirmed_cns_plan` |
| 报告 | `PlanningReportService` + `reporting/builder.py` + `html_renderer.py` + `pdf_renderer.py`（P19，依赖本机 Playwright） | `cns_planning_reports` |
| 导出 | `ExportService`（project / routes GeoJSON / sites GeoJSON） | — |

### 2.4 三条主链的交叉点

| 交叉点 | 说明 |
|---|---|
| 正式航路 → CNS 能力 | `operational_routes` 是 `coverage_3d` / `cns_service_capability` / `service_timeline` / corridor 的共同输入 |
| CNS 需求 → 全链 | `required_cns` 变化失效 `coverage` / `cns_gap*` / `cns_service_capability` / `service_timeline` / `cns_site_plan` / `cns_corridor*` / `report` |
| 走廊缺口 → 设施规划 | `cns_corridor_gap_assessment` 是 `cns_corridor_site_plan` 的单向输入（P15 → P16） |
| 安全证据 | `route_safety_evidence_v2` 只读聚合 candidate/validation/adoption/risk-profile/transition，**不反向失效任何上游** |
| 报告 | `report` 是几乎所有链的汇点（失效传播的末端） |

### 2.5 现状与目标链的差距（只列需要收敛的部分）

| # | 差距 | 证据 | 收敛方向（复用现有） |
|---|---|---|---|
| G1 | 正式航路主链已存在但**未显名**：`RoutePlannerV1`（二维 A\*）仍是默认 `route_planner`，与 Theta\* V2 主链并列在 UI 中 | `registry.py:142` vs `:148-151`；`step03` 操作→运行航路 三个入口 | 保留 `route_planner` 作为"快速二维参考线"，但在 UI/文档中明确 Theta\* V2 链为唯一正式航路主链 |
| G2 | readiness 只有 `ready/partial/blocked/not_calculated/not_ready`，**缺 `ready_with_assumptions`** | 全库 grep 0 命中 | 在既有 readiness 快照上加一个档位，复用已有 `assumptions` / `confirmed=false` 字段 |
| G3 | `existing_cns_facilities` 无法区分「缺数据」与「已确认没有既有设施」 | `empty_collection()` 只有 `not_calculated`（`project_state.py:192-193`、`cns_input_service.py:12-16`） | 新增一个显式的基线声明键（§6.4） |
| G4 | layout 层的 `coverage`（demo 级二维圆）握有 `steps['5']` 门禁 | `workflow_service.py:1047-1048`、`registry.py:436` maturity=demo | 门禁改由 `coverage_3d` 或 `cns_service_capability` 承担 |
| G5 | 6 类算法游离注册表 + 前端只列 9/13 个 type | §1.4 | 先补前端 labels（零风险），再视情况补 manifest |
| G6 | `invalidation` 的声明式依赖图已非单一事实来源 | `services/invalidation.py:8-52` 的 `DEPENDENTS` + `invalidation_service.py` 的 43 个命令式方法 + 注入式 invalidator | 把既有命令式接线反向投影回声明式表（§7.4） |

---

## 3. 模块 Keep/Merge/Archive/Delete 表

分类判据见 §0.1。表格中"证据"列给出关键 `文件:行号`。

### 3.1 `cns_planner/application/`

| 模块 | 分类 | 判据与证据 |
|---|---|---|
| `app_context.py` | KEEP_PRODUCTION | 唯一运行上下文；注入全部 GIS 适配（`:128-142`、`:195-800`） |
| `workflow_service.py` | KEEP_PRODUCTION | 六步 facade + 38 个 service 装配（`:266-541`）+ snapshot（`:580-795`） |
| `project_state.py` | KEEP_PRODUCTION | schema-v2 唯一权威构造与回填（`:205-372`、`:419-728`） |
| `invalidation_service.py` | KEEP_PRODUCTION（**建议收敛**） | 唯一失效 authority；43 个方法 + 注入 invalidator（`:15-641`） |
| `session.py` | KEEP_PRODUCTION | 会话 + 延迟提交 + sidecar 恢复（`:113`） |
| `workspace_service.py` / `route_service.py` / `risk_service.py` / `risk_v2_service.py` | KEEP_PRODUCTION | 步 2–3 主链输入与 Risk V1/V2 |
| `layered_route_planner_service.py` | KEEP_PRODUCTION | 主链规划唯一写入者（65 KB） |
| `route_risk_profile_service.py` | KEEP_PRODUCTION | 主链风险画像唯一写入者 |
| `layered_route_validation_service.py` | KEEP_PRODUCTION | 主链巡航段验证唯一写入者 |
| `layered_operational_adoption_service.py` | KEEP_PRODUCTION | 主链发布唯一写入者 |
| `route_3d_profile_service.py` | KEEP_PRODUCTION | 主链剖面派生唯一写入者 |
| `vertical_transition_validation_service.py` | KEEP_PRODUCTION | 主链爬升/下降验证唯一写入者 |
| `route_safety_evidence_service.py` | KEEP_PRODUCTION | 只读证据聚合（70 KB） |
| `spatial_3d_service.py` / `route_operating_layer_service.py` | KEEP_PRODUCTION | 三维覆盖、高度层目录与程序 |
| `cns_planning_service.py` | KEEP_PRODUCTION（**内含 MERGE**） | 唯一写入者；但其内部 2D 覆盖来自 `CoveragePlannerV1`（见 §3.3） |
| `cns_service_capability_service.py` / `operational_timing_service.py` / `building_clearance_service.py` / `route_vertical_profile_service.py` | KEEP_PRODUCTION | 步 5 能力链 |
| `cns_input_service.py` | KEEP_PRODUCTION | CNS 五类输入唯一入口 |
| `requirement_recommendation_service.py` | KEEP_PRODUCTION | 步 4 需求建议 |
| `corridor_service.py` / `corridor_gap_service.py` / `corridor_site_planning_service.py` | KEEP_PRODUCTION | 步 5 走廊→缺口→提案 |
| `site_planning_service.py` | OPTIONAL | P11 planning-segment 站址提案，与 P16 并存（目标对象与度量不同） |
| `closed_loop_service.py` | KEEP_PRODUCTION | 唯一 what-if 重跑与事务式 Apply |
| `plan_review_service.py` / `report_service.py` / `export_service.py` | KEEP_PRODUCTION | 步 6 |
| `tower_obstacle_service.py` | KEEP_PRODUCTION | 铁塔派生层（障碍物高度 + 共塔候选） |
| `radar_surveillance_layout_service.py` | KEEP_PRODUCTION | 雷达划设唯一写入者（99 KB，工程严谨度最高的一条链） |
| `encounter_3d_service.py` | ADVANCED | DAA 实验室（未入注册表） |
| `safety_policy_service.py` | KEEP_PRODUCTION | 唯一走正规 service 的安全模块 |
| `source_audit_service.py` / `reference_data_service.py` / `reference_link_service.py` | KEEP_PRODUCTION | 来源审计与就绪诊断 |
| `project_directory_service.py` / `project_service.py` / `operation_service.py` | KEEP_PRODUCTION | 项目目录、项目元数据、运行规则 |
| `route_experiment_service.py` | ADVANCED | 规划器对照实验（V1 vs V2 benchmark），非主链 |
| `route_planner_v3_service.py` | ADVANCED（**有生产旁路**） | V3-A/B/C 实验编排 + 独立容器 |
| `v3_operational_adoption_service.py` | ADVANCED（**有生产写入**） | V3-D 会写 `operational_routes` + `spatial_3d`（`:768-769`） |
| `gap_analysis_service.py` | ARCHIVE | Gap V1 服务；仍被 `/api/workflow/gap-analysis` legacy 别名使用 |
| `gap_analysis_v2_service.py` | KEEP_PRODUCTION | 步 5 缺口 V2 |
| `review_service.py`（`ReviewService`） | LEGACY / DELETE_CANDIDATE | 仅 19 行；唯一消费者是 `workflow_service.py:1420` 的 `review()`，结果只塞进 snapshot `result["review"]`（`:787`），无 API、无前端 |
| `constraint_validation.py` | KEEP_PRODUCTION | planner 前 fail-closed 输入校验 |
| `export_service.py` | KEEP_PRODUCTION | 导出 |

### 3.2 `cns_planner/domain/`（57 个模块，按组归纳）

| 模块组 | 分类 | 说明 |
|---|---|---|
| `project.py` / `status.py` / `provenance.py` / `quantities.py` / `reference_crs.py` / `geometry_health.py` / `algorithm_manifest.py` | KEEP_PRODUCTION | 基础契约与状态机 |
| `cns_inputs.py` / `cns_performance.py` / `cns_reliability.py` / `cns_service_model.py` / `cns.py` | KEEP_PRODUCTION | CNS 输入与性能契约（`cns_reliability` 的 normalizer 被 `cns_inputs` 使用） |
| `requirement_policy.py` | KEEP_PRODUCTION | 运行上下文 + 显式 Policy 契约 |
| `layered_route.py` / `layered_theta_v2.py` | KEEP_PRODUCTION | 主链契约与目标函数常量（被 V1/V2 双方 import） |
| `route_risk_profile.py` / `route_3d_profile.py` / `route_vertical_profile.py` / `layered_route_validation.py` / `layered_operational_adoption.py` | KEEP_PRODUCTION | 主链产物契约；`layered_route_validation.py` 的 `stable_fingerprint`/`utc_now` 被 3 个下游复用 |
| `route_safety_evidence_v2.py` / `vertical_transition_validation.py` | KEEP_PRODUCTION | 证据聚合与过渡验证契约 |
| `building_clearance.py` / `building_geometry_quality.py` | KEEP_PRODUCTION | 唯一 roof/净空语义 |
| `spatial_3d.py` / `altitude_layer_defaults.py` / `reference_route_link.py` | KEEP_PRODUCTION | 三维与高度层 |
| `population_shelter.py` / `population_nodata.py` / `planning_exposure.py` | KEEP_PRODUCTION（`planning_exposure` = ADVANCED） | 人口×遮盖、NoData 语义、规划暴露度（默认关闭） |
| `risk.py` / `risk_v2.py` | KEEP_PRODUCTION | Risk V1/V2 顶层契约 |
| `regulatory_constraints.py` / `communication_planning_field.py` | KEEP_PRODUCTION（功能 = ADVANCED） | 未配置即 `not_evaluated`，不阻塞、不进入 cost |
| `safety_policy.py` / `closed_loop.py` / `plan_review.py` / `reporting.py` | KEEP_PRODUCTION | 步 4–6 契约 |
| `cns_corridor.py` / `cns_planning_objectives.py` / `corridor_site_planning.py` / `site_planning.py` / `cns_corridor.py` | KEEP_PRODUCTION | P14/P15/P16 契约 |
| `tower_colocation.py` / `tower_obstacle.py` / `towers.py` | KEEP_PRODUCTION | 铁塔派生层 |
| `radar_surveillance_layout.py` | KEEP_PRODUCTION | 雷达契约（含 V1.1 语义指纹） |
| `v3_operational_adoption.py` / `experiment.py` | ADVANCED | V3-D 契约与实验契约 |
| `source_audit.py` / `airspace.py` / `reference_crs.py` | KEEP_PRODUCTION | 审计与空域（display_only） |
| `encounter_3d.py` | ADVANCED | DAA |
| `__init__.py` | MERGE | 再导出 `gap/model.py` 等空壳契约（`:6`） |

### 3.3 `cns_planner/algorithms/`

| 模块 | 分类 | 判据与证据 |
|---|---|---|
| `registry.py` | KEEP_PRODUCTION | 算法治理（19 manifest / 13 type） |
| `coverage/geometric_3d.py`（GeometricCoverage3DV1） | KEEP_PRODUCTION | 7 处生产 import；`coverage_3d` 是 P8/P9/P10/P14 主链上游 |
| `coverage/v1.py`（CoveragePlannerV1） | **MERGE（→ 合流进三维覆盖链）** | maturity=`demo`（`registry.py:436`），输出 `coverage` 不进入任何 P7–P17 主链，却仍握有 `steps['5']` 门禁（`workflow_service.py:1047`）；与 3D 覆盖在 Step05 同屏两组卡片，仅共享一个 `distance_m` 工具函数（`geometric_3d.py:10`） |
| `coverage_planner.py`（3 行 shim） | **DELETE_CANDIDATE** | 唯一消费者 `tests/test_architecture.py:6,15`（同义性断言） |
| `service_capability/v1.py` | KEEP_PRODUCTION | 步 5 能力；`corridor_gap/v1.py:10-11` 复用其 `confirmed_independent_provider_count` |
| `timeline/v1.py` / `protection/v1.py` | KEEP_PRODUCTION | 步 5 时间线与保护包络 |
| `corridor/v1.py` / `corridor_gap/v1.py` | KEEP_PRODUCTION | P14/P15 |
| `requirements/manual_v1.py` | **OPTIONAL** | 默认但纯透传（`return current`），是"默认值 + 旧项目回填 + `stable_compatibility` maturity"三重契约锚点 |
| `requirements/operational_context_v2.py` | **OPTIONAL** | 功能上已可替代 V1，但非默认 |
| `radar_layout/*`（v1 1017 行 + milp 552 + candidates 213 + geometry 221 + `__init__`） | KEEP_PRODUCTION | 5 条路由 + 完整前端 + 2201 行测试；但**未入注册表** |
| `building_clearance/v1.py` | KEEP_PRODUCTION | 4 条路由 + step03 面板；**未入注册表** |
| `route_vertical_profile/v1.py` | KEEP_PRODUCTION | 显示用离散采样；**未入注册表** |
| `encounter_3d/v1.py` + `state_machine.py` | ADVANCED | DAA 实验室；**未入注册表** |
| `grid/mht4063.py` + `grid/service.py` | KEEP_PRODUCTION | MH/T 4063 网格 |
| `route/v1.py`（RoutePlannerV1） | KEEP_PRODUCTION（**降级为参考线**） | 默认 `route_planner`；二维经纬度 56×56 网格 + BBOX 硬约束，非工程模型（`registry.py:276-281`） |
| `route_planner.py`（3 行 shim） | **DELETE_CANDIDATE** | 唯一消费者 `tests/test_architecture.py:8,15` |
| `contracts.py` | **DELETE_CANDIDATE** | `RouteRequest`/`RouteResult`/`RouteAlgorithm` 全库 0 消费者（仅 `test_skeleton.py:6` import 其 `registry` 再导出）；**且 `:39` 存在反向悬空 import `from .registry import AlgorithmRegistry`** |

### 3.4 `cns_planner/risk/`、`safety/`、`simulation/`

| 模块 | 分类 | 判据与证据 |
|---|---|---|
| `risk/v1.py`（RiskModelV1） | KEEP_PRODUCTION | 默认 risk_model；`grid_risk` 是当前唯一被 planner 消费的风险输入 |
| `risk/model_v2.py` + `factors_v2.py` + `domains_v2.py` + `normalization.py` | KEEP_PRODUCTION | Risk Framework V2 分层实现；`domains_v2` 无直接测试（仅经 `model_v2.py` 覆盖） |
| `risk/accessors_v2.py` | KEEP_PRODUCTION | 被 Theta\* V2、layered service、route_risk_profile 三方消费 |
| `risk/route_exposure.py` | KEEP_PRODUCTION | layered planner 的 soft cost 来源 |
| `risk/route_profile.py` | KEEP_PRODUCTION | 主链 RouteRiskProfile 计算核心 |
| `risk/model.py`（`RiskModel` Protocol） | **DELETE_CANDIDATE** | 仅 `risk/__init__.py:3` 再导出；`@runtime_checkable` 但全库无 `isinstance(..., RiskModel)` |
| `safety/service_state.py` | ADVANCED | 被 router + timeline + service_capability 调用；独立 REST 出口无前端消费者 |
| `safety/event_evaluator.py` | KEEP_PRODUCTION | step04 FHA preview |
| `safety/coupling.py` | KEEP_PRODUCTION | step04 功能耦合 preview |
| `safety/fault_tree.py` | ADVANCED | 有路由、**无前端** |
| `safety/reliability.py` | **DELETE_CANDIDATE（唯一真孤儿）** | 45 行 `evaluate_reliability`；生产调用点 = 0（仅 `safety/__init__.py:11` 再导出 + 测试 import） |
| `safety/` 全体 | **架构不一致（见 §4.4）** | `api/router.py:7-10` 直连，绕过 Application 层：无 service、无 ProjectState、无指纹、无失效链 |
| `simulation/traffic_simulator.py` / `conflict_detector.py` | KEEP_PRODUCTION | 二维恒速 CPA；`/api/workflow/traffic-simulate` 是双重孤儿路由但前端经 mutate 使用 |

### 3.5 `gap/`、`site_planner/`、`layered_route_planner/`、`route_planner/`、`route_planner_v3/`

| 模块 | 分类 | 判据与证据 |
|---|---|---|
| `gap/v2.py`（CNSGapAnalyzerV2） | KEEP_PRODUCTION | 下游 6 个服务消费（site_planning / closed_loop / plan_review / reporting / v3 bridge / safety evidence） |
| `gap/v1.py`（CNSGapAnalyzerV1） | **MERGE（→ V2）** | 仍为默认，但输出 1-D 航路长度缺口，语义已被 V2 的 breakpoint 区间覆盖；前端经 legacy 别名 `mutate('gap-analysis')` 使用（`step05_cns.js:451`→`router.py:483`） |
| `gap/model.py` | **MERGE** | 仅 `gap/__init__.py:3` + `domain/__init__.py:6` 再导出 |
| `gap/model_v2.py`（40 行 TypedDict） | **DELETE_CANDIDATE** | **全库 import = 0，测试 = 0** |
| `site_planner/reuse_first_v1.py` | OPTIONAL | 目标对象 = `cns_gap_analysis_v2` planning segments，度量 = planning-gap length |
| `site_planner/corridor_reuse_first_v2.py` | KEEP_PRODUCTION | 目标对象 = P15 confirmed voxels，度量 = requirement-unit volume；what-if 编排在 Application |
| `layered_route_planner/theta_star_v2.py` | **KEEP_PRODUCTION（正式主链核心）** | 默认 layered planner；88 KB |
| `layered_route_planner/supercover.py` | KEEP_PRODUCTION | 无 API/前端/独立测试，但被生产规划器**唯一**依赖（`theta_star_v2.py:72-75`）——**不可按"无入口"删除** |
| `layered_route_planner/planner.py`（LayeredRoutePlannerV1） | **ARCHIVE** | 仍注册（`registry.py:216`）、仍可选、有专属前台（`layered_route_planner.js:18,367`）、且是 service 构造默认与兜底实例（`layered_route_planner_service.py:137,168`） |
| `route_planner/risk_aware_v2.py`（RiskAwareRoutePlannerV2 **+ GridGraph**） | **KEEP_PRODUCTION（必须拆分）** | 见 §4.2.2：`GridGraph` 被两个生产 layered 规划器 + risk profile 复用，导致"业务算法"与"网格邻接原语"耦合在同一个 21.8 KB 模块 |
| `route_planner_v3/`（24 文件） | **ADVANCED（V3-D 有生产写入）** | 全家族未注册；V3-A/B 战略与精化、V3-C 连续验证、V3-D 采纳；**V3-C 的 `continuous_validators.py` 已被生产链复用**（`layered_route_validation_service.py:19`、`vertical_transition_validation_service.py:852-856`） |
| `route_planner_v3/planner.py`（V3StrategicPlanner） | ADVANCED（**测试薄弱**） | 主干 `V3StrategicPlanner` 无直接 import、仅符号级覆盖；测试全部堆在 continuous/fine 支线 |
| `route_planner_v3/corridor.py` / `cost.py` / `fine_grid.py` | ADVANCED（**测试薄弱**） | 同上 |

### 3.6 `api/`、`web/js/`、`persistence/`、`services/`、`legacy/`

| 模块 | 分类 | 判据与证据 |
|---|---|---|
| `api/router.py` | KEEP_PRODUCTION（**结构债**） | 212 条路由集中在两个大分发函数；44 条无前端、46 条无测试、22 条双重孤儿 |
| `api/server.py` | KEEP_PRODUCTION | `X-CNS-Revision` 乐观并发（`:82-86` 409） |
| `api/file_browser.py` / `security.py` | KEEP_PRODUCTION | 文件浏览与路径安全 |
| `web/js/main.js` | KEEP_PRODUCTION（**需拆分**） | 35 KB 组装 + 全部写入路径收敛（`:57-69`） |
| `web/js/state/workflow_snapshot.js` | KEEP_PRODUCTION | 唯一允许写 `flow` 的路径 + 逐 cell hydrate（`:131-193`） |
| `web/js/workflow/step03_routes.js` | KEEP_PRODUCTION（**必须拆分**） | 1907 行 / 165.5 KB，含 4 个二级分段 × 13 面板 × 3 套规划算法 + 1000+ 行模型投影 |
| `web/js/workflow/layered_theta_v2.js`(76 KB) / `layered_route_planner.js`(30 KB) / `layered_route_validation.js`(28 KB) / `layered_operational_adoption.js`(50 KB) / `route_risk_profile.js`(56 KB) / `route_operating_layer.js` / `route3d_profile.js` / `route_vertical_profile.js` | KEEP_PRODUCTION（`layered_route_planner.js` = **ARCHIVE 面板**） | 主链前端；`layered_route_planner.js:18,367` 按 `algorithm_id` 分流渲染 V1 专属面板 |
| `web/js/workflow/radar_surveillance_layout.js` + `map/radar_layout_overlay.js` | KEEP_PRODUCTION | 雷达划设前端 |
| `web/js/workflow/daa_encounter_lab.js` | ADVANCED | DAA 实验室 |
| `web/js/workflow/risk_framework_v2.js` | KEEP_PRODUCTION | Risk V2 面板（内含 Legacy V1 对照） |
| `web/js/map/*`（22 文件） | KEEP_PRODUCTION | 地图渲染与覆盖层 |
| `web/js/sources/source_center.js` | KEEP_PRODUCTION | 数据源中心 |
| `persistence/project_repository.py` / `project_compaction.py` / `data_source_repository.py` | KEEP_PRODUCTION（compaction = **需扩展**） | 原子写 + 内容寻址 sidecar；目前只覆盖 2 个容器（§5.4） |
| `services/`（11 个 facade） | **ARCHIVE** | 除 `invalidation.py`（承载真实 `DEPENDENTS`）外均为兼容导入；`AI_DEV_CONTEXT.md` §10.4 已自述待删 |
| `legacy/` + `legacy_app.py` | **ARCHIVE** | Streamlit/schema-v1 原型，已隔离；新代码不得依赖 |

### 3.7 分类统计

| 分类 | 数量 | 备注 |
|---|---|---|
| **KEEP_PRODUCTION** | **96** | 含 5 条"无独立入口但被生产链唯一依赖"的模块（`supercover.py`、`risk/normalization.py`、`continuous_validators.py`、`route_operating_layer_service.py`、`project_directory_service.py`） |
| MERGE | 8 | `coverage/v1.py`、`gap/v1.py`、`gap/model.py`、`domain/__init__.py` 再导出、`coverage_planner.py`(shim 归 DELETE)、`route_planner.py`(shim 归 DELETE) 等 |
| OPTIONAL | 4 | `requirements/manual_v1.py`、`requirements/operational_context_v2.py`、`site_planner/reuse_first_v1.py`、`planning_exposure` 支线 |
| ADVANCED | 12 | `route_planner_v3/*`(按阶段计 4)、`safety/fault_tree.py`、`safety/service_state.py`、`encounter_3d/*`、`communication_planning_field`、`planning_exposure`、`route_experiment_service.py` |
| **ARCHIVE** | **7** | `layered_route_planner/planner.py`、`services/` facade、`legacy/`、`gap_analysis_service.py`、`review_service.py`、`CoveragePlannerV1`(若走归档而非合并)、`LayeredRoutePlannerV1` 前端面板 |
| **DELETE_CANDIDATE** | **9** | `algorithms/route_planner.py`、`algorithms/coverage_planner.py`、`algorithms/contracts.py`、`gap/model.py`、`gap/model_v2.py`、`risk/model.py`、`safety/reliability.py`、`review_service.py`、`services/` 中纯重复 facade |
| 合计受审条目 | 136 | |

> **terminal 汇报口径**：production keep = **96**；archive/delete candidate = **7 + 9 = 16**。

---

## 4. 旧技术引用关系与删除阻塞项

### 4.1 引用图谱总表（模块 → imports → service → API → state → frontend → tests）

| 算法/技术路径 | 被谁 import | Application service | API 路由 | ProjectState 键 | 前端 | 测试 | 分类 |
|---|---|---|---|---|---|---|---|
| RoutePlannerV1 | `registry.py:15,214`、`route_planner.py:2`、`route/__init__.py:1` | `RouteService`（`workflow_service.py:272,336`） | `POST /api/workflow/operational`(`router.py:478`)、`/api/algorithms/select`(`:349`) | 写 `operational_routes`、`result_statuses.routes` | `step03_routes.js:20` PLANNER_V1、`:1896` | 6 个 py 文件 + characterization | KEEP（降级） |
| RiskAwareRoutePlannerV2 | `registry.py:16,215`、`planner.py:44,595`、`theta_star_v2.py:71,537`、`risk/route_profile.py:31,105` | `RouteService`、`RoutePlanningExperimentService`（`:344,293`） | `/api/workflow/operational`、`/api/route-experiments/evaluate`(`:320`) | 写 `operational_routes`、`route_planning_experiments` | `step03_routes.js:21,1697` | `test_risk_aware_route_planner_v2.py` 等 5 个 | KEEP（拆分） |
| LayeredRoutePlannerV1（旧 A\*） | `registry.py:17,216`、`layered_route_planner_service.py:53-56,137,168` | `LayeredRoutePlannerService`（planner 注入 `:398-399`） | `GET /api/layered-route-*`（`:76-80`）、`POST .../evaluate-real`(`:370-372`) | `layered_route_candidates`、三个 policy | `layered_route_planner.js:18,367,521` | `test_layered_route_planner.py` 等 4 个 | **ARCHIVE** |
| LayeredRiskAwareThetaStarV2 | `registry.py:18,217`、`layered_route_planner_service.py:57` | `LayeredRoutePlannerService._uses_theta_star`(`:676`) | layered 路由组 + Theta\* V2 专属 policy（`:82-91`） | `shelter_coefficient_policy`、`theta_v2_objective_policy`、`max_route_risk_density`、`regulatory_constraints`、`communication_planning_field`、`planning_exposure_policy` | `layered_theta_v2.js` 全套 | `test_layered_theta_star_v2.py` 等 5 个 | **KEEP（主链）** |
| RoutePlannerV3 A/B/C/D | `route_planner_v3/__init__.py`、`route_planner_v3_service.py:32-63`、`v3_operational_adoption_service.py:30-38` | `RoutePlannerV3ExperimentService`(`:348`)、`V3OperationalAdoptionService`(`:480`) | 12+ 条（`:146-154`、`:322-346`） | `v3_*_policy`、`route_planner_v3_experiments`、`v3_operational_adoptions`、`v3_cns_assessment_bundle` **+ `operational_routes`、`spatial_3d`** | `step03_routes.js:255-1583`、`step04_operation.js:176-225`、`map/route_planner_v3_overlay.js` | 6 个 py 文件 | **ADVANCED** |
| Risk V1 | `registry.py:23`、`project_state.py:7`、`workflow_service.py:10` | `RiskService`(`:355`) | 经 `/api/workflow`、`/api/state` | 写 `grid_risk` | `step02_workspace.js`、`map/grid_overlay.js` | `test_risk_model.py` 等 3 个 | KEEP |
| Risk V2 | `workflow_service.py:11`、`model_v2.py:24,28` | `RiskFrameworkV2Service`(`:363`) | `/api/grid-risk-v2`(`:141`)、`/api/risk-policy-v2`(`:142`)、readiness(`:143`)、POST(`:316-317`) | 写 `grid_risk_v2`、`risk_policy_v2` | `risk_framework_v2.js`、`step02:599` | `test_risk_framework_v2.py` 等 2 个 | KEEP |
| CoveragePlannerV1 | `registry.py:8,218`、`cns_planning_service.py` | `CNSPlanningService`(`:425`) | `POST /api/workflow/coverage`(`:485`) | 写 `coverage` | `step05_cns.js:380-386,434` | `test_v1_algorithm_characterization.py` 金样板 | **MERGE** |
| GeometricCoverage3DV1 | `registry.py:9,221`、`corridor/v1.py:10-11`、`building_clearance/v1.py:20`、`route_vertical_profile/v1.py:12`、`project_state.py:30`、`radar_surveillance_layout_service.py:24`、`route_3d_profile_service.py:34` | `Spatial3DService`(`:426`)、`SitePlanningService`、`ClosedLoopService`、`PlanReviewService` | `GET /api/coverage-3d`(`:129`)、`POST .../evaluate`(`:416`) | 写 `coverage_3d` | `step05_cns.js:395,452` | 6 个 py 文件 | KEEP |
| CNSGapAnalyzerV1 | `registry.py:19`、`project_state.py:28`、`workflow_service.py:18` | `GapAnalysisService`(`:330`) | `GET /api/cns-gaps`(`:58`)、`POST .../analyze`(`:297`)、legacy 别名 `/api/workflow/gap-analysis`(`:483`) | 写 `cns_gap_analysis` | `step05_cns.js:394,451`（legacy 别名） | `test_cns_gap_analysis.py` 等 2 个 | **MERGE** |
| CNSGapAnalyzerV2 | `registry.py:20`、`project_state.py:29`、`workflow_service.py:19`、`site_planning_service.py:9` | `GapAnalysisV2Service`(`:331`) + 3 个下游 | `GET/POST /api/cns-gap-analysis-v2`(`:59,298`) | 写 `cns_gap_analysis_v2` | `step05_cns.js:403,460` | `test_cns_gap_analysis_v2.py` | KEEP |
| Corridor Gap V1 | `registry.py:14` | `CNSCorridorGapService`(`:452`) + 2 个下游 | `GET/POST /api/cns-corridor-gap`(`:64,304`)、objectives(`:63,303`) | 写 `cns_corridor_gap_assessment`、`cns_planning_objectives` | `step05_cns.js:398,455-456` | `test_cns_corridor_gap.py` | KEEP |
| ReuseFirstSitePlannerV1 | `registry.py:21`、`project_state.py:41`、`workflow_service.py:70` | `SitePlanningService`(`:441`) | `GET/POST /api/cns-site-plan`(`:60,299`) | 写 `cns_site_plan`、`site_planning_policy` | `step05_cns.js:404,461` | `test_cns_site_planner.py` 等 2 个 | OPTIONAL |
| CorridorReuseFirstSitePlannerV2 | `registry.py:22`、`workflow_service.py:71` | `CorridorSitePlanningService`(`:455`) | `GET/POST /api/cns-corridor-site-plan`(`:65,305`) | 写 `cns_corridor_site_plan`、`corridor_site_planning_policy` | `step05_cns.js:400,457` | `test_corridor_site_planner_v2.py` | KEEP |
| ManualRequiredCNSV1 | `registry.py:24` | `RequirementRecommendationService`(`:327,1038`) | `GET/POST /api/cns-required-recommendation*`(`:51,281-282`) | 读 `required_cns`、写 `required_cns_recommendation` | `step04_operation.js:151-171` | `test_required_cns_recommendation.py:67-70` | OPTIONAL |
| OperationalContextRequiredCNSV2 | `registry.py:25` | 同上 | 同上 | 读 `cns_operation_context`/`cns_requirement_policies` | 同上 | 同上 | OPTIONAL |
| Timeline | `registry.py:11`、`project_state.py:34` | `OperationalTimingService`(`:437`) | `GET/POST /api/service-timeline`(`:132,419`) | 写 `service_timeline` | `step05_cns.js:401,458` | `test_operational_timing.py` | KEEP |
| Protection | `registry.py:12`、`project_state.py:35` | `OperationalTimingService` | `GET/POST /api/protection-envelope`(`:133,420`) | 写 `protection_envelope` | `step04:284`、`step05:402,459`、`protection_budget.js` | `test_operational_timing.py:223,258` | KEEP |
| Reliability | **仅 `safety/__init__.py:11`** | **无** | **无** | 无 | **无** | `test_cns_reliability_service_state.py:12,81-102` | **DELETE_CANDIDATE** |
| Safety（event/coupling/fault-tree/service-state） | `api/router.py:7-10` **直连** | **无**（router 内联调用） | 4 条 POST(`:235-261`) | 无（纯计算，不持久化） | coupling ✅ `step04:322`；event ✅ `:308`；fault-tree ❌；service-state ❌ | `test_cns_safety_policy.py`、`test_cns_coupling.py`、`test_cns_reliability_service_state.py` | ADVANCED + **架构债** |
| DAA/Encounter 3D | `workflow_service.py:68`、`project_state.py:37` | `Encounter3DService`(`:474`) | `GET/POST /api/encounter-3d`(`:159,421`) | 写 `encounter_3d_assessment` | `daa_encounter_lab.js` | `test_encounter_3d.py` | ADVANCED |
| Radar | `radar_surveillance_layout_service.py:25`、`gis/radar_layout_adapter.py` | `RadarSurveillanceLayoutService`(`:383`) | 5 条(`:108-126`、`:400-405`) | 写 `radar_surveillance_layout`、`radar_surveillance_policy` | `radar_surveillance_layout.js` + `map/radar_layout_overlay.js` | `test_radar_surveillance_layout.py`(123 KB) + 前端 26 KB | KEEP |

### 4.2 五条旧算法路径的删除阻塞项

#### 4.2.1 RoutePlannerV1 — 删除阻塞：**最高**

| # | 阻塞项 | 证据 |
|---|---|---|
| 1 | **它是新项目默认 `route_planner`** | `registry.py:142` |
| 2 | registry 注册与实例化闭包 | `registry.py:214` |
| 3 | 运行期注入 `RouteService` | `workflow_service.py:272,336` |
| 4 | 实验服务硬编码对照对 | `route_experiment_service.py:292` |
| 5 | 前端身份常量与双 planner 判定 | `step03_routes.js:20,194` |
| 6 | 6 个测试文件依赖（含 characterization 金样板） | `test_architecture.py:7,15`、`test_route_planning_benchmark.py:14,319`、`test_algorithm_registry.py:64`、`test_v1_algorithm_characterization.py` 等 |
| 7 | 兼容 shim 入口 | `algorithms/route_planner.py:2` |

→ **结论：不可删除。** 应降级为"二维快速参考线"，保留其 `route_planner` 默认地位**但把 UI 与文档的正式航路入口收敛到 Theta\* V2 链**。

#### 4.2.2 RiskAwareRoutePlannerV2 — 删除阻塞：**高（结构性）**

| # | 阻塞项 | 证据 |
|---|---|---|
| 1 | **同模块内 `GridGraph` 被两个生产 layered 规划器 import** | `layered_route_planner/planner.py:44,595`、`layered_route_planner/theta_star_v2.py:71,537` |
| 2 | `GridGraph` 还被风险画像复用 | `risk/route_profile.py:31,105` |
| 3 | registry 注册为 `route_planner` 可选实现 | `registry.py:16,215` |
| 4 | 实验对照对 | `route_experiment_service.py:293` |
| 5 | 前端身份常量 | `step03_routes.js:21,1697` |
| 6 | 失效逻辑按 algorithm_id 判定 | `invalidation_service.py:472-476` |

→ **结论：文件不可删，但必须先拆分。** `GridGraph`（`risk_aware_v2.py:23`）应抽为独立模块（如 `route_planner/grid_graph.py`），V2 本体才能降级为纯可选算法。这是"想淘汰 V2 却删不掉文件"的根因。

#### 4.2.3 LayeredRoutePlannerV1 — 删除阻塞：**中高**

| # | 阻塞项 | 证据 |
|---|---|---|
| 1 | 仍注册为 `layered_route_planner` 合法实现 | `registry.py:17,216,323-324` |
| 2 | 仍出现在 `/api/algorithms` catalog 与 select 可选集 | `workflow_service.py:597,993` |
| 3 | **service 构造默认 planner 与兜底实例** | `layered_route_planner_service.py:137,168` |
| 4 | 前端有专属 V1 面板（按 algorithm_id 严格分流） | `layered_route_planner.js:18,20,22,367-382,521` |
| 5 | 老项目持久化选择不迁移 | `registry.py:144-147`、`workflow_service.py:395-398`（"no silent migration"） |
| 6 | 4 个测试文件 | `test_layered_route_planner.py`、`test_layered_planner_baseline.py`、`test_layered_theta_star_v2.py:47,1009-1029`、`test_phase35_bug_route_002.py:377` |

→ **结论：技术上可归档（无生产默认），但会破坏"老项目显式保存 V1 仍可运行"的承诺与 UI 分流。** 建议冻结为只读兼容源并公布弃用窗口（§9 阶段 3）。

#### 4.2.4 LayeredRiskAwareThetaStarV2 — 删除阻塞：**最高（它就是主链）**

| # | 阻塞项 | 证据 |
|---|---|---|
| 1 | **默认 layered planner** | `registry.py:148-151` |
| 2 | registry 注册 + 老项目参数规范化分支 | `registry.py:18,217,193-201` |
| 3 | 前端全量面板与搜索参数改写通道 | `layered_theta_v2.js:12-13,693-770,1197-1288` |
| 4 | 唯一依赖 `supercover.py` | `theta_star_v2.py:72-75,306,1540` |
| 5 | 5 个测试文件 | `test_layered_theta_star_v2.py`、`test_bug_route_004/005`、`test_bug_shelter_ui_001` 等 |

→ **结论：不存在删除可行性。** 相反它应被**显名为主链唯一规划器**。

#### 4.2.5 RoutePlannerV3（A/B/C/D）— 删除阻塞：**高（旁路风险）**

| # | 阻塞项 | 证据 |
|---|---|---|
| 1 | 12+ 条 API 路由 | `router.py:146-154,322-346` |
| 2 | 前端三套面板 + 地图 overlay | `step03_routes.js:255-1583`、`step04_operation.js:176-225`、`map/route_planner_v3_overlay.js` |
| 3 | 持久化容器已在 `blank_project` | `project_state.py:259-269` |
| 4 | **V3-D 真实写生产面** | `v3_operational_adoption_service.py:768-769`（`operational_routes` + `spatial_3d.route_altitude_profiles`） |
| 5 | V3-C 验证器被生产链复用 | `layered_route_validation_service.py:19`、`vertical_transition_validation_service.py:852-856` |
| 6 | 6 个测试文件 | `test_route_planner_v3*.py`、`test_bug_route_003_runtime_rebind.py` |

→ **结论：不可删；但必须显式隔离。** 关键风险是"未注册却可生产发布"——`algorithm_selection` 治理链对这条写路径无效。

### 4.3 不得按文件名/入口判断的反例（引用图谱反证）

| 模块 | 表面症状 | 实际结论 | 证据 |
|---|---|---|---|
| `layered_route_planner/supercover.py` | 无 API、无前端、无独立测试 | **KEEP_PRODUCTION** | 被生产规划器唯一依赖（`theta_star_v2.py:72-75,306,1540`） |
| `risk/normalization.py` | 只有一个 import | **KEEP** | 被 `domain/planning_exposure.py:50` 使用，属 V2 归一化内核 |
| `route_planner_v3/continuous_validators.py` | 位于"实验包"内 | **KEEP（已在生产链）** | `layered_route_validation_service.py:19`、`vertical_transition_validation_service.py:852-856`、`domain/vertical_transition_validation.py:495,647,822` |
| `application/route_operating_layer_service.py` | 分片式小 API | **KEEP** | 高度层目录与程序是主链前置条件 |
| `application/project_directory_service.py` | 未被 API 直接引用 | **KEEP** | 被 `app_context.py` 与 Save As/Open 白名单依赖 |
| `gap/model.py`、`gap/model_v2.py`、`risk/model.py`、`algorithms/contracts.py` | "看起来像接口层" | **DELETE_CANDIDATE** | 全库消费者为 0 或仅 `__init__` 再导出（§3.5、§3.4） |
| `safety/reliability.py` | 有实现、有测试 | **DELETE_CANDIDATE** | 生产调用点 = 0（§4.1） |
| `radar_layout/*` | 未入注册表 | **KEEP_PRODUCTION** | 5 路由 + 完整前端 + 2201 行测试 + 独立失效链 |

### 4.4 删除阻塞项分类汇总

| 阻塞类型 | 涉及模块 | 处理策略 |
|---|---|---|
| **A. 默认 selection 锚定** | RoutePlannerV1、CNSGapAnalyzerV1、ReuseFirstSitePlannerV1、ManualRequiredCNSV1、RiskModelV1、CoveragePlannerV1 | 改默认为独立决策项，需同步 `test_algorithm_registry.py:62-81,104,108` |
| **B. 被生产模块 import** | `GridGraph`(→两个 layered planner)、`supercover.py`、`continuous_validators.py`、`normalization.py` | 先抽公共模块，再谈降级 |
| **C. service 兜底/构造默认** | LayeredRoutePlannerV1、CoveragePlannerV1 | 移除兜底路径，改为显式 raise 或显式注入 |
| **D. 前端 algorithm_id 分流** | `layered_route_planner.js`、`step03_routes.js` PLANNER_V1/V2 | 前端收敛后同步删除分支 |
| **E. 已保存项目兼容** | LayeredRoutePlannerV1 的持久化 selection、legacy `devices`/`aircraft`/`rules` | 保留读取能力，禁止新写入 |
| **F. 测试契约** | 全量 manifest 相等断言、characterization、默认 selection 断言 | 删除前必须同步改测试 |
| **G. 未注册但可生产写入** | V3-D | 纳入 registry 治理或在 API 层加显式 owner 声明 |
| **H. 绕过 Application 层** | `safety/*` 4 个模块 | 补 service + 持久化 + 指纹，或明确降级为 preview-only |

---

## 5. ProjectState 瘦身清单

### 5.1 顶层 key 全清单与分类

**实测基线**：`projects/current_project.json` = 1,206,939 B（磁盘 1,295,212 B），**99 个顶层 key**。以下为全清单与分类。

图例：**IN**=INPUT（外部输入/用户配置）｜**PO**=POLICY（工程策略，需显式确认）｜**AR**=AUTHORITATIVE_RESULT（结论性、不可重算）｜**DA**=DERIVED_ARTIFACT（可重算派生）｜**LG**=LEGACY（旧版本兼容）｜**EX**=EXPERIMENTAL（实验容器）

| key | 体积(B) | 分类 | 说明 |
|---|---|---|---|
| `schema_version` / `revision` / `project` / `last_saved_at` | 1 / 1 / 180 / 34 | 元数据 | schema 与修订号 |
| `schema_version` 系列 + `result_index` | 444 | DA | compaction 索引（内容寻址 sidecar 指针） |
| `workspace` | 0 | **IN** | 工作区 bbox |
| `grid` | 0 | **IN** | MH/T 标准网格几何（真实工作区会达万级 cell） |
| `data_source_profiles` | 3,282 | **IN** | population/terrain/terrain_dtm 产品契约 |
| `grid_attributes` | 5,104 | **IN** | 逐 cell 映射结果（population/terrain/airspace/traffic/conflict/buildings/property_exposure/infrastructure/towers）——**真实工作区下最大** |
| `traffic_simulation` | 0 | DA | 交通仿真结果 |
| `grid_risk` | 1,433 | DA | Risk V1 相对指数 |
| `risk_policy_v2` | 1,776 | **PO** | Risk V2 聚合策略（默认 pending，无生产权重） |
| `grid_risk_v2` | 5,560 | DA | Risk V2 因子/域指数（**已被 compaction 外置 cells**） |
| `spatial_3d` | 249 | **IN+PO+DA+EX** | 混合容器：`altitude_layers`(IN/PO)、`route_operating_layers`(IN)、`departure_arrival_procedures`(IN)、`route_altitude_profiles`(DA)、`site_vertical_profiles`(DA)、`route_3d_profiles`(**DA**) |
| `altitude_layer_defaults_initialized` | 5 | 元数据 | 一次性初始化标记 |
| `coverage_3d` | 413 | DA | 三维几何覆盖 |
| `cns_service_capability` | 579 | DA | 静态能力判定 |
| `operational_timing` | 388 | **IN+DA** | `route_motion_profiles`/`service_scenarios`/`response_time_budgets`/`encounter_scenarios` |
| `service_timeline` | 252 | DA | 运行时间线 |
| `protection_envelope` | 393 | DA | 保护距离 |
| `route_vertical_profiles` | 421 | DA | 显示用剖面采样 |
| `encounter_3d_assessment` | 582 | DA（EX） | DAA 评估 |
| `nodes` / `node_seq` / `route_seq` / `retired_route_ids` / `scenario_routes` | 0 / 1 / 1 / 0 / 0 | **IN** | 节点与场景航路 |
| `operational_routes` | 0 | **AR** | 正式运行航路（主链产物） |
| `aircraft` / `rules` | 0 / 0 | **LG** | 旧运行规则（含 legacy `lambda_per_hour=1/mtbf_h`） |
| `reference_landing_sites` | **112,353** | **IN（外置首选）** | 参考起降点来源事实 |
| `reference_routes` | **453,919** | **IN（外置首选）** | 真实参考航线来源事实（37.6% 文件体积） |
| `reference_route_links` | 177 | IN | 参考航线↔OD 关联 |
| `reference_route_import_preview` | 0 | DA（临时） | 导入预览 |
| `route_planning_experiments` | 255 | **EX** | 规划器对照实验 |
| `tower_obstacle_policy` / `tower_clearance_policy` / `tower_colocation_policy` | 115 / 154 / 558 | **PO** | 铁塔派生策略 |
| `tower_obstacle_profiles` / `tower_colocation_candidates` | 445 / 875 | DA | 派生事实（373 条） |
| `radar_surveillance_policy` / `radar_surveillance_layout` | （在 818 内合计） | **PO** / DA | 雷达划设 |
| `v3_planning_policy` / `v3_fine_refinement_policy` / `v3_continuous_validation_policy` | 2,603 / 503 / 1,674 | **EX-PO** | V3 三阶段策略 |
| `route_planner_v3_experiments` | 289 | **EX** | V3-A/B/C 实验容器（含 refinements/validations 大数组） |
| `v3_operational_adoptions` | 93 | **EX-AR** | V3-D 采纳记录 |
| `v3_cns_assessment_bundle` | 104 | **EX** | V3-D CNS 评估包 |
| `airspace_policies` | 90 | **IN** | 空域策略（display_only） |
| `source_audits` | 10,781 | **IN（可重建，外置候选）** | 来源审计 |
| `equipment_reference_catalog` | **25,791** | **IN（外置首选）** | 真实设备资料库（离线整理，canonical JSON 可重载） |
| `aircraft_profiles` | 3,063 | **IN（外置候选）** | 飞行器能力目录（`catalogs/*.json` 可重载） |
| `selected_aircraft_profile_id` | 0 | **IN** | 选中飞行器 |
| `required_cns` | 1,665 | **AR** | 权威 CNS 需求 |
| `cns_operation_context` | 774 | **IN** | 运行上下文 |
| `cns_requirement_policies` | 149 | **PO** | 需求推证规则 |
| `required_cns_recommendation` | 464 | DA（建议） | 需求建议 |
| `required_cns_adoption` | 202 | **AR** | 需求采用 provenance |
| `device_catalog` / `devices` | 10,029 / 733 | **IN（外置候选）** / **LG** | 设备目录 / V1 投影副本 |
| `existing_cns_facilities` / `candidate_sites` | 118 / 110 | **IN** | 既有设施 / 候选站址 |
| `cns_gap_analysis` | 143 | **LG** | Gap V1 结果 |
| `cns_gap_analysis_v2` | 372 | DA | Gap V2 结果 |
| `site_planning_policy` / `cns_site_plan` | 496 / 525 | **PO** / DA（OPTIONAL 链） | P11 |
| `closed_loop_assessment` | 597 | DA | P12 闭环 |
| `cns_corridor_policy` / `cns_corridor_assessment` | 152 / 665 | **PO** / DA | P14 |
| `cns_planning_objectives` / `cns_corridor_gap_assessment` | 118 / 672 | **PO** / DA | P15 |
| `corridor_site_planning_policy` / `cns_corridor_site_plan` | 574 / 872 | **PO** / DA | P16 |
| `cns_plan_review` / `confirmed_cns_plan` | 271 / 162 | **AR** | P18 评审与确认计划 |
| `cns_planning_reports` | 64 | **AR** | P19 报告 |
| `safety_policy` / `safety_assessment` | 6,949 / 79 | **PO** / DA | 安全策略与预留结果 |
| `building_clearance_policy` / `building_clearance_assessment` | 239 / 467 | **PO** / DA | 建筑净空 |
| `layered_route_planning_request` | 398 | **IN+PO** | 主链规划请求 |
| `layered_route_feasibility_policy` / `layered_route_cost_policy` | 435 / 395 | **PO** | 主链可行性与代价策略 |
| `layered_route_candidates` | 251 | DA（**主链核心中间产物**） | 含 masks/cells（**已 compaction 外置**） |
| `route_risk_profile_policy` / `route_risk_profiles` | 2,241 / 316 | **PO** / DA | 主链风险画像 |
| `layered_route_validations` | 290 | DA | 主链巡航段验证 |
| `layered_operational_adoptions` | 110 | **AR** | 主链发布记录 |
| `route_safety_evidence_v2` | 329 | DA | 安全证据聚合 |
| `vertical_transition_validations` | 404 | DA | 爬升/下降验证 |
| `shelter_coefficient_policy` | 675 | **PO** | 遮盖系数（用户确认 1.0 基线） |
| `population_nodata_policy` | 782 | **PO** | NoData 语义 |
| `regulatory_constraints` | 818 | **PO** | 法规约束（未配置 → not_evaluated） |
| `communication_planning_field` | 821 | **PO（EX）** | 通信规划场（未接入 cost/constraint） |
| `theta_v2_objective_policy` | 1,002 | **PO** | 主链目标函数 0.8/0.1/0.1 |
| `max_route_risk_density` | 917 | **PO** | 主链评估约束 |
| `planning_exposure_policy` | 743 | **PO（EX）** | 规划暴露度（默认关闭） |
| `risks` | 435 | **LG** | 4 个 assessment 占位（GRC/技术/生命/财产，均 not_calculated 或 pending） |
| `coverage` | 0 | **LG** | CoveragePlannerV1 二维圆覆盖 |
| `result_statuses` | 1,237 | **AR** | 结果状态机（唯一被前端与失效链共同消费） |

### 5.2 实测体积分布（Top 12）

| # | key | 字节 | 占比 | 性质 |
|---|---|---|---|---|
| 1 | `reference_routes` | 453,919 | **37.6%** | 只读来源事实 |
| 2 | `reference_landing_sites` | 112,353 | **9.3%** | 只读来源事实 |
| 3 | `equipment_reference_catalog` | 25,791 | 2.1% | 只读参考目录 |
| 4 | `source_audits` | 10,781 | 0.9% | 可重建审计 |
| 5 | `device_catalog` | 10,029 | 0.8% | 可重载 catalog |
| 6 | `safety_policy` | 6,949 | 0.6% | 策略 |
| 7 | `grid_risk_v2` | 5,560 | 0.5% | 派生（cells 已外置） |
| 8 | `grid_attributes` | 5,104 | 0.4% | 映射输入（当前工作区为空） |
| 9 | `data_source_profiles` | 3,282 | 0.3% | 产品契约 |
| 10 | `aircraft_profiles` | 3,063 | 0.3% | 可重载 catalog |
| 11 | `algorithm_selection` | 2,894 | 0.2% | 策略 |
| 12 | `v3_planning_policy` | 2,603 | 0.2% | 实验策略 |

> **关键观察**：这份 1.2 MB 项目**尚未建立真实工作区**（`workspace`/`grid`/`nodes`/`operational_routes` 皆为 0 B），却已有 1.2 MB。**约 49.8%（601 KB）是 6 个只读参考/目录容器**。一旦建立真实工作区，`grid` + `grid_attributes.*.cells` + `grid_risk.cells` 会再增加数 MB。

### 5.3 建议移出 `project.json` 的大型状态

按"是否可从已登记来源确定性重建"排序：

| 优先级 | key | 现体积 | 外置方案 | 依据 |
|---|---|---|---|---|
| **P0** | `reference_routes` | 454 KB | sidecar `reference/routes.json` + `reference_index{sha256,count,crs}` | 来源 CSV/XLSX/GeoJSON 已在 `source_audits` 登记；`reference_data_service.restore_routes_from_source()` 已实现自动恢复路径（`workflow_service.py:1147-1148`） |
| **P0** | `reference_landing_sites` | 112 KB | sidecar `reference/landing_sites.json` | 同上（`:1139-1140` 已实现 bootstrap 恢复） |
| **P0** | `equipment_reference_catalog` | 26 KB | 直接复用 canonical JSON 源文件（只存 sha256 + 路径） | `reference_data/equipment_catalog.py` 已是"离线整理 canonical JSON"形态；`normalize_equipment_reference_catalog()` 只做校验 |
| **P1** | `device_catalog` / `aircraft_profiles` | 13 KB | 复用 `catalogs/*.json` + `selected_*_id` | `cns_input_service.ensure_catalogs()` 已有 `Catalog.load(config_dir)` 回退路径（`:26-35`） |
| **P1** | `source_audits` | 11 KB | 外置 `audits.json`（保留 sha256 + status 摘要在主文档） | 审计需要可追溯，但逐文件明细不必随主文档 |
| **P2** | `grid_attributes.*.cells` | 真实工作区下数 MB | 扩展现有 compaction | 已有 `slim_grid_attributes()` 投影（`workflow_service.py:183-202`）+ `GET /api/workspace/grid/attributes` 明细端点，**基础设施已就绪** |
| **P2** | `grid.cells` | 真实工作区下数 MB | 扩展现有 compaction | 逐 cell 结果按 `grid_id` 关联，可确定性重建 |
| **P2** | `grid_risk.cells` | 真实工作区下 MB 级 | 扩展现有 compaction（对齐 `grid_risk_v2`） | `grid_risk_v2.cells` 已外置，V1 未覆盖是**不对称** |
| **P3** | `coverage_3d` 逐点采样 | 数千点 | 扩展现有 compaction | 已有 `/api/coverage-3d` 专用端点 + snapshot 只带摘要 |
| **P3** | `route_planner_v3_experiments`（含 refinements/validations） | 无上界（历史累积） | 扩展现有 compaction + 保留 `MAX` 条 | 实验容器**只增不减**，是长期无界增长点 |
| **P3** | `radar_surveillance_layout` | 单元格级结果 | 扩展现有 compaction | 已有独立 readiness 端点 |

### 5.4 已有 compaction 机制现状与扩展点

**已实现**（`persistence/project_compaction.py`，103 行）：

| 能力 | 现状 |
|---|---|
| 外置容器 | 仅 2 个：`grid_risk_v2.cells`、`layered_route_candidates.items` + `.masks` |
| 存储形态 | gzip 压缩 + sha256 内容寻址 → `.cns-results/<sha256>.json.gz` |
| 完整性 | 读取时校验 sha256，不匹配即 `raise ValueError("项目结果文件指纹不匹配")`（`:89-90`） |
| 索引 | `result_index`（schema_version / artifact / sha256 / 各容器 status/指纹/计数） |
| 恢复 | `restore_compacted_results()`（`:79-103`），由 `session.py:113` 调用 |
| 缓存排除 | `_population_shelter_cache`、`_planning_exposure_cache` 不持久化（`:21`、`workflow_service.py:103-105`） |

**扩展点（复用同一机制，不新建）**：

1. `payload` 字典新增键即可（`:28-32`）：`grid_cells`、`grid_attribute_cells`、`grid_risk_cells`、`coverage_3d_samples`、`v3_experiments`、`radar_layout_cells`。
2. `document[...]["cells"] = {}` 的置空逻辑（`:43-51`）同样按 key 复制。
3. `result_index` 增加对应计数块（`:52-75`）。
4. 前端无需改动：`slim_*` 投影 + `detail_available` + 专用端点模式（`workflow_snapshot.js:131-193`）已经是通用通道。

**结论：ProjectState 瘦身不需要新架构，只需要把 compaction 覆盖范围从 2 个容器扩展到 8–10 个，并把 5 个只读容器改为 sidecar 引用。** 预期 `current_project.json` 从 1.2 MB 降到约 150–250 KB（不含真实工作区）。

### 5.5 瘦身收益与风险

| 项 | 收益 | 风险 | 缓解 |
|---|---|---|---|
| 只读容器外置 | 文件体积 −50% | 项目脱离 sidecar 后无法打开 | sidecar 内联回退 + sha256 校验 + 明确错误提示（已有 `restore_compacted_results` 模式） |
| 逐 cell 外置 | 真实工作区下 −80% 以上 | Save As / Open 必须原样带回 sidecar | `ProjectDirectoryService.SPATIAL_SOURCE_KEYS` 白名单已存在同类机制（`AI_DEV_CONTEXT.md` §4 末尾） |
| 实验容器裁剪 | 消除无界增长 | 历史实验丢失 | 保留最近 N 条 + 归档到 sidecar 而非删除 |
| `spatial_3d` 混合容器拆分 | 语义清晰（IN/PO/DA 分离） | 需 schema 迁移 | 用 additive key 而非改结构（保持 schema v2） |

---

## 6. 数据 Required/Assumable/Optional 表

### 6.1 依赖分类总表

来源：`data/registry.py:20-71`（23 个 SourceDefinition）、各 readiness 服务、`reference_link_service.data_readiness()`（`:120-194`）、`layered_route_planner_service.readiness_snapshot()`（`:333-415`、`:1040-1080`）。

图例：**REQ**=REQUIRED｜**ASM**=ASSUMABLE｜**OPT**=OPTIONAL｜**ENH**=ENHANCEMENT

| # | 输入 | 分类 | 消费者 | 缺失时的当前行为 | 缺失时的目标行为 |
|---|---|---|---|---|---|
| 1 | `workspace` bbox（用户框选） | **REQ** | 网格 → 全链 | `grid_unavailable` → blocked | blocked |
| 2 | MH/T 标准网格 `grid`（含 L8） | **REQ** | 全部逐 cell 映射、主链规划 | `grid_unavailable` → blocked（`layered_route_planner_service.py:410-411`） | blocked |
| 3 | `scenario_routes`（显式 OD） | **REQ** | 主链规划请求 | `scenario_route_not_available` / `not_found`（`:400-406`） | blocked |
| 4 | `spatial_3d.altitude_layers` + confirmed `nominal_altitude_m` + `vertical_reference` | **REQ** | 主链固定巡航高度层 | `altitude_layer_not_found` / `altitude_layer_not_resolvable`（`:363-371`）；AGL/WGS84 缺 geoid 证据 → blocked | blocked（不得猜 datum） |
| 5 | `layered_route_planning_request.confirmed` | **REQ** | 主链规划 | `planning_request_not_confirmed`（`:358-359`） | blocked |
| 6 | `terrain_dtm`（FABDEM verified，含 EGM2008 元数据） | **REQ** | 主链 terrain floor + 连续验证 | `terrain_source_unavailable`（`:389-390`） | blocked |
| 7 | `buildings` / `building_grid`（L8 聚合，含 height/ground/valid_fraction） | **REQ**（主链）；OPT（其他） | 主链 building 门控 | 缺 `height`/`ground` 或 `valid_height_fraction<1` → `unknown`（绝不当 0） | blocked（主链）/ not_evaluated（其他） |
| 8 | `building_clearance_policy` confirmed | **REQ** | 主链 building 净空 | `building_clearance_policy_not_confirmed`（`:395-396`） | blocked |
| 9 | `layered_route_feasibility_policy.terrain_vertical_clearance_m` | **REQ** | 主链垂向可行性 | `feasibility_policy` 未确认（`:374-378`，注释"无默认值"） | blocked |
| 10 | `layered_route_cost_policy` 三个 λ | **REQ** | 主链代价函数 | 未全部显式确认（`:380-384`，`null != 0`） | blocked；显式 `0` 合法 |
| 11 | `grid_attributes.population`（canonical density） | **REQ** | Risk V1/V2 → 主链 risk 项 | 映射 status ≠ passed → risk 分量 unresolved | blocked |
| 12 | `shelter_coefficient_policy` confirmed | **REQ**（Theta\* V2 主链） | population × shelter | `shelter_coefficient_policy_not_confirmed`（`:1053-1054`） | blocked；缺失时**绝不补 0** |
| 13 | `population_nodata_policy` | **ASM** | 人口 NoData 语义 | 未确认 → 来源范围内 NoData 保持 `missing_data`（`project_state.py:332-333`） | ready_with_assumptions（假设 NoData 视为缺失而非 0） |
| 14 | `theta_v2_objective_policy`（0.8/0.1/0.1） | **ASM** | 主链目标函数 | 默认带 `user_defined_baseline` provenance，可运行 | ready_with_assumptions（已显式标注 baseline） |
| 15 | Theta\* V2 `heading_bin_count=8` / `theta_min_deg=5.0` | **ASM** | 主链搜索参数 | 默认携带 `software_algorithm_baseline_not_engineering_confirmed` + provenance（`registry.py:399-410`） | **ready_with_assumptions 的标准范例**（已有实现，只差档位名） |
| 16 | `max_route_risk_density` | **ASM** | 主链候选评估约束 | 默认 1.0（"deliberately wide temporary"） | ready_with_assumptions |
| 17 | `coverage_model.parameters.sample_spacing_m = 500` | **ASM** | 三维几何覆盖 | 显式 `assumption: engineering_sampling_assumption`、`confirmed: false`（`registry.py:154-161`） | **ready_with_assumptions 范例** |
| 18 | `device_catalog`（demo/default） | **ASM** | 覆盖与能力 | `CoveragePlannerV1` maturity=`demo`；设备参数由 DeviceCatalog 兼容转换 | ready_with_assumptions（需显式标注 demo） |
| 19 | `aircraft_profiles` + `selected_aircraft_profile_id` | **REQ**（CNS 链） | RequiredCNS/能力/时间线 | 未选 → 相关能力评估 `not_calculated` | blocked（CNS 链）/ not_evaluated（航路主链） |
| 20 | `required_cns` | **REQ**（CNS 链） | 覆盖/缺口/能力/走廊 | `pending_required_cns()` → `pending_confirmation` | blocked（CNS 链） |
| 21 | `existing_cns_facilities` | **REQ（但有语义缺口）** | Gap/能力/覆盖/站址 | `not_calculated` + count=0（**无法区分"无数据"与"确认没有"**） | 见 §6.4 |
| 22 | `candidate_sites` | **OPT** | 站址提案 | 空 → 只能 `new_build_candidate` tier | not_evaluated（tier 1–4 不可用），不阻塞 |
| 23 | `towers`（真实铁塔） | **OPT** | 共塔 tier + 塔高净空 | 空 → 无 `tower_colocation_host` tier；不影响其他链 | not_evaluated |
| 24 | `cns_operation_context` + `cns_requirement_policies` | **OPT** | 需求建议（P17） | 未配置 → `not_configured`（`operational_context_v2.py:62`） | not_evaluated，不阻塞正式需求 |
| 25 | `airspace` / `airspace_policies` | **OPT** | 仅显示 | 恒 `display_only`，不进入任何可行性判定或指纹 | not_evaluated（`display_only`） |
| 26 | `regulatory_constraints` | **OPT** | 主链候选合规标注 | 未配置 → `regulatory_compliance = not_evaluated`，**不阻塞搜索**（`regulatory_constraints.py:430-451`） | not_evaluated |
| 27 | `communication_planning_field` | **OPT（当前 dead）** | 无（`used_in_cost=false`、`used_as_constraint=false`） | 不影响 path/cost（`registry.py:427`） | not_evaluated |
| 28 | `property_exposure` | **OPT** | Risk V2 因子 | 无数据 → factor `unknown`，**不补 0** | not_evaluated |
| 29 | `infrastructure` | **OPT** | Risk V2 因子 | 同上 | not_evaluated |
| 30 | `traffic` / `conflict`（UAV 交通仿真） | **OPT** | Risk V1/V2 air domain | 缺 → air domain `unresolved`；**不是地面交通** | not_evaluated |
| 31 | `land_mask` | **OPT** | 雷达划设 land/sea | 未配置 → `surface_class = unknown`（fail-closed） | not_evaluated |
| 32 | `terrain`（GLO-30 DSM） | **OPT** | 显示与剖面 | 主链用 FABDEM；DSM 缺失只影响对比展示 | not_evaluated |
| 33 | `reference_routes` / `reference_landing_sites` | **OPT** | 参考展示、OD 起点选择 | 空 → 无参考；`[lon,lat]` 在 CRS 未确认时只临时展示 | not_evaluated |
| 34 | `equipment_reference_catalog` | **OPT** | 只读查阅 | 不自动进入 DeviceCatalog / Coverage（`AI_DEV_CONTEXT.md` §3） | not_evaluated |
| 35 | `source_audits` | **OPT** | 数据就绪面板 | 空 → `not_checked` | not_evaluated |
| 36 | `planning_exposure_policy` | **OPT（默认关闭）** | Theta\* V2 规划暴露度 | 未配置即不生效（`project_state.py:338-340`） | blocked if enabled / not_evaluated if disabled |
| 37 | 建筑精确 footprint 几何（vs L8 聚合） | **ENH** | 净空与验证精度 | 有则升级为 exact polygon 判定；无则保留 coarse envelope | 不改变可用性 |
| 38 | 独立冗余证据（provider independence） | **ENH** | Corridor Gap / 能力 | 缺 → P8 返回 evidence-limited（非 meets），fail-closed | 不改变可用性 |
| 39 | 铁塔实测高度 | **ENH** | 共塔候选精度 | 缺 → 使用声明高度并标 `unverified` | 不改变可用性 |
| 40 | 记录来源 sha256/checksum | **ENH** | 来源可追溯 | 当前为 `configured_assumption`（`source_profiles.py:26,43`） | 不改变可用性 |
| 41 | 事件/耦合观察数据（EventObservation） | **ENH** | FHA/FTA/耦合 | 空 → 不形成任何安全结论 | 不改变可用性 |
| 42 | 真实 encounter track / policy / capability | **ENH** | DAA 实验室 | 未配置 → 默认 `not_calculated / NO_TRAFFIC` | 不改变可用性 |

### 6.2 缺失行为矩阵（目标）

| 缺失项 | blocked | ready_with_assumptions | not_evaluated |
|---|---|---|---|
| workspace / grid / scenario route | ✅ | | |
| AltitudeLayer confirmed / planning request | ✅ | | |
| FABDEM verified / buildings 事实 | ✅ | | |
| clearance / λ 工程值 | ✅ | （λ 显式 0 时可视为"纯距离假设"→ 应走 ASM） | |
| population / shelter policy | ✅ | | |
| θ/heading 搜索参数、sample_spacing、objective policy、risk density、NoData 语义 | | ✅ | |
| demo device catalog | | ✅（必须显式标注 demo） | |
| required_cns / aircraft profile | ✅（CNS 链） | | ✅（航路主链旁路时） |
| existing CNS | ✅（无声明时） | | ✅（确认无既有设施时走完整缺口评估） |
| airspace / regulatory / communication / property / infrastructure / traffic / land_mask / planning_exposure | | | ✅ |
| candidate_sites / towers / reference_* / equipment_reference_catalog / source_audits | | | ✅ |

### 6.3 当前 readiness 档位缺口（Phase4-B 必须补齐）

| 缺口 | 现状 | 影响 |
|---|---|---|
| 无 `ready_with_assumptions` | 只有 `ready/partial/blocked/not_calculated`（`reference_link_service.py:240-241`）+ `ready/not_ready`（`layered_route_planner_service.py:415,1080`） | 用户无法区分"缺关键工程参数（真 blocked）"与"用了软件基线（可继续）" |
| "可假设"信息散落 | 分布在 `assumptions`、`confirmed=false`、`parameter_origin=software_baseline`、`assumption: engineering_sampling_assumption` 等 4 种不同字段 | 前端必须逐面板特判；无法汇总成一步概览 |
| 缺 `not_evaluated` 的统一出口 | 各 result 容器内都有 `not_evaluated` 字典（`geometric_3d.py:382`、`service_capability/v1.py:454`、`gap/v2.py:306` 等），但没有"本项目哪些能力未评估"的顶层汇总 | Step06 无法给出可信的"未评估清单" |

### 6.4 `missing existing CNS` vs `confirmed no existing CNS`

**当前实现事实**：`existing_cns_facilities` 由 `empty_collection("existing-cns-facilities")` 初始化（`project_state.py:282`、`cns_input_service.py:12-16`），只有 `status: "not_calculated"` + `count: 0` + `items: []`。

**问题**：以下两种截然不同的现实在项目状态中**完全同形**：

1. **缺失既有设施数据**（用户还没导入）→ 任何"覆盖/缺口/能力"结论都应是 `incomplete / unknown`，**不得**得出"全部缺口"或"需要新建 N 个站"。
2. **已确认本地没有既有 CNS 设施**（用户明确声明）→ 可以合法地得出结论"全部需求需靠新建满足"，并让 `ReuseFirstSitePlannerV1`/`CorridorReuseFirstSitePlannerV2` 的 `existing_cns_facility` / `existing_shared_site` 两个 reuse tier 判定为"确定不可用"而非"未评估"。

**受影响的下游**（全部当前无法区分）：

| 下游 | 当前行为 | 应有差异 |
|---|---|---|
| `CNSGapAnalyzerV1` / `V2` | 空设施 → 视为"无覆盖" | missing → incomplete；confirmed-none → 合法缺口 |
| `GeometricCoverage3DV1` / `CNSServiceCapabilityV1` | 空设施 → 无 provider | missing → unresolved；confirmed-none → unknown provider 是确定事实 |
| `CNSCorridorGapAnalyzerV1`（独立冗余） | 空设施 → 冗余 0 | missing → 冗余"未评估"；confirmed-none → 冗余确实为 0 |
| `ReuseFirstSitePlannerV1` / `V2` | `reuse_tiers` 前两层跳过，直接用 `new_build_candidate` | missing → 提案应被阻断或标注；confirmed-none → 正常提案 |
| `ClosedLoopService` | Before/After 都基于空基线 | missing → 无可信 before |
| `data_readiness()` | `airspace_policies` 有 `not_applicable` 但 `existing_cns` 无对应块 | 应新增一个 block，状态为 `not_declared` / `confirmed_none` / `confirmed_present` |

**Phase4-B 最小方案（additive，不改 schema 版本）**：新增一个顶层键（命名示例 `cns_existing_baseline`）：

```json
{
  "status": "not_declared | confirmed_none | confirmed_present",
  "confirmed": false,
  "source": "...",
  "evidence": null,
  "declared_at": null,
  "declared_by": null
}
```

接入点（全部复用既有机制）：
- `project_state.blank_project()` 初始化为 `not_declared`；
- `normalize_project()` 做 additive 回填（旧项目一律 `not_declared`，**绝不默认成 confirmed_none**）；
- `cns_input_service` 增加一个 `declare_existing_baseline(payload)` 用例；
- API 增加 `GET/POST /api/cns-existing-baseline`；
- `invalidation_service` 增加 `existing_cns_baseline_changed` 键，失效链与现有 `existing_cns` **完全相同**（复用 `DEPENDENTS["existing_cns"]`）；
- 前端 Step05「已有设施」面板加一个显式声明控件（三态，默认 `not_declared`）。

---

## 7. Canonical Workflow DAG

### 7.1 输入层（Step 1–2）

```text
[外部来源事实]
  basemap(QGIS/QGZ) ─┐
  population(WorldPop GeoTIFF) ─┤
  terrain(GLO-30 DSM) ─┤
  terrain_dtm(FABDEM DTM) ─┤
  buildings / building_grid(GPKG) ─┤
  airspace(GPKG/SHP/GeoJSON) ─┤──→ source_audits（来源审计）
  traffic / conflict(JSON/CSV) ─┤         │
  land_mask(GPKG) ─┤                     │
  towers(CSV/XLSX/GPKG) ─┤               │
  reference_landing_sites / reference_routes(XLSX/CSV) ─┤
  equipment_reference_catalog(JSON) ─┤   │
  aircraft / devices(JSON catalog) ─┘    │
                                          ▼
workspace bbox ──→ grid（MH/T 4063，稳定 grid_id）
                     │
                     ▼
              grid_attributes（population / terrain / airspace / traffic / conflict /
                               buildings / property_exposure / infrastructure / towers）
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
   grid_risk（Risk V1）      grid_risk_v2（Risk Framework V2：factor → ground/air_traffic/environment_obstacle）
   ← 当前 planner 唯一输入      ← layered planner soft cost 输入
```

### 7.2 正式航路主链 DAG（Step 3）

```text
grid_attributes.population
  × shelter_coefficient_policy
  → population_shelter 场（按需派生，不持久化逐 cell）
        │
spatial_3d.altitude_layers（confirmed nominal_altitude_m + vertical_reference）
  → layered_route_planning_request（scenario/OD + 显式 selected layer）
        │
        ├── layered_route_feasibility_policy（terrain_vertical_clearance_m + building_clearance_policy）
        └── layered_route_cost_policy（λ_ground / λ_air_traffic / λ_environment_obstacle）
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │ LayeredRiskAwareThetaStarV2（默认 layered_route_planner）          │
  │  state = (grid_id, incoming_heading_bin)                          │
  │  LOS = supercover traversal（同时做风险积分与硬门控）              │
  │  J = 0.8·E_risk + 0.1·C_turn + 0.1·L                              │
  └──────────────────────────────────────────────────────────────────┘
        │
        ▼
  layered_route_candidates（强制 operational_route=false / continuous_validation_required=true）
        │
        ├──→ RouteRiskProfile（risk/route_profile.py + route_risk_profile_policy）
        │      → route_risk_profiles（domain/interval 风险画像）
        │
        ▼
  terrain/building validation（两段合成）
        ├── 巡航段：LayeredRouteValidation（复用 V3-C continuous_validators）
        │      → layered_route_validations（validated_candidate | failed | unresolved | validation_incomplete | not_ready | stale）
        └── 爬升/下降段：VerticalTransitionValidation
               → vertical_transition_validations（validated | failed | unresolved | not_ready | stale）
        │
        ▼
  LayeredOperationalAdoption（Apply 需 confirmed=true + expected_validation_fingerprint 防 TOCTOU）
        → layered_operational_adoptions（published | stale | revoked）
        │
        ▼
  operational_routes  ← ★ 正式 Operational Route（唯一权威运行航路）
        │
        ├──→ Route3DProfile（spatial_3d.route_3d_profiles，distance-parametric 薄层派生）
        ├──→ RouteVerticalProfile（显示用离散采样）
        ├──→ RouteOperatingLayer（route → layer 显式分配 + 离场/进场程序）
        └──→ RouteSafetyEvidenceV2（只读聚合四 domain，不合成 Safety Score）
```

**旁路（应在 UI 中明确隔离，不属主链）**：

| 旁路 | 产物 | 与主链关系 |
|---|---|---|
| RoutePlannerV1（二维 A\*） | `operational_routes`（legacy 二维） | **同写主链容器**——这是最大的入口歧义 |
| RiskAwareRoutePlannerV2（二维风险 A\*） | `operational_routes` | 同上 |
| RoutePlannerV3 A→B→C→D | `route_planner_v3_experiments` → `v3_operational_adoptions` **→ `operational_routes`** | V3-D 同样写主链容器 |
| 规划器对照实验 | `route_planning_experiments` | 纯 benchmark，不写运行航路 |

> **收敛原则（不推倒重写）**：主链已实现且工程质量最高，**保留 Theta\* V2 链为唯一正式航路主链**；三条旁路保留实现与 API（供研究与对照），但在前端收敛为「高级 / 实验」区，并在 A/B/C/D 与 V1/V2 的产物上强制标注 `operational_route_authority = layered_theta_v2_adoption_only`。

### 7.3 CNS 需求 → 能力 → 设施 DAG（Step 4–5）

```text
required_cns（project_default + route_overrides）  ← 权威输入
  ↑ Adopt（仅用户显式）
required_cns_recommendation ← OperationalContextRequiredCNSV2
  ↑
cns_operation_context + cns_requirement_policies

aircraft_profiles（selected） + device_catalog + existing_cns_facilities/{candidate_sites}
        │
        ├──→ coverage（CoveragePlannerV1，demo 级二维圆）        ← 待合并
        ├──→ coverage_3d（GeometricCoverage3DV1，EGM2008 几何）
        │       │
        │       ├──→ cns_service_capability（CNSServiceCapabilityV1）
        │       │       │
        │       │       └──→ service_timeline（+ operational_timing.route_motion_profiles / service_scenarios）
        │       │               │
        │       │               └──→ protection_envelope（+ response_time_budgets / encounter_scenarios）
        │       │
        │       └──→ cns_gap_analysis_v2（CNSGapAnalyzerV2，P7+P8+P9 保守区间合并）
        │               │
        │               └──→ cns_site_plan（ReuseFirstSitePlannerV1）    ← OPTIONAL 支线
        │
        ├──→ cns_corridor_assessment（CNSServiceCorridorV1，P14 corridor voxel）
        │       │
        │       └──→ cns_corridor_gap_assessment（CNSCorridorGapAnalyzerV1，P15 + cns_planning_objectives）
        │               │
        │               └──→ cns_corridor_site_plan（CorridorReuseFirstSitePlannerV2，P16）
        │                       │
        │                       └──→ closed_loop_assessment（P12 what-if 重跑 + 事务式 Apply）
        │
        └──→ radar_surveillance_layout（+ radar_surveillance_policy + towers + land_mask + FABDEM）
```

### 7.4 失效传播 DAG（现状 vs 目标）

**声明式层**（`services/invalidation.py:8-52`，52 条上游键 → 18 个结果名）：

```text
data / workspace ─→ environment_risk, routes, coverage, cns_gap, coverage_3d, cns_service_capability,
                    service_timeline, cns_gap_v2, cns_site_plan, cns_corridor_assessment, report
route            ─→ coverage, cns_gap, coverage_3d, …, required_cns_recommendation, report
rules            ─→ routes, coverage, …, technical_risk, report
aircraft_profile ─→ routes, coverage, …, technical_risk, report
required_cns     ─→ coverage, cns_gap, …, cns_corridor_site_plan, report
devices          ─→ coverage, …, cns_corridor_site_plan, technical_risk, report
existing_cns     ─→ coverage, …, technical_risk, report
candidate_sites  ─→ coverage, cns_site_plan, cns_corridor_site_plan, technical_risk, report
route_algorithm  ─→ routes, coverage, …, report
layered_route_planner_algorithm ─→ report
coverage_algorithm / gap_algorithm / coverage_model / service_model / timeline_model ─→ 各自下游
corridor_policy / corridor_model / corridor_result / planning_objectives / corridor_gap_analyzer
cns_corridor_gap_result ─→ cns_corridor_site_plan
requirement_context / requirement_policies / requirement_model ─→ required_cns_recommendation
safety_policy    ─→ safety_assessment, technical_risk, report
motion_profile / service_scenario / response_time_budget / encounter_scenario ─→ service_timeline / protection_envelope
```

**命令式层**（`invalidation_service.py`）：43 个专用方法 + 9 个运行期注入的 invalidator（`layered_route_invalidator`、`route_risk_profile_invalidator`、`layered_route_validation_invalidator`、`route_3d_profile_invalidator`、`vertical_transition_validation_invalidator`、`route_safety_evidence_invalidator`、`radar_surveillance_layout_invalidator`、`v3_adoption_invalidator`、`adoption_invalidator`），全部在 `workflow_service.py:387-541` 逐条 `setattr` 接线。

**实测缺口（需要在 Phase4-B 补的边）**：

| 缺口 | 证据 | 后果 |
|---|---|---|
| `operational_timing` 容器本身无键级登记 | `services/invalidation.py` 全文 `operational_timing` 0 命中；只有 `motion_profile`/`service_scenario`/`response_time_budget`/`encounter_scenario` 四个子键 | 容器内新增子结构（如新的 timing 字段）容易漏失效 |
| `route_planning_experiments` 未登记 | 不在 `DEPENDENTS` 任何 value 中 | 实验容器在输入变化后保持"新鲜"，可能误导对照 |
| `v3_cns_assessment_bundle` 未登记 | 同上 | V3-D 评估包不随上游失效 |
| `reference_route_links` 未登记 | 同上 | 参考航线关联不随 OD 变化失效 |
| `source_audits` 自身不被上游 stale | 同上 | 来源变更后审计状态保持旧值 |
| `safety_assessment` 仅由 `safety_policy` 触发 | `:51` | 上游能力变化不失效安全评估 |
| `cns_planning_reports` 历史个体 | 仅 `mark_active_report_stale()` 处理 active | 历史报告无 stale 标记 |
| `traffic_simulation` 仅由 traffic 源分支触发 | `SOURCE_ATTRIBUTES` 中 `traffic_simulation` → `("traffic","conflict")` | 工作区变化时可能不失效 |
| Radar / Route3DProfile / VerticalTransition / SafetyEvidence / layered_* / tower_* 全部绕开 `DEPENDENTS` | `invalidation_service.py:38-65` | `DEPENDENTS` 已**不是**单一事实来源 |

**Phase4-B 目标**：把命令式接线**反向投影**为声明式表（`staled_by` / `downstream_of`），保留现有方法作为执行器（不改行为），新增派生结果时只需登记一行。

---

## 8. 前端六步重构映射

### 8.1 目标六步 ← 现有 step 映射

| 目标业务链 | 现有 step | 变更性质 |
|---|---|---|
| **1 数据准备** | step01 项目准备 + step02 的数据源/映射部分 | 重新划分（数据源中心从 Step01/跨步收敛为独立一步） |
| **2 环境与风险** | step02 的工作区/网格/Risk V1/V2/高度层 | **保留，几乎不变** |
| **3 航路规划与发布** | step03（收敛：OD → Theta\* V2 → RouteRiskProfile → validation → adoption → operational route） | **收敛**（V1/V2 A\*、V3 下沉高级/实验） |
| **4 CNS需求** | step04 的 aircraft/required_cns/requirement policy/rules | **聚焦**（从"运行规则"改为"CNS需求"；安全/DAA 移入高级） |
| **5 CNS能力评估与设施规划** | step05（coverage_3d → capability → timeline → corridor → corridor gap → P16 site plan → radar） | **聚焦**（移除 legacy 站址链与保护包络重复入口） |
| **6 方案评审与报告** | step06 | **保留，几乎不变**（合并顶栏重复入口） |

> 结论：**新链与现有六步是近似一一对应的**，不需要重建导航结构；主要工作是"重命名 + 分区收敛 + 去重 + 术语清理"。

### 8.2 逐 step 面板归属表

**Step 1 数据准备**（现有 step01 + step02 数据部分）

| 面板 | 现有位置 | 归属 | 动作 |
|---|---|---|---|
| 项目与存储位置 | step01 操作 | ✅ production | 保留 |
| 地名定位（天地图） | step01 操作 | ✅ production | 保留 |
| 数据健康 | step01 结果 | ✅ production | 保留 |
| 规划输入就绪 | step01 结果 | ✅ production | 保留 |
| 算法选择（9 类下拉 + schema 原文） | step01 高级 | ⚠️ production 但需改造 | **不放 JSON schema 原文**；改为业务化标签；补 4 个缺失 type |
| 数据源中心（QGIS/人口/DEM/FABDEM/建筑/铁塔/参考数据） | 跨步 + `source_center.js` 对话框 | ✅ production | **提升为 Step1 主体**（当前分散） |
| 数据就绪诊断（`dataReadinessPanel`） | step03 结果→可行性与净空 | ⚠️ 错位 | **移到 Step1**；新增 existing CNS 基线声明块 |

**Step 2 环境与风险**（现有 step02，基本不变）

| 面板 | 归属 | 动作 |
|---|---|---|
| 工作区范围（框选/清除/保存 + 破坏性确认） | ✅ production | 保留 |
| 标准网格与建筑环境 | ✅ production | 保留（术语：MH/T L8 → "规划网格"） |
| 数据映射（6 张状态卡 + NoData 面板） | ✅ production | 保留；NoData 策略标注为"可假设" |
| 专题浏览（8 项 + Risk V2 选项） | ✅ production（含 Legacy Risk V1 选项） | Legacy 选题降为"高级/对照" |
| 风险框架（V2 主卡 + Legacy V1 对照卡 + V2 工程面板） | ✅ production | **V1 卡降为"高级：旧版风险模型"** |
| 高度层（layer id / 垂向基准 / nominal / 上下界） | ✅ production | 保留（主链前置） |

**Step 3 航路规划与发布**（现有 step03 收敛）

| 面板 | 现位置 | 现分类 | 目标分类 | 动作 |
|---|---|---|---|---|
| 起降点与 OD（参考起降点/项目起降点/OD 创建） | 操作 | P | **P** | 保留 |
| 分层候选（Theta\* V2 面板） | 操作 | P | **P（唯一正式候选入口）** | 保留并显名"正式航路候选" |
| 分层候选（V1 A\* 面板） | 操作 | L | **高级：旧版分层规划器** | 移入高级区 |
| 运行航路（3 个入口并列） | 操作 | L+P+D | **P（仅 Layered Candidate 发布）** | 「现有/Legacy 运行航路生成」与 V3-D 发布移入高级/实验 |
| 高度与程序（巡航高度层 + Route3DProfile） | 操作 | P | **P** | 保留 |
| 当前航路 + 航路剖面 | 结果 | P | **P** | 保留 |
| 可行性与净空（就绪/建筑净空/塔净空/巡航验证） | 结果 | P | **P** | 保留（就绪诊断移 Step1） |
| 路径风险画像（RouteRiskProfile） | 结果 | P | **P（主链第 4 段）** | 保留 |
| 对比与验证（参考航线对比 + 规划器并列） | 结果 | D | **高级** | 移入高级区 |
| 参考数据与关联 | 高级 | P | 高级 | 保留 |
| Legacy / Risk-Aware V2 参数 | 高级 | L | 高级 | 保留并显式标注"非正式主链" |
| V3 实验（V3-A/B/C/D + 规划器比较实验） | 高级 | X | 高级：实验 | 两套实验面板**合并为一个** |
| 规划诊断 | 高级 | P | 高级 | 保留 |
| 剖面与运动（Route 3D Altitude Profile + Route Motion Profile） | 高级 | X+P 混排 | 高级 | 明确分层（profile=P，motion=X） |

**Step 4 CNS需求**（现有 step04 聚焦）

| 面板 | 现位置 | 目标 | 动作 |
|---|---|---|---|
| 飞行器能力（AircraftCNSProfile + 厂家/型号/速度/MTBF） | 操作 | **P** | 保留；MTBF 的 legacy `lambda_per_hour` 标注为兼容字段 |
| 飞行规则（高度/间隔/时延 + Legacy λ 摘要） | 操作 | **P** | 保留；Legacy λ 降为高级注释 |
| CNS 需求 + 地面设备能力 | 结果 | **P（本步主体）** | 保留并提升为主面板 |
| 需求建议（Policy Panel + Evaluate/Adopt） | 结果 | **P** | 保留 |
| 服务走廊（corridorPolicyPanel） | 结果 | **重复** | **移出 Step4**，归 Step5 |
| 时间与场景（operationalTiming + ProtectionBudget + DAA Lab） | 高级 | P + X | 保留在高级；DAA Lab 显式标"实验" |
| 安全与耦合（SafetyPolicy + FTA/FMEA + Coupled Preview） | 高级 | P | 保留在高级 |
| V3 CNS 评估（`routePlannerV3CnsSummary`） | 高级 | X | 移入实验区 |

**Step 5 CNS能力评估与设施规划**（现有 step05 收敛）

| 面板 | 现位置 | 目标 | 动作 |
|---|---|---|---|
| 设备与参数（含 `planCoverage`→二维圆覆盖） | 操作/结果 | **MERGE** | 二维圆覆盖降为"高级：旧版二维覆盖"；主结果改为 `coverage_3d` |
| 已有设施（导入） | 操作 | **P** | **新增三态基线声明**（未声明/确认无/确认有） |
| 候选站址（导入/从已有生成 + 共塔候选） | 操作 | **P** | 保留 |
| 基础覆盖（coverageResult + gapPanel） | 结果 | **P（改造）** | gapPanel 收敛为 V2 |
| 3D 与能力（coverage3dPanel + capabilityPanel） | 结果 | **P（本步主体）** | 保留并提升 |
| 服务走廊（corridorPanel） | 结果 | **P（唯一入口）** | 从 Step4 合并到此 |
| 监视雷达初步划设 | 结果 | **P** | 保留 |
| 规划目标与缺口（planningObjectives + corridorGapSummary） | 结果 | **P** | 保留 |
| 走廊站址优化（"生成 P16 Proposal"） | 高级 | **P（应提升）** | 术语去除 P16；提升为结果区主链末端 |
| 运行时间线 | 高级 | P | 保留 |
| 保护与 Gap V2 | 高级 | **重复**（保护包络已在 Step4） | 保护包络保留在 Step4；本处只留 Gap V2 |
| Legacy 与闭环（sitePlanPanel V1 + closedLoopPanel） | 高级 | **L + X** | V1 站址降为"高级：旧版站址规划"；闭环保留 |

**Step 6 方案评审与报告**（现有 step06）

| 面板 | 目标 | 动作 |
|---|---|---|
| 评审概览 / 方案比较 / 方案编辑 / 确认与应用 | P | 保留（Select ≠ Confirm ≠ Apply 语义保持不变） |
| 状态总览 + 路线安全证据 | P | 保留；术语去 P 编号 |
| 报告与交付 | P | 保留；**移除顶栏重复入口**（`main.js:430` 与 `index.html:16`） |
| 需求依据 / 布站提案证据 | P | 保留；"P16 走廊站址规划提案"改为业务名 |

### 8.3 技术术语 → 业务语言映射表

| 现状术语 | 出现位置（示例） | 建议业务语言 |
|---|---|---|
| Theta\* / Theta\* V2 | `layered_theta_v2.js:31,661,676,699,755,1011` | **分层航路规划**（正式） |
| A\* | `layered_route_planner.js:374,378,521`、`layered_theta_v2.js:1060` | 网格搜索（仅在高级/对照区保留） |
| any-angle | `layered_legend.js:46` | 任意角度航段 |
| voxel / 体素 | `step05_cns.js:344,365,397,398,400`、`step06_review.js:44,109,126,426` | **服务单元** |
| LOS / 视线 | `layered_theta_v2.js:755,996,1055,1069-1073` | 航段视线检查 |
| supercover | `layered_theta_v2.js:1004` | 网格穿越（审计明细） |
| heuristic | `step03_routes.js:667,1159` | 启发函数 → 搜索代价估计 |
| mask / 掩码 | `index.html:75`、`layered_route_planner.js:378,429-431` | **可行性层** |
| coarse | `index.html:75` | **战略层**（非精确） |
| fingerprint | `index.html:89`、`step06_review.js:339-358`、`route_risk_profile.js:718`、`layered_theta_v2.js:1076-1086` | **输入指纹（审计）** |
| readiness | `index.html:89`、`step03:1062,1069`、`layered_theta_v2.js:671,834,1098` | **就绪状态** |
| display_only | `index.html:89`、`step05:72` | 仅显示，不参与计算 |
| pending_confirmation | `index.html:89,152,180` | 待人工确认 |
| fail-closed | `index.html:89` | 缺数据即拒绝（安全默认） |
| what-if | `step05:400` | 试算 / 假设推演 |
| TOCTOU 防护 | `step03:1351,1532` | 版本校验（防并发改动） |
| schema（Parameters 原文） | `step01_project.js:19` | **不展示 JSON schema 原文** |
| JSON 原文泄漏 | `step01_project.js:19`（`JSON.stringify(parameter_schema)`） | 改为业务化参数表单 |
| Markdown 泄漏 | `index.html:163`（`**层级无关的建筑事实精确聚合**`） | 改为 `<strong>` |

### 8.4 P 编号清理清单

**应保留的真业务编号**：`MH/T 4063.1`、`L6/L7/L8`、`EGM2008`、`EPSG:4326/3855/3850/4490`、`ARP4761A`、`JARUS`、`U-space`。

**应从用户可见文本中移除的开发阶段编号**（保留在代码注释与证据 JSON 中）：

| 编号 | 位置 | 替换建议 |
|---|---|---|
| P4 | `step04:103,241` | "运行服务状态" / "可靠性规范" |
| P6 | `step04:86` | "功能耦合" |
| P7 | `step03:281,291,1699`；`step04:189,208,209`；`step05:403,404,405`；`step06:180` | "三维几何覆盖" |
| P8 | `step03:281,291`；`step04:103,190,208,209`；`step05:401,403,404,405`；`step06:180` | "静态服务能力" |
| P9 | `step03:281,291,1739`；`step04:191,208,209`；`step05:403,405`；`step06:180` | "运行时间线" |
| P10 | `step03:281,291`；`step04:192,208,209`；`step05:404,405`；`step06:180` | "保护包络" |
| P11 | `step05:323` | "站址提案（规划缺口）" |
| P12 | `step05:143,281,323-324,404,405` | "闭环复核" |
| P13 | `step03:1699` | "二维风险感知规划" |
| P14 | `step05:356,365,400`；`step06:180` | "服务走廊" |
| P15 | `step05:400` | "走廊缺口" |
| P16 | `step05:400`；`step06:424` | "走廊站址规划提案" |
| P18 | `step06:428` | "方案评审" |
| P19 | 未发现 | — |

> 注意：`step05:9,66`、`step03:285`、`step04:285` 的代码注释已声明"工程编号只留在说明与证据里"，但实际 UI 与注释**自相矛盾**——这本身就是需要在 Phase4-B 收口的事项。

### 8.5 重复入口收敛表

| 能力 | 现有入口 | 收敛为 | 处置 |
|---|---|---|---|
| 生成运行航路 | ① `/api/workflow/operational` ② layered adoption apply ③ V3-D adoption apply | **② layered adoption apply** | ①③ 下沉高级/实验并加显著标注 |
| 高度剖面 | ① `/api/spatial-3d/route-profile` ② `/api/spatial-3d/route-operating-layer` ③ `/api/route-3d-profiles/evaluate` | **③ Route3DProfile（主链）+ ② 为配置** | ① 作为 legacy 剖面移入高级 |
| 覆盖评估 | ① `mutate('coverage')`（二维圆） ② `/api/coverage-3d/evaluate` | **②** | ① 降为"高级：旧版二维覆盖" |
| 布站提案 | ① `/api/cns-site-plan`（V1） ② `/api/cns-corridor-site-plan/evaluate`（P16） | **②** | ① 降为"高级：旧版站址规划" |
| 保护包络 | ① `step04:284` ② `step05:459` | **①** | 移除 ② |
| 服务走廊 | ① `step04:287`（带 policy） ② `step05:454`（无 payload） | **①（在 Step5 呈现）** | 合并为一个入口，policy 与结果同屏 |
| 报告生成 | ① 顶栏导出菜单（`main.js:430`） ② step06 报告面板（`:470`） | **②** | 移除顶栏重复项 |
| 数据导出 | ① 顶栏（`main.js:383-391`） ② step06（`:409` 三个 anchor） | **②** | 移除顶栏重复项 |
| 算法选择 | ① step01 高级 ② step03 Legacy 参数（`:1896`） ③ layered Theta\* V2 搜索参数 | **① 统一入口 + 各面板只读展示当前生效值** | ②③ 改为"跳转到算法设置"链接 |
| 规划实验 | ① `experimentPanel`（`route-experiments`） ② `experimentPanelV3`（`route-planner-v3-experiments`） | **合并为一个"实验"面板 + 两个 tab** | 减少导航分支 |
| 运行时间线 | ① `step03:1900`（只写 route_motion_profiles） ② `step04:281-283` | **②** | ① 保留为剖面输入配置，明确标注 |
| 参考航线预览 | ① source_center 对话框 ② step03 高级 import-confirm | 保留两者（不同阶段） | 加交叉链接 |
| 净空可视化 | ① step03 面板 ② 地图图层 | 保留两者 | 面板加"在地图显示"按钮 |

---

## 9. 建议删除/归档顺序

原则：**每一步都保证主链可运行、测试可绿、可回滚**；先做"零行为变更"的收敛，再做"有测试同步"的删除。

### 阶段 0：工作树与仓库卫生（零风险，不涉及代码）

| # | 动作 | 对象 | 依据 |
|---|---|---|---|
| 0.1 | 清理 `_dsh_prof/`（含完整源码副本，363 文件 / 9.99 MB） | 未跟踪临时目录 | `git status` `?? _dsh_prof/` |
| 0.2 | 清理 `_dsh_prev_corridor.diff`、`_dsh_prev_others.diff`、`qgis_polygon_inventory.json`、`ersyidingDocumentsChatGPTCNS规划系统` | 未跟踪临时文件 | 同上 |
| 0.3 | 清理约 30 个 `_*` / `tmp*` / `pytest-*` 目录 | 测试残留 | 目录枚举 |
| 0.4 | 补 `.gitignore` 覆盖 `_dsh_*`、`_probe*`、`_vbase*`、`_bt_*`、`outputs/` | `.gitignore` | 防止再次污染 |
| 0.5 | **先处理其他 AI 的 4 个已修改文件**（提交或回退，由人类决定） | `corridor/v1.py`、`coverage/geometric_3d.py`、`service_capability/v1.py`、`test_cns_corridor.py` | 避免与 Phase4-B 改动混叠 |

### 阶段 1：零行为变更的收敛（无删代码）

| # | 动作 | 范围 | 风险 |
|---|---|---|---|
| 1.1 | UI 术语业务化（§8.3 映射表） | `web/js/workflow/*.js`、`index.html` | 低（文案） |
| 1.2 | 移除 P4–P18 泄漏（§8.4） | 同上 | 低（文案） |
| 1.3 | 前端算法下拉补 4 个缺失 type | `step01_project.js:12` | 低 |
| 1.4 | 移除 `parameter_schema` JSON 原文展示、修复 `**` Markdown 泄漏 | `step01_project.js:19`、`index.html:163` | 低 |
| 1.5 | 重复入口收敛（§8.5） | `main.js`、`step04`、`step05`、`step06` | 中（需回归前端测试） |
| 1.6 | 旁路入口加显式标注（"非正式主链"） | `step03` 运行航路三入口 | 低 |
| 1.7 | 新增 `ready_with_assumptions` 档位（复用已有 `assumptions` 字段） | 各 readiness 服务 + 前端徽章 | 中 |

### 阶段 2：删除空壳与真孤儿（需同步测试）

| # | 删除对象 | 同步修改 | 风险 |
|---|---|---|---|
| 2.1 | `algorithms/route_planner.py`（3 行 shim） | `tests/test_architecture.py:8,15` | 低 |
| 2.2 | `algorithms/coverage_planner.py`（3 行 shim） | `tests/test_architecture.py:6,15` | 低 |
| 2.3 | `algorithms/contracts.py`（含反向悬空 import） | `tests/test_skeleton.py:6` 改为直接 import registry | 低 |
| 2.4 | `gap/model_v2.py`（0 import、0 测试） | 无 | 低 |
| 2.5 | `gap/model.py`（仅再导出） | `gap/__init__.py:3`、`domain/__init__.py:6` | 低 |
| 2.6 | `risk/model.py`（仅再导出） | `risk/__init__.py:3`、`tests/test_risk_model.py:6` | 低 |
| 2.7 | `safety/reliability.py`（生产调用点 0） | `safety/__init__.py:11`、`tests/test_cns_reliability_service_state.py`（移除或改为直接测 `service_state`） | 中（需人类确认"可靠性不做产品"） |
| 2.8 | `application/review_service.py` | `workflow_service.py:29,787,1420` | 中（snapshot `review` 字段消失，需检查前端） |

### 阶段 3：legacy 算法归档（冻结为只读兼容源）

| # | 对象 | 动作 | 阻塞项处理 |
|---|---|---|---|
| 3.1 | `CoveragePlannerV1` | 保留实现，`maturity` 保持 `demo`，从 `coverage` 门禁中移除；前端降为"高级：旧版二维覆盖" | 解除阻塞 G4（`workflow_service.py:1047`） |
| 3.2 | `CNSGapAnalyzerV1` | 从"默认"降级为"仅显式选择"（或直接默认 V2）；前端收敛为单一缺口入口 | 需同步 `test_algorithm_registry.py:104,108`、`test_cns_gap_analysis.py` |
| 3.3 | `ManualRequiredCNSV1` | 保留为默认（默认值契约成本最低），但 UI 不再展示；或改默认 V2 并同步测试 | 需人类决策（影响所有旧项目默认行为） |
| 3.4 | `ReuseFirstSitePlannerV1` | 前端降为"高级：旧版站址规划" | 低 |
| 3.5 | `LayeredRoutePlannerV1` | ① 移除 service 构造默认与兜底实例（`layered_route_planner_service.py:137,168`）② 前端 V1 面板移入高级 ③ 保留读取旧项目 selection 的能力 ④ 公布弃用窗口 | 阻塞项 C（兜底路径）+ D（前端分流）+ E（旧项目） |
| 3.6 | `RoutePlannerV1` | 保留默认地位，但 UI 明示"二维快速参考线，非正式航路" | 阻塞项 A 不动 |
| 3.7 | `RiskAwareRoutePlannerV2` | **先抽 `GridGraph` 为独立模块**，再把 V2 降为可选算法 | 阻塞项 B（关键前置） |
| 3.8 | `services/` facade（11 个） | 主版本升级时删除；先在文档标注 deprecated | 8 个测试仍 import（见 §10.4 风险） |
| 3.9 | `legacy/` | 保持隔离，不新增依赖 | 无 |

### 阶段 4：V3 家族下沉与治理收口

| # | 动作 | 依据 |
|---|---|---|
| 4.1 | 把 `route_planner_v3` 全家族在算法目录与 UI 中统一标为"进阶/实验" | 全家族未注册（§1.4） |
| 4.2 | **V3-D 的 publish 路径纳入显式 owner 声明**（或在 registry 补 manifest） | "未注册却可生产发布"（§4.2.5） |
| 4.3 | 为 `route_planner_v3/planner.py`、`corridor.py`、`cost.py`、`fine_grid.py`、`risk/domains_v2.py` 补直接测试 | 测试薄弱（`test_route_planner_v3_service.py` 覆盖不足） |
| 4.4 | V3 实验容器加条数上限 + 超限归档到 sidecar | 无界增长（§5.3 P3） |

### 阶段 5：ProjectState 瘦身

| # | 动作 | 预期收益 | 风险 |
|---|---|---|---|
| 5.1 | `reference_routes` / `reference_landing_sites` → sidecar + 索引 | −566 KB（−47%） | sidecar 丢失 → 需内联回退 |
| 5.2 | `equipment_reference_catalog` / `device_catalog` / `aircraft_profiles` → 引用 canonical JSON | −39 KB | 源文件移动 → 需路径重解析 |
| 5.3 | `source_audits` 明细 → sidecar（主文档留摘要） | −10 KB | 审计追溯需读 sidecar |
| 5.4 | 扩展 compaction：`grid.cells`、`grid_attributes.*.cells`、`grid_risk.cells`、`coverage_3d` 采样、`radar_surveillance_layout` 单元格 | 真实工作区下 −80%+ | 需同步 `project_repository._result_artifact_path` |
| 5.5 | `spatial_3d` 内 IN/PO/DA 语义拆分（additive key，不改 schema 版本） | 语义清晰 | 需 additive 回填 |

### 阶段 6：失效传播声明式化

| # | 动作 | 依据 |
|---|---|---|
| 6.1 | 把 43 个命令式方法与 9 个注入式 invalidator 反向投影为声明式 `staled_by` 表 | §7.4 |
| 6.2 | 补齐 8 条缺口边（`operational_timing`、`route_planning_experiments`、`v3_cns_assessment_bundle`、`reference_route_links`、`source_audits`、`safety_assessment`、`cns_planning_reports`、`traffic_simulation`） | §7.4 |
| 6.3 | 新增 existing CNS 基线声明（§6.4）并接入失效链 | §6.4 |

### 阶段依赖图

```text
阶段0（卫生）
  → 阶段1（零行为收敛）
      → 阶段2（删空壳）
          → 阶段3（legacy 归档）
              → 阶段4（V3 治理）
                  → 阶段5（ProjectState 瘦身）
                      → 阶段6（失效声明式化）
```

**可并行**：阶段 2 与阶段 1 可部分并行；阶段 5 与阶段 3/4 无强依赖，可并行。

---

## 10. Phase4-B 最小开发范围

### 10.1 范围内（建议纳入 Phase4-B）

| # | 工作项 | 对应章节 | 复用现有实现 | 预估产出 |
|---|---|---|---|---|
| **B1** | **主链显名与旁路隔离**：在 UI/文档中把 Layered Theta\* V2 → RouteRiskProfile → validation → adoption → operational route 定为唯一正式航路主链；三条旁路（V1 二维、V2 风险、V3 A/B/C/D）统一移入「高级/实验」并加显著标注 | §2.1、§8.2、§8.5 | 全部已实现，仅改前端分区与文案 | 前端 6 个文件；1 份主链说明文档 |
| **B2** | **readiness 档位补齐**：新增 `ready_with_assumptions` 与统一 `not_evaluated` 汇总；把已有的 `assumptions` / `parameter_origin` / `confirmed=false` 字段归一化到一个公共结构 | §6.3 | 数据已存在于 4 种字段 | 1 个公共 helper + 各 readiness 快照 1 处调用 + 前端 1 个徽章组件 |
| **B3** | **existing CNS 基线声明**：新增 `cns_existing_baseline`（`not_declared` / `confirmed_none` / `confirmed_present`）+ API + Step05 控件 + 失效链（复用 `DEPENDENTS["existing_cns"]`） | §6.4 | 复用 `cns_input_service` / `invalidation_service` / `data_readiness` 模式 | 1 个 additive state key + 1 个 API + 1 个面板控件；旧项目一律 `not_declared` |
| **B4** | **ProjectState 瘦身（第一批）**：只读容器 sidecar 化（`reference_routes`、`reference_landing_sites`、`equipment_reference_catalog`）+ compaction 扩展到 `grid.cells` / `grid_attributes.*.cells` / `grid_risk.cells` | §5.3、§5.4 | 复用 `project_compaction.py` 机制与 `slim_*` 投影 | `current_project.json` 从 1.2 MB → 约 150–250 KB（无真实工作区时） |
| **B5** | **UI 术语与 P 编号清理 + 重复入口收敛** | §8.3、§8.4、§8.5 | 纯文案与分区 | 8 个前端文件 |
| **B6** | **死代码删除**：`algorithms/route_planner.py`、`algorithms/coverage_planner.py`、`algorithms/contracts.py`、`gap/model.py`、`gap/model_v2.py`、`risk/model.py` + 同步 3 个测试文件 | §3.7、§9 阶段 2 | 无 | 删 6 文件，改 3 测试 |
| **B7** | **`GridGraph` 拆分**：`risk_aware_v2.py:23` 抽为 `route_planner/grid_graph.py`，V2 本体保留 | §4.2.2 | 纯搬移 + re-export | 1 个新模块 + 3 处 import 调整 |
| **B8** | **失效链补齐**：补 8 条缺口边为声明式登记（不改现有行为） | §7.4 | 复用 `DEPENDENTS` 表 | 1 个文件改动 + 1 个回归测试 |
| **B9** | **测试补强**：为 `route_planner_v3/planner.py`、`risk/domains_v2.py` 补直接测试；把 `step03_routes.js` 的模型投影部分抽为独立模块并加单测 | §3.5、§4.4 | 无 | 2 个新测试文件 + 1 个前端模块拆分 |

### 10.2 范围外（明确不做）

| 不做项 | 理由 |
|---|---|
| 推倒重写任何算法或服务 | 主链工程质量已高（`AI_DEV_CONTEXT.md` §11.0 已冻结为 `Route Planning V1.0 frozen baseline`） |
| 改变 Theta\* V2 / RouteRiskProfile / validation / adoption 的算法语义 | 冻结基线，仅允许 bug 修复、真实数据适配、性能与 UI |
| 开发 V3-E、Dubins、clothoid、3D search、CNS/energy 联合优化 | 明列 future work |
| 删除 `RoutePlannerV1` / `LayeredRiskAwareThetaStarV2` / V3 家族 | 阻塞项未解除（§4.2） |
| 改变 `algorithm_selection` 默认值 | 影响所有旧项目；需单独决策 |
| 引入新框架 / 构建步骤 / 状态管理库 | 前端为原生 ES Modules 无构建，保持不变 |
| 真实的 `nominal_altitude_m` / λ / clearance 等工程数值 | 必须人工工程确认，禁止补默认值 |
| `services/` facade 整体删除 | 8 个测试仍 import，需先迁移（可留到后续版本） |

### 10.3 验收标准

| # | 验收项 | 判据 |
|---|---|---|
| A1 | 主链唯一性 | UI 中"生成运行航路"只有 1 个权威入口；两条旁路入口带"非正式主链"标注 |
| A2 | readiness 三档 | 至少 5 个 readiness 快照能返回 `ready_with_assumptions` 并列出假设清单；Step06 有统一"未评估清单" |
| A3 | existing CNS 语义 | `not_declared` 下任何覆盖/缺口结论不得为 `passed`；`confirmed_none` 下可正常出结论 |
| A4 | ProjectState 体积 | `projects/current_project.json` 在**同一项目内容**下 ≤ 400 KB（当前 1.2 MB） |
| A5 | 术语零泄漏 | 用户可见文本中 `Theta*` / `voxel` / `LOS` / `supercover` / `heuristic` / `fingerprint` / `P4`–`P18` 命中数为 0（注释与证据 JSON 除外） |
| A6 | 死代码 | 6 个文件删除后 `python -m pytest -q` 全绿（含 `node --test`） |
| A7 | 失效完整性 | 8 条缺口边全部登记；新增 1 条"派生结果未登记"的自动检查测试 |
| A8 | 主链回归 | `test_layered_theta_star_v2.py`、`test_route_risk_profile.py`、`test_layered_route_validation_adoption.py`、`test_vertical_transition_validation.py`、`test_layered_route_planner.py`、`test_zhoushan_golden_case_e2e.py` 全绿且**输出指纹不变** |

### 10.4 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| 前端 `.test.mjs`（129 KB + 185 KB）是**源码字符串匹配式伪覆盖**，文案改动会大面积红测 | **高** | B5 前先评估这两个文件的断言方式；必要时把它们转换为"行为断言"而非字符串断言；改动分批提交 |
| `services/` facade 被 8 个测试直接 import | 中 | 阶段 3.8 不做，留到后续版本 |
| `project_compaction` 扩展后 Save As / Open 需带回 sidecar | 中 | 复用 `ProjectDirectoryService.SPATIAL_SOURCE_KEYS` 白名单机制；sidecar 缺失时内联回退 |
| 删除 `safety/reliability.py` 可能违背"可靠性是 P4 能力基线"的产品定位 | 中 | 需人类决策：要么补 service + API（提升为 ADVANCED），要么删除并记录 |
| 默认 selection 迁移（Gap V1 → V2、Manual → Operational）改变旧项目行为 | 中 | B 阶段**不做**默认迁移；只降级 UI 呈现 |
| 其他 AI 的 4 个未提交修改与新改动冲突 | **高** | 阶段 0.5 先由人类处理这 4 个文件 |
| 主链"显名"被误解为"删除其他实现" | 低 | 报告与 UI 文案明确"保留实现、仅收敛入口" |

### 10.5 建议的 Phase4-B 交付顺序

```text
B0  阶段0 仓库卫生 + 处理 4 个遗留修改（人类决策）
 ↓
B1  主链显名与旁路隔离（前端分区 + 标注）        ← 无后端风险，最先做
 ↓
B2  UI 术语 / P 编号清理 + 重复入口收敛          ← 与 B1 同批提交
 ↓
B3  readiness 三档 + not_evaluated 汇总          ← 后端小改 + 前端徽章
 ↓
B4  existing CNS 基线声明                        ← additive，独立可验收
 ↓
B5  失效链补齐（8 条边）                         ← 纯登记，行为不变
 ↓
B6  GridGraph 拆分                               ← 纯搬移，解锁后续 V2 降级
 ↓
B7  死代码删除（6 文件 + 3 测试）                ← 全绿后提交
 ↓
B8  ProjectState 瘦身（sidecar + compaction 扩展）← 独立可验收
 ↓
B9  测试补强 + step03 拆分                       ← 长期健康度
```

---

## 附录 A：关键证据索引（便于复核）

| 主题 | 位置 |
|---|---|
| 算法注册表与默认选择 | `cns_planner/algorithms/registry.py:35,139-169,211-231` |
| ProjectState 空状态 | `cns_planner/application/project_state.py:205-372` |
| ProjectState 回填 | 同上 `:419-728` |
| 服务装配 | `cns_planner/application/workflow_service.py:266-541` |
| 快照投影与 slim | 同上 `:100-259,580-795` |
| `DEPENDENTS` 声明式失效图 | `cns_planner/services/invalidation.py:8-52` |
| 失效执行与注入式 invalidator | `cns_planner/application/invalidation_service.py:15-65,67-130,200-448` |
| API 路由分发 | `cns_planner/api/router.py:26-181,233-512` |
| 项目压缩 sidecar | `cns_planner/persistence/project_compaction.py` |
| 数据源注册表 | `cns_planner/data/registry.py:20-71,74-142` |
| 数据就绪 | `cns_planner/application/reference_link_service.py:120-194` |
| 主链 readiness blockers | `cns_planner/application/layered_route_planner_service.py:333-415,1040-1080` |
| 合成航路主链 V2 输入契约 | `cns_planner/algorithms/registry.py:366-431`（Theta\* V2 manifest） |
| UI 术语与 P 编号 | `cns_planner/web/js/workflow/*.js`、`cns_planner/web/index.html` |
| 已知技术债（官方自述 45 项） | `AI_DEV_CONTEXT.md:1183-1268` |
| 冻结基线声明 | `AI_DEV_CONTEXT.md:1254-1268` |

## 附录 B：本审计的四个并行只读子审计

| 子审计 | 覆盖范围 | 关键产出 |
|---|---|---|
| 前端六步 | `web/index.html`、`main.js`、`shell.js`、`workflow/*`（含 165 KB 的 step03）、`state/*`、`map/*` | 六步面板分类、14 类术语泄漏、14 项 P 编号泄漏、15 项重复入口 |
| 航路家族 | `algorithms/route*`、`route_planner/*`、`layered_route_planner/*`、`route_planner_v3/*`、13 个 route/layered application 服务 | 五条算法路径的删除阻塞项、V3-A/B/C/D 边界、`GridGraph` 隐性耦合 |
| CNS 家族 | `coverage`、`service_capability`、`requirements`、`corridor*`、`radar_layout`、`gap`、`site_planner`、`risk`、`safety`、`simulation`、`encounter_3d` | 五组"V1 vs V2"关系判定、Reliability 真孤儿、Radar 规模（不含测试 ≈6308 行） |
| API/测试/失效 | `api/router.py`、`tests/*`（116 文件）、`services/invalidation.py`、`application/invalidation_service.py` | 212 条路由、44 无前端 / 46 无测试 / 22 双重孤儿、116 测试矩阵、失效遗漏清单 |

> 子审计 C 在过程中误建了 `docs/_audit_cns_family_readonly_draft.md` 并已立即删除（`Test-Path` 复核为 `False`）；审计结束时的 `git status` 与开始时逐行一致，工作树已复原。

---

*报告结束。本报告为只读审计产出，未修改任何被审计代码，未执行 commit / push / reset，未继续 P14 优化。*
