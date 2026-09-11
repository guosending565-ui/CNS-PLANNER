# CNS 航路规划系统

本项目是本地 Python + QGIS + 原生 JavaScript 的低空航路 CNS 规划工作台。当前六步流程已贯通项目、工作区、MH/T 标准网格、人口/DEM/空域属性、交通与冲突暴露、相对风险、V1 航路规划和 C/N/S 覆盖布站。

## 启动

Windows 下推荐双击 `启动地图.cmd`，或在项目根目录运行：

```powershell
python map_app.py
```

启动器会查找已安装的 QGIS Python，并打开 <http://127.0.0.1:8765>。非默认 QGIS 安装位置可通过 `CNS_QGIS_PYTHON` 指定。终端中的 `Ctrl+C` 用于停止服务，日志位于 `outputs/map-server.log`。

旧 Streamlit schema-v1 原型仍可通过以下命令打开，但它位于 `cns_planner/legacy/`，不再是新版工作台的领域模型：

```powershell
python -m streamlit run legacy_app.py
```

## 六步流程

1. 项目与数据：创建、打开、另存项目，管理数据源。
2. 工作区与环境：定义工作区，生成标准网格，映射人口、DEM、空域、交通、冲突与相对风险。
3. 航路设计：维护节点、场景航路和 V1 运行航路。
4. 运行规则：选择 Aircraft CNS Profile，分别配置 RequiredCNS、运行方向、高度与间隔。
5. 设备与布站：查看 DeviceCatalog，导入已有 CNS 设施与候选站址，并运行 CoveragePlannerV1。
6. 确认与导出：统一状态复核并导出项目、航路和站点。

## 架构

```text
cns_planner/
  api/             HTTP 传输、安全、路由与静态资源
  application/     ApplicationContext、六步编排及业务用例服务
  domain/          schema-v2 ProjectState、状态和未来算法契约
  gis/             QGIS/GDAL、CRS、数据加载、渲染和空间适配
  data/            统一数据源注册、健康检查及 grid_id 映射
  algorithms/      grid、route/v1、coverage/v1
  risk/            可替换 RiskModel 与 RiskModelV1
  simulation/      交通仿真和 CPA 冲突检测
  persistence/     原子 JSON repository
  legacy/          保留的 Streamlit/schema-v1 原型
  web/             无构建步骤的 HTML/CSS/ES Modules 前端
```

`cns_planner/map_server.py` 仅保留启动、组装及旧导入兼容；HTTP 行为在 `api/`，QGIS 行为在 `gis/`。`application/workflow_service.py` 是六步 facade，具体用例分布在 project/workspace/route/operation/risk/CNS/export service。旧模块路径保留为薄兼容导入，现有集成无需立即改写。

前端入口 `web/app.js` 仅加载 `web/js/main.js`。API、Store、地图投影/渲染/交互/网格索引、数据源中心和六个步骤均为独立 ES Module；CSS 通过 `web/style.css` 按职责加载 `web/css/` 下文件，无 npm 或打包步骤。

## 数据源

数据源由 `cns_planner/data/registry.py` 统一声明，字段包括 id、名称、类别、类型、格式、用途、必需阶段、健康、覆盖和来源元数据。当前接入 QGIS 空域、人口 GeoTIFF、GLO-30 DEM、Aircraft/Device JSON catalog，以及 Existing CNS/Candidate Site 的 JSON、CSV、Point GeoJSON 输入；建筑、财产暴露、障碍物、基础设施和铁塔保留扩展入口。

新增真实数据源应沿 `Registry → GIS Adapter → data/mapping Service → grid_id attributes` 扩展，算法不得直接读取 QGIS 对象或本机路径。

## 项目状态与安全

新版唯一权威状态为 schema v2 `ProjectState`。项目状态和数据源设置分别通过 repository 原子写入。打开未知 schema、损坏 JSON 或数据源加载失败时保留当前有效项目；Save As 失败不会切换活动项目，也不会遗留不完整最终文件或 `.tmp` 文件。旧 schema v1 仅由 legacy 入口使用。

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

开发约束、V1 外部契约、状态失效关系和最新测试基线见 `AI_DEV_CONTEXT.md`；业务与数据字典见 `docs/`。
