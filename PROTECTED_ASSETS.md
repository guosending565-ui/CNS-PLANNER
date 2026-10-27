# PROTECTED_ASSETS.md — 发布基线受保护资产与决策台账

> 适用基线：`origin/main @89b008d` + C3-A/B/C 研究切片；候选 `_work/d1/rc`。
> 本文件是 **Git 可跟踪的保护依据**。任何清理、分支删除、`git worktree prune`、
> 目录整理之前，**必须先读完本文件**。
>
> **2026-10-27（D1）重写说明**：旧版保护的是 `89e2f64` 系三维系统的 15 个文件
> （`visualization_3d_service.py`、`visualization-3d.html` 等）。这些文件**不在**
> `89b008d`，干净检出后不存在；原样保留会让复现出现死链。本版按**当前发布候选**
> 重写，并把旧三维资产降为 §5 的**历史附录（NOT_IN_BASELINE）**。

---

## 0. 一句话结论

本发布基线保护四类资产：**P14 语义保护文件**、**N v2 研究合同与运行时**、
**研究图件模板/渲染入口**、**Cesium 运行时 vendor**。
外部数据（FABDEM、EGM2008、land_mask、建筑、铁塔、研究运行根）**不入 Git**，按 SHA-256 校验。
旧三维系统属于历史分支，**不属本基线**，不得混入。

---

## 1. P14 语义保护文件（最硬约束）

### 1.1 四个文件

| 文件 | 作用 |
|---|---|
| `cns_planner/algorithms/corridor/v1.py` | P14 走廊评估；C3-B0 仅追加 `navigation_integrity_supplement`（显式配置才生效） |
| `cns_planner/algorithms/coverage/geometric_3d.py` | 三维几何覆盖 |
| `cns_planner/algorithms/service_capability/v1.py` | 服务能力判定 |
| `tests/test_cns_corridor.py` | 走廊契约测试 |

### 1.2 哈希规则（LF 规范化）

- 基线文件：`tests/p14_protected_sha256.json`。
- 计算方式：读取工作树字节后 `.replace(b"\r\n", b"\n")`，再 SHA-256。
- **不得**用 CRLF 原始字节作为基线；Windows 检出差异不是语义漂移。
- 三个测试以同一规则校验：
  `test_phase4_b6r_immutable_task_input.py`、`test_phase4_b6x_heavy_task_runtime.py`、
  `test_phase4_b9r1_async_admission.py`。

### 1.3 当前值（2026-10-27 D1 复核，全部一致）

| 文件 | LF SHA-256 |
|---|---|
| `cns_planner/algorithms/corridor/v1.py` | `80240cf8d79b50f6b7edc429443b8913c27cad63fa7fcfd4fa906d5443742cf7` |
| `cns_planner/algorithms/coverage/geometric_3d.py` | `68f39a884ce173d11ae943a85bdb9f7b66daea2f124cdd8e5bc716a745c5f01b` |
| `cns_planner/algorithms/service_capability/v1.py` | `4632d2996e2fc59b8d21ec79e98676e1731d87106d53b2b600a9d176f2352a1c` |
| `tests/test_cns_corridor.py` | `6c95ea7fcb5ca0f152e215d7b90a7a8948260f32ab9e01a18db214f5dd0c6451` |

> D1 结论：已正确进入当前集成树，**不再重新基线化**。若未来修改 corridor 的补充逻辑，
> 必须在同一批次显式更新 JSON 与三个测试，并记录 decision_id。

### 1.4 修改纪律

- 任何改动 `corridor/v1.py` 的批次必须同时给出：无配置时旧结果/指纹不变的证据、LF 哈希、
  三个 P14 测试通过。
- 不允许“为了让测试变绿”而静默改基线；必须写清是 bug 修复、数据适配还是语义变更。

---

## 2. N v2 研究合同与运行时

### 2.1 合同文件

