# D1 Git 纳入/排除清单（来源候选 `_work/c3base/integration`）

- 基线：`89b008d`（`89b008da13bce36b1b2933b571dc4323016debfd`）
- 候选根：`D:\Projects\CNS-PLANNER\_work\d1\rc`
- tracked 改动：**28**；untracked：**47**；合计 **75**
- SHA-256 为工作树原始字节前 16 位；完整值见 `d1-git-inventory.json`。
- `xy`：`M ` = 已暂存修改；` M` = 工作树修改；`MM` = 暂存后又改；`??` = 未跟踪。

> 身份要区分两种：**Git blob SHA**（`head_blob`/`index_blob`，JSON 内）用于 diff/复现；
> **SHA-256** 用于文件内容身份与发布清单。两者不可互相替代。
>
> 下表 SHA-256 是**来源候选 `integration` 当时的值**；D1 修改过的 5 个文件
> （`template_catalog.py`、`render_r0005_research_figures.py`、`qgis_figure_renderer.py`、
> `map_figure_service.py`、`test_map_figures_cns_templates.py`）在候选 `_work/d1/rc` 中已有新值，见 §D。

## A. 28 个 tracked 改动（相对 `89b008d`）

| # | 路径 | 类别 | xy | 字节 | 行 | SHA-256(16) | 用途 |
|---:|---|---|---|---:|---:|---|---|
| 1 | `.gitignore` | other | ` M` | 1,395 | 74 | `8f217ed65b1e452b` | 排除运行时/研究数据与 Cesium vendor；vendor 取舍见 docs/22 §6 |
| 2 | `cns_planner/algorithms/corridor/v1.py` | business | `M ` | 73,454 | 1,394 | `bfd822b37ad71233` | P14 走廊算法：显式配置下追加 navigation_integrity_supplement（无配置时结果/指纹不变） |
| 3 | `cns_planner/algorithms/registry.py` | business | `M ` | 29,038 | 514 | `0aa1fca61843baa0` | 注册 navigation_integrity_endpoint_middle_reuse_v1@1.0；显式拒绝替换 canonical P16 |
| 4 | `cns_planner/api/router.py` | business | `MM` | 77,340 | 1,164 | `8647b7a9ee7a9185` | 研究只读 API、visualization-3d、报告、研究任务提交与 formal mutation 门禁 |
| 5 | `cns_planner/api/server.py` | business | `MM` | 8,414 | 158 | `f46d806d7a34b137` | 文本响应 UTF-8 编码修复；研究写请求 token/read-scope 门禁 |
| 6 | `cns_planner/application/cns_input_service.py` | business | `M ` | 22,217 | 430 | `91a19dac03d3d150` | 研究输入装配：surface facts、2 km 设备输入接线 |
| 7 | `cns_planner/application/corridor_service.py` | business | `M ` | 18,683 | 354 | `d11cda8f137156ac` | 走廊服务：NM3 研究输入接线 |
| 8 | `cns_planner/application/corridor_site_planning_service.py` | business | `M ` | 209,437 | 3,948 | `8ae157abb5168659` | P16 站址规划：中段 N 复用、post-plan 投影（+591） |
| 9 | `cns_planner/application/invalidation_service.py` | business | `M ` | 42,284 | 778 | `e7c7cb8948e91143` | 失效传播：新增研究/中段动作相关失效入口（单向最小） |
| 10 | `cns_planner/application/plan_projection.py` | business | ` M` | 17,627 | 382 | `0cf1333d202b080e` | P17/P18 规划投影：中段动作与身份字段 |
| 11 | `cns_planner/application/plan_review_service.py` | business | `M ` | 39,470 | 686 | `af0e7a3602d77972` | P18 评审/Confirm/Apply 门禁；研究模式禁止正式写 |
| 12 | `cns_planner/application/project_state.py` | business | `M ` | 50,834 | 916 | `b72a4cf5e5640ac9` | schema-v2 状态规范化：NM3/中段复用容器 |
| 13 | `cns_planner/domain/cns_service_contract.py` | business | `M ` | 42,801 | 952 | `4b560a40a15e1b0e` | C 统一 2 km；RID 陆/岸 2 km、海 5 km；redundancy_by_surface 2/1 |
| 14 | `cns_planner/domain/cns_service_registry.py` | business | ` M` | 31,446 | 665 | `2bc867d32262860c` | N v2 显式 opt-in 归一化；默认 v1 10 km 不变 |
| 15 | `cns_planner/domain/navigation_integrity_monitoring.py` | business | ` M` | 24,740 | 521 | `361bdb8acd863bc5` | 正式 N v1 合同；仅显式 v2 标记时委派研究层 |
| 16 | `cns_planner/reporting/map_templates/template_catalog.py` | business | ` M` | 29,270 | 620 | `bad4d1d6257de203` | CNS 图例/覆盖策略：通信 2 km、RID 陆/岸 2 km 海 5 km、N v1 10 km |
| 17 | `cns_planner/tasks/handlers.py` | business | `MM` | 18,612 | 360 | `2f23c6f07dade6da` | 研究任务 handler：fresh_compute / resume_checkpoint |
| 18 | `cns_planner/tasks/service.py` | business | `M ` | 31,911 | 710 | `141c48c6e461673a` | HeavyTask 服务接线（研究任务与正式任务隔离） |
| 19 | `cns_planner/tasks/task_specs.py` | business | `MM` | 94,376 | 2,008 | `e61c80c01951162c` | 研究任务 TaskSpec（research_isolated_read_only / fresh_compute） |
| 20 | `tests/p14_protected_sha256.json` | test | ` M` | 456 | 6 | `9c009106d9be4c84` | P14 保护哈希基线（四个文件，LF 规范化） |
| 21 | `tests/test_algorithm_registry.py` | test | `M ` | 11,884 | 243 | `d02c9fa63d73bfc9` | 注册表回归：研究算法注册与 canonical P16 不被替换 |
| 22 | `tests/test_cns_communication_rid_services.py` | test | `M ` | 35,706 | 705 | `76760d030ea3c301` | C/RID 合同：2 km、陆/岸 2 海 5、surface-aware |
| 23 | `tests/test_p16_prefilter_and_prepared_cells.py` | test | `M ` | 9,143 | 199 | `896052ebb86247eb` | P16 预筛/预备单元回归 |
| 24 | `tests/test_phase4_b6r_immutable_task_input.py` | test | ` M` | 45,408 | 1,033 | `d156862caa7991a7` | P14 保护文件字节一致（LF 规范化） |
| 25 | `tests/test_phase4_b6x_heavy_task_runtime.py` | test | ` M` | 33,458 | 773 | `d7902aac26ab0f0b` | P14 保护文件字节一致（LF 规范化） |
| 26 | `tests/test_phase4_b9r1_async_admission.py` | test | ` M` | 43,179 | 961 | `7fc563b5cc013c29` | P14 保护基线与工作区一致（LF 规范化） |
| 27 | `tests/test_required_cns_adoption_services.py` | test | `M ` | 23,360 | 481 | `7cfb2b2e8e093284` | RequiredCNS 采纳：通信 2 km 裁定 |
| 28 | `tests/test_round2_surface_wiring.py` | test | `M ` | 93,925 | 1,930 | `eaf2d0ef3ad4ee40` | surface 接线回归（含历史断言更新） |

