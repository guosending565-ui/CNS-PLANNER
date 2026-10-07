"""Round32-J：通用 workflow 快照的有界投影契约。

Round32-J0 只读归因确认，真实项目的冷 ``workflow.snapshot()`` 约 69–78 s、
``/api/workflow`` 约 142.6 MiB，主体是 ``cns_plan_review``（104.98 MB）与重复的
P17 计算证据。本文件锁定修复后的契约：

1. workflow 的 ``cns_plan_review`` 投影不再携带大型审计证据，且**从不 deepcopy** 它们；
2. canonical ``ProjectState`` 里这些证据逐字仍在（只读投影，不删数据）；
3. P18 variant 的前端必需字段全部保留；
4. continuous-service 嵌套 Radar 的逐项大明细不进入 workflow 快照；
5. P17 的 status / 门禁 / limitations / disclosure 逐字段不变（含 fail-closed）；
6. snapshot 调用不写 state、不动 revision；
7. 显式失效缓存后的**真正冷构建**与缓存命中内容一致，且不靠缓存才"变小"；
8. 写操作返回值与 ``GET /api/workflow`` 共享同一份有界 contract。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from cns_planner.api.router import ApiRouter
from cns_planner.application.result_currentness import projected_result
from cns_planner.application.workflow_projection import (
    CONTINUOUS_SERVICE_WORKFLOW_PROJECTION,
    PLAN_REVIEW_DETAIL_ENDPOINT,
    PLAN_REVIEW_WORKFLOW_PROJECTION,
    slim_continuous_service_for_workflow,
    slim_plan_review_for_workflow,
)
from test_corridor_site_planner_v2 import configured, prepare_p17

#: 前后端共用的机器可读投影契约（单一真源）。后端断言真实快照满足它；前端
#: ``round32j_step06_projection_contract.test.mjs`` 用它构造同形 fixture。
_CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures" / "round32j_workflow_projection_contract.json")
    .read_text(encoding="utf-8")
)


class _Context:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = None


def _encoded_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _ready_review(tmp_path):
    """P16 → P17 → P18 全部走正式接口，得到一个已评价的方案审查。"""

    workflow = configured(tmp_path)
    workflow.evaluate_cns_corridor_site_plan()
    prepare_p17(workflow, source="Round32-J fixture 显式工程阈值（绝不是法规阈值）")
    workflow.initialize_cns_plan_review()
    return workflow


# ---------------------------------------------------------------------------
# 1–3. P18 方案审查投影
# ---------------------------------------------------------------------------

class _DeepcopyCounter:
    """哨兵：一旦被 ``deepcopy`` 就计数（用于证明投影从不复制被省略的证据）。"""

    def __init__(self) -> None:
        self.copies = 0

    def __deepcopy__(self, memo):
        self.copies += 1
        return _DeepcopyCounter()


def _review_fixture():
    sentinel = _DeepcopyCounter()
    return sentinel, {
        "status": "current",
        "model_scope": "human_reviewed_plan_decision",
        "automatic_overall_score": None,
        "automatic_rank": None,
        "review_id": "REV-1",
        "baseline_fingerprint": "baseline-fp",
        "input_fingerprints": {"p15": "fp-p15"},
        "selected_variant_id": "PV-1",
        "initialized_from": {"source": "test"},
        "continuous_service_gate": {"status": "acceptable_with_managed_gap"},
        "variants": [
            {
                "variant_id": "PV-1",
                "name": "Baseline",
                "source": "baseline",
                "selected_action_ids": [],
                "source_p16_fingerprint": "p16-fp",
                "baseline_fingerprint": "baseline-fp",
                "notes": "",
                "status": "evaluated",
                "evaluation": {
                    "status": "evaluated",
                    "actions": [{"action_id": "a1"}, {"action_id": "a2"}],
                    "applied_refs": ["ref-1"],
                    # 大型 canonical 审计证据：投影必须省略，且不得 deepcopy。
                    "before_p15": {"routes": [{"route_id": "R1"} for _ in range(500)],
                                   "sentinel": sentinel},
                    "after_p15": {"routes": [{"route_id": "R1"} for _ in range(500)]},
                    "authoritative_hypothetical": {
                        "p14": {"status": "passed"}, "p15": {"status": "passed"},
                        "persisted_as_upstream": False,
                    },
                    "planned_p14_fingerprint": "p14-fp",
                    "planned_p15_fingerprint": "p15-fp",
                    "comparison_matrix": [
                        {"route_id": "R1", "subsystem": "C", "objective_status": "met"},
                    ],
                    "confirmation_gate": {
                        "status": "ready_for_confirmation", "confirmation_allowed": True,
                    },
                    "action_summary": {
                        "selected_count": 2, "explicit_costs_by_unit": {"unit": 3.0},
                    },
                    "continuous_service_projection": {
                        "evaluated_for_this_variant": True,
                        "projected_status": "acceptable_with_managed_gap",
                    },
                },
            },
        ],
    }


def test_plan_review_projection_omits_large_audit_evidence_and_discloses_it():
    _, review = _review_fixture()
    variant = slim_plan_review_for_workflow(review)["variants"][0]
    evaluation = variant["evaluation"]

    assert set(evaluation) == {
        "status", "comparison_matrix", "confirmation_gate", "action_summary",
        "continuous_service_projection", "planned_p14_fingerprint",
        "planned_p15_fingerprint",
        "detail_available", "detail_omitted", "omitted_detail_fields", "detail_endpoint",
    }
    assert evaluation["detail_available"] is True
    assert evaluation["detail_omitted"] is True
    assert evaluation["omitted_detail_fields"] == [
        "actions", "after_p15", "applied_refs", "authoritative_hypothetical", "before_p15",
    ]
    assert evaluation["detail_endpoint"] == PLAN_REVIEW_DETAIL_ENDPOINT
    for omitted in ("before_p15", "after_p15", "authoritative_hypothetical", "actions"):
        assert omitted not in evaluation, omitted
    # variant / review 顶层元数据与顺序逐字保留。
    assert variant["variant_id"] == "PV-1"
    assert variant["selected_action_ids"] == []
    assert variant["name"] == "Baseline" and variant["source"] == "baseline"


def test_plan_review_projection_never_deepcopies_omitted_evidence():
    sentinel, review = _review_fixture()
    projected = slim_plan_review_for_workflow(review)

    # 哨兵位于被省略的 before_p15 内：投影不得对它做 deepcopy（否则冷构建依旧慢）。
    assert sentinel.copies == 0
    assert projected["variants"][0]["evaluation"]["detail_omitted"] is True
    # 保留字段仍然彼此独立（隔离语义不变）。
    projected["variants"][0]["evaluation"]["comparison_matrix"][0]["subsystem"] = "tampered"
    assert review["variants"][0]["evaluation"]["comparison_matrix"][0]["subsystem"] == "C"


def test_plan_review_projection_keeps_step06_consumed_fields_verbatim():
    _, review = _review_fixture()
    evaluation = slim_plan_review_for_workflow(review)["variants"][0]["evaluation"]
    canonical = review["variants"][0]["evaluation"]

    for name in (
        "status", "comparison_matrix", "confirmation_gate", "action_summary",
        "continuous_service_projection", "planned_p14_fingerprint",
        "planned_p15_fingerprint",
    ):
        assert evaluation[name] == canonical[name], name
    projected = slim_plan_review_for_workflow(review)
    assert projected["workflow_projection"] == PLAN_REVIEW_WORKFLOW_PROJECTION
    assert projected["status"] == review["status"]
    assert projected["selected_variant_id"] == review["selected_variant_id"]
    assert projected["variant_count"] == 1
    assert projected["variants_detail_endpoint"] == PLAN_REVIEW_DETAIL_ENDPOINT


# ---------------------------------------------------------------------------
# 4–5. P17 连续服务投影
# ---------------------------------------------------------------------------

def _p17_result_fixture():
    return {
        "schema_version": 1,
        "status": "acceptable_with_managed_gap",
        "baseline_status": "unacceptable",
        "post_plan_status": "acceptable_with_managed_gap",
        "algorithm_id": "continuous_service_acceptability_v1",
        "algorithm_version": "2.2",
        "managed_gap_count": 1,
        "unacceptable_count": 0,
        "unknown_count": 0,
        "primary_threat_status": "passed",
        "supplementary_threat_status": "limitation",
        "input_fingerprint": "p17-fp",
        "disclosure_lines": ["managed gap：服务中断 6.0 s ≤ 阈值 8.0 s（缺口真实存在）"],
        "limitations": [{"limitation_id": "L1", "disclosure": "Radar 非合作监视限制"}],
        "routes": [{"route_id": "R1", "status": "acceptable_with_managed_gap"}],
        "parameters": {"parameters": {}},
        "baseline": {"routes": [{"route_id": "R1"}], "status": "unacceptable"},
        "post_plan_projection": {
            "available": True,
            "status": "acceptable_with_managed_gap",
            "route_count": 1,
            "applied_action_ids": ["candidate_site:S1:C1"],
            "projection_semantics": (
                "post_plan_projection_hypothetical_state_never_written_into_existing_cns"
            ),
            "persisted_as_upstream": False,
            "comparison": {
                "improved_service_count": 1, "remaining_gap_count": 0,
                "improved_services": [], "remaining_gaps": [],
            },
            "radar_surveillance_layout": {
                "status": "feasible",
                "count": 2000,
                "collection_id": "RAD-1",
                "schema_version": 1,
                "algorithm_id": "radar_surveillance_layout_v1",
                "algorithm_version": "1.0",
                "proposal_only": True,
                "evaluated_at": "2026-01-01T00:00:00Z",
                "model_scope": "radar_surveillance_layout",
                "boundaries": {"min_altitude_m": 50},
                "items": [{"sample_id": f"s{index}"} for index in range(2000)],
            },
        },
    }


def test_continuous_service_projection_summarises_nested_radar_detail():
    result = _p17_result_fixture()
    slim = slim_continuous_service_for_workflow(result)

    assert slim["workflow_projection"] == CONTINUOUS_SERVICE_WORKFLOW_PROJECTION
    assert slim["detail_available"] is True
    # 顶层字段逐字保留（除了被摘要化的 post_plan_projection）。
    for name in (
        "status", "baseline_status", "post_plan_status", "managed_gap_count",
        "unacceptable_count", "unknown_count", "primary_threat_status",
        "supplementary_threat_status", "input_fingerprint", "disclosure_lines",
        "limitations", "routes", "parameters", "baseline",
    ):
        assert slim[name] == result[name], name

    radar = slim["post_plan_projection"]["radar_surveillance_layout"]
    assert "items" not in radar
    assert radar["items_count"] == 2000
    assert radar["items_detail"] == "artifact"
    assert radar["detail_omitted"] is True
    assert radar["omitted_detail_fields"] == ["items"]
    # Radar 的**业务结论**逐字保留，绝不只是被删掉。
    for name in ("status", "count", "collection_id", "schema_version", "algorithm_id",
                 "algorithm_version", "proposal_only", "boundaries"):
        assert radar[name] == result["post_plan_projection"]["radar_surveillance_layout"][name], name


def test_continuous_service_projection_keeps_step6_consumed_fields_verbatim():
    result = _p17_result_fixture()
    post = slim_continuous_service_for_workflow(result)["post_plan_projection"]
    canonical = result["post_plan_projection"]

    assert set(post) == set(canonical)
    for name in canonical:
        if name == "radar_surveillance_layout":
            continue
        assert post[name] == canonical[name], name
    # 摘要化后的嵌套 Radar 仍是一个可判定对象，而不是被删成 None。
    assert isinstance(post["radar_surveillance_layout"], dict)
    assert post["radar_surveillance_layout"]["status"] == "feasible"


def test_continuous_service_projection_keeps_unavailable_projection_unavailable():
    """投影绝不把"没有 post-plan 投影"变成"看起来已计算"。"""

    result = {"status": "unknown", "post_plan_projection": None}
    slim = slim_continuous_service_for_workflow(result)
    assert slim["post_plan_projection"] is None
    assert slim["status"] == "unknown"
    # 空容器 / 未评估时原样返回，绝不补一个"看起来已计算"的空结构。
    assert slim_continuous_service_for_workflow(None) is None
    assert slim_continuous_service_for_workflow({}) == {}


# ---------------------------------------------------------------------------
# 6–7. 集成：只读 + 真正冷构建
# ---------------------------------------------------------------------------

def test_workflow_snapshot_omits_plan_review_evidence_but_canonical_state_keeps_it(tmp_path):
    workflow = _ready_review(tmp_path)
    snapshot = workflow.snapshot()

    review = snapshot["cns_plan_review"]
    assert review["workflow_projection"] == PLAN_REVIEW_WORKFLOW_PROJECTION
    assert review["variants"]
    for variant in review["variants"]:
        evaluation = variant["evaluation"]
        assert evaluation["detail_omitted"] is True
        for omitted in ("before_p15", "after_p15", "authoritative_hypothetical", "actions"):
            assert omitted not in evaluation, omitted
        # Step06 的必需字段一个都不能少。
        assert variant["variant_id"] and "selected_action_ids" in variant
        assert "confirmation_gate" in evaluation
        assert "comparison_matrix" in evaluation
        assert "action_summary" in evaluation
        assert "continuous_service_projection" in evaluation

    # canonical state 逐字保留完整审计证据（投影只影响返回值）。
    canonical = workflow.state["cns_plan_review"]["variants"]
    assert canonical
    for variant in canonical:
        assert "before_p15" in variant["evaluation"]
        assert "after_p15" in variant["evaluation"]
        assert "authoritative_hypothetical" in variant["evaluation"]


def test_workflow_snapshot_projection_is_read_only(tmp_path):
    workflow = _ready_review(tmp_path)
    before_state = deepcopy(workflow.state)
    before_revision = workflow.state["revision"]
    before_statuses = deepcopy(workflow.state.get("result_statuses") or {})

    workflow.invalidate_snapshot_cache()
    snapshot = workflow.snapshot()

    assert workflow.state == before_state, "快照投影绝不写 state"
    assert workflow.state["revision"] == before_revision, "快照投影绝不动 revision"
    assert workflow.state["result_statuses"] == before_statuses
    assert snapshot["revision"] == before_revision


def test_cold_snapshot_after_invalidate_is_bounded_and_matches_the_cached_one(tmp_path):
    workflow = _ready_review(tmp_path)

    # 显式失效缓存 ⇒ 这一次是真正的冷构建，绝不靠缓存才"变小"。
    workflow.invalidate_snapshot_cache()
    cold = workflow.snapshot()
    warm = workflow.snapshot()

    assert cold == warm
    assert cold is not warm, "返回的顶层容器必须彼此独立"
    assert cold["cns_plan_review"]["workflow_projection"] == PLAN_REVIEW_WORKFLOW_PROJECTION
    bounded = _encoded_size(cold["cns_plan_review"])
    canonical = _encoded_size(workflow.state["cns_plan_review"])
    assert bounded < canonical, (bounded, canonical)


def test_continuous_service_container_is_shared_with_the_bounded_result(tmp_path):
    """通用快照里 P17 的大结果只存在**一份**对象（不重复投影）。"""

    workflow = _ready_review(tmp_path)
    workflow.invalidate_snapshot_cache()
    snapshot = workflow.snapshot()

    container = snapshot["continuous_service_acceptability"]
    assert container is snapshot["cns_continuous_service"]["result"]
    assert container["workflow_projection"] == CONTINUOUS_SERVICE_WORKFLOW_PROJECTION
    radar = ((container.get("post_plan_projection") or {})
             .get("radar_surveillance_layout") or {})
    if radar:
        assert "items" not in radar, "嵌套 Radar 的逐项证据体绝不进入通用快照"
    # canonical 的容器一处都不能少（投影只影响返回值）。
    assert "post_plan_projection" in workflow.state["continuous_service_acceptability"]


def test_workflow_snapshot_drops_the_nested_radar_evidence(tmp_path):
    """把真实的 P17 投影态（含 2000 条 Radar 证据）灌进 state，快照必须摘要化它。"""

    workflow = _ready_review(tmp_path)
    fixture = _p17_result_fixture()
    workflow.state["continuous_service_acceptability"]["post_plan_projection"] = (
        fixture["post_plan_projection"]
    )
    workflow.invalidate_snapshot_cache()
    snapshot = workflow.snapshot()

    container = snapshot["continuous_service_acceptability"]
    radar = container["post_plan_projection"]["radar_surveillance_layout"]
    assert "items" not in radar
    assert radar["items_count"] == 2000
    assert radar["items_detail"] == "artifact"
    assert radar["status"] == "feasible"
    assert container["post_plan_projection"]["available"] is True
    assert container["post_plan_projection"]["applied_action_ids"] == [
        "candidate_site:S1:C1",
    ]

    bounded = _encoded_size(container)
    canonical = _encoded_size(workflow.state["continuous_service_acceptability"])
    assert bounded < canonical, (bounded, canonical)
    # 同一份有界投影也是 cns_continuous_service.result。
    assert container is snapshot["cns_continuous_service"]["result"]


# ---------------------------------------------------------------------------
# 5 + 8. P17 门禁不变；写响应与 GET /api/workflow 共享同一 contract
# ---------------------------------------------------------------------------

def test_p17_step6_gate_is_identical_with_and_without_the_bounded_projection(tmp_path):
    workflow = _ready_review(tmp_path)
    service = workflow.continuous_service_service

    authoritative = service.step6_gate()
    bounded_result = slim_continuous_service_for_workflow(
        projected_result(workflow.state, "continuous_service_acceptability")
    )
    bounded = service.step6_gate(bounded_result)

    assert bounded == authoritative, "有界投影绝不改变任何门禁字段"
    for name in ("status", "confirmation_allowed", "projected_status", "baseline_status",
                 "post_plan_status", "managed_gap_count", "unacceptable_count",
                 "unknown_count", "limitations", "disclosure_lines", "reasons"):
        assert bounded[name] == authoritative[name], name


def test_write_response_and_get_workflow_share_the_same_bounded_contract(tmp_path):
    workflow = _ready_review(tmp_path)
    router = ApiRouter(_Context(workflow))

    written = router.post("/api/cns-plan-review/initialize", {}).data
    fetched = router.get("/api/workflow", {}, {}).data

    for payload in (written, fetched):
        review = payload["cns_plan_review"]
        assert review["workflow_projection"] == PLAN_REVIEW_WORKFLOW_PROJECTION
        assert sorted(review) == sorted(written["cns_plan_review"])
        for variant in review["variants"]:
            assert "before_p15" not in variant["evaluation"]
        assert payload["cns_continuous_service"]["result"]["detail_available"] is True

    assert written["cns_plan_review"]["workflow_projection"] == (
        fetched["cns_plan_review"]["workflow_projection"]
    )


# ---------------------------------------------------------------------------
# 9. 前后端共用的机器可读契约（单一真源）
# ---------------------------------------------------------------------------

def test_real_snapshot_satisfies_the_shared_machine_readable_contract(tmp_path):
    """真实快照必须满足 `tests/fixtures/round32j_workflow_projection_contract.json`。

    前端 ``round32j_step06_projection_contract.test.mjs`` 读同一份清单构造同形 fixture，
    因此这里失败就意味着"后端下发的字段"与"前端锁定的消费契约"已经漂移。
    """

    workflow = _ready_review(tmp_path)
    workflow.invalidate_snapshot_cache()
    snapshot = workflow.snapshot()

    plan = _CONTRACT["plan_review"]
    review = snapshot["cns_plan_review"]
    assert review["workflow_projection"] == plan["workflow_projection"]
    assert review["variants_detail_endpoint"] == plan["detail_endpoint"]
    for name in plan["review_required_fields"]:
        assert name in review, f"review 投影缺少必需字段 {name}"
    assert review["variants"], "契约测试需要至少一个 variant"

    for variant in review["variants"]:
        for name in plan["variant_required_fields"]:
            assert name in variant, f"variant 投影缺少必需字段 {name}"
        evaluation = variant["evaluation"]
        for name in plan["variant_evaluation_fields"]:
            assert name in evaluation, f"variant.evaluation 缺少消费字段 {name}"
        for name in plan["variant_evaluation_omitted_fields"]:
            assert name not in evaluation, f"审计证据 {name} 不得进入通用快照"

    continuous = _CONTRACT["continuous_service"]
    result = snapshot["cns_continuous_service"]["result"]
    assert result["workflow_projection"] == continuous["workflow_projection"]
    assert result["detail_endpoint"] == continuous["detail_endpoint"]
    for name in continuous["result_required_fields"]:
        assert name in result, f"P17 结果缺少必需字段 {name}"

    post = result.get(continuous["result_projection_field"])
    if isinstance(post, dict):
        for name in continuous["post_plan_required_fields"]:
            assert name in post, f"post_plan_projection 缺少必需字段 {name}"
        radar = post.get("radar_surveillance_layout")
        if isinstance(radar, dict):
            for name in continuous["radar_summary_fields"]:
                assert name in radar, f"Radar 摘要缺少业务结论字段 {name}"
            for name in continuous["radar_omitted_fields"]:
                assert name not in radar, f"Radar 明细 {name} 不得进入通用快照"

