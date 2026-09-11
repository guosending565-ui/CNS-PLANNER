import streamlit as st
from .project_v1 import Project
from .storage import dumps, loads

STEPS = ["1 项目与数据", "2 工作区与基础环境", "3 航路", "4 运行规则", "5 设备与布站", "6 确认与导出"]
NEXT = {
    STEPS[2]: "起降点、稳定航路编号、场景/运行航路分离和逐条勾选将在第 3 阶段实现；A* 在第 4 阶段实现。",
    STEPS[3]: "将提供项目默认规则和航路覆盖配置；G1 暂不判定。",
    STEPS[4]: "将按固定水平半径逐分系统计算，支持规则布站、铁塔共址、补盲和人工调整。",
    STEPS[5]: "正式报告与成果导出尚未实现；当前仅支持项目元信息 JSON。",
}


def main():
    st.set_page_config(page_title="CNS 航路规划", page_icon="🧭", layout="wide")
    st.title("CNS 航路规划系统")
    st.caption("单机工作台 · 第一阶段 0.1.0 · 六步业务骨架")
    st.info("当前可验收：创建项目、元信息保存恢复、区域边界预览。数据导入与规划计算尚未实现。")
    step = st.sidebar.radio("业务步骤", STEPS)
    st.sidebar.caption("可自由浏览；浏览页面不代表已完成业务检查。")
    if "project" not in st.session_state:
        st.session_state.project = None
    p = st.session_state.project
    if step == STEPS[0]:
        st.subheader(step)
        with st.form("new_project"):
            name = st.text_input("新项目名称", max_chars=120)
            mode = st.selectbox("航路模式", ["single", "multi"], format_func=lambda x: "单条航路" if x == "single" else "多条航路")
            replace = st.checkbox("替换当前会话项目（请先下载保存）") if p else True
            if st.form_submit_button("创建项目"):
                if not replace:
                    st.error("请先保存当前项目，再勾选替换。")
                else:
                    try:
                        candidate = Project(name=name.strip(), mode=mode)
                        candidate.validate()
                        st.session_state.project = candidate
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
        uploaded = st.file_uploader("打开项目元信息（JSON）", type=["json"])
        allow_open = st.checkbox("允许打开文件替换当前会话（请先保存）") if p else True
        if st.button("打开所选项目", disabled=uploaded is None):
            if not allow_open:
                st.error("请先保存当前项目，再勾选替换。")
            else:
                try:
                    candidate = loads(uploaded.getvalue())
                    st.session_state.project = candidate
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        st.caption("人口、铁塔、地形与约束数据导入将在第二阶段加入。当前 JSON 不含业务数据，不是完整项目备份。")
    elif p is None:
        st.warning("请先在第 1 步创建或打开项目。")
    elif step == STEPS[1]:
        st.subheader(step)
        st.write("输入项目矩形边界（WGS84 经纬度）。不预设任何城市。")
        with st.form("bbox_" + p.id):
            defaults = p.bbox or [None] * 4
            vals = [st.number_input(label, value=value, format="%.6f") for label, value in zip(["西经度", "南纬度", "东经度", "北纬度"], defaults)]
            if st.form_submit_button("应用边界"):
                try:
                    candidate = Project(**{**p.to_dict(), "bbox": vals})
                    candidate.validate()
                    st.session_state.project = candidate
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        if p.bbox:
            w, s, e, n = p.bbox
            st.markdown(f'''<svg viewBox="0 0 800 250" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="工作区边界示意">
            <rect x="100" y="35" width="600" height="170" fill="#e5f4f5" stroke="#147d92" stroke-width="3"/>
            <text x="100" y="25">西北：{w:.6f}, {n:.6f}</text><text x="400" y="235">东南：{e:.6f}, {s:.6f}</text>
            <text x="280" y="125">项目工作区 · 未载入空间数据</text></svg>''', unsafe_allow_html=True)
            st.caption("离线边界示意，不按比例，不用于测距。交互地图组件与真实图层在下一阶段验证。")
        st.warning("人口覆盖范围尚未检查；未提供真实地形，未做净空校验。")
    else:
        st.subheader(step)
        st.info(NEXT[step])
        st.write("状态：未实现 / 未计算。不会生成示例达标结果。")
    p = st.session_state.project
    if p:
        st.sidebar.divider()
        st.sidebar.write(f"当前项目：{p.name}")
        st.sidebar.caption(f"ID：{p.id}")
        st.sidebar.write("单条航路" if p.mode == "single" else "多条航路")
        st.sidebar.download_button("保存元信息 JSON", dumps(p), file_name=f"project-{p.id}.json", mime="application/json")
        st.sidebar.caption("下载到浏览器下载目录；刷新前请保存。另存、数据打包与自动恢复将在第二阶段实现。")
