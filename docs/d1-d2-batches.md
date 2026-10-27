# D1 → D2 主题化提交批次与 D3 干净检出复现说明（2026-10-27）

> 本文件只**准备**批次与步骤，不创建提交、不修改远端。
> 候选：`_work/d1/rc`；来源：`_work/c3base/integration @89b008d`。
> 机器清单：`docs/d1-candidate-inventory.json`（**100 项**；除清单自身的自引用 SHA 外，逐文件含 SHA-256）。
> 文件总数：**100** = tracked 修改 33 + untracked 59（含治理/文档 12）+ vendor 8。
> 其中 D1 为 99（tracked 32 + untracked 59 + vendor 8）；D2 新增 1 项
> `cns_planner/domain/cns_inputs.py`（正式路径禁止凭 version 启用研究 N v2 的权限修复）。
> 99 与文档旧称 98 的差异已定位：P0–P8 旧表把 `.gitignore` 写在 P8 说明里但未计为条目；
> 补入后 98 + `.gitignore` = 99，D2 再加 `cns_inputs.py` = **100**。

---

## 1. 批次原则

1. **治理先行**：P0 先入裁定载体，后续批次才能引用 decision_id。
2. **一批一主题**：每批可独立回滚、独立验证；不混入旧工作区无关改动。
3. **测试跟随功能**：P6 单列便于审查，但允许与 P1–P5 合并为同一 PR；不允许测试长期落后于代码。
4. **vendor 独立**：Cesium 入 Git 单独一批（P8），便于按许可/体积回滚。
5. **不夹带**：`_work/`、`audit_sandboxes/`、`research/`、`outputs/`、`projects/` 一律不入。

---

## 2. 批次总览

| 批次 | 主题 | 文件数 | 依赖 | 验收 |
|---|---|---:|---|---|
| P0 | 治理文档与决策台账 | 12 | 无 | `docs/` 路径检查、台账可追溯 |
| P1 | 研究运行时后端（API/任务/输入/报告） | 12 | P0 | 研究 API 合同、任务 spec |
| P2 | NM3 / N 合同 / CNS 服务合同 / 权限门禁 | 20 | P1 | NM3 回归、N v2 回归、2 km 合同、正式路径拒绝 v2 |
| P3 | 研究二维前端 | 6 | P1 | 前端合同测试、`node --check` |
| P4 | 研究三维 + EGM2008 | 6 | P1/P2 | 3D 投影测试、EGM fail-closed |
| P5 | 图件模板修复与研究图件工具 | 5 | P2 | 图例 2 km/RID 披露、六图重渲染 |
| P6 | 测试与 fixture | 28 | P1–P5 | pytest / node 全绿（已知基线失败单列） |
| P7 | 研究复现工具 | 2 | P1 | dispatch/serve 可启动 |
| P8 | Cesium vendor + `.gitignore` | 9 | 无 | 许可校验、三维页可加载、仅 8 文件 |
| **合计** | | **100** | | 无重复、无遗漏 |

---

## 3. 精确文件清单

### P0 治理文档与决策台账（12）

| # | 文件 | 动作 |
|---:|---|---|
| 1 | `AGENTS.md` | 新增 |
| 2 | `PROTECTED_ASSETS.md` | 新增（重写） |
| 3 | `CLEANUP_PLAN.md` | 新增 |
| 4 | `docs/20-历史裁定与未集成成果梳理-2026-10-27.md` | 新增（校准） |
| 5 | `docs/21-首版研究软件发布基线与治理收口-2026-10-27.md` | 新增 |
| 6 | `docs/22-发布依赖环境与许可证MANIFEST-2026-10-27.md` | 新增 |
| 7 | `docs/d1-git-inventory.md` | 新增 |
| 8 | `docs/d1-git-inventory.json` | 新增 |
| 9 | `docs/d1-candidate-inventory.json` | 新增 |
| 10 | `docs/d1-v1-v4-acceptance.md` | 新增 |
| 11 | `docs/d1-d2-batches.md` | 新增 |
| 12 | `docs/decisions/2026-09-ai-dev-context-full-archive.md` | 新增 |