| 文件 | 约束 |
|---|---|
| `cns_planner/domain/navigation_integrity_contract_v2.py` | `RESEARCH_ONLY=True`、`CONSUMER_SCOPE="research_only"`、`RADIUS_M=1000.0`、显式 opt-in |
| `cns_planner/domain/navigation_integrity_monitoring.py` | 正式 v1 默认（10 000 m、`confirmed=true`）；仅显式 v2 标记委派 |
| `cns_planner/domain/cns_service_registry.py` | `_normalize_navigation_integrity_planning`：未声明版本走 v1；未知/冲突 fail-closed |
| `cns_planner/domain/navigation_middle_reuse.py` | NM3 中段复用（端点 + 2 个不同物理站中段） |
| `cns_planner/site_planner/navigation_middle_reuse_v1.py` | 研究算法注册包装 |
| `cns_planner/application/navigation_planning_integration.py` | NM3 规划集成与 P16/P17/P18 投影 |
| `cns_planner/application/navigation_integrity_research_service.py` | 研究只读服务 |
| `config/navigation_integrity/*.json` | 显式研究配置 |

### 2.2 不变量

1. 正式默认、排序、指纹在无显式 v2 时**不变**。
2. v2 只服务研究链（NM3 / R0005）；不得写入正式 ProjectState。
3. 缺坐标/缺身份 → `unknown`，不得猜测或回退 v1 冒充 v2。
4. 端点为 `confirmed_deficit` 时不得读作 satisfied。

### 2.3 研究运行时

| 文件 | 约束 |
|---|---|
| `cns_planner/application/research_run_service.py` | 只读运行注册/清单/恢复；要求 `authority=research_isolated` |
| `cns_planner/application/research_compute_service.py` | fresh_compute 编排；结果写研究根，不写正式状态 |
| `cns_planner/application/research_input_service.py` | 2 km 写入设备/RequiredCNS；surface facts 重建 fail-closed |
| `cns_planner/application/research_parameters.py` | 参数合同；改参数必须产生新指纹 |
| `cns_planner/application/research_map_service.py` | 二维投影（只读 manifest） |
| `cns_planner/application/research_report_service.py` | 报告同源生成（HTML/Markdown） |
| `cns_planner/application/research_visualization_service.py` | 三维投影；EGM2008 fail-closed |
| `cns_planner/api/router.py`、`server.py` | read scope、写 token、禁止 formal mutation |
| `cns_planner/tasks/*` | 研究任务类型；fresh_compute / resume_checkpoint |

### 2.4 研究前端

`cns_planner/web/research.html`、`research_3d.html`、`research.css`、`research3d.css`、
`js/research_app.js`、`js/research_map.js`、`js/research_map_renderer.js`、`js/research_runs.js`、
`js/research_3d.js`、`js/visualization3d/basemap.js`。

约束：前端只读 manifest；不复制业务门限（不得出现 `4000/2000/5000` 或 `2/1` 的硬编码半径/重数）；
地图覆盖圈只投影输入半径；Radar 无覆盖。

---

## 3. 图件模板与渲染入口

| 文件 | 保护点 |
|---|---|
| `cns_planner/reporting/map_templates/template_catalog.py` | `CNS_COVERAGE_POLICY`：C 2 km；RID 陆/岸 2 km、海 5 km；N v1 10 km；图例名 |
| `cns_planner/gis/qgis_figure_renderer.py` | `LEGEND_COMPACT_LABELS`：通信 2 km、RID 海 5 km |
| `cns_planner/application/map_figure_service.py` | 通信/RID 披露；覆盖圈 `input_radius` 语义 |
| `tools/render_r0005_research_figures.py` | 六图装配；RID 披露陆/岸 2 km、海 5 km |
| `tools/build_r0005_terrain_grid.py` | 三维显示地形（FABDEM + EGM2008） |

### 3.1 图面铁律

1. 覆盖圈是**输入半径投影**，不是保障结果；必须写 `input_radius_projection_not_business_result` 等价披露。
2. 通信 C：2 km；不得残留 4 km 图例。
3. RID：陆/岸 2 km、海 5 km；单一 5 km 圆必须披露“按海上上限绘制、未按 surface 拆分”。
4. Radar：`not_calculated/unknown` 时**不画任何覆盖几何**；unknown 不得读作 pass。
5. N：动作点 proposal_only；1 km 只是输入半径，不画成覆盖。

