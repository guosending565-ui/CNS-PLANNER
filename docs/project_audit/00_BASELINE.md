# R0 GitHub 审查快照基线

日期：2026-10-22（北京时间）。分支：`snapshot/2026-10-22-review`。基础提交：`9404d168cc46ca37aa3242f9b2451956b2c28760`，加本次最小航路发布资格修复、直接测试与审查文档。**这是供远程代码审查的完整源码快照，不是正式 RC 验收，不证明真实业务全通过。** 精确快照身份以该分支 `git rev-parse HEAD` 和 GitHub 远端同名 ref 为准；本文件不循环写入自身提交 SHA。

## 阅读入口与证据等级

- [系统架构、六步流程、模块状态与核心算法](01_STATUS_MAP.md)
- [数据来源、质量、校验值与依赖](02_DATA_QUALITY.md)
- [M0–M6 路线和开发优先级](03_ROADMAP.md)
- [AI 协作、止损、测试与学习规范](04_AI_WORKFLOW.md)
- [初次只读审查的逐文件差异及机器证据](05_EVIDENCE.md)

代码事实、定向测试、历史验收、工程估算、未知分开记录。六份文档由 R0 初次审查稿复制并更新适用范围；旧树不修改。历史证据不是此快照的当前端到端验收。

## 源码身份与最小移植

| 对象 | 本轮核查身份 | 用途 |
|---|---|---|
| 开发树 | `feat/p16-background-task`，`68e26742feafd8cfe67e3e429de8e078ca688bba`，102 tracked 修改 | 只读比较，不作为发布来源 |
| 基础提交 | `integration/round38-acceptance`，`9404d168cc46ca37aa3242f9b2451956b2c28760` | 由负责人指定；包含三维、来源门禁及条件研究代码 |
| 验收树 | 同一 HEAD，24 tracked 修改、8 untracked 测试 | 仅取下列资格修复，不整体合并 |
| 远端 main | `89b008da13bce36b1b2933b571dc4323016debfd` | GitHub API 与实际 git ls-remote 均已核对，不更改 |
| 历史预验收 | docs/15 记录 `30281644731602bf967ee44d866da470ecb4f2b0`，69 checks / 0 failed | 仅历史身份，不能覆盖后续三维/算法版本 |

基础提交相对开发树 HEAD 含 28 个后继提交、404 文件提交级差异；开发树工作区又有独立未提交修改。不能批量覆盖。本地 main 仍为 `4c7a0a0…`，落后远端 15 提交，不代表 GitHub main。

| 移植文件 | 来源与审查范围 |
|---|---|
| `cns_planner/application/layered_operational_adoption_service.py` | 与验收树该文件逐字节一致；精确解析候选身份/指纹/几何，以 live candidate 的显式 true 资格决定发布 |
| `cns_planner/application/layered_route_validation_service.py` | 与验收树该文件逐字节一致；验证结果保留原 verdict，只补资格不足提示，不授予采纳资格 |
| `tests/test_layered_route_validation_adoption.py` | 移植 fixture 的显式 true 与直接资格/警告回归；不带依赖未提交 Theta* 测试语义的最后一个 provisional 生成测试 |
| `tests/test_r15_candidate_identity.py` | 原有身份、失效、几何与 Preview/Apply 测试；仅追加六种不经过 Preview 直接 Apply 的权威资格用例 |

未知资格（缺失、None、字符串 unknown）都不能通过。候选/验证失效不能通过。只有显式 true 且原有来源、风险画像、连续验证等门禁同时满足才允许发布。请求传入 `operational_adoption_allowed=true` 不能覆盖 live candidate。后端路径：`/api/layered-operational-adoptions/apply` → `WorkflowService.apply_layered_operational_adoption` → `LayeredOperationalAdoptionService.apply` → 每次重新 `projection` → 原生产 `_apply_working` 与 authority guard。无需用户先点击 Preview；原写入者、失效传播及算法数学不变。

## 失败复现与回归证据

干净 `9404d16` 上，移植的既有身份测试首先复现：false 候选的 `publication_allowed` 仍为 true，**1 failed / 1 passed，-x 停止**。应用两服务补丁后，直接发布及原回归为 **101 passed**：

```powershell
# 在此审查分支的干净工作树执行；TEMP/--basetemp 使用可写目录。
D:\tools\anaconda\python.exe -m pytest -q -x --tb=short -p no:cacheprovider tests/test_r15_candidate_identity.py tests/test_layered_route_validation_adoption.py tests/test_algorithm_registry.py tests/test_cns_service_capability.py
```

