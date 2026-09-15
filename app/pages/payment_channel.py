"""看板页面 7 · 渠道对比（支付方式）"""
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import altair as alt                          # noqa: E402
import pandas as pd                           # noqa: E402
import streamlit as st                        # noqa: E402

from db import (                              # noqa: E402
    ACCENT, date_params, filters, money, query, stop_if_empty, to_month,
)

f = filters()
st.header("渠道对比")
st.caption("数据源：`v_payment_daily_channel`（日 × 支付方式）")

st.info(
    "**口径说明**：只统计**有效订单**的支付记录，与销售侧一致。\n\n"
    "这里有一个必须讲清的点：**各渠道的订单数不能相加**。"
    "一单可以用多种支付方式（比如「优惠券 + 信用卡」），实测有 **2,211 单**是这种情况，"
    "所以把各渠道订单数加起来会虚高。**金额是可加的**，订单数不行。\n\n"
    "这和 `dws_sale_daily.order_cnt` 是同一类问题：**跨天可加、跨维度不可加**。"
)

# ---------------- 渠道汇总（在筛选后的区间上重新聚合）----------------
ch = query(
    """
    SELECT payment_type,
           SUM(order_cnt)        AS order_cnt,
           SUM(pay_row_cnt)      AS pay_row_cnt,
           SUM(payment_amount)   AS payment_amount,
           SUM(installments_sum) AS installments_sum
    FROM v_payment_daily_channel
    WHERE purchase_date BETWEEN :d1 AND :d2
    GROUP BY payment_type
    ORDER BY payment_amount DESC
    """,
    date_params(f),
)
stop_if_empty(ch)

total_amount = float(ch["payment_amount"].sum())
ch["amount_pct"] = (ch["payment_amount"] / total_amount * 100).round(2)
# 加权平均分期 = Σ分子 ÷ Σ分母。不能用 AVG(平均分期)，那是「均值的均值」。
ch["avg_installments"] = (ch["installments_sum"] / ch["pay_row_cnt"]).round(2)

# 真实去重订单数（区间内、有效订单、且有支付记录）
# 注意：各渠道 order_cnt 相加会虚高，所以真实值必须单独算
real_orders = query(
    """
    SELECT COUNT(DISTINCT d.order_id) AS order_cnt
    FROM dwd_order d
    JOIN olist_order_payments_dataset p ON p.order_id = d.order_id
    WHERE d.is_valid
      AND d.purchase_at >= :d1
      AND d.purchase_at <  :d2 + INTERVAL '1 day'
    """,
    date_params(f),
)
real_orders = int(real_orders.iloc[0]["order_cnt"])
summed_orders = int(ch["order_cnt"].sum())
over_count = summed_orders - real_orders

overall_installments = (
    float(ch["installments_sum"].sum()) / float(ch["pay_row_cnt"].sum())
)

# ---------------- 指标 ----------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("支付总金额", money(total_amount), help="各渠道金额可加，相加即总额")
c2.metric("覆盖订单（去重）", f"{real_orders:,}",
          help="区间内有效订单且有支付记录的去重数")
c3.metric("支付渠道数", f"{len(ch)}")
c4.metric("加权平均分期", f"{overall_installments:.2f} 期",
          help="Σ分期数 ÷ Σ支付笔数。直接对渠道平均分期求平均是「均值的均值」")

if over_count > 0:
    st.warning(
        f"**各渠道订单数相加 = {summed_orders:,}，真实去重订单 = {real_orders:,}，"
        f"虚高 {over_count:,} 单。** "
        "一单可含多种支付方式，所以订单数**跨渠道不可加** —— "
        "看板上任何「各渠道订单数占比」都要用金额或支付笔数当分母，不能用订单数。"
    )

# ---------------- 占比 ----------------
st.divider()
st.subheader("渠道构成")

