"""
按层拆分的 ETL 任务入口 —— 供调度器（Airflow / systemd timer）逐步调用

与 run_all.py 的分工：
    run_all.py --date X    一次性把 DWD → DWS → ADS 串起来跑完（手工 / 本地用）
    etl_tasks.py           同样这些步骤，但**每层一个函数**（调度器用）

为什么要拆：
    调度器需要「每层一个 task」才能做到：失败单独重试、UI 上看出卡在哪层、
    以及把「必须全量」的那批（cohort 口径）挂在最后单独跑。
    run_all.py --date 是一把梭，做不到这些。

为什么不会出现两套逻辑：
    层 → SQL 文件的映射直接 import `run_all.INCREMENTAL_FILES_BY_LAYER`，
    执行原语也共用 `sql_runner`。所以「调度跑的顺序」和「手工跑的顺序」
    来自同一份定义。

⚠️ 失败必须**抛异常**，不能只返回非零 —— Airflow 靠异常判定 task 失败，
   返回码它不认。所以下面所有函数在非 0 时统一 raise RuntimeError。

命令行用法（也方便 systemd timer 直接调）：
    python scripts/etl_tasks.py --layer dwd --target 2017-10-28
    python scripts/etl_tasks.py --full-only
    python scripts/etl_tasks.py --quality-gate
"""
import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import replay_config                                            # noqa: E402  (项目根目录)
from sql_runner import get_engine, report_rows, run_sql_file   # noqa: E402
from run_all import (                                          # noqa: E402
    INCREMENTAL_FILES_BY_LAYER, LOOKBACK_DAYS_DEFAULT, REQUIRED_TABLES, table_exists,
)

LAYERS = tuple(INCREMENTAL_FILES_BY_LAYER)


def window(target: date, lookback: int = LOOKBACK_DAYS_DEFAULT) -> tuple:
    """
    把「目标日」展开成实际重算区间。

    为什么要有回看窗口：上游数据会迟到（今天才推昨天的订单），
    只算 target 当天会让昨天新到的数据永远进不了数仓。
    """
    return target - timedelta(days=lookback), target


# ===============================================================
# 「本次处理哪一天」的解析：正常模式 / 数据回放模式
# ===============================================================
# 为什么需要回放模式：
#   本项目的源 CSV 是静态的（2016-09 ~ 2018-10），不会有新数据每天到达。
#   若照真实日期跑，定时任务每天都在处理"今天"，而这个区间里没有数据 —— 纯空转。
#   回放模式把「真实日期」映射到「数据区间里的某一天」，
#   于是每天的调度都有真实数据可处理，水位线真的在往前推。
#
#   映射是**纯函数（无状态）**：同一个 logical date 永远映射到同一个购买日，
#   所以重跑那次调度结果一致 —— 幂等性不受影响（有状态的游标会破坏这一点）。

DATA_START = date(2016, 9, 4)
DATA_END = date(2018, 10, 17)
DATA_SPAN = (DATA_END - DATA_START).days + 1        # 774 个日历天

# ⚠️ 回放参数的**唯一来源**是项目根目录的 `replay_config.py`。
#
# 这里以前读的是 `OLIST_RUN_MODE` / `OLIST_REPLAY_EPOCH` / `OLIST_REPLAY_UNIT_SECONDS`
# 环境变量，而 `dags/olist_daily.py` 走的是 `replay_config.py` —— **同一个参数两个来源**。
# systemd 那条路径靠 `EnvironmentFile` 传值、命令行那条路径靠 shell 环境，
# 两边一旦不一致就会出现「手动跑是 A 日、调度跑是 B 日」，极难查。
# 现在统一成 import，环境变量改不动回放行为了（这是故意的）。
RUN_MODE = replay_config.RUN_MODE.strip().lower()


def _parse_epoch(raw: str):
    """
    回放起点，支持两种写法：

        '2026-09-15'              → 当天 00:00Z（粗粒度）
        '2026-09-15T06:30:00'     → 精确到秒

    为什么要支持带时间：offset 是 `floor((now - epoch) / 步长)`，
    若 epoch 只能落在午夜，那"从第 0 天开始"就只在半夜那几分钟成立 ——
    白天启动演示时 offset 已经跑掉一大截，回放会从数据区间中间开始。
    """
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return date.fromisoformat(raw)


