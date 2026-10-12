---
name: cns-layered-route-change
description: 改动 CNS-PLANNER 生产航路主链（Layered Risk-Aware Theta* V2、candidate、feasibility mask、continuous validation、Route3DProfile、operational adoption）时必须遵守的开发流程与冻结边界。当任务涉及 cns_planner/layered_route_planner/、domain/layered_route.py、domain/vertical_transition_validation.py、layered_route_planner_service、layered_operational_adoption_service、route_3d_profile_service、vertical_transition_validation_service、RouteRiskProfile、theta_star_v2、航路 candidate/validation/adoption、或用户说"改航路规划/改 planner/发布航路"时加载。
whenToUse: 任何触碰生产航路规划语义、candidate 容器、连续验证判据、或航路发布/采用流程的改动。
---

# 生产航路主链变更规范

生产链固定为：

```
OD + 显式 AltitudeLayer
  → LayeredRiskAwareThetaStarV2（layered_risk_aware_theta_star_v2@2.0）
  → RouteRiskProfile
  → LayeredRouteValidation / VerticalTransitionValidation（独立连续验证）
  → LayeredOperationalAdoption（人工发布）
  → operational_routes
```

## 1. 先判定：这次改动落在哪一类

| 类别 | 是否允许 | 需要做什么 |
|---|---|---|
| bug 修复 | ✅ | 定位根因 + 加回归测试；不改语义 |
| 真实数据适配（显式来源/CRS/工程确认参数） | ✅ | 保留 `null != 0`，缺证据保持 `unknown/pending` |
| 性能与 UI | ✅ | 不得改变算法输出 |
| **改变搜索/启发/目标函数语义** | ❌ 默认禁止 | 必须走 §2 的显式版本化流程，且需人工确认真实场景 |

冻结对象（除上述三类外不得改动）：Theta* V2 搜索与启发函数、`theta_v2_objective_policy` 目标函数、`RouteRiskProfile` 域/阈值数学、`LayeredRouteValidation` 与 `VerticalTransitionValidation` 的判据与 verdict 映射、`Route3DProfile` 的几何派生与 fail-closed 边界。

## 2. 若确实要改语义：显式版本化流程

缺任何一步都算静默变更，必须返工：

1. 升 `algorithm_version` 或 `geometry_version`；
2. 更新 `input_fingerprint` / `validation_fingerprint` 的对应分量；
3. 旧 candidate / validation / adoption 显式变 `stale`（**保留为 stale 证据，不删除记录**）；
4. 跑完整回归（不只跑定向用例）；
5. 由人工确认真实场景（例如真实 N004 案例）后才可宣布生效。

## 3. 三条不可违反的语义红线

- **candidate 永远不是运行航路**：`LayeredRouteCandidate` 强制 `operational_route=false`、`continuous_validation_required=true`。不要为了让下游"跑通"而放宽这两个字段。
- **搜索 pass ≠ 验证通过**：feasibility mask 的 `pass` 只表示 coarse 战略包线可行，**不构成**连续验证证据；两者不一致时 fail-closed。
- **`unknown != 0`**：terrain/building 缺 height、缺 ground、`valid_height_fraction < 1`、NoData 一律 `unknown`，绝不补 0、绝不当作无约束。

## 4. 关键文件地图

| 职责 | 文件 |
|---|---|
| 生产水平 planner（Theta* V2） | `cns_planner/layered_route_planner/theta_star_v2.py` |
| feasibility mask / supercover | `cns_planner/layered_route_planner/feasibility.py`、`supercover.py` |
| 领域契约（request / policy / mask / candidate） | `cns_planner/domain/layered_route.py` |
| 建筑净空唯一实现（roof 公式） | `cns_planner/domain/building_clearance.py` |
| 垂向过渡验证契约 | `cns_planner/domain/vertical_transition_validation.py` |
| candidate/mask 唯一写入者 | `cns_planner/application/layered_route_planner_service.py` |
| 固定巡航 continuous validation 写入者 | `cns_planner/application/layered_route_validation_service.py` |
| 人工发布/撤销（唯一） | `cns_planner/application/layered_operational_adoption_service.py` |
| Route3DProfile 唯一写入者 | `cns_planner/application/route_3d_profile_service.py` |
| 垂向过渡验证编排 | `cns_planner/application/vertical_transition_validation_service.py` |
| GIS 边界（terrain/building 事实） | `cns_planner/gis/layered_feasibility_adapter.py` |
| 失效传播（唯一权威） | `cns_planner/application/invalidation_service.py` |

> 先确认写入者再动手。用 `rg -n '<容器名>' cns_planner/application` 复核，不要凭记忆。

## 5. 定向测试落点

```powershell
# 定向：只跑相关文件（开发循环默认）
D:\tools\anaconda\python.exe -m pytest -q -x --tb=short -p no:cacheprovider tests/test_layered_theta_star_v2.py
D:\tools\anaconda\python.exe -m pytest -q -p no:cacheprovider tests/test_layered_route_validation_adoption.py
```

先 `rg -l 'layered|theta' tests/` 找到真正相关的用例文件再跑，**不要**在开发循环里跑全量。

## 6. 参考文档

- 决策索引与六条硬约束：`AI_DEV_CONTEXT.md`
- 开发协议与红线：`AGENTS.md`
- 历史决策原文（grep 定位后再读）：`docs/decisions/2026-09-ai-dev-context-full-archive.md`
- V3 研究线（非生产）：`docs/route_planner_v3_architecture.md`
