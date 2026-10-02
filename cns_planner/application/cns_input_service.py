"""CNS catalog, requirement, existing-facility and candidate-site use cases.

Phase4-B2B-1：``CNSInputService`` 是 canonical ``required_cns`` 的**唯一 production
content owner**（迁移期的 RequiredCNS command facade）。两种命令都经过这里：
``set_required_cns``（用户手工 direct confirm）与 ``adopt_required_cns``
（recommendation 显式 Adopt）。``RequirementRecommendationService.adopt`` 只能委托本
服务，自己不再持有 canonical 赋值语句。
"""

from copy import deepcopy
from datetime import datetime, timezone

from ..catalogs import AircraftCNSProfileCatalog, DeviceCatalog
from ..domain.cns_inputs import (
    backfill_device_contract, normalize_aircraft_profile, normalize_candidate_site,
    normalize_existing_facility, normalize_required_cns,
)
from ..domain.navigation_augmentation import normalize_navigation_site_suitability
from ..domain.assumptions import normalize_assumption, normalize_assumption_registry
from ..domain.cns_existing_baseline import (
    existing_cns_baseline_readiness,
    normalize_cns_existing_baseline,
)
from .production_write_authority import assert_write_authority


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def empty_collection(collection_id):
    return {
        "status": "not_calculated", "collection_id": collection_id,
        "source": None, "metadata": {}, "count": 0, "items": [],
    }


