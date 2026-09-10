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
  ├─ 项目保存/打开
  └─ WorkflowService
       ├─ RoutePlannerV1
       ├─ CoveragePlannerV1
       ├─ ResultLedger / ResultStatus
       └─ JSON 状态文件

cns_planner/web/index.html + style.css + app.js + tiles.js
  └─ 六步界面、Canvas 地图、浏览器端 XYZ 瓦片、API 调用和前端状态

legacy_app.py -> cns_planner/ui/app.py -> Project + storage.py
  └─ 第一阶段 Streamlit 元信息原型；与新版工作台状态不共享
```

运行时以本机 `127.0.0.1:8765` 提供服务。HTTP 请求使用线程服务器；QGIS 操作通过 `Queue + Future` 回到 Qt 所在线程。新版项目状态默认保存在 `projects/current_project.json`，地图源保存在 `projects/map_sources.json`；另存项目时使用所选目录内的 `project_state.json` 和 `data_sources.json`。

## 2. 六步业务流程

1. **项目与数据**：编辑项目名，选择项目目录进行保存/打开，搜索地名；统一数据源中心负责 QGIS、人口、地形及其他数据源的登记和健康提示。
2. **工作区与环境**：在地图上绘制 WGS84 矩形工作区，计算近似面积，并检查人口、地形和本地图层是否覆盖工作区。
3. **航路设计**：添加/删除起降点，按方向生成场景航路，维护不复用的航路编号，再由 `RoutePlannerV1` 生成运行航路。
4. **运行规则**：录入飞行器、速度、MTBF、双向高度、水平间隔和链路时延，计算 λ、总时延及反应距离并校验规则；规则变化会使下游结果失效。
5. **设备与布站**：配置 C/N/S 主站与补盲设备，由 `CoveragePlannerV1` 独立布站、共址、采样检查覆盖并输出统计。
6. **确认与导出**：审查环境、技术、生命和财产风险以及依赖状态；导出项目 JSON、航路 GeoJSON、站点 GeoJSON并保存当前项目。

## 3. 已实现能力

- Windows 本地启动器：发现 QGIS Python、复用健康服务、写启动日志、等待就绪并打开浏览器。
- QGIS 项目、本地空域图层、人口 GeoTIFF、GLO-30 DEM 的只读装载、校验、样式化和按视口渲染。
- 浏览器端平移、缩放、定位、图层开关、透明度控制；在线 XYZ 瓦片与 QGIS 本地渲染分离，并具有限流、缓存和过期请求取消。
- 本机文件/目录浏览，地图数据源“仅校验”和“应用”，文件级及工作区覆盖健康检查，在线瓦片与地名搜索独立检查。
- 六步工作流状态自动保存与重启恢复，项目目录另存/打开，稳定节点/航路序号和退役航路号。
- 当前端到端原型：矩形工作区、场景航路、基于固定规则网格的 A* 运行航路、硬约束 BBOX 阻断、运行规则校验、C/N/S 确定性覆盖布站、补盲和共址。
- 明确的多状态结果与保守失效传播；未知风险不会被聚合为通过。
- 项目 JSON、航路 GeoJSON、站点 GeoJSON 导出。
- MH/T 4063 网格几何计算模块及专项测试；尚未接入六步工作流。

上述算法、设备库和工程参数仍属于可替换的工程原型；正式风险代价、生命风险、财产风险、真实障碍几何和规范参数尚未完成。

## 4. 重要文件职责与负担判断

| 文件 | 当前职责 | 判断 |
|---|---|---|
| `map_app.py`（83 行） | QGIS runner 发现、进程启动、日志、健康等待、浏览器打开 | 当前职责集中，是薄启动器；后续仅需避免继续加入业务逻辑。 |
| `app.py`（27 行） | CLI 入口转发及 Streamlit iframe 兼容入口 | 足够薄。 |
| `cns_planner/map_server.py`（808 行） | QGIS 生命周期和线程桥、数据装载/样式/元数据/渲染、数据健康补丁、硬约束提取、文件浏览、在线检查、项目存取、HTTP 安全/路由/响应、全局运行状态 | **职责严重过多，是首要拆分对象。** 基础设施、应用服务和传输层彼此耦合。 |
| `cns_planner/services/workflow.py`（339 行） | 状态 schema/default/load/save、项目/工作区/节点/航路 CRUD、场景生成、算法编排、规则和设备校验、失效、风险聚合、三类导出 | **职责过多。** 工作流编排、仓储、校验、状态机和导出混在一个可变字典服务中。 |
| `cns_planner/algorithms/route_planner.py`（104 行） | 经纬度到固定网格映射、约束 BBOX 栅格化、A*、路径简化、结果/指纹/风险占位组装 | 文件不大但边界不清；算法核与 GIS/结果适配应分离，保持现有 V1 行为作为回归基线。 |
| `cns_planner/algorithms/coverage_planner.py`（108 行） | 距离和插值、设备选择、C/N/S 循环、主站/补盲、物理站址共址、覆盖采样、缺口统计、编号及指纹 | 文件不大但业务职责密集；布站、共址、覆盖评估和结果组装应形成独立策略接口。 |
| `cns_planner/web/app.js`（795 行） | API 客户端、全局状态、地图坐标/Canvas 绘制、交互、六步 HTML 模板与事件绑定、数据源中心、在线检查、本机浏览器 | **前端首要拆分对象。** 状态、视图和副作用均依赖模块级可变变量，难以单测。 |
| `cns_planner/web/tiles.js`（216 行） | 浏览器端 XYZ 选择、并发、缓存、请求、绘制与健康检查 | 相对独立，但网络状态、缓存策略和渲染仍可进一步解耦。 |
| `models/status.py` + `services/invalidation.py` | 结果枚举、风险聚合模型、依赖失效表 | 方向正确；当前 `WorkflowService.review()` 又实现了一遍聚合逻辑，存在双源规则。 |
| `services/data_registry.py` + `data_health.py` | 数据源目录和阶段化健康检查 | 已分层，但 terrain 的登记/健康逻辑仍由 `MapData.metadata()` 临时补入。 |
| `domain.py` + `storage.py` + `ui/app.py` | 旧 Streamlit 元信息模型、序列化与页面 | 与 schema v2 新工作流并存，形成两个项目模型和两套状态/保存语义。 |

## 5. 项目保存与状态管理现状

- `WorkflowService` 直接持有并修改嵌套 `dict`，每次操作后把整个状态写入同一个 JSON；单文件写入使用临时文件替换，具备基础原子性。
- schema 不匹配、JSON 损坏或读取异常时会静默创建空项目，没有显式迁移、隔离、备份或错误审计。
- “另存为”先保存活动状态再复制到目标，随后重建全局 `WORKFLOW`；数据源另存为第二个 JSON。当前不是 `manifest/data/results/audit` 形式的完整项目包，也不复制或校验外部数据。
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

结果：**59 passed, 6 skipped, 0 failed**。

6 项跳过均来自 `tests/test_map_http.py`，原因是需要先启动真实 QGIS 地图服务并设置 `CNS_MAP_TESTS=1`。默认测试覆盖：项目元信息序列化、Streamlit 骨架、状态聚合和失效、数据健康、启动器、瓦片缓存、六步工作流原型、V1 航路/覆盖 characterization、A* 硬约束失败，以及 MH/T 4063 网格几何。当前缺口包括前端自动化、项目目录 save/open 回归、并发写入、schema 迁移/损坏恢复和真实 QGIS 集成的自动化启动。

### V1 characterization 覆盖与实际契约

- `tests/test_v1_algorithm_characterization.py` 使用纯内存固定 fixture，锁定相同输入的完整重复结果和固定 SHA-256 `input_fingerprint`，不依赖 QGIS、网络、本机路径、时间戳或临时目录。
- `RoutePlannerV1` 当前公开结果字段为 `route_id/status/path/reason/algorithm_id/algorithm_version/input_fingerprint/environment_risk`。成功样例锁定绕开中心 BBOX 硬约束的折线路径和关键节点；失败样例锁定纵向硬约束完全阻断时的空路径与失败原因。当前没有 `algorithm_name`、距离或统计字段。
- `CoveragePlannerV1` 当前顶层字段为 `status/layers/physical_sites/algorithm_id/algorithm_version/input_fingerprint/parameters`；C/N/S 层分别包含状态、站点、统计和消息。固定样例锁定主站、补盲站、跨系统共址、物理站址、站点编号、平均覆盖重数以及未覆盖点/航段字段。
- Coverage V1 的当前可观察行为是：只要存在主站设备，发现零覆盖采样点便立即插入补盲站并把该点计为已覆盖，因此固定成功样例的 `uncovered_samples=0`、`uncovered_segments=[]`；缺少主站时该分系统直接返回 `missing_data`，这两个未覆盖字段仍为 `0` 和空列表。共址可能把新补盲站移动到已有站址，但当前实现不会在移动后重新核验该采样点是否仍处于覆盖半径内。上述行为仅记录并锁定，未在本轮修正。
- 两个 V1 都提供 `algorithm_id` 与 `algorithm_version="1.0"`，均不提供名为 `algorithm_name` 的字段。Route V1 不提供距离；Coverage V1 提供逐系统站点数、主/补盲/共址数、平均重数及未覆盖采样/航段，但不提供覆盖或未覆盖距离。

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
  gis/
    qgis_runtime.py          # Qt/QGIS 线程桥
    source_loader.py         # QGIS/GeoTIFF 装载与校验
    renderer.py              # 视口渲染
    workspace_health.py
    constraints.py           # GIS 几何到算法约束输入
  persistence/
    project_repository.py
    data_source_repository.py
    migrations/
  services/
    data_registry.py
    data_health.py
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

1. `map_server.py`、`workflow.py`、`web/app.js` 是三个高耦合中心，修改影响面大。
2. 项目存储不是完整、版本化、可迁移的项目包；打开/另存缺少事务与恢复策略。
3. 服务端全局可变状态在多线程 HTTP 下没有一致性边界，存在并发覆盖和读取中间状态的风险。
4. 两代 UI/项目模型并存且不共享状态；README 也同时描述多个阶段口径，容易造成维护歧义。
5. V1 航路把工作区固定离散为 `56 × 56` 经纬度网格，硬约束来自图层名称识别及整层 BBOX；这只是原型近似，尚非正式空间约束模型。
6. V1 覆盖把距离采样、布站、共址、补盲、覆盖判定和统计合为一体，且使用 demo/default 参数与设备库。
7. `SafetyResult` 与 `WorkflowService.review()` 重复聚合规则，状态主要以裸字符串穿过 API 和持久层。
8. terrain 数据源由 `MapData.metadata()` 补丁式加入，默认源含机器相关绝对路径，配置可移植性不足。
9. 默认测试不执行真实 QGIS HTTP 集成，且没有前端、并发、迁移和完整项目生命周期测试。

## 10. 下一阶段计划

### P1：先固化行为，再拆边界

1. **已完成：**建立首个 Git 基线提交，并保存本文件所记录的测试结果。
2. **已完成：**为 `RoutePlannerV1`、`CoveragePlannerV1` 增加 characterization/golden tests，锁定成功、失败、编号、指纹、C/N/S、共址和缺口输出。
3. 为项目自动保存、另存、打开、无效 schema、损坏文件和中途失败增加仓储测试；明确错误必须保留当前有效项目。
4. 从 `map_server.py` 优先抽出无业务变化的 HTTP 路由、QGIS 线程桥、项目仓储和数据源仓储；保留兼容 façade 与原 API。
5. 给工作流仓储和活动项目切换建立锁/事务边界，移除 handler 对模块级可变全局的直接写入。

### P2：收敛模型与前端状态

1. 引入类型化 `ProjectState`/DTO 和唯一的风险聚合实现，显式处理 schema 迁移与错误恢复。
2. 把 `WorkflowService` 拆为用例编排、领域校验、仓储、导出服务；接入统一失效策略。
3. 拆分 `web/app.js` 为 API、store、地图和六步页面模块，并补最小前端回归测试。
4. 将 terrain 正式纳入统一数据源注册/健康服务，去除 `MapData.metadata()` 的补丁逻辑。

### P3：再演进业务能力

在以上基线和边界稳定后，再按需求推进真实 GIS 约束适配、正式风险模型、项目包、报告和算法升级；不得在结构重构中顺带改变现有 V1 结果。
