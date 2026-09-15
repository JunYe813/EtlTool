# Airflow 使用手册（本项目）

配套 `docs/部署到服务器.md`（怎么装）。本篇讲**装完之后怎么用**。

---

## 一、这套东西由什么组成

| 组件 | 位置 / 端口 | 说明 |
|---|---|---|
| scheduler | systemd `airflow-scheduler` | 心跳。到点创建 DAG Run、把 task 派出去 |
| webserver | systemd `airflow-webserver`，**8080** | Web UI |
| 元数据库 | Docker PG `ubuntu-postgres-1`，**127.0.0.1:5434** 的 `airflow` 库 | 存所有运行状态 |
| 环境变量 | `~/airflow/airflow.env` | 两个服务都 `EnvironmentFile` 读它 |
| DAG 目录 | `/opt/etl_data/dags` | 跟着 git 版本管理 |
| 任务日志 | `~/airflow/logs/` | 也在 UI 里能看 |

**两个 venv 别搞混**（这是本项目最容易踩的地方）：

| venv | 装了什么 | 用途 |
|---|---|---|
| `/opt/airflow-venv` | Airflow + psycopg2 + python-dotenv | 跑调度器，**也跑 DAG 里的 task** |
| `/opt/etl_data/venv` | ETL 那 4 个包 | 手工/脚本方式跑 ETL |

> DAG 里的 task 是用 **airflow-venv** 的 Python 执行的。所以 Airflow 环境缺 `python-dotenv`
> 这类依赖时，DAG 会在**解析阶段**就挂，UI 上只看到 DAG 变红。

---

## 二、日常操作：命令行 vs UI

| 想做什么 | 命令行 | UI |
|---|---|---|
| 看 DAG 列表 | `airflow dags list` | 首页 DAGs 列表 |
| 看某次运行的状态 | `airflow dags list-runs -d <dag>` | DAG → Runs |
| 看某个 task 的日志 | 看 `~/airflow/logs/` | **点 task 方块 → Logs** ← 更方便 |
| 暂停 / 恢复 DAG | `airflow dags pause/unpause <dag>` | DAG 列表左侧的开关 |
| **触发一次（今天）** | `airflow dags trigger <dag>` | **Trigger DAG 按钮** |
| **触发一次（指定某天）** | `airflow dags trigger <dag> -e 2017-11-15` | ❌ **UI 做不到** |
| **回补一段历史** | `airflow dags backfill <dag> --start-date ... --end-date ...` | ❌ **UI 没有入口**（Airflow 3 才加） |
| 重跑某天的某几个 task | `airflow tasks clear ...` | DAG → Graph → 选中 task → **Clear** |
| 看 DAG 依赖图 | `airflow dags show <dag>`（需 graphviz） | DAG → **Graph** ← 更好用 |
| 服务启停 | `sudo systemctl restart airflow-scheduler` | ❌ 必须命令行 |

**结论**：

- **日常运维（看状态、看日志、暂停恢复、清任务重跑）UI 完全够用**
- **两件事必须走命令行**：**指定日期触发** 和 **回补** —— 因为 Airflow 2.x 的 UI 不允许指定 logical date，也没有 backfill 入口

---

## 三、命令速查

```bash
# 让环境变量生效（每条命令前建议先跑，或在 ~/.bashrc 里 source 一次）
set -a; source ~/airflow/airflow.env; set +a
```

### 服务

```bash
sudo systemctl status  airflow-scheduler airflow-webserver --no-pager | grep -E "Active|Memory"
sudo systemctl restart airflow-scheduler
sudo systemctl restart airflow-webserver
journalctl -u airflow-scheduler -n 100 --no-pager      # scheduler 日志
```

### DAG

```bash
airflow dags list                                      # 列表（看 is_paused 列）
airflow dags list-import-errors                        # ★ DAG 变红时第一个查这个
airflow dags unpause olist_warehouse_daily             # 恢复调度
airflow dags list-runs -d olist_warehouse_daily        # 历史运行

# 触发指定的某一天（CLI 专有）
airflow dags trigger olist_warehouse_daily -e 2017-11-15

# 回补一段区间
airflow dags backfill olist_warehouse_daily \
    --start-date 2016-09-04 --end-date 2018-10-18
```

### 单独跑/重跑 task

```bash
# 前台单跑一个 task（不进调度，看不到重试）
airflow tasks test olist_warehouse_daily dwd_incremental 2017-11-15

# 清掉某天的 task 状态 → 调度器会重新跑
airflow tasks clear olist_warehouse_daily \
    --start-date 2017-11-15 --end-date 2017-11-15 --yes
```

### 查元数据库

```bash
docker exec -it ubuntu-postgres-1 psql -U postgres -d airflow -c \
  "SELECT COUNT(*) AS runs, MIN(execution_date), MAX(execution_date)
   FROM dag_run WHERE dag_id = 'olist_warehouse_daily';"
```

