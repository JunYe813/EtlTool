-- ====== DWS 增量装载（三张汇总表）======
-- 参数：%(start_date)s  %(end_date)s
--
-- 【为什么必须 DELETE 区间，不能只 UPSERT】
--   三张表都是聚合层（一行 = 一个维度组合的汇总）。
--   当某组合「重算后不再产生」时，UPSERT 无从冲突，旧行会留成幽灵行。
--   实测：一行幽灵数据让当日 GMV 从 30,878.56 变成 1,030,878.55（虚高 33 倍）。
--   → 聚合层统一用「DELETE 区间 + INSERT」，结果等价于全量重建该区间。
--
-- 【主键的作用】三张表都有主键（维度组合键），它是幂等的最后一道防线：
--   万一 DELETE 区间没删干净，INSERT 会撞主键**直接报错**暴露问题；
--   没有主键的话重复数据会静默插入，只能靠 GMV 翻倍才发现。
--
-- ⚠️ 列名注意点：三张表的日期列都叫 purchase_at，
--    而源表 dwd_order_detail 的列叫 purchase_date。
--
-- ⚠️ 分组注意点：seller_state / category_name 这类「退化维度」必须用 MAX 取，
--    不能放进 GROUP BY —— 否则同一 (日期, 卖家) 出现两个州值时会产生两行、撞主键。

DELETE FROM dws_sale_daily
 WHERE purchase_at BETWEEN %(start_date)s AND %(end_date)s;

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
    is_valid AND purchase_date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY
    purchase_date,
    COALESCE(category_name, 'unknown'),
    COALESCE(seller_state, 'unknown');


DELETE FROM dws_sale_daily_seller
 WHERE purchase_at BETWEEN %(start_date)s AND %(end_date)s;

INSERT INTO dws_sale_daily_seller(purchase_at,seller_id,seller_state,order_cnt,item_cnt,gmv)
SELECT
    purchase_date,
    seller_id,
    MAX(seller_state),
    COUNT(DISTINCT order_id) as order_cnt,
    COUNT(*) as item_cnt,
    SUM(price) as gmv
FROM
    dwd_order_detail
WHERE
    is_valid AND purchase_date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY 
    purchase_date,seller_id;






DELETE FROM dws_sale_daily_product
 WHERE purchase_at BETWEEN %(start_date)s AND %(end_date)s;

INSERT INTO dws_sale_daily_product(purchase_at,product_id,category_name,order_cnt,item_cnt,gmv)
SELECT
    purchase_date,
    product_id,
    MAX(category_name),
    COUNT(DISTINCT order_id) as order_cnt,
    COUNT(*) as item_cnt,
    SUM(price) as gmv
FROM
    dwd_order_detail
WHERE
    is_valid AND purchase_date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY 
    purchase_date,product_id;