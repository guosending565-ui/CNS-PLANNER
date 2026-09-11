"""Aircraft and operating-rule use cases."""

from .project_state import assessment


class OperationService:
    def __init__(self, session, invalidation, snapshot):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot

    def set_rules(self, payload):
        required = (
            "manufacturer", "model", "cruise_speed", "max_speed", "mtbf",
            "height_ab", "height_ba", "delay_sensor", "delay_command",
        )
        if any(payload.get(name) in (None, "") for name in required):
            raise ValueError("请完整填写飞行器、运行高度和两段时延")
        cruise, maximum, mtbf = (float(payload[name]) for name in ("cruise_speed", "max_speed", "mtbf"))
        height_ab, height_ba = float(payload["height_ab"]), float(payload["height_ba"])
        delay_sensor, delay_command = float(payload["delay_sensor"]), float(payload["delay_command"])
        if min(cruise, maximum, mtbf, height_ab, height_ba) <= 0 or min(delay_sensor, delay_command) < 0:
            raise ValueError("速度、可靠性和高度须大于零，时延不得为负")
        total_delay_ms = delay_sensor + delay_command
        reaction_distance_m = cruise * total_delay_ms / 1000
        same_level = payload.get("height_mode") == "same"
        separation = float(payload.get("horizontal_separation") or 0)
        clearance = float(self.session.defaults["engineering_parameters"]["vertical_clearance_m"]["value"])
        valid = separation >= 2 * reaction_distance_m if same_level else abs(height_ab - height_ba) >= clearance
        message = "规则校验通过" if valid else ("同高度层水平间隔不足" if same_level else f"双向高度差须至少 {clearance:g} m")
        state = self.session.state
        aircraft_id = str(payload.get("aircraft_id") or state.get("selected_aircraft_profile_id") or "") or None
        if aircraft_id and aircraft_id not in {item.get("aircraft_id") for item in state.get("aircraft_profiles", {}).get("items", [])}:
            raise ValueError("飞行器能力档案不存在")
        state["selected_aircraft_profile_id"] = aircraft_id
        state["aircraft"] = {
            "aircraft_id": aircraft_id,
            "manufacturer": str(payload["manufacturer"]), "model": str(payload["model"]),
            "cruise_speed_mps": cruise, "max_speed_mps": maximum,
            "mtbf_h": mtbf, "lambda_per_hour": 1 / mtbf,
            "route_id": payload.get("route_id") or None,
            "source": payload.get("source") or "用户输入",
        }
        state["rules"] = {
            "height_ab_m": height_ab, "height_ba_m": height_ba,
            "height_mode": payload.get("height_mode", "different"),
            "horizontal_separation_m": separation,
            "direction_rule": payload.get("direction_rule", "按航向"),
            "delay_sensor_to_platform_ms": delay_sensor,
            "delay_platform_to_aircraft_ms": delay_command,
            "total_delay_ms": total_delay_ms,
            "reaction_distance_m": round(reaction_distance_m, 3),
            "status": "passed" if valid else "failed", "message": message,
        }
        state["risks"]["technical"] = assessment("pending_confirmation", "MTBF 与 λ 已计算，技术风险模型和阈值待确认")
        self.invalidation.workflow("rules")
        self.session.save()
        return self.snapshot()
