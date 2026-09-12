-- 参数：%(start_date)s  %(end_date)s
DELETE FROM dwd_order_detail
 WHERE purchase_date BETWEEN %(start_date)s AND %(end_date)s;

INSERT INTO dwd_order_detail 
(order_id,order_item_id,product_id,category_name,seller_id,seller_state,seller_city,purchase_date,order_status,is_valid,price,freight_value,create_date,modified_date)
SELECT
    t1.order_id,
    t1.order_item_id::INT,
    t1.product_id,
    COALESCE(t4.product_category_name_english,'unknown') as category_name,
    t1.seller_id,
    t3.seller_state,
    t3.seller_city,
    -- 显式 ::date 截断：order_purchase_timestamp 是 timestamp，
    -- 依赖 PG 赋值隐式转换虽然能跑通，但日期精度丢失不显式、换 COPY 等导入方式会报错
    t5.order_purchase_timestamp::date as purchase_date,
    t5.order_status,
    (t5.order_status IN ('processing','invoiced','approved','shipped','delivered')) as is_valid,
    CAST(t1.price as NUMERIC(10,2)) as price,
    CAST(t1.freight_value as NUMERIC(10,2)) as freight_value,
    CURRENT_TIMESTAMP as create_date,
    CURRENT_TIMESTAMP as modified_date
FROM olist_order_items_dataset t1
LEFT JOIN olist_products_dataset t2 ON t1.product_id = t2.product_id
LEFT JOIN olist_sellers_dataset t3 ON t1.seller_id = t3.seller_id
LEFT JOIN product_category_name_translation t4 ON t2.product_category_name = t4.product_category_name
LEFT JOIN olist_orders_dataset t5 ON t1.order_id = t5.order_id
WHERE t5.order_purchase_timestamp::date BETWEEN %(start_date)s AND %(end_date)s;




DELETE FROM dwd_order WHERE purchase_at::date BETWEEN %(start_date)s AND %(end_date)s;


INSERT INTO dwd_order 
(order_id,customer_id,customer_unique_id,customer_state,customer_city,order_status,purchase_at,approved_at,delivered_carrier_at,delivered_customer_at,estimated_delivery_at,item_count,gmv,freight_total,is_valid,quality_flag,create_date,modified_date)
SELECT
    t1.order_id,
    t1.customer_id,
    t2.customer_unique_id,
    t2.customer_state,
    t2.customer_city,
    t1.order_status,
    t1.order_purchase_timestamp as purchase_at,
    t1.order_approved_at as approved_at,
    t1.order_delivered_carrier_date as delivered_carrier_at,
    t1.order_delivered_customer_date as delivered_customer_at,
    t1.order_estimated_delivery_date as estimated_delivery_at,
    COALESCE(t3.item_count,0) as item_count,
    COALESCE(t3.gmv,0) as gmv,
    -- 原写法 COALESCE(t3.freight_total) 只有一个参数，等于没兜底，
    -- 无明细订单的运费会留 NULL，和上面 gmv/item_count 的 0 不一致，聚合时 NULL 会传染
    COALESCE(t3.freight_total, 0) as freight_total,
    (t1.order_status in ('processing','invoiced','approved','shipped','delivered')) as is_valid,
    CASE
        WHEN t3.order_id IS NULL and t1.order_status in ('invoiced','shipped') THEN '异常-无明细但已发货/已开票'
        WHEN t3.order_id IS NULL THEN '无明细订单'
        ELSE NULL
    END AS quality_flag,
    CURRENT_TIMESTAMP as create_date,
    CURRENT_TIMESTAMP as modified_date
FROM
    olist_orders_dataset t1
LEFT JOIN olist_customers_dataset t2 ON t1.customer_id = t2.customer_id
LEFT JOIN
    (
        SELECT order_id,
            count(*) as item_count,
            sum(price) as gmv,
            sum(freight_value) as freight_total
        FROM
            dwd_order_detail
        WHERE purchase_date BETWEEN %(start_date)s AND %(end_date)s
        GROUP BY
            order_id
    ) t3
ON t1.order_id = t3.order_id
WHERE t1.order_purchase_timestamp::date BETWEEN %(start_date)s AND %(end_date)s;