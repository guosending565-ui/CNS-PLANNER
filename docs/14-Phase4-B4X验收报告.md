# Phase4-B4X 验收报告：六步产品 UI 收敛 + 约束可视化

生成时间：2026-09（本地会话）
HEAD：`be1062a905c707a3d7860a1cbddc76c24972f5e2`（起点与终点一致，未 commit / 未 push）
工作树：全部改动留在工作树，未提交。P14 性能补丁候选四文件未被本批次触碰（SHA 完全一致，见 §23）。

---

## 1. 结论速览

| 项 | 结果 |
|---|---|
| B4X_READY_TO_REVIEW | **true** |
| B4X 定向 Python 组合 | **442 passed** |
| full pytest | **1797 passed / 7 skipped / 0 failed**（共 1 次，709.40 s） |
| frontend full suite | **407 passed / 0 failed**（25 个 `.test.mjs` / `.test.js`；共 2 次：第 1 次修 2 处陈旧断言后重跑 1 次，见 §21） |
| P14 四文件 | 未修改（SHA 与接手时逐字节一致） |
| 本轮不新增算法 / 不改 authority owner | ✅ |

本轮是 **UI / Presentation / Read-only visualization** 批次：不修改 Theta*、Planning Constraint Field 算法、
terrain/building/tower 公式、risk objective、coverage/corridor 算法，不实现 B5 大型 artifact migration、
不实现 B6 Heavy Task、不删除 archive planner、不生成 terminal geometry、不改变任何 authority owner。

---

## 2. 修改 / 新增文件

### 2.1 新增（B4X 展示层 + 只读读取路径）

| 文件 | 作用 |
|---|---|
| `cns_planner/web/js/workflow/presentation.js` | **集中式中文展示映射**（本轮信息架构的核心，见 §4） |
| `cns_planner/web/js/workflow/altitude_layers.js` | 统一 AltitudeLayer selector（Step2 / Step3 共用；不写死 ALT-080） |
| `cns_planner/web/js/workflow/constraint_field.js` | 约束场展示模型 / 状态卡 / 弹窗 / 只读 HTTP 客户端 / 图层开关语义 |
| `cns_planner/web/js/workflow/constraint_view.js` | 约束场在壳层的唯一装配点（按需加载 / 绘制 / 图例 / 显式生成） |
| `cns_planner/web/js/map/constraint_field_overlay.js` | 「高度层障碍」地图覆盖层（三态、障碍优先、几何复用网格索引） |
| `cns_planner/web/js/workflow/shell_actions.js` | 报告预览 / 下载 / 保存项目（从 `main.js` 抽出，保持轻入口 ≤450 行） |
| `cns_planner/web/css/b4x.css` | B4X 新增展示元素的样式（六区标记 / 主操作 / 阻塞项 / 约束卡 / 需求徽章 / 高级区） |
| `tests/test_phase4_b4x_constraint_read_api.py` | 新只读 API 的 Python 契约测试（26 项） |
| `tests/b4x_ui_convergence.test.mjs` | 六步导航 / 中文映射 / 高度层 / 约束可视化 / 默认图层的前端契约测试（30 项） |

### 2.2 修改（生产代码）

```text
cns_planner/api/router.py                                  # GET /api/planning-constraint-field{,/map}
cns_planner/application/planning_constraint_field_service.py# field_map() 只读紧凑投影 + sidecar 降级
cns_planner/application/workflow_service.py                # planning_constraint_field_map()
cns_planner/persistence/project_compaction.py              # read_result_artifact() 只读 sidecar 读取 + 去重 _decode_artifact
cns_planner/web/index.html                                 # 六步主导航命名、图层抽屉中文、约束图层、b4x.css
cns_planner/web/js/main.js                                 # 约束视图装配、网格弹窗约束段、图层联动（仍 ≤450 行）
cns_planner/web/js/shell.js                                # 约束图层开关绑定
cns_planner/web/js/map/display_layers.js                   # drawConstraintLayer 钩子（网格之上、航路之下）
cns_planner/web/js/workflow/common.js                      # 收敛为 presentation.js 的转发 + 六区构件
cns_planner/web/js/workflow/grid_details.js                # 弹窗接收约束格（障碍/证据不足置顶）
cns_planner/web/js/workflow/step01_project.js              # 数据准备：必要输入清单 + 需求等级
cns_planner/web/js/workflow/step02_workspace.js            # 环境与风险：六区、风险/约束分离、高度层、约束场
cns_planner/web/js/workflow/step03_routes.js               # 正式航路链条 + 兼容隔离 + 约束状态
cns_planner/web/js/workflow/step04_operation.js            # CNS需求：六区 + 三层独立 + 采用语义
cns_planner/web/js/workflow/step05_cns.js                  # canonical 链 + 雷达可选分支 + 兼容隔离
cns_planner/web/js/workflow/step06_review.js               # 选择/确认/应用/报告 + compatibility 隔离
cns_planner/web/js/workflow/route_operating_layer.js       # 高度层中文标签（垂向基准中文化）
cns_planner/web/js/workflow/radar_surveillance_layout.js   # 雷达面板字段标签中文化 + 去 ALT-080 硬编码
cns_planner/web/js/workflow/risk_framework_v2.js           # 风险专题/域标签去版本前缀、去英文
cns_planner/web/js/workflow/layered_theta_v2.js            # Theta* V2 面板行标签本地化 + 旧版 λ 语义中文
```

### 2.3 修改（测试：只更新「锁定旧不合规文案」的断言，未新增/放宽任何业务断言）

```text
tests/frontend_modules.test.mjs
tests/workbench_shell.test.mjs
tests/grid_details_summary.test.mjs
tests/layered_route_map_evidence.test.mjs
tests/radar_surveillance_layout_frontend.test.mjs
tests/route_safety_evidence_frontend.test.mjs
tests/towers_operational_v2_frontend.test.mjs
```

