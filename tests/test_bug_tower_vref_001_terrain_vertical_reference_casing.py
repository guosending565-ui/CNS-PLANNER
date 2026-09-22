"""BUG-TOWER-VREF-001 回归测试：FABDEM 垂直基准的大小写不得让地形事实被误降级。

现场证据（修复前）：``GET /api/tower-obstacle-profiles`` 中 373 塔全部 unresolved，
其中 258 塔为 ``terrain_elevation_unresolved``，理由
``terrain_vertical_reference_not_confirmed``，而 ``terrain_vertical_reference``
写的是 ``"EGM2008_orthometric"``（真实 FABDEM metadata casing）。

根因：``cns_planner/gis/fine_environment_adapter.py`` 用 ``.lower()`` 判定 FABDEM
垂直基准（接受 ``"EGM2008_orthometric"``），却把**原始 casing** 作为 terrain fact
的 ``vertical_reference`` 传出；``domain/tower_obstacle.py::_terrain_reading()``
用大小写敏感比较 ``reference == "egm2008_orthometric"``，于是已经有效的地形事实被
错误降级为 unresolved。

本文件只锁定这一条边界：

* 已确认基准（任意 casing / 前后空白）⇒ 归一化为 canonical ``egm2008_orthometric``；
* 其它任何 vertical_reference ⇒ 继续 fail-closed 为 unresolved（绝不放宽）；
* 未知 base_type（115 塔）的保守语义不变。
"""

from __future__ import annotations

from cns_planner.domain.tower_obstacle import (
    EGM2008_ORTHOMETRIC, build_tower_obstacle_profile, build_tower_obstacle_profiles,
)
from cns_planner.gis.tower_obstacle_adapter import build_tower_obstacle_facts

#: 真实 FABDEM metadata 的 casing（现场证据里的原样字符串）。
REAL_FABDEM_VERTICAL_REFERENCE = "EGM2008_orthometric"

#: 只有 strip + case-insensitive 命中 EGM2008 正高的写法才允许被接受。
ACCEPTED_VERTICAL_REFERENCES = (
    REAL_FABDEM_VERTICAL_REFERENCE,
    "egm2008_orthometric",
    "EGM2008_ORTHOMETRIC",
    "Egm2008_Orthometric",
    "  egm2008_orthometric  ",
    "\tEGM2008_ORTHOMETRIC\n",
)

#: 其它垂直基准一律保持 unresolved（fail-closed），绝不因为"像"而被接受。
REJECTED_VERTICAL_REFERENCES = (
    "unknown",
    "",
    "   ",
    None,
    "EGM96_orthometric",
    "egm2008_geoid",
    "egm2008_orthometric_msl",
    "wgs84_ellipsoidal",
    "agl",
    "not_egm2008_orthometric",
    "orthometric",
)


def tower(tower_id="T1", *, site_type="地面角钢塔", height_m=15.0):
    return {
        "tower_id": tower_id, "name": f"塔{tower_id}", "longitude": 122.005,
        "latitude": 30.005, "site_type": site_type, "height_m": height_m,
        "elevation_m": 12.0, "district": "定海区",
        "source": {"type": "file_row", "file_name": "towers.xlsx", "sheet": "Sheet1", "row": 3},
        "evidence": [{"type": "file_row", "sheet": "Sheet1", "row": 3}],
    }


def terrain_fact(elevation=100.0, *, reference=REAL_FABDEM_VERTICAL_REFERENCE, status="passed"):
    return {
        "status": status, "elevation_m": elevation,
        "vertical_reference": reference, "reason": None,
    }


# ======================================================================================
# 1. 真实 metadata casing：terrain accepted 且塔可继续解析
# ======================================================================================


def test_real_fabdem_casing_is_accepted_and_tower_stays_resolved():
    """BUG-TOWER-VREF-001 现场场景：'EGM2008_orthometric' + passed + 有效 elevation。"""

    profile = build_tower_obstacle_profile(
        tower(site_type="地面角钢塔", height_m=15.0),
        terrain=terrain_fact(100.0, reference=REAL_FABDEM_VERTICAL_REFERENCE),
    )
    assert profile["terrain_status"] == "passed"
    assert profile["terrain_reason"] is None
    assert profile["terrain_elevation_m"] == 100.0
    assert profile["status"] == "resolved"
    assert profile["vertical_status"] == EGM2008_ORTHOMETRIC + "_resolved"
    assert profile["tower_top_orthometric_m"] == 115.0


