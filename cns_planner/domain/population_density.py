"""人口密度的**分母语义**契约（Round35-D）。

背景（Round35-C 实测根因）
-------------------------
``population_density_people_km2`` 这一个字段过去在 ``partial`` 覆盖时用
**有效覆盖面积**作分母（``count / covered_area``），``full`` 时用**格面积**作分母。
字段名不说明分母是什么，于是同一个名字在同一张表里代表了两种不同的量：

* 逐格实测放大倍数 = ``格面积 / 覆盖面积 = 1 / 覆盖率``（中位数 1.00–3.99 与
  ``1/coverage`` 逐档吻合到 3 位有效数字）；
* 覆盖率极小时（未确认 NoData 策略下 57 格 < 2%）会被外推成病态密度
  （最大 6639.7 人/km²，其中一格 count 仅 0.0014 人）。

本模块把**三个量**分开并锁定各自的分母：

===============================================  ==========================================  ==========================
字段                                              定义                                          分母
===============================================  ==========================================  ==========================
``population_count_people``                       ``Σ 值 × min(1, 重叠面积/像元面积)``          ——（人，不是密度）
``population_density_people_km2``                 ``count / 完整格面积``                        **始终**是完整 L8 格面积
``population_density_observed_area_people_km2``   ``count / 真实有效覆盖面积``                  有效覆盖面积
===============================================  ==========================================  ==========================

**风险消费端只使用整格平均密度**（``population_density_people_km2``）：它的分母恒为完整
格面积，因此天然不会有 ``1/覆盖率`` 放大，也不需要为了"消掉病态值"而丢弃真实的面积加权
人口数量。有效覆盖密度仍然保留（它是观测到的局部密度，有报告价值），但它**不冒充**整格
密度。

``MIN_USABLE_SOURCE_COVERAGE_FRACTION`` 只用于判定**有效覆盖密度是否可信**
（``observed_density_confidence``），**不**用于丢弃 ``population_count_people``，
也**不**改变 ``coverage_status`` —— 低覆盖率网格的人口数量与整格平均密度依然是真实、
可用的观测量，只是"观测覆盖区密度"在那点面积上没有统计意义。
"""

from __future__ import annotations

import math
from numbers import Real

#: 采样/映射语义版本。``1.1`` 是"单一密度字段、partial 时用有效覆盖面积"的旧口径；
#: ``1.2`` 是"三个量、整格平均密度作主口径"的现口径。
DENSITY_SEMANTICS_VERSION = "1.2"
LEGACY_DENSITY_SEMANTICS_VERSION = "1.1"

#: 逐格人口数量（人）。面积加权后的真实数量，**始终**保留（不因覆盖率低而丢弃）。
COUNT_FIELD = "population_count_people"

#: **整格平均人口密度**（person/km²）：分母**始终**是完整格面积。风险消费端唯一主输入。
DENSITY_FIELD = "population_density_people_km2"

#: **有效覆盖区人口密度**（person/km²）：分母是真实有效覆盖面积。报告/诊断用途。
OBSERVED_DENSITY_FIELD = "population_density_observed_area_people_km2"

DENSITY_SEMANTICS_WHOLE_CELL = "whole_cell_mean_density"
DENSITY_SEMANTICS_OBSERVED_AREA = "observed_covered_area_density"
DENSITY_SEMANTICS_CONFIRMED_ZERO = "confirmed_zero_population_exposure"

#: ``observed_density_confidence`` 的两个取值。
OBSERVED_DENSITY_SUFFICIENT = "sufficient_source_coverage"
OBSERVED_DENSITY_INSUFFICIENT = "insufficient_source_coverage"

