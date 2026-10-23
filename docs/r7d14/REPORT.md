# R7-D14 C批空间采样与Radar实际集成

基线 `e4e65f27`，分支 `codex/r7d13-ab-integration`；来源D9 `2b953bfc`，共同祖先 `68e2674`。执行位置为用户明确授权的既有R7-D13工作树。主工作区保持原状。

## 冲突及实际保留功能

D12原C批5文件、12处文本冲突已逐段协调：algorithm/radar_layout/v1（4）、radar_surveillance_layout_service（3）、layered_feasibility_adapter（1）、radar_layout_adapter（2）、radar_surveillance_layout.js（2）。没有整文件ours/theirs选择。无冲突依赖也通过三方合并与必要函数级移植接回；候选三方文件和日志保存在本机 `D:/Projects/_cns_backups/r7d14/`。

保留main的required-route先决条件、land-mask点分类与readiness、HTTP/worker共用事实provider、来源冻结、请求快照及A/B显式采纳边界。Radar-II仍只允许Stage A严格证明不可行后升级，保留Stage A诊断与成本披露；LOS证据不完整不能证明物理缺口或升级。陆域点分类没有采用D9的bbox外unknown替换；人口格级open-sea证据独立保持范围外unknown。

接回真实地形LOS和联合建筑LOS的只读服务、状态/诊断/分页端点与既有证据UI；生产划设入口仍按既定合同只允许terrain_los_mode=disabled。真实LOS是独立几何证据，不冒充探测概率、设备安装确认或安全通过。未请求的状态读取不打开DEM/GPKG、不写ProjectState或artifact。

接回GIS显式height_evidence_level/height_estimate_m、D2原生像元精确线段与圆角矩形影响区间（含折线、相切、重叠与NoData，不插值/重采样）。采样语义保留D2已版本化的v2身份。

共享站址投影接入WorkflowService及Radar/P16既有worker；来源仍为登记节点/起降点、真实塔、显式确认真实既有CNS，合成及未确认设施被排除。地面安装高度必须有显式证据，不能继承塔顶或默认挂高；main显式全塔源表规划原点仍保留估算身份。readiness与计算共用该策略。本次请求安装政策与显式provider完整透传；节点/设施安装证据变化进入Radar指纹。

PCF接回网格输入新鲜度及建筑覆盖unknown判定；提交/发布拒绝stale属性。worker完整透传terrain_dataset、towers、tower_clearance_policy，避免丢失地形基准及净空输入。未新增权威writer或失效传播入口。

D7面积overlap与D8风险消费修复接回：有效面积分配优先于缺失格心统计，整格密度与有效覆盖区诊断密度分离，stale/unknown不转0/pass。

## 验证与审查

最终日志见本机final-*.log。核心采样/人口/LOS/共享站址/PCF和新增接线测试、main Radar/D4/后台回归、人口高度语义、A/B输入冻结及PCF非搜路合同均通过，具体计数见verification.json。Node Radar、A/B单次请求/恢复/采纳、人口脚本通过，两个变更JS通过node --check；变更Python语法及git diff --check通过。

只读审查发现节点/设施安装证据未进入service指纹，已修复并添加高度/source/confirmed六种变更回归。严格塔顶LOS夹具显式关闭main源表规划估算策略，使山脊/未知测试保持其真实前提；该规划策略由main独立测试覆盖。人口前端脚本移除基线已失效的450行main.js大小断言，保留专用端点、hydrate与组件行为断言，未修改E批main.js。

一次扩展PCF测试误触发合成Theta*搜路测试并失败（no_path与candidate断言差异）。未调整Theta*算法或追查该非本批问题，随后明确排除4项搜路相关测试；18项PCF输入/纯判定合同通过。未执行真实耗时正式规划、GridRiskV2或P14–P17正式计算。未作真实QGIS端到端验收声明。tools/remove_config_bom.py在该基线不存在，改用逐字节BOM检查，新增JSON均UTF-8无BOM。

## 历史证据与恢复

只读核对R7-D8现存risk_profile.json及报告：R0005原14顶点/13折线边、481积分子段；ground=208.811602280，environment_obstacle=201.029042892，ground/environment各481 resolved，air_traffic仍481 unresolved。8008格和249格消费修复沿用历史证据，不重算、不修改原件。文件SHA256和字段定位见historical_evidence.json。

本提交白名单仅含源码、测试、工具和本报告；不含正式ProjectState、research业务数据、空间文件或历史压缩包。恢复标签 `checkpoint/r7d14-c-integration`；本地bundle路径 `D:/Projects/_cns_backups/r7d14-c-integration.bundle`（提交后建立并verify）。无push、无合并main、无reset/clean；主工作区保留所有原有改动。

## D批最小继续任务

仅继续D12原D批6文件：continuous_service/v1、domain/cns_continuous_service、report_service、map_figure_service、html_renderer及test_round29q_p17_radar_limitation。保留main detection proxy、有效状态白名单、per-route草稿披露；按函数接回D9固定1000m、N缺证unknown、managed_gaps门控、外置字段与stop诊断。优先合成/只读验证状态零污染，不重跑规划。E批与指定起降点PNG及其SHA要求保持原清单，不重新调查。
