DROP TABLE IF EXISTS dwd_order_payment CASCADE;
CREATE TABLE IF NOT EXISTS dwd_order_payment (
    order_id        varchar(100) PRIMARY KEY,
    -- ★ 从 dwd_order 冗余过来的日期列。订单主表已有 purchase_at，这里刻意再存一份：
    --   ① 让本表能独立按日期区间 DELETE + INSERT（增量），不必每次 join 回 dwd_order
    --   ② 与其他 DWD/ADS 表保持一致：每张表都自带日期列，才能做分区改造（见 数据字典 7.6）
    --   ③ P2-1 支付方式趋势分析直接用它，不用再回关订单表
    --   冗余的代价只有 4 字节 × 99,440 行，换来的是可增量、可分区。
    purchase_date   date,
    payments_amount NUMERIC(14,2),   -- Σ payment_value
    payment_cnt     int,             -- 支付笔数（>1 即多期）
    has_voucher     boolean,
    main_type       varchar(20),     -- 金额最大的支付方式
    create_date     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ⚠️ 必须先按 order_id 聚合：支付表一单可多期支付（payment_sequential），
--    实测 103,886 行 → 99,440 个订单，其中 2,961 单是多期支付。
--    直接逐行比对会全部算错。
--
-- ⚠️ main_type 的 ORDER BY 里 payment_type 兜底键不能省：并列时 ARRAY_AGG 的
--    取值顺序是任意的，会导致重跑结果不可复现、check_idempotent 失败
--    （同类教训见 ads_top_product 的 product_id 兜底）。
--
-- JOIN dwd_order 只为取 purchase_date。已实测孤儿支付 = 0（支付表的 order_id
-- 全部能在订单主表里找到），所以这里的 INNER JOIN 不会丢行。
INSERT INTO dwd_order_payment
(order_id, purchase_date, payments_amount, payment_cnt, has_voucher, main_type, create_date)
SELECT
    p.order_id,
    o.purchase_at::date,
    ROUND(SUM(p.payment_value), 2),
    COUNT(*),
    BOOL_OR(p.payment_type = 'voucher'),
    (ARRAY_AGG(p.payment_type ORDER BY p.payment_value DESC, p.payment_type))[1],
    CURRENT_TIMESTAMP
FROM olist_order_payments_dataset p
JOIN dwd_order o ON o.order_id = p.order_id
GROUP BY p.order_id, o.purchase_at::date;


-- ============================== 校验 ==============================
-- ① 行数应为 99,440；Σpayments_amount 应等于 ODS 支付表的 Σpayment_value
-- SELECT COUNT(*), ROUND(SUM(payments_amount),2) FROM dwd_order_payment;
-- SELECT ROUND(SUM(payment_value),2) FROM olist_order_payments_dataset;
--
-- ② 多期支付订单数（基准 2,961）
-- SELECT COUNT(*) FROM dwd_order_payment WHERE payment_cnt > 1;
--
-- ③ purchase_date 不应有为空的行（基准 0）
-- SELECT COUNT(*) FROM dwd_order_payment WHERE purchase_date IS NULL;
