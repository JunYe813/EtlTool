-- ====== ADS 增量装载（只含「可增量」的三张表）======
-- 参数：%(start_date)s  %(end_date)s
--
-- 【为什么只有这三张能增量】
--   判断法则：**这个结果是否只依赖本区间的数据？**
--     ✅ ads_sale_overview_daily   日粒度，只依赖当天
--     ✅ ads_top_product           窗口按 purchase_date 分区，每天名次只依赖当天
--     ✅ ads_fulfillment_monthly   月粒度，只依赖该月
--     ❌ ads_user_retention        分母是「全体买家的首购日」→ 依赖全部历史
--     ❌ ads_user_repeat_*         全周期 / 按首购月分组 → 依赖全部历史
--     ❌ ads_top_seller            全周期累计；且只有 3,053 行，全量重建更简单
--   后三张不在本文件，由 ads_build.rebuild_full_only() 全量重建。
--
-- 【聚合层一律 DELETE 区间 + INSERT】不能只 UPSERT（组合消失会留幽灵行）。

-- ---------------------------------------------------------------
-- ① ads_sale_overview_daily：日粒度销售总览
-- 口径与全量版完全一致：数据源 dwd_order（订单粒度），客单价写死在表里
-- ---------------------------------------------------------------
DELETE FROM ads_sale_overview_daily
 WHERE purchase_date BETWEEN %(start_date)s AND %(end_date)s;

INSERT INTO ads_sale_overview_daily
(purchase_date, gmv, freight_total, paid_amount,
 order_cnt, item_cnt, buyer_cnt, avg_order_value)
SELECT
    purchase_at::date                              AS purchase_date,
    ROUND(SUM(gmv), 2)                             AS gmv,
    ROUND(SUM(freight_total), 2)                   AS freight_total,
    ROUND(SUM(gmv) + SUM(freight_total), 2)        AS paid_amount,
    COUNT(*)                                       AS order_cnt,
    COALESCE(SUM(item_count), 0)                   AS item_cnt,
    COUNT(DISTINCT customer_unique_id)             AS buyer_cnt,
    ROUND(SUM(gmv) / NULLIF(COUNT(*), 0), 2)       AS avg_order_value
FROM dwd_order
WHERE is_valid
  AND purchase_at::date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY purchase_at::date;


-- ---------------------------------------------------------------
-- ② ads_top_product：日粒度 Top10
-- ⚠️ 名次是「相对排序」——那天只要有一笔数据变化，整个 Top10 都可能重排，
--    所以必须整天重算，不能只补新增。
-- ⚠️ ORDER BY 里的 product_id 是并列兜底，不能丢：否则并列时顺序任意、结果不可复现
--    （历史实测有 20 天存在第 10/11 名 GMV 完全并列）。
-- ---------------------------------------------------------------
DELETE FROM ads_top_product
 WHERE purchase_date BETWEEN %(start_date)s AND %(end_date)s;

INSERT INTO ads_top_product (purchase_date, product_id, category_name, gmv, rank_no)
WITH top_product AS (
    SELECT
        purchase_date,
        product_id,
        MAX(category_name) AS category_name,
        SUM(price)         AS gmv,
        ROW_NUMBER() OVER (
            PARTITION BY purchase_date
            ORDER BY SUM(price) DESC, product_id
        ) AS rank_no
    FROM dwd_order_detail
    WHERE is_valid
      AND purchase_date BETWEEN %(start_date)s AND %(end_date)s
    GROUP BY purchase_date, product_id
)
SELECT purchase_date, product_id, category_name, gmv, rank_no
FROM top_product
WHERE rank_no <= 10;