本轮实际 basetemp：`D:/Projects/cns-r0-audit-probes/r0-final-tests`，`PYTHONDONTWRITEBYTECODE=1`，PYTHONPATH 指向隔离工作树。未启动真实 QGIS HTTP，未执行昂贵 Theta*/P14/P15/P16 重算，未对正式 ProjectState 执行 Apply。true 的真实服务发布/保存覆盖仅使用 pytest 临时项目；独立边界测试用 mock 验证不得调用生产写入。

算法快照仍为 Theta* 2.1、P14 1.4、P15 1.3、P16 2.3；13 类型、10 个默认组合可解析。本补丁不改变算法版本或数学。

## ProjectState、S3/S4 与现实边界

初次审查只读核对的已知文件：

| 对象 | schema / revision | SHA256 |
|---|---|---|
| 开发树默认 current_project | 2 / 21 | `e8548f28c70643cb8049f8368d61faa4f83e41b7d8a1dd628ddbbf2884a95f00` |
| 验收树 S4A `copy/current_project.json` | 2 / 523 | `dc81637d3a713d2398ea256c4143e654d7680d387295cfacea7ed097bd4d00a3` |
| 验收树 S4A `p16_result.json` | P16 2.3 / proposal_ready | `4cf078440230c722d0f18c7e54e7eec0812d386aff41ed8269cd60b074401dac` |

来源位置为负责人验收树的 `projects/round38_cns_experiment_b/rounds/S4A/`；不复制内容。两份 state 的 project_id 相同不代表同一个有效运行基线。rev523 是实验副本，未被提升为正式权威 state；与裸基础提交的精确执行源码/指纹仍未闭合。

- S3 为连续失保归因；S4 定位了实验候选 373→20 的裁剪来源。该进度来自原工作区 docs/16–18 记录，本快照保留摘要，不声称本轮重验每个历史产物。
- S4-A1 原始结果已直接读取：746 候选动作、13 选中、219 残余目标；停止原因 `coverage_objectives_met_but_continuous_service_unacceptable`。记录中的 S 最长连续失保 590.60 m 未在本轮独立重算。
- S4-A2 的 Adopt/Apply 后正式 P17 评价没有完成证据；不将 hypothetical 收益当已安装/正式服务。
- 131 的 ALT-150 几何命中却无 confirmed 进展仍是待最小复现疑点；不预判正常门禁或产品 bug。
- 建筑覆盖/高度、C3、塔安装原点/物理挂载、设备/机载参数和 provider 独立性等缺证仍保留。未知不补零或改 pass。

## 尚未纳入的成果与发布边界

未移植 P16 七字段外置恢复、P16 缓存/GC 性能修订、报告锁与披露、导出/HTML、迁移与 owned restore、Theta* 新修订、导航监测 v2、研究 evaluator/worker 的未提交变更，以及开发树制图/数据语义等其他差异。05 的逐文件表是**初次审查时相对基础提交**的清单；其中本节列出的四个直接移植文件已有本轮后续处理，不能再全部视作未集成。其余仍待单独审查，不影响此次“审查快照”的发布，不冒称完整 RC。

本次只 stage 明确的两个源码文件、两个测试文件、六份 Markdown 和 README 最小入口。原始数据、state、artifact、机器配置、真实 Key/Token/密码和受限文件不加入本次提交。历史分支、开发树和验收树均保留；不修改 main、不 reset/clean/rebase、不 force push。

历史安全初筛复用精确基础提交的 3,525 个唯一 blob / 294,172,409 字节记录：所列高置信凭据格式无命中，无 >10 MiB blob。发布前另查新增文件、实际暂存清单、配置凭据候选及将上传历史的可疑文件路径；这些检查不声称穷尽所有未知秘密。大型数据仅保留非敏感登记/hash和获取方式，授权仍需负责人确认。

阶段口径、正式验收输入以及未合入成果的后续取舍仍待负责人决定；本轮目标止于审查快照。提交后核对远端同名 ref 与本地完整 SHA，结果在最终交付报告列出。

## 独立复核与交付检查

独立只读代码复核结论：可提交最小移植，未发现后端发布绕过或数学语义变化。非阻断遗留：readiness提示尚未共用新增门禁，可能显示eligible而Preview/Apply拒绝；已登记，不扩大本次补丁。101项测试通过后仅修改文档，production源码未再变化。新增文件通过UTF-8/BOM、相对链接、围栏、暂存diff及体积检查；不含运行state或大数据。
