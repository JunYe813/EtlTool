"""
数据回放演示：清空数仓 → 从零逐日累积 → 随时恢复全量

背景：源 CSV 是静态的（2016-09 ~ 2018-10），全量重建后数仓里已经是**完整数据**。
此时再跑「数据回放」，每个 run 只是 DELETE + INSERT 同一批行（净变化为零）——
它演示的是「流水线稳定运转 + 幂等」，**看不出数据从无到有的累积**。

想看到累积效果，就得先把 DWD/DWS/ADS 清空，让它从 `2016-09-04` 一天天堆起来。

★ 安全性：**只清 DWD/DWS/ADS，ODS 源数据一行不动。**
  而 DWD 是从 ODS 推出来的，所以随时能 10 秒恢复全量（`restore` 子命令）。
  「清空」是一次可撤销的实验，不是有风险的操作。

用法：
    python scripts/demo_replay.py status      # 看当前进度（数仓覆盖了几天、水位线在哪）
    python scripts/demo_replay.py reset       # 清空 + 重置水位线（需加 --yes 才真执行）
    python scripts/demo_replay.py restore     # 全量重建，恢复完整数据

清空之后的演示流程：
    # 1. 用密集一点的调度快速推进（可选，默认每天一天）
    #    ~/airflow/airflow.env:  OLIST_SCHEDULE='*/2 * * * *'
    #    sudo systemctl restart airflow-scheduler airflow-webserver
    # 2. 让 scheduler 自己跑，或手动推进若干天：
    #    for d in 2026-09-15 2026-09-16 2026-09-17; do
    #      airflow dags trigger olist_warehouse_daily -e "$d" -c '{"force_replay": true}'
    #    done
    # 3. 每跑几次就 `python scripts/demo_replay.py status` 看累积
    # 4. 演示完 `python scripts/demo_replay.py restore` 恢复全量
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sql_runner import execute_script, fetch_all, get_engine      # noqa: E402

# 表清单从 check_idempotent 的 LAYER_OBJECTS 派生（剔掉视图）——
# 这样新增对象时只要同步那一处，这份脚本自动跟着走，不会漏。
from check_idempotent import LAYER_OBJECTS                        # noqa: E402

TRUNCATE_TABLES = [
    t for layer in ("dwd", "dws", "ads")
    for t in LAYER_OBJECTS[layer]
    if not t.startswith("v_")
]

# 全量状态下的基准值（见 docs/数据字典.md 第九节）
FULL_ORDER_ROWS = 99441
FULL_GMV = 13494400.74
DATA_FIRST_DAY = "2016-09-04"
WATERMARK_RESET_TO = "2016-09-03"      # 数据开始日的前一天


def show_status() -> None:
    engine = get_engine()

    covered = fetch_all(engine, """
        SELECT MIN(purchase_date), MAX(purchase_date), COUNT(DISTINCT purchase_date)
        FROM dwd_order_detail
    """)[0]
    orders = fetch_all(engine, "SELECT COUNT(*) FROM dwd_order")[0][0]
    gmv = fetch_all(engine, "SELECT COALESCE(ROUND(SUM(gmv), 2), 0) FROM dwd_order WHERE is_valid")[0][0]
    wm = fetch_all(engine,
                   "SELECT last_date FROM etl_watermark WHERE job_name = 'incremental'")
    wm_value = wm[0][0] if wm else None

    print("=" * 72)
    print("数据回放进度")
    print("=" * 72)

    if orders == 0:
        print("  数仓是空的 —— 已重置，等待回放开始")
    else:
        print(f"  明细覆盖区间   {covered[0]} ~ {covered[1]}")
        print(f"  覆盖天数       {covered[2]:,} 天")
        print(f"  水位线         {wm_value}")
        print()
        print(f"  {'':<14}{'当前':>16}   {'全量基准':>16}")
        print(f"  {'订单数':<14}{orders:>16,}   {FULL_ORDER_ROWS:>16,}")
        print(f"  {'GMV':<14}{float(gmv):>16,.2f}   {FULL_GMV:>16,.2f}")
        pct = orders / FULL_ORDER_ROWS * 100 if FULL_ORDER_ROWS else 0
        bar = "#" * int(pct / 2.5)
        print(f"\n  数据完整度     [{bar:<40}] {pct:5.1f}%")

    print()
    print("  " + "-" * 68)
    print("  下一步：")
    if orders == 0:
        print("    · 让 Airflow 跑起来（每天推进一天），或手动推进：")
        print("      airflow dags trigger olist_warehouse_daily -e 2026-09-15 \\")
        print("        -c '{\"force_replay\": true}'")
    else:
        print("    · 继续回放 → 每天一个 run，数字会往上长")
        print("    · 想回到完整数据 → python scripts/demo_replay.py restore")
    print("=" * 72)


def do_reset(assume_yes: bool) -> int:
    engine = get_engine()

    print("=" * 72)
    print("清空数仓（准备从零回放）")
    print("=" * 72)
    print(f"  将 TRUNCATE 这 {len(TRUNCATE_TABLES)} 张表：")
    for t in TRUNCATE_TABLES:
        print(f"    - {t}")
    print()
    print("  ★ ODS 源数据不动，所以随时能恢复：")
    print("      python scripts/demo_replay.py restore")
    print()

    if not assume_yes:
        print("  这是破坏性操作，需要显式确认。确认后请加 --yes 重跑：")
        print("      python scripts/demo_replay.py reset --yes")
        return 1

    execute_script(engine, "TRUNCATE TABLE "
                           + ", ".join(TRUNCATE_TABLES) + " CASCADE;")
    execute_script(engine,
                   "UPDATE etl_watermark SET last_date = %(d)s WHERE job_name = 'incremental'",
                   {"d": WATERMARK_RESET_TO})
    print(f"  [OK] 已清空 {len(TRUNCATE_TABLES)} 张表")
    print(f"  [OK] 水位线已退回 {WATERMARK_RESET_TO}")
    print()
    print("  现在可以让 Airflow 从零累积了。跑几次后回来看：")
    print("      python scripts/demo_replay.py status")
    print("=" * 72)
    return 0


def do_restore() -> int:
    print("=" * 72)
    print("恢复全量（ODS 还在，所以直接全量重建即可）")
    print("=" * 72)
    import subprocess

    code = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_all.py"), "--skip-ods"],
        cwd=ROOT,
    ).returncode
    if code == 0:
        print("\n  [OK] 已恢复完整数据")
        show_status()
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="数据回放演示工具")
    parser.add_argument("action", choices=["status", "reset", "restore"],
                        help="status=看进度 / reset=清空 / restore=恢复全量")
    parser.add_argument("--yes", action="store_true", help="reset 时确认执行")
    args = parser.parse_args()

    if args.action == "status":
        show_status()
        return 0
    if args.action == "reset":
        return do_reset(args.yes)
    return do_restore()


if __name__ == "__main__":
    sys.exit(main())
