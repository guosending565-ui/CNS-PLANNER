# Phase4-B0 目标架构契约

| 项 | 契约 |
|---|---|
| 文档状态 | Phase4-B0.1 目标架构基线；本阶段仅定义契约，不实现生产代码 |
| 事实基线 | Git `HEAD 82b5b09` + `PHASE4_ARCHITECTURE_AUDIT.md` |
| 目标态裁决 | 本文；当本文与审计报告的现状分类冲突时，以本文为目标态，以审计报告的引用关系作为迁移约束 |
| 变更策略 | 迁移、收敛、抽取、兼容读取；不推倒重写 |
| 非目标 | 本文不证明、修复或重新定义任何算法性能；不改变已冻结算法语义 |

## 0. 契约范围与强制原则

本文把 Phase4-A 审计结论转化为可实施、可验收的目标架构契约。所有 Phase4 后续实现批次必须满足以下原则：

1. 产品只有一套 production 工作流，用户主界面按六个业务步骤组织，不按开发阶段编号或算法版本组织。
2. 每类 authoritative result 只能有一个 production writer；其他实现只能产生候选、建议、实验结果或兼容读取结果。
3. 正式航路只由固定巡航高度的分层风险感知规划链生成、验证并发布。
4. 正式 CNS 链只使用三维几何覆盖、服务能力、服务走廊、走廊缺口和走廊站址规划。
5. `ProjectState` 保存事实、策略、确认、活动标识、摘要/指纹和权威采纳/评审记录，不充当大型计算结果仓库。
6. 大型可重算结果必须进入 artifact/sidecar；工作流快照只传播摘要和可解析引用。
7. 所有缺失输入都必须按 `REQUIRED / ASSUMABLE / OPTIONAL / ENHANCEMENT` 解释，禁止用隐式零值掩盖未知事实。
8. workflow lifecycle 与领域 assessment 必须分离；`failed` 不是领域“不通过”，`passed` 也不等于工作流“完成”。
9. 旧项目必须可读；兼容读取不得使新项目继续写入 legacy state。
10. Phase4 采用增量迁移。任何归档或删除都必须先解除审计报告列出的 import、API、state、frontend、test 和保存项目兼容阻塞。
11. “可以生成/比较候选”与“可以形成 authoritative result”是两道不同门槛；计算成功不自动授予 Adopt、Publish 或 Confirm 权限。

### 0.1 P14 性能工作线的地位

当前未提交工作线定义为 **P14 PERFORMANCE PATCH CANDIDATE**：

- `cns_planner/algorithms/corridor/v1.py`
- `cns_planner/algorithms/coverage/geometric_3d.py`
- `cns_planner/algorithms/service_capability/v1.py`
- `tests/test_cns_corridor.py`

该 patch 已有 benchmark 和语义等价测试，但没有复现原正式 R0003 `>40min` 现场。因此：

- 不得将其视为正式解决了性能问题；
- 不得以其当前实现细节重新定义目标架构；
- 不得在 Phase4-B0 修改、格式化、删除、stash 或提交上述文件；
- 后续只有在原现场或经批准的等价代表性数据集上复现、对照并通过后，才能改变性能问题状态。

本次调查形成两个必须进入目标态的架构事实：

1. 服务走廊完整派生结果可达到约 **82 MB**。走廊空间单元明细及其 evidence 必须进入 artifact/sidecar，禁止作为普通 `ProjectState` 内容或 workflow snapshot 传播。
2. 当前长计算采用同步 HTTP 并持有 mutation lock，客户端 `Ctrl+C` 不能取消后端计算。目标架构必须提供 Heavy Task、progress 和 cancellation；它们不在 B0.1 文档阶段实现。

---

## 1. Product Mission

产品使命是：把权威数据、明确工程假设和可追溯算法结果，收敛为一条从环境建模、运行航路发布，到 CNS 需求、能力与设施方案，再到人工评审和报告的可审计规划链。

产品面向规划人员表达业务目标、输入缺口、假设、结果和下一步；算法名称、内部版本、实现原语和调试证据只在“高级/审计”中出现。

目标产品保证：

- **单链**：用户在主界面中只看到一条可形成正式结果的 production 路径。
- **单写**：每类权威结果只有一个生产写入 owner。
- **可解释**：缺数据、采用假设、未评估、计算失败和领域不通过具有不同语义。
- **可追溯**：每个派生节点可由输入、策略、假设、实现版本和 artifact 指纹重建。
- **可演进**：旧项目可读，旧算法可在兼容或研究边界内运行，但不能旁路写入 production 结果。
- **可运营**：长计算可观察、可取消，不以长时间占用同步请求和全程 mutation lock 为常态。

---

## 2. Canonical Six-Step Workflow

产品只有以下一套 production 工作流：

| Step | 名称 | 业务目标 | production 入口与结果 | 下一步门槛 |
|---|---|---|---|---|
| 1 | 数据准备 | 建立项目、来源、范围、坐标与输入清单 | 权威数据源、来源审计、工作区准备情况 | 必需来源可解析；缺失项已被分类 |
| 2 | 环境与风险 | 建立规划网格、地形/建筑/铁塔/空域/要地环境、Population × Shelter 风险场，并按选定固定高度层派生 Planning Constraint Field | `environment`（含 derived `planning_constraint_field` artifact）、`risk_field` | Risk Field 与 Constraint Field 分离；固定高度层约束单元必须为 `pass/blocked/unknown`，`unknown` 不得静默成为 `pass` |
| 3 | 航路规划与发布 | 从 OD 和固定巡航高度生成候选；搜索同时消费 soft Risk Field 与 hard Planning Constraint Field；再完成风险画像和独立连续验证，经人工确认发布 | `route_candidate` → `route_validation` → `operational_route` | blocked 单元不可扩展；unknown 按显式 policy，production 默认不可扩展；只有通过独立连续/native validation 与 authority gate 的运行航路可进入下游 |
| 4 | CNS需求 | 基于运行场景形成并确认 CNS 需求 | `required_cns` | 权威需求已确认；建议本身不具权威性 |
| 5 | CNS能力与设施规划 | 评估三维覆盖和服务能力，识别走廊缺口，形成设施方案；包含监视雷达规划 | `coverage` → `service_capability` → `service_corridor` → `capability_gap` → `facility_plan` | 既有 CNS 基线已声明，方案证据完整或警告已披露 |
| 6 | 方案评审与报告 | 比较、选择、确认、应用方案并形成报告 | `plan_review` → `confirmed_plan` → `report` | 确认与报告均引用同一组冻结输入和 artifact |

### 2.1 航路 production 主链

唯一 production 航路主链为：

```text
OD + fixed cruise altitude
  ├──→ Population × Shelter Risk Field (soft planning cost)
  └──→ Planning Constraint Field (terrain/building/tower/airspace/critical-site feasibility)
  → LayeredRiskAwareThetaStarV2
  → RouteRiskProfile
  → independent continuous/native constraint validation
  → Layered Operational Adoption
  → Operational Route
```

约束：

- `LayeredRiskAwareThetaStarV2` 是唯一 production planner。
- `ALT-080` 的 canonical 含义固定为 **80 m EGM2008 orthometric**，不是 80 m AGL；任何 AGL、椭球高或未知垂向基准必须先有显式、可审计的转换证据，否则为 `unknown/blocked`，不得猜测。
- Risk Field 与 Planning Constraint Field 是两个独立输入：前者仅为 `Population × Shelter` soft cost；后者承载 terrain/building/tower/airspace/critical-site 的 feasibility/hard constraint，禁止把障碍物高度或禁限区编码为风险惩罚。
- Planning Constraint Field 是 `environment` 的 per-layer derived artifact，由 `route_candidate` 消费；它不新增顶级 canonical workflow node，六步 workflow 与 13-node canonical DAG 保持不变。
- Constraint Field 每个单元至少使用 `pass/blocked/unknown`：`blocked` 单元不可扩展；`unknown` 必须经过显式 policy，production 默认 `block_search`，禁止 `unknown → pass`。任何允许 unknown 的非默认试算仍保持 provisional，且不能通过 Operational Adoption authority gate。
- Terrain hard rule：当 `layer_orthometric_m < terrain_orthometric_m + terrain_vertical_clearance_m` 时为 `blocked`；缺高度、垂向基准或 clearance 为 `unknown`，不得补 0。
- Building hard rule：`building_top_orthometric = resolved_ground_orthometric + building_height_m`；当 `layer_orthometric_m < building_top_orthometric + building_vertical_clearance_m` 时为 `blocked`。缺 ground、height、可接受的 `height_status` 或几何证据为 `unknown`。
- 只有已确认的 `tower_top_orthometric` 才能形成 tower hard 判定；楼面塔、base datum 或 mounting semantics 不明确时保持 `unknown`。同一份 Tower Source Facts 分别派生 `tower_obstacle_profiles`（Step 2/3）与 `tower_colocation_candidates`（Step 5），不得复制源数据。
- `RestrictedArea` / `ProtectedSite` 可保存 point/polygon/multipolygon source facts，但规划只消费最终确认的 protection geometry；confirmed `hard_exclusion` 直接进入 Constraint Field blocked mask。protection radius 只能来自 source 或显式 policy，禁止按 category 猜测。
- 无要地数据时可按明确的 airspace/regulatory authority policy 生成带 `critical_site_not_evaluated` warning 的 provisional candidate；不得把“未提供要地数据”静默解释为“无禁飞区”。
- Population 是候选规划的 REQUIRED 输入；Shelter coefficient 是 ASSUMABLE 输入，可用明确登记的 `value=1.0, basis=engineering_baseline` 继续，但不得描述为真实遮蔽事实。
- 正式候选必须消费与选定高度层和输入指纹一致的 Planning Constraint Field。terrain/building/tower 等证据缺失时，对应空间为 `unknown`；默认不得扩展，也不得用风险惩罚替代 hard gate。
- 候选航路不得直接成为 `operational_routes`。
- 发布必须验证候选、风险画像、Constraint Field、独立连续/native constraint validation 和期望指纹，保留现有防并发改动语义。搜索 mask 与连续验证必须分别运行；搜索 `pass` 不是连续验证证据。
- 固定巡航高度障碍约束与起降场/进离场障碍分开：本阶段只覆盖 cruise altitude constraint；`Departure/Arrival Procedure Validation` 后续以 terminal protection volume 与 climb/descent obstacle penetration 独立实现，不塞入本轮 Theta* 搜索。
- `RoutePlannerV1`、`RiskAwareRoutePlannerV2`、`LayeredRoutePlannerV1` 不再是 production planner，进入 compatibility/archive。
- RoutePlannerV3 A/B/C/D 进入 research/archive；V3-D 最终不得写 `operational_routes` 或其他 production 权威容器。
- `RiskAwareRoutePlannerV2` 中的 `GridGraph` 必须先抽取到公共基础模块，之后才能归档 planner。
- V3-C 中被 production 使用的连续验证器必须先抽取到 neutral production module，production 服务不得长期反向依赖 research/archive 包。

