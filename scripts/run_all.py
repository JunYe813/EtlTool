"""
一键全流程：ODS → DWD → DWS → ADS

用法：
    python scripts/run_all.py                # 全量重建
    python scripts/run_all.py --skip-ods     # 跳过 ODS 导入（数据已在库里时更快）
    python scripts/run_all.py --only ads     # 只跑某一层

幂等性：全链路都是 DROP + CREATE + INSERT 全量重建，重复执行结果完全一致。

关于「按购买日增量」：
    大纲里的 `run_all.py --date <某天>` 目前**未实现**，且不能只改 ADS 层。
    原因：01_dwd.sql / 02_dws.sql 都是 DROP TABLE + CREATE 全量重建，
    DWD 一跑就把所有分区重算了，ADS 单独做增量没有意义。
    要做真正的增量，需要 DWD / DWS / ADS 三层一起改为
    「DELETE FROM t WHERE purchase_date BETWEEN :start AND :end」+ 参数化 INSERT。
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sql_runner import get_engine, report_rows, run_sql_file   # noqa: E402

LAYERS = {
    "dwd": ["sql/01_dwd.sql"],
    "dws": ["sql/02_dws.sql"],
}


def run_sql_layer(name: str, files) -> None:
    print(f"\n{'=' * 68}\n{name}\n{'=' * 68}")
    engine = get_engine()
    for rel_path in files:
        objects = run_sql_file(engine, rel_path)
        report_rows(engine, objects)


def run_ods() -> None:
    print(f"\n{'=' * 68}\nODS 导入\n{'=' * 68}")
    # 子进程执行：olist_import.py 自身带「已导入文件跳过」逻辑
    subprocess.run([sys.executable, str(ROOT / "scripts" / "olist_import.py")],
                   cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="电商数仓一键全流程")
    parser.add_argument("--skip-ods", action="store_true", help="跳过 ODS 导入")
    parser.add_argument("--only", choices=["ods", "dwd", "dws", "ads"],
                        help="只跑指定层")
    args = parser.parse_args()

    t0 = time.perf_counter()
    targets = [args.only] if args.only else ["ods", "dwd", "dws", "ads"]

    if "ods" in targets and not args.skip_ods:
        run_ods()
    elif "ods" in targets:
        print("跳过 ODS 导入（--skip-ods）")

    for layer in ("dwd", "dws"):
        if layer in targets:
            run_sql_layer(layer.upper() + " 构建", LAYERS[layer])

    if "ads" in targets:
        # 直接调用模块内的 rebuild()，保证和对账逻辑用的是同一份代码
        from ads_build import rebuild
        code = rebuild()
        if code != 0:
            print(f"\n总耗时 {time.perf_counter() - t0:.2f}s")
            return code

    print(f"\n全流程完成，总耗时 {time.perf_counter() - t0:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
