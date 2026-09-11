"""Workspace coverage checks over normalized source extents."""


def intersects(left, right):
    return left[0] <= right[2] and left[2] >= right[0] and left[1] <= right[3] and left[3] >= right[1]


def workspace_health(sources, bbox):
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("工作区范围无效")
    bbox = [float(value) for value in bbox]
    population_ok = intersects(bbox, sources.population_bbox_wgs84)
    terrain_ok = intersects(bbox, sources.terrain_bbox_wgs84)
    covered = [layer for layer in sources.layers
               if intersects(bbox, sources.layer_boxes_wgs84.get(layer["id"], [-180, -90, 180, 90]))]
    return {
        "status": "passed" if population_ok and covered else "missing_data",
        "population": {"status": "passed" if population_ok else "missing_data", "message": "人口数据覆盖工作区" if population_ok else "人口数据不覆盖工作区"},
        "airspace": {"status": "passed" if covered else "missing_data", "message": f"{len(covered)} 个空域/本地图层覆盖工作区" if covered else "空域数据不覆盖工作区"},
        "terrain": {"status": "passed" if terrain_ok else "missing_data", "message": "GLO-30 DEM 覆盖工作区" if terrain_ok else "GLO-30 DEM 不覆盖工作区"},
        "buildings": {"status": "missing_data", "message": "建筑数据尚未接入"},
        "property": {"status": "missing_data", "message": "财产暴露数据尚未接入"},
        "loaded_layer_count": len(sources.layers), "covered_layer_count": len(covered),
    }