### 2.2 CNS production 主链

唯一正式 CNS 主链为：

```text
Required CNS
  → GeometricCoverage3D
  → CNSServiceCapability
  → Service Corridor
  → Corridor Gap
  → Corridor Site Planning
  → Plan Review / Report
```

约束：

- `CoveragePlannerV1` 2D、`CNSGapAnalyzerV1`、`ReuseFirstSitePlannerV1` 退出 production 主界面，保留兼容读取或高级审计入口直至删除前置满足。
- Timeline、Protection、Reliability、Safety、DAA 统一放入 Advanced，不得阻塞六步核心主链。
- Radar 保持 `KEEP_PRODUCTION`，归属 Step 5 Surveillance planning；默认是 OPTIONAL production branch，仅当 adopted Required CNS 或 surveillance policy 明确要求 Radar 时才是 REQUIRED，且不改变 CNS 正式主链的顺序和写入 authority。

---

## 3. Canonical Backend DAG

### 3.1 全局顺序

```text
environment
  → risk_field
  → route_candidate
  → route_validation
  → operational_route
  → required_cns
  → coverage
  → service_capability
  → service_corridor
  → capability_gap
  → facility_plan
  → plan_review
  → report
```

节点契约统一包含：

```json
{
  "node_id": "canonical_name",
  "inputs": [],
  "dependencies": {
    "required": [],
    "assumable": [],
    "optional": [],
    "enhancement": []
  },
  "status": "not_started",
  "output_maturity": "provisional",
  "readiness": {
    "state": "blocked",
    "blockers": [],
    "assumption_ids": []
  },
  "assumptions": [],
  "warnings": [],
  "fingerprint": null,
  "artifact": null,
  "invalidates": [],
  "next": []
}
```

`fingerprint`、artifact locator 和内部算法版本属于 API/审计信息，不得直接泄漏到 production 主界面。

### 3.2 节点级契约

下表中的“常态状态”表示一次执行完成后的 lifecycle 状态；`output_maturity` 与 workflow status 正交。`completed` 只说明计算执行成功，不说明结果已可发布。

| Node | output maturity | inputs | required / assumable dependencies | 常态 status / readiness | assumptions / warnings | fingerprint / artifact | invalidates | next |
|---|---|---|---|---|---|---|---|---|
| `environment` | provisional facts/summary + per-layer derived artifacts | workspace、来源配置、规划网格、人口、terrain/building/tower/airspace/critical-site facts、约束策略 | R: workspace、grid、population；A: population NoData policy；fixed-layer planning 对 terrain 与 applicable constraint evidence fail-closed | `completed` 或 `completed_with_warnings` / `ready` 或 `ready_with_assumptions` | 缺失 constraint evidence 映射为 `unknown/not_evaluated`，不得补 0 或声明无障碍/无禁飞区 | 输入/来源/映射策略指纹；grid/attribute cells 与 per-layer `planning_constraint_field` 在 artifact；ProjectState/snapshot 只留摘要、指纹与 locator | 全部下游 | `risk_field` |
| `risk_field` | provisional derived result | environment 摘要、风险策略 | R: population；A: shelter coefficient、风险聚合软件基线；O: traffic/conflict/property/infrastructure | `completed[_with_warnings]` / `ready[_with_assumptions]` | shelter `1.0` 仅为 engineering baseline；未评估因子不能补 0 | 风险输入与策略指纹；risk cells 在 artifact | `route_candidate` 至 `report` | `route_candidate` |
| `route_candidate` | **always provisional** | OD、固定巡航高度、environment、Risk Field、Planning Constraint Field、规划策略 | R: scenario route、高度层、planning request、population、cost policy、与高度层/输入指纹一致的 Constraint Field；A: shelter coefficient、搜索参数、目标策略、风险阈值 | 可搜索时 `completed[_with_warnings]` / `ready[_with_assumptions]`；blocked cell 不可扩展；unknown 按显式 policy，production 默认不可扩展 | Risk Field 只影响 soft cost；terrain/building/tower/airspace/critical-site 只影响 feasibility。要地数据未提供时必须 `critical_site_not_evaluated`，是否允许 provisional 由 authority policy 决定 | 候选、risk 与 constraint 输入/策略/实现指纹；候选几何和 Constraint Field locator 为 artifact | `route_validation` 及全部下游 | `route_validation` |
| `route_validation` | provisional evidence；通过 authority gate 后 eligible | route candidate、RouteRiskProfile、Constraint Field lineage、source-native terrain/building/tower 与 confirmed protection geometry | R: candidate、risk profile、search Constraint Field lineage、独立 continuous/native validators；A: 经批准采样参数 | 可执行则 `completed[_with_warnings]`；缺 REQUIRED evidence 为 `blocked`；各域结果独立保存 `passed/failed/unknown` | continuous validation 不读取 mask verdict 充当证据；任何适用域 `unknown` 不得伪装 pass；search/continuous 不一致 fail-closed。terminal procedure 单列 deferred，不混入 cruise verdict | 验证输入/来源/策略/validator 指纹；详细区间、native pixel、footprint/tower/protection geometry evidence 为 artifact | `operational_route` 及全部下游 | `operational_route` |
| `operational_route` | **authoritative** | eligible validation、用户确认、expected fingerprint | R: terrain validation passed、所有适用建筑验证 passed、确认、版本一致 | `completed` 或 `completed_with_warnings` / `ready[_with_assumptions]` | 允许已登记 ASSUMABLE 基线，但必须披露；任何 provisional-only 缺口阻断发布 | 发布记录指纹；ProjectState 保留权威 adoption record，重几何可引用 artifact | `required_cns` 至 `report` | `required_cns` |
| `required_cns` | provisional recommendation → adopted **authoritative** canonical result | operational route、aircraft profile、要求来源/工程基线、用户 Adopt | R: operational route、canonical adopted `required_cns`（对下游）；A: Generic Engineering Aircraft Profile、正式 requirement source 的替代工程基线；O: recommendation policy | 采用真实来源可 `completed`；采用允许基线为 `completed_with_warnings` / `ready_with_assumptions` | Engineering Required CNS Baseline 必须先显式 Adopt；recommendation 不得自动写权威结果 | 需求内容、来源类型、assumption IDs 与确认指纹；通常内联保存 | `coverage` 至 `report` | `coverage` |
| `coverage` | provisional → authoritative canonical result | adopted required CNS、设备、既有设施基线、route 3D profile、terrain | R: canonical adopted required CNS、route、可执行 existing baseline；A: sample spacing、demo catalog、`assume_empty_for_planning` | `completed[_with_warnings]` / `ready[_with_assumptions]` | assumed-empty 必须引用 assumption 且不得声称现实无设施；覆盖未知不等同零覆盖 | 覆盖输入/策略指纹；coverage samples 在 artifact | `service_capability` 至 `report` | `service_capability` |
| `service_capability` | provisional → authoritative canonical result | coverage、设备技术参数、adopted Required CNS | R: coverage、canonical Required CNS、服务模型；A: 明示的软件/工程基线 | `completed[_with_warnings]` / 同上 | evidence-limited 能力保持 unknown；继承上游 assumption IDs | 输入与模型指纹；详细 provider/evidence 在 artifact | `service_corridor` 至 `report` | `service_corridor` |
| `service_corridor` | provisional → authoritative canonical result | operational route、coverage、service capability、corridor policy | R: authoritative route、coverage、capability；A: 经登记的空间采样策略 | `completed[_with_warnings]` / 同上 | 完整明细可能约 82 MB；不得内联；继承所有事实基线限定 | 摘要指纹；corridor cells、空间单元与 evidence 强制为 artifact | `capability_gap` 至 `report` | `capability_gap` |
| `capability_gap` | provisional → authoritative canonical result | service corridor、adopted Required CNS、冗余证据、planning objectives | R: corridor、canonical requirements、可执行 existing baseline；E: 独立 provider 证据 | `completed[_with_warnings]` / `ready[_with_assumptions]` | 缺独立性证据时 outcome 为 unknown/evidence-limited；assumed-empty 必须披露 | 缺口输入指纹；区间/空间明细为 artifact | `facility_plan` 至 `report` | `facility_plan` |
| `facility_plan` | provisional proposal → authoritative plan | capability gap、已有设施基线、候选站址、共塔事实、策略 | R: gap、可执行 existing baseline；A: assumed-empty planning mode；O: candidate sites、towers；E: verified tower height | `completed[_with_warnings]` / `ready[_with_assumptions]` | 缺可选候选只影响 tier；假设为空时报告固定披露 | 方案输入指纹；方案摘要内联，计算明细为 artifact | `plan_review`、`report` | `plan_review` |
| `plan_review` | provisional selection → authoritative confirmation | authoritative facility plan variants、证据摘要、用户选择/确认 | R: 至少一个 authority-eligible 方案、非 stale 上游、reviewer confirmation | `completed[_with_warnings]` / `ready[_with_assumptions]` | Select ≠ Confirm ≠ Apply；警告与 assumptions 必须确认；provisional proposal 不得 Confirm | review/adoption 指纹；权威 review record 内联 | `report`；Apply 后失效同 scope 旧 active report | `report` |
| `report` | authoritative immutable artifact | active confirmed plan 及冻结的 route、requirements、coverage/capability/gap/plan/review 摘要 | R: scope 内 active confirmed plan；A: 无新增假设，只继承并披露 | `completed[_with_warnings]` / `ready[_with_assumptions]` | 未评估项、assumed-empty 固定声明和全部 active assumptions 必须进入报告 | manifest 指纹；HTML/PDF/交付包为 artifact | 无 | `[]` |

