from cns_planner.data.mapping.airspace_eligibility import AirspaceEligibilityService


def _cells(width=3, height=3):
    return [
        {
            "grid_id": f"G-{row}-{column}", "row": row, "column": column,
            "bbox": [column, row, column + 1, row + 1],
            "center": [column + 0.5, row + 0.5],
        }
        for row in range(height) for column in range(width)
    ]


def _feature(feature_id, coordinates):
    return {
        "feature_id": feature_id,
        "geometry": {"type": "Polygon", "coordinates": [coordinates]},
        "category": "source fact only",
        "type": None,
        "crs": {"normalized_geometry": "EPSG:4326"},
        "source": {"file": "fixture.geojson"},
    }


def _policies(*allowed, blocked=()):
    return {"items": [
        {"feature_id": feature_id, "route_eligibility": "allowed", "confirmed": True, "source": "test"}
        for feature_id in allowed
    ] + [
        {"feature_id": feature_id, "route_eligibility": "blocked", "confirmed": True, "source": "test"}
        for feature_id in blocked
    ]}


def test_no_confirmed_allowed_is_missing_and_policy_not_inferred_from_category():
    feature = _feature("A", [[0, 0], [2, 0], [2, 1], [0, 1], [0, 0]])
    result = AirspaceEligibilityService().build(_cells(2, 1), [feature], {"items": []})
    assert result["status"] == "missing_data"
    assert result["allowed_grid_ids"] == []
    assert result["feature_counts"] == {"allowed": 0, "blocked": 0, "unknown": 1}


def test_partial_cell_is_not_allowed_but_complete_inside_cells_and_edges_pass():
    cells = _cells(3, 1)
    feature = _feature("A", [[0, 0], [2.5, 0], [2.5, 1], [0, 1], [0, 0]])
    result = AirspaceEligibilityService().build(cells, [feature], _policies("A"))
    assert result["status"] == "passed"
    assert result["allowed_grid_ids"] == ["G-0-0", "G-0-1"]
    assert result["allowed_edges"] == [["G-0-0", "G-0-1"]]
    assert "G-0-2" not in result["connector_safe_grid_ids"]


def test_concave_polygon_has_no_shortcut_through_missing_center():
    # U-shaped polygon covers left/right columns and bottom row, but excludes top center.
    polygon = [[0, 0], [3, 0], [3, 3], [2, 3], [2, 1], [1, 1], [1, 3], [0, 3], [0, 0]]
    result = AirspaceEligibilityService().build(
        _cells(), [_feature("U", polygon)], _policies("U"),
    )
    assert "G-1-1" not in result["allowed_grid_ids"]
    assert "G-2-1" not in result["allowed_grid_ids"]
    assert ["G-2-0", "G-2-2"] not in result["allowed_edges"]


def test_diagonal_corner_cutting_and_disjoint_islands_have_no_edge():
    diagonal_features = [
        _feature("A", [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]),
        _feature("B", [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]),
    ]
    diagonal = AirspaceEligibilityService().build(
        _cells(2, 2), diagonal_features, _policies("A", "B"),
    )
    assert diagonal["allowed_grid_ids"] == ["G-0-0", "G-1-1"]
    assert diagonal["allowed_edges"] == []

    islands = [
        _feature("L", [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]),
        _feature("R", [[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]),
    ]
    disjoint = AirspaceEligibilityService().build(
        _cells(3, 1), islands, _policies("L", "R"),
    )
    assert disjoint["allowed_grid_ids"] == ["G-0-0", "G-0-2"]
    assert disjoint["allowed_edges"] == []


def test_confirmed_blocked_and_unconfirmed_allowed_are_counted_without_becoming_allowed():
    features = [
        _feature("A", [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]),
        _feature("B", [[1, 0], [2, 0], [2, 1], [1, 1], [1, 0]]),
        _feature("C", [[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]),
    ]
    policies = _policies("A", blocked=("B",))
    policies["items"].append({
        "feature_id": "C", "route_eligibility": "allowed", "confirmed": False,
        "source": "draft",
    })
    result = AirspaceEligibilityService().build(_cells(3, 1), features, policies)
    assert result["feature_counts"] == {"allowed": 1, "blocked": 1, "unknown": 1}
    assert result["allowed_grid_ids"] == ["G-0-0"]
