# Round37 T3D-3A P17 三维保护走廊设计

## 目标与边界

本轮只提供只读保护几何、Cesium 展示和下一轮入侵事件判定合同；不重算 P17，不写 `ProjectState`，不把工程示意、历史结果或二维像素 fallback 升格为权威结果。

## 权威数据与绑定

- 战术 `protection_envelope` 与 P17 `cns_continuous_service.result.routes[].corridor` 严格分离。
- 历史 P17 corridor 绑定 R0005 的 11 点路线（9904.422448544 m），水平半宽 990 m；以路线几何指纹和逐点坐标双重校验。
- 7 点路线（9525.660063644 m）与当前 14 点 demo 路线（9404.937615151 m）均不得复用该 corridor；当前 demo 返回 `geometry_version_conflict`。
- 持久化 P17 算法版本 2.2 与当前代码 2.1 的差异原样披露，不重算、不迁移为 current。

## 接口

`GET /api/visualization-3d/protection-volumes`

- 默认 `mode=project_result`：返回历史可展示的水平边界/ALT-100 名义高度切片，以及当前 demo 的几何冲突状态。
- `mode=engineering_scenario_preview`：必须显式提供路线身份、水平半宽、下界和上界。所有数值必须有限、正向、有序，路线必须属于当前只读路线集合且位于项目范围。
- 响应携带对象 ID、路线/保护几何身份、P17 input fingerprint、算法版本、几何维度、current/stale、证据状态、`required_evidence` 和隔离声明。

真实 P17 对象 ID 为 `protection-corridor:{route_id}:{altitude_layer_id}`；工程示意使用独立的 `protection-preview:*` 命名空间、实体组和颜色。

## 几何与高度

- 水平走廊在 EPSG:32651 中用 Shapely 对路线缓冲，圆形端部和连接部，随后转换回 WGS84；对无效、自交输出先修复再规范化。
- 输出米制面积、边界、路线长度和可复算的几何指纹，测试覆盖转弯、自交及坐标往返。
- 历史 P17 仅有 ALT-100 名义高度切片。软件工程默认的 90–125 m 不构成已确认垂直界面，状态固定为 `vertical_extent_unconfirmed`，不得输出正式挤出体。
- 工程示意的上下高度完全来自本次用户输入，并显式标记 EGM2008 正高与非权威性质；Cesium 复用既有 EGM2008 到椭球高转换。

## 入侵语义合同

- `horizontal_projection_intersection`：只证明轨迹水平投影与走廊相交。
- `three_dimensional_volume_intrusion`：仅在保护体有确认或明确工程示意上下界、且轨迹同时满足水平和垂直条件时产生。
- `surveillance_detection`：只由独立真实监视证据确认，绝不由几何相交推导。

正式模式因真实轨迹为空而返回 blocked。历史/stale P17 不得驱动当前预警；模拟轨迹始终携带 `simulated=true`、工程假设和隔离字段。

## 前端生命周期

航路、历史 P17、冲突状态和工程示意分别呈现。业务图层默认关闭；开启时按需请求，关闭时清理对应实体；复用现有 Cesium Viewer、FABDEM/EGM2008 与 LazyLayerRegistry，不重建 Viewer，也不混用 Radar 楔体实体。