验证：`rg -n "D-01[3-6]" PROTECTED_ASSETS.md`；`git diff --check`；文档中引用的路径存在（`docs/20–22`、`decisions`）。

### P1 研究运行时后端（12）

| # | 文件 | 来源 |
|---:|---|---|
| 1 | `cns_planner/application/research_run_service.py` | untracked |
| 2 | `cns_planner/application/research_compute_service.py` | untracked |
| 3 | `cns_planner/application/research_input_service.py` | untracked |
| 4 | `cns_planner/application/research_map_service.py` | untracked |
| 5 | `cns_planner/application/research_parameters.py` | untracked |
| 6 | `cns_planner/application/research_report_service.py` | untracked |
| 7 | `cns_planner/application/navigation_integrity_research_service.py` | untracked |
| 8 | `cns_planner/api/router.py` | tracked `MM` |
| 9 | `cns_planner/api/server.py` | tracked `MM` |
| 10 | `cns_planner/tasks/handlers.py` | tracked `MM` |
| 11 | `cns_planner/tasks/service.py` | tracked `M ` |
| 12 | `cns_planner/tasks/task_specs.py` | tracked `MM` |

验证：`pytest tests/test_c3a_research_run.py tests/test_c3b_research_ui.py -q`；`python -m py_compile` 新增模块。

> `router.py`/`server.py` 的 `MM` 表示暂存后又有工作树改动；D2 提交前必须
> `git add` 当前工作树内容，不能只提交旧索引。

### P2 NM3 / N 合同 / CNS 服务合同 / 权限门禁（20）

| # | 文件 | 来源 |
|---:|---|---|
| 1 | `cns_planner/domain/navigation_integrity_contract_v2.py` | untracked |
| 2 | `cns_planner/domain/navigation_middle_reuse.py` | untracked |
| 3 | `cns_planner/domain/navigation_integrity_monitoring.py` | tracked ` M` |
| 4 | `cns_planner/site_planner/navigation_middle_reuse_v1.py` | untracked |
| 5 | `cns_planner/application/navigation_planning_integration.py` | untracked |
| 6 | `cns_planner/application/corridor_site_planning_service.py` | tracked `M ` |
| 7 | `cns_planner/application/corridor_service.py` | tracked `M ` |
| 8 | `cns_planner/application/cns_input_service.py` | tracked `M ` |
| 9 | `cns_planner/application/invalidation_service.py` | tracked `M ` |
| 10 | `cns_planner/application/plan_projection.py` | tracked ` M` |
| 11 | `cns_planner/application/plan_review_service.py` | tracked `M ` |
| 12 | `cns_planner/application/project_state.py` | tracked `M ` |
| 13 | `cns_planner/algorithms/corridor/v1.py` | tracked `M `（P14 保护） |
| 14 | `cns_planner/algorithms/registry.py` | tracked `M ` |
| 15 | `cns_planner/domain/cns_service_contract.py` | tracked `M ` |
| 16 | `cns_planner/domain/cns_service_registry.py` | tracked ` M` |
| 17 | `config/navigation_integrity/endpoint_middle_reuse_v1.json` | untracked |
| 18 | `config/navigation_integrity/planning.example.json` | untracked |
| 19 | `config/navigation_integrity/selection.schema.json` | untracked |
| 20 | `cns_planner/domain/cns_inputs.py` | tracked ` M`（D2 权限修复：`normalize_required_cns(..., allow_research_v2=False)`） |

验证：`pytest tests/test_navigation_middle_reuse_v1.py tests/test_navigation_planning_integration.py tests/test_round40a_navigation_integrity_v2.py -q`；
P14 保护哈希 3 项；`git diff --check`。负向验收：正式 `normalize_required_cns` 只给
`planning.version=..._v2` 必须 `ValueError`；研究调用点显式 `allow_research_v2=True` 才可通过。

### P3 研究二维前端（6）

