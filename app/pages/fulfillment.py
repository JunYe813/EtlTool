"""看板页面 5 · 履约漏斗"""
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import altair as alt                          # noqa: E402
import pandas as pd                           # noqa: E402
import streamlit as st                        # noqa: E402

from db import ACCENT, WARN, date_params, filters, query, stop_if_empty   # noqa: E402

f = filters()
st.header("履约漏斗")
st.caption("数据源：`v_fulfillment_funnel`（日粒度） + `ads_fulfillment_monthly`")

st.info(
    "**口径说明**：漏斗使用**全量订单**，不过滤有效订单。"
    "因为漏斗的价值就在于体现「下单 → 审批 → 交承运 → 签收」的逐级流失，"
    "而取消/未支付订单正是流失的主要来源；过滤掉它们会把漏斗削平、看不出问题。"
    "销售类指标仍按有效订单口径（见「销售总览」页）。"
)

funnel = query(
    """
    SELECT SUM(order_cnt)      AS order_cnt,
           SUM(approved_cnt)   AS approved_cnt,
           SUM(shipped_cnt)    AS shipped_cnt,
           SUM(delivered_cnt)  AS delivered_cnt,
           SUM(canceled_cnt)   AS canceled_cnt
    FROM v_fulfillment_funnel
    WHERE purchase_date BETWEEN :d1 AND :d2
    """,
    date_params(f),
)
stop_if_empty(funnel)
r = funnel.iloc[0]

orders = int(r["order_cnt"])
if orders == 0:
    st.warning("当前筛选条件下没有订单。")
    st.stop()

stages = pd.DataFrame({
    "阶段": ["下单", "支付审批", "交承运", "客户签收"],
    "订单数": [int(r["order_cnt"]), int(r["approved_cnt"]),
               int(r["shipped_cnt"]), int(r["delivered_cnt"])],
    "时间戳列": ["order_purchase_timestamp", "order_approved_at",
                 "order_delivered_carrier_date", "order_delivered_customer_date"],
})
stages["占下单比"] = (stages["订单数"] / orders)
stages["本环节流失"] = stages["订单数"].shift(1).fillna(orders) - stages["订单数"]

cols = st.columns(4)
for col, (_, s) in zip(cols, stages.iterrows()):
    col.metric(s["阶段"], f"{s['订单数']:,}", f"{s['占下单比'] * 100:.2f}%")

c1, c2, c3 = st.columns(3)
c1.metric("取消订单数", f"{int(r['canceled_cnt']):,}",
          f"{int(r['canceled_cnt']) / orders * 100:.2f}%")
c2.metric("未签收订单", f"{orders - int(r['delivered_cnt']):,}",
          f"{(1 - int(r['delivered_cnt']) / orders) * 100:.2f}%")
c3.metric("整体签收率", f"{int(r['delivered_cnt']) / orders * 100:.2f}%")

st.divider()

# ---------- 漏斗图 ----------
funnel_chart = (
    alt.Chart(stages)
    .mark_bar(size=44, color=ACCENT)
    .encode(
        y=alt.Y("阶段:N", sort=None, title=None),
        x=alt.X("订单数:Q", title="订单数"),
        tooltip=[alt.Tooltip("阶段:N", title="阶段"),
                 alt.Tooltip("订单数:Q", title="订单数", format=","),
                 alt.Tooltip("占下单比:Q", title="占下单比", format=".2%"),
                 alt.Tooltip("本环节流失:Q", title="本环节流失", format=",")],
    )
    .properties(height=260, title="履约漏斗（全量订单）")
)
text = funnel_chart.mark_text(align="left", dx=6, fontSize=13).encode(
    text=alt.Text("订单数:Q", format=","))
st.altair_chart(funnel_chart + text, width="stretch")

loss = stages[stages["本环节流失"] > 0]
if not loss.empty:
    st.caption(
        "各环节流失："
        + "；".join(f"{s['阶段']} 流失 {int(s['本环节流失']):,} 单"
                    for _, s in loss.iterrows())
    )

st.divider()

