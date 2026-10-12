---
name: cns-invalidation-chain
description: CNS-PLANNER 的 ProjectState 写入权限与失效传播规范：谁是某个容器的唯一写入者、失效必须单向且最小、以及 38 个定向失效入口的清单。当任务要写 ProjectState、新增/修改结果容器、改动上游输入后需要标记下游 stale、排查"改了数据但下游没失效/失效过头把结果清了"、或新增 schema 字段时加载。
whenToUse: 任何涉及状态写入、容器新增、失效传播、stale 语义或 schema backfill 的改动。
---

# 写入权限与失效传播规范

权威状态是 schema-v2 `ProjectState`（原子 JSON + `revision` 乐观锁）。**失效传播的唯一权威实现是 `cns_planner/application/invalidation_service.py`。**

## 1. 动手前必做：确认写入者

```powershell
# 例：查 operational_routes 由谁写
rg -n 'operational_routes' cns_planner/application
```

已知的权威写入者（**可能随后续开发变化，以 grep 结果为准**）：

| 容器 | 写入者 |
|---|---|
| `operational_routes` | `application/route_service.py`（场景/OD 写入）、`application/layered_operational_adoption_service.py`（正式发布/撤销）、`application/v3_operational_adoption_service.py`（V3-D 发布） |
| `grid_risk_v2` / `risk_policy_v2` | `application/risk_v2_service.py`（唯一） |
| `spatial_3d.route_3d_profiles` | `application/route_3d_profile_service.py`（唯一） |
| `layered_route_candidates` + 三类 layered 策略 | `application/layered_route_planner_service.py`（唯一；**不写** `operational_routes`） |
| `AltitudeLayer` / `RouteOperatingLayer` / 进离场程序 | `application/route_operating_layer_service.py`（唯一） |
| `vertical_transition_validations` | `application/vertical_transition_validation_service.py`（唯一） |
| 失效传播 | `application/invalidation_service.py`（唯一权威） |

**不得新增第二个写入者，不得绕过 service 直接改 `state[...]`。**

## 2. 失效三条铁律

1. **单向**：只 stale 依赖该输入的下游；**绝不反向 stale 上游**。
2. **最小**：不要图省事调用过宽的入口。
3. **证据保留**：旧结果变 `stale` 是**保留证据**，不是删除记录。stale 只可查看，不可继续 Apply 或生成新权威结果。

> 经典反例：给 route 发布调用 `workflow("route")` 会**立刻 stale 刚发布的结果**。V3-D 为此使用专用入口 `operational_route_published(route_ids, reason=...)`，只 stale downstream 并保持新 route 与 current validation 不变。

## 3. 定向失效入口清单（`InvalidationService` 实测 38 个）

**航路与风险**：`risk()`、`risk_v2(reason)`、`layered_route(reason)`、`planning_constraint_field(reason)`、`route_risk_profile(reason)`、`layered_route_validation(reason)`、`route_safety_evidence(reason)`、`operational_route_published(route_ids, *, reason, preserve_published_routes=...)`

**三维与净空**：`route_3d_profile(reason, *, propagate)`、`route_3d_profile_changed(reason)`、`vertical_transition_validation(reason)`、`vertical_transition_validation_changed(reason)`、`building_clearance(reason)`、`route_vertical_profiles(reason)`、`route_operating_layer(reason)`

**CNS 链**：`coverage_3d()`、`cns_service_capability()`、`service_timeline()`、`protection_envelope()`、`encounter_3d(reason)`、`closed_loop_assessment()`、`cns_corridor()`、`cns_corridor_gap()`、`cns_corridor_site_plan()`、`continuous_service(reason)`、`cns_plan_review(reason)`、`requirement_recommendation(reason)`

**安全与策略**：`safety_policy()`

**环境与源**：`grid_sources(changed_sources)`、`tower_data_changed(reason, *, include_derived)`、`tower_planning_policy_changed(reason)`、`surface_facts_changed(reason, *, include_facts)`、`surface_classification_changed(reason)`、`radar_surveillance_layout(reason)`

**通用与末端**：`workflow(changed)`（谨慎使用）、`report(reason)`

> 用 `rg -n '^\s{4}def ' cns_planner/application/invalidation_service.py` 重新确认当前清单。

## 4. 按业务场景的失效要点

- **grid / workspace 变化** ⇒ 所有网格属性、traffic/conflict、risk 失效或重算。
- **单源变化**（population / terrain / airspace）⇒ 只失效对应属性 + risk。`airspace` 与 `basemap` **不触发 planning 失效**（适飞空域是 display-only）。
- **Risk Framework V2 变化** ⇒ 只把 `grid_risk_v2` 标 stale，并追加 `layered_route_candidate`；**绝不** stale 当前 `routes`、`operational_routes`、CNS 结果或 legacy `grid_risk`。
- **算法选择变化** ⇒ 沿既有 route dependency 失效；`layered_route_planner` 选择变化只 stale candidate/mask。
- **schema 变更** ⇒ **additive + backfill 优先**：旧项目必须仍能打开；未知/损坏 JSON、加载失败、Save As 失败都**不得**替换当前有效项目。

## 5. 如何验证失效行为

多数字段级失效语义已有回归测试，先跑定向用例而不是自己重写：

```powershell
D:\tools\anaconda\python.exe -m pytest -q -x --tb=short -p no:cacheprovider tests/test_layered_route_validation_adoption.py
D:\tools\anaconda\python.exe -m pytest -q -p no:cacheprovider tests/test_algorithm_registry.py
```

先用 `rg -l 'invalidation' tests/` 找覆盖你那条链的用例文件。

## 6. 参考文档

- 各容器的完整失效规则（逐条）：`docs/decisions/2026-09-ai-dev-context-full-archive.md`（grep `失效`）
- 决策索引的写入者速查表：`AI_DEV_CONTEXT.md` §2
- 开发协议红线 6/7：`AGENTS.md`
