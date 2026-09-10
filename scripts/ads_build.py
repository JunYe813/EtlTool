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

# 顺序即依赖顺序：视图依赖表，表之间互相独立
ADS_FILES = [
    "sql/03_ads_sale_overview.sql",
    "sql/03_ads_top_product.sql",
    "sql/03_ads_top_seller.sql",
    "sql/03_ads_user_retention.sql",
    "sql/03_ads_user_repeat.sql",
    "sql/03_ads_fulfillment.sql",
    "sql/04_views.sql",
]

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
]


def print_snapshot(engine) -> None:
    """打印关键指标快照，供人工核对合理性"""
    print("\n【指标快照】")
    rows = fetch_all(engine, """
        SELECT
            MIN(purchase_date), MAX(purchase_date), COUNT(*),
            ROUND(SUM(gmv), 2), SUM(order_cnt), SUM(item_cnt),
            ROUND(SUM(gmv) / NULLIF(SUM(order_cnt), 0), 2)
        FROM ads_sale_overview_daily
    """)
    first, last, days, gmv, orders, items, aov = rows[0]
    # 买家总数不能从日表 SUM(buyer_cnt) 拿：跨天复购的买家每天各被计一次，
    # 实测 SUM 得 97272，而真实去重买家是 94986（2064 个买家在多天购买）。
    # 去重买家总数只认 ads_user_repeat_overall（全周期按 customer_unique_id 去重）。
    buyers = scalar(engine, "SELECT buyer_cnt FROM ads_user_repeat_overall")
    print(f"  销售总览：{first} ~ {last}，共 {days} 天")
    print(f"    GMV = {gmv:,.2f}   有效订单 = {orders:,}   明细行 = {items:,}   客单价 = {aov}")
    print(f"    去重买家 = {buyers:,}（注意：日表 SUM(buyer_cnt) = "
          f"{scalar(engine, 'SELECT SUM(buyer_cnt) FROM ads_sale_overview_daily'):,}"
          f"，含跨天复购重复计数，不可当买家总数用）")

    rep = fetch_all(engine, """
        SELECT buyer_cnt, repeat_buyer_cnt, repeat_rate, orders_per_buyer
        FROM ads_user_repeat_overall
    """)[0]
    print(f"  用户复购：买家 {rep[0]:,}，复购 {rep[1]:,}，复购率 {rep[2]}，人均 {rep[3]} 单")

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
    fun = fetch_all(engine, """
        SELECT SUM(order_cnt), SUM(approved_cnt), SUM(shipped_cnt), SUM(delivered_cnt),
               ROUND(SUM(avg_deliver_days * delivered_cnt) / NULLIF(SUM(delivered_cnt), 0), 2)
        FROM ads_fulfillment_monthly
    """)[0]
    print(f"  履约漏斗（全量）：下单 {fun[0]:,} → 审批 {fun[1]:,} → 交承运 {fun[2]:,} → 签收 {fun[3]:,}"
          f"，平均签收 {fun[4]} 天（按签收单量加权）")

    cats = scalar(engine, "SELECT COUNT(DISTINCT category_name) FROM v_sale_daily_category")
    sellers = scalar(engine, "SELECT COUNT(*) FROM ads_top_seller")
    states = scalar(engine, "SELECT COUNT(DISTINCT seller_state) FROM v_sale_daily_seller_state")
    print(f"  维度：类目 {cats} 个（有有效订单的）卖家 {sellers:,} 个，卖家州 {states} 个")

    unknown = scalar(engine, """
        SELECT ROUND(SUM(gmv) * 100.0 / (SELECT SUM(gmv) FROM dws_sale_daily), 2)
        FROM dws_sale_daily WHERE category_name = 'unknown'
    """)
    print(f"  数据质量：类目 'unknown' 的 GMV 占比 {unknown}%")


def rebuild() -> int:
    """重建全部 ADS 对象并做对账，返回退出码"""
    engine = get_engine()

    print("=" * 68)
    print("ADS 层构建")
    print("=" * 68)

    # 先删视图：SQL 文件里的 DROP TABLE 已带 CASCADE，这里再显式做一次是为了
    # ① 构建日志可见 ② 让 04_views.sql 的 CREATE OR REPLACE VIEW 不受列定义变更限制
    removed = drop_all_views(engine)
    print(f"  [OK] 清理旧视图 {len(removed)} 个" + (f": {', '.join(removed)}" if removed else "（首次构建）"))

    for rel_path in ADS_FILES:
        objects = run_sql_file(engine, rel_path)
        report_rows(engine, objects)

    print("\n【一致性对账】")
    all_ok = all(
        check_consistency(engine, label, sqls)
        for label, sqls in CONSISTENCY_CHECKS
    )

    print_snapshot(engine)

    print("\n" + "=" * 68)
    print("对账结果：" + ("全部通过 [OK]" if all_ok else "存在不一致 [FAIL] 请检查上面标 [FAIL] 的项"))
    print("=" * 68)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(rebuild())
