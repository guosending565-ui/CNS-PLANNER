# CNS 规划系统开发上下文

> 基线日期：2026-09-10（Asia/Shanghai）
> 本文只记录当前实现与后续拆分方向；建立本基线时未修改算法或业务行为。

## 1. 当前架构

系统当前同时保留新版地图工作台和旧版 Streamlit 元信息原型。

```text
启动地图.cmd / map_app.py / app.py
                  |
                  v
cns_planner/map_server.py
  ├─ 本机 HTTP API 与静态文件服务
  ├─ QGIS/Qt 主线程任务队列、图层装载与视口渲染
  ├─ 数据源、文件浏览、在线服务检查
  ├─ 项目保存/打开编排与活动项目切换
  ├─ DataSourceRepository / ProjectRepository
  └─ WorkflowService
       ├─ WorkspaceGridService -> algorithms/grid/mht4063.py
       ├─ PopulationGridService / TerrainGridService -> GdalRasterAdapter
       ├─ AirspaceGridService -> QgisAirspaceAdapter
       ├─ TrafficSimulator / ConflictDetector
       ├─ TrafficGridService / ConflictGridService
       ├─ RiskModel Protocol -> RiskModelV1
       ├─ RoutePlannerV1
       ├─ CoveragePlannerV1
       ├─ ResultLedger / ResultStatus
       └─ ProjectRepository

cns_planner/web/index.html + style.css + app.js + tiles.js + grid_theme.js
  └─ 六步界面、Canvas 地图、网格专题、浏览器端 XYZ 瓦片、API 调用和前端状态

legacy_app.py -> cns_planner/ui/app.py -> Project + storage.py
  └─ 第一阶段 Streamlit 元信息原型；与新版工作台状态不共享
```

运行时以本机 `127.0.0.1:8765` 提供服务。HTTP 请求使用线程服务器；QGIS 操作通过 `Queue + Future` 回到 Qt 所在线程。新版项目状态默认保存在 `projects/current_project.json`，地图源保存在 `projects/map_sources.json`；另存项目时使用所选目录内的 `project_state.json` 和 `data_sources.json`。

## 2. 六步业务流程

1. **项目与数据**：编辑项目名，选择项目目录进行保存/打开，搜索地名；统一数据源中心负责 QGIS、人口、地形及其他数据源的登记和健康提示。
2. **工作区与环境**：在地图上绘制 WGS84 矩形工作区，计算近似面积，生成与工作区相交的 MH/T 4063.1 标准网格，并检查人口、地形和本地图层是否覆盖工作区。
3. **航路设计**：添加/删除起降点，按方向生成场景航路，维护不复用的航路编号，再由 `RoutePlannerV1` 生成运行航路。
4. **运行规则**：录入飞行器、速度、MTBF、双向高度、水平间隔和链路时延，计算 λ、总时延及反应距离并校验规则；规则变化会使下游结果失效。
5. **设备与布站**：配置 C/N/S 主站与补盲设备，由 `CoveragePlannerV1` 独立布站、共址、采样检查覆盖并输出统计。
6. **确认与导出**：审查环境、技术、生命和财产风险以及依赖状态；导出项目 JSON、航路 GeoJSON、站点 GeoJSON并保存当前项目。

## 3. 已实现能力

- Windows 本地启动器：发现 QGIS Python、复用健康服务、写启动日志、等待就绪并打开浏览器。
- QGIS 项目、本地空域图层、人口 GeoTIFF、GLO-30 DEM 的只读装载、校验、样式化和按视口渲染；人口原始栅格值、DEM 高程统计和空域矢量相交结果已分别按标准 `grid_id` 映射。
- 浏览器端平移、缩放、定位、图层开关、透明度控制；在线 XYZ 瓦片与 QGIS 本地渲染分离，并具有限流、缓存和过期请求取消。
- 本机文件/目录浏览，地图数据源“仅校验”和“应用”，文件级及工作区覆盖健康检查，在线瓦片与地名搜索独立检查。
- 六步工作流状态自动保存与重启恢复，项目目录另存/打开，稳定节点/航路序号和退役航路号。
- 项目状态与数据源 JSON 的底层文件 I/O 已分别统一到 `ProjectRepository` 和 `DataSourceRepository`，保持既有格式和原子替换方式。
- 当前端到端原型：矩形工作区、场景航路、基于固定规则网格的 A* 运行航路、硬约束 BBOX 阻断、运行规则校验、C/N/S 确定性覆盖布站、补盲和共址。
- 明确的多状态结果与保守失效传播；未知风险不会被聚合为通过。
- 项目 JSON、航路 GeoJSON、站点 GeoJSON 导出。
- MH/T 4063 网格几何计算模块及专项测试；已通过独立 `WorkspaceGridService` 接入工作区、项目状态、读取 API 和 Canvas 图层，支持稳定内部 `grid_id`、点击查看、工作区变化重生成及旧项目回填。
- 标准网格人口源值、DEM平均高程及Ground/Airspace Constraint/Overall Risk Canvas专题：按`grid_id`关联geometry、原始属性和派生风险，提供分级图例、无数据样式及单格解释查询。
- 可替换 `RiskModel` 接口和参数化 `RiskModelV1` 已接入；V1输出相对风险指数、贡献因子与数据完整度，不把结果解释为事故概率。
- 可复现的直线多无人机运行仿真、按 `grid_id` 累计驻留时间的交通暴露，以及带前视时间、水平安全距离和冷却参数的二维 CPA 潜在冲突暴露已接入；交通/冲突是独立原始属性，RiskModelV1 只消费其归一化结果。

上述算法、设备库和工程参数仍属于可替换的工程原型；正式风险代价、生命风险、财产风险、真实障碍几何和规范参数尚未完成。

