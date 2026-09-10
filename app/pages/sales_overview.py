"""看板页面 1 · 销售总览"""
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import altair as alt                          # noqa: E402
import streamlit as st                        # noqa: E402

from db import (                              # noqa: E402
    ACCENT, WARN, date_params, filters, money, query, stop_if_empty, to_month,
)

f = filters()
st.header("销售总览")
st.caption("数据源：`ads_sale_overview_daily`（日粒度，客单价口径已写死在表里）")

df = query(
    """
    SELECT purchase_date, gmv, freight_total, paid_amount,
           order_cnt, item_cnt, avg_order_value
    FROM ads_sale_overview_daily
    WHERE purchase_date BETWEEN :d1 AND :d2
    ORDER BY purchase_date
    """,
    date_params(f),
)
stop_if_empty(df)

gmv = float(df["gmv"].sum())
freight = float(df["freight_total"].sum())
paid = float(df["paid_amount"].sum())
orders = int(df["order_cnt"].sum())
items = int(df["item_cnt"].sum())

# 区间客单价必须用「区间 GMV ÷ 区间订单数」重算。
# 不能对日客单价求平均，也不能用 SUM(dws_sale_daily.order_cnt) 当分母
# （那张表按 日×类目×州 拆分，跨类目/跨州订单会被重复计数）。
aov = gmv / orders if orders else 0.0

# 去重买家必须区间现算：日表 buyer_cnt 跨天相加会把跨天复购的买家重复计数
# （实测 SUM = 97272，真实去重 = 94986）
buyers = int(query(
    """
    SELECT COUNT(DISTINCT customer_unique_id) AS buyer_cnt
    FROM dwd_order
    WHERE is_valid AND purchase_at::date BETWEEN :d1 AND :d2
    """,
    date_params(f),
)["buyer_cnt"].iloc[0])

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("GMV", money(gmv), help="有效订单的明细 price 合计，不含运费")
c2.metric("有效订单数", f"{orders:,}", help="已完成支付的订单（排除 created/canceled/unavailable）")
c3.metric("客单价", f"R$ {aov:,.2f}", help="区间 GMV ÷ 区间有效订单数")
c4.metric("去重买家数", f"{buyers:,}", help="按 customer_unique_id 去重，区间内现算")
c5.metric("明细行数", f"{items:,}")
st.caption(
    f"区间实付金额（含运费）R$ {paid:,.2f}，其中运费 R$ {freight:,.2f} "
    f"（占比 {freight / paid * 100:.1f}%）"
)

st.divider()

# ---------------- 趋势图 ----------------
granularity = st.radio("趋势粒度", ["按日", "按月"], horizontal=True, label_visibility="collapsed")

plot = df[["purchase_date", "gmv", "order_cnt"]].copy()
if granularity == "按月":
    plot = (plot.groupby(to_month(plot["purchase_date"]))
                .agg({"gmv": "sum", "order_cnt": "sum"})
                .reset_index()
                .rename(columns={"purchase_date": "period"}))
else:
    plot = plot.rename(columns={"purchase_date": "period"})

plot["gmv_ma"] = plot["gmv"].rolling(7 if granularity == "按日" else 3,
                                     min_periods=1).mean()

base = alt.Chart(plot).encode(
    x=alt.X("period:T", title=None),
    tooltip=[
        alt.Tooltip("period:T", title="日期", format="%Y-%m-%d"),
        alt.Tooltip("gmv:Q", title="GMV", format=",.0f"),
        alt.Tooltip("order_cnt:Q", title="订单数", format=","),
    ],
)

gmv_chart = (
    base.mark_area(color=ACCENT, opacity=0.22)
    + base.mark_line(color=ACCENT, strokeWidth=2).encode(y=alt.Y("gmv:Q", title="GMV (R$)"))
    + base.mark_line(color=WARN, strokeWidth=1.6, strokeDash=[5, 3])
          .encode(y=alt.Y("gmv_ma:Q", title="GMV (R$)"))
).properties(height=300, title=f"GMV {granularity}趋势（虚线为移动平均）")

order_chart = (
    base.mark_bar(color="#72B7B2", opacity=0.85)
        .encode(y=alt.Y("order_cnt:Q", title="订单数"))
).properties(height=300, title=f"有效订单数 {granularity}趋势")

left, right = st.columns([3, 2])
left.altair_chart(gmv_chart, width="stretch")
right.altair_chart(order_chart, width="stretch")

with st.expander("查看明细数据"):
    show = df.rename(columns={
        "purchase_date": "购买日期", "gmv": "GMV", "freight_total": "运费",
        "paid_amount": "实付金额", "order_cnt": "订单数", "item_cnt": "明细行数",
        "avg_order_value": "日客单价",
    })
    st.dataframe(show, width="stretch", hide_index=True)
    st.download_button("下载 CSV", show.to_csv(index=False).encode("utf-8-sig"),
                       file_name="sales_overview.csv", mime="text/csv")
