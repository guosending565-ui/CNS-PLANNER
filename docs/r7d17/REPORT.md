# R7-D17 集成版本正式验收启动与 GitHub 开发分支同步

基线：R7-D16 最终 HEAD `4717037738cb8ddd0bf5b9eb570506f8e1b1be0b`（分支
`codex/r7d13-ab-integration`，位于 `origin/main` `89b008d` 之上 5 个提交）。
执行位置：用户授权的既有 R7-D13 隔离工作树
`C:/Users/yiding/.codex/worktrees/r7d13-ab-integration/CNS-PLANNER`。
`D:/Projects/CNS-PLANNER` 主工作区、正式 `ProjectState`、8765/8776 服务与历史工作树均未修改/清理。

---

## 一、结论摘要

| 项目 | 结果 |
|---|---|
| GitHub 开发分支 | **已推送** `dev/r7d17-integration`，发布审核 **pass** |
| 真实 R0005 验收副本 | 已建立并冻结（revision **524**，SHA-256 `cbb09d87…`） |
| Step01–06 | Step01 **current**；Step02/03/04/06 **stale**；Step05 **failed** |
| 真实业务验收 | **未通过**：C/S 走廊缺口、N unknown、P16 仅提案、P17 未计算、报告未生成 |
| P16 历史动作 | canonical 结果 0 项；恢复证据容器 13 项，全部未确认安装 |
| P17 | 旧 2.2 结果 **blocked**（当前 2.5）；未升级为 current |
| 既有失败 | 17 Python + 6 Node 全部复现；1 产品缺陷、4 算法语义变更、18 测试过期、0 环境缺证 |
| Theta* 45° | **未修改**；旧夹具需要更新，不为通过测试放宽约束 |
| 耗时重算 | 本轮 **0** 次 Theta*/P14–P17 全量重算 |

---

## 二、GitHub 同步

### 2.1 分支关系

- 本地 `codex/r7d13-ab-integration` 与 `origin/main` 的 merge-base 为 `89b008d…`；
  **领先 5 个提交、落后 0 个提交**（可从 main fast-forward）。
- 发布分支：`dev/r7d17-integration`，推送 HEAD `b76e054f9607434040704782fe860ab7953cc0a7`。
- 链接：<https://github.com/guosending565-ui/CNS-PLANNER/tree/dev/r7d17-integration>
- 提交：<https://github.com/guosending565-ui/CNS-PLANNER/commit/b76e054f9607434040704782fe860ab7953cc0a7>

### 2.2 公开发布审核（`docs/r7d17/release_audit.json`）

| 检查 | 结果 | 说明 |
|---|---|---|
| 公开白名单 | pass | 891 个跟踪文件，全部落在 `cns_planner/ tests/ tools/ docs/` 或根级文档/配置 |
| 禁用路径/数据 | pass | 无 `projects/ research/ outputs/`、无 `*.tif/*.gpkg/*.qgz`、无 bundle/压缩包 |
| 密钥 | pass | 对全部文本 blob 做高信号正则扫描（AK/SK、GitHub/Slack/OpenAI、私钥、URL 凭据、`tk=`），0 命中 |
| Git 可达历史 | pass | 唯一根 `5b21166`；D9 提交 `2b953bf` **不可达**；相对公开 main 无新增禁用路径 |
| 许可证 | warn | CesiumJS 1.126 Apache-2.0 原文 SHA-256 `3111d10e…` 逐字节一致；仓库自身未声明根 LICENSE（治理项，不阻塞源码分支） |
| 归档 | pass | 无 `.bundle/.zip/.7z/.rar` |
| 大文件 | pass | 最大 `Cesium.js` 5.14 MB；总载荷 26.47 MB；无 ≥20 MB 文件 |
| BOM / tree mode | pass | JSON/YAML 无 BOM；无 symlink/submodule |

范围要点：相对 `origin/main` 新增/修改 **282** 个源码/测试/文档文件；不含 D9 研究历史、
R0005 数据、P16 原件、历史 bundle。发布审核只暂停 push 的条件未被触发，因此已完成推送；
验收本身不因 push 完成而被视为通过。

### 2.3 可恢复性

- 本地分支、`checkpoint/r7d17-integration` 标签与离线 bundle
  `D:/Projects/_cns_backups/r7d17-integration.bundle`（`git bundle verify` 通过）。
