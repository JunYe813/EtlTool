"""
幂等性验证：重建前后对比每张表/视图的内容摘要

对应项目大纲的验收标准：
    「run_all.py 重跑两次结果一致（幂等验证）」

做法：对每张表按行算 MD5，排序后再聚合一次 MD5，得到与物理行序无关的内容指纹。
      重建前后指纹一致 → 同样输入必得同样输出 → 幂等成立。

用法：
    python scripts/check_idempotent.py            # 验证 ADS 层
    python scripts/check_idempotent.py --all      # 验证 DWD + DWS + ADS 全链路
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sql_runner import fetch_all, get_engine, run_sql_file   # noqa: E402

LAYER_OBJECTS = {
    "dwd": ["dwd_order_detail", "dwd_order"],
    "dws": ["dws_sale_daily"],
    "ads": [
        "ads_sale_overview_daily",
        "ads_top_product",
        "ads_top_seller",
        "ads_user_retention",
        "ads_user_repeat_overall",
        "ads_user_repeat_monthly",
        "ads_fulfillment_monthly",
        "v_sale_daily_category",
        "v_sale_daily_seller_state",
        "v_sale_daily_buyer_state",
        "v_fulfillment_funnel",
        "v_retention_curve",
    ],
}

# 行指纹：整行转 jsonb 后剔除审计时间列，再 MD5；排序后聚合，消除物理行序影响。
#
# 为什么要剔除 create_date / modified_date：
#   这两列是 ETL 审计列，语义是"本批次数据的写入/更新时间"，全量重建时必然刷新，
#   它们变化不代表幂等性被破坏。真正要验证的是**业务数据**是否一致。
#   项目里所有表都有这两列，视图没有 —— to_jsonb 上做 - 运算，键不存在时是无操作，
#   所以同一段 SQL 对表和视图都适用。
#
# 注意：视图的 MATCH 是更强的证据 —— 它们直接反映业务数据，且不含审计列。
DIGEST_SQL = """
SELECT COUNT(*),
       COALESCE(MD5(STRING_AGG(row_hash, '' ORDER BY row_hash)), '-')
FROM (
    SELECT MD5((TO_JSONB(t) - 'create_date' - 'modified_date')::text) AS row_hash
    FROM "{table}" t
) x
"""


def snapshot(engine, tables) -> dict:
    """
    记录对象的内容指纹。

    对象不存在时记为 None —— 改名/新增对象后第一次运行时库里的还是旧结构，
    此时不该直接报错，而应识别为"本次新建"。
    """
    out = {}
    for t in tables:
        try:
            out[t] = fetch_all(engine, DIGEST_SQL.format(table=t))[0]
        except Exception:                                  # noqa: BLE001
            out[t] = None
    return out


def rebuild_dwd_dws():
    engine = get_engine()
    for rel_path in ("sql/01_dwd.sql", "sql/02_dws.sql"):
        run_sql_file(engine, rel_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="数仓幂等性验证")
    parser.add_argument("--all", action="store_true",
                        help="验证 DWD + DWS + ADS 全链路（默认只验证 ADS）")
    args = parser.parse_args()

    layers = ["dwd", "dws", "ads"] if args.all else ["ads"]
    objects = [t for layer in layers for t in LAYER_OBJECTS[layer]]

    engine = get_engine()
    print(f"验证范围：{', '.join(l.upper() for l in layers)}（{len(objects)} 个对象）\n")

    print("① 记录当前内容指纹 ...")
    before = snapshot(engine, objects)
    for t, snap in before.items():
        if snap is None:
            print(f"     {t:<26} {'（对象尚不存在，本次将新建）':>28}")
        else:
            print(f"     {t:<26} {snap[0]:>8,} 行  {snap[1][:16]}")

    print("\n② 全量重建 ...")
    t0 = time.perf_counter()
    if args.all:
        rebuild_dwd_dws()
    from ads_build import rebuild
    if rebuild() != 0:
        print("\n重建过程对账未通过，终止验证")
        return 1
    elapsed = time.perf_counter() - t0

    print(f"\n③ 对比重建后的内容指纹（重建耗时 {elapsed:.2f}s）...")
    after = snapshot(engine, objects)

    mismatch = []
    for t in objects:
        b = before[t]
        a = after[t]
        if b is None:
            print(f"  [NEW]    {t:<26} 本次新建  {a[0]:>8,} 行  {a[1][:16]}")
        elif a is None:
            print(f"  [GONE]   {t:<26} 重建后消失")
            mismatch.append(t)
        elif b == a:
            print(f"  [MATCH]  {t:<26} {a[0]:>8,} 行  {a[1][:16]}")
        else:
            print(f"  [DIFFER] {t:<26} {b[0]:,} -> {a[0]:,} 行  {b[1][:16]} -> {a[1][:16]}")
            mismatch.append(t)

    print("\n" + "=" * 68)
    if mismatch:
        print(f"幂等验证 FAIL：{len(mismatch)} 个对象内容发生变化 -> {', '.join(mismatch)}")
        return 1
    print(f"幂等验证 PASS：{len(objects)} 个对象重建前后内容完全一致")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
