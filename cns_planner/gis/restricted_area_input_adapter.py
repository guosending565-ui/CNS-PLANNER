"""受限区域输入的唯一组装点：显式 restricted_areas ⊕ regulatory_constraints。

为什么需要这一层
----------------
正式产品的受限区事实上存在于**两个**互不相通的入口：

* ``state["restricted_areas"]`` —— Planning Constraint Field 直接消费的受限区集合
  （``domain.restricted_area`` 契约，分 ``airspace`` / ``critical_site`` 两个域）；
* ``state["regulatory_constraints"]`` —— 正式产品的法规约束接口
  （``domain.regulatory_constraints`` 契约，``no_fly_zone`` / ``restricted_zone`` /
  ``corridor_reservation``）。

两者之间原本**完全没有桥接**，因此即使产品里配置了法规约束，PCF 也仍然全域
``restricted_area_dataset_unresolved`` / ``protected_site_dataset_unresolved``。

本模块建立**单向** adapter：``regulatory_constraints`` → PCF restricted area domain
input。方向是单向的：PCF 的受限区结论绝不写回 ``regulatory_constraints``，也绝不改变
既有 ``evaluate_regulatory_intersection`` 的语义。

硬边界（与用户契约一致）
------------------------
* **不能直接等价的字段保持 unresolved，绝不猜**：水平几何只有 ``polygon`` /
  ``bbox`` 两种可等价转换成 GeoJSON Polygon；其它几何形态**不桥接**并如实记录；
  垂向基准不是 EGM2008 正高时（``agl`` / ``unknown``）**不换算**，该区域保持
  ``unknown``；
* ``confirmed`` 仍然要求 item 自己带 ``source`` 与 ``evidence``：缺一即不是已确认，
  PCF 侧保持 ``unknown``；
* 分域：``regulatory_constraints`` 只描述空域约束，因此一律映射到 ``airspace``；
  ``critical_site`` **只**能来自显式 ``restricted_areas`` 或用户显式声明；
* 数据集状态只能是 ``confirmed_present`` / ``confirmed_none`` / ``not_configured``，
  且 **``not_configured`` 绝不自动升级为 ``confirmed_none``**：``confirmed_none``
  必须由用户显式确认并同时给出 source / evidence（见
  :func:`declared_domain_states`）。
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.regulatory_constraints import (
    is_configured, normalize_regulatory_constraints,
)
from ..domain.restricted_area import (
    normalize_restricted_area_collection, normalize_restricted_area_declarations,
)

#: 每个 domain 只允许这三个状态（``not_provided`` / ``unresolved`` 归入 not_configured）。
DOMAIN_DATASET_STATES = ("confirmed_present", "confirmed_none", "not_configured")

#: ``regulatory_constraints`` 的几何水平基准：既有 ``evaluate_regulatory_intersection``
#: 就是把约束几何与 ``path_crs == "OGC:CRS84"`` 的航线做经纬度相交，因此这里的等价
#: 声明来自**既有实现语义**，不是新猜测。
REGULATORY_GEOMETRY_CRS = "OGC:CRS84"

#: 约束类型映射：只映射语义明确等价的；受限区/走廊保留**不是**无条件的硬排除，
#: 因此映射为 ``conditional``（需要显式 policy 确认才成为硬排除，否则保持 unknown）。
_CONSTRAINT_TYPE_MAP = {
    "no_fly_zone": "hard_exclusion",
    "restricted_zone": "conditional",
    "corridor_reservation": "conditional",
}

_VERTICAL_REFERENCE_EGM2008 = "egm2008_orthometric"


def domain_dataset_state(value):
    """把一个 domain 状态收敛到三者之一；``not_provided`` / ``unresolved`` → not_configured。"""

    text = str(value or "")
    if text in ("confirmed_present", "confirmed_none"):
        return text
    if text == "not_configured":
        return "not_configured"
    return "not_configured"


def declared_domain_states(value):
    """用户**显式**的域声明 → ``{domain: {status, source, evidence}}``。

    ``value`` 形如::

        {"airspace": {"confirmed_none": True, "source": "...", "evidence": "..."},
         "critical_site": {"confirmed_none": True, "source": "...", "evidence": "..."}}

    关键语义：

    * 只有 ``confirmed_none`` **显式为真**、且同时给出 ``source`` 与 ``evidence`` 时，
      该域才是 ``confirmed_none``；
    * 缺 source / evidence、或未勾选 → 该域 ``not_configured``，PCF 继续报
      ``*_dataset_unresolved``（unknown）；
    * 系统**绝不**自行判断"没有该类约束"。
    """

    raw = value if isinstance(value, dict) else {}
    declarations = normalize_restricted_area_declarations(raw)
    result = {}
    for domain in ("airspace", "critical_site"):
        item = declarations[domain]
        source = item.get("source")
        evidence = item.get("evidence") or []
        confirmed_none = item["confirmed_none"] is True and bool(source) and bool(evidence)
        result[domain] = {
            "status": "confirmed_none" if confirmed_none else "not_configured",
            "source": source if confirmed_none else None,
            "evidence": evidence if confirmed_none else [],
            "confirmed_none_requested": item["confirmed_none"] is True,
            "authority_complete": bool(source) and bool(evidence),
        }
    return result


def _bbox_polygon(bbox):
    west, south, east, north = (float(value) for value in bbox)
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south], [east, south], [east, north], [west, north], [west, south],
        ]],
    }


def _geojson_geometry(geometry):
    """``regulatory_constraints`` 几何 → GeoJSON Polygon；不等价时返回 ``None``。"""

    raw = geometry if isinstance(geometry, dict) else {}
    kind = str(raw.get("kind") or "")
    if kind == "bbox":
        bbox = raw.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return None
        try:
            return _bbox_polygon(bbox)
        except (TypeError, ValueError):
            return None
    if kind == "polygon":
        rings = []
        for ring in raw.get("polygon") or []:
            points = [
                [float(point[0]), float(point[1])]
                for point in ring or []
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
            if len(points) >= 4:
                rings.append(points)
        if not rings:
            return None
        return {"type": "Polygon", "coordinates": rings}
    return None


def _evidence_list(value):
    if isinstance(value, dict):
        return [deepcopy(value)]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [deepcopy(item) for item in (value or [])]


def regulatory_constraints_as_restricted_areas(value):
    """``regulatory_constraints`` → PCF restricted area items（单向、无损、不猜）。

    返回 ``{"configured", "items", "skipped", "provenance"}``。``skipped`` 里的每一条都
    是**不能无损等价**的约束，绝不静默丢弃、也绝不猜一个几何或垂向范围。
    """

    collection = normalize_regulatory_constraints(value)
    configured = is_configured(collection)
    items, skipped = [], []
    for constraint in collection.get("items") or []:
        constraint_id = str(constraint.get("constraint_id") or "")
        geometry = _geojson_geometry(constraint.get("geometry"))
        if geometry is None:
            skipped.append({
                "constraint_id": constraint_id,
                "reason": "regulatory_geometry_not_convertible_to_pcf_geometry",
                "geometry_kind": (constraint.get("geometry") or {}).get("kind"),
            })
            continue
        constraint_type = _CONSTRAINT_TYPE_MAP.get(
            str(constraint.get("constraint_type") or ""), "conditional",
        )
        scope = constraint.get("vertical_scope") or {}
        reference = str(scope.get("vertical_reference") or "unknown")
        unbounded = scope.get("unbounded") is True
        if reference == _VERTICAL_REFERENCE_EGM2008:
            vertical_reference = reference
            lower = scope.get("lower_egm2008_m")
            upper = scope.get("upper_egm2008_m")
        else:
            # 垂向基准不等价：绝不换算，也不把 AGL/未知范围当正高范围。该区域在 PCF 侧
            # 会因 vertical_status=unknown 而保持 unknown（fail-closed）。
            vertical_reference = None
            lower = upper = None
            unbounded = False
        items.append({
            "feature_id": constraint_id,
            "name": constraint_id,
            "category": "regulatory_constraint",
            "domain": "airspace",
            "geometry": geometry,
            "geometry_crs": REGULATORY_GEOMETRY_CRS,
            "constraint_type": constraint_type,
            "lower_altitude_m": lower,
            "upper_altitude_m": upper,
            "vertical_reference": vertical_reference,
            "vertical_unbounded": unbounded,
            "source": deepcopy(constraint.get("source")),
            "evidence": _evidence_list(constraint.get("evidence")),
            "confirmed": bool(constraint.get("confirmed")) and str(constraint.get("status")) == "confirmed",
        })
    return {
        "configured": configured,
        "items": items,
        "skipped": skipped,
        "provenance": {
            "direction": "regulatory_constraints_to_planning_constraint_field_restricted_areas",
            "reverse_write": False,
            "regulatory_dataset_status": collection.get("status"),
            "regulatory_dataset_fingerprint": collection.get("dataset_fingerprint"),
            "regulatory_constraint_count": int(collection.get("count") or 0),
            "converted_count": len(items),
            "skipped": skipped,
            "geometry_crs_source": (
                "existing_regulatory_matching_semantics(route path_crs == OGC:CRS84)"
            ),
            "constraint_type_map": deepcopy(_CONSTRAINT_TYPE_MAP),
            "vertical_reference_conversion_guessed": False,
            "not_equivalent_fields_remain_unresolved": True,
        },
    }


def restricted_area_dataset_inputs(state, *, payload=None, declarations=None):
    """组装 PCF 的受限区域输入（显式 restricted_areas ⊕ regulatory 桥接 ⊕ 用户声明）。

    * 显式 ``restricted_areas`` 的 item 优先（同 ``feature_id`` 时不覆盖用户数据）；
    * ``domain_states``：显式/用户声明优先；否则在 regulatory 数据集**已配置**时把
      ``airspace`` 记为 ``confirmed_present``（因为确实有已配置的约束数据）；
    * ``confirmed_none`` **只**能来自用户显式声明，绝不由"没有数据"推导。
    """

    state = state if isinstance(state, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    explicit_raw = (
        payload["restricted_areas"] if "restricted_areas" in payload
        else state.get("restricted_areas")
    )
    regulatory_raw = (
        payload["regulatory_constraints"] if "regulatory_constraints" in payload
        else state.get("regulatory_constraints")
    )
    declared_raw = (
        declarations if declarations is not None
        else payload.get("restricted_area_declarations", state.get("restricted_area_declarations"))
    )

    explicit = normalize_restricted_area_collection(explicit_raw)
    bridged = regulatory_constraints_as_restricted_areas(regulatory_raw)
    declared = declared_domain_states(declared_raw)

    items = [deepcopy(item) for item in explicit.get("items") or []]
    known = {str(item.get("feature_id")) for item in items}
    for item in bridged["items"]:
        if str(item.get("feature_id")) in known:
            continue
        items.append(item)

    domain_states = {}
    for domain in ("airspace", "critical_site"):
        explicit_state = (explicit.get("domain_states") or {}).get(domain) or {}
        declaration = declared.get(domain) or {}
        if declaration.get("status") == "confirmed_none":
            # 用户显式确认：语义是"按当前依据当前范围内无该类约束"。
            state_entry = {
                "status": "confirmed_none",
                "source": deepcopy(declaration.get("source")),
                "evidence": deepcopy(declaration.get("evidence")),
                "declared_by": "explicit_user_confirmation",
            }
        else:
            state_entry = {
                "status": domain_dataset_state(explicit_state.get("status")),
                "source": deepcopy(explicit_state.get("source")),
                "evidence": deepcopy(explicit_state.get("evidence") or []),
            }
        if (
            state_entry["status"] != "confirmed_none"
            and domain == "airspace"
            and bridged["configured"]
        ):
            state_entry["status"] = "confirmed_present"
            if not state_entry.get("source") or not state_entry.get("evidence"):
                state_entry["source"] = state_entry.get("source") or deepcopy(
                    (normalize_regulatory_constraints(regulatory_raw)).get("source")
                )
                state_entry["evidence"] = (
                    state_entry.get("evidence")
                    or _evidence_list((normalize_regulatory_constraints(regulatory_raw)).get("evidence"))
                )
            state_entry["source_of_status"] = "regulatory_constraints_dataset_configured"
        domain_states[domain] = state_entry

    merged = normalize_restricted_area_collection({
        "source": explicit.get("source"),
        "evidence": explicit.get("evidence"),
        "domain_states": domain_states,
        "items": items,
    })
    merged["declarations"] = {
        domain: {
            "status": declared[domain]["status"],
            "confirmed_none_requested": declared[domain]["confirmed_none_requested"],
            "authority_complete": declared[domain]["authority_complete"],
            "semantics": "user_explicit_confirmation_never_system_inference",
        }
        for domain in ("airspace", "critical_site")
    }
    merged["regulatory_bridge"] = deepcopy(bridged["provenance"])
    merged["domain_dataset_states"] = tuple(DOMAIN_DATASET_STATES)
    merged["not_configured_never_becomes_confirmed_none"] = True
    return merged


__all__ = [
    "DOMAIN_DATASET_STATES", "REGULATORY_GEOMETRY_CRS",
    "declared_domain_states", "domain_dataset_state",
    "regulatory_constraints_as_restricted_areas", "restricted_area_dataset_inputs",
]
