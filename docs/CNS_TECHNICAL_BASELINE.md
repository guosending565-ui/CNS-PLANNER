# CNS-PLANNER 下一阶段总体技术基线与开发指导

> 文档状态：技术基线 / 研究与开发指导  
> 项目：`guosending565-ui/CNS-PLANNER`  
> 日期：2026-09-11  
> 适用范围：在当前 schema-v2、六步工作流、RiskModelV1、CNS Gap Analysis、CoveragePlannerV1 基础上继续演进。  
> 重要原则：本文中的标准、论文和模型用于建立“研究与工程基线”，不代表项目已经满足适航、运行批准或任何监管认证要求。

---

## 1. 文档目的

本文件把当前项目从“低空航路 + 二维 CNS 覆盖 + 初步风险评估”的工作台，进一步定义为一个可逐步实现的：

**低空航路风险评估、CNS 性能需求分析、三维服务覆盖、可靠性与不可接受事件评估、CNS 缺口分析和设施协同规划平台。**

后续开发不再围绕零散按钮和单个算法补丁推进，而统一围绕下面的闭环：

```text
数据源与算法配置
    ↓
工作区 / 标准二维网格 / 高度层
    ↓
环境与运行风险评估
    ↓
三维航路 + Operational Volume / Protection Volume
    ↓
飞行器机载 CNS 能力 + 运行规则 + Required CNS Performance
    ↓
已有地面 CNS / 铁塔 / 候选站址
    ↓
C/N/S 三维服务仿真 + 设备可靠性
    ↓
沿航路的 Service State / Outage / Degradation
    ↓
单项与耦合 Unacceptable Events
    ↓
CNS Gap
    ↓
共塔优先 CNS Site Planning
    ↓
重新仿真、重新评价、形成 residual risk
    ↓
可追溯导出
```

系统最终不应只回答“这里有没有覆盖”，而应回答：

1. 这条航路在给定运行规则和机型下需要什么 CNS 性能？
2. 现有基础设施和机载设备能否共同满足这些性能？
3. 在何处、持续多久、以什么方式发生性能降级或功能失效？
4. 这些失效何时构成不可接受事件？
5. 单一 C/N/S 失效和功能耦合后如何改变安全状态？
6. 新增哪些设备、优先复用哪些现有塔或站址，可以以较低成本满足要求？
7. 规划后 residual risk 是否下降到可接受水平？

---

## 2. 当前仓库基线与必须保护的内容

当前项目已具备以下重要基础：

- `RiskModel` 可替换接口；
- `RiskModelV1` 输出 `[0,1]` 相对风险指数，明确不是事故概率；
- `GapAnalyzer` / `CNSGapAnalyzerV1`；
- `AircraftCNSProfileCatalog`；
- `RequiredCNS`；
- `DeviceCatalog`；
- `ExistingCNSFacility`；
- `CandidateSite`；
- `CoveragePlannerV1`；
- MH/T 标准二维网格和稳定 `grid_id`；
- 人口、DEM、空域、交通、冲突等 `grid_id` 映射；
- schema-v2 `ProjectState`、持久化和状态失效关系。

后续修改必须遵守以下兼容原则：

1. **不修改 RiskModelV1 的语义。** 它继续作为相对风险 baseline。
2. **不把 CoveragePlannerV1 强行升级成三维复杂模型。** V1 继续作为二维圆覆盖 baseline。
3. **不破坏现有 `grid_id`。** 三维能力通过高度层/voxel 扩展，而不是推翻二维数据底座。
4. **新增算法必须带 algorithm id、version、参数和输入指纹。**
5. **真实值、模拟值、工程默认值、未知值必须明确区分。**
6. **NoData / unknown 不能自动解释为 0 风险、0 人口或满足要求。**

---

## 3. 下一阶段总体架构

建议新增六个稳定概念层：

```text
Data Semantics / Units / Provenance
Algorithm Registry
CNS Performance & Reliability
3D Service / Coverage Simulation
Unacceptable Event & Coupling
CNS Site Planning
```

对应建议目录：

```text
cns_planner/
  algorithms/
    registry.py
    route/
      v1.py                  # 保留
      risk_astar_v2.py       # 新 baseline
    coverage/
      v1.py                  # 保留
      model.py
      geometric_3d.py
      communication.py
      navigation.py
      surveillance.py
    site_planning/
      model.py
      greedy_v1.py

  risk/
    model.py                 # 保留公共契约
    v1.py                    # 保留相对风险
    ground_quantitative_v2.py
    sora_profile.py

  safety/
    model.py
    service_state.py
    reliability.py
    unacceptable_event.py
    coupling.py
    fault_tree.py

  domain/
    quantities.py
    provenance.py
    algorithm_manifest.py
    cns_inputs.py            # 扩展而非补丁式堆字段
    cns_requirements.py
    cns_reliability.py
    spatial_3d.py

  simulation/
    service_timeline.py
    protection_volume.py

  data/
    generators/
      tower_uniform_v0.py
      tower_ppp_v1.py
      tower_population_weighted_v2.py
```

---

## 4. 算法必须前端可选择、可解释、可审计

### 4.1 AlgorithmRegistry

前端所说的“替换算法”，第一阶段不是让用户上传并执行任意 Python，而是从系统已注册的算法中选择。

建议定义：

```json
{
  "algorithm_id": "risk_astar_v2",
  "name": "Risk-aware A*",
  "algorithm_type": "route_planner",
  "version": "2.0.0",
  "maturity": "research_baseline",
  "description": "在 A* 的边代价中加入风险成本",
  "inputs": ["grid", "risk", "constraints", "aircraft_speed"],
  "outputs": ["route", "distance", "risk_cost"],
  "assumptions": ["静态风险场", "非负边代价"],
  "references": [],
  "parameters": {}
}
```

前端至少展示：

