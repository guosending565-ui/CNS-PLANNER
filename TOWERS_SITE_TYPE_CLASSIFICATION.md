# 通信铁塔 site_type 分类统计与人工裁定表

统计只读来源：真实报送 Excel；分类规则见 `cns_planner/domain/tower_obstacle.py` 的
`ROOFTOP_MARKERS` / `GROUND_MARKERS` / `classify_base_type`。
**除"楼顶"已明确纳入 rooftop 之外，本轮不自动给任何 unknown 类型分类。**

---

## 1. 统计来源

| 项目 | 值 |
|---|---|
| 源文件 | `D:\aaa2026project\UOM\舟山\基础数据\各单位报送的补充材料\各单位报送的补充材料\航路航线规划-铁塔数据.xlsx` |
| SHA-256 | `d722c111596acb4766bcc9d4cb5ce326f3a4b1f8364a5db871cb60220e2a411b` |
| 记录数 | 373 |
| 无效行 | 0 |
| 是否入 Git | 否（原始 Excel 不入库） |

**复现方式**（只读，不改任何数据）：

```python
from collections import Counter
from pathlib import Path
from cns_planner.domain.tower_obstacle import classify_base_type
from cns_planner.reference_data.towers import load_towers

record = load_towers(Path(r"D:\aaa2026project\UOM\舟山\基础数据\各单位报送的补充材料"
                         r"\各单位报送的补充材料\航路航线规划-铁塔数据.xlsx"))
types = Counter(item.get("site_type") for item in record["items"])
buckets = Counter(classify_base_type(t)[0] for t in types.elements())
print(buckets)                                  # {'rooftop': 219, 'ground': 39, 'unknown': 115}
for site_type, number in types.most_common():   # 每个 site_type 的判定与数量
    print(classify_base_type(site_type), number, repr(site_type))
```

同一统计已固化为回归测试
`tests/test_towers_operational_integration_v2.py::test_real_tower_site_type_classification_is_219_39_115`
（真实文件不存在时自动 skip）。

## 2. 判定规则（当前代码）

* `rooftop` 命中特征（`ROOFTOP_MARKERS`）：`楼面 / 楼顶 / 屋顶 / 屋面 / rooftop / roof`
  * 「楼顶」在 FIX-TOWER-TYPE-001 中显式纳入：语义明确属于 rooftop，不需要人工判断；
* `ground` 命中特征（`GROUND_MARKERS`）：`地面 / 落地 / ground`
* 未命中任何特征 ⇒ `unknown`（`site_type_unclassified`）；`site_type` 缺失 ⇒ `unknown`（`site_type_missing`）
* 判定顺序：先查 rooftop，再查 ground；两者都不命中即 `unknown`
* 其余 unknown 类型本轮**保持 unknown**，不自动设为 ground（那会低估楼面塔障碍高度）

## 3. 分类结果（楼顶修正后）

| base_type | 数量 | 占比 | 变化 |
|---|---|---|---|
| `rooftop` | **219** | 58.7% | +2（楼顶景观塔 ×2） |
| `ground` | **39** | 10.5% | 不变 |
| `unknown` | **115** | 30.8% | −2 |
| 合计 | 373 | 100% | |

判定来源分布：

| base_type | reason | 数量 |
|---|---|---|
| rooftop | `site_type_rooftop_marker` | 219 |
| ground | `site_type_ground_marker` | 39 |
| unknown | `site_type_unclassified` | 114 |
| unknown | `site_type_missing` | 1 |

## 4. 全部 site_type 分布（按数量降序）