## B. 47 个 untracked 文件

| # | 路径 | 类别 | xy | 字节 | 行 | SHA-256(16) | 用途 |
|---:|---|---|---|---:|---:|---|---|
| 1 | `cns_planner/application/navigation_integrity_research_service.py` | business | `??` | 1,260 | 18 | `6a8362af4d353a14` | N 研究只读服务（v2 合同视图） |
| 2 | `cns_planner/application/navigation_planning_integration.py` | business | `??` | 28,573 | 568 | `50dc0eae56c75ff1` | NM3 规划集成：端点+中段动作、P16/P17/P18 投影 |
| 3 | `cns_planner/application/research_compute_service.py` | business | `??` | 45,669 | 946 | `b0881af26ff4304b` | fresh_compute 编排：P14→P18、身份/artifact/manifest |
| 4 | `cns_planner/application/research_input_service.py` | business | `??` | 14,445 | 330 | `0401cae0743d48fd` | 研究输入准备：2 km 设备半径、surface facts 重建 |
| 5 | `cns_planner/application/research_map_service.py` | business | `??` | 9,438 | 232 | `273f84dca3a64d64` | 二维研究地图投影服务 |
| 6 | `cns_planner/application/research_parameters.py` | business | `??` | 8,179 | 196 | `47cb8728df2ec982` | 研究参数合同与参数指纹 |
| 7 | `cns_planner/application/research_report_service.py` | business | `??` | 24,722 | 544 | `2b3d4af8407b7563` | 研究报告 HTML/Markdown 同源生成 |
| 8 | `cns_planner/application/research_run_service.py` | business | `??` | 17,506 | 362 | `c0f908f175725a45` | 研究运行注册/清单/恢复（只读） |
| 9 | `cns_planner/application/research_visualization_service.py` | business | `??` | 23,143 | 501 | `f2b873d8426d23eb` | 三维研究投影：EGM2008 正高→椭球高、地形、实体 |
| 10 | `cns_planner/domain/navigation_integrity_contract_v2.py` | business | `??` | 16,248 | 407 | `fe5b5f60ff655e81` | N v2 研究合同（RESEARCH_ONLY、1000 m、显式 opt-in） |
| 11 | `cns_planner/domain/navigation_middle_reuse.py` | business | `??` | 10,680 | 163 | `fa3ebadcac485a86` | NM3 中段复用领域逻辑（2 个不同物理站中段热点） |
| 12 | `cns_planner/gis/egm2008_geoid.py` | business | `??` | 17,047 | 411 | `233292f900b2bfa4` | EGM2008 网格只读适配（PROJ vgridshift，fail-closed） |
| 13 | `cns_planner/site_planner/navigation_middle_reuse_v1.py` | business | `??` | 1,086 | 20 | `18d140235e107696` | NM3 站点复用算法注册包装 |
| 14 | `cns_planner/web/js/research_3d.js` | frontend | `??` | 39,751 | 924 | `67115e4f10352192` | 研究三维页面（Cesium）：航路/站址/设备/N 动作/图层状态 |
| 15 | `cns_planner/web/js/research_app.js` | frontend | `??` | 22,721 | 546 | `6b108336825f29a4` | 研究二维应用：加载/boot/参数修改/提交/任务轮询 |
| 16 | `cns_planner/web/js/research_map.js` | frontend | `??` | 4,319 | 113 | `46d306af8fe04c6a` | 研究二维地图入口 |
| 17 | `cns_planner/web/js/research_map_renderer.js` | frontend | `??` | 13,671 | 373 | `8568384595535939` | 研究二维 Canvas 渲染与标签避让 |
| 18 | `cns_planner/web/js/research_runs.js` | frontend | `??` | 5,409 | 122 | `dadcebef8de4fb1b` | 研究运行列表/身份面板 |
| 19 | `cns_planner/web/js/visualization3d/basemap.js` | frontend | `??` | 44,178 | 855 | `f641eaeb3860c4d6` | 研究三维 viewer/底图封装（复用 Cesium） |
| 20 | `cns_planner/web/research.css` | frontend | `??` | 11,977 | 121 | `f23385d44feb62b6` | 研究二维样式 |
| 21 | `cns_planner/web/research.html` | frontend | `??` | 9,054 | 158 | `9ce6ac6905079079` | 研究二维页面 |
| 22 | `cns_planner/web/research3d.css` | frontend | `??` | 5,380 | 60 | `1d86d6749340f1f9` | 研究三维样式 |
| 23 | `cns_planner/web/research_3d.html` | frontend | `??` | 8,031 | 134 | `33f8f9f200ee1ada` | 研究三维页面 |
| 24 | `config/navigation_integrity/endpoint_middle_reuse_v1.json` | config | `??` | 146 | 6 | `c30fcc38e0bf39df` | NM3 端点/中段复用配置 |
| 25 | `config/navigation_integrity/planning.example.json` | config | `??` | 257 | 11 | `9ff99db30ef593fa` | N 规划输入示例（显式 v2） |
| 26 | `config/navigation_integrity/selection.schema.json` | config | `??` | 709 | 18 | `6db062d2c2bc854f` | NM3 选择项 schema |
| 27 | `tests/c3a_research_run.test.mjs` | test | `??` | 1,521 | 27 | `3335da83bdcfd491` | 研究运行前端合同测试 |
| 28 | `tests/c3b_research_ui.test.mjs` | test | `??` | 3,967 | 81 | `feca710445588686` | 研究二维 UI 合同测试（参数/提交/门禁） |
| 29 | `tests/c3c_research_map_labels.test.mjs` | test | `??` | 1,150 | 26 | `f2e5880b1d4e36a0` | 研究二维标签避让/端点分类测试 |
| 30 | `tests/fixtures/navigation_middle_reuse/endpoint_context.json` | test | `??` | 25,655 | 692 | `03551395a1035db7` | NM3 端点上下文 fixture |
| 31 | `tests/fixtures/navigation_middle_reuse/endpoint_input.json` | test | `??` | 2,192 | 96 | `8d0d4d2a4f4f20d0` | NM3 端点输入 fixture |
| 32 | `tests/fixtures/navigation_middle_reuse/middle_bindings.json` | test | `??` | 111 | 4 | `1518ba7a09d78d24` | NM3 中段绑定 fixture |
| 33 | `tests/fixtures/navigation_middle_reuse/sites.json` | test | `??` | 810,620 | 30,386 | `f83736798c260eb4` | NM3 站点池 fixture（810 KB） |
| 34 | `tests/fixtures/navigation_middle_reuse/stations.json` | test | `??` | 4,473 | 200 | `979d84ecb0359da6` | NM3 站址 fixture |
| 35 | `tests/test_c2b_middle_projection.py` | test | `??` | 3,754 | 98 | `ace310587cb23ad5` | C2-B 中段投影回归 |
| 36 | `tests/test_c3a_research_run.py` | test | `??` | 3,655 | 73 | `0d0ae819630ccbcf` | 研究运行/恢复回归 |
| 37 | `tests/test_c3b_research_ui.py` | test | `??` | 9,827 | 209 | `ac0fb3ff95262a36` | 研究二维 UI 后端合同 |
| 38 | `tests/test_c3c_research_figures.py` | test | `??` | 4,772 | 97 | `46e88d547a0551d0` | 研究图件 spec/身份回归 |
| 39 | `tests/test_c3c_research_report.py` | test | `??` | 9,335 | 196 | `f1edfcc20d0d4a04` | 研究报告同源/编码回归 |
| 40 | `tests/test_c3c_research_visualization.py` | test | `??` | 13,381 | 273 | `804b17ddc2408908` | 研究三维投影/垂直基准回归 |
| 41 | `tests/test_navigation_middle_reuse_v1.py` | test | `??` | 7,835 | 143 | `be1727e02c0f9b52` | NM3 中段复用算法回归 |
| 42 | `tests/test_navigation_planning_integration.py` | test | `??` | 9,122 | 150 | `a53dd2c636868027` | NM3 规划集成回归 |
| 43 | `tests/test_round40a_navigation_integrity_v2.py` | test | `??` | 19,993 | 436 | `a3666ddb38e998ac` | N v2 研究合同回归（v1 默认不变、显式 opt-in） |
| 44 | `tools/build_r0005_research_dispatch.py` | tool | `??` | 6,560 | 150 | `27910a19ffffdea0` | 构建 R0005 研究 dispatch 状态 |
| 45 | `tools/build_r0005_terrain_grid.py` | figure-tool | `??` | 13,124 | 329 | `b921b7665343afc1` | 从 FABDEM+EGM2008 构建三维显示地形（QGIS） |
| 46 | `tools/render_r0005_research_figures.py` | figure-tool | `??` | 30,895 | 659 | `c477923907ed2280` | R0005 六类研究图渲染（QGIS，A4/300 DPI） |
| 47 | `tools/serve_r0005_research.py` | tool | `??` | 4,206 | 84 | `2a1c47d4a9d0c2c7` | 研究只读服务入口（二维/三维/报告） |