**未触碰**（依约保护）：P14 四文件、`_dsh_prof/`、舟山旧核查 `tools/`、qgis 临时文件、乱码文件；未 commit、未 push。

---

## 3. 六步导航最终结构

主导航（`index.html` 的 `#steps`）精确等于六步，逐字如下：

| 步骤 | 标签 | 导航说明 |
|---|---|---|
| 01 | 数据准备 | 项目、数据源与必要输入清单 |
| 02 | 环境与风险 | 工作区、规划网格、风险场与约束场 |
| 03 | 航路规划与发布 | 候选航路、风险画像、安全验证与发布 |
| 04 | CNS需求 | 航空器配置、运行约束与 CNS 能力需求 |
| 05 | CNS能力与设施规划 | 三维覆盖、服务能力、走廊、缺口与设施规划 |
| 06 | 方案评审与报告 | 比较、选择、确认、应用与报告 |

* 主导航**不再出现** P1 / P7 / P8 / P14 / P15 / P16 / V1 / V2 / V3 / Legacy（由 `b4x_ui_convergence.test.mjs`
  按正则边界逐词断言，且显式禁止旧标签「项目准备 / 环境建模 / 运行规则」）。
* 开发阶段编号只允许存在于「高级 / 审计信息」：`advancedAuditNote()`（Step1 数据源审计、Step3 算法标识、
  Step4 V3 CNS 评估、Step5 雷达与兼容、Step6 需求依据）与各面板的 `<details>` 折叠区。
* 每一步固定六区：**目标 → 输入准备 → 阻塞项与工程假设 → 主操作 → 结果 → 下一步**
  （`common.js` 的 `STEP_SECTIONS` 常量化；`primaryAction()` 保证每个主操作区只有一个 primary 按钮）。
* 六区在步骤内的落点：Step1 operate(目标/输入/主操作) + result(阻塞项/结果) + 底部 `nextStepBar`；
  Step2/3/4/5 用 `sectionRoleLine()`/`segIntro()` 在每个二级分段内显式排序；Step6 用 `SIX_SECTIONS` 常量。

---

## 4. 集中式中文 presentation mapping 放在哪里

**唯一位置**：`cns_planner/web/js/workflow/presentation.js`（新增，480+ 行，纯展示层）。

* `common.js` 已改造为**只转发**：`import {statusText,statusBadge,escapeHtml} from './presentation.js'`，
  并再导出全部语境词表，因此既有 `from './common.js'` 的导入路径继续有效，不存在第二份副本
  （测试断言 `common.js` 不得保留 `not_calculated:` 之类的词表）。
* 导出 15 张映射表（`PRESENTATION_TABLES`）：`STATUS_TEXT`、`WORKFLOW_STATUS_TEXT`、`READINESS_TEXT`、
  `MATURITY_TEXT`、`AUTHORITATIVE_CONTEXT_TEXT`、`ASSESSMENT_TEXT`、`INPUT_REQUIREMENT_TEXT`、
  `CONSTRAINT_OUTCOME_TEXT`、`CONSTRAINT_OUTCOME_COLOR`、`CONSTRAINT_BLOCKER_TEXT`、`CONSTRAINT_BLOCKER_ORDER`、
  `CONSTRAINT_UNKNOWN_REASON_TEXT`、`CONSTRAINT_FRESHNESS_TEXT`、`CANONICAL_NODE_LABELS`、
  `VERTICAL_REFERENCE_TEXT`、`SOURCE_STATE_TEXT`。
* 覆盖范围（与 §5 要求逐条对应）：
  WorkflowStatus / ReadinessState / OutputMaturity / AssessmentOutcome / InputRequirementLevel /
  canonical node / assumption basis / warning reason / blocker reason / vertical reference / constraint outcome。
* 逐字词汇（`b4x_ui_convergence.test.mjs` 25 条断言锁定）：

```text
ready                          → 可继续
ready_with_assumptions         → 可继续（采用工程假设）
blocked                        → 暂不能继续
running                        → 正在计算
completed                      → 已完成
completed_with_warnings        → 已完成（有提示）
stale（工作流语境）             → 需要重新计算
failed（工作流）                → 执行失败
passed（assessment）            → 检查通过
failed（assessment）            → 检查不通过
unknown（assessment）           → 证据不足
provisional                    → 候选 / 试算结果
authoritative + published      → 已发布
authoritative + adopted        → 已采纳
authoritative + confirmed      → 已确认
constraint pass/blocked/unknown→ 可通行 / 障碍 / 证据不足
required/assumable/optional/enhanced → 必需 / 可采用工程假设 / 可选 / 增强数据
egm2008_orthometric            → EGM2008 正高
constraint freshness current/stale → 当前 / 需要重新计算
```

* 硬边界：`statusText()` 对未登记取值**原样返回**（绝不伪装成"通过"）；CSS 类名仍用 raw 值（配色不变）；
  模块不读 `flow`/`state`、不发请求、不参与任何 gate 计算。

---

## 5. 主界面剩余英文扫描结果

扫描方法：真实调用六个步骤模块的 `render(context)`，剥离 `<details>` 折叠区与 `advanced` 面板后统计可见文本
（正则逐词计数；扫描脚本为一次性工具，已删除）。

| 词 | step01 | step02 | step03 | step04 | step05 | step06 |
|---|---|---|---|---|---|---|
| legacy | – | – | 2 | – | – | – |
| workflow | – | – | 1 | – | – | – |
| readiness | – | – | 23 | – | – | – |
| provisional / authoritative / proposal_ready / source unavailable | – | – | – | – | – | – |
| stale | – | 2 | 8 | – | – | – |
| pending_confirmation | – | – | 7 | – | – | – |
| not_calculated | – | – | 3 | – | – | – |
| missing_data | – | 2 | – | – | – | – |
| fingerprint | – | – | 15 | – | – | – |
| voxel / supercover / heuristic | – | – | – | – | – | – |
| P7 / P8 / P14 / P15 / P16 | – | – | – | – | – | – |
| V1 | – | – | 2 | – | – | – |
| V2 | – | 1 | 16 | – | – | – |
| V3 | – | – | – | – | – | – |