## 4. 重要文件职责与负担判断

| 文件 | 当前职责 | 判断 |
|---|---|---|
| `map_app.py`（83 行） | QGIS runner 发现、进程启动、日志、健康等待、浏览器打开 | 当前职责集中，是薄启动器；后续仅需避免继续加入业务逻辑。 |
| `app.py`（27 行） | CLI 入口转发及 Streamlit iframe 兼容入口 | 足够薄。 |
| `cns_planner/map_server.py`（804 行） | QGIS 生命周期和线程桥、数据装载/样式/元数据/渲染、数据健康补丁、硬约束提取、文件浏览、在线检查、项目保存/打开编排、HTTP 安全/路由/响应、全局运行状态 | **职责仍然严重过多。** 底层项目/数据源文件 I/O 已委托 repository，但活动切换、QGIS 应用和传输层仍耦合。 |
| `cns_planner/services/workflow.py`（339 行） | 状态 schema/default 与兼容判定、项目/工作区/节点/航路 CRUD、场景生成、算法编排、规则和设备校验、失效、风险聚合、三类导出；通过 repository 加载/保存 | **职责仍然过多。** 底层项目 JSON I/O 已抽离，何时保存、schema 策略和状态生命周期仍由工作流负责。 |
| `cns_planner/services/grid_service.py` | 把工作区 BBOX 适配为现有 MH/T 4063.1 平面网格：选择可控层级、枚举相交格、生成稳定内部 ID 与 GeoJSON geometry | 独立于 Workflow、HTTP、QGIS 和文件 I/O；网格几何尺寸及边界复用 `algorithms/grid/mht4063.py`，未重复实现标准细分算法。 |
| `cns_planner/services/raster_grid_adapter.py` | 只读打开单波段 GDAL 栅格，把 WGS84 格网边界转换到源 CRS，读取相交像元中心并过滤 NoData/越界值 | 栅格 I/O、CRS 与仿射窗口适配层；不依赖 Workflow、项目状态或具体人口/DEM业务语义。 |
| `population_grid_service.py` + `terrain_grid_service.py` | 分别按基础格网 `grid_id` 计算人口源值统计与 DEM 有效样本、均值、最小值、最大值 | 只保存属性映射，不复制或改变基础格网 geometry；人口单位未由波段元数据确认时保持 `unverified`。 |
| `airspace_grid_service.py` + `qgis_airspace_adapter.py` | 按基础格网 `grid_id` 编排空域无覆盖/部分相交/完全覆盖状态，并用 QGIS 完成矢量 CRS 转换、工作区过滤、空间索引候选筛选及真实几何相交 | 只保存空域属性映射，不复制基础格网 geometry；Workflow 不持有 QGIS 对象或相交算法。 |
| `cns_planner/simulation/traffic_simulator.py` + `conflict_detector.py` | 按固定seed生成多无人机直线时序轨迹；对轨迹对执行二维CPA潜在冲突检测、pair去重和冷却抑制 | 不依赖Workflow、网格、QGIS或文件I/O；当前是可替换的基础运行场景，不含动力学、垂直间隔或安全标准认定。 |
| `traffic_grid_service.py` + `conflict_grid_service.py` + `grid_spatial_index.py` | 通过网格BBOX空间桶把轨迹段驻留时间及CPA事件关联到现有`grid_id`，计算原始/归一化交通和冲突暴露 | 不复制geometry；归一化参考值和分位数参数化，空间查询采用半开边界避免边界点重复归属。 |
| `cns_planner/risk/model.py` + `risk/v1.py` | 定义 `RiskModel.evaluate(grid, grid_attributes, parameters)` 可替换接口；V1集中实现Ground、Operational Air、Airspace Constraint和Overall相对风险、输入审计及完整度 | 不读文件、不依赖QGIS/HTTP/Workflow，不写回原始属性；全部参考值、权重、类别映射和等级阈值均由parameters提供或记录为工程默认。 |
| `cns_planner/persistence/project_repository.py` + `data_source_repository.py` | 项目状态和数据源文件的存在性检查、UTF-8 JSON 读写、`.tmp` 原子替换，以及项目状态文件复制 | 边界纯净；不依赖 `map_server`、`WorkflowService`、QGIS、业务校验、失效或 HTTP。 |
| `cns_planner/algorithms/route_planner.py`（104 行） | 经纬度到固定网格映射、约束 BBOX 栅格化、A*、路径简化、结果/指纹/风险占位组装 | 文件不大但边界不清；算法核与 GIS/结果适配应分离，保持现有 V1 行为作为回归基线。 |
| `cns_planner/algorithms/coverage_planner.py`（108 行） | 距离和插值、设备选择、C/N/S 循环、主站/补盲、物理站址共址、覆盖采样、缺口统计、编号及指纹 | 文件不大但业务职责密集；布站、共址、覆盖评估和结果组装应形成独立策略接口。 |
| `cns_planner/web/app.js`（795 行） | API 客户端、全局状态、地图坐标/Canvas 绘制、交互、六步 HTML 模板与事件绑定、数据源中心、在线检查、本机浏览器 | **前端首要拆分对象。** 状态、视图和副作用均依赖模块级可变变量，难以单测。 |
| `cns_planner/web/tiles.js`（216 行） | 浏览器端 XYZ 选择、并发、缓存、请求、绘制与健康检查 | 相对独立，但网络状态、缓存策略和渲染仍可进一步解耦。 |
| `cns_planner/web/grid_theme.js` | 网格专题分位断点、颜色分级、无数据色和数值格式化纯函数 | 小型无DOM依赖模块，可由Node直接测试；不读取API或栅格。 |
| `models/status.py` + `services/invalidation.py` | 结果枚举、风险聚合模型、依赖失效表 | 方向正确；当前 `WorkflowService.review()` 又实现了一遍聚合逻辑，存在双源规则。 |
| `services/data_registry.py` + `data_health.py` | 数据源目录和阶段化健康检查 | 已分层，但 terrain 的登记/健康逻辑仍由 `MapData.metadata()` 临时补入。 |
| `domain.py` + `storage.py` + `ui/app.py` | 旧 Streamlit 元信息模型、序列化与页面 | 与 schema v2 新工作流并存，形成两个项目模型和两套状态/保存语义。 |

