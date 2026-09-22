# Towers V2 Final Semantic Closeout

本轮是语义一致性收口，不新增功能。基线：
`37b73c9d6343096cdcb4507fdb3beb9641f03152`（`feat: complete tower clearance and colocation workflow`）。

---

## 1. HEAD / git status

| 项目 | 值 |
|---|---|
| `git rev-parse HEAD` | `37b73c9d6343096cdcb4507fdb3beb9641f03152` |
| `git log -1 --oneline` | `37b73c9 feat: complete tower clearance and colocation workflow` |
| 开工时 `git status --short` | **clean**（无输出） |
| 本轮结束时 | 仅本轮的 12 个修改文件 + 1 个新增报告（见 §4）；**未 commit / 未 push** |

未执行 reset / stash / checkout / commit / push，未覆盖任何已有修改。

## 2. 四个问题是否真实存在

全部**真实存在**（逐条以开工时 HEAD 的代码为准）：

| 编号 | 结论 | 证据（开工时代码） |
|---|---|---|
| FIX-TOWER-SEM-001 | 存在 | `tower_colocation_policy.device_mount_confirmed` 是全局 boolean；`enabled = confirmed && device_mount_confirmed && assumption != null`；候选派生时 `metadata.host.device_mount_confirmed = enabled && policy.device_mount_confirmed`，`planning_profile.add_device_allowed = True`。全局一勾 ⇒ 373 条候选全部表现为"已确认可物理安装" |
| FIX-TOWER-SEM-002 | 存在 | 候选 `available_subsystems = []`；`_candidate_actions()` 对每个 target device 无条件生成 action；`_action()` 只检查 planning_profile / vertical / device 侧，**没有**任何"站点声明的分系统 vs 设备分系统"判定 ⇒ 无证据被静默当成"都能装" |
| FIX-TOWER-TYPE-001 | 存在 | `ROOFTOP_MARKERS = ("楼面","屋顶","屋面","rooftop","roof")` 不含"楼顶"；真实数据中"楼顶景观塔"×2 落入 `unknown` |
| FIX-TOWER-DOC-001 | 存在 | `domain/tower_obstacle.py` 的 limitations 写着"不参与净空判定"；`TOWERS_SITE_TYPE_CLASSIFICATION.md` §2/§6 同样表述 |
| FIX-TOWER-META-001 | 存在 | `LayeredFeasibilityAdapter.describe()` 笼统声明 `not_horizontal_clearance: True`，而 Tower 水平净空**已经**真实参与（`tower_clearance_policy.tower_horizontal_clearance_m` 扩展塔点到 cell）；`COARSE_ENVELOPE_SEMANTICS` 也只有笼统键 |

## 3. 根因

1. **SEM-001**：把两个不同层级的语义（"允许作为规划宿主" / "物理安装可行"）压进**同一个全局
   boolean**，并把它**传播到每一条候选**。全局策略是一个规划假设，不是逐塔现场调查结论。
2. **SEM-002**：`_action()` 的 eligibility 缺少"分系统证据"这一层；且 `available_subsystems`
   的**空数组**与"未声明"在代码里不可区分，两者都被当成"无约束"而非"无证据"。
3. **TYPE-001**：marker 词表遗漏了真实报送数据的同义用词"楼顶"（与"楼面/屋顶/屋面"同义）。
4. **DOC-001**：把"塔顶未解析"的**后果**写成了"不参与净空判定"，容易被读成"对航路没有影响"；
   真实行为恰好相反 —— 该空间是 `unknown` 且不可穿越（fail-closed）。
5. **META-001**：adapter metadata 写于 Tower 水平净空接入**之前**，此后未随算法更新，
   于是 metadata 与实际算法不一致。

## 4. 修改文件

