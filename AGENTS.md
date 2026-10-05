# AGENTS.md —— CNS-PLANNER 项目级开发协议

> 本文件由 `@deepseek-ai/dsh-agent-instructions` 自动注入到每轮请求（项目根 → 当前工作目录的候选链）。
> 因此它必须短：**只放每轮都需要的规则**。背景知识一律留在文档里，按需查阅。

## 0. 项目身份与权威信息源

- **CNS-PLANNER**：低空航路规划与 CNS（通信/导航/监视）设施规划本地工作台。Python + QGIS/GDAL + 原生 JavaScript ES Modules（前端**无构建步骤**）。
- 权威状态：schema-v2 `ProjectState`（原子 JSON + `revision` 乐观锁）。大结果外置为 artifact（gzip + sha256），快照只留摘要/指纹/locator。
- 文档入口（**先读索引，不要读全文**）：
  1. [`AI_DEV_CONTEXT.md`](AI_DEV_CONTEXT.md) —— 精简决策索引，含"该查哪份文档"的地图与六条硬约束；
  2. [`README.md`](README.md) —— 项目定位、六步工作流、启动方式、算法注册表；
  3. 需要历史决策原因/字段契约时，才 `grep` 归档 [`docs/decisions/2026-09-ai-dev-context-full-archive.md`](docs/decisions/2026-09-ai-dev-context-full-archive.md)。
- 当前阶段：**Phase 4 冻结版 Release Candidate**。只做预验收、收敛与文档对齐；**不引入新算法、不进入 Phase 5**。
- 产品语义底线：**缺失 ≠ 0，unknown ≠ pass，未评估 ≠ 通过**。

## 1. 七条红线（违反即返工）

1. **依赖方向**：`API → Application → Domain/Algorithm`。算法包（`algorithms/`、`route_planner_v3/`、`layered_route_planner/`）**不 import QGIS/GDAL、不读文件、不写 ProjectState**。真实空间数据只由 `gis/` 适配为领域输入。
2. **算法语义冻结**：`Route Planning V1.0 frozen baseline` 的搜索/启发/目标函数、`RouteRiskProfile` 数学、`LayeredRouteValidation` 与 `VerticalTransitionValidation` 判据、`Route3DProfile` 几何派生——除 **bug 修复 / 真实数据适配 / 性能与 UI** 外不得改动。真要改必须显式版本化（升 `algorithm_version`/`geometry_version`、更新 fingerprint、旧结果 stale、完整回归）；**禁止静默变更语义**。
3. **`unknown != 0`、`missing_data != 0`**：缺数据/NoData/未确认**绝不补 0、绝不猜默认值、绝不静默 coarsen**。需要默认值时，正确做法是显式 `null` + `pending_confirmation`，并在 UI/报告说明"证据不足"。
4. **适飞空域 = `display_only_reference_layer`**：不参与 risk、可行性、search、readiness、fingerprint、staleness、CNS。DATA-3 = `retired/not_applicable_by_architecture_decision`。
5. **候选/提案不具权威性**：candidate / proposal / recommendation / preview 不得直接写正式容器（如 `operational_routes`、`required_cns`、`confirmed_cns_plan`）。只有显式人工确认（Adopt / Confirm / Apply）才升级。`operational_route=false`、`proposal_only=true`、`requires_closed_loop_validation=true`、`final_validation_performed=false` 等标记下游必须尊重。
6. **失效单向且最小**：一切失效传播以 [`cns_planner/application/invalidation_service.py`](cns_planner/application/invalidation_service.py) 为唯一权威。只 stale 真正依赖该输入的下游；**不得反向 stale 上游**，不得图省事调用过宽的失效入口（例如给 route 发布调用 `workflow("route")` 会立刻 stale 刚发布的结果）。
7. **写权限**：先确认目标容器的权威写入者，再动手（索引 §2 有速查表 + grep 命令）。不得新增第二个写入者，不得绕过 service 直接改 `state[...]`。

## 2. 任务分派

