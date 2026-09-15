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
    #    replay_config.py:  SCHEDULE = "*/2 * * * *"   每 2 分钟一次（必须 5 段 cron）
    #    （改完 git pull 即可，调度器会自动重新解析 DAG，不用重启服务）
    #    ⚠️ SCHEDULE 和 REPLAY_UNIT_SECONDS 必须配对，见 replay_config.py 的说明
    # 2. 让 scheduler 自己跑，或手动推进若干天：
    #    for d in 2026-09-15 2026-09-16 2026-09-17; do
    #      airflow dags trigger olist_warehouse_daily -e "$d" -c '{"force_replay": true}'
    #    done
    # 3. 每跑几次就 `python scripts/demo_replay.py status` 看累积
    # 4. 演示完 `python scripts/demo_replay.py restore` 恢复全量
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sql_runner import execute_script, fetch_all, get_engine      # noqa: E402

# 回放配置的唯一来源 —— 和 DAG 读的是同一份（项目根目录的 replay_config.py）
import replay_config                                              # noqa: E402

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
        # ⚠️ 水位线和上面两行**量的不是一回事**，必须标清楚：
        #    "覆盖区间" = 数仓里**真有数据**的日子；
        #    "水位线"   = 调度**处理到**哪天，**空天也算**（源数据 2016-09~10
        #                 61 个日历天里只有 14 天有订单，所以水位线会明显跑在前面）。
        #    而且 upsert 用 GREATEST，**只进不退** —— 一旦被异常运行推到未来，
        #    它要等回放真正走到那里才会自洽。
        #    不标清楚的话，这两个数字并排看会以为"不一致 = 出故障"（实测被误导过）。
        print(f"  已处理到       {wm_value}   （含空天；只进不退，与上面的覆盖天数不是一回事）")
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


def _guess_interval_seconds(expr):
    """
    从常见 cron 写法粗估「两次运行的间隔」（秒）。

    只覆盖演示会用的几种形式，认不出来就返回 None 由调用方兜底 ——
    这里刻意不做完整的 cron 解析（那是 croniter 的活），够用就行。
    """
    if not expr:
        return None
    f = expr.split()
    if len(f) not in (5, 6):
        return None
    # ⚠️ 段数**不影响**「分 / 时」的位置：croniter（Airflow 的底层实现）把 6 段
    #    当作 `分 时 日 月 周 年` —— **第 6 段是「年」，不是「秒」**。
    #
    #    这里曾经写成 `f[1], f[2]`（误以为 6 段是 `秒 分 时 …`），于是对
    #    错误的 `"0 0/2 * * * *"` 也推断出 120 秒、给出 `[OK]` 的**假通过** ——
    #    而它实际含义是「每 2 小时」。**诊断工具和配置犯了同一个误解，
    #    就互相放过了**，白等了几个小时才发现。所以这里刻意按 croniter 的
    #    真实语义取 `f[0], f[1]`。
    minute, hour = f[0], f[1]

    def every(field, base):
        """*/N 或 A/N → N*base"""
        if field.startswith("*/"):
            field = field[2:]
        elif "/" in field:
            field = field.split("/")[1]
        else:
            return None
        try:
            return int(field) * base
        except ValueError:
            return None

    if minute == "*":
        return 60
    got = every(minute, 60)
    if got:
        return got
    if hour == "*":
        return 3600
    got = every(hour, 3600)
    if got:
        return got
    return 86400 if minute.isdigit() else None