REPLAY_EPOCH = _parse_epoch(replay_config.REPLAY_EPOCH)

# 回放步长：真实时间每过这么多秒，数据时间推进一天。
#   86400（默认）= 一天推一天        —— 配合 SCHEDULE = "0 2 * * *"
#   120          = 每 2 分钟推一天    —— 配合 SCHEDULE = "*/2 * * * *"（快速演示）
#
# ⚠️ **必须和 SCHEDULE 的周期匹配**。如果调度是每 2 分钟一次、而步长还是 86400，
#    同一天内的多次运行会算出同一个 offset → 反复处理同一天，看着像"回放不推进"。
#    跑 `python scripts/demo_replay.py preview` 可以一次验证两者是否匹配。
REPLAY_UNIT_SECONDS = replay_config.REPLAY_UNIT_SECONDS


def _as_utc(ts) -> datetime:
    """
    str / date / datetime → 带 UTC 时区的 datetime。

    接受字符串是因为配置通常写成文本（`replay_config.REPLAY_EPOCH`
    和 `replay_config.REPLAY_EPOCH` 都是字符串），调用方不该被迫先转换一遍。
    """
    if isinstance(ts, str):
        ts = _parse_epoch(ts)          # '2026-09-15' 或 '2026-09-15T06:36:00'
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)


