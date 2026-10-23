# PROTECTED_ASSETS.md — 三维系统受保护资产清单

> 本文件由 **R6-C（2026-10-22/23）只读审计**生成，是 Git 可跟踪的保护依据；
> **R7-A（2026-10-23）** 修正了文件计数（14 → **15**）与前端路径前缀
> （`web/js/…` → `cns_planner/web/js/…`），补齐 `router.py` 的完整身份与三维路由段身份，
> 并把"能启动"所需的 Cesium / 运行时依赖写进清单。
> 机器可校验版本：`docs/3d-protected-assets.json`；校验工具：`tools/verify_3d_assets.py`。
> 本轮**没有**合并、覆盖、移动、删除任何三维代码，也没有重启或中断 8776 服务。
> 任何 R8 清理、`git worktree prune`、分支删除、`D:\Projects` 整理之前，**必须先读完本文件**。

---

## 0. 一句话结论

三维系统**不在**主工作区 `D:\Projects\CNS-PLANNER`（分支 `feat/p16-background-task`）里；
它存在于 **7 个内容一致的工作树**（`89e2f64` 系）以及 4 个历史副本中。
这 7 棵树里的三维文件是**同一份有效源码**（其中 3 个前端模块仅行尾不同）；
历史副本是演进证据，**不含**主流版本没有的独有功能。

---

## 1. 在线服务身份（审计期间未中断）

| 项 | 值 |
|---|---|
| 监听 | `127.0.0.1:8776` |
| PID | `69112`（审计时；启动于 2026-10-22 23:08:56） |
| 解释器 | `C:\Program Files\QGIS 3.44.14\bin\python.exe`（`python-qgis-ltr.bat`） |
| 命令 | `python -m cns_planner.map_server` |
| project_root | `D:\Projects\_cns_workspaces\integration\round38_acceptance\CNS-PLANNER` |
| 工作树 HEAD | `9404d16`（`integration/round38-acceptance`） |
| 审计方式 | 仅 `GET /api/health`、`/api/visualization-3d/manifest`、`/api/visualization-3d/terrain/layer.json`、`/api/visualization-3d/imagery/config` |

**不要**为了"整理"而重启它：它服务的三维页面与二维工作台是同一个进程。

---

## 2. 受保护源码（正式跟踪版本）

主清单来自 `89e2f64` 系工作树，与 8776 正在运行的树内容一致。

**文件计数（R7-A 更正）**：受保护源码是 **15 个路径** = 后端 4 + 前端 11。
R6-C 的旧文案写"14 个"，但表格实际列了 15 行（`router.py` 也占一个路径），
两者矛盾；R7-A 以 **15** 为准，并用 `docs/3d-protected-assets.json` 逐文件记录
字节数与 SHA-256，避免再次靠人眼数行。

**路径前缀（R7-A 更正）**：R6-C 前端表里写作 `web/js/visualization3d/app.js`，
在仓库中**不存在**该路径；真实位置是 `cns_planner/web/js/visualization3d/app.js`
（`web/` 是 `cns_planner/web/` 的省略写法）。下表已改为完整的仓库相对路径。

**`router.py` 的身份分两层**（R7-A 更正）：

| 层次 | 身份 | 说明 |
|---|---|---|
| 全文件 | 随所属树而变：`89e2f64` 干净树 **91 536 B / `432453cf54ff16d2…`**；R6-C V2 集成树 **92 274 B / `c69666cab0f93670…`**；8776 运行树（`9404d16`）**91 370 B / `812103486a31174e…`** | V2 集成树多出的是 v2 提交分支，**不是**三维改动 |
| 三维路由段 | **17 638 B / 311 行 / `8e9eb40b43b5ab372ed6fe618146ae6b0149cee8761b39e13dba55c2cde2bfc9`**，三棵树**逐字节相同** | 从 `VISUALIZATION_3D_ACTIONS = {` 到 `def _visualization_3d(` 之后的下一个 `\n    def ` 之前 |

因此校验 `router.py` 时**必须以三维路由段为主判据**：全文件 SHA 不同不代表三维被改，
三维段 SHA 不同才是真的被改。

