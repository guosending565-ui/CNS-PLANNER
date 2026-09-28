"""Step5 **中立** land-mask 只读分类源装配（Round 2 P0 收口）。

本模块是 Communication / RID 正式 surface classification 的**唯一**陆域源入口：

* 它只消费 ``surface_classification_policy``（中立策略）+ 显式 land-mask 数据源路径；
* 它**绝不**读取 ``radar_surveillance_policy``，也不依赖 Radar layout 的运行时
  ``facts_provider`` —— 新项目无需先运行任何 Radar layout 就能生成 surface facts；
* 唯一分类实现仍是 :class:`cns_planner.gis.radar_layout_adapter.LandMaskSource`，
  本模块不复制任何 polygon / shapely / 海岸线算法，也不提供"用 DEM NoData 推断海洋"
  的任何路径。

旧项目的兼容参数由 :mod:`cns_planner.domain.surface_classification` 的**一次性**
迁移函数在 normalize/backfill 阶段写入中立策略；运行时不存在任何 Radar 回退。
"""

from __future__ import annotations

from ..domain.surface_classification import normalize_surface_classification_policy
from .radar_layout_adapter import LandMaskSource


def land_mask_source_path(state, explicit_path=None):
    """land-mask 数据源路径：显式注入优先，其次项目 state 中已登记的来源记录。

    只解析**数据源路径**这一项事实（与任何 policy 无关）；没有配置时返回 ``None``，
    调用方据此 fail-closed（全部 ``unknown``），绝不猜测。
    """

    if explicit_path:
        return explicit_path
    if not isinstance(state, dict):
        return None
    recorded = state.get("result_index")
    recorded = recorded.get("land_mask") if isinstance(recorded, dict) else None
    if isinstance(recorded, dict) and recorded.get("path"):
        return recorded["path"]
    paths = state.get("data_source_paths")
    value = paths.get("land_mask") if isinstance(paths, dict) else None
    return value or None


def classification_parameters(policy):
    """中立策略里的陆域分类参数（``(layer_name, coastal_buffer_m)``）。

    ``layer_name=None`` 表示由既有读取器按保守关键词自动选层，不是"未配置"。
    """

    normalized = normalize_surface_classification_policy(policy)
    return (
        normalized["land_mask_layer_name"],
        normalized["coastal_uncertainty_buffer_m"],
    )


def build_land_mask_source(path, policy):
    """按**中立** surface classification policy 构造只读陆域分类源。

    未配置数据源路径时返回 ``None``（fail-closed）：``surface_class_facts`` 的每一格
    保持 ``unknown``，**不是**"全部是海"。
    """

    if not path:
        return None
    layer_name, buffer_m = classification_parameters(policy)
    return LandMaskSource(
        path, layer_name=layer_name, coastal_uncertainty_buffer_m=buffer_m,
    )


__all__ = [
    "build_land_mask_source", "classification_parameters", "land_mask_source_path",
]