- 算法名称；
- 版本；
- 算法类型；
- 核心思路；
- 输入/输出；
- 关键参数；
- 适用条件；
- 主要假设；
- 数据需求；
- 文献/标准来源；
- maturity：`demo / research_baseline / engineering_candidate / validated`。

### 4.2 第一阶段注册算法建议

| 类型 | Baseline | 用途 |
|---|---|---|
| risk | `RiskModelV1` | 保留当前相对风险 |
| quantitative ground risk | `GroundThirdPartyRiskV2` | 测试量化地面风险 |
| route | `RoutePlannerV1` | 当前结果基准 |
| route | `RiskAStarV2` | 风险感知路径规划测试 |
| gap | `CNSGapAnalyzerV1` | 当前二维 gap baseline |
| coverage | `CoveragePlannerV1` | 当前二维半径 baseline |
| coverage | `GeometricCoverage3DV1` | 第一阶段三维几何测试 |
| site planning | `GreedyReuseFirstPlannerV1` | 共塔优先规划 baseline |

---

## 5. 数据结构首先解决“单位、语义和来源”问题

### 5.1 当前人口数据必须纠正语义

当前使用的 WorldPop R2025A 中国人口产品，官方定义是：

- 100 m 产品约为 3 arc-second；
- WGS84；
- **栅格值单位为每个源像元中的人数（number of people per pixel/grid-cell）**；
- 不是直接的 `people/km²`；
- R2025A 当前官方标注为 alpha 产品。

因此系统不应继续把源像元值笼统叫“人口密度”。

应区分：

```text
population_source_count_people       # 原始源像元人数
population_count_people              # 映射到目标 MH/T 网格后的人数
population_density_people_km2        # 目标网格人数 / 目标网格面积
population_exposure                  # 风险模型实际使用的暴露量
```

映射原则：

- 人口 count 型 raster 聚合到更大网格时，采用**面积权重守恒求和**；
- 不应该用普通 bilinear 后把结果继续解释为人数；
- “显示密度”应在目标网格完成总人数聚合后，再用目标网格实际面积换算；
- 经纬度格网面积随纬度变化，不能用固定“100 m × 100 m”直接替代真实面积。

官方来源：WorldPop Population Counts, DOI `10.5258/SOTON/WP00839`。

### 5.2 DEM 单位和高程基准

Copernicus DEM GLO-30 官方产品手册说明：

- 水平 CRS：WGS84-G1150 / EPSG:4326；
- 垂直基准：EGM2008 / EPSG:3855；
- 垂直单位：m；
- GLO-30 纬向 grid spacing 为 1 arc-second；
- 产品本质上是 DSM，包含建筑、基础设施和植被形成的表面。

所以项目不能只存：

```json
{"elevation": 32.5}
```

应至少存：

```json
{
  "value": 32.5,
  "unit": "m",
  "vertical_reference": "EGM2008",
  "quantity": "surface_elevation",
  "source": "COP-DEM GLO-30"
}
```

### 5.3 统一 Quantity/Unit 规则

建议所有影响计算的数值都具备明确物理量语义。

内部 canonical units：

| 物理量 | canonical unit |
|---|---|
| 水平距离 | m |
| 高度/高程 | m |
| 面积 | m² |
| 速度 | m/s |
| 时间 | s |
| UI 延迟显示 | ms，但计算前转换到 s |
| 经纬度 | degree，仅作为地理坐标输入/输出 |
| 人数 | person |
| 人口密度 | person/km² |
| 概率/可用度 | 0–1 |
| failure rate | 1/h |
| MTBF/MTTR | h |
| 频率 | Hz / MHz / GHz，字段中明确单位 |
| 发射功率 | W 或 dBm，字段中明确语义 |
| 路损/增益 | dB / dBi |

建议通用结构：

```json
{
  "value": 12.3,
  "unit": "m",
  "quantity": "horizontal_accuracy",
  "source_unit": "m",
  "conversion": null,
  "source": "...",
  "confirmed": true
}
```

前端允许用户看到单位，但算法内部禁止通过字段名猜单位。

---

## 6. 真实数据、模拟数据与手动数据必须同等成为正式 DataSource

每个数据源增加：

```text
source_mode = real | synthetic | manual
source_id
source_version
source_reference
source_unit
canonical_unit
spatial_resolution
vertical_reference
quality_status
confidence
created_at
```

若为 synthetic，再增加：

```text
generator_id
generator_version
parameters
random_seed
calibration_reference
```

### 6.1 铁塔模拟建议

按成熟度分三级：

**V0 — uniform / regular**  
只用于界面和流程测试。

**V1 — homogeneous Poisson point process (PPP)**  
用于基础随机空间仿真。

**V2 — population-weighted / land-use-weighted point process**  
生成强度由人口、建设区、道路等控制，例如：

```text
lambda(x,y) = lambda0 * f(population, built_up, roads)
```

系统必须在 UI 中明确显示“模拟铁塔”，不能与真实运营商/中国铁塔数据混淆。

---

## 7. Required CNS 必须从“需要 C/N/S”升级为“需要什么性能”

`DeviceCatalog` 是设备能力；`AircraftCNSProfile` 是机载能力；`RequiredCNS` 是任务/航路要求。三者必须保持独立。

### 7.1 Communication requirement

建议字段：

```text
required
service_type: c2 | ats | payload
network_scope: public | private | dedicated | satellite
technology: 4g | 5g | wifi | dedicated_radio | satellite | other
max_latency_s
max_continuous_outage_s
max_cumulative_outage_s
min_availability
min_redundancy
handover_allowed
compatible_airborne_interfaces
contingency_policy_id
```

FAA 的高级 UAS 运行申请材料明确要求运营人提供 C2 Link type、C2 lost-link latency threshold（秒）以及 lost-link procedure，因此“最大连续断链时间 + 失联处置”应作为核心数据结构，而不能只保留 coverage ratio。

### 7.2 Navigation requirement

建议字段：