| 文件 | 改动 |
|---|---|
| `cns_planner/domain/tower_colocation.py` | 规划层/实施层字段拆分（`planning_host_use_confirmed` / `physical_mount_confirmed` / `requires_site_survey`）；legacy 兼容映射；候选 metadata 新增 `planning_host` 投影 |
| `cns_planner/domain/tower_obstacle.py` | `ROOFTOP_MARKERS` 增加"楼顶"；limitations 统一为 fail-closed 表述 |
| `cns_planner/domain/site_planning.py` | `CandidateAction` 补 `planning_host_status` / `subsystem_mount_status` / `physical_mount_confirmed` / `requires_site_survey` |
| `cns_planner/domain/layered_route.py` | `COARSE_ENVELOPE_SEMANTICS` 按域拆分水平净空说明（building vs tower + bbox 包络） |
| `cns_planner/application/site_planning_service.py` | `_action()` 增加分系统证据判定（declared 硬约束 / unverified 不阻断）与两层状态字段 |
| `cns_planner/gis/layered_feasibility_adapter.py` | `describe()` 拆分水平净空 metadata，删除笼统声明 |
| `cns_planner/layered_route_planner/planner.py` | `tower_height_unresolved` 的 reason 文案改为统一 fail-closed 表述 |
| `cns_planner/web/js/workflow/step05_cns.js` | 共塔策略表单改为"允许真实铁塔作为共塔规划宿主候选（工程规划假设）"；显示物理安装未核实/需勘察；候选与 Proposal 行显示两层状态 |
| `tests/test_towers_operational_integration_v2.py` | 更新旧断言 + 新增 §7 要求的 16 项覆盖 |
| `tests/towers_operational_v2_frontend.test.mjs` | 更新共塔表单断言 + 新增 Proposal 两层状态与运行时渲染断言 |
| `TOWERS_SITE_TYPE_CLASSIFICATION.md` | 统计更新为 219/39/115；unknown 清单 8 类；fail-closed 统一表述 |
| `TOWERS_OPERATIONAL_INTEGRATION_V2_REPORT.md` | §5 策略字段、§18 已知限制（含规划宿主≠物理安装）、bbox 包络与非精确径向净空表述 |
| `TOWERS_V2_FINAL_SEMANTIC_CLOSEOUT.md` | 新增：本报告 |

未修改：`theta_star_v2.py`（gate 的 fail-closed 行为本就正确，本轮只加测试断言）、
`invalidation_service.py`、`tower_obstacle_service.py`（除既有 `set_clearance_policy`）。

## 5. 新的共塔规划语义

```
真实铁塔 TowerSite（源数据事实）
      │  派生
      ▼
TowerColocationCandidate
  planning_profile.reuse_class      = tower_colocation_host
  planning_profile.add_device_allowed = true   ← 仅当"规划宿主允许 + 服务原点假设"成立
  metadata.host.planning_host_use_confirmed = true
  metadata.host.physical_mount_confirmed    = false   ← 恒 false
  metadata.host.requires_site_survey        = true    ← 恒 true
  metadata.planning_host.planning_host_status   = eligible
  metadata.planning_host.subsystem_mount_status = unverified
      │  _candidate_actions()
      ▼
CandidateAction（P11/P16 共用）
  planning_host_status / subsystem_mount_status / physical_mount_confirmed / requires_site_survey
```

* `enabled = planning_host_use_confirmed && service_origin_assumption != null`
  （**只有两个条件**；物理安装确认不再是启用条件，也不再有对应的 UI 入口）；
* 语义仍是 **prefer 共塔，不是 force**：tier 顺序
  `Existing CNS → Existing Shared Site → Tower Colocation Host → Candidate Site → New-build Candidate` 未变；
* 未确认时共塔候选保持 `ineligible`（`add_device_allowed = null`），普通候选站/新建候选照常补盲。

## 6. physical mount / site survey 语义

| 字段 | 取值 | 含义 |
|---|---|---|
| `physical_mount_confirmed` | 恒 `false` | 没有任何逐塔现场调查数据；任何输入组合都无法把它打开（测试锁定） |
| `requires_site_survey` | 恒 `true` | 上述未核实的直接后果 |
| `subsystem_mount_status` | 恒 `unverified`（策略级） | 没有逐塔分系统安装证据 |
| `device_mount_confirmed` | 恒 `false`（legacy 别名） | 只作为旧项目**输入**读取，canonical 值不接受它 |

**legacy 兼容（不破坏旧项目 reopen）**：旧项目里的
`confirmed=true + device_mount_confirmed=true` 会被映射为
`planning_host_use_confirmed=true`（保持 `enabled=true`），物理层被强制收窄为
`false` / `true`；`normalize` 对自身的输出幂等（测试断言）。

## 7. subsystem compatibility 语义

`_action()` 现在按证据强度分三种状态，**只有硬冲突才排除**：