| # | 文件 |
|---:|---|
| 1 | `cns_planner/web/research.html` |
| 2 | `cns_planner/web/research.css` |
| 3 | `cns_planner/web/js/research_app.js` |
| 4 | `cns_planner/web/js/research_map.js` |
| 5 | `cns_planner/web/js/research_map_renderer.js` |
| 6 | `cns_planner/web/js/research_runs.js` |

验证：`node --check` 各 JS；`node tests/c3b_research_ui.test.mjs`；`node tests/c3c_research_map_labels.test.mjs`。

### P4 研究三维 + EGM2008（6）

| # | 文件 |
|---:|---|
| 1 | `cns_planner/gis/egm2008_geoid.py` |
| 2 | `cns_planner/application/research_visualization_service.py` |
| 3 | `cns_planner/web/research_3d.html` |
| 4 | `cns_planner/web/research3d.css` |
| 5 | `cns_planner/web/js/research_3d.js` |
| 6 | `cns_planner/web/js/visualization3d/basemap.js` |

验证：`pytest tests/test_c3c_research_visualization.py -q`；`node --check cns_planner/web/js/research_3d.js`；
EGM 缺失时 `Visualization3DUnavailable` fail-closed。

### P5 图件模板修复与研究图件工具（5）

| # | 文件 | 动作 |
|---:|---|---|
| 1 | `cns_planner/reporting/map_templates/template_catalog.py` | 4 km → 2 km；RID 海 5 km |
| 2 | `cns_planner/gis/qgis_figure_renderer.py` | D1 新增修改：图例 2 km/海 5 km |
| 3 | `cns_planner/application/map_figure_service.py` | D1 新增修改：披露 2 km；RID 输入半径≠保障结果 |
| 4 | `tools/render_r0005_research_figures.py` | D1 修改：RID 披露陆/岸 2 km、海 5 km |
| 5 | `tools/build_r0005_terrain_grid.py` | 三维显示地形构建 |

验证：`pytest tests/test_c3c_research_figures.py tests/test_round40a_navigation_integrity_v2.py -q`；
QGIS 重渲染两 run 各 6 张，检查图例文本与 SHA。

### P6 测试与 fixture（28）

| 类别 | 文件 |
|---|---|
| tracked（11） | `tests/p14_protected_sha256.json`、`test_algorithm_registry.py`、`test_cns_communication_rid_services.py`、`test_p16_prefilter_and_prepared_cells.py`、`test_phase4_b6r_immutable_task_input.py`、`test_phase4_b6x_heavy_task_runtime.py`、`test_phase4_b9r1_async_admission.py`、`test_required_cns_adoption_services.py`、`test_round2_surface_wiring.py`、`test_map_figures_cns_templates.py`、`test_map_figures_cartographic_polish.py`（后两者为 D1 图例口径更新） |
| 研究 Python（9） | `test_c2b_middle_projection.py`、`test_c3a_research_run.py`、`test_c3b_research_ui.py`、`test_c3c_research_figures.py`、`test_c3c_research_report.py`、`test_c3c_research_visualization.py`、`test_navigation_middle_reuse_v1.py`、`test_navigation_planning_integration.py`、`test_round40a_navigation_integrity_v2.py` |
| 前端（3） | `c3a_research_run.test.mjs`、`c3b_research_ui.test.mjs`、`c3c_research_map_labels.test.mjs` |
| fixture（5） | `tests/fixtures/navigation_middle_reuse/{endpoint_context,endpoint_input,middle_bindings,sites,stations}.json` |

验证：`pytest tests/ -q -x --tb=short -p no:cacheprovider`（仅受影响集合；不跑全量历史套件）；
`node tests/c3a_research_run.test.mjs && node tests/c3b_research_ui.test.mjs && node tests/c3c_research_map_labels.test.mjs`。

### P7 研究复现工具（2）

| # | 文件 |
|---:|---|
| 1 | `tools/build_r0005_research_dispatch.py` |
| 2 | `tools/serve_r0005_research.py` |

验证：`python tools/serve_r0005_research.py --help`；`python tools/build_r0005_research_dispatch.py --help`。

### P8 Cesium vendor + `.gitignore`（9）

