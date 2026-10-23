# R7-D19 通信半径修正与导航服务监测名称统一

基线 1e3973b，既有 codex/r7d13-ab-integration 工作树。正式 ProjectState、主工作区和历史 artifact 未修改。原有5个影像相关未提交文件不纳入提交。

## 变更与历史边界

通信 surface policy 的陆/沿海/海上统一为2000m，策略身份 r7d19-comm-2km-v1。原始参考是“4.9GHz航线站间距约4km”，2km半径为研究假设，不是实测覆盖保证。设备目录新增 COMM-BASELINE-2KM；旧 COMM-BASELINE-4KM 项目设备、安装记录及结果不自动迁移。新增 zhoushan_v2.json（ZH-CNS-EA-V2 / r7d19-v2）；zhoushan_v1.json 保持原字节。P16 场景校验按版本识别设备型号，旧场景仍识别旧型号。

目录中显式半径、几何、设备ID与 parameter_metadata 进入既有输入指纹；场景ID/版本进入既有场景指纹。几何半径通过 effective_radius_m / index_max_range_m 消费，能力计算指纹包含整个 device_catalog，P16影响矩阵身份包含来源源码与P14/P15指纹。新旧输入不复用身份；未手工改写正式失效状态。

二维覆盖圈标签读取实际设备半径，历史4km图仍显示4km。报告模板与三维默认半径引用同一权威常量，三维斜距几何与安装高程门禁保持原契约。用户可见“导航完好性监视”“导航完整性监测”“导航监测”统一为“导航服务监测”，service key/API保持兼容；真实完整性/精度字段和门禁未改。

## 可靠性参考证据

MTBF≥150000h、MTTR≤1h可作为设备参考声明登记，但本轮未发现匹配原文页码/设备型号的可核实证据，登记为 pending_source_verification，computational_use=false。不得把不等式当精确值或直接换算系统可用性。新基线 MTBF/MTTR/availability 保持缺失；修正 normalize_device 强制缺失MTBF转数值的错误，可靠性保持 missing_data。

## D8与D18来源差异

只读核验见 artifact_identity.json：D8 grid_risk_v2 指纹 f1d60794…，D18 api_risk2 指纹7a74c819…；policy相同，输入不同。D18脚本从独立副本 api_risk2.json 消费已冻结网格风险，并没有加载D8研究 grid_risk_v2.json。D18覆盖0.949183265、未知477.901352097m、积分105.208100982，与D8报告修正前值逐项一致。D8修正后 ground 100%、481段resolved、积分208.811602280。结论是旧artifact消费链，不能把旧数值包装成新修正结果；不是人口需重新采样。D8的100%是风险消费解析率，不是原始人口观测面积100%。原件与历史结论全部保留。

## 受影响链与最小重算范围

1. 在独立研究副本显式登记/选择新2km目录设备及V2场景；保留旧目录设备和旧结果对照。仅更新代码不会自动替换现有ProjectState目录。
2. C几何覆盖 → 静态能力 → P14走廊 → P15缺口 → P16候选/影响矩阵/规划 → P17连续服务 → 二维/三维图件和报告。混合C/S任务因整体输入身份变化需新任务，但S数学、RID陆2km/海5km、Radar语义不变。
3. 风险最小处理：验证D8 artifact与当前路线/网格/政策身份后，在独立副本显式绑定D8网格风险，重跑既有航路风险画像/复验；不重采人口，不把D8结果直接升级为正式结果。建筑4tCa7及air_traffic未知独立保留。
4. 不需要重建地形/建筑/人口、重跑Theta*或PCF。固定1000m走廊不变。若将来调整路线，再按正式服务失效链执行。

本轮 Theta*/P14–P17/P16 真实规划运行次数均为0。

## 定向验证

Python第一组190 passed：test_r7d19_baseline、test_cns_communication_rid_services、test_r7d8_population_consumption、test_round38_w1a_fixed_corridor、test_round38_w1b_fixed_corridor、test_round38_t3d4_3d_cns_integration、test_map_figures_cns_templates、test_navigation_monitoring_simulation、test_round38_p17_navigation_unknown_time。

第二组57 passed：test_cns_reliability_service_state、test_round32i_project_save_reliability、test_r7b_assumption_sites_3d、test_round38_fig_band_and_currentness、test_navigation_monitoring_simulation_api。

Node：round38_n2_navigation_ui 41 passed、round38_t3d4_3d_cns_frontend 20 passed、map_figures_cns_frontend 6 passed。更改JS入口 node --check 通过。配置JSON逐项解析及UTF-8无BOM检查通过（仓库不存在约定的tools/remove_config_bom.py，执行了等价只读检查）。未执行QGIS真实HTTP/渲染验收。

提交与发布：仅白名单文件本地提交，随后运行同D18规则的audit_release.py审核；审核通过才快进推送既有开发分支，不合并main，不reset/clean。
