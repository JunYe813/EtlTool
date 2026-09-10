"""
全局配置：数据库连接 + 数仓口径常量

设计原则：**口径唯一来源**。
所有脚本和看板都从这里取连接和口径定义，避免同一条口径在 5 个文件里各写一遍、
改的时候漏掉一处导致看板数字和 SQL 对不上。

口径定义见 docs/数据字典.md 第五节。
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

# ---------------- 路径 ----------------
ROOT = Path(__file__).resolve().parent
SQL_DIR = ROOT / "sql"
SCRIPTS_DIR = ROOT / "scripts"
APP_DIR = ROOT / "app"

load_dotenv(ROOT / ".env")

# ---------------- 数据库连接 ----------------
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DB_URL = (
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# 原始 CSV 目录（ODS 导入用）
DATA_DIR = os.getenv("FILE_PATH")


# ================= 业务口径（改这里即全局生效）=================

# 有效订单：已完成支付的订单状态集合
# 排除 created(未支付) / canceled(取消) / unavailable(不可用)
VALID_STATUSES = ("processing", "invoiced", "approved", "shipped", "delivered")

# 与上面等价的 SQL 片段（供需要拼 SQL 的场景使用）
VALID_STATUS_SQL = "('processing','invoiced','approved','shipped','delivered')"

# 留存观察窗口（天）。N 日留存 = 首购后第 1~N 日内再次购买的买家占比
RETENTION_WINDOWS = (7, 30, 60, 90)

# 商品日榜 TopN
TOP_N = 10

# 复购定义：统计期内购买次数 >= 该值
REPEAT_MIN_ORDERS = 2

# 看板小样本保护：cohort / 分组规模小于该值不展示，避免 1/1=100% 这类噪声
MIN_GROUP_SIZE = 20


def get_engine() -> Engine:
    """
    统一的数据库引擎入口。

    pool_pre_ping: 远程库连接被中间设备回收后自动重连，避免看板报
                   "server closed the connection unexpectedly"
    pool_recycle:  30 分钟主动回收，绕过云厂商的空闲连接超时
    """
    return create_engine(DB_URL, pool_pre_ping=True, pool_recycle=1800)
