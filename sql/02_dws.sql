-- ======DWS（按维度汇总）======
-- 建表：dws_sale_daily（按 购买日 × 英文类目 × 卖家州）
--
-- ⚠️ 重要口径警告：order_cnt 不可跨行相加！
--    本表 order_cnt = COUNT(DISTINCT order_id)，是在 (日×类目×州) 分组内去重的。
--    一个订单若含多个类目（实测 785 单）或多个卖家州（实测 509 单），会在多行各记 1 单。
--    实测 SUM(order_cnt) = 99279，而真实有效订单数 = 98202，虚高 1077 单。
--    → GMV / item_cnt 可加；订单数、客单价等订单粒度指标一律走 dwd_order。
--
-- 用途：带维度下钻的分析（类目占比、州对比、日趋势）
-- CASCADE 原因同 01_dwd.sql：视图（v_category_share 等）依赖本表，全量重建需连带删除
DROP TABLE IF EXISTS dws_sale_daily CASCADE;

CREATE TABLE dws_sale_daily(
    purchase_at date,
    category_name varchar(100),
    seller_state varchar(10),
    order_cnt int,
    item_cnt int,
    gmv NUMERIC(10,2),
    PRIMARY KEY(purchase_at,category_name,seller_state)
);

INSERT INTO dws_sale_daily 
(purchase_at,category_name,seller_state,order_cnt,item_cnt,gmv)
SELECT
    purchase_date,
    -- PG 主键隐含 NOT NULL。DWD 里 category_name 已兜成 'unknown'，
    -- 但 seller_state 来自 LEFT JOIN 卖家表，卖家缺失时会是 NULL —— 一旦出现就整批插入失败。
    -- 这里兜成 'unknown' 而不是 WHERE 过滤掉，避免直接丢掉 GMV 破坏对账。
    COALESCE(category_name, 'unknown') AS category_name,
    COALESCE(seller_state, 'unknown')  AS seller_state,
    COUNT(DISTINCT order_id) AS order_cnt,
    COUNT(*) AS item_cnt,
    SUM(price) AS gmv
FROM
    dwd_order_detail
WHERE
    is_valid
GROUP BY
    purchase_date,
    COALESCE(category_name, 'unknown'),
    COALESCE(seller_state, 'unknown');


-- ============================== 对账校验 ==============================
-- 三处 GMV 必须完全一致（实测 = 13494400.74），不一致说明链路有丢数：
-- ① DWS 汇总总 GMV
-- SELECT ROUND(SUM(gmv),2) FROM dws_sale_daily;
-- ② 明细层直接算（同口径：有效订单）
-- SELECT ROUND(SUM(price),2) FROM dwd_order_detail WHERE is_valid;
-- ③ 订单层算（另一个角度）
-- SELECT ROUND(SUM(gmv),2) FROM dwd_order WHERE is_valid;

-- 订单数对账：DWS 的 SUM(order_cnt) 会大于真实订单数，这是设计使然（见上方警告），
-- 真实有效订单数应查 dwd_order：
-- SELECT COUNT(*) FROM dwd_order WHERE is_valid;   -- 98202