def show_preview(interval_arg=None, steps: int = 6) -> int:
    """
    回放映射预览：**在调度周期的节奏上**探测，看每次运行会不会推进一天。

    为什么要这样探测：如果按「步长的节奏」探测，无论步长多大都会显示"正常推进"，
    抓不到「步长 ≫ 调度周期」这个真正的问题。
    必须在**调度真的多久跑一次**这个节奏上算，才能看出问题。

    判据（interval = 调度间隔，unit = 步长）：
        interval == unit   → [OK]   每次运行正好推进 1 天（演示的理想状态）
        interval <  unit   → [FAIL] 同一天内多次运行算出同一个目标日 → 反复处理同一天
        interval >  unit   → [WARN] 每次跳好几天 → 会跳过一些日子
    """
    from etl_tasks import DATA_END, DATA_SPAN, DATA_START, resolve_target

    # 配置全部来自 replay_config.py —— 和 DAG 同一份，不用 source 环境变量
    unit = replay_config.REPLAY_UNIT_SECONDS
    schedule = replay_config.SCHEDULE
    run_mode = replay_config.RUN_MODE
    epoch = replay_config.REPLAY_EPOCH
    guessed = _guess_interval_seconds(schedule)

    def human(sec):
        if sec is None:
            return "?"
        return f"{sec / 60:.0f} 分钟" if sec >= 60 else f"{sec} 秒"

    print("=" * 72)
    print("回放映射预览（配置来自 replay_config.py，和 DAG 同一份）")
    print("=" * 72)
    print(f"  RUN_MODE                  = {run_mode}")
    print(f"  REPLAY_EPOCH              = {epoch}")
    print(f"  REPLAY_UNIT_SECONDS       = {unit}  →  真实时间每 {human(unit)} 推进一天")
    print(f"  SCHEDULE                  = {schedule}")
    print(f"  数据区间                   {DATA_START} ~ {DATA_END}")

    if run_mode.strip().lower() != "replay":
        print("\n  当前不是 replay 模式，回放映射不生效（直接用真实日期）。")
        return 0

    interval = interval_arg or guessed
    src = ("--interval 指定" if interval_arg else
           "从 SCHEDULE 推断" if guessed else
           "推断失败，退回用步长 —— 结果仅供参考，请用 --interval 显式指定")
    print(f"\n  调度间隔 ≈ {human(interval)}（{src}）")

    # ---- 核心判据：间隔 vs 步长 ----
    if interval and interval != unit:
        print()
        if interval < unit:
            print(f"  [FAIL] 调度间隔({human(interval)}) < 步长({human(unit)})")
            print(f"         后果：要过 {unit / interval:.0f} 次运行目标日才前进 1 天 ——")
            print("               在那之前每次都反复处理同一天，看起来像「回放不推进」。")
            print(f"         修法：把 replay_config.py 的 REPLAY_UNIT_SECONDS 改成 {int(interval)}")
        else:
            print(f"  [WARN] 调度间隔({human(interval)}) > 步长({human(unit)})")
            print(f"         后果：每次跳 {interval / unit:.0f} 天，会跳过一些日子。")
            print(f"         修法：把 replay_config.py 的 REPLAY_UNIT_SECONDS 改成 {int(interval)}")

    # ---- 可视化：在调度节奏上探测 ----
    probe = interval or unit
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    def rt(ts):
        """用**配置文件里的**参数算映射（不依赖 etl_tasks 的环境变量默认值）"""
        return resolve_target(ts, mode=run_mode, epoch=epoch, unit_seconds=unit)

    cur = rt(base)
    cur_offset = (cur - DATA_START).days
    print(f"\n  当前时刻对应的回放进度：第 {cur_offset} 天（{cur}）")
    if cur_offset > 3:
        suggested = base.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"  想让它从第 0 天（{DATA_START}）开始，就把 replay_config.py 里的")
        print(f"  REPLAY_EPOCH 改成「现在」附近（支持带时间）：")
        print(f"      REPLAY_EPOCH = \"{suggested}\"")
        print(f"  然后跑 python scripts/demo_replay.py reset --yes 清空数仓重新累积。")

    print(f"\n  按调度节奏探测 {steps} 次（每次间隔 {human(probe)}）：")
    results = []
    for k in range(steps):
        t = base + timedelta(seconds=probe * k)
        target = rt(t)
        results.append(target)
        print(f"    {t:%Y-%m-%d %H:%M}Z  →  {target}")

    gaps = [(results[i + 1] - results[i]).days for i in range(len(results) - 1)]
    advanced = sum(g for g in gaps if g > 0)
    wrapped = any(g < 0 for g in gaps)

    print()
    if interval and interval == unit:
        print("  [OK] 步长与调度周期匹配 —— 每次运行推进 1 天")
    elif advanced == 0 and not wrapped:
        print("  [FAIL] 探测期间目标日**完全没推进** —— 就是上面那个问题")
    else:
        tail = "，期间绕回过区间开头，属预期）" if wrapped else "）"
        print(f"  [OK] 映射在推进（探测期间共前进 {advanced} 天{tail}")
    print("=" * 72)
    return 0 if (interval == unit or advanced > 0 or wrapped) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="数据回放演示工具")
    parser.add_argument("action", choices=["status", "preview", "reset", "restore"],
                        help="status=看进度 / preview=看回放映射 / reset=清空 / restore=恢复全量")
    parser.add_argument("--yes", action="store_true", help="reset 时确认执行")
    parser.add_argument("--interval", type=int,
                        help="preview：调度间隔（秒）。不传则从 replay_config.SCHEDULE 推断")
    args = parser.parse_args()

    if args.action == "status":
        show_status()
        return 0
    if args.action == "preview":
        return show_preview(args.interval)
    if args.action == "reset":
        return do_reset(args.yes)
    return do_restore()


if __name__ == "__main__":
    sys.exit(main())
