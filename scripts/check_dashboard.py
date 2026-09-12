"""
看板冒烟测试：用 Streamlit 官方 AppTest 无头运行每个页面，捕获异常

不需要启动浏览器/服务器，直接执行页面脚本并断言没有异常抛出。
放在 scripts/ 下，改动看板后跑一遍即可确认没写崩。

两个阶段：
  阶段 1 · 默认筛选 —— 入口 + 5 个页面各跑一遍
  阶段 2 · 带筛选条件 —— 只对吃维度筛选的页面跑多个场景

为什么要有阶段 2：
    全局筛选器为空时，dim_where() 返回的 SQL 片段是空字符串，
    那段拼 SQL 的代码路径根本不会执行 —— 于是"漏传绑定参数"
    （A value is required for bind parameter 'xxx'）这类 bug
    在默认筛选下完全正常，冒烟测试也全过，只有用户真去点筛选器才炸。
    阶段 2 主动注入筛选条件，把这条路径覆盖上。

用法：
    python scripts/check_dashboard.py
"""
import sys
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text                     # noqa: E402
from streamlit.testing.v1 import AppTest        # noqa: E402

from config import get_engine                   # noqa: E402

ENTRY = ROOT / "app" / "dashboard.py"
PAGES = [
    "pages/sales_overview.py",
    "pages/product_analysis.py",
    "pages/seller_region.py",
    "pages/user_analysis.py",
    "pages/fulfillment.py",
]

# 只有这两个页面调用了 dim_where()，会拼 {cat_where} / {state_where}
# （其余 3 个页面只用 date_params，筛选维度对它们无影响）
FILTER_PAGES = [
    "pages/product_analysis.py",     # dim_where(f, "category_name", "cats")
    "pages/seller_region.py",        # dim_where(f, "seller_state", "states")
]


def element_count(at, name: str) -> int:
    """统计某类 Streamlit 元素数量；不同版本元素名可能不同，取不到就返回 0"""
    try:
        return len(at.get(name))
    except Exception:                                  # noqa: BLE001
        return 0


def describe(at) -> str:
    return (f"metric {element_count(at, 'metric')}"
            f" · 图表 {element_count(at, 'vega_lite_chart')}"
            f" · 表格 {element_count(at, 'dataframe')}")


def run_page(page_path: str, filters: dict = None) -> tuple:
    """
    运行一个页面，返回 (是否通过, 异常文本)。

    filters 为 None 时走页面自己的兜底（相当于默认筛选）；
    否则注入 st.session_state["filters"]，模拟用户在侧边栏做了筛选。
    """
    at = AppTest.from_file(str(ROOT / "app" / page_path), default_timeout=120)
    if filters is not None:
        at.session_state["filters"] = filters
    at.run()
    if at.exception:
        return False, "\n".join(str(e.value) for e in at.exception), at
    return True, "", at


def sample_filter_values():
    """
    从库里取真实的类目/卖家州作为筛选值。

    不硬编码，避免哪天类目名变了测试静默失效（拿不到就退化成空筛选，不会误报失败）。
    """
    engine = get_engine()
    with engine.connect() as conn:
        # 必须 DISTINCT：这两张视图的粒度是「日 × 维度」，不加会取到同一个值的多天
        cats = conn.execute(text(
            "SELECT DISTINCT category_name FROM v_sale_daily_category "
            "WHERE category_name <> 'unknown' ORDER BY 1 LIMIT 2"
        )).scalars().all()
        states = conn.execute(text(
            "SELECT DISTINCT seller_state FROM v_sale_daily_seller_state "
            "WHERE seller_state <> 'unknown' ORDER BY 1 LIMIT 1"
        )).scalars().all()
        d1, d2 = conn.execute(text(
            "SELECT MIN(purchase_date), MAX(purchase_date) FROM ads_sale_overview_daily"
        )).one()
    return list(cats), list(states), d1, d2


def build_scenarios(cats, states, d1, d2):
    """构造筛选场景：每个场景是一套完整的 filters dict"""
    def mk(c, s):
        return {"d1": d1, "d2": d2, "cats": list(c), "states": list(s), "min_cohort": 20}

    scenarios = [("无筛选", mk([], []))]
    if cats:
        scenarios.append((f"选 1 个类目（{cats[0]}）", mk(cats[:1], [])))
    if len(cats) > 1:
        scenarios.append((f"选 2 个类目", mk(cats[:2], [])))
    if states:
        scenarios.append((f"选 1 个卖家州（{states[0]}）", mk([], states[:1])))
    if cats and states:
        scenarios.append(("类目 + 州 组合", mk(cats[:1], states[:1])))
    return scenarios


def main() -> int:
    print("=" * 74)
    print("看板冒烟测试（Streamlit AppTest，无头运行）")
    print("=" * 74)

    failed = []

    # ---------------- 阶段 1：默认筛选 ----------------
    print("\n[阶段 1] 默认筛选")

    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    if at.exception:
        print("[FAIL] 入口 dashboard.py")
        for e in at.exception:
            print("       " + str(e.value))
        return 1
    print(f"[OK]   入口 dashboard.py"
          f"（title={at.title[0].value if at.title else '-'}）")

    for page in PAGES:
        try:
            ok, err, at = run_page(page)
        except Exception:                          # noqa: BLE001
            print(f"[FAIL] {page}")
            print(traceback.format_exc())
            failed.append(page)
            continue
        if ok:
            print(f"[OK]   {page}  （{describe(at)}）")
        else:
            print(f"[FAIL] {page}")
            for line in err.splitlines():
                print("       " + line[:130])
            failed.append(page)

    # ---------------- 阶段 2：带筛选条件 ----------------
    print("\n[阶段 2] 带筛选条件（覆盖 dim_where 拼 SQL 的代码路径）")
    try:
        cats, states, d1, d2 = sample_filter_values()
    except Exception as ex:                        # noqa: BLE001
        print(f"[SKIP] 取筛选值失败，跳过阶段 2：{type(ex).__name__}: {ex}")
        cats = states = []
        d1 = d2 = None

    if d1 and d2:
        scenarios = build_scenarios(cats, states, d1, d2)
        print(f"       筛选值取自数据库：类目={cats}  卖家州={states}")
        for page in FILTER_PAGES:
            for label, filt in scenarios:
                try:
                    ok, err, at = run_page(page, filt)
                except Exception:                  # noqa: BLE001
                    print(f"[FAIL] {page} · {label}")
                    print(traceback.format_exc())
                    failed.append(f"{page}[{label}]")
                    continue
                if ok:
                    print(f"[OK]   {page:<28} · {label:<22} （{describe(at)}）")
                else:
                    print(f"[FAIL] {page:<28} · {label}")
                    for line in err.splitlines():
                        print("       " + line[:130])
                    failed.append(f"{page}[{label}]")
        n_scenarios = len(scenarios)
    else:
        n_scenarios = 0

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 74)
    if failed:
        print(f"冒烟测试 FAIL：{len(failed)} 项异常")
        for f in failed:
            print(f"   - {f}")
        return 1
    print(f"冒烟测试 PASS：入口 + {len(PAGES)} 个页面"
          + (f" + {len(FILTER_PAGES)} 个筛选页面 × {n_scenarios} 个场景" if n_scenarios else "")
          + " 全部无异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
