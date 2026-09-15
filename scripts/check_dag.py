"""
本地校验 DAG 文件能否被导入 —— **不需要装 Airflow**

为什么需要这个
--------------
DAG 在调度器那边导入失败时，症状是：

    调度器活着（active）、`dag` 表 0 行、**不报错**

极难定位 —— 我们这次查了很久才想到去看 `import_error` 表。

本地没装 Airflow，`py_compile` 只能查**语法**，查不出 `NameError` 这类
「语法没问题、但运行时名字未定义」的错误。本项目就踩过一次：
重构时删掉了 `DATA_START` 的定义，但 `start_date=DATA_START` 还在用
→ `NameError` → 调度器扫到 DAG 却跳过它。

做法
----
用**桩模块**顶替 `airflow`，然后真的 `import` 一遍 DAG 文件。
导入会执行模块顶层代码 —— 任何 `ImportError` / `NameError` / 拼写错误都会抛出来。
顺带打印出 DAG 的关键配置，肉眼核对 `schedule` / `start_date` / `end_date`。

用法
----
    python scripts/check_dag.py

⚠️ 改了 `dags/` 下的文件就跑一遍。这个检查比 `py_compile` 强，
   因为它真的执行了导入过程。
"""
import importlib.util
import sys
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAGS_DIR = ROOT / "dags"

for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def install_airflow_stubs():
    """
    造一套最小的假 airflow，够 DAG 模块顶层代码跑完就行。

    只实现 DAG（当上下文管理器）和 PythonOperator（支持 >> 串依赖）。
    这也是"够用就好"——目的是触发导入期的错误，不是模拟 Airflow。
    """
    pkg = types.ModuleType("airflow")
    pkg.__path__ = []                       # 让 airflow.operators 能当子包导入

    class DAG:
        last = None

        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            DAG.last = self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    pkg.DAG = DAG

    operators = types.ModuleType("airflow.operators")
    operators.__path__ = []
    py = types.ModuleType("airflow.operators.python")

    class PythonOperator:
        def __init__(self, *args, **kwargs):
            self.task_id = kwargs.get("task_id")
            self.raw_kwargs = kwargs

        def __rshift__(self, other):
            return other                    # 让 a >> b >> c 能串起来不报错

    py.PythonOperator = PythonOperator
    pkg.operators = operators
    operators.python = py

    sys.modules["airflow"] = pkg
    sys.modules["airflow.operators"] = operators
    sys.modules["airflow.operators.python"] = py
    return DAG, PythonOperator


def check_one(path: Path, DAG, PythonOperator) -> bool:
    """导入一个 DAG 文件；返回是否成功"""
    DAG.last = None
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)             # ← 顶层代码在这里执行
    except Exception:                               # noqa: BLE001
        print(f"  [FAIL] {path.relative_to(ROOT)} —— 导入失败")
        for line in traceback.format_exc().rstrip().splitlines():
            print(f"         {line}")
        print()
        print("         这就是调度器会静默跳过的原因（dag 表 0 行、日志里才有痕迹）。")
        return False

    dag = DAG.last
    if dag is None:
        print(f"  [WARN] {path.relative_to(ROOT)} 导入成功，但没构造出 DAG 对象")
        return True

    kw = dag.kwargs
    print(f"  [OK]   {path.name}")
    for key in ("dag_id", "schedule", "start_date", "end_date", "catchup",
                "max_active_runs"):
        if key in kw:
            print(f"           {key:<16} = {kw[key]}")
    n_tasks = sum(1 for v in vars(module).values() if isinstance(v, PythonOperator))
    print(f"           {'task 数':<16} = {n_tasks}")
    return True


def main() -> int:
    DAG, PythonOperator = install_airflow_stubs()
    files = sorted(DAGS_DIR.glob("*.py"))
    if not files:
        print(f"[FAIL] {DAGS_DIR} 里没有 .py 文件")
        return 1

    print("=" * 72)
    print("DAG 导入检查（用桩模块顶替 airflow，真的执行一遍导入）")
    print("=" * 72)

    failed = [f for f in files if not check_one(f, DAG, PythonOperator)]

    print("=" * 72)
    if failed:
        print(f"[FAIL] {len(failed)} 个 DAG 文件导入失败："
              + ", ".join(str(f.name) for f in failed))
        return 1
    print(f"[OK] {len(files)} 个 DAG 文件全部导入成功")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
