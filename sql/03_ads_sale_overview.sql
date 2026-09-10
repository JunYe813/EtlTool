-- ====== ADS：销售总览（日粒度）======
-- 服务看板「销售总览」section 的 3 个 metric 卡片 + GMV 日趋势
--
-- 口径（依据 docs/数据字典.md 第五节）：
--   gmv          = 有效订单的 Σ 明细 price（不含运费）
--   order_cnt    = 有效订单数
--   item_cnt     = 有效订单的明细行数
--   buyer_cnt    = 去重买家数（customer_unique_id，不是 customer_id）
--   paid_amount  = gmv + freight_total，可与支付表 Σpayment_value 对账
--   avg_order_value = gmv ÷ order_cnt  ← 客单价口径写死在本表，看板不再重算
--
-- ⚠️ 数据源必须用 dwd_order（订单粒度，一单一行），不能用 dws_sale_daily：
--    dws_sale_daily 按 (日×类目×卖家州) 拆分，跨类目/跨州订单会被多行重复计数，
--    实测 SUM(order_cnt)=99279 而真实有效订单数=98202（虚高 1077 单），
--    若拿它算客单价会得到 135.92 而非正确的 137.41。
--
-- ⚠️ 本表各列的可加性（看板聚合时务必注意）：
--    gmv / freight_total / paid_amount / item_cnt  → 可加，跨天求和即区间合计
--    order_cnt                                     → 可加（一单只属一个购买日）
--    buyer_cnt                                     → **不可加**！跨天复购的买家每天都各计一次。
--        实测 SUM(buyer_cnt) = 97272，而真实去重买家只有 94986
--        （2064 个买家在多天购买）。区间去重买家请查 ads_user_repeat_overall，
--        或直接 COUNT(DISTINCT customer_unique_id) FROM dwd_order WHERE is_valid AND 日期区间。
--    avg_order_value                               → 不可加也不可平均，区间客单价需用 区间GMV÷区间订单数 重算

DROP TABLE IF EXISTS ads_sale_overview_daily CASCADE;

CREATE TABLE ads_sale_overview_daily(
    purchase_date   date PRIMARY KEY,
    gmv             NUMERIC(14,2) NOT NULL DEFAULT 0,
    freight_total   NUMERIC(14,2) NOT NULL DEFAULT 0,
    paid_amount     NUMERIC(14,2) NOT NULL DEFAULT 0,
    order_cnt       int           NOT NULL DEFAULT 0,
    item_cnt        int           NOT NULL DEFAULT 0,
    buyer_cnt       int           NOT NULL DEFAULT 0,
    avg_order_value NUMERIC(10,2),
    create_date     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_date   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

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
GROUP BY purchase_at::date;

CREATE INDEX IF NOT EXISTS idx_ads_overview_date ON ads_sale_overview_daily (purchase_date);


-- ============================== 对账校验 ==============================
-- ① 三层 GMV 必须一致（实测 13494400.74）
-- SELECT ROUND(SUM(gmv),2) FROM ads_sale_overview_daily;              -- ADS
-- SELECT ROUND(SUM(gmv),2) FROM dwd_order WHERE is_valid;             -- DWD 订单层
-- SELECT ROUND(SUM(price),2) FROM dwd_order_detail WHERE is_valid;    -- DWD 明细层

-- ② 订单数 / 买家数（实测 98202 / 94986）
-- SELECT SUM(order_cnt), SUM(buyer_cnt) FROM ads_sale_overview_daily;

-- ③ 与支付表对账：实付金额 vs Σpayment_value
--    差异属预期（支付表含无明细订单、含运费口径不同），这正是"主从核对"要讲的点
-- SELECT ROUND(SUM(paid_amount),2) FROM ads_sale_overview_daily;
-- SELECT ROUND(SUM(payment_value),2) FROM olist_order_payments_dataset;
