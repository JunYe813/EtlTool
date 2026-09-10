-- ======ODS（原始层）======
-- 建表 DDL。全部带 IF NOT EXISTS，可重复执行（幂等），
-- 数据导入由 scripts/olist_import.py 负责（按 imp_file_log 跳过已导入文件）。

-- 文件导入日志表（循环导入本地文件，如果表中已存在该文件就跳过不导入）
CREATE TABLE IF NOT EXISTS imp_file_log(
    file_name varchar(100) PRIMARY KEY,
    row_count int,
    import_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 客户（买家表）
CREATE TABLE IF NOT EXISTS olist_customers_dataset(
    customer_id varchar(100),
    customer_unique_id varchar(100),
    customer_zip_code_prefix varchar(10),
    customer_city varchar(50),
    customer_state varchar(10),
    import_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_file varchar(100)
);
CREATE INDEX IF NOT EXISTS idx_customers_index ON olist_customers_dataset (customer_id,customer_unique_id);

-- 订单主表
CREATE TABLE IF NOT EXISTS olist_orders_dataset(
    order_id varchar(100),
    customer_id varchar(100),
    order_status varchar(20),
    order_purchase_timestamp TIMESTAMP,
    order_approved_at TIMESTAMP,
    order_delivered_carrier_date TIMESTAMP,
    order_delivered_customer_date TIMESTAMP,
    order_estimated_delivery_date TIMESTAMP,
    import_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_file varchar(100)
);
CREATE INDEX IF NOT EXISTS idx_orders_index ON olist_orders_dataset (order_id,customer_id);

-- 订单子表（明细）
CREATE TABLE IF NOT EXISTS olist_order_items_dataset(
    order_id varchar(100),
    order_item_id varchar(10),
    product_id varchar(100),
    seller_id varchar(100),
    shipping_limit_date TIMESTAMP,
    price NUMERIC(10,2),
    freight_value NUMERIC(10,2),
    import_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_file varchar(100)
);
CREATE INDEX IF NOT EXISTS idx_orders_items_index ON olist_order_items_dataset (order_id,product_id);

-- 支付表
CREATE TABLE IF NOT EXISTS olist_order_payments_dataset(
    order_id varchar(100),
    payment_sequential int,
    payment_type varchar(50),
    payment_installments int,
    payment_value NUMERIC(10,2),
    import_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_file varchar(100)
);
CREATE INDEX IF NOT EXISTS idx_orders_payments_index ON olist_order_payments_dataset (order_id,payment_sequential);

-- 评价表
CREATE TABLE IF NOT EXISTS olist_order_reviews_dataset(
    review_id varchar(100),
    order_id varchar(100),
    review_score int,
    review_comment_title varchar(200),
    review_comment_message text,
    review_creation_date TIMESTAMP,
    review_answer_timestamp TIMESTAMP,
    import_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_file varchar(100)
);
CREATE INDEX IF NOT EXISTS idx_orders_reviews_index ON olist_order_reviews_dataset (order_id,review_id);

-- 商品表
-- 注意：product_name_lenght / product_description_lenght 是**源数据的拼写错误**
-- （应为 length），这里保留原名以便与原 CSV 逐列对应，DWD 层才做列改名/清洗。
CREATE TABLE IF NOT EXISTS olist_products_dataset(
    product_id varchar(100),
    product_category_name varchar(100),
    product_name_lenght int,
    product_description_lenght int,
    product_photos_qty int,
    product_weight_g int,
    product_length_cm int,
    product_height_cm int,
    product_width_cm int,
    import_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_file varchar(100)
);
CREATE INDEX IF NOT EXISTS idx_orders_products_index ON olist_products_dataset (product_id);

-- 卖家表（店铺）
CREATE TABLE IF NOT EXISTS olist_sellers_dataset(
    seller_id varchar(100),
    seller_zip_code_prefix varchar(10),
    seller_city varchar(100),
    seller_state varchar(10),
    import_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_file varchar(100)
);
CREATE INDEX IF NOT EXISTS idx_orders_sellers_index ON olist_sellers_dataset (seller_id);

-- 类目翻译表（葡语 → 英文）
CREATE TABLE IF NOT EXISTS product_category_name_translation(
    product_category_name text,
    product_category_name_english text,
    import_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_file varchar(100)
);

-- 未建表：olist_geolocation_dataset（60MB，同一邮编多条经纬度需去重，业务价值低）
-- 详见 docs/数据字典.md 第二节第 8 条
