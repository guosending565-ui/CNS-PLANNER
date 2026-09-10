"""Both python app.py and Streamlit's original URL lead to the map workbench."""
from map_app import URL, ensure_server, main


def streamlit_entry():
    import streamlit as st
    import streamlit.components.v1 as components
    st.set_page_config(page_title="CNS 地图工作台", layout="wide", initial_sidebar_state="collapsed")
    st.markdown("<style>.block-container{padding:0.7rem 1rem}header[data-testid=stHeader]{display:none}</style>", unsafe_allow_html=True)
    try:
        with st.spinner("正在启动本机地图服务…"):
            ensure_server()
        st.link_button("在独立窗口打开地图（推荐）", URL)
        components.iframe(URL, height=920, scrolling=True)
    except (RuntimeError, OSError) as exc:
        st.error(str(exc))
        st.info("也可以在工程目录双击“启动地图.cmd”。")
        if st.button("重新检查并启动"):
            st.rerun()


if __name__ == "__main__":
    import sys
    if "streamlit.runtime.scriptrunner" in sys.modules:
        streamlit_entry()
    else:
        raise SystemExit(main())