## 5. 项目保存与状态管理现状

- `WorkflowService` 直接持有并修改嵌套 `dict`，并决定何时保存；工作区网格作为顶层 `grid`，人口/DEM/空域及未来数据命名空间作为顶层 `grid_attributes`，风险派生结果独立保存为顶层 `grid_risk`。底层加载和整份状态 JSON 写入由 `ProjectRepository` 完成，仍沿用 `.tmp` 后替换的基础原子机制。
- schema 不匹配、JSON 损坏或读取异常时会静默创建空项目，没有显式迁移、隔离、备份或错误审计。
- “另存为”仍由 `map_server.py` 编排：先保存活动状态、通过 `ProjectRepository` 复制到目标、重建全局 `WORKFLOW`，再通过 `DataSourceRepository` 写第二个 JSON。当前不是 `manifest/data/results/audit` 形式的完整项目包，也不复制或校验外部数据。
- `open_project()` 会先尝试切换数据源，再切换全局活动项目，整个过程没有事务边界或统一回滚。
- `DATA`、`WORKFLOW`、`ACTIVE_PROJECT_FILE` 是模块级全局变量；`ThreadingHTTPServer` 可并发执行工作流读写，而工作流状态和临时文件没有专用锁。
- 前端再用 `state`、`flow`、`view` 等全局变量保存服务快照和交互状态，没有显式 store、动作模型或并发更新策略。
- 结果状态同时存在枚举与多处字符串实现；健康状态又使用另一套 `ready/warning/error` 词汇，需要保持语义边界并消除重复聚合。

## 6. 测试基线

基线环境：Python `3.13.9`，pytest `8.4.2`。

基线命令：

```powershell
python -m pytest -q -rs
```

结果：**99 passed, 6 skipped, 4 xfailed, 0 failed**。

6 项跳过均来自 `tests/test_map_http.py`，原因是需要先启动真实 QGIS 地图服务并设置 `CNS_MAP_TESTS=1`。4 项严格 `xfail` 记录已确认的持久化安全缺陷，修复后会以 `XPASS(strict)` 使测试失败，从而要求显式更新基线。默认测试覆盖：项目元信息序列化、Streamlit 骨架、状态聚合和失效、数据健康、启动器、瓦片缓存、六步工作流原型、V1 航路/覆盖 characterization、项目持久化 characterization、A* 硬约束失败、MH/T 4063 网格几何、workspace→grid 闭环，人口/DEM/空域→grid_id，确定性多机轨迹、驻留时间累计、CPA pair去重/冷却、冲突点网格映射，以及 RiskModel替换、交通/冲突输入消费、风险等级、缺失值、保存恢复与输入失效。`node --check cns_planner/web/app.js`与`grid_theme.js`通过；`node --test tests/grid_theme.test.js`为 **3 passed, 0 failed**，锁定分位分级、无数据颜色、WGS84相交判断和半开边界命中。当前缺口包括真实浏览器自动化、并发写入、schema 迁移实现和真实 QGIS 集成的自动化启动。

### M1 工作区标准网格契约

- `WorkspaceGridService.generate(workspace_bbox)` 是 Workflow 复用的唯一工作区网格入口；默认请求 L7，在单项目网格超过 5000 格时确定性降低层级，直到满足上限。服务输出与 QGIS、HTTP 和项目文件无关。
- 顶层 `grid` 字段包含 `status/standard/id_scheme/preferred_level/level/coarsened/workspace_bbox/cell_size_degrees/count/cells`；每个 cell 包含 `grid_id/level/bbox/center/geometry`，geometry 是 WGS84 GeoJSON Polygon。
- `grid_id` 当前为 `mht4063-global-index-v1` 内部稳定索引，不是尚未实现的 MH/T 正式编码。相同标准版本、层级和格位的 ID 稳定，可作为人口、DEM、空域、风险和 CNS 性能派生表的关联键。
- `set_workspace()` 在保存工作区时同步生成新 grid，旧 grid 不会继续留存；`clear_workspace()` 清空 grid。项目状态连同 grid 原样保存和恢复。schema 仍为 2；旧 schema-v2 项目缺少 grid 时根据已保存 workspace 回填，缺少 workspace 时保持未计算。
- `GET /api/workspace/grid` 返回当前 grid；无工作区时返回带空 `cells` 的 `not_calculated` 快照。前端“标准网格”图层默认开启，点击格网显示 `grid_id` 和层级。

### M2 栅格网格属性契约

