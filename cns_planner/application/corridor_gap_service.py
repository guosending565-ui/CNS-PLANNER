"""Application use cases for P15 objectives and corridor gap assessment."""

from copy import deepcopy

from ..domain.cns_planning_objectives import normalize_cns_planning_objectives
from .corridor_service import conclusion_changed
from .result_currentness import apply_algorithm_semantics_stale, projected_result


def _no_cancel_check():
    """同步路径的缺省取消检查：不取消。"""


def _progress_reporter(on_progress):
    """只上报**真实执行节点**的只读进度钩子（无回调时是 no-op）。

    Round 31-D：P15 的算法内部没有可量化的循环单元（逐体元判定是一次纯函数调用），
    因此这里绝不产生假百分比、绝不为了刷新进度重复计算，也不插入固定 sleep；它只把
    真实阶段如实交给后台任务窗口。同步路径不传回调，行为逐字段不变。
    """

    if not callable(on_progress):
        return lambda value, message=None: None

    state = {"last": -1.0}

    def report(value, message=None):
        current = max(0.0, min(1.0, float(value)))
        if current <= state["last"]:
            return
        state["last"] = current
        on_progress(current, message)

    return report


class CNSCorridorGapService:
    def __init__(self, session, analyzer, invalidation, snapshot):
        self.session, self.analyzer = session, analyzer
        self.invalidation, self.snapshot = invalidation, snapshot

    def objectives_snapshot(self):
        return deepcopy(self.session.state.get("cns_planning_objectives") or {})

    def result_snapshot(self):
        """只读投影：算法语义版本变化时如实标注 stale（绝不误判 current）。"""

        result = self.session.state.get("cns_corridor_gap_assessment") or self.analyzer.empty()
        return apply_algorithm_semantics_stale(
            result, self.analyzer.algorithm_id, self.analyzer.algorithm_version,
        )

    def set_objectives(self, payload):
        raw = payload.get("cns_planning_objectives", payload) if isinstance(payload, dict) else payload
        normalized = normalize_cns_planning_objectives(raw)
        if normalized != self.session.state.get("cns_planning_objectives"):
            self.session.state["cns_planning_objectives"] = normalized
            self.invalidation.cns_corridor_gap()
            self.session.save()
        return self.snapshot()

    def evaluate(self, payload=None, *, on_progress=None, cancel_check=None):
        """同步入口：计算 P15 并立即发布为 canonical 结果。

        Round 31-D 把方法体拆成 :meth:`plan`（纯计算）+ :meth:`apply_computed`
        （唯一写入：失效传播 / canonical 落库 / ``session.save()``）。本方法的
        可观测行为与拆分前**逐字段一致**：同一个 ``plan`` 结果经同一个收尾段发布，
        携带规划目标更新时的写入与失效顺序也保持原样（先写目标并
        ``cns_corridor_gap()``，再写结论并按 ``conclusion_changed`` 决定 P16 失效）。
        """

        outcome = self.plan(payload, on_progress=on_progress, cancel_check=cancel_check)
        return self.apply_computed(
            outcome["result"], objectives=outcome["objectives"],
            objectives_declared=outcome["objectives_declared"],
        )

    def plan(self, payload=None, *, on_progress=None, cancel_check=None):
        """P15 的**纯计算段**：只读 state，绝不写 canonical 结果、绝不落盘。

        后台任务的 worker 在内存 state 上调用它（``session.save()`` 由主进程的
        :meth:`apply_computed` 独占），因此 worker 永远不会成为第二个 canonical
        写入者。``on_progress`` / ``cancel_check`` 是**可选**的只读钩子：前者只上报
        真实阶段，后者只做协作式取消检查，两者都不参与任何判定。
        """

        report = _progress_reporter(on_progress)
        check = cancel_check if callable(cancel_check) else _no_cancel_check
        payload = payload if isinstance(payload, dict) else {}
        state = self.session.state
        objectives_declared = "cns_planning_objectives" in payload
        report(0.05, "正在准备 CNS 能力缺口输入")
        check()
        #: 请求显式给出规划目标时以请求值为准（请求值将在发布阶段与结论同事务写入），
        #: 否则用 state 里已确认的目标。绝不在这里改 state。
        objectives = (
            normalize_cns_planning_objectives(payload["cns_planning_objectives"])
            if objectives_declared
            else deepcopy(state.get("cns_planning_objectives") or {})
        )
        report(0.35, "正在校验 CNS 服务走廊结论是否仍然有效")
        check()
        #: Round 29-J：P15 只接受**有效 current** 的 P14。旧算法语义版本的 P14 在这里
        #: 被只读投影为 stale（stored payload 原样保留），算法层随即按既有 fail-closed
        #: 规则返回 missing_data —— 绝不用旧 P14 得出新 P15。
        corridor = projected_result(state, "cns_corridor_assessment")
        report(0.65, "正在按规划目标判定能力缺口")
        check()
        result = self.analyzer.evaluate(
            corridor, state.get("required_cns") or {}, objectives,
        )
        check()
        report(0.95, "正在整理 CNS 能力缺口结论")
        if not isinstance(result, dict) or not result:
            raise ValueError("CNS 能力缺口计算没有产出结果")
        return {
            "result": result, "objectives": objectives,
            "objectives_declared": objectives_declared,
        }

    def apply_computed(self, result, *, objectives=None, objectives_declared=False):
        """把**已经算好**的 P15 结果发布为 canonical（唯一 production writer 入口）。

        后台任务在 worker 进程里算完、staged 成 artifact 后，由主进程在
        ``mutation_lock`` 内调用本方法；收尾段与 :meth:`evaluate` 完全同源，因此
        异步路径与同步路径得到同一份落库形态（同一 ``result_statuses`` 映射、同一
        失效传播、同一次 ``session.save()``）。
        """

        state = self.session.state
        if objectives_declared and objectives is not None:
            normalized = normalize_cns_planning_objectives(objectives)
            if normalized != state.get("cns_planning_objectives"):
                state["cns_planning_objectives"] = normalized
                self.invalidation.cns_corridor_gap()
        previous = state.get("cns_corridor_gap_assessment") or {}
        state["cns_corridor_gap_assessment"] = result
        # 只有结论真的变化时 P16 才失效：重算得到同一 fingerprint 时 P16 的基线
        # 仍然成立，把它标成 stale 只会让 P18 的门禁（要求 current P16）无法满足。
        if conclusion_changed(previous, result):
            self.invalidation.cns_corridor_site_plan()
        state.setdefault("result_statuses", {})["cns_corridor_gap_assessment"] = _result_status(result.get("status"))
        self.session.save()
        return self.snapshot()


def _result_status(status):
    return {
        "passed": "passed", "failed": "failed", "pending_confirmation": "pending_confirmation",
        "missing_data": "missing_data", "not_applicable": "not_applicable", "stale": "stale",
    }.get(status, "pending_confirmation")
