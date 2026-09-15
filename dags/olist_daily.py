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

   配置在 **`replay_config.py`**（项目根目录）—— 写进代码，不走环境变量：
       RUN_MODE              = "replay"              # replay / real
       REPLAY_EPOCH          = "2026-09-15T06:36:00" # 第 0 天对应的真实时刻
       REPLAY_UNIT_SECONDS   = 120                   # 真实时间每多少秒推进一天
       SCHEDULE              = "*/2 * * * *"         # 调度周期（**必须 5 段**）

   为什么不用环境变量：环境变量要经 systemd 的 `EnvironmentFile` 传到调度器进程，
   而 CLI 走的是 shell 环境 —— 两条路径读到的不是一份配置，再加上 `airflow.cfg`
   里还有一份首次生成的旧默认值，就是"三个来源互相覆盖"。
   实测症状：**手动触发能跑通、自动调度不跑**，而且极难查。
   写进代码后改完 `git pull` 就生效（调度器自动重新解析 DAG），不用重启服务。

   ⚠️ 步长和调度周期必须匹配。演示时想快点推进，两个一起改：

       正式：REPLAY_UNIT_SECONDS = 86400  +  SCHEDULE = "0 2 * * *"
       演示：REPLAY_UNIT_SECONDS = 120    +  SCHEDULE = "*/2 * * * *"

   ⚠️⚠️ `SCHEDULE` **必须是 5 段 cron**。写成 6 段（曾误用 `"0 0/2 * * * *"`）
   不会报错，但 croniter 把 6 段当作 `分 时 日 月 周 年`（第 6 段是**年**，不是秒），
   于是它变成「每 2 小时」—— 调度器一切正常，只是几小时才跑一次，极难察觉。
   铁证看元数据库的 `next_dagrun_create_after` 是否落在整点偶数小时。
   详见 `replay_config.py` 的「调度周期」一节。

   只改调度不改步长的话，同一天内的多次运行会算出同一个 offset ——
   反复处理同一天，**看起来像"回放不推进"**。
   跑 `python scripts/demo_replay.py preview` 可以一次验证两者是否匹配。

   两种用法都支持，互不冲突：
       ① 让它按周期自动推进      —— 起 scheduler 就行，什么都不用敲
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

import replay_config                                       # noqa: E402  (项目根目录)
from etl_tasks import (                                    # noqa: E402
    LAYERS, advance_watermark, check_prerequisites, resolve_target,
    run_full_only, run_layer, run_quality_gate, window,
)

LOOKBACK_DAYS = 3

# ---- 回放 / 调度配置全部来自 replay_config.py -------------------------------
# 为什么不从环境变量读：环境变量要经 systemd 的 EnvironmentFile 传到调度器，
# 而 CLI 走的是 shell 环境 —— 两条路径读到的不是一份配置，再叠加 airflow.cfg 里
# 一份首次生成的旧默认值，就是"三个来源互相覆盖"。写进代码没有这个问题，
# 而且改完 git pull 就生效（调度器自动重新解析），不用重启服务。
RUN_MODE = replay_config.RUN_MODE.strip().lower()
SCHEDULE = replay_config.SCHEDULE

# DAG 的起始日期 —— Airflow 用它推第一个调度点。
# 取数据集开始日：回放模式下 logical date 只用来算 offset，起点早一点不影响。
# ⚠️ 必须在这里定义 —— 删掉它会让下面的 `start_date=DATA_START` 抛
#    `NameError`，而 DAG 导入失败的症状是「调度器活着、dag 表 0 行、不报错」，
#    极难定位。改完 DAG 一定跑 `python scripts/check_dag.py`（见那个脚本的说明）。
DATA_START = datetime(2016, 9, 4)

# ⚠️ end_date 固定为 None —— **不要**在这里设 DAG 结束日期。
#    Airflow 一旦认为 DAG 已过 end_date，就再也不创建新 run，表现为
#    `dag.next_dagrun_create_after` 为 NULL、"跑完一次就没后续"，而且不报错。
#    代价：RUN_MODE="real" 时没有数据的日子也会空跑 —— 但空跑是看得见的，
#    静默不再调度是看不见的。宁可空跑。
END_DATE = None

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
    """
    本次运行要处理的购买日（回放模式下会映射到数据区间里的某一天）。

    用 `logical_date`（带时区的 datetime）而不是 `ds`（只有日期）——
    回放步长按**秒**算，需要时间粒度才能支持「2 分钟推一天」这类密集演示调度。

    ⚠️ 回放参数**显式传进去**（来自 replay_config.py），不依赖
    `etl_tasks` 模块级那个环境变量默认值 —— 环境变量在 systemd 那条路径上不可靠。
    """
    logical = context["logical_date"]
    if not _should_replay(context):
        return logical.date()
    target = resolve_target(
        logical,
        mode=RUN_MODE,
        epoch=replay_config.REPLAY_EPOCH,
        unit_seconds=replay_config.REPLAY_UNIT_SECONDS,
    )
    print(f"[回放模式] logical date {logical} → 实际处理的购买日 {target}")
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
