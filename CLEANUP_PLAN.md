# CLEANUP_PLAN.md — D:\Projects 175G 只读盘点与分级清理清单

> 生成时间：2026-10-25（Asia/Shanghai）
> 口径：全量只读扫描 `D:\Projects` 共 **349 942 个文件 / 192.38 GB**（= 179.2 GiB，与你说的 "175G" 同量级）。
> 本次**没有删除、移动、压缩、改写任何被盘点的文件**，没有 commit，没有改产品代码。
> 唯一写入：`CLEANUP_PLAN.md` 与本文件同目录的 `_cleanup_audit/`（扫描脚本 + 原始聚合结果，可整体删除）。
> 证据口径：只比较文件路径、大小、扩展名、git 引用可达性。**没有解析 git objects 内部内容**（只列 `objects/pack` 的文件名与大小用于重复判定）。
> 二级原始明细：`_cleanup_audit/out_a.txt`（各目录一二级子目录 + top10）、`_cleanup_audit/out_b.txt`（扩展名/bundle/manifest/重复组）、`_cleanup_audit/out_c.txt`（15 个受保护仓库与 bundle 结果）。

---

## 0. 先说三件影响全盘结论的事

**① 你想引用的 `DECISIONS.md` / `INDEX.md` 在仓库里不存在。**
在 `D:\Projects\CNS-PLANNER` 全量 md 里检索 `DECISIONS.md|INDEX.md|docs/runs` → **0 命中**。实际承担"决策/索引"职责的是：

- `docs/decisions/2026-09-ai-dev-context-full-archive.md`（269 KB，唯一 decisions 目录内容）
- `AI_DEV_CONTEXT.md`（文档地图与权威写入者）、`docs/project_audit/01_STATUS_MAP.md`、`docs/project_audit/05_EVIDENCE.md`
- `_cns_workspaces/CONTROL.md`（101 KB，双 AI 接力控制台）+ `_cns_workspaces/BASELINE.json`（141 KB）
- `PROTECTED_ASSETS.md`（16 KB，**三维受保护资产清单 + §7 清理八条前置条件**）

所以下面凡说"被文档引用"，指的就是这批文件。`docs/runs` 同样不存在（`docs/` 下只有 `decisions/`、`project_audit/`、`superpowers/` 三个子目录）。

**② 8776 验收服务当前没在跑。** `Get-NetTCPConnection -LocalPort 8776` 无监听，`python` 进程数 0。`PROTECTED_ASSETS.md` §1 记录的"8776 运行中、不可重启"已过期；但 `round38_acceptance` 树仍是最后一条集成分支（`9404d16`），**不能按"进程已停"就当废目录**。