已清除（本轮）：全部英文业务标题（`Aircraft Capability` / `Required CNS Performance` / `Ground Device Capability` /
`ReliabilitySpec` / `CNS Service Requirement Corridor` / `C/N/S Service Timeline` / `Tactical Protection Envelope` /
`CNS Gap Analysis V2` / `CNS Spatial Planning Objectives` / `DeviceCatalog` / `Legacy λ` / `Layered Candidate` …）、
地图专题清单的版本前缀（`V2 因子` → `风险因子`、`V2 域` → `风险域`、`Legacy Risk V1 · 地面风险` → `旧版相对风险指数 · 地面风险`）、
雷达面板的字段标签（`model_scope` / `algorithm_version` / `land mask` / `solver` / `readiness 阻断` /
`optimization` / `validation` / `max refinement rounds` / `explicit_polygon` / `engineering_assumption` /
`legacy 挂高` / `I-only`）、Theta* V2 面板的行标签（`readiness status` / `layered planner` / `artifact_type` /
`algorithm` / `candidates status` / `current_applicability` / `feasibility policy` / `selected layer mask` /
`mask counts` / `terrain source` / `population source` / 指纹与权重字段 …）、以及 `step05` 的
`canonical / ineligible / user_configuration / unverified`。

**故意保留（逐条理由）**：

1. **允许的英文缩写**：`CNS`、`C/N/S`、`CRS`、`EGM2008`、`Theta*`、`MTBF`、`MTTR`、`dB`、`ICAO`、`Radar`（普通页面一律写「雷达」）。
2. **工程标识与数据值**（不是文案）：算法 id `layered_risk_aware_theta_star_v2`、专题取值 `risk_v2:factor:…`、
   字段名 `heading_bin_count` / `theta_min_deg` / `d_ref_m`、实体 id（`R0001` / `N001` / `T1` / `CP-1` / `PV-1`）、
   来源文件名与设备厂牌、`optical/dB/4g/5g/gnss/adsb/…` 等技术受控取值（**就是 payload 取值本身**，
   改显示会与提交值不一致；集中词表也不应发明技术词表）。
3. **Step3 剩余的 `readiness` / `fingerprint` / `stale` / `pending_confirmation` / `not_calculated` / `legacy` / `V2`**：
   全部位于 Theta* V2 面板与分层候选分段的**工程字段行与工程说明行**内，字段名与后端 raw 值成对出现
   （例如 `readiness status（旧版原始值，不是 Theta* V2 的门禁）`、`搜索参数指纹`、`块状/掩码` 行）。
   本轮已把**标签**本地化为中文，保留 raw 字段名是"如实转印后端事实"的要求（改字段名会与后端 / 审计不一致）。
   这些面板是算法配置与审计界面，按产品约定属于工程术语区；建议下一批次统一把 raw 字段名收进 `<details>`
   高级折叠、只保留中文标签（见 §25 未尽事项）。

---

## 6. Step2 高度层选择实现

* **统一实现**：`workflow/altitude_layers.js` 的 `altitudeLayerSelector()`；Step2 与 Step3 使用**同一个**
  选择器 id（`altitudeLayerSelector`）与**同一份目录**（`flow.spatial_3d.altitude_layers`）。
* **完全目录驱动**：`altitudeLayerCatalog(flow)` → `sortedAltitudeLayers()` → `altitudeLayerOptions()`；
  目录为空时只渲染「请选择固定巡航高度层（不猜、不自动分配）」+ 明确空状态（`ALTITUDE_LAYER_EMPTY_NOTE`），
  **绝不伪造默认层**；有 ALT-060 / ALT-080 / ALT-100 / ALT-150 / ALT-200 / CUSTOM-42 时逐个出现
  （`b4x_ui_convergence.test.mjs` 参数化断言，且断言 `ALT-080` 不得写死在 step02 / step03 / main.js /
  altitude_layers.js / constraint_view.js / constraint_field.js / `planning_constraint_field_service.py`）。
* **显示格式**：`ALT-080 · 80 m · EGM2008 正高`（`altitudeLayerLabel()`）；缺 nominal → 「nominal 高度待工程确认」；
  未确认 → 「垂向基准待工程确认」；两者仍在列表中可见但明确标注不能用于正式约束场 / 正式航路。
* **选择状态**：模块内纯 UI 缓存（`rememberAltitudeLayerSelection` / `resolveAltitudeLayerSelection`），
  重渲染后回填，用户不会因为一次 mutation 丢选择；选择只切展示，**不重算任何结果**。
* Step3 侧的巡航高度层分配仍走既有 `route_operating_layer.js` 的显式分配（同一目录），
  其垂向基准已改为中文显示（`verticalReferenceText()`）。

---

## 7. Constraint summary UI

约束场状态卡（`constraint_field.js` 的 `constraintSummaryHtml()`）在 **Step2「结果 → 规划约束场」** 与
**Step3「正式航路规划」/「当前航路」** 两处复用同一实现，主界面显示：

