# Round37 T3D-3A P17 三维保护走廊实施计划

> 约束：本计划在现有 `feat/round37-t3d-3a-protection` 工作树内执行；依用户要求，本轮增量不 commit/push/merge/reset/clean/rebase。

## Task 1：保护几何与 P17 绑定（TDD）

**文件：**
- 新增 `tests/test_visualization_3d_protection_volume.py`
- 新增 `cns_planner/application/visualization_3d_protection_volume.py`

先写失败测试，覆盖 11 点精确绑定、14 点冲突、缺半宽 fail-closed、EPSG:32651 缓冲面积/边界/转弯/自交/往返、ALT-100 仅切片。再实现纯函数和只读装配器，运行该定向测试至通过。

## Task 2：入侵事件判定合同（TDD）

**文件：**
- 新增 `tests/test_visualization_3d_intrusion_contract.py`
- 新增 `cns_planner/application/visualization_3d_intrusion_contract.py`

先写失败测试，覆盖空真实轨迹 blocked、仅水平相交、完整垂直范围的三维进入/离开、缺监视证据不推导检测、historical/stale 隔离。再实现无状态合同函数并定向验证。

## Task 3：只读 API 薄接线（TDD）

**文件：**
- 修改 `cns_planner/application/visualization_3d_service.py`
- 修改 `cns_planner/api/router.py`
- 修改 `cns_planner/api/server.py`
- 新增或扩展相关 API 测试

先写接口失败测试，再接入 `GET /api/visualization-3d/protection-volumes`。真实模式读取 canonical state/现有 hydration；示意模式只用请求参数生成瞬时结果，不写状态。验证错误码、身份字段、缓存头和输入校验。

## Task 4：Cesium 懒加载展示（TDD）

**文件：**
- 新增 `cns_planner/web/js/visualization3d/protection_volume.js`
- 薄改 `cns_planner/web/js/visualization3d/app.js`
- 薄改 `cns_planner/web/visualization-3d.html`
- 新增 `tests/visualization3d_protection.test.mjs`

先写 Node 行为测试，再实现独立实体组、颜色、点击元数据、定位、清除和主动示意表单。验证默认关闭、开启按需加载、关闭清理、Viewer 身份不变，并运行相关 `node --check`。

## Task 5：验收与交付

- 运行受影响 Python/Node 定向测试及 `git diff --check`，不重复全量回归或规划。
- 在 `CNS_IMAGERY_ONLINE_MODE=disabled` 下启动服务，以真实 Edge GPU 分别截图历史 P17、当前 demo 冲突、工程示意，并记录 ArcGIS 上游请求为 0。
- 复核 revision 468、ProjectState SHA256 和受保护源数据未变，确认无新增 ProjectState writer。
- 写入 `D:\Projects\_cns_workspaces\deliverables\round37_t3d_3a_protection` 的报告、数值证据、截图与下一轮合同；停止服务，更新 CONTROL/BASELINE，释放锁。
