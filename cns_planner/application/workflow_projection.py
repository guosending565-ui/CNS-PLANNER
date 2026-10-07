"""Round32-J：通用 workflow 快照的大容器**只读**投影。

背景（Round32-J0 只读性能归因，真实项目副本 ``project_state.json`` 288.8 MB）：

* ``/api/workflow`` 冷构建约 69–78 s，主体是 ``WorkflowService._project_snapshot()``
  对 ``cns_plan_review``（104.98 MB）做整树 ``deepcopy``；
* P18 的每个 variant 都把 ``before_p15`` / ``after_p15`` /
  ``authoritative_hypothetical``（两个 variant 合计约 100 MB）留在 ``evaluation`` 里：
  它们是 canonical 审计事实，但**通用六步 UI 一个字段都不消费**；
* P17 的 ``post_plan_projection.radar_surveillance_layout.items`` 约 7.56 MB，
  在 ``continuous_service_acceptability`` 与 ``cns_continuous_service.result``
  两个容器里各序列化一次。

本模块只做**展示投影**，四条硬约束：

1. **只读**：不写 ``ProjectState``、不 save、不重算，也不触碰 canonical 结论；
2. **直接构造**：从 canonical 对象构造小返回值，绝不"先 deepcopy 大容器再删字段"
   （否则 HTTP 变小了、冷构建仍然慢）；
3. **allow-list**：只保留通用 UI 真正消费的字段，省略项显式披露
   （``detail_available`` / ``detail_omitted`` / ``omitted_detail_fields``），
   不做"所有叫 variants / items 的都删"这类全局字段名黑名单；
4. **不新增 detail 接口**：完整内容继续由既有专用只读接口提供
   （``GET /api/cns-plan-review``、``GET /api/cns-continuous-service``、
   ``GET /api/radar-surveillance-layout``）。

canonical 审计材料逐字保留在 ``state`` 里；后端业务服务（Confirm / Apply / 报告 /
指纹 / revision）继续消费完整数据，本模块不参与它们的任何路径。
"""

from __future__ import annotations

from copy import deepcopy

#: 投影标识：前端与测试据此确认拿到的是有界投影，而不是 canonical 容器。
PLAN_REVIEW_WORKFLOW_PROJECTION = "plan_review_workflow_projection_v1"
CONTINUOUS_SERVICE_WORKFLOW_PROJECTION = "continuous_service_workflow_projection_v1"

#: 完整 detail 的**既有**专用只读入口（本轮不新增任何接口）。
PLAN_REVIEW_DETAIL_ENDPOINT = "/api/cns-plan-review"
CONTINUOUS_SERVICE_DETAIL_ENDPOINT = "/api/cns-continuous-service"
RADAR_LAYOUT_DETAIL_ENDPOINT = "/api/radar-surveillance-layout"

# ---------------------------------------------------------------------------
# P18 方案审查（cns_plan_review）
# ---------------------------------------------------------------------------

#: review 顶层随快照下发的字段：``empty_plan_review()`` 的字段集，外加运行期由
#: ``PlanReviewService`` 写入的派生字段。其余键只作披露，**绝不深拷贝**。
_PLAN_REVIEW_FIELDS = (
    "status", "model_scope", "automatic_overall_score", "automatic_rank",
    "review_id", "baseline_fingerprint", "input_fingerprints",
    "selected_variant_id", "initialized_from", "reasons",
    "continuous_service_gate", "stale_reason", "schema_version",
)

#: variant 顶层随快照下发的字段（``make_variant()`` 全字段，``evaluation`` 单独投影）。
_PLAN_VARIANT_FIELDS = (
    "variant_id", "name", "source", "selected_action_ids",
    "source_p16_fingerprint", "baseline_fingerprint", "notes", "status",
)

#: ``variant.evaluation`` 中**通用 Step06 UI 真正消费**的字段（allow-list）。
#:
#: 逐项消费点（``cns_planner/web/js/workflow/step06_review.js``）：
#:
#: * ``comparison_matrix``            -> ``comparisonCards`` / ``comparisonMatrixTable``
#: * ``confirmation_gate``            -> ``planReviewSummary`` / ``confirmPanel`` / ``variantCards``
#: * ``action_summary``               -> ``formatCosts``（显式费用，按单位分组）
#: * ``continuous_service_projection`` -> ``variantContinuousServiceProjection``（本 variant P17）
#: * ``status`` / ``planned_p14_fingerprint`` / ``planned_p15_fingerprint`` -> 审计标识
_PLAN_VARIANT_EVALUATION_FIELDS = (
    "status",
    "comparison_matrix",
    "confirmation_gate",
    "action_summary",
    "continuous_service_projection",
    "planned_p14_fingerprint",
    "planned_p15_fingerprint",
)