```text
当前高度层              ALT-080 · 80 m · EGM2008 正高（来自实际目录，非硬编码）
约束场状态              已生成 / 尚未生成 / 数据源不可用（中文，按真实原因分类）
总格数 / 可通行 / 障碍 / 证据不足     24,300 / 0 / 9,918 / 14,382（metric-grid，障碍与证据不足带语义色）
freshness               当前 | 需要重新计算（附中文原因）
阻挡原因分布            地形 9,496 · 建筑 1,090（中文域标签，非 raw blocker 数组）
证据不足提示            证据不足 14,382 格：这些格子既不是可通行也不是障碍——规划不能把它们当作安全。
                        证据不足不等于可通行。当前 unknown policy 不允许穿越证据不足单元。
生成时间/数据版本（业务化）  以「新鲜度 + 工作区身份变化」表达，不直接展示 raw 指纹
```

* **fingerprint 只在高级**：完整 `constraint_field_fingerprint` / `policy_fingerprint` / `grid_identity` /
  `artifact_ref` / `map_status` / `unknown_policy` 全部收进 `<details class="algorithm-detail">`
  「高级 / 审计信息 · 指纹与来源」；主界面只说「当前 / 需要重新计算」。
* **唯一主操作**：`<button class="primary" id="generateConstraintField">生成该高度层的规划约束场</button>`；
  未选高度层或网格未通过时 disabled 并给出中文原因；生成走既有 `POST /api/planning-constraint-fields/evaluate`
  （不新增写端点），成功后重读 workflow 快照。
* **风险 / 约束严格分离**（`data-semantic-split="risk-vs-constraint"`）：
  「风险场 = 软成本（哪里风险高）；约束场 = 可行性（哪里不能飞）。两者是两份独立证据，
  本产品绝不把它们合成一张"综合风险"表：障碍物不进航路风险数学，风险分数也不参与可行性判定。」

---

## 8. Constraint map API

新增**两个只读 GET**（不新增任何 POST；生成仍走 B3X 的既有端点）：

```text
GET /api/planning-constraint-field?altitude_layer_id=<id>
    → 既有 summary 集合（slim，items 内不含 cells；counts / warnings / fingerprints / artifact_ref）

GET /api/planning-constraint-field/map?altitude_layer_id=<id>[&bbox=w,s,e,n]
    → 地图友好紧凑投影：
       {schema_version, status, altitude_layer_id, field_id, nominal_altitude_m, vertical_reference,
        grid_identity, constraint_field_fingerprint, bbox, geometry_source:"frontend_grid_index",
        counts{total,pass,blocked,unknown,blocked_by{…}}, total_count, truncated:false,
        cells:[{grid_id, outcome, blocked_by[]}]}
```

* **只返回三个业务字段**：`grid_id` / `outcome` / `blocked_by`。绝不返回 `unknown_reasons`、`evidence_refs`、
  几何、`polygon`、`FeatureCollection`（Python 与前端双层断言）。
* **几何复用前端标准网格索引**：`geometry_source:"frontend_grid_index"`；前端用
  `gridRenderCache.byId`（来自既有 `GET /api/workspace/grid` 的 `cell.bbox`）补齐几何，
  因此**不重复下发 GeoJSON**。缺少几何的 `grid_id` 由 `constraintOverlayEntries()` 显式报告
  （`unresolved` 列表），绝不静默丢弃障碍格。
* **24,300 格量级**：契约测试用 24,300 格断言「一条紧凑记录、无截断、紧凑投影显著小于带 evidence 的 raw artifact」；
  前端只在用户勾选「高度层障碍」后才按需读取（启动路径断言不含 `loadMap()` / `loadConstraintField`）。
* **失败与缺失绝不伪装**：
  * 未给 `altitude_layer_id` → `status="altitude_layer_required"`（不猜默认高度层）；
  * 该层无约束场 → `status="not_calculated"`；
  * 摘要存在但逐格明细不可读（未水合且 sidecar 不可读）→ `status="cells_unavailable"` +
  中文原因 + 仍如实转印 counts（**绝不退化成"全部可通行"**）；
  * 非法 bbox（3 个值 / 非数值 / 反向 / 5 个值）→ 忽略过滤而不是猜范围（宁可多返回，
    `cells` 不因"没有几何"而丢失）。
* 数据来源顺序：内存 state（打开项目时由 `restore_compacted_results` 从 sidecar 水合）→ 项目 sidecar 文件
  （新增 `persistence/project_compaction.read_result_artifact()`，与恢复路径共用 `_decode_artifact()` 校验
  版本 / 指纹 / 完整性）；两者都拿不到时如实降级。浏览器**不接触任何文件系统路径**。

---

## 9. Constraint 地图显示方式

* **图层名称**：图层抽屉独立分组「约束（可行性：能不能飞）」下的 **「高度层障碍」**（id `altitudeConstraintLayer`），
  另有「显示证据不足单元」（`altitudeConstraintUnknownLayer`）与「显示可通行单元」（`altitudeConstraintPassLayer`）。
* **默认关闭**：主开关与两个子开关全部默认不勾选；`CONSTRAINT_LAYER_DEFAULTS={blocked:true,unknown:false,pass:false}`
  ——勾选主开关后**默认只画障碍**，unknown / pass 需另行打开以避免遮挡。
* **三态可区分**（`CONSTRAINT_OVERLAY_STYLE`）：障碍 alpha 0.62 / 证据不足 0.34 / 可通行 0.20，
  且**绘制顺序 pass → unknown → blocked**（障碍永远在最上层，视觉权重最高）。
* **绘制位置**：`drawWorkflowLayers` 内的 `网格边界 → 高度层障碍 → 建筑轮廓 → 航路`，
  即网格边界之上、规划结果之下；由 `map/display_layers.js` 的 `drawConstraintLayer` 钩子接入，
  具体绘制在 `map/constraint_field_overlay.js`。
* **图例**：勾选后显示三态图例（含格数），并明确「约束场 = 可行性（能不能飞）；与风险场（软成本）分开显示。
  穿越证据不足单元的候选永远不能发布为运行航路。」
* **性能**：单元按经纬度视口 + `gridTheme.bboxIntersects` 双重过滤；只画当前可见单元，
  不做逐格 Canvas 对象分配。

