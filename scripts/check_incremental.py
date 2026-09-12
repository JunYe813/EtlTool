"""
增量验收：验证「增量结果 ≡ 全量结果」+「增量幂等」

为什么这条验收最有说服力：
    源数据是静态 CSV、不会变。所以增量重算某区间后，业务数据必须与全量重建
    **逐字节一致**。任何不一致都说明增量逻辑有问题。
    这比"看板数字看着对"强得多。

流程：
    ① 快照 DWD / DWS / ADS 全部对象的指纹（复用 check_idempotent.py 的算法）
    ② 跑一次增量（--date + 回看窗口）
    ③ 再快照 → 与 ① 对比，必须全部 MATCH   ← 证明「增量 ≡ 全量」
    ④ 再跑一次**同样**的增量
    ⑤ 第三次快照 → 与 ③ 对比，必须全部 MATCH ← 证明「增量幂等」
    ⑥ 再用**跨多个月份的宽回看窗口**跑一次，仍必须 MATCH
       ← 这一步是回归测试：`ads_fulfillment_monthly` 是月粒度，
          DELETE 区间若写成 `IN (start月, end月)`，跨 ≥3 个月时会漏删中间月，
          撞主键报 duplicate key。默认回看 3 天最多跨 2 个月，所以 ①~⑤ 抓不到它。

用法：
    python scripts/check_incremental.py                      # 自动取区间中位的那一天
    python scripts/check_incremental.py --date 2017-01-05
    python scripts/check_incremental.py --lookback 7         # 自定义回看窗口
"""
import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sql_runner import fetch_all, get_engine, scalar   # noqa: E402

# 与 check_idempotent.py 用同一套指纹算法（剔除审计时间列）
DIGEST_SQL = """
SELECT COUNT(*),
       COALESCE(MD5(STRING_AGG(row_hash, '' ORDER BY row_hash)), '-')
FROM (
    SELECT MD5((TO_JSONB(t) - 'create_date' - 'modified_date')::text) AS row_hash
    FROM "{table}" t
) x
"""

OBJECTS = [
    # DWD
    "dwd_order_detail", "dwd_order",
    # DWS
    "dws_sale_daily", "dws_sale_daily_seller", "dws_sale_daily_product",
    # ADS（表 + 视图，全都要验）
    "ads_sale_overview_daily", "ads_top_product", "ads_top_seller",
    "ads_user_retention", "ads_user_repeat_overall", "ads_user_repeat_monthly",
    "ads_fulfillment_monthly",
    "v_sale_daily_category", "v_sale_daily_seller_state", "v_sale_daily_buyer_state",
    "v_fulfillment_funnel", "v_retention_curve",
]

# 宽回看窗口（天）——必须大到让区间跨 ≥3 个月份，才能覆盖月粒度表的漏删缺陷
WIDE_LOOKBACK = 90

# 增量跑完后必须仍然成立的「三层 GMV 一致」
GMV_CHECKS = [
    ("ads_sale_overview_daily", "SELECT ROUND(SUM(gmv),2) FROM ads_sale_overview_daily"),
    ("dwd_order（订单层）",      "SELECT ROUND(SUM(gmv),2) FROM dwd_order WHERE is_valid"),
    ("dwd_order_detail（明细层）", "SELECT ROUND(SUM(price),2) FROM dwd_order_detail WHERE is_valid"),
    ("dws_sale_daily",          "SELECT ROUND(SUM(gmv),2) FROM dws_sale_daily"),
]


def snapshot(engine) -> dict:
    return {t: fetch_all(engine, DIGEST_SQL.format(table=t))[0] for t in OBJECTS}


def pick_middle_date(engine) -> date:
    """取区间中位的那一天 —— 避免回看窗口撞到数据边界"""
    return scalar(engine, """
        SELECT purchase_date FROM ads_sale_overview_daily
        ORDER BY purchase_date
        OFFSET (SELECT COUNT(*) / 2 FROM ads_sale_overview_daily) LIMIT 1
    """)


def compare(before: dict, after: dict, label: str) -> list:
    """对比两份快照，返回不一致的对象名列表"""
    print(f"\n   对比：{label}")
    mismatch = []
    for t in OBJECTS:
        b, a = before[t], after[t]
        if b == a:
            print(f"   [MATCH]  {t:<28} {a[0]:>8,} 行  {a[1][:16]}")
        else:
            print(f"   [DIFFER] {t:<28} {b[0]:,} -> {a[0]:,} 行  {b[1][:12]} -> {a[1][:12]}")
            mismatch.append(t)
    return mismatch


