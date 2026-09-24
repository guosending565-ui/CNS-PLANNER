# Phase4-B2A Production Write Authority 只读审计

| 项 | 值 |
|---|---|
| 审计批次 | Phase4-B2A（B2 前置只读审计） |
| 审计基线 | Git `HEAD 7a0118e4d7181c9e794633d6b3e7f8bbba340fb6` |
| 权威目标态 | `PHASE4_TARGET_ARCHITECTURE.md`（第 3、4、8、10、12、15 节）、`PHASE4_FIXED_LAYER_CONSTRAINT_AUDIT.md` |
| 现状参考 | `PHASE4_ARCHITECTURE_AUDIT.md`（Phase4-A，仅作交叉佐证，不覆盖目标态裁决） |
| 审计范围 | 6 类 result：`operational_routes`、`required_cns`、canonical `coverage`、canonical `facility_plan`、`confirmed_plan`、`radar_surveillance_layout`；外加 router 直写面与 invalidation 耦合 |
| 只读声明 | 未修改任何 Python/JS/测试/文档；未执行 commit/push/reset/stash/clean；未触碰 P14 PERFORMANCE PATCH CANDIDATE 四文件（`cns_planner/algorithms/corridor/v1.py`、`cns_planner/algorithms/coverage/geometric_3d.py`、`cns_planner/algorithms/service_capability/v1.py`、`tests/test_cns_corridor.py`），也未触碰 `_dsh_prof/`、`docs/舟山起降场核对与80m障碍检查报告.md`、`qgis_polygon_inventory.json`、`tools/check_*.py` 等舟山数据/障碍检查工作线临时文件 |
| 非目标 | 不开始 B3/B3A；不迁移；不删除旧算法；不重做 frontend；本报告只建立事实图与 B2B 最小计划 |

## 审计口径（先声明，避免计数歧义）

本报告对"writer"使用三个互斥层级，**只有第 1 层计入 CURRENT_WRITER_COUNT**：

| 层级 | 定义 | 是否计入 writer 数 |
|---|---|---|
| **L1 CONTENT_WRITER** | 写入/替换/删除该 result 的**结果内容**（几何、方案、需求、布局、确认记录） | 是 |
| **L2 STATUS_WRITER** | 只改 `status` / `stale_reason` / `result_statuses` / `current_applicability`，不改内容 | 否（单独列"stale 写路径数"） |
| **L3 SEED_OR_NORMALIZER** | `blank_project` 初始值、`normalize_project` additive backfill、旧项目迁移 stale 标记 | 否（单独列"兼容写路径数"） |
| **L4 TEST_FIXTURE** | 仅出现在 `tests/`、benchmark、`_dsh_prof/` 副本中的写点 | 否（明确不属于运行期 writer） |

计数同时给出两个视角：

- **服务级计数**：同一 service 的多个方法算 1 个 writer（用于对照 `TARGET_WRITER_COUNT=1`）。
- **语句级计数**：每个可执行赋值语句算 1（用于定位删除/加 guard 的精确位置）。

`TARGET_WRITER_COUNT = 1` 指服务级。

---

## 1 Executive Summary

### 1.1 一句话结论

六类结果中，**只有 `radar_surveillance_layout` 已经事实上达成"唯一 production writer"**；`facility_plan`（以 `cns_corridor_site_plan` 为承载体）与 `confirmed_plan`（`confirmed_cns_plan`）的**内容 writer 也已经是 1 个**，但两者分别存在 legacy 并行 writer 与旁路 applicability writer；真正严重超编的是 `operational_routes`（3 个 writer、9 个内容写点）、`required_cns`（2 个 writer）与 canonical `coverage_3d`（3 个 writer）。

### 1.2 逐类计数总览

| # | Result | canonical state key | 当前服务级 writer 数 | 当前语句级内容写点 | Stale 写路径 | 兼容/迁移写路径 | 目标唯一 owner | 达标? |
|---|---|---|---|---|---|---|---|---|
| 1 | `operational_routes` | `operational_routes` | **3** | **9** | 3 | 1（seed） | `LayeredOperationalAdoptionService` | ❌ 严重超编 |
| 2 | `required_cns` | `required_cns` | **2** | **2** | 0 | 2（normalize + seed） | `RequiredCNSCommandService`（迁移期 `CNSInputService`） | ❌ 超编 1 |
| 3 | canonical `coverage` | `coverage_3d`（canonical）/ `coverage`（legacy 2D） | **3**（canonical）+ 1（legacy 2D） | **3** + 1 | 1 | 1（seed） | `Spatial3DService` + `GeometricCoverage3D` | ❌ 超编 2 |
| 4 | canonical `facility_plan` | `cns_corridor_site_plan` | **1** | **2**（同 service） | 4 | 2（normalizer/迁移） | `CorridorSitePlanningService` | ✅ 内容 writer 已达标；legacy `cns_site_plan` 仍活跃 |
| 5 | `confirmed_plan` | `confirmed_cns_plan` | **1** | **2**（同 service） | 2 | 3（response copy + seed） | `PlanReviewService` | ⚠️ 内容 writer 达标；`ClosedLoopService` 旁路改 applicability；scope 语义未实现 |
| 6 | `radar_surveillance_layout` | `radar_surveillance_layout` | **1** | **1** | 1 | 1（normalizer） | `RadarSurveillanceLayoutService` | ✅ 唯一 writer |

### 1.3 最危险的结构性事实

1. **默认 production 路径并不走目标 owner。** `default_algorithm_selection()` 把 `route_planner` 默认为 `RoutePlannerV1`（`algorithms/registry.py:142`）、`site_planner` 默认为 `ReuseFirstSitePlannerV1`（`:165`）、`coverage_planner` 默认为 `CoveragePlannerV1`（`:152`）。因此 UI 主入口 `/api/workflow/operational`、`/api/cns-site-plan`、`/api/workflow/coverage` 写的是**旧算法结果**，`LayeredOperationalAdoptionService` / `CorridorSitePlanningService` / `Spatial3DService` 虽然存在且已接线，却不是默认写入者。
2. **"目标态声明"与"运行期声明"不一致。** 多个服务在文档字符串里声明 `never writes operational_routes`（`layered_route_planner_service.py:11`、`route_planner_v3_service.py:6`、`route_experiment_service.py:6`…），但 `route_service.py:243` 与 `v3_operational_adoption_service.py:768` 实际写入。声明是注释，不是 guard。
3. **invalidation 存在两套并行体系。** 声明式 `DEPENDENTS`（`services/invalidation.py:8-52`）与命令式手工 stale（`application/invalidation_service.py` 641 行 + 各 service 内部 `_post_apply_statuses` / `_propagate_publish` / `_apply_post_commit_statuses`）语义不一致：**同一个业务语义"运行航路变化"在 `RouteService` 路径下会 stale `required_cns_recommendation`，在 `LayeredOperationalAdoptionService` 路径下不会**。`DEPENDENTS` 中没有 `operational_route_published` 键，只有同名命令式方法。
4. **没有覆盖六类的 production write authority contract test suite。** 全仓库 `tests/` 对 `authority` 的 18 处命中全部属于 B1 的 assumption `authority_effect` 或 CRS authority 字符串。唯一的 writer 边界测试是 Radar 的 `test_radar_layout_never_writes_cns_or_coverage_results`（`tests/test_radar_surveillance_layout.py:2433-2453`），它只锁定"radar 不写 coverage / corridor / facilities"，**没有**任何测试断言 `operational_routes` / `required_cns` / `coverage_3d` / `confirmed_cns_plan` 的 exactly-one writer，也没有 router/archive 写入拦截测试。
5. **Radar 是唯一已达标的类别**，且它同时具备两层现成范本：`FORBIDDEN_WRITE_KEYS`（`radar_surveillance_layout_service.py:70-76`）这一 allowlist 模式，以及 `test_radar_layout_never_writes_cns_or_coverage_results` 这一边界测试写法——可直接抽为全局 guard + contract test 的实现模板。
6. **`facility_plan` 与 `confirmed_plan` 的"1"是脆弱的。** 两者的内容 writer 确实只有 1 个，但 `plan_review_service.py:206` 与 `closed_loop_service.py:89-90` 的 `state.clear(); state.update(working)` 使它们**顺带成为 `coverage_3d` / `cns_corridor_*` 的内容 writer**；authority 泄漏不是通过"多写自己的 key"，而是通过"整表替换别人的 key"。

### 1.4 最危险的 5 个旁路写入（按风险排序）

| 排名 | 旁路写入 | 位置 | 为什么最危险 |
|---|---|---|---|
| 1 | `RouteService.generate_operational` 直接把 planner 结果写成 `operational_routes` | `application/route_service.py:243`，经 `POST /api/workflow/operational`（`router.py:478`） | **默认 production 路径**（默认 `route_planner` = `RoutePlannerV1`，`registry.py:142`），且是主界面 Step 3 入口。绕过全部 authority gate：无 validation、无 RouteRiskProfile、无 expected fingerprint、无显式 Adopt |
| 2 | `V3OperationalAdoptionService._publish_all` / `_revoke_in_working_copy` 写 `operational_routes` | `application/v3_operational_adoption_service.py:768`、`:913`，经 `POST /api/route-planner-v3-operational-adoptions/apply`（`router.py:344`，UI `step03_routes.js:1537`） | 目标架构**点名禁止**（`PHASE4_TARGET_ARCHITECTURE.md:113, 276`）；虽有自己的 publish gate，但 provenance 来自 research/archive 的 V3 链，且它的 `_propagate_publish`（`:844`）自建一套 invalidation，进一步分叉 |
| 3 | `RequirementRecommendationService.adopt` 直接写 canonical `required_cns` | `application/requirement_recommendation_service.py:99`，经 `POST /api/cns-required-recommendation/adopt`（`router.py:282`，UI `step04_operation.js:280`） | recommendation 服务同时扮演 evaluator 与 writer，正是目标明令禁止的形态（`:653, 877`）；且它不更新/清除 `required_cns_adoption` 的 history 语义，会让 `plan_review_service.confirm` 的 `requirement_basis`（`:139-142`）引用到可能失真的采纳记录 |
| 4 | `ClosedLoopService.apply` 以工作副本重算结果覆盖 canonical `coverage_3d` / `cns_service_capability` / `service_timeline` / `cns_gap_analysis_v2`，并改 `confirmed_cns_plan.current_applicability` | `application/closed_loop_service.py:80-90`、`:376-380`，经 `POST /api/cns-closed-loop/apply`（`router.py:301`，UI `step05_cns.js:463`） | 目标规定 ClosedLoopService 只能是 working-copy / proposal（`:281, 662, 878`），但它实际**提交 canonical 上游**且**直接改 confirmed plan**；同时它完全绕过 `CorridorSitePlanningService`，使 P12 与 canonical facility_plan（P16）无调用关系 |
| 5 | `PlanReviewService.apply` 用 `state.clear(); state.update(working)` 整表替换，把重算出的上游写入 canonical | `application/plan_review_service.py:174/183/196/206`，经 `POST /api/cns-plan-review/apply`（`router.py:311`，UI `step06_review.js:468`） | PlanReviewService 是 confirmed_plan 的**合法 owner**，但 Apply 顺带成了 `coverage_3d` / `cns_service_capability` / `service_timeline` / `cns_gap_analysis_v2` / `cns_corridor_assessment` / `cns_corridor_gap_assessment` 的 writer。"合法 owner 的越权"比"非法 owner 的写入"更难发现，因为它藏在合法命令内部 |

（第 6 位，供参考：`SitePlanningService.evaluate` 写 `cns_site_plan`（`application/site_planning_service.py:50`）—— 虽只写 legacy key 且报告链已不消费，但它仍在 production UI 暴露（`step05_cns.js:461`），是 legacy 站址链继续"看起来可用"的原因。）

---

## 2 operational_routes writer map

### 2.1 事实

- canonical key：`operational_routes`（`project_state.py:255` 初始 `[]`）。
- 目标唯一 owner：`LayeredOperationalAdoptionService`（`PHASE4_TARGET_ARCHITECTURE.md:652`），未来可显名 `OperationalRoutePublishService`。
- 目标禁止写入者：`RouteService`/`RoutePlannerV1`、`RiskAwareRoutePlannerV2`、`LayeredRoutePlannerV1`、V3-D、实验服务。

### 2.2 Writer 表（L1 内容 writer）