`planning_constraint_field` 是 `environment` 的派生 artifact，不是第 14 个 canonical node。它按 `(workspace/grid, altitude_layer_id, source fingerprints, policy fingerprints)` 物化，最小摘要契约为：

```json
{
  "artifact_type": "planning_constraint_field",
  "constraint_field_id": "...",
  "altitude_layer": {
    "altitude_layer_id": "ALT-080",
    "layer_orthometric_m": 80.0,
    "vertical_reference": "egm2008_orthometric"
  },
  "cell_states": ["pass", "blocked", "unknown"],
  "unknown_policy": "block_search",
  "domains": ["terrain", "building", "tower", "airspace", "critical_site"],
  "counts": {"pass": 0, "blocked": 0, "unknown": 0},
  "source_fingerprints": {},
  "policy_fingerprints": {},
  "artifact": {"locator": "...", "sha256": "...", "schema_version": "..."},
  "warnings": []
}
```

逐 cell/domain evidence（高度、required floor、reason codes、constraint IDs、protection geometry refs）只保存在 artifact；ProjectState 与 workflow snapshot 不得内联。单元合成优先级为 `blocked > unknown > pass`，但必须保留所有 domain verdict，禁止用 dominant verdict 丢失证据。

### 3.3 输出成熟度与 authority gate

| 成熟度 | 用途 | 允许动作 | 禁止动作 |
|---|---|---|---|
| `provisional` | 数据准备未完成或使用尚未满足 authority gate 的候选/试算；用于比较、排查与继续准备 | 保存为候选 artifact、比较、重算、补充证据 | 直接写 `operational_routes`、直接写 `confirmed_plan`、对外宣称正式结论 |
| `authoritative` | 对应 REQUIRED validation/evidence 已完成，唯一 production writer 已执行 Adopt/Publish/Confirm | 进入 canonical 下游、形成 active record、生成报告 | 绕过 owner、隐藏 assumptions、把适用范围扩大为外部事实 |

ASSUMABLE 输入在 assumption registry 中被显式采用后，可以支持 authoritative planning result；该“权威”只在记录的假设边界内成立，不证明假设对应现实事实。任何标记为 `provisional_only` 的缺口仍会阻断 authority gate。route candidate 本身永远是 provisional，只有经过正式 validation 和 Operational Adoption 才能产生 authoritative operational route。

### 3.4 失效语义

- 上游 authoritative input、policy、confirmation、active ID 或 artifact fingerprint 变化时，下游从任何完成态转为 `stale`。
- `stale` 结果只可查看，不可继续参与 production Apply 或生成新的权威结果。
- 失效图必须成为单一声明式事实源；现有命令式 invalidator 在迁移期作为执行器保留。
- `report` 是失效链末端；历史报告保留不可变文件，但 manifest 标记其相对当前项目为 stale。
- 实验和 Advanced 节点不得反向使 production 节点 stale，除非用户显式把其输出采纳为 production 输入；而本契约禁止研究 planner 输出被采纳为运行航路。

---

## 4. Production / Advanced / Archive 最终模块表

本表是最终目标分类，不是对审计报告现状分类的重述。

| 领域/模块 | 最终区域 | 目标职责 | 迁移约束 |
|---|---|---|---|
| `LayeredRiskAwareThetaStarV2` | Production | 唯一正式航路候选 planner | 保持现有算法语义和测试指纹 |
| RouteRiskProfile | Production | 正式候选风险画像 | 只消费 production candidate |
| terrain/building continuous validation | Production neutral module | 正式连续验证原语 | 先从 V3-C 抽取，再让 production 与 research 分别依赖 neutral module |
| Layered Operational Adoption | Production | 唯一运行航路发布用例 | 拒绝非正式 planner provenance |
| `GridGraph` | Production common infrastructure | 网格邻接公共原语 | 先从 `RiskAwareRoutePlannerV2` 抽取；不得保留向 archive planner 的反向依赖 |
| GeometricCoverage3D | Production | 唯一正式覆盖计算 | 重采样明细外置 |
| CNSServiceCapability | Production | 正式能力评估 | assessment 与 workflow status 分离 |
| Service Corridor / Corridor Gap / Corridor Site Planning | Production | 正式 CNS 能力与设施链 | corridor 空间单元及 evidence 强制外置 |
| Required CNS recommendation models | Production supporting | 生成可解释建议，供用户显式 Adopt | 无 `required_cns` 直接写权；唯一 writer 仍是 command owner |
| Plan Review / Report / Export | Production | 评审、确认、报告与交付 | 只读取 canonical summaries/artifacts |
| Radar surveillance planning | Production，Step 5 | 监视设施规划支线 | Radar detail 外置；默认 OPTIONAL，仅当 adopted Required CNS 或 surveillance policy 明确要求 Radar 时成为 REQUIRED |
| source audit、workspace/grid、risk、Project/Directory、invalidation、persistence | Production infrastructure | 六步公共支撑 | 失效规则声明式化；旧项目兼容读取 |
| Timeline / Protection | Advanced | 时间与保护分析 | 不得阻塞核心六步，不得成为 facility plan 隐式前置 |
| Reliability / Safety / DAA | Advanced | 进阶安全和遭遇分析 | 经 Application service 访问；无证据时不得形成 production 结论 |
| planning exposure / communication field / planner benchmark | Advanced | 进阶研究与对照 | 默认关闭，不写 production 权威结果 |
| `RoutePlannerV1` | Compatibility/Archive | 旧项目读取、对照 | 不在主界面；移除 `operational_routes` 写权 |
| `RiskAwareRoutePlannerV2` | Compatibility/Archive | 旧项目读取、对照 | `GridGraph` 抽取后归档；移除写权 |
| `LayeredRoutePlannerV1` | Compatibility/Archive | 旧 selection 的只读兼容 | 移除 service 默认/兜底；不发布运行航路 |
| RoutePlannerV3 A/B/C/D | Research/Archive | 研究实验 | V3-C 公共验证器抽出；V3-D 移除 production publish 权 |
| CoveragePlannerV1 2D | Compatibility/Archive | 旧项目/审计查看 | 退出门禁与主界面；不写 canonical `coverage` |
| CNSGapAnalyzerV1 | Compatibility/Archive | 旧结果读取 | 不写 canonical `capability_gap` |
| CNSGapAnalyzerV2（非 corridor gap） | Advanced/Compatibility | 旧区间缺口分析与对照 | 不在核心链，不写 canonical `capability_gap` |
| ReuseFirstSitePlannerV1 | Compatibility/Archive | 旧提案读取 | 不写 canonical `facility_plan` |
| ClosedLoopService | Advanced | working-copy 试算与前后对照 | 不直接 Apply `confirmed_plan`；需要确认时委托 PlanReviewService |
| legacy schema/UI、纯 facade、空壳/真孤儿 | Archive → Delete candidate | 兼容或待删除 | 满足第 13 节前置后才能删除 |

Archive 的含义是：冻结功能、禁止新增产品能力、禁止成为新项目默认值、禁止获得 production write authority；它不等于立即删代码。

---

## 5. Data Requirement Matrix

### 5.1 统一等级

| 等级 | 定义 | 缺失行为 |
|---|---|---|
| `REQUIRED` | 当前节点达到其声明成熟度不可缺少的事实或已确认策略 | 缺失时 authority gate=`blocked`；若节点契约明确允许，仍可生成带 warning 的 provisional 结果 |
| `ASSUMABLE` | 有明确、可解释、可登记的软件/工程基线，可在用户知情下继续 | readiness=`ready_with_assumptions`；必须登记 assumption |
| `OPTIONAL` | 不属于核心结论；缺失只使相关分支 `not_evaluated` | 核心节点仍可 ready，不得用零值代替 |
| `ENHANCEMENT` | 只提升精度、证据或方案质量 | 不改变核心 readiness；在 warnings/未评估清单披露 |

### 5.2 关键输入矩阵

