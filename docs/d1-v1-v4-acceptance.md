# D1 固定验收清单 V1–V4（2026-10-27）

> 本文件**锁定** V1–V4 的含义与通过判据。Astra 新提出的**合同测试、fresh compute、展示测试**
> 作为子测试组挂在 V1–V4 下，**不重新定义** V1–V4，也不新增版本号。
> 适用候选：`_work/d1/rc`（`89b008d` + C3-A/B/C 研究切片 + D1 治理/图面修复）。

---

## 0. 一句话定义

| 版本 | 名称 | 固定含义 |
|---|---|---|
| **V1** | 完整交互链 | 用户能从二维界面完成“加载 → 修改允许参数 → 提交真实重算 → 查看 P14–P18 → 导出报告”的完整闭环 |
| **V2** | Radar 缺失正确传播 | Radar 缺失必须沿 `Radar not_calculated → S unknown → P14/P15 failed → P18 evidence_required` 正确传播，不得被任何一层洗成 pass |
| **V3** | 重启恢复不重算 | 停服重启后只读恢复同一结果，所有冻结身份逐字节一致，不触发求解器/worker |
| **V4** | 二维/三维/QGIS/报告同源 | 四个展示端读取**同一份** run manifest，身份与阶段结果 SHA 一致；输入改变时四端同步跟随 |

---

## 1. V1：完整交互链

### 1.1 通过判据

1. 二维页加载已发布 run：显示 7 站 / 18 设备 / 5 既有宿主 / 2 规划新建 / 4 个 N 动作。
2. 允许修改的参数只有 `navigation_middle_monitor_sites`（`c3b-research-parameters-1`）；
   修改后界面提示“将生成新输入指纹”，提交按钮可用。
3. 提交返回 HTTP 202，`mode=fresh_compute`、`solver_executed=true`；任务最终 `succeeded`。
4. 新 run 的 `input_version`、`input_fingerprint`、`parameter_fingerprint` 与旧 run 不同；
   旧结果不得冒充新结果。
5. 可查看 P14–P18 阶段结果与证据容器；可导出 HTML/Markdown 报告。
6. 报告/界面显示的结论全部来自 manifest，不硬编码。

### 1.2 证据

| 证据 | 路径 | 关键值 |
|---|---|---|
| 默认 run 提交 | `_work/c3b/evidence/c3b-evidence.json` | `task-04ab281910214b50`、202、`fresh_compute` |
| 参数修改 | `_work/c3b/evidence/ui-parameter-edit.json`、`02-parameter-edited.png` | 新指纹提示 |
| 界面提交 | `_work/c3b/evidence/ui-submitted-run.json`、`03-ui-submitted-run.png` | `task-3c57399bf205485e` succeeded |
| 最终界面 | `_work/c3b/evidence/04-final-2d.png` | 7/18/4 |
| 报告 | `_work/c3b/evidence/R0005-research-report-final.html/.md` | 与 manifest 同身份 |

### 1.3 子测试组

| 子组 | 覆盖 | 命令 |
|---|---|---|
| 合同测试组 | 参数合同、N v2 显式 opt-in、2 km 输入、RID surface | `pytest tests/test_c3b_research_ui.py tests/test_round40a_navigation_integrity_v2.py tests/test_required_cns_adoption_services.py` |
| fresh compute 组 | `POST /api/research/runs` → TaskSpec → worker → 五阶段 | `pytest tests/test_c3a_research_run.py tests/test_c2b_middle_projection.py` |
| 展示测试组 | 二维标签避让、端点/中段分类 | `node tests/c3c_research_map_labels.test.mjs`；`node tests/c3b_research_ui.test.mjs` |

### 1.4 当前结论

C3-B 已 PASS（研究切片）；D1 未重跑 fresh_compute（按要求不运行新的 P14–P18 全链）。
**V1 证据沿用 C3-B 冻结 run，未被 D1 改写。**

---

## 2. V2：Radar 缺失正确传播

### 2.1 通过判据（链路必须逐级成立）