| 任务 | 先看 |
|---|---|
| 改航路规划 / candidate / validation / adoption | `AI_DEV_CONTEXT.md` 硬约束 2、[`docs/route_planner_v3_architecture.md`](docs/route_planner_v3_architecture.md)、`cns_planner/layered_route_planner/`、`cns_planner/domain/layered_route.py` |
| 改真实数据链路（地形/建筑/人口/网格） | `cns_planner/gis/`（`source_loader`、`layered_feasibility_adapter`、`building_footprint_aggregation`、`path_resolver`）、`cns_planner/data/registry.py` |
| 改前端 | `cns_planner/web/js/`（ES Module，无构建）；全局状态只能经 `web/js/state/workflow_snapshot.js` 的 `applyWorkflowSnapshot` 写入 —— 见红线之外的 `BUG-GRID-POPUP-001` 约定 |
| 改 CNS 能力/覆盖/走廊/站址/评审/报告 | `cns_planner/application/` 对应 service + `docs/15-Phase4-Final-Preacceptance.md` |
| 改 schema / 持久化 | `cns_planner/application/project_state.py`、`cns_planner/persistence/`；**additive + backfill 优先**，旧项目必须仍能打开 |
| 排查数据源/健康/失效 | `cns_planner/data/health.py`、`source_audit`、`invalidation_service.py` |

## 3. 开发闭环（默认路径）

1. **定位**：`grep` 找函数/契约/调用点，确认权威写入者与失效入口。不要先通读目录。
2. **读最小上下文**：`read` 带 `offset`/`limit` 只读目标片段。**同一文件在一次任务中不要整读两次以上**。
3. **改一个逻辑单元**：一次改完一个完整函数/契约块，不要"改一行 → 跑一次 → 再改"（每次往返都会重发整个上下文，是最大的成本来源）。
4. **定向验证**：只跑相关测试文件（见 §4）。开发循环里**不要**跑全量 `pytest tests/`。
5. **提交前**：跑影响面测试 + 相关 `node --check`，必要时 `git diff --check`。

## 4. 测试与检查命令（精确到文件）

Python 解释器固定为 `D:\tools\anaconda\python.exe`（Python 3.13.9 / pytest 8.4.2）。

```powershell
# 定向（开发循环默认用法）—— 只跑相关文件，输出收敛
D:\tools\anaconda\python.exe -m pytest -q -x --tb=short -p no:cacheprovider tests/test_<相关>.py

# 关键词批量
D:\tools\anaconda\python.exe -m pytest -q -k "layered or theta" 2>&1 | Select-Object -Last 25

# 全量（只在收尾/交付前跑一次，且必须收窄输出）
D:\tools\anaconda\python.exe -m pytest -q -p no:cacheprovider 2>&1 | Select-Object -Last 40
```

前端（Node，仓库根目录执行；`node:test` 在进程内运行，**不要**用 `node --test <file>`）：

```powershell
node tests/frontend_modules.test.mjs
node tests/grid_theme.test.js
node --check cns_planner/web/app.js
node --check cns_planner/web/js/main.js
```

真实 QGIS HTTP 集成默认跳过：`tests/test_map_http.py` 需 `$env:CNS_MAP_TESTS=1` 且 8765 上已有服务。

**沙箱注意**：受限文件沙箱下，pytest 的 `--basetemp` 与每个 per-test 目录用 mode `0o700`，会被拒绝写入，产出大量伪 setup error。若在受限沙箱内跑，需显式给可写 basetemp，并把 `%TEMP%` 指到工作区内（见归档 §8 的完整命令）。**看到大面积 setup error 时先怀疑沙箱，而不是代码。**

## 5. 省 token 的工程纪律（请严格遵守）

本项目的成本几乎全部来自"每一步都重发整个上下文"。请求次数与上下文体积就是你该控制的两个旋钮。

- **禁止在根目录做全仓探索**。根目录有大量临时/生成目录（`_diag/`、`_diag2/`、`_out/`、`_output/`、`_work/`、`_vbase*/`、`_round29j/`、`_round29k/`、`_bt_sim*/`、`b9tmp/`、`b9_e2e_runs/`、`tmp_b9/`、`outputs/`、`projects/`、`_cscan/`、`_migration_smoke/`、`_tmproot/`、`_runtime*/`、`.pytest_cache/`、`.pytest_tmp/`、`.test_runtime/`）。这些**不是源码交付**，不要读、不要 grep、不要"顺便看看"。
- **工具输出必须收敛**：pytest 加 `-q -x --tb=short` 并 `Select-Object -Last N`；grep 结果过多时先收窄 pattern/include；**禁止**整文件 `read`（用 `offset`/`limit`）；**禁止**打印整份大 JSON/GeoJSON/报告。
- **同一文件不重复读**：第一次读就该带够行数；需要第二段时用 `offset` 定位，而不是重读全文。
- **批量机械改动**（改名、批量加断言、批量补参数）优先交给 `subagent` 处理，主会话只做决策与验收 —— 不要把几百次碎 edit 留在主上下文里。
- **不要为了"确认一下"重复跑同一命令**。先看已有输出，再决定是否需要新证据。
- 长任务请**分会话**推进：一个功能一个会话。单会话跑到数百步后，每一步都在为前面全部历史付费。