def test_accepted_references_are_written_back_as_canonical():
    """成功识别后对外写回 canonical 写法，不再把原始 casing 泄漏出去。"""

    for reference in ACCEPTED_VERTICAL_REFERENCES:
        profile = build_tower_obstacle_profile(
            tower(), terrain=terrain_fact(100.0, reference=reference),
        )
        assert profile["terrain_status"] == "passed", reference
        assert profile["terrain_vertical_reference"] == EGM2008_ORTHOMETRIC, reference
        assert profile["status"] == "resolved", reference


def test_lowercase_egm2008_reference_keeps_working():
    """既有 canonical 小写写法继续兼容（不因修复而回归）。"""

    profile = build_tower_obstacle_profile(
        tower(), terrain=terrain_fact(100.0, reference=EGM2008_ORTHOMETRIC),
    )
    assert profile["terrain_status"] == "passed"
    assert profile["terrain_vertical_reference"] == EGM2008_ORTHOMETRIC
    assert profile["status"] == "resolved"


# ======================================================================================
# 2. 其它垂直基准继续 fail-closed（绝不放宽为任意字符串）
# ======================================================================================


def test_rejected_references_stay_unresolved():
    for reference in REJECTED_VERTICAL_REFERENCES:
        profile = build_tower_obstacle_profile(
            tower(), terrain=terrain_fact(100.0, reference=reference),
        )
        assert profile["terrain_status"] == "unresolved", reference
        assert profile["status"] == "unresolved", reference
        assert profile["vertical_status"] == "terrain_elevation_unresolved", reference
        assert profile["tower_top_orthometric_m"] is None, reference
        assert profile["terrain_elevation_m"] is None, reference
        assert "terrain_vertical_reference_not_confirmed" in (profile["terrain_reason"] or ""), reference


def test_rejected_references_keep_their_own_text_and_unknown_fallback():
    """未确认基准不写回 canonical，也不把任意字符串说成已确认。"""

    profile = build_tower_obstacle_profile(
        tower(), terrain=terrain_fact(100.0, reference="EGM96_orthometric"),
    )
    assert profile["terrain_vertical_reference"] == "EGM96_orthometric"

    missing = build_tower_obstacle_profile(tower(), terrain=terrain_fact(100.0, reference=None))
    assert missing["terrain_vertical_reference"] == "unknown"


def test_casing_alone_never_bypasses_elevation_or_status_requirements():
    """大小写归一化不改变其它 fail-closed 条件：高程缺失/未 passed 仍 unresolved。"""

    no_elevation = build_tower_obstacle_profile(
        tower(),
        terrain={"status": "passed", "elevation_m": None,
                 "vertical_reference": REAL_FABDEM_VERTICAL_REFERENCE, "reason": None},
    )
    assert no_elevation["terrain_status"] == "unresolved"
    assert no_elevation["status"] == "unresolved"

    not_passed = build_tower_obstacle_profile(
        tower(), terrain=terrain_fact(100.0, reference=REAL_FABDEM_VERTICAL_REFERENCE,
                                       status="unknown"),
    )
    assert not_passed["terrain_status"] == "unresolved"
    assert not_passed["status"] == "unresolved"


# ======================================================================================
# 3. base_type_unknown 的保守语义不变（115 塔仍保持 unresolved）
# ======================================================================================


def test_unknown_base_type_stays_unresolved_even_with_real_casing():
    profile = build_tower_obstacle_profile(
        tower(site_type="角钢塔"), terrain=terrain_fact(100.0),
    )
    assert profile["base_type"] == "unknown"
    assert profile["vertical_status"] == "base_type_unknown"
    assert profile["status"] == "unresolved"
    assert profile["tower_top_orthometric_m"] is None


