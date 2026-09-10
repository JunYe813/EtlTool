"""看板页面 2 · 商品分析"""
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import altair as alt                          # noqa: E402
import pandas as pd                           # noqa: E402
import streamlit as st                        # noqa: E402

from db import (                              # noqa: E402
    ACCENT, TOP_N, date_params, dim_where, filters, money, query, stop_if_empty,
    to_month,
)

f = filters()
st.header("商品分析")
st.caption("数据源：`v_sale_daily_category`（日×类目）+ `dwd_order_detail`（商品粒度）+ `ads_top_product`")

# ---------- 类目 GMV（按筛选区间重新聚合）----------
cat_where, cat_params = dim_where(f, "category_name", "cats")

cats = query(
    f"""
    SELECT category_name,
           SUM(gmv)     AS gmv,
           SUM(item_cnt) AS item_cnt
    FROM v_sale_daily_category
    WHERE purchase_at BETWEEN :d1 AND :d2 {cat_where}
    GROUP BY category_name
    """,
    date_params(f) + cat_params,
)
stop_if_empty(cats)

# 占比在筛选后的数据集上现算。
# 不直接用视图里预算好的百分比 —— 那种写法按日期筛选后分母仍是全周期总额，加起来不等于 100%。
total_gmv = float(cats["gmv"].sum())
cats["gmv_pct"] = (cats["gmv"] / total_gmv * 100).round(2)
cats = cats.sort_values("gmv", ascending=False)

top_n_pie = st.slider("饼图展示类目数", 5, 20, 10, help="其余类目归并为「其他」")

head = cats.head(top_n_pie).copy()
rest = cats.iloc[top_n_pie:]
if not rest.empty:
    # 用 pd.concat 而不是已移除的 DataFrame.append
    head = pd.concat([head, pd.DataFrame([{
        "category_name": "其他",
        "gmv": rest["gmv"].sum(),
        "item_cnt": rest["item_cnt"].sum(),
        "gmv_pct": rest["gmv_pct"].sum(),
    }])], ignore_index=True)

c1, c2, c3 = st.columns(3)
c1.metric("区间 GMV", money(total_gmv))
c2.metric("类目数", f"{len(cats)}")
c3.metric("最大类目", cats["category_name"].iloc[0],
          help=f"占比 {cats['gmv_pct'].iloc[0]:.2f}%")

pie = (
    alt.Chart(head)
    .mark_arc(innerRadius=70, stroke="#fff", strokeWidth=1)
    .encode(
        theta=alt.Theta("gmv:Q"),
        color=alt.Color("category_name:N", legend=alt.Legend(title="类目", orient="right")),
        tooltip=[
            alt.Tooltip("category_name:N", title="类目"),
            alt.Tooltip("gmv:Q", title="GMV", format=",.0f"),
            alt.Tooltip("gmv_pct:Q", title="占比 %", format=".2f"),
        ],
    )
    .properties(height=420, title="类目 GMV 占比")
)

bar = (
    alt.Chart(cats.head(15))
    .mark_bar(color=ACCENT)
    .encode(
        x=alt.X("gmv:Q", title="GMV (R$)"),
        y=alt.Y("category_name:N", sort="-x", title=None),
        tooltip=[
            alt.Tooltip("category_name:N", title="类目"),
            alt.Tooltip("gmv:Q", title="GMV", format=",.0f"),
            alt.Tooltip("item_cnt:Q", title="销量", format=","),
            alt.Tooltip("gmv_pct:Q", title="占比 %", format=".2f"),
        ],
    )
    .properties(height=420, title="类目 GMV Top 15")
)

left, right = st.columns([2, 3])
left.altair_chart(pie, width="stretch")
right.altair_chart(bar, width="stretch")

st.divider()

# ---------- Top 类目趋势 ----------
st.subheader("Top 类目趋势")
trend_cats = st.multiselect("选择类目", cats["category_name"].head(12).tolist(),
                            default=cats["category_name"].head(4).tolist())
if trend_cats:
    trend = query(
        f"""
        SELECT purchase_at, category_name, gmv
        FROM v_sale_daily_category
        WHERE purchase_at BETWEEN :d1 AND :d2
          AND category_name = ANY(:trend_cats)
        ORDER BY purchase_at
        """,
        date_params(f) + (("trend_cats", tuple(trend_cats)),),
    )
    trend["month"] = to_month(trend["purchase_at"])
    monthly = trend.groupby(["month", "category_name"], as_index=False)["gmv"].sum()
    st.altair_chart(
        alt.Chart(monthly)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("month:T", title=None),
            y=alt.Y("gmv:Q", title="GMV (R$)"),
            color=alt.Color("category_name:N", title="类目"),
            tooltip=[alt.Tooltip("month:T", title="月份", format="%Y-%m"),
                     alt.Tooltip("category_name:N", title="类目"),
                     alt.Tooltip("gmv:Q", title="GMV", format=",.0f")],
        )
        .properties(height=320, title="类目 GMV 月度趋势"),
        width="stretch",
    )

st.divider()


