# R7-D15 D 批连续 CNS 服务、评审与报告实际集成

基线 `240b7dcea119eca6f4fff9e07a9eec07f1586606`，分支 `codex/r7d13-ab-integration`；来源 D9 `2b953bfcbf9ccc74e5c07ad582d67324c0a164ca`，共同祖先 `68e2674`。执行位置为用户明确授权的既有 R7-D13 隔离工作树 `C:/Users/yiding/.codex/worktrees/r7d13-ab-integration/CNS-PLANNER`。`D:/Projects/CNS-PLANNER` 主工作区保持原状；未 push、未合并 main、未 reset/clean、未清理任何历史工作树。

## 一、D 批 6 文件冲突解决

按 D12/D14 既定 D 批清单，原 6 文件共 11 处 `git merge-file` 冲突逐段协调，没有整文件 ours/theirs 覆盖：

1. `cns_planner/algorithms/continuous_service/v1.py`（1 处）：同一插入点同时保留 main 的 Radar 规划期探测代理、有效状态白名单、逐 service P15 覆盖判定、`probe=None` 双语义；以及 D9 的固定 1000 m 保障带宽、响应及时性与固定宽带解耦、N 降级时长缺证 unknown、`managed_gaps` 证据门禁、`detection_inputs` 输入快照。
2. `cns_planner/domain/cns_continuous_service.py`（1 处）：采用 D9 的 `round38-w1b-fixed-band-response-decoupled` 契约；算法版本合并 main 2.2（Radar 代理）与 D9 2.4（固定带宽/N 缺证）语义后升级为 `2.5`，因此 2.0/2.1/2.3/2.4 的旧持久化结果一律 effective stale。
3. `cns_planner/application/report_service.py`（1 处）：保留 main 的有界诊断草稿（不返回 `report_data`/`source_artifacts`，不做 821 MB 级响应），同时接回 D9 的 P18 评审证据外置与摘要化；外置只写报告证据 artifact，不写 `ProjectState`。
4. `cns_planner/application/map_figure_service.py`（2 处）：第一处同时保留 main 的 Radar-I/II 规划扇区与站址绘制，以及 D9 的雷达规划安装高度读取；第二处保留 main 的 per-route Radar 草稿披露条件，并追加 D9 的固定保障带 currentness 披露与导航监测关联披露。
5. `cns_planner/reporting/html_renderer.py`（1 处）：保留 main 的 P16 proposal 可读区块（未确认、未应用），同时使用 D9 的 `review_summary` 有界评审证据和 `display`；P16 原始动作只在渲染时只读消费，完整证据仍由 artifact 承载。
6. `tests/test_round29q_p17_radar_limitation.py`（5 处）：合并为同一版本断言：当前 2.5，2.0/2.1/2.3/2.4 全部 stale；同时保留 main 的 2.1 旧结果 stale 断言与 D9 的 2.3 旧结果断言。

## 二、必要依赖接回（非 6 文件冲突，但 D 批语义所需）

- 固定 1000 m 走廊：`domain/cns_corridor.py`、`domain/cns_planning_objectives.py`、`algorithms/corridor/v1.py`（1.3→1.4）、`algorithms/corridor_gap/v1.py`（1.2→1.3）、`application/corridor_service.py`、`application/corridor_site_planning_service.py`、`site_planner/corridor_reuse_first_v2.py`、`gis/corridor_geometry.py`、`gis/corridor_band.py`、`gis/figure_currentness.py`、`application/fixed_corridor_geometry_service.py`。
- 评审与报告：`reporting/review_evidence.py`、`reporting/builder.py`、`application/plan_review_service.py`、`reporting/map_templates/__init__.py`、`reporting/map_templates/template_catalog.py`、`gis/figure_style.py`。
- 任务冻结与组合根：`tasks/task_specs.py`（P14 输入冻结共享米制 geometry）、`application/workflow_service.py` 与 `application/app_context.py`（只读注入 `FixedCorridorGeometryService`）。
- 导航监测图层模块 `application/navigation_monitoring_figure.py` 已接入图件能力；其生产 provider 属 E 批装配，本轮不新增 3D/导航 UI。

固定带宽契约 `fixed-1000m-v1`：中心线左右各 500 m、总宽 1000 m；它是 CNS 服务保障目标带，不是设备覆盖、不是认证导航覆盖区、不是法规批准空间。P14 的固定几何身份进入 P15/P16 输入指纹；旧几何身份的 P14 不得被 P15 当作 current 全宽证据。

## 三、保留 main 与接回 D9 的边界

保留 main：

- Radar planning detection proxy（`planning_proxy_validated`、`detection_distance_usable`、逐 service `service_coverage_status`，proxy≠实测探测概率）；
- Radar layout 有效状态白名单（仅 `proposal_ready` 可产出代理）；
- 每航路草稿披露与报告状态控制；有界报告预览与真实 `report_mode`；
- `probe=None` 的“证据不足”与“已全覆盖”分离，不把无缺口说成无探测证据。

