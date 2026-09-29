"""专题制图模板（Presentation / Cartographic Export）。

只包含**版式与样式目录**：模板声明、图层样式键、图例分组。这里没有任何业务算法，
也不打开任何数据集；真正的 QGIS 对象创建在 :mod:`cns_planner.gis.qgis_figure_renderer`。
"""

from .template_catalog import (  # noqa: F401
    AVAILABLE, COMMERCIAL_TEMPLATES, LAYER_DISPLAY_NAMES, LAYER_SOURCE_ROLES, PLANNED,
    ROUTE_OVERVIEW_LEGEND_ORDER, ROUTE_OVERVIEW_PARAMETERS, ROUTE_OVERVIEW_V1,
    ROUTE_OVERVIEW_V1_TEMPLATE, TEMPLATE_SCHEMA_VERSION, catalog, is_available,
    parameters, template,
)

__all__ = [
    "AVAILABLE", "COMMERCIAL_TEMPLATES", "LAYER_DISPLAY_NAMES", "LAYER_SOURCE_ROLES",
    "PLANNED", "ROUTE_OVERVIEW_LEGEND_ORDER", "ROUTE_OVERVIEW_PARAMETERS",
    "ROUTE_OVERVIEW_V1", "ROUTE_OVERVIEW_V1_TEMPLATE", "TEMPLATE_SCHEMA_VERSION",
    "catalog", "is_available", "parameters", "template",
]
