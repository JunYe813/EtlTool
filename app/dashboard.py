"""
Olist 巴西电商 · 数仓 BI 看板（入口）

运行：
    streamlit run app/dashboard.py

结构：
    app/dashboard.py        入口：全局筛选器 + 多页面导航
    app/db.py               共享数据访问层（连接、缓存、筛选器）
    app/pages/*.py          5 个分析页面
"""
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
for _p in (str(_APP_DIR), str(_APP_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import streamlit as st                       # noqa: E402

from db import render_sidebar_filters        # noqa: E402

st.set_page_config(
    page_title="Olist 电商数仓看板",
    page_icon=":material/insights:",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Olist 巴西电商 · 数仓看板")
st.caption(
    "ODS → DWD → DWS → ADS 四层数仓 ｜ "
    "指标口径见 `docs/数据字典.md`，口径常量集中在 `config.py`"
)

# 全局筛选器必须在 nav.run() 之前渲染：
# 入口脚本先于页面脚本执行，5 个页面因此共享同一套筛选条件
render_sidebar_filters()

navigation = st.navigation([
    st.Page("pages/sales_overview.py",  title="销售总览",   icon=":material/trending_up:", default=True),
    st.Page("pages/product_analysis.py", title="商品分析",  icon=":material/inventory_2:"),
    st.Page("pages/seller_region.py",   title="卖家与地区", icon=":material/storefront:"),
    st.Page("pages/user_analysis.py",   title="用户分析",   icon=":material/groups:"),
    st.Page("pages/fulfillment.py",     title="履约漏斗",   icon=":material/local_shipping:"),
])
navigation.run()