```text
required
navigation_type: gnss | gnss_rtk | inertial | visual | terrestrial | hybrid
max_horizontal_error_m
max_vertical_error_m
integrity_requirement
continuity_requirement
min_availability
max_degradation_time_s
min_redundancy
contingency_policy_id
```

导航应采用 performance-based 思路，而不是“某导航塔半径内=true”。ICAO PBN 体系强调导航规范与运行所需性能的对应关系；ASTM F3609-25 为 UAS PNT 提供定位保证、导航和时间同步框架。

### 7.3 Surveillance requirement

建议字段：

```text
required
target_cooperation: cooperative | non_cooperative | mixed
sensor_mode: active | passive | mixed
min_detection_range_m
min_detection_probability
max_update_interval_s
max_track_loss_s
max_alert_latency_s
min_redundancy
required_protection_volume
```

ASTM F3442-25 的适用场景明确考虑 mixed cooperative / non-cooperative traffic，因此合作/非合作目标必须进入数据模型，而不是只作为设备文字说明。

---

## 8. Aircraft CNS Profile 必须参与“何时功能真的失效”的判断

地面服务缺失不必然等于飞行器 CNS 功能失效。

例如：

- 外部 GNSS/RTK 服务短时降级，但机载 INS 可以桥接；
- 公网 5G 失效，但机载还有独立专用链路；
- 地面合作监视暂时缺失，但机载非合作 DAA 能力仍满足运行要求。

因此需新增：

```text
AircraftCNSProfile
  communication.capabilities[]
  navigation.capabilities[]
  surveillance.capabilities[]
  fallback_modes[]
  max_bridge_time_s
  autonomous_contingencies[]
  reliability_specs[]
```

最终判定不是：

```text
GroundCoverage == false -> CNS Failure
```

而是：

```text
External Service State
       +
Aircraft Capability State
       +
Operational Requirement
       +
Contingency Capability
       ↓
Effective CNS Functional State
```

建议状态：

```text
available
available_degraded
contingency
lost
unknown
```

---

## 9. CNS 可靠性：从 MTBF 字段升级为统一 ReliabilitySpec

### 9.1 不要把“常见失效率”写死成系统事实

可靠性参数优先级：

1. 制造商、认证资料或真实运维统计；
2. 标准、试验报告或可追溯公开文献；
3. 明确标记为 `demo_assumption` 的工程测试参数。

不存在可靠来源时，宁可显示 `pending_confirmation`，也不要伪造“行业平均 MTBF”。

### 9.2 ReliabilitySpec

```json
{
  "metric_type": "mtbf",
  "value": 20000,
  "unit": "h",
  "failure_mode": "loss_of_function",
  "operating_condition": "nominal",
  "source": "demo_assumption",
  "confirmed": false
}
```

也应支持：

```text
failure_rate_per_h
availability
empirical_failure_probability
mttr_h
confidence_interval
sample_size
```

### 9.3 第一阶段可靠性计算

测试阶段可以采用恒定失效率指数模型：

```text
lambda = 1 / MTBF
R(t) = exp(-lambda * t)
P_failure(t) = 1 - R(t)
```

若有 MTTR，可计算稳态可用度的经典近似：

```text
A = MTBF / (MTBF + MTTR)
```

必须在结果中声明：

- 使用恒定失效率假设；
- 是否独立；
- 时间单位；
- 参数来源。

NASA Reliability-Centered Maintenance Guide 给出了指数失效模型中 MTBF、failure rate 与可靠度的关系；IEC 61709:2017 给出了电子元件失效率数据和应力修正用于可靠性预测的标准框架。

### 9.4 地面设备和机载设备都需要可靠性

一次实际 CNS 功能成功不仅取决于地面设备，也取决于：

```text
Ground Device
Network / Service
Airborne Receiver / Transmitter / Sensor
Required Performance
```

所以可靠性不能只挂在 `CNSDevice`，还应挂在机载 capability。

---

## 10. 不可接受事件：采用 ARP4761A-inspired FHA → FTA/FMEA 作为成熟 baseline

### 10.1 为什么选择这套方案

SAE ARP4761A/ED-135 是成熟的航空系统安全评估方法框架，强调从功能危险分析（FHA）识别 failure conditions 和 safety objectives，再通过系统安全评估方法验证架构是否满足目标。项目不宣称满足适航认证，但可以借鉴其“功能—危险—失效条件—安全目标—架构验证”的方法。

第一阶段不建议直接从零构建复杂 Bayesian Network。推荐：

```text
Operational Function
    ↓
Functional Hazard Analysis (FHA)
    ↓
Failure Condition / Unacceptable Event
    ↓
Fault Tree (FTA) / FMEA
    ↓
Basic Events + Reliability
    ↓
Top Event Probability / Qualitative State
```

当有足够数据后，再将需要条件依赖、时序和人机交互的部分升级到 Bayesian Network、Dynamic Bayesian Network 或 Petri Net。

CNS/ATM 领域已有研究使用 stochastic Petri nets 处理分布式 CNS/ATM 安全问题；航空 Data Comm 研究也使用 Bayesian Network 处理通信技术对碰撞风险的影响。

### 10.2 本项目明确不考虑的共同原因失效

当前阶段**不考虑“同一铁塔断电导致 C/N/S 同时失效”这一共同原因失效**，也不把供电、机房、回传网络共同故障作为项目核心。

但仍考虑**功能耦合**，因为这直接关系到航路安全能力。

### 10.3 单项 Unacceptable Event 示例

Communication：

```text
UE-C-01:
Effective C2 state == lost
AND continuous_outage > max_continuous_outage_s
AND no acceptable autonomous contingency
```

Navigation：

```text
UE-N-01:
positioning error > required accuracy
持续超过 max_degradation_time_s
AND fallback capability does not restore required performance
```

Surveillance：

```text
UE-S-01:
required target class cannot be detected/tracked inside required protection volume
OR detection-to-alert timeline exceeds permitted budget
```

