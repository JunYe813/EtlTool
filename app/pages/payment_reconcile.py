"""看板页面 6 · 支付对账"""
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
st.header("支付对账")
st.caption("数据源：`ads_payment_reconcile`（订单粒度）+ `ads_payment_reconcile_daily`（日汇总）")

st.info(
    "**这一页最想说明的事：口径对齐优先于差异追查。**\n\n"
    "支付表和明细表对账，初步看有 **1,079 笔**单边或差异、净差 **R$165,318.88**。"
    "但先做归因就会发现：净差的 **98.4% 来自 775 笔「有支付、无明细」**订单，"
    "而其中 **767 笔是 `unavailable`/`canceled` 状态 —— 这些订单本来就不该有明细行**。\n\n"
    "所以这不是脏数据，是**对账口径没对齐**。剔除这批订单后，真正的金额差异只剩 **303 笔**。"
    "下面那个口径开关就是用来演示这件事的。"
)

# ---------------- 口径开关 ----------------
scope = st.radio(
    "对账口径",
    ["全量对账（全部 99,441 单）", "可比对口径（剔除 unavailable / canceled）"],
    horizontal=True,
    help="只是统计口径的切换，数据一行都不会被删 —— 对账表的立场是"
         "「把所有对不上的都记下来并归因」，而不是「把不好看的删掉」",
)
comparable_only = scope.startswith("可比对")
scope_where = " AND is_comparable " if comparable_only else ""

# ---------------- 核心指标 ----------------
kpi = query(
    f"""
    SELECT COUNT(*)                                                    AS order_cnt,
           COUNT(*) FILTER (WHERE diff_type = '一致')                    AS matched_cnt,
           COUNT(*) FILTER (WHERE diff_type = '金额不符')                 AS amount_diff_cnt,
           COUNT(*) FILTER (WHERE diff_type = '仅支付无明细')             AS pay_only_cnt,
           COUNT(*) FILTER (WHERE diff_type = '仅明细无支付')             AS items_only_cnt,
           COALESCE(ROUND(SUM(diff_amount), 2), 0)                       AS net_diff,
           COALESCE(ROUND(SUM(diff_amount)
                          FILTER (WHERE diff_type = '金额不符'), 2), 0)   AS amount_diff_sum,
           COALESCE(ROUND(SUM(ABS(diff_amount))
                          FILTER (WHERE diff_type = '金额不符'), 2), 0)   AS amount_diff_abs
    FROM ads_payment_reconcile
    WHERE purchase_date BETWEEN :d1 AND :d2 {scope_where}
    """,
    date_params(f),
)
stop_if_empty(kpi)
k = kpi.iloc[0]

orders = int(k["order_cnt"])
if orders == 0:
    st.warning("当前筛选条件下没有订单。")
    st.stop()

matched = int(k["matched_cnt"])
diff_n = int(k["amount_diff_cnt"]) + int(k["pay_only_cnt"]) + int(k["items_only_cnt"])

c1, c2, c3, c4 = st.columns(4)
c1.metric("参与对账订单", f"{orders:,}", scope.split("（")[0])
c2.metric("一致率", f"{matched / orders * 100:.2f}%",
          help="一致单数 ÷ 参与对账订单数。注意：一致率必须用分子分母重算，"
               "不能对日度比率取平均（那是「均值的均值」，本项目已实测踩过）")
c3.metric("差异笔数", f"{diff_n:,}",
          f"{diff_n / orders * 100:.2f}%",
          delta_color="off")
c4.metric("真·金额差异净额", f"R$ {float(k['amount_diff_sum']):,.2f}",
          help="只含「金额不符」这一类。有符号相加，正负会抵消 —— "
               f"绝对额是 R$ {float(k['amount_diff_abs']):,.2f}")

c1, c2, c3, c4 = st.columns(4)
c1.metric("总净差（含结构性单边）", f"R$ {float(k['net_diff']):,.2f}",
          help="三类差异的有符号合计。这是「看总额」视角，"
               "它把「结构性问题」和「金额错误」混在了一起")
c2.metric("金额不符", f"{int(k['amount_diff_cnt']):,} 笔",
          help="两边都有记录但差额 > 0.01 —— 唯一的真·金额差异")