- `grid_attributes.population` 和 `grid_attributes.terrain` 是以基础 `grid.cells[].grid_id` 为键的独立派生属性表，不包含 geometry。两类结果均记录 `status/source/algorithm_id/sampling_version/grid_level/count/covered_count/cells`。
- `GdalRasterAdapter` 将每格 WGS84 BBOX 边界加密后转换到栅格 CRS，裁剪像元窗口，再把像元中心反投影到 WGS84，以西/南闭、东/北开的边界规则筛选；NoData、非有限值和无覆盖窗口不进入统计。
- 人口映射输出 `valid_sample_count/value_sum/value_mean/value_min/value_max`。数值遵循栅格 scale/offset，但只解释为源栅格值；`value_unit` 仅取波段单位元数据，未标注时 `unit_status=unverified`，不自动转换成人/km²。
- DEM 映射输出 `valid_sample_count/mean_elevation/min_elevation/max_elevation`。单位优先取波段元数据；GLO-30 未标注时按已登记产品契约记录为米，并在 `unit_status` 中区分来源。
- 保存工作区时先重建基础格网并清空旧属性，再通过两个独立 service 映射当前人口和 terrain 数据；结果随项目 JSON 保存恢复。旧 schema-v2 项目缺少 `grid_attributes` 时回填两个 `not_calculated` 空结果。
- 数据源更新会比较实际加载后的路径：population 变化只把人口属性标为 `stale`，terrain 变化只把 DEM 属性标为 `stale`；basemap 仍沿用既有工作流失效传播。
- `GET /api/workspace/grid/attributes` 返回当前网格派生属性；M2 的人口/DEM字段保持兼容，M4在同一响应中增加空域属性。第02步显示各自状态、已处理格数和有效/命中格数。

### M3 标准网格专题与查询契约

- 第02步以独立checkbox控制标准网格轮廓，以“无/人口/DEM”radio控制单一专题模式。原始QGIS人口/地形栅格仍是可同时开启的独立checkbox；专题模式不控制轮廓或点击查询。勾选标准网格但尚无workspace/grid时，地图明确提示先在第02步保存工作区。
- 前端在初始加载及每次workflow变化后并行同步`GET /api/workspace/grid`与`GET /api/workspace/grid/attributes`，以请求序号丢弃陈旧响应；随后按`grid_id`一次性关联geometry与属性，并预计算人口/DEM分位断点、ID索引和空间桶。`paint()`只裁剪并绘制当前可见网格，不重新整理属性，也不绘制批量文字标签。
- 人口专题使用`value_mean`做相对分级。仅当`unit_status=verified_from_raster_metadata`时展示实际`value_unit`；否则固定显示“人口源值（单位未核实）”，不赋予人口密度含义。
- DEM专题使用`mean_elevation`做相对分级，单位取`elevation_unit`，图例及点击详情沿用M2的`source`与`unit_status`。没有可用属性、单格无有效样本或整体结果失效时使用独立灰色无数据样式。
- Canvas内部视图范围为EPSG:3857，网格为WGS84；绘制前必须通过`visibleLonLatBounds()`转换后再做可见性裁剪。先前直接比较两种坐标导致网格被全部跳过，现已修正。workspace保存、打开项目和重启恢复后使用`fitLonLatBbox()`自动定位工作区，网格边界以高对比紫色、1.6px绘制在QGIS位图和专题填色之上。
- 点击查询只要求当前grid存在且处于平移模式，不依赖标准网格或专题开关。`eventLonLat()`输出WGS84，经缓存空间桶筛选后用西/南闭、东/北开的WGS84规则命中基础格，再读取已按`grid_id`关联的M2属性；显示grid_id、level、人口样本数/mean/sum/单位状态，以及DEM样本数/mean/min/max/单位/来源。

### M4 空域网格属性契约

- `grid_attributes.airspace` 以基础 `grid.cells[].grid_id` 为键，不复制 geometry；顶层记录 `status/source/algorithm_id/algorithm_version/grid_level/count/hit_count/cells`。成功映射时每格状态区分 `no_coverage/partial_intersection/full_coverage`；适配失败时顶层和单格均为 `failed`，不会把未知错误伪装为无覆盖。每格还记录 `intersected_layer_count/coverage_ratio/airspaces`。
- 每个命中项记录 `layer_id/name/feature_id/category/type/intersection_ratio/intersection_area/source_attributes`。`source_attributes` 只保留名称、类别、类型和上下限/高度等白名单字段；`intersection_area` 使用源图层 CRS 的面积单位，跨数据源比较优先使用无量纲 `intersection_ratio`。
- `QgisAirspaceAdapter` 将当前 workspace BBOX 转换到每个矢量图层 CRS，并通过 provider 矩形过滤只读取工作区要素；每层只建一次 `QgsSpatialIndex`，逐格仅对 BBOX 候选做真实 geometry 相交。基础网格仍来自 `WorkspaceGridService`，Workflow 只校验当前 grid level/ID 并保存结果。
- 保存/修改 workspace 会重建 grid 并清空全部派生属性；basemap/QGIS 数据源实际变化只将 `airspace` 标为 `stale`，不改变人口/DEM属性。项目 JSON 原样保存空域结果；旧 schema-v2 项目缺少 `airspace` 时回填 `not_calculated`。
- 继续复用 `GET /api/workspace/grid/attributes`，没有增加重复 API。第02步显示空域映射状态、处理格数和命中格数；前端按 `grid_id` 缓存空域属性，点击单格时列出最多8个空域命中及剩余计数，不增加空域专题着色。

### M5–M7 风险模型与统一数据契约

