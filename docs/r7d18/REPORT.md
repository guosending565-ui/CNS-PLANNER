# R7-D18 真实 R0005 输入闭环与历史测试债修复

基线：R7-D17 最终 HEAD `97be3cf2de2952632d62b0b55112351bf9662d90`（分支
`codex/r7d13-ab-integration`）。
执行位置：既有 R7-D17 隔离工作树
`C:/Users/yiding/.codex/worktrees/r7d13-ab-integration/CNS-PLANNER`。
正式 `ProjectState`、`D:/Projects/CNS-PLANNER` 主工作区、8765/8776 服务均未修改/清理。

---

## 一、结论摘要

| 项目 | 结果 |
|---|---|
| 历史失败 23 项 | **全部关闭**：17 Python + 6 Node 通过；新增 2 项回归 |
| 产品缺陷 | **2 项**：sync/async corridor policy 冻结不一致；D4 风险画像 connector 语义在集成分支运行时 TypeError |
| Step01 | **current**：`invalid_source_path` 已消除，0 项非法来源 |
| Step02 | **stale**：PCF ALT-100 current；grid_risk_v2 仍 `pending_confirmation` |
| Step03 | **stale**：14 顶点 R0005 已用 D4 契约复验，结论 `unknown`；未跑 Theta* |
| Step04 | **current**：建议已重新评估为 `recommendation_ready`（proposal-only）；历史采纳仍 `superseded` |
| P14 / P15 | **业务 failed，不是计算 stale**；指纹与算法版本均匹配当前代码 |
| 剩余 C/N/S | C 2727 确认缺口 / S 2727 确认缺口 / N 2727 unknown；最长 9404.94 m |
| 重算 | Theta* 0 次；P14/P15/P16/P17/连续服务 0 次；只做了 1 次既有航路 D4 复验 |
| 正式提交 | 本地 commit + 推送既有 GitHub 开发分支（见 §十） |

---

## 二、测试债修复（23 项）

### 2.1 Python（17 项 → 全部通过）

| 测试 | 处理 |
|---|---|
| `test_corridor_async_result_matches_sync_semantics` | **产品修复**：同步入口始终归一化 corridor policy（见 §三.1） |
| `test_p14_protected_files_are_byte_identical` | 按 Round38 surface-aware 变更（8c32897 / ddd2f9a）显式重建 2 个基线哈希，并写入版本说明 |
| `test_open_existing_project_switches_state_and_restores_sources` | 期望状态改走同一加载/迁移路径；忽略 `_` 前缀只读派生缓存 |
| `test_building_mapping_is_level_independent_when_footprints_are_configured` | 精确键集合改为 required-subset，显式登记 5 个新增分配/覆盖字段 |
| `test_workflow_calls_replaceable_model_and_persists_then_stales_risk` | 按 B5X 压缩契约比较摘要，并单独断言重开后的 cells 水合 |
| `test_profile_ground_exposure_matches_a_direct_canonical_integral` | 按候选实际 `grid_path` 逐格对比，修复起点格未进入航线时的 KeyError |
| `test_navigation_rtk_lost_with_adequate_gnss_*` | 改为断言 2.5 语义：`navigation_state=acceptable_degraded`、验收 `unknown`（缺降级时长证据） |
| `test_navigation_rtk_lost_with_insufficient_gnss_accuracy_*` | 保留 `unacceptable`；`exceeds_limit` 在缺时长证据时必须为 `None` |
| `test_navigation_rtk_loss_from_route_service_evidence_*` | 保留结构推导；验收结论保持 `unknown` |
| `test_protection_corridor_uses_explicit_parameters` | 断言固定 1000 m 带宽（半宽 500）与 `D_protection=410` 解耦，`outer_half_width_m=None` |
| `test_route_planner_v3_continuous.py` 7 项建筑用例 | 夹具补 `height_status=confirmed` + `height_evidence_level=verified`；合成证据适配器显式透传证据等级 |

### 2.2 Node（6 项 → 全部通过）

