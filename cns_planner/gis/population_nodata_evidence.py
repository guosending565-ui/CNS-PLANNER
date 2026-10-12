"""开阔海域零人口工程假设的**逐格证据**装配（Round35-D）。

本模块只做一件事：把 canonical 陆域掩膜（:class:`cns_planner.gis.radar_layout_adapter.LandMaskSource`，
与 surface classification **同源**）的**格级**几何事实装配成
:func:`cns_planner.domain.population_nodata.open_sea_zero_permitted` 能消费的证据字典。

分层与边界
----------
* 它不复制任何陆海判定算法：几何事实全部来自 ``LandMaskSource.cell_land_relation()``；
* 它不写文件、不读 ProjectState、不改任何算法阈值；纯函数 + 只读探测；
* 它**不**在未确认模式二时被调用（调用方先看 ``open_sea_evidence_required()``），
  因此普通请求不会因此扫描 GIS 数据；
* 证据只说明"该格是否是开阔海域"，**不**说明"该格人口为 0"——后者永远由
  ``population_nodata_policy`` 的显式确认加上"缺少有效观测"共同决定。

关键要求（任务书 D3-B）
-----------------------
:meth:`LandMaskSource.cell_land_relation` 回答的是"**整个格子**与陆域的关系"，因此
"格心判为 sea、但格边缘实际包含陆地"的格子会得到 ``land_intersects``，
**不会**被当成开阔海域。数据有效 footprint 之外的格子得到 ``outside_data_footprint``，
同样不会被自动当海。
"""

from __future__ import annotations

from ..domain.population_nodata import (
    OPEN_SEA_ELIGIBILITY_CONDITIONS, open_sea_evidence_required,
)
from .land_mask_source import build_land_mask_source


def build_open_sea_evidence(grid, *, land_mask_path, surface_policy, nodata_policy):
    """按当前确认的 NoData 策略装配逐格陆域缺席证据。

    未确认模式二时返回 ``None``（**零开销**，调用方无需再做任何事）。
    确认了模式二但没有可用的 canonical 陆域源时返回 ``{}``：这是**空证据**，
    映射层会对每一格 fail-closed 拒绝判 0（绝不"没有掩膜就当海"）。
    """

    if not open_sea_evidence_required(nodata_policy):
        return None
    source = build_land_mask_source(land_mask_path, surface_policy)
    return open_sea_evidence_from_source(grid, source)


def open_sea_evidence_from_source(grid, source):
    """用给定 ``LandMaskSource``（或 ``None``）为整批网格逐格探测证据。"""

    if source is None:
        return {}
    evidence = {}
    for cell in (grid or {}).get("cells") or []:
        if not isinstance(cell, dict) or not cell.get("grid_id"):
            continue
        evidence[str(cell["grid_id"])] = source.cell_land_relation(cell.get("bbox"))
    return evidence


def open_sea_evidence_summary(evidence):
    """证据分类计数（审计/报告用；不解锁任何语义）。"""

    counts = {
        "open_sea": 0, "land_intersects": 0, "within_coastal_buffer": 0,
        "outside_data_footprint": 0, "unknown": 0,
    }
    if not isinstance(evidence, dict):
        return {"status": "not_built", "counts": dict(counts), "total": 0}
    for item in evidence.values():
        relation = str((item or {}).get("relation") or "unknown")
        counts[relation if relation in counts else "unknown"] += 1
    return {
        "status": "built",
        "counts": counts,
        "total": len(evidence),
        "eligibility_conditions": list(OPEN_SEA_ELIGIBILITY_CONDITIONS),
        "land_intersects_never_becomes_zero": True,
    }


__all__ = [
    "build_open_sea_evidence", "open_sea_evidence_from_source",
    "open_sea_evidence_summary",
]
