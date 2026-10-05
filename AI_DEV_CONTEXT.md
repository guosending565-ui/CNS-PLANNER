# CNS 规划系统开发上下文（决策索引）

> **这是每轮开发唯一需要完整读取的项目文档。**
> 它的作用是：让你知道"当前状态是什么、该去查哪份文档、哪些线不能碰"，而不是复述全部历史。
>
> 上一版 926 行的完整开发上下文（含 P1–P20 全部历史决策、字段级契约与逐轮测试基线）已归档为
> [`docs/decisions/2026-09-ai-dev-context-full-archive.md`](docs/decisions/2026-09-ai-dev-context-full-archive.md)。
> **只在需要查证某个历史决策的原因、字段语义或某轮基线时才打开它，并且用 grep 定位到段落再读**，不要整份读。

## 0. 按需文档地图

先查这里，再决定读哪份文件。绝大多数任务**不需要**读第二份文档。

| 你需要知道 | 打开 |
|---|---|
| 项目定位、六步工作流、启动方式、算法注册表、测试入口 | [`README.md`](README.md)（权威入口，先读它） |
| Phase 4 目标架构：六步契约、canonical DAG、成熟度与 authority gate | [`PHASE4_TARGET_ARCHITECTURE.md`](PHASE4_TARGET_ARCHITECTURE.md) |
| 按模块职责找文件、找某个能力属于哪个阶段（P1–P20） | 本文 → 再进归档的 §4「重要文件职责」 |
| 某个历史决策为什么这样定、字段级契约、某轮测试基线数字 | [`docs/decisions/2026-09-ai-dev-context-full-archive.md`](docs/decisions/2026-09-ai-dev-context-full-archive.md)（grep 定位） |
| V3-A/B/C/D 研究线与连续几何验证的完整语义 | [`docs/route_planner_v3_architecture.md`](docs/route_planner_v3_architecture.md) |
| CNS 技术基线（信号/链路/设备层面的依据） | [`docs/CNS_TECHNICAL_BASELINE.md`](docs/CNS_TECHNICAL_BASELINE.md) |
| Phase 4 冻结版预验收结论与已知限制 | [`docs/15-Phase4-Final-Preacceptance.md`](docs/15-Phase4-Final-Preacceptance.md) |
| 上一阶段的验收报告 / 已知限制与待办 | [`docs/13-Phase4-B3X验收报告.md`](docs/13-Phase4-B3X验收报告.md)、[`docs/14-Phase4-B4X验收报告.md`](docs/14-Phase4-B4X验收报告.md)、[`docs/10-Phase3已知限制与待办.md`](docs/10-Phase3已知限制与待办.md) |

## 1. 当前状态（一句话）

- 冻结基线：tag `phase4-final-freeze`；当前 `main` 是冻结基线**之上的预验收稳定化修复**——不含新算法、不改冻结数学。
- 阶段：**Phase 4 冻结版 Release Candidate**。只做预验收、收敛与文档对齐；**不引入新算法、不进入 Phase 5**。
- 正式工作流只有一套：README §3 的六步。`P1`–`P20` 只是开发历史编号，**不是**用户工作流。
- 生产主链：Step 3 `OD + 固定巡航高度 → LayeredRiskAwareThetaStarV2 → RouteRiskProfile → 独立连续验证 → Layered Operational Adoption → Operational Route`；Step 4–6 `Required CNS → Coverage3D → ServiceCapability → ServiceCorridor → CorridorGap → CorridorSitePlanning → PlanReview/Report`。

## 2. 权威写入者速查（改状态前必查）

同一个容器可能有多个合法写入者，不同阶段各写一部分。**动手前先用 30 秒确认你要改的容器由谁写**：

```powershell
# 例：查 operational_routes 的写入者
rg -n 'operational_routes' cns_planner/application
```

当前已知（可能随后续开发变化，以 grep 结果为准）：

| 容器 | 写入者 |
|---|---|
| `operational_routes` | `application/route_service.py`（场景/OD 写入）、`application/layered_operational_adoption_service.py`（正式发布/撤销）、`application/v3_operational_adoption_service.py`（V3-D 发布） |
| `grid_risk_v2` / `risk_policy_v2` | `application/risk_v2_service.py`（唯一） |
| `spatial_3d.route_3d_profiles` | `application/route_3d_profile_service.py`（唯一） |
| `layered_route_candidates` / 三类 layered 策略 | `application/layered_route_planner_service.py`（唯一，**不写** `operational_routes`） |
| `AltitudeLayer` / `RouteOperatingLayer` / 进离场程序 | `application/route_operating_layer_service.py`（唯一） |
| 一切失效传播 | `application/invalidation_service.py`（唯一权威） |

## 3. 六条硬约束（细节见同目录各文档，违反即返工）

1. **依赖方向不可逆**：API → Application → Domain/Algorithm。算法包不 import QGIS/GDAL、不读文件、不写 ProjectState。
2. **算法语义冻结**：Route Planning V1.0 frozen baseline 的搜索/启发/目标函数、`RouteRiskProfile` 数学、`LayeredRouteValidation` 与 `VerticalTransitionValidation` 判据与 verdict 映射、`Route3DProfile` 几何派生与 fail-closed 边界，**除 bug 修复 / 真实数据适配 / 性能与 UI 外不得改动**。任何语义变更必须显式版本化（升 `algorithm_version` / `geometry_version`、更新 fingerprint、旧结果 stale、完整回归），**不允许静默变更**。
3. **`unknown != 0`、`missing_data != 0`、`未评估 != 通过`、`validated route != safe route`、`confirmed CNS gap != route unsafe`、`regulatory not configured != passed`。** 缺数据就是缺数据，**绝不补 0、绝不猜默认值、绝不静默 coarsen**。
4. **适飞空域是 `display_only_reference_layer`**：不参与 risk、可行性、search、readiness、fingerprint、staleness 或 CNS。DATA-3 已 `retired/not_applicable_by_architecture_decision`。
5. **一个容器一个语义**：candidate / proposal / recommendation / projection 都不具权威性；只有显式人工确认（Adopt / Confirm / Apply）才升级为 authoritative。`proposal_only`、`operational_route=false`、`requires_closed_loop_validation` 这类标记不得被下游忽略。
6. **失效单向且最小**：只 stale 真正依赖该输入的下游；不得反向 stale 上游，不得为了省事调用过宽的失效入口。

## 4. 每轮开工前 3 步（省时省钱，务必执行）

1. 读本文 §0 表格，选定**唯一**需要的细节文档，并只读其中相关章节（用 `read` 的 `offset`/`limit` 或 `grep` 定位）。
2. `grep` 精确定位要改的函数/契约，确认权威写入者与失效入口；**不要**逐个文件通读、不要同一文件重复整读。
3. 运行**定向**测试（见 [`AGENTS.md`](AGENTS.md) 的测试命令），不要在开发循环里跑全量 `pytest tests/`。

## 5. 提交前最低验收

- 改动的定向测试通过；
- 相关 JS 语法检查通过（若改了前端）；
- 若涉及算法或状态语义，回归对应 characterization / 契约测试；
- 若改了 schema，确认旧项目 backfill 路径仍然可打开（additive 优先）。