| 站点声明 | `subsystem_mount_status` | eligibility |
|---|---|---|
| 显式声明了可用分系统，且**不含**该设备分系统 | `declared_not_compatible` | `ineligible`（附原因"站点声明的可用分系统不包含该设备分系统"） |
| 显式声明了可用分系统，且包含 | `declared_compatible` | 不受影响 |
| **没有任何声明**（真实铁塔候选 `available_subsystems=[]`） | `unverified` | **不受影响**（仍可 eligible） |

于是：

* "没有证据"**不**被解释成"所有目标设备都能装"，也**不**被解释成"都不能装"；
* 共塔方案仍可作为工程 Proposal 参与 P11/P16 的 what-if 比较，并保持在普通候选之前；
* Proposal 必须显式显示 `subsystem_mount_status = unverified`（Step05 已展示）；
* `available_subsystems` 不会被改写成 `["C","N","S"]`，也不杜撰任何安装兼容性；
* 设备参数仍然只来自 DeviceCatalog / `equipment_reference_catalog` / 用户确认；
* 已有 CNS facility 的语义完全未变。

## 8. site_type 最新分类统计

**由代码实测**（真实 Excel，SHA-256 `d722c111…a411b`，373 条 / 0 无效行）：

| base_type | 修正前 | 修正后 |
|---|---|---|
| rooftop | 217 | **219**（+2：楼顶景观塔 ×2） |
| ground | 39 | **39** |
| unknown | 117 | **115** |

unknown 现为 **8 类 / 115 塔**：角钢塔 58、H杆塔 22、单管塔 14、造型景观塔 13、
水泥杆塔 3、通信灯杆塔 2、一体化塔房 2、（空）1。
其余未知类型本轮**全部保持 unknown**，未自动设为 ground。
完整裁定表见 `TOWERS_SITE_TYPE_CLASSIFICATION.md` §5。
该统计已固化为回归测试（真实文件不存在时自动 skip）。

## 9. unresolved fail-closed 真实链路

```
TowerObstacleProfile.status = unresolved
  → 该塔所在 cell 的 tower fact: data_status = unknown
  → LayerFeasibilityMask: reason_code = tower_height_unresolved, cell status = unknown,
    tower_required_clearance_egm2008_m = null（不生成具体 floor）
  → Theta* V2 _build_gate.classify: domain = unknown
  → unknown 不可穿越（fail-closed）
```

统一表述（代码注释 / UI / 报告 / 分类文档一致）：

> 塔顶高度未解析时不生成具体 tower clearance floor；相关空间保持 unknown，
> 并在路径搜索中 fail-closed，不得作为已验证安全可通行区域。

测试直接驱动 `_build_gate(...)` 断言 `verdict = {"domain":"unknown","reason_code":"tower_height_unresolved"}`。

## 10. horizontal-clearance metadata

| metadata 键 | 值 |
|---|---|
| `building_horizontal_clearance` | `not_modeled_here_deferred_to_continuous_validation` |
| `tower_horizontal_clearance` | `explicit_policy_bbox_envelope` |
| `tower_horizontal_clearance_envelope` | `coarse_bbox_envelope_not_exact_radial_clearance` |
| `tower_horizontal_clearance_is_exact_radial` | `false` |
| `not_horizontal_clearance` | `true`（保留既有键，含义收窄为"整体不是精确水平净空结论"） |

同样的按域拆分同时写入 `describe()`（adapter metadata）与 `COARSE_ENVELOPE_SEMANTICS`（mask 语义）。
本轮**不**实现精确圆形 corridor clearance。

## 11. 测试结果

```text
$ python -m pytest tests -q -p no:cacheprovider
1495 passed, 6 skipped in 450.55s (0:07:30)     [exit code: 0]
```

（本轮开始时同命令为 1485 passed；净增 10 项后端断言，其中包含真实 373 条数据重统计。）

前端：18 个 `.test.mjs` 逐文件执行，**312 项断言、0 失败**（`FILES_WITH_FAILURES=0`）。

