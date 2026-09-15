"""
ADS 层构建 + 自动对账

用法：
    python scripts/ads_build.py

幂等性：每个 SQL 都是 DROP + CREATE + INSERT 全量重建，
        同样的输入必得同样的输出，重复执行结果完全一致。
        （若要做按购买日增量，需 DWD/DWS/ADS 三层一起改造成参数化删除+插入，
          只改 ADS 层没有意义——DWD 一全量重跑，增量基础就被冲掉了。）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sql_runner import (                      # noqa: E402
    check_consistency, drop_all_views, fetch_all, get_engine, report_rows,
    run_sql_file, scalar,
)

# ============ 按「能否按日增量」分组 —— 增量加载（P0-1）的核心判断 ============
# 判断法则：**这个表的结果是否只依赖本区间的数据？**
#   ✅ 只依赖本区间        → 可增量（日表 / 月表）
#   ❌ 依赖全部历史        → 必须全量重建（cohort 口径）
#   ⚠️ 依赖全量但成本极低  → 全量更简单（表只有几千行）

ADS_INCREMENTAL = [
    "sql/03_ads_sale_overview.sql",   # 日粒度，只依赖当天
    "sql/03_ads_top_product.sql",     # 窗口按 purchase_date 分区，每天名次只依赖当天
    "sql/03_ads_fulfillment.sql",     # 月粒度，只依赖该月
    # 订单/日粒度：一个订单只属一个购买日，结果只依赖本订单自身的明细额与支付额
    "sql/05_ads_payment_reconcile.sql",
]

ADS_FULL_ONLY = [
    "sql/03_ads_top_seller.sql",      # 全周期累计；仅 3,053 行，全量重建更简单
    "sql/03_ads_user_retention.sql",  # cohort 口径：分母是「全体买家的首购日」
    "sql/03_ads_user_repeat.sql",     # cohort 口径：全周期 / 按首购月分组
]

# 视图必须最后跑（依赖上面所有表）
VIEWS_FILE = "sql/04_views.sql"

# 全量模式 = 可增量组 + 必须全量组 + 视图
ADS_FILES = ADS_INCREMENTAL + ADS_FULL_ONLY + [VIEWS_FILE]

# 对账项：同一口径用不同算法算出的值必须相等
CONSISTENCY_CHECKS = [
    (
        "GMV 三处一致（ADS 总览 / DWD 订单层 / DWD 明细层）",
        [
            "SELECT ROUND(SUM(gmv), 2) FROM ads_sale_overview_daily",
            "SELECT ROUND(SUM(gmv), 2) FROM dwd_order WHERE is_valid",
            "SELECT ROUND(SUM(price), 2) FROM dwd_order_detail WHERE is_valid",
        ],
    ),
    (
        "买家口径一致（留存 cohort 规模合计 = 复购买家数）",
        [
            "SELECT SUM(cohort_size) FROM ads_user_retention",
            "SELECT buyer_cnt FROM ads_user_repeat_overall",
        ],
    ),
    (
        "复购买家数一致（overall = monthly 合计）",
        [
            "SELECT repeat_buyer_cnt FROM ads_user_repeat_overall",
            "SELECT SUM(repeat_buyer_cnt) FROM ads_user_repeat_monthly",
        ],
    ),
    (
        "履约漏斗覆盖全量订单（月度合计 = dwd_order 总行数）",
        [
            "SELECT SUM(order_cnt) FROM ads_fulfillment_monthly",
            "SELECT COUNT(*) FROM dwd_order",
        ],
    ),
    (
        "卖家维度 GMV = 明细层 GMV",
        [
            "SELECT ROUND(SUM(gmv), 2) FROM ads_top_seller",
            "SELECT ROUND(SUM(price), 2) FROM dwd_order_detail WHERE is_valid",
        ],
    ),
    (
        "月度 GMV 合计 = 日度 GMV 合计",
        [
            "SELECT ROUND(SUM(gmv), 2) FROM ads_sale_overview_daily",
            "SELECT ROUND(SUM(gmv), 2) FROM dws_sale_daily",
        ],
    ),
    (
        "明细行数一致（日表 item_cnt 合计 = 明细层有效行数）",
        [
            "SELECT SUM(item_cnt) FROM ads_sale_overview_daily",
            "SELECT COUNT(*) FROM dwd_order_detail WHERE is_valid",
        ],
    ),
    # ---------- 支付对账（P1-1）----------
    # 这几条是「跨层兜底」：对账表一旦静默丢行，总额立刻对不上。
    # 注意这里比的是**全量**口径（不筛 is_valid），因为对账的对象是全部 99,441 单。
    #
    # ⚠️ 「对账表支付额 = 支付表合计」那条**不在**这里，它在 COMPLETENESS_CHECKS。
    #    原因：它的另一端是**全量 ODS**，而数仓是增量累积的 —— 回放演示中途
    #    拿只加载了几天的对账表去比两年的 ODS，必然误报。下面这条比的是
    #    dwd_order_detail（DWD 层，和 ADS 同步增量），两端范围始终一致，才留在这里。
    (
        "对账表覆盖订单层全部行数（行数 = dwd_order 总行数）",
        [
            "SELECT COUNT(*) FROM ads_payment_reconcile",
            "SELECT COUNT(*) FROM dwd_order",
        ],
    ),
    (
        "对账表明细额 = 明细层合计（跨层兜底，证明没漏行）",
        [
            "SELECT ROUND(SUM(items_amount), 2) FROM ads_payment_reconcile",
            "SELECT ROUND(SUM(price + COALESCE(freight_value, 0)), 2) FROM dwd_order_detail",
        ],
    ),
    (
        "对账表明细额 = 订单层全量明细额（口径自洽，锁住「全量 vs is_valid」）",
        [
            "SELECT ROUND(SUM(items_amount), 2) FROM ads_payment_reconcile",
            "SELECT ROUND(SUM(gmv + freight_total), 2) FROM dwd_order",
        ],
    ),
    (
        "对账四类之和 = 对账总行数（分类无遗漏、无重复）",
        [
            "SELECT SUM(c) FROM ("
            "  SELECT COUNT(*) AS c FROM ads_payment_reconcile GROUP BY diff_type"
            ") x",
            "SELECT COUNT(*) FROM ads_payment_reconcile",
        ],
    ),
    (
        "对账日汇总 = 对账明细（订单数）",
        [
            "SELECT SUM(order_cnt) FROM ads_payment_reconcile_daily",
            "SELECT COUNT(*) FROM ads_payment_reconcile",
        ],
    ),
    (
        "对账日汇总净额 = 对账明细净额",
        [
            "SELECT ROUND(SUM(net_diff), 2) FROM ads_payment_reconcile_daily",
            "SELECT ROUND(SUM(diff_amount), 2) FROM ads_payment_reconcile",
        ],
    ),
]

# ---------------------------------------------------------------------------
# 需要「数仓已完整加载」才成立的对账项 —— 拿数仓去和**全量 ODS** 做兜底比对。
#
# 为什么单独分出来：数仓是**增量累积**的。数据回放演示从零开始一天天攒，
# 中途仓库是**故意不完整**的，这时拿它对全量 ODS 比总额只会误报
# （对账表只有几天的数据，ODS 却是两年）。
#
# 所以只在「dwd_order 行数 = ODS 订单行数」时才启用这条 ——
# 加载完整后它会自动恢复，不需要人工开关。
# ---------------------------------------------------------------------------
COMPLETENESS_CHECKS = [
    (
        "对账表支付额 = 支付表全量合计（跨层兜底，证明没漏行）",
        [
            "SELECT ROUND(SUM(payments_amount), 2) FROM ads_payment_reconcile",
            "SELECT ROUND(SUM(payment_value), 2) FROM olist_order_payments_dataset",
        ],
    ),
]


def warehouse_is_complete(engine) -> bool:
    """数仓是否已加载全部订单（数据回放演示中途会是 False）"""
    return scalar(engine, "SELECT COUNT(*) FROM dwd_order") == \
        scalar(engine, "SELECT COUNT(*) FROM olist_orders_dataset")


def _n(value, default=0):
    """
    聚合结果兜底成 0。

    ⚠️ 数仓只加载了**部分历史**时（数据回放演示从零累积），
    `SUM()` / `ROUND(...)` / `SUM(...) FILTER (...)` 在没有匹配行时返回 **NULL**，
    直接拿去格式化会抛：

        TypeError: unsupported format string passed to NoneType.__format__

    这个坑在"全量数据"下**永远看不到**，只有从零累积的初始阶段才暴露 ——
    本项目实测就是这样：跑了一整年全量都没事，做回放演示第一天就炸。
    """
    return default if value is None else value


def print_snapshot(engine) -> None:
    """
    打印关键指标快照，供人工核对合理性。

    全程对「空表 / NULL」兜底（见 `_n()` 的说明）—— 因为数仓在增量累积的
    中途本来就可能只有几天数据，这个函数不该因此崩掉。
    """
    print("\n【指标快照】")
    rows = fetch_all(engine, """
        SELECT
            MIN(purchase_date), MAX(purchase_date), COUNT(*),
            COALESCE(ROUND(SUM(gmv), 2), 0),
            COALESCE(SUM(order_cnt), 0),
            COALESCE(SUM(item_cnt), 0),
            COALESCE(ROUND(SUM(gmv) / NULLIF(SUM(order_cnt), 0), 2), 0)
        FROM ads_sale_overview_daily
    """)
    first, last, days, gmv, orders, items, aov = rows[0]
    # 买家总数不能从日表 SUM(buyer_cnt) 拿：跨天复购的买家每天各被计一次，
    # 实测 SUM 得 97272，而真实去重买家是 94986（2064 个买家在多天购买）。
    # 去重买家总数只认 ads_user_repeat_overall（全周期按 customer_unique_id 去重）。
    buyers = _n(scalar(engine, "SELECT buyer_cnt FROM ads_user_repeat_overall"))
    day_buyers = _n(scalar(engine, "SELECT SUM(buyer_cnt) FROM ads_sale_overview_daily"))
    # 空库时 MIN/MAX 都是 NULL，直接打印会得到「None ~ None」，看着像坏了
    span = "（尚无数据）" if first is None else f"{first} ~ {last}，共 {_n(days)} 天"
    print(f"  销售总览：{span}")
    print(f"    GMV = {gmv:,.2f}   有效订单 = {orders:,}   明细行 = {items:,}   客单价 = {aov}")
    print(f"    去重买家 = {buyers:,}（注意：日表 SUM(buyer_cnt) = {day_buyers:,}"
          f"，含跨天复购重复计数，不可当买家总数用）")

    rep_rows = fetch_all(engine, """
        SELECT buyer_cnt, repeat_buyer_cnt, repeat_rate, orders_per_buyer
        FROM ads_user_repeat_overall
    """)
    if rep_rows:
        rep = [_n(v) for v in rep_rows[0]]
        print(f"  用户复购：买家 {rep[0]:,}，复购 {rep[1]:,}，复购率 {rep[2]}，人均 {rep[3]} 单")
    else:
        print("  用户复购：（ads_user_repeat_overall 为空 —— 数仓只加载了部分历史时属正常）")

    print("  留存率（已剔除右删失 cohort 与 cohort_size < 20 的小样本）：")
    for window, cohort_n, rate in fetch_all(engine, """
        SELECT window_days,
               COUNT(DISTINCT cohort_date),
               ROUND(SUM(retained_cnt)::NUMERIC / NULLIF(SUM(cohort_size), 0), 4)
        FROM v_retention_curve
        WHERE is_complete AND cohort_size >= 20
        GROUP BY window_days
        ORDER BY window_days
    """):
        shown = "  n/a" if rate is None else f"{rate:.4f}"
        print(f"    {window:>3} 日：{shown}  （{cohort_n} 个满窗 cohort）")

    # 平均履约时长必须按签收单量加权：直接 AVG(月度均值) 是「均值的均值」，
    # 实测会得到 14.31，而真实值是 12.50（小月份被赋予了和 1 月同样的权重）。
    fun_rows = fetch_all(engine, """
        SELECT COALESCE(SUM(order_cnt), 0), COALESCE(SUM(approved_cnt), 0),
               COALESCE(SUM(shipped_cnt), 0), COALESCE(SUM(delivered_cnt), 0),
               ROUND(SUM(avg_deliver_days * delivered_cnt) / NULLIF(SUM(delivered_cnt), 0), 2)
        FROM ads_fulfillment_monthly
    """)
    if fun_rows:
        fun = [_n(v) for v in fun_rows[0]]
        print(f"  履约漏斗（全量）：下单 {fun[0]:,} → 审批 {fun[1]:,} → 交承运 {fun[2]:,}"
              f" → 签收 {fun[3]:,}，平均签收 {fun[4]} 天（按签收单量加权）")
    else:
        print("  履约漏斗：（ads_fulfillment_monthly 为空）")

    cats = _n(scalar(engine, "SELECT COUNT(DISTINCT category_name) FROM v_sale_daily_category"))
    sellers = _n(scalar(engine, "SELECT COUNT(*) FROM ads_top_seller"))
    states = _n(scalar(engine, "SELECT COUNT(DISTINCT seller_state) FROM v_sale_daily_seller_state"))
    print(f"  维度：类目 {cats} 个（有有效订单的）卖家 {sellers:,} 个，卖家州 {states} 个")

    unknown = scalar(engine, """
        SELECT ROUND(SUM(gmv) * 100.0 / (SELECT SUM(gmv) FROM dws_sale_daily), 2)
        FROM dws_sale_daily WHERE category_name = 'unknown'
    """)
    print(f"  数据质量：类目 'unknown' 的 GMV 占比 {_n(unknown)}%")

    # 支付对账（P1-1）：净差 16.5 万里 98.4% 是「有支付、无明细」的口径问题，
    # 不是金额错误 —— 那 767 笔是 unavailable/canceled，本就不该有明细行。
    # 剔除后真·金额差异只剩 303 笔 / 2,871.06。看板据此做口径切换。
    rec_rows = fetch_all(engine, """
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE diff_type = '一致'),
               COUNT(*) FILTER (WHERE diff_type = '金额不符'),
               COUNT(*) FILTER (WHERE diff_type = '仅支付无明细'),
               COUNT(*) FILTER (WHERE diff_type = '仅明细无支付'),
               ROUND(SUM(diff_amount), 2),
               ROUND(SUM(diff_amount) FILTER (WHERE diff_type = '金额不符'), 2),
               COUNT(*) FILTER (WHERE NOT is_comparable),
               COUNT(*) FILTER (WHERE is_comparable),
               COUNT(*) FILTER (WHERE is_comparable AND diff_type = '一致'),
               ROUND(SUM(diff_amount) FILTER (WHERE is_comparable), 2)
        FROM ads_payment_reconcile
    """)
    rec = [_n(v) for v in rec_rows[0]] if rec_rows else [0] * 11

    total, matched = rec[0], rec[1]
    diff_n = rec[2] + rec[3] + rec[4]
    net_diff, amt_diff = rec[5], rec[6]
    cmp_total, cmp_matched, cmp_net = rec[8], rec[9], rec[10]
    # 分母为 0 时不求百分比（数仓刚清空、还没有任何对账行）
    pct_of_gmv = (float(amt_diff) * 100.0 / float(gmv)) if gmv else 0.0
    rate_all = (matched * 100.0 / total) if total else 0.0
    rate_cmp = (cmp_matched * 100.0 / cmp_total) if cmp_total else 0.0

    print(f"  支付对账：参与 {total:,} 单，一致 {matched:,}，差异 {diff_n:,} 笔"
          f"（金额不符 {rec[2]} / 仅支付无明细 {rec[3]} / 仅明细无支付 {rec[4]}）")
    print(f"    净差 {net_diff:,.2f}，其中真·金额差异 {amt_diff:,.2f}"
          f"（占有效 GMV {pct_of_gmv:.4f}%）")
    print(f"    口径切换：全量 {total:,} 单 · 一致率 {rate_all:.2f}%"
          f"  →  剔除 unavailable/canceled {rec[7]:,} 单后 {cmp_total:,} 单 · "
          f"一致率 {rate_cmp:.2f}% · 净差 {cmp_net:,.2f}")


def report_reconciliation(engine) -> int:
    """
    一致性对账 + 指标快照，返回退出码。

    抽成独立函数的原因：**全量重建和增量装载两条路径都要跑对账** ——
    否则增量模式就没有任何校验了。

    「对全量 ODS 兜底」那类对账项只在数仓已完整加载时才跑，
    见 COMPLETENESS_CHECKS 的说明 —— 数据回放演示中途仓库是故意不完整的。
    """
    print("\n【一致性对账】")
    checks = list(CONSISTENCY_CHECKS)
    if warehouse_is_complete(engine):
        checks += COMPLETENESS_CHECKS
    else:
        print(f"  [SKIP] 数仓尚未加载全部历史，跳过 {len(COMPLETENESS_CHECKS)} 条"
              f"「对全量 ODS 兜底」的对账项")
        print("         （数据回放演示中途属预期；加载完整后会自动恢复）")

    all_ok = all(
        check_consistency(engine, label, sqls)
        for label, sqls in checks
    )

    print_snapshot(engine)

    print("\n" + "=" * 68)
    print("对账结果：" + ("全部通过 [OK]" if all_ok else "存在不一致 [FAIL] 请检查上面标 [FAIL] 的项"))
    print("=" * 68)
    return 0 if all_ok else 1


def _rebuild_files(files, title: str) -> int:
    """公共流程：清理视图 → 依次执行 SQL → 对账"""
    engine = get_engine()

    print("=" * 68)
    print(title)
    print("=" * 68)

    # 先删视图：SQL 文件里的 DROP TABLE 已带 CASCADE，这里再显式做一次是为了
    # ① 构建日志可见 ② 让 04_views.sql 的 CREATE OR REPLACE VIEW 不受列定义变更限制
    removed = drop_all_views(engine)
    print(f"  [OK] 清理旧视图 {len(removed)} 个" + (f": {', '.join(removed)}" if removed else "（首次构建）"))

    for rel_path in files:
        objects = run_sql_file(engine, rel_path)
        report_rows(engine, objects)

    return report_reconciliation(engine)


def rebuild() -> int:
    """全量重建全部 ADS 对象（9 张表 + 5 个视图）并做对账"""
    return _rebuild_files(ADS_FILES, "ADS 层构建（全量）")


def rebuild_full_only() -> int:
    """
    只重建「必须全量」的那批 + 视图 + 对账 —— 供增量流程（run_all.py --date）调用。

    增量模式下，可增量的三张表已经由 sql/load/03_ads_inc.sql 按区间重算过了，
    这里只补三件事：
      - ads_user_retention / ads_user_repeat_*  cohort 口径，依赖全体历史，不能增量
      - ads_top_seller                          全周期累计，且只有 3,053 行
      - 5 个视图                                定义未变，但前面 drop_all_views 把它们删了，要重建
    """
    return _rebuild_files(ADS_FULL_ONLY + [VIEWS_FILE],
                          "ADS 层构建（增量模式：必须全量的部分）")


if __name__ == "__main__":
    sys.exit(rebuild())
