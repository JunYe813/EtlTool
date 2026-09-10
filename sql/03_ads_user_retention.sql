-- ====== ADS：用户留存（Cohort 留存）======
-- 口径：
--   1) 首购日 cohort_date = 该 customer_unique_id 在有效订单中最早的购买日
--   2) N 日留存 = 首购日之后第 1~N 日内（**不含首购当天**）再次购买的买家数 ÷ 该 cohort 规模
--   3) 分子分母都按 customer_unique_id 去重
--   4) observable_days = 数据最大购买日 - cohort_date，即该 cohort 实际可观察的天数
--
-- ⚠️ 右删失（right censoring）：
--    当 observable_days < N 时，该 cohort 的 N 日留存尚未观察完整，数值必然偏低，
--    不代表留存下滑。看板必须用 observable_days >= N 过滤。
--    实测有效订单最大购买日 = 2018-09-03，故 2018-06-05 之后的 cohort 的 90 日留存均不完整。
--
-- ⚠️ 小样本噪声：
--    cohort_size = 1 时，该买家恰好复购就会得到 100% 留存。
--    实测历史上出现过 cohort_size=1 而 30 日留存=1.00 的 cohort。
--    看板需用 cohort_size >= config.MIN_GROUP_SIZE 过滤。
--
-- 本次修复的两处旧问题：
--   ① 原 first_customer varchar(100) 实际插入的是 COUNT(...) 一个整数 → 改名 cohort_size 并改 int
--   ② 原 retention*_rate NUMERIC(10,2) 会把 ROUND(...,4) 砍成 2 位小数，
--      导致 613 个 cohort 里 498 个的 7 日留存变成 0.00（Olist 留存本来就只有 1~3%）
--      → 改为 NUMERIC(10,4)

DROP TABLE IF EXISTS ads_user_retention CASCADE;

CREATE TABLE ads_user_retention(
    cohort_date        date PRIMARY KEY,
    cohort_size        int  NOT NULL DEFAULT 0,
    observable_days    int,
    retention7d_count  int,
    retention7d_rate   NUMERIC(10,4),
    retention30d_count int,
    retention30d_rate  NUMERIC(10,4),
    retention60d_count int,
    retention60d_rate  NUMERIC(10,4),
    retention90d_count int,
    retention90d_rate  NUMERIC(10,4),
    create_date        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_date      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO ads_user_retention
(cohort_date, cohort_size, observable_days,
 retention7d_count,  retention7d_rate,
 retention30d_count, retention30d_rate,
 retention60d_count, retention60d_rate,
 retention90d_count, retention90d_rate)
WITH first_date AS (
    SELECT customer_unique_id, MIN(purchase_at::date) AS fd
    FROM dwd_order
    WHERE is_valid
    GROUP BY customer_unique_id
),
max_date AS (
    SELECT MAX(purchase_at::date) AS maxd
    FROM dwd_order
    WHERE is_valid
),
cohort AS (
    -- 每个买家 × 其全部购买日，组成 cohort 明细，再用 FILTER 一次算出 4 个窗口
    SELECT t1.fd                 AS cohort_date,
           t1.customer_unique_id,
           t2.purchase_at::date  AS buy_date
    FROM first_date t1
    LEFT JOIN dwd_order t2
           ON t1.customer_unique_id = t2.customer_unique_id
          AND t2.is_valid
)
SELECT
    cohort_date,
    COUNT(DISTINCT customer_unique_id) AS cohort_size,
    (SELECT maxd FROM max_date) - cohort_date AS observable_days,

    COUNT(DISTINCT customer_unique_id)
        FILTER (WHERE buy_date BETWEEN cohort_date + 1 AND cohort_date + 7)  AS r7_cnt,
    ROUND(
        COUNT(DISTINCT customer_unique_id)
            FILTER (WHERE buy_date BETWEEN cohort_date + 1 AND cohort_date + 7)::NUMERIC
        / NULLIF(COUNT(DISTINCT customer_unique_id), 0), 4)                   AS r7_rate,

    COUNT(DISTINCT customer_unique_id)
        FILTER (WHERE buy_date BETWEEN cohort_date + 1 AND cohort_date + 30) AS r30_cnt,
    ROUND(
        COUNT(DISTINCT customer_unique_id)
            FILTER (WHERE buy_date BETWEEN cohort_date + 1 AND cohort_date + 30)::NUMERIC
        / NULLIF(COUNT(DISTINCT customer_unique_id), 0), 4)                   AS r30_rate,

    COUNT(DISTINCT customer_unique_id)
        FILTER (WHERE buy_date BETWEEN cohort_date + 1 AND cohort_date + 60) AS r60_cnt,
    ROUND(
        COUNT(DISTINCT customer_unique_id)
            FILTER (WHERE buy_date BETWEEN cohort_date + 1 AND cohort_date + 60)::NUMERIC
        / NULLIF(COUNT(DISTINCT customer_unique_id), 0), 4)                   AS r60_rate,

    COUNT(DISTINCT customer_unique_id)
        FILTER (WHERE buy_date BETWEEN cohort_date + 1 AND cohort_date + 90) AS r90_cnt,
    ROUND(
        COUNT(DISTINCT customer_unique_id)
            FILTER (WHERE buy_date BETWEEN cohort_date + 1 AND cohort_date + 90)::NUMERIC
        / NULLIF(COUNT(DISTINCT customer_unique_id), 0), 4)                   AS r90_rate
FROM cohort
GROUP BY cohort_date;


-- ============================== 校验 ==============================
-- ① cohort 数与规模合计（实测 613 个 cohort，规模合计 94986 = 总买家数，
--    必须与 ads_user_repeat_overall.buyer_cnt 相等，否则留存和复购两套口径打架）
-- SELECT COUNT(*), SUM(cohort_size) FROM ads_user_retention;
-- SELECT buyer_cnt FROM ads_user_repeat_overall;

-- ② 精度修复验证：7 日留存不应再是一堆 0.00
-- SELECT retention7d_rate, COUNT(*) FROM ads_user_retention GROUP BY 1 ORDER BY 1;

-- ③ 满窗 cohort 的 30 日留存（剔除右删失后才是可对外讲的数字）
-- SELECT ROUND(SUM(retention30d_count)::NUMERIC / SUM(cohort_size), 4)
-- FROM ads_user_retention WHERE observable_days >= 30 AND cohort_size >= 20;
