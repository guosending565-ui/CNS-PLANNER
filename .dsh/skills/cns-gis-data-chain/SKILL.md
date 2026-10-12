---
name: cns-gis-data-chain
description: 改动 CNS-PLANNER 真实空间数据链路（数据源注册、路径解析、CRS、地形/建筑/人口事实、可行性与建筑净空适配）时的开发规范与必守约定。当任务涉及 cns_planner/gis/、cns_planner/data/registry.py、source_loader、layered_feasibility_adapter、building_footprint_aggregation、fine_environment_adapter、path_resolver、qgis_project_layers、DEM/FABDEM/建筑轮廓/人口栅格、或用户说"数据源没生效/建筑约束不对/CRS 不对/图层读不到"时加载。
whenToUse: 任何触碰 GIS 边界、数据源注册与解析、栅格/矢量读取、或把真实空间数据转成领域输入的改动。
---

# GIS 数据链路变更规范

分层固定为：`data/registry.py` 声明来源 → `gis/` 适配为领域输入 → `application/` 编排 → 算法层只消费标准输入。**算法包绝不 import QGIS/GDAL、不读文件。**

## 1. 六条硬约定（违反即返工）

1. **`degree-as-meter` 禁止**：栅格分辨率必须按 CRS 的真实线性单位换算。投影 CRS 用其自身已验证的 linear unit；地理 CRS 用 `Geod`（无 pyproj 时用有标签的球面 haversine）测**相邻像元中心**的地面距离。当 `resolution_m` 实际是度时，必须写 `unresolved` 并阻塞，**不得直接当米**。
2. **NoData 不填补、不当 0**：地形/人口 NoData ⇒ `missing_data`/`unresolved`；禁止 bilinear 填补、禁止当 0、禁止外包插。
3. **单部件 ≠ 多部件**：QGIS 投影后单部件 polygon 常变成 MultiPolygon。必须用逐部件提取（`_metric_rings_of()` / `building_geometry_quality.prepare_footprint_polygons`），**不得只调 `asPolygon()` 后静默返回空环**。
4. **源数据永不改写**：不 `make_valid` 源 geometry、不修改 gpkg/shapefile、不写回源文件；`source_modified=false` 是硬不变量。质量修复失败保持 `unknown`。
5. **建筑净空只有一套公式**：`cns_planner/domain/building_clearance.py` 的 `building_roof_elevation`（= ground + height）与 `evaluate_vertical_clearance`（= `minimum_z − (roof + vertical_clearance)`）是**唯一**实现。不得出现第二套 roof 公式。
6. **`unknown != 0`**：`building_count=0` 才表示无建筑垂向约束；`valid_height_fraction < 1`、height 或 ground 缺失一律 `unknown`。

## 2. 层级语义

- L8 建筑事实：按 cell bounds **精确直映**，不读取预处理 `grid_key` 作为正式编码。
- 非 L8：按当前层级对**原始 footprint** 做**精确几何聚合**（`building_footprint_aggregation`）——建筑质心分配计数与高度统计、`EPSG:32651` 米制 `footprint ∩ cell` 精确相交面积。`cross_level_averaging=false`、`cross_level_interpolation=false`。
- 显式声明 `unsupported` **优于**静默 coarsen。若要 coarsen，必须显式失败或显式声明，并记录 provenance。

## 3. 路径与图层解析

- 统一走 `cns_planner/gis/path_resolver.py`（单一格式表：`.qgz`/`.qgs`/`.gpkg`/`.shp`/`.geojson`）。
- QGIS 工程图层解析用 `cns_planner/gis/qgis_project_layers.py`（纯标准库解析 qgz/qgs）。**远程 datasource 绝不当本地文件**。
- 每次 `metadata()` 都要做 `exists`/`is_file` 检查（`path_checks`），避免复用旧 schema/extent 造成"缓存旧状态"。

## 4. 关键文件地图

| 职责 | 文件 |
|---|---|
| 数据源声明（id/category/type/formats/health…） | `cns_planner/data/registry.py` |
| 统一路径解析 | `cns_planner/gis/path_resolver.py` |
| QGIS 工程图层解析 | `cns_planner/gis/qgis_project_layers.py` |
| 源加载总入口 | `cns_planner/gis/source_loader.py` |
| layered planner 的地形/建筑事实边界 | `cns_planner/gis/layered_feasibility_adapter.py` |
| 建筑轮廓层级无关聚合 | `cns_planner/gis/building_footprint_aggregation.py` |
| 建筑几何质量评估 | `cns_planner/domain/building_geometry_quality.py` |
| 建筑净空唯一公式 | `cns_planner/domain/building_clearance.py` |
| V3-B/V3-C 精细环境与原生栅格窗口 | `cns_planner/gis/fine_environment_adapter.py` |
| 数据源健康与审计 | `cns_planner/data/health.py`、`source_audit` |

## 5. 定向测试落点

```powershell
D:\tools\anaconda\python.exe -m pytest -q -x --tb=short -p no:cacheprovider tests/test_building_level_independent_facts.py
D:\tools\anaconda\python.exe -m pytest -q -p no:cacheprovider tests/test_building_environment_clearance.py
D:\tools\anaconda\python.exe -m pytest -q -p no:cacheprovider tests/test_data_semantics.py
```

先 `rg -l 'footprint|building|raster|resolution' tests/` 定位真正相关的用例，不要跑全量。

## 6. 真实数据本机事实（省得重新探索）

- FABDEM V1.2 DTM：`7200×10800`、Float32、NoData `-9999`、`EPSG:4326`、1 arc-second，元数据明确 `EGM2008_orthometric`。
- GBA buildings：538,228 个 MULTIPOLYGON、`EPSG:4326`、带 RTree，字段含 `id/source/height_m/height_var/height_status`。
- L8 building grid：143,013 个 POLYGON、`EPSG:4326`。
- **`GLO-30 DSM` 只用于原有地形链，明确禁止用于屋顶公式。**

## 7. 参考文档

- 数据源角色与本机默认路径：`docs/decisions/2026-09-ai-dev-context-full-archive.md`（grep `数据源角色`）
- 建筑环境与净空链的历史决策：同上（grep `FABDEM` / `建筑`）
- 决策索引：`AI_DEV_CONTEXT.md`；开发协议：`AGENTS.md`