| site_type | 数量 | 当前判定 | reason | 示例 tower_id |
|---|---|---|---|---|
| 楼面拉线塔 | 94 | rooftop | `site_type_rooftop_marker` | 330903908000000688 |
| **角钢塔** | **58** | **unknown** | `site_type_unclassified` | 330903908000000816 |
| 楼面美化天线外罩 | 41 | rooftop | `site_type_rooftop_marker` | 330903500000000084 |
| 楼面抱杆 | 37 | rooftop | `site_type_rooftop_marker` | 330903908000000234 |
| 楼面增高架 | 32 | rooftop | `site_type_rooftop_marker` | 330903908000000058 |
| 落地拉线塔 | 23 | ground | `site_type_ground_marker` | 330903908000000515 |
| **H杆塔** | **22** | **unknown** | `site_type_unclassified` | 330903908000000147 |
| **单管塔** | **14** | **unknown** | `site_type_unclassified` | 330903900010001735 |
| **造型景观塔** | **13** | **unknown** | `site_type_unclassified` | 330903500000001454 |
| 楼面支撑杆 | 11 | rooftop | `site_type_rooftop_marker` | 330903908000000587 |
| 简易落地塔 | 9 | ground | `site_type_ground_marker` | 330903908000000595 |
| 落地增高架 | 5 | ground | `site_type_ground_marker` | 330903500010001628 |
| **水泥杆塔** | **3** | **unknown** | `site_type_unclassified` | 330903908000000090 |
| 地面支撑杆 | 2 | ground | `site_type_ground_marker` | 330903500010001629 |
| **通信灯杆塔** | **2** | **unknown** | `site_type_unclassified` | 330903500010001630 |
| **一体化塔房** | **2** | **unknown** | `site_type_unclassified` | 330903500000000054 |
| 楼顶景观塔 | 2 | **rooftop** | `site_type_rooftop_marker` | 330921908000000237 |
| 楼面角钢塔 | 2 | rooftop | `site_type_rooftop_marker` | 33092270000001 |
| （`site_type` 为空） | **1** | **unknown** | `site_type_missing` | 330922908000000026 |

## 5. unknown 清单（8 类 / 115 塔，待人工裁定）

| # | site_type | 数量 | 示例 tower_id | 裁定（请勾选） | 依据 / 备注 |
|---|---|---|---|---|---|
| 1 | 角钢塔 | 58 | 330903908000000816 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 2 | H杆塔 | 22 | 330903908000000147 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 3 | 单管塔 | 14 | 330903900010001735 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 4 | 造型景观塔 | 13 | 330903500000001454 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 5 | 水泥杆塔 | 3 | 330903908000000090 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 6 | 通信灯杆塔 | 2 | 330903500010001630 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 7 | 一体化塔房 | 2 | 330903500000000054 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 8 | （空） | 1 | 330922908000000026 | ☐ ground ☐ rooftop ☐ 保持 unknown | 源数据没有填细分类型 |

（原第 8 项「楼顶景观塔 ×2」已按 FIX-TOWER-TYPE-001 判定为 rooftop，移出本表。）

## 6. 裁定后如何落地（需要你确认方式，本轮未做）

| 需求 | 落地方式 | 是否属于本轮范围 |
|---|---|---|
| 把某一类判为 `ground` **或** 全部判为同一类 | 显式配置 `tower_obstacle_policy.default_base_type`（现有字段，UI 未暴露） | 否，等裁定 |
| 逐类映射（例如"角钢塔=ground、一体化塔房=rooftop"同时生效） | 需要新增逐类映射契约（新字段 + 新 UI），或把裁定写回源数据的"铁塔细分类型"列 | 否，等裁定 |
| 补全第 8 条的源数据类型 | 由报送单位补数据 | 否，等裁定 |

## 7. unresolved 的真实影响链（FIX-TOWER-DOC-001 统一表述）

**塔顶高度未解析时不生成具体 tower clearance floor；相关空间保持 unknown，
并在路径搜索中 fail-closed，不得作为已验证安全可通行区域。**

具体链路（与代码一致）：

```
TowerObstacleProfile.status = unresolved
    → 该塔所在 cell 的 tower fact: data_status = unknown
    → LayerFeasibilityMask: reason_code = tower_height_unresolved, cell status = unknown
    → Theta* V2 gate: unknown 不可穿越（fail-closed）
```

因此这 115 个 unknown 塔既不会被当作"可以飞越的低矮障碍"，也不会被当作"这里没有塔"：
它们所在空间在路径搜索中**不可通过**，直到塔型被裁定、塔顶高度可解析为止。
它们的位置事实、地图显示与共塔宿主候选身份不受影响。

## 8. 本轮的边界声明

* 未修改 `TowerObstacleProfile` 高度推导数学（地面/楼面公式、垂直基准、fail-closed 语义）；
* 除 `ROOFTOP_MARKERS` 新增「楼顶」外，分类规则未改动；
* 未修改共塔 `REUSE_TIERS` 顺序；
* 未修改原始铁塔 Excel，也未把统计结果写回项目 state。