---

## 四、本项目的约定

```
precheck → dwd_incremental → dws_incremental → ads_incremental → ads_full_only → quality_gate
```

| task | 干什么 | 为什么单独成 task |
|---|---|---|
| `precheck` | 检查 11 张前提表在不在 | 增量 SQL 里没有 CREATE，表不在一开始就该失败 |
| `dwd_incremental` | `sql/load/01_dwd_inc.sql` | 按层拆，失败能单独重试、UI 上能看出卡在哪层 |
| `dws_incremental` | `sql/load/02_dws_inc.sql` | 同上 |
| `ads_incremental` | `sql/load/03_ads_inc.sql` | 同上 |
| `ads_full_only` | cohort 口径表 + 全周期卖家榜 + 视图 | **依赖全部历史**，和「只依赖本区间」性质不同 |
| `quality_gate` | 14 条一致性对账 | 数据质量门禁，不通过就 fail，不让下游拿到错数据 |

**`ds`（logical date）在这个项目里 = 要处理的「购买日」。** 回补某天，就是发起一次 `ds` 等于那天的运行。

数字对照（别记混）：

| | 天数 |
|---|---|
| 有订单的购买日 | **634** |
| `2016-09-04 ~ 2018-10-17` 的日历天 | **774** |

回补按**日历**逐日跑，所以是 774 次，其中约 140 天是空跑（区间内没有数据）。真实流水线每天都要跑，空跑也要走一遍。

---

## 五、定时推进与「数据回放」模式

### 问题：源数据是静态的

CSV 只覆盖 `2016-09-04 ~ 2018-10-17`，**明天不会长出新数据**。
照真实日期跑的话，定时任务每天都在处理"今天"，而这个区间里没有数据 —— **纯空转**。

### 解法：把「真实日期」映射到「数据区间里的某一天」

```
真实日期        实际处理的购买日
2026-09-15  →  2016-09-04
2026-09-16  →  2016-09-05
2026-09-17  →  2016-09-06
...            走到 2018-10-17 后绕回开头（循环回放）
```

于是**每天的调度都有真实数据可处理**，水位线真的在往前推 —— 这才是「按购买日增量」的完整演示。

**映射是纯函数**（不存任何游标）：同一个 logical date 永远映射到同一天，
所以重跑那次调度结果一致，**幂等性不受影响**。有状态的游标会破坏这一点（失败重跑结果会变）。

### 关键设计：回放**只对定时调度生效**

| 触发方式 | run_id 前缀 | 是否回放 | 为什么 |
|---|---|---|---|
| **定时调度** | `scheduled__` | ✅ **回放** | 让每天的自动调度都有真实数据 |
| **手动触发** | `manual__` | ❌ 不映射 | 否则没法指定「我要跑 2017-11-15 那天」 |
| **回补** | `backfill__` | ❌ 不映射 | 否则 `--start-date/--end-date` 会被映射打乱 |

这样两条用法互不干扰：**想跑哪天就传哪天，定时运行自动推进。**

### 配置（都在 `~/airflow/airflow.env`）

```bash
OLIST_RUN_MODE=replay            # replay（默认，映射到历史）/ real（用真实日期）
OLIST_REPLAY_EPOCH=2026-09-15    # 回放起点：真实日期从这天算 offset 0
OLIST_SCHEDULE=0 2 * * *         # 调度周期
```

改完要重启服务（DAG 文件的 env 是启动时读的）：

```bash
sudo systemctl restart airflow-scheduler airflow-webserver
```

### 怎么验证 / 怎么快速演示

**① 手动模拟一次回放**（不用等到 02:00）：

```bash
airflow dags trigger olist_warehouse_daily -e 2026-09-16 -c '{"force_replay": true}'
# 日志里会打印：[回放模式] logical date 2026-09-16 → 实际处理的购买日 2016-09-05
```

**② 想几分钟看完多天推进**，把周期改密：

```bash
# airflow.env
OLIST_SCHEDULE=*/2 * * * *        # 每 2 分钟推进一天
```

⚠️ **别调得比单次运行耗时更短** —— 一次 run 约 6~9 秒（`ads_full_only` 占一半），
配合 `max_active_runs=1`，周期短于运行耗时时任务会排队积压。**30 秒是安全下限。**

**③ 重置回放演示**（让水位线从头开始推进）

水位线用 `GREATEST` 只前进不后退，所以回放从 2016-09-04 重新开始时它可能停在旧值上。
想从零看它推进，就把它手动退回去。

⚠️ **注意连的是数仓库，不是 Airflow 元数据库** —— `etl_watermark` 在 `etl_data` 里（5432，原生 PG），
不在 Docker 那个存 Airflow 状态的库里（5434）：

