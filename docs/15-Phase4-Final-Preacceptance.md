# Phase 4 冻结版预验收报告（Phase4-FINAL-RC Preacceptance）

> 本轮范围：**冻结版六步流程预验收 + README 收敛 + 本地遗留清理**。
> 不做 Phase 5、不开发新算法、不修改 frozen 数学、不 commit / push / 打新 tag。

## 0. 基线与身份

| 项 | 值 |
|---|---|
| 冻结基线 | tag `phase4-final-freeze`（本报告记录的就是该 tag 上的验收；该 tag 之后 main 上继续做预验收稳定化修复，见第 16 节） |
| HEAD（验收当时） | `30281644731602bf967ee44d866da470ecb4f2b0` |
| 固定解释器 | `D:\tools\anaconda\python.exe`（Python 3.13.9） |
| 地理依赖 | shapely 2.1.2 / geopandas 1.1.4 / pyproj / rasterio 在固定解释器内**可用**，本轮**未出现 ENVIRONMENT-LIMITED** |
| 真实后端 | QGIS 3.44.14 自带 `python-qgis*.bat` 运行 `-m cns_planner.map_server`（`http://127.0.0.1:8765`），`/api/health` 如实上报 `git_commit` |
| 隔离验收项目 | `C:\Users\yiding\Documents\ChatGPT\CNS_VALIDATION\phase4_final_smoke\rc_acceptance_run`（由 `release_chain_fixture` 复制；源 fixture 与用户正式项目**均未被修改**） |
| 验收脚本 | `tools/phase4_rc_acceptance.py`（六步真实 HTTP 验收）、`tools/phase4_rc_restart_check.py`（重启复验）；运行期中间产物在 `C:\Users\yiding\Documents\ChatGPT\_cns_verify\`（工作区外，不污染仓库） |

## 1. 验收结论速览

| 步骤 | 结论 |
|---|---|
| Step 1 数据准备 | **PASS** |
| Step 2 环境与风险 | **PASS_WITH_WARNING**（真实数据链的 constraint evidence 未配置，如实 `unknown/not_calculated`） |
| Step 3 航路规划与发布 | **PASS** |
| Step 4 CNS需求 | **PASS** |
| Step 5 CNS能力与设施规划 | **PASS_WITH_WARNING**（coverage 停在 `missing_data`：无可用三维几何 provider） |
| Step 6 方案评审与报告 | **PASS**（真正走到最终报告，四类 artifact 可读） |

```text
PHASE4_PREACCEPTANCE_READY=true
```

真实 HTTP 验收：**69 checks / 0 failed**（elapsed 82.3s）。

## 2. 六步逐步结果

### Step 1 数据准备 — PASS

- 项目 Open/Save/Reopen 正常；`project_storage.file` 随 Open 正确切换目标项目。
- canonical workflow 快照可读，六步导航元数据完整。
- 强制要求：**未出现错误自动切换项目**；**未出现 runtime binding 丢失**。

### Step 2 环境与风险 — PASS_WITH_WARNING

- canonical L8 网格存在（`grid` 摘要可用）。
- **高度层为数据驱动**：目录实例为 `ALT-ZS-300-EGM2008`（非 080 层），证明架构**没有写死 80 m**；`ALT-060/080/100` 只是目录实例。
- Population NoData policy = `not_configured`（`no_confirmed_population_nodata_semantics`），如实显示而未猜值。
- Shelter coefficient policy = `confirmed`，`default_coefficient=1.0`（engineering baseline）。
- Planning Constraint Field 接口可读；本 fixture 为 `not_calculated`，属真实数据链未配置（**warning，非缺陷**）。
- 地图"高度层障碍"默认关闭与 blocked/unknown 表达由前端模块契约测试覆盖（见第 7 节）。

### Step 3 航路规划与发布 — PASS

- 运行航路存在：`SCN-ZS-TAOHUADAO-HANJINGWAN`（5 点位，2D path）。
- 高度层 identity 由 provenance 携带（`candidate_id` 含 `ALT-ZS-300-EGM2008`），且 `egm2008_altitude_in_third_coordinate=false`（EGM2008 高度不混写进第三维）。
- 风险画像、连续验证、operational adoption 记录齐备。
- algorithm catalog 未把 `RoutePlannerV1` 作为 production 默认（无旧 fallback）。
- 约束：blocked 单元不可搜索、`unknown` 默认不可扩展等硬语义，其真实数据侧证据由本 fixture 的 fail-closed 结果与源码/单测共同覆盖（第 7 节）。

### Step 4 CNS需求 — PASS

- `required_cns.status = passed`，`source = explicit_adoption_from_required_cns_recommendation`，即经**显式 Adopt** 才成为 canonical 结果；推荐本身不具权威性。

### Step 5 CNS能力与设施规划 — PASS_WITH_WARNING

- **Existing CNS baseline 声明（本轮新增闭环保卫路径）**：
  - 静默空基线（无 assumption）被拒绝；
  - 无设施/证据的 `confirmed_present` 被拒绝；
  - `confirmed_present + assume_empty_for_planning` 自相矛盾声明被拒绝；
  - 合法 `not_declared + assume_empty_for_planning` + 显式工程假设被接受并持久化，assumption 保留 `report_disclosure`，重复声明幂等。
- coverage_3d：`missing_data` —— 原因如实为"没有可用的明确三维几何服务提供者"（fixture 未导入既有 CNS 设施）。**未伪造 0 或 pass**（warning）。
- cns_service_capability：`does_not_meet_under_model`，`maturity=engineering_baseline`。
- service corridor 同步路径：`pending_confirmation`，指纹可用。
- **service corridor 异步 heavy task（优先验证路径）**：HTTP `202` + `task_id` → heartbeat 可观测 → 终态 `succeeded` → 结果发布并驱动 P14。immutable snapshot 指纹校验生效。
- corridor gap：`pending_confirmation`；corridor site plan：`no_action_required`。
- Radar：readiness 只读可读，`not_ready`，**不阻塞主链**（默认 OPTIONAL）。
- performance admission：超天花板请求按包线处理，**hard ceiling 不可 override**。

### Step 6 方案评审与报告 — PASS

真实走通 `initialize → select → evaluate → confirm → apply → report`：

- `S6-SELECT-2`：select 生效但**不产生** confirmed plan（`select ≠ confirm`）。
- `S6-CONFIRM-3`：confirm 后 `application.status=not_applied`（`confirm ≠ apply`）。
- `S6-APPLY-0`：错误 `plan_id` 被拒绝/判 stale；`S6-APPLY-2`：apply 后 `plan_id` **未被替换**（`CP-b53ffe8d814a2733115a`）。
- 报告：`status=passed`，`active_report_id=RPT-c10f960bc5b9e6232bfbda7b`，`source_plan_id` 与 confirmed plan 一致（报告引用同一正式输入）。
- artifact 可读：`report.html` 431 746 B、`report.pdf` 186 029 B（`%PDF`）、`planning-package.zip` 250 638 B（`PK` magic）、`/api/export/sites` 可用。

## 3. 修复的 BLOCKER / MAJOR

本轮共发现并修复 **3 个高置信缺陷**（2 MAJOR + 1 BLOCKER），全部为**最小闭环保卫/绑定修复**，未改动任何 frozen 数学、阈值或 authority 语义。

### BUG-01（MAJOR）Existing CNS baseline 缺少 readiness 声明守卫

| 字段 | 内容 |
|---|---|
| step | Step 5 |
| 操作 | `POST /api/cns-existing-baseline` 声明 `confirmed_present`（无设施、无证据） |
| expected | 拒绝——`confirmed_present` 无法解析到设施或证据 |
| actual | 返回 200 并持久化一个 readiness=`blocked` 的声明 |
| 证据 | 修复前 `GET /api/cns-existing-baseline` 返回 `{"knowledge_status": null, ...}`；域函数 `existing_cns_baseline_readiness` 已能判定 `confirmed_present_facilities_or_evidence_required`，但应用层只在 `assume_empty_for_planning` 分支校验 |
| 根因 | `CNSInputService.set_existing_baseline` 仅对空基线模式检查 readiness，其余模式直接落盘 |
| 是否修复 | 是 |
| 修改文件 | `cns_planner/application/cns_input_service.py` |
| 回归结果 | `tests/test_cns_existing_baseline.py` 7 passed（新增第 140 行起 3 项）；HTTP 验收 S5-BASE-1/2/2b 全 PASS |

### BUG-02（BLOCKER）重算 P15 使 P16 必然 stale，P18 审阅无法进入

| 字段 | 内容 |
|---|---|
| step | Step 6（触发路径在 Step 5） |
| 操作 | Step 5 依次执行 corridor → gap → site-plan 后，进入 Step 6 点击"初始化方案审查" |
| expected | 审阅可初始化（P14/P15 均为 current） |
| actual | `P18 需要 current cns_corridor_gap_assessment` / `P18 需要 current P16 proposal/status`，**Step 6 完全无法进入** |
| 证据 | 真实 HTTP：`/api/cns-plan-review/initialize` 返回 400；逐步追踪显示 gap 重算后 `cns_corridor_site_plan` 变 `stale`，而 site-plan 重算又使 gap 变 `stale`，两者互斥 |
| 根因 | ①`corridor_gap_service.evaluate` 与 `corridor_service` 的写入路径**无条件**调用失效，即使重算得到**完全相同的 `input_fingerprint`**；②`PlanReviewService._require_current_inputs` 把 `stale` P16 当作不可用 |
| 是否修复 | 是 |
| 修改文件 | `cns_planner/application/corridor_service.py`、`cns_planner/application/corridor_gap_service.py`、`cns_planner/application/plan_review_service.py` |
| 回归结果 | `tests/test_plan_review.py` 12 passed（新增 3 项：P15 指纹不变保持 P16、P14 指纹不变保持 P15/P16 且 P18 可初始化、P14 真 stale 仍 fail-closed）；HTTP 验收 S6-INIT-1/2 起 Step 6 全链 PASS |

修复要点：新增共享判定 `conclusion_changed(previous, current)` —— **只有 `input_fingerprint` 变化才让下游失效**，同一结论的重算不再让 P15/P16 变 stale。P18 门禁的处置已由本轮 PREACCEPTANCE-R1 收紧，见第 16 节（当时临时接受 stale P16 的做法已撤销）。

### BUG-03（MAJOR/BLOCKER）项目切换后 heavy task 冻结旧项目输入，异步任务永不发布

| 字段 | 内容 |
|---|---|
| step | Step 5（async heavy task） |
| 操作 | Open 项目 B 后提交 async 服务走廊评估 |
| expected | 任务按项目 B 的输入计算并发布正式结果 |
| actual | 任务全部终态 `stale` / `task_input_changed`，**永不发布** |
| 证据 | 任务详情：`advanced.error.code=task_input_changed`；对提交快照做逐字段离线比对发现快照记录的是**另一份（空）项目输入**（`grid.cells=null`、`routes=0`、`required_cns=pending_confirmation`），而 publish 时当前 state 是完整项目 → 指纹必然不同 |
| 根因 | `HeavyTaskService.bind_project(project_file)` 只重新绑定 `project_file/workdir/store/snapshot_store`，**没有重新绑定 `self.workflow`**；项目切换会用新的 `WorkflowService` 实例替换当前实例，提交阶段却仍从旧实例取 state |
| 是否修复 | 是 |
| 修改文件 | `cns_planner/tasks/service.py`、`cns_planner/application/app_context.py` |
| 回归结果 | `tests/test_phase4_b6r_immutable_task_input.py` 30 passed（新增 `test_project_switch_rebinds_workflow_for_new_submissions`，并保留仅传 `project_file` 的旧签名兼容）；HTTP 验收 S5-ASYNC-1..5 全 PASS（`succeeded` + 结果发布） |

## 4. 未修 Minor / UX / 观察项

| ID | severity | step | 说明 |
|---|---|---|---|
| MINOR-01 | MINOR | 全局 | `/api/state` 的 `project_dir` 字段为 `null`（真实项目路径在 `project_storage.file/directory`）。若前端有依赖该字段的展示需另行确认；本轮未改动该契约。 |
| MINOR-02 | MINOR | Step 5 | coverage 停在 `missing_data`（无三维几何 provider、无既有设施）。这是**真实数据链未配置**而非缺陷，但会让首次人工验收在 Step 5 看到"未计算"，建议验收前先导入既有 CNS 设施或明确接受该状态。 |
| UX-01 | UX | Step 2 | Population NoData policy 为 `not_configured`，界面会显示未配置；真实数据侧需人工确认语义后才能推进 population 相关判定。 |
| OBS-01 | — | Step 3 | 本轮 fixture 的运行航路来自 release-chain fixture（`provenance.source_type=layered_candidate_operational_adoption_v1`），**未在隔离项目内重新跑一遍真实 Theta\* V2 搜索**。真实数据侧的 Constraint Field 仍为 `3161 unknown / 0 pass`（fail-closed），需人工补齐 terrain/tower/受限区事实后重跑。 |

## 5. Assumptions / unknown / not evaluated（如实记录）

**Active assumptions（验收项目内）**

| assumption_id | field | value | basis | authority_effect |
|---|---|---|---|---|
| `ASM-CNS-EMPTY` | `cns_existing_baseline` | `empty` | `engineering_baseline` | `allowed_with_disclosure` |
| `ASM-PHASE4-RC-PUBLISH-FIXTURE` | `route_publication_evidence` | `synthetic_schema_matched_fixture` | 验收 fixture | 披露 |

披露文本（必须进入报告）：**空既有设施工程规划基线；不表示现实中不存在 CNS 设施。**

**unknown / not evaluated**

- `planning_constraint_fields` = `not_calculated`（真实数据链未配置）。
- `population_nodata_policy` = `not_configured`。
- `coverage_3d` = `missing_data`（无可用三维几何 provider）。
- `radar_surveillance_layout` readiness = `not_ready`（OPTIONAL 支线，不阻塞）。
- `cns_planning_objectives` = `objectives_not_configured`（因此 confirm 需要显式 `confirm_without_objectives` + reason，系统如实要求）。

## 6. Save / Reopen / Restart 结果

| 场景 | 结果 |
|---|---|
| Step3 发布后 Save/Open | PASS（route/adoption/risk profile/validation 保持） |
| Step5 heavy task 后 Save/Open | PASS（P14/P15/P16 与 assumption 保持） |
| Step6 报告后 Save/Open | PASS（`plan_id`、`active_report_id`、artifact ref 均可解析） |
| **关闭并重启服务后重新打开项目** | **PASS** —— `plan_id=CP-b53ffe8d814a2733115a`、`application.status=applied`、`active_report_id=RPT-c10f960bc5b9e6232bfbda7b`、`baseline=assume_empty_for_planning`、operational route 1 条、P16 `no_action_required`、P18 `applied`、`report.html` 431 746 B 可读、后台任务状态恢复为 `succeeded` |

重启后另验证：乐观锁冲突返回 `409` + body 携带权威 `revision` 与中文提示（"项目状态已更新（请求 N，当前 M），请刷新后重试"），刷新 revision 后重试成功——该行为与前端"刷新后重试"路径一致。

## 7. Backend / Frontend 状态

**后端**

- 全量 pytest：见第 8 节（本轮最后一次执行结果）。
- 定点回归（本轮修复相关）：`test_cns_existing_baseline.py`(7) + `test_plan_review.py`(12) + `test_corridor_site_planner_v2.py` + `test_cns_corridor_gap.py` + `test_cns_corridor.py` + `test_phase4_b6r_immutable_task_input.py`(30) + `test_planning_report.py` = **104 passed**。
- `git diff --check`：见第 8 节。

**前端**

- 逐个运行全部 `tests/*.test.mjs` + `tests/grid_theme.test.js`：**0 failed**（明细见第 8 节）。
- `node --check` 覆盖本轮改动的 `step05_cns.js`（含其依赖模块经前端测试加载验证）。

**本轮前端改动**（`cns_planner/web/js/workflow/step05_cns.js`）：Existing CNS baseline 面板改为读取 `flow.cns_existing_baseline`（事实状态 + 规划模式 + 声明来源 + 显式确认），并按契约向 `/api/cns-existing-baseline` 提交声明与工程假设；无声明时回落到既有 `existing_cns_facilities` 展示，保持向后兼容。

## 8. 测试与静态检查

| 项目 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `D:\tools\anaconda\python.exe -m pytest -q -p no:cacheprovider` | **1686 passed, 7 skipped, 0 failed**（793.5s） |
| 跳过项 | —— | 7 项均为 `tests/test_map_http.py` 的真实 QGIS HTTP 集成（默认跳过，需 `CNS_MAP_TESTS=1`）；本轮已用真实 8765 服务以 HTTP 验收替代覆盖 |
| 定点回归 | 见第 7 节命令 | **104 passed** |
| 前端全量 | 逐个 `node tests/*.test.mjs` + `node tests/grid_theme.test.js` | **0 failed**（27 个 `.test.mjs` 合计 484 项 + `grid_theme` 3 项） |
| JS 语法 | `node --check` `app.js` / `js/main.js` / `js/workflow/step05_cns.js` | exit 0 |
| Python 语法 | `python -m compileall -q cns_planner tests tools` | exit 0 |
| 空白/冲突标记 | `git diff --check` | exit 0（无输出） |

## 9. 删除的本地文件（A 类：确认一次性/临时产物）

删除前先做 inventory（`git status --short`），逐项判定；**未使用 `git clean -fd` / `-fdx`**。

| 路径 | 类型 | 大小 | 判定依据 |
|---|---|---|---|
| `_b9_baseline/` | 目录 | ≈9.5 MB | 完整旧源码副本，未被源码/测试/README 引用 |
| `_dsh_prof/` | 目录 | ≈962 MB | DSH 分析/前端 tap 临时目录 |
| `b9r2q_prof/` | 目录 | ≈85 MB | 性能 profiling 一次性产物 |
| `b9r2q_runs/` | 目录 | ≈161 MB | benchmark 运行残留 |
| `b9r_state_runs/` | 目录 | ≈383 MB | benchmark 运行残留 |
| `qgis_polygon_inventory.json` | 文件 | 51 KB | `PHASE4_ARCHITECTURE_AUDIT.md:50,1155` 明确列为待清理临时产物 |
| `ersyidingDocumentsChatGPTCNS规划系统` | 文件 | 13 630 B | 乱码错误路径产生的无效文件；`PHASE4_ARCHITECTURE_AUDIT.md:50,1155` 同样列为待清理 |
| `tools/alt080_recon.py` / `alt080_recon2.py` / `alt080_recon3.py` | 文件 | ≈12 KB | 一次性排查脚本，无源码/测试/文档引用 |
| `tools/check_coord_defect.py` | 文件 | 1.6 KB | 一次性坐标缺陷排查 |
| `tools/et_probe.py` / `et_probe2.py` | 文件 | ≈4 KB | 一次性 `.et` 探测 |
| `tools/proj_probe.py` / `proj_sites.py` | 文件 | ≈2 KB | 一次性投影/站点探测 |
| `tools/radar_layout_mayi_dongbailian.py` | 文件 | 21 KB | 单点雷达布设一次性脚本，无引用 |
| `tools/xlsx_dump.py` | 文件 | 0.7 KB | 一次性表格 dump |

合计释放约 **1.7 GB**。删除后复查 `git status --short`，已跟踪文件与正式数据**未受影响**。

## 10. 保留的未跟踪文件及原因（B 类：人工确认后删除）

以下文件**未被自动删除**，因为它们属"仍可能具备数据恢复/复核价值"或为本轮证据：

| 路径 | 保留原因 |
|---|---|
| `docs/舟山起降场核对与80m障碍检查报告.md` | 有实质结论的舟山起降场/80 m 障碍审计报告（`.et` 解析链路唯一记录） |
| `tools/check_building_grid_sqlite.py`、`check_building_heights.py`、`check_building_heights_sqlite.py`、`check_src_coord_with_project_parser.py`、`compare_zhoushan_sites.py`、`compare_zhoushan_sites_v2.py`、`et_dump.py`、`et_to_xlsx.py`、`verify_missing_neighbours.py` | 均被上述舟山报告引用；`.et` 是 WPS 专有格式，`et_dump.py`/`et_to_xlsx.py` 是**唯一可用的转换来源**，删除会丢失数据恢复能力 |
| `tools/alt080_figure.py` | 31 KB 图表工具，可能用于该报告的图件复现，无引用但保守保留 |
| `docs/15-Phase4-Final-Preacceptance.md` | 本报告（本轮交付物） |

处理建议：先决定 `docs/舟山起降场核对与80m障碍检查报告.md` 的去留（正式归档进 `docs/` 并随之保留其 tools，或整体移出仓库），再一并清理，避免留下无法复现的报告。

## 11. README 修改摘要

`README.md` 已按当前 HEAD 实码重写（中文优先，**不再用 P 编号解释用户主流程**，算法版本只出现在技术章节）：

1. 当前定位与冻结基线（tag/HEAD、Phase 4 RC 定位、明确"只有一套 production 工作流"）。
2. 启动方法（`map_app.py`、QGIS Python、8765、日志、Job Object 与健康身份校验）。
3. canonical 六步工作流表（数据准备 / 环境与风险 / 航路规划与发布 / CNS需求 / CNS能力与设施规划 / 方案评审与报告）。
4. 航路正式链（Risk Field → Planning Constraint Field → `LayeredRiskAwareThetaStarV2` → `RouteRiskProfile` → 独立连续验证 → Operational Adoption → Operational Route）与约束。
5. 高度层：数据驱动、EGM2008 正高、`ALT-060/080/100` 仅为目录实例、不写死 80 m、起飞/爬升/下降与固定巡航层分离。
6. Constraint Field：`terrain/building/tower/airspace/critical_site`、`pass/blocked/unknown`、fail-closed、逐 cell 证据外置。
7. CNS 正式链：Required CNS → Coverage3D → Service Capability → Corridor → Gap → Site Planning → Review/Report。
8. Existing CNS 基线与工程假设（`not_declared`/`confirmed_none`/`confirmed_present` × `factual`/`assume_empty_for_planning` 及守卫规则）。
9. Radar：Step 5 OPTIONAL 支线、proposal-only、必要时才 REQUIRED。
10. ProjectState + artifact/sidecar + 失效语义 + 原子持久化。
11. Heavy Task：持久、progress、cancel、immutable snapshot、性能准入（hard ceiling 不可 override）。
12. 已验证性能包线：仅写当前正式验证范围（`3e6` 已验证 / `1e7` 安全天花板），不宣称无限规模。
13. Compatibility / Research：legacy 只读兼容、`RoutePlannerV3` research/history、不属于 production。
14. 当前算法注册表（10 类 production 选型，逐条 id@version）。
15. 架构树按当前实际模块重写。
16. 测试与人工验收入口（固定解释器命令、前端命令、六步验收路径）。

## 12. 人工验收时重点观察项

1. **Step 5 的真实数据前置**：`coverage_3d` 当前停在 `missing_data`（无三维几何 provider）。人工验收应先在 Step 5 导入既有 CNS 设施（或确认接受该状态），否则后续 corridor/capability 只能得到"能力不足/未计算"的结论。
2. **Planning Constraint Field 的真实性**：真实舟山数据下 3161 格仍为 `unknown`（fail-closed）。需补齐 terrain/tower/受限区/要地事实并显式确认后重跑，才能看到 `pass/blocked` 的真实分布。
3. **高度层非 080 路径**：建议用 `ALT-060` 或自定义合法层完整跑一次 Step 3，确认目录驱动、垂向基准与约束场 identity 一致。
4. **Existing CNS baseline 的三个事实状态**：分别在 `not_declared`+`assume_empty_for_planning`、`confirmed_none`、`confirmed_present`（带真实设施）下各走一次 Step 5，确认报告披露与 readiness 状态符合预期。
5. **异步 heavy task**：在**切换项目之后**提交一次服务走廊任务，确认 `202 → progress/heartbeat → succeeded → 结果发布`（这是本轮修复 BUG-03 的直接回归面）。
6. **报告与已确认方案的一致性**：确认报告 `source_plan_id` 始终等于当前 `confirmed_cns_plan.plan_id`，且 Apply 后旧报告被标记 stale。
7. **重启恢复**：完全关闭并重启服务后重开项目，确认 `plan_id`/`active_report_id`/假设/artifact 引用都不丢失。

## 13. 复现方式

```powershell
# 1) 启动真实后端（QGIS Python）
$env:QT_QPA_PLATFORM='offscreen'; $env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
$env:CNS_LAUNCHER_GIT_COMMIT=(git rev-parse HEAD)
& "C:\Program Files\QGIS 3.44.14\bin\python-qgis.bat" -m cns_planner.map_server

# 2) 六步真实 HTTP 验收（脚本位于工作区外，不污染仓库）
D:\tools\anaconda\python.exe tools\phase4_rc_acceptance.py `
  --project-dir C:\Users\yiding\Documents\ChatGPT\CNS_VALIDATION\phase4_final_smoke\rc_acceptance_run

# 3) 服务重启后复验
D:\tools\anaconda\python.exe tools\phase4_rc_restart_check.py

# 4) 定向回归
D:\tools\anaconda\python.exe -m pytest tests/test_plan_review.py tests/test_cns_existing_baseline.py `
  tests/test_phase4_b6r_immutable_task_input.py -q -p no:cacheprovider

# 5) 全量后端与前端
D:\tools\anaconda\python.exe -m pytest -q -p no:cacheprovider
node tests/frontend_modules.test.mjs
```

## 14. 最终测试输出

```text
后端全量：1686 passed, 7 skipped in 793.53s (0:13:13)
前端全量：27 个 .test.mjs（484 项）+ grid_theme（3 项）= 487 passed, 0 failed
定点回归：104 passed
git diff --check：通过（无输出）
node --check（app.js / main.js / step05_cns.js）：通过
python -m compileall cns_planner tests tools：通过
真实 HTTP 六步验收：checks=69 failed=0（elapsed 82.3s）
服务重启后复验：RESTART_REOPEN_RESULT=PASS
```

## 15. 最终报告交付摘要（对应任务第十一节）

| 项 | 值 |
|---|---|
| 冻结基线 | `phase4-final-freeze` |
| HEAD（验收当时） | `30281644731602bf967ee44d866da470ecb4f2b0` |
| smoke 项目 | `C:\Users\yiding\Documents\ChatGPT\CNS_VALIDATION\phase4_final_smoke\rc_acceptance_run` |
| 六步结果 | Step1 PASS / Step2 PASS_WITH_WARNING / Step3 PASS / Step4 PASS / Step5 PASS_WITH_WARNING / Step6 PASS |
| 是否真正走到最终报告 | **是**（`report status=passed`，`report.html` 431 746 B、`report.pdf` 186 029 B、`planning-package.zip` 250 638 B 均可读） |
| 发现 BUG 总数 / 分级 | 3 个高置信缺陷：BLOCKER 1（BUG-02，另 BUG-03 具 BLOCKER 影响面）+ MAJOR 2（BUG-01、BUG-03）；另记录 MINOR 2 / UX 1 / OBS 1（未修） |
| 已修 BLOCKER | BUG-02（P18 门禁死锁） |
| 已修 MAJOR | BUG-01（baseline readiness 守卫）、BUG-03（任务服务随项目切换重新绑定 workflow） |
| 待人工观察 Minor/UX | MINOR-01（`/api/state.project_dir` 为 null）、MINOR-02/OBS-01（coverage 与 constraint field 的真实数据前置）、UX-01（Population NoData 未配置） |
| save / reopen / restart | 全部 PASS（含服务关闭重启后读取最终结果） |
| README 修改 | 整篇按 HEAD 实码重写（16 节，中文优先，不再用 P 编号解释主流程） |
| 删除文件清单 | 5 个目录 + 12 个文件（约 1.7 GB），明细见第 9 节 |
| 保留未跟踪文件及原因 | 见第 10 节（舟山数据工作线 + 本报告） |
| 定向测试 | 104 passed |
| full pytest | 1686 passed, 7 skipped, 0 failed |
| frontend 测试 | 487 passed, 0 failed |
| git diff --check | 通过 |
| `PHASE4_PREACCEPTANCE_READY` | **true** |
| 人工验收前剩余 blocker | **无**；剩余为数据前置（Step 5 三维几何 provider / 既有 CNS 设施）与 Step 2 真实 constraint evidence |

## 16. 冻结基线之上的预验收稳定化修订（Phase4-PREACCEPTANCE-R1）

> 范围：**只收紧 P16 stale → P18 review 门禁 + 文档对齐**。不重构、不改算法、不开新功能、不 commit / push / 打 tag。

### 16.1 为什么撤销"接受 stale P16"

BUG-02 一轮曾让 `PlanReviewService._require_current_inputs` 在 `cns_corridor_site_plan.status == "stale"` 时直接放行。这一放行不安全：

- `candidate_sites` 与 `corridor_site_planning_policy`（DEPENDENTS 中 `candidate_sites` / `site_planner` / `corridor_site_planning_policy` 均只指向 `cns_corridor_site_plan`）的变化**只**让 P16 变 stale，P14/P15 仍是 current；
- 此时放行，P18 会读取**旧** P16 的 `candidate_actions` / `selected_actions` 去初始化正式评审，可能基于已经变化的候选站或设备。

### 16.2 修改

| 文件 | 修改 |
|---|---|
| `cns_planner/application/plan_review_service.py` | 删除 `proposal_status == "stale" → return`；恢复 fail-closed：只有 `proposal_ready` / `no_action_required` / `no_eligible_proposal` / `evidence_required` 可进入 P18，其余（含 `stale` / `missing_data` / `not_calculated`）抛 `P18 需要 current P16 proposal/status`。P14/P15 判定未改。 |
| `cns_planner/application/corridor_service.py`、`corridor_gap_service.py` | **保留** `conclusion_changed(previous, current)`：同一 `input_fingerprint` 的 P14/P15 重算仍不得让下游 stale。 |
| `README.md` | 冻结基线不再写死 `HEAD <sha>`；改为"冻结基线 tag `phase4-final-freeze` + 当前 main 为冻结基线之上的预验收稳定化修复"。 |
| `tests/test_plan_review.py` | 新增 A–E 回归（见 16.3），删除旧的"stale P16 仍可初始化"断言。 |

### 16.3 回归测试 A–E

| 用例 | 场景 | 断言 |
|---|---|---|
| A | 完整 current P14/P15/P16 → 重算相同 P14 | P15/P16 input_fingerprint 不变且非 stale；P18 initialize PASS |
| B | 完整 current P14/P15/P16 → 重算相同 P15 | P16 保持 current；P18 initialize PASS |
| C | 完整 current P14/P15/P16 → 导入新的 `candidate_sites` | 只有 P16 变 stale（旧 `candidate_actions` 仍在 state 中）；P14/P15 仍 current；P18 initialize **拒绝** |
| D | site planning policy / device catalog 变化 | P16 stale；P18 initialize **拒绝**（device 同时 stale P14，因此按上游门禁拒绝） |
| E | C 之后重新 evaluate P16 → `proposal_ready` | P18 initialize PASS |

补充：`stale` / `missing_data` / `not_calculated` 三种 P16 状态均被拒绝；P14 真 stale 仍按上游门禁 fail-closed。

**重点结论**："无变化重算"不再制造 stale（A/B PASS），但"真正 P16 输入变化"仍 fail-closed（C/D 拒绝、E 恢复）。

### 16.4 本轮验证

```text
HEAD：f373fac45bd534df7a70594611e7b433a7960618（未 commit、未 push）
定向：pytest -q tests/test_plan_review.py tests/test_corridor_site_planner_v2.py tests/test_cns_corridor_gap.py
      → 47 passed
full：pytest -q -p no:cacheprovider  → 1691 passed, 7 skipped
git diff --check → 通过（无空白错误）
PREACCEPTANCE_R1_READY=true
```

说明：full 首跑曾出现 1 项与本次改动无关的偶发失败
（`tests/test_phase4_b6r_immutable_task_input.py::test_restart_can_read_snapshot_and_run_queued_task`
——真实 worker 子进程时序导致的 `task_input_changed`）。该文件单独重复运行 3 次均 30 passed，
且该文件在字母序上先于本轮改动的 `tests/test_plan_review.py` 执行；全套重跑 1691 passed 未复现。