```text
bug_shelter_ui_001_confirmed_contract.test.mjs   pass=9  fail=0
bug_ui_pop_002_coverage_scopes.test.mjs          pass=8  fail=0
building_mapping_frontend.test.mjs               pass=10 fail=0
frontend_modules.test.mjs                        pass=83 fail=0
grid_details_summary.test.mjs                    pass=7  fail=0
layered_route_map_evidence.test.mjs              pass=15 fail=0
layered_validation_adoption_frontend.test.mjs    pass=31 fail=0
map_default_layers.test.mjs                      pass=4  fail=0
map_lod_clustering.test.mjs                      pass=19 fail=0
map_measure_tool.test.mjs                        pass=12 fail=0
phase35_feasibility_policy_save_chain.test.mjs   pass=7  fail=0
phase35_stabilization_frontend.test.mjs          pass=2  fail=0
population_nodata_frontend.test.mjs              pass=11 fail=0
route_risk_profile_frontend.test.mjs             pass=14 fail=0
route_safety_evidence_frontend.test.mjs          pass=8  fail=0
towers_map_layer.test.mjs                        pass=6  fail=0
towers_operational_v2_frontend.test.mjs          pass=18 fail=0
workbench_shell.test.mjs                         pass=48 fail=0
FILES_WITH_FAILURES=0
```

本轮新增/更新的覆盖（对应用户 §7 的 16 项）：

| # | 覆盖 | 位置 |
|---|---|---|
| 1 | 全局"允许共塔规划"≠ 373 塔 physical mount confirmed | 后端 `test_global_host_confirmation_never_implies_physical_mount_for_all_towers` |
| 2 | `physical_mount_confirmed` 默认 false | 后端 `test_physical_mount_and_site_survey_defaults_are_fail_closed` |
| 3 | `requires_site_survey` 默认 true | 同上 |
| 4 | 共塔 Proposal 在规划假设确认后可进入 P11/P16 | 后端端到端 + `test_colocation_policy_save_through_the_existing_endpoint` |
| 5 | `tower_colocation_host` 仍优先于普通 candidate | 既有 `test_planner_selects_tower_colocation_before_plain_candidate` + 端到端 |
| 6 | fallback 普通候选保持有效 | 既有 `test_application_site_plan_falls_back_to_plain_candidate_end_to_end` |
| 7 | `available_subsystems=[]` 不被解释为真实安装能力 | 后端 `test_empty_available_subsystems_is_not_a_capability_claim` / `test_declared_subsystems_are_still_enforced` |
| 8 | Proposal 显式显示 `subsystem_mount_status=unverified` | 前端 `FRONTEND: the Proposal surfaces the two-layer host/mount status` |
| 9 | 不自动生成 Existing CNS facility | 既有 `test_colocation_candidates_never_create_existing_facilities_in_state` |
| 10 | 不杜撰设备性能 | 既有 `test_tower_colocation_never_invents_device_parameters` |
| 11 | "楼顶景观塔"×2 变为 rooftop | 后端 `test_rooftop_markers_include_louding_and_stay_conservative` + 真实统计测试 |
| 12 | 其余 115 个 unknown 继续 unknown | 后端 `test_real_tower_site_type_classification_is_219_39_115` |
| 13 | unresolved tower cell → unknown → Theta* fail-closed | 后端 `test_unresolved_tower_cell_is_unknown_and_the_gate_fails_closed` |
| 14 | metadata 正确说明 tower horizontal bbox envelope | 后端 `test_adapter_metadata_separates_building_and_tower_horizontal_clearance` |
| 15 | 0.8/0.1/0.1 不变 | `test_theta_v2_objective_weights_are_untouched` + `test_layered_theta_star_v2.py` |
| 16 | Risk V2 不读取 towers | `test_towers_are_not_a_risk_framework_factor_input` / `test_tower_heights_never_enter_the_risk_or_profile_math` |

另外：`save/reopen 不丢`由 `test_colocation_action_keeps_two_layer_status_through_save_and_reopen` 覆盖；
legacy 兼容由 `test_legacy_policy_input_still_enables_planning_host_only` 覆盖。

## 12. 未修改内容

Theta* V2 any-angle、parent LOS rewiring、0.8/0.1/0.1 objective、population×shelter、
Risk Framework V2 数学、RouteRiskProfile 数学、`REUSE_TIERS` 顺序、Tower 地图图标、
map legend、LOD、clustering、TowerSite 原始事实契约、原始 Excel、AirspacePolicy、
Operational Adoption 核心语义、既有 CNS facility 语义。

Tower 三角色继续严格分离：
`TowerSite`（真实数据事实）/ `TowerColocationCandidate`（CNS 工程规划宿主候选）/
`TowerObstacleProfile`（航路障碍物净空派生事实）。

## 13. 是否已经可以进入人工验收

四项（SEM-001 / SEM-002 / TYPE-001+DOC-001 / META-001）均已闭环，全量回归通过（见 §11）。

**Towers Operational Integration V2 可以冻结，下一步进入人工验收，不再继续扩展功能。**