### 10.4 耦合不可接受事件示例

本项目优先研究“功能依赖导致的组合失效”，而非共塔供电 CCF。

**C + S：**

```text
监视系统检测到入侵目标
AND
通信链路无法在允许时间内把冲突信息送达航路飞行器
→ Tactical mitigation ineffective
```

**N + S：**

```text
导航精度/完整性降级
AND
监视/DAA 算法依赖本机位置
→ ownship state uncertainty 超出监视/避碰允许范围
```

**C + N：**

```text
导航降级需要远程干预
AND
C2 同时不可用
AND
机载自主 contingency 不足
→ 组合事件升级为 unacceptable
```

上述规则必须是 `OperationProfile` 条件化的，而不能写成所有任务通用逻辑。

### 10.5 Fault Tree 数据结构

```json
{
  "event_id": "UE-CS-001",
  "name": "tactical_mitigation_unavailable",
  "top_event": true,
  "logic": "AND",
  "children": [
    {"event_ref": "SURVEILLANCE_CONFLICT_DETECTED"},
    {"event_ref": "ALERT_DELIVERY_EXCEEDS_BUDGET"}
  ],
  "operational_applicability": ["route_profile_A"],
  "severity": "configured",
  "probability": null,
  "status": "pending_input",
  "source": "ARP4761A-inspired project model"
}
```

### 10.6 概率计算原则

- 只有在 independence 假设成立时，才直接相乘独立 basic event 概率；
- 不确定依赖关系时，不得静默假设独立；
- 可以先输出 qualitative / bounded result；
- 需要复杂依赖时再使用 BN/DBN；
- 所有概率必须有明确时间基准，例如 `per flight hour`、`per mission`。

参考 UAS 研究：2019 年一项 DAA 概率安全评估使用 FTA 分析 cooperative / non-cooperative sensing，并显式研究导航与监视依赖，证明这种“功能耦合 + 概率”框架适合作为本项目研究入口。

---

## 11. 三维空间：保留二维 MH/T Grid，新增 Altitude Layer / Voxel

不要将现有二维标准网格替换掉。

建议：

```text
grid_id               # 继续承载人口/DEM/空域等二维属性
altitude_layer_id      # 高度层
voxel_id = grid_id + altitude_layer_id
```

示例：

```text
CN3309...@AGL060
CN3309...@AGL090
CN3309...@AGL120
```

高度必须明确：

```text
reference = AGL | MSL | EGM2008 | ellipsoid
```

推荐项目内部：

- 地表高程：EGM2008 正高；
- 航路运行高度：保留原始 AGL/MSL 语义；
- 计算前统一转换到明确的三维直角坐标或局部投影坐标。

---

## 12. C/N/S 覆盖不能共用一个“半球”物理模型

### 12.1 Coverage V0：几何半球只作为软件验证 baseline

可首先实现：

```text
GeometricCoverage3DV1
```

站点 `(x0,y0,z0)` 与 voxel `(x,y,z)` 的三维距离：

```text
d3 <= radius
AND z >= z0
```

形成半球/球/圆锥/扇区几何服务体。

**该模型只能称为 geometric service volume，不能称为真实 4G/5G、GNSS 或 radar coverage。**

### 12.2 Communication 覆盖模型

建议分级：

**C0 — Geometric**  
半球/扇区，仅测试三维算法。

**C1 — Link budget / free-space baseline**  
使用 ITU-R P.525 free-space attenuation 作为基础参考；加入发射功率、天线增益、接收灵敏度和 margin。

**C2 — Terrain/obstacle aware**  
加入 DEM LOS、Fresnel/obstacle 与 diffraction；ITU-R P.526 可作为衍射工程参考。

**C3 — Cellular aerial UE model**  
4G/LTE 无人机场景优先参考 3GPP TR 36.777 `Enhanced LTE support for aerial vehicles`；5G/通用蜂窝 channel 可参考 3GPP TR 38.901，但必须注意后者是广义 0.5–100 GHz channel model，不等于低空无人机专用认证模型。

C2 link 的功能/性能要求可参考 RTCA DO-377A 系列的 UAS C2 Link system performance 框架。RTCA SC-228 正在持续扩展 cellular network 对 UAS C2 的标准化工作。

Communication service state 不能只看 RSSI，应支持：

```text
coverage / received power
latency
packet loss / availability
handover
network compatibility
redundancy
```

公网/专网不是简单标签，而应影响网络参数和站点规划策略。

### 12.3 Navigation 覆盖模型

必须按导航类型区分。

**GNSS：** 不应被建模成“某个地面塔的半球”。其服务判据应以定位性能为核心。

**GNSS-RTK / correction service：** 同时依赖：

```text
base/correction geometry
correction service availability
communication delivery
airborne GNSS/RTK capability
```

**INS / visual navigation：** 是机载 capability，不属于地面基础设施 coverage。

Navigation 评价应围绕：

```text
accuracy
integrity
continuity
availability
degradation time
```

参考 ICAO PBN、ASTM F3609-25 和 RTCA DO-397 Navigation Gaps for UAS。

### 12.4 Surveillance 覆盖模型

Surveillance 至少分：

```text
cooperative / non_cooperative
active / passive
```

**S0 — Geometric sensor volume**  
球/扇区/圆锥 + 最大检测范围。

**S1 — LOS-aware**  
加入 DEM/障碍 LOS。

**S2 — sensor-performance**  
增加：

```text
P_detection
P_track
update_interval
position_accuracy
latency
false_alarm (后续)
```

合作目标接收类设备（如 ADS-B / Remote ID receiver）更接近 RF link；非合作 active surveillance（如 radar）应使用其自身 detection model，不与通信传播模型混用。

DAA/监视性能参考 ASTM F3442-25 和 RTCA DO-365C 系列。

---

## 13. 航路必须从 LineString 升级为 Operational Volume + Protection Volume

### 13.1 保护范围的核心时间链

将航路周边保护范围定义为：

