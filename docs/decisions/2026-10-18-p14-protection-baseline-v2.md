# P14 保护基线 v2：接受走廊 1.4 的版本化裁决

裁决日期：2026-10-18；任务 P0-B2-U；用户明确批准方案 1（版本化修订保护基线），不采用历史失败例外。

## 来源与修订边界

原四对象契约最初登记于 f991c7999ec9ed5608e53e97401d701d66fbe5ca；本次冲突的两个旧值有效来源为 69fd034ad461f8f2139b22727e8f55e194f88513。95e81285748eeb4cc4ae70f3c348171351373fbb 将 corridor 1.3 演进到 1.4（fixed-1000m-v1 固定宽度及共享米制几何身份），却未同步保护清单。当前接受基线为 84fc7d460603092e0305ada041878eb10726b5be，继承该历史冲突。

本次仅更新 tests/p14_protected_sha256.json 的两个值。四个原始保护对象及 B6R 完整断言均保留；不改测试实现、算法、测试内容、正式 ProjectState 或历史证据。保护清单本身不属于原 687 项或既有 703 项保护检查集合；另外两值原样保留。corridor/v1.py 的工作树仍属于 G4f HOLD，本次计算和隔离验证只使用接受 HEAD 的 blob，不采用其 dirty 内容。

计算口径严格复用现有 file_sha256：逐 1 MiB chunk 将 CRLF 转为 LF，再 SHA256；不去 BOM，不删除空格。Git blob OID 与内容 SHA256 分别记录。新值已从接受 HEAD 独立计算并与候选值核对。

|保护对象|旧 SHA256|v2 SHA256|处理|
|---|---|---|---|
|`cns_planner/algorithms/corridor/v1.py`|`a01a250aea542a3c99d2e217429f740d1e4885226abc85043637188b0e39191c`|`4c952e64f73fb4491451229d4712d7515571f406eb0766ca9a5d929a2d55b127`|版本化修订|
|`cns_planner/algorithms/coverage/geometric_3d.py`|`68f39a884ce173d11ae943a85bdb9f7b66daea2f124cdd8e5bc716a745c5f01b`|`68f39a884ce173d11ae943a85bdb9f7b66daea2f124cdd8e5bc716a745c5f01b`|保持原值|
|`cns_planner/algorithms/service_capability/v1.py`|`4632d2996e2fc59b8d21ec79e98676e1731d87106d53b2b600a9d176f2352a1c`|`4632d2996e2fc59b8d21ec79e98676e1731d87106d53b2b600a9d176f2352a1c`|保持原值|
|`tests/test_cns_corridor.py`|`6c95ea7fcb5ca0f152e215d7b90a7a8948260f32ab9e01a18db214f5dd0c6451`|`ce9bde7f3dc4f0c3028c95a45eafb3ab63e1960a5dde4912ade8ceb9cbe1fc60`|版本化修订|

## HEAD / 候选 A/B 归因证据

保留外置 P0-B2-T 原始证据：p0b2t_p14_baseline_20261018/ab_A.log、ab_B.log、ab_results.json、ab_contract_equivalence.json、prefix_integrity.json。A = 纯接受 HEAD；B = 同一 HEAD + 精确 21 项 G4bd 白名单。两者分别 1 failed in 0.22s / 0.21s，失败 node 均为 tests/test_phase4_b6r_immutable_task_input.py::test_p14_protected_files_are_byte_identical，同两个文件、同旧期望与实际新值；无 skip/xfail。CRLF/LF、BOM 和 G4f 均已排除。归类为 pre_existing_baseline_conflict；A/B 同失败不是跳过门禁的授权。

本轮重新核对历史关键 blob、原始保护集合及原失败日志 SHA。外置 p0b2u_p14_contract_20261018/03_p14_baseline_verified.json 保存完整对象身份、口径和日志指纹。双 AI 权威规范第 4.1 节已持久化归因规则，对 Codex 和 DeepSeek Harness 同等适用。

## 独立回归

独立 Windows 短路径隔离验证实测通过。P14 快照为接受 HEAD + 本次两文件；G4bd 预验证快照为接受 HEAD + 本次两文件 + 21 项精确候选文件。未复制后续组或 G4f。

- P14 / python：exit 0；日志 `D:\Projects\_cns_round38_global_recovery\p0b2u_p14_contract_20261018\test_logs\P14\python.txt`；SHA256 `f7f1032df3795b1f2813c7d950a00e5a105c5bcf7cc78a6db9d004ed76cad90d`。
- G4bd_preview / python：exit 0；日志 `D:\Projects\_cns_round38_global_recovery\p0b2u_p14_contract_20261018\test_logs\G4bd_preview\python.txt`；SHA256 `c46624676db4b034ea06f5f649f7180977908b52d35794ce8a1cf1b239dc0c4a`。
- G4bd_preview / node_0：exit 0；日志 `D:\Projects\_cns_round38_global_recovery\p0b2u_p14_contract_20261018\test_logs\G4bd_preview\node_0.txt`；SHA256 `91a6f69287ee9ab7a0ea21695d4ba4d8d0bb80e736403800c22576aac3bcafdc`。
- G4bd_preview / syntax_0：exit 0；日志 `D:\Projects\_cns_round38_global_recovery\p0b2u_p14_contract_20261018\test_logs\G4bd_preview\syntax_0.txt`；SHA256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。
- G4bd_preview / syntax_1：exit 0；日志 `D:\Projects\_cns_round38_global_recovery\p0b2u_p14_contract_20261018\test_logs\G4bd_preview\syntax_1.txt`；SHA256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。

完整命令、环境探针、返回码及统计保存在外置 05_p14_regression_results.md 与 test_logs/*/results.json。

## 证明范围

此次修订不代表走廊 1.4 整体业务正确性已经完全证明。不影响 G4f HOLD、M3 independent verification 未完成、M6 关闭、R2.3-A fixture-only、真实设备资格 unknown 或其他研究结论。不启服务、不执行真实 GIS/CNS 计算，不 push/merge。