| # | result | state key | writer/service | API | write method | current purpose | production allowed? | target action | compatibility requirement |
|---|---|---|---|---|---|---|---|---|---|
| 1 | operational_routes | `operational_routes` | `RouteService.generate_operational`<br>`application/route_service.py:243` | `POST /api/workflow/operational`（`router.py:478`） | `state["operational_routes"] = results`（整集合替换） | 用当前 `route_planner`（默认 `route_planner_v1`，可切 `risk_aware_route_planner_v2`）对全部 `scenario_routes` 规划正式航路 | **否** | 移除 canonical 写权；旧项目读取保留；如需保留计算，写入独立 legacy namespace（B2B-1） | 必须保留旧项目 `operational_routes` 可读；`/api/export/routes` 与 report 消费不变；`test_v1_algorithm_characterization.py` 金样板不得回归 |
| 2 | operational_routes | `operational_routes` | `RouteService.generate_scenario`<br>`application/route_service.py:122` | `POST /api/workflow/scenario`（`router.py:472`） | `state["scenario_routes"], state["operational_routes"] = created, []` | 重建场景航路时清空已发布运行航路 | 条件允许（清空=删除派生，不是产生权威结果） | 保留，但需走 owner 提供的显式 revoke/clear 语义或登记为 L2 | 旧项目语义必须保持（清空是既有契约） |
| 3 | operational_routes | `operational_routes` | `RouteService.generate_scenario_od`<br>`application/route_service.py:178` | `POST /api/workflow/scenario-od`（`router.py:473`） | 同上（整集合清空） | 同上（显式 OD 版） | 条件允许（同上） | 同上 | 同上 |
| 4 | operational_routes | `operational_routes` | `RouteService.delete_route`<br>`application/route_service.py:188` | `POST /api/workflow/route-delete`（`router.py:477`） | 列表推导过滤单条 | 删除场景航路时同步删除对应运行航路 | 条件允许（删除=回收，不是产生权威结果） | 归为 owner 的 revoke 语义或显式白名单 | 旧项目 ID 回收语义不变 |
| 5 | operational_routes | `operational_routes` | `RouteService.delete_node`<br>`application/route_service.py:82` | `POST /api/workflow/node-delete`（`router.py:471`） | 列表推导过滤被 retired 的 route | 删除起降点时同步删除相关运行航路 | 条件允许（同上） | 同上 | 同上 |
| 6 | operational_routes | `operational_routes` | `LayeredOperationalAdoptionService._apply_working`<br>`application/layered_operational_adoption_service.py:247-250` | `POST /api/layered-operational-adoptions/apply`（`router.py:389`） | 先过滤同 `route_id` 再 append `deepcopy(projection["route"])`，然后按 `route_id` 排序 | **目标态**：消费 Theta* V2 candidate + RouteRiskProfile + 独立验证 + 显式 `confirmed=true`，发布运行航路 | **是（唯一允许者）** | 保持；B2B-1 以此 service 为 allowlist 基准 | 必须保持 `confirmed=true`、`expected_validation_fingerprint`、`replace_existing` 门禁与事务语义（工作副本 + `_install_in_place`）不变 |
| 7 | operational_routes | `operational_routes` | `LayeredOperationalAdoptionService._revoke_working`<br>`application/layered_operational_adoption_service.py:341` | `POST /api/layered-operational-adoptions/revoke`（`router.py:390`） | 移除同 `route_id` 并恢复 `before.route` | 撤销发布、按 ownership 回滚 | **是** | 保持 | `ownership.route_owned` 语义与 `preserved_foreign_modifications` 必须保留 |
| 8 | operational_routes | `operational_routes` | `V3OperationalAdoptionService._publish_all`<br>`application/v3_operational_adoption_service.py:768` | `POST /api/route-planner-v3-operational-adoptions/apply`（`router.py:344`） | upsert `deepcopy(projection["route"])` 后排序 | V3-C 验证航路发布进 `operational_routes` | **否（目标明令禁止）** | **B2B-1 移除 production publish 写权**；Preview 保留为 research/diagnostic | V3 历史 adoption 记录可读；`test_route_planner_v3_operational_adoption.py` 需改为断言"不可写 canonical"；旧项目已发布的 route 必须仍可读可导出 |
| 9 | operational_routes | `operational_routes` | `V3OperationalAdoptionService._revoke_in_working_copy`<br>`application/v3_operational_adoption_service.py:913` | `POST /api/route-planner-v3-operational-adoptions/revoke`（`router.py:345`） | 移除/恢复 route | V3 adoption 撤销 | **否** | 与 #8 同批处理 | 同上 |

### 2.3 L2 / L3 / L4 写点（不计入 writer 数）

| 层级 | 位置 | 说明 |
|---|---|---|
| L2 | `application/invalidation_service.py:80-86` | `workflow()` 内按 `_should_stale_operational_route` 把 route `status` 置 `stale` |
| L2 | `application/invalidation_service.py:462-466` | `operational_route_published(..., preserve_published_routes=True)` 把刚发布的 route 恢复 `passed` |
| L2 | `application/v3_operational_adoption_service.py:860-863` | V3-D `_propagate_publish` 恢复已发布 route `status` |
| L3 | `application/project_state.py:255` | `blank_project` 初始 `"operational_routes": []` |
| L4 | `tests/test_airspace_decoupling_v1.py:365`、`test_bug_persist_reuse_tier_001.py:133`、`test_building_environment_clearance.py:282`、`test_bug_workspace_clear_layered_request.py:32`、`test_closed_loop.py:113`、`test_cns_gap_analysis.py:106`、`test_cns_reliability_service_state.py:189`、`test_cns_site_planner.py:104`、`test_corridor_site_planner_v2.py:112`、`test_operational_timing.py:300`、`test_layered_route_validation_adoption.py:216/312/337/356`、`test_layered_route_planner.py:840/1014`、`test_radar_surveillance_layout.py:1640`、`test_route_3d_profile.py:52`、`test_route_operating_layer.py:38`、`test_risk_framework_v2.py:586/624`、`test_risk_aware_route_planner_v2.py:329`、`test_route_planner_v3_operational_adoption.py:621/732/774/819/920`、`test_route_vertical_profile.py:123/205/218/231`、`test_spatial_3d.py:125/159/170/195`、`test_route_risk_profile.py:770/789/837`、`test_route_safety_evidence_v2.py:174`、`test_vertical_transition_validation.py:92` 等 | fixture 预置，不属运行期 writer |
| L4 | `_dsh_prof/ref/cns_planner/...` 下同名副本 | P14 探查副本（未跟踪、未被 production import），**不得作为 writer 统计**，也不得触碰 |

### 2.4 调用链

```text
[目标态 owner — 允许]
POST /api/layered-operational-adoptions/project   router.py:387
  → WorkflowService.project_layered_operational_adoption        workflow_service.py:1318
  → LayeredOperationalAdoptionService.projection                (只读投影)
POST /api/layered-operational-adoptions/apply     router.py:389
  → WorkflowService.apply_layered_operational_adoption          workflow_service.py:1324
  → LayeredOperationalAdoptionService.apply                     :182
      → 校验 confirmed=true / projection.status=ready / conflict / expected_validation_fingerprint
      → deepcopy 工作副本 → _apply_working                       :218
          → state["operational_routes"] upsert                   :247-250
          → state["spatial_3d"]["route_operating_layers"] upsert
          → state["layered_operational_adoptions"] 追加 + 旧记录 superseded
          → invalidation.operational_route_published(...)         :299
      → _normalize(working) → _install_in_place(original, ...) → session.save()
POST /api/layered-operational-adoptions/revoke    router.py:390
  → LayeredOperationalAdoptionService.revoke                      :306 → _revoke_working :327
      → state["operational_routes"] 移除/恢复                     :341
      → invalidation.operational_route_published(...)             :364

[旧 V1/V2 — 目标禁止，当前默认生效]
POST /api/workflow/operational                    router.py:478（★ router 直接把 data.hard_constraints 传入）
  → WorkflowService.generate_operational                        workflow_service.py:1166
  → RouteService.generate_operational                           route_service.py:234
      → validate_hard_constraints + planner_context（只读输入视图）
      → RouteService.plan_routes(self.planner, ...)             :215
          planner = WorkflowService.route_planner = _selected_algorithm("route_planner")
          默认 Registry: RoutePlannerV1（registry.py:142）；可切换为 RiskAwareRoutePlannerV2
      → state["operational_routes"] = results                   :243   ★ canonical 写入
      → state["result_statuses"]["routes"] = passed/failed/...  :245
      → invalidation.workflow("route")                          :255
      → session.save()                                          :258

[V3-D — 目标禁止]
POST /api/route-planner-v3-operational-adoptions/apply  router.py:344
  → WorkflowService.apply_v3_operational_adoption               workflow_service.py:912
  → V3OperationalAdoptionService.apply                          :649
      → publish_gate(entry, require_production=True) / project / batch atomic
      → state["operational_routes"] = routes（upsert+sort）     :768   ★ canonical 写入
      → state["spatial_3d"]["route_altitude_profiles"] upsert   :769
      → state["v3_operational_adoptions"] upsert                :773
      → self._propagate_publish(published_ids)                  :775 → :844（手工 stale 12 个 key，不经过 InvalidationService）
      → session.save()                                          :718
```

### 2.5 计数

| 指标 | 值 |
|---|---|
| `CURRENT_WRITER_COUNT`（服务级） | **3** — `LayeredOperationalAdoptionService`（允许）、`RouteService`（禁止）、`V3OperationalAdoptionService`（禁止） |
| 语句级内容写点 | **9**（见 2.2） |
| L2 stale/status 写路径 | 3 |
| L3/L4 | seed 1 + fixture 30+ |
| `TARGET_WRITER_COUNT` | **1**（`LayeredOperationalAdoptionService`） |
| 需移除的 canonical 写权 | `RouteService.generate_operational:243`、`V3OperationalAdoptionService._publish_all:768`、`V3OperationalAdoptionService._revoke_in_working_copy:913`（共 3 个语句；服务级 2 个） |

---

## 3 required_cns writer map

### 3.1 事实

- canonical key：`required_cns`（`project_state.py:288` 初始 `pending_required_cns()`）。
- 目标唯一 writer：`RequiredCNSCommandService`；迁移期由 `CNSInputService` 作为**唯一 facade**，统一 `direct confirm` 与 `recommendation adopt` 两种命令（`PHASE4_TARGET_ARCHITECTURE.md:653`）。
- 明确禁止：recommendation evaluator、router 内联逻辑、importer 直接写 state。

### 3.2 Writer 表（L1 内容 writer）

| # | result | state key | writer/service | API | write method | current purpose | production allowed? | target action | compatibility requirement |
|---|---|---|---|---|---|---|---|---|---|
| 1 | required_cns | `required_cns` | `CNSInputService.set_required_cns`<br>`application/cns_input_service.py:106` | `POST /api/required-cns`（`router.py:278`）与 `POST /api/workflow/required-cns`（`router.py:481`） | 构造 `current` → `state["required_cns"] = normalize_required_cns(current)` | 用户在 `project` / `route` scope 显式手工确认 CNS 需求 | **是**（目标 facade 的 direct-confirm 命令） | 保留并升级为唯一命令 owner；补 `source=user_configuration`/确认指纹与 `revision` 校验 | 两个 API 别名都必须继续可用（前端 `step04_operation.js:295` 走 `mutate('required-cns')`）；`scope=route` 必须继续校验 `scenario_routes` 存在 |
| 2 | required_cns | `required_cns` | `RequirementRecommendationService.adopt`<br>`application/requirement_recommendation_service.py:99` | `POST /api/cns-required-recommendation/adopt`（`router.py:282`） | `state["required_cns"] = adopted`（另写 `required_cns_adoption` 记录） | 用户显式 Adopt Engineering Baseline / recommendation，成为 canonical 需求 | **否**（recommendation 服务不得直接写 canonical） | B2B-1：`adopt` 改为**委托** `CNSInputService` 的 adopt 命令（保留指纹校验与 `required_cns_adoption` 记录），自身不再直接赋值 | Adopt 行为、`required_cns_adoption` 记录结构、`source=explicit_adoption_from_required_cns_recommendation` 与冲突拒止（`conflict` → raise）必须保持；`test_required_cns_recommendation.py` 必须继续通过 |

### 3.3 L2 / L3 / L4

| 层级 | 位置 | 说明 |
|---|---|---|
| L3 | `application/cns_input_service.py:49`（`ensure_catalogs`） | 启动/恢复时对已有值做 `normalize_required_cns`（backfill，不产生新事实） |
| L3 | `application/project_state.py:585` | `normalize_project` setdefault `pending_required_cns()` |
| L3 | `application/project_state.py:291-292` | `required_cns_recommendation` / `required_cns_adoption` 默认容器 |
| L4 | `tests/test_cns_site_planner.py:117`、`test_cns_gap_analysis.py:107`、`test_cns_service_capability.py:207`、`test_corridor_site_planner_v2.py:131`、`test_cns_reliability_service_state.py:192`、`test_closed_loop.py:131`、`test_operational_timing.py:252`、`test_cns_gap_analysis_v2.py:194`、`test_route_planner_v3_operational_adoption.py:233` | fixture |

**否证与补充事实（本轮新增核实）**

| 事实 | 证据 | 结论 |
|---|---|---|
| `algorithms/requirements/*` **不持有 state/session** | `manual_v1.py:15-35`、`operational_context_v2.py:20-202` 只有 `__init__` / `evaluate(...)` 与私有纯函数，无 `state[`、无 `session` | required_cns 的 authority 问题**完全在 Application 层**，与算法/registry 无关；收敛不必触碰任何 requirement 算法 |
| **不存在 legacy 别名 key**（如 `cns_requirements`） | 全仓 grep `cns_requirements` / `cns_requirement\b` 零命中；仅 `required_cns`、`required_cns_recommendation`、`required_cns_adoption` | 与 coverage 不同，required_cns 没有"新旧双 key"问题，迁移面更小 |
| `required_cns_adoption` **只有一个写点、且无任何失效路径** | 写：`requirement_recommendation_service.py:100`（仅 adopt）；seed/backfill：`project_state.py:292, 589`。全仓**无**对该 key 的 stale 标记代码 | 印证 9.3 缺口 ④，且后果被放大（见下行） |
| `required_cns_adoption` 被权威产物消费 | `plan_review_service.py:141`（confirm 写入 `requirement_basis.adoption`）、`reporting/builder.py:45`、`domain/reporting.py:49` | 用 `set_required_cns` 手工覆盖需求后，旧 adoption 记录仍为 `adopted` 并继续进入 confirmed plan 与报告 ⇒ 权威记录可能失真，这是 #3 旁路（`requirement_recommendation_service.py:99`）之外的**独立失配** |

