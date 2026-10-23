# R7-D20 阶段检查点（未完成真实保障评价）

仅使用 D:/Projects/_cns_acceptance/r7d20/R0005-rev525 独立副本。来源为 D18 的 R0005-rev524 文件夹，实际 project.revision 为525。正式状态和主工作区未写入；5项既有影像修改不纳入检查点。

已核验加载水合后的8008格 grid 与人口属性逐项等于 D8 所用 D7 输入，risk_policy_v2相同。D8 artifact身份沿用D19已记录SHA256 d0f1a7df44560986c91170543184f88980e1cc6c9127f6a36069a6f05d703420；本轮按既有 operational-route-clearance-v2 接口只读消费，不绕过RiskFrameworkV2Service写入状态，不重跑网格风险或人口采样。

一次D4复验含一次风险画像：R0005 14顶点，ALT-100；地面风险 coverage=1、unresolved_length_m=0、exposure_index_m=208.811602280。旧D18对应0.949183265、477.901352097m、105.208100982。地形passed，铁塔passed，建筑unresolved 1项；air_traffic覆盖0、未解析9404.408922465m。总状态unknown，research_only=true，operational_authorization=false。详见existing_route_validation_summary.json。固定1000m目标带未改。未执行Step04采纳。

发现D19名称替换使历史required_cns中精确的“导航完整性监测”政策来源被校验器拒绝。最小兼容修复仅接受两种精确历史文案，归一为“导航服务监测”；不改变两端各1套设备、10km关联范围或其他门禁。新增测试先失败后修复通过；其他来源仍拒绝。

## 等待设施基线选择

D18冻结状态实际保留EXISTING-SYN-001，设备C-PRIMARY-SYN为15km研究假设，并非COMM-BASELINE-4KM或COMM-BASELINE-2KM。直接导入2km目录不会替换此内嵌设备。若继续称其为“真实2km保障基线”，会混入合成设施能力；擅自删除它同样会改变用户未明确指定的设施基线。已向用户询问：保留历史对照而新基线采用无已确认设施，或提供真实设施清单。未取得答复前不执行依赖该选择的计算。

COMM-BASELINE-2KM与ZH-CNS-EA-V2为待执行显式选择；旧4km模型/结果保持。C覆盖、静态能力、P14、P15有效新批次均0；连续服务2.5与P17均未执行，不能判断其可计算。P16运行/动作应用为0，不能复用13项历史动作或断言5项C动作清零。尚无新2km缺口数、失保连续段或最小站址数量，不能将历史2727缺口/9404.94m冒充新结果。二维/三维截图尚未执行。

下一步：确认设施基线后，走既有service选择2km目录与V2场景、依次执行唯一批次C覆盖/静态能力/P14/P15，按当前性与依赖门禁决定连续服务和P17；输出新旧C/S/N对照与必要截图，再给P16最小优化建议。保持RID规则、FC30导航unknown及建筑unknown；不启动大规模P16。

定向回归：test_r7d20_legacy_navigation、test_r7d19_baseline、test_r7d8_population_consumption，共25 passed。公开发布暂不执行，完整D20验收及发布审核尚未完成。