pie = (
    alt.Chart(ch)
    .mark_arc(innerRadius=70)
    .encode(
        theta=alt.Theta("payment_amount:Q", title="支付金额"),
        color=alt.Color("payment_type:N", title="支付方式",
                        scale=alt.Scale(scheme="tableau10")),
        tooltip=[alt.Tooltip("payment_type:N", title="支付方式"),
                 alt.Tooltip("payment_amount:Q", title="支付金额", format=",.2f"),
                 alt.Tooltip("amount_pct:Q", title="金额占比", format=".2f"),
                 alt.Tooltip("order_cnt:Q", title="订单数（不可加）", format=","),
                 alt.Tooltip("avg_installments:Q", title="平均分期", format=".2f")],
    )
    .properties(height=320, title="各渠道支付金额占比")
)
bar = (
    alt.Chart(ch)
    .mark_bar(size=34, color=ACCENT)
    .encode(
        y=alt.Y("payment_type:N", sort="-x", title=None),
        x=alt.X("payment_amount:Q", title="支付金额"),
        tooltip=[alt.Tooltip("payment_type:N", title="支付方式"),
                 alt.Tooltip("payment_amount:Q", title="支付金额", format=",.2f"),
                 alt.Tooltip("amount_pct:Q", title="占比", format=".2f")],
    )
    .properties(height=320, title="各渠道支付金额")
)
btext = bar.mark_text(align="left", dx=6, fontSize=12).encode(
    text=alt.Text("payment_amount:Q", format=",.0f"))

col1, col2 = st.columns([1, 1])
with col1:
    st.altair_chart(pie, width="stretch")
with col2:
    st.altair_chart(bar + btext, width="stretch")

st.dataframe(
    ch.rename(columns={
        "payment_type": "支付方式", "order_cnt": "订单数（不可加）",
        "pay_row_cnt": "支付笔数", "payment_amount": "支付金额",
        "amount_pct": "金额占比", "installments_sum": "分期数合计",
        "avg_installments": "平均分期",
    })[["支付方式", "支付金额", "金额占比", "订单数（不可加）",
        "支付笔数", "平均分期"]],
    width="stretch", hide_index=True,
    column_config={
        "支付金额": st.column_config.NumberColumn("支付金额", format="R$ %.2f"),
        "金额占比": st.column_config.NumberColumn("金额占比", format="%.2f%%"),
        "平均分期": st.column_config.NumberColumn("平均分期", format="%.2f"),
    },
)

# ---------------- 月度趋势 ----------------
st.divider()
st.subheader("渠道份额月度趋势")

daily = query(
    """
    SELECT purchase_date, payment_type, payment_amount, pay_row_cnt
    FROM v_payment_daily_channel
    WHERE purchase_date BETWEEN :d1 AND :d2
    """,
    date_params(f),
)

if daily.empty:
    st.info("当前筛选区间内没有数据。")
else:
    # 月度聚合在 pandas 里做，不写 DATE_TRUNC —— 看板的 query() 走 text()，
    # SQL 里的 ::cast 会被当成绑定参数（见 db.py 顶部说明）
    monthly = (
        daily.assign(purchase_month=to_month(daily["purchase_date"]))
        .groupby(["purchase_month", "payment_type"], as_index=False)["payment_amount"].sum()
    )
    monthly["month_total"] = monthly.groupby("purchase_month")["payment_amount"].transform("sum")
    monthly["share"] = monthly["payment_amount"] / monthly["month_total"]

    area = (
        alt.Chart(monthly)
        .mark_area(opacity=0.85)
        .encode(
            x=alt.X("purchase_month:T", title=None),
            y=alt.Y("share:Q", title="金额占比", stack="normalize", axis=alt.Axis(format="%")),
            color=alt.Color("payment_type:N", title="支付方式",
                            scale=alt.Scale(scheme="tableau10")),
            tooltip=[alt.Tooltip("purchase_month:T", title="月份", format="%Y-%m"),
                     alt.Tooltip("payment_type:N", title="支付方式"),
                     alt.Tooltip("payment_amount:Q", title="支付金额", format=",.2f"),
                     alt.Tooltip("share:Q", title="占比", format=".2%")],
        )
        .properties(height=320, title="各渠道金额占比月度变化（归一化堆叠）")
    )
    st.altair_chart(area, width="stretch")
    st.caption(
        "按**金额**份额归一化堆叠。用金额而不是订单数，正是因为订单数跨渠道不可加。"
    )
