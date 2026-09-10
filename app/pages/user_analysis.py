"""看板页面 4 · 用户分析"""
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import altair as alt                          # noqa: E402
import pandas as pd                           # noqa: E402
import streamlit as st                        # noqa: E402

from db import ACCENT, WARN, filters, query, stop_if_empty   # noqa: E402

f = filters()
st.header("用户分析")
st.caption(
    "数据源：`ads_user_retention` / `v_retention_curve` / `ads_user_repeat_overall` / "
    "`ads_user_repeat_monthly`"
)

# ---------- 全周期复购（单值）----------
rep = query("""
    SELECT buyer_cnt, repeat_buyer_cnt, repeat_rate, orders_per_buyer
    FROM ads_user_repeat_overall
""")
stop_if_empty(rep)
row = rep.iloc[0]

st.subheader("复购概览（全周期）")
c1, c2, c3, c4 = st.columns(4)
c1.metric("购买买家数", f"{int(row['buyer_cnt']):,}", help="按 customer_unique_id 去重")
c2.metric("复购买家数", f"{int(row['repeat_buyer_cnt']):,}", help="有效订单数 ≥ 2 的买家")
c3.metric("复购率", f"{float(row['repeat_rate']) * 100:.2f}%")
c4.metric("人均订单数", f"{float(row['orders_per_buyer']):.2f}")
st.caption(
    "Olist 是典型低频电商：全周期复购率仅约 3%，增长主要靠拉新而非复购。"
    "该表为全周期口径，不随日期筛选变化。"
)

st.divider()

# ---------- 留存曲线 ----------
st.subheader("Cohort 留存曲线")
st.caption(
    "留存定义：首购后**第 1~N 日内**再次购买（不含首购当天）的买家 ÷ 该 cohort 规模。"
    "已自动剔除「观察窗未满」的 cohort（右删失）与规模过小的 cohort（噪声）。"
)

curve = query(
    """
    SELECT cohort_date, window_days, cohort_size, retained_cnt, retention_rate, observable_days
    FROM v_retention_curve
    WHERE cohort_date BETWEEN :d1 AND :d2
      AND is_complete
      AND cohort_size >= :min_cohort
    ORDER BY cohort_date, window_days
    """,
    (("d1", f["d1"]), ("d2", f["d2"]), ("min_cohort", f["min_cohort"])),
)

if curve.empty:
    st.warning(
        "当前筛选条件下没有满足条件的 cohort。"
        "请放宽日期范围，或调低侧边栏的「留存最小 cohort 规模」。"
    )
