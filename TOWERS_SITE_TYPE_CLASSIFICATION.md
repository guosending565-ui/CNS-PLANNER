# 通信铁塔 site_type 分类统计与人工裁定表

本轮（Towers Operational Integration V2 收口）只做**只读统计**：
不自动给 `unknown` 分类、不改 `TowerObstacleProfile` 的分类规则与高度推导规则、
不改原始铁塔 Excel。下表等待人工裁定。

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
print(buckets)                                  # ground / rooftop / unknown
for site_type, number in types.most_common():   # 每个 site_type 的判定与数量
    print(classify_base_type(site_type), number, repr(site_type))
```

## 2. 判定规则（当前代码，未改动）

* `rooftop` 命中特征（`ROOFTOP_MARKERS`）：`楼面 / 屋顶 / 屋面 / rooftop / roof`
* `ground` 命中特征（`GROUND_MARKERS`）：`地面 / 落地 / ground`
* 未命中任何特征 ⇒ `unknown`（`site_type_unclassified`）；`site_type` 缺失 ⇒ `unknown`（`site_type_missing`）
* 判定顺序：先查 rooftop，再查 ground；两者都不命中即 `unknown`
* `unknown` 的塔 ⇒ `TowerObstacleProfile.status = unresolved` ⇒ **不参与任何净空判定**（fail-closed）

## 3. 分类结果

| base_type | 数量 | 占比 |
|---|---|---|
| `rooftop` | **217** | 58.2% |
| `ground` | **39** | 10.5% |
| `unknown` | **117** | 31.4% |
| 合计 | 373 | 100% |

判定来源分布：

| base_type | reason | 数量 |
|---|---|---|
| rooftop | `site_type_rooftop_marker` | 217 |
| ground | `site_type_ground_marker` | 39 |
| unknown | `site_type_unclassified` | 116 |
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
| **楼顶景观塔** | **2** | **unknown** | `site_type_unclassified` | 330921908000000237 |
| 楼面角钢塔 | 2 | rooftop | `site_type_rooftop_marker` | 33092270000001 |
| （`site_type` 为空） | **1** | **unknown** | `site_type_missing` | 330922908000000026 |

## 5. unknown 清单（9 类 / 117 塔，待人工裁定）

| # | site_type | 数量 | 示例 tower_id | 裁定（请勾选） | 依据 / 备注 |
|---|---|---|---|---|---|
| 1 | 角钢塔 | 58 | 330903908000000816 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 2 | H杆塔 | 22 | 330903908000000147 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 3 | 单管塔 | 14 | 330903900010001735 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 4 | 造型景观塔 | 13 | 330903500000001454 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 5 | 水泥杆塔 | 3 | 330903908000000090 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 6 | 通信灯杆塔 | 2 | 330903500010001630 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 7 | 一体化塔房 | 2 | 330903500000000054 | ☐ ground ☐ rooftop ☐ 保持 unknown | |
| 8 | 楼顶景观塔 | 2 | 330921908000000237 | ☐ ground ☐ rooftop ☐ 保持 unknown | 类型名含"楼顶"，当前 marker 只覆盖 楼面/屋顶/屋面/rooftop/roof；**若裁定 rooftop，需要显式补充 marker** |
| 9 | （空） | 1 | 330922908000000026 | ☐ ground ☐ rooftop ☐ 保持 unknown | 源数据没有填细分类型 |

## 6. 裁定后如何落地（需要你确认方式，本轮未做）

| 需求 | 落地方式 | 是否属于本轮范围 |
|---|---|---|
| 把某一类判为 `ground` **或** 全部判为同一类 | 显式配置 `tower_obstacle_policy.default_base_type`（现有字段，UI 未暴露） | 否，等裁定 |
| 逐类映射（例如"角钢塔=ground、楼顶景观塔=rooftop"同时生效） | 需要新增逐类映射契约（新字段 + 新 UI），或把裁定写回源数据的"铁塔细分类型"列 | 否，等裁定 |
| 让"楼顶"被识别为 rooftop | 显式扩充 `ROOFTOP_MARKERS`（属分类规则改动，会改变 TowerObstacleProfile 判定） | 否，等裁定 |
| 补全第 9 条的源数据类型 | 由报送单位补数据 | 否，等裁定 |

在这之前，117 个 `unknown` 塔的 `TowerObstacleProfile.status` 保持 `unresolved`：

* 它们的塔顶不参与航路净空（不会被视为障碍物，也不会被视为"没有塔"）；
* 它们仍然正常出现在地图、共塔候选与 CNS 规划宿主候选池里（位置是真实事实）；
* 共塔候选的服务原点仍为未确认（`vertical_profile.confirmed = false`），因此不会让规划
  基于一个未解析的塔顶高度下结论。

## 7. 本轮的边界声明

* 未修改 `TowerObstacleProfile` 数学（高度推导、垂直基准、fail-closed 语义）；
* 未修改分类规则（`ROOFTOP_MARKERS` / `GROUND_MARKERS` / `classify_base_type`）；
* 未修改共塔 `REUSE_TIERS` 顺序；
* 未修改原始铁塔 Excel，也未把统计结果写回项目 state。
