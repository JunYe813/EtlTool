-- ======DWD（清洗层）======
-- =====================================明细表sql
-- 口径：明细粒度，一单多行（一单可多商品、多卖家）
--   category_name : 葡语类目 LEFT JOIN 翻译表取英文，无匹配/空值 → 'unknown'
--   seller_state / seller_city : 维度退化，卖家维度冗余进明细宽表
--   purchase_date : 用下单日（销售日口径写死为 order_purchase_timestamp）
--   is_valid      : 与订单主表同一口径（见 config.py VALID_STATUSES）
--
-- 关于 CASCADE：04_views.sql 里的视图依赖本文件的表（如 v_fulfillment_funnel 依赖 dwd_order）。
-- DWD 是全量重建（DROP TABLE + CREATE），PostgreSQL 不允许删除仍有依赖的对象，
-- 第二次执行会报 DependentObjectsStillExist。用 CASCADE 连带删掉派生视图，
-- 视图随后由 04_views.sql 统一重建 —— 这样每个 SQL 文件都能独立重跑，不依赖执行顺序。
DROP TABLE IF EXISTS dwd_order_detail CASCADE;

CREATE TABLE dwd_order_detail(
    order_id varchar(100),
    order_item_id int,
    product_id varchar(100),
    category_name varchar(100),
    seller_id varchar(100),
    seller_state varchar(10),
    seller_city varchar(100),
    purchase_date  date,
    order_status   varchar(50),
    is_valid       boolean,
    price NUMERIC(10,2),
    freight_value NUMERIC(10,2),
    create_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (order_id, order_item_id)
);

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
LEFT JOIN olist_orders_dataset t5 ON t1.order_id = t5.order_id;



-- ===============================主表sql
-- 口径：订单粒度，一单一行
--   customer_unique_id : 唯一买家（customer_id 会重复，不等于唯一买家）
--   gmv                : 该订单明细 Σprice（不含运费），由明细表聚合而来
--   freight_total      : 该订单明细 Σfreight_value
--   is_valid           : 有效订单（已完成支付）
--   quality_flag       : 数据质量标记，NULL 表示正常
DROP TABLE IF EXISTS dwd_order CASCADE;

CREATE TABLE dwd_order(
    order_id varchar(100),
    customer_id varchar(100),
    customer_unique_id varchar(100),
    customer_state varchar(10),
    customer_city varchar(50),
    order_status varchar(20),
    purchase_at TIMESTAMP,
    approved_at TIMESTAMP,
    delivered_carrier_at TIMESTAMP,
    delivered_customer_at TIMESTAMP,
    estimated_delivery_at TIMESTAMP,
    item_count int,
    gmv NUMERIC(10,2),
    freight_total NUMERIC(10,2),
    is_valid BOOLEAN,
    quality_flag varchar(20),
    create_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (order_id)
);

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
        GROUP BY
            order_id
    ) t3
ON t1.order_id = t3.order_id;


-- ===============================数据质量核对
-- 口径说明：is_valid 只由 order_status 决定（见 docs/数据字典.md 第五节），
-- 因此存在 3 笔 is_valid=true 但无明细的订单（invoiced/shipped，gmv=0），
-- quality_flag 已标记为 '异常-无明细但已发货/已开票'，保留待业务确认，不在 DWD 层擅自改口径。
-- 影响：ADS 订单数 / 客单价分母包含这 3 单（占比 3/98202），GMV 不受影响。
-- 如需剔除，在 ADS 层加 AND item_count > 0，而不是改这里的 is_valid。

-- 统计数据情况
-- SELECT
--     count(*) as total_count,
--     SUM(CASE WHEN quality_flag is NOT NULL THEN 1 ELSE 0 END) as abnormal_count,
--     SUM(CASE WHEN is_valid THEN 1 ELSE 0 END) as valid_count
-- FROM
--     dwd_order;

-- 计算gmv（总成交额）
-- SELECT sum(gmv) from dwd_order WHERE is_valid;
