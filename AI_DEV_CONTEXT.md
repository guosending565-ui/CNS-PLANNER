# CNS 规划系统开发上下文

> 架构基线：2026-09-11（Asia/Shanghai）
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
3. 航路设计：节点、场景航路、RoutePlannerV1 运行航路。
4. 运行规则：飞行器、方向、高度、间隔和监视延迟。
5. 设备与布站：C/N/S 设备参数和 CoveragePlannerV1。
6. 确认与导出：统一 ResultStatus 复核，导出项目、航路和站址。

## 3. 已实现能力

- M1：workspace 生成/裁剪 MH/T 4063 标准网格，稳定 `grid_id`，保存恢复、API、Canvas 显示和点击。
- M2–M4：人口 GeoTIFF、GLO-30 DEM、QGIS 空域按 `grid_id` 映射；处理 CRS、NoData、无覆盖和定向失效。
- M5–M6：可替换 `RiskModel`；Ground、Airspace Constraint、Overall 相对风险，参数化权重/阈值、贡献解释和完整度。
- M7：可复现多机直线轨迹、逐格驻留时间、二维 CPA 潜在冲突、traffic/conflict 风险输入。
- CNS 核心输入：AircraftCNSProfileCatalog、RequiredCNS、DeviceCatalog、ExistingCNSFacility、CandidateSite 已纳入 schema v2；已有能力与任务需求严格分离。
- CNS Gap Analysis：按 operational route 和 C/N/S 分系统比较 RequiredCNS、机载能力及已有设施水平覆盖，输出长度口径覆盖率、连续缺口航段和稳定输入指纹；结果可直接作为未来 CNSSitePlanner 输入。
- RoutePlannerV1：固定工作区离散、硬约束 BBOX、A*、geometry/关键节点/统计/指纹。
- CoveragePlannerV1：C/N/S 主站、补盲、共址、未覆盖点/航段、统计/指纹。
- schema-v2 项目自动保存、打开、Save As 与数据源恢复；失败操作保留当前有效项目并清理临时文件。
- 本地 QGIS 渲染、原始人口/DEM 图层、在线瓦片、统一数据源中心、六步 ES Module 前端。

## 4. 重要文件职责