#: 有效覆盖密度的**可信度**下限（工程阈值，非数据事实）。
#:
#: Round35-D 裁定：该阈值**只**限制"有效覆盖密度是否可信"，**不**作为丢弃
#: ``population_count_people`` / 整格平均密度、或把 ``partial`` 降级为 ``missing`` 的理由。
#: 依据：整格平均密度的分母恒为格面积，覆盖率再小也只让密度变小，不再产生病态放大；
#: 因此"低覆盖率 ⇒ 高密度异常"这一旧理由在新口径下不成立（见
#: ``03_coverage_threshold_comparison.csv``）。
MIN_USABLE_SOURCE_COVERAGE_FRACTION = 0.02

#: 低覆盖率格上，有效覆盖密度的诊断值字段（**绝不**进入任何风险消费）。
OBSERVED_DENSITY_DIAGNOSTIC_FIELD = "observed_density_diagnostic_people_km2"

#: 每个量的单位与分母声明（供 JSON 结果与报告直接引用）。
FIELD_CONTRACTS = {
    COUNT_FIELD: {
        "unit": "person",
        "denominator": None,
        "definition": "sum(source_value * min(1, overlap_area / pixel_area))",
    },
    DENSITY_FIELD: {
        "unit": "person/km2",
        "denominator": "full_grid_cell_area",
        "definition": "population_count_people / full grid cell area",
        "density_semantics": DENSITY_SEMANTICS_WHOLE_CELL,
    },
    OBSERVED_DENSITY_FIELD: {
        "unit": "person/km2",
        "denominator": "valid_covered_area",
        "definition": "population_count_people / observed valid covered area",
        "density_semantics": DENSITY_SEMANTICS_OBSERVED_AREA,
    },
}


def whole_cell_density(count, grid_area_m2):
    """整格平均密度：``count / 完整格面积``（分母恒为格面积，与覆盖率无关）。"""

    area = _positive(grid_area_m2)
    value = _finite(count)
    if area is None or value is None:
        return None
    return value / (area / 1_000_000.0)


def observed_area_density(count, covered_area_m2, grid_area_m2, *, coverage_fraction=None):
    """有效覆盖密度：``count / 真实有效覆盖面积``；覆盖率过低时返回 ``None``。

    返回 ``None`` 表示"该点上没有统计意义的观测覆盖密度"，**不是**"人口为 0"。
    调用方必须同时保留 :func:`whole_cell_density` 的结果。
    """

    area = _positive(covered_area_m2)
    grid_area = _positive(grid_area_m2)
    value = _finite(count)
    if area is None or value is None:
        return None
    if grid_area is not None:
        area = min(area, grid_area)
    fraction = _finite(coverage_fraction)
    if fraction is None:
        fraction = area / grid_area if grid_area else None
    if fraction is not None and fraction < MIN_USABLE_SOURCE_COVERAGE_FRACTION:
        return None
    return value / (area / 1_000_000.0)


def observed_density_confidence(coverage_fraction, *, grid_area_m2=None, covered_area_m2=None):
    """有效覆盖密度的可信度标记（``sufficient`` / ``insufficient``）。"""

    fraction = _finite(coverage_fraction)
    if fraction is None:
        area = _positive(covered_area_m2)
        grid_area = _positive(grid_area_m2)
        if area is not None and grid_area:
            fraction = min(area, grid_area) / grid_area
    if fraction is None:
        return None
    return (
        OBSERVED_DENSITY_SUFFICIENT
        if fraction >= MIN_USABLE_SOURCE_COVERAGE_FRACTION
        else OBSERVED_DENSITY_INSUFFICIENT
    )


def density_semantics_record(*, grid_area_m2, coverage_status, coverage_fraction,
                             observed_area_m2=None):
    """两个密度字段的**单位 + 分母 + density_semantics** 显式声明。"""

    grid_area = _finite(grid_area_m2)
    return {
        "semantics_version": DENSITY_SEMANTICS_VERSION,
        "count": dict(FIELD_CONTRACTS[COUNT_FIELD], value_present=True),
        "whole_cell": dict(
            FIELD_CONTRACTS[DENSITY_FIELD],
            support_area_m2=_round(grid_area),
            denominator_source="actual_grid_area",
        ),
        "observed_area": dict(
            FIELD_CONTRACTS[OBSERVED_DENSITY_FIELD],
            support_area_m2=_round(observed_area_m2),
            denominator_source="valid_covered_area",
            confidence=observed_density_confidence(
                coverage_fraction,
                grid_area_m2=grid_area_m2, covered_area_m2=observed_area_m2,
            ),
            minimum_usable_coverage_fraction=MIN_USABLE_SOURCE_COVERAGE_FRACTION,
        ),
        "coverage_status": coverage_status,
        "risk_consumes": DENSITY_FIELD,
        "observed_area_is_not_the_whole_cell_density": True,
    }


