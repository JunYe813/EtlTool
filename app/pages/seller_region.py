"""看板页面 3 · 卖家与地区"""
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import altair as alt                          # noqa: E402
import streamlit as st                        # noqa: E402

from db import (                              # noqa: E402
    ACCENT, date_params, dim_where, filters, money, query, stop_if_empty,
)

f = filters()
st.header("卖家与地区")
st.caption("数据源：`v_sale_daily_seller_state` + `v_sale_daily_buyer_state` + `ads_top_seller`")

state_where, state_params = dim_where(f, "seller_state", "states")

# ---------- 卖家州 ----------
seller_states = query(
    f"""
    SELECT seller_state,
           SUM(gmv)      AS gmv,
           SUM(item_cnt) AS item_cnt
    FROM v_sale_daily_seller_state
    WHERE purchase_at BETWEEN :d1 AND :d2 {state_where}
    GROUP BY seller_state
    ORDER BY gmv DESC
    """,
    date_params(f) + state_params,
)
stop_if_empty(seller_states)

# ---------- 买家州 ----------
buyer_states = query(
    """
    SELECT customer_state,
           SUM(order_cnt) AS order_cnt,
           SUM(gmv)       AS gmv
    FROM v_sale_daily_buyer_state
    WHERE purchase_date BETWEEN :d1 AND :d2
    GROUP BY customer_state
    ORDER BY gmv DESC
    """,
    date_params(f),
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("卖家州数", f"{len(seller_states)}")
c2.metric("买家州数", f"{len(buyer_states)}")
c3.metric("卖家州 GMV 合计", money(seller_states["gmv"].sum()))
c4.metric("买家州 GMV 合计", money(buyer_states["gmv"].sum()))
st.caption(
    "两个 GMV 合计相等是正常的（同一批有效订单，只是换个维度看）。"
    "但**订单数跨州不可相加**：一单可含多个卖家州的商品，会被重复计数。"
)

seller_bar = (
    alt.Chart(seller_states)
    .mark_bar(color=ACCENT)
    .encode(
        x=alt.X("seller_state:N", sort="-y", title="卖家州"),
        y=alt.Y("gmv:Q", title="GMV (R$)"),
        tooltip=[alt.Tooltip("seller_state:N", title="州"),
                 alt.Tooltip("gmv:Q", title="GMV", format=",.0f"),
                 alt.Tooltip("item_cnt:Q", title="销量", format=",")],
    )
    .properties(height=330, title="各卖家州 GMV 对比")
)

buyer_bar = (
    alt.Chart(buyer_states)
    .mark_bar(color="#F58518")
    .encode(
        x=alt.X("customer_state:N", sort="-y", title="买家州"),
        y=alt.Y("gmv:Q", title="GMV (R$)"),
        tooltip=[alt.Tooltip("customer_state:N", title="州"),
                 alt.Tooltip("gmv:Q", title="GMV", format=",.0f"),
                 alt.Tooltip("order_cnt:Q", title="订单数", format=",")],
    )
    .properties(height=330, title="各买家州 GMV 对比")
)

st.altair_chart(seller_bar, width="stretch")
st.altair_chart(buyer_bar, width="stretch")

st.divider()

# ---------- 卖家 TopN ----------
st.subheader("卖家 TopN")
topn = st.slider("TopN", 5, 50, 15)

sellers = query(
    f"""
    SELECT seller_id,
           MAX(seller_state)         AS seller_state,
           MAX(seller_city)          AS seller_city,
           ROUND(SUM(price), 2)      AS gmv,
           COUNT(DISTINCT order_id)  AS order_cnt,
           COUNT(*)                  AS qty
    FROM dwd_order_detail
    WHERE is_valid AND purchase_date BETWEEN :d1 AND :d2 {state_where}
    GROUP BY seller_id
    ORDER BY gmv DESC
    LIMIT :topn
    """,
    date_params(f) + state_params + (("topn", topn),),
)

if sellers.empty:
    st.info("当前筛选条件下没有卖家数据。")
else:
    sellers = sellers.reset_index(drop=True)
    sellers.index = sellers.index + 1
    st.dataframe(
        sellers.rename(columns={
            "seller_id": "卖家 ID", "seller_state": "州", "seller_city": "城市",
            "gmv": "GMV", "order_cnt": "订单数", "qty": "销量",
        }),
        width="stretch",
    )
    total_sellers = query(
        f"""
        SELECT COUNT(*) AS n FROM (
            SELECT seller_id FROM dwd_order_detail
            WHERE is_valid AND purchase_date BETWEEN :d1 AND :d2 {state_where}
            GROUP BY seller_id
        ) x
        """,
        date_params(f) + state_params,
    )["n"].iloc[0]
    st.caption(f"区间内共 {int(total_sellers):,} 个有成交的卖家（排行榜只展示前 N 名）。")

with st.expander("全周期卖家榜（来自 ADS 预聚合表 `ads_top_seller`）"):
    st.caption("筛选器不作用于该表 —— 它是全周期预聚合结果，用于「不受日期筛选影响的总榜」。")
    st.dataframe(
        query("""
            SELECT seller_id, seller_state, seller_city, gmv, order_cnt, item_cnt, avg_item_price
            FROM ads_top_seller ORDER BY gmv DESC LIMIT 20
        """).rename(columns={
            "seller_id": "卖家 ID", "seller_state": "州", "seller_city": "城市",
            "gmv": "全周期 GMV", "order_cnt": "订单数", "item_cnt": "销量",
            "avg_item_price": "件均价",
        }),
        width="stretch", hide_index=True,
    )