| # | 文件 |
|---:|---|
| 1 | `cns_planner/web/vendor/cesium/Cesium.js` |
| 2 | `cns_planner/web/vendor/cesium/LICENSE.md` |
| 3 | `cns_planner/web/vendor/cesium/LICENSE-PROVENANCE.md` |
| 4 | `cns_planner/web/vendor/cesium/Widgets/widgets.css` |
| 5 | `cns_planner/web/vendor/cesium/Assets/approximateTerrainHeights.json` |
| 6 | `cns_planner/web/vendor/cesium/Assets/Images/ion-credit.png` |
| 7 | `cns_planner/web/vendor/cesium/Assets/IAU2006_XYS/IAU2006_XYS_0.json` |
| 8 | `cns_planner/web/vendor/cesium/Assets/IAU2006_XYS/IAU2006_XYS_18.json` |
| 9 | `.gitignore`（D2 修改：`vendor/*` + `!vendor/cesium/`，仅放行该目录） |

验证：`git check-ignore -v` 对 8 文件均不命中、对 `vendor/other/*` 仍命中；
Cesium LICENSE LF 规范化 `3111d10e…`、Cesium.js LF `b796f8a6…`、widgets.css LF `702c5adf…`；
PNG 用原始 SHA `721870c1…`；`find vendor/cesium -type f` 恰好 8 个文件（+ 无临时资产）。
完整 SHA 表见 `docs/22` §3.1。

> D2 裁定：vendor 入 Git（B5 已解除）。若未来改为外置，登记 SHA 与获取说明，且三维页
> 必须显式提示“vendor 未提供”，不得静默 404。

---

## 4. D2 提交分组候选（5 个主题化提交，源码与相关测试同行）

原则：按依赖顺序 P0 → 运行时/合同 → 二维 → 三维/vendor → 图件；每组合并可独立回滚；
不机械按 P0–P8 做 9 次独立回归，只在每组提交前跑该组受影响测试。

| 提交候选 | 主题 | 包含 | 文件数 | 建议标题 |
|---|---|---|---:|---|
| **C1** | 治理与决策台账 | P0 全部（含 `PROTECTED_ASSETS.md` D-013/14 修订、`docs/21`/`22`、本文件） | 12 | `docs: add release-baseline governance and decision ledger` |
| **C2** | 研究运行时 + NM3/N/CNS 合同 + 权限门禁 + 复现工具 | P1 12 + P2 20 + P7 2；P6 中后端/合同测试（9 tracked + `test_c2b`/`c3a`/`c3b`/`c3c_report`/`navigation_middle`/`navigation_planning`/`round40a` + 5 fixtures） | 55 | `feat(research): add isolated runtime, NM3 contracts and N v2 permission gate` |
| **C3** | 研究二维前端 | P3 6 + P6 前端测试 3（`c3a_research_run`/`c3b_research_ui`/`c3c_research_map_labels` .test.mjs） | 9 | `feat(research): add 2D research UI` |
| **C4** | 研究三维 + EGM2008 + Cesium vendor | P4 6 + `test_c3c_research_visualization.py` + P8 9（含 `.gitignore`） | 16 | `feat(research): add 3D projection with EGM2008 and CesiumJS 1.126` |
| **C5** | 图件模板修复与研究图件工具 | P5 5 + `test_c3c_research_figures.py` + `test_map_figures_cns_templates.py` + `test_map_figures_cartographic_polish.py` | 8 | `fix(figures): align communication 2km and RID surface disclosures` |
| **合计** | | | **100** | |

映射核对：12 + 55 + 9 + 16 + 8 = 100；P6 的 28 项已全部落位（C2 21、C3 3、C4 1、C5 3），
没有测试被单独留到最后；vendor 随三维提交，回滚 C4 即同时回滚三维页与 vendor。

> 提交顺序固定 C1 → C2 → C3 → C4 → C5。C2 依赖 C1 的 decision_id（D-017/D-018、D-015/D-016）；
> C3/C4 依赖 C2 的 API 与合同；C5 只依赖 C2 的 2 km/RID 合同常量。

---

## 5. D2 最终纳入清单核对（100 项）

