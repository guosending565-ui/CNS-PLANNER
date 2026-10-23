# R7-D13 A/B 集成检查点（2026-10-23）

基线 `89b008da`，来源 D9 `2b953bfcbf9ccc74e5c07ad582d67324c0a164ca`，共同祖先 `68e2674`。本地 main 仍为 `4c7a0a0`，没有移动 main。分支 `codex/r7d13-ab-integration`。本报告是局部集成验收，不是 D9 全量发布或 C/D/E 验收。

## 冲突处理

D12 A批8文件、B批5文件，共13文件（实际三方文本冲突40段）已处理。采用 `git merge-file` 逐文件三方合并，再逐段协调；没有整文件 ours/theirs 覆盖。双方主线保留：main 的 P15/Radar/航路任务注册、发布writer和单次请求适配；D9 的 V2只读结果artifact引用、source audit任务、冻结算法选择、来源内容身份门禁；main 的快照 read-pass 隔离和有界投影；D9 的运行期绑定缓存失效、D4工程演示与正式采纳分离及预览指纹。

原13项路径：tasks/handlers.py、service.py、task_specs.py；web/js/api/client.js、tasks.js；tests/b6x_heavy_task_frontend.test.mjs、round31a_p16_background_task.test.mjs、test_round31a_p16_background_task.py；application/app_context.py、workflow_service.py、layered_operational_adoption_service.py；web/js/workflow/step01_project.js；tests/layered_validation_adoption_frontend.test.mjs。源码路径均位于cns_planner下。

无文本冲突依赖也已实际审查和集成，路径及内容SHA见allowlist.json。重点包括api/server.py项目切换锁、project_state/project_directory、task_spec/task_input/worker/task_store、采纳前端/领域、建筑证据门禁/连续验证器、只读来源加载及land-mask审计身份。api/router.py仅合入B政策端点和A任务来源绑定，E其余端点装配仍待继续。

C/D/E耦合段不在本轮提前装配：固定走廊几何注入、共享站址生产worker接线、PCF新增输入、GIS高度列/LOS和三维服务保持待办；这些生产计算路径继续使用main已配套实现。D9对应逻辑仍完整保存在离线bundle中，后续按函数接回，不得误以为D9全量已集成。新增V2算法源码只作为既有D9任务依赖带入，未调用、未改算法语义。

## 最终验证

Python定向144通过：
- 保存/快照隔离/空白项目/只读打开/显式采纳/D4：84。
- main有界投影和真实probe子进程任务运行时：30（排除corridor_async、p14_protected、synchronous_endpoints）。
- immutable task input：9（snapshot_store、resolver_reports、algorithm_identity、missing/corrupt/tampered snapshot、restart_can_read、async_http_submit）。
- 项目切换API并发及P16仅提交时版本冻结：9。
- V2来源绑定、参考设施不可用/来源变化的计算前拒绝：6；不执行V2 runner。

独立审查发现并修复来源审计worker与main GIS检查器回调签名不兼容、几何事实探测函数缺失。三方合并source_inspection.py后，新增真实SQLite/Shapely GeoPackage审计和取消回调2项通过，受影响数据源健康4项通过；独立复核无其他确定P1/P2。

Node定向62通过：round32k2_task_request_once 3、round32k_task_refresh_once 3、b6x_heavy_task_frontend 14、f03_new_project_frontend 3、layered_validation_adoption_frontend 33、round31a_p16_background_task 6。所有变更JS均通过node --check；新增Python模块导入通过；git diff --check通过。JSON配置UTF-8无BOM字节检查通过；约定工具tools/remove_config_bom.py在基线和当前原工作区均不存在，故不能执行该脚本。

覆盖：parsed JSON与fetch两种注入单次POST；刷新相同task_id恢复；输入/算法身份冻结与损坏拒绝；save/open、revision、Windows保存重试及失败保留原件；请求间快照隔离、投影有界；明确confirm/preview fingerprint/replace intent；D4只读不生成candidate、几何指纹门禁、缺证不通过。测试使用临时目录或合成传输层，不连接8765真实QGIS服务，不作真实GIS端到端验收声明。

日志存于本机 `D:/Projects/_cns_backups/r7d13-final-tests.json`（含最终Node日志和一次已修复的依赖失败记录）、`r7d13-final-python.json`（其余最终Python通过日志）。84项最终通过见执行记录。

## 保护与恢复

D9完整离线备份：`D:/Projects/_cns_backups/r7d13-d9-2b953bfc.bundle`，git bundle verify通过。所有历史工作树保留；本轮仅在新集成树写源码和临时测试，不打开或修改正式ProjectState，没有reset/clean或删除工作树，没有push或合并main。候选依赖中不属于A/B的文件已移入本机备份r7d13-deferred-candidates，未提交。

此提交白名单只包含源码/版本化配置/测试/本报告；不含research、业务状态、空间数据或历史压缩包。它是本地集成白名单，不代表公开发布审核通过。D9完整历史禁止直接推送公共GitHub。后续必须从通过发布审查的白名单在main上建立干净远端分支，不能把D9历史作为远端父提交。

## C/D/E最小继续任务（需求不重查）

1. C（D12原5项）：逐函数协调采样/Radar；接回共享站址、PCF与相关生产worker输入；D2原生像元区间无缝隙、D7/D8人口unknown/overlap与投影守恒只做合成/只读门禁，不重算风险或P16。
2. D（原6项）：协调连续服务/评审/报告，保留main detection proxy与有效状态白名单，接D9固定1000m和N缺证unknown、managed_gaps；恢复外置字段及stop诊断，保持状态零污染。
3. E（原4项）：按端点装配router、main任务入口、Step05和三维；复用A/B单次POST与项目切换锁；按docs/19与3d-protected-assets验收19项三维交互。
4. E内执行既定PNG替换：`D:/浏览器下载内容/无人机停机坪图标.png`，SHA256 `37f83a99215bc49670b22ec5e6c700a583dc99c6d963536f133d25e40365e76b`；二维marker与三维billboard共享该PNG，检查透明边缘/锚点/缩放，不再调查图标需求。

A/B定向范围内无剩余已知失败；完整D9运行依赖、真实QGIS/三维验收、C/D/E15项及公开发布审查尚未完成。禁止把本检查点解释为全量集成完成。