def test_collection_only_ground_and_rooftop_towers_become_resolved():
    """同一批塔里：可解析的地面塔恢复 resolved，unknown 塔仍 unresolved。"""

    collection = build_tower_obstacle_profiles(
        [tower("T-GROUND", site_type="地面角钢塔", height_m=15.0),
         tower("T-UNKNOWN", site_type="角钢塔", height_m=15.0)],
        terrain_by_tower={
            "T-GROUND": terrain_fact(100.0, reference=REAL_FABDEM_VERTICAL_REFERENCE),
            "T-UNKNOWN": terrain_fact(100.0, reference=REAL_FABDEM_VERTICAL_REFERENCE),
        },
        building_by_tower={},
    )
    assert collection["resolved_count"] == 1
    assert collection["unresolved_count"] == 1
    assert collection["items"]["T-GROUND"]["status"] == "resolved"
    assert collection["items"]["T-GROUND"]["terrain_vertical_reference"] == EGM2008_ORTHOMETRIC
    assert collection["items"]["T-UNKNOWN"]["status"] == "unresolved"
    assert collection["items"]["T-UNKNOWN"]["vertical_status"] == "base_type_unknown"
    assert collection["warnings"] == ["tower_obstacle_unresolved:T-UNKNOWN:base_type_unknown"]


# ======================================================================================
# 4. 端到端：真实 FABDEM metadata casing 经 GIS 边界后不再被降级
# ======================================================================================


class _FabdemTerrainStub:
    """只读 FABDEM 采样器 stub：casing 与 ``FabdemWindowTerrainSource`` 完全一致。"""

    def __init__(self, *, reference=REAL_FABDEM_VERTICAL_REFERENCE, elevation=100.0):
        self.vertical_reference = reference
        self.elevation = elevation
        self.vertical_status = (
            "confirmed" if str(reference).lower() == EGM2008_ORTHOMETRIC else "unresolved"
        )
        self.sampled_ids = []

    def usable(self):
        if self.vertical_status != "confirmed":
            return False, "terrain_vertical_datum_unresolved"
        return True, None

    def sample_cells(self, cells, *, transform):
        result = {}
        for cell in cells:
            cell_id = str(cell["fine_cell_id"])
            self.sampled_ids.append(cell_id)
            result[cell_id] = {
                "data_status": "passed",
                "surface_elevation_max_egm2008_m": self.elevation,
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
                "valid_pixel_count": 4, "nodata_pixel_count": 0, "reason": None,
            }
        return result


def test_adapter_passes_raw_fabdem_casing_through_to_domain():
    """锁定真实 metadata casing 场景：GIS 边界传出 'EGM2008_orthometric'。"""

    towers = {"T1": tower("T1", site_type="地面角钢塔", height_m=15.0)}
    facts = build_tower_obstacle_facts(towers, terrain_source=_FabdemTerrainStub())
    assert facts["terrain"]["T1"]["vertical_reference"] == REAL_FABDEM_VERTICAL_REFERENCE
    assert facts["terrain"]["T1"]["status"] == "passed"


def test_end_to_end_real_casing_yields_resolved_tower():
    """GIS 边界 → domain：真实 casing 的地面塔必须 resolved 且塔顶 = DTM + 塔身。"""

    towers = {"T1": tower("T1", site_type="地面角钢塔", height_m=15.0)}
    facts = build_tower_obstacle_facts(towers, terrain_source=_FabdemTerrainStub(elevation=100.0))
    collection = build_tower_obstacle_profiles(
        list(towers.values()),
        terrain_by_tower=facts["terrain"], building_by_tower=facts["buildings"],
    )
    profile = collection["items"]["T1"]
    assert collection["resolved_count"] == 1, collection["warnings"]
    assert profile["status"] == "resolved"
    assert profile["terrain_status"] == "passed"
    assert profile["terrain_vertical_reference"] == EGM2008_ORTHOMETRIC
    assert profile["tower_top_orthometric_m"] == 115.0


def test_end_to_end_unknown_datum_still_fails_closed_at_adapter():
    """垂直基准真的未确认时，GIS 边界就不采样，domain 继续 unresolved。"""

    stub = _FabdemTerrainStub(reference="unknown")
    towers = {"T1": tower("T1", site_type="地面角钢塔", height_m=15.0)}
    facts = build_tower_obstacle_facts(towers, terrain_source=stub)
    assert stub.sampled_ids == []
    assert facts["terrain"]["T1"]["vertical_reference"] == "unknown"
    assert facts["terrain"]["T1"]["status"] == "unresolved"

    collection = build_tower_obstacle_profiles(
        list(towers.values()),
        terrain_by_tower=facts["terrain"], building_by_tower=facts["buildings"],
    )
    assert collection["items"]["T1"]["status"] == "unresolved"
    assert collection["items"]["T1"]["vertical_status"] == "terrain_elevation_unresolved"
