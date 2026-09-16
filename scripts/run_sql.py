"""
通用 SQL 脚本执行器

用法：
    # 直接指定文件
    python scripts/run_sql.py sql/01_dwd.sql sql/02_dws.sql

    # 用预置步骤名（避免手敲长路径）
    python scripts/run_sql.py --steps dwd dws
"""
import argparse
import sys

from sql_runner import drop_all_views, get_engine, report_rows, run_sql_file

# 预置步骤：步骤名 → SQL 文件
#
# ⚠️ 这几个清单**不再手抄**，而是引用各自的唯一定义处：
#     · ODS            → 只有 00_ods.sql（没有别处定义，这里写死）
#     · DWD / DWS 全量 → run_all.LAYERS
#     · ADS 完整清单   → ads_build.ADS_FILES（增量组 + 必须全量组 + 视图，顺序已按依赖排好）
#
# 为什么改掉手抄：旧版本漏了两个文件 ——
#     `--steps dwd` 缺 sql/04_dwd_payment.sql
#     `--steps ads` 缺 sql/05_ads_payment_reconcile.sql
# 后果是**静默少建两张表**：不报错、不提示，只是后面的增量 SQL 找不到表、
# 或者看板支付对账页空白。这与 run_all / etl_tasks 之间「清单只有一份」是同一条原则。
from ads_build import ADS_FILES                     # noqa: E402
from run_all import LAYERS                          # noqa: E402

STEPS = {
    "ods": ["sql/00_ods.sql"],
    "dwd": LAYERS["dwd"],
    "dws": LAYERS["dws"],
    "ads": ADS_FILES,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="执行 SQL 脚本")
    parser.add_argument("files", nargs="*", help="SQL 文件路径（相对项目根目录）")
    parser.add_argument("--steps", nargs="+", choices=sorted(STEPS),
                        help="按预置步骤名执行")
    args = parser.parse_args()

    targets = list(args.files)
    for step in (args.steps or []):
        targets.extend(STEPS[step])

    if not targets:
        parser.print_help()
        return 1

    engine = get_engine()

    # 走 ads 步骤时同样要先删视图，否则重建 ads_* 表会因视图依赖失败
    if "ads" in (args.steps or []) or any("04_views" in f for f in targets):
        removed = drop_all_views(engine)
        print(f"  [OK] 清理旧视图 {len(removed)} 个")

    for rel_path in targets:
        objects = run_sql_file(engine, rel_path)
        report_rows(engine, objects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