| 层 | 期望 | 不得 |
|---|---|---|
| Radar | `not_calculated` / `unknown`，无覆盖几何 | 不得画有效覆盖、不得标 satisfied |
| S | `unknown`（2772 体元） | 不得读作 pass；不得用 C/RID 满足替代 |
| P14 | `failed`（business_failed），非 stale/missing_data | 不得改判为 passed |
| P15 | `failed`（business_failed） | 同上 |
| P16 | `proposal_ready`（仅提案） | 不得 Adopt/Confirm/Apply |
| P17 | `fully_satisfied` 仅覆盖连续中断事件 | 不得读作 S/Radar 覆盖通过 |
| P18 | `review_initialized`、`confirmed=false`、`applied=false`、`confirmation_allowed=false`、变体 `evidence_required` | 不得 Confirm/Apply |

### 2.2 证据

| 证据 | 路径 | 关键值 |
|---|---|---|
| manifest 阶段 | `_work/c3b/run/runs/c3b-20261027-d/run_manifest.json` | P14/P15 `failed`、P16 `proposal_ready`、P17 `fully_satisfied`、P18 `review_initialized` |
| P18 门禁 | 同上 `stages.P18` | `confirmation_allowed=false`、`confirmed=false`、`applied=false` |
| Radar 限制图 | `_work/d1/figures/c3b-20261027-d/cns_radar_limitation.png` | 无 Radar 覆盖 |
| P18 界面 | `_work/c3b/evidence/05-p18-no-confirm.png` | 禁止 Confirm/Apply |

### 2.3 子测试组

| 子组 | 覆盖 | 命令 |
|---|---|---|
| 合同测试组 | P18 门禁、S/Radar 不得互相替代 | `pytest tests/test_c3c_research_report.py tests/test_c3c_research_figures.py` |
| 展示测试组 | Radar 图只画限制、综合图不把 unknown 画成满足 | `pytest tests/test_c3c_research_figures.py` |

### 2.4 注意

`gates.p17_step6_gate.confirmation_allowed=true` 是 **P17 连续服务维度**的门禁，
不等于 P18 Confirm 被允许。P18 的 `confirmation_allowed=false` 才是发布/确认门禁。
二者不得混读。

---

## 3. V3：重启恢复不重算

### 3.1 通过判据

停服 → 重启（新 PID、新 token）后：

1. `manifest_sha256`、`manifest_body_sha256`、`result_list_sha256`、`input_version`、
   `input_fingerprint`、五阶段 `result_sha256` 全部一致。
2. 3D 载荷 SHA 与对象计数一致；报告 SHA 一致；QGIS 索引 SHA 与 12 张 PNG 逐字节一致。
3. `solver_executed=false`、`recomputed_anything=false`、`no_new_task=true`。
4. 重启后 UI 不带 `?run=` 也能恢复同一 run。

### 3.2 证据

| 证据 | 路径 | 关键值 |
|---|---|---|
| 重启对比 | `_work/c3c/evidence/restart_comparison.json` | `all_frozen_fields_identical=true`、两 run 全 true |
| 重启前后 | `_work/c3c/evidence/restart_before.json`、`restart_after.json` | task 数 6→6 |
| 独立重载 | `_work/c3b/evidence/recovery.json`、`fresh_reload.json` | `solver_executed=false` |
| QGIS 重渲染 | `_work/c3c/evidence/qgis_after_restart/` | 12/12 一致 |
| 最终停服 | `_work/c3c/evidence/final_stop.json` | `workers_stopped=true` |

### 3.3 子测试组

| 子组 | 覆盖 | 命令 |
|---|---|---|
| 恢复组 | manifest 只读、checkpoint 校验、task 终态 | `pytest tests/test_c3a_research_run.py` |
| 展示组 | 重启后 2D/3D 浏览器复核 | `node tests/c3b_research_ui.test.mjs`（静态合同） |

### 3.4 当前结论

C3-C 已 PASS；D1 未重启服务、未改 run 数据，V3 证据不变。

---

## 4. V4：二维/三维/QGIS/报告同源

### 4.1 通过判据