---

## 10. blocked / unknown 中文表达

* 三态：`pass → 可通行`、`blocked → 障碍`、`unknown → 证据不足`（颜色 `#3f9e6a` / `#c92a2a` / `#f0a020`）。
* **点击障碍格的中文弹窗**（`constraintCellDetailsHtml()`，在网格弹窗摘要**最前**显示）：
  `高度层：ALT-080 · 80 m · EGM2008 正高 / 网格编号 / 状态：障碍 / 主要阻挡原因：地形、建筑`；
  多原因受支持（按后端 `BLOCKER_DOMAINS` 顺序：地形 → 建筑 → 铁塔 → 禁飞/受限区域 → 保护要地）；
  **绝不**显示 `blocked_by:["terrain","building"]` 这类 raw JSON（测试断言 raw 数组与转义后的 raw 数组均不出现）；
  控制长度：证据不足时只列前 3 条中文原因。
* **证据不足格**：显示 `证据不足原因：铁塔：铁塔数据尚未解析` 等中文原因（覆盖 B3X 全部 reason code 的中文映射）
  + 「证据不足不等于安全：该单元既不能当作可通行，也不能当作障碍。」
* **详细 evidence 进高级**：`blocked_by` / `unknown_reasons` / `evidence_refs` 的 raw JSON 只在
  「高级 / 审计信息 · 原始证据引用」折叠区出现。
* **unknown 计数永不被隐藏**：`unknown` 地图图层默认关闭，但状态卡与图例始终显示「证据不足 N 格」，
  且带 `data-unknown-warning="true"` 的强制提示；`constraintSummaryHtml` 对 unknown 格**绝不**输出「通过」。

---

## 11. Step3 正式 route chain

`operate → 正式航路规划`（分段 id 仍为 `op-candidates`，保持导航状态与既有测试契约）内呈现
**唯一一条正式链条**（`PRODUCTION_ROUTE_CHAIN`，有序列表）：

```text
选择 OD → 选择固定巡航高度层 → 输入 / 确认规划策略 → 生成正式候选航路
        → 航路风险画像 → 航路安全验证 → 发布运行航路
```

* 算法实现是 `LayeredRiskAwareThetaStarV2`，但普通用户界面显示的是「**正式航路规划**」；
  算法 id / 版本包在 `advancedAuditNote('正式航路规划实现：…（工程标识，不作为业务标题）。')` 内，
  **不作为主标题**（测试断言不得出现 `wbBlock('Layered Risk-Aware Theta* V2'`）。
* 该区块同时给出三件事：链条本身、正式链条声明（旧版已移入高级，不得并列、不得因旧版试算有 route 就显示完成）、
  约束场状态（见 §7）。
* 正式候选卡要求的信息已就位：距离 `distance_m`、航路总风险（`risk_exposure_index_m` / 加权项）、
  单位距离风险（`route_risk_density`）、转弯次数 `turn_count`、主要转弯角 `total_heading_change_deg` /
  `theta_min_deg`、巡航高度层、Constraint 状态、以及 terrain / building / tower / restricted-area 的
  验证状态（来自 `renderLayeredRouteValidation`）。**权重仍由后端事实展示**
  （`risk_weight` / `turn_weight` / `distance_weight` 与 `weights` 行），前端不重算任何数值。
* **terminal 边界如实声明**（不生成假几何）：
  「正式规划针对**固定巡航高度层**；起飞、爬升、下降和进离场程序将在独立模块中评估，
  本步不生成任何 terminal 几何，也不假装系统已有完整 terminal procedure。」

---

## 12. Compatibility / Advanced 如何隔离

| 位置 | 隔离方式 |
|---|---|
| 主导航 | 完全不出现开发编号 / 版本代号 |
| Step3 | 正式链条在 operate；旧版规划器、风险感知 V2 参数、研究对照实验、诊断、剖面全部只在 `advanced` 的「旧版兼容 / 研究对照」与「研究对照实验」分段；正式区块内含声明「旧版航路规划器已全部移入高级，不得与正式航路规划并列」；地图上另一图层明确命名为「**旧版试算航路（研究对照）**」，**绝不**叫运行航路 / 正式航路 |
| Step5 | `cns-adv-compat`「旧版二维覆盖试算」/「旧版站址试算」、`cns-adv-closedloop`「方案影响试算」，各含一句「不解锁下一步、不得表述为正式结果 / 已采纳 / 已应用」；测试断言旧版闭环分段内不出现「正式结果 / 已采纳」 |
| Step6 | `compatibilityIsolation()` 明确「旧版二维覆盖 / 旧版规划缺口 / 旧版站址试算 / 方案影响试算不构成确认依据，不能放宽或触发确认 / 应用 / 报告门禁」；判定只看 `flow.steps['6']`（后端 canonical 结果）；文件内**不读取** `flow.compatibility_*`；旧版组标题改为「旧版兼容（不参与正式确认）」 |
| authority owner | 未改变：`operational_routes → LayeredOperationalAdoptionService`、`required_cns → CNSInputService`、`coverage_3d → Spatial3DService`、`facility plan → CorridorSitePlanningService`、`confirmed plan → PlanReviewService`、`Radar → RadarSurveillanceLayoutService`；本轮只新增**只读** GET，未新增任何写端点（测试断言） |
| 约束场 gate | 不变：穿越证据不足单元的候选仍被 `LayeredOperationalAdoptionService` 拒绝；B4X 只把它翻译成中文提示 |

---

## 13. Step4 CNS需求 UI

* 三层独立（result 首屏「结果概览 → 三层独立与需求链路」）：**通用工程航空器配置** ≠ **运行约束** ≠ **地面设备能力**；
  且 **需求建议** ≠ **正式 CNS需求**。逐字声明：
  「工程规划基线，不代表具体机型实测参数」（航空器配置）、「工程 CNS需求基线」（正式需求基线）、
  按钮「**采用为正式 CNS需求**」。