#: 白名单之外、从通用快照省略的大型 canonical 审计证据（清单只用于披露）。
_PLAN_VARIANT_EVALUATION_EVIDENCE_FIELDS = (
    "before_p15", "after_p15", "authoritative_hypothetical", "actions", "applied_refs",
)


def slim_plan_review_for_workflow(review):
    """P18 ``cns_plan_review`` 的通用 workflow 只读投影。

    canonical review 逐字段保留在 ``state`` 中；这里只挑出六步 UI 需要的
    review 顶层字段与每个 variant 的门禁 / 比较矩阵 / P17 投影结论。
    返回的 ``variants`` 顺序与 canonical 一致（``selected_variant_id`` 的解析
    因此逐字不变）。
    """

    if not isinstance(review, dict):
        return review
    slim = {name: deepcopy(review[name]) for name in _PLAN_REVIEW_FIELDS if name in review}
    variants = [
        slim_plan_variant_for_workflow(variant)
        for variant in (review.get("variants") or [])
        if isinstance(variant, dict)
    ]
    slim["variants"] = variants
    slim["variant_count"] = len(variants)
    slim["variants_detail_available"] = True
    slim["variants_detail_endpoint"] = PLAN_REVIEW_DETAIL_ENDPOINT
    slim["variant_evaluation_detail_omitted_fields"] = list(
        _PLAN_VARIANT_EVALUATION_EVIDENCE_FIELDS
    )
    omitted = sorted(set(review) - set(_PLAN_REVIEW_FIELDS) - {"variants"})
    if omitted:
        slim["review_detail_omitted_fields"] = omitted
    slim["workflow_projection"] = PLAN_REVIEW_WORKFLOW_PROJECTION
    return slim


def slim_plan_variant_for_workflow(variant):
    """单个 P18 variant 的只读投影：元数据 + ``evaluation`` 投影。"""

    if not isinstance(variant, dict):
        return variant
    slim = {name: deepcopy(variant[name]) for name in _PLAN_VARIANT_FIELDS if name in variant}
    slim["evaluation"] = slim_plan_variant_evaluation_for_workflow(variant.get("evaluation"))
    return slim


def slim_plan_variant_evaluation_for_workflow(evaluation):
    """``variant.evaluation`` 的只读投影（省略大型审计证据，显式披露其存在）。

    ``evaluation is None``（尚未评价）原样保留 ``None`` —— "未评价"绝不因为投影
    变成空 dict，也绝不补默认值。
    """

    if not isinstance(evaluation, dict):
        return evaluation
    slim = {
        name: deepcopy(evaluation[name])
        for name in _PLAN_VARIANT_EVALUATION_FIELDS if name in evaluation
    }
    omitted = sorted(
        name for name in _PLAN_VARIANT_EVALUATION_EVIDENCE_FIELDS if name in evaluation
    )
    slim["detail_available"] = True
    slim["detail_omitted"] = bool(omitted)
    slim["omitted_detail_fields"] = omitted
    slim["detail_endpoint"] = PLAN_REVIEW_DETAIL_ENDPOINT
    return slim


# ---------------------------------------------------------------------------
# P17 连续服务可接受性（continuous_service_acceptability / cns_continuous_service.result）
# ---------------------------------------------------------------------------

#: P17 结果里需要替换成**摘要**的计算证据字段（保留业务结论，去掉证据体）。
_P17_RESULT_EVIDENCE_FIELDS = ("post_plan_projection",)

#: P17 post-plan 投影态里同样需要摘要化的计算证据字段。
_P17_POST_PLAN_EVIDENCE_FIELDS = ("radar_surveillance_layout",)

#: P17 post-plan 的 Radar layout 里逐项大明细（真实项目约 7.56 MB）。Radar 的
#: **业务结论**在下面的摘要字段里逐字保留，只去掉逐项证据体。
_P17_RADAR_DETAIL_FIELDS = ("items",)

#: Radar layout 随通用快照下发的业务结论摘要字段（标量 / 小结构）。
_P17_RADAR_SUMMARY_FIELDS = (
    "status", "count", "collection_id", "schema_version", "algorithm_id",
    "algorithm_version", "proposal_only", "evaluated_at", "model_scope",
    "boundaries", "semantics", "note",
)