| 测试 | 处理 |
|---|---|
| `frontend_modules` P7–P12 文案 | 改为断言当前 canonical 必需性文案 |
| `frontend_modules` layered bind / legacy λ | 测试桩区分 `panelError(message,'hint')` 与 error；保留 evaluate-real 单次 POST 断言 |
| `frontend_modules` candidate metrics | 改为断言模型值 + 当前展示标签“父节点重连捷径” |
| `route_risk_profile_frontend` | 桩补 `paint:()=>{}` |
| `cns_service_round3_frontend` | 复位路径数 2→3（同项目 / 常规打开 / 新建项目） |

### 2.3 定向结果

- Python：**237 passed**（7 个历史文件 + D4 契约 + 走廊定向）。
- Node：`frontend_modules` 84/84、`route_risk_profile_frontend` 16/16、
  `cns_service_round3_frontend` 48/48。
- 未放宽：fail-closed、N unknown、建筑高度证据门禁、Theta* 生产 45° 约束均未修改。

---

## 三、产品缺陷与最小修复

### 3.1 sync/async corridor `input_fingerprint` 不一致

根因：请求未显式携带 policy 时，同步 `_assemble_inputs` 直接消费历史
`state["cns_corridor_policy"]` 的旧 shape；异步快照始终
`normalize_cns_corridor_policy`。旧 shape 缺 `width_semantics` /
`horizontal_width_editable` / `legacy_stale_route_ids`，于是同一输入得到两个指纹。

修复：`cns_planner/application/corridor_service.py::_assemble_inputs` 对
“请求 policy 或 state 回落”统一归一化。归一化幂等，显式 policy 行为不变。

回归：`test_corridor_sync_freezes_the_same_normalized_policy_as_async`，
断言同步/异步冻结的 policy 完全一致，且固定契约字段生效。

### 3.2 D4 既有航路风险画像在集成分支不可运行

根因：R7-D13 集成了 `operational_route_profile.py`（会传
`connector_semantics=`），但未带入 D4 对 `risk/route_profile.py` 的 6 行适配；
真实复验时 `_route_risk_segment() got an unexpected keyword argument`。

修复：`_route_risk_segment` 接受可选 `connector_semantics`，默认仍为
`SEGMENT_CONNECTOR_SEMANTICS`；候选路径行为逐字不变。

回归：`test_existing_route_risk_segments_use_operational_connector_semantics`，
输入 grid + grid_risk_v2 时产生 segments，且 provenance 使用
`operational_polyline_sampled_segments_no_cell_center_connector`。

---

## 四、真实 R0005 工作副本

| 项 | 值 |
|---|---|
| 冻结源 | `D:/Projects/_cns_acceptance/r7d17/source/project_state.json` |
| 冻结 revision / SHA | 524 / `cbb09d87dcf8287fa37f4be8a5bbd7e2ae65dca7a08b4bad12243d7d8c48c536` |
| 本轮独立副本 | `D:/Projects/_cns_acceptance/r7d18/R0005-rev524/` |
| 当前 revision / SHA | 525 / `10ba5ca14f459c34bdead9b33d3561ccbce9e20e506d5821c26dc1784b74fe42` |
| 当前字节数 | 288,385,599（建议重新评估后按 B5X 压缩保存） |
| 项目 | `zhoushan_screenshot_01_20261001_1647`（`9e349d0f…`） |
| 服务 | 独立 `127.0.0.1:8795`；未启动/停止 8765/8776 |

冻结副本只读；所有变更只发生在 r7d18 独立副本。

---

## 五、Step01–04 状态

### Step01 数据准备：**current**

- `workspace=passed`、8,008 格；`source_audits=passed`、12 项，**0 项
  `invalid_source_path`**。
- 设备资料库：原配置指向“设备性能”目录（DOCX/PDF/XLSX），来源审计按“必须是
  具体文件”报 `invalid_source_path`。仓库内
  `cns_planner/config/equipment_reference_catalog.json` 是该来源的**已登记**
  canonical catalog（其 `registered_sources` 逐项列出目录中的 9 个文件），
  因此只把副本配置改为该 JSON（并复制进副本 `derived/`），未猜测其它路径。
  当前状态 `configured_unverified`（显式校验待做），不再是非法路径。