def resolve_target(real_ts, mode: str = None, epoch: date = None,
                   unit_seconds: int = None) -> date:
    """
    把「本次运行的真实时间」解析成「要处理的购买日」。

    mode:
      "real"   —— 直接用真实日期（数据每天真实到达的场景）
      "replay" —— 映射到数据区间里的某一天（本项目的默认，见上方说明）

    回放映射（纯函数、无状态）：

        offset = floor((real_ts - epoch) / 步长秒数) % DATA_SPAN
        target = DATA_START + offset 天

    **为什么用「秒」算而不是「天数差」**：天数差只有天的粒度，
    同一天内跑多次会得到同一个 offset —— 回放就不推进了。
    按秒算才能支持比"一天一次"更密的演示调度（比如 2 分钟一天）。

    走到区间末尾会自动绕回开头（循环回放），方便反复演示。
    """
    m = mode or RUN_MODE
    if m != "replay":
        return real_ts.date() if isinstance(real_ts, datetime) else real_ts

    unit = unit_seconds or REPLAY_UNIT_SECONDS
    elapsed = (_as_utc(real_ts) - _as_utc(epoch or REPLAY_EPOCH)).total_seconds()
    offset = int(elapsed // unit)

    # epoch 落在"现在之后一点点"时 elapsed 会是负数（比如把 epoch 设成当前时刻、
    # 但秒数取整差了几十秒）。这种小负数不该绕到数据末尾去，直接从第 0 天开始。
    if offset < 0:
        offset = 0
    return DATA_START + timedelta(days=offset % DATA_SPAN)


def advance_watermark(target: date) -> date:
    """
    把水位线推进到本次处理的购买日，返回更新后的实际值。

    用 GREATEST 保证只前进不后退（补跑历史某天不会把水位线拉回去），
    语义与 `run_all.upsert_watermark` 完全一致 —— 两条路径共用一个水位线。

    ⚠️ 回放模式下水位线可能已经在前面（比如回放从 2016-09-04 重新开始时，
    它会停在之前跑到的最大日期）。想从零开始看它推进，见
    docs/Airflow使用手册.md「重置回放演示」。
    """
    from run_all import read_watermark, upsert_watermark

    engine = get_engine()
    upsert_watermark(engine, "incremental", target)
    current = read_watermark(engine)
    print(f"水位线 etl_watermark.last_date = {current}")
    return current


def check_prerequisites() -> None:
    """增量 SQL 里没有 CREATE，所以这些表必须已存在（首次要先跑一次全量）"""
    engine = get_engine()
    missing = [t for t in REQUIRED_TABLES if not table_exists(engine, t)]
    if missing:
        raise RuntimeError(
            "以下表不存在，请先跑一次全量 `python scripts/run_all.py`：\n  - "
            + "\n  - ".join(missing)
        )


def run_layer(layer: str, start: date, end: date) -> list:
    """
    跑某一层的增量 SQL（DELETE 区间 + INSERT）。

    幂等：同一区间重跑结果完全一致（由 check_incremental.py 验证），
    所以调度器开 retries 是安全的 —— 失败重试不会写坏数据。
    """
    if layer not in INCREMENTAL_FILES_BY_LAYER:
        raise ValueError(f"未知的层：{layer}（可选 {LAYERS}）")

    engine = get_engine()
    objects = []
    for rel_path in INCREMENTAL_FILES_BY_LAYER[layer]:
        objects.extend(run_sql_file(engine, rel_path,
                                    {"start_date": start, "end_date": end}))
    if objects:
        report_rows(engine, objects)
    return objects


def run_full_only() -> int:
    """
    重建「必须全量」的 ADS 对象 + 视图 + 对账。

    cohort 口径的留存/复购表依赖**全部历史**（新一天的数据会改变 cohort 定义），
    不能按日增量；ads_top_seller 是全周期榜，也只有 3,053 行，全量重建更简单。
    """
    from ads_build import rebuild_full_only as _rebuild

    code = _rebuild()
    if code != 0:
        raise RuntimeError("必须全量的那批 ADS 对象重建后对账未通过")
    return code


def run_quality_gate() -> int:
    """
    只跑对账断言，不重建任何东西。

    单独成一个 task 的意义：它是**数据质量门禁** —— 对不上就让 DAG 失败，
    不允许下游/看板拿到错数据。放在最后跑，能明确区分
    「跑挂了」和「跑完了但数不对」。
    """
    from ads_build import report_reconciliation

    code = report_reconciliation(get_engine())
    if code != 0:
        raise RuntimeError("数据质量门禁未通过：对账存在不一致")
    return code


def daily(target: date, lookback: int = LOOKBACK_DAYS_DEFAULT) -> None:
    """把各层串起来跑一遍 —— 等价于 run_all.py --date，供不方便拆 task 的调度器用"""
    check_prerequisites()
    start, end = window(target, lookback)
    print(f"增量区间：{start} ~ {end}（回看 {lookback} 天）")
    for layer in LAYERS:
        print(f"\n{'=' * 68}\n{layer.upper()} 增量\n{'=' * 68}")
        run_layer(layer, start, end)
    run_full_only()


def main() -> int:
    parser = argparse.ArgumentParser(description="按层拆分的 ETL 任务入口")
    parser.add_argument("--layer", choices=LAYERS, help="只跑某一层的增量")
    parser.add_argument("--target", help="目标购买日 YYYY-MM-DD（与 --layer 搭配）")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS_DEFAULT,
                        help=f"回看天数（默认 {LOOKBACK_DAYS_DEFAULT}）")
    parser.add_argument("--full-only", action="store_true", help="只重建必须全量的 ADS 对象")
    parser.add_argument("--quality-gate", action="store_true", help="只跑对账门禁")
    parser.add_argument("--check", action="store_true", help="只做前置检查（表是否都在）")
    args = parser.parse_args()

    t0 = time.perf_counter()
    try:
        if args.check:
            check_prerequisites()
            print(f"[OK] {len(REQUIRED_TABLES)} 张前提表全部存在")
        elif args.full_only:
            run_full_only()
        elif args.quality_gate:
            run_quality_gate()
        elif args.layer:
            if not args.target:
                print("[FAIL] --layer 必须搭配 --target")
                return 1
            check_prerequisites()
            start, end = window(date.fromisoformat(args.target), args.lookback)
            print(f"{args.layer.upper()} 增量：{start} ~ {end}")
            run_layer(args.layer, start, end)
        else:
            parser.print_help()
            return 1
    except Exception as exc:                       # noqa: BLE001
        print(f"\n[FAIL] {type(exc).__name__}: {exc}")
        return 1

    print(f"\n[OK] 耗时 {time.perf_counter() - t0:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