---

## 4. Cesium 运行时 vendor

| 项 | 值 |
|---|---|
| 目录 | `cns_planner/web/vendor/cesium/` |
| 版本 | CesiumJS 1.126 |
| 许可 | Apache-2.0（`LICENSE.md` 含 third-party 声明） |
| 文件 | `Cesium.js`、`LICENSE.md`、`LICENSE-PROVENANCE.md`、`Widgets/widgets.css`、`Assets/approximateTerrainHeights.json`、`Assets/Images/ion-credit.png`、`Assets/IAU2006_XYS/{0,18}.json` |
| 状态 | **已裁定入 Git**（D2；`.gitignore` 仅放行 `vendor/cesium/`，其它 vendor 仍排除） |

- 文件数恰好 8（含 `LICENSE.md` 与 `LICENSE-PROVENANCE.md`）；完整 SHA 表见 `docs/22` §3.1。
- 许可 LF 规范化 SHA：`LICENSE.md` `3111d10e…`、`Cesium.js` `b796f8a6…`、`widgets.css` `702c5adf…`；
  PNG `ion-credit.png` 用原始 SHA `721870c1…`（不做 LF 规范化）。
- 若未来改为外置：必须登记 SHA；干净检出三维页显式报“vendor 未提供”，不得静默 404。

---

## 5. 历史三维资产（NOT_IN_BASELINE，仅追溯）

以下内容来自 `89e2f64` 系工作树，**不属于** `89b008d` 发布基线，**不作为**本基线复现依据：

- 15 个受保护源码：`application/visualization_3d_service.py`、`visualization_3d_imagery_service.py`、
  `gis/terrain_tile_adapter.py`、`api/router.py` 三维段、`web/visualization-3d.html`、
  `web/tiles.js`、`web/js/visualization3d/*`（9 个模块）。
- 机器清单：`docs/3d-protected-assets.json`（**未随本基线发布**）。
- 校验工具：`tools/verify_3d_assets.py`（**未随本基线发布**）。

如需恢复该三维系统，必须另立基线、单独验收；不得把它的文件/哈希混入本清单，
也不得因为“文件曾在别的工作树存在”就声称本基线具备该能力。

---

## 6. 外部数据（不入 Git，按 SHA 校验）

| 资产 | 路径（本机） | SHA-256 |
|---|---|---|
| FABDEM DTM | `D:\aaa2026project\...\Zhoushan_FABDEM_DTM_30m.tif` | `21406bd901fbebae…b13d8799b8` |
| EGM2008 geoid | `D:\aaa2026project\...\geoid\us_nga_egm08_25.tif` | `4191d471eefebf24…272bef17a` |
| land_mask | `projects\derived\land_mask_osm\zhoushan_land_osm_v2.gpkg` | `fe7918c96636178b…cbb6180aef` |
| cartographic_land | `projects\derived\cartographic_land\cartographic_land_v1.gpkg` | `647e1d7bff1c5ccb…c704783726` |
| 铁塔 xlsx | `D:\aaa2026project\...\航路航线规划-铁塔数据.xlsx` | `d722c111596acb47…2e2a411b` |
| 建筑 gpkg | `D:\aaa2026project\...\zhoushan_buildings_v3.gpkg` | `4f1657495d8422ac…c7391b97` |
| 建筑 L8 网格 | `D:\aaa2026project\...\zhoushan_building_grid_L8_v3.gpkg` | `06f9d9a66671d8fc…c3cb8b157f4` |
| 研究运行根 | `_work/c3b/run/`（**临时目录，待固定**） | 见各 run manifest |
| 源研究输入 | `_work/c2ar/research/`（**临时目录，待固定**） | 源状态 `c7822166…` |

完整表与入 Git/外置规则见 `docs/22-发布依赖环境与许可证MANIFEST-2026-10-27.md`。

---

## 7. 恢复与校验命令（只读）