### Step02 环境与风险：**stale**

- `grid=passed`；`grid_risk=stale`（1.1）；`grid_risk_v2=pending_confirmation`
  （2.0，policy 未确认）；`environment_risk=stale`。
- PCF ALT-100：`completed_with_warnings`，指纹
  `pcf-af35826ef22b0a6025258d666a99d1cf4df43241a2683a46981388993bcf193c`，
  8,008 格：pass 7,115 / blocked 760（terrain 758、building 28）/ unknown 133；
  `stale_reason=null`。**不需要重算**：既有航路 D4 复验不消费 PCF cells；
  其明细 artifact 目前不可寻址（`artifact_ref=null`），如实保留为“明细不可用”。

### Step03 航路规划与发布：**stale**

- 高度层：`ALT-100` 已在 planning request 中显式确认（`source=screenshot_run_explicit_100m`），
  readiness 的 `selected_altitude_layer_id=ALT-100`。
- 运行航路：`R0005`，14 顶点，`status=passed`；provenance 明确
  `strict_safety_validation_passed=false`、`operational_authorization=false`、
  `engineering_conditional_acceptance=true`、`use_scope=engineering_demo_only`。
- D4 既有航路复验（`operational-route-clearance-v2`，只读）：
  - 总状态 **`unknown`**；
  - 地形 **passed**，最小净空 22.760002 m；
  - 铁塔 **passed**；
  - 建筑 **unresolved 1**（`4tCa7`，96.507356 m，仅 predicted 高度证据）；
  - 风险 **unknown / `risk_index_partial`**：ground partial（coverage 0.949183，
    unresolved 477.901 m）、air_traffic unresolved、environment_obstacle resolved；
  - `research_only=true`、`formal_admission_evaluated=false`、state 未改。
- **未重新搜索 Theta***；candidate / risk profile / validation / adoption 链仍
  `stale`（candidate 0、risk profile 9、validation 20、adoption 9）。
- 结论：Step03 **未**被提升为 current；既有航路只得到真实验证证据，且证据不足
  使其保持 `unknown`。这是 fail-closed 的正确结果。

### Step04 CNS 需求：**current（建议）/ historical（采纳）**

- `required_cns=passed`。
- 建议：用生产服务 `evaluate_required_cns_recommendation` 重新评估；
  `status=recommendation_ready`、`result_status=passed`、
  `input_fingerprint=71e4bbd2cf1207f319a2c91a593c774b991b0379da89acd60239064fac3758b5`、
  `proposal_only=true`、`requires_user_adoption=true`、无冲突/缺证。
- 采纳：历史记录 `status=superseded`，`recommendation_fingerprint=199869b6…`、
  `required_cns_fingerprint=d8ecf86b…`，与当前 `required_cns` 指纹
  `7d94225a…` 不一致。**未自动 adopt，也未篡改采纳状态**。
- 解释：建议身份已恢复为当前 proposal；采纳仍如实保持历史，只有用户显式
  Adopt 才会成为 current。

---

## 六、P14/P15 只读核对（业务 failed ≠ 计算 stale）

| 容器 | 状态 | 算法版本 | 指纹 | 结论 |
|---|---|---|---|---|
| P14 服务走廊 | failed | 1.4 / 当前 1.4 | `b070276b…`（与冻结一致） | **业务失败，不是 stale**；可复用证据，不得当 current |
| P15 能力缺口 | failed | 1.3 / 当前 1.3 | `952103e3…`（与冻结一致） | 同上 |

- C：2727 确认缺口（satisfied 0 / confirmed_deficit 2727 / unknown 0）。
- S：2727 确认缺口；N：2727 **unknown**，不是通过。
- 最长连续缺口 9404.937615 m。
- P16：`proposal_ready`（2.3），canonical `selected_actions=0`；恢复证据容器
  13 项历史动作全部未确认安装，本轮未应用。
- 连续服务：存储 2.2 / 当前代码 2.5 → **计算 stale**，需按 2.5 重算；
  与 P14/P15 的“业务 failed”严格区分。

---

