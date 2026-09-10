-- ======DWS（按维度汇总）======
-- 建表：dws_sale_daily（按 购买日 × 英文类目 × 卖家州）
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


-- 建表：dws_sale_daily_seller（按天 x 店铺）
DROP TABLE IF EXISTS dws_sale_daily_seller CASCADE;

CREATE TABLE dws_sale_daily_seller (
    purchase_at  date,
    seller_id    varchar(100),
    seller_state varchar(10),
    order_cnt    int,
    item_cnt     int,
    gmv          NUMERIC(14,2),
    PRIMARY KEY (purchase_at, seller_id)
);

INSERT INTO dws_sale_daily_seller(purchase_at,seller_id,seller_state,order_cnt,item_cnt,gmv)
SELECT
    purchase_date,
    seller_id,
    seller_state,
    COUNT(DISTINCT order_id) as order_cnt,
    COUNT(*) as item_cnt,
    SUM(price) as gmv
FROM
    dwd_order_detail
WHERE
    is_valid
GROUP BY 
    purchase_date,seller_id,seller_state;




-- 建表：dws_sale_daily_product（按天 x 商品）
DROP TABLE IF EXISTS dws_sale_daily_product CASCADE;

CREATE TABLE dws_sale_daily_product (
    purchase_at   date,
    product_id    varchar(100),
    category_name varchar(100),
    order_cnt     int,
    item_cnt      int,
    gmv           NUMERIC(14,2),
    PRIMARY KEY (purchase_at, product_id)
);

INSERT INTO dws_sale_daily_product(purchase_at,product_id,category_name,order_cnt,item_cnt,gmv)
SELECT
    purchase_date,
    product_id,
    category_name,
    COUNT(DISTINCT order_id) as order_cnt,
    COUNT(*) as item_cnt,
    SUM(price) as gmv
FROM
    dwd_order_detail
WHERE
    is_valid
GROUP BY 
    purchase_date,product_id,category_name;

-- SELECT * from dws_sale_daily_product LIMIT 3;

-- SELECT * FROM dws_sale_daily_product WHERE purchase_at BETWEEN '2016-09-04' AND '2016-12-31' ORDER BY gmv DESC LIMIT 10;
-- ============================== 对账校验 ==============================
-- 三处 GMV 必须完全一致（实测 = 13494400.74），不一致说明链路有丢数：
-- ① DWS 汇总总 GMV
-- SELECT ROUND(SUM(gmv),2) FROM dws_sale_daily;
-- ② 明细层直接算（同口径：有效订单）
-- SELECT ROUND(SUM(price),2) FROM dwd_order_detail WHERE is_valid;
-- ③ 订单层算（另一个角度）
-- SELECT ROUND(SUM(gmv),2) FROM dwd_order WHERE is_valid;