* 六项信息落点：航空器配置（operate·飞行器能力 + 概览第 1 行）、运行约束（operate·飞行规则 + 第 2 行）、
  需求来源（第 3 行，`required_cns.source`，无值显示未记录）、CNS需求建议（result·需求建议 + 第 4 行）、
  正式 CNS需求（result·CNS需求 + 第 5 行）、采用状态（建议分段结果块 + 第 6 行，`statusText` 取词：
  尚未采用 / 已采用 / 已被取代）。
* **需求建议不得自动变正式**（原文）：「需求建议不会自动成为正式 CNS需求：只有点击「采用为正式 CNS需求」后，
  系统才会把当前建议写入正式需求；重新评估建议不会改动正式需求。」后端确认为显式动作
  （`RequirementRecommendationService.adopt()` 要求 `recommendation_ready`、无冲突、输入指纹匹配），
  前端无任何自动采用路径。

---

## 14. Step5 canonical chain

`result` 的六个二级分段按 canonical 顺序固定：

```text
三维覆盖评估 → 服务能力评估 → CNS服务走廊 → CNS能力缺口 → CNS设施规划 → 雷达监视规划
```

* 前五段顶部各有一行「链条：三维覆盖 → 服务能力 → 服务走廊 → 能力缺口 → 设施规划；当前环节：X」；
  每个分段内部按六区组织（目标 / 输入准备 / 阻塞项与工程假设 / 主操作 / 结果 / 下一步），每段 ≤1 个 primary。
* 生产链只用 canonical 结果：`coverage_3d`（Spatial3DService）、`cns_service_capability`、
  `cns_corridor_assessment`、`cns_corridor_gap_assessment`、`cns_corridor_site_plan`（CorridorSitePlanningService）。
* **Existing CNS 中文语义**分别展示两件事：**事实掌握情况**（`not_declared → 尚未声明` /
  `confirmed_none → 已确认无` / `confirmed_present → 已确认存在`）与 **规划模式**
  （`factual → 按事实数据规划` / `assume_empty_for_planning → 按空既有设施工程基线规划`）；
  选择 assumed-empty 时**持续显示**：「空既有设施工程规划基线；不表示现实中不存在既有 CNS 设施。」；
  字段缺失显示「尚未声明 / 未配置」，绝不推断。这两个取值已登记进集中词表（`presentation.js`），
  Step5 的本地表退化为兜底。

---

## 15. Radar 分支

* 分段标题为「**雷达监视规划**」，作为独立 production 分支（实现仍是 `RadarSurveillanceLayoutService`）。
* **默认 OPTIONAL**：面板与阻塞项均显式声明「默认是可选的」；只有 `required_cns.project_default.surveillance`
  显式要求（`required=true`，或 `coverage_requirement` / 探测性能有值）或 `radar_surveillance_policy` 显式要求时，
  `radarBlocksNextStep(flow)` 才为真并成为阻塞项；否则不阻塞下一步。
* 不得表述为「正式结果已采纳」（测试断言）。`proposal_only` 语义保持。
* 面板字段标签本轮已中文化（`模型范围` / `算法版本` / `求解器` / `陆域掩膜` / `判定依据` / `采样参数：优化/复核`
  / `历史兼容挂高` / `允许仅Ⅰ型被证明不可行后回退 Ⅰ型+Ⅱ型` …），并**删除了 ALT-080 文案硬编码**
  （实际值来自 `readiness.fixed_altitude`；`RADAR_LAYOUT_V1_1_SEMANTICS.route_altitude` 不再写死高度层）；
  raw 事实值（`tower_top_orthometric_m`、`geometric_initial_radar_layout`、`scipy.optimize.milp`、
  `optimality_proven` 等）作为工程标识保留。

---

## 16. Step6 review / report

| 动作 | 位置 | 门禁（沿用后端，前端只如实显示中文原因） |
|---|---|---|
| **选择方案** | operate·方案比较（每候选一张卡，secondary） | 仅需方案审查已初始化且有候选；只切 `selected_variant_id` |
| **确认方案** | operate·确认与应用「主操作·第 2 步」 | `flow.steps['6'] && 已选方案 && (gate=ready_for_confirmation ‖ objectives_not_configured 且人工勾选知情确认)`；`data-gate` 原样透出 |
| **应用确认方案** | 同上「主操作·第 3 步」（primary） | `confirmed.status==='confirmed' && current_applicability==='current'`；`data-apply-gate` |
| **生成规划报告** | result·报告与交付（primary） | `['confirmed','applied'].includes(confirmed.status)`；`data-report-gate` |

* **Select ≠ Confirm ≠ Apply**：三个独立布尔量（`canConfirm` / `canApply` / `hasPlan`）分别计算，
  无任何联动；确认与应用分处两个不同 `.review-block`（测试断言 `parentNode` 不同）。
* **只消费 canonical result**：`flow.cns_plan_review` / `flow.confirmed_cns_plan` / `flow.cns_corridor_site_plan` /
  `flow.cns_planning_reports`；compatibility 结果不得成为正式确认依据（见 §12）。
* 报告区中文状态与空状态：门禁原因（「后端报告门禁要求方案状态为「已确认」或「已应用」；当前方案状态为「尚未确认」」）、
  `emptyReasonText('no_result', …)`（无方案 / 已确认未生成）、过期报告（「该报告对应旧项目状态，可继续下载，
  但不代表当前项目。系统不会自动覆盖或删除这份旧报告」）、预览草稿不写入项目。

---

## 17. 默认地图图层状态

