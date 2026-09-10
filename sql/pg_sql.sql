-- ============================================================
-- 临时查询草稿本（排查 / 对账用，不参与构建流程）
-- 正式的分层构建见 sql/00 ~ 04；这里只放随手写的诊断 SQL
-- ============================================================

-- ---------- PG 会话 / 慢查询排查 ----------
-- SELECT pid, usename, application_name, client_addr, state,
--        left(query, 100) AS query_preview,
--        query_start, now() - query_start AS duration
-- FROM pg_stat_activity
-- WHERE state IS NOT NULL
-- ORDER BY query_start NULLS LAST;

-- 取消 pid=12345 的当前查询
-- SELECT pg_cancel_backend(12345);


-- ---------- 各层行数速查 ----------
-- SELECT 'ods_order' AS t, COUNT(*) FROM olist_orders_dataset
-- UNION ALL SELECT 'ods_order_item', COUNT(*) FROM olist_order_items_dataset
-- UNION ALL SELECT 'dwd_order', COUNT(*) FROM dwd_order
-- UNION ALL SELECT 'dwd_order_detail', COUNT(*) FROM dwd_order_detail
-- UNION ALL SELECT 'dws_sale_daily', COUNT(*) FROM dws_sale_daily
-- UNION ALL SELECT 'ads_sale_overview_daily', COUNT(*) FROM ads_sale_overview_daily
-- UNION ALL SELECT 'ads_user_retention', COUNT(*) FROM ads_user_retention
-- UNION ALL SELECT 'ads_top_product', COUNT(*) FROM ads_top_product;


-- ---------- 主从核对：无明细的订单分布 ----------
-- SELECT t1.order_status, COUNT(*)
-- FROM olist_orders_dataset t1
-- LEFT JOIN olist_order_items_dataset t2 ON t2.order_id = t1.order_id
-- WHERE t2.order_id IS NULL
-- GROUP BY t1.order_status;

-- 已发货/已开票却没有明细的订单（口径异常，dwd_order.quality_flag 已标记）
-- SELECT t1.order_id, t1.order_status, t1.order_purchase_timestamp
-- FROM olist_orders_dataset t1
-- LEFT JOIN olist_order_items_dataset t2 ON t1.order_id = t2.order_id
-- WHERE t2.order_id IS NULL AND t1.order_status IN ('shipped','invoiced');


-- ---------- GMV / 客单价 ----------
-- ⚠️ 订单数不能从 dws_sale_daily 相加：
--    该表按 (日×类目×卖家州) 拆分，跨类目订单(785 单)/跨州订单(509 单)会被多行重复计数。
--    实测 SUM(order_cnt) = 99279，而真实有效订单数 = 98202，虚高 1077 单。
--    用它算客单价会得到 135.92，正确值是 137.41。
--    订单粒度指标一律走 dwd_order；dws_sale_daily 只用于带维度下钻的 GMV 分析。

-- 日趋势（推荐直接用 ADS 表，客单价口径已在表里写死）
SELECT purchase_date   AS "购买日期",
       gmv             AS "GMV",
       order_cnt       AS "订单数",
       avg_order_value AS "客单价"
FROM ads_sale_overview_daily
ORDER BY purchase_date DESC
LIMIT 20;

-- 区间汇总：注意 buyer_cnt 不可跨天相加（跨天复购的买家每天各计一次），
-- 区间去重买家必须现算，否则会把 94986 个买家算成 97272。
-- SELECT ROUND(SUM(gmv), 2)                            AS gmv,
--        SUM(order_cnt)                                AS order_cnt,
--        ROUND(SUM(gmv) / NULLIF(SUM(order_cnt), 0), 2) AS avg_order_value,
--        (SELECT COUNT(DISTINCT customer_unique_id) FROM dwd_order
--          WHERE is_valid
--            AND purchase_at::date BETWEEN '2018-01-01' AND '2018-03-31') AS buyer_cnt
-- FROM ads_sale_overview_daily
-- WHERE purchase_date BETWEEN '2018-01-01' AND '2018-03-31';

-- 若坚持从 DWS 层看日 GMV（GMV 本身可加，没问题；只取 gmv 一列即可）
-- SELECT purchase_at, SUM(gmv) AS gmv
-- FROM dws_sale_daily
-- GROUP BY purchase_at
-- ORDER BY purchase_at;


-- ---------- 留存：剔除右删失后的真实留存曲线 ----------
-- SELECT window_days,
--        COUNT(DISTINCT cohort_date) AS cohorts,
--        ROUND(SUM(retained_cnt)::NUMERIC / SUM(cohort_size), 4) AS retention_rate
-- FROM v_retention_curve
-- WHERE is_complete AND cohort_size >= 20
-- GROUP BY window_days ORDER BY window_days;