- `RiskModel` 是运行时可检查的 `Protocol`，唯一计算入口为 `evaluate(grid, grid_attributes, parameters) -> risk_result`。`WorkflowService`持有该接口并负责选择、调用、验证grid_id及保存结果；公式不得进入Workflow。
- M5先建立无公式占位契约；M6实现地面/空域约束基础指数；M7在不改变接口的情况下升级为`algorithm_id=risk-model-v1-relative-index`、`algorithm_version=1.1`并消费交通/冲突属性。输出始终标记`semantics=relative_index`，不是事故概率、碰撞概率或安全标准判定。
- 顶层 `grid_risk` 与 `grid_attributes` 分离，包含 `status/algorithm_id/algorithm_version/parameters/input_status/source_versions/input_versions/grid_level/count/cells`。`cells`只以`grid_id`为键，每格包含`ground/air/airspace_constraint/overall/contributors/status`，不复制geometry，也不把任何风险字段写回人口、DEM、空域、交通或冲突属性。
- `source_versions`逐命名空间记录输入状态、映射算法ID/版本和数据源；`parameters`保存本次模型全部参数以及动态参考值的`resolved_value`。
- `grid_attributes`预留独立空状态：`buildings`、`property_exposure`、`infrastructure`、`towers`。这些命名空间具有统一的`status/source/algorithm_id/algorithm_version/grid_level/count/cells`入口，本轮不实现数据读取或映射。
- workspace变化会丢弃旧grid_id对应的交通、冲突和风险结果；人口、DEM、空域、交通/冲突及未来扩展输入源发生变化时，已有`grid_risk`与环境风险状态变为`stale`。风险状态和结果随项目JSON保存，旧schema-v2项目缺失扩展字段时回填空状态。

### M6 Relative Risk V1数学与缺失语义

- Population因子使用单格`value_mean=x`：`P=clip(log(1+max(x,0))/log(1+P_ref),0,1)`。`P_ref`可显式传入，默认从当前数据集有效格的可配置分位数（工程默认0.95）求得并写入`resolved_value`，没有固定全国人口阈值。人口单位未核实时仍允许相对归一化，但Ground结果标记`risk_semantics=relative_only`且贡献因子保留`population_unit_status=unverified`。
- Terrain因子仅使用起伏`relief=max_elevation-min_elevation`：`T=clip(relief/T_ref,0,1)`；`T_ref`同样支持显式值或当前数据集可配置分位数。`mean_elevation`只保留为解释变量，不直接转成危险度。
- Ground公式为`G=sum(w_k*R_k)/sum(w_k)`，只对状态有效且配置权重大于零的P/T/B/E/I因子重归一化。工程默认仅启用`population=0.8`、`terrain=0.2`；建筑、财产和基础设施权重默认为0且保持`not_available`，未来配置正权重但缺数据时会降低完整度，绝不会按零风险参与。
- Airspace Constraint Risk对每个空域命中计算`r_j=clip(intersection_ratio_j*category_weight_j,0,1)`，单格`A=max(r_j)`以避免重叠图层简单累加。类别权重映射默认留空，必须由parameters提供；未知类别默认`unknown_category_policy=missing`，也可显式选`zero`或配置`default_weight`。没有空域命中的有效`no_coverage`格可得到A=0，这与缺失数据不同。
- Overall公式为`R=sum(w_c*R_c)/sum(w_c)`，只组合可用的Ground与Airspace Constraint分量；工程默认权重`ground=0.7`、`airspace_constraint=0.3`并标记`engineering_default`。未采用`1-(1-G)(1-A)`，因为G/A不是独立事件概率。
- 风险等级阈值通过`parameters.risk_levels`配置，工程默认五级为`very_low/low/medium/high/very_high`及边界0/0.2/0.4/0.6/0.8/1。每个因子记录`raw/normalized/weight/normalized_weight/contribution/status`；Ground、Air和Overall均记录`score/level/status/semantics/data_completeness`。
- `data_completeness`按当前配置权重计算有效信息占比；missing/NoData因子不参与分子和风险加权。所有Ground因子缺失则Ground为`missing_data`；Overall仍可仅使用其他可用分量并通过低完整度揭示信息不足；stale输入保持stale，不重新解释。
- `absolute_risk`仅保存`P_failure * P_ground_impact * Exposure * Consequence`扩展契约及所需输入，当前固定为`not_calculated`，不伪造飞行器失效率、撞地概率、暴露或后果参数。
- 第02步专题单选新增Ground Risk、Airspace Constraint Risk、Overall Risk，使用固定0–1五级相对指数图例和独立无数据色。点击格网继续显示人口、DEM和空域详情，并追加G/A/R分数与等级、P/T归一化和贡献、数据完整度、语义及算法版本。

### M7 无人机运行暴露、CPA与RiskModel V1.1

- `TrafficSimulator.simulate(parameters)`输入UAV数量、OD、速度范围、高度、仿真时长、步长和`random_seed`，输出确定性的多机直线轨迹、逐时刻WGS84坐标、速度/高度、活动时长及输入指纹。相同输入完整结果相同；当前轨迹核可在未来替换为RoutePlanner输出，不把仿真写入Workflow。
- `grid_attributes.traffic`按基础`grid_id`记录`flight_count/flight_seconds/grid_area_km2/traffic_density_raw/traffic_density_norm/status`。轨迹段与格网相交长度比例用于分配驻留秒数，所有UAV累计；`D_raw=flight_seconds/(grid_area_km2*simulation_seconds)`，`D_norm`使用显式参考值或数据集分位参考值裁剪到0–1，不复制geometry。
- `ConflictDetector`对每个无序UAV pair只比较一次，在共同时间段内用局部等距近似计算二维相对位置/速度；仅当`0<t_CPA<T_lookahead`且`d_CPA<D_safe`时记录`potential_conflict`，同一pair在`conflict_cooldown_seconds`内不重复计数。三个阈值均为场景参数，不声明为安全标准；当前未使用高度做垂直分离判定。
- `grid_attributes.conflict`按CPA中点的WGS84坐标和半开边界规则映射回唯一`grid_id`，记录`conflict_count/conflict_rate/conflict_rate_norm/conflict_points/status`；`conflict_rate=conflict_count/simulation_seconds`，归一化同样支持显式或分位参考值。
- RiskModel调用方式仍为`evaluate(grid, grid_attributes, parameters)`。V1.1地面风险要求P与D有效：`G=clip(P*D*(1+lambda_T*T)+lambda_B*B,0,1)`；T缺失时只省略地形增量，B当前不启用。运行空中风险为`A=(alpha_D*D+(1-alpha_D)*C)`，缺失分量按既有规则从有效权重中剔除并报告完整度。Overall只组合当前可用的Ground与运行空中风险；M6空域约束指数继续独立保存在`airspace_constraint`，不冒充运行冲突风险。
- `traffic_simulation`保存仿真轨迹、参数、算法版本、指纹和冲突检测结果；traffic/conflict属性与`grid_risk.input_versions`随项目JSON往返恢复。仿真输入变化使traffic、conflict、risk依次stale；仅冲突输入变化使conflict/risk stale；人口/DEM变化只使risk stale；workspace变化清空仿真及三类派生结果。
- 第02步专题单选增加Traffic Exposure和Conflict Exposure，分别使用`traffic_density_norm`及`conflict_rate_norm`做0–1相对着色；单格查询增加flight count/seconds、原始与归一化traffic density、conflict count/rate，同时保留人口、DEM、空域和全部风险详情。不增加轨迹动画。

