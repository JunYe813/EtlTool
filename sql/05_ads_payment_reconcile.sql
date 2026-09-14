DROP TABLE IF EXISTS ads_payment_reconcile CASCADE;
CREATE TABLE IF NOT EXISTS ads_payment_reconcile (
    order_id        varchar(100) PRIMARY KEY,
    order_status    varchar(20),     -- ★ 归因的关键
    purchase_date   date,
    items_amount    NUMERIC(14,2),
    payments_amount NUMERIC(14,2),
    diff_amount     NUMERIC(14,2),   -- payments − items（有符号）
    diff_type       varchar(30),
    is_comparable   boolean,         -- ★ 本应有明细吗
    create_date     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);



INSERT INTO ads_payment_reconcile
(order_id,order_status,purchase_date,items_amount,payments_amount,diff_amount,diff_type,is_comparable,create_date)
WITH items AS (
    SELECT order_id, SUM(price + COALESCE(freight_value, 0)) AS items_amount
    FROM dwd_order_detail
    GROUP BY order_id                 -- ⚠️ 不过滤 is_valid：对账要全量
), pay AS (
    SELECT order_id, payments_amount FROM dwd_order_payment
)
SELECT
    o.order_id,
    o.order_status,
    o.purchase_at::date                                        AS purchase_date,
    COALESCE(i.items_amount, 0)                                AS items_amount,
    COALESCE(p.payments_amount, 0)                             AS payments_amount,
    ROUND(COALESCE(p.payments_amount,0)
        - COALESCE(i.items_amount,0), 2)                       AS diff_amount,
    CASE
        WHEN i.order_id IS NULL THEN '仅支付无明细'             -- 结构性单边
        WHEN p.order_id IS NULL THEN '仅明细无支付'             -- 结构性单边
        WHEN ABS(COALESCE(p.payments_amount,0)
               - COALESCE(i.items_amount,0)) > 0.01 THEN '金额不符'
        ELSE '一致'
    END                                                        AS diff_type,
    o.order_status NOT IN ('unavailable','canceled')            AS is_comparable,
    CURRENT_TIMESTAMP
FROM dwd_order o
LEFT JOIN items i ON i.order_id = o.order_id
LEFT JOIN pay   p ON p.order_id = o.order_id;



DROP TABLE IF EXISTS ads_payment_reconcile_daily CASCADE;
CREATE TABLE IF NOT EXISTS ads_payment_reconcile_daily (
    purchase_date   date PRIMARY KEY,
    order_cnt       int,
    matched_cnt     int,
    pay_only_cnt    int,
    items_only_cnt  int,
    amount_diff_cnt int,
    amount_diff_sum NUMERIC(14,2),   -- 真·金额差异净额
    net_diff        NUMERIC(14,2)    -- 含单边的总净额
);

INSERT INTO ads_payment_reconcile_daily
SELECT
    purchase_date,
    COUNT(*),
    COUNT(*) FILTER (WHERE diff_type = '一致'),
    COUNT(*) FILTER (WHERE diff_type = '仅支付无明细'),
    COUNT(*) FILTER (WHERE diff_type = '仅明细无支付'),
    COUNT(*) FILTER (WHERE diff_type = '金额不符'),
    COALESCE(SUM(diff_amount) FILTER (WHERE diff_type = '金额不符'), 0),
    SUM(diff_amount)
FROM ads_payment_reconcile
GROUP BY purchase_date;