"""Read-only registry for the R4-A standardized Zhoushan facility inventory.

R4-A 把两份 ``.et`` 汇总表标准化成 241 个去重设施：起降设施 107、机巢 89、
通信基站 18、5G-A 27。本模块只把它们读成**按类别独立**的只读集合：

* 四个类别各有自己的 JSON 文件与 SHA-256，绝不跨类别合并、绝不重分类；
* 保留 ``facility_id``、原始来源行、坐标精度、坐标原文与 CRS 证据状态；
* 不覆盖既有 98 条 ``reference_landing_sites``，也不覆盖 373 座铁塔；
* 数据源未声明 CRS；R4-B-CRS 只登记 ``engineering_assumed``（EPSG:4490），
  不把假设升级为 ``confirmed``，因此坐标只能展示/做只读敏感性，不能做米制量算，
  更不能据此把设施升级成 ``confirmed`` 的 CNS 站址或设备。

本模块不写 ``ProjectState``，不生成覆盖，不生成候选动作。
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from ..domain.reference_crs import CRS84, empty_crs_record


REGISTRY_ID = "zhoushan-standardized-facilities"
REGISTRY_VERSION = "r4a-v1"
COLLECTION_KEYS = ("landing", "nest", "base_station", "5g_a")
_LABELS = {
    "landing": "标准化起降设施",
    "nest": "机巢",
    "base_station": "通信基站",
    "5g_a": "5G-A",
}
DEFAULT_DATA_DIR = Path(__file__).with_name("data") / "zhoushan_standardized_facilities"


def empty_standardized_facilities():
    return {
        "status": "not_calculated",
        "registry_id": REGISTRY_ID,
        "version": REGISTRY_VERSION,
        "semantics": "read_only_reference_inventory_not_installed_equipment",
        "planning_integration": "selection_required",
        "crs": empty_crs_record(note=(
            "R4-A 源 .et 未声明 CRS；EPSG:4490 只是 engineering_assumed 工程假设，"
            "不启用米制量算，也不把坐标升级为 confirmed。"
        )),
        "collections": {},
        "items": [],
        "counts": {"total": 0},
        "audit": {
            "duplicate_facility_ids": [],
            "same_name_groups": [],
            "cross_category_same_name_groups": [],
            "category_name_suggestions": [],
            "shared_registry_link_groups": [],
            "auto_merge": False,
        },
        "integrity": {"status": "not_checked", "files": []},
        "warnings": [],
    }


def _sha256(path):
    return sha256(path.read_bytes()).hexdigest().upper()


def _collection_crs(label, adjudication=None):
    record = empty_crs_record(note=(
        f"{label} 源 .et 未声明 CRS；EPSG:4490 为工程人工裁定（engineering_assumed），"
        "仅用于显示与只读敏感性分析，不启用米制量算，也不把坐标升级为 confirmed。"
    ))
    record["representation_crs"] = {
        "value": CRS84,
        "status": "declared",
        "axis_order": "lon_lat",
        "declared_by_format": False,
        "source": {"type": "source_column_order", "note": "源表列顺序为经度、纬度"},
        "evidence": [{
            "type": "source_column_order",
            "value": "lon_lat",
            "note": "只描述读取顺序，不证明原始 source CRS。",
        }],
    }
    if isinstance(adjudication, dict):
        evidence = _adjudication_evidence(adjudication)
        record["source_crs"] = {
            "value": adjudication.get("assumed_crs") or "EPSG:4490",
            "status": "pending_confirmation",
            "axis_order": "lon_lat",
            "confirmed": False,
            "declared_crs": None,
            "confirmed_crs": None,
            "assumption_status": "engineering_assumed",
            "source": {
                "type": "human_adjudication",
                "origin": "r4b_crs_verification",
                "record_id": adjudication.get("record_id"),
                "adjudicated_by": adjudication.get("adjudicated_by"),
                "adjudicated_at": adjudication.get("adjudicated_at"),
                "method": adjudication.get("method"),
            },
            "evidence": evidence,
            "limitations": deepcopy(adjudication.get("limitations") or []),
            "note": (
                "源文件 declared_crs=null；EPSG:4490 是人工裁定的工程假设，"
                "不是源声明，也不等于 confirmed。"
            ),
        }
        record["adjudication"] = deepcopy(adjudication)
        record["note"] = (
            f"{label} 源 .et 未声明 CRS；EPSG:4490 为 engineering_assumed，"
            "仅用于显示与只读敏感性分析，不解除正式空间门禁。"
        )
    return record


def _adjudication_evidence(adjudication):
    """Compact evidence list for the CRS record (the full record stays in ``adjudication``)."""
    evidence = []
    payload = adjudication.get("evidence") or {}
    controls = payload.get("controls") or []
    if controls:
        evidence.append({
            "type": "independent_wgs84_control_points",
            "value": "OpenStreetMap",
            "note": (
                f"{len(controls)} 个空间分散控制点；WGS84 残差 "
                f"{payload.get('primary_wgs84_m', {}).get('p50')} m (p50)，"
                f"GCJ-02 反算残差 {payload.get('primary_gcj02_m', {}).get('p50')} m (p50)。"
            ),
            "control_ids": [row.get("control_id") for row in controls],
        })
        evidence.append({
            "type": "gcj02_bd09_rejected",
            "value": True,
            "note": (
                "GCJ-02/BD-09 解释下的残差比 WGS84 解释大 "
                f"{payload.get('gcj02_over_wgs84_ratio_p50')} 倍 (p50)，拒绝火星坐标解释。"
            ),
        })
    book = payload.get("book0_vs_book4_same_site") or {}
    if book.get("count"):
        evidence.append({
            "type": "cross_book_consistency",
            "value": book.get("count"),
            "note": (
                "两本 .et 同址配对中 "
                f"{book.get('exact_lt_0_01m')} 条完全一致，"
                f"平均东/北偏移 {book.get('mean_east_m')} / {book.get('mean_north_m')} m；"
                "未发现整表系统性平移。"
            ),
        })
    registry = payload.get("et_vs_registry98_same_site") or {}
    if registry.get("count"):
        evidence.append({
            "type": "cross_source_consistency",
            "value": registry.get("count"),
            "note": (
                "与 98 条参考起降点同址配对中 "
                f"{registry.get('exact_lt_0_01m')} 条完全一致，"
                f"平均东/北偏移 {registry.get('mean_east_m')} / {registry.get('mean_north_m')} m。"
            ),
        })
    towers = payload.get("towers_vs_facilities") or {}
    if towers.get("name_containment_matches"):
        evidence.append({
            "type": "cross_source_tower_consistency",
            "value": towers.get("name_containment_matches"),
            "note": (
                "铁塔 XLSX 与 .et 设施的名称包含、<=200 m 对照 "
                f"{towers.get('name_containment_matches')} 组，"
                f"距离中位数 {(towers.get('distance_m') or {}).get('p50')} m；"
                "同源可能性高，不作绝对证据。"
            ),
        })
    evidence.append({
        "type": "declared_crs_absent",
        "value": None,
        "note": "源 .et/.xlsx 的 CRS 关键词扫描为 0 命中；declared_crs 保持 null。",
    })
    evidence.append({
        "type": "epsg4490_vs_4326_indistinguishable",
        "value": (payload.get("proj_4490_to_4326") or {}).get("max"),
        "note": (
            "PROJ 4490→4326 位移约 0 m，但整秒坐标量化约 31 m、OSM 控制点约 10–50 m，"
            "不能据此区分 4490 与 4326；故停在 engineering_assumed。"
        ),
    })
    return evidence


def _same_name_groups(items):
    groups = {}
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        groups.setdefault(name, []).append(item)
    result = []
    for name, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        result.append({
            "name": name,
            "count": len(members),
            "facility_ids": sorted(str(item.get("facility_id")) for item in members),
            "facility_categories": sorted({str(item.get("facility_category")) for item in members}),
            "distinct_coordinates": len({
                tuple(item.get("coordinate") or ()) for item in members
            }),
            "auto_merge": False,
            "reason": "same_name_is_not_proof_of_same_site",
        })
    return result


def _registry_link_groups(items):
    groups = {}
    for item in items:
        link = str(item.get("registry_match_id") or "").strip()
        if not link:
            continue
        groups.setdefault(link, []).append(item)
    result = []
    for link, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        distances = sorted({
            float(item.get("registry_match_m"))
            for item in members
            if isinstance(item.get("registry_match_m"), (int, float))
        })
        result.append({
            "registry_match_id": link,
            "registry_match_name": members[0].get("registry_match_name"),
            "facility_ids": sorted(str(item.get("facility_id")) for item in members),
            "facility_categories": sorted({str(item.get("facility_category")) for item in members}),
            "registry_match_distances_m": distances,
            "max_distance_spread_m": (max(distances) - min(distances)) if len(distances) > 1 else 0.0,
            "auto_merge": False,
            "reason": "shared_reference_id_is_not_proof_of_same_point",
        })
    return result


def _audit(items):
    ids = {}
    for item in items:
        facility_id = str(item.get("facility_id") or "")
        ids[facility_id] = ids.get(facility_id, 0) + 1
    duplicate_ids = sorted(facility_id for facility_id, count in ids.items() if count > 1)
    same_name = _same_name_groups(items)
    cross_category = [
        group for group in same_name
        if len(group["facility_categories"]) > 1
    ]
    suggestions = [
        {
            "facility_id": item.get("facility_id"),
            "name": item.get("name"),
            "facility_category": item.get("facility_category"),
            "action": "keep_source_category",
            "reason": "name_mentions_landing_but_category_is_explicit",
        }
        for item in items
        if item.get("name_suggests_landing_site")
        and item.get("facility_class") != "landing"
    ]
    return {
        "duplicate_facility_ids": duplicate_ids,
        "same_name_groups": same_name,
        "cross_category_same_name_groups": cross_category,
        "category_name_suggestions": suggestions,
        "shared_registry_link_groups": _registry_link_groups(items),
        "auto_merge": False,
    }


def load_standardized_facilities(root=None):
    """Load the four category-separated collections; never mutate the files."""

    data_dir = Path(root) if root is not None else DEFAULT_DATA_DIR
    result = empty_standardized_facilities()
    if not data_dir.is_dir():
        result["status"] = "blocked"
        result["warnings"].append(f"registry_data_missing:{data_dir}")
        result["integrity"] = {"status": "blocked", "files": []}
        return result

    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        result["status"] = "blocked"
        result["warnings"].append("registry_manifest_missing")
        result["integrity"] = {"status": "blocked", "files": []}
        return result
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    adjudication = manifest.get("crs_adjudication")
    files = []
    integrity_passed = True
    items = []
    for entry in manifest.get("collections") or []:
        key = str(entry.get("key") or "")
        path = data_dir / str(entry.get("file") or "")
        file_check = {
            "key": key,
            "file": path.name,
            "expected_sha256": entry.get("sha256"),
            "actual_sha256": None,
            "status": "missing",
        }
        if key not in COLLECTION_KEYS or not path.is_file():
            integrity_passed = False
            files.append(file_check)
            result["warnings"].append(f"registry_collection_missing:{key}")
            continue
        actual = _sha256(path)
        file_check["actual_sha256"] = actual
        file_check["status"] = "passed" if actual == entry.get("sha256") else "sha256_mismatch"
        if file_check["status"] != "passed":
            integrity_passed = False
        files.append(file_check)
        payload = json.loads(path.read_text(encoding="utf-8"))
        collection_items = [
            deepcopy(item) for item in payload.get("items") or []
            if isinstance(item, dict)
        ]
        for item in collection_items:
            item["collection"] = key
            for field in ("coordinate_precision", "coordinate_format", "coordinate_status"):
                if item.get(field) == "":
                    item[field] = None
        result["collections"][key] = {
            "collection_id": payload.get("collection_id"),
            "label": payload.get("label") or _LABELS[key],
            "facility_class": payload.get("facility_class"),
            "source": deepcopy(payload.get("source")),
            "crs": _collection_crs(
                payload.get("label") or _LABELS[key],
                payload.get("crs_adjudication") or adjudication,
            ),
            "count": len(collection_items),
            "items": collection_items,
        }
        items.extend(collection_items)

    items.sort(key=lambda item: (str(item.get("collection")), str(item.get("facility_id"))))
    result["items"] = items
    result["audit"] = _audit(items)
    result["integrity"] = {"status": "passed" if integrity_passed else "blocked", "files": files}
    result["status"] = "passed" if integrity_passed else "blocked"
    by_collection = {
        key: len(result["collections"].get(key, {}).get("items") or [])
        for key in COLLECTION_KEYS
    }
    by_category = {}
    by_collection_coordinate = {
        key: {"total": 0, "valid_coordinate": 0, "missing_or_invalid_coordinate": 0}
        for key in COLLECTION_KEYS
    }
    for item in items:
        category = str(item.get("facility_category") or "")
        by_category[category] = by_category.get(category, 0) + 1
        bucket = by_collection_coordinate.setdefault(
            str(item.get("collection")),
            {"total": 0, "valid_coordinate": 0, "missing_or_invalid_coordinate": 0},
        )
        bucket["total"] += 1
        if item.get("coordinate"):
            bucket["valid_coordinate"] += 1
        else:
            bucket["missing_or_invalid_coordinate"] += 1
    result["counts"] = {
        "total": len(items),
        "unique_facility_ids": len({item.get("facility_id") for item in items}),
        "by_collection": by_collection,
        "by_collection_coordinate": by_collection_coordinate,
        "landing": by_collection["landing"],
        "nests": by_collection["nest"],
        "base_stations": by_collection["base_station"],
        "5g_a": by_collection["5g_a"],
        "by_category": dict(sorted(by_category.items())),
        "valid_coordinate": sum(bool(item.get("coordinate")) for item in items),
        "missing_or_invalid_coordinate": sum(not item.get("coordinate") for item in items),
    }
    source_books = deepcopy(manifest.get("source_books") or [])
    result["source_books"] = source_books
    top_crs = empty_crs_record(note=(
        "R4-A 源 .et 未声明 CRS；EPSG:4490 只是工程人工裁定（engineering_assumed），"
        "不启用米制量算，也不把坐标升级为 confirmed。"
    ))
    top_crs["representation_crs"] = deepcopy(
        _collection_crs("汇总")["representation_crs"]
    )
    if isinstance(adjudication, dict):
        top_crs["source_crs"] = deepcopy(
            _collection_crs("汇总", adjudication)["source_crs"]
        )
        top_crs["adjudication"] = deepcopy(adjudication)
    result["crs"] = {
        **top_crs,
        "declared_crs": None,
        "confirmed_crs": None,
        "assumed_crs": manifest.get("crs", {}).get("assumed_crs"),
        "assumption": manifest.get("crs", {}).get("assumption"),
        "assumption_status": manifest.get("crs", {}).get("assumption_status"),
        "confirmed": False,
        "metric_measurement_enabled": False,
    }
    result["registry_id"] = manifest.get("registry_id") or REGISTRY_ID
    result["version"] = manifest.get("version") or REGISTRY_VERSION
    result["semantics"] = manifest.get("semantics") or result["semantics"]
    result["planning_integration"] = manifest.get("planning_integration") or "selection_required"
    if result["audit"]["duplicate_facility_ids"]:
        result["warnings"].append("duplicate_facility_ids_present")
    return result