- 未强推、未合并 `main`、未 reset/clean、未删除任何历史工作树。

---

## 三、真实 R0005 验收副本与冻结

### 3.1 为什么是 revision 524

正式主工作区 `D:/Projects/CNS-PLANNER/projects/current_project.json` 为 revision 21、
项目 `baipishuhuitu1.0`，**不含 R0005**（0 次出现）。本轮可用且身份匹配的 R0005 权威状态是
R7-D10 从 D9/S4A 恢复的状态：

| 项 | 值 |
|---|---|
| 源 | `research/r7d10/isolated_recovered_state.zip` → `project_state.json` |
| zip SHA-256 | `643fd8817e72ddc95c6ba3ab59715093b931a331cb7c7600b315a9768c95f745` |
| 状态 SHA-256 | `cbb09d87dcf8287fa37f4be8a5bbd7e2ae65dca7a08b4bad12243d7d8c48c536` |
| revision | 524 |
| 项目 | `zhoushan_screenshot_01_20261001_1647`（project_id `9e349d0f…`） |
| 规模 | 2 nodes / 1 scenario route / 1 operational route / 373 towers / 98 参考起降点 |
| 恢复分类 | `identity_matched_historical_proposal_only`；不是本轮重新计算 |

> 正式 ProjectState 自始至终未被打开或写入；验收副本独立位于
> `D:/Projects/_cns_acceptance/r7d17/R0005-rev524/`，状态字节与源一致。

### 3.2 冻结内容（`docs/r7d17/acceptance_freeze.json`）

- **数据源**：完整路径、大小、mtime、SHA-256（小文件）或 size/mtime（大栅格）；
  包含人口、GLO-30、FABDEM、建筑 v3、building grid、陆域/制图陆地面、铁塔/起降点/参考航线。
- **算法身份**：P14 `cns_service_corridor_v1@1.4`、P15 `cns_corridor_gap_v1@1.3`、
  P16 `corridor_reuse_first_site_planner_v2@2.3`、连续服务 `2.2`（当前代码 **2.5**）、
  P17 `tactical_protection_envelope_v1@1.0`、Theta* `2.0`。
- **输入指纹**：P14 `b070276b…`、P15 `952103e3…`、P16 `fc878dad…`、
  连续服务 `2e03f20e…`、方案审查 baseline `b817893e…`。
- **独立服务**：`http://127.0.0.1:8794`，未启动/停止 8765/8776。

---

## 四、Step01–06 状态（只读核查，不强制重算）

| 步 | 分类 | 关键结果 | 输入指纹 / 阻塞 |
|---|---|---|---|
| 01 数据准备 | **current** | workspace passed，104.835 km²，L8 8,008 格，source audits 12 项 passed | 设备参考目录为目录而非具体文件 → `invalid_source_path`（“数据源不完整”提示），不阻塞读取 |
| 02 环境与风险 | **stale** | grid passed；environment_risk stale；grid_risk stale（1.1）；grid_risk_v2 pending_confirmation（2.0）；PCF passed；surface facts passed；建筑净空 not_calculated | `riskframeworkv2-7a74c819…`；grid_risk 明细 `artifact_unavailable` |
| 03 航路规划与发布 | **stale** | R0005 运行航路 **passed**、14 顶点；candidate / risk profile / validation / adoption 全部 stale | `planning_constraint_field_changed`；**未选择固定高度层**，约束场不可用 |
| 04 CNS 需求 | **stale** | required_cns passed；建议 stale；采用 superseded | 建议 `6e50c319…`，`layered_validation_evidence_outdated` |
| 05 CNS 能力与设施 | **failed** | coverage stale、capability stale；走廊 **failed**（C 2727 缺口 / S 2727 缺口 / N 2727 unknown，9404.9 m）；缺口 failed；P16 proposal_ready；连续服务 stale；Radar stale | P14 `b070276b…`、P15 `952103e3…`、P16 `fc878dad…`；coverage/capability 明细引用缺失 |
| 06 方案评审与报告 | **stale** | 审查 stale；方案 not_confirmed；报告 not_calculated；P17 not_calculated；3D 剖面 not_calculated | `continuous_service_parameters_changed`；确认门禁“规划目标未满足” |

补充事实：

