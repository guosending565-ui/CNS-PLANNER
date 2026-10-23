# R7-D20 新2km通信基线下R0005保障评价

结论：独立验收副本的一次有效批次完成；当前C/S未保障、N缺证，连续服务2.5为unacceptable。业务验收未通过，未启动P16规划。

## 冻结、授权与身份

沿用codex/r7d13-ab-integration工作树。仅D:/Projects/_cns_acceptance/r7d20/R0005-rev525允许业务写入。主工作区、正式ProjectState、5项既有影像修改未纳入本轮改动。R0005的14顶点、ALT-100、固定1000m目标带、网格/人口/地形/建筑来源、required_cns与373座铁塔保持。批次前后对operational_routes/spatial_3d/grid/grid_attributes/required_cns/towers/candidate_sites做摘要一致性断言并通过。

用户续授权明确：已确认既有设施0项；EXISTING-SYN-001与15km研究参数仅作历史对照；373塔只是候选宿主，不能算已装provider。历史相关状态与结果完整封存于验收目录historical_before_2km.json，D18源副本保留。零设施通过CNSInputService.import_existing与set_existing_baseline唯一写入者声明；非手改state。

设备显式启用COMM-BASELINE-2KM、旧COMM-BASELINE-4KM禁用并保留历史记录；选定ZH-CNS-EA-V2 / r7d19-v2，完整版本配置及内容SHA记录于selection.json。场景为候选规划选择，未运行其候选生成/P16；本次P14/P15输入指纹由既有目录/设施/策略契约产生，不能冒称场景ID直接进入了P14指纹。RID陆2km、海5km及物理站址独立性规则不变。

## 新旧C/S/N结果

| 口径 | C满足/确认缺口/unknown | S满足/确认缺口/unknown | N满足/确认缺口/unknown |
|---|---|---|---|
| 历史P15（原样封存） | 0/2727/0 | 0/2727/0 | 0/0/2727 |
| 新2km零设施（三层总计） | 0/2727/0 | 0/2727/0 | 0/0/2727 |
| 新2km ALT-100目标层 | 0/909/0 | 0/909/0 | 0/0/909 |

P14既有契约评价80/100/150m三层各909格，共2727体素。ALT-100切片为909格（land174、coastal_uncertain26、sea709）；本轮未改变高度层输入以制造另一次计算。新P7几何覆盖状态missing_data、静态能力unknown，P14/P15均业务failed；不能把P7缺数据转成pass。P14在零provider证据下将C/S缺口判为confirmed_deficit，N独立保留unknown。

C/S连续确认缺口各1段：0.000–9404.937615m，最长9404.937615m；N不存在可宣称的“零失保”，其0个确认缺口段对应全程缺证。上述为空间体素保守纵向投影，非实测无线失保轨迹或正式连续性概率。风险画像路径采样长度9404.408922m与走廊投影长度使用不同几何口径，不强行抹平。

旧P14指纹b070276bfb6a628564b59b2b112d6e9dedd5be9626e31a21f1f3a76e3134ae4a；新7b47a6d0977930f08e0cea6be9f43b48fb5f5b6d77b62818c21c0710127752f7。旧P15为952103e3e98bb2d3056606d4aa139d94ae3679fc03f92399e66852c64cd977e2；新f995d0456f8d528cdb672dd2bdd0229fc6a22b55df154494152cc6762b2a2960。完整摘要见comparison.json。

三种身份不混用：15km合成设施存在于历史输入；历史4km目录/研究结果保留；新2km基线为零设施且新指纹。历史P14/P15与冻结合成设施输入并非同一次受控半径实验，因此历史和新缺口数字相同不能证明2km与4km保障等效。也不能由此延用旧5项C动作“清零”的结论。

## 风险与D4

D8与水合后的当前8008格grid、population明细逐项相等，risk_policy_v2相同；D8 SHA256 d0f1a7df44560986c91170543184f88980e1cc6c9127f6a36069a6f05d703420本轮复核一致。按既有operational-route-clearance-v2契约只读消费artifact，执行一次D4内风险画像，不绕过RiskFrameworkV2Service写正式风险状态，不重采人口、不重跑网格风险或Theta*。

地面风险解析覆盖由旧D18的0.949183265变为1，未知477.901352097m变为0，积分105.208100982变为208.811602280。地形与铁塔passed；建筑4tCa7 unresolved 1项；air_traffic未解析9404.408922465m。D4整体unknown，research_only=true，operational_authorization=false；正式飞行门禁不变，未执行Step04 Adopt。

## 连续服务与P17

P14/P15 current且业务failed可供生产连续服务入口消费；旧P16按既有失效链成为stale。入口确认无有效方案投影/无applied_action_ids后，仅执行一次baseline连续服务2.5，status与baseline_status均unacceptable，post_plan_status为空。C/S全失保及冗余降级段按已选FC30速度15m/s折算626.995841s，C全失保阈值3s，超限；保留该阈值的既有工程证据来源。FC30导航缺证不被总状态覆盖为通过。

独立protection_envelope（旧P17航路保护区）未计算：response_time_budgets和encounter_scenarios均为空，缺少真实前置；不能借连续服务结果强行生成保护区。三维因此继续标注目标带为非权威历史预览。

## 候选宿主与下一次P16最小优化建议

373塔保留为候选宿主：Viewer来源显示77座塔顶已解析但未权威确认，296座塔顶未解析；不等于373座安装合格站。新基线未生成新候选方案、不重用旧影响矩阵/13项动作，不能报告新“最少站数”。下一次最小工作应先限定R0005/ALT-100/1000m带，对这373塔及已有真实起降点做安装资格、高度和独立物理站址过滤，再用2km模型建立新的C/RID影响矩阵；以C/S全程缺口为目标先做小范围可行性筛选。陆地/海岸C与RID需2个不同物理站址、海上需1个；同塔多设备不增加独立重数。导航服务监测维持端点策略，FC30导航unknown须补实证，不能靠增加C站点替代。未授权开启大规模P16。

## 展示、缺陷与验证

8796为D20副本独立展示服务。二维P15结果和三维固定目标带截图保存在验收目录screenshot_2d.png、screenshot_3d.png。浏览器视口仅约326px宽，截图主要展示控制/证据面板，不作为高质量全幅地形图验收；三维清晰保留stale与缺证提示，未伪造已确认覆盖。

修复两处本轮真实阻塞：历史导航政策文案兼容（仅两种精确旧名称，其他门禁不变）；只读Profile卡片忽略禁用旧4km设备，显示已启用2km设备。均先失败回归后修复。Python45项通过（D20旧名、D19、连续服务、导航unknown时长）；Node48项通过及新增2项active-profile断言通过，JS node --check通过。前阶段另有D8消费定向回归通过。有效计算批次：P7/静态能力/P14/P15各1、连续服务1；D4含风险画像1；人口/Theta*/P16均0。

本轮结束后仅做本地检查点和公开发布审核，审核通过才普通快进推送既有dev/r7d17-integration；不合并main、不reset/clean。