### 3.4 调用链

```text
[允许 — direct confirm]
POST /api/required-cns | /api/workflow/required-cns   router.py:278 / 481
  → WorkflowService.set_required_cns                            workflow_service.py:1170
  → CNSInputService.set_required_cns                            cns_input_service.py:88
      → 校验 payload 对象 / requirements 对象 / scope ∈ {project, route}
      → scope=route 时校验 route_id ∈ state["scenario_routes"]
      → state["required_cns"] = normalize_required_cns(current) :106   ★ canonical 写入
      → invalidation.workflow("required_cns")                   :107
      → session.save()

[目标禁止 — recommendation 直接写 canonical]
POST /api/cns-required-recommendation/evaluate   router.py:281
  → RequirementRecommendationService.evaluate                   :75
      → 只写 state["required_cns_recommendation"]               :82（合规：非 canonical）
POST /api/cns-required-recommendation/adopt      router.py:282
  → RequirementRecommendationService.adopt                      :87
      → 校验 conflict / status=recommendation_ready / input_fingerprints 未过期
      → state["required_cns"] = adopted                         :99    ★ canonical 写入（旁路）
      → state["required_cns_adoption"] = {...}                  :100
      → invalidation.workflow("required_cns")                   :109
      → session.save()
```

### 3.5 计数

| 指标 | 值 |
|---|---|
| `CURRENT_WRITER_COUNT`（服务级） | **2** — `CNSInputService`（允许）、`RequirementRecommendationService`（禁止） |
| 语句级内容写点 | **2** |
| `TARGET_WRITER_COUNT` | **1**（`RequiredCNSCommandService`；迁移期 `CNSInputService`） |
| 需移除的 canonical 写权 | `requirement_recommendation_service.py:99`（1 个语句；改为委托即可，无需新模块） |

### 3.6 关键回答（对应任务问题清单）

1. **谁能直接写 canonical `required_cns`？** 仅两个位置：`cns_input_service.py:106`（手工确认）与 `requirement_recommendation_service.py:99`（recommendation adopt）。
2. **谁只是 recommendation？** `RequirementRecommendationService.evaluate`（`requirement_recommendation_service.py:75-85`）只写 `required_cns_recommendation`，不碰 canonical —— 合规。
3. **谁只是 compatibility importer / normalizer？** `CNSInputService.ensure_catalogs:49`（normalize backfill）与 `project_state.py:585`（setdefault）—— 不产生权威事实。
4. **recommendation evaluator 是否已直接写 canonical？** evaluator 本体**没有**；但同一 service 的 `adopt` 命令**直接写了** canonical（`:99`），构成 writer 旁路。
5. **router 是否内联写？** 否。router 只做分发（`router.py:278/281/282/481`），无内联 state 赋值。
6. **写入后触发哪些 invalidation？** 两条路径都调用 `invalidation.workflow("required_cns")`，展开为 `DEPENDENTS["required_cns"]` = `coverage, cns_gap, cns_service_capability, service_timeline, cns_gap_v2, cns_site_plan, cns_corridor_assessment, cns_corridor_gap_assessment, cns_corridor_site_plan, report`。**注意缺 `coverage_3d`**，且 **`required_cns_adoption` 记录不会被清除**（见 9.3）。

---

## 4 coverage writer map

### 4.1 事实：两个 key，两个时代

| 视角 | state key | 生产者 | 现状定位 |
|---|---|---|---|
| 旧 2D coverage | **`coverage`** | `CoveragePlannerV1`（经 `CNSPlanningService.plan_coverage`） | legacy；仍由默认 `coverage_planner` 选择写入，仍是 `result_statuses["coverage"]` 与 `workflow_service._steps()` 第 5/6 步门禁的判据（`workflow_service.py:1047-1048`） |
| canonical coverage | **`coverage_3d`** | `GeometricCoverage3DV1`（经 `Spatial3DService.evaluate`） | 目标 canonical；已接 Step 5 主链（corridor / capability / gap 全链消费） |

目标态（`PHASE4_TARGET_ARCHITECTURE.md:654`）：`Spatial3DService` + `GeometricCoverage3D` 作为一个 Application owner；`CoveragePlannerV1` 只能 compatibility/archive，**不得写 canonical coverage**。

### 4.2 Writer 表（L1 内容 writer）

| # | result | state key | writer/service | API | write method | current purpose | production allowed? | target action | compatibility requirement |
|---|---|---|---|---|---|---|---|---|---|
| 1 | coverage_3d（canonical） | `coverage_3d` | `Spatial3DService.evaluate`<br>`application/spatial_3d_service.py:96` | `POST /api/coverage-3d/evaluate`（`router.py:416`） | `state["coverage_3d"] = result` | 目标态：三维几何覆盖正式计算 | **是（唯一允许者）** | 保持；B2B-2 以其为 allowlist 基准 | `GET /api/coverage-3d` 快照结构、`parameters` 回填、`Spatial3DService.set_route_profile` 的 `invalidation.coverage_3d()` 语义不变；P14 保护文件 `algorithms/coverage/geometric_3d.py` 只读复用 |
| 2 | coverage_3d（canonical） | `coverage_3d` | `ClosedLoopService` → `rerun_p7_p10_chain`<br>`application/closed_loop_service.py:190`（经 `apply` 的 `:80-90` 提交） | `POST /api/cns-closed-loop/apply`（`router.py:301`） | `state["coverage_3d"] = coverage_model.evaluate(...)` 于 workcopy，再由 `current.clear(); current.update(committed)` 提交 | P12 闭环试算把"应用站址后的 P7-P10 重算结果"直接写回 canonical 上游容器 | **否**（目标：ClosedLoopService 只能 working-copy / proposal） | B2B-2：结果写独立 `closed_loop_working_copy` / proposal namespace，不覆盖 canonical `coverage_3d`；需要 canonical 更新时委托 `Spatial3DService` | `test_closed_loop.py` 的 preview/apply 语义、`commit_status`、`existing_cns_facilities` 应用结果必须保持；报告链不得读不到 |
| 3 | coverage_3d（canonical） | `coverage_3d` | `PlanReviewService.apply`<br>`application/plan_review_service.py:174` + `:183` + `:206`（`rerun_p7_p10_chain` → `state.clear(); state.update(working)`） | `POST /api/cns-plan-review/apply`（`router.py:311`） | 重算 P7-P10 / P14-P15 后整体替换 canonical state | P18 Apply 需要"应用确认方案后的权威上游" | **否**（超出 PlanReview 的 confirmed-plan authority） | B2B-2：Apply 必须通过各 owner 的显式 command 提交，而不是整表替换 | `test_plan_review.py` 的 P10 gate、fingerprint 校验、`_post_apply_statuses` 映射必须保持 |
| 4 | coverage（旧 2D） | `coverage` | `CNSPlanningService.plan_coverage`<br>`application/cns_planning_service.py:44` | `POST /api/workflow/coverage`（`router.py:485`） | `state["coverage"] = self.planner.plan(state["operational_routes"], devices)` | 旧 2D 布站规划 | 仅 legacy namespace（当前写的是 legacy key，未污染 `coverage_3d`，但仍是 production UI 入口） | B2B-2：保留兼容读取；写权保留在 legacy namespace 语义下（本 key 即 legacy），并解除 `_steps()` 门禁与主界面依赖 | `test_v1_algorithm_characterization.py` 金样板、`test_algorithm_registry.py`、`test_cns_inputs.py`、`test_closed_loop.py`、`test_risk_aware_route_planner_v2.py` 中对 `result_statuses["coverage"]` 的既有断言必须继续通过 |

### 4.3 仍读取二维 legacy coverage 的消费者

| 消费者 | 位置 | 读/写 | 说明 |
|---|---|---|---|
| `WorkflowService._steps` | `application/workflow_service.py:1047-1048` | 读 `result_statuses["coverage"]` + `state["coverage"]["status"]` | **Step 5/6 门禁仍由 legacy 2D coverage 决定** —— 唯一真实生产读点，目标态必须改为 canonical `coverage_3d` |
| `ExportService.sites` | `application/export_service.py:27`（API `GET /api/export/sites`，`router.py:164`） | 读 `state["coverage"]["layers"]` | 兼容导出；主界面"导出站址"仍依赖 legacy 布站结果 |
| `ClosedLoopService._apply_post_commit_statuses` | `application/closed_loop_service.py:350-352` | **写** `state["coverage"]["status"] = "stale"` + `result_statuses` | 命令式 stale 旧 2D |
| `PlanReviewService._post_apply_statuses` | `application/plan_review_service.py:349-353` | 读/写（key 列表含 `("coverage","coverage")`） | 兼容状态映射 + stale |
| `InvalidationService.workflow` | `application/invalidation_service.py:90-91` | 写 `state["coverage"]["status"] = "stale"` | 声明式 `DEPENDENTS` 命中；驱动键含 `data/workspace/route/rules/aircraft_profile/required_cns/devices/existing_cns/candidate_sites/sites/route_algorithm/coverage_algorithm` |
| `InvalidationService.workflow`（算法选择） | `services/invalidation.py:24` | 声明 `coverage_algorithm → ("coverage","report")` | 选另一个 `coverage_planner` 只 stale legacy 2D（canonical 由 `coverage_model` 键管，`:26`） |
| `WorkspaceService.clear_workspace` | `application/workspace_service.py:151` | **写** `state["coverage"] = None` | 清空工作区时重置 legacy 结果（L3 重置写点） |
| 前端 Step 5 / Step 6 / 地图 | `web/js/workflow/step05_cns.js:381, 395, 414`；`web/js/workflow/step06_review.js:58, 78`；`web/js/map/display_layers.js:489-501` | 读 `flow.coverage.layers` | 主界面仍渲染 legacy 2D 覆盖卡片与覆盖圈 |
| **不读 legacy coverage 的相关服务** | `gap/v1.py:26-30`（入参为 routes / required_cns / aircraft_profile / existing_facilities / device_catalog，**不含 coverage**）、`domain/reporting.py:45-68`（快照 keys 不含 `coverage` / `coverage_3d`）、`reporting/builder.py`（无 coverage 引用） | — | **更正常识：legacy 2D coverage 的消费者比预期少**；报告链与 Gap 链均不读它，其唯一硬依赖是 Step 门禁 + 主界面显示 + 站址导出 |
| `tests/test_*` | 多处（`test_workflow.py:63-65,72`、`test_closed_loop.py:192,211`、`test_cns_inputs.py:125,131`、`test_algorithm_registry.py:124,134,170,199`、`test_risk_aware_route_planner_v2.py:272,340`、`test_planning_report.py:126`、`test_spatial_3d.py:128-147,133,147`…） | fixture | 不属运行期消费者 |

### 4.4 调用链

```text
[canonical — 允许]
POST /api/coverage-3d/evaluate                 router.py:416
  → WorkflowService.evaluate_coverage_3d                        workflow_service.py:1392
  → Spatial3DService.evaluate                                   spatial_3d_service.py:78
      → 剔除 facility.planning_profile（保持 P7 输入指纹）
      → model.evaluate(operational_routes, spatial_3d, grid, grid_attributes, facilities, device_catalog)
      → state["coverage_3d"] = result                           :96   ★ canonical 写入
      → invalidation.cns_service_capability()                   :97
      → state["result_statuses"]["coverage_3d"] / ["report"]    :98-99
      → session.save()

[canonical — 旁路 1]
POST /api/cns-closed-loop/apply                router.py:301
  → ClosedLoopService.apply                                   closed_loop_service.py:45
      → 重跑 _build_assessment(original) 并与 Preview 指纹比对
      → committed = deepcopy(original)
      → committed["coverage_3d"] = planned["coverage_3d"]（+ cns_service_capability / service_timeline / cns_gap_analysis_v2 / existing_cns_facilities）  :80-85
      → _apply_post_commit_statuses(committed)                :88 → :336（手工 stale coverage(2D)/cns_gap/cns_site_plan/cns_corridor*/cns_plan_review/confirmed_cns_plan）
      → current.clear(); current.update(committed)            :89-90  ★ canonical 整表替换
      → session.save()                                        :91

[canonical — 旁路 2]
POST /api/cns-plan-review/apply                router.py:311
  → PlanReviewService.apply                                   plan_review_service.py:157
      → rerun_p7_p10_chain(baseline, ...)                     :174  ★ 写 deepcopy(baseline) 的 coverage_3d
      → working = deepcopy(baseline); rerun_p7_p10_chain(working, ...)  :175/183
      → working["cns_corridor_assessment"], working["cns_corridor_gap_assessment"] = planned_p14, planned_p15  :196
      → working["confirmed_cns_plan"] = committed_plan        :203
      → _post_apply_statuses(working) + mark_active_report_stale  :204-205
      → state.clear(); state.update(working)                  :206  ★ canonical 整表替换
      → session.save()                                        :207

[legacy 2D]
POST /api/workflow/coverage                    router.py:485
  → WorkflowService.plan_coverage                              workflow_service.py:1207
  → CNSPlanningService.plan_coverage                           cns_planning_service.py:35
      → 门禁：rules passed / result_statuses["routes"]=passed / operational_routes 全部 passed
      → state["coverage"] = planner.plan(operational_routes, devices)  :44   （planner 默认 CoveragePlannerV1）
      → invalidation.coverage_3d()                             :46（legacy 写入即 stale canonical ⇒ 方向正确）
      → session.save()
```