- 二维航路 R0005 起点 `[122.267222, 29.866667]`、终点 `[122.188333, 29.819444]`，
  14 个顶点，`operational_routes.status=passed`。
- 373 座铁塔：77 座塔顶已解析但仍未权威确认（`resolved_unconfirmed`），296 座未解析；
  未把 known 当 confirmed。
- 98 个参考起降点 + 2 个项目节点；二维 marker 与三维 billboard 共用
  `landing_pad.png`（SHA-256 `37f83a99…`）。
- 固定 1000 m 契约：半宽 500 m、总宽 1000 m、`fixed-1000m-v1`；
  几何 `corridorgeometryv1-4ded6ec1…` 可用，但 `currentness=stale`，
  `drawable_as_current_band=false`，只允许非权威历史预览。

---

## 五、二维 / 三维实际截图

截图目录：`D:/Projects/_cns_acceptance/r7d17/shots/`（**不进入 Git**，避免把真实空间数据推到公开仓库）。

### 二维

![二维 R0005、373 铁塔、98 起降点与历史走廊](D:/Projects/_cns_acceptance/r7d17/shots/2d-r0005-layers.png)

- 控制台错误 **0**、HTTP 4xx **0**。
- 开启 12 个业务图层（铁塔、参考起降点、既有 CNS、候选站址、C/N/S、P17 走廊、
  CNS 通信/导航/RID/缺口/设施）均无报错；C/N/S 与覆盖面因结果 stale 而不产生 current 图层。
- 画面中的宽走廊带是连续服务/P17 历史预览，不代表 current 保障结论。

### 三维

![三维 R0005 俯视：航路、固定 1000 m 带（stale 预览）与铁塔](D:/Projects/_cns_acceptance/r7d17/shots/3d-r0005-overhead.png)

- 控制台错误 **0**、HTTP 4xx **0**；20 个图层开关，默认只开离线影像/FABDEM 地形。
- `entity_count=1334`；铁塔 373（77/296，统一 PNG）；起降点 98（2+96，PNG billboard）；
  CNS 站址可见。
- 固定 1000 m 带：`band_count=1`，半宽 500/总宽 1000，R0005，
  `currentness=stale`，仅历史预览（2 个实体）。
- P17：`status=blocked`、`stored_algorithm_version=2.2`、
  `current_code_algorithm_version=2.5`、`algorithm_version_conflict=true`，
  `volume_count=1`、`available_horizontal_count=0`、`blocked_count=1`、零写入。
  截图：`3d-p17-blocked.png`。

### 步骤与结果截图

| 截图 | 内容 |
|---|---|
| `step1.png` … `step6.png` | 六个步骤工作台 |
| `s3-res-route.png` | Step03 当前航路 R0005 通过 |
| `s5-cns-res-coverage.png` / `s5-cns-res-capability.png` | 覆盖/能力 stale 与 C/N/S 逐项状态 |
| `s5-cns-res-corridor.png` / `s5-cns-res-gap.png` | 走廊/缺口 failed，2727 与 9404.9 m |
| `s5-cns-res-site.png` / `s5-p16-actions.png` | P16 仅提案、canonical 已选动作 0 |
| `s5-cns-res-continuous.png` | 连续服务 stale（2.2 vs 2.5） |
| `s6-review-res-status.png` / `s6-review-res-report.png` | 总体已失效、报告未计算 |

---

## 六、P14/P15/P16 复用性与 P17

| 容器 | state 版本 | 当前版本 | 状态 | 可复用性判断 |
|---|---|---|---|---|
| P14 服务走廊 | 1.4 | 1.4 | failed | **算法可复用，业务不通过**；不得把 failed 当 current |
| P15 能力缺口 | 1.3 | 1.3 | failed | 同上；C/S 确认缺口 2727，N 2727 unknown |
| P16 设施规划 | 2.3 | 2.3 | proposal_ready | **历史提案可读，不能当已安装/current**；canonical `selected_actions=0` |
| 连续服务 | 2.2 | **2.5** | stale | **不可复用**；`algorithm_semantics_changed`，需按 2.5 重算 |
| P17 保护包络 | 1.0 | 1.0 | not_calculated | 旧结果不得升级为 current；3D 以 2.2/2.5 冲突 fail-closed |