## C. 纳入结论（按类别）

| 类别 | 纳入 D2 | 说明 |
|---|---|---|
| 业务源码（18 tracked + 13 untracked） | 是 | 产品/研究运行时；按 P1/P2 批次 |
| 前端（10 untracked） | 是 | research 页面与 JS/CSS；P3/P4 |
| 配置（3 untracked） | 是 | `config/navigation_integrity/*` |
| 测试（9 tracked + 17 untracked） | 是 | 含 fixture 与 `*.test.mjs`；P6 |
| 图件/地形工具（3） | 是 | `render_*`、`build_r0005_terrain_grid`；P5 |
| 研究服务工具（1） | 是 | `serve_r0005_research.py`；P7 |
| vendor（Cesium 8 文件） | **待裁定** | 当前 `.gitignore` 排除；建议入 Git（B5） |
| 临时产物 | 无 | 本清单 0 项 |

## D. D1 增补（相对 integration）

| 文件 | 类型 | 说明 |
|---|---|---|
| `AGENTS.md` | 新增 | 旧工作区治理约定入候选 |
| `PROTECTED_ASSETS.md` | 新增（重写） | 当前基线受保护资产 + 决策台账 |
| `CLEANUP_PLAN.md` | 新增 | D-012 落实记录 |
| `docs/20-历史裁定与未集成成果梳理-2026-10-27.md` | 新增（校准） | 历史裁定与未集成项 |
| `docs/decisions/2026-09-ai-dev-context-full-archive.md` | 新增 | AGENTS 引用的历史归档 |
| `docs/21-...`、`docs/22-...`、`docs/d1-*` | 新增 | D1 交付 |
| `docs/d1-candidate-inventory.json` | 新增 | D2 候选 100 项最终身份（D1 99 + `cns_inputs.py`；清单自引用 SHA 省略，其余含 SHA-256） |
| `cns_planner/gis/qgis_figure_renderer.py` | 修改 | 图例 4 km → 2 km；RID 海 5 km |
| `cns_planner/application/map_figure_service.py` | 修改 | 通信披露 4 km → 2 km；RID 披露 |
| `cns_planner/reporting/map_templates/template_catalog.py` | 修改（原已在 28） | 图例 4 km → 2 km；RID 海 5 km |
| `tools/render_r0005_research_figures.py` | 修改 | RID 披露陆/岸 2 km、海 5 km |
| `tests/test_map_figures_cns_templates.py` | 修改 | 通信测试改为“按设备输入半径”；semantics 去数字 |
| `tests/test_map_figures_cartographic_polish.py` | 修改 | 最坏情况图例预算改用当前 2 km / 海 5 km 文本 |

## E. 排除清单

| 对象 | 原因 |
|---|---|
| `projects/`（正式 ProjectState、派生数据、map_sources.json） | 运行数据；288 MB |
| `_work/`、`audit_sandboxes/`、`research/`、`outputs/` | 研究/运行数据，不入源码 |
| `cns_planner/web/vendor/cesium/` | 当前 `.gitignore` 排除；见 B5 待裁定 |
| FABDEM、EGM2008、建筑、铁塔、人口 | 外部数据，SHA 校验 |
| `docs/17–19`、`docs/3d-protected-assets.json`、`tools/verify_3d_assets.py` | 历史快照/历史三维资产，不属本基线 |
| `__pycache__/`、`*.pyc`、日志、临时输出 | 无（清单中 0 项） |