### 后续可替换规划接口约定（仅文档，本轮不实现）

- `RoutePlanner.plan(start, end, grid, risk, constraints) -> RouteResult`。`risk`接收独立的`grid_risk`契约；规划器不得读取原始栅格、QGIS对象或Workflow全局状态。现有`RoutePlannerV1`尚未适配该接口，本轮保持行为不变。
- `CNSSitePlanner.plan(route, required_cns, candidate_sites, device_catalog, parameters) -> CNSPlanResult`。`required_cns`是任务需求，`candidate_sites`是候选站址，`device_catalog`只提供地面设备能力；规划器不得把飞行器已有能力混入需求。现有`CoveragePlannerV1`尚未适配该接口，本轮保持行为不变。
- 未来建立两个独立目录：`catalogs/device_catalog/`管理C/N/S地面设备型号、覆盖与性能参数；`catalogs/aircraft_cns_profile_catalog/`管理典型无人机已有CNS能力及默认需求配置。每个飞行器profile必须分别保存`existing_capabilities`与`default_requirements`，调用时生成的`required_cns`也保持独立，禁止用“已有能力”隐式替代“任务需求”。

### V1 characterization 覆盖与实际契约

- `tests/test_v1_algorithm_characterization.py` 使用纯内存固定 fixture，锁定相同输入的完整重复结果和固定 SHA-256 `input_fingerprint`，不依赖 QGIS、网络、本机路径、时间戳或临时目录。
- `RoutePlannerV1` 当前公开结果字段为 `route_id/status/path/reason/algorithm_id/algorithm_version/input_fingerprint/environment_risk`。成功样例锁定绕开中心 BBOX 硬约束的折线路径和关键节点；失败样例锁定纵向硬约束完全阻断时的空路径与失败原因。当前没有 `algorithm_name`、距离或统计字段。
- `CoveragePlannerV1` 当前顶层字段为 `status/layers/physical_sites/algorithm_id/algorithm_version/input_fingerprint/parameters`；C/N/S 层分别包含状态、站点、统计和消息。固定样例锁定主站、补盲站、跨系统共址、物理站址、站点编号、平均覆盖重数以及未覆盖点/航段字段。
- Coverage V1 的当前可观察行为是：只要存在主站设备，发现零覆盖采样点便立即插入补盲站并把该点计为已覆盖，因此固定成功样例的 `uncovered_samples=0`、`uncovered_segments=[]`；缺少主站时该分系统直接返回 `missing_data`，这两个未覆盖字段仍为 `0` 和空列表。共址可能把新补盲站移动到已有站址，但当前实现不会在移动后重新核验该采样点是否仍处于覆盖半径内。上述行为仅记录并锁定，未在本轮修正。
- 两个 V1 都提供 `algorithm_id` 与 `algorithm_version="1.0"`，均不提供名为 `algorithm_name` 的字段。Route V1 不提供距离；Coverage V1 提供逐系统站点数、主/补盲/共址数、平均重数及未覆盖采样/航段，但不提供覆盖或未覆盖距离。

### 项目持久化 characterization 与安全期望

- `tests/test_project_persistence_characterization.py` 的全部文件均位于 pytest 临时目录；通过 QGIS 导入桩加载现有 `map_server.py` 保存/打开函数，不启动 QGIS、不访问网络，也不读写真实 `projects/`。
- 当前正常行为已锁定：`WorkflowService` 自动保存后可重新加载完整状态；主要字段、节点/航路、规则、设备、覆盖、风险及结果状态往返保持；另存会复制状态、写 `data_sources.json` 并把活动项目切换到新目录；打开有效目录会恢复项目状态和数据源。
- 当前安全行为已锁定：项目文件不存在时拒绝打开；数据源适配器加载失败发生在活动项目切换之前，当前 `WORKFLOW` 和 `ACTIVE_PROJECT_FILE` 保持不变，目标目录文件不被写入。
- 严格 `xfail` 安全期望：不支持的 schema 和损坏的项目 JSON 应报错且保留当前有效项目。现状是 `_load()` 静默生成新空项目，而 `open_project()` 仍切换活动项目。
- 严格 `xfail` 安全期望：保存异常不应遗留中间文件。现状是临时文件原子替换失败会留下 `.tmp`；`save_project_as()` 直接 `copy2` 到最终文件，复制中断可能留下部分 `project_state.json`。两种情况下原活动项目文件/指针仍保留，但目标目录需要清理或事务化。

### P1-3 持久化层边界