c3.metric("仅支付无明细", f"{int(k['pay_only_cnt']):,} 笔",
          help="支付表有、明细表无。绝大多数是取消/不可用订单，本就不该有明细行")
c4.metric("仅明细无支付", f"{int(k['items_only_cnt']):,} 笔",
          help="反向单边，最该逐单追的一类")

# ---------------- 差异构成 ----------------
st.divider()
st.subheader("差异构成：结构性单边 vs 金额差异")

comp = pd.DataFrame({
    "类别": ["仅支付无明细", "仅明细无支付", "金额不符"],
    "笔数": [int(k["pay_only_cnt"]), int(k["items_only_cnt"]), int(k["amount_diff_cnt"])],
})

# 三类净额分别取（金额不符用有符号净额，与前两类口径一致）
comp_amt = query(
    f"""
    SELECT diff_type,
           COALESCE(ROUND(SUM(diff_amount), 2), 0) AS net_diff,
           COALESCE(ROUND(SUM(ABS(diff_amount)), 2), 0) AS abs_diff
    FROM ads_payment_reconcile
    WHERE purchase_date BETWEEN :d1 AND :d2 {scope_where}
    GROUP BY diff_type
    """,
    date_params(f),
)
comp = comp.merge(
    comp_amt.rename(columns={"diff_type": "类别"}), on="类别", how="left"
).fillna(0)
comp["占比"] = comp["笔数"] / max(diff_n, 1)

chart = (
    alt.Chart(comp)
    .mark_bar(size=40, color=ACCENT)
    .encode(
        y=alt.Y("类别:N", sort="-x", title=None),
        x=alt.X("笔数:Q", title="笔数"),
        tooltip=[alt.Tooltip("类别:N", title="类别"),
                 alt.Tooltip("笔数:Q", title="笔数", format=","),
                 alt.Tooltip("net_diff:Q", title="净额", format=",.2f"),
                 alt.Tooltip("abs_diff:Q", title="绝对额", format=",.2f")],
    )
    .properties(height=220, title="差异笔数构成")
)
text = chart.mark_text(align="left", dx=6, fontSize=13).encode(
    text=alt.Text("笔数:Q", format=","))
st.altair_chart(chart + text, width="stretch")

st.dataframe(
    comp.rename(columns={"net_diff": "净额", "abs_diff": "绝对额"})[
        ["类别", "笔数", "占比", "净额", "绝对额"]
    ],
    width="stretch", hide_index=True,
    column_config={
        "占比": st.column_config.NumberColumn("占比", format="%.2f%%"),
        "净额": st.column_config.NumberColumn("净额", format="R$ %.2f"),
        "绝对额": st.column_config.NumberColumn("绝对额", format="R$ %.2f"),
    },
)

struct_amt = float(comp.loc[comp["类别"] != "金额不符", "abs_diff"].sum())
amt_amt = float(comp.loc[comp["类别"] == "金额不符", "abs_diff"].sum())
if struct_amt + amt_amt > 0:
    st.caption(
        f"**结构性单边 R$ {struct_amt:,.2f}** vs **真·金额差异 R$ {amt_amt:,.2f}** —— "
        f"前者是后者的 {struct_amt / max(amt_amt, 0.01):,.0f} 倍。"
        "如果只报一个「净差 16.5 万」，就等于把「订单本来没有明细」"
        "和「金额算错了」混成一个数字。"
    )

# ---------------- 日度趋势 ----------------
st.divider()
st.subheader("对账差异日度趋势")

daily = query(
    """
    SELECT purchase_date, order_cnt, matched_cnt, amount_diff_cnt,
           amount_diff_sum, net_diff
    FROM ads_payment_reconcile_daily
    WHERE purchase_date BETWEEN :d1 AND :d2
    ORDER BY purchase_date
    """,
    date_params(f),
)

if daily.empty:
    st.info("当前筛选区间内没有日度数据。")