else:
    summary = (curve.groupby("window_days", as_index=False)
                    .agg(cohorts=("cohort_date", "nunique"),
                         buyers=("cohort_size", "sum"),
                         retained=("retained_cnt", "sum"))
                    .assign(retention_rate=lambda d: (d["retained"] / d["buyers"]).round(4))
                    .sort_values("window_days"))

    cols = st.columns(len(summary))
    for col, (_, r) in zip(cols, summary.iterrows()):
        col.metric(f"{int(r['window_days'])} 日留存",
                   f"{r['retention_rate'] * 100:.2f}%",
                   help=f"{int(r['cohorts'])} 个满窗 cohort，{int(r['buyers']):,} 名买家")

    line = (
        alt.Chart(curve)
        .mark_line(strokeWidth=2, color=ACCENT)
        .encode(
            x=alt.X("cohort_date:T", title="首购日 (cohort)"),
            y=alt.Y("retention_rate:Q", title="留存率", axis=alt.Axis(format=".1%")),
            color=alt.Color("window_days:N", title="留存窗口",
                            scale=alt.Scale(scheme="tableau10")),
            tooltip=[
                alt.Tooltip("cohort_date:T", title="cohort", format="%Y-%m-%d"),
                alt.Tooltip("window_days:N", title="窗口(天)"),
                alt.Tooltip("cohort_size:Q", title="cohort 规模", format=","),
                alt.Tooltip("retention_rate:Q", title="留存率", format=".2%"),
            ],
        )
        .properties(height=380, title="各 cohort 留存曲线（按留存窗口分色）")
    )
    st.altair_chart(line, width="stretch")

    # 加权汇总曲线更能反映整体水平
    agg = (curve.groupby("window_days", as_index=False)
                .agg(retained=("retained_cnt", "sum"), buyers=("cohort_size", "sum"))
                .assign(retention_rate=lambda d: (d["retained"] / d["buyers"]).round(4))
                .sort_values("window_days"))
    bars = (
        alt.Chart(agg)
        .mark_bar(color="#54A24B", size=60)
        .encode(
            x=alt.X("window_days:O", title="留存窗口（天）"),
            y=alt.Y("retention_rate:Q", title="加权留存率", axis=alt.Axis(format=".1%")),
            tooltip=[alt.Tooltip("window_days:O", title="窗口(天)"),
                     alt.Tooltip("buyers:Q", title="买家数", format=","),
                     alt.Tooltip("retained:Q", title="留存买家数", format=","),
                     alt.Tooltip("retention_rate:Q", title="留存率", format=".2%")],
        )
        .properties(height=300, title="加权留存率（分子分母各自求和后相除）")
    )
    st.altair_chart(bars, width="stretch")
    st.caption(
        "加权留存率 = Σ留存买家数 ÷ Σcohort规模，比「留存率的平均」更准确 —— "
        "小 cohort 的波动不应和大 cohort 等权。"
    )

st.divider()

# ---------- 复购趋势 ----------
st.subheader("复购率趋势（按首购月）")
monthly = query("""
    SELECT first_month, buyer_cnt, repeat_buyer_cnt, repeat_rate
    FROM ads_user_repeat_monthly
    ORDER BY first_month
""")
if not monthly.empty:
    monthly = monthly.copy()
    # 标记右删失：首购太晚的 cohort 没有足够时间复购，复购率必然偏低
    max_purchase = query("SELECT MAX(purchase_at)::date AS max_d FROM dwd_order WHERE is_valid")["max_d"].iloc[0]
    # first_month 已在数据访问层统一转成 datetime64；与标量日期相减得到 timedelta 序列
    monthly["observable_days"] = (pd.Timestamp(max_purchase)
                                  - pd.to_datetime(monthly["first_month"])).dt.days
    monthly["is_complete"] = monthly["observable_days"] >= 90

    chart = (
        alt.Chart(monthly)
        .mark_line(point=True, strokeWidth=2)
        .encode(
            x=alt.X("first_month:T", title="首购月"),
            y=alt.Y("repeat_rate:Q", title="复购率", axis=alt.Axis(format=".1%")),
            tooltip=[
                alt.Tooltip("first_month:T", title="首购月", format="%Y-%m"),
                alt.Tooltip("buyer_cnt:Q", title="新增买家", format=","),
                alt.Tooltip("repeat_buyer_cnt:Q", title="复购买家", format=","),
                alt.Tooltip("repeat_rate:Q", title="复购率", format=".2%"),
                alt.Tooltip("observable_days:Q", title="可观察天数"),
            ],
        )
        .properties(height=320, title="各首购月买家的复购率")
    )
    censored = (
        alt.Chart(monthly[~monthly["is_complete"]])
        .mark_circle(size=90, color=WARN)
        .encode(x="first_month:T", y="repeat_rate:Q")
    )
    st.altair_chart(chart + censored, width="stretch")
    st.caption(
        "红点为**观察窗不足 90 天**的首购月（右删失）—— 这些买家还没机会复购，"
        "复购率偏低属统计假象，不代表业务下滑。"
    )
    st.dataframe(
        monthly.rename(columns={
            "first_month": "首购月", "buyer_cnt": "新增买家",
            "repeat_buyer_cnt": "复购买家", "repeat_rate": "复购率",
            "observable_days": "可观察天数", "is_complete": "窗口完整",
        }),
        width="stretch", hide_index=True,
    )