- `ProjectRepository` 只负责项目状态文件的存在性/文件检查、JSON 反序列化、保持既有参数的 JSON 序列化、`.tmp` 原子替换和 Save As 文件复制。
- `DataSourceRepository` 只负责地图源及项目数据源 JSON 的存在性/文件检查、反序列化和 `.tmp` 原子替换写入。
- `WorkflowService` 保留 schema 版本判定、空状态生成、静默恢复策略、时间戳更新及保存时机；`map_server.py` 保留活动路径选择、目标目录/文件名、legacy 文件回退、`WORKFLOW` 替换、数据源回退取值、QGIS `DATA.load()` 和失效编排。
- 所有生产调用方已统一通过 repository 读写项目状态、`map_sources.json` 和 `data_sources.json`；repository 不反向依赖上层，因此未形成循环依赖。
- `tests/test_repositories.py` 增加 3 项纯临时目录单测，锁定项目 JSON 往返、Save As 字节复制和数据源 JSON 往返。原 4 项持久化安全 `xfail` 保持不变。

Git 基线状态（2026-09-10）：`main` 已建立首个代码基线提交；源代码、测试、文档和可复现配置纳入版本控制，缓存、日志、临时文件、运行项目数据和机器相关设置由 `.gitignore` 排除。首个提交前复测结果为 **55 passed, 6 skipped, 0 failed**。

## 7. 架构原则

- 算法核心保持纯函数/明确契约，不直接依赖 HTTP、QGIS 对象、全局状态或文件路径。
- QGIS 适配负责 CRS、栅格/矢量读取和领域输入转换；算法不隐式把经纬度当等距平面。
- API handler 只做认证、请求解析、调用应用服务和响应映射；不承载业务规则。
- 工作流负责编排，领域模型维护不变量，仓储负责版本化、原子写入、迁移、备份与恢复。
- 项目数据、派生结果、算法/模型版本和输入指纹可审计；未知、缺失、失效与失败不得降级成通过。
- 数据源保持只读，在线服务故障不阻塞离线核心功能；凭据不进入日志或导出。
- 前端以单一状态源和显式 action 更新，地图视图状态与业务项目状态分离。
- 拆分采用小步、可回滚方式；先补 characterization tests，再搬迁代码，V1 算法结果必须保持不变。

## 8. 建议目标目录结构（本轮不移动）

```text
cns_planner/
  launchers/                 # 启动、QGIS runner 发现、进程健康等待
  api/
    server.py                # HTTP server 生命周期
    security.py              # 本机同源、token、请求限制
    handlers/                # map / workflow / project / sources / export
  application/
    workflow_service.py      # 六步用例编排
    commands.py              # 显式命令/输入 DTO
    review_service.py        # 状态聚合
  domain/
    project.py               # ProjectState 与不变量
    route.py
    coverage.py
    status.py
  algorithms/
    route/v1.py              # 保留 RoutePlannerV1 行为
    coverage/v1.py           # 保留 CoveragePlannerV1 行为
    grid/mht4063.py
    contracts.py
  risk/
    model.py                   # 已实现 RiskModel Protocol
    v1.py                      # 已实现保守占位模型与统一结果契约
  simulation/
    traffic_simulator.py       # 可复现多无人机直线轨迹
    conflict_detector.py       # 参数化二维CPA潜在冲突检测
  catalogs/
    device_catalog/            # 未来：C/N/S地面设备型号及性能
    aircraft_cns_profile_catalog/ # 未来：已有机载能力与默认需求配置
  gis/
    qgis_runtime.py          # Qt/QGIS 线程桥
    source_loader.py         # QGIS/GeoTIFF 装载与校验
    renderer.py              # 视口渲染
    workspace_health.py
    constraints.py           # GIS 几何到算法约束输入
  persistence/              # 已建立底层 JSON I/O 边界
    project_repository.py   # 已实现
    data_source_repository.py # 已实现
    migrations/
  services/
    data_registry.py
    data_health.py
    grid_service.py             # 已实现的 workspace→标准网格适配
    raster_grid_adapter.py      # 已实现的只读 GDAL/CRS/NoData 适配
    population_grid_service.py  # 已实现的原始人口值网格统计
    terrain_grid_service.py     # 已实现的 DEM 网格统计
    airspace_grid_service.py    # 已实现的空域属性状态与结果组装
    qgis_airspace_adapter.py    # 已实现的空域 CRS/空间索引/几何相交适配
    grid_spatial_index.py       # 网格BBOX空间桶与半开边界命中
    traffic_grid_service.py     # 轨迹驻留时间→grid_id交通暴露
    conflict_grid_service.py    # CPA事件→grid_id冲突暴露
    invalidation.py
    tile_cache.py
  web/
    index.html
    css/
    js/
      api.js
      store.js
      map.js
      tiles.js
      grid_theme.js           # 已实现的网格分级纯函数
      sources.js
      workflow/step01.js ... step06.js
tests/
  unit/
  characterization/         # 锁定 V1 业务结果
  integration/              # 项目仓储、QGIS/HTTP
  frontend/
  fixtures/
```

## 9. 当前技术债