### 4.5 计数

| 指标 | 值 |
|---|---|
| canonical `coverage_3d` `CURRENT_WRITER_COUNT`（服务级） | **3** — `Spatial3DService`（允许）、`ClosedLoopService`（禁止）、`PlanReviewService`（禁止） |
| canonical 语句级内容写点 | 3 处（`spatial_3d_service.py:96`、`closed_loop_service.py:190`、`plan_review_service.py:174/183/206` 视为同一 P7-P10 重算链 1 处） |
| legacy 2D `coverage` 内容 writer | **1**（`CNSPlanningService`） |
| legacy 2D 生产写点合计 | **6** — `cns_planning_service.py:44`（内容）、`:45`（status）、`invalidation_service.py:91`（stale）、`closed_loop_service.py:351-352`（stale）、`plan_review_service.py:352`（stale）、`workspace_service.py:151`（重置为 `None`） |
| canonical `coverage_3d` 生产写点合计 | **5** — `spatial_3d_service.py:96`（内容）、`:98`（status）、`invalidation_service.py:499-500`（stale）、`closed_loop_service.py:190`（内容，经 `:81-91` 提交）、`plan_review_service.py:174/183`（内容，经 `:206` 提交） |
| 合并"能写任一 coverage 结果"的独立语义写点 | **11**（按 file:line 计 13 条） |
| L3 | `project_state.py:354`（`"coverage": None`）、`:357-358`（status 默认）、`:246`/`:495`（`coverage_3d` 默认与 backfill）、`:733`（`result_statuses` backfill） |
| `TARGET_WRITER_COUNT` | **1**（`Spatial3DService` + `GeometricCoverage3D`） |
| 需移除的 canonical 写权 | `closed_loop_service.py:190`（经 `:80-90` 提交）、`plan_review_service.py` 的 P7-P10 重跑写回（`:174/183/206`） |
| 前置（B2B-1 范围外但必须做） | `workflow_service.py:1047-1048` Step 5/6 门禁从 `coverage` 切到 `coverage_3d` |

### 4.6 关键回答（对应任务问题清单）

1. **哪个 key 是当前旧 coverage？** `coverage`（2D，`CoveragePlannerV1`）。
2. **哪个应成为 canonical coverage？** `coverage_3d`（`GeometricCoverage3DV1`）——代码已按此布置，但**默认算法选择与 Step 门禁仍指向旧 2D**。
3. **哪些消费者仍读二维 legacy coverage？** 见 4.3。**实测比预期少**：唯一硬生产读点是 `workflow_service.py:1047-1048`（Step 5/6 门禁），其次是主界面显示（`step05_cns.js:381/395/414`、`step06_review.js:58/78`、`map/display_layers.js:489-501`）与站址导出（`export_service.py:27`）。**报告链（`domain/reporting.py` / `reporting/builder.py`）与 Gap 链（`gap/v1.py`）都不读它** —— 这一点与"coverage 迁移影响面可能很大"的直觉相反，是 B2B-2 可拆细的依据。
4. **CoveragePlannerV1 是否仍写 canonical？** 否 —— 它写 `coverage`（legacy key），未污染 `coverage_3d`；但它的写入会 `invalidation.coverage_3d()`（把 canonical 标 stale），因此 legacy 与 canonical 之间是**单向 stale 耦合**，不是同名双写。
5. **本轮不迁移，仅建立事实图** —— 已按要求只记录不修改。

---

## 5 facility_plan writer map

### 5.1 事实：`facility_plan` 是 DAG 节点名，不是 state key

- 代码中**不存在** `state["facility_plan"]`。`facility_plan` 只作为 canonical DAG 节点出现（`domain/canonical_workflow.py:40`、`:142`），其 state 投影为 `canonical_workflow` 节点状态（`project_state.py:457`），**不含方案内容**。
- 目标 canonical `facility_plan` 的**实际承载体**是 **`cns_corridor_site_plan`**（P16，`CorridorSitePlanningService`）；报告链已按此消费（`reporting/builder.py:64` 读 `cns_corridor_site_plan`；`domain/reporting.py:53`）。
- 旧 `cns_site_plan`（P11，`ReuseFirstSitePlannerV1`）是其 legacy 前身，**仍在 production UI 暴露且仍被 canonical 侧读取**（见 5.3）。

### 5.2 Writer 表（L1 内容 writer）

| # | result | state key | writer/service | API | write method | current purpose | production allowed? | target action | compatibility requirement |
|---|---|---|---|---|---|---|---|---|---|
| 1 | canonical facility_plan | `cns_corridor_site_plan` | `CorridorSitePlanningService.evaluate`<br>`application/corridor_site_planning_service.py:143` | `POST /api/cns-corridor-site-plan/evaluate`（`router.py:305`） | `state["cns_corridor_site_plan"] = result` + `result_statuses` 映射（`:144-148`） | 目标态：走廊站址方案（P16） | **是（唯一允许者）** | 保持；B2B-2 以其为 allowlist 基准 | `test_corridor_site_planner_v2.py`、`test_cns_corridor.py`（**P14 保护测试文件，不得修改**）必须继续通过；输出指纹与 `status` 取值集合（`proposal_ready`/`no_action_required`/`no_eligible_proposal`/`evidence_required`）不得变化 |
| 2 | canonical facility_plan | `cns_corridor_site_plan` | `CorridorSitePlanningService._save_missing`<br>`application/corridor_site_planning_service.py:156` | 同上 | `self.session.state["cns_corridor_site_plan"] = self.planner.empty("missing_data")` + `result_statuses="missing_data"` | 上游缺失时降级为显式 missing 结果 | **是**（同一 owner 的降级分支） | 保持（建议在 guard 中登记为同 owner 方法） | 同上 |
| 3 | legacy site plan | `cns_site_plan` | `SitePlanningService.evaluate`<br>`application/site_planning_service.py:50` | `POST /api/cns-site-plan`（`router.py:299`，UI `step05_cns.js:461`） | `state["cns_site_plan"] = result` + `result_statuses`（`:51-53`） | 旧站址提案（`ReuseFirstSitePlannerV1` / 默认 `site_planner`） | **否** | B2B-2：退出 production 写权；要么写独立 legacy namespace，要么改为只读回放；`/api/cns-site-plan` 的 POST 降级为 compatibility | 旧项目 `cns_site_plan` 必须仍可读；`test_cns_site_planner.py`、`test_towers_real_data.py:312-324`、`test_bug_persist_reuse_tier_001.py` 的 legacy 断言要继续通过 |

### 5.3 L2 / L3 / L4 写点

| 层级 | 位置 | 说明 |
|---|---|---|
| L2 | `application/invalidation_service.py:570-571` | `cns_site_plan()` stale 自身 + `result_statuses` |
| L2 | `application/invalidation_service.py:612-613` | `cns_corridor_site_plan()` stale 自身 + `result_statuses`（随后 `cns_plan_review()`） |
| L2 | `application/closed_loop_service.py:356-358` | P12 提交后把 `cns_site_plan.status` 置 stale（不改内容） |
| L2 | `application/v3_operational_adoption_service.py:852-856`（索引 `_STALE_RESULTS` 含 `("cns_site_plan","cns_site_plan")`、`("cns_corridor_site_plan","cns_corridor_site_plan")`，见 `:69-82`） | V3-D publish 手工 stale |
| L2 | `application/plan_review_service.py:352` | `_post_apply_statuses` 状态映射（含 `cns_site_plan`、`cns_corridor_site_plan`） |
| L3 | `application/project_state.py:299/306`（`blank_project`）、`:419/425`（reuse-tier 迁移 stale）、`:610/618`（setdefault 容器）、`:724/728`（`result_statuses` 默认） | seed / normalizer / 迁移 |
| L4 | `tests/test_cns_site_planner.py`、`test_corridor_site_planner_v2.py`、`test_bug_persist_reuse_tier_001.py`、`test_layered_route_validation_adoption.py:362`、`test_towers_operational_integration_v2.py:773`、`test_towers_real_data.py:313`、`test_radar_surveillance_layout.py:2441` | fixture |

### 5.4 调用链

```text
[canonical — 允许]
POST /api/cns-corridor-site-plan/evaluate      router.py:305
  → WorkflowService.evaluate_cns_corridor_site_plan             workflow_service.py:1187
  → CorridorSitePlanningService.evaluate
      → 迭代 _what_if(action, ...)（只读上游 + 假设性走廊重算）
      → self.invalidation.cns_plan_review("p16_reevaluated")     corridor_site_planning_service.py:142
      → state["cns_corridor_site_plan"] = result                 :143   ★ canonical 写入
      → result_statuses 映射                                      :144-148
      → session.save()                                            :149

[legacy — 目标禁止]
POST /api/cns-site-plan                        router.py:299（UI step05_cns.js:461）
  → WorkflowService.evaluate_cns_site_plan                      workflow_service.py:1181
  → SitePlanningService.evaluate                                site_planning_service.py:27
      → 可选写 site_planning_policy + invalidation.cns_site_plan()   :35-36
      → _candidate_actions / _what_if（what-if 覆盖/能力计算，不落盘）  :40-45, :58+
      → self.invalidation.closed_loop_assessment()               :49
      → state["cns_site_plan"] = result                          :50    ★ legacy 写入
      → result_statuses["cns_site_plan"] / ["report"]            :51-54
      → session.save()                                            :55
```

### 5.5 计数

| 指标 | 值 |
|---|---|
| canonical facility_plan（`cns_corridor_site_plan`）`CURRENT_WRITER_COUNT` | **1**（`CorridorSitePlanningService`，2 个方法） |
| legacy `cns_site_plan` 内容 writer | **1**（`SitePlanningService`） |
| 全部 facility-plan-类写点（含 L2） | 12（canonical 内容 2 + legacy 内容 1 + L2 stale 5 + L3 迁移/seed 4；另有 4 处 setdefault 容器） |
| `TARGET_WRITER_COUNT` | **1**（`CorridorSitePlanningService`） |
| 目标态待关闭 | legacy `cns_site_plan` 的 production 写权；报告/门禁对 legacy key 的残余读取 |

### 5.6 关键回答（对应任务问题清单）

1. **旧 site plan 写什么 key、谁消费？** 写 `cns_site_plan`，唯一内容 writer 是 `site_planning_service.py:50`。消费者分三层：
   - **硬生产消费者（1）**：`ClosedLoopService` —— `closed_loop_service.py:65-67` 以 `cns_site_plan.status == "proposal_ready"` 与 `input_fingerprint` 为准入条件，`:99-104` 取 `selected_actions` 构造 application，`domain/closed_loop.py:116-120` 把它纳入 baseline 指纹。
   - **stale 生命周期消费者（2）**：`PlanReviewService._post_apply_statuses`（`plan_review_service.py:349-353`）、`V3OperationalAdoptionService._propagate_publish`（`:852-856`，索引 `:74`）。
   - **API/UI 兼容消费者**：`GET /api/cns-site-plan`（`router.py:60`）、`web/js/workflow/step05_cns.js:461`。
   - **已不消费**：报告链 —— `domain/reporting.py:47-60` 的快照白名单无该 key，`reporting/builder.py` 全文无命中（最终 facility plan 节取 `confirmed_cns_plan` + `existing_cns_facilities`，决策证据取 `cns_corridor_site_plan`，`:63-70`）。
   - **否证（重要）**：planner 层**没有** state 写权 —— `site_planner/reuse_first_v1.py`（`empty`/`plan`，`:20-41, 121-147`）与 `site_planner/corridor_reuse_first_v2.py`（`empty`/`rank`/`assemble`，`:19-21, 57-90`）都只返回 dict，不持有 session、不触碰 state。因此 facility plan 的 authority 问题**完全在 Application 层**，收敛不必触碰任何 planner 或算法。
2. **corridor site plan 写什么 key、谁消费？** 写 `cns_corridor_site_plan`；消费者：`PlanReviewService.initialize/_actions/_require_current_inputs`（`plan_review_service.py:40, 235, 263`）、`domain/plan_review.py:55-58`、`reporting/builder.py:64`、`domain/reporting.py:53`。
3. **两者是否互相覆盖或双写？** **不互相覆盖**，是两个独立 key、两条独立 API、两个独立 planner。同一 key 内部存在多条 stale 写路径（各 4~5 条），但**没有同一 key 的第二个内容 writer**。
4. **`cns_site_plan` 是否仍被 canonical 下游消费？** 是（ClosedLoop 前置 + V3-D stale + PlanReview 状态映射），但**报告与最终交付已不消费**；因此它的"仍活跃"主要是 P12/P18 的内部前置，而非产品输出。
5. **ClosedLoopService 是否写 site plan / 是否绕过 CorridorSitePlanningService？** 它**不写** site plan 内容（只置 stale 与作为前置消费）；也**不调用** `CorridorSitePlanningService` —— 它自建 P7-P10 工作副本（`rerun_p7_p10_chain`），最终改 `existing_cns_facilities` + 上游容器。这意味着 **P12 与 P16 是两条互不知情的方案链**，属目标态要收敛的缺口。
6. **目标 canonical `facility_plan` 对应哪个 key？** 现状与目标一致地落在 `cns_corridor_site_plan`（文档未要求改名，代码亦无别名）。

