# AGENTS.md — CNS-PLANNER 开发约定

## 项目边界

- 本项目是 Python + QGIS/GDAL + 原生 JavaScript ES Modules 的本地低空航路与 CNS 规划工作台；前端无构建步骤。
- 当前为 Phase 4 冻结版 Release Candidate：只做预验收、缺陷修复、性能/UI 收敛和文档对齐，不新增算法、不进入 Phase 5。
- schema-v2 `ProjectState` 是权威状态；大结果外置为 artifact，状态只保留摘要、指纹和 locator。
- 面向用户的文本使用简体中文；代码、API、schema 和算法 ID 保持英文。

## 按需读取

不要在每轮通读项目文档。先 `rg` 定位，再按任务只读相关章节：

| 任务 | 必要入口 |
|---|---|
| 项目定位、六步流程、启动或测试 | `README.md` 对应章节 |
| 判断应查哪份文档、写入者或硬约束 | `AI_DEV_CONTEXT.md` 的文档地图/权威写入者/硬约束 |
| canonical DAG、成熟度、authority gate、ProjectState | `PHASE4_TARGET_ARCHITECTURE.md` 对应章节 |
| 航路 candidate/validation/adoption | `docs/route_planner_v3_architecture.md`、`cns_planner/layered_route_planner/`、`cns_planner/domain/layered_route.py` |
| 真实空间数据 | `cns_planner/gis/`、`cns_planner/data/registry.py` |
| CNS 能力、覆盖、走廊、站址、评审或报告 | 对应 `cns_planner/application/*_service.py` 与 `docs/15-Phase4-Final-Preacceptance.md` |
| 历史决策原因或字段级旧契约 | 用 `rg` 定位 `docs/decisions/2026-09-ai-dev-context-full-archive.md`，禁止整读 |

## 长期不变量

1. 依赖方向为 `API → Application → Domain/Algorithm`。算法不 import QGIS/GDAL、不读文件、不写 `ProjectState`；空间数据由 `gis/` 适配。
2. 冻结算法语义只能因 bug 修复、真实数据适配、性能或 UI 调整而改；语义变更必须显式版本化并更新 fingerprint/staleness 与回归。
3. 缺失不等于 0，`unknown` 不等于 `pass`，未评估不等于通过；不得猜默认值或静默 coarsen。
4. 适飞空域仅为 `display_only_reference_layer`，不得进入 risk、search、readiness、fingerprint、staleness 或 CNS。
5. candidate/proposal/recommendation/preview 不具权威性；只有显式 Adopt/Confirm/Apply 才能写正式容器。
6. 失效传播只由 `cns_planner/application/invalidation_service.py` 定义，必须单向且最小。
7. 改状态前用 `rg` 确认 `cns_planner/application/` 中的 production writer；不得新增第二写入者或绕过 service 直改 state。

## 低 Token 开发闭环

1. 用 `rg`/`rg --files` 精确定位函数、契约、调用点和测试；不要全仓探索生成目录。
2. 一次读取足够的目标片段，一次修改一个完整逻辑单元；同一文件不要重复整读。
3. 保留工作区现有改动，只触碰任务范围；批量编辑前先检查 `git diff`。
4. 默认只跑受影响的定向测试，输出使用 `-q -x --tb=short` 并截尾；不在开发循环运行全量 pytest。
5. 前端无构建步骤；改动 JS 后运行相关 `node tests/<file>` 和受影响入口的 `node --check`。不要用 `node --test tests/*.test.mjs`。
6. 交付前检查 `git diff --check` 和任务范围内的 diff；只有用户明确要求时才 commit/push。

Python 固定使用：

```powershell
D:\tools\anaconda\python.exe -m pytest -q -x --tb=short -p no:cacheprovider tests/test_<相关>.py
```

受限沙箱出现大量 pytest setup error 时，先按 `README.md` §16 配置工作区内的 `TEMP`/`--basetemp`，不要把权限错误误判为产品缺陷。真实 QGIS HTTP 测试默认跳过，除非 8765 服务已就绪且显式启用。

## 本机与文件安全

- 唯一工作区是 `D:\Projects\CNS-PLANNER`；不要混用其他仓库副本。
- 不搜索 `_diag/`、`_diag2/`、`_out/`、`_output/`、`_work/`、`_vbase*/`、`_round*/`、`_bt_sim*/`、`b9tmp/`、`b9_e2e_runs/`、`tmp_b9/`、`outputs/`、`projects/`、`_cscan/`、`_migration_smoke/`、`_tmproot/`、`_runtime*/`、`.pytest_cache/`、`.pytest_tmp/`、`.test_runtime/`。
- JSON/YAML 配置必须 UTF-8 无 BOM；PowerShell 5.1 的 `.ps1` 若含中文则保留 BOM。配置变更后运行 `python tools/remove_config_bom.py --check`。
- **这条容易记反，务必看清**：DSH 对 `profiles/desktop/package.json` 等配置**直接 `JSON.parse`、不去 BOM**，开头带 `EF BB BF` 会在第 0 个字符崩溃、表现为**软件起不来**（2026-10-05 真实发生过，crash 日志停在 `readProfileManifest`）；而 PowerShell 5.1 的 `Set-Content -Encoding UTF8` / `Out-File -Encoding UTF8` **会写入 BOM**。
  - 写 JSON/YAML 一律用 `[System.IO.File]::WriteAllText($p, $text, (New-Object System.Text.UTF8Encoding($false)))`；读取时先 `TrimStart([char]0xFEFF)`。
  - **反过来**：`.ps1` 脚本本身**必须**带 BOM，否则 `powershell.exe`(5.1) 按 ANSI 解码中文注释与字符串，引号错乱并报假的"缺少右 }"。