| 检查 | 结果 |
|---|---|
| 清单唯一性 | 100 个路径全部唯一；0 重复 |
| 与实际候选一致 | 33 tracked 修改 + 59 untracked（12 治理/文档 + 47 研究源码/测试/配置/工具）+ 8 vendor = 100；`git status -uall` 与清单逐项比对无缺、无多 |
| 99 vs 98 | 旧 P0–P8 表合计 98，因为 `.gitignore` 只写在 P8 说明里；补入后 99。D2 新增 `cns_inputs.py`（权限修复）后 100 |
| 路径修正 | P8 `License/LICENSE.md` 幻影路径改为实际 `LICENSE.md`；新增 `.gitignore` 条目 |
| 不允许夹带 | 排除 `_work/`、`projects/`、`outputs/`、`audit_sandboxes/`、`research/`、`rounds/`、`_cleanup_audit/`、`_cscan/`、大型数据、凭据、历史副本；`cns_planner/**` 与 `tools/**` 无 `_work/` 硬编码运行时路径（仅工具 docstring 示例，D3 以 `--run-root` 覆盖） |

## 6. D2 执行前检查（结果）

1. `git status --porcelain -uall` 与 `docs/d1-candidate-inventory.json` 一致；无未登记文件 → **通过（100/100）**。
2. `git diff --check` 无输出 → **通过**。
3. `git diff HEAD --stat`：33 tracked（D1 32 + D2 `cns_inputs.py`）；D1 文本修改未改算法/指纹 → **通过**。
4. D1 四个图件修改文件的 diff 仅限文本/披露 → **通过**。
5. `docs/22` §4 的 SHA 已按 2026-10-27 实测重算：FABDEM/EGM2008/land_mask/cartographic/铁塔/建筑/建筑网格全部一致 → **通过**。
6. vendor 已裁定入 Git；8 文件 + LICENSE + PROVENANCE 逐项 SHA 已核；`.gitignore` 仅放行 `vendor/cesium/` → **通过（B5 解除）**。
7. `research_isolated` 权限负向验收：正式 `normalize_required_cns` 对仅含 `version=..._v2` 的输入抛 `ValueError`；研究调用点显式 `allow_research_v2=True` 后通过 → **通过**。
8. Cesium 目录恰好 8 个文件；无其他 vendor 或临时资产 → **通过**。

> 已知基线失败（非 D2 引入，单列不掩盖）：`tests/test_round2_surface_wiring.py::test_runtime_surface_paths_never_reference_radar_policy`
> 在 `origin/main@89b008d` 即失败（测试要求 `app_context.py` 的 `_land_policy`，基线无该本地改动）；
> D2 不修改该非研究文件，运行该文件其余 106 项通过。

---

## 7. D3 干净检出复现（未执行）

### 7.1 目标

在**不含 `_work/`** 的干净目录中，仅凭仓库 + 外部数据 SHA 校验，启动研究软件并复现 V1–V4。

### 7.2 步骤