---

## 6 confirmed_plan writer map

### 6.1 事实

- canonical key：`confirmed_cns_plan`（**不是** `confirmed_plan`）；`confirmed_plan` 仅作为 canonical DAG 节点名（`domain/canonical_workflow.py:149,155`）。
- 目标唯一 writer：`PlanReviewService`（`PHASE4_TARGET_ARCHITECTURE.md:656`）。
- 目标约束：`Select != Confirm != Apply`；`ClosedLoopService` 不得直接 canonical Apply；每个 `(scenario_id, operational_route_id)` scope 只允许一个 active confirmed plan，历史记录可多个（`:588`、`:922`）。

### 6.2 Writer 表

| # | result | state key | writer/service | API | write method | current purpose | production allowed? | target action | compatibility requirement |
|---|---|---|---|---|---|---|---|---|---|
| 1 | confirmed_plan（内容） | `confirmed_cns_plan` | `PlanReviewService.confirm`<br>`application/plan_review_service.py:153` | `POST /api/cns-plan-review/confirm`（`router.py:310`） | `self.session.state["confirmed_cns_plan"] = plan`（`plan = empty_confirmed_plan("confirmed")` + update，`:132-152`） | 人工 Confirm 一个 variant，形成权威确认记录（含 `history` 归档旧记录） | **是（唯一允许者）** | 保持；B2B-1 以其为 allowlist 基准，并补 scope 唯一性 | `Select ≠ Confirm ≠ Apply` 必须保持：`select` 只改 `review.selected_variant_id`（`:101-108`），`confirm` 不改所选 variant，`apply` 不改确认内容；`test_plan_review.py`、前端 `step06_review.js:456-468` 契约不变 |
| 2 | confirmed_plan（状态） | `confirmed_cns_plan` | `PlanReviewService.apply`<br>`application/plan_review_service.py:203` | `POST /api/cns-plan-review/apply`（`router.py:311`） | `working["confirmed_cns_plan"] = committed_plan`（`status="applied"`，`:197-203`），随后 `state.clear(); state.update(working)` `:206` | 把 confirmed plan 实际应用到既有设施基线 | **是**（同 owner 的 Apply 命令） | 保持；但必须剥离它顺带对 canonical 上游（`coverage_3d` / `cns_corridor_*`）的写权（见第 4、9 节） | `plan_id` 校验、`source_fingerprints` 全量比对（`:167-170`）、`_p10_gate` 回归拒止（`:184-186`）、`_rejected(...)` 返回形状必须保持 |
| 3 | confirmed_plan（applicability） | `confirmed_cns_plan.current_applicability` | `InvalidationService.cns_plan_review`<br>`application/invalidation_service.py:625-629` | 由 P16/P14/上游变更触发（如 `corridor_site_plan → cns_plan_review()`） | `confirmed["current_applicability"] = "stale"; confirmed["stale_reason"] = reason` | 声明式失效：上游基线变化使 active confirmed plan 失效（**保留历史快照**） | **是（L2，合规）** | 保留，并纳入声明式失效表 | `status ∈ {confirmed, applied}` 才置 stale；历史 `history` 不得删除 |
| 4 | confirmed_plan（applicability） | `confirmed_cns_plan.current_applicability` | `ClosedLoopService._apply_post_commit_statuses`<br>`application/closed_loop_service.py:376-380` | `POST /api/cns-closed-loop/apply`（`router.py:301`） | `confirmed["current_applicability"] = "stale"; stale_reason = "P12 ExistingCNS application changed P18 baseline"` | P12 提交后使 P18 基线失效 | **否**（Advanced 服务直接改 canonical confirmed plan；应委托 `PlanReviewService` / `InvalidationService`） | B2B-1：改为调用 `PlanReviewService` 或 `InvalidationService.cns_plan_review(reason)` | `test_closed_loop.py` 中 P18 失效断言必须保持等价 |
| 5 | confirmed_plan（response-only） | — | `PlanReviewService._rejected`<br>`application/plan_review_service.py:268-271` | 所有 `/api/cns-plan-review/*` | `response["confirmed_cns_plan"] = result`（改 `snapshot()` 返回的 deepcopy） | 在响应里附加 `apply_attempt` 拒绝原因 | **是**（不改 state） | 保留；guard 需允许"仅改响应副本"的写法 | 不得误判为 canonical 写权 |
| 6 | confirmed_plan（seed） | `confirmed_cns_plan` | `application/project_state.py:308`、`:624` | `blank_project` / `normalize_project` | `empty_confirmed_plan()` 默认 | 空项目/旧项目 backfill | L3 | 保留 | 旧项目无该 key 必须可打开 |

### 6.3 三条语义路径（Select / Confirm / Apply）

| 阶段 | 实现位置 | 写什么 | 是否可绕过 |
|---|---|---|---|
| Initialize | `plan_review_service.py:36-63` | `state["cns_plan_review"]`（含 baseline + auto variant） | 需先有 current P14/P15/P16（`_require_current_inputs:258-264`） |
| Create variant | `:65-88` | `cns_plan_review.variants` | 拒绝原地修改已 confirmed variant（`:73-74`） |
| Evaluate | `:90-99` | variant.evaluation（**只读重算，不落 canonical 上游**，`:226` 明示 `persisted_as_upstream: False`） | — |
| **Select** | `:101-108` | `review["selected_variant_id"]` | **不写 confirmed plan** ✅ |
| **Confirm** | `:110-155` | `confirmed_cns_plan`（`status="confirmed"`，旧记录进 `history`） | 需 `confirmation_gate.status == ready_for_confirmation`（或显式 `confirm_without_objectives` + reason）✅ 有门禁 |
| **Apply** | `:157-211` | `confirmed_cns_plan`（`status="applied"`）+ **canonical 上游容器**（`:174/196/206`） | 需 `plan_id` 匹配 + `current_applicability=current` + 全量 `source_fingerprints` 比对 + P10 gate ✅ 有门禁，但**越权写上游** ⚠️ |

结论：**`Select != Confirm != Apply` 当前成立**（三段互不隐式推进，各自的写点互不重叠）；Confirm 与 Apply 的门禁完整。两点需要注意：

- **Confirm 不强制前置 Select**：`confirm` 取 variant 的表达式是 `payload.get("variant_id") or review.get("selected_variant_id")`（`plan_review_service.py:112`），因此调用方可以完全跳过 `/select` 直接确认任意 variant。语义上 `select` 目前只是 UI 辅助，不是状态机前驱。若目标态要求严格的 `Select → Confirm → Apply`，这是唯一需要产品决策的收紧点。
- **真正的问题不在语义分离，而在 Apply 的写入范围过宽**（`plan_review_service.py:174/183/196/206` 使其顺带成为 `coverage_3d` / `cns_service_capability` / `service_timeline` / `cns_gap_analysis_v2` / `cns_corridor_assessment` / `cns_corridor_gap_assessment` 的 writer），以及 `ClosedLoopService` 的旁路 applicability 写入。

### 6.4 scope 与 active 基数

| 目标 | 现状 | 证据 |
|---|---|---|
| 每个 `(scenario_id, operational_route_id)` scope 恰有 0 或 1 个 active confirmed plan | **未实现**：`confirmed_cns_plan` 是**单一对象**，只有 `current_applicability ∈ {not_evaluated, current, stale}`；`scenario_id` 在全仓库只出现于 `domain/operational_timing.py:178,186`，与 P18 无关；已确认 plan 与 review 均无 scope 字段 | `domain/plan_review.py:113-118`（`empty_confirmed_plan` 无 scope 字段）、`:103-110`（`empty_plan_review` 无 scope 字段） |
| 历史记录可多个、切换 active 不删除历史 | **部分实现**：`confirm` 时把旧记录压入 `history` 数组（`plan_review_service.py:127-130`），但**归档只保留 6 个键**（`plan_id`、`variant_id`、`decision`、`variant`、`application`、`current_applicability`），**丢失** `source_fingerprints`、`requirement_basis`、`confirmed_actions`、`acknowledgements`、`before_p15`、`after_p15`、`stale_reason` | `plan_review_service.py:128-135` |
| `ProjectState.active_ids.active_confirmed_plan_by_scope` | **不存在** | `project_state.py` 无该结构；`domain/project.py:54` 的契约字段只有 `confirmed_cns_plan` |
| scope 的隐式替代品 | `review_baseline_fingerprint`（`domain/plan_review.py:29-64`）把 `operational_routes`(:37)、`existing_cns_facilities`(:38)、`required_cns`(:32)、`candidate_sites`(:39)、`spatial_3d`/`grid`(:41-42)、P7-P16 结果身份(:48-59) 全部纳入指纹；confirm 写入 `source_fingerprints`（`plan_review_service.py:143-148`），apply 逐 key 比对（`:167-170`） | 航线集合一变即 `stale_plan` 拒绝，`tests/test_plan_review.py:82-84` 已覆盖 |

→ B2B-1 只能先加"同一时间仅一个 active、且 Apply 必须匹配当前 scope 指纹"的断言（guard + contract test）；真正的 per-scope map 属后续批次（需要先定义 `scenario_id`，属跨 P14-P19 的 schema 变更）。

### 6.5 计数

| 指标 | 值 |
|---|---|
| `CURRENT_WRITER_COUNT`（内容，服务级） | **1** — `PlanReviewService` ✅ |
| 内容语句写点 | 2（`plan_review_service.py:153`、`:203`） |
| L2 applicability 写路径 | **2** — `invalidation_service.py:625-629`（合规）、`closed_loop_service.py:376-380`（旁路） |
| L3 seed | 2（`project_state.py:308`、`:624`） |
| `TARGET_WRITER_COUNT` | **1**（`PlanReviewService`） |
| 待关闭 | `ClosedLoopService` 的 applicability 写权；Apply 的越权上游写；per-scope active map |

---

## 7 radar writer map

### 7.1 事实

- canonical key：`radar_surveillance_layout`（`project_state.py:270` 初始 `empty_radar_surveillance_layout()`）。
- 目标：`RadarSurveillanceLayoutService` 是唯一 production writer；Radar 保持 `KEEP_PRODUCTION`（Step 5），默认 OPTIONAL，仅当 adopted Required CNS / surveillance policy 明确要求时 REQUIRED（`PHASE4_TARGET_ARCHITECTURE.md:660,923`）。**本轮不修改 Radar。**

### 7.2 Writer 表

| # | result | state key | writer/service | API | write method | current purpose | production allowed? | target action | compatibility requirement |
|---|---|---|---|---|---|---|---|---|---|
| 1 | radar_surveillance_layout | `radar_surveillance_layout` | `RadarSurveillanceLayoutService.evaluate`<br>`application/radar_surveillance_layout_service.py:1321` | `POST /api/radar-surveillance-layout/evaluate`（`router.py:401-405`） | `state[LAYOUT_KEY] = records` | 显式一次求解 = 一次划设方案（proposal-only） | **是（唯一）** | 保持不动 | 不触碰；`test_radar_surveillance_layout.py`（123 KB）与前端 26 KB 测试不得回归 |
| 2 | radar_surveillance_layout（policy） | `radar_surveillance_policy` | `RadarSurveillanceLayoutService.set_policy` / `evaluate`<br>`:458`、`:462`、`:1257` | `POST /api/radar-surveillance-policy`（`router.py:400`） | `state[POLICY_KEY] = normalize_...` | 划设策略配置 | **是** | 保持 | 同上 |
| 3 | radar_surveillance_layout（status） | `radar_surveillance_layout` | `RadarSurveillanceLayoutService.stale_for_reason`<br>`:1905` | 由 `InvalidationService.radar_surveillance_layout()` 调用（`:253-265`） | `state[LAYOUT_KEY] = records`（仅改 status/stale_reason） | 声明式失效 | **是（L2）** | 保持 | 不得反向 stale 任何上游 |
| 4 | radar_surveillance_layout（snapshot） | — | `WorkflowService.snapshot`<br>`workflow_service.py:781-783` | `GET /api/workflow` | `result["radar_surveillance_layout"] = summary_snapshot()`（改快照副本） | 有界摘要投影 | **是（读投影）** | 保持 | 不得内联逐 sample 明细（已符合） |
| 5 | radar_surveillance_layout（seed/normalize） | `radar_surveillance_layout` | `application/project_state.py:541-543`（`normalize_project`）、`:270`（`blank_project`） | 打开项目 / 新建 | `value["radar_surveillance_layout"] = normalize_radar_surveillance_layout(value.get(...))` | L3 backfill **改写既有值**（非纯 setdefault） | L3 | 保留，但 B2B 后应核对 normalize 幂等性 | 旧项目必须可打开 |

### 7.3 关键结论

**`RadarSurveillanceLayoutService` 已经是事实上唯一 content writer。**

- `algorithms/radar_layout/*`（`v1.py` 875 行、`milp.py` 552 行、`candidates.py`、`geometry.py`）是纯算法层，不持有 session、不写 state。
- `gis/radar_layout_adapter.py` 是 GIS 证据适配层，不写 state。
- 该 service 自带 `FORBIDDEN_WRITE_KEYS`（`:70-76`，含 `cns_corridor_assessment`、`cns_corridor_gap_assessment`、`cns_corridor_site_plan`、`cns_site_plan`、`device_catalog`、`operational_routes`、`route_operating_layers`）与 `modifies_operational_routes: False`（`:107`）声明——**这是全仓库唯一的 write allowlist/denylist 实现，应作为 B2B guard 的范本**。

