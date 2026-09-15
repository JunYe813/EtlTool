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
   所以这个 DAG 的日常定时运行是演示性质的，**真正有意义的用法是回补（backfill）**：
   把历史逐日重放一遍，最后指纹与全量重建完全一致 —— 那是对增量设计最强的验收。

   回补（Airflow 2.10 用 `airflow backfill create`；更早版本是 `airflow dags backfill`）：
       airflow dags test olist_warehouse_daily 2017-10-28          # 先单日试跑
       airflow backfill create --dag-id olist_warehouse_daily \
           --from-date 2016-09-04 --to-date 2018-10-17
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
    LAYERS, check_prerequisites, run_full_only, run_layer, run_quality_gate, window,
)

LOOKBACK_DAYS = int(os.environ.get("OLIST_LOOKBACK_DAYS", "3"))

# 数据实际跨度（见 docs/数据字典.md 第九节）。
# end_date 设成最后一天，这样回补不会去跑没有数据的日期。
DATA_START = datetime(2016, 9, 4)
DATA_END = datetime(2018, 10, 17)

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


def _target(**context) -> str:
    """本次运行对应的购买日 = Airflow 的逻辑日期（回补时就是被回补的那天）"""
    return context["ds"]


def task_dwd(**context):
    target = date.fromisoformat(_target(**context))
    start, end = window(target, LOOKBACK_DAYS)
    print(f"DWD 增量：{start} ~ {end}（回看 {LOOKBACK_DAYS} 天）")
    return run_layer("dwd", start, end)


def task_dws(**context):
    target = date.fromisoformat(_target(**context))
    start, end = window(target, LOOKBACK_DAYS)
    print(f"DWS 增量：{start} ~ {end}")
    return run_layer("dws", start, end)


def task_ads(**context):
    target = date.fromisoformat(_target(**context))
    start, end = window(target, LOOKBACK_DAYS)
    print(f"ADS 增量：{start} ~ {end}")
    return run_layer("ads", start, end)


def task_precheck():
    """最前面卡一道：增量 SQL 里没有 CREATE，表不在就直接失败，别跑一半才炸"""
    check_prerequisites()
    print("前提表检查通过")


with DAG(
    dag_id="olist_warehouse_daily",
    description="Olist 四层数仓按购买日增量（回补用）",
    doc_md=__doc__,
    start_date=DATA_START,
    end_date=DATA_END,
    schedule="0 2 * * *",         # 每天 02:00（演示用，数据是静态的）
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
