"""产物 currentness 的**唯一权威实现**（Round 29-J）。

裁定（与 Round29-H 的 algorithm-semantics stale 一致，本轮收口）：

* ``ProjectState.result_statuses`` 是**持久化 raw 状态**。加载项目**绝不**改写它，
  也绝不因此自动保存；
* 任何消费方都必须通过本模块读取产物的**有效**状态，绝不直接读 raw；
* 若存储结果由**不同算法语义版本**产生（stored ``algorithm_id`` /
  ``algorithm_version`` 与当前实现不一致），有效状态恒为 ``stale``：

  - 它**不是** ``passed``（绝不把 stale 当 current）；
  - 它**不是** ``failed``（绝不把 stale 当 failed）；
  - stored payload 逐字段保留原内容，只有重新 evaluate 才产生 current 新结果。

本模块只做**只读投影**：不写 ``state``、不重算、不改写任何业务结论字段。
"""

from __future__ import annotations

from copy import deepcopy

from ..algorithms.corridor.v1 import CNSServiceCorridorV1
from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from ..algorithms.coverage.geometric_3d import GeometricCoverage3DV1
from ..algorithms.service_capability.v1 import CNSServiceCapabilityV1
from ..algorithms.timeline.v1 import RouteServiceTimelineV1
from ..domain.cns_continuous_service import (
    CONTINUOUS_SERVICE_ALGORITHM_ID,
    CONTINUOUS_SERVICE_ALGORITHM_VERSION,
)
from ..domain.radar_surveillance_layout import (
    ALGORITHM_ID as RADAR_ALGORITHM_ID,
    ALGORITHM_VERSION as RADAR_ALGORITHM_VERSION,
)
from ..site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2


#: 产物键 → 该产物的**当前**算法语义身份 ``(algorithm_id, algorithm_version)``。
#:
#: 只有登记在这里、且存储结果带 ``algorithm_id`` / ``algorithm_version`` 的产物，
#: 才能被判定"算法语义已变化 ⇒ stale"。未登记的产物退回 raw ``result_statuses``
#: （它们的 currentness 由各自的命令路径负责，本模块**不猜测**）。
RESULT_ALGORITHM_SEMANTICS: dict[str, tuple[str, str]] = {
    "cns_corridor_assessment": (
        CNSServiceCorridorV1.algorithm_id, CNSServiceCorridorV1.algorithm_version,
    ),
    "cns_corridor_gap_assessment": (
        CNSCorridorGapAnalyzerV1.algorithm_id, CNSCorridorGapAnalyzerV1.algorithm_version,
    ),
    "cns_corridor_site_plan": (
        CorridorReuseFirstSitePlannerV2.algorithm_id,
        CorridorReuseFirstSitePlannerV2.algorithm_version,
    ),
    "coverage_3d": (
        GeometricCoverage3DV1.algorithm_id, GeometricCoverage3DV1.algorithm_version,
    ),
    "cns_service_capability": (
        CNSServiceCapabilityV1.algorithm_id, CNSServiceCapabilityV1.algorithm_version,
    ),
    "service_timeline": (
        RouteServiceTimelineV1.algorithm_id, RouteServiceTimelineV1.algorithm_version,
    ),
    "radar_surveillance_layout": (RADAR_ALGORITHM_ID, RADAR_ALGORITHM_VERSION),
    #: Round 29-Q：P17 的补充威胁语义发生变化（已证明的 Radar managed physical gap
    #: 由 ``unknown`` 收口为 ``limitation``），因此 2.0 的旧结果必须 effective stale。
    "continuous_service_acceptability": (
        CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION,
    ),
}

#: ``result_statuses`` 里被显式登记算法语义的产物键（供快照投影遍历）。
PROJECTED_RESULT_KEYS: tuple[str, ...] = tuple(RESULT_ALGORITHM_SEMANTICS)


def current_algorithm_identity(result_key: str) -> tuple[str, str] | None:
    """该产物当前的算法语义身份；未登记时返回 ``None``（绝不猜测）。"""

    return RESULT_ALGORITHM_SEMANTICS.get(str(result_key))


def apply_algorithm_semantics_stale(result, algorithm_id, algorithm_version):
    """在**只读投影**上标注"算法语义版本已变化 ⇒ stale"（Round 29-H）。

    裁定：只要新代码**不能**把旧持久化结果安全视为同一语义，即使上游输入一个字节都没变，
    也必须判 stale —— 绝不能因为 ``input_fingerprint`` 相同就误判 current。

    本函数只改投影对象的 ``status`` / ``stale_reason`` / 诊断块，**不写 state**、不重算、
    不改写任何业务结论字段；``algorithm_id`` / ``algorithm_version`` 缺失的历史结果
    （例如手工构造的最小 fixture）保持不变。
    """

    if not isinstance(result, dict) or not result:
        return result
    stored_id = str(result.get("algorithm_id") or "")
    stored_version = str(result.get("algorithm_version") or "")
    if not stored_id and not stored_version:
        return result
    if stored_id == str(algorithm_id) and stored_version == str(algorithm_version):
        return result
    projected = deepcopy(result)
    projected["status"] = "stale"
    projected["stale_reason"] = "algorithm_semantics_changed"
    projected["algorithm_semantics_stale"] = {
        "stored_algorithm_id": stored_id, "stored_algorithm_version": stored_version,
        "current_algorithm_id": str(algorithm_id),
        "current_algorithm_version": str(algorithm_version),
        "reason": (
            "旧持久化结果由不同算法语义版本产生，不能被安全视为同一结论："
            "必须重算，绝不因为上游输入未变而当作 current"
        ),
    }
    return projected


def projected_result(state, result_key):
    """返回产物存储结果 + 算法语义 stale 只读投影（绝不写 ``state``）。

    未登记算法语义的产物返回**原对象**（只读契约由调用方遵守，与既有
    ``result_snapshot()`` 行为一致），因此本函数不会为未登记的产物制造额外深拷贝。
    """

    container = (state or {}).get(result_key)
    if not isinstance(container, dict) or not container:
        return container
    identity = RESULT_ALGORITHM_SEMANTICS.get(str(result_key))
    if identity is None:
        return container
    return apply_algorithm_semantics_stale(container, identity[0], identity[1])


def effective_result_status(state, result_key, *, default="not_calculated"):
    """返回产物的**有效**状态 —— 所有消费方唯一允许使用的 currentness 判据。

    fail-closed 优先级：raw ``stale`` → 算法语义 stale → raw → ``default``。
    """

    key = str(result_key)
    raw = ((state or {}).get("result_statuses") or {}).get(key)
    if raw == "stale":
        return "stale"
    projected = projected_result(state, key)
    if (
        isinstance(projected, dict)
        and projected.get("status") == "stale"
        and projected.get("stale_reason") == "algorithm_semantics_changed"
    ):
        return "stale"
    return raw or default


def effective_result_statuses(state):
    """``result_statuses`` 的整表只读投影（``/api/state`` 与快照使用）。"""

    statuses = dict((state or {}).get("result_statuses") or {})
    for key in statuses:
        statuses[key] = effective_result_status(state, key)
    return statuses


def is_current_result(state, result_key):
    """"该产物是否为 current 且可评估"。"""

    return effective_result_status(state, result_key) not in (
        "stale", "not_calculated", "missing_data",
    )