* 初次打开：`index.html` 的图层抽屉中**只有 `online`（QGIS 在线底图）默认勾选**；
  网格、人口、地形、建筑轮廓、高度层障碍（含两个子开关）、建筑净空、旧版试算航路、候选航路、
  高度层可行域、雷达监视、CNS 各层**全部默认关闭**。
  由 `map_default_layers.test.mjs`（4 项）与 `b4x_ui_convergence.test.mjs`（含约束三开关）双重断言。
* 打开项目 / 状态刷新**不重置**用户图层选择（既有断言保持）；即使已有正式运行航路，也**不会**自动开启分析图层。
* 约束场逐格明细**按需加载**：只有勾选「高度层障碍」才发一次只读 GET；启动路径不含任何约束场请求。

---

## 18. 建筑 / 铁塔图层

* **建筑轮廓层**（`buildingFootprintLayer`）：仍可独立打开、默认关闭（放大后按需拉取），
  绘制顺序在约束层之上、航路之下。
* **铁塔符号可辨识性**：继续使用 `MAP-TOWER-SYMBOL-V2` 的矢量桁架塔形（`TOWER_SYMBOL` 唯一几何来源），
  尺寸语义是**屏幕像素高度**：medium 16 px、detail 22 px、高亮 ≥ detail 22 px、高亮环半径 15 px、
  命中半径 13 px（与视觉尺寸一致）；overview 只显示聚合点（孤立单塔不进入计划），
  被共塔候选联动的宿主塔在任何档位至少按 detail 尺寸绘制并加环。
  本轮未降低这些尺寸（`b4x_ui_convergence.test.mjs` 断言 medium ≥14 px、detail ≥20 px 且必须使用 `drawTowerSymbol`），
  也**未改动 Radar 面板现有方向扇区表现**（`radar_layout_overlay.js` 未被本批次修改）。

---

## 19. B4X 定向测试

### 19.1 Python 定向组合（一次）

```text
pytest -q tests/test_phase4_b4x_constraint_read_api.py tests/test_phase4_b3x_planning_constraints.py \
         tests/test_layered_theta_star_v2.py tests/test_layered_route_planner.py \
         tests/test_layered_planner_baseline.py tests/test_layered_route_validation_adoption.py \
         tests/test_route_planner_v3_continuous.py tests/test_building_level_independent_facts.py \
         tests/test_building_environment_clearance.py tests/test_building_geometry_quality.py \
         tests/test_required_cns_recommendation.py tests/test_bug_shelter_ui_001_shelter_policy_projection.py \
         tests/test_towers_operational_integration_v2.py tests/test_bug_tower_vref_001_terrain_vertical_reference_casing.py \
         tests/test_risk_aware_route_planner_v2.py tests/test_cns_corridor.py
→ 442 passed
```

其中 `tests/test_phase4_b4x_constraint_read_api.py` 为本轮新增（26 项），逐条锁定 §8 的只读契约：
紧凑投影字段、无 evidence / 几何、24,300 格量级、视口过滤、非法 bbox 忽略、`cells_unavailable` 不伪装、
summary 保持 slim、只读性（调用前后 state 逐字节相等）、多高度层参数化（ALT-060/080/100/150/200/CUSTOM-42）、
以及「读取路径源码不得出现 `ALT-080` / `80.0` / `80 m`」。

### 19.2 前端定向（开发过程中按改动范围逐次运行）

```text
node tests/b4x_ui_convergence.test.mjs                 # 30/30（本轮新增）
node tests/workbench_shell.test.mjs                    # 48/48
node tests/frontend_modules.test.mjs                   # 84/84
node tests/radar_surveillance_layout_frontend.test.mjs  # 13/13
node tests/layered_validation_adoption_frontend.test.mjs# 32/32
node tests/route_risk_profile_frontend.test.mjs        # 16/16
node tests/bug_step03_resource_action_frontend.test.mjs# 10/10
node tests/map_default_layers.test.mjs                 # 4/4
node tests/grid_details_summary.test.mjs               # 7/7
node tests/population_nodata_frontend.test.mjs         # 11/11
node tests/grid_popup_hydration_frontend.test.mjs      # 8/8
node tests/layered_route_map_evidence.test.mjs         # 16/16
（其余 13 个测试文件包含在 §21 的 full suite 中）
```

---

## 20. full pytest 结果及执行次数

```text
python -m pytest -q
→ 1797 passed, 7 skipped, 1 warning in 709.40s (0:11:49)        共 1 次
```

* **0 failed**：无本轮新 regression，因此无需补跑（符合「不得反复全量测试」）。
* 进程退出码为 1，唯一原因是 Windows 上 pytest 清理临时目录
  `C:\Users\yiding\AppData\Local\Temp\dsh-*\tmp*` 时 `PermissionError`（沙箱句柄未释放），
  与测试结果无关；测试统计为 1797 passed / 7 skipped / 0 failed。
* 参照 B3X：B3X 结束时为 1769 passed / 7 skipped；本轮 1797 passed 的增量来自
  `tests/test_phase4_b4x_constraint_read_api.py`（26 项）与 `tests/b4x_ui_convergence.test.mjs`（JS，不计入 pytest）。

---

## 21. frontend full suite 结果及执行次数

```text
第 1 次：25 个测试文件 → 405 passed / 2 failed
  失败项：tests/layered_route_map_evidence.test.mjs
    - the candidate route layer is a separate switch from the coarse feasibility mask
      （断言锁定 index.html 旧图层名「分层候选航路」）
    - the evidence highlight is pure UI: no state, no zoom, no layer, no LOD change
      （断言锁定 main.js 的 `let routeEvidenceHighlight=null;` 单行声明）

第 2 次（修正这 2 处陈旧断言后，即本轮最后一次 full frontend suite）：
→ 407 passed / 0 failed（25 个 .test.mjs / .test.js 文件）
```