**③ 输入源不在 `D:\Projects` 内。** `projects/map_sources.json` 里 DEM / 人口 / 建筑 / 塔清单 / 起降点 / 真实航路全部指向 `D:\aaa2026project\UOM\...`（已确认该目录存在），`land_mask` 指向 `CNS-PLANNER\projects\derived\land_mask_osm\`。你记的 `_cns_sources` 顶层目录**不存在**；`D:\Projects` 下的"输入源"实际是 `_cns_round35_*` 的 `downloads/`、`sources/`、`_work/` 三个下载副本。

---

## 1. 第一步：占用地图

### 1.1 总览（第一层，按大小）

| 目录 | 大小 | 文件数 | 占五大目录比例 |
|---|---:|---:|---:|
| `_cns_backups` | **91.52 GB** | 153 695 | 51.1% |
| `CNS-PLANNER` | **35.07 GB** | 57 478 | 19.6% |
| `_cns_workspaces` | **27.76 GB** | 71 602 | 15.5% |
| `_cns_acceptance` | **12.93 GB** | 10 163 | 7.2% |
| `_cns_round38_global_recovery` | **11.86 GB** | 36 185 | 6.6% |
| 五大合计 | **179.14 GB** | 329 123 | 100% |
| 其余 47 个 `_cns_r32*/_cns_round3x*` 等 | **13.24 GB** | 20 819 | — |
| **D:\Projects 总计** | **192.38 GB** | 349 942 | — |

磁盘：`D:` 总 653 GiB / 已用 411.7 GiB / 剩余 241.2 GiB。

`_cns_backups` 之外最大的散目录：`_cns_r32j0_perf` 2.17 GB、`_cns_round35_s5_acceptance` 1.36 GB、`_cns_r32i_savecheck` 1.21 GB、`_cns_r32g` 1.17 GB、`_cns_r32e` 1.10 GB、`_cns_round35_e_buildings` 1.02 GB、`_cns_tmp` 0.91 GB。

### 1.2 `_cns_backups` — 91.52 GB

二级子目录：

| 二级 | 大小 | 文件数 | 内容类型 |
|---|---:|---:|---|
| `p0b1s_20261018_155628\restore` | **45.54 GB** | 76 140 | 恢复演练产物（解压副本 + inert git 元数据 + mirror 裸仓库） |
| `p0b1s_20261018_155628\snapshots` | **45.29 GB** | 75 572 | 备份本体（15 个受保护仓库的工作树 + .git 快照） |
| `p0b1s_20261018_155628\continuation_162014` | 99.3 MB | 44 | 第二次续做的 manifest / checksum |
| `p0b1s_20261018_155628\continuation_161537` | 98.0 MB | 66 | 第一次续做的 manifest / checksum |
| `p0b1s_20261018_155628\bundles` | 55.4 MB | 7 | git bundle（`shared` / `A7_0` / `A7_1` / `A7_HIST_0..2`） |
| `p0b1s_20261018_155628\file_inventory.csv` | 56.7 MB | 1 | 全量文件清单 |
| `p0b1s_20261018_155628\BACKUP_COMPLETE_CHECKSUMS.sha256` | 20.2 MB | 1 | 全量 SHA256 封条 |
| `p0b1s_20261018_155628\03_backup_checksums.sha256` | 11.5 MB | 1 | 分段校验清单 |
| `p0b1s_20261018_155628\logs` / `tail` / `reparse_metadata` | ~0.6 MB | 50 | 命令日志、收尾报告、重解析点清单 |
| 其余 18 个 `r7d1x*` 目录/文件 | **0.36 GB** | ~1 500 | 老轮次合并残留（`.bundle`、`.ours/.theirs/.base` 冲突副本、测试 json） |

`p0b1s` 的顶层还有 `capacity.json`（source_files 75 572 / source_bytes 48.6 GB）、`resume_evidence.json`（**从 `p0b1s_20261018_155146` 续做**，reused 75 571 条记录）、`restore_results.json`、`round37_project_state_inventory.json`。

`snapshots` 二级（这是"备份本体"的真实结构）：

| 二级 | 大小 | 说明 |
|---|---:|---|
| `A6_0` | 25.28 GB | `D:\Projects\CNS-PLANNER` 工作区（不含 .git）的 2026-10-18 快照 |
| `A1` | 5.15 GB | `_cns_workspaces\integration\round38_acceptance\CNS-PLANNER` 快照（含 `.acceptance_setup` 1.79 GB、`projects` 3.28 GB） |
| `A8` | 2.91 GB | `_cns_round38_global_recovery\local_recovery_20261018_100555` 整包副本 |
| `A6_0_git_0` | 1.86 GB | CNS-PLANNER 真 `.git`（origin=github.com/guosending565-ui/CNS-PLANNER） |
| `A6_0_git_1` | 1.87 GB | worktree gitdir（origin=`D:\Projects\CNS-PLANNER`） |
| `A6_0_git_2` | 1.87 GB | 同上 |
| `A6_0_git_3` | 1.87 GB | 同上 |
| `A7_1_git_0` | 1.87 GB | `checkpoint/phase36-pre3d` 仓库 .git |
| `A7_1` | 0.67 GB | 该检查点工作树 |
| `A6-EXT-ROUND37-PROJECTS` | 0.65 GB | 外置 projects 快照 |
| `A5` | 0.63 GB | 旧工作树 |
| `A6_5` | 0.42 GB | `dev\round38_n1_navigation` 树 |
| 其余 `A6_1..6` / `A7_0/2/3` / `A7_HIST_0..2` | < 60 MB | 小仓库与历史快照 |

`snapshots` 前 10 大文件（全部是 git pack，**同名同大小，跨目录重复**）：

| 路径（相对 `_cns_backups\p0b1s_20261018_155628`） | 大小 | 类型 |
|---|---:|---|
| `snapshots\A6_0_git_0\objects\pack\pack-5049135a….pack` | 1.77 GB | git pack |
| `snapshots\A6_0_git_1\objects\pack\pack-c5de039e….pack` | 1.75 GB | git pack |
| `snapshots\A6_0_git_2\objects\pack\pack-c5de039e….pack` | 1.75 GB | git pack |
| `snapshots\A6_0_git_3\objects\pack\pack-c5de039e….pack` | 1.75 GB | git pack |
| `snapshots\A7_1_git_0\objects\pack\pack-c5de039e….pack` | 1.75 GB | git pack |
| `snapshots\A6_0\_work\landmask_osm\…`（+`_work` 下 3 个 `.git` 各 1.75 GB） | 1.75 GB×3 | git pack |
| `snapshots\A8\local_recovery…\02_GIT\CNS-PLANNER-full.bundle` | 18.2 MB | git bundle |
| `snapshots\A1\.acceptance_setup\round38_real\…json` | 275 MB | 项目状态 |
| `snapshots\A6_0\outputs\…` / `_work\round29l_live\…json` | 250–300 MB | 运行时响应 |

`restore` 前 10 大文件：与 `snapshots` 同构（`restore\inert_metadata\A6_0_git_0..3`、`restore\mirror_A7_0.git`、`restore\A1\…`），同样是 pack 与大 json。

### 1.3 `CNS-PLANNER` — 35.07 GB

| 二级 | 大小 | 文件数 | 内容类型 |
|---|---:|---:|---|
| `_work` | **16.94 GB** | 9 305 | 临时工作副本（含 3 个完整 git 仓库各 1.94 GB） |
| `_diag` | 3.07 GB | 16 094 | 诊断产物：`_edge_profile*` 五个浏览器 profile 2.2 GB + 截图 |
| `_vbase_r25` | 2.74 GB | 13 343 | 旧基线树（`_diag` 2.5 GB + `_runtime_projects` 0.22 GB） |
| `outputs` | 2.40 GB | 4 375 | 运行输出（`pytest_tmp*`/`sys_tmp` 缓存为主） |
| `.git` | 2.25 GB | 818 | 主仓库（活跃） |
| `_runtime_projects` | 2.23 GB | 435 | 运行时项目副本（3 个 250–900 MB 的 state 副本） |
| `_output` | 1.30 GB | 1 537 | 旧输出（`round28_c_residual` 0.45、`reports` 0.27） |
| `_tmproot` | 1.23 GB | 145 | 临时项目根（`round261_*`） |
| `audit_sandboxes` | 1.09 GB | 2 126 | **2026-10-24 r0005 三阶段审计沙盒**（最新证据） |
| `research` | 0.82 GB | 538 | 研究产物（`r7d10` 310 MB、`m4-2c-b` 261 MB、`r7d8` 199 MB） |
| `_migration_smoke` | 0.50 GB | 30 | 迁移冒烟副本 |
| `.test_runtime` | 0.29 GB | 2 079 | pytest 运行时依赖 |
| 其余（`docs`/`cns_planner`/`tests`/`projects`/`tools`/`.codex_stage` 等） | **~0.16 GB** | ~5 000 | 产品源码与文档（`projects` 35.6 MB、`docs` 0.9 MB、`cns_planner` 19.5 MB、`.codex_stage` 0.27 MB） |

前 10 大文件：

| 路径 | 大小 | 类型 |
|---|---:|---|
| `.git\objects\pack\pack-cb237257….pack` | 1.82 GB | git pack（活跃仓库） |
| `_work\round34_t07d2b_building_los_snapshot\.git\…pack-c5de039e….pack` | 1.75 GB | git pack |
| `_work\round34_t07d2c0_combined_los\.git\…pack-c5de039e….pack` | 1.75 GB | git pack |
| `_work\round34_t07d2c1_los_closeout\.git\…pack-c5de039e….pack` | 1.75 GB | git pack |
| `_work\landmask_osm\extract\land-polygons-split-4326\land_polygons.shp` | 1.23 GB | 输入源（OSM 陆地多边形，可重下） |
| `_work\landmask_osm\land-polygons-split-4326.zip` | 884.9 MB | 压缩包（同源，可重下） |
| `_work\round29q\p19_preview_response.json` | 796.9 MB | 运行时响应 |
| `_work\round29p\live\p19_preview_response.json` | 796.9 MB | 同名同大小重复 |
| `outputs\sys_tmp\dsh_pytest_tmp\…\1089a00e….staged` | 298.0 MB | 临时产物 |
| `_work\pytest_tmp\…\1089a00e….staged` | 298.0 MB | 临时产物（同内容 3 份） |

### 1.4 `_cns_workspaces` — 27.76 GB

| 二级 | 大小 | 文件数 | 内容类型 |
|---|---:|---:|---|
| `integration` | 10.76 GB | 7 204 | **`round38_acceptance` 9.99 GB（8776 运行树 / 最后集成分支）** + `r6c_p16_v2_ui` 0.75 GB |
| `acceptance` | 6.94 GB | 9 258 | 验收快照：`round37_a_2d_preacceptance` 3.22、`round36_d1_zhoushan_demo` 1.04、`round36_zhoushan_demo` 1.04、`round37_b0` 0.69、`round34_t07_i2_h3` 0.31 |
| `dev` | 4.06 GB | 18 467 | 开发工作树：`r7b_pool` 0.94、`round37_b0` 0.71、`r7d4` 0.67、`r7b_3d` 0.58、`r7d3b` 0.36、`round5_r5` 0.30… |
| `derived` | 2.98 GB | 29 454 | **`round38_offline_imagery` 2.92 GB / 29 451 瓦片**（离线影像源） |
| `deliverables` | 1.34 GB | 1 359 | 交付：`round38_site1_crs` 0.83、`r7d1` 0.26 |
| `test` | 0.89 GB | 1 487 | `round38_int_a` 测试树 |
| `test_tmp` | 0.65 GB | 412 | `r38n2` 测试临时 |
| `checkpoint` | 0.13 GB | 3 637 | **`r7a-3d-protected` 0.045 GB（三维受保护检查点）** |
| `tmp` / `scripts` / `review` / `audit` | < 50 MB | ~300 | 协作脚本与审查记录 |
| `CONTROL.md` / `BASELINE.json` 及 9 个 `.bak` | ~1.2 MB | 19 | **协作状态权威文件** |

前 10 大文件：

| 路径 | 大小 | 类型 |
|---|---:|---|
| `acceptance\round37_a_2d_preacceptance\CNS-PLANNER\.git\…pack-c5de039e….pack` | 1.75 GB | git pack（与主仓同 pack） |
| `integration\round38_acceptance\CNS-PLANNER\projects\round38_cns_experiment_b\…json` | 298.0 MB | 实验项目状态 |
| `dev\r7d4\research\r7d6\project_state.json` | 285.2 MB | 项目状态副本 |
| `dev\r7d4\research\r7d5\recovered\project_state.json` | 285.2 MB | 同上（另一份） |
| `acceptance\round37_a_2d_preacceptance\logs\browser_DH\export_project.bin` | 281.1 MB | 浏览器导出 |
| `integration\round38_acceptance\...\round38_real_acceptance\current_project.json` | 275.3 MB | 项目状态 |
| 同目录 `current_project.json.bak` | 275.3 MB | 备份 |
| `.acceptance_setup\round38_real\recovery\…json` | 275.2 MB | 恢复副本 |
| `.acceptance_setup\round38_real\recovery\…json` | 274.7 MB | 恢复副本 |
| `acceptance\round37_b0\CNS-PLANNER\projects\current_project.json` | 274.7 MB | 项目状态 |

### 1.5 `_cns_acceptance` — 12.93 GB

| 二级（轮次） | 大小 | 文件数 | 内容类型 |
|---|---:|---:|---|
| `r7d22` | 3.00 GB | 2 712 | A/B 对照实验（`repo` 2.16 = 工作树 0.34 + `.git` 1.82） |
| `r7d25` | 2.54 GB | 1 395 | 覆盖优化（`repo` 2.16 同上 + `optimization.json` 232 MB） |
| `r7d23` | 2.19 GB | 1 390 | 准入分析（`repo` 2.16 同上） |
| `r7d17` | 1.36 GB | 336 | 早期 P14/P15 探针（`source` 745 MB、`R0005-rev524` 593 MB） |
| `r7d18` | 1.06 GB | 101 | API 状态快照（`R0005-rev524` 875 MB，含 488.8 MB `project_state.json`×2） |
| `r7d20` | 1.01 GB | 97 | 2 km 能力（`historical_before_2km.json` 307 MB） |
| `r7d26` | 0.63 GB | 21 | 候选站址（`after_p14` 203 MB、`full62_p14` 188 MB） |
| `r7d35` | 0.57 GB | 1 070 | **D35 最新结论轮**（`project` 0.36 + `cartography` 425 文件） |
| `r7d31` | 0.22 GB | 1 586 | 站址扩展诊断（`p14_full_diagnostic.json` 88 MB） |
| `r7d27`/`r7d33`/`r7d21`/`r7d32`/`r7d34`/`r7d24`/`r7d29`/`r7d28`/`r7d19` | 0.32 GB | ~2 500 | 其余轮次（`r7d33` 含 45 MB `computed_100m_matrix.npz`；`r7d19` 仅 1 个 `final_release_audit.json`） |

前 10 大文件：3 份 1.82 GB `pack-cb237257….pack`（`r7d22/23/25\repo\.git`，**与 `CNS-PLANNER\.git` 同一 pack**）、2×488.8 MB `project_state.json`（`r7d17\R0005-rev524` 与 `r7d17\source`）、488.8 MB `project_state.json.bak`（`r7d18`）、306.6 MB `historical_before_2km.json`（`r7d20`）、275 MB 级 `project_state.json`×3。

### 1.6 `_cns_round38_global_recovery` — 11.86 GB

| 二级 | 大小 | 文件数 | 内容类型 |
|---|---:|---:|---|
| `rounds` | **7.48 GB** | 1 559 | R01–R30B 历史轮次产物（每轮 `output/`、`backup/`、`delivery/`） |
| `local_recovery_20261018_100555` | **2.85 GB** | 1 376 | 本地恢复整包（`03_STATE` 2.10 GB、`05_ACCEPTANCE` 0.62 GB、`00_MANIFEST` 含 `MANIFEST.csv`/`CHECKSUMS.sha256`） |
| `p2a4a_c_only_smoke_20261018` | 0.61 GB | 2 826 | C-only 冒烟（A/B 各 0.29 GB） |
| `p0b2v_dependency_closeout_20261018` | 134 MB | 5 624 | 依赖收尾（`prefixes` 5 473 文件） |
| `p0b2u_p14_contract_20261018` | 113 MB | 4 606 | P14 契约 |
| `p2a4d5_vm_synthetic` | 92 MB | 240 | VM 介质（两个 `.iso` 27 MB + 16 MB） |
| `p2a4b_real_c_execution_addendum` / `p2a4c1_runtime_isolation` / `p0b2w_final_closeout` | 0.2 GB | 6 700 | 执行与隔离证据 |
| `p0b2r_git_resume` / `p0b2_git_closeout` / `p2a4d7c_worker_broker` | 0.19 GB | 7 900 | git 收尾与 broker |
| `asset_delivery_audit` | 40 MB | 76 | 资产交付审计（`17_PRIORITY_BACKUP_MANIFEST.csv`） |
| `handoffs` / `coordination` / `requirements` | ~2.2 MB | 178 | 交接与协调（`HANDOFF.md/json`、`.sha256`） |
| 根级 `01..07_*.md` + `file_index.tsv` + `asset_index.json` + `data_assets.tsv` | ~7.5 MB | 12 | 索引与报告 |

前 10 大文件：`rounds\R20\output\study_report_preview.html` 531.6 MB、`rounds\R23\delivery\attachments\section_11_p18.json` 400.4 MB、`local_recovery…\03_STATE\rev494\current_project.json` 275.3 MB、`03_STATE\recovery\rev491_*` 275.2 MB、`03_STATE\recovery\rev482_*` 274.7 MB、`rounds\R05..R10\backup\current_project*.json` 各 274.7 MB。

### 1.7 跨目录重复（这是 175G 里最肥的一块）

按"文件名 + 精确大小"分组（≥200 MB），**同一份 git pack 最多出现 12 次**：

| 对象 | 单份 | 份数 | 出现位置（摘要） | 冗余量 |
|---|---:|---:|---|---:|
| `pack-c5de039e603d8e846d4834b4d3f20f1bf514554b` | 1.75 GB | **12** | `CNS-PLANNER\_work\round34_t07d2b/c0/c1\.git`、`backups\…\snapshots\A6_0_git_1..3`、`snapshots\A7_1_git_0`、`restore\inert_metadata\A6_0_git_1..3`、`restore\inert_metadata\A7_1_git_0`、`_cns_workspaces\acceptance\round37_a_2d\…\.git` | **19.25 GB** |
| `pack-cb2372572a1566ba4fbc8db5f306557ee16bacdf` | 1.82 GB | **4** | `CNS-PLANNER\.git`（活跃）、`_cns_acceptance\r7d22/23/25\repo\.git` | **5.46 GB** |
| `pack-5049135a24c90cc122ee8c8f785869320385317c` | 1.77 GB | **2** | `snapshots\A6_0_git_0`、`restore\inert_metadata\A6_0_git_0` | 1.77 GB |
| `pack-3ac8c5b9…` / `pack-12746d08…` / 其余小 pack | ≤0.17 GB | 2–12 | 同上模式 | ~1.0 GB |

全盘 `.pack` 合计 **102 个 / 33.53 GB**；按"每份内容留 1 份"去重后，**冗余约 27 GB**。

另外两个大重复群：

- `project_state.json` / `current_project.json`（250–490 MB 级）在 `CNS-PLANNER\_work`、`_runtime_projects`、`_tmproot`、`_cns_acceptance\r7d17/18/20/22/25`、`_cns_round38…\rounds`、`_cns_backups\{snapshots,restore}` 之间同大小成组出现，单组 2–4 份。
- `local_recovery_20261018_100555`（2.85 GB）本体 + `_cns_backups\…\snapshots\A8` + `restore\A8` = **3 份**。
- `round36_zhoushan_demo` 与 `round36_d1_zhoushan_demo` 各 1.04 GB / 1 401 文件、同大小结构，疑似同一轮的两份拷贝。

---

## 2. 第二步：分级清单

> 唯一性判断的依据分成四类，表里用 `[R]`/`[D]`/`[H]`/`[?]` 标注：
> `[R]` 可在别处重建（源仓库/源文件仍在，或可按输入重算）；`[D]` 被文档/清单引用（`PROTECTED_ASSETS.md`、`CONTROL.md`、`BASELINE.json`、`docs/`、轮次 `REPORT.md`/`artifact_manifest.json`）；`[H]` 含唯一 hash / bundle / SHA256 清单；`[?]` 查不到，按 unknown 处理。

### 2.A 立即可删（低风险，源在、可重建、无文档引用）

| 路径 | 大小 | 类型 | 唯一性判断 | 建议 | 风险 | 预计释放 |
|---|---:|---|---|---|---|---:|
| `_cns_backups\p0b1s_20261018_155628\restore` | **45.54 GB** | 恢复演练产物 | `[R]` 由 `snapshots` 解出，`restore_results.json` 记录 `sha256_all_passed=true`（A1 5 535 000 725 B、A6_0 27 139 659 219 B）；A1/A6_0 的 head `95e8128`/`68e2674` 在主仓 `git cat-file -t` 均为 commit。无文档引用该路径 | 删 | 失去"备份确实能解开"的现场实物；只剩 SHA256 清单与日志作旁证 | 45.54 |
| `_cns_backups\…\snapshots\A6_0_git_1` `_git_2` `_git_3` | 5.58 GB | worktree git 元数据 | `[R]` 三份 pack 均为 `pack-c5de039e`（同一 1.75 GB），origin 都指向本地 `D:\Projects\CNS-PLANNER`；对应 head `4c7a0a0`/`68e2674`/`2566275` 在主仓可达 | 删 | 若主仓 `.git` 同时损坏，少 3 个冗余源 | 5.58 |
| `_cns_backups\…\snapshots\A7_1_git_0` | 1.87 GB | 检查点仓库 .git | `[R]` `checkpoint/phase36-pre3d` 工作树仍在 `_cns_workspaces\checkpoint\phase36-pre3d`，head `2566275` 主仓可达；pack 同 `c5de039e` | 删 | 同上 | 1.87 |
| `_cns_acceptance\r7d22\repo\.git` `r7d23\repo\.git` `r7d25\repo\.git` | 5.46 GB | 验收 repo 的 .git | `[R]` 三份都是 `pack-cb237257`，与 `CNS-PLANNER\.git` 同 pack 同名同大小；工作树保留即可复查源码 | 删 `.git`，留工作树 | 三份 repo 变为"无历史快照"，不能 `git diff` | 5.46 |
| `CNS-PLANNER\_work\round34_t07d2c0_combined_los\.git`、`…t07d2c1_los_closeout\.git` | 3.50 GB | 临时仓库 .git | `[R]` 同为 `pack-c5de039e`，head `68e2674` 在主仓可达；`_work` 本身是临时目录 | 删（保留 `t07d2b` 一份） | A7_HIST_1/2 失去本地 git，只剩工作树 | 3.50 |
| `CNS-PLANNER\_diag` | 3.07 GB | 临时产物 | `[R]` 五个 `_edge_profile*` 浏览器 profile（2.2 GB）+ 截图，PROTECTED_ASSETS §4 明确"诊断脚本/派生不属产品源码" | 删 | 无（诊断现场不可回放） | 3.07 |
| `CNS-PLANNER\_vbase_r25` | 2.74 GB | 旧基线树 | `[R]` 其 `_diag` 2.5 GB 与 `_diag` 同源；`_runtime_projects` 0.22 GB 为 state 副本 | 删 | 无 | 2.74 |
| `CNS-PLANNER\_output` | 1.30 GB | 旧输出 | `[R]` `round28_c_residual`、`reports` 均为可重跑轮次产物 | 删 | 无 | 1.30 |
| `CNS-PLANNER\_tmproot` | 1.23 GB | 临时项目根 | `[R]` `round261_*` 为 2026-10 早期临时验收 | 删 | 无 | 1.23 |
| `CNS-PLANNER\_runtime_projects` | 2.23 GB | 运行时项目副本 | `[R]` `zhoushan_screenshot_01`、`_round28a_copy`、`_round29_osm_copy` 三份 state 副本，均有同名同大小副本在 `_cns_backups` | 删 | 浏览器"最近项目"入口失效 | 2.23 |
| `CNS-PLANNER\_migration_smoke` | 0.50 GB | 冒烟副本 | `[R]` `zhoushan_save_20261002`、`smoke_20261002_api` 各 252 MB | 删 | 无 | 0.50 |
| `CNS-PLANNER\.test_runtime` | 0.29 GB | 测试运行时 | `[R]` `pytest` 195 MB + `phase2-deps` 90 MB，可重装 | 删 | 下次测试需重装依赖 | 0.29 |
| `CNS-PLANNER\outputs`（仅 `sys_tmp`、`pytest_tmp*`、`pytest_full`、`pt_full`、`pt_final` 等缓存） | ~1.80 GB | 临时产物 | `[R]` 同内容 `.staged` 298 MB×3 已在 `_work\pytest_tmp*` 与 `_cns_backups` 各一份；`phase3-e2e-20260921` 0.40 GB 为 9 月 E2E | 删缓存，留 `reports/` | 无 | 1.80 |
| `CNS-PLANNER\_work\landmask_osm` | 2.17 GB | 输入源（下载） | `[R]` `.zip` 884.9 MB + 解出 `.shp` 1.23 GB 是 OSM `land-polygons-split-4326`，可重下；产品用 `projects\derived\land_mask_osm\zhoushan_land_osm_v2.gpkg`，不是它 | 删（保留 zip 亦可） | 需重下 OSM 才能复算掩膜 | 2.17 |
| `CNS-PLANNER\_work\round29l_live` `round29p` `round29q` `round32` `pytest_tmp*` | 5.70 GB | 临时产物 | `[R]` 大量 250–797 MB 的 `*_response.json` 运行时响应 | 删 | 无 | 5.70 |
| `_cns_workspaces\test` + `test_tmp` + `tmp` | 1.58 GB | 测试临时 | `[R]` `round38_int_a`、`r38n2` 测试树 | 删 | 无 | 1.58 |
| `_cns_workspaces\acceptance\round36_d1_zhoushan_demo` | 1.04 GB | 重复验收快照 | `[R]` 与 `round36_zhoushan_demo` 同大小同文件数（1.04 GB / 1 401 vs 1 406），是同一轮两份拷贝 | 删其一 | 若两者其实有 5 个文件差异，需先 diff | 1.04 |
| `_cns_round38_global_recovery\local_recovery_20261018_100555` | 2.85 GB | 恢复整包 | `[R]` `_cns_backups\…\snapshots\A8` 与 `restore\A8` 各存一份；`00_MANIFEST\CHECKSUMS.sha256` 可校验 | 删（保留 `A8` 一份 + MANIFEST） | 若同时删 A8 就失去唯一副本 | 2.85 |
| `_cns_backups\r7d14*`/`r7d15*`/`r7d16*`/`r7d13-deferred-candidates` 等 13 个残留目录 | ~0.15 GB | 老轮次冲突副本 | `[R]` `.ours/.theirs/.base/.merged/.diff3` 是 merge 中间件；对应 head `eddccb1` 在主仓 `codex/r7d13-ab-integration` 可达 | 删冲突件，留 `*.bundle`（bundle 见第三步，0.20 GB 全留） | 无 | 0.15 |
| **A 档小计** | | | | | | **≈ 89 GB** |

### 2.B 核对后删 / 压缩归档（中风险）

| 路径 | 大小 | 类型 | 唯一性判断 | 建议 | 风险 | 预计释放 |
|---|---:|---|---|---|---|---:|
| `_cns_backups\…\snapshots\A6_0` | **25.28 GB** | 完整工作区副本 | `[?]`→偏 `[R]`：其 `_diag` 3.07 / `outputs` 2.40 / `_vbase_r25` 2.74 / `_runtime_projects` 2.23 / `_output` 1.30 / `_tmproot` 1.23 / `_migration_smoke` 0.49 / `.test_runtime` 0.28 的**大小与当前 CNS-PLANNER 完全一致**，是逐字节重复；但 `_work` 为 11.32 GB vs 当前 16.94 GB，含 10-18 之后被改写/删除的 untracked 中间产物 | 压缩归档（或先做差异清单再删） | 若其中确有"当前已不存在"的 `_work` 半成品，删后不可重建 | 归档至 ~8–10 GB，净释放 15–17 |
| `_cns_backups\…\snapshots\A1` | 5.15 GB | 完整副本 | `[R]` 对应树仍在且更新（`round38_acceptance` 9.99 GB / `9404d16`）；但其中 `projects` 3.28 GB 是 10-18 的 `current_project.json` 版本，`projects/current_project.json` 现为 revision 471 | 归档（留 `projects` 内 state 的 hash 清单） | 丢失 rev471 之前的状态快照 | 4–5 |
| `_cns_acceptance\r7d17\R0005-rev524` + `r7d17\source` | 1.36 GB | 验收快照 | `[D]` `r7d17` 有 23 个 probe 脚本与 `probe.png`，是 P14/P15 探针原始证据；但两份 488.8 MB `project_state.json` 同大小 | 留脚本+图，删 state 副本（留 1 份） | 失去该轮 state 现场 | 0.98 |
| `_cns_acceptance\r7d18\R0005-rev524` | 0.88 GB | 验收快照 | `[D]` 与 `r7d17` 的 rev524 高度重叠（488.8 MB `project_state.json` + 275 MB `.bak`） | 留 `api_*.json`，删 state | 同上 | 0.76 |
| `_cns_acceptance\r7d20\historical_before_2km.json` | 0.31 GB | 单轮大产物 | `[?]` 仅 `r7d20` 提及；无跨轮引用 | 压缩归档 | 该轮"2 km 历史基线"不可即时复算 | 0.25 |
| `_cns_acceptance\r7d21`~`r7d32` 的大 json/npz（`full62_p14` 188 MB、`after_p14` 203 MB、`optimization` 232 MB、`p14_full_diagnostic` 88 MB 等） | ~3.00 GB | 中间大产物 | `[D]` 各轮有 `REPORT.md`/`artifact_manifest.json`/`protected_hashes.json`；这些 json 是报告数据源，删后报告不可复现 | 压缩归档（zst/7z） | 报告中数字失去可复算底稿 | 压缩后净释放 ~1.5–2.0 |
| `_cns_round38_global_recovery\rounds\R05`~`R30B` 的 `backup/`、`output/`、`delivery/attachments` | ~5.50 GB | 历史轮次大产物 | `[D]` 每轮有 `r12_final_manifest.json` 等；`R20\output\study_report_preview.html` 531.6 MB、`R23\delivery\attachments\section_11_p18.json` 400.4 MB 为单轮巨型件 | 压缩归档；manifest/报告保留原样 | 历史轮次可视化预览不可直接打开 | 4.0–5.0 |
| `_cns_workspaces\dev` 旧轮次（`r7b_pool` 0.94、`round37_b0` 0.71、`r7d4` 0.67、`r7b_3d` 0.58、`r7d3b` 0.36、`round5_r5` 0.30、`r5g`/`r6_p16`/`round4b` 0.15） | ~3.50 GB | 开发工作树 | `[D]` `PROTECTED_ASSETS.md` §3 明确：这 7 棵是 `89e2f64` 系**唯一有效三维源码**，且 §3.1 结论"在主流版本固化前不得删除" | **保留**（不删）；仅可删各树的 `.git`（pack 与主仓同源） | 违反 §7 前置条件 1 | 删 `.git` 约 0.2（体积小，收益低） |
| `_cns_workspaces\acceptance\round37_a_2d_preacceptance\.git`（`CNS-PLANNER\.git`） | 1.75 GB | 验收树 .git | `[R]` `pack-c5de039e` 与 `_work\round34_t07d2b\.git` 同 pack；head `2566275` 主仓可达 | 删 `.git`，留工作树（三维源码 CRLF 版在此树，必须留文件） | 该树不能 `git status/diff` | 1.75 |
| `_cns_workspaces\acceptance\round37_b0` + `round34_t07_i2_h3` + `round34_t07_i2*` 交付副本 | ~1.00 GB | 验收快照 | `[D]` `round34_t07_i2_h3` 有 `06_integration_frozen_manifest.json`（81 KB，含 SHA），是交付冻结证据 | 保留；仅删重复的 `_scratch`/`tmp` | 违反交付冻结 | 0.1 |
| `_cns_workspaces\integration\r6c_p16_v2_ui` | 0.75 GB | 旧集成分支树 | `[D]` `PROTECTED_ASSETS.md` §3 记为历史副本（`2cc8bad`，三维 service 143 965 B） | 保留 | 丢失 P16-V2 UI 中间态 | 0 |
| `_cns_workspaces\derived\round38_offline_imagery` | 2.92 GB | 离线影像瓦片 | `[?]` 29 451 个瓦片，`PROTECTED_ASSETS.md` §4 只把"影像瓦片代理缓存"列为可重算，未点名此目录；产品 `map_sources.json` 未引用它 | 待核实（见第四步） | 删除后离线影像模式失效（`status=offline_mode` 依赖它） | 2.92 |
| `CNS-PLANNER\research` | 0.82 GB | 研究产物 | `[?]` 代码 `cns_planner/` 内 **0 处**把 `research/` 当路径常量；但含 `r5\evidence\rev523\copy_checksum.json`（被多处副本引用） | 保留 `copy_checksum`/`protected_hashes`，删大 state 副本 | 研究轮不可复现 | 0.5 |
| **B 档小计** | | | | | | **≈ 30–45 GB**（含 A6_0 归档） |

### 2.C 保留（不删，理由见第三步）

| 路径 | 大小 | 类型 | 唯一性判断 | 建议 | 风险 | 预计释放 |
|---|---:|---|---|---|---|---:|
| `_cns_backups\…\snapshots\A6_0_git_0` | 1.86 GB | 主仓 .git 快照 | `[H]` 唯一一份带 github origin 的完整 .git 快照；`restore_results.json` 用它做过 checkout | 保留 | 删则失去离线 .git 备份 | 0 |
| `_cns_backups\…\snapshots\A8` | 2.91 GB | 恢复包 | `[H]` 含 `Manifest.csv` 607 KB + `CHECKSUMS.sha256` 205 KB | 保留（A 档删本体后它是唯一副本） | 删则恢复包归零 | 0 |
| `_cns_backups\…\bundles\`（7 个）+ 根级 5 个 `*.bundle` | 0.33 GB | git bundle | `[H]` 18 个 bundle 自带 SHA256（`bundle_results.json` 逐条记录） | 保留全部 | 违反"所有 .bundle 不可删" | 0 |
| `_cns_backups\…\BACKUP_COMPLETE_CHECKSUMS.sha256` / `03_backup_checksums.sha256` / `file_inventory.csv` / `preflight.json` / `commands.json` / `source_map.json` / `repo_states.json` | 0.13 GB | manifest/SHA 清单 | `[H]` 备份的唯一性证明 | 保留 | 备份不可验证 | 0 |
| `_cns_backups\r7d13-d9-2b953bfc.bundle` | 148.7 MB | git bundle | `[H]` 2b953bf = `codex/r7d9-risk-checkpoint`，主仓可达但仍保留离线 bundle | 保留 | 无 | 0 |
| `CNS-PLANNER\.git` | 2.25 GB | 活跃仓库 | `[D]` 唯一主干历史 | 保留 | — | 0 |
| `CNS-PLANNER\projects\current_project.json`（+`.bak`、`.bak-stabilization`） | 5.4 MB | 权威状态 | `[D]` `data_sources.json`/`map_sources.json` 同目录；产品唯一入口 | 保留 | 状态归零 | 0 |
| `CNS-PLANNER\docs\decisions\2026-09-ai-dev-context-full-archive.md` | 269 KB | 决策归档 | `[D]` AGENTS.md 指定为历史决策唯一入口 | 保留 | 违反"docs/decisions 不可删" | 0 |
| `CNS-PLANNER\docs\`（其余 36 个文件） | 0.62 MB | 报告 | `[D]` `15-Phase4-Final-Preacceptance.md`、`19-三维系统资产与恢复说明.md` 等 | 保留 | — | 0 |
| `CNS-PLANNER\PROTECTED_ASSETS.md` + `docs\3d-protected-assets.json` + `tools\verify_3d_assets.py` | 0.1 MB | 受保护清单 | `[H]` 15 个文件逐字节 SHA + 清理八条前置条件 | 保留 | 违反 §7 | 0 |
| `_cns_workspaces\integration\round38_acceptance` | 9.99 GB | 最后集成分支树 | `[D]` `PROTECTED_ASSETS.md` §1 记为 8776 project_root；`9404d16`，含 `projects\round38_cns_experiment_b` 5.40 GB 实验结论 | 保留 | 丢失当前结论运行树 | 0 |
| `_cns_workspaces\checkpoint\r7a-3d-protected` | 44.8 MB | 三维检查点 | `[D]` `PROTECTED_ASSETS.md` §3 唯一独立检查点（15 文件 + Cesium + 校验器） | 保留 | 违反 §7 前置条件 1 | 0 |
| `_cns_workspaces\CONTROL.md` + `BASELINE.json` + 9 个 `.bak` | 1.2 MB | 协作权威 | `[D]` 所有开发任务先读 | 保留 | 状态断链 | 0 |
| `_cns_acceptance\r7d33` `r7d34` `r7d35` | 0.75 GB | **当前结论三连** | `[D]` D33 池 → D34 优化（"新增 5 站址/16 设备/8 站"）→ D35 验证（"909/909，研究通过、业务阻塞"）；三份 `REPORT.md` 逐轮互相引用 | 保留（含 `artifact_manifest.json`/`protected_hashes.json`） | 丢失当前结论链 | 0 |
| `_cns_acceptance\r7d26`~`r7d32` 的 `REPORT.md`/`PLAN.md`/`REPRODUCE.md`/`artifact_manifest.json`/`protected_hashes.json` | ~40 MB | 轮次报告+清单 | `[D]` D33 明确引用 D32 rev533 与 1284 行既有动作 | 保留（大 json 可归档） | 上游结论断链 | 0 |
| `_cns_round38_global_recovery\local_recovery…\00_MANIFEST`（`MANIFEST.csv`/`CHECKSUMS.sha256`/`VERIFY_RESULT.json`/`RECOVERY_ROOT.md`） | ~1 MB | manifest | `[H]` `continuation_identity.json` 记录了其中 6 个文件的 expected/actual SHA 全等 | 保留 | 恢复包不可验证 | 0 |
| `_cns_round38_global_recovery\01..07_*.md` + `file_index.tsv` + `asset_index.json` | ~7.5 MB | 索引与报告 | `[D]` 全局恢复结论 | 保留 | — | 0 |
| `CNS-PLANNER\audit_sandboxes\r0005_stage0/stages123/stages456_20261024_01` | 1.09 GB | 最新审计沙盒 | `[D]` 2026-10-24（最新）r0005 三阶段审计，含 `snapshot_manifest.json` 1.3 MB / `input_manifest.json` 162 KB / `bundle_index.json` | 保留 | 丢失最新审计链 | 0 |

**汇总：A 档 ≈ 89 GB；B 档 30–45 GB；C 档保留基线 ≈ 22–47 GB。**
**可释放区间 ≈ 110–135 GB；清理后 `D:\Projects` ≈ 57–82 GB。**

---

## 3. 第三步：保留清单（不可删）

### 3.1 当前有效轮次与上一轮

| 角色 | 对象 | 证据 |
|---|---|---|
| 当前有效轮次（研究结论） | `D:\Projects\_cns_acceptance\r7d35`（D35"精确 100m 固定联合方案验证"） | `r7d35\REPORT.md`：909/909 C/S 达标、LOS 0 m/1 m 无缺口、阻塞项为"正式业务确认" |
| 上一轮 | `D:\Projects\_cns_acceptance\r7d34`（D34 R0005 C/S 联合优化） | `r7d34\REPORT.md`：新增 5 站址/16 设备/总 8 站，输入指纹 `7694c326…` |
| 上上轮（池） | `D:\Projects\_cns_acceptance\r7d33` | `r7d33\REPORT.md`：4211 来源→2864 坐标/5730 动作，C/S 上界 909/909 |
| 当前有效集成轮 | `D:\Projects\_cns_workspaces\integration\round38_acceptance\CNS-PLANNER`（`9404d16`，`integration/round38-acceptance`） | `PROTECTED_ASSETS.md` §1/§3；`git worktree list` 实测 |
| 当前审计轮 | `D:\Projects\CNS-PLANNER\audit_sandboxes\r0005_{stage0,stages123,stages456}_20261024_01` | 目录名日期 2026-10-24，`snapshot_manifest.json`/`input_manifest.json` 齐全 |
| 主仓库 | `D:\Projects\CNS-PLANNER`（`feat/p16-background-task` @ `68e2674`，dirty 199） | `git rev-parse` 实测 |

### 3.2 所有 `.bundle`（18 个 / 326.7 MB，一个都不删）

| 路径 | 大小 |
|---|---:|
| `_cns_backups\r7d13-d9-2b953bfc.bundle` | 148.7 MB |
| `_cns_backups\p0b1s_20261018_155628\bundles\shared.bundle` | 18.2 MB |
| `_cns_backups\p0b1s_20261018_155146\bundles\shared.bundle` | 18.2 MB |
| `_cns_backups\…\155628\restore\A8\local_recovery…\02_GIT\CNS-PLANNER-full.bundle` | 18.2 MB |
| `_cns_backups\…\155628\snapshots\A8\local_recovery…\02_GIT\CNS-PLANNER-full.bundle` | 18.2 MB |
| `_cns_round38_global_recovery\local_recovery…\02_GIT\CNS-PLANNER-full.bundle` | 18.2 MB |
| `_cns_backups\r7d17-integration.bundle` / `r7d16-e-integration.bundle` | 12.4 MB ×2 |
| `_cns_backups\r7d15-d-integration.bundle` / `r7d14-c-integration.bundle` / `r7d13-ab-integration.bundle` | 7.0–7.4 MB ×3 |
| `_cns_backups\…\155628\bundles\A7_1.bundle` / `A7_0.bundle` / `A7_HIST_0..2.bundle` / `A7_0_original_shallow.bundle` | 3.6–7.1 MB ×6 |
| `_cns_backups\p0b1s_20261018_155146\bundles\A7_0.bundle` | 3.6 MB |

> 注意：`_cns_backups\p0b1s_20261018_155146\`（85.6 MB）是**中断的备份**，`resume_evidence.json` 证明 `155628` 从它续做、复用了 75 571 条记录。它只有 manifest/checksum 与 2 个 bundle，**不占空间，建议整目录保留**作"续做证据"。

### 3.3 所有 manifest / SHA256 清单

- `_cns_backups\p0b1s_20261018_155628\`：`BACKUP_COMPLETE_CHECKSUMS.sha256`（20.2 MB）、`03_backup_checksums.sha256`（11.5 MB）、`file_inventory.csv`（56.7 MB）、`source_map.json`（2.0 MB）、`repo_states.json`（444 KB）、`bundle_results.json`/`git_bundle_results.json`、`preflight.json`、`commands.json`、`round37_project_state_inventory.json`、`resume_evidence.json`、`capacity.json`、`tail_manifest.json`
- `…\continuation_161537\` 与 `…\continuation_162014\`：各自的 `BACKUP_COMPLETE_CHECKSUMS.sha256`、`03_backup_checksums.sha256`、`file_inventory.csv`、`continuation_identity.json`、`seal_policy.json`
- `_cns_round38_global_recovery\local_recovery…\00_MANIFEST\`：`MANIFEST.csv` 607 KB、`CHECKSUMS.sha256` 205 KB、`VERIFY_RESULT.json`、`COPY_FAILURES.json`、`PACKAGE_SUMMARY.json`、`RECOVERY_ROOT.md`
- `_cns_workspaces\deliverables\**\*_frozen_manifest.json`（81–82 KB ×8）、`_cns_workspaces\deliverables\round36_d0\evidence\r36_source_manifest.json`
- `_cns_acceptance\r7d32..r7d35\{artifact_manifest,source_manifest,source_code_manifest,input_manifest}.json` 与各轮 `protected_hashes.json`
- `CNS-PLANNER\docs\3d-protected-assets.json`、`CNS-PLANNER\PROTECTED_ASSETS.md`、`CNS-PLANNER\projects\map_sources.json`/`data_sources.json`
- `CNS-PLANNER\audit_sandboxes\r0005_stage0_20261024_01\snapshot_manifest.json`（1.3 MB）
- `_cns_round35_e_buildings\downloads\gba_height\checksums.sha512`（153 KB，**输入源 hash，别丢**）

### 3.4 docs/decisions、docs/runs、projects/current_project.json

- `docs\decisions\`：只有 1 个文件 `2026-09-ai-dev-context-full-archive.md`（269 KB）→ 保留。
- `docs\runs\`：**不存在**（`docs/` 下只有 `decisions/`、`project_audit/`、`superpowers/`）。等效物是 `_cns_workspaces\CONTROL.md` + `_cns_acceptance\r7d*\REPORT.md` + `_cns_round38_global_recovery\rounds\r*\`，按第三步前三项保留。
- `projects\current_project.json`：1.80 MB（+ `.bak` 1.80 MB + `.bak-stabilization` 1.78 MB）→ 保留全部三份。

### 3.5 `_cns_sources` 类输入源

`D:\Projects` 下**没有** `_cns_sources` 目录。实际输入源分两类：

1. **外部权威源（不在本次范围）**：`D:\aaa2026project\UOM\...`（FABDEM DTM、GLO30、人口栅格、`zhoushan_buildings_v3.gpkg`、`building_grid_L8_v3.gpkg`、塔清单 xlsx、起降点/真实航路 xlsx），已在 `projects\map_sources.json` 登记且目录存在。
2. **`D:\Projects` 内的下载副本（可重下，但建议先留 hash）**：
   - `_cns_round35_e_buildings\downloads` 856 MB（建筑高度 GBA 下载，含 `checksums.sha512`）+ `_work` 187 MB → **保留 manifest 与 checksum，数据可重下**
   - `_cns_round35_s5_acceptance\sources` 714 MB（`buildings_v3.gpkg` 445 MB 等 4 个）+ `isolated_project` 674 MB
   - `_cns_round35_f4_building_data\_work` 494 MB（`buildings_v3.gpkg` 445 MB，与上条同文件）
   - `_cns_round35_b2/b3` 等其余 round35 目录（合计 ~0.2 GB）
   - `CNS-PLANNER\projects\derived\land_mask_osm\zhoushan_land_osm_v2.gpkg`（产品直接引用，**保留**）、`projects\derived\cartographic_land\cartographic_land_v1.gpkg`（**保留**）

---

## 4. 第四步：待你裁决项（按影响排序）

### 裁决 1（最大，45.5 GB）：`p0b1s` 的 `restore/` 是"恢复演练现场"，删还是留？

- **选项 A（推荐）：删 `restore/`，只留 `snapshots/` + 全部清单。**
  - 代价：失去"备份真的解开过"的实物；证据降级为 `restore_results.json`（`sha256_all_passed=true`，A1 5 535 000 725 B / A6_0 27 139 659 219 B）+ `logs\A1_restore_clone.log` 等 20 个日志。重跑一次恢复演练约需 2×48.6 GB 读写。
  - 释放：45.54 GB。
- **选项 B：保留 `restore\A1` + `restore\A6_0` 的 `inert_metadata`，删其余。**
  - 代价：只释放约 20 GB，且 `restore` 与 `snapshots` 的对应关系变得不完整，下次核对要重建映射。
  - 释放：~20 GB。

> 证据：`_cns_backups\…\restore_results.json` 只有 A1 与 A6_0 两条记录；`snapshots` 的 45.29 GB 是 `capacity.json` 记录的 75 572 个源文件；`restore` 的 76 140 个文件里有 8 份 `A*_git_*` 副本（与 snapshots 各一份，见 §1.7）。

### 裁决 2（25.3 GB）：`snapshots\A6_0` 是"10-18 工作区完整副本"，删/归档/留？

- **选项 A：先做差异清单（对比当前 `CNS-PLANNER`），只归档 `_work` 独有部分，其余删。**
  - 代价：需 1–2 小时只读比对；对比结果本身要落盘。
  - 释放：15–17 GB（净）。
- **选项 B：整体压缩归档到外部盘/冷存储（zst 后约 8–10 GB）。**
  - 代价：一次性 25 GB 读 + 压缩耗时；归档后检索成本高。
  - 释放：15–17 GB（原地）/ 25.28 GB（移出 D 盘）。

> 证据：`_diag` 3.07 / `outputs` 2.40 / `_vbase_r25` 2.74 / `_runtime_projects` 2.23 / `_output` 1.30 / `_tmproot` 1.23 / `_migration_smoke` 0.49 / `.test_runtime` 0.28 与当前主仓**大小完全一致**（逐目录对比见 `_cleanup_audit/out_a.txt` 第 2029–2070 行区间），是重复；但 `_work` 为 11.32 GB vs 当前 16.94 GB，即 10-18 之后主仓 `_work` 又长了 5.6 GB，反向说明 A6_0 含**已被改写**的中间件。

### 裁决 3（6.5 GB）：`_cns_acceptance\r7d22/23/25\repo` 的 `.git` 删不删？

- **选项 A（推荐）：删 3 个 `.git`（5.46 GB），保留工作树。**
  - 代价：三份 repo 无法 `git diff`；如需追溯用主仓 `pack-cb237257`（同一 pack，已确认同名同大小）。
  - 释放：5.46 GB。
- **选项 B：保留 `.git`，改为删工作树里的重复 `project`/`source` 副本。**
  - 代价：只释放约 1.0 GB，三份 1.82 GB pack 继续占 5.46 GB。

### 裁决 4（2.9 GB）：`_cns_workspaces\derived\round38_offline_imagery`（29 451 个瓦片）是否产品依赖？

- **选项 A：待核实后删（若确认只是 8776 演示的离线底图缓存）。**
  - 代价：离线影像模式（`status=offline_mode`）页面变空白，需重新拉瓦片。
  - 释放：2.92 GB。
- **选项 B：保留。**
  - 代价：2.92 GB 长期占用；但保住了 `PROTECTED_ASSETS.md` §7 前置条件 5 的"离线模式可渲染"。

> 证据：`PROTECTED_ASSETS.md` §4 只写"影像瓦片代理缓存……删除后按需重算"，未点名该目录；`projects\map_sources.json` 也没有 `round38_offline_imagery` 条目 → **unknown**，必须你裁决。

### 裁决 5（0.8 GB）：`CNS-PLANNER\research` 是否被产品引用？

- **结论先行：代码不引用**。`cns_planner/**/*.py` 检索 `outputs/`、`research/` 路径常量 → **0 命中**；`research` 的 4 706 次命中全部是 `AGENTS.md`/`README.md` 的**排除性规定**（"不搜索 research/"）与副本里的 `copy_checksum.json`。
- **选项 A：只留每轮 `copy_checksum.json`/`protected_hashes.json`，删 `r7d10`(310 MB)、`m4-2c-b`(261 MB)、`r7d8`(199 MB) 的 state 载荷。** 释放 ~0.5 GB。
- **选项 B：整体保留。** 代价 0.82 GB；保住研究报告的可复算底稿。

> 同理 `CNS-PLANNER\outputs` 也不被产品引用（产品只写 `projects/`），其中 `reports/` 建议留、缓存删。

### 裁决 6（3.0 GB）：`_cns_acceptance` 哪几轮是"当前结论来源"？

- **必须留：`r7d33` + `r7d34` + `r7d35`**（0.75 GB）。理由：三份 `REPORT.md` 构成 D33 池 → D34 优化 → D35 验证的完整链，D35 是"研究通过 / 业务阻塞"的当前结论。
- **留报告+manifest、归档大 json：`r7d26`–`r7d32`**（7 轮，各含 `REPORT.md`/`PLAN.md`/`REPRODUCE.md`/`artifact_manifest.json`/`protected_hashes.json`）。D33 明确引用 D32 rev533 与 1284 行既有动作，删了上游就断链。
- **可只留脚本与图：`r7d17`–`r7d21`**（state 副本 488 MB×2、275 MB×2 可删，合计 ~2.0 GB 可释放）。
- **`r7d19` 只有 1 个 `final_release_audit.json`（4 KB）→ 留；`r7d24` 11.5 MB → 留。**

### 裁决 7（3.9 GB）：`CNS-PLANNER\_work\round34_t07d2b/c0/c1` 三个 1.94 GB 临时仓库

- **选项 A：删 `c0`/`c1` 的 `.git`（3.5 GB），留工作树；`t07d2b` 整份保留。** 代价：A7_HIST_1/2 失去 git 历史（head `68e2674` 在主仓可达）。
- **选项 B：三个整目录移出 D 盘归档（5.8 GB 全部释放）。** 代价：`repo_states.json` 里 A7_HIST_0..2 三条受保护记录失去对应实体，需同步更新 `repo_states.json` 的登记说明。

### 裁决 8（1.0 GB）：`round36_zhoushan_demo` vs `round36_d1_zhoushan_demo` 是否真重复

- 两者 1.04 GB / 1 401 与 1.04 GB / 1 406 文件。**选项 A：先 `robocopy /L` 或 `diff` 确认后删一份**（1.04 GB）；**选项 B：都留**（1.04 GB）。

---

## 5. 结论（200 字）

D:\Projects 实占 192.38 GB（179.2 GiB）。真正的问题不是"项目大"，而是 `_cns_backups\p0b1s_20261018_155628` 用 91.16 GB 同时存了**备份本体（snapshots 45.29 GB）和恢复演练现场（restore 45.54 GB）**，而这两份里又各有 4–12 份相同的 git pack；全盘 102 个 `.pack` 共 33.53 GB，去重后冗余约 27 GB。低风险可立即释放 **约 89 GB**（restore 45.54 + pack 冗余 16.4 + 主仓临时目录 21.0 + 测试/冒烟/重复快照 5.7）；核对归档后再释放 **30–45 GB**。**预计释放 110–135 GB，清理后剩 57–82 GB；最少必须保留约 22 GB，稳妥保留 45–50 GB**（主仓 `.git` 2.25、18 个 bundle 0.33、全部 SHA 清单、`round38_acceptance` 运行树 9.99、r7d33/34/35 0.75，以及 A6_0 快照一份）。**最大风险是删掉 `restore/` 与 `snapshots\A6_0` 后，"备份确实能解开"和"10-18 的 untracked 中间产物"只剩清单、没有实物**；这两个必须先按 `PROTECTED_ASSETS.md` §7 的八条前置条件过一遍，再动手。