1. 同一 run 的 `run_id / input_version / input_fingerprint / contract_version / 结果清单 SHA`
   在以下六处一致：
   - 2D 界面；
   - manifest HTTP 头；
   - 3D HTTP 头；
   - 3D 载荷；
   - 报告 HTTP 头；
   - QGIS `figures_index.json`。
2. 五个阶段 `result_sha256` 在 manifest、3D 载荷、QGIS 索引三处一致。
3. 3D 载荷连续两次 GET 的 `payload_sha256` 相同。
4. 两个 run（4 N / 3 N）身份全部不同；N 动作数真实跟随输入，不硬编码。
5. 报告正文 run_id 与完整结果清单 SHA 与 manifest 一致。

### 4.2 证据

| 证据 | 路径 | 关键值 |
|---|---|---|
| 跨端身份 | `_work/c3c/evidence/cross_end_identity.json` | `all_consistent=true`、`changed_run_tracks_input=true` |
| 3D 载荷 | 同文件 runs/3D 段 | `d567787d…` / `46bfb94f…` |
| QGIS 索引 | `_work/d1/figures/<run>/figures_index.json` | `c4664cd1…` / `7d10c20f…`（D1 重渲染） |
| 报告 | `_work/c3c/evidence/reports/R0005-research-report-<run>.html/.md` | 头与正文一致 |
| 2D/3D 截图 | `_work/c3c/evidence/2d-*-final.png`、`3d-*-overview.png` | 7 站/18 设备/4 或 3 N |

### 4.3 子测试组

| 子组 | 覆盖 | 命令 |
|---|---|---|
| 展示测试组（3D） | EGM2008 正高→椭球高、地形网格、实体计数 | `pytest tests/test_c3c_research_visualization.py` |
| 展示测试组（QGIS） | 六图 spec/身份/图例边界 | `pytest tests/test_c3c_research_figures.py` |
| 展示测试组（报告） | 同源身份、HTML/Markdown 编码 | `pytest tests/test_c3c_research_report.py` |

### 4.4 当前结论

C3-C 已 PASS。D1 因修正通信/RID 图例与披露，重渲染 12 张图：
- 4N run 的 communication/navigation/radar/route 与 C3-C 逐字节相同；RID/combined 因文本变化而变；
- 3N run 六张均为 D1 新 SHA；
- 新 SHA 见 `docs/21` §5.5 与 `_work/d1/figures/<run>/figures_index.json`。
- **同源身份字段未被 D1 改动**（manifest/结果/报告/3D 载荷均只读）。

---

## 5. 验收执行矩阵（D3 用）

| 检查 | 命令 | 期望 |
|---|---|---|
| P14 保护哈希 | `pytest tests/test_phase4_b6r_immutable_task_input.py tests/test_phase4_b6x_heavy_task_runtime.py tests/test_phase4_b9r1_async_admission.py -q` | 3 passed |
| 研究 Python | `pytest tests/test_c3a_research_run.py tests/test_c3b_research_ui.py tests/test_c3c_research_figures.py tests/test_c3c_research_report.py tests/test_c3c_research_visualization.py tests/test_navigation_planning_integration.py tests/test_round40a_navigation_integrity_v2.py -q` | 全 passed |
| 前端 | `node tests/c3a_research_run.test.mjs`、`c3b_research_ui.test.mjs`、`c3c_research_map_labels.test.mjs` | PASS |
| 六图重渲染 | `python-qgis-ltr.bat tools/render_r0005_research_figures.py --run-root <root> --run-id <run> --output <out> --sources <map_sources>` | 6 张 A4/300 DPI |
| 同源 | 比对 `figures_index.json` / 报告头 / 3D 载荷 / manifest 身份 | 六处一致 |
| 重启 | 停服 → 重启 → 比对冻结字段 | `no_solver_executed=true` |

---

## 6. 不得重定义

- 不把“合同测试通过”单独称为 V5。
- 不把“fresh compute 成功”重新定义为 V1 之外的新版本。
- 不把“QGIS 图能打开”替代 V4 的同源判据。
- V1–V4 的编号、名称与判据在本文件锁定；后续新增只能加子测试组，不能改定义。