def check_gmv(engine) -> bool:
    """增量跑完后，三层 GMV 必须仍然一致"""
    print("\n   三层 GMV 一致性：")
    values = {}
    for label, sql in GMV_CHECKS:
        values[label] = scalar(engine, sql)
        print(f"     {label:<26} {values[label]:,}")
    ok = len(set(str(v) for v in values.values())) == 1
    print("     -> " + ("一致 [OK]" if ok else "不一致 [FAIL]"))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="增量验收")
    parser.add_argument("--date", help="增量目标日（YYYY-MM-DD），默认自动取区间中位日")
    parser.add_argument("--lookback", type=int, default=3, help="回看天数（默认 3）")
    args = parser.parse_args()

    from run_all import run_incremental      # 复用实际跑的流程

    engine = get_engine()
    target = date.fromisoformat(args.date) if args.date else pick_middle_date(engine)

    print("=" * 74)
    print("增量验收（增量 ≡ 全量  且  增量幂等）")
    print("=" * 74)
    print(f"目标日：{target}   回看：{args.lookback} 天")

    # ---------- ① 基线快照 ----------
    print("\n① 记录基线指纹 ...")
    base = snapshot(engine)

    # ---------- ② 第一次增量 ----------
    print(f"\n② 第一次增量（区间 {target} 回看 {args.lookback} 天）...")
    t0 = time.perf_counter()
    if run_incremental(target, args.lookback) != 0:
        print("\n[FAIL] 增量执行失败（对账未通过）")
        return 1
    print(f"   耗时 {time.perf_counter() - t0:.2f}s")

    # ---------- ③ 对比：增量 ≡ 增量前 ----------
    print("\n③ 验证「增量结果 ≡ 增量前」")
    first = snapshot(engine)
    mismatch1 = compare(base, first, "增量前 vs 增量后")
    gmv_ok = check_gmv(engine)

    # ---------- ④ 第二次同样的增量 ----------
    print(f"\n④ 第二次增量（同一目标日，验证幂等）...")
    t0 = time.perf_counter()
    if run_incremental(target, args.lookback) != 0:
        print("\n[FAIL] 第二次增量执行失败")
        return 1
    print(f"   耗时 {time.perf_counter() - t0:.2f}s")

    # ---------- ⑤ 对比：两次增量结果一致 ----------
    print("\n⑤ 验证「增量幂等」（两次增量结果一致）")
    second = snapshot(engine)
    mismatch2 = compare(first, second, "第一次增量 vs 第二次增量")

    # ---------- ⑥ 宽回看窗口（跨多个月份）回归 ----------
    wide_start = target - timedelta(days=WIDE_LOOKBACK)
    months = scalar(engine, """
        SELECT COUNT(*) FROM generate_series(
            DATE_TRUNC('month', %(s)s::date),
            DATE_TRUNC('month', %(e)s::date),
            INTERVAL '1 month')
    """, {"s": wide_start, "e": target})
    print(f"\n⑥ 宽回看窗口回归（--lookback {WIDE_LOOKBACK}：{wide_start} ~ {target}，"
          f"跨 {months} 个月份）...")
    t0 = time.perf_counter()
    if run_incremental(target, WIDE_LOOKBACK) != 0:
        print("\n[FAIL] 宽回看窗口增量执行失败"
              "（月粒度表的 DELETE 区间可能漏删中间月份）")
        return 1
    print(f"   耗时 {time.perf_counter() - t0:.2f}s")
    third = snapshot(engine)
    mismatch3 = compare(second, third, f"默认回看 vs 宽回看 {WIDE_LOOKBACK} 天")
    if months < 3:
        print(f"   [WARN] 本次区间只跨 {months} 个月份，未覆盖漏删缺陷场景"
              f"（换一个更靠后的 --date 再验）")

    # ---------- 汇总 ----------
    print("\n" + "=" * 74)
    failed = []
    if mismatch1:
        failed.append(f"增量结果 ≠ 增量前（{len(mismatch1)} 个对象）：{', '.join(mismatch1)}")
    if mismatch2:
        failed.append(f"两次增量结果不一致（{len(mismatch2)} 个对象）：{', '.join(mismatch2)}")
    if not gmv_ok:
        failed.append("三层 GMV 不一致")
    if mismatch3:
        failed.append(f"宽回看窗口结果不一致（{len(mismatch3)} 个对象）：{', '.join(mismatch3)}")

    if failed:
        print("增量验收 FAIL：")
        for f in failed:
            print(f"   - {f}")
        return 1
    print(f"增量验收 PASS：{len(OBJECTS)} 个对象 · 增量 ≡ 全量 · 两次增量结果一致 · "
          f"宽回看（跨 {months} 个月）一致 · GMV 三层一致")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
