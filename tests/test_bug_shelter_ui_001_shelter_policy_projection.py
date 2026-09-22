"""BUG-SHELTER-UI-001 后端侧：`shelter_coefficient_policy` 的三条投影必须一致。

前端 Step03 Theta* V2 面板的 checkbox 只读 ``flow.shelter_coefficient_policy.confirmed``，
因此这里固定：

* ``GET /api/shelter-coefficient-policy``（= ``WorkflowService.shelter_coefficient_policy()``）；
* ``POST /api/shelter-coefficient-policy`` 的返回（前端拿它重新渲染）；
* ``WorkflowService.snapshot()["shelter_coefficient_policy"]``（前端真正的数据源）

三者在 ``confirmed`` / ``status`` / ``source`` / ``default_coefficient`` / ``per_grid_overrides``
上必须完全相同。只有能在这里复现"后端 confirmed=true 但投影丢失"时，才允许最小修复
snapshot 同步/投影/重渲染链；否则保持业务代码不变，只保留本测试。
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cns_planner.api.router import ApiRouter  # noqa: E402
from test_phase35_bug_route_002 import (  # noqa: E402
    DEFAULTS, _ApiContext, project,
)

GRID_A = "MHT4063-L8-C00000000-RP00000000"
GRID_B = "MHT4063-L8-C00000001-RP00000000"

CONFIRMED_PAYLOAD = {
    "default_coefficient": 0.75,
    "per_grid_overrides": {GRID_A: 0.25},
    "source": "Phase3.5人工验收：舟山海岛遮蔽系数工程基线",
    "evidence": {"statement": "工程评审记录 ENG-ZS-2026-014"},
    "provenance": "explicit_override",
    "confirmed": True,
}

UNCONFIRMED_PAYLOAD = {
    "default_coefficient": 0.75,
    "per_grid_overrides": {GRID_A: 0.25},
    "source": "Phase3.5人工验收：舟山海岛遮蔽系数工程基线",
    "provenance": "explicit_override",
    "confirmed": False,
}

#: 前端 checkbox 真正读取的那个键。
FRONTEND_KEY = "shelter_coefficient_policy"


def service_for(tmp_path, name):
    service, _ = project(tmp_path, name=name)
    return service


def frontend_policy(snapshot):
    """模拟前端 ``layeredThetaV2Model(flow)`` 的取值路径。"""

    assert isinstance(snapshot, dict), type(snapshot)
    assert FRONTEND_KEY in snapshot, (
        f"workflow snapshot 必须包含 {FRONTEND_KEY}：否则前端读不到 confirmed，"
        "checkbox 永远无法回填"
    )
    return snapshot[FRONTEND_KEY]


def test_get_projection_and_snapshot_agree_when_confirmed(tmp_path):
    service = service_for(tmp_path, "bug-shelter-ui-001-confirmed.json")
    saved = service.set_shelter_coefficient_policy(deepcopy(CONFIRMED_PAYLOAD))
    projected = service.shelter_coefficient_policy()
    snapshot = service.snapshot()

    assert projected["confirmed"] is True
    assert projected["status"] == "confirmed"
    assert projected["status_reason"] is None
    assert projected["default_coefficient"] == 0.75
    assert projected["source"] == CONFIRMED_PAYLOAD["source"]
    assert projected["per_grid_overrides"] == {GRID_A: 0.25}

    # POST 返回体（前端立即用来重新渲染的那份）。
    assert frontend_policy(saved) == projected
    # workflow snapshot（前端真正的数据源）。
    assert frontend_policy(snapshot) == projected
    assert frontend_policy(snapshot)["confirmed"] is True


def test_get_projection_and_snapshot_agree_when_not_confirmed(tmp_path):
    service = service_for(tmp_path, "bug-shelter-ui-001-unconfirmed.json")
    saved = service.set_shelter_coefficient_policy(deepcopy(UNCONFIRMED_PAYLOAD))
    projected = service.shelter_coefficient_policy()
    snapshot = service.snapshot()

    assert projected["confirmed"] is False
    assert projected["status"] == "pending_confirmation"
    assert projected["status_reason"] == "shelter_coefficient_policy_not_confirmed"
    assert frontend_policy(saved) == projected
    assert frontend_policy(snapshot) == projected
    assert frontend_policy(snapshot)["confirmed"] is False


def test_api_post_then_get_and_snapshot_agree(tmp_path):
    service = service_for(tmp_path, "bug-shelter-ui-001-api.json")
    router = ApiRouter(_ApiContext(service))

    posted = router.post("/api/shelter-coefficient-policy", deepcopy(CONFIRMED_PAYLOAD)).data
    assert frontend_policy(posted)["confirmed"] is True

    fetched = router.get("/api/shelter-coefficient-policy", {}, {}).data
    assert fetched["confirmed"] is True
    assert fetched == router.post(
        "/api/shelter-coefficient-policy", deepcopy(CONFIRMED_PAYLOAD)
    ).data[FRONTEND_KEY]

    # 三条投影必须逐字段相同（前端只会读其中一条）。
    assert frontend_policy(posted) == fetched
    assert frontend_policy(service.snapshot()) == fetched


def test_the_confirmed_policy_survives_a_project_reload_and_stays_projected(tmp_path):
    path = tmp_path / "bug-shelter-ui-001-reload.json"
    service = service_for(tmp_path, path.name)
    service.set_shelter_coefficient_policy(deepcopy(CONFIRMED_PAYLOAD))

    from cns_planner.application.workflow_service import WorkflowService

    reloaded = WorkflowService(path, DEFAULTS)
    projected = reloaded.shelter_coefficient_policy()
    assert projected["confirmed"] is True
    assert projected["source"] == CONFIRMED_PAYLOAD["source"]
    assert frontend_policy(reloaded.snapshot()) == projected


def test_the_projection_never_invents_confirmations_for_default_state(tmp_path):
    service = service_for(tmp_path, "bug-shelter-ui-001-default.json")
    service.set_shelter_coefficient_policy({"confirmed": False})
    policy = frontend_policy(service.snapshot())
    # 未显式确认时永远保持未确认：不猜系数、不伪造 confirmed。
    assert policy["confirmed"] is False
    assert policy["status"] != "confirmed"
