# CNS-PLANNER：低空航路 CNS 规划系统

CNS-PLANNER 是一个面向低空航路规划与 CNS（Communication / Navigation / Surveillance）工程分析的本地工作台，采用 **Python + QGIS/GDAL + 原生 JavaScript ES Modules**。系统当前已从地图浏览原型发展为可保存、恢复和审计的 schema-v2 项目，并形成“数据语义 → 风险/航路 → CNS 需求 → 可靠性/安全 → 三维覆盖 → 静态服务能力”的分析链。

> 当前 P1–P8 为工程/研究基线。几何覆盖、静态服务能力、可靠性统计、运行时服务状态和安全事件是不同层次，系统明确禁止将它们相互等同。

## 启动

Windows 下推荐双击 `启动地图.cmd`，或在项目根目录运行：

```powershell
python map_app.py
```

启动器会查找已安装的 QGIS Python，并打开 <http://127.0.0.1:8765>。非默认 QGIS 安装位置可通过 `CNS_QGIS_PYTHON` 指定。终端 `Ctrl+C` 停止服务，日志位于 `outputs/map-server.log`。

旧 Streamlit/schema-v1 原型保留在 `cns_planner/legacy/`，不再是新版领域模型。

## 当前六步工作流

1. **项目与数据**：创建、打开、另存项目；配置 QGIS、人口、DEM、CNS catalog 等数据源；查看算法选择与 Manifest。
2. **工作区与环境**：定义工作区，生成 MH/T 标准网格，映射人口、GLO-30 DEM、空域、交通与冲突，计算相对风险；配置三维高度层。
3. **航路设计**：维护节点、场景航路和 RoutePlannerV1 运行航路，并配置航路高度剖面。
4. **运行规则与安全**：配置 Aircraft CNS Profile、RequiredCNS、C/N/S 性能、可靠性与 fallback；支持 ServiceState、FHA/FTA/FMEA、安全策略及跨 C/N/S 功能耦合 preview。
5. **设备与 CNS 分析**：维护 DeviceCatalog、Existing CNS 与 Candidate Sites；运行二维 CoveragePlannerV1、三维 GeometricCoverage3DV1 和 Technology-Aware CNS Service Capability V1。
6. **确认与导出**：复核状态并导出项目、航路、站点和分析结果。

## P1–P8 能力基线

- **P1 Data Semantics & Provenance**：WorldPop 人口 count/density、实际网格面积、NoData、GLO-30 DSM/EGM2008 垂向语义及来源元数据。
- **P2 Algorithm Registry**：使用 `(algorithm_type, algorithm_id, version)` 精确注册和选择算法；禁止找不到版本时静默 fallback。
- **P3 CNS Performance Contracts**：Aircraft Capability、RequiredCNS、Ground Device Capability 分离，建立 C/N/S 类型与性能契约并兼容 V1 字段。
- **P4 Reliability & ServiceState**：可靠性统计与当前服务状态分离；支持 `available / available_degraded / contingency / lost / unknown` 及 machine-readable fallback。
- **P5 Safety Baseline**：ServiceState → FailureCondition → UnacceptableEvent 分层，提供轻量 FHA、FTA、FMEA；未确认策略不形成安全结论。
- **P6 CNS Functional Coupling**：支持 C+S、C+N、N+S 的功能依赖、时序/重叠条件和 Coupled Event preview；不计算耦合概率。
- **P7 3D Spatial & Geometric Coverage**：明确 AGL / EGM2008 正高 / WGS84 椭球高；lazy VoxelRef、3D 航路采样、球/半球几何覆盖。
- **P8 Technology-Aware Service Capability**：在 P7 几何门控后进一步检查技术、机载接口、已确认 ServiceModel 和 RequiredCNS；通信支持 ITU-R P.525 自由空间链路预算参考。

## 模型层次与边界

```text
Data / Provenance
      ↓
2D Grid + 3D Route / Altitude
      ↓
Geometric Coverage (P7)
      ↓
Static CNS Service Capability (P8)
      ↓
Runtime ServiceState / Reliability (P4)
      ↓
Safety Events & Functional Coupling (P5/P6)
      ↓
Future Gap V2 / Protection & Timeline / Site Planning
```

关键语义：