| 输入 | 等级 | 主要节点 | 缺失或未确认时的契约 |
|---|---|---|---|
| workspace、规划网格、scenario OD | REQUIRED | environment、route_candidate | blocked |
| fixed cruise altitude、vertical reference、planning request | REQUIRED | route_candidate | blocked；不得猜测垂向基准；`ALT-080 = 80 m EGM2008 orthometric`，不是 AGL |
| Population | REQUIRED | environment、risk_field、route_candidate | 缺失时不能生成 route candidate，blocked |
| Shelter coefficient | ASSUMABLE | risk_field、route_candidate | 无真实数据时允许 `value=1.0, basis=engineering_baseline`，readiness=`ready_with_assumptions`；不得描述为真实遮蔽事实 |
| Planning Constraint Field | REQUIRED | environment artifact、route_candidate、route_validation lineage | 必须与选定高度层、来源和策略指纹一致；cells 至少 `pass/blocked/unknown`。`blocked` 不可扩展；production 默认 unknown 不可扩展，禁止 unknown→pass |
| Terrain orthometric evidence + vertical clearance | REQUIRED for fixed-layer constraint | environment、route_candidate、route_validation、operational_route | `layer < terrain + clearance` 为 blocked；缺 height/datum/clearance 为 unknown，绝不补 0；搜索 mask 与 native continuous validation 分别执行 |
| Building footprint/height/ground + clearance | 条件性 REQUIRED；覆盖范围内适用即 REQUIRED | environment、route_candidate、route_validation、operational_route | `top = resolved_ground + height`；`layer < top + clearance` 为 blocked；缺 ground/height、不可接受 `height_status`、无效 geometry 或 CRS 未解析为 unknown；不得以缺数据推导 not_applicable |
| Tower Source Facts / confirmed tower top + clearance | 障碍约束条件性 REQUIRED；Step 5 共塔 OPTIONAL | environment、route_candidate、route_validation、facility_plan | 同一 source facts 双派生，不复制源数据；只有 confirmed `tower_top_orthometric` 可 hard 判定，楼面塔/base datum/mounting semantics 不明确为 unknown |
| Airspace / regulatory constraint geometry and policy | 条件性 REQUIRED | environment、route_candidate、route_validation、operational_route | display-only 图层不能冒充规划约束；confirmed hard exclusion 进入 blocked。未配置/未确认必须 `not_evaluated/unknown`，authority gate 行为由显式 policy 决定 |
| RestrictedArea / ProtectedSite facts + confirmed protection geometry | 条件性 REQUIRED | environment、route_candidate、route_validation、operational_route | 支持 point/polygon/multipolygon source facts，但规划仅消费 confirmed protection geometry；radius 必须来自 source 或显式 policy，禁止按 category 猜测；无数据可 provisional + warning，但不得静默视为无禁飞区 |
| route cost policy | REQUIRED | route_candidate | 未确认 blocked；显式 0 是合法输入而非缺失 |
| 搜索参数、目标策略、risk density | ASSUMABLE | route_candidate | ready_with_assumptions；保留来源与适用范围 |
| population NoData policy | ASSUMABLE | environment、risk_field | ready_with_assumptions；默认解释是 missing，不是人口 0 |
| Aircraft Profile source | ASSUMABLE | required_cns、coverage/service calculations | 无真实 profile 时允许显式采用 Generic Engineering Aircraft Profile，readiness=`ready_with_assumptions`；不得冒充具体机型事实 |
| canonical adopted `required_cns` | REQUIRED downstream | coverage 至 report | 未 Adopt 时下游 blocked；只有唯一 command owner 可写 |
| formal Requirement source | ASSUMABLE | required_cns | 缺失时允许生成 Engineering Required CNS Baseline；必须由用户显式 Adopt 后才成为 canonical `required_cns`，并保持 `ready_with_assumptions` |
| `cns_existing_baseline` | 条件性 REQUIRED/ASSUMABLE | coverage 至 facility_plan | `not_declared + factual` blocked；`not_declared + assume_empty_for_planning` 为 ready_with_assumptions；其余见 5.3 |
| coverage sample spacing、明确允许的 demo device catalog | ASSUMABLE | coverage | ready_with_assumptions，并显著警告数据等级 |
| candidate sites | OPTIONAL | facility_plan、radar | 不阻塞；对应 reuse tier 未评估 |
| traffic/conflict、property、infrastructure、land mask | OPTIONAL | 风险、radar | 对应 assessment unknown/not_evaluated；不得补 0；不得混入 Constraint Field 冒充 hard constraint |
| provider independence、checksums | ENHANCEMENT | gap、facility_plan、audit | 提升证据；缺失不改变无关核心节点可运行性 |
| Radar data/policy | OPTIONAL，条件满足时 REQUIRED | Step 5 Radar branch | 默认不阻塞；当 adopted Required CNS 或 surveillance policy 明确要求 Radar 时，该分支及其必要输入成为 REQUIRED |
| Timeline / Protection / Reliability / Safety / DAA 输入 | OPTIONAL/ENHANCEMENT | Advanced | 仅影响 Advanced 分支，不阻塞核心六步 |

### 5.3 Existing CNS 基线

必须新增并统一使用：

```json
{
  "cns_existing_baseline": {
    "knowledge_status": "not_declared | confirmed_none | confirmed_present",
    "planning_mode": "factual | assume_empty_for_planning",
    "source": null,
    "evidence_ref": null,
    "declared_at": null,
    "declared_by": null
  }
}
```

语义：

| knowledge_status + planning_mode | readiness | 下游行为 |
|---|---|---|
| `not_declared + factual` | `blocked` | 事实未知，coverage、capability、corridor gap、facility plan 不得形成可采纳结论 |
| `not_declared + assume_empty_for_planning` | `ready_with_assumptions` | 建立 active assumption registry item；允许以空既有设施工程规划基线继续，不得描述成现实中不存在 CNS |
| `confirmed_none + factual` | `ready` | 可合法形成“事实已确认无既有能力”的零基线 |
| `confirmed_none + assume_empty_for_planning` | `ready`，但规范化为 `factual` | 已有事实确认，无需继续保留假设模式 |
| `confirmed_present + factual` | facilities/evidence 可解析时 `ready`，否则 `blocked` | 使用确认存在的设施事实 |
| `confirmed_present + assume_empty_for_planning` | `blocked` | 与已确认存在的事实冲突，禁止用空基线覆盖 |

`not_declared + assume_empty_for_planning` 的 assumption 至少记录 `field=cns_existing_baseline`、`value=empty`、`basis=engineering_baseline`、适用节点、采用人和报告披露文本“空既有设施工程规划基线”。它允许继续规划，但不改变 `knowledge_status`。

旧项目 additive migration 一律回填：

```json
{
  "knowledge_status": "not_declared",
  "planning_mode": "factual"
}
```

禁止从空 `existing_cns_facilities` 推断 `confirmed_none`，也禁止迁移过程自动启用 `assume_empty_for_planning`。

---

## 6. Assumption Registry Schema

### 6.1 最小 schema

```json
{
  "assumptions": {
    "items": [
      {
        "assumption_id": "asm-...",
        "scope": "project | node | artifact",
        "node_id": "route_candidate",
        "field": "search.heading_bin_count",
        "value": 8,
        "unit": null,
        "basis": "software_baseline | engineering_baseline | user_declaration | source_limitation",
        "reason": "...",
        "source_ref": null,
        "owner": "system | user-id | role",
        "confirmed": false,
        "authority_effect": "allowed_with_disclosure | provisional_only",
        "report_disclosure": "...",
        "created_at": "RFC3339",
        "expires_at": null,
        "invalidates_on": ["policy-change", "source-change"],
        "status": "active | superseded | withdrawn"
      }
    ]
  }
}
```

### 6.2 规则

- 每个 `ASSUMABLE` 缺失或未确认项都必须对应一个 active assumption；不能只留散落字符串。
- assumption 必须有作用域、值、依据、owner、确认状态和失效条件。
- assumption 改变时，所有消费该 assumption 的 node 必须 stale。
- `ready_with_assumptions` 必须返回 `assumption_ids`，前端以业务语言展示摘要。
- assumption 不得把 `REQUIRED` 输入降级为可选，也不得把 unknown assessment 转成 passed。
- `authority_effect=allowed_with_disclosure` 可在完成 REQUIRED validation/evidence 后支持 authoritative planning result；`provisional_only` 永远不能通过 Publish/Confirm gate。
- `cns_existing_baseline.knowledge_status=not_declared` 仍是事实未知；只有用户选择 `planning_mode=assume_empty_for_planning` 后，才能建立独立 assumption 并继续。该 assumption 不得改写 knowledge status。
- 审计/API 可见完整 registry；production 主界面只显示“采用了哪些假设、影响什么、如何解除”。

### 6.3 最低必备假设模板

| 场景 | registry 最低内容 | authority effect | 强制披露 |
|---|---|---|---|
| 无真实 Shelter coefficient | `field=shelter_coefficient`、`value=1.0`、`basis=engineering_baseline` | `allowed_with_disclosure` | “采用遮蔽系数 1.0 工程基线；不是现场遮蔽事实” |
| 无真实 Aircraft Profile | `field=aircraft_profile`、Generic Engineering Aircraft Profile ID/版本、适用范围 | `allowed_with_disclosure` | “采用通用工程飞行器能力基线；不是具体机型数据” |
| 无正式 Requirement source | Engineering Required CNS Baseline 的内容、版本、依据及 Adopt 记录 | Adopt 前 `provisional_only`；显式 Adopt 后 `allowed_with_disclosure` | “CNS 需求来自工程基线，不是正式要求来源” |
| Existing CNS 未声明但按空基线规划 | `field=cns_existing_baseline`、`value=empty`、`basis=engineering_baseline`、planning scope | `allowed_with_disclosure` | **“空既有设施工程规划基线；不表示现实中不存在 CNS”** |
| fixed-layer constraint evidence 缺 terrain/building/tower 高度事实 | 不得用 assumption 伪造事实；Constraint Field 对应 cell=`unknown`、production policy=`block_search` | 不产生可宣称 feasible 的正式候选；更不能 Publish | “障碍证据不足；unknown 未按 0 或 pass 处理” |
| 无 RestrictedArea / ProtectedSite 数据 | `field=critical_site_dataset`、`basis=source_limitation`、适用 authority policy 与范围 | 仅在 policy 明确允许时 `provisional_only`；始终 `not_evaluated` | “要地/保护区未评估；不表示不存在禁限区” |