### 7.4 计数

| 指标 | 值 |
|---|---|
| `CURRENT_WRITER_COUNT`（内容，服务级） | **1** ✅ |
| 内容语句写点 | 1（`evaluate:1321`）+ policy 3（`:458/462/1257`） |
| L2 stale 写路径 | 1（`:1905`） |
| L3 | 2（seed + normalize） |
| L4 | `tests/test_radar_surveillance_layout.py` 多处 fixture |
| `TARGET_WRITER_COUNT` | **1** ✅ 已达标 |

---

## 8 router direct-write map

### 8.1 router 是否直接 mutate state？

**否。** `cns_planner/api/router.py`（525 行）中没有任何 `state[...] = ...`、`state.setdefault(...)` 或对 `session.state` 的字段赋值。六类结果的全部 router 入口都是**方法分发**：

| result | router 行 | 分发目标 | 是否 bypass Application owner |
|---|---|---|---|
| operational_routes | `:389` `/api/layered-operational-adoptions/apply`<br>`:478` `/api/workflow/operational`<br>`:344` `.../v3-operational-adoptions/apply` | `workflow.apply_layered_operational_adoption`<br>`workflow.generate_operational`<br>`workflow.apply_v3_operational_adoption` | 否（都经 workflow service）；但 `Router → WorkflowService → RouteService/V3-D` 链条本身是**被禁止的 writer**（问题在 owner，不在 router） |
| required_cns | `:278` `/api/required-cns`<br>`:282` `.../adopt`<br>`:481` `/api/workflow/required-cns` | `workflow.set_required_cns`<br>`workflow.adopt_required_cns_recommendation` | 否 |
| coverage | `:416` `/api/coverage-3d/evaluate`<br>`:485` `/api/workflow/coverage` | `workflow.evaluate_coverage_3d`<br>`workflow.plan_coverage` | 否 |
| facility_plan | `:299` `/api/cns-site-plan`<br>`:305` `/api/cns-corridor-site-plan/evaluate` | `workflow.evaluate_cns_site_plan`<br>`workflow.evaluate_cns_corridor_site_plan` | 否 |
| confirmed_plan | `:306-311` `/api/cns-plan-review/{initialize,variant,evaluate,select,confirm,apply}` | `workflow.initialize_cns_plan_review` … `apply_confirmed_cns_plan` | 否 |
| radar | `:400-405` | `workflow.set_radar_surveillance_policy` / `evaluate_radar_surveillance_layout` | 否 |

### 8.2 router 是否直接调用 algorithm？

**两处例外（但都不是写 state）**：

1. `router.py:7-10` 直接 import 并内联调用 safety 纯计算（`evaluate_service_state`、`evaluate_safety_events`、`evaluate_fault_tree`、`evaluate_coupled_events`），入口 `:235-261`。这些**不持久化**（Phase4-A `:436` 已标为"ADVANCED + 架构债"）。它们不属本轮 6 类 result，但违反"router 不得承担业务逻辑"目标态，属 B8 结构清理范围。
2. `router.py:197` `_building_footprints` 内延迟 import `gis.building_footprint_aggregation`（只读 GeoJSON，明示"不写任何项目状态"，`:194`）。
3. `router.py:443` `/api/source-audits/verify` 内延迟 import `gis.source_inspection.inspect_geopackage`（只读）。

### 8.3 router 是否承担 validation / business logic？

| 位置 | 内容 | 判定 |
|---|---|---|
| `:109-121` | `demo_preview_only` / `route_source` 查询参数解析并组装 payload | 传输层解析，**可接受** |
| `:125` | `route_id` 查询参数解析 | 可接受 |
| `:129` | `vertical_transition_validation_readiness(query)` 直接把 query 传入 | 可接受（解析在 service 内） |
| `:210-227` | `/api/building-footprints` 的 bbox / limit / tolerance 解析与容错 | 传输层解析，**可接受**（但含 role 白名单校验） |
| `:435-451` | `/api/source-audits/verify` 组装 `details`（含 `inspect_geopackage` 调用与 schema 提取） | **轻微越界**：router 承担了"审计输入构造"业务逻辑 |
| `:264-421` | 巨型 `resource_actions` 字典（150+ 条 lambda） | 传输分发，**符合目标态**（command dispatch） |
| `:490-512` | 数据源 payload 白名单清洗（`clean` 字典） | **轻微越界**：源角色白名单属领域校验 |

结论：router **不承担 authoritative business writer 角色**，也**不做 revision/token 校验**；越界仅限 `source-audits/verify` 的输入构造与数据源字段清洗两处（低风险，B8 处理）。

### 8.4 router 是否调用 session.save？

**直接调用：否。** 两处间接提交边界由 router 拥有：

| 位置 | 代码 | 说明 |
|---|---|---|
| `:514-516` | `_save_workflow` → `self.context.workflow.save()` | `/api/workflow/save` 的显式保存（`:486`） |
| `:430` | `with workflow.deferred_save():` → 先 `apply_population_grid_attribute`，块结束统一提交 | Population remap 的**一次提交**语义 |
| `:460` | `with workflow.deferred_save():` → 先 `set_workspace` 再 `apply_grid_attributes` | 工作区 + 属性重算的**一次提交**语义 |
| `:262-263` | `/api/project/save-as` / `/api/project/open` → `context.save_project_as` / `context.open_project` | 经 Application context，非直接 `session.save()` |

→ router 掌握"提交边界"（deferred save），但不掌握"写什么"。这与目标态"Router 只负责 transport / auth / revision guard / parsing / dispatch / response"**基本一致**，唯一需要未来收紧的是把 deferred-save 边界下沉为 Application command 的事务语义。

### 8.5 revision / token / request-id handling 在哪一层？

| 关注点 | 所在层 | 证据 |
|---|---|---|
| 会话 token 生成 | `application/app_context.py:45`（`secrets.token_urlsafe(24)`） | 组合根 |
| token 校验 | `api/server.py:52,64`（`valid_token(self.headers, self.context.token)`） | 传输层 |
| 同源校验 | `api/server.py:47,50,64`（`same_origin`）→ `api/security.py` | 传输层 |
| **revision guard** | `api/server.py:71-86`：`X-CNS-Revision` 必填；`StaleRevisionError` → HTTP 409 并回传当前 revision | **传输层**，不在 router、不在 service |
| mutation lock | `api/server.py:78-88`（`context.mutation_lock`，覆盖 revision 比对 + POST 分发） | 传输层 |
| revision 递增 | `application/session.py:78-114`（`_commit()` 原子递增 `revision` / `project.revision` / `workspace.revision`，失败回滚） | 持久化层 |
| 响应头 `X-CNS-Revision` | `api/server.py:56-60,89-92` | 传输层 |
| request-id | **不存在**（无 request id / trace id 机制） | — |

结论：**revision 与 token 处理都不在 router**，符合目标态；`request-id` 缺失属 B6 Heavy Task 范围（目标文档 `:666-690`）。

---

## 9 invalidation coupling map

### 9.1 两套体系

| 体系 | 位置 | 机制 | 覆盖 |
|---|---|---|---|
| 声明式 | `services/invalidation.py:8-52`（`DEPENDENTS`，52 键）+ `:55-63`（`ResultLedger.invalidate`） | 上游键 → 下游 result 名，`workflow(changed)` 统一执行 | 传统 P1-P18 主链 |
| 命令式 | `application/invalidation_service.py`（641 行，43+ 方法、9 个运行期注入 invalidator，接线在 `workflow_service.py:387-541`） | 专用方法 + 注入回调 | layered / V3 / Route3DProfile / VerticalTransition / SafetyEvidence / Radar / tower / adoption 等全部 additive 产物 |

Phase4-A `:973` 已指出"Radar / Route3DProfile / VerticalTransition / SafetyEvidence / layered_* / tower_* 全部绕开 `DEPENDENTS`，`DEPENDENTS` 已不是单一事实来源"。本轮审计在其之上补充了**具体的 writer→invalidation 耦合缺口**。

### 9.2 五类核心 result 的 writer → invalidation 耦合

| Writer（写入者） | 触发方式 | 实际 stale 的下游 | 缺口 |
|---|---|---|---|
| `LayeredOperationalAdoptionService.apply`（`:299`）/ `revoke`（`:364`） | `invalidation.operational_route_published({route_id}, reason=...)` → `:445-470` | `coverage_3d`、`building_clearance`、`cns_site_plan`、`cns_corridor`（→ gap → corridor_site_plan → plan_review → confirmed plan）、`report`、`route_safety_evidence`；并把已发布 route 恢复 `passed` | **①** `operational_route_published` 未登记在 `DEPENDENTS`；**②** 它故意不调用 `workflow("route")`（`:448-451`），因此 **`required_cns_recommendation` 不会被 stale**，而 `DEPENDENTS["route"]` 本来包含它；**③** 未直接 stale `radar_surveillance_layout`（radar 消费已发布航路，见 `radar_surveillance_layout_service.py:383,609`）——只能靠 `workflow_service.py:127-130` 的 `changed ∈ {route,...}` 分支，而该分支在 `operational_route_published` 路径下**不会触发** |
| `RouteService.generate_operational`（`:255`） | `invalidation.workflow("route")` | `DEPENDENTS["route"]` = `coverage, cns_gap, coverage_3d, cns_service_capability, service_timeline, cns_gap_v2, cns_site_plan, cns_corridor_assessment, required_cns_recommendation, report`；再由 `:120-130` 追加 `building_clearance`、`radar_surveillance_layout` | 与上方**同一业务语义、不同 stale 集合**（此处 stale recommendation，上方不 stale）⇒ 语义不一致 |
| `V3OperationalAdoptionService.apply`（`:775`） | `self._propagate_publish(published_ids)` → `:844-863`（**完全手工**，不经过 `InvalidationService`） | `_STALE_STATUS_KEYS` 11 个 + `_STALE_RESULTS` 12 个容器（`:61-82`）+ `mark_active_report_stale` | **④ duplicative**：与 `operational_route_published` 覆盖的重叠下游被两套代码分别维护；**⑤** 不含 `route_safety_evidence`（另一 writer 会 stale 它）⇒ 同一语义在不同发布路径下结果不同 |
| `CNSInputService.set_required_cns`（`:107`）/ `RequirementRecommendationService.adopt`（`:109`） | `invalidation.workflow("required_cns")` | `DEPENDENTS["required_cns"]` = `coverage, cns_gap, cns_service_capability, service_timeline, cns_gap_v2, cns_site_plan, cns_corridor_assessment, cns_corridor_gap_assessment, cns_corridor_site_plan, report` | **⑥ 缺 `coverage_3d`**：目标态 coverage 以 adopted required CNS 为 REQUIRED 输入（`PHASE4_TARGET_ARCHITECTURE.md:201,317`），当前 `coverage_3d` 的 evaluate 签名（`spatial_3d_service.py:91-95`）未消费 `required_cns`，因此"未 stale"暂时不产生错误结果，但**契约与实现不一致**，B3A/B4 接入后会成为真实漏洞；**⑦ `required_cns_adoption` 记录不被清除或标记** ⇒ manual 覆盖 recommendation 采纳结果后，`required_cns_adoption` 仍显示 `adopted`，`plan_review_service.confirm` 又把它写入 `requirement_basis`（`:139-142`）⇒ 权威记录可能失真 |
| `Spatial3DService.evaluate`（`:97`） | `invalidation.cns_service_capability()` → `:506-516` → `service_timeline` → `cns_gap_v2` → `cns_site_plan` → `closed_loop_assessment`；`cns_corridor` → `cns_corridor_gap` → `cns_corridor_site_plan` → `cns_plan_review`；`report`；`route_safety_evidence` | 完整下游链 | **⑧ writer 自己直接写 `state["result_statuses"]["report"] = "not_calculated"`（`:99`）**，绕过 `mark_active_report_stale`（其它路径用后者）⇒ 报告失效语义不统一 |
| `CNSPlanningService.plan_coverage`（`:46`） | `invalidation.coverage_3d()` | stale canonical `coverage_3d` + 其下游 | 合理的 legacy→canonical 单向 stale；**⑨** 但写 legacy `coverage` 的 writer 会让 canonical 结果变 stale，而反向不会 ⇒ 用户可能看到"canonical 陈旧只因 legacy 重算" |
| `ClosedLoopService.apply`（`:88`） | `_apply_post_commit_statuses(committed)`（`:336-380`，**手工**，不调 `InvalidationService`） | 手工置 stale：`coverage`(2D)、`cns_gap_analysis`、`cns_site_plan`、`cns_corridor_assessment`、`cns_corridor_gap_assessment`、`cns_plan_review`、`confirmed_cns_plan.current_applicability` | **⑩ command-specific manual stale**：完全绕开 `DEPENDENTS` 与 `InvalidationService`；缺 `radar_surveillance_layout`、缺 `route_safety_evidence_v2` |
| `PlanReviewService.apply`（`:204-206`） | `_post_apply_statuses(working)` + `mark_active_report_stale` | 状态映射（`:327-353`）+ report | **⑪** 同上，绕开 `InvalidationService`；且它**同时覆盖 canonical 上游内容**（`:174/196`），使"上游失效"与"上游被改写"混为一谈 |
| `CorridorSitePlanningService.evaluate`（`:142`） | `invalidation.cns_plan_review("p16_reevaluated")` | stale `cns_plan_review` + `confirmed_cns_plan.current_applicability` + report | 覆盖正确；**⑫** 但未 stale `report` 之外的任何下游（当前无其他下游，可接受） |
| `RadarSurveillanceLayoutService.evaluate`（`:1321`） | 未调用 `InvalidationService`（proposal-only，无下游） | 无 | 可接受；`stale_for_reason` 由其上游注入调用 |