- `RiskModelV1` 输出是 **[0,1] 相对工程风险指数**，不是事故概率。
- `coverage` 是 CoveragePlannerV1 的二维布站结果。
- `coverage_3d` 是 **geometric_only** 三维几何覆盖，不代表传播、探测、完整性或可用度满足要求。
- `cns_service_capability` 是静态技术/性能能力判定，不等同于当前服务 `available`。
- ReliabilitySpec 是统计属性，不会被随机抽样来制造当前 ServiceState。
- SafetyPolicy 为工程安全评估输入，不代表认证结论。
- 缺失、NoData、unknown、pending_confirmation 不得自动转换为 0、通过或安全。

## 当前算法注册

Algorithm Registry 当前包含：

```text
risk_model       / risk-model-v1-relative-index @ 1.1
route_planner    / route_planner_v1             @ 1.0
coverage_planner / coverage_planner_v1          @ 1.0
cns_gap_analyzer / cns_gap_analysis_v1           @ 1.0
coverage_model   / geometric_coverage_3d_v1      @ 1.0
service_model    / cns_service_capability_v1     @ 1.0
```

业务 Service 依赖算法对象而不是 Registry；Registry 只在 Application/Composition 层负责解析和实例化。

## 架构

```text
cns_planner/
  api/                 HTTP 路由与传输
  application/         六步工作流与用例编排、定向失效
  domain/              schema-v2、CNS/3D/可靠性/安全数据契约
  gis/                 QGIS/GDAL、CRS、栅格与空间适配
  data/                数据源 Registry、SourceProfile、grid 映射
  algorithms/
    route/              RoutePlannerV1
    coverage/           CoveragePlannerV1 + GeometricCoverage3DV1
    service_capability/ Technology-Aware CNS Service Capability V1
  gap/                  CNSGapAnalyzerV1
  risk/                 RiskModelV1 与可替换风险接口
  safety/               Reliability、ServiceState、FC/UE、FTA、耦合分析
  simulation/           交通仿真和 CPA 冲突检测
  persistence/          原子 JSON repository
  web/                  HTML/CSS/ES Modules 前端
  legacy/               Streamlit/schema-v1 兼容原型
```

架构原则：**API → Application → Domain/Algorithm**。GIS Adapter 负责把真实空间数据转成领域输入；算法不得直接依赖 QGIS 对象或任意本机路径。

## 数据与垂向基准

当前主要支持：QGIS 空域、本地 GIS 图层、WorldPop 人口 GeoTIFF、Copernicus GLO-30 DEM、交通/冲突数据、Aircraft/Device JSON catalog、Existing CNS / Candidate Site 的 JSON、CSV、Point GeoJSON。

GLO-30 在项目中按 DSM、米、WGS84/EPSG:4326 水平坐标、EGM2008/EPSG:3855 正高处理。P7 的 canonical vertical reference 为 EGM2008 orthometric；AGL 依赖 DEM 地表高转换；缺少 geoid transformation 时禁止将 WGS84 椭球高直接视为 EGM2008 正高。

## 项目状态与兼容

新版唯一权威状态为 schema v2 `ProjectState`，P1–P8 均采用 additive/backfill 扩展。V1 算法的公开语义和输入指纹受 characterization tests 保护；新字段在必要处通过兼容视图与 V1 隔离。

打开未知 schema、损坏 JSON 或数据源加载失败时保留当前有效项目；Save As 失败不会切换活动项目，也不会遗留不完整最终文件或 `.tmp` 文件。

## 验证

```powershell
python -m pytest -q -rs
node --test tests/grid_theme.test.js tests/frontend_modules.test.mjs
node --check cns_planner/web/app.js
node --check cns_planner/web/js/main.js
```

真实 QGIS 服务启动后可额外运行：

```powershell
$env:CNS_MAP_TESTS = "1"
python -m pytest -q tests/test_map_http.py
```

当前精确测试基线、已知 `QgsSpatialIndex` 测试替身问题、保护边界和技术债以 `AI_DEV_CONTEXT.md` 为准。

## 下一阶段

下一阶段将继续把 P8 的静态能力沿航路映射到时间轴，并建立可审计的响应时间预算/保护距离工程基线；之后再进入 Gap V2、风险感知航路与复用优先的 CNS 设施规划闭环。

详细技术路线见 `docs/CNS_TECHNICAL_BASELINE.md`。