## 七、剩余问题与 Step05–06 最小任务

### 7.1 仍存在的真实问题

1. `grid_risk_v2` 仍 `pending_confirmation`：风险策略未确认，air_traffic 指数缺失；
   这是既有航路风险 `unknown` 的直接原因。
2. 建筑 `4tCa7` 只有 predicted 高度证据；96.507 m 的数值裕度不构成正式安全结论。
3. R0005 是工程演示条件接受航路：`strict_safety_validation_passed=false`、
   `operational_authorization=false`。
4. P14/P15 业务失败：C/S 2727 缺口、N 2727 unknown。
5. P16 仅提案，13 项历史动作未确认安装；连续服务 2.2 vs 2.5 stale。
6. Step06 方案审查 stale、方案未确认、报告未计算、P17 未计算。

### 7.2 Step05–06 必须计算的最小任务（建议，不在本轮执行）

1. 由人工确认 `risk_policy_v2` 的聚合方法与权重；只重算 GridRiskV2 一次。
2. 按 2.5 只重算连续服务一次（P14/P15 仍是 failed，不因重算变 pass）。
3. 若正式链要求，先以内容寻址核对 P14 `1c46ac…`；匹配则恢复，不匹配才重算
   P14→P15 一次。
4. P16 仅在 C/S 仍有确认缺口且用户显式确认后生成/应用；13 项历史动作继续按
   unverified 对待。
5. P17 只在连续服务 current 后计算；Step06 再走方案审查 → 确认 → 应用 → 报告。
6. 需要正式 PASS 时，必须先补 `4tCa7` 的 survey/verified 高度证据与风险策略；
   不得用 predicted 高度或部分风险指数冒充通过。

---

## 八、验证命令与结果

```powershell
D:\tools\anaconda\python.exe -m pytest -q --tb=short -p no:cacheprovider `
  tests/test_phase4_b6x_heavy_task_runtime.py `
  tests/test_project_persistence_characterization.py `
  tests/test_building_level_independent_facts.py `
  tests/test_risk_model.py `
  tests/test_risk_v2_canonical_integration.py `
  tests/test_continuous_service_acceptability.py `
  tests/test_route_planner_v3_continuous.py `
  tests/test_operational_route_clearance.py `
  tests/test_cns_corridor.py
# 237 passed

node tests/frontend_modules.test.mjs          # 84/84
node tests/route_risk_profile_frontend.test.mjs  # 16/16
node tests/cns_service_round3_frontend.test.mjs   # 48/48
```

其他：`git diff --check` 0 错误；改动 Python `py_compile` 通过；
`tools/r7d18_route_revalidation.py` 由 QGIS Python 真实运行（FABDEM + 建筑 GPKG +
铁塔 profile），state 前后 SHA 一致。

---

## 九、证据文件

| 文件 | 内容 |
|---|---|
| `step_status.json` | Step01–04 状态、PCF、D4 复验、P14/P15/P16/P17、C/N/S |
| `existing_route_validation.json` | D4 复验身份摘要（不含真实坐标；完整证据在验收副本） |
| `existing_route_validation_summary.json` | 精简结果 |
| `tools/r7d18_route_revalidation.py` | 可重放的 D4 真实复验入口 |
| `tools/r7d18_audit_steps.py` | 可重放的 Step01–04 只读审计 |
| 验收副本 `D:/Projects/_cns_acceptance/r7d18/d4_existing_route_validation.json` | 完整证据（含真实坐标，不入 Git） |

---

## 十、提交、发布与边界

- 本地 commit 仅包含本轮 D18 源码/测试/工具/文档；未合并 `main`、未
  reset/clean、未应用 13 项历史 P16 设施、未改正式 `ProjectState`。
- 工作树中另有 6 个与本轮无关的影像预算改动（`imagery_budget.py` 等）在
  本轮期间出现；按“保留工作区现有改动”原则未回滚，也**未纳入本轮提交**。
- GitHub 同步：按 R7-D17 已通过的公开发布规则（白名单、禁用路径/数据、密钥扫描、
  BOM/大文件检查）执行后推送既有开发分支；不合并 `main`。