---

## 7. Workflow Status Contract

### 7.1 唯一 workflow lifecycle enum

```text
not_started
blocked
ready
ready_with_assumptions
running
completed
completed_with_warnings
failed
stale
```

| 状态 | 定义 | 允许的主要转移 |
|---|---|---|
| `not_started` | 尚无执行意图，或上游尚未到达可评估阶段 | → blocked / ready / ready_with_assumptions |
| `blocked` | 当前请求动作或目标成熟度所需的 REQUIRED 依赖缺失、无效或未确认 | → ready / ready_with_assumptions / not_started |
| `ready` | REQUIRED 已满足，且没有 active assumption | → running / blocked / stale |
| `ready_with_assumptions` | REQUIRED 已满足，至少一个 ASSUMABLE 由 registry 覆盖 | → running / ready / blocked / stale |
| `running` | 已创建执行，正在产生进度；Heavy Task 必须可取消 | → completed / completed_with_warnings / failed / ready / ready_with_assumptions |
| `completed` | 执行成功、结果完整、无未处置 warning | → stale |
| `completed_with_warnings` | 执行成功，但含假设、未评估项、provisional-only 缺口或其他非执行失败 warning | → stale |
| `failed` | 执行或基础设施失败；不是领域 assessment failed | → running（显式重试）/ blocked / ready / ready_with_assumptions |
| `stale` | 曾完成的结果与当前输入/策略/确认/上游指纹不一致 | → running（重算）/ blocked / ready / ready_with_assumptions |

取消不是持久化终态：取消完成后回到执行前的 `ready` 或 `ready_with_assumptions`；若取消期间上游变化则为 `blocked` 或 `stale`。取消事件写审计日志，未提交的临时 artifact 不成为 active result。

### 7.2 `status` 与 `readiness`

- `status` 是节点 lifecycle 的持久化摘要，使用完整九态 enum。
- `readiness.state` 是执行前判定，只能是 `blocked / ready / ready_with_assumptions`，并列出 blockers 和 assumption IDs。
- 节点处于 `running/completed/completed_with_warnings/failed/stale` 时仍可重新计算 readiness，用于说明能否重试或重算；二者不得互相覆盖。
- readiness 必须带 `operation` 或 `target_maturity` 上下文。同一 route node 可以对“生成 provisional candidate”为 ready，同时对“Publish operational route”为 blocked。

### 7.3 领域 assessment 分离

领域判断只使用：

```text
passed
failed
unknown
```

规则：

- workflow `completed` 表示计算成功，不表示 assessment `passed`。
- assessment `failed` 表示业务/工程检查不通过，不表示执行失败。
- assessment `unknown` 表示证据不足或该维度未评估；不得映射为 `passed` 或数值 0。
- 可选或增强输入缺失时，对应 assessment 为 `unknown`，workflow 可为 `completed_with_warnings`。
- 当前动作的 REQUIRED 证据缺失时，在执行前 `blocked`；若节点契约和显式 authority policy 允许 provisional 执行（例如要地数据未提供但 policy 允许试算），可 `completed_with_warnings`，但 authority gate 保持 blocked。terrain/building/tower 高度缺失产生 Constraint Field `unknown`，不得用 assumption 变成 `pass`。

### 7.4 Workflow status 与 output maturity 的组合规则

| workflow status | provisional output | authoritative output |
|---|---|---|
| `completed` | 允许；表示候选计算完整，不授予发布权 | 仅唯一 writer 完成 authority gate 后允许 |
| `completed_with_warnings` | 常见；用于缺可选数据、采用假设或存在 provisional-only 缺口 | 仅 warnings 全部为 `allowed_with_disclosure` 且 REQUIRED evidence 完整时允许 |
| `blocked` | 可保留此前候选 artifact，但不得新执行被阻断动作 | 不得产生或替换 active authoritative record |
| `stale` | 只读、可比较，必须重算后再使用 | 不得继续参与新的 Adopt/Publish/Confirm |

`output_maturity` 必须随 API response、artifact manifest 和 workflow summary 返回。provisional 结果可以继续被比较或用于补充准备，但不得直接写 `operational_routes` 或 `confirmed_plan`。从 provisional 到 authoritative 不是字段翻转，而是由唯一 production writer 在验证输入指纹和 authority gate 后创建新的 adoption/confirmation record。

---

## 8. ProjectState Target Schema

### 8.1 允许持久化的内容

`ProjectState` 只保存：

- authoritative user inputs；
- policies；
- confirmations；
- active IDs；
- artifact summaries/fingerprints；
- authoritative adoption/review records。

目标逻辑结构：

```json
{
  "schema_version": "2.x-additive",
  "revision": 0,
  "project": {},
  "inputs": {
    "workspace": {},
    "sources": {},
    "scenario_routes": [],
    "aircraft_profiles": [],
    "required_cns": {},
    "existing_cns_facilities": [],
    "candidate_sites": []
  },
  "policies": {},
  "confirmations": {
    "cns_existing_baseline": {
      "knowledge_status": "not_declared",
      "planning_mode": "factual"
    },
    "planning_request": {},
    "required_cns": {}
  },
  "active_ids": {
    "scenario_route_id": null,
    "operational_route_id": null,
    "aircraft_profile_id": null,
    "facility_plan_id": null,
    "active_confirmed_plan_by_scope": {
      "<scenario_id>:<operational_route_id>": "<confirmed_plan_id>"
    },
    "report_id": null
  },
  "workflow": {
    "nodes": {}
  },
  "assumptions": {
    "items": []
  },
  "artifacts": {
    "index": {}
  },
  "authoritative_records": {
    "route_adoptions": [],
    "required_cns_adoptions": [],
    "plan_reviews": [],
    "confirmed_plans": [],
    "report_manifests": []
  },
  "compatibility": {
    "source_schema": null,
    "migration_notes": []
  }
}
```

这是逻辑目标，不要求一次性重命名当前 99 个顶层 key。迁移期允许现有物理结构继续存在，但新写入必须能映射到该逻辑结构，并遵守大型明细不内联原则。

### 8.2 禁止继续进入 ProjectState 的内容

以下大型或可重算结果必须进入 artifact/sidecar：

- grid cells；
- grid attributes cells；
- risk cells；
- coverage samples；
- corridor spatial cells、corridor voxels 和全部 evidence；
- radar detail/cells；
- 实验 traces、refinements、validations 和 benchmark 明细。

同样禁止在 workflow snapshot 中复制这些内容。snapshot 只包含状态、计数、extent、关键指标、warnings、assumption IDs、fingerprint 和 artifact locator。

### 8.3 兼容规则

- 旧项目先经 normalizer 读取，再映射为 canonical view；原文件只有在用户显式保存时才升级。
- 旧 key 可读，但 canonical service 不得产生新的 legacy key、legacy result 或 legacy selection。
- legacy 数据迁移不得推断用户未作出的确认，尤其不得把空 `existing_cns_facilities` 迁移成 `confirmed_none` 或自动启用 `assume_empty_for_planning`。
- 保存新项目时只写 canonical state + compatibility metadata；不得双写新的 legacy state。
- 兼容 writer 只允许用于“原格式导出副本”，不得成为活动项目的默认保存路径。
- `confirmed_plans` 历史记录可有多个；每个 `(scenario_id, operational_route_id)` scope 只允许一个 active confirmed plan，新确认只能原子替换同 scope 的 active pointer，不删除历史记录。

---

## 9. Artifact Storage Model

### 9.1 模型

复用现有内容寻址 sidecar 机制并扩展覆盖范围：

```text
ProjectState.artifacts.index
  → artifact manifest
      {artifact_id, kind, sha256, media_type, compression, byte_size,
       item_count, schema_version, producer, created_at,
       input_fingerprint, output_maturity, assumption_ids,
       summary, storage_locator}
  → .cns-results/<sha256>.json.gz 或其他受支持的不可变对象
```

要求：

- artifact 以内容寻址、不可变、sha256 校验；同内容可去重。
- 每个 artifact 有 schema version、producer、input fingerprint、大小、计数和摘要。
- ProjectState 只保存 manifest/index 和 active artifact ID，不保存大型 payload。
- Save As、Open、Export 必须把引用的 artifact 作为同一项目交付单元处理。
- sidecar 缺失或校验失败必须产生明确错误/blocked 状态，禁止静默按空结果继续。
- 新 artifact type 必须同时登记 retention、hydration API、snapshot summary、失效和清理策略。
- active artifact、任何 confirmed record 引用的 artifact、任何 report manifest 引用的 artifact 永久保留，不进入垃圾回收候选。
- 其他 stale/unreachable artifact 在 Phase4 本阶段不自动删除；只提供 dry-run GC 清单，实际删除策略留待 Phase4 之后另行授权。

### 9.2 强制外置策略