```text
Detect
  + Track
  + Processing / Decide
  + Platform Communication
  + Aircraft Reception / Command
  + Aircraft Reaction
  + Maneuver
```

总时间：

```text
T_total = T_detect
        + T_track
        + T_processing
        + T_comm
        + T_decision
        + T_aircraft_reaction
        + T_maneuver
```

保护距离的第一阶段工程表达：

```text
D_protect = V_relative * T_total
          + D_maneuver
          + D_uncertainty
```

该公式是项目工程抽象，不应标成监管公式。

JARUS SORA 2.5 Annex H 定义了 Tactical Conflict Detection and Alerting Safety Service，并围绕 detect / decide 等战术缓解环节组织服务要求，可作为 Protection Volume 和服务时延链设计的主要依据。

### 13.2 规划意义

监视规划问题由：

```text
“航路线有没有被雷达/接收机覆盖”
```

升级为：

```text
“是否能够在入侵目标到达危险范围之前完成发现、确认、通知和飞行器响应”
```

这会直接把监视设备性能、通信时延和无人机响应时间耦合起来。

---

## 14. 风险计算 baseline：先采用成熟的第三方地面风险量化框架

### 14.1 为什么不立即把 RiskModelV1 改成概率

当前 RiskModelV1 的 `[0,1]` 输出是 relative index，其输入数据仍不完整，且人口单位之前未完成确认。

正确方式是：

- 保留 `RiskModelV1`；
- 新增 `GroundThirdPartyRiskV2`；
- 两者同时可选；
- V2 只有输入满足条件时才输出 absolute / quantitative risk。

### 14.2 量化地面风险基本结构

成熟 UAV third-party ground risk 文献通常采用：

```text
Probability of aircraft failure
    ×
Crash / impact location distribution
    ×
Exposed population
    ×
Impact / fatality consequence
    ×
Shelter mitigation
```

2022 年 Aerospace 的一项城市商业 UAS 风险研究将 expected third-party fatalities 表达为对空间位置的风险积分，并显式包含 crash probability、crash location、impact area、fatality probability、population density 和 shelter protection。

建议 V2 输入：

```text
failure_probability_per_flight_hour
flight_time_in_cell_s
crash_location_kernel
population_count_people
shelter_factor
impact_area_m2
conditional_fatality_probability
```

结果至少包含：

```text
expected_fatalities_per_mission
expected_fatalities_per_flight_hour
assumptions
parameter_sources
confidence/status
```

### 14.3 什么时候不允许输出 absolute risk

以下任一关键参数未知时：

```text
failure probability
population semantics
impact/crash model
fatality consequence
```

结果必须：

```text
status = pending_assumptions / missing_data
```

而不是用 0 或随意默认值冒充绝对概率。

### 14.4 SORA 在本项目中的角色

JARUS SORA 2.5 适合用于：

- operational risk assessment profile；
- GRC / ARC；
- strategic / tactical mitigation；
- OSO / safety portfolio 的研究参考。

但不应该把 SORA 的等级数字直接当作网格中的连续事故概率。

---

## 15. 路径规划 baseline：Risk-aware A*，暂不直接上强化学习

建议 RoutePlannerV2 采用 Risk-aware A*，原因：

1. A* 本身成熟、确定性高、容易测试；
2. 当前项目已有 A* 思路，迁移成本低；
3. 2018 ICUAS 已有 UAV `riskA*` 工作，将位置风险图作为 A* risk cost；
4. 相比 DRL，更容易解释“为什么航路绕开某一区域”；
5. 很适合作为以后复杂算法的 baseline。

第一版边代价可写成：

```text
edge_cost = distance_cost + risk_weight * normalized_risk_cost
```

但系统应**单独保存物理风险量和算法优化代价**，避免不同单位被隐藏地相加。

当 `GroundThirdPartyRiskV2` 输出风险率时，可根据航段驻留时间：

```text
delta_t = segment_length / aircraft_speed
segment_risk = risk_rate * delta_t
```

再累积航路风险。

建议输出：

```text
route_length_m
estimated_time_s
relative_risk_cost
quantitative_ground_risk (if available)
risk_reduction_vs_shortest
algorithm_id/version
```

后续再考虑 Theta*、RRT*、多目标/MILP 或学习方法。

参考：Primatesta et al., `A Risk-aware Path Planning Method for Unmanned Aerial Vehicles`, ICUAS 2018, DOI `10.1109/ICUAS.2018.8453354`。

---

## 16. CNS Gap V2：从“未覆盖长度”升级为“功能缺口时间线”

对于航路采样点 `(x,y,z,t)`，分别计算：

```text
C_state(t)
N_state(t)
S_state(t)
```

再与机载 capability 和 Required CNS 对比。

Gap V2 应输出：

```text
coverage_gap_length_m
continuous_outage_s
max_continuous_outage_s
cumulative_outage_s
degraded_duration_s
redundancy_below_requirement_s
reliability_shortfall
unacceptable_event_ids
reasons[]
```

例如 UI 结果不再只写：

```text
通信未覆盖 820 m
```

而应写：

```text
C2：航段 R1 发生 820 m 服务缺口
按 25 m/s 预计连续 32.8 s
航路要求 max_continuous_outage = 10 s
机载备用链路：无
=> UE-C-01 triggered
```

注意：示例数字仅为说明数据结构，不作为项目默认安全阈值。

---

## 17. CNS Site Planning：共塔优先，但不把共塔当作绝对目标

### 17.1 Reuse-first 顺序

```text
Existing CNS Facility
    ↓
Existing Tower / Infrastructure
    ↓
Candidate Site
    ↓
New Site
```

只有在现有基础设施无法满足时，才建议新建站。

### 17.2 共塔可行性不是“距离小于 2 km”

建议 CandidateSite / Tower 增加：

```text
tower_height_m
available_mount_height_m
site_type
usable_subsystems
power_available (仅作为站点可用条件；本阶段不做共因断电模型)
backhaul_available
structural_capacity_status
land_access_status
```