### 2.1 后端

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `cns_planner/application/visualization_3d_service.py` | 149 792 | `79c5a150e19208fb360477544bb0ff00d72f2181d243eb25fa9c34621c6f7a39` |
| `cns_planner/application/visualization_3d_imagery_service.py` | 46 596 | `e391be8d33e13c4caabec95f85ce0c45589a93fb1426d70e95a8d88ba2a82432` |
| `cns_planner/gis/terrain_tile_adapter.py` | 18 266 | `db4e6707713067eac37a4b5ef9dd661b332dcad1df239870d93586ab179f7374` |
| `cns_planner/api/router.py`（三维白名单与代理） | 91 536（干净树）/ 92 274（V2 集成树）/ 91 370（8776 树） | 全文件见 §2 的表；**三维段** `8e9eb40b43b5ab37…`（三树一致） |

### 2.2 前端

| 文件 | 字节 | SHA-256（`89e2f64` 系 / 6 树一致） |
|---|---:|---|
| `cns_planner/web/visualization-3d.html` | 14 792 | `1a0431d6c91a36a79e1abd0f4e6efb7f01ec8373f8c20ccbd403cbc5074ebef8` |
| `cns_planner/web/tiles.js` | 7 012 | `80240f95ecf7d7759f573dd0bab3d11cc3642136ae2c80cf15a9745ffafff0ec` |
| `cns_planner/web/js/visualization3d/app.js` | 187 326 | `8b4529728ca1ec82dda323901d5a176e5967afa51325fe0582590839471243e6` |
| `cns_planner/web/js/visualization3d/basemap.js` | 44 022 | `76d988a1b644dc4f581f96d941d735886d3be6687929338f562ab8fcd983377e` |
| `cns_planner/web/js/visualization3d/camera_presets.js` | 1 185 | `b5e668ffaf93c46a585c4cd40d6f8f19abb6242ad85cab3cffea127421f63d4b` |
| `cns_planner/web/js/visualization3d/camera_state.js` | 1 077 | `dc1c7b10a770a84a241ca3595d0eb13e0160303c3d516d36573d82969524da7f` |
| `cns_planner/web/js/visualization3d/cns_sites.js` | 26 256 | `3b2cd825803c75b6334d6d85f92d88a0df558a337efc1ba7fbb993286a42deff` |
| `cns_planner/web/js/visualization3d/fixed_corridor_band.js` | 11 578 | `9ac65af2beba69373dd8a0a186732a8c8a636c2e117e28e04fe6829ff3dd582a` |
| `cns_planner/web/js/visualization3d/intrusion_playback.js` | 22 508 | `3d4bcdfe700a60ed95c9430c8dc3b968afb5852a4382c0d79c174a94e0bfdb2e` |
| `cns_planner/web/js/visualization3d/protection_volume.js` | 11 332 | `794197c988706758c3a2d3fd319b7baeb7440f7cf03c630fb7d048cad6591dfd` |
| `cns_planner/web/js/visualization3d/radar_volume.js` | 15 129 | `96437ab1373eeefef62a91aaaaa99733d78eea13fbdf941f46a3e8de607afb26` |

> **行尾差异（不是内容差异）**：8776 正在运行的 `round38_acceptance` 树里，
> `cns_sites.js` / `fixed_corridor_band.js` / `intrusion_playback.js` 是 **LF**，
> SHA 分别为 `550794d3…` / `842548a3…` / `1f627f6f…`；
> 其余 6 棵树是 **CRLF**，行内容逐行相同（字节差 = 行数）。
> 二者**不是**两份实现，不要因为 SHA 不同就当成"另一个版本"。

### 2.3 三维功能清单（manifest 的 13 个图层）

`terrain_fabdem`、`route_geometry_demo`、`route_geometry_canonical`、`towers`、
`cns_devices`、`cns_facilities`、`communication_volumes`、`rid_footprints`、
`radar_volumes`、`protection_volumes`、`cns_site_identity`、`fixed_corridor_band`、
`simulated_intrusion`。