### 9.3 缺口清单（任务要求的四类）

| 类型 | 实例 | 位置 |
|---|---|---|
| **writer 漏 invalidation** | ① `required_cns_recommendation` 在 layered adoption 发布后不 stale | `invalidation_service.py:445-470` vs `services/invalidation.py:11` |
| | ② `radar_surveillance_layout` 在 layered adoption 发布后不 stale | `invalidation_service.py:445-470`；对照 `:127-130` |
| | ③ `coverage_3d` 不在 `DEPENDENTS["required_cns"]` | `services/invalidation.py:14` |
| | ④ `required_cns_adoption` 不被任何失效覆盖 | `invalidation_service.py:632-641`（只处理 recommendation） |
| | ⑤ `route_safety_evidence_v2` 不在 V3-D `_propagate_publish` 下游 | `v3_operational_adoption_service.py:844-863` |
| **router 自己补 invalidation** | **无** — router 不调用 `InvalidationService` | `api/router.py` 全文无 `invalidation` 引用 |
| **duplicate invalidation** | ⑥ 同一"运行航路发布"语义存在三套实现：`operational_route_published`（layered）、`_propagate_publish`（V3-D）、`workflow("route")`（RouteService） | `invalidation_service.py:445`、`v3_operational_adoption_service.py:844`、`RouteService` `:255` |
| | ⑦ `cns_site_plan` 被 4 条命令式路径 stale（`:570`、`closed_loop_service.py:357`、`v3_...:852-856`、`plan_review_service.py:352`）而声明式 `DEPENDENTS` 里只由 `data/workspace/route/...` 驱动 | 各文件 |
| **command-specific manual stale** | ⑧ `ClosedLoopService._apply_post_commit_statuses` | `closed_loop_service.py:336-380` |
| | ⑨ `PlanReviewService._post_apply_statuses` | `plan_review_service.py:327-353` |
| | ⑩ `V3OperationalAdoptionService._propagate_publish` | `v3_operational_adoption_service.py:844-863` |
| **DEPENDENTS 未登记但 service 手工 stale** | ⑪ `operational_route_published`（无键） | `invalidation_service.py:445` |
| | ⑫ `building_clearance` / `route_vertical_profiles` / `route_safety_evidence_v2` / `radar_surveillance_layout` / `layered_*` / `tower_*` 全部无声明式键 | `invalidation_service.py:333-470`、`:253-302` |
| | ⑬ `report` 直接写 `result_statuses` 而非统一 `mark_active_report_stale` | `spatial_3d_service.py:99`、`site_planning_service.py:54`、`cns_planning_service.py:47` |

---

## 10 compatibility constraints

任务第 9 节要求：不得简单删除所有旧 writer；每条旧路径必须分类。分类如下。

| 类别 | 定义 | 本审计中的成员 |
|---|---|---|
| **A. 必须完全取消 production write** | 该路径可以继续存在/计算，但不得再写 canonical authoritative key | `route_service.py:243`（RoutePlannerV1/V2 → `operational_routes`）<br>`v3_operational_adoption_service.py:768`、`:913`（V3-D → `operational_routes`）<br>`requirement_recommendation_service.py:99`（recommendation → `required_cns`）<br>`closed_loop_service.py:190`（→ `coverage_3d`）<br>`plan_review_service.py:174/206` 的 P7-P10 上游写回（→ `coverage_3d` / `cns_corridor_*`）<br>`closed_loop_service.py:376-380`（→ `confirmed_cns_plan.current_applicability`）<br>`site_planning_service.py:50`（ReuseFirstSitePlannerV1 → `cns_site_plan`） |
| **B. 旧项目读取必须保留** | 只读兼容，任何批次不得破坏 | `normalize_project` 对 `operational_routes` / `required_cns` / `coverage` / `coverage_3d` / `cns_site_plan` / `cns_corridor_site_plan` / `confirmed_cns_plan` / `radar_surveillance_layout` 的全部 backfill（`project_state.py:255,270,288,299,306,308,354,495,541,585,610,618,624`）<br>`GET` 快照族 API（`router.py:36-181`）<br>`/api/export/project`、`/api/export/routes`、`/api/export/sites`（`router.py:162-164`）<br>旧项目 fixture 测试（`test_project_persistence_characterization.py`、`test_bug_persist_reuse_tier_001.py`、`test_zhoushan_golden_case_e2e.py`） |
| **C. 旧 API 可继续计算 compatibility result，但必须写独立 legacy/compatibility namespace** | 计算保留、键位隔离 | `/api/workflow/coverage` → `coverage`（已是独立 legacy key，符合 C）<br>`/api/cns-site-plan` → `cns_site_plan`（已是独立 legacy key，符合 C 的键位要求；待关闭的是它在 UI/门禁里的 production 地位）<br>`/api/workflow/operational` → 需新增 legacy namespace 承载 RoutePlannerV1/V2 结果（当前直接写 canonical，不满足 C）<br>V3-D preview 已满足 C（只预览、不落 canonical） |
| **D. 只存在测试 fixture / import migration，不属运行期 writer** | 不计入 writer 数，但产生迁移约束 | `tests/**` 中 40+ 处 `state["operational_routes"]/["coverage_3d"]/["required_cns"]/["cns_site_plan"]/...` 赋值<br>`benchmark/**` fixture<br>`_dsh_prof/ref/**` 代码副本（未跟踪、不得作为证据引用为运行期 writer） |

### B2B 最小迁移方式（原则）

1. **先断写权，后归档**：用 allowlist guard 在 Application 层拦截 A 类写点，不改算法、不删代码、不动测试语义。
2. **不双写**：A 类路径要么写独立 legacy namespace，要么只返回响应不落 state；禁止同时写 canonical + legacy。
3. **读旧写新**：B 类全部保留；C 类只新增 legacy namespace 承载，不改旧 key 含义。
4. **一次只动一类**：见第 11 节 B2B-1 / B2B-2 拆分。
5. **guard 由 contract test 双向保护**：既要证明"owner 能写"，也要证明"非 owner 写会失败"（目标文档 `:664`）。

---

## 11 B2B minimal file plan

原则：**不要为了完成 B2 一次修改几十个模块**。拆为 B2B-1（小范围、可直接做）与 B2B-2（影响面大、允许后置）。

### 11.1 B2B-1（推荐先做）：route + required_cns + confirmed_plan

| 动作 | 文件 | 具体改动 | 风险 |
|---|---|---|---|
| **新增 production authority guard** | `cns_planner/application/production_write_authority.py`（新增） | 提供 `WRITE_ALLOWLIST = {key: {owner_class}}`、`assert_write_authority(owner, key)`、`ProductionWriteAuthorityError`；以 `radar_surveillance_layout_service.py:70-76` 的 `FORBIDDEN_WRITE_KEYS` 为范本 | 低（纯新增） |
| **移除 V3-D 的 canonical route 写权** | `cns_planner/application/v3_operational_adoption_service.py` | `apply`（`:649`）在生产模式改为 raise / 仅返回 preview；`_publish_all`（`:733-776`）与 `_revoke_in_working_copy`（`:913`）不再写 `state["operational_routes"]`；保留 V3 历史 adoption 记录可读与 revoke 历史 | 中（需同步改 `test_route_planner_v3_operational_adoption.py` 的期望，但**不修改其算法断言**，只改"是否落 canonical"） |
| **收敛 RouteService 写权** | `cns_planner/application/route_service.py` | `generate_operational`（`:234-256`）不再写 `state["operational_routes"]`；结果写入新增 legacy 容器（如 `state["legacy_operational_routes"]`）或仅返回；清空/删除类写点（`:82/122/178/188`）保留但经 guard 登记为"派生回收"白名单 | 中（默认 planner 即 V1，改动会影响默认流程 ⇒ 需与前端 `step03_routes.js` 的提示同步；建议 B2B-1 内先"禁止写 canonical"并提供 legacy 视图，前端切换留 B4） |
| **required_cns 单写入口** | `cns_planner/application/requirement_recommendation_service.py`、`cns_planner/application/cns_input_service.py` | `adopt`（`:87-111`）改为调用 `CNSInputService` 的新 `adopt_required_cns(...)` 命令；`CNSInputService` 成为唯一 `required_cns` 赋值点，并在 adopt 时同时写 `required_cns_adoption` | 低（行为等价迁移） |
| **confirmed_plan 单写入口 + ClosedLoop 降权** | `cns_planner/application/closed_loop_service.py` | `_apply_post_commit_statuses`（`:376-380`）改为调用 `InvalidationService.cns_plan_review(reason)`；不再直接改 `confirmed_cns_plan` | 低 |
| **装配 guard** | `cns_planner/application/workflow_service.py` | 在 `__init__`（`:266-541`）为 owner service 注入 authority；`select_algorithm`（`:943-988`）对 `route_planner` / `layered_route_planner` 增加 capability 声明校验 | 低-中 |
| **新增 contract tests** | `tests/test_production_write_authority.py`（新增） | 见第 12 节 | 低 |
| **可选最小切片（facility_plan 的 content 收敛）** | `cns_planner/application/site_planning_service.py`、`cns_planner/api/router.py`、`cns_planner/application/workflow_service.py` | 仅关闭 `site_planning_service.py:50` 这一个违规 content writer（写独立 legacy namespace 或只读回放），`/api/cns-site-plan` 的 POST 降为 compatibility；**不动** P11/P16 算法、不动 registry、不动 `cns_site_plan` 的 5 条 stale 路径 | 低-中（3 文件即可让 canonical facility_plan 的 content writer 唯一；但会改变 `/api/cns-site-plan` 的写语义 ⇒ 需产品确认，见 13.3） |

**B2B-1 明确不做**：不改算法语义；不删 archive 代码；不动 P14 四文件；不重做前端（只改后端 authority 与提示文案）；不实现 per-scope active map（只加断言）。

### 11.2 B2B-2（可后置）：coverage + facility_plan

| 动作 | 文件 | 具体改动 |
|---|---|---|
| **canonical coverage 单 owner** | `cns_planner/application/spatial_3d_service.py` | 保持为唯一写入者；补 `required_cns` 输入契约（与 B3A 协同） |
| **ClosedLoop 停止写 canonical coverage** | `cns_planner/application/closed_loop_service.py` | 重算结果写 working-copy / proposal namespace（`:80-90`）；需要生效时委托 `Spatial3DService` |
| **PlanReview 停止整表替换上游** | `cns_planner/application/plan_review_service.py` | `apply`（`:157-211`）不再 `state.clear(); state.update(working)` 覆盖 `coverage_3d` / `cns_corridor_*`；改为逐 owner 提交或写入 plan-scoped proposal |
| **legacy 2D coverage 隔离** | `cns_planner/application/cns_planning_service.py`、`cns_planner/application/workflow_service.py`、`cns_planner/application/workspace_service.py`、`cns_planner/application/export_service.py`、`cns_planner/services/invalidation.py` | `plan_coverage` 保留写入 legacy `coverage`；`_steps()`（`:1047-1048`）门禁改为 canonical `coverage_3d`；`workspace_service.py:151` 的重置语义对齐新 namespace；`export_service.py:27` 的站址导出明确标 legacy；`invalidation.py:24` 的 `coverage_algorithm` 边随兼容键调整 |
| **legacy site plan 隔离** | `cns_planner/application/site_planning_service.py` | `evaluate`（`:27-56`）退出 production 写权（legacy namespace 或只读回放）；`/api/cns-site-plan` 的 POST 降级为 compatibility |
| **消费者收敛** | `cns_planner/reporting/builder.py`、`cns_planner/domain/reporting.py`、`cns_planner/domain/closed_loop.py`、`cns_planner/domain/plan_review.py` | 解除对 `cns_site_plan` 的报告/门禁依赖（ClosedLoop 前置需另定 canonical 来源） |
| **invalidation 单向化** | `cns_planner/application/invalidation_service.py`、`cns_planner/services/invalidation.py` | 把 `operational_route_published`、`_propagate_publish`、`_post_apply_statuses`、`_apply_post_commit_statuses` 反向登记为声明式边（保留现有方法作为执行器） |
| **normalizer** | `cns_planner/application/project_state.py` | legacy namespace backfill + 旧项目读取 fixture |
| **新增 contract tests** | `tests/test_coverage_facility_authority.py`、`tests/test_legacy_namespace_compat.py`（新增） | 见第 12 节 |

**拆分理由**：coverage 与 facility_plan 的写权纠偏牵连 `ClosedLoopService`（P12）、`PlanReviewService`（P18）、报告链与 Step 门禁，还牵动 `_dsh_prof/` 舟山数据验证过的既有 E2E（`test_zhoushan_golden_case_e2e.py`）。一次做完会同时改动 6+ 服务与 5+ 测试文件，违反"不要一次大重构"。

---

## 12 authority contract test plan

新增（B2B-1）：