```bash
PGPASSWORD='你的数仓密码' psql -h 127.0.0.1 -p 5432 -U postgres -d etl_data -c \
  "UPDATE etl_watermark SET last_date = '2016-09-03' WHERE job_name = 'incremental';"
```

（`2016-09-03` 是数据开始日的前一天 —— 退到那天，第一次回放运行就会把它推进到 `2016-09-04`。）

---

## 六、2G 机器的调参（为什么这么配）

都在 `~/airflow/airflow.env`：

| 变量 | 值 | 为什么 |
|---|---|---|
| `AIRFLOW__CORE__EXECUTOR` | `LocalExecutor` | 不需要 Celery/Redis，少两个进程 |
| `AIRFLOW__CORE__PARALLELISM` | `2` | 限制全局并行 task 数 |
| `AIRFLOW__CORE__MAX_ACTIVE_TASKS_PER_DAG` | `1` | 一次只跑一个 task，避免内存尖峰 |
| `AIRFLOW__CORE__MAX_ACTIVE_RUNS_PER_DAG` | `1` | 回补时串行，不并发 |
| `AIRFLOW__WEBSERVER__WORKERS` | `2` | **1 个会被慢请求全堵住**（已实测踩过） |
| `AIRFLOW__WEBSERVER__WEB_SERVER_WORKER_TIMEOUT` | `300` | 默认太短，跑 ETL 时元数据库查询会变慢 |
| `AIRFLOW__CORE__LOAD_EXAMPLES` | `False` | 不加载示例 DAG |

**为什么不开 triggerer**：只有 deferrable operator 才需要它，本项目没有。

**内存体检命令**（回补期间隔一阵看一眼）：

```bash
free -h
```

`available` 别持续掉到 200MB 以下；swap 别一直涨。

---

## 七、排错清单

| 症状 | 先查什么 |
|---|---|
| **DAG 在 UI 上变红 / 不出现** | `airflow dags list-import-errors` ← 十有八九是缺依赖或 DAG 语法错 |
| **触发后一直 `queued`，task 不跑** | DAG 是不是**暂停**状态（`airflow dags list` 看 `is_paused`） |
| **UI 打不开** | ① 服务器上 `curl http://127.0.0.1:8080/home` 通不通<br>② `systemctl status airflow-webserver`<br>③ 都不通 → 是 SSH 隧道/VS Code 转发的问题 |
| **UI 突然假死几秒后恢复** | gunicorn worker 被超时杀掉了 —— 看第四节那个 `WEB_SERVER_WORKER_TIMEOUT` |
| **`password authentication failed`** | `airflow.env` 里的密码和实际不符；用 `psql` 单独验一次 |
| **task 失败但看不到原因** | UI 点那个 task → Logs；或 `journalctl -u airflow-scheduler` |
| **配置改了不生效** | 改 `airflow.env` 后必须 `sudo systemctl restart airflow-scheduler airflow-webserver` |

---

## 八、已知的坑（都实测踩过）

**① `--end-date` 不含当天**

调度点是 `02:00`，而 `--end-date 2017-11-03` 被解析成 `2017-11-03T00:00`，
`11-03T02:00` 那次运行落在边界外 → **静默少一天**。

```bash
# ✗ 只跑了 11-01、11-02
airflow dags backfill <dag> --start-date 2017-11-01 --end-date 2017-11-03
# ✓ 末尾多给一天
airflow dags backfill <dag> --start-date 2017-11-01 --end-date 2017-11-04
```

跑完**一定用 `dag_run` 的 COUNT 核对天数** —— 这个坑不报错、全绿、数仓也对，只是少一天。

**② `airflow dags test` 的结论不能代表真实路径**

`dags test` 把 6 个 task 跑在**同一个进程**里，而真实调度是
`scheduler → LocalExecutor → 子进程`。同一个 task 在 `dags test` 里失败、在真实路径里成功，
本项目中真实发生过。**验证要用 `dags trigger` / `dags backfill`。**

**③ backfill 的 `state success not in running=` WARNING 是噪声**

`backfill_job_runner.py` 的内部状态跟踪和 scheduler 抢着更新 task 状态，每个 task 打一条告警。
不是错误。**判据是 run 的最终状态和 `check_idempotent` 的结果，不是这行告警。**

**④ `--logical-date` 在 2.x 不存在**

`airflow dags trigger` 用的是 `-e/--exec-date`。而 `-e` 在 `dags backfill` 里是 `--end-date` ——
同一个字母两个含义，**建议一律用长参数**。

**⑤ 配置文件里的占位符**

从模板复制配置时，第一件事是 `grep` 一遍有没有没替换的
`你的密码` / `<服务器IP>` / `your_password_here`，而不是直接跑。
跑起来后报的错（如 `password authentication failed`）离根因很远。
