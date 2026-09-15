"""
Olist 四层数仓 · 每日增量 DAG

设计要点（面试可直接讲的三条）：

1. **按层拆 task，而不是一个「跑全流程」的大 task**
   这样某一层失败能单独重试、UI 上能看出卡在哪层。
   更重要的是：把「必须全量」的那批（cohort 口径的留存/复购表）单独挂成
   一个 task —— 它依赖**全部历史**，和「只依赖本区间」的那批性质不同，
   不该混在一个 task 里。这条判断在 `ads_build.py` 里是常量列表，
   在 DAG 里就是**任务结构**，一眼能看出来。

2. **重试是安全的，因为增量是幂等的**
   `run_layer` 走的是「DELETE 区间 + INSERT」，同一区间重跑结果完全一致
   （由 `check_incremental.py` 验证，含跨 4 个月的宽回看回归）。
   所以这里敢开 `retries=3` —— 幂等性不是口号，是敢重试的前提。

3. **对账是独立的数据质量门禁 task**
   对不上就让 DAG 失败，不允许下游/看板拿到错数据。
   放在最后，能明确区分「跑挂了」和「跑完了但数不对」这两种故障。

⚠️ 关于数据：源 CSV 是静态的（2016-09 ~ 2018-10），**不会有新数据每天到达**。
   直接照真实日期跑，定时任务每天处理的都是"今天"，而这个区间里没有数据 —— 纯空转。

   所以这个 DAG 有个**数据回放模式（默认开启）**：
   把真实日期映射到数据区间里的某一天，让每天的调度都有真实数据可处理，水位线真的在往前推。

       真实日期        实际处理的购买日
       2026-09-15  →  2016-09-04
       2026-09-16  →  2016-09-05
       ...            走到 2018-10-17 后自动绕回开头（循环回放）

   映射是**纯函数**（不依赖任何存储的游标）：同一个 logical date 永远映射到同一天，
   所以重跑那次调度结果一致，幂等性不受影响。

   配置（都在 ~/airflow/airflow.env）：
       OLIST_RUN_MODE=replay           # replay（默认，映射到历史）/ real（用真实日期）
       OLIST_REPLAY_EPOCH=2026-09-15   # 回放起点：真实日期从这天开始算 offset 0
       OLIST_SCHEDULE=0 2 * * *        # 调度周期；演示时想快点推进可以改密一些

   两种用法都支持，互不冲突：
       ① 让它每天自动推进一天      —— 起 scheduler 就行，什么都不用敲
       ② 一次性回补历史（补数场景） —— airflow dags backfill
          ⚠️ 回补时 --end-date 不含当天（调度点是 02:00，而它被解析成 00:00），
             末尾要多给一天，跑完用 dag_run 的 COUNT 核对天数。
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# 项目根目录：调度器的工作目录不一定是项目目录，所以要显式 bootstrap sys.path
PROJECT_ROOT = Path(os.environ.get("OLIST_PROJECT_ROOT", "/opt/etl_data"))
for _p in (str(PROJECT_ROOT / "scripts"), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from airflow import DAG                                    # noqa: E402
from airflow.operators.python import PythonOperator        # noqa: E402

from etl_tasks import (                                    # noqa: E402
    LAYERS, advance_watermark, check_prerequisites, resolve_target,
    run_full_only, run_layer, run_quality_gate, window,
)

LOOKBACK_DAYS = int(os.environ.get("OLIST_LOOKBACK_DAYS", "3"))

# 数据实际跨度（见 docs/数据字典.md 第九节）
DATA_START = datetime(2016, 9, 4)
DATA_END = datetime(2018, 10, 17)

# 回放模式：把「真实日期」映射到「数据区间里的某一天」，见文件顶部说明
RUN_MODE = os.environ.get("OLIST_RUN_MODE", "replay").strip().lower()

# 调度周期。
#   正式演示：0 2 * * *     每天 02:00 推进一天
#   快速演示：*/2 * * * *   想几分钟看完多天推进时改密一些（见 docs/Airflow使用手册.md）
SCHEDULE = os.environ.get("OLIST_SCHEDULE", "0 2 * * *")

# real 模式下数据区间有界，给 end_date；回放模式要一直跑下去（循环回放），不能设
END_DATE = DATA_END if RUN_MODE == "real" else None

default_args = {
    "owner": "wujunye",
    "depends_on_past": False,
    # 幂等 → 敢重试。迟到数据靠 lookback 窗口覆盖，不靠人工补数。
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "execution_timeout": timedelta(minutes=30),
    "email_on_failure": False,        # 没配邮件服务器，告警走 Airflow UI + 日志
}


def _should_replay(context) -> bool:
    """
    本次运行要不要做回放映射。

    只对**定时调度**的运行生效 —— 这是关键，否则手动触发和回补会变得很反直觉：

      · `scheduled__` 定时触发 → **回放**。让每天的调度都有真实数据可处理。
      · `manual__`   手动触发 → **不映射**，`-e` 传哪天就跑哪天。
                                 否则你没法指定"我要跑 2017-11-15 那天"。
      · `backfill__` 回补     → **不映射**，logical date 本身就是数据日期。
                                 否则 --start-date/--end-date 会被映射打乱。

    想手动模拟一次回放（不必等到 02:00），加 conf：
        airflow dags trigger olist_warehouse_daily -e 2026-09-16 -c '{"force_replay": true}'
    """
    dag_run = context.get("dag_run")
    conf = getattr(dag_run, "conf", None) or {}
    if conf.get("force_replay"):
        return True
    if RUN_MODE != "replay":
        return False
    return str(context.get("run_id", "")).startswith("scheduled__")


def _target(**context) -> date:
    """本次运行要处理的购买日（回放模式下会映射到数据区间里的某一天）"""
    real = date.fromisoformat(context["ds"])
    if not _should_replay(context):
        return real
    target = resolve_target(real, mode="replay")
    print(f"[回放模式] logical date {real} → 实际处理的购买日 {target}")
    return target


def task_dwd(**context):
    target = _target(**context)
    start, end = window(target, LOOKBACK_DAYS)
    print(f"DWD 增量：{start} ~ {end}（回看 {LOOKBACK_DAYS} 天）")
    return run_layer("dwd", start, end)


def task_dws(**context):
    target = _target(**context)
    start, end = window(target, LOOKBACK_DAYS)
    print(f"DWS 增量：{start} ~ {end}")
    return run_layer("dws", start, end)


def task_ads(**context):
    target = _target(**context)
    start, end = window(target, LOOKBACK_DAYS)
    print(f"ADS 增量：{start} ~ {end}")
    objects = run_layer("ads", start, end)
    # 三层都成功了才推进水位线 —— 它记录「已处理到哪个购买日」。
    # 放在这里而不是 dwd：DWS/ADS 挂了的话，这次增量还没算完，水位线不该前进。
    advance_watermark(target)
    return objects


def task_precheck():
    """最前面卡一道：增量 SQL 里没有 CREATE，表不在就直接失败，别跑一半才炸"""
    check_prerequisites()
    print("前提表检查通过")


with DAG(
    dag_id="olist_warehouse_daily",
    description="Olist 四层数仓按购买日增量（定时推进 / 回补 两用）",
    doc_md=__doc__,
    start_date=DATA_START,
    end_date=END_DATE,            # 回放模式下为 None：要一直跑下去（循环回放）
    schedule=SCHEDULE,
    catchup=False,                # 不做自动 catchup，回补显式发起
    max_active_runs=1,            # 串行；2C2G 的机器上并发跑增量只会互相抢内存
    default_args=default_args,
    tags=["olist", "warehouse", "etl"],
) as dag:

    precheck = PythonOperator(
        task_id="precheck",
        python_callable=task_precheck,
    )

    dwd = PythonOperator(task_id="dwd_incremental", python_callable=task_dwd)
    dws = PythonOperator(task_id="dws_incremental", python_callable=task_dws)
    ads = PythonOperator(task_id="ads_incremental", python_callable=task_ads)

    # 必须全量的那批：cohort 口径（依赖全部历史）+ 全周期卖家榜 + 视图
    full_only = PythonOperator(
        task_id="ads_full_only",
        python_callable=run_full_only,
        execution_timeout=timedelta(minutes=60),
    )

    # 数据质量门禁：对账不通过就 fail，不让下游拿到错数据
    quality_gate = PythonOperator(
        task_id="quality_gate",
        python_callable=run_quality_gate,
    )

    precheck >> dwd >> dws >> ads >> full_only >> quality_gate