P16 细节：恢复证据容器 `cns_corridor_site_plan_results` 有 **746** 个候选动作、
**13** 个历史选择动作、5454 个目标；13 项全部
`physical_mount_confirmed=false`、`requires_site_survey=true`、
`subsystem_mount_status=unverified`。canonical `cns_corridor_site_plan.selected_actions` 为
**0**，因此 UI 的“已选动作 0”是 canonical 事实，不是漏读；13 项只存在于恢复证据容器，
既不是已安装设施，也不是本轮重新计算的结果。

---

## 七、17 项 Python + 6 条 Node 失败归因

完整逐项证据见 [`failure_attribution.json`](failure_attribution.json)。

### Python（`17 failed, 189 passed`）

| 测试 | 归因 | 结论 / 动作 |
|---|---|---|
| `test_corridor_async_result_matches_sync_semantics` | **产品缺陷** | sync/async 仅 `input_fingerprint` 不同（`2fdd2175…` vs `4a48fd57…`），几何与状态一致；需统一任务输入冻结 |
| `test_p14_protected_files_are_byte_identical` | 测试过期 | 2/4 保护文件哈希来自 B6X 旧基线；Round38 surface-aware 已改，且相对 main/基线无本轮 diff；需 owner 重建保护清单 |
| `test_open_existing_project_switches_state_and_restores_sources` | 测试过期 | D15 fixed-1000m-v1 改变 `cns_corridor_policy` 形状 |
| `test_building_mapping_is_level_independent_when_footprints_are_configured` | 测试过期 | 建筑事实键集合扩展，旧“恰好相等”断言过期 |
| `test_workflow_calls_replaceable_model_and_persists_then_stales_risk` | 测试过期 | apply 返回压缩摘要、reopen 水合 cells（B5X 契约） |
| `test_profile_ground_exposure_matches_a_direct_canonical_integral` | 测试过期 | 夹具 grid_id 仍是 `…-RP0`，当前 canonical 为 `…-RP00027000` |
| `test_navigation_rtk_lost_with_adequate_gnss_is_acceptable_degraded` | 算法语义变更 | D15 2.5：N 降级时长缺证 → unknown；不得改回 acceptable |
| `test_navigation_rtk_lost_with_insufficient_gnss_accuracy_is_unacceptable` | 算法语义变更 | 同上；缺证时不可判定，不是 unacceptable |
| `test_navigation_rtk_loss_from_route_service_evidence_is_structural` | 算法语义变更 | 同上 |
| `test_protection_corridor_uses_explicit_parameters` | 算法语义变更 | D15 将固定 1000 m 带与响应/保护走廊解耦，旧 `outer=500+410` 过期 |
| `test_route_planner_v3_continuous.py` 7 项建筑用例 | 测试过期 | 建筑验证已要求 confirmed height evidence；夹具只给 `height_m`，得到 unresolved 是 fail-closed 正确行为 |

### Node（3 文件 / 6 条）

| 测试 | 归因 | 结论 |
|---|---|---|
| `frontend_modules` P7-P12 文案 | 测试过期 | 断言旧“监视雷达默认可选”文案 |
| `frontend_modules` layered bind | 测试过期 | 测试桩把 `panelError(hint)` 当失败/调用；产品仍是单次 `evaluate-real` POST |
| `frontend_modules` legacy λ | 测试过期 | 同上；`blockers.thetaV2` 仍为 0，legacy λ 未参与阻塞 |
| `frontend_modules` candidate metrics | 测试过期 | HTML 不再输出字面量 `rewired_parent_shortcuts`，模型值仍为 7 |
| `route_risk_profile_frontend` | 测试过期 | 测试桩缺 `c.paint` |
| `cns_service_round3_frontend` | 测试过期 | 复位层数 2→3，硬编码数字过期 |

统计：**1 产品缺陷、4 算法语义变更（测试未跟）、18 测试过期、0 环境缺证**。
与 R7-D16 在 `8f1c927` 上复现的失败集合一致，本轮未新增失败。

---

## 八、Theta* 45° 端点约束

- `theta_star_v2.py` 的 `ENDPOINT_TRANSITION_ADMISSIBILITY_DEG = 45.0` 未改；
  相对基线 `8f1c927` 与 `origin/main` 均 **无 diff**。
