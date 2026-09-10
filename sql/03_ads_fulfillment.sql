-- ====== ADS：履约漏斗（按月）======
-- 服务看板「履约漏斗」section 的逐级流失 + 平均履约时长
--
-- 口径：数据源 dwd_order，**用全量订单，不过滤 is_valid**
--   漏斗的意义是看「下单 → 审批 → 交承运 → 签收」的逐级流失，
--   canceled(取消) / created(未支付) 订单正是流失的主要来源，
--   过滤掉 is_valid 会把漏斗削平，看不出问题。
--   （销售类指标仍按有效订单口径，见 03_ads_sale_overview.sql）
--
-- 分母统一为「当月下单订单数」，各级率 = 该级订单数 ÷ 当月订单数
-- 实测（全量）：99441 → 审批 99281 → 交承运 97658 → 签收 96476，平均签收 12.50 天

DROP TABLE IF EXISTS ads_fulfillment_monthly CASCADE;

CREATE TABLE ads_fulfillment_monthly(
    purchase_month    date PRIMARY KEY,
    order_cnt         int,
    approved_cnt      int,
    shipped_cnt       int,
    delivered_cnt     int,
    canceled_cnt      int,
    approve_rate      NUMERIC(10,4),
    ship_rate         NUMERIC(10,4),
    deliver_rate      NUMERIC(10,4),
    cancel_rate       NUMERIC(10,4),
    avg_approve_hours NUMERIC(10,2),
    avg_ship_days     NUMERIC(10,2),
    avg_deliver_days  NUMERIC(10,2),
    late_cnt          int,
    late_rate         NUMERIC(10,4),
    create_date       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_date     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

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
GROUP BY DATE_TRUNC('month', purchase_at)::date;


-- ============================== 校验 ==============================
-- ① 订单总数（全量应 = 99441）
-- SELECT SUM(order_cnt) FROM ads_fulfillment_monthly;

-- ② 漏斗逐级递减，且各级 <= order_cnt
-- SELECT purchase_month, order_cnt, approved_cnt, shipped_cnt, delivered_cnt, cancel_rate
-- FROM ads_fulfillment_monthly ORDER BY purchase_month;

-- ③ 整体平均签收时长（实测 12.50 天）
-- SELECT ROUND(AVG(delivered_customer_at::date - purchase_at::date), 2)
-- FROM dwd_order WHERE is_valid AND delivered_customer_at IS NOT NULL;
