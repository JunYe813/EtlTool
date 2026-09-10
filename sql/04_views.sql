-- ====== ADS：视图层 ======
-- 视图 vs 表的划分原则：
--   能预聚合、被看板高频读的  → 落表（ads_*）
--   需要按看板筛选器（日期/类目/州）现场算的 → 建视图（v_*）
--
-- ================== 设计约束（很重要）==================
-- 看板带日期筛选器，因此视图**必须保留日期粒度**，且**只暴露可加指标**：
--
--   ✗ 错误做法：视图里直接 GROUP BY 类目、并用窗口函数算好占比。
--     一旦看板按日期筛选，占比的分母仍是全周期总额，加起来不等于 100%。
--     （本项目第一版就这么写的，属于典型陷阱）
--   ✓ 正确做法：视图只暴露「日 × 维度」粒度的原始可加指标（gmv / item_cnt / order_cnt），
--     占比、客单价等派生指标由看板在筛选后的数据集上现算。
--
--   ✗ 不要暴露跨天不可加的去重指标：buyer_cnt 跨天相加会把同一买家重复计数
--     （实测 SUM = 97272，真实去重买家 = 94986）。
--
-- ⚠️ 另一条通用口径警告：dws_sale_daily.order_cnt 不可跨行相加
--    （按 日×类目×州 拆分，跨类目/跨州订单被重复计数，见 02_dws.sql 顶部说明）。
--    因此下面基于 dws_sale_daily 的视图**只暴露 gmv 和 item_cnt**，
--    订单数一律从 dwd_order / dwd_order_detail 取。

-- ---------------------------------------------------------------
-- ① 销售 日 × 类目（看板「商品分析」的占比饼图 + 类目趋势）
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_sale_daily_category AS
SELECT
    purchase_at,
    category_name,
    ROUND(SUM(gmv), 2) AS gmv,
    SUM(item_cnt)      AS item_cnt
FROM dws_sale_daily
GROUP BY purchase_at, category_name;


-- ---------------------------------------------------------------
-- ② 销售 日 × 卖家州（看板「卖家与地区」的州对比）
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_sale_daily_seller_state AS
SELECT
    purchase_at,
    seller_state,
    ROUND(SUM(gmv), 2) AS gmv,
    SUM(item_cnt)      AS item_cnt
FROM dws_sale_daily
GROUP BY purchase_at, seller_state;


-- ---------------------------------------------------------------
-- ③ 销售 日 × 买家州（看板「卖家与地区」的买家分布）
-- dws_sale_daily 没有买家维度，走 dwd_order（订单粒度，order_cnt/gmv 都可加）
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_sale_daily_buyer_state AS
SELECT
    purchase_at::date   AS purchase_date,
    customer_state,
    COUNT(*)            AS order_cnt,
    ROUND(SUM(gmv), 2)  AS gmv
FROM dwd_order
WHERE is_valid
GROUP BY purchase_at::date, customer_state;


-- ---------------------------------------------------------------
-- ④ 履约漏斗（日粒度）
-- 看板日期筛选需按天切，月度表（ads_fulfillment_monthly）粒度不够
-- 口径同月度表：**全量订单，不过滤 is_valid**，取消/未支付正是漏斗流失的来源
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_fulfillment_funnel AS
SELECT
    purchase_at::date AS purchase_date,
    COUNT(*)                                   AS order_cnt,
    COUNT(approved_at)                         AS approved_cnt,
    COUNT(delivered_carrier_at)                AS shipped_cnt,
    COUNT(delivered_customer_at)               AS delivered_cnt,
    COUNT(*) FILTER (WHERE order_status = 'canceled') AS canceled_cnt,
    ROUND(AVG(EXTRACT(EPOCH FROM (approved_at - purchase_at)) / 3600), 2) AS avg_approve_hours,
    ROUND(AVG(delivered_carrier_at::date  - purchase_at::date), 2)        AS avg_ship_days,
    ROUND(AVG(delivered_customer_at::date - purchase_at::date), 2)        AS avg_deliver_days
FROM dwd_order
WHERE purchase_at IS NOT NULL
GROUP BY purchase_at::date;


-- ---------------------------------------------------------------
-- ⑤ 留存曲线（长表）
-- ads_user_retention 是宽表（7/30/60/90 四组列），画多窗口折线要写 4 个图层；
-- 这里 UNION ALL 转成长表，看板一个图层 + 颜色区分 window_days 就能画完。
-- is_complete = 该 cohort 的该窗口是否已观察完整（右删失标记），看板据此过滤。
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW v_retention_curve AS
SELECT cohort_date, cohort_size, observable_days, 7  AS window_days,
       retention7d_count  AS retained_cnt, retention7d_rate  AS retention_rate,
       (observable_days >= 7)  AS is_complete
FROM ads_user_retention
UNION ALL
SELECT cohort_date, cohort_size, observable_days, 30,
       retention30d_count, retention30d_rate,
       (observable_days >= 30)
FROM ads_user_retention
UNION ALL
SELECT cohort_date, cohort_size, observable_days, 60,
       retention60d_count, retention60d_rate,
       (observable_days >= 60)
FROM ads_user_retention
UNION ALL
SELECT cohort_date, cohort_size, observable_days, 90,
       retention90d_count, retention90d_rate,
       (observable_days >= 90)
FROM ads_user_retention;


-- ============================== 校验 ==============================
-- ① 日×类目 视图的 GMV 合计应 = 全量 GMV 13494400.74
-- SELECT ROUND(SUM(gmv),2) FROM v_sale_daily_category;

-- ② 日×买家州 的订单数合计应 = 有效订单数 98202
-- SELECT SUM(order_cnt) FROM v_sale_daily_buyer_state;

-- ③ 留存长表应有 613 × 4 = 2452 行
-- SELECT window_days, COUNT(*) FROM v_retention_curve GROUP BY 1 ORDER BY 1;

-- ④ 剔除右删失与小样本后的真实留存曲线（这是能对外讲的数字）
-- SELECT window_days,
--        COUNT(DISTINCT cohort_date) AS cohorts,
--        ROUND(SUM(retained_cnt)::NUMERIC / SUM(cohort_size), 4) AS retention_rate
-- FROM v_retention_curve
-- WHERE is_complete AND cohort_size >= 20
-- GROUP BY window_days ORDER BY window_days;