| # | 测试 | 断言 | 目标文件 |
|---|---|---|---|
| 1 | `test_operational_routes_single_writer` | 构造完整 workflow，调用 `apply_layered_operational_adoption` 成功写入；随后直接构造 `V3OperationalAdoptionService` 与 `RouteService` 的 apply/generate 调用，断言**抛 `ProductionWriteAuthorityError`** 或**未写 canonical key** | 新增 |
| 2 | `test_v1_v2_cannot_write_operational_routes` | 对每个 `algorithm_selection["route_planner"]` 可能值（`route_planner_v1`、`risk_aware_route_planner_v2`）断言 `generate_operational` 不改变 `state["operational_routes"]`（可改变 legacy 容器） | 新增 |
| 3 | `test_v3d_cannot_publish_operational_routes` | `route-planner-v3-operational-adoptions/apply` 在 production evidence 下不得写 `operational_routes`；preview 仍可用 | 新增 |
| 4 | `test_required_cns_single_command_owner` | `set_required_cns` 与 recommendation `adopt` 均写入同一个 owner 记录的 adoption；断言 `RequirementRecommendationService` 不再持有赋值语句（可用 `inspect.getsource` 静态断言归属） | 新增 |
| 5 | `test_required_cns_recommendation_never_auto_writes` | 仅 `evaluate` 时 `state["required_cns"]` 不变 | 新增 |
| 6 | `test_closed_loop_cannot_write_confirmed_plan` | `apply_closed_loop` 后 `confirmed_cns_plan.plan_id` 不变；`current_applicability` 变化必须经 `PlanReviewService` / `InvalidationService` | 新增 |
| 7 | `test_select_confirm_apply_are_distinct` | `select` 不改 `confirmed_cns_plan`；`confirm` 不改 `selected_variant_id`；`apply` 不改 `variant` 内容 | 新增 |
| 8 | `test_router_has_no_state_write` | 静态扫描 `api/router.py`：不得出现 `state[` 赋值、`session.save`、`invalidation.` 调用 | 新增（静态断言） |
| 9 | `test_single_active_confirmed_plan_per_scope` | 同 scope 第二次 confirm 后，仅一个 `current_applicability == "current"`，历史 `len(history)` 增加 | 新增（B2B-1 先加前置断言） |
| 10 | `test_authority_allowlist_is_complete` | `WRITE_ALLOWLIST` 覆盖全部 6 类 canonical key，且每个 key 的 owner 与实际模块路径一致 | 新增 |

复用/扩展（不新增文件）：

| # | 现有测试 | 扩展内容 |
|---|---|---|
| 11 | `tests/test_layered_route_validation_adoption.py` | 追加"发布后 `required_cns_recommendation` / `radar_surveillance_layout` 必须 stale"的断言（对应 9.3 缺口 ①②） |
| 12 | `tests/test_cns_inputs.py` | 追加 `required_cns_adoption` 在 manual set 后的语义断言（缺口 ④） |
| 13 | `tests/test_radar_surveillance_layout.py:2433-2453`（`test_radar_layout_never_writes_cns_or_coverage_results`） | **不修改，仅作为 contract test 模板**：它已锁定"radar 不写 coverage / corridor / facilities"，B2B 的新 guard 应复刻其写法（先快照其它 key，再执行 owner 操作，再断言其它 key 逐字节不变） |
| 14 | `tests/test_architecture.py` | 追加"router 文件不得出现 authoritative 写入模式"的静态检查（与 #8 合并可） |
| 15 | `tests/test_plan_review.py` | 追加：(a) apply 前后 `plan_id` / `variant_id` / `confirmed_actions` / `source_fingerprints` / `history` 逐字节不变，仅 `status`→`applied`（覆盖 6.3 的 identity 要求）；(b) 连续 confirm 两个 variant 后 `len(history) == 1` 且 `history[0]["plan_id"]` 保留（覆盖 6.4）；(c) `invalidation_service.cns_corridor_site_plan()` 连续两次后 `stale_reason` 不二次漂移（失效幂等） |
| 16 | `tests/test_planning_report.py` | 追加：`confirmed_cns_plan.status == "confirmed"` 但 `current_applicability == "stale"` 时 `report_service.generate` 必须抛错（现行为见 `report_service.py:46-49`，当前无测试覆盖） |
| 17 | （可选）新增静态 AST 扫描 | 断言 `"confirmed_cns_plan"` / `"operational_routes"` / `"required_cns"` / `"coverage_3d"` / `"cns_corridor_site_plan"` / `"radar_surveillance_layout"` 的**赋值目标**在 `cns_planner/**` 中只出现在各自的 allowlist 文件内（`ast.Assign` / `ast.Subscript` / `dict.setdefault` 目标扫描），比运行时断言更早发现新旁路 |

**不得**在 B2B-1 修改的保护文件：`tests/test_cns_corridor.py`（P14）、`cns_planner/algorithms/corridor/v1.py`、`cns_planner/algorithms/coverage/geometric_3d.py`、`cns_planner/algorithms/service_capability/v1.py`。

---

## 13 risk / rollback plan

### 13.1 风险表

| # | 风险 | 触发条件 | 影响 | 缓解 |
|---|---|---|---|---|
| 1 | **默认路径行为改变导致既有 E2E 失败** | `RouteService.generate_operational` 停止写 canonical 后，用户点"生成运行航路"看不到结果 | Step 3 主流程观感断裂；`test_zhoushan_golden_case_e2e.py` 失败 | B2B-1 保留计算并写入 legacy 容器 + 返回 legacy 视图；E2E 断言改为读取新 namespace；或在 B2B-1 仅加 guard 告警而不硬失败（需明确选择，见 13.3） |
| 2 | **V3-D 研究线回归** | `apply` 改为不可写 canonical | `test_route_planner_v3_operational_adoption.py`（59 KB）大量断言失败 | 只改"是否落 canonical"的期望，不改 V3-C 验证/几何断言；保留 `preview` 全功能；保留历史 adoption 记录读取 |
| 3 | **P14 保护线被误伤** | 任何对 4 个保护文件的写入 | 违反 B0.1 §0.1 禁令 | B2B-1 计划中**不包含**这 4 个文件；改动前 `git status --short` 必须仍为同样的 4 个 ` M`；B2B-1 提交前再次核对 |
| 4 | **required_cns adopt 迁移引入指纹漂移** | adopt 委托后 `required_cns_adoption` 字段顺序/内容变化 | `plan_review_service.confirm` 的 `requirement_basis` 与 `test_required_cns_recommendation.py:73,192,198` 失败 | 逐字段保持：`status`/`recommendation_fingerprint`/`algorithm_id`/`algorithm_version`/`context_fingerprint`/`policy_fingerprint`/`required_cns_fingerprint`/`source`/`provenance`；用等价性测试对照迁移前后 dict |
| 5 | **invalidation 变化引起 stale 传播扩大** | 补 9.3 缺口 ①②③ | 更多结果被标 stale，用户需重算 | 只在 guard 落地后、以独立小提交补边；每条边配一个断言测试；先改"漏 stale"，**不动**现有 stale 集合 |
| 6 | **guard 误拦 owner 自身** | allowlist 只按类名匹配而 owner 通过 `deepcopy`/工作副本写入 | 正常发布被拒 | guard 采用"写入点显式 `owner=` 传参 + 类 allowlist"双条件；并允许 `_rejected` 这类"仅改响应副本"的白名单写法（`plan_review_service.py:270`） |
| 7 | **P12/P18 语义收敛影响报告** | B2B-2 改变 ClosedLoop/PlanReview 写入范围 | 报告 manifest 与指纹变化，历史报告对照失败 | B2B-2 单独批次；保留旧报告只读；新报告重新生成 |

### 13.2 回滚计划

| 场景 | 回滚动作 |
|---|---|
| B2B-1 任一 contract test 无法通过 | 单个文件级 `git checkout -- <file>`（**不含** P14 四文件）；guard 模块新增文件直接删除 |
| B2B-1 已提交但发现阻断 | 独立 revert 提交；因 B2B-1 不改算法、不改 state schema、不迁移数据，revert 无数据遗留 |
| B2B-2 回滚 | 同上；额外需确认 legacy namespace 中已写入的数据可被读取层忽略（namespace 为纯新增 key，旧代码不读即等价于未写） |
| 紧急停止 | `WRITE_ALLOWLIST` 增加"observer/告警模式"开关（不 raise 只记录），使 guard 变为纯监控 |

### 13.3 需要用户裁决的一个选择（B2B-1 前置）

`RouteService.generate_operational` 的收敛有两种强度，必须在 B2B-1 开始前确定：

| 方案 | 行为 | 优点 | 缺点 |
|---|---|---|---|
| **A. 硬失败** | 非 owner 写 canonical 直接 `raise ProductionWriteAuthorityError` | 语义最严格，contract test 最清晰 | 默认 UI 的"生成运行航路"按钮立即报错 ⇒ 必须先做前端提示（与"不重做 frontend"约束冲突，需最小文案改动） |
| **B. 软隔离**（推荐） | 非 owner 写 canonical 被重定向到 legacy namespace，canonical 不变，响应带 `compatibility_write: true` | 旧项目与既有 E2E 不中断；写权事实上已断开 | contract test 需断言"canonical 未变"而非"抛异常"；需新增 namespace key 与读取适配 |

### 13.4 验证门（B2B-1 完成条件）

1. `pytest tests/ -q` 全绿（尤其 `test_layered_route_validation_adoption.py`、`test_route_planner_v3_operational_adoption.py`、`test_plan_review.py`、`test_closed_loop.py`、`test_required_cns_recommendation.py`）。
2. `tests/test_production_write_authority.py` 全绿（第 12 节 10 项）。
3. `git status --short` 与 B2A 开始时**完全一致**（4 个 ` M` + 同样的未跟踪项），证明未触碰 P14 与舟山工作线。
4. `git diff --stat` 不含 `cns_planner/algorithms/corridor/v1.py`、`cns_planner/algorithms/coverage/geometric_3d.py`、`cns_planner/algorithms/service_capability/v1.py`、`tests/test_cns_corridor.py`。
5. 六类 result 的 `CURRENT_WRITER_COUNT` 在报告中给出的清单内各降为 `TARGET_WRITER_COUNT`（facility_plan / radar 已达标；confirmed_plan 需关闭 ClosedLoop 旁路；operational_routes / required_cns 需完成 A 类收敛；coverage 留 B2B-2）。

---

## 附录 A：本报告的关键证据索引

| 主题 | 文件:行 |
|---|---|
| `LayeredOperationalAdoptionService` 发布/撤销 | `application/layered_operational_adoption_service.py:182, 218, 247, 306, 327, 341, 376` |
| `RouteService` canonical 写点 | `application/route_service.py:82, 122, 178, 188, 243` |
| `V3OperationalAdoptionService` canonical 写点 | `application/v3_operational_adoption_service.py:44-82, 649, 733, 768, 844, 913` |
| `required_cns` 写点 | `application/cns_input_service.py:49, 106`；`application/requirement_recommendation_service.py:99` |
| `coverage` / `coverage_3d` 写点 | `application/spatial_3d_service.py:96, 98`；`application/cns_planning_service.py:44, 45`；`application/closed_loop_service.py:190, 350-352`；`application/plan_review_service.py:174, 183, 206, 349-353`；`application/workspace_service.py:151`；`application/invalidation_service.py:90-91, 496-500` |
| facility plan 写点 | `application/corridor_site_planning_service.py:143, 156`；`application/site_planning_service.py:50, 51` |
| facility plan planner 层否证 | `site_planner/reuse_first_v1.py:20-41, 121-147`；`site_planner/corridor_reuse_first_v2.py:19-21, 57-90`（纯计算，无 state） |
| confirmed plan 写点 | `application/plan_review_service.py:112, 127-130, 153, 203`；`application/invalidation_service.py:625-629`；`application/closed_loop_service.py:376-380` |
| radar 写点 | `application/radar_surveillance_layout_service.py:70-76, 458, 462, 1257, 1321, 1905` |
| radar 现有边界测试（模板） | `tests/test_radar_surveillance_layout.py:2433-2453` |
| 默认算法选择 | `algorithms/registry.py:139-169`（`route_planner`=RoutePlannerV1 `:142`；`coverage_planner`=CoveragePlannerV1 `:152`；`site_planner`=ReuseFirstSitePlannerV1 `:165`） |
| 声明式失效表 | `services/invalidation.py:8-52` |
| 命令式失效 | `application/invalidation_service.py:67-131, 445-470, 493-641` |
| revision / token | `api/server.py:47-96`；`application/session.py:61-114`；`application/app_context.py:45` |
| UI 暴露面 | `web/js/workflow/step03_routes.js:1537, 1807`；`step04_operation.js:280, 295`；`step05_cns.js:457, 461, 462, 463`；`step06_review.js:456-468`；`layered_operational_adoption.js:868, 883, 896` |
| 目标态 owner 裁决 | `PHASE4_TARGET_ARCHITECTURE.md:652-664` |

## 附录 B：口径与非目标复述

- 本报告计数口径见开头"审计口径"；`TARGET_WRITER_COUNT = 1` 为**服务级**。
- `CURRENT_WRITER_COUNT` 只统计 L1 内容 writer；L2/L3/L4 单独列出，避免与 `TARGET_WRITER_COUNT=1` 产生表面矛盾。
- 本报告未开始 B3 / B3A，未迁移任何 key，未修改任何旧算法、frontend 或测试。
- 未修改、未格式化、未 stash、未提交 P14 PERFORMANCE PATCH CANDIDATE 四文件与舟山数据/障碍检查工作线临时文件。