-- ---------------------------------------------------------------
-- ③ ads_fulfillment_monthly：月粒度履约漏斗
-- ⚠️ 改一天要重算**整个月**；且回看窗口可能跨月（比如 --date 是 3 号、回看 3 天
--    就回到上个月）→ 所以删除范围要按「月」对齐到 start / end 所在月。
-- ⚠️ 必须用 >= <= 的**区间**，不能用 IN (trunc(start), trunc(end))：
--    跨 ≥3 个月时（如 --lookback 90），中间月份不在 IN 列表里 → 漏删，
--    紧接着被下面的 INSERT 重插，撞 PRIMARY KEY (purchase_month) 直接报
--    `duplicate key value violates unique constraint`，增量任务中断。
--    （默认 --lookback 3 最多跨 2 个月，所以这个坑平时看不出来 —— 实测复现过。）
--    区间写法覆盖全部中间月。
-- ⚠️ purchase_month 存的是「月首日」，DELETE 必须用同样的 DATE_TRUNC 表达式匹配。
-- 口径：用全量订单，不过滤 is_valid（取消 / 未支付正是漏斗流失的来源）
-- ---------------------------------------------------------------
DELETE FROM ads_fulfillment_monthly
 WHERE purchase_month >= DATE_TRUNC('month', %(start_date)s::date)::date
   AND purchase_month <= DATE_TRUNC('month', %(end_date)s::date)::date;

INSERT INTO ads_fulfillment_monthly
(purchase_month, order_cnt, approved_cnt, shipped_cnt, delivered_cnt, canceled_cnt,
 approve_rate, ship_rate, deliver_rate, cancel_rate,
 avg_approve_hours, avg_ship_days, avg_deliver_days, late_cnt, late_rate)
SELECT
    DATE_TRUNC('month', purchase_at)::date AS purchase_month,
    COUNT(*)                               AS order_cnt,
    COUNT(approved_at)                     AS approved_cnt,
    COUNT(delivered_carrier_at)            AS shipped_cnt,
    COUNT(delivered_customer_at)           AS delivered_cnt,
    COUNT(*) FILTER (WHERE order_status = 'canceled') AS canceled_cnt,

    ROUND(COUNT(approved_at)::NUMERIC           / NULLIF(COUNT(*),0), 4) AS approve_rate,
    ROUND(COUNT(delivered_carrier_at)::NUMERIC  / NULLIF(COUNT(*),0), 4) AS ship_rate,
    ROUND(COUNT(delivered_customer_at)::NUMERIC / NULLIF(COUNT(*),0), 4) AS deliver_rate,
    ROUND(COUNT(*) FILTER (WHERE order_status = 'canceled')::NUMERIC
          / NULLIF(COUNT(*),0), 4)                                       AS cancel_rate,

    -- 审批时长用小时（多数订单在 1 天内审批完，用天会全是 0）
    ROUND(AVG(EXTRACT(EPOCH FROM (approved_at - purchase_at)) / 3600), 2) AS avg_approve_hours,
    ROUND(AVG(delivered_carrier_at::date  - purchase_at::date), 2)        AS avg_ship_days,
    ROUND(AVG(delivered_customer_at::date - purchase_at::date), 2)        AS avg_deliver_days,

    -- 逾期签收：实际签收晚于预计送达
    COUNT(*) FILTER (WHERE delivered_customer_at > estimated_delivery_at) AS late_cnt,
    ROUND(COUNT(*) FILTER (WHERE delivered_customer_at > estimated_delivery_at)::NUMERIC
          / NULLIF(COUNT(delivered_customer_at), 0), 4)                   AS late_rate
FROM dwd_order
WHERE purchase_at IS NOT NULL
  AND purchase_at::date >= DATE_TRUNC('month', %(start_date)s::date)::date
  AND purchase_at::date <  DATE_TRUNC('month', %(end_date)s::date)::date + INTERVAL '1 month'
GROUP BY DATE_TRUNC('month', purchase_at)::date;


-- ============================== 校验 ==============================
-- ① 增量跑完后，GMV 合计仍应等于 13,494,400.74
-- SELECT ROUND(SUM(gmv),2) FROM ads_sale_overview_daily;
-- SELECT ROUND(SUM(gmv),2) FROM dwd_order WHERE is_valid;
--
-- ② 每天的 Top10 行数不应超过 10
-- SELECT purchase_date, COUNT(*) FROM ads_top_product
-- GROUP BY 1 HAVING COUNT(*) > 10;
--
-- ③ 履约漏斗月份数不应变化（25 个月），且订单总数仍为 99,441
-- SELECT COUNT(*), SUM(order_cnt) FROM ads_fulfillment_monthly;
