-- ====== ADS：用户复购 ======
-- 口径（依据 docs/数据字典.md 第五节）：
--   复购率 = 统计期内购买 >= 2 次的买家数 ÷ 总购买买家数
--   买家一律按 customer_unique_id 去重（customer_id 会重复，不等于唯一买家）
--   订单口径 = 有效订单（is_valid）
--
-- 拆成两张表：
--   ads_user_repeat_overall —— 全周期单值，给看板 metric 卡片
--   ads_user_repeat_monthly —— 按「首购月」分组，给复购趋势线

-- ---------------------------------------------------------------
-- ① 全周期复购率（单行）
-- 实测：buyer_cnt=94986, repeat_buyer_cnt=2887, repeat_rate=0.0304
-- ---------------------------------------------------------------
DROP TABLE IF EXISTS ads_user_repeat_overall CASCADE;

CREATE TABLE ads_user_repeat_overall(
    buyer_cnt        int,
    repeat_buyer_cnt int,
    repeat_rate      NUMERIC(10,4),
    orders_per_buyer NUMERIC(10,4),
    create_date      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_date    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO ads_user_repeat_overall
(buyer_cnt, repeat_buyer_cnt, repeat_rate, orders_per_buyer)
SELECT
    COUNT(*)                                                      AS buyer_cnt,
    COUNT(*) FILTER (WHERE order_cnt >= 2)                        AS repeat_buyer_cnt,
    ROUND(COUNT(*) FILTER (WHERE order_cnt >= 2)::NUMERIC
          / NULLIF(COUNT(*), 0), 4)                               AS repeat_rate,
    ROUND(SUM(order_cnt)::NUMERIC / NULLIF(COUNT(*), 0), 4)       AS orders_per_buyer
FROM (
    SELECT customer_unique_id, COUNT(*) AS order_cnt
    FROM dwd_order
    WHERE is_valid
    GROUP BY customer_unique_id
) x;


-- ---------------------------------------------------------------
-- ② 按首购月分组的复购率（趋势）
-- 首购月 = 该买家最早有效购买日所在月份，即 cohort 分组
--
-- ⚠️ 右删失：最后 1~2 个月首购的买家没有足够时间复购，复购率必然偏低，
--    不代表业务变差。看板应过滤或标注首购月 <= 数据最大购买日 - 90 天的 cohort。
-- ---------------------------------------------------------------
DROP TABLE IF EXISTS ads_user_repeat_monthly CASCADE;

CREATE TABLE ads_user_repeat_monthly(
    first_month      date PRIMARY KEY,          -- 首购月首日
    buyer_cnt        int,
    repeat_buyer_cnt int,
    repeat_rate      NUMERIC(10,4),
    create_date      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_date    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO ads_user_repeat_monthly
(first_month, buyer_cnt, repeat_buyer_cnt, repeat_rate)
SELECT
    DATE_TRUNC('month', first_date)::date                         AS first_month,
    COUNT(*)                                                      AS buyer_cnt,
    COUNT(*) FILTER (WHERE order_cnt >= 2)                        AS repeat_buyer_cnt,
    ROUND(COUNT(*) FILTER (WHERE order_cnt >= 2)::NUMERIC
          / NULLIF(COUNT(*), 0), 4)                               AS repeat_rate
FROM (
    SELECT customer_unique_id,
           MIN(purchase_at::date) AS first_date,
           COUNT(*)               AS order_cnt
    FROM dwd_order
    WHERE is_valid
    GROUP BY customer_unique_id
) x
GROUP BY DATE_TRUNC('month', first_date)::date;


-- ============================== 校验 ==============================
-- ① 两表买家数必须相等（实测 94986），不等说明分组口径不一致
-- SELECT buyer_cnt FROM ads_user_repeat_overall;
-- SELECT SUM(buyer_cnt) FROM ads_user_repeat_monthly;

-- ② 复购率（实测 0.0304）
-- SELECT * FROM ads_user_repeat_overall;

-- ③ 复购趋势（看是否存在明显的右删失下滑）
-- SELECT first_month, buyer_cnt, repeat_rate FROM ads_user_repeat_monthly ORDER BY first_month;
