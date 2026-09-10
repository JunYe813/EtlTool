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
STEPS = {
    "ods": ["sql/00_ods.sql"],
    "dwd": ["sql/01_dwd.sql"],
    "dws": ["sql/02_dws.sql"],
    "ads": [
        "sql/03_ads_sale_overview.sql",
        "sql/03_ads_top_product.sql",
        "sql/03_ads_top_seller.sql",
        "sql/03_ads_user_retention.sql",
        "sql/03_ads_user_repeat.sql",
        "sql/03_ads_fulfillment.sql",
        "sql/04_views.sql",
    ],
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