### 2.4 恢复必需、但不属于 15 个受保护源码的依赖（R7-A 补齐）

恢复**不是**只复制 §2.1/§2.2 的 15 个文件。页面还直接引用 Cesium，服务还依赖
静态挂载与项目数据；缺任何一项都会"文件都在但页面打不开"：

| 依赖 | 位置 | 身份/说明 |
|---|---|---|
| Cesium 运行时（8 个 git 跟踪文件，5.7 MB） | `cns_planner/web/vendor/cesium/` | `Cesium.js` 5 159 916 B / `e4dc17055dbad7d4…`；`Widgets/widgets.css` 32 052 B / `dd409f4c9dbc0d6c…`；另有 `Assets/approximateTerrainHeights.json`、`Assets/Images/ion-credit.png`、`Assets/IAU2006_XYS/*_0.json`、`*_18.json`、`LICENSE.md`、`LICENSE-PROVENANCE.md`（逐文件 SHA 见 JSON 清单） |
| HTTP 静态挂载与路由 | `cns_planner/map_server.py`（`STATIC = ROOT/cns_planner/web`）、`cns_planner/api/server.py` | 非三维专属；恢复树必须与三维源码同一棵树，否则页面与 API 版本错配 |
| 项目状态 | `projects/current_project.json`（或 `POST /api/project/open` 打开的项目目录） | 三维层只读消费，不重算业务结果 |
| 地形 / 影像 | FABDEM `Zhoushan_FABDEM_DTM_30m.tif`（65 656 187 B / `21406bd901fbebae…`）、EGM2008 网格、`land_mask` gpkg | 以目标树 `projects/data_sources.json` 为准；缺失时 manifest 报 `egm2008_grid_missing` 等 reason_code，**不要用 0 代替** |

机器清单：`docs/3d-protected-assets.json` 的 `vendor_files` / `runtime_files` /
`data_requirements` 三段；校验入口见 §9。

---

## 3. 工作树 / 分支 / HEAD / dirty 矩阵

| 工作树绝对路径 | 分支 | HEAD | dirty | 三维页面 |
|---|---|---|---:|---|
| `D:\Projects\_cns_workspaces\integration\round38_acceptance\CNS-PLANNER` | `integration/round38-acceptance` | `9404d16` | 32 | tracked（**8776 运行中**） |
| `C:\Users\yiding\.codex\worktrees\r6b-p16-v2\CNS-PLANNER` | detached | `89e2f64` | 9 | tracked（**R6-B P16-V2 成果**） |
| `C:\Users\yiding\.codex\worktrees\cns-r0-review\CNS-PLANNER` | `snapshot/2026-10-22-review` | `4938736` | 0 | tracked |
| `D:\Projects\_cns_workspaces\dev\r5g` | `feat/r5g-landmask-legacy` | `89e2f64` | 0 | tracked |
| `D:\Projects\_cns_workspaces\dev\r6_p16` | `feat/r6-p16-assumption-pool` | `89e2f64` | 4 | tracked |
| `D:\Projects\_cns_workspaces\dev\round4b` | detached | `4938736` | 12 | tracked |
| `D:\Projects\_cns_workspaces\dev\round5_r5` | `feat/r5-engineering-assumption` | `cf62afe` | 0 | tracked |
| `D:\Projects\CNS-PLANNER` | `feat/p16-background-task` | `68e2674` | 156 | **无三维页面** |
| `D:\Projects\_cns_workspaces\checkpoint\r7a-3d-protected\CNS-PLANNER` | `checkpoint/r7a-3d-protected` | `89e2f64` + R7-A 文档提交 | 0 | tracked（**R7-A 独立检查点：15 文件 + Cesium + 清单 + 校验器**） |

另有历史副本（同一仓库的分支/工作树）：