设备增加：

```text
mount_height_requirement_m
frequency
antenna_pattern
power_requirement
backhaul_requirement
compatible_site_types
```

形成：

```text
can_install(site, device) -> result + reasons
```

### 17.3 第一阶段 SitePlanner

建议 `GreedyReuseFirstPlannerV1`：

1. 对最大/最严重 gap 排序；
2. 搜索现有可复用站址；
3. 对每个候选站址模拟服务改善量；
4. 以 coverage gain / cost 或 unacceptable-event reduction 排序；
5. 迭代加入设备；
6. 若无可行复用站，提出新建建议；
7. 重新运行 Coverage + Gap + Safety。

后续再升级为：

- Set Cover；
- Maximum Coverage Location Problem；
- MILP；
- 多目标优化。

### 17.4 目标函数建议

后续优化可考虑：

```text
minimize:
  construction_cost
+ equipment_cost
+ maintenance_proxy
+ uncovered_penalty
+ unacceptable_event_penalty
```

subject to：

```text
Required CNS performance satisfied
max outage not exceeded
minimum redundancy satisfied
site/device compatibility satisfied
```

本阶段按照用户要求，**不加入“共塔导致供电共同失效”的 common-cause risk penalty**。

---

## 18. ProjectState 下一版建议

不要求立即 bump schema；先设计、测试，等数据结构稳定后再迁移。

目标状态：

```text
algorithm_selection
algorithm_registry_snapshot

data_sources[*].provenance
unit_registry

altitude_layers
voxel_index

operation_profiles
aircraft_profiles
selected_aircraft_profile_id
required_cns

cns_reliability
cns_service_simulation
unacceptable_event_catalog
unacceptable_event_results
coupling_rules

cns_gap_analysis_v2
site_plan
residual_assessment
```

每个派生结果至少记录：

```text
status
algorithm_id
algorithm_version
input_fingerprint
parameters
source_versions
created_at
```

---

## 19. 前端六步保持不变，但职责升级

### Step 1 — 项目与数据

增加：

- 数据源 real/synthetic/manual；
- 数据单位、分辨率、CRS、时间版本；
- 算法选择；
- 算法详情抽屉；
- synthetic generator 设置；
- 数据质量警告。

### Step 2 — 工作区与环境

增加：

- altitude layer；
- population count / density 切换；
- ground quantitative risk / relative risk 切换；
- 数据完整度与单位状态。

### Step 3 — 航路设计

增加：

- RoutePlanner 选择；
- risk-aware A* 参数；
- 3D route；
- Operational Volume；
- Protection Volume。

### Step 4 — 运行规则

增加：

- Aircraft CNS capabilities；
- C/N/S Required Performance；
- public/private/dedicated communication；
- 4G/5G/专用链路兼容；
- cooperative/non-cooperative surveillance；
- active/passive sensor；
- unacceptable event threshold；
- contingency mode。

### Step 5 — 设备与布站

调整为：

```text
Existing Infrastructure
→ 3D Service Simulation
→ Reliability
→ CNS Gap
→ Unacceptable Events
→ Reuse-first Site Planning
→ Re-evaluation
```

地图可切换：

```text
C service volume
N service volume
S service volume
CNS gap
unacceptable segments
existing towers
planned colocated sites
new sites
```

### Step 6 — 确认与导出

报告必须包含：

- 使用的数据及单位；
- synthetic/real 状态；
- 算法和版本；
- 关键假设；
- C/N/S requirement；
- reliability input；
- gap；
- unacceptable events；
- 新建设备/共塔设备；
- residual risk；
- 未确认参数。

---

## 20. 完整推荐工作流

### Phase A：数据基线

1. 创建/打开项目；
2. 确定工作区；
3. 加载 QGIS、WorldPop、GLO-30、空域；
4. 检查 source CRS、unit、NoData；
5. 缺失 towers / building exposure 等数据时，选择 synthetic provider；
6. 保存 provenance 与 random seed。

### Phase B：风险与航路

1. 生成人口 count 与 density；
2. 运行 `RiskModelV1` 作为 baseline；
3. 输入可靠 failure/consequence 参数时运行 `GroundThirdPartyRiskV2`；
4. 使用 RoutePlannerV1 生成 shortest/baseline；
5. 使用 RiskAStarV2 生成 risk-aware route；
6. 比较长度、时间、风险。

### Phase C：运行规则

1. 选择 aircraft profile；
2. 配置机载 C/N/S capabilities；
3. 配置 Required CNS performance；
4. 配置 flight rule / traffic context；
5. 配置 lost-link / navigation degradation contingency；
6. 生成 unacceptable-event applicability。

### Phase D：三维 CNS Assurance

1. 生成 altitude layers；
2. 将 route 采样为 `(x,y,z,t)`；
3. 运行 C/N/S CoverageModel；
4. 叠加设备 ReliabilitySpec；
5. 叠加 aircraft capability；
6. 计算 effective service state timeline；
7. 运行 UnacceptableEventEvaluator；
8. 生成 CNSGapV2。

### Phase E：基础设施规划

1. 搜索 Existing Tower / Facility；
2. 检查 device-site compatibility；
3. 共塔优先；
4. 无法解决时搜索 CandidateSite；
5. 仍无法解决时生成 NewSite 建议；
6. 重算 service/gap/events；
7. 迭代直至要求满足或输出 infeasible。

### Phase F：输出

输出：

```text
Baseline route
Risk-aware route
CNS service volumes
Reliability assumptions
CNS gaps
Triggered unacceptable events
Existing-site reuse plan
New-site plan
Residual assessment
Data/algorithm/source manifest
```

---

## 21. 开发顺序建议

### P0 — Unit + Provenance 修正（必须第一优先）

- 修正 WorldPop 人口语义；
- 增加单位和 quantity；
- DEM vertical datum；
- real/synthetic/manual；
- 前端显示 data quality。

