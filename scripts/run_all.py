"""
一键全流程：ODS → DWD → DWS → ADS

用法：
    python scripts/run_all.py                         # 全量重建（首次 / 重置）
    python scripts/run_all.py --skip-ods              # 跳过 ODS 导入
    python scripts/run_all.py --only ads              # 只跑某一层（仅全量模式）
    python scripts/run_all.py --date 2017-01-05       # 按购买日增量重跑
    python scripts/run_all.py --date 2017-01-05 --lookback 7   # 自定义回看窗口

两条路径并存：
    全量路径：sql/01_dwd.sql · 02_dws.sql · 03_ads_*.sql   （DROP + CREATE，用于首次 / 重置）
    增量路径：sql/load/01_dwd_inc.sql · 02_dws_inc.sql · 03_ads_inc.sql
              （DELETE 区间 + INSERT，不 DROP，用于日常 / 补数）

为什么要有两条路径：
    增量改造不能把全量路径改掉 —— 那样万一增量出问题就没法退回来。
    真实 ETL 也是这个模式：**初始化走全量，之后走增量**。

为什么要回看窗口（--lookback）：
    不能只算 --date 当天。真实场景里上游数据会迟到（今天才推昨天的订单），
    只算当天会让昨天的新增数据永远进不了数仓。所以默认重算 [date-3, date]。
"""
import argparse
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sql_runner import (                       # noqa: E402
    execute_script, get_engine, report_rows, run_sql_file, scalar,
)

LAYERS = {
    "dwd": ["sql/01_dwd.sql"],
    "dws": ["sql/02_dws.sql"],
}

# 增量路径：顺序即依赖顺序（DWS / ADS 依赖 DWD；明细先于主表）
INCREMENTAL_FILES = [
    "sql/load/01_dwd_inc.sql",
    "sql/load/02_dws_inc.sql",
    "sql/load/03_ads_inc.sql",
]

# 增量路径的前提：这些表必须已存在（增量 SQL 里没有 CREATE）
REQUIRED_TABLES = [
    "dwd_order_detail", "dwd_order",
    "dws_sale_daily", "dws_sale_daily_seller", "dws_sale_daily_product",
    "ads_sale_overview_daily", "ads_top_product", "ads_fulfillment_monthly",
]

LOOKBACK_DAYS_DEFAULT = 3


def run_sql_layer(name: str, files) -> None:
    print(f"\n{'=' * 68}\n{name}\n{'=' * 68}")
    engine = get_engine()
    for rel_path in files:
        objects = run_sql_file(engine, rel_path)
        report_rows(engine, objects)


def run_ods() -> None:
    print(f"\n{'=' * 68}\nODS 导入\n{'=' * 68}")
    # 子进程执行：olist_import.py 自身带「已导入文件跳过」逻辑
    subprocess.run([sys.executable, str(ROOT / "scripts" / "olist_import.py")],
                   cwd=ROOT, check=True)


def table_exists(engine, name: str) -> bool:
    """用 to_regclass 判断表是否存在（不存在返回 NULL，不会抛异常）"""
    return bool(scalar(engine, f"SELECT to_regclass('public.{name}') IS NOT NULL"))


def upsert_watermark(engine, job_name: str, last_date: date) -> None:
    """
    写水位线：记录「已处理到哪个购买日」。

    用 GREATEST 保证水位线只前进不后退 —— 补跑历史某天时不该把水位线拉回去。
    """
    execute_script(engine, """
        INSERT INTO etl_watermark (job_name, last_date, update_time)
        VALUES (%(j)s, %(d)s, CURRENT_TIMESTAMP)
        ON CONFLICT (job_name) DO UPDATE
        SET last_date   = GREATEST(etl_watermark.last_date, EXCLUDED.last_date),
            update_time = CURRENT_TIMESTAMP
    """, {"j": job_name, "d": last_date})


def read_watermark(engine, job_name: str = "incremental"):
    """读回水位线当前值（用于校验 upsert 之后的真实状态，而不是假设）"""
    return scalar(engine,
                  "SELECT last_date FROM etl_watermark WHERE job_name = %(j)s",
                  {"j": job_name})


def run_incremental(target: date, lookback: int) -> int:
    """按购买日增量重跑 [target - lookback, target] 区间"""
    engine = get_engine()

    print("=" * 68)
    print("增量装载")
    print("=" * 68)

    # ① 增量路径的前提：表必须已存在（增量 SQL 里没有 CREATE）
    missing = [t for t in REQUIRED_TABLES if not table_exists(engine, t)]
    if missing:
        print(f"  [FAIL] 以下表不存在，请先跑一次全量：")
        for t in missing:
            print(f"         - {t}")
        print("         python scripts/run_all.py")
        return 1

    # ② 回看窗口
    start = target - timedelta(days=lookback)
    params = {"start_date": start, "end_date": target}
    print(f"  增量区间：{start} ~ {target}（回看 {lookback} 天）\n")

    # ③ 按 DWD → DWS → ADS 跑增量 SQL
    for rel_path in INCREMENTAL_FILES:
        objects = run_sql_file(engine, rel_path, params)
        report_rows(engine, objects)

    # ④ 更新水位线
    #    注意：这里打印的是**读回来的真实值**，不是 target。
    #    补数跑一个更早的日期时 GREATEST 会保留较大值，此时 target ≠ 水位线，
    #    若直接打印 target 会误导（看起来像是水位线被回退了）。
    upsert_watermark(engine, "incremental", target)
    current = read_watermark(engine)
    if current == target:
        print(f"  [OK] 水位线 etl_watermark.last_date = {current}")
    else:
        print(f"  [OK] 水位线 etl_watermark.last_date = {current}"
              f"（本次目标 {target} 更早，GREATEST 保留较大值，水位线未回退）")

    # ⑤ 跑「必须全量」的那部分（cohort 表 + ads_top_seller）+ 对账
    from ads_build import rebuild_full_only
    return rebuild_full_only()


def main() -> int:
    parser = argparse.ArgumentParser(description="电商数仓一键全流程")
    parser.add_argument("--skip-ods", action="store_true", help="跳过 ODS 导入")
    parser.add_argument("--only", choices=["ods", "dwd", "dws", "ads"],
                        help="只跑指定层（仅全量模式有效）")
    parser.add_argument("--date", help="按购买日增量重跑，格式 YYYY-MM-DD")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS_DEFAULT,
                        help=f"回看天数，处理上游迟到数据（默认 {LOOKBACK_DAYS_DEFAULT}）")
    args = parser.parse_args()

    t0 = time.perf_counter()

    # ================= 增量模式 =================
    if args.date:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"[FAIL] --date 格式错误：{args.date}（应为 YYYY-MM-DD）")
            return 1
        code = run_incremental(target, args.lookback)
        print(f"\n总耗时 {time.perf_counter() - t0:.2f}s")
        return code

    # ================= 全量模式（原行为不变） =================
    targets = [args.only] if args.only else ["ods", "dwd", "dws", "ads"]

    if "ods" in targets and not args.skip_ods:
        run_ods()
    elif "ods" in targets:
        print("跳过 ODS 导入（--skip-ods）")

    for layer in ("dwd", "dws"):
        if layer in targets:
            run_sql_layer(layer.upper() + " 构建", LAYERS[layer])

    if "ads" in targets:
        # 直接调用模块内的 rebuild()，保证和对账逻辑用的是同一份代码
        from ads_build import rebuild
        code = rebuild()
        if code != 0:
            print(f"\n总耗时 {time.perf_counter() - t0:.2f}s")
            return code

    print(f"\n全流程完成，总耗时 {time.perf_counter() - t0:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
