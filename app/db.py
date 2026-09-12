"""
看板共享的数据访问层 + 全局筛选器

设计要点：
  1) 所有查询走 ADS 层的表/视图，看板不直接碰 ODS/DWD 明细（分层复用的意义）
  2) 派生指标（占比、区间客单价）在筛选后的数据集上**现算**，
     不用视图里预先算好的百分比 —— 否则按日期筛选后分母仍是全周期总额
  3) SQL 用命名参数绑定，不做字符串拼接，避免注入
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import text

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
for _p in (str(ROOT), str(APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import MIN_GROUP_SIZE, RETENTION_WINDOWS, TOP_N, get_engine  # noqa: E402,F401

CACHE_TTL = 300
engine = get_engine()

# 颜色：跟 Streamlit 默认主题协调，避免每个页面各写一套
ACCENT = "#4C78A8"
WARN = "#E45756"


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """
    参数化查询，返回 DataFrame。

    params 统一用 ((名称, 值), ...) 的元组形式而不是 dict：
    元组可哈希，能直接作为 st.cache_data 的缓存键；dict 不行。
    列表值在内部转回 list（psycopg2 会适配成 SQL 数组，配合 = ANY(...) 使用）。
    """
    binds = {k: (list(v) if isinstance(v, tuple) else v) for k, v in params}
    with engine.connect() as conn:
        df = pd.read_sql_query(text(sql), conn, params=binds or None)
    return _normalize_dates(df)


def _normalize_dates(df: pd.DataFrame) -> pd.DataFrame:
    """
    把 object 型日期列统一转成 datetime64。

    为什么需要：psycopg2 读回的 DATE 列在 DataFrame 里是 object dtype，
    元素是 datetime.date 而不是 datetime64 —— 直接 .dt 访问会报
    「Can only use .dt accessor with datetimelike values」。
    在数据访问层统一收敛，页面就不用每处都记得 pd.to_datetime()。
    （实测 pandas 3.0.5 仍有此行为）
    """
    if df.empty:
        return df
    for col in df.columns:
        s = df[col]
        if s.dtype == object and isinstance(s.iloc[0], date):
            df[col] = pd.to_datetime(s)
    return df


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def date_bounds() -> tuple:
    """数据实际日期范围，用作筛选器默认值（别用 datetime.now()，数据是 2016-2018）"""
    df = query("SELECT MIN(purchase_date) AS d_min, MAX(purchase_date) AS d_max "
               "FROM ads_sale_overview_daily")
    return df["d_min"].iloc[0], df["d_max"].iloc[0]


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def category_options() -> list:
    df = query("SELECT DISTINCT category_name FROM v_sale_daily_category "
               "WHERE category_name <> 'unknown' ORDER BY 1")
    return df["category_name"].tolist()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def seller_state_options() -> list:
    df = query("SELECT DISTINCT seller_state FROM v_sale_daily_seller_state ORDER BY 1")
    return df["seller_state"].tolist()


def dim_where(f: dict, column: str, key: str) -> tuple:
    """
    生成维度筛选的 SQL 片段，返回 (SQL, 参数元组)。

    column 由本文件写，值一律走绑定参数，无注入风险。
    空选择 = 全选，不加条件。
    """
    values = f.get(key) or []
    if not values:
        return "", ()
    name = f"{key}_vals"
    return f" AND {column} = ANY(:{name}) ", ((name, tuple(values)),)


def render_sidebar_filters() -> None:
    """
    渲染侧边栏全局筛选器，结果写入 st.session_state["filters"]。

    放在入口脚本（dashboard.py）里，这样 5 个页面共享同一套筛选条件，
    切换页面时筛选状态不丢失。
    """
    d_min, d_max = date_bounds()
    st.sidebar.header("筛选条件")

    picked = st.sidebar.date_input(
        "购买日期",
        value=(d_min, d_max),          # 默认全量范围，而不是"最近 7 天"
        min_value=d_min,
        max_value=d_max,
        help=f"数据实际范围：{d_min} ~ {d_max}",
    )
    # date_input 在用户只点了起始日期时会返回长度 1 的元组，需要兜底
    if isinstance(picked, (list, tuple)):
        if len(picked) == 2:
            d1, d2 = picked
        elif len(picked) == 1:
            d1 = d2 = picked[0]
        else:
            d1, d2 = d_min, d_max
    else:
        d1 = d2 = picked

    cats = st.sidebar.multiselect(
        "英文类目", category_options(), default=[],
        placeholder="全部类目",
        help="类目分析页会用到；留空表示全部，本区块按类目维度聚合，不受卖家州筛选影响",
    )
    states = st.sidebar.multiselect( 
        "卖家州", seller_state_options(), default=[],
        placeholder="全部卖家州",
        help="卖家与地区页会用到；留空表示全部",
    )
    min_cohort = st.sidebar.slider(
        "留存最小 cohort 规模", min_value=1, max_value=200,
        value=MIN_GROUP_SIZE, step=5,
        help="cohort 规模太小时会被 1/1=100% 这类噪声放大，设个门槛更可信",
    )

    st.sidebar.divider()
    if st.sidebar.button("刷新数据缓存", width="stretch"):
        # ADS 层重跑后缓存不会自动失效，需要手动清
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption(
        f"数据源：PostgreSQL ADS 层 · 缓存 {CACHE_TTL}s · "
        f"留存窗口 {RETENTION_WINDOWS}"
    )

    st.session_state["filters"] = {
        "d1": d1, "d2": d2,
        "cats": cats, "states": states,
        "min_cohort": min_cohort,
    }


def filters() -> dict:
    """读取全局筛选条件；直接运行单个页面文件时给全量兜底"""
    f = st.session_state.get("filters")
    if f:
        return f
    d_min, d_max = date_bounds()
    return {"d1": d_min, "d2": d_max, "cats": [], "states": [],
            "min_cohort": MIN_GROUP_SIZE}


def date_params(f: dict) -> tuple:
    """日期区间的绑定参数，供各页复用"""
    return (("d1", f["d1"]), ("d2", f["d2"]))


def money(value, unit: str = "R$") -> str:
    """金额展示：Olist 是巴西数据，用 R$ 更贴合业务背景"""
    if value is None:
        return "-"
    return f"{unit} {value:,.0f}"


def to_month(series: pd.Series) -> pd.Series:
    """转成「月首日」时间戳，供 Altair 时间轴按月聚合使用"""
    return pd.to_datetime(series).dt.to_period("M").dt.to_timestamp()


def stop_if_empty(df: pd.DataFrame) -> None:
    if df.empty:
        st.warning("当前筛选条件下没有数据，请放宽日期范围或清空维度筛选。")
        st.stop()