### 5.1 编辑方式收敛

- **一次 edit 覆盖一个完整逻辑单元**（整段函数 / 整块契约 / 整组相关行），不要"改一行 → 跑一次 → 再改"。
- 优先用 `grep` 定位精确行，再用 `edit` 的 `old_string` 做**最小而完整**的替换；`replace_all` 只用于真正需要全局替换的场合。
- 需要读第二段时用 `read` 的 `offset`/`limit` 定位，**不要重读整个文件**。
- 同一文件在一次任务中整读不超过两次。
- 改动前先确认目标容器的权威写入者与失效入口（见 `AI_DEV_CONTEXT.md` §2、skill `cns-invalidation-chain`），避免"先改后返工"。

### 5.2 工具输出收敛

- pytest 一律 `-q`（必要时 `-x`）+ `--tb=short`，并用 `Select-Object -Last N` / `Select-String` 截尾；
- 大 JSON / GeoJSON / 报告**不要整份打印**，用脚本先算摘要再输出；
- `grep` 命中过多时先收窄 `pattern` 或加 `include`，不要直接读溢出结果文件；
- 需要多个不相关检查时合并成**一条**命令，减少往返次数。

## 6. 目录导航

```text
cns_planner/
  api/                 HTTP 路由、同源/会话 token/乐观锁门禁
  application/         六步用例编排、失效传播、各容器的唯一写入者（production write authority）
  domain/              schema-v2 与领域契约（constraint field、CNS 基线、成熟度、安全事件…）
  gis/                 QGIS/GDAL 适配、CRS、栅格/矢量、地形与建筑事实、路径解析
  data/                数据源 Registry、SourceProfile、健康检查、grid_id 映射
  algorithms/          production 算法实现（route / corridor / corridor_gap / coverage / service_capability / radar_layout…）
  layered_route_planner/  生产默认水平规划器（Theta* V2）
  route_planner_v3/    研究线 V3-A/B/C（未注册进 Registry，不写正式容器）
  tasks/               Heavy task：immutable snapshot、worker、可取消
  persistence/         原子 JSON repository、项目压缩、artifact 引用
  reporting/           报告构建、HTML/PDF 渲染
  reference_data/      只读参考数据（起降点、航线、设备事实目录）
  web/                 HTML/CSS/ES Modules 前端（六步工作台）
  compatibility/       旧项目/旧算法只读兼容
  legacy/              Streamlit/schema-v1 原型 —— **新代码不得新增对它的依赖**
tests/                 正式测试套件（pytest + node:test）
tools/                 只读报告/审计/基线生成脚本
docs/                  阶段报告与契约文档；docs/decisions/ 为历史决策归档
```

## 7. 本机环境注意

- 启动：`python map_app.py` 或双击 `启动地图.cmd`；就绪后 <http://127.0.0.1:8765>；日志 `outputs/map-server.log`。QGIS Python 非默认位置用 `CNS_QGIS_PYTHON` 指定。
- `D:\Projects\CNS-PLANNER\_diag` 目录权限异常：子进程（Python/Node）**无法在该目录写文件**（`PermissionError`/`EPERM`）。需要临时诊断产物时用 `_diag2` 或 `outputs/`。
- 本机存在两个仓库副本（`D:\Projects\CNS-PLANNER` 与 `C:\Users\yiding\Documents\ChatGPT\CNS规划系统`）。**工作区以 `D:\Projects\CNS-PLANNER` 为准**，不要混用路径。
- 面向用户的文本用简体中文；代码/API/schema/算法 ID 保持英文契约不变（归档 §6 P19 已定此原则）。
- **BOM 陷阱（曾导致应用无法启动）**：`profiles/desktop/package.json` 等 DSH 配置文件被 Harness **直接 `JSON.parse`、不去 BOM**，开头带 `EF BB BF` 会在第 0 个字符崩溃、表现为**软件起不来**。而 PowerShell 5.1 的 `Set-Content -Encoding UTF8` / `Out-File -Encoding UTF8` **会写入 BOM**。
  - 写 JSON/YAML 配置一律用：`[System.IO.File]::WriteAllText($p, $text, (New-Object System.Text.UTF8Encoding($false)))`；读取时先 `TrimStart([char]0xFEFF)`。
  - **例外**：`.ps1` 脚本本身**需要** BOM——否则 `powershell.exe`(5.1) 按 ANSI 解码中文注释与字符串，引号错乱并报假的"缺少右 }"。
  - 改完自检：`python tools/remove_config_bom.py --check`。