# ---------- 商品按天汇总 ----------
st.subheader("单日单品 TopN（爆单榜）")
topn = st.slider("groupProDate", 5, 50, TOP_N, key="group_Pro_Date")
st.caption(
    "每条记录 = 某商品在某一天的销售汇总（数据源 dws_sale_daily_product）。"
    "按单日 GMV 排序取前 N，用于定位销售峰值日；"
    "要看区间累计最高商品请见下方「热销商品 TopN」。"
)
productsDate = query(
    f"""
    SELECT purchase_at as purchase_date,
           product_id,
           category_name,
           order_cnt,
           item_cnt,
           gmv
    FROM dws_sale_daily_product
    WHERE purchase_at BETWEEN :d1 AND :d2
    ORDER BY gmv DESC, purchase_date, product_id
    LIMIT :topn
    """,
    date_params(f) + (("topn", topn),),
)

if productsDate.empty:
    st.info("当前筛选条件下没有商品数据。")
else:
    productsDate = productsDate.reset_index(drop=True)
    productsDate["label"] = (
    productsDate["purchase_date"].dt.strftime("%Y-%m-%d")   # 日期放最前面
        + " · " + productsDate["category_name"].str.slice(0, 12)  # 类目缩到 12 字，给日期腾地方
        + " · " + productsDate["product_id"].str.slice(0, 8)
    )
    # productsDate.index = productsDate.index + 1     # 排名从 1 开始

    # 横向柱状图展示
    st.altair_chart(
        alt.Chart(productsDate)
        .mark_bar()
        .encode(
            x = alt.X("gmv:Q",title="GMV(R$)"),
            y = alt.Y("label:N",title=None,sort="-x"),
            tooltip=[
                    alt.Tooltip("purchase_date:T", title="日期", format="%Y-%m-%d"),
                    alt.Tooltip("product_id:N", title="商品 ID"),
                    alt.Tooltip("category_name:N", title="类目"),
                    alt.Tooltip("gmv:Q", title="GMV", format=",.0f"),
                    alt.Tooltip("order_cnt:Q", title="订单数", format=","),
                    alt.Tooltip("item_cnt:Q", title="销量", format=",")
                ]
            )
        .properties(height=600, title="商品按天汇总"),
        width="stretch"

    )

    st.dataframe(
        productsDate.rename(columns={
            "product_id": "商品 ID", "category_name": "类目",
            "gmv": "GMV", "order_cnt": "订单数", "qty": "销量",
        }),
        width="stretch",
    )




# ---------- 热销商品 TopN ----------
st.subheader("热销商品 TopN")
topn = st.slider("TopN", 5, 50, TOP_N, key="topn_products")

products = query(
    f"""
    SELECT product_id,
           MAX(category_name)        AS category_name,
           ROUND(SUM(price), 2)      AS gmv,
           COUNT(DISTINCT order_id)  AS order_cnt,
           COUNT(*)                  AS qty
    FROM dwd_order_detail
    WHERE is_valid AND purchase_date BETWEEN :d1 AND :d2 {cat_where}
    GROUP BY product_id
    ORDER BY gmv DESC
    LIMIT :topn
    """,
    date_params(f) + cat_params + (("topn", topn),),
)


if products.empty:
    st.info("当前筛选条件下没有商品数据。")
else:
    products = products.reset_index(drop=True)
    products["label"] = products["category_name"].str.slice(0, 18) + " · " + products["product_id"].str.slice(0, 8)
    products.index = products.index + 1     # 排名从 1 开始

    # 横向柱状图展示
    st.altair_chart(
        alt.Chart(products)
        .mark_bar()
        .encode(
            x = alt.X("gmv:Q",title="GMV(R$)"),
            y = alt.Y("label:N",title=None,sort="-x"),
            tooltip=[
                alt.Tooltip("product_id:N", title="商品 ID"),   # 完整 ID
                alt.Tooltip("category_name:N", title="类目"),
                alt.Tooltip("gmv:Q", title="GMV", format=",.0f"),
                alt.Tooltip("order_cnt:Q", title="订单数", format=","),
                alt.Tooltip("qty:Q", title="销量", format=",")
                ]
            )
        .properties(height=600, title="热销商品 TopN"),
        width="stretch"

    )

    st.dataframe(
        products.rename(columns={
            "product_id": "商品 ID", "category_name": "类目",
            "gmv": "GMV", "order_cnt": "订单数", "qty": "销量",
        }),
        width="stretch",
    )

with st.expander("每日 Top10 商品榜（来自 ADS 预聚合表 `ads_top_product`）"):
    st.caption(
        "单日榜单直接用预聚合结果，避免每次现算 ROW_NUMBER()。"
        "该表的排序已加 `product_id` 兜底，并列时结果可复现。"
    )
    day = st.date_input("选择日期", value=f["d2"] if f["d2"] else None, key="top_day")
    if day:
        daily = query(
            """
            SELECT rank_no, product_id, category_name, gmv
            FROM ads_top_product
            WHERE purchase_date = :d
            ORDER BY rank_no
            """,
            (("d", day),),
        )
        if daily.empty:
            st.info(f"{day} 没有榜单数据（可能当天无有效订单）。")
        else:
            st.dataframe(
                daily.rename(columns={"rank_no": "排名", "product_id": "商品 ID",
                                      "category_name": "类目", "gmv": "GMV"}),
                width="stretch", hide_index=True,
            )