def slim_continuous_service_for_workflow(result):
    """P17 结果的通用 workflow 只读投影。

    保留：``status`` / ``baseline_status`` / ``post_plan_status`` / 计数 /
    ``routes`` / ``parameters`` / ``limitations`` / ``disclosure_lines`` /
    威胁分层状态 / 指纹 等 Step05 与 Step06 消费的全部字段。
    摘要化：``post_plan_projection.radar_surveillance_layout`` 的逐项证据体
    （Radar 的业务结论摘要仍然逐字保留）。

    ``result`` 为 ``None`` / 空 dict 时原样返回 —— "尚未评估"绝不因为投影变成
    一个"看起来已计算"的空结构。
    """

    if not isinstance(result, dict) or not result:
        return result
    slim = {}
    for key, value in result.items():
        if key in _P17_RESULT_EVIDENCE_FIELDS:
            continue
        slim[key] = deepcopy(value)
    if "post_plan_projection" in result:
        slim["post_plan_projection"] = slim_p17_post_plan_projection_for_workflow(
            result.get("post_plan_projection")
        )
    slim["detail_available"] = True
    slim["detail_endpoint"] = CONTINUOUS_SERVICE_DETAIL_ENDPOINT
    slim["workflow_projection"] = CONTINUOUS_SERVICE_WORKFLOW_PROJECTION
    return slim


def slim_p17_post_plan_projection_for_workflow(post_plan_projection):
    """P17 ``post_plan_projection`` 的只读投影。

    ``comparison`` / ``applied_action_ids`` / ``available`` / ``status`` /
    ``route_count`` / ``projection_semantics`` 等 Step05/Step06 消费的字段全部原样
    保留；只有 Radar 的逐项证据体被摘要化。
    """

    if not isinstance(post_plan_projection, dict):
        return post_plan_projection
    slim = {}
    for key, value in post_plan_projection.items():
        if key in _P17_POST_PLAN_EVIDENCE_FIELDS:
            continue
        slim[key] = deepcopy(value)
    if "radar_surveillance_layout" in post_plan_projection:
        slim["radar_surveillance_layout"] = slim_p17_radar_layout_for_workflow(
            post_plan_projection.get("radar_surveillance_layout")
        )
    return slim


def slim_p17_radar_layout_for_workflow(radar_surveillance_layout):
    """P17 投影态里嵌套 Radar layout 的只读投影。

    保留 Radar 的业务结论（``status`` / ``count`` / 算法身份 / ``boundaries`` /
    ``proposal_only`` 等），去掉逐项 ``items`` 证据体并声明其外置入口 —— 与
    ``layered_route_candidates.masks`` / ``grid.cells`` 的既有做法一致。
    """

    if not isinstance(radar_surveillance_layout, dict):
        return radar_surveillance_layout
    slim = {
        name: deepcopy(radar_surveillance_layout[name])
        for name in _P17_RADAR_SUMMARY_FIELDS if name in radar_surveillance_layout
    }
    omitted = sorted(
        name for name in _P17_RADAR_DETAIL_FIELDS if name in radar_surveillance_layout
    )
    for name in omitted:
        value = radar_surveillance_layout.get(name)
        if isinstance(value, (dict, list)):
            slim[f"{name}_count"] = len(value)
        slim[f"{name}_detail"] = "artifact"
    slim["detail_available"] = True
    slim["detail_omitted"] = bool(omitted)
    slim["omitted_detail_fields"] = omitted
    slim["detail_endpoint"] = RADAR_LAYOUT_DETAIL_ENDPOINT
    return slim


__all__ = [
    "PLAN_REVIEW_WORKFLOW_PROJECTION",
    "CONTINUOUS_SERVICE_WORKFLOW_PROJECTION",
    "PLAN_REVIEW_DETAIL_ENDPOINT",
    "CONTINUOUS_SERVICE_DETAIL_ENDPOINT",
    "RADAR_LAYOUT_DETAIL_ENDPOINT",
    "slim_plan_review_for_workflow",
    "slim_plan_variant_for_workflow",
    "slim_plan_variant_evaluation_for_workflow",
    "slim_continuous_service_for_workflow",
    "slim_p17_post_plan_projection_for_workflow",
    "slim_p17_radar_layout_for_workflow",
]
