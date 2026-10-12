---
name: cns-frontend-change
description: 改动 CNS-PLANNER 前端（原生 JavaScript ES Module、无构建步骤）时必须遵守的状态写入与地图图层约定。当任务涉及 cns_planner/web/、web/js/main.js、web/js/state/workflow_snapshot.js、六步工作台、地图图层/渲染、grid popup、slim workflow 快照、前端测试（tests/*.test.mjs、tests/grid_theme.test.js）、或用户说"页面上不对/图层不显示/数据不同步/前端报错"时加载。
whenToUse: 任何前端模块、状态同步、地图图层或渲染相关的改动。
---

# 前端变更规范

前端是**原生 JavaScript ES Modules，无构建步骤** —— 不存在打包产物，改完 JS 直接由浏览器加载。不要引入打包器、不要新增构建命令。

## 1. 唯一状态写入路径（最容易踩的坑）

全局 `flow` 状态**只能**经 `cns_planner/web/js/state/workflow_snapshot.js` 的 `applyWorkflowSnapshot` 写入。这是 BUG-GRID-POPUP-001 的修复约定：

- `applyWorkflow` / `mutate` / `refreshWorkflow` / `resourceAction` / 打开项目 / bootstrap **全部必须**经过 `applyWorkflowSnapshot`；
- 顺序固定：先安装 snapshot → 再按 `detail_available` 从 `GET /api/workspace/grid` + `GET /api/workspace/grid/attributes` hydrate 逐 cell 明细 → 最后才 render/paint；
- 竞态由单调递增的 `gridDataSerial` **独占裁决**，旧响应一律丢弃，绝不覆盖新工作区。

**不要绕过它直接写 `flow`。**

## 2. 快照为什么是 slim 的（不要"顺手"改回去）

通用 `/api/workflow` 快照为性能做 `slim_grid_attributes()`，**不含** `grid_attributes.*.cells`（上万 cells 塞进快照会显著拖慢）。逐 cell 明细靠上面的 hydrate 通道按需获取。

- **不要**为了方便把 cells 加回通用快照；
- popup 之类需要逐格数据的场景，走 hydrate 通道；
- `populationCellStructureDiagnostics` 在 mapping `status=passed` 但某 `grid_id` 缺记录时必须显式报 `population_cell_missing_for_grid_id`，**绝不静默退化为"缺少数据"**。

## 3. 地图图层约定

- **display-only 图层绝不参与规划判定**：适飞空域（`id="air"`，标注"仅显示，不参与路线约束"）、建筑轮廓图层（默认关闭，纯显示层，不写 ProjectState，不参与净空判定）。
- 建筑轮廓图层有 LOD 三档：`res>30` 不请求；`8<res≤30` 上限 600；`res≤8` 上限 3000。后端 `GET /api/building-footprints` 只读，qgz 由后端解析，按视图 bbox + RTree + 简化返回 GeoJSON。
- **前端不得做显示期几何裁剪**：`layered_candidate_overlay.js` 内已有 `slice(` / `pop()` / `clip` 反向断言锁定——裁剪会让地图几何与 objective / risk / LOS 账本不一致。
- 路径规划类 overlay 的坐标拟合：用 V3-B state path 的 (metric, geographic) 对拟合仿射/尺度映射；**不可拟合时不猜、不画**。

## 4. 关键文件地图

| 职责 | 文件 |
|---|---|
| 稳定入口 | `cns_planner/web/app.js` |
| 前端组装 | `cns_planner/web/js/main.js` |
| **唯一**全局状态写入 | `cns_planner/web/js/state/workflow_snapshot.js` |
| 六步模块 | `cns_planner/web/js/step01_project.js` … `step06_review.js` |
| 地图与图层叠加 | `cns_planner/web/js/map/` |
| 渲染 / 瓦片 / 网格主题 | `cns_planner/web/js/renderer.js`、`tiles.js`、`grid_theme.js` |
| 数据源中心 | `cns_planner/web/js/source_center.js` |

## 5. 测试与语法检查（Node 在进程内运行）

```powershell
# 前端纯函数测试（在仓库根目录执行）
node tests/frontend_modules.test.mjs
node tests/grid_theme.test.js

# 改动后的语法检查（最低要求）
node --check cns_planner/web/app.js
node --check cns_planner/web/js/main.js
```

> **不要用 `node --test <file>`** —— 受限沙箱下会因 piped stdio EPERM 失败。`node <file>` 即可（`node:test` 在进程内运行）。

## 6. 参考文档

- 决策索引与硬约束：`AI_DEV_CONTEXT.md`；开发协议：`AGENTS.md`
- BUG-GRID-POPUP-001 与 slim snapshot 的历史决策：`docs/decisions/2026-09-ai-dev-context-full-archive.md`（grep `workflow_snapshot` / `slim`）
- 适飞空域解耦（display-only）决策：同上（grep `display_only_reference_layer`）
