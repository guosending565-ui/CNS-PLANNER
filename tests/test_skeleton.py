import json
import pytest
from streamlit.testing.v1 import AppTest
from cns_planner.domain import Project
from cns_planner.storage import dumps, loads
from cns_planner.algorithms.contracts import AlgorithmRegistry


def test_manifest_roundtrip():
    original = Project("跨地区项目", mode="multi", bbox=[100, 20, 101, 21])
    assert loads(dumps(original)) == original


@pytest.mark.parametrize("payload", ["[]", "{}", '{"name":"x","schema_version":2}', '{"name":"x","bbox":[10,20,9,21]}', '{"name":"x","bbox":[0,0,NaN,1]}', '{"name":"x","unknown":1}'])
def test_reject_invalid_manifest(payload):
    with pytest.raises(ValueError):
        loads(payload)


def test_registry_has_no_fallback():
    registry = AlgorithmRegistry()
    assert registry.keys() == ()
    with pytest.raises(ValueError):
        registry.get("astar")


def test_ui_create_boundary_and_browse():
    app = AppTest.from_file("legacy_app.py").run()
    assert not app.exception
    app.text_input[0].set_value("验收项目")
    app.button[0].click().run()
    original_id = app.session_state.project.id
    app.sidebar.radio[0].set_value("2 工作区与基础环境").run()
    for widget, value in zip(app.number_input, [100.0, 20.0, 101.0, 21.0]):
        widget.set_value(value)
    app.button[0].click().run()
    assert app.session_state.project.bbox == [100, 20, 101, 21]
    app.number_input[0].set_value(102.0)
    app.button[0].click().run()
    assert app.error
    assert app.session_state.project.bbox == [100, 20, 101, 21]
    assert app.session_state.project.id == original_id
    for step in ["3 航路", "4 运行规则", "5 设备与布站", "6 确认与导出"]:
        app.sidebar.radio[0].set_value(step).run()
        assert not app.exception
        assert any("未实现" in item.value for item in app.markdown)