| 位置 | HEAD | 三维版本 | 判定 |
|---|---|---|---|
| `D:\Projects\_cns_workspaces\dev\round37_b0\CNS-PLANNER` | `7bd86d2` | service 108 771 / html 11 729 | 中间态；无独有函数 |
| `D:\Projects\_cns_workspaces\dev\round38_n1_navigation\CNS-PLANNER` | `7bd86d2` | service 137 434 / html 9 917 | 中间态；无独有函数 |
| `D:\Projects\_cns_workspaces\test\round38_int_a\CNS-PLANNER` | `9404d16` | service 143 965 / html 13 271 | 中间态；无独有函数 |
| `D:\Projects\CNS-PLANNER\.codex_stage\work` | 未跟踪 | service 57 813 / html 5 228 | 2026-10-12 原型（无 cns_sites / radar / protection） |
| `D:\Projects\_cns_workspaces\deliverables\round37_t3d_1\patch\new_files` | 未跟踪 | service 54 976 / html 5 020 | 已应用补丁的原始副本 |
| `D:\Projects\_cns_workspaces\deliverables\round38_n1_navigation\g0\source_snapshot` | 未跟踪 | service 108 771 / html 11 729 | 验收快照 |

### 3.1 唯一性判定方法

逐一比较六个版本 `visualization_3d_service.py` 的顶层 `def` / `class` 集合：

- 主流（149 792 B）：**24** 个；
- `round38_int_a` / `round38_n1`：22 个；
- `round37_b0`：21 个；
- `.codex_stage` / `deliverables`：17 个；
- **没有任何版本存在主流之外的函数**。

前端模块同理：主流包含全部 9 个模块（`cns_sites`、`fixed_corridor_band`、
`intrusion_playback`、`protection_volume`、`radar_volume` 等），旧版按阶段缺失。

**结论**：有效且唯一的三维源码是 `89e2f64` 系的 7 个工作树；其余副本是可追溯证据，
不是"唯一有效源码"，但**在没有把主流版本固化到受保护位置之前，仍不得删除**。

---

## 4. 数据与可再生成资产

| 资产 | 路径 | 身份 |
|---|---|---|
| FABDEM 地形 | `D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed\Zhoushan_FABDEM_DTM_30m.tif` | 65 656 187 B，SHA-256 `21406bd901fbebae211528a158ca95eef899e2074322625717b215b13d8799b8`，7200×10800 |
| 影像 | `arcgis_world_imagery`（代理）/ 离线影像源 | manifest：`status=offline_mode`、`reason_code=imagery_online_mode_disabled`、`credential_exposed=false` |
| 陆海掩膜 | `D:\Projects\CNS-PLANNER\projects\derived\land_mask_osm\zhoushan_land_osm_v2.gpkg` | 由 `data/registry.py` 登记 |
| 项目状态（8776） | `...\round38_acceptance\CNS-PLANNER\projects\current_project.json` | 288 062 638 B，revision 471 |

**可再生成（可删）**：

- 地形瓦片 `/api/visualization-3d/terrain/{z}/{x}/{y}.terrain` 与晕渲 `relief` 派生；
  同一 z/x/y + 同一 FABDEM/EGM2008 输入 ⇒ 同字节，删除后按需重算；
- 影像瓦片代理缓存（受 `CACHE_TERRAIN` 私有缓存策略约束）；
- `.codex_stage/diagnose_cesium_options.py`、`diagnose_t3d_render.py` 只是诊断脚本，
  不是产品源码；`.codex_stage/*.md` 是设计/计划文档，保留可追溯性。

---

## 5. API 契约（只读白名单）

