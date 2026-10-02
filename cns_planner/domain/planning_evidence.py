"""Round 2.4 —— **工程证据 / 规划假设** 的正式输入契约（纯 domain，无 I/O）。

背景（本轮要解决的真实产品缺口）
--------------------------------

Round 2.3 为止，一旦主链缺一条工程证据（例如"地面通信设备的网络归属范围"或
"机载是否具备网络远程识别参与能力"），唯一的处理方式是**让开发者改数据 / JSON**。
这不满足"人工可使用系统"。本模块把这类缺口变成一等公民：

* **事实与假设严格分开**：每条记录必须显式声明
  :data:`EVIDENCE_SOURCE_TYPES` 之一，且 `engineering_assumption` 必须携带
  ``statement`` / ``source`` / ``confirmed_by_user`` / ``report_disclosure``；
* **绝不写入 device catalog**：工程假设不是厂家设备事实，因此它落在**独立的**
  ``project_state["planning_evidence"]`` 里，并由
  :func:`apply_aircraft_evidence` 在**规划消费点**叠加到机载能力上
  （叠加结果必须带 ``airborne_evidence`` 来源标注供下游披露）；
* **可审计**：每条记录带 ``source`` / ``source_type`` / ``confirmed`` /
  ``confirmation_status`` / ``provenance`` / ``report_disclosure``；
* **绝不按 device_id 硬编码**：``field`` 是唯一路由键，值域由
  :data:`PLANNING_EVIDENCE_FIELDS` **枚举校验**（非法值直接 ``ValueError``）。

与既有 ``assumptions``（Phase4-B1）的关系：这是**另一个**、更窄的容器，专门承载
"参与 CNS 判定的工程证据"，因此它有自己的 field 注册表与披露语义；
``assumptions`` 继续服务 workflow readiness 等既有用途，两者互不覆盖。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone


PLANNING_EVIDENCE_SCHEMA_VERSION = "round2.4-engineering-evidence"

#: 来源类型 —— **事实与假设必须严格分开**。
#:
#: * ``confirmed_source_fact``：有正式资料支持（本模块只登记，绝不代填）；
#: * ``engineering_assumption``：为规划目的采用的工程假设，**必须**带
#:   ``statement`` / ``source`` / ``confirmed_by_user`` / ``report_disclosure``；
#: * ``unknown``：尚无依据（保持 unknown，绝不升级为事实或假设）。
EVIDENCE_SOURCE_TYPES = (
    "confirmed_source_fact",
    "engineering_assumption",
    "unknown",
)

#: 来源类型的权威效应：只有它决定该记录**是否被允许参与判定**。
#:
#: ``unknown`` 永不参与判定（它的作用是如实登记"这里确实没有依据"）。
EVIDENCE_AUTHORITY_EFFECT = {
    "confirmed_source_fact": "allowed_with_disclosure",
    "engineering_assumption": "allowed_with_disclosure",
    "unknown": "not_participating",
}

PROJECT_EVIDENCE_SCOPES = ("project", "aircraft")

#: 子系统证据的合法取值（枚举校验，绝不是自由文本）。
COMMUNICATION_AIRBORNE_INTERFACES = ("ip",)
COOPERATIVE_SURVEILLANCE_TECHNOLOGIES = (
    "network_remote_id", "adsb", "multilateration",
)

#: ---------------------------------------------------------------------------
#: 允许人工补录的工程证据字段（本轮只解决真正阻塞主链的字段）。
#: ---------------------------------------------------------------------------
#:
#: 每个字段声明：
#:
#: * ``subsystem`` / ``kind``：它叠加到机载能力的哪一层；
#: * ``value_type``：``enum`` 逐项校验（列表）或 ``enum_scalar`` 单值校验；
#: * ``allowed``：合法值域（**fail-closed**，绝不放任自由文本）；
#: * ``demand_field``：它在需求侧由哪个 ``type`` 字段表达（用于"为什么需要它"）；
#: * ``label`` / ``semantics``：前端与报告的可读说明。
PLANNING_EVIDENCE_FIELDS = {
    "communication_network_scope": {
        "label": "通信网络归属范围（机载侧）",
        "subsystem": "C",
        "kind": "aircraft_type_field",
        "value_type": "enum_scalar",
        "allowed": ("public", "private", "dedicated", "managed_service", "other"),
        "demand_field": "network_scope",
        "declared_field": "network_scope",
        "semantics": (
            "需求侧 ``type.network_scope`` 是**真实类型门禁**（有枚举约束）。机载能力档案"
            "未声明该字段时判定保持 unknown；本记录为该机载平台显式提供该声明。"
        ),
    },
    "communication_airborne_interfaces": {
        "label": "通信机载接口（机载侧）",
        "subsystem": "C",
        "kind": "aircraft_type_interfaces",
        "value_type": "enum",
        "allowed": COMMUNICATION_AIRBORNE_INTERFACES,
        "demand_field": "interfaces",
        "declared_field": "interfaces",
        "semantics": (
            "需求侧 ``type.interfaces`` 是**真实类型门禁**，且要求机载与地面提供者接口"
            "**有交集**才算兼容。本记录显式追加该机载平台的接口声明（不删除既有声明）。"
        ),
    },
    "remote_id_participation": {
        "label": "机载网络远程识别参与能力",
        "subsystem": "S",
        "kind": "airborne_cooperative_surveillance_services",
        "value_type": "enum",
        "allowed": COOPERATIVE_SURVEILLANCE_TECHNOLOGIES,
        #: 需求侧用 ``technology`` 表达"要求哪种合作监视参与技术"
        #: （``network_remote_id``）；机载侧由参与服务声明列表承载同一事实。
        "demand_field": "technology",
        "declared_field": "cooperative_surveillance_services",
        "semantics": (
            "机载在 RID 服务里的角色是 **cooperative target**（广播 / 网络上报 Remote ID），"
            "**不是**被动 sensor。需求侧 ``sensor_mode`` 描述的是**地面网络 RID 接收节点**"
            "的工作模式，绝不用于机载判定。本记录显式声明该机载平台**支持参与**的合作监视"
            "技术；机载未声明时判定保持 unknown（evidence required），"
            "绝不自动降级为 does_not_meet_under_model。"
        ),
    },
}


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------


def empty_planning_evidence() -> dict:
    return {
        "schema_version": PLANNING_EVIDENCE_SCHEMA_VERSION,
        "items": [],
    }


def normalize_engineering_evidence(value: dict, *, now: str | None = None) -> dict:
    """规范化一条工程证据 / 规划假设（fail-closed 枚举校验 + 强制披露）。"""

    if not isinstance(value, dict):
        raise ValueError("工程证据条目必须是对象")
    evidence_id = _required_text(value, "evidence_id")
    field = _required_text(value, "field")
    spec = PLANNING_EVIDENCE_FIELDS.get(field)
    if spec is None:
        raise ValueError(
            "field 不在允许的人工工程证据清单内：" + ", ".join(sorted(PLANNING_EVIDENCE_FIELDS))
        )
    source_type = _required_text(value, "source_type")
    if source_type not in EVIDENCE_SOURCE_TYPES:
        raise ValueError(
            "source_type 必须是 " + " / ".join(EVIDENCE_SOURCE_TYPES)
        )
    scope = str(value.get("scope") or "aircraft").strip()
    if scope not in PROJECT_EVIDENCE_SCOPES:
        raise ValueError("scope 必须是 project / aircraft")
    target_id = _optional_text(value.get("target_id"))
    if scope == "aircraft" and not target_id:
        raise ValueError("scope=aircraft 的工程证据必须声明 target_id（机载档案 ID）")

    normalized_value = _normalize_value(spec, value.get("value"))
    statement = _optional_text(value.get("statement"))
    source = _optional_text(value.get("source"))
    reason = _optional_text(value.get("reason"))
    confirmed = value.get("confirmed") is True
    confirmed_by_user = value.get("confirmed_by_user") is True
    report_disclosure = _optional_text(value.get("report_disclosure"))

    if source_type == "unknown":
        #: ``unknown`` 的作用是**如实登记缺口**，因此它绝不携带参与判定的值。
        if normalized_value is not None:
            raise ValueError("source_type=unknown 的工程证据不得携带 value")
        confirmed_by_user = value.get("confirmed_by_user") is True
    elif source_type == "confirmed_source_fact":
        if not source:
            raise ValueError("confirmed_source_fact 必须声明 source（正式资料出处）")
        if not confirmed:
            raise ValueError("confirmed_source_fact 必须显式 confirmed=true")
    else:  # engineering_assumption
        missing = [
            name for name, current in (
                ("statement", statement), ("source", source),
                ("reason", reason), ("report_disclosure", report_disclosure),
            ) if not current
        ]
        if missing:
            raise ValueError("engineering_assumption 缺少必填字段：" + ", ".join(missing))
        if confirmed_by_user is not True:
            raise ValueError("engineering_assumption 必须 confirmed_by_user=true（由用户确认）")
        if value.get("declared_by") in (None, ""):
            raise ValueError("engineering_assumption 必须声明 declared_by")

    created_at = _optional_text(value.get("created_at")) or now or _utc_now()
    return {
        "evidence_id": evidence_id,
        "schema_version": PLANNING_EVIDENCE_SCHEMA_VERSION,
        "scope": scope,
        "target_id": target_id,
        "field": field,
        "subsystem": spec["subsystem"],
        "kind": spec["kind"],
        "value": normalized_value,
        "source_type": source_type,
        "source": source,
        "statement": statement,
        "reason": reason,
        "declared_by": _optional_text(value.get("declared_by")),
        "confirmed": confirmed,
        "confirmed_by_user": confirmed_by_user,
        "confirmation_status": "confirmed" if confirmed else "pending_confirmation",
        "authority_effect": EVIDENCE_AUTHORITY_EFFECT[source_type],
        "report_disclosure": report_disclosure,
        "provenance": _provenance(value.get("provenance"), spec, source_type, source),
        "created_at": created_at,
        "status": _status(value.get("status")),
    }


def normalize_planning_evidence_registry(value: dict | None) -> dict:
    if value is None:
        return empty_planning_evidence()
    if not isinstance(value, dict):
        raise ValueError("planning_evidence registry 必须是对象")
    items = value.get("items") or []
    if not isinstance(items, list):
        raise ValueError("planning_evidence.items 必须是数组")
    normalized = [normalize_engineering_evidence(item) for item in items]
    ids = [item["evidence_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("evidence_id 重复")
    return {
        "schema_version": PLANNING_EVIDENCE_SCHEMA_VERSION,
        "items": normalized,
    }


def active_evidence_items(
    registry: dict | None, *, target_id: str | None = None, field: str | None = None,
) -> list[dict]:
    """参与判定的记录（``status=active`` 且权威效应允许参与）。"""

    result = []
    for item in normalize_planning_evidence_registry(registry)["items"]:
        if item["status"] != "active":
            continue
        if item["authority_effect"] == "not_participating":
            continue
        if target_id is not None and item["scope"] == "aircraft" and item["target_id"] != target_id:
            continue
        if field is not None and item["field"] != field:
            continue
        result.append(deepcopy(item))
    return result


# ---------------------------------------------------------------------------
# 消费：把工程证据叠加到机载能力（绝不写回 aircraft profile 本身）
# ---------------------------------------------------------------------------


def apply_aircraft_evidence(profile: dict | None, registry: dict | None) -> dict | None:
    """把工程证据叠加到**选定机载档案**的一份副本上。

    * 输入 ``profile`` / ``registry`` 都**不被修改**；返回新对象；
    * 只叠加 ``scope=aircraft`` 且 ``target_id`` 与该档案匹配的记录；
    * ``source_type=unknown`` 的记录**不参与**（它们只登记缺口）；
    * 叠加结果在 ``aircraft_evidence`` 上保留逐字段来源，供 P8/P14/P15/P16
      与报告披露"这条能力来自事实还是工程假设"；
    * 没有任何有效记录时**逐字节返回原档案的深拷贝**（旧项目行为完全不变）。
    """

    result = deepcopy(profile) if isinstance(profile, dict) else profile
    if not isinstance(result, dict):
        return result
    target_id = str(result.get("aircraft_id") or "")
    items = active_evidence_items(registry, target_id=target_id) if target_id else []
    items = [
        item for item in items
        if item["scope"] == "aircraft" and item["target_id"] == target_id
    ]
    if not items:
        return result
    sources = {}
    for item in items:
        spec = PLANNING_EVIDENCE_FIELDS[item["field"]]
        subsystem = _subsystem_name(spec["subsystem"])
        block = result.setdefault(subsystem, {})
        if not isinstance(block, dict):
            continue
        type_block = block.setdefault("type", {})
        if not isinstance(type_block, dict):
            continue
        if spec["kind"] == "aircraft_type_field":
            type_block[spec["declared_field"]] = item["value"]
        elif spec["kind"] == "aircraft_type_interfaces":
            existing = [str(entry) for entry in type_block.get("interfaces") or []]
            for entry in item["value"]:
                if entry not in existing:
                    existing.append(entry)
            type_block["interfaces"] = existing
        elif spec["kind"] == "airborne_cooperative_surveillance_services":
            from .cns_service_contract import AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD

            declared = list(type_block.get(AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD) or [])
            for technology in item["value"]:
                entry = {"technology": technology}
                if technology == "network_remote_id":
                    entry["service_subtype"] = "cooperative_surveillance"
                if entry not in declared:
                    declared.append(entry)
            type_block[AIRBORNE_COOPERATIVE_SURVEILLANCE_FIELD] = declared
        sources[item["field"]] = {
            "evidence_id": item["evidence_id"],
            "source_type": item["source_type"],
            "source": item["source"],
            "confirmed": item["confirmed"],
            "confirmed_by_user": item["confirmed_by_user"],
            "report_disclosure": item["report_disclosure"],
        }
    if not sources:
        return result
    summary = {
        "source_types": sorted({item["source_type"] for item in items}),
        "source_type": (
            items[0]["source_type"] if len({item["source_type"] for item in items}) == 1
            else "mixed"
        ),
        "fields": sources,
        "semantics": "engineering_planning_input_disclosed_not_manufacturer_fact",
        "planning_input_only": any(
            item["source_type"] == "engineering_assumption" for item in items
        ),
    }
    result["airborne_evidence"] = deepcopy(summary)
    #: P8 的机载判定只收到**子系统 capability 子树**，因此来源标注必须同时写到
    #: 每个被叠加的子系统块上，否则"这条能力来自工程假设"会在那一层丢失。
    for field, entry in sources.items():
        spec = PLANNING_EVIDENCE_FIELDS[field]
        block = result.get(_subsystem_name(spec["subsystem"]))
        if isinstance(block, dict):
            block["airborne_evidence"] = deepcopy(summary)
    return result


def evidence_disclosure_lines(registry: dict | None) -> list[str]:
    """给报告 / 前端用的披露行（逐条原文，绝不改写）。"""

    lines = []
    for item in normalize_planning_evidence_registry(registry)["items"]:
        if item["status"] != "active":
            continue
        spec = PLANNING_EVIDENCE_FIELDS[item["field"]]
        if item["source_type"] == "unknown":
            lines.append(f"{spec['label']}：尚无依据（unknown），不参与判定。")
            continue
        prefix = (
            "有正式资料支持" if item["source_type"] == "confirmed_source_fact"
            else "工程规划假设（不代表厂家既有设备事实）"
        )
        lines.append(
            f"{spec['label']}＝{_render_value(item['value'])}（{prefix}；"
            f"来源：{item['source'] or '未记录'}）。"
            f"{item['report_disclosure'] or ''}"
        )
    return lines


def evidence_satisfies_requirement(
    field: str, value, required_value, spec: dict | None = None,
):
    """该字段的当前取值是否**真的满足需求**（逐项核对，绝不含糊）。

    返回 ``(满足?, 原因)``：

    * ``True``  —— 满足需求（可以作为一条成立的能力事实/假设参与判定）；
    * ``False`` —— **确认不满足**（取值本身与需求冲突，例如机载接口与地面提供者
      无交集）——这不是"缺证据"，而是真实不兼容，必须如实报出；
    * ``None``  —— **无法确认**（缺值 / 缺证据），保持 ``evidence_required``。

    ``field_status`` 若只判断"机载有没有给值"，就会把"机载写了 ``validation_radio_v1``、
    地面要求 ``ip``"这种**明确冲突**误报成"已具备可用依据"，从而让用户看不到真实阻塞。
    """

    spec = spec or PLANNING_EVIDENCE_FIELDS[field]
    kind = spec["kind"]
    if kind == "aircraft_type_interfaces":
        demanded = [str(item) for item in (required_value or []) if str(item)]
        declared = [str(item) for item in (value or []) if str(item)]
        if not demanded:
            return None, "需求未声明接口要求"
        if not declared:
            return None, "机载档案未声明任何接口"
        return bool(set(demanded) & set(declared)), (
            "机载接口与需求接口有交集" if set(demanded) & set(declared)
            else "机载接口与需求接口无交集（地面提供者要求 " + "、".join(demanded) + "）"
        )
    if kind == "airborne_cooperative_surveillance_services":
        if required_value in (None, "", "unknown"):
            return None, "需求未声明参与技术"
        declared = [str(item) for item in (value or []) if str(item)]
        if not declared:
            return None, "机载未声明参与能力"
        return required_value in declared, (
            "机载参与能力覆盖需求技术" if required_value in declared
            else "机载参与能力不覆盖需求技术 " + str(required_value)
        )
    if required_value in (None, "", "unknown", []):
        return None, "需求未声明该字段"
    if value in (None, "", "unknown", []):
        return None, "尚未提供取值"
    return value == required_value, (
        "取值与需求一致" if value == required_value else "取值与需求不一致"
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _normalize_value(spec, value):
    if spec["value_type"] == "enum_scalar":
        if value in (None, ""):
            return None
        text = str(value).strip().lower()
        if text not in spec["allowed"]:
            raise ValueError(
                f"value 无效：{text}；必须是 {', '.join(spec['allowed'])} 之一"
            )
        return text
    if value in (None, ""):
        #: 未给出取值 ⇒ 返回 ``None``（而不是空列表），这样 ``source_type=unknown``
        #: 的"绝不携带取值"判定与"确实没填"在语义上完全一致。
        return None
    items = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for raw in items:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        if text not in spec["allowed"]:
            raise ValueError(
                f"value 无效：{text}；必须是 {', '.join(spec['allowed'])} 之一"
            )
        if text not in result:
            result.append(text)
    if not result:
        raise ValueError("value 至少需要一个合法取值")
    return result


def _provenance(value, spec, source_type, source):
    result = deepcopy(value) if isinstance(value, dict) else {}
    result.setdefault("field", None)
    result.update({
        "evidence_field": spec["label"],
        "demand_field": spec["demand_field"],
        "source_type": source_type,
        "source": source,
        "collector": "user_input_engineering_evidence",
        "container": "project_state.planning_evidence",
        "never_written_to_device_catalog": True,
    })
    if result.get("field") is None:
        result.pop("field")
    return result


def _subsystem_name(code):
    return {"C": "communication", "N": "navigation", "S": "surveillance"}.get(code, code)


def _render_value(value):
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return str(value)


def _status(value):
    text = str(value or "active").strip().lower()
    if text not in ("active", "withdrawn", "superseded"):
        raise ValueError("工程证据 status 必须是 active / withdrawn / superseded")
    return text


def _required_text(value, field):
    text = _optional_text(value.get(field))
    if not text:
        raise ValueError(f"工程证据.{field} 必填")
    return text


def _optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "COMMUNICATION_AIRBORNE_INTERFACES", "COOPERATIVE_SURVEILLANCE_TECHNOLOGIES",
    "EVIDENCE_AUTHORITY_EFFECT", "EVIDENCE_SOURCE_TYPES",
    "PLANNING_EVIDENCE_FIELDS", "PLANNING_EVIDENCE_SCHEMA_VERSION",
    "PROJECT_EVIDENCE_SCOPES",
    "active_evidence_items", "apply_aircraft_evidence", "empty_planning_evidence",
    "evidence_disclosure_lines", "evidence_satisfies_requirement",
    "normalize_engineering_evidence", "normalize_planning_evidence_registry",
]