接回 D9：

- 固定 1000 m CNS 保障走廊及 P14/P15/P16 的宽度契约；
- N 导航降级时长/阈值缺证 → N 状态 unknown，不进入 managed gap 或 Step6 时间容忍接受链；
- `managed_gaps` 必须有 `duration_s` 与 `limit_s` 证据；
- P16 外置动作、残余确认目标与 `stop_diagnostics` 的只读消费；
- P18 评审证据外置与摘要化，不改变 C/N/S 业务结论。

## 四、C/N/S 历史语义核对（只读，不重算）

使用本机 `research/r7d10` 恢复的 R0005 历史 P14/P15/P16 证据（`p16_result.json` 263,909,253 字节）。脚本 `docs/r7d15/verify_historical.py` 只读加载并核对，结果见 `docs/r7d15/historical_verification.json`：

| 分系统 | 布设前确认缺口 | 布设后确认缺口 | 布设前 unknown | 布设后 unknown | 最长连续失保 前→后 (m) |
|---|---:|---:|---:|---:|---|
| C communication | 2727 | 0 | 0 | 0 | 9404.937615 → 0 |
| N 机载导航 | 0 | 0 | 2727 | 2727 | 存储 0，但语义为 unknown，不代表通过 |
| S rid_cooperative | 2727 | 219（2508 satisfied） | 0 | 0 | 9404.937615 → 590.603453 |

P16 身份与停止诊断：`target_voxel_count=5454`；13 项 selected_actions 全部 `physical_mount_confirmed=false`、`requires_site_survey=true`、`subsystem_mount_status=unverified`，**没有任何历史 P16 动作被转为正式已安装设施**；`stop_diagnostics.selection_limit=13`，`stop_reason=coverage_objectives_met_but_continuous_service_unacceptable`，`terminal_managed_gap_count=0`，残余确认目标 219，unknown 证据 2727。P16 原件 SHA256 与 D10 `evidence_manifest.json`、`identity.json` 一致，分类仍为 `identity_matched_historical_proposal_only`。

## 五、验证与测试

Python 定向 819 通过、1 项已知范围外 deselected：

- D15 核心/走廊/图件/报告组：395 通过；
- A/B/C/D14 集成、Radar、共享站址、人口与 PCF 组：283 通过、1 deselected（合成 Theta* 已知项）；
- 评审、冻结输入、项目切换、采纳组：141 通过。

Node 定向 116 通过：parsed JSON 与 fetch 单次 POST 3+3、重任务前端 14、P16 后台 6、采纳 33、Radar 18、Radar 后台 2、人口 nodata 11、图件 16+4+6。

另通过：全部改动 Python `py_compile`、关键模块导入、`git diff --check`、新增/修改 JSON 无 BOM 字节检查。未运行 Theta*/GridRiskV2/P14–P17 真实规划，未修改正式 `ProjectState`。未作真实 QGIS/三维端到端验收声明。

## 六、范围外待核项

`tests/test_phase4_b3x_planning_constraints.py::test_theta_star_and_supercover_never_cross_blocked_constraint_cells` 仍为 `no_path` vs `candidate` 差异；D14 已记录为范围外合成 Theta* 失败。本轮未修改 Theta* 算法、未追查该非本批问题，详见 `docs/r7d15/pending_items.json`。完整验收前再判断是否存在实际回归。

## 七、保护与恢复

白名单仅含源码、测试、工具性脚本与 `docs/r7d15` 证据；不含正式 ProjectState、research 业务数据、空间文件或历史压缩包。提交后建立恢复标签 `checkpoint/r7d15-d-integration` 与本机离线 bundle `D:/Projects/_cns_backups/r7d15-d-integration.bundle`（`git bundle verify` 通过）。无 push、无合并 main、无 reset/clean；主工作区保留全部原有改动。

## 八、E 批最小继续任务

1. 按端点装配 `cns_planner/api/router.py`，接回 `GET /api/fixed-corridor-geometry` 与导航监测只读端点；保留 A/B 单次 POST、项目切换锁和后台任务入口。
2. 接回 main 任务入口、Step05 与三维只读路由；复用本轮 `FixedCorridorGeometryService` 与固定带宽契约，不在 UI 侧复制几何。
3. 按 `docs/19` 与 `docs/3d-protected-assets.json` 逐项验收 19 项三维交互；按既定清单替换指定起降点 PNG（`D:/浏览器下载内容/无人机停机坪图标.png`，SHA256 `37f83a99215bc49670b22ec5e6c700a583dc99c6d963536f133d25e40365e76b`），二维 marker 与三维 billboard 共享同一 PNG，检查透明边缘/锚点/缩放；不重新调查图标需求。
4. 完整验收前复核 Theta* 待核项；若确认与本轮无关，再继续 E 批，不擅自修改算法。