# ---------- 履约时长 ----------
st.subheader("履约时长")
dur = query(
    """
    SELECT ROUND(SUM(avg_approve_hours * order_cnt) / NULLIF(SUM(order_cnt), 0), 2) AS approve_h,
           ROUND(SUM(avg_ship_days     * shipped_cnt)  / NULLIF(SUM(shipped_cnt), 0), 2)   AS ship_d,
           ROUND(SUM(avg_deliver_days  * delivered_cnt)/ NULLIF(SUM(delivered_cnt), 0), 2) AS deliver_d
    FROM ads_fulfillment_monthly
    WHERE purchase_month BETWEEN :d1 AND :d2
    """,
    date_params(f),
)
if dur.empty or pd.isna(dur["deliver_d"].iloc[0]):
    st.info("当前筛选区间内没有完整的履约时长数据。")
else:
    d = dur.iloc[0]
    c1, c2, c3 = st.columns(3)
    c1.metric("平均审批时长", f"{float(d['approve_h']):.1f} 小时")
    c2.metric("平均发货时长", f"{float(d['ship_d']):.1f} 天", help="下单 → 交承运")
    c3.metric("平均签收时长", f"{float(d['deliver_d']):.1f} 天", help="下单 → 客户签收")
    st.caption(
        "汇总时长按订单量加权（`Σ(月度均值 × 订单数) ÷ Σ订单数`）。"
        "直接对月度均值求平均是「均值的均值」，会得到偏大的结果 —— "
        "实测未加权 14.31 天 vs 加权 12.50 天。"
    )

st.divider()

# ---------- 月度趋势 ----------
st.subheader("月度履约趋势")
monthly = query(
    """
    SELECT purchase_month, order_cnt, approved_cnt, shipped_cnt, delivered_cnt,
           cancel_rate, avg_approve_hours, avg_ship_days, avg_deliver_days, late_rate
    FROM ads_fulfillment_monthly
    WHERE purchase_month BETWEEN :d1 AND :d2
    ORDER BY purchase_month
    """,
    date_params(f),
)

if monthly.empty:
    st.info("当前筛选区间内没有月度数据。")
else:
    melt = monthly.melt(
        id_vars=["purchase_month", "order_cnt"],
        value_vars=["approved_cnt", "shipped_cnt", "delivered_cnt"],
        var_name="阶段", value_name="订单数",
    ).replace({"approved_cnt": "支付审批", "shipped_cnt": "交承运", "delivered_cnt": "客户签收"})

    trend = (
        alt.Chart(melt)
        .mark_line(point=True, strokeWidth=2)
        .encode(
            x=alt.X("purchase_month:T", title=None),
            y=alt.Y("订单数:Q", title="订单数"),
            color=alt.Color("阶段:N", title="阶段", scale=alt.Scale(scheme="tableau10")),
            tooltip=[alt.Tooltip("purchase_month:T", title="月份", format="%Y-%m"),
                     alt.Tooltip("阶段:N", title="阶段"),
                     alt.Tooltip("订单数:Q", title="订单数", format=",")],
        )
        .properties(height=320, title="各履约阶段月度订单数")
    )

    dur_trend = (
        alt.Chart(monthly)
        .mark_line(point=True, strokeWidth=2, color=WARN)
        .encode(
            x=alt.X("purchase_month:T", title=None),
            y=alt.Y("avg_deliver_days:Q", title="天"),
            tooltip=[alt.Tooltip("purchase_month:T", title="月份", format="%Y-%m"),
                     alt.Tooltip("avg_deliver_days:Q", title="平均签收天数", format=".2f"),
                     alt.Tooltip("avg_ship_days:Q", title="平均发货天数", format=".2f"),
                     alt.Tooltip("late_rate:Q", title="逾期率", format=".2%")],
        )
        .properties(height=320, title="平均签收时长月度趋势")
    )

    st.altair_chart(trend, width="stretch")
    st.altair_chart(dur_trend, width="stretch")

    st.dataframe(
        monthly.rename(columns={
            "purchase_month": "月份", "order_cnt": "下单", "approved_cnt": "审批",
            "shipped_cnt": "交承运", "delivered_cnt": "签收", "cancel_rate": "取消率",
            "avg_approve_hours": "审批小时", "avg_ship_days": "发货天数",
            "avg_deliver_days": "签收天数", "late_rate": "逾期率",
        }),
        width="stretch", hide_index=True,
    )