```powershell
# 0) 前提：D2 已产生 C1–C5 提交（本轮不产生）
$CAND = "D:\Projects\CNS-PLANNER-clean"
git clone --no-local D:\Projects\CNS-PLANNER\_work\d1\rc $CAND   # D3 以 C1–C5 提交后的候选为准
cd $CAND
git status --porcelain -uall        # 期望空

# 1) 环境
D:\tools\anaconda\python.exe --version     # 3.13.9
& 'C:\Program Files\QGIS 3.44.14\bin\qgis-ltr-bin.exe' --version  # 3.44.14
node --version                              # v24.15.0
D:\tools\anaconda\python.exe -c "import pyproj;print(pyproj.proj_version_str)"  # 9.8.1

# 2) 外部数据 SHA（逐项对照 docs/22 §4；2026-10-27 已实测一致）
Get-FileHash <FABDEM> -Algorithm SHA256        # 21406bd9…
Get-FileHash <EGM2008> -Algorithm SHA256       # 4191d471…
Get-FileHash <land_mask> -Algorithm SHA256     # fe7918c9…
Get-FileHash <cartographic_land> -Algorithm SHA256  # 647e1d7b…

# 3) 数据源配置：候选不含 projects/，按 docs/22 §4.1 的 schema 新建运行配置
New-Item -ItemType Directory -Force projects | Out-Null
# 写入 projects\map_sources.json（真实路径，不入 Git；不得提交）

# 4) 研究运行根/输入（B3/B4）：D2 已登记 SHA，但未复制 4.5 GB
$RUN_ROOT   = "D:\CNS-PLANNER-release\data\research-runs"
$INPUT_ROOT = "D:\CNS-PLANNER-release\data\research-inputs"
# 迁移命令见 docs/22 §5.1；复制后核对：
Get-FileHash "$INPUT_ROOT\runtime\project_state.json" -Algorithm SHA256  # c7822166…
Get-FileHash "$RUN_ROOT\runs\c3b-20261027-d\run_manifest.json" -Algorithm SHA256  # 68a4a0bd…
# 关键：把 $RUN_ROOT\dispatch_state.json 的 research_fresh_input.root 从
# D:\Projects\CNS-PLANNER\_work\c2ar\research 改为 $INPUT_ROOT（否则仍依赖 _work/）

# 5) 启动研究只读服务
D:\tools\anaconda\python.exe tools\serve_r0005_research.py --root $RUN_ROOT
# 读取 $RUN_ROOT\ready.json 的 port/token

# 6) V1–V4
# 2D: http://127.0.0.1:<port>/?run=c3b-20261027-d
# 3D: http://127.0.0.1:<port>/research_3d.html?run=c3b-20261027-d
# 报告: /api/research/runs/<run>/report?format=html
# QGIS: python-qgis-ltr.bat tools\render_r0005_research_figures.py ...
```

> D2 准备结论：外部数据 SHA 与 7 个环境版本全部核对通过；研究输入（168 MB）与运行根
> （4.5 GB）已定位、已登记 SHA，但**未复制**；`dispatch_state.json` 的 `_work` 路径必须在
> D3 复制后改写。因此 D3 仍未执行，B3/B4 仍需负责人授权后解除。

### 7.3 通过判据

- `git status` 干净；无 `_work/` 依赖。
- 外部数据 SHA 全部匹配；缺失时服务显式报 `terrain_unavailable`/`egm2008_grid_missing`，
  不得用 0 代替。
- 两 run 的 manifest/结果清单/五阶段 SHA 与 `docs/22` §5 一致。
- 2D/3D/QGIS/报告六处身份一致（V4）。
- 停服重启后冻结字段一致且 `no_solver_executed=true`（V3）。

### 7.4 发布五元组

D3 记录：`commit SHA` + `patch SHA`（D2 diff）+ `data SHA`（FABDEM/EGM/land_mask/cartographic/研究根）
+ `algorithm/schema version`（`contract_version`、`CNS_LAYOUT_PROFILE`）
+ `run ID`（两个冻结 run）。

---

## 8. 阻塞 D3 的项

| ID | 项 | 处理 |
|---|---|---|
| B3 | 研究运行根在 `_work/c3b/run`（4.5 GB） | D2 已登记路径/大小/manifest SHA；待负责人授权复制到 `D:\CNS-PLANNER-release\data\research-runs` |
| B4 | 源研究输入在 `_work/c2ar/research`（168 MB） | D2 已登记 `project_state.json` SHA；待复制到 `research-inputs` 并改写 `dispatch_state.json` |
| B5 | Cesium vendor 取舍 | **D2 已解除**：8 文件 + LICENSE + PROVENANCE 入 Git，`.gitignore` 仅放行 `vendor/cesium/` |
| B6 | P16 成本 / actual_delivery unknown | 不阻塞软件复现；阻塞保障结论 |
| B1/B2 | Radar layout / RID surface 拆分 | 不阻塞“研究软件复现”；阻塞“保障确认” |
| B10 | 历史项目通信设备仍可能 4 km | 研究输入已改 2 km；正式历史数据迁移未做（D-017 管理） |

> D1/D2 的发布目标是**研究软件可复现**；CNS 保障确认仍被 B1/B2/B6 阻断。