class CNSInputService:
    def __init__(self, session, invalidation, adapter, snapshot):
        self.session, self.invalidation = session, invalidation
        self.adapter, self.snapshot = adapter, snapshot

    def ensure_catalogs(self):
        state, config_dir = self.session.state, self.session.defaults_path.parent
        if state.get("aircraft_profiles", {}).get("status") != "passed":
            try:
                state["aircraft_profiles"] = AircraftCNSProfileCatalog.load(config_dir)
            except OSError:
                state["aircraft_profiles"] = AircraftCNSProfileCatalog.from_defaults(self.session.defaults.get("aircraft_library"))
        if state.get("device_catalog", {}).get("status") != "passed":
            try:
                state["device_catalog"] = DeviceCatalog.load(config_dir)
            except OSError:
                state["device_catalog"] = DeviceCatalog.from_defaults(self.session.defaults.get("device_library"))
        # Additive schema-v2 backfill also applies to catalogs restored from an
        # older project, not only to freshly imported catalog files.
        if state.get("aircraft_profiles", {}).get("status") == "passed":
            state["aircraft_profiles"]["items"] = [
                normalize_aircraft_profile(item) for item in state["aircraft_profiles"].get("items", [])
            ]
            state["aircraft_profiles"]["count"] = len(state["aircraft_profiles"]["items"])
        #: Round 2.6：canonical 机载档案（FC30）必须**可通过正式 UI 选中**。
        #: 已保存项目的 ``aircraft_profiles`` 是冻结目录，旧项目里可能只有合成档案；
        #: 这里按规范 id **幂等补齐**（绝不覆盖项目里已有的同 id 条目，也绝不改选中项）。
        self.ensure_canonical_aircraft_profiles()
        if state.get("device_catalog", {}).get("status") == "passed":
            state["device_catalog"]["items"] = [
                backfill_device_contract(item) for item in state["device_catalog"].get("items", [])
            ]
            state["device_catalog"]["count"] = len(state["device_catalog"]["items"])
        state.setdefault("selected_aircraft_profile_id", None)
        state["required_cns"] = normalize_required_cns(state.get("required_cns"))
        state.setdefault("existing_cns_facilities", empty_collection("existing-cns-facilities"))
        state.setdefault("candidate_sites", empty_collection("candidate-sites"))
        state["existing_cns_facilities"]["items"] = [
            normalize_existing_facility(item, index)
            for index, item in enumerate(state["existing_cns_facilities"].get("items") or [])
        ]
        state["existing_cns_facilities"]["count"] = len(state["existing_cns_facilities"]["items"])
        state["candidate_sites"]["items"] = [
            normalize_candidate_site(item, index)
            for index, item in enumerate(state["candidate_sites"].get("items") or [])
        ]
        state["candidate_sites"]["count"] = len(state["candidate_sites"]["items"])

    def ensure_canonical_aircraft_profiles(self):
        """把 canonical 机载档案（FC30）**幂等**并入项目目录，使其可在正式 UI 中选中。

        Round 2.6 规则：

        * 只补齐**缺失**的 canonical 条目；项目里已有的同 ``aircraft_id`` 条目**原样保留**
          （正式导入的目录不得被内置档案静默覆盖）；
        * 绝不改变 ``selected_aircraft_profile_id``（补齐 ≠ 选中）；官方验收机型的切换必须
          由用户在正式 UI / API 上显式执行；
        * 因为不改变选中项，所以补齐本身**不触发 invalidation**（不制造无意义的 stale）。
        """

        from ..domain.fc30_profile import FC30_AIRCRAFT_ID, fc30_aircraft_profile

        catalog = self.session.state.get("aircraft_profiles") or {}
        if catalog.get("status") != "passed":
            return
        items = catalog.setdefault("items", [])
        existing = {str(item.get("aircraft_id") or "") for item in items}
        if FC30_AIRCRAFT_ID in existing:
            return
        items.append(fc30_aircraft_profile())
        catalog["count"] = len(items)

    def select_aircraft(self, aircraft_id):
        profile = AircraftCNSProfileCatalog.find(self.session.state["aircraft_profiles"], str(aircraft_id or ""))
        if profile is None:
            raise ValueError("飞行器能力档案不存在")
        self.session.state["selected_aircraft_profile_id"] = profile["aircraft_id"]
        self.invalidation.workflow("aircraft_profile")
        return self._save()

    def import_aircraft_catalog(self, path):
        catalog = AircraftCNSProfileCatalog.load_path(path)
        self.session.state["aircraft_profiles"] = catalog
        selected = self.session.state.get("selected_aircraft_profile_id")
        if selected and AircraftCNSProfileCatalog.find(catalog, selected) is None:
            self.session.state["selected_aircraft_profile_id"] = None
        self.invalidation.workflow("aircraft_profile")
        return self._save()

    def import_device_catalog(self, path):
        catalog = DeviceCatalog.load_path(path)
        state = self.session.state
        state["device_catalog"] = catalog
        state["devices"] = DeviceCatalog.to_coverage_v1(catalog)
        self.invalidation.workflow("devices")
        return self._save()

    def set_required_cns(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("RequiredCNS 请求必须是对象")
        assert_write_authority(self, "required_cns")
        state = self.session.state
        current = deepcopy(state.get("required_cns"))
        scope, requirements = payload.get("scope", "project"), payload.get("requirements")
        if not isinstance(requirements, dict):
            raise ValueError("RequiredCNS requirements 必须是对象")
        if scope == "project":
            current["project_default"] = requirements
        elif scope == "route":
            route_id = str(payload.get("route_id") or "")
            if route_id not in {item.get("route_id") for item in state.get("scenario_routes", [])}:
                raise ValueError("RequiredCNS 航路不存在")
            current.setdefault("route_overrides", {})[route_id] = requirements
        else:
            raise ValueError("RequiredCNS scope 必须是 project 或 route")
        current["source"] = str(payload.get("source") or "用户配置")
        state["required_cns"] = normalize_required_cns(current)
        # manual direct confirm 覆盖了此前的 recommendation adoption：旧 adoption 记录
        # 不得继续伪装成"当前有效依据"。
        self._supersede_adoption_for_manual_set()
        self.invalidation.workflow("required_cns")
        return self._save()

    def adopt_required_cns(self, required_cns, adoption, *, reason="required_cns_adopted"):
        """唯一 canonical ``required_cns`` 写入命令（recommendation 显式 Adopt）。

        ``required_cns`` / ``adoption`` 的业务内容与 fingerprint 由调用方
        （``RequirementRecommendationService``）构造，本命令不改写其中任何字段，
        只负责赋值、失效传播与保存。
        """

        assert_write_authority(self, "required_cns")
        state = self.session.state
        state["required_cns"] = normalize_required_cns(deepcopy(required_cns))
        state["required_cns_adoption"] = deepcopy(adoption)
        self.invalidation.workflow("required_cns")
        return self._save()

    def _supersede_adoption_for_manual_set(self):
        """手工 direct confirm 后把此前 status=adopted 的采纳记录标为 superseded。

        复用既有 ``required_cns_adoption.status`` 枚举，不引入第四套状态词；历史字段
        一律保留，只追加 ``superseded_by`` / ``superseded_at`` / ``previous_status``。
        """

        state = self.session.state
        adoption = state.get("required_cns_adoption")
        if not isinstance(adoption, dict) or adoption.get("status") != "adopted":
            return {}
        superseded = deepcopy(adoption)
        superseded["previous_status"] = "adopted"
        superseded["status"] = "superseded"
        superseded["superseded_by"] = "manual_direct_required_cns"
        superseded["superseded_at"] = _utc_now()
        state["required_cns_adoption"] = superseded
        return superseded

    def import_existing(self, payload):
        self.session.state["existing_cns_facilities"] = self.adapter.load_existing(payload)
        self.invalidation.workflow("existing_cns")
        return self._save()

    def set_existing_baseline(self, payload):
        """Declare the existing-CNS fact state and its separate planning mode.

        An empty imported collection is deliberately not treated as evidence of
        ``confirmed_none``.  ``assume_empty_for_planning`` is accepted only with
        an explicitly confirmed, disclosure-bearing assumption in the same
        command so the project can never persist a silent empty baseline.
        """

        if not isinstance(payload, dict):
            raise ValueError("Existing CNS baseline 请求必须是对象")
        requested = payload.get("cns_existing_baseline", payload)
        baseline = normalize_cns_existing_baseline(requested)
        baseline["declared_at"] = baseline.get("declared_at") or _utc_now()
        state = self.session.state
        registry = normalize_assumption_registry(state.get("assumptions"))
        assumes_empty = baseline["planning_mode"] == "assume_empty_for_planning"

        assumption = payload.get("assumption")
        normalized_assumption = None
        if assumes_empty:
            if not isinstance(assumption, dict) or assumption.get("confirmed") is not True:
                raise ValueError("按空既有设施工程基线规划必须显式确认 assumption")
            normalized_assumption = normalize_assumption(assumption)
            if (
                normalized_assumption["field"] != "cns_existing_baseline"
                or normalized_assumption["value"] != "empty"
                or normalized_assumption["status"] != "active"
            ):
                raise ValueError("Existing CNS baseline assumption 必须是 active empty assumption")

        # The command is repeatable: replace the same assumption id and retire
        # other active assumptions for this field instead of accumulating
        # contradictory planning declarations.
        retained = []
        replacement_id = (
            normalized_assumption["assumption_id"] if normalized_assumption else None
        )
        for item in registry["items"]:
            if replacement_id and item["assumption_id"] == replacement_id:
                continue
            if item["field"] == "cns_existing_baseline" and item["status"] == "active":
                item = {**item, "status": "superseded"}
            retained.append(item)
        if normalized_assumption is not None:
            retained.append(normalized_assumption)
        registry["items"] = retained

        readiness = existing_cns_baseline_readiness(
            baseline,
            facilities=state.get("existing_cns_facilities"),
            assumption_registry=registry,
        )
        # Readiness 校验对所有模式生效：否则会持久化出一个 blocked 声明
        # （例如无设施/无证据的 confirmed_present，或没有任何空基线 assumption
        # 的 not_declared + factual），下游 coverage 至 facility_plan 只能拿到
        # 永远无法满足的 blocked 前置。
        if readiness["state"] == "blocked":
            blockers = "、".join(readiness.get("blockers") or []) or "unknown"
            raise ValueError(f"Existing CNS baseline 声明不满足 readiness contract：{blockers}")

        state["cns_existing_baseline"] = baseline
        state["assumptions"] = registry
        self.invalidation.workflow("existing_cns")
        return self._save()

    def import_candidates(self, payload):
        self.session.state["candidate_sites"] = self.adapter.load_candidates(payload)
        self.invalidation.workflow("candidate_sites")
        return self._save()

    # ---- Round D：navigation_site_suitability（站址事实，不是第二套容器） ---------
    #
    # 站址适用性是**既有站址条目上的一个显式声明**，因此它写在既有 collection
    # （``existing_cns_facilities`` / ``candidate_sites`` / ``towers``）条目的
    # ``metadata.navigation_site_suitability`` 上，**不**新建任何站址容器：
    #
    #   * ``metadata`` 是既有 normalization（``normalize_existing_facility`` /
    #     ``normalize_candidate_site``）唯一原样保留的扩展位置，因此写入后
    #     ``ensure_catalogs`` 的规范化与 reopen 都不会丢字段；
    #   * 共塔候选是**派生**结果，所以铁塔上的声明由
    #     ``tower_colocation.declared_navigation_site_suitability`` 过继到候选，
    #     而不是直接写派生集合（写派生集合会在下次派生时丢失）；
    #   * 合格性判定（``confirmed`` + ``planning_use_confirmed``）唯一归属
    #     ``navigation_augmentation`` 的 fail-closed 规则：这里只做规范化与保存。
    NAVIGATION_SITE_COLLECTIONS = {
        "existing_cns_facility": ("existing_cns_facilities", ("facility_id", "site_id")),
        "candidate_site": ("candidate_sites", ("site_id",)),
        "tower_colocation_host": ("towers", ("tower_id",)),
    }

    def set_navigation_site_suitability(self, payload):
        """在**既有**站址条目上写入 ``navigation_site_suitability``。

        payload：``site_source`` + 该来源的主键之一 + ``navigation_site_suitability``。
        ``navigation_site_suitability = null`` 表示**显式清除**该站址的适用性声明
        （回到"无证据"，不是"不合格"）。本命令不推断坐标、不生成站址、不排序候选。
        """

        if not isinstance(payload, dict):
            raise ValueError("navigation_site_suitability 请求必须是对象")
        source = str(payload.get("site_source") or "").strip()
        spec = self.NAVIGATION_SITE_COLLECTIONS.get(source)
        if spec is None:
            raise ValueError(
                "site_source 必须是 existing_cns_facility / candidate_site / "
                "tower_colocation_host"
            )
        collection_key, id_keys = spec
        collection = self.session.state.get(collection_key)
        if not isinstance(collection, dict):
            raise ValueError(f"{collection_key} 集合不存在")
        wanted = [
            str(payload.get(key) or "").strip() for key in id_keys
        ]
        wanted = [value for value in wanted if value]
        if not wanted:
            raise ValueError(f"site_source={source} 需要 {' / '.join(id_keys)} 之一")
        item = next(
            (
                entry for entry in collection.get("items") or []
                if any(
                    str(entry.get(key) or "").strip() == value
                    for key in id_keys for value in wanted
                )
            ),
            None,
        )
        if item is None:
            raise ValueError(f"{collection_key} 中不存在该站址：{' / '.join(wanted)}")
        raw = payload.get("navigation_site_suitability")
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            item["metadata"] = metadata
        if raw is None:
            metadata.pop("navigation_site_suitability", None)
        else:
            normalized = normalize_navigation_site_suitability(raw)
            if normalized is None:
                raise ValueError("navigation_site_suitability 必须是对象或 null")
            #: 派生结论（``eligible_for_navigation_reference_station``）**不**持久化：
            #: 它必须每次由 fail-closed 规则重新判定，绝不落成站址的既有事实。
            metadata["navigation_site_suitability"] = {
                key: value for key, value in normalized.items()
                if key != "eligible_for_navigation_reference_station"
            }
        collection["count"] = len(collection.get("items") or [])
        #: 站址事实变化只沿 P14 → P15 → P16 → report 传播（见 services/invalidation.py）。
        self.invalidation.workflow("navigation_site_suitability")
        return self._save()

    def candidates_from_existing(self):
        facilities = self.session.state["existing_cns_facilities"].get("items", [])
        items = [{
            "site_id": item["site_id"], "name": item["name"], "coordinate": item["coordinate"],
            "elevation_m": item.get("elevation_m"), "site_type": "existing_facility",
            "vertical_profile": deepcopy(item.get("vertical_profile")),
            "available_subsystems": list(dict.fromkeys(device["subsystem"] for device in item.get("devices", []))),
            "usable": item.get("status") == "active", "locked": True,
            "source": "已有 CNS 设施", "metadata": {"facility_id": item["facility_id"]},
        } for item in facilities]
        self.session.state["candidate_sites"] = self.adapter.load_candidates({"items": items, "metadata": {"derived_from": "existing_cns_facilities"}})
        self.invalidation.workflow("candidate_sites")
        return self._save()

    def _save(self):
        self.session.save()
        return self.snapshot()
