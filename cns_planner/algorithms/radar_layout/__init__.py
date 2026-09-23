"""Radar Surveillance Layout V1 — 方向性雷达几何初步划设（additive）。

模块边界（不修改任何既有语义）：

* :mod:`.geometry` —— 米制平面几何、航路里程采样、单面阵覆盖判定；
* :mod:`.candidates` —— ``tower × radar_type`` 候选方向生成与确定性剪枝；
* :mod:`.milp` —— ``scipy.optimize.milp`` + HiGHS 精确求解（绝不 greedy 冒充最优）；
* :mod:`.v1` —— 两阶段（I-only → 必要时 I+II）编排与 5 m 连续覆盖复核。

本包不 import QGIS/GDAL、不读文件、不写任何既有结果容器；GIS 事实由
``cns_planner.gis.radar_layout_adapter`` 在 Application 边界注入。
"""

from .v1 import (  # noqa: F401
    LAYOUT_STATUSES, SAMPLE_COVERAGE_STATUSES, SOFTWARE_BASELINE, STAGE_I_ONLY,
    STAGE_LABELS, STAGE_MIXED, actual_site_coverage, build_route_samples,
    parameters_block, required_count_for, solve_layout, validation_report,
)

__all__ = [
    "LAYOUT_STATUSES", "SAMPLE_COVERAGE_STATUSES", "SOFTWARE_BASELINE", "STAGE_I_ONLY",
    "STAGE_LABELS", "STAGE_MIXED", "actual_site_coverage", "build_route_samples",
    "parameters_block", "required_count_for", "solve_layout", "validation_report",
]
