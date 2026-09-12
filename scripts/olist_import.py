"""
ODS 层导入：data/*.csv → PostgreSQL

用法：
    python scripts/olist_import.py

幂等性：以 imp_file_log 记录已导入文件名，重复执行会跳过已导入文件，
        不会产生重复数据（对应"导入不丢不重"的验收标准）。

注意：本脚本是「首次导入」语义。若要重新导入某张表，
      需先 TRUNCATE 目标表并删除 imp_file_log 里对应记录，
      否则会被判定为"已导入"而跳过。
"""
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DATA_DIR, get_engine      # noqa: E402

# 不导入的文件：地理表 60MB 且业务价值低（数据字典第二节第 8 条建议跳过）
SKIP_FILES = {"olist_geolocation_dataset.csv"}

# 这两张表含葡萄牙语原文（评价正文、卖家城市名），UTF-8 解码会失败，必须用 latin-1
LATIN1_TABLES = {"olist_order_reviews_dataset", "olist_sellers_dataset"}

engine = get_engine()


def get_imported_files() -> set:
    """已导入的文件名集合"""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT file_name FROM imp_file_log")).fetchall()
    return {r[0] for r in rows}


def import_data(file_path: str, table_name: str) -> None:
    """把一个 CSV 追加导入到同名表，并登记到 imp_file_log"""
    encoding = "latin-1" if table_name in LATIN1_TABLES else "utf-8"
    # dtype=str：ODS 层原样保留源数据，不做类型推断，避免前导零/精度丢失
    df = pd.read_csv(file_path, dtype=str, low_memory=False, encoding=encoding)

    # 溯源列：对标 RawXxx 的 DataCopy 思路，便于回溯数据来自哪个文件、哪一批次
    df["import_date"] = datetime.now()
    df["source_file"] = os.path.basename(file_path)

    df.to_sql(table_name, engine, if_exists="append", method="multi",
              chunksize=5000, index=False)
    print(f"[{table_name}] 新增导入 {len(df):,} 行")

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO imp_file_log (file_name, row_count, import_date) "
                 "VALUES (:f, :r, :i)"),
            {"f": os.path.basename(file_path), "r": len(df), "i": datetime.now()},
        )


def main() -> int:
    if not DATA_DIR:
        print("未配置 FILE_PATH，请检查 .env")
        return 1

    imported = get_imported_files()
    for file_name in sorted(os.listdir(DATA_DIR)):
        if not file_name.endswith(".csv") or file_name in SKIP_FILES:
            print(f"跳过（非 CSV 或在跳过清单中）: {file_name}")
            continue
        if file_name in imported:
            print(f"跳过（已导入）: {file_name}")
            continue
        print(f"正在导入: {file_name}")
        import_data(os.path.join(DATA_DIR, file_name), file_name[:-4])
    return 0


if __name__ == "__main__":
    sys.exit(main())
