"""Workspace coverage checks over normalized source extents."""


def intersects(left, right):
    return left[0] <= right[2] and left[2] >= right[0] and left[1] <= right[3] and left[3] >= right[1]


def workspace_health(sources, bbox):
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("工作区范围无效")
    bbox = [float(value) for value in bbox]
    population_ok = intersects(bbox, sources.population_bbox_wgs84)
    terrain_ok = intersects(bbox, sources.terrain_bbox_wgs84)
    terrain_dtm_ok = bool(sources.terrain_dtm_bbox_wgs84) and intersects(bbox, sources.terrain_dtm_bbox_wgs84)
    vector_info = getattr(sources, "vector_info", {}) or {}
    buildings_extent = (vector_info.get("buildings") or {}).get("extent")
    building_grid_extent = (vector_info.get("building_grid") or {}).get("extent")
    buildings_ok = bool(buildings_extent) and intersects(bbox, buildings_extent)
    building_grid_ok = bool(building_grid_extent) and intersects(bbox, building_grid_extent)
    covered = [layer for layer in sources.layers
               if intersects(bbox, sources.layer_boxes_wgs84.get(layer["id"], [-180, -90, 180, 90]))]
    return {
        "status": "passed" if population_ok and covered else "missing_data",
        "population": {"status": "passed" if population_ok else "missing_data", "message": "人口数据覆盖工作区" if population_ok else "人口数据不覆盖工作区"},
        "airspace": {"status": "passed" if covered else "missing_data", "message": f"{len(covered)} 个空域/本地图层覆盖工作区" if covered else "空域数据不覆盖工作区"},
        "terrain": {"status": "passed" if terrain_ok else "missing_data", "message": "GLO-30 DEM 覆盖工作区" if terrain_ok else "GLO-30 DEM 不覆盖工作区"},
        "terrain_dtm": {"status": "passed" if terrain_dtm_ok else "missing_data", "message": "FABDEM DTM 覆盖工作区" if terrain_dtm_ok else "FABDEM DTM 不覆盖工作区"},
        "buildings": {"status": "passed" if buildings_ok else "missing_data", "message": "GBA 单体建筑覆盖工作区" if buildings_ok else "GBA 单体建筑不覆盖工作区"},
        "building_grid": {"status": "passed" if building_grid_ok else "missing_data", "message": "L8 建筑环境网格覆盖工作区" if building_grid_ok else "L8 建筑环境网格不覆盖工作区"},
        "property": {"status": "missing_data", "message": "财产暴露数据尚未接入"},
        "loaded_layer_count": len(sources.layers), "covered_layer_count": len(covered),
    }