| Artifact kind | 强制外置内容 | snapshot 允许内容 |
|---|---|---|
| `grid` | 全量 cells | count、levels、extent、CRS、sha256 |
| `grid_attributes` | 每类逐 cell 属性 | completeness、min/max、missing count、sha256 |
| `risk_field` | risk cells / factors detail | 汇总分布、unknown count、sha256 |
| `coverage` | 逐点/逐设施 samples | coverage ratio、provider counts、warnings、sha256 |
| `service_corridor` | **全部 corridor spatial cells/voxels/evidence，无论大小** | route count、cell count、covered/gap volume、warnings、sha256 |
| `radar_layout` | candidate/detail cells、求解明细 | selected sites、coverage metrics、solver summary、sha256 |
| `experiment` | traces、refinements、validations、benchmark series | experiment ID、结论摘要、sha256 |
| `report` | HTML/PDF/交付包 | title、generated_at、source fingerprints、sha256 |

服务走廊采用按类别强制外置，而不是仅按文件大小阈值外置。约 82 MB 的观测证明该结构不能继续依赖普通 snapshot 的“足够小”假设。

### 9.3 Artifact API

- `GET /api/artifacts/{artifact_id}/summary`：production UI 摘要。
- `GET /api/artifacts/{artifact_id}/content`：按权限流式读取，支持分页/范围读取；不得先 hydrate 到全局 workflow snapshot。
- `GET /api/artifacts/{artifact_id}/manifest`：高级/审计信息。
- 删除 active、confirmed 或 report-referenced artifact 必须拒绝。
- Phase4 只提供 stale/unreachable artifact 的 dry-run GC：输出 artifact ID、大小、最后引用、不可达依据和预计释放空间，不执行自动删除。

---

## 10. API Owner / Production Write Authority

### 10.1 单写原则

Router 只做传输、鉴权、并发版本检查和命令分发；不得直接调用领域算法写状态。每个 authoritative result 由一个 Application owner 负责验证、写入、失效和审计。

| Authoritative result | 唯一 production writer / API owner | 允许输入 | 禁止写入者 | 写入条件 |
|---|---|---|---|---|
| `operational_routes` | `LayeredOperationalAdoptionService`（未来可显名 `OperationalRoutePublishService`） | Theta* V2 candidate + RouteRiskProfile + terrain/building validation + explicit confirmation | RouteService/RoutePlannerV1、RiskAwareRoutePlannerV2、LayeredRoutePlannerV1、V3-D、实验服务 | candidate 虽可 provisional，但写入前必须有可验证 terrain evidence、所有 policy-applicable building evidence、合法 provenance 和匹配的 expected fingerprint |
| `required_cns` | `RequiredCNSCommandService`；迁移期由 `CNSInputService` 作为唯一 facade，统一 direct confirm 与 recommendation adopt 两种命令 | 正式要求，或用户显式 Adopt 的 Engineering Required CNS Baseline/recommendation | recommendation evaluator、router 内联逻辑、importer 直接写 state | 用户显式 Adopt、来源/基线类型与 assumptions 记录、revision 匹配；recommendation 永不自动写入 |
| canonical `coverage` | `Spatial3DService` + `GeometricCoverage3D`，作为一个 Application owner | operational route、canonical adopted required CNS、设施与设备、existing baseline | CoveragePlannerV1 2D、Advanced/Research 服务 | `factual` 基线有效，或 `assume_empty_for_planning` 有 active assumption；结果必须携带 maturity 与 assumption IDs |
| canonical `facility_plan` | `CorridorSitePlanningService` | canonical capability gap、planning objectives、现有设施/候选站址 | `SitePlanningService` / ReuseFirstSitePlannerV1、实验服务 | 上游非 stale、proposal provenance 完整；provisional proposal 不得直接 Confirm |
| `confirmed_plan` | `PlanReviewService` | authority-eligible facility plan variant + evidence + reviewer confirmation | ClosedLoopService、report service、router、任何 planner | Select、Confirm、Apply 顺序明确；expected plan fingerprint 匹配；每个 scenario/operational route scope 仅一个 active，历史记录不删除 |

补充：

- `RadarSurveillanceLayoutService` 是 radar layout 的唯一 production writer；Radar 默认 OPTIONAL，只有 adopted Required CNS 或 surveillance policy 明确要求时才成为对应 Step 5 scope 的 REQUIRED 分支。
- `PlanningReportService` 是 report manifest 的唯一 writer；只消费 `confirmed_plan` 及其冻结引用。
- `ClosedLoopService` 可在 working copy 中重算候选方案，但不能直接写 `confirmed_plan`；Apply 必须委托 `PlanReviewService`。
- compatibility/archive API 必须返回独立 namespace 的 result，不得写 canonical key。
- production write authority 必须由服务层 allowlist 和 contract test 双重保护，而不是只靠隐藏前端按钮。

### 10.2 Heavy Task、progress 与 cancellation 目标契约

适用于 corridor、coverage、service capability、radar、批量验证及任何超出交互式时限的计算。目标运行形态固定为**本机持久 worker process**：任务队列和任务状态持久化在本机项目/应用数据中，桌面应用通过本机 IPC/HTTP 与 worker 通信；Phase4 不引入 Redis、Celery 或任何外部消息系统。

```text
POST command
  → 202 Accepted + task_id + initial node status=running
  → worker reads immutable input snapshot
  → progress events {stage, completed, total, percent?, message, heartbeat_at}
  → cooperative cancellation checkpoints
  → temporary artifact
  → short mutation lock: compare revision/fingerprint, atomically publish manifest + summary
```

API 最小面：

- `POST /api/tasks` 或各 command 返回 `202 + task_id`；
- `GET /api/tasks/{task_id}` 返回状态和最新进度；
- `POST /api/tasks/{task_id}/cancel` 请求取消；
- 可选 SSE/event stream 用于进度，不要求客户端轮询大型 state；
- cancellation 必须在算法循环/批次边界检查，不能只取消 HTTP 客户端连接；
- mutation lock 只覆盖最终 revision 检查与原子发布，不覆盖整个长计算；
- 取消、失败或 fingerprint 冲突不得替换 active artifact；临时 artifact 进入可回收区；
- worker 崩溃后任务可判定失败，不得无限保持 running。
- 应用 UI 退出不等同任务取消；本机 worker 重启后应从持久状态恢复为可继续、可重试或明确 failed，具体恢复能力由 task kind 声明。

该能力列入目标架构和后续实现批次，**不在 B0.1 文档阶段实现**。

---

## 11. Frontend Information Architecture

### 11.1 每个 Step 的固定结构

每一步固定按以下六块呈现：

1. 目标
2. 输入准备
3. 阻塞项/假设
4. 主操作
5. 结果
6. 下一步

主界面只呈现 canonical workflow node 的业务状态。Advanced/审计通过二级入口展开，不与主操作并列，不拥有 production Apply/Publish 按钮。

### 11.2 六步页面内容

| Step | 主界面 | Advanced/审计 |
|---|---|---|
| 1 数据准备 | 项目、数据源、来源健康、工作区输入清单 | 算法 manifest、schema、来源校验细节 |
| 2 环境与风险 | 工作区、规划网格、Population × Shelter Risk Field；terrain/building/tower/airspace/critical-site 来源与 per-layer Planning Constraint Field 摘要 | 风险因子细节、Constraint Field domain/cell evidence、旧风险模型对照、可选数据层 |
| 3 航路规划与发布 | OD、固定巡航高度（明确显示 EGM2008 orthometric）、Risk Field + Constraint Field、候选成熟度、风险画像、独立 continuous/native authority gate、确认发布 | archived planner 对照、研究实验、详细搜索 mask 与连续验证证据；Departure/Arrival Procedure Validation 显示为 deferred 独立范围 |
| 4 CNS需求 | 真实或 Generic Engineering Aircraft Profile、运行约束、Requirement source/Engineering Baseline、显式 Adopt | Timeline、Protection、Reliability、Safety、DAA |
| 5 CNS能力与设施规划 | Existing CNS knowledge status + planning mode、三维覆盖、能力、服务走廊、缺口、站址方案、条件性 Radar | 旧二维覆盖/缺口/站址结果、详细空间证据 |
| 6 方案评审与报告 | 比较、选择、确认、应用、报告与交付 | 指纹、provenance、artifact manifest、未评估清单 |

### 11.3 主界面禁用术语

以下文本不得出现在 production 主界面的标题、按钮、状态卡和下一步说明中：

```text
P7 / P8 / P14 / P15 / P16
legacy
V1 / V2 / V3
fingerprint
voxel
supercover
heuristic
```

它们只允许在“高级/审计”出现。业务文案使用“正式航路规划”“服务单元”“版本校验”“网格穿越”“搜索代价估计”“旧项目兼容”等用户语义；其中“旧项目兼容”也只能在设置/审计区出现。

### 11.4 交互状态