```powershell
# P14 保护哈希（LF 规范化）
D:\tools\anaconda\python.exe -m pytest -q tests/test_phase4_b6r_immutable_task_input.py tests/test_phase4_b6x_heavy_task_runtime.py tests/test_phase4_b9r1_async_admission.py

# 图例 2 km / RID 披露
rg -n "通信规划服务半径|RID 输入半径|4 km" cns_planner/reporting cns_planner/gis cns_planner/application/map_figure_service.py

# 研究服务
D:\tools\anaconda\python.exe tools/serve_r0005_research.py --root <研究运行根>

# QGIS 六图
& 'C:\Program Files\QGIS 3.44.14\bin\python-qgis-ltr.bat' tools/render_r0005_research_figures.py `
  --run-root <研究运行根> --run-id <run_id> --output <输出目录> --sources <map_sources.json>
```

---

## 8. 变更纪律

1. 本文件列出的受保护文件，修改必须带：理由、影响范围、定向测试、是否改指纹/基线。
2. 任何“清理”不得删除本文件列出的文件；`D:\Projects` 整理前先核对 §1–§4。
3. 历史资产（§5）不得因清理误删；但也不得被当作当前能力。
4. 每轮结束更新 §9 决策台账；新增口径必须带日期与出处。

---

## 9. 用户决策台账（清理/交接/新轮次开始前必读）

状态：`active` = 已确认且已落实在产品代码（须指向 `cns_planner/`）；`active-unimplemented` = 已确认但代码未落实；
`research-only` = 只在隔离研究/研究运行路径成立，产品无对应默认；`pending` = 待裁决；`superseded` = 被取代；
`historical-preserved` = 历史编号与语义保留，只作追溯，不覆盖原裁定、不新增授权。

| ID | 日期 | 口径 | 落实位置 | 状态 |
|---|---|---|---|---|
| D-001 | 2026-10-25 | C：陆域每点 2 个不同物理站、海面 1 个 | `domain/cns_service_contract.py` `COMMUNICATION_REDUNDANCY_BY_SURFACE`；`COMMUNICATION_RADIUS_BY_SURFACE=2000`；`application/research_input_service.py` | `active`（合同/输入；R0005 保障结论未证明） |
| D-002 | 2026-10-24 | RID：陆域 2 站、海面 1 站 | `RID_REDUNDANCY_BY_SURFACE`；`RID_RADIUS_BY_SURFACE={land:2000,coastal:2000,sea:5000}`；`template_catalog.py` | `active`（合同/图面；surface 拆分未做） |
| D-003 | 2026-10-24 | ±500 m 是 CNS 覆盖范围；航路只要求中心线不穿塔体及 12 m 净空 | 研究 `route_constraints.py`；产品无 500 m/12 m 硬编码 | `research-only` |
| D-004 | 2026-10-24 | 航路划设 12 m 净空；不通过列航路整改 | 研究 `route_constraints.py`；产品仅 policy 驱动 `vertical_clearance_m` | `research-only` |
| D-005 | 2026-10-24 | 地形 5 m + 下包络 2 m + 垂直裕量 5 m；DEM 3×3 最高值 | 研究 `scenario.json`/`assumptions.json`；产品未冻结该组合 | `research-only` |
| D-006 | 2026-10-24 | 预测建筑高度直接进入计算，标 `assumed` | 研究 `geo.py`；产品无 `predicted_height` 链路 | `research-only` |
| D-007 | 2026-10-25 | 已授权向 Esri 发送 10 处坐标取公开影像 | 研究 `imagery/review.json` | `research-only` |
| D-008 | 2026-10-25 | 新站仅陆域；最少新建第一、成本第二 | 研究 `proposals.py`；产品 P16 为累计增益启发式 | `research-only` |
| D-009 | 2026-10-24 | N 端点职责保留；2 个不同物理站中段热点 | `domain/navigation_middle_reuse.py`、`site_planner/navigation_middle_reuse_v1.py`、`navigation_integrity_contract_v2.py`（`RESEARCH_ONLY`） | `active`（研究路径；显式 v2 opt-in） |
| D-010 | 2026-10-24 | Radar ±45°；不计入 RID/C 冗余 | `algorithms/radar_layout/v1.py` `azimuth_half_width_deg=45.0` | `active` |
| D-011 | 2026-10-25 | 网格 125 m 仅建议；待裁定 | 产品无 125 m 定版；研究 `sensitivity_table.csv` | `pending` |
| D-012 | 2026-10-25 | 清理：`restore` 已删、`A6_0` 归档、离线影像与研究保留 | `CLEANUP_PLAN.md`、`_cleanup_audit/`（仓库外） | `active` |
| D-013 | 历史（早于 2026-10-27） | Radar 楼高假设：雷达原点/塔顶可按塔体与建筑高度（含预测楼高）估算，但必须保留 `engineering_confirmed=false`，不得把 `predicted` 升级为 `confirmed` | 完整裁定引用：`radar_origin_egm2008_m=tower_top_orthometric_m`（archive §8.14 `BUG-RADAR-ORIGIN-003`）；研究记录 `R0005-RESEARCH-RADAR-2026-10-27`（Radar 楼高/LOS/容量研究）；正式线仅 `radar_all_tower_rooftop_estimate_v1`（默认关闭）；Round35-F3 H0–H4 与地形 LOS/容量**未集成、未全部产品化** | `historical-preserved` |
| D-014 | 历史（早于 2026-10-27） | predicted 证据等级：`predicted` 建筑高度可参与估计，但不能独自支撑正式净空 pass；五级证据 `verified/source_tagged/derived/predicted/unknown`，`source_tagged` 不等于现场测量 `verified`，缺失/未解析不等于 pass | 历史裁定：`docs/17` §12.5、`PHASE4_FIXED_LAYER_CONSTRAINT_AUDIT.md` §…；Round35-F3 证据分级（未集成） | `historical-preserved` |
| D-015 | 2026-10-27 | N v2 仅研究专用、显式 opt-in；正式默认仍 v1 10 km/`confirmed=true` | `navigation_integrity_contract_v2.py`、`cns_service_registry.py` | `active`（research-only） |
| D-016 | 2026-10-27 | 研究软件发布 ≠ CNS 保障确认；P18 保持禁止 Confirm/Apply | `plan_review_service.py`、`research_compute_service.py`；P18 `confirmation_allowed=false` | `active` |
| D-017 | 2026-10-27 | 通信规划半径统一 2 km；历史 4 km 无效；半径不是保障结果 | `domain/cns_service_contract.py`、`template_catalog.py`、`map_figure_service.py`、`research_input_service.py` | `active` |
| D-018 | 2026-10-27 | RID 陆/岸 2 km、海 5 km；图面必须披露且不得混淆输入半径与保障结果 | `cns_service_contract.py`、`template_catalog.py`、`render_r0005_research_figures.py` | `active` |

> **编号冲突修订（D2，2026-10-27）**：D1 草案曾把“通信 2 km”和“RID 陆/岸 2 km、海 5 km”
> 写成 D-013/D-014；这两个编号在更早的治理/执行记录中已用于 **Radar 楼高假设** 与
> **predicted 证据等级**。D2 恢复历史编号与语义（`historical-preserved`，只作追溯，不覆盖
> 原裁定），把当前两项改为 **D-017/D-018**。映射：草案 D-013 → D-017；草案 D-014 → D-018。
> 历史原文若后续找回，以其为准；本表只登记编号、语义与映射，不重写历史结论。

> **D-013 研究引用（D2，2026-10-27）**：`R0005-RESEARCH-RADAR-2026-10-27` 是 R0005 的 Radar
> 楼高/LOS/容量研究切片。D-013 只作历史追溯：正式产品仅保留默认关闭的
> `radar_all_tower_rooftop_estimate_v1`，H0–H4 证据分级、地形 LOS/容量、人口/建筑占用等
> 研究结论**未全部产品化**；研究运行结果不得当作正式能力或 CNS 保障结论，也不得写正式
> `ProjectState`。

> 维护规则：`active` 必须指向 `cns_planner/` 生产文件；只有研究证据的用 `research-only`；
> `active-unimplemented` 必须在下一轮转为 `active`/`superseded`；不得把历史研究结论标成 `active`。
