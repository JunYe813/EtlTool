"""
看板冒烟测试：用 Streamlit 官方 AppTest 无头运行每个页面，捕获异常

不需要启动浏览器/服务器，直接执行页面脚本并断言没有异常抛出。
放在 scripts/ 下，改动看板后跑一遍即可确认没写崩。

用法：
    python scripts/check_dashboard.py
"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest      # noqa: E402

ENTRY = ROOT / "app" / "dashboard.py"
PAGES = [
    "pages/sales_overview.py",
    "pages/product_analysis.py",
    "pages/seller_region.py",
    "pages/user_analysis.py",
    "pages/fulfillment.py",
]


def run_page(page_path: str) -> tuple:
    """以 dashboard.py 为入口、切到指定页面运行，返回 (是否通过, 异常文本)"""
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    # 让入口脚本把导航指向目标页面
    at.query_params["page"] = page_path
    at.run()
    if at.exception:
        return False, "\n".join(str(e.value) for e in at.exception)
    return True, ""


def element_count(at, name: str) -> int:
    """统计某类 Streamlit 元素数量；不同版本元素名可能不同，取不到就返回 0"""
    try:
        return len(at.get(name))
    except Exception:                                  # noqa: BLE001
        return 0


def main() -> int:
    print("=" * 68)
    print("看板冒烟测试（Streamlit AppTest，无头运行）")
    print("=" * 68)

    # 入口本身
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    if at.exception:
        print("[FAIL] 入口 dashboard.py")
        for e in at.exception:
            print("       " + str(e.value))
        return 1
    print(f"[OK]   入口 dashboard.py"
          f"（title={at.title[0].value if at.title else '-'}）")

    failed = []
    # AppTest 一次只执行一个脚本；这里逐个把页面文件当独立脚本跑，
    # 以覆盖全部页面代码路径（页面里的 sys.path 兜底保证它们可独立运行）
    for page in PAGES:
        path = ROOT / "app" / page
        at = AppTest.from_file(str(path), default_timeout=120)
        try:
            at.run()
        except Exception:                       # noqa: BLE001
            print(f"[FAIL] {page}")
            print(traceback.format_exc())
            failed.append(page)
            continue
        if at.exception:
            print(f"[FAIL] {page}")
            for e in at.exception:
                print("       " + str(e.value))
            failed.append(page)
        else:
            n_metric = element_count(at, "metric")
            n_chart = element_count(at, "vega_lite_chart")
            n_table = element_count(at, "dataframe")
            print(f"[OK]   {page}"
                  f"  （metric {n_metric} · 图表 {n_chart} · 表格 {n_table}）")

    print("=" * 68)
    if failed:
        print(f"冒烟测试 FAIL：{len(failed)} 个页面异常 -> {', '.join(failed)}")
        return 1
    print(f"冒烟测试 PASS：入口 + {len(PAGES)} 个页面全部无异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
