"""
SQL 脚本执行的公共逻辑

被 run_sql.py / ads_build.py / run_all.py / check_ads.py 复用，
避免连接和执行的样板代码在 4 个脚本里各写一遍。
"""
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，遇到编码不了的字符会直接 UnicodeEncodeError 打断构建流程。
# errors="replace" 让输出永不致命（中文在 GBK 下正常显示，个别字符降级为 ?）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

from sqlalchemy import text                      # noqa: E402
from sqlalchemy.engine import Engine             # noqa: E402

from config import get_engine                    # noqa: E402,F401  (重新导出给调用方)

# 从脚本里抓 CREATE TABLE / CREATE VIEW 的对象名，用于跑完打印行数
_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)",
    re.I,
)


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def execute_script(engine: Engine, sql: str) -> None:
    """
    在一个事务里原样执行一段（可含多条语句的）SQL。

    为什么用原生 DBAPI 游标，而不是 text() 或 exec_driver_sql()：
      - text() 会把 SQL 里的冒号当绑定参数解析。本项目脚本大量使用 ::NUMERIC
        / ::date 显式转型，交给它解析容易踩坑。
      - exec_driver_sql() 在语句含**字面量 %** 时会出问题：04_views.sql 里算
        GMV 占比用到 `SUM(gmv) * 100.0 / ...`，SQLAlchemy 会把 C 扩展的
        immutabledict 作为参数透传给 psycopg2，而它不是 dict/sequence，
        直接抛 `TypeError: immutabledict is not a sequence`。
      - 原生游标完全不经过 SQLAlchemy 的参数机制，多语句脚本可原样执行，
        和用 psql 跑 .sql 文件的行为一致。

    为什么用 engine.begin() 而不是 connect()：
      事务包裹，异常自动回滚。PostgreSQL 的 DDL 是事务性的，
      半路失败不会留下"表建了但没数据"的中间态。
    """
    with engine.begin() as conn:
        raw_conn = conn.connection.driver_connection      # 底层 psycopg2 连接
        with raw_conn.cursor() as cur:
            cur.execute(sql)


def run_sql_file(engine: Engine, rel_path) -> list:
    """执行一个 SQL 文件（支持多语句脚本），返回文件内创建的表/视图名列表"""
    path = Path(rel_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"SQL 文件不存在: {path}")

    sql = path.read_text(encoding="utf-8")
    t0 = time.perf_counter()
    execute_script(engine, sql)
    print(f"  [OK] {_display(path)}  ({time.perf_counter() - t0:.2f}s)")

    objects, seen = [], set()
    for name in _CREATE_RE.findall(sql):
        if name.lower() not in seen:
            seen.add(name.lower())
            objects.append(name)
    return objects


def drop_all_views(engine: Engine) -> list:
    """
    删除 public schema 下所有视图，返回被删的视图名。

    为什么在重建 ADS 之前主动调用（虽然 SQL 文件里已经用了 DROP TABLE ... CASCADE）：
      1) 显式优于隐式：构建日志能直接看到"派生视图已失效"，而不是靠 CASCADE 悄悄带走。
      2) 支持视图演进：04_views.sql 用的是 CREATE OR REPLACE VIEW，
         一旦改了视图的列名/列类型，PG 会拒绝替换并报
         `cannot change name of view column` / `cannot drop columns from view`。
         先删后建就不受这个限制，视图定义可以自由重构。

    动态从 pg_views 读取而不是硬编码列表：以后在 04_views.sql 里加视图，
    这里不用同步改，不会因为漏了一行而破坏幂等性。
    """
    names = [r[0] for r in fetch_all(engine, """
        SELECT viewname FROM pg_views
        WHERE schemaname = 'public'
        ORDER BY viewname
    """)]
    if names:
        # CASCADE 处理视图之间的依赖；视图不存数据，删掉零成本
        execute_script(engine, "\n".join(
            f'DROP VIEW IF EXISTS "{n}" CASCADE;' for n in names
        ))
    return names


def scalar(engine: Engine, sql: str):
    """执行单值查询"""
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def fetch_all(engine: Engine, sql: str):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def count_rows(engine: Engine, name: str) -> int:
    """对象行数；视图（含 UNION ALL 的）同样可查"""
    return scalar(engine, f'SELECT COUNT(*) FROM "{name}"')


def report_rows(engine: Engine, objects) -> None:
    for name in objects:
        try:
            print(f"       - {name}: {count_rows(engine, name):,} 行")
        except Exception as exc:                       # noqa: BLE001
            print(f"       - {name}: 行数读取失败 ({type(exc).__name__})")


def check_consistency(engine: Engine, label: str, sqls) -> bool:
    """
    一致性校验：把多条 SQL 的结果互相比对（不硬编码期望值）。

    硬编码 13494400.74 这类常量在数据变动后会失效；
    比对"同一口径的不同算法是否互相吻合"才是真正有意义的对账。
    """
    values = [scalar(engine, s) for s in sqls]
    ok = len({str(v) for v in values}) == 1
    mark = "[OK]  " if ok else "[FAIL]"
    joined = " == ".join(str(v) for v in values)
    print(f"  {mark} {label}: {joined}")
    return ok
