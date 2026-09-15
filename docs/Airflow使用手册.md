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

> ⚠️ **命令一律用绝对路径 `/opt/airflow-venv/bin/airflow`** —— venv 激活只对当前 shell 有效，
> 重连或新开终端就没了，裸 `airflow` 会报 `command not found`。

```bash
# ★★ 改了 dags/ 下的文件，**先在本地**跑这个（不需要装 Airflow）
python scripts/check_dag.py

# 服务器上
/opt/airflow-venv/bin/airflow dags list                  # 列表（看 is_paused 列）
/opt/airflow-venv/bin/airflow dags list-import-errors    # ★ DAG 变红/不出现时第一个查这个
/opt/airflow-venv/bin/airflow dags unpause <dag_id>
/opt/airflow-venv/bin/airflow dags list-runs -d olist_warehouse_daily

# 触发指定的某一天（CLI 专有）
/opt/airflow-venv/bin/airflow dags trigger olist_warehouse_daily -e 2017-11-15

# 回补一段区间
/opt/airflow-venv/bin/airflow dags backfill olist_warehouse_daily \
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

### 配置（都在项目根目录的 `replay_config.py`）

**⚠️ 回放参数只有这一个来源。** 早期版本读的是 `~/airflow/airflow.env` 里的
`OLIST_RUN_MODE` / `OLIST_REPLAY_EPOCH` / `OLIST_REPLAY_UNIT_SECONDS` / `OLIST_SCHEDULE`，
但 DAG 走的是 `replay_config.py` —— **同一个参数两个来源**，于是出现「手动跑是 A 日、
调度跑是 B 日」，极难排查。现在那几个环境变量**已经没有任何代码在读它们了**，
设了也不生效（`airflow.env` 里若还留着，直接删掉即可）。

```python
# replay_config.py
RUN_MODE            = "replay"              # replay（映射到历史）/ real（用真实日期）
REPLAY_EPOCH        = "2026-09-15T06:36:00" # 回放起点，建议精确到秒
REPLAY_UNIT_SECONDS = 86400                 # 步长：真实时间每过多少秒 = 数据时间推进一天
SCHEDULE            = "0 2 * * *"           # 调度周期
```

改完**不用重启服务** —— `git pull` 后调度器会自动重新解析 DAG：

```bash
cd /opt/etl_data && git pull
```

（`SCHEDULE` / `end_date` 必须在 DAG **解析期**确定，而解析期查库是 Airflow 不推荐的做法，
所以这些值放在代码里、跟着 git 版本管理，是刻意的设计。）

### 怎么验证 / 怎么快速演示

**① 手动模拟一次回放**（不用等到 02:00）：

```bash
airflow dags trigger olist_warehouse_daily -e 2026-09-16 -c '{"force_replay": true}'
# 日志里会打印：[回放模式] logical date 2026-09-16 → 实际处理的购买日 2016-09-05
```

**② 想几分钟看完多天推进** —— ⚠️ **步长和调度周期必须一起改**

```python
# replay_config.py 里同时改这两行
REPLAY_UNIT_SECONDS = 120        # 每 2 分钟推一天
SCHEDULE            = "0 0/2 * * * *"   # 每 2 分钟跑一次
```

| 场景 | `REPLAY_UNIT_SECONDS` | `SCHEDULE` |
|---|---|---|
| 正式演示 | `86400` | `"0 2 * * *"` |
| 快速演示 | `120` | `"0 0/2 * * * *"` |
| 极速演示 | `30` | `"* * * * *"`（每分钟，约 43 秒一步） |

> `SCHEDULE` 用的是 **6 段 cron（第一段是秒）**：`"0 0/2 * * * *"` = 每分钟的偶数分整点。
> 写成 5 段的 `"*/2 * * * *"` 是「每 2 分钟」的另一种写法，Airflow 也收，但含义不如 6 段直白。

**只改调度、不改步长会怎样**：步长还是 `86400` 时，同一天内的多次运行会算出**同一个 offset**
→ 反复处理同一天，**看起来像"回放不推进"**。反过来步长比周期小太多，会一天跳好几步。
跑 `python scripts/demo_replay.py preview` 可以一次验证两者是否配对。

**只改调度、不改步长会怎样**：步长还是 `86400` 时，同一天内的多次运行会算出**同一个 offset**
→ 反复处理同一天，**看起来像"回放不推进"**。反过来步长比周期小太多，会一天跳好几步。

⚠️ **别把周期调得比单次运行耗时更短** —— 一次 run 约 6~9 秒（`ads_full_only` 占一半），
配合 `max_active_runs=1`，周期短于运行耗时时任务会排队积压。**30 秒是安全下限。**

### ⚠️ 一个必须搞清的问题：回放**不会**让数据"从无到有"

全量重建之后，数仓里**已经是完整数据**了。此时跑回放，每个 run 做的是
`DELETE` 掉那几天的行 + `INSERT` 一模一样的行 —— **净变化为零**。

所以回放演示的是：

| | 演示的是 |
|---|---|
| ✅ | **流水线每天稳定运转** + 每次覆盖后指纹不变（幂等性的现场证明） |
| ❌ | ~~数据从无到有地累积~~ |

**想看到"数据一天天堆起来"，必须先清空数仓。** 用现成的工具：

```bash
python scripts/demo_replay.py status      # 看当前进度（覆盖天数、水位线、完整度）
python scripts/demo_replay.py reset --yes # 清空 DWD/DWS/ADS + 重置水位线
python scripts/demo_replay.py restore     # 全量重建，恢复完整数据
```

**★ 安全性**：`reset` **只清 DWD/DWS/ADS，ODS 源数据一行不动**。
而 DWD 是从 ODS 推出来的，所以 `restore` 能在 10 秒内恢复全量。
**「清空」是一次可撤销的实验，不是有风险的操作。**

清空后的期望：

| 时间 | 数仓里 |
|---|---|
| 第 1 个 run 后 | `2016-09-04` 一天 |
| 第 10 个 run 后 | 约 13 天（回看窗口也覆盖了前面的日子） |
| … | 一天天累积，水位线跟着推进，**看板上的 GMV/订单数一天天变多** |

> **现实提醒**：从零走到全量要 **774 个 run** ——
> 每天一天 = 约 2 年；2 分钟一天 = 约 26 小时。
> **所以别期望回放到全量**，看前 20~50 天的累积过程就足够说明机制了。

### ③ 重置水位线（低层做法）

`demo_replay.py reset` 已经包含这一步，一般不用手动做。需要单独重置时：

水位线用 `GREATEST` 只前进不后退，所以回放从 2016-09-04 重新开始时它可能停在旧值上。

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
| **DAG 在 UI 上变红 / 不出现** | `/opt/airflow-venv/bin/airflow dags list-import-errors`，**或查 `import_error` 表**（见下） |
| **调度器活着，但 `dag` 表 0 行、不报错** | 见下面的「⑨ 最阴的一种失败」 |
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

**⑥ `airflow.env` 里含空格的值必须加引号**

> **现状**：回放参数（含 `SCHEDULE`）已经搬到 `replay_config.py`，
> `airflow.env` 里**不再需要**任何带空格的值，所以这一条对回放配置已经不适用了。
> 留着是因为它记录了一个真实的教训 —— 而且 `airflow.env` 里若将来再加带空格的值，
> 同样会踩。**这个坑本身也是回放配置搬家的原因之一。**

`airflow.env` 会被**两种解析器**读，规则不一样：

| 谁读 | `SOME_VAR=0 2 * * *` 的处理 |
|---|---|
| **systemd**（`EnvironmentFile=`） | ✅ 正确 —— 取 `=` 之后的**全部内容**当值 |
| **bash**（`source ~/airflow/airflow.env`） | ❌ **空格是分隔符** —— 值只剩 `0`，然后把 `2` 当命令执行 |

实测症状：`echo $VAR` 是**空的**，终端还会冒一行 `2: command not found`。
更坑的是 **systemd 那边其实是对的**，所以 scheduler 行为正常，
只有你在命令行 `source` 之后做 `airflow dags trigger` 之类的操作时才会异常。

```bash
# ✗ 值被截断
SOME_VAR=0 2 * * *
# ✓ 两边都能正确解析（systemd 会剥掉引号）
SOME_VAR='0 2 * * *'
```

**规则：值里有空格就加引号。** cron 表达式、带空格的路径都属于这类。

**这条坑的真正教训**：同一份配置被两条路径用两种规则读取时，**故障会只在一半场景下出现**，
而且症状离根因极远。正确解法不是"记得加引号"，而是**让配置只有一个来源** ——
所以回放参数现在放在 `replay_config.py`，systemd 和命令行读的是同一份 Python 代码。

**⑦ 手动触发不要用「未来的 logical date」—— 会永远卡在 queued**

```bash
airflow dags trigger <dag> -e 2026-09-16     # 若 2026-09-16 还没到
```

**不会报错**，UI 上也显示"触发成功"，但 run 会永远停在 `queued`，
`Start Date` 空着、`Duration 0s`，task 的 `state` 全是空。

**原因**：Airflow 不调度 logical date 还在未来的 DagRun —— 那个"数据周期"逻辑上还没结束。
（对定时调度不成问题，因为 logical date 必然在过去；只有手动 `-e` 能指定未来时间。）

**判据（实测过 5 个数据点的规律）**：

| logical date | 相对现在 | 结果 |
|---|---|---|
| 2017-11-01 / 2017-11-02 / 2017-10-28 | 过去 | ✅ success |
| 2026-09-14T02:00（scheduled） | 过去 | ✅ success |
| **2026-09-16T00:00** | **未来** | ❌ **queued 不动** |
| 2026-09-15T00:00 | 已过去 | ✅ success |

**注意时区换算**：你本地是 UTC+8。`-e 2026-09-16` 生成的 logical date 是
`2026-09-16T00:00:00+00:00` = 本地 **9-16 早上 8 点** —— 所以"明天"这种写法很容易踩。

**这类坑的共性**（和 ① `--end-date` 一样）：**不报错、只静默**。
所以每次手动触发后，**养成看一眼 run 状态的习惯**，不要看到"触发成功"就走。

**⑧ 新建的 DAG 默认是暂停的**

`dags_are_paused_at_creation` 默认 `True` → **新建 DAG 一律暂停**。

症状和 ⑦ 一模一样：UI 上 DAG 看得见、能手动触发，但 run 永远 `queued`。

```bash
airflow dags unpause <dag_id>       # 或 UI 列表左侧的开关
```

**建议**：`git pull` 拉到新 DAG 之后，顺手跑一次 `airflow dags list` 看 `is_paused` 那一列。

**⑨ 最阴的一种失败：DAG 导入失败，但什么都不报**

症状：**调度器 `active`、`dag` 表 0 行、日志里也没有明显报错。**

原因：DAG 文件在**导入期**抛异常（`NameError` / `ImportError` / 模块级拼写错误），
调度器扫到它 → 导入失败 → 跳过。它不会让调度器崩，所以看起来"一切正常"。

```bash
# ★ 查这张表 —— Airflow 把导入失败的原因记在这里
docker exec -it ubuntu-postgres-1 psql -U postgres -d airflow -c \
  "SELECT filename, LEFT(stacktrace, 600) FROM import_error;"
```

**本项目踩过一次**：重构时删掉了 `DATA_START` 的定义，但 `start_date=DATA_START` 还在用
→ `NameError` → 调度器扫到 DAG 却跳过 → 查了很久。

**本地预防**（不需要装 Airflow）：

```bash
python scripts/check_dag.py
```

它用桩模块顶替 `airflow`，**真的 import 一遍** DAG 文件 —— 任何导入期错误都会抛出来，
顺带打印 `schedule` / `start_date` / `end_date` 供肉眼核对。

> **`py_compile` 不够用** —— 它只查语法，查不出 `NameError` 这类
> "语法没问题、但运行时名字未定义"的错误。**改了 `dags/` 下的文件就跑 `check_dag.py`。**
