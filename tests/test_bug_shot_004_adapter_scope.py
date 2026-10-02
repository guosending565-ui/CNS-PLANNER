"""BUG-SHOT-004 回归：app_context 里跨函数复用的 GIS 适配器必须真正可见。

缺陷形态：某个 ``ApplicationContext`` 方法只在**另一个方法**的函数体里
``from ..gis... import X``，却在本方法里直接调用 ``X``。Python 的名称解析发生在调用时，
所以这种代码既通不过静态检查、也不会在"另一个方法未被走到"的路径上暴露，直到真实执行到
这一行才抛 ``NameError: name 'X' is not defined``——本轮的
``restricted_area_continuous_evidence`` 正是这样让正式验证接口 500、阻塞 operational route。

本测试用 CPython 自己编译期使用的 ``symtable``（作用域表）来判定：在任一函数作用域里被
**读取**、又不是该作用域 local / enclosing free / 模块 global 的名字，就是"运行时一定
NameError"的名字。
"""

import symtable
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "cns_planner" / "application" / "app_context.py"


def _scope_problems(scope, trail=()):
    """返回 ``[(scope_name, symbol_name), ...]``：读取但任何作用域都无法提供绑定的名字。"""

    problems = []
    for symbol in scope.get_symbols():
        if not symbol.is_referenced():
            continue
        if (
            symbol.is_local() or symbol.is_parameter() or symbol.is_free()
            or symbol.is_global() or symbol.is_declared_global() or symbol.is_imported()
            or symbol.is_assigned()
        ):
            continue
        # 既不是 local、也没有 enclosing/global 绑定：运行时必然 NameError。
        problems.append((".".join(trail + (scope.get_name(),)), symbol.get_name()))
    for child in scope.get_children():
        problems.extend(_scope_problems(child, trail + (scope.get_name(),)))
    return problems


def test_no_function_reads_a_name_it_cannot_resolve():
    source = MODULE.read_text(encoding="utf-8")
    table = symtable.symtable(source, str(MODULE), "exec")
    problems = _scope_problems(table)
    assert problems == [], (
        "以下名字被读取但在其作用域链里没有任何绑定，运行到这里必然 NameError："
        f"{problems}"
    )


def test_restricted_area_evidence_is_visible_to_its_call_site():
    """定向锚点：这一行曾经因缺少 import 让正式验证接口 500。"""

    source = MODULE.read_text(encoding="utf-8")
    table = symtable.symtable(source, str(MODULE), "exec")
    name = "restricted_area_continuous_evidence"
    hits = []

    def walk(scope, path=()):
        symbol = next(
            (item for item in scope.get_symbols() if item.get_name() == name), None
        )
        if symbol is not None and symbol.is_referenced():
            hits.append((path + (scope.get_name(),), symbol))
        for child in scope.get_children():
            walk(child, path + (scope.get_name(),))

    walk(table)
    assert hits, f"{name} 应当仍然被 app_context 消费"
    for path, symbol in hits:
        assert (
            symbol.is_local() or symbol.is_free() or symbol.is_global()
            or symbol.is_imported()
        ), (
            f"{'.'.join(path)} 读取 {name}，但该名称在它的作用域链里不可解析"
            "（运行时必然 NameError）"
        )