else:
    melt = daily.melt(
        id_vars=["purchase_date", "order_cnt"],
        value_vars=["net_diff", "amount_diff_sum"],
        var_name="口径", value_name="金额",
    ).replace({"net_diff": "总净差（含结构性单边）",
               "amount_diff_sum": "真·金额差异"})

    trend = (
        alt.Chart(melt)
        .mark_line(point=False, strokeWidth=2)
        .encode(
            x=alt.X("purchase_date:T", title=None),
            y=alt.Y("金额:Q", title="净额"),
            color=alt.Color("口径:N", title="口径",
                            scale=alt.Scale(range=[WARN, ACCENT])),
            tooltip=[alt.Tooltip("purchase_date:T", title="日期", format="%Y-%m-%d"),
                     alt.Tooltip("口径:N", title="口径"),
                     alt.Tooltip("金额:Q", title="净额", format=",.2f"),
                     alt.Tooltip("order_cnt:Q", title="当日订单", format=",")],
        )
        .properties(height=320, title="日度净额：两条线的量级差就是「口径问题 vs 金额错误」")
    )
    st.altair_chart(trend, width="stretch")
    st.caption(
        "趋势图读的是**预聚合日表** `ads_payment_reconcile_daily`，固定为**全量口径**"
        "（不受上面的口径开关影响）—— 换成可比对口径两条线的形状基本不变，"
        "差异都集中在少数几天。"
    )

# ---------------- 差异明细 ----------------
st.divider()
st.subheader("差异明细")

kinds = query(
    """
    SELECT diff_type, COUNT(*) AS cnt
    FROM ads_payment_reconcile
    WHERE purchase_date BETWEEN :d1 AND :d2 AND diff_type <> '一致'
    GROUP BY 1 ORDER BY 2 DESC
    """,
    date_params(f),
)
all_kinds = kinds["diff_type"].tolist() if not kinds.empty else []

picked = st.multiselect("差异类型", all_kinds, default=[],
                        placeholder="全部类型",
                        help="留空表示全部。金额不符 = 真·金额差异；"
                             "仅支付无明细 = 结构性单边（多为取消/不可用订单）")

kw, kp = "", date_params(f)
if picked:
    kw = " AND diff_type = ANY(:kinds) "
    kp = kp + (("kinds", tuple(picked)),)

detail = query(
    f"""
    SELECT order_id, order_status, purchase_date, items_amount, payments_amount,
           diff_amount, diff_type
    FROM ads_payment_reconcile
    WHERE purchase_date BETWEEN :d1 AND :d2
      AND diff_type <> '一致' {kw}
    ORDER BY ABS(diff_amount) DESC
    LIMIT 500
    """,
    kp,
)

if detail.empty:
    st.info("当前筛选条件下没有差异单据。")
else:
    st.caption(f"按差额绝对值倒序，最多显示 500 行（当前 {len(detail):,} 行）。"
               "订单状态是归因的关键：`unavailable` / `canceled` 的订单本就不该有明细行。")
    st.dataframe(
        detail.rename(columns={
            "order_id": "订单号", "order_status": "订单状态",
            "purchase_date": "购买日期", "items_amount": "明细额",
            "payments_amount": "支付额", "diff_amount": "差额", "diff_type": "差异类型",
        }),
        width="stretch", hide_index=True,
        column_config={
            "明细额": st.column_config.NumberColumn("明细额", format="R$ %.2f"),
            "支付额": st.column_config.NumberColumn("支付额", format="R$ %.2f"),
            "差额": st.column_config.NumberColumn("差额", format="R$ %.2f"),
        },
    )

    # 真正需要逐单追的：状态正常却没有明细行
    suspect = query(
        """
        SELECT order_id, order_status, purchase_date, payments_amount, diff_type
        FROM ads_payment_reconcile
        WHERE purchase_date BETWEEN :d1 AND :d2
          AND is_comparable
          AND diff_type <> '一致'
        ORDER BY diff_amount DESC
        """,
        date_params(f),
    )
    st.markdown(
        f"**真正需要逐单追的：{len(suspect):,} 笔。** "
        "剔除取消/不可用之后，剩下的都是订单状态看起来正常、却对不上的单据 ——"
        "不归因的话面对 1,079 笔无从下手，归因完只剩这些。"
    )
    if not suspect.empty:
        st.dataframe(
            suspect.rename(columns={
                "order_id": "订单号", "order_status": "订单状态",
                "purchase_date": "购买日期", "payments_amount": "支付额",
                "diff_type": "差异类型",
            }),
            width="stretch", hide_index=True,
            column_config={
                "支付额": st.column_config.NumberColumn("支付额", format="R$ %.2f"),
            },
        )