- `blocked`：显示具体缺失事实和解决入口，不显示可执行主按钮。
- `ready_with_assumptions`：显示假设数量、影响和确认/替换入口；允许执行。
- `running`：显示 stage、进度、心跳和取消按钮。
- `completed_with_warnings`：结果和 warning 同屏，禁止隐藏警告。
- `stale`：结果只读，主按钮变为“按当前输入重新计算”。
- 领域 `failed/unknown` 以“检查不通过/证据不足”呈现，不复用 workflow 失败样式。
- 所有结果卡同时显示业务化成熟度：“候选/试算”对应 `provisional`，“已采纳/已发布/已确认”对应 `authoritative`；不能用绿色 completed 徽章暗示 provisional 已正式生效。
- terrain/building/tower 高度或适用 clearance 缺失时显示 Constraint Field `unknown` 与具体 blocker；production 默认不得扩展。无要地数据时只有在显式 authority policy 允许试算的条件下才显示“生成 provisional 候选”，并持续显示 `critical_site_not_evaluated`，不得显示“无禁飞区”。
- Shelter 缺失时提供“采用遮蔽系数 1.0 工程基线”动作，明确说明它不是现场遮蔽事实。
- Aircraft Profile 缺失时允许选择带固定版本的 Generic Engineering Aircraft Profile，并在 Step 4/6 持续显示 assumption。
- 正式 Requirement source 缺失时允许生成 Engineering Required CNS Baseline；只有“显式 Adopt”后才出现 canonical Required CNS，下游显示 `ready_with_assumptions`。Recommendation 只显示建议，不提供隐式写入。
- Existing CNS 控件必须分别编辑 `knowledge_status` 与 `planning_mode`。选择“未知但按空基线规划”时不得把事实状态改成“确认无”，并在 Step 5、Step 6 和报告中显示“空既有设施工程规划基线；不表示现实中不存在 CNS”。
- Radar 默认显示为可选 production 分支；只有 adopted Required CNS 或 surveillance policy 明确要求时才进入阻塞项/下一步门槛。

---

## 12. Legacy Migration Strategy

迁移采用“读旧、写新、先断写权、再归档、最后删除”的 strangler 方式：

1. **建立 canonical view**：normalizer 将旧顶层 key 和旧 algorithm selection 映射到 canonical node，不改变原文件。
2. **建立 authority guard**：先在 Application 层阻止 archive/research writer 写 canonical authoritative result，即使旧 API 仍存在。
3. **抽取公共依赖**：抽 `GridGraph` 到公共基础模块；抽 V3-C continuous validators 到 neutral production module。
4. **切换 production readers/writers**：六步 UI 和 canonical API 只使用唯一 production owner。
5. **外置大型派生结果**：对旧项目首次保存执行 additive compaction；旧内联结果仍可读取，重算后只写 artifact。
6. **冻结兼容入口**：archive 功能立即退出 production UI；旧 API 可返回兼容 namespace 的结果，但响应携带 deprecation/compatibility metadata，不再更新 canonical state。
7. **迁移测试与项目样本**：建立旧项目 fixture、canonical round-trip、无 legacy 新写入、authority guard 和 artifact 完整性测试。
8. **Phase4 全周期保持旧项目读取**：所有 Phase4 批次都必须保留旧项目 open/read/export；不得用物理删除换取前端收敛。
9. **满足删除前置后删除**：物理删除只能发生在 B7/B8，且对应 delete gate 全部通过。

不做以下“伪迁移”：

- 不批量把旧空值解释为用户确认；
- 不静默把旧 planner 选择改成 production planner 后覆盖旧结果；
- 不同时向 canonical 和 legacy 容器双写；
- 不为了减少改动让 archive service 保留 production write authority；
- 不把 82 MB 级结果先塞入 snapshot 再由前端裁剪。

---

## 13. Delete Prerequisites

任何删除必须同时满足适用的前置条件，并在独立批次中完成；物理删除只允许在 B7/B8：

| 对象 | 删除/彻底归档前置 |
|---|---|
| RoutePlannerV1 | 默认 selection 已解除；旧项目 fixture 可读；API/UI 写入口移除；实验对照改读 archive；characterization 测试迁移 |
| RiskAwareRoutePlannerV2 | `GridGraph` 已抽取且所有 production import 改到公共模块；失效逻辑不再按该 algorithm ID；无 production 写入口 |
| LayeredRoutePlannerV1 | service 构造默认和兜底移除；旧 selection 有 compatibility adapter；专属前端迁出；测试迁移 |
| RoutePlannerV3 | continuous validators 已抽到 neutral module；V3-D production publish 权已移除；实验 state/artifact 可导出；API/UI 移入 research/archive |
| CoveragePlannerV1 2D | Step 5 门禁改为 canonical coverage/capability；旧 `coverage` 可读；主界面和默认 selection 不再依赖 |
| CNSGapAnalyzerV1 | canonical gap consumer 全部切到 corridor gap；legacy alias 和默认 selection 解除；旧结果 fixture 可读 |
| ReuseFirstSitePlannerV1 | canonical facility plan consumer 全部切到 corridor site plan；旧提案可读；主界面入口移除 |
| safety reliability 真孤儿/其他空壳 | 先确认 Advanced 产品能力由何模块承接；清理再导出与测试；不得误删仍被 production import 的 neutral primitive |
| services facade / legacy UI | 所有 import 迁移；保存项目不再引用；公布并经过兼容窗口 |

通用 delete gate：

- `rg`/依赖图无 production import；
- 无 production API route、frontend action、ProjectState writer；
- 无默认 registry selection 或 service fallback；
- 旧项目兼容 fixture 通过；
- Python、前端、E2E 和 authority contract tests 通过；
- 有可回滚提交，且不与 P14 PERFORMANCE PATCH CANDIDATE 混合。

---

## 14. Phase4 Implementation Batches

每批独立提交、独立验收；不得把 P14 性能候选工作线混入 Phase4 架构提交。

| Batch | 目标 | 主要交付 | 明确不做 |
|---|---|---|---|
| B0 | 固化目标契约 | 本文；目标 DAG、authority、状态、迁移和验收基线 | 不改生产代码、不宣称性能已解决 |
| B1 | Canonical contract foundation | workflow status/readiness DTO、provisional/authoritative maturity、assessment 三态、assumption registry、双维度 `cns_existing_baseline`、声明式 node registry | 不改 planner 算法语义 |
| B2 | Production write authority | 五类 authoritative result 的 service owner、router 禁止直写、authority contract tests；先移除 V1/V2/V3-D publish 权 | 不删 archive 代码 |
| B3 | Neutral dependency extraction | 抽取 `GridGraph`；抽取 continuous validators；production imports 改指向 neutral modules | 不改计算结果/指纹 |
| B3A | Fixed-Layer Constraint Foundation | Planning Constraint Field domain/artifact、terrain/building/tower/airspace/critical-site adapters、三态与 unknown policy、Theta* V2 feasibility mask 接线、独立 continuous/native validation contract、状态/指纹/sidecar 契约与专项测试 | 不新增 canonical node；不把 Risk Field 与 Constraint Field 合并；不实现 terminal protection volume 或 climb/descent penetration |
| B4 | Six-step frontend convergence | 六步重排、固定页面结构、候选/权威门槛、工程基线动作、主链唯一入口、Planning Constraint Field 状态与 warning、Advanced/Archive 立即退出 production UI、禁用术语清理 | 不改变后端结果语义 |
| B5 | ProjectState/artifact migration | 扩展 compaction；外置 grid/attributes/risk/coverage/**corridor cells+evidence**/radar/experiment；旧项目读兼容；禁止新 legacy state | 不做无依据的数据推断 |
| B6 | Heavy Task platform | 本机持久 worker process、持久任务队列、progress、heartbeat、cooperative cancellation、短锁原子发布、临时 artifact 清理 | 不引入 Redis/Celery/外部消息系统；不以断开 HTTP 充当取消 |
| B7 | Compatibility/archive migration | 旧项目 adapter、archive namespace、selection/read path、deprecation metadata、兼容 fixtures；通过专项 delete gate 后才允许物理删除 | Phase4 全周期不得破坏旧项目读取 |
| B8 | Delete candidates and structural cleanup | 仅对 delete gate 已通过的对象物理删除；拆分大 router/step 文件，收敛 invalidation 单一事实源 | 不提前删除，不与功能算法优化混批 |
| B9 | Performance qualification | 在原 R0003 或批准的等价代表集复现；对 patch 做语义、资源、取消和端到端基准 | 未通过前不关闭正式性能问题 |

推荐依赖顺序：

```text
B0 → B1 → B2 → B3 → B3A → B4
          ├──────────→ B5 → B6
          └──────────→ B7 → B8
B5 + B6 + 代表性数据准备 → B9
```

B3A 必须在 neutral continuous validators / `GridGraph` 抽取完成后实施，并在 B4 frontend convergence 前闭合后端契约。B4 可在 B3A 后与 B5 部分并行，但任何 UI production Publish 切换必须等待 B2 authority guard 生效；B3A 的 Constraint Field cells 与连续验证明细须按 B5 artifact 目标设计，禁止新增大对象内联债务。

---

## 15. Acceptance Criteria

### 15.1 产品与工作流

- 主导航精确呈现六步业务工作流，不出现按开发阶段或版本组织的 production 入口。
- Step 3 只有一个 production“发布运行航路”入口，且只接受 Theta* V2 主链候选。
- Step 2 明确分栏显示 Risk Field（Population × Shelter soft cost）与 Planning Constraint Field（feasibility/hard constraint）；二者 schema、指纹、状态和图例不得混用。
- Step 3 对 `ALT-080` 明确显示 `80 m EGM2008 orthometric`，不得显示或解释为 `80 m AGL`；候选卡同时显示 Risk Field 与对应高度层 Constraint Field lineage。
- Step 5 主链为三维覆盖 → 服务能力 → 服务走廊 → 走廊缺口 → 走廊站址规划；Radar 默认是 OPTIONAL production branch，仅在 adopted Required CNS 或 surveillance policy 明确要求时变为 REQUIRED。
- Timeline、Protection、Reliability、Safety、DAA 均在 Advanced，关闭或未配置时不阻塞核心六步。
- production 主界面禁用术语清单自动扫描为 0 命中；高级/审计区不受此限制。