### P1 — AlgorithmRegistry

- 注册 Risk/Route/Coverage/Gap/Site planner；
- 前端算法选择和详情说明。

### P2 — CNS 类型与 Required Performance

- public/private/dedicated；
- 4G/5G；
- active/passive；
- cooperative/non-cooperative；
- Aircraft capability 与 requirement 分离。

### P3 — ReliabilitySpec + ServiceState

- MTBF/failure rate/availability；
- ground + airborne reliability；
- available/degraded/contingency/lost。

### P4 — Unacceptable Events baseline

- FHA-style catalog；
- FTA logic；
- C/N/S 单项事件；
- C+S、N+S、C+N 功能耦合。

### P5 — Altitude Layer + GeometricCoverage3D

先把三维数据流和前端跑通，不急于做高保真传播。

### P6 — CNS type-specific coverage

- communication link model；
- navigation performance model；
- surveillance sensor model。

### P7 — GroundThirdPartyRiskV2 + RiskAStarV2

用成熟量化风险模型和 risk-aware A* 建立论文实验 baseline。

### P8 — Protection Volume

建立 Detect→Decide→Communicate→React 时间预算。

### P9 — CNSGapV2

从长度 gap 升级为时间/性能 gap。

### P10 — Reuse-first SitePlanner

先 greedy，再优化。

---

## 22. 必须建立的测试

### Units

- WorldPop source value 不得被标记为 `people/km2`；
- aggregation 后人数守恒；
- density = people / actual cell area；
- ms/s、h/s 转换测试；
- altitude reference 不明确则禁止三维判定。

### Reliability

- MTBF → lambda → R(t) 数值测试；
- mission duration 不同得到不同 failure probability；
- unknown reliability 不得自动设为 1。

### Service state

- ground service lost，但机载 fallback 满足 → 不应直接判 lost；
- fallback bridge time 超限 → lost；
- requirement 不同 → 同一物理覆盖可得到不同 service state。

### Coupled events

- S 发现目标 + C 正常 → 不触发 C+S top event；
- S 发现目标 + C alert latency 超限 → 触发；
- N degraded 但 S 不依赖该 navigation input → 不应自动触发 N+S；
- common tower power failure 不在当前模型范围。

### Coverage

- V1 二维结果不得因新增 3D 模块改变；
- geometric 3D 与 radio/sensor model 输出语义不同；
- terrain blocking 有明确 status。

### Route

- RiskAStarV2 在 risk weight=0 时应接近距离 baseline；
- 高风险区域代价增加后路径可避让；
- 固定输入必须 deterministic。

---

## 23. 其他建议

### 23.1 增加 Scenario 而不是不停覆盖同一 ProjectState

以后建议支持：

```text
Scenario A: public 5G + cooperative surveillance
Scenario B: private 5G + radar
Scenario C: public/private dual link
```

同一数据基线下比较：

- 航路；
- 设备数量；
- cost proxy；
- CNS gap；
- unacceptable events；
- residual risk。

这非常适合论文实验。

### 23.2 增加 Assumption Register

项目很多参数早期必然来自假设，因此建议新增：

```text
assumption_id
parameter
value/unit
reason
source
confirmed
impact_if_wrong
```

这样不会再出现“代码里有一个数，但半年后不知道为什么是这个数”。

### 23.3 增加 Model Validity / Applicability

每个算法都应说明：

```text
applicable_altitude_range
applicable_technology
required_data
known_limitations
validation_status
```

尤其避免把 ITU 地面传播模型、3GPP aerial model、radar 模型混用。

### 23.4 把“缺数据”当正式工程结果

结果状态统一：

```text
passed
failed
missing_data
pending_confirmation
not_applicable
stale
```

任何高风险、CNS、可靠性结果都不允许用“缺数据=0”处理。

---

# 24. 推荐参考文献与技术文件

下面的来源优先选择标准组织、监管机构和同行评议论文。论文写作时仍应根据学校格式重新生成参考文献格式。

## 24.1 UAS 风险与安全评估

1. **JARUS. SORA v2.5 package, 2024.**  
   SORA 2.5 Main Body、Annex C/D/E/F/H 等。  
   https://jarus-rpas.org/publications/

2. **JARUS. SORA v2.5 Annex H — UAS Safety Services Considerations.**  
   Tactical Conflict Detection and Alerting、外部 Safety Service 等。  
   https://jarus-rpas.org/wp-content/uploads/2024/12/SORA-v2.5-Annex-H-Release-JAR_doc_34.pdf

3. **SAE International. ARP4761A: Guidelines for Conducting the Safety Assessment Process on Civil Aircraft, Systems, and Equipment. Revised 2023-12-20.**  
   DOI: https://doi.org/10.4271/ARP4761A  
   https://saemobilus.sae.org/standards/arp4761a-guidelines-conducting-safety-assessment-process-civil-aircraft-systems-equipment

4. **Vismari, L. F.; Camargo Jr., J. B. A safety assessment methodology applied to CNS/ATM-based air traffic control system. Reliability Engineering & System Safety, 2011, 96(7):727–738.**  
   DOI: https://doi.org/10.1016/j.ress.2011.02.007

5. **Probabilistic Safety Assessment for UAS Separation Assurance and Collision Avoidance Systems. Aerospace, 2019, 6(2), 19.**  
   使用 FTA 分析 UAS separation/DAA，并讨论导航—监视依赖。  
   https://www.mdpi.com/2226-4310/6/2/19

6. **Bayesian network model of aviation safety: Impact of new communication technologies on mid-air collisions. Reliability Engineering & System Safety, 2024.**  
   用 BN 建模新通信技术与航空安全的系统交互。  
   https://www.sciencedirect.com/science/article/pii/S0951832023008190

## 24.2 C2 / Communication