1. `map_server.py`、`workflow.py`、`web/app.js` 是三个高耦合中心，修改影响面大；P1-3 只完成了前两者底层项目/数据源文件 I/O 的抽离。
2. 项目存储不是完整、版本化、可迁移的项目包；打开/另存缺少事务与恢复策略。不支持 schema 或损坏 JSON 会静默切换为空项目，保存失败会遗留 `.tmp` 或部分复制的最终文件。
3. 服务端全局可变状态在多线程 HTTP 下没有一致性边界，存在并发覆盖和读取中间状态的风险。
4. 两代 UI/项目模型并存且不共享状态；README 也同时描述多个阶段口径，容易造成维护歧义。
5. V1 航路把工作区固定离散为 `56 × 56` 经纬度网格，硬约束来自图层名称识别及整层 BBOX；这只是原型近似，尚非正式空间约束模型。
6. V1 覆盖把距离采样、布站、共址、补盲、覆盖判定和统计合为一体，且使用 demo/default 参数与设备库。
7. `SafetyResult` 与 `WorkflowService.review()` 重复聚合规则，状态主要以裸字符串穿过 API 和持久层。
8. terrain 数据源由 `MapData.metadata()` 补丁式加入，默认源含机器相关绝对路径，配置可移植性不足。
9. 默认测试不执行真实 QGIS HTTP 集成，且没有前端、并发、迁移和完整项目生命周期测试。
10. 当前 MH/T 模块尚未提供正式网格编码；M1 使用显式标记的稳定内部索引。大工作区为控制状态体积会自动降低网格层级，前端当前直接遍历可见网格，后续大数据属性和空间查询需要专用服务而不能继续膨胀工作流快照。
11. M2 当前逐格读取栅格窗口，优先保证正确的 CRS、边界和 NoData 语义；大工作区的批量窗口读取、空间缓存和后台进度尚未优化。人口源数据的实际物理单位仍须由数据提供方元数据或产品说明确认。
12. M3使用项目内相对分位分级，适合比较当前工作区内的高低，不适合跨项目绝对比较；Canvas仍会逐个绘制可见格，大于当前5000格上限或需要多专题叠加时应改用离屏缓存/WebGL，而不是继续增加同步paint负担。
13. M4 当前在每次 workspace 保存时同步重算全部空域映射；虽然已按工作区过滤并为每层建立空间索引，但大型矢量源仍需要后台任务、进度和增量缓存。`intersection_area` 的单位随源图层 CRS，正式风险模型应使用比率或先统一到等面积投影。
14. M6提供的是可解释相对指数，不是标准认可阈值或真实事故概率；默认权重和等级仅为显式标记的工程初值，空域类别权重默认未配置。建筑、财产暴露、基础设施、铁塔和绝对概率模型仍待真实数据与参数接入。
15. M7交通仿真仅支持恒速直线、二维位置和统一高度元数据；CPA采用局部平面近似且尚未使用垂直间隔、航迹不确定性或飞行动力学。归一化默认依赖当前项目分位数，只适合项目内相对比较，正式运行评估需要经审定的场景参数、3D冲突模型和跨项目稳定参考值。

## 10. 下一阶段计划

### P1：先固化行为，再拆边界

1. **已完成：**建立首个 Git 基线提交，并保存本文件所记录的测试结果。
2. **已完成：**为 `RoutePlannerV1`、`CoveragePlannerV1` 增加 characterization/golden tests，锁定成功、失败、编号、指纹、C/N/S、共址和缺口输出。
3. **已完成测试基线：**为项目自动保存、另存、打开、无效 schema、损坏文件和中途失败增加持久化 characterization；危险现状以严格 `xfail` 登记，修复尚未实施。
4. **部分完成：**项目仓储和数据源仓储已在不改变外部行为的前提下抽离；HTTP 路由和 QGIS 线程桥尚未拆分，原 API 保持不变。
5. 给工作流仓储和活动项目切换建立锁/事务边界，移除 handler 对模块级可变全局的直接写入。

### P2：收敛模型与前端状态

1. 引入类型化 `ProjectState`/DTO 和唯一的风险聚合实现，显式处理 schema 迁移与错误恢复。
2. 把 `WorkflowService` 拆为用例编排、领域校验、仓储、导出服务；接入统一失效策略。
3. 拆分 `web/app.js` 为 API、store、地图和六步页面模块，并补最小前端回归测试。
4. 将 terrain 正式纳入统一数据源注册/健康服务，去除 `MapData.metadata()` 的补丁逻辑。

### P3：再演进业务能力

1. **M1 已完成：**现有 MH/T 4063.1 平面网格已形成 workspace→service→workflow state/repository→API→地图图层的纵向闭环。
2. **M2 已完成：**人口与 DEM 分别通过小型 service 和共享的只读 GDAL adapter 映射到基础 `grid_id`，具备状态、来源、版本、定向失效、项目恢复和读取 API。
3. **M3 已完成：**标准网格轮廓、人口源值和DEM平均高程形成Canvas专题闭环，支持动态图例、无数据样式、缓存关联及单格详情。
4. **M4 已完成：**本地 QGIS 矢量空域通过独立 service/adapter 映射到基础 `grid_id`，具备 CRS 转换、空间索引、相交类别、精简来源属性、定向失效、保存恢复、API状态和单格查询闭环。
5. **M5 已完成：**建立可替换`RiskModel`、独立`grid_risk`持久化契约、扩展属性空命名空间及统一失效关系。
6. **M6 已完成：**RiskModelV1实现参数化Ground、Airspace Constraint和Overall相对风险，包含贡献解释、完整度、未知类别策略、风险等级及最小前端专题；未接入A*或CNS布站。
7. **M7 已完成：**建立可复现多无人机直线轨迹、按grid_id累计驻留时间的交通暴露、参数化二维CPA潜在冲突检测和冲突暴露，并将P/D/T Ground、D/C Operational Air及Overall接入RiskModelV1.1；未修改A*或CNS布站。
8. **M8建议：**在保持RiskModel接口不变的前提下，为运行场景增加真实/规划航迹导入与三维CPA（水平/垂直阈值分离）适配，建立可审计场景参数配置；随后再接建筑与财产暴露，避免把未经校准的模拟参数当作安全标准。
9. 在以上基线和边界稳定后，再按需求推进正式概率模型、项目包、报告和算法升级；不得在结构重构中顺带改变现有V1结果。
