# R7-D17 集成源码发布状态

- **分支**：`dev/r7d17-integration`
- **基线**：R7-D16 最终 HEAD `4717037738cb8ddd0bf5b9eb570506f8e1b1be0b`
  （`codex/r7d13-ab-integration`，位于 `origin/main` `89b008d` 之上 5 个提交）
- **发布审核**：`docs/r7d17/release_audit.json`，verdict = **pass**
  （白名单、禁用路径、密钥、可达历史、许可证、归档、大文件、BOM、tree mode 九项）
- **审核范围**：888 个跟踪文件、约 26.45 MB 载荷；相对 `origin/main` 新增/修改 278 个源码/测试/文档文件。
- **未包含**：`projects/`、`research/`、`outputs/`、空间数据（`*.tif/*.gpkg/*.qgz`）、
  P16 原件、D9 研究历史、历史 bundle；D9 提交 `2b953bf` 不在本分支祖先链中。
- **未执行**：force push、合并 `main`、删除历史工作树、修改正式 ProjectState。

## 状态声明

**集成完成；真实业务验收待完成。**

本分支的源码与测试集成（R7-D13–R7-D16 A/B/C/D/E 批）已完成并通过定向回归；
真实 R0005 项目的 Step01–06、二维/三维、P14–P17 历史结果与报告尚未完成本轮正式验收。
本分支不是 Phase 4 正式发布许可，也不代表真实业务结果已被重新计算或认证。

## 复核方式

```powershell
git clone --branch dev/r7d17-integration https://github.com/guosending565-ui/CNS-PLANNER.git
cd CNS-PLANNER
D:\tools\anaconda\python.exe docs\r7d17\audit_release.py
```

审核脚本只读 Git 对象与工作树，输出 `docs/r7d17/release_audit.json`。

## 已知条件

- 仓库自身尚未声明根 `LICENSE`（这是仓库治理项，不阻塞本轮源码分支推送）；
  随源码分发的 CesiumJS 1.126 使用 Apache-2.0，`LICENSE.md` 与官方标签原文
  逐字节一致（SHA-256 `3111d10e…`）。
- 可达历史中的 `.test_runtime/pytest/...` 二进制样本属于**已公开** `origin/main`
  既有历史（14 个路径），本分支没有新增此类披露。
- 真实业务验收所需数据源为只读外部依赖，不进入 Git。