7. **FAA. Instructions for Drone Operators Completing FAA Form 7711-2.**  
   明确包含 C2 link type、C2 Lost Link Latency Threshold、Lost Link Procedure、Navigation degradation threshold。  
   https://www.faa.gov/uas/advanced_operations/instructions-drone-operators-completing-faa-form-7711-2

8. **RTCA DO-377A. Minimum Aviation System Performance Standards for C2 Link Systems Supporting Operations of UAS.**  
   RTCA SC-228。  
   https://www.rtca.org/products/do-377a-electronic/

9. **3GPP TR 36.777. Enhanced LTE support for aerial vehicles. Release 15.**  
   https://portal.3gpp.org/desktopmodules/Specifications/SpecificationDetails.aspx?specificationId=3231

10. **3GPP TR 38.901. Study on channel model for frequencies from 0.5 to 100 GHz.**  
    https://portal.3gpp.org/desktopmodules/Specifications/SpecificationDetails.aspx?specificationId=3173

11. **ITU-R P.525-5. Calculation of free-space attenuation, 2024.**  
    https://www.itu.int/rec/R-REC-P.525/en

12. **ITU-R P.526-16. Propagation by diffraction, 2025.**  
    https://www.itu.int/rec/R-REC-P.526/en

## 24.3 Navigation / PNT

13. **ICAO Doc 9613. Performance-Based Navigation (PBN) Manual, 5th Edition, 2023.**  
    https://store.icao.int/en/performance-based-navigation-pbn-manual-doc-9613

14. **ASTM F3609-25. Standard Specification for Positioning Assurance, Navigation, and Time Synchronization (PNT) for UAS.**  
    DOI: https://doi.org/10.1520/F3609-25  
    https://store.astm.org/f3609-25.html

15. **RTCA DO-397. Guidance Material: Navigation Gaps for UAS.**  
    RTCA SC-228 发布信息：  
    https://www.rtca.org/news/standards-oversight-committee-acts-on-emerging-technologies-and-safety-oversight/

## 24.4 Surveillance / DAA

16. **ASTM F3442-25. Standard Specification for Detect and Avoid System Performance Requirements.**  
    https://store.astm.org/standards/f3442

17. **RTCA DO-365C / DO-365C Change 1. Minimum Operational Performance Standards for Detect and Avoid Systems.**  
    https://www.rtca.org/

18. **ICAO Performance-Based Communication and Surveillance (PBCS).**  
    PBCS 用 RCP / RSP performance specification 评价 communication / surveillance，而不是绑定某一种技术。  
    https://www.icao.int/airnavigation/pbcs-overview

## 24.5 Reliability

19. **IEC 61709:2017. Electric components — Reliability — Reference conditions for failure rates and stress models for conversion.**  
    https://webstore.iec.ch/en/publication/28554

20. **NASA Reliability-Centered Maintenance Guide for Facilities and Collateral Equipment.**  
    包含 MTBF、failure rate 和 exponential reliability 的基础关系。  
    https://www.nasa.gov/wp-content/uploads/2023/02/nasa_rcmguide.pdf

## 24.6 风险量化与路径规划

21. **Primatesta, S. et al. A Risk-aware Path Planning Method for Unmanned Aerial Vehicles. ICUAS 2018.**  
    RiskA* baseline。  
    DOI: https://doi.org/10.1109/ICUAS.2018.8453354

22. **A Simulation Study of Risk-Aware Path Planning in Mitigating the Third-Party Risk of a Commercial UAS Operation in an Urban Area. Aerospace, 2022, 9(11), 682.**  
    给出 third-party ground fatality risk 的空间积分结构。  
    https://www.mdpi.com/2226-4310/9/11/682

23. **Pang, B.; Hu, X.; Dai, W.; Low, K. H. Third Party Risk Modelling and Assessment for Safe UAV Path Planning in Metropolitan Environments.**  
    https://arxiv.org/abs/2107.01834

24. **Risk Assessment Model for UAV Cost-Effective Path Planning in Urban Environments. IEEE Access, 2020.**  
    DOI: https://doi.org/10.1109/ACCESS.2020.3016118

## 24.7 当前项目数据

25. **WorldPop R2025A Population Counts.**  
    中国 100 m：单位为 people per pixel/grid-cell，WGS84，约 3 arc-second。  
    DOI: https://doi.org/10.5258/SOTON/WP00839  
    https://hub.worldpop.org/geodata/listing?id=135

26. **Copernicus DEM Product Handbook / GLO-30.**  
    GLO-30、WGS84-G1150、EGM2008、vertical unit metres。  
    https://dataspace.copernicus.eu/sites/default/files/media/files/2024-06/geo1988-copernicusdem-spe-002_producthandbook_i5.0.pdf

---

# 25. 当前阶段的核心决策

在正式开始下一轮编码前，暂定以下研究/工程 baseline：

```text
Operational risk framework:
    JARUS SORA 2.5 as operational reference

System safety / unacceptable events:
    ARP4761A-inspired FHA + FTA/FMEA

Coupled CNS events:
    Functional dependency coupling first
    Bayesian/Petri-net reserved for later
    Shared tower power common-cause failure explicitly out of scope

Ground quantitative risk:
    Expected third-party fatality risk model

Route planning:
    Risk-aware A*

3D coverage first baseline:
    Geometric service volume

Communication refinement:
    ITU-R P.525/P.526 + 3GPP aerial/cellular references

Navigation:
    Performance-based PNT, not generic hemisphere

Surveillance:
    Cooperative/non-cooperative + active/passive + DAA performance

Reliability:
    ReliabilitySpec + MTBF/failure rate/availability
    no invented industry-average failure rates

Site planning:
    Reuse-first greedy baseline
    later Set Cover / MILP / multi-objective
```

这组 baseline 的目的不是声明它们已经是最终论文方法，而是提供一个**成熟、可解释、可复现、能与以后高级方法比较的测试基线**。

后续讨论和开发应优先检查本文件中的结构是否需要调整，再进入具体代码实现。