### 15.2 状态与数据语义

- 所有 canonical node 使用统一九态 workflow enum；readiness 只使用三态子集。
- 每个结果都有 `provisional/authoritative` maturity，且与 workflow status 分离；`completed` 不自动授予 Adopt/Publish/Confirm 权限。
- 所有领域评估只使用 `passed/failed/unknown`，并有测试证明不与 workflow `failed/completed` 混用。
- 每个 `ready_with_assumptions` 都能解析到 assumption registry 的 active item。
- 当前请求动作/成熟度的 REQUIRED 缺失必为 blocked；节点契约明确允许时可生成 provisional 结果，但 authority gate 仍 blocked。OPTIONAL/ENHANCEMENT 缺失不被补 0。
- Population 缺失时 route candidate blocked；Shelter coefficient 缺失时可登记 `value=1.0, basis=engineering_baseline` 并得到 `ready_with_assumptions`，且所有 UI/报告均不把它描述为真实遮蔽事实。
- Planning Constraint Field 作为 `environment` derived artifact 存在，不增加 canonical node；其每个 cell 至少为 `pass/blocked/unknown`，测试证明 blocked 不可扩展、unknown 默认不可扩展且不存在 unknown→pass 或缺失高度→0。
- Terrain 公式测试覆盖 `layer_orthometric_m < terrain_orthometric_m + terrain_vertical_clearance_m`；等于 floor 为 pass，低于 floor 为 blocked，缺高度/基准/clearance 为 unknown。
- Building 公式测试覆盖 `building_top_orthometric = resolved_ground_orthometric + building_height_m` 与 `layer_orthometric_m < building_top_orthometric + building_vertical_clearance_m`；等于 floor 为 pass，缺 ground/height、不可接受 `height_status`、无效 geometry 或未解析 CRS 为 unknown。
- `validate_buildings` 及其 evidence adapter 有专项测试证明消费 `height_status`，对 Polygon/MultiPolygon 的所有受影响部件分别做 ground sampling/clearance，并且 horizontal CRS 来自显式可验证配置而非固定 EPSG:32651。
- `_FabdemRasterBase` 的 resolution/accessor 定义唯一，无重复方法遮蔽；回归测试覆盖 projected linear-unit 与 geographic geodesic resolution，degree 不得当 metre。
- 同一 Tower Source Facts 仅存一份，并可追溯派生 `tower_obstacle_profiles` 与 `tower_colocation_candidates`；只有 confirmed tower top 进入 hard 判定，楼面塔/base datum/mounting semantics 不明确时为 unknown。tower 还必须在独立 continuous/native validation 中复核，不能只依赖 search mask。
- `RestrictedArea` / `ProtectedSite` 支持 point/polygon/multipolygon source facts；只有 confirmed protection geometry + confirmed `hard_exclusion` 进入 blocked mask。测试证明 category 不会自动产生 protection radius，无数据产生 `not_evaluated` warning 而不是“无禁飞区”。
- search feasibility mask 与 continuous/native validation 使用独立 evidence evaluation：前者 pass 不可替代后者；任何适用域 failed/unknown 或两层结果不一致均阻断 Operational Adoption。
- fixed-cruise constraint 的验收不要求 terminal protection volume、climb/descent obstacle penetration；它们登记为独立 `Departure/Arrival Procedure Validation` deferred scope，不得以 fixed-layer pass 冒充 terminal pass。
- 无真实 Aircraft Profile 时可显式采用版本化 Generic Engineering Aircraft Profile，并得到 `ready_with_assumptions`。
- 无正式 Requirement source 时可生成 Engineering Required CNS Baseline；显式 Adopt 前保持 provisional，Adopt 后写 canonical `required_cns` 并保持 assumptions；recommendation 不自动写入。
- `knowledge_status=not_declared + planning_mode=factual` 时下游 blocked；切换 `assume_empty_for_planning` 后为 `ready_with_assumptions`，事实状态仍为 unknown，且报告包含固定披露。
- `confirmed_none` 可合法计算事实零既有能力基线；`confirmed_present` 无可解析 facilities/evidence 时仍 blocked。

### 15.3 单一写入 authority

- 对 `operational_routes`、`required_cns`、canonical `coverage`、canonical `facility_plan`、`confirmed_plan` 各有 exactly-one production writer contract test。
- 直接从 router、archive、Advanced 或 Research 服务写上述 key 的测试必须失败。
- V3-D、RoutePlannerV1/V2、LayeredRoutePlannerV1 无法写 `operational_routes`。
- recommendation 不能直接写 `required_cns`；必须通过唯一 command owner 的显式 Adopt。
- closed-loop working copy 不能直接写 `confirmed_plan`。
- provisional candidate/proposal 不能直接写 `operational_routes` 或 `confirmed_plan`。
- `confirmed_plans` 历史允许多个，但每个 `(scenario_id, operational_route_id)` scope 恰有零或一个 active confirmed plan；切换 active 不删除历史。

### 15.4 ProjectState 与 artifact

- 新项目不产生 legacy state；旧项目可打开、可查看旧结果、保存后只新增 canonical state/artifact 引用。
- grid cells、grid attribute cells、risk cells、coverage samples、corridor cells/voxels/evidence、radar detail、experiment traces 均不在 ProjectState 或 workflow snapshot 内联。
- Planning Constraint Field cells、continuous/native interval/pixel/footprint/tower/protection evidence 均进入 artifact/sidecar；ProjectState 与 workflow snapshot 只保存状态、计数、domain 摘要、warnings、fingerprint 和 artifact locator。
- 使用代表性 82 MB corridor 派生结果测试时，workflow snapshot 只随摘要大小增长，不随空间单元数量线性增长。
- artifact sha256 不匹配、sidecar 缺失和 schema 不兼容均 fail explicit，不返回空结果冒充成功。
- Save As/Open/Export 保持 artifact 可达性和校验结果一致。
- active、confirmed、report-referenced artifact 永久保留；其他 stale/unreachable artifact 在 Phase4 不自动删除，GC 仅生成 dry-run 清单。

### 15.5 Heavy Task

- 长计算返回 task ID；客户端断开不被误报为取消。
- Heavy Task 由本机持久 worker process 执行，重启后任务状态可恢复判定；依赖清单中不含 Redis、Celery 或外部消息系统。
- progress 至少包含 stage、完成量/总量（可知时）、消息和 heartbeat。
- cancel 在规定 checkpoint 延迟内生效；取消后 active result 不变，临时 artifact 可回收。
- mutation lock 只覆盖最终 compare-and-publish；运行期间其他只读请求可用。
- revision/fingerprint 冲突时任务不覆盖新状态，结果标记冲突并可审计。

### 15.6 迁移与回归

- `GridGraph` 和 continuous validators 抽取前后，既有 production semantic/golden tests 与输出指纹不变。
- B3A 在 B3 neutral dependency extraction 之后、B4 frontend convergence 之前通过专项 contract tests；不新增顶级 canonical node，不改变六步 workflow、13-node DAG、B1 assumptions/existing CNS、production writer authority 或 P14 性能候选文件。
- archive/research 代码删除前，第 13 节对应 gate 全部有机器可验证证据。
- 旧项目 fixture 覆盖旧 algorithm selection、空 existing facilities、内联大型结果和 legacy key。
- Phase4 全周期旧项目 open/read/export 保持可用；archive 功能从 production UI 立即移除，物理删除仅在 B7/B8 对应 delete gate 通过后发生。
- Phase4 各批次不包含四个 P14 PERFORMANCE PATCH CANDIDATE 文件的无关修改。
- 正式性能问题只有在原 R0003 或批准的等价代表集完成端到端复现和对照后才能标记 resolved。

---

## 16. 已裁决事项（Phase4-B0.1）

本章固化五项已关闭裁决；它们现在是实现约束。

| # | 已裁决规则 | 实现约束 |
|---|---|---|
| 1 | Artifact retention | active、confirmed、report-referenced 永久保留；其他 stale/unreachable artifact 在 Phase4 不自动删除，只提供 dry-run GC |
| 2 | Heavy Task deployment | 使用本机持久 worker process 和本机持久任务队列；不引入 Redis、Celery 或外部消息系统 |
| 3 | Compatibility window | Phase4 全周期保持旧项目读取；archive 功能立即退出 production UI；物理删除仅在 B7/B8 delete gate 通过后 |
| 4 | Active confirmed plan cardinality | 历史 confirmed plan 可多个；每个 scenario / operational route scope 只允许一个 active confirmed plan |
| 5 | Radar applicability | 默认 OPTIONAL production branch；仅当 adopted Required CNS 或 surveillance policy 明确要求 Radar 时成为 REQUIRED |

B0.1 内部契约已闭合。

---

## 17. 决策摘要

本文确立五项核心变化：

1. 从多版本并列入口收敛为唯一六步 production 工作流和唯一 Theta* V2 航路发布链。
2. 正式 CNS 链收敛到三维覆盖、服务能力、走廊、缺口、走廊站址、评审报告；其他能力分流到 Advanced/Archive。
3. 建立统一 workflow status、独立 output maturity 与 assessment 三态、数据依赖四级和最小 assumption registry，使不完整数据可形成 provisional 结果而不能旁路发布。
4. 将 ProjectState 收敛为轻量权威状态，把包括约 82 MB corridor 空间单元/evidence 在内的大型派生结果强制外置。
5. 以唯一 production writer、本机持久且可取消的 Heavy Task 和“读旧写新”的批次迁移替代旁路写入、同步长锁和整体重写。