- 旧用例 `test_theta_star_and_supercover_never_cross_blocked_constraint_cells`
  仍是 `no_path` vs `candidate`：3×3 阻挡网格的唯一绕行以 90° 到达终点，45°/60° 下
  `arrival_admissible=false`。归因为**夹具早于端点过渡策略**，不是几何/LOS/门禁回归。
- 处理决定：旧夹具需要更新（改为 ≤45° 到达，或在测试内显式声明 90° 前提并标明仅测试前提）；
  **不为通过测试放宽生产 45° 默认值**。

---

## 九、真实阻塞与下一轮最小完整验收

### 9.1 真实阻塞

1. **Step02/03 输入未闭环**：未选择固定巡航高度层 → PCF 不可用 → candidate/风险画像 stale。
2. **Step05 业务失败**：C/S 走廊各 2727 体元确认缺口，N 2727 unknown（FC30 缺精度/完好性/
   降级时长证据）；P14/P15 为 failed，不是算法版本问题。
3. **P16 不可当已安装**：只有 proposal 与恢复证据中的 13 项历史动作，全部未确认安装；
   必须显式确认后才能应用。
4. **连续服务与 P17 版本断代**：state 2.2 vs 当前 2.5；3D P17 已 blocked；
   旧 P17 不得升级为 current。
5. **Step06 报告门禁**：方案未确认、P17 未评估 → 正式报告不能生成。
6. **数据源治理**：设备参考目录配置为目录而非具体文件，source audit 报
   `invalid_source_path`；这是“数据源不完整”提示的来源之一。
7. **测试债**：1 个 async 输入指纹产品缺陷 + 4 个 2.5 语义测试 + 13 个夹具/文案/桩过期。

### 9.2 下一轮最小完整验收任务（建议）

1. **冻结输入**：固定 R0005、ALT-100、固定 1000 m 走廊、373 铁塔、真实人口/地形/建筑；
   记录数据源 SHA 与算法版本。
2. **只算必要链**：为 ALT-100 生成/确认 PCF（若当前版本语义已变）；只对 R0005 走一次
   candidate → validation（不扩全图搜索）；确认运行航路仍为 current。
3. **P14→P15**：按固定 1000 m 契约重算一次，保留 C/S 2727 与 N unknown 的前后指纹；
   不因结果存在就跳过失败结论。
4. **连续服务 2.5 + P17**：在 P14/P15 current 之后重算连续服务与 P17；
   旧 2.2 结果只作历史对照。
5. **P16**：仅在 C/S 仍有确认缺口且用户显式确认后生成/应用；13 项历史动作仍按
   `unverified` 对待，不写 installed。
6. **Step06**：完成方案审查 → 确认 → 应用 → 报告；报告必须显示 P17 与连续服务真实状态。
7. **测试债清理**：先修 async 输入指纹产品缺陷；再按 `failure_attribution.json`
   逐项更新测试/夹具，禁止放宽 fail-closed 语义。

---

## 十、隔离、检查点与变更

- 未修改正式项目；未 reset/clean；未删除历史工作树；未启动/停止 8765/8776。
- 本轮产品修改仅 1 处：`restore_compacted_results` 在重新读取 artifact 时清除上一次加载
  留下的 `artifact_diagnostics` / `detail_status` / `detail_error`（恢复副本补回明细后不再
  永久显示“明细不可用”）；新增定向回归，18 项通过。
- 代码/证据提交：`b76e054`（发布审核 + 修复）；本报告提交见 Git 历史。
- 恢复标签：`checkpoint/r7d17-integration`；
  离线 bundle：`D:/Projects/_cns_backups/r7d17-integration.bundle`。

## 十一、证据文件

| 文件 | 内容 |
|---|---|
| `release_audit.json` | 公开发布审核（白名单/历史/密钥/许可证/归档/大文件/BOM） |
| `step_status.json` | Step01–06 容器状态、版本、输入指纹与分类 |
| `acceptance_freeze.json` | 源/副本 SHA、revision、数据源、算法身份、P16/P17 身份 |
| `failure_attribution.json` | 17 Python + 6 Node 逐项归因 |
| `theta_45.json` | 45° 端点约束与夹具决策 |
| `visual_evidence.json` | 二维/三维截图清单与控制台/HTTP 证据 |
| `audit_release.py` / `audit_steps.py` / `freeze_acceptance.py` | 可重放只读脚本 |