- `cns_planner/map_server.py`：QGIS 进程启动、ApplicationContext 组装、事件循环；旧测试/本地集成所需名字为兼容 facade。
- `cns_planner/application/app_context.py`：唯一运行上下文，持有 QGIS runtime、workflow、data、tile cache、repositories 和活动项目会话。
- `cns_planner/api/router.py`：保持 `/api/*` URL，只解析用例和响应，不依赖算法实现。
- `cns_planner/application/workflow_service.py`：六步 facade、步骤可进入性和 snapshot；具体变更委派给 Project/Workspace/Route/Operation/Risk/CNSPlanning/Export Service。
- `application/project_state.py`：schema-v2 空状态、兼容字段回填与 schema 校验。
- `domain/algorithm_manifest.py`、`algorithms/registry.py`：算法可解释元数据、精确注册/查询/实例化和四个 V1 默认注册；factory/Python 实现路径不进入 ProjectState 或 API Manifest。
- `application/cns_input_service.py`：五类 CNS 规划输入的选择、导入、需求覆盖、保存与下游失效。
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
```

- workspace/grid 变化：所有网格属性、traffic/conflict 和 risk 失效或重算。
- population 变化只失效 population + risk；terrain 同理；QGIS/airspace 变化只失效 airspace + risk。
- traffic simulation 变化失效 traffic、conflict、risk。
- Aircraft/RequiredCNS/Device/ExistingCNS/CandidateSite 变化仅使相应下游 routes、coverage、technical risk、report stale；不改写已保存的 V1 算法结果结构。
- workspace、operational route、运行规则/选定机型、RequiredCNS、DeviceCatalog 或 ExistingCNS 变化会使 cns_gap_analysis stale；CandidateSite 不是 Gap Analysis 输入。
- 缺失/NoData/未知不得转换成零风险或通过。
- 不支持 schema、损坏 JSON、数据源加载失败不会替换当前项目；Save As 失败不切换 active project。

## 6. 算法外部契约

- RoutePlannerV1 与 CoveragePlannerV1 保留公开输入输出、`status`、`algorithm_name/version`、`input_fingerprint`、geometry/站址/统计和固定输入确定性。
- RiskModel 接口固定为 `evaluate(grid, grid_attributes, parameters) -> risk_result`。RiskModelV1 输出为 `relative_index`，不是事故或碰撞概率。
- P2 Algorithm Registry 使用精确 `(algorithm_type, algorithm_id, version)` 键，当前注册：`risk-model-v1-relative-index@1.1`、`route_planner_v1@1.0`、`coverage_planner_v1@1.0`、`cns_gap_analysis_v1@1.0`。找不到精确版本直接报错，禁止回退。
- `AlgorithmManifest` 固定包含 `name/provider/maturity/description/inputs/outputs/parameter_schema/assumptions/limitations/references`；Registry 内部 factory 不序列化，ProjectState 只保存选择与参数。
- Workflow 启动时从 Registry 解析四个选择，再把算法对象注入 Risk/Route/CNSPlanning/GapAnalysis Service；业务 Service 不依赖 Registry。
- 未来 RoutePlanner：`plan(start, end, grid, risk, constraints) -> RouteResult`。
- 未来 CNSSitePlanner：`plan(route, required_cns, candidate_sites, device_catalog, parameters) -> CNSPlanResult`。
- `DeviceCatalog`（地面设备型号/性能）与 `AircraftCNSProfileCatalog`（机载已有能力/默认需求）必须分开；已有能力不得等同需求。

P3 CNS Taxonomy & Performance Contract：

- AircraftCNSProfile（机载能力）、RequiredCNS（运行需求）和 CNSDevice（地面设备能力）共用轻量 C/N/S taxonomy，但分别保存，互不推导或替代。
- 每个分系统采用 `type + performance + contingency + source + confirmed + confirmation_status`；未知分类使用允许的 `unknown` 或 `null`，未知性能保持 `null/pending_confirmation`，不填入法规或安全阈值。
- C 类型包含 `service_type/technology/network_scope/interfaces`，性能包含秒制时延、失链/中断、可用度与冗余；N 类型包含导航 technology，性能包含水平/垂直误差、完整性、告警/降级时间、可用度与冗余；S 类型包含 target cooperation、sensor mode、technology，性能包含探测距离/概率、更新/航迹丢失/告警时间、可用度与冗余。
- canonical 时间单位统一为秒；schema-v2 继续保留并同步 V1 别名：`max_latency_s <-> latency_ms`、`max_horizontal_error_m <-> accuracy_m`、`integrity_required <-> integrity`、`max_update_interval_s <-> update_interval_s`、`min_redundancy <-> redundancy`。同时提供不一致检测，禁止静默选择其中一个值。
- availability/probability 校验为 0..1；冗余为正整数；时间和距离非负。Catalog 恢复时执行 additive contract backfill，旧 `capabilities/radius_m/mtbf_h` 等字段原样保留。

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

## 11. 下一阶段计划

1. 建立 CNS Gap Analysis：按航路比较 RequiredCNS、机载能力与 ExistingCNS 覆盖，输出可解释缺口。
2. 引入可替换 CNSSitePlanner，使用 gap、CandidateSite 与 DeviceCatalog，继续保护 CoveragePlannerV1。
3. 将 grid_risk 作为 RoutePlannerV2 的标准代价输入，在新版本中逐步替换 56×56 原型；不要修改 RoutePlannerV1。
4. 接入建筑/财产/基础设施真实映射，保持 `grid_attributes` 原始属性与 `grid_risk` 派生结果分离。
5. 补 ApplicationContext 并发事务、schema migrations、项目 manifest/audit 和真实 QGIS 集成 CI/验收脚本。