| 路径 | action | 说明 |
|---|---|---|
| `/api/visualization-3d/manifest` | `manifest` | 图层清单 + 地形/影像身份 |
| `/api/visualization-3d/routes` | `routes` | 三维航路 |
| `/api/visualization-3d/terrain-window` | `terrain-window` | 地形窗口 |
| `/api/visualization-3d/communication-volumes` | `communication-volumes` | 通信三维体积 |
| `/api/visualization-3d/rid-footprints` | `rid-footprints` | RID 陆海非正圆覆盖 |
| `/api/visualization-3d/radar-volumes` | `radar-volumes` | Radar 方向性覆盖 |
| `/api/visualization-3d/protection-volumes` | `protection-volumes` | 保护区/隔离工程示意 |
| `/api/visualization-3d/cns-sites` | `cns-sites` | CNS 站址四类身份 |
| `/api/visualization-3d/fixed-corridor-bands` | `fixed-corridor-bands` | 固定 1000 m 保障目标带 |
| `/api/visualization-3d/intrusion-assessment` | `intrusion-assessment` | 模拟轨迹事件判定 |
| `/api/visualization-3d/terrain/layer.json` | 特殊分支 | TileJSON（heightmap-1.0，zmax=13） |
| `/api/visualization-3d/imagery/config` | 特殊分支 | 影像来源与授权状态 |
| `/api/visualization-3d/imagery/{source}/{z}/{x}/{y}` 等 | 特殊分支 | 影像瓦片代理（只读） |

其它 `/api/visualization-3d/*` 一律 404（白名单，不落静态文件解析）。

---

## 6. 运行入口

- 前端页面：`/visualization-3d.html`（`cns_planner/web/visualization-3d.html`）；
- 服务：`python -m cns_planner.map_server`，默认 `127.0.0.1:8765`，
  可用 `CNS_MAP_PORT` 起独立实例（本项目验收用 8776/8777）；
- 依赖：QGIS 3.44.14 自带 Python、GDAL、EGM2008 网格、FABDEM DTM、
  Cesium 运行于浏览器端（无构建步骤）。

---

## 7. R8 清理前置条件（缺一不可）

1. `89e2f64` 系的三维文件已被固化到一个**不会被 prune** 的位置
   （例如合并到 `main`，或复制到 `_cns_workspaces/protected/3d/` 并记录 SHA）；
2. 本文 §2 的 **15 个文件**（以 `docs/3d-protected-assets.json` 为准）与固化位置逐字节一致，
   且 `router.py` 的三维路由段身份一致（R7-A 更正：旧文案"14 个"是错的）；
3. 8776（或替代端口）的三维服务能重新启动，且下面 4 个只读请求返回 200：
   `manifest`、`terrain/layer.json`、`imagery/config`、`routes`；
4. 13 个 manifest 图层全部可见（或明确记录"因数据源缺失而 blocked"）；
5. 三维页面能加载并渲染地形 + 影像（离线模式）+ 航路 + 铁塔 + CNS 设施 + RID + 通信 + Radar + 保护区；
6. `.codex_stage` 的设计/诊断文档已归档（可读，不要求运行）；
7. 所有历史副本的 HEAD 与分支名已在本文 §3 登记（删除后仍可追溯）；
8. 清理前重新生成本文件（或本文件的校验段），确认没有新增未登记的三维文件。

---

## 8. 本轮明确不做

- 不合并、不覆盖、不 cherry-pick 三维代码；
- 不删除、不移动任何工作树或分支；
- 不重启、不停机 8776；
- 不清理 `D:\Projects`；
- 不把三维结果接入 P16-V2 前端（另列后续任务）。

---

## 9. 快速自检命令（只读）

R7-A 起有一条统一入口（只读、仅标准库、不启动/不停止任何服务）：

```powershell
# 0) 15 个文件 + router 三维段 + Cesium 依赖 + 页面资源图（不通服务也能跑）
D:\tools\anaconda\python.exe tools/verify_3d_assets.py --root <工作树>
# 服务已起时追加只读 HTTP 验收（health / manifest / layer.json / imagery / routes / 页面与静态资源）
D:\tools\anaconda\python.exe tools/verify_3d_assets.py --root <工作树> --probe http://127.0.0.1:8778
```

```powershell
# 1) 在线服务身份
Invoke-RestMethod http://127.0.0.1:8776/api/health

# 2) 三维页面与后端是否存在
Test-Path 'D:\Projects\_cns_workspaces\integration\round38_acceptance\CNS-PLANNER\cns_planner\web\visualization-3d.html'

# 3) 文件身份（任选其一）
Get-FileHash '<工作树>\cns_planner\application\visualization_3d_service.py' -Algorithm SHA256
```
