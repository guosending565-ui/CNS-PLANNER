# Phase36 Pre-3D checkpoint 运行与数据说明

本分支只冻结代码、测试、配置和可复现说明。GitHub 克隆**不包含**舟山权威项目状态、GPKG、TIF、缓存或运行结果，也不能仅凭克隆仓库宣称真实数据链已就绪。外部文件的大小和 SHA256 见 `phase36_pre3d_data_manifest.json`。

## 启动前准备

1. 使用 Python 解释器 `D:\tools\anaconda\python.exe`，按 `README.md` 启动或运行测试。
2. 在“数据源中心”登记本机实际数据路径，不要把其他机器的绝对路径写入仓库配置。
3. 对 buildings、terrain DTM 和 building grid 分别核对 SHA256；摘要不匹配时视为不同输入，必须重新审计，不得沿用本 checkpoint 的真实数据结论。
4. 权威 `project_state.json` 仅作为外部项目输入提供。加载后先执行来源审计和刷新恢复检查，再开始六步预验收。

## 版本化建筑网格重建口径

`zhoushan_building_grid_L8_v3_heightstats_v1.gpkg` 不随 Git 提供。若不能由项目负责人单独提供，可从已核验的 v3 建筑与原 L8 v3 网格副本重建，且不得覆盖源文件：

- `height_max_m`、`height_mean_m`、`height_p95_m` 使用与格网 footprint 精确相交的建筑集合及 `height_estimate_m`；
- `height_p95_m` 使用 NumPy 默认 linear percentile（R-7 / `PERCENTILE.INC`）；
- `valid_height_fraction = clamp((centroid_building_count - height_unresolved_count) / centroid_building_count)`；centroid count 为 0 时写 NULL；
- 不做额外取整，不把缺失写成 0，不升级 `predicted` 证据等级；
- 输出必须是新的版本化 GPKG，并保留 CRS、`grid_key`、几何、非目标列和空间索引；
- 重建后运行 `inspect_building_grid_height_statistics`，并核对清单中的 58,105,856 B 与 SHA256。若不能逐字节复现，应生成新的版本身份并重新执行失效传播与回归，不得冒用本清单摘要。

本机历史交付中的重建程序名为 `d3_recompute_grid_stats_v2.py`，但它不属于 Git checkpoint；需要时应随 Round36-D3 证据包单独提供，并先审查其中的输入/输出路径。

## 工程演示边界

R0005 / ALT-100 / 4tCa7 的工程条件接受只用于显式 demo projection。`predicted` 在严格验证中仍为 `unresolved`；demo Apply 不写 canonical `operational_routes`；Step05 只有显式 `engineering_demo_mode=true` 才可只读消费该投影。`operational_authorization=false` 同时适用于严格与工程演示记录：内部验证或发布不等于现实运行许可。