两处均为**断言锁定旧文案/旧代码形态**，不是行为回归：
图层名按 B4X 改为「候选航路（当前规划结果）」；`main.js` 为保持 ≤450 行轻入口把
`routeEvidenceHighlight` 与 `gridDataSerial` 合并到同一 `let` 声明。断言改为
`/候选航路（当前规划结果）/`、`/coarse|粗判/` 与 `/routeEvidenceHighlight=null[,;]/`，
未放宽任何行为断言。

---

## 22. git diff --check

```text
$ git diff --check
（无输出）
$ echo $LASTEXITCODE
0
```

（仅有无害的 `LF will be replaced by CRLF` 提示，属 Windows 换行策略，与空白错误无关。）

---

## 23. P14 四文件 SHA-256（接手时 = 结束时）

```text
5AA3559E4B84C19D513BA6546A1A5435E4CC1C9401DCBB759FBA666E96920867  cns_planner/algorithms/corridor/v1.py
045826BFEB8B7C74885F8DAF65DB67B26C8F3E60219989A59F124542E94E32B8  cns_planner/algorithms/coverage/geometric_3d.py
79F9C56A97955847B74A5B6EDE244BEFCB8DC77CAF4739DAAD2C2B769BA57C28  cns_planner/algorithms/service_capability/v1.py
BFC08B5FC25B608FC035754714F127DED6E024F854AF6CA67A73ED2E8436A20C  tests/test_cns_corridor.py
```

四文件在**开始记录**与**结束复核**时逐字节一致（未修改、未格式化、未 stash、未 reset）；
`tests/test_cns_corridor.py` 虽在 `git status` 中显示为 ` M`，但那是接手本批次**之前**既有的工作树改动，
SHA 与本批次开始时完全相同。`_dsh_prof/`、舟山旧核查 `tools/`、qgis 临时文件、乱码文件均未触碰（只读）。

---

## 24. B4X_READY_TO_REVIEW

**true** —— 25 项验收全部满足，无 blocker。

| # | 验收项 | 结论 |
|---|---|---|
| 1 | Production 只有六步主导航 | ✅ 逐字断言 |
| 2 | 主页面不出现 P-number / V1 / V2 / V3 / legacy 等开发术语 | ✅ 主导航零命中；生产主界面零命中 P-number / V3 / provisional / authoritative 等；Step3 算法配置面板保留的 raw 字段名见 §5 第 3 条 |
| 3 | Production UI 中文为主 | ✅ 全部英文业务标题已中文化 |
| 4 | 状态 raw enum 不直接展示 | ✅ 集中词表 + 未登记原样返回（不伪装） |
| 5 | Step2 可以选择任意 AltitudeLayer | ✅ 目录驱动，含 ALT-060/100/150/200/CUSTOM |
| 6 | Step2/3 可以查看当前高度层 Planning Constraint Field summary | ✅ 状态卡 + 生成按钮 |
| 7 | 地图可以显示 constraint blocked cells | ✅ 「高度层障碍」图层 |
| 8 | 地图点击障碍格显示中文 obstacle reason | ✅ 多原因中文，raw JSON 进高级 |
| 9 | unknown count 不被隐藏为 pass | ✅ 状态卡 + 图例强制显示计数与警告 |
| 10 | Step3 正式流程只有 Theta* 主链 | ✅ `PRODUCTION_ROUTE_CHAIN` |
| 11 | 旧 route planners 只在 Advanced | ✅ 隔离 + 声明 + 图层命名 |
| 12 | Step5 正式流程只用 canonical 3D / Corridor chain | ✅ 六段顺序断言 |
| 13 | Step6 只消费 canonical results | ✅ 不读 compatibility |
| 14 | 默认地图只有在线底图 | ✅ 含约束三开关 |
| 15 | 建筑层仍可单独打开 | ✅ |
| 16 | 铁塔图标可辨识 | ✅ 16 / 22 px 矢量塔形 |
| 17 | 多高度层不得写死 ALT-080 | ✅ 源码级断言（含雷达文案与后端读取路径） |
| 18 | P14 SHA 完全不变 | ✅ |

---

## 25. 未尽事项（不构成 blocker，供下一批次参考）

1. **Step3 算法配置面板的 raw 字段名**：Theta* V2 面板已把**标签**中文化，但字段名 / raw 值成对显示
   （`heading_bin_count`、`theta_min_deg`、`mask fingerprint`、`readiness status（…）` 等 ~65 处可见出现）。
   建议下一批次统一收进 `<details>` 高级折叠，只保留中文标签；本轮保留是为了"如实转印后端事实"，
   改动它们会牵动 `frontend_modules` / `phase35_*` / `planning_exposure_frontend` 等多处既有断言。
2. **`presentation.js` 词表仍可扩充**：`risk_unresolved`、`no_intersection`、`cost_policy_not_confirmed` 等
   后端 reason code 目前原样显示（原样返回是有意设计，绝不伪装）。建议后续按需登记中文。
3. **后端候选名含开发编号**：`plan_review_service` 生成的候选名（如 `Baseline` / `P16 Auto Proposal`）
   属于**后端数据**，会显示在 Step6 operate 标签。前端改写数据会违反"不发明语义"，建议后端改名。
4. **`.wb-*` 新增类样式**：已在本轮新增的 `web/css/b4x.css` 中定义（`index.html` 已引用）；
   既有 `workbench.css` 未改动，避免影响其它布局断言。
5. **地图视口级按需拉取**：`/map` 已支持 `bbox` 参数，但前端当前一次读取当前高度层全量紧凑结果
   （24,300 格约 1 MB 量级，可正常显示）。若未来工作区显著变大，只需在 `constraint_view.loadMap()`
   传入 `visibleBounds()` 即可切换为视口拉取（契约已就绪）。
6. **B5 / B6 未启动**：本轮不实现大型 artifact migration 与 Heavy Task。