def is_current_density_semantics(cell):
    """``cell`` 是否携带**当前口径**（1.2）的整格平均密度。

    旧项目里由 ``1.1`` 代码写下的值在 ``partial`` 时用有效覆盖面积作分母；回填时会被
    显式标注为 ``observed_covered_area_density`` / version ``1.1``，于是这里返回
    ``False`` —— 历史值**保持旧语义**，不会悄悄参与新的风险口径。

    判据与 :func:`density_semantics_disposition` 完全一致（单一实现）：
    ``density_semantics`` 字段是权威，它声明了分母是什么。
    """

    return density_semantics_disposition(cell) == "current"


def density_semantics_disposition(cell):
    """风险消费端对一格密度语义的裁定：``current`` / ``confirmed_zero`` /
    ``legacy_not_usable`` / ``undeclared``。

    * ``current``：显式声明为整格平均密度（或声明为 1.2 版）；
    * ``confirmed_zero``：已确认的零人口暴露（分母同样是整格）；
    * ``legacy_not_usable``：显式声明为**旧口径**有效覆盖密度 —— 不得当作新口径消费；
    * ``undeclared``：没有任何语义声明（上游未标注）—— 放行，但必须带质量标记。
    """

    if not isinstance(cell, dict):
        return "undeclared"
    semantics = str(cell.get("density_semantics") or "").strip()
    if semantics == DENSITY_SEMANTICS_WHOLE_CELL:
        return "current"
    if semantics == DENSITY_SEMANTICS_CONFIRMED_ZERO:
        return "confirmed_zero"
    if semantics == DENSITY_SEMANTICS_OBSERVED_AREA:
        return "legacy_not_usable"
    if not semantics:
        return "undeclared"
    return "undeclared"


def legacy_density_semantics_marker():
    """旧项目历史密度的显式语义标记（回填时写入，绝不重算数值）。"""

    return {
        "density_semantics": DENSITY_SEMANTICS_OBSERVED_AREA,
        "density_semantics_version": LEGACY_DENSITY_SEMANTICS_VERSION,
        "density_semantics_is_current": False,
        "density_semantics_note": (
            "历史值由 1.1 口径写下：partial 覆盖时分母是有效覆盖面积，"
            "不等于整格平均密度。未重新计算前不得作为新口径风险输入。"
        ),
    }


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive(value):
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _round(value):
    number = _finite(value)
    return None if number is None else round(number, 9)


__all__ = [
    "COUNT_FIELD", "DENSITY_FIELD", "DENSITY_SEMANTICS_CONFIRMED_ZERO",
    "DENSITY_SEMANTICS_OBSERVED_AREA", "DENSITY_SEMANTICS_VERSION",
    "DENSITY_SEMANTICS_WHOLE_CELL", "FIELD_CONTRACTS",
    "LEGACY_DENSITY_SEMANTICS_VERSION", "MIN_USABLE_SOURCE_COVERAGE_FRACTION",
    "OBSERVED_DENSITY_DIAGNOSTIC_FIELD", "OBSERVED_DENSITY_FIELD",
    "OBSERVED_DENSITY_INSUFFICIENT", "OBSERVED_DENSITY_SUFFICIENT",
    "density_semantics_disposition", "density_semantics_record",
    "is_current_density_semantics", "legacy_density_semantics_marker",
    "observed_area_density", "observed_density_confidence", "whole_cell_density",
]
