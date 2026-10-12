---
name: cns-test-commands
description: CNS-PLANNER 的测试与检查命令规范（精确到文件、输出收敛、沙箱兼容），以及降低 token 消耗的运行纪律。当准备运行 pytest、node 前端测试、node --check、全量回归，或遇到测试出现大量伪 setup error / 沙箱写入失败 / 想确认"该跑哪些测试"时加载。
whenToUse: 需要执行或选择测试与语法检查命令时（开发循环、提交前、交付前全量回归）。
---

# 测试与检查命令规范

**核心原则：开发循环里只跑相关文件，绝不跑全量。** 本项目 157 个 `test_*.py`，全量输出是数万 token 的纯消耗；而每次模型往返都会重发整个上下文，所以"少跑、跑准、收窄输出"直接等于省钱。

## 1. Python（解释器固定，不要用裸 `python`）

```powershell
# 定向（开发循环默认）—— 只跑相关文件，失败即停，短回溯
D:\tools\anaconda\python.exe -m pytest -q -x --tb=short -p no:cacheprovider tests/test_<相关>.py

# 关键词批量：先收窄输出再提交给自己看
D:\tools\anaconda\python.exe -m pytest -q -k "layered or theta" 2>&1 | Select-Object -Last 25

# 全量（只在交付前跑一次，且必须收尾输出）
D:\tools\anaconda\python.exe -m pytest -q -p no:cacheprovider 2>&1 | Select-Object -Last 40
```

环境：Python 3.13.9（`D:\tools\anaconda\python.exe`），pytest 8.4.2，`pythonpath=["."]` 已在 `pyproject.toml` 配好。

**选哪些文件**：先用 `rg -l '<关键词>' tests/` 列出候选，再挑 1–3 个最相关的跑。不要凭猜测连跑五六个文件。

## 2. 前端（Node，仓库根目录执行）

```powershell
node tests/frontend_modules.test.mjs      # 前端纯函数 / 模块契约
node tests/grid_theme.test.js             # 网格主题

# 改过前端 JS 后的最低要求
node --check cns_planner/web/app.js
node --check cns_planner/web/js/main.js
```

> **禁止 `node --test <file>`** —— 受限沙箱下因 piped stdio EPERM 失败；`node <file>` 等价可用（`node:test` 在进程内运行）。

## 3. 沙箱注意（看到大面积伪失败先怀疑它，不是代码）

受限文件沙箱会拒绝写入 mode `0o700` 的目录 —— pytest 的 basetemp、每个 per-test 目录、以及 `tempfile.mkdtemp` 都是该 mode；同时 `%TEMP%` 位于 workspace 之外也不可写。所以受限沙箱内的完整可用形式是：

```powershell
$env:PYTHONPATH='outputs'
$env:TMP=$env:TEMP=$env:TMPDIR='D:\Projects\CNS-PLANNER\outputs\sys_tmp'
D:\tools\anaconda\python.exe -m pytest -q -rs -p no:cacheprovider -p dsh_tmp_plugin --basetemp=outputs/pytest_tmp
```

其中 `outputs/dsh_tmp_plugin.py` 是 gitignored 的沙箱兼容 shim，**只把 `0o700` 改成宽松 mode**，不改测试语义与产品行为。

**若当前会话已是完全权限（danger-full-access），通常不必加这一串**——先按 §1 的简单形式跑。

## 4. 真实 QGIS HTTP 集成

`tests/test_map_http.py` 默认跳过；需要 3080/8765 上有真实服务并设 `$env:CNS_MAP_TESTS=1`。不要为了"让它通过"去改这个测试。

## 5. 已知的、与本次改动无关的失败

历史上存在一项长期 known failure：`test_qgis_adapter_transforms_crs_filters_workspace_and_uses_spatial_index`（QgsSpatialIndex 测试替身签名问题）。判定"我的改动是否引入失败"时，先确认该失败是否本来就在。

## 6. 输出收敛纪律（务必照做）

- pytest 一律 `-q`（必要时 `-x`）+ `--tb=short`，并用 `Select-Object -Last N` 截尾；
- **禁止**把 pytest 完整输出原样贴进上下文；
- `rg` 命中过多时先加 `--include`/收窄 pattern，不要直接读整份结果文件；
- 同一命令**不要为了"确认一下"重复跑**；先看已有输出再决定是否需要新证据；
- 需要跑多个不相关检查时，把它们合并成**一条**命令（减少往返次数）。

## 7. 参考文档

- 开发协议与红线：`AGENTS.md`
- 各轮完整测试基线的历史数字（需要查证时再 grep）：`docs/decisions/2026-09-ai-dev-context-full-archive.md`
