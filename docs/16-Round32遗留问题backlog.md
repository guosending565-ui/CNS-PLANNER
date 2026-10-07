# Round32 遗留问题 Backlog

登记于 **Round32-F**（修复 Step04 运行规则导致已发布 Layered Operational Route 被错误标记 stale）。
本轮只修复该 BLOCKER（分支 `fix/round32-rules-route-invalidation`），下列问题**均未**在本轮改动，
按裁定登记到此，各自等待独立轮次处理。

| # | 问题 | 现象 / 影响 | 处理约束与建议 |
|---|---|---|---|
| 1 | 大项目保存偶发 `WinError 5` | `project_state.json` 约 275 MB（真实项目副本实测 288 MB）时保存偶发拒绝访问 | **独立轮次**处理：先确认真正发生锁冲突的位置与目标文件完整性（文件句柄、杀软、`.bak` 轮换、`os.replace` 目标），再做最小重试/退避修复；**不要**直接扩大 `os.replace` 的重试次数 |
| 2 | 打开项目后旧阶段提示残留 | 打开项目后 `#projectOpenStatus` 长期停留在「正在打开项目… 正在初始化地图视图与工作区图层…」，而步骤状态已更新为最新 | 前端阶段提示的生命周期收敛；不得影响真实加载状态与 readiness |
| 3 | 后台任务终态的陈旧心跳 | 任务已结束，界面仍显示在运行的心跳 | 任务终态与心跳的一致性收敛 |
| 4 | `panelError` 历史消息 | `#panelError` 会保留与当前操作无关的历史文案（例如打开项目成功后的目录提示） | 面板错误区按操作生命周期清理 |
| 5 | `aircraftRoute` 下拉为空 | Step04「飞行规则」段的绑定航路下拉无选项 | 与 Step03 已发布航路（如 R0005）的选项来源对齐 |
| 6 | 报告 Radar planning proxy 披露 | 报告未充分披露 Radar 规划的代理口径 | 只读披露层，不改算法语义 |
| 7 | P17 Radar provenance | P17（连续服务可接受性）对 Radar 证据的来源披露不足 | 只读披露层，不改算法语义 |

## 说明

- 第 1 项属于**保存可靠性**问题，优先级高于其余各项，但不与本 BLOCKER 合并处理。
- 第 2–7 项均为界面/披露层问题，不改变 canonical DAG、失效语义或任何算法结论。
- 本轮未新增失效语义版本号：现有系统没有「失效语义正式版本号」这一机制，失效权威仍是
  `cns_planner/application/invalidation_service.py` 与 `cns_planner/services/invalidation.py` 的
  `DEPENDENTS`，本轮只在既有语义内收紧边界（`rules` 不再使正式采纳拥有的航路几何失效），
  未改动 `DEPENDENTS["rules"]`。
