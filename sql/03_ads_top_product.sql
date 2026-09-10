-- SELECT * FROM ads_top_product LIMIT 3;
DROP TABLE IF EXISTS ads_top_product CASCADE;
-- 需求：每天销售额 Top 10 商品
--
-- ⚠️ 必须给 ORDER BY 加确定性 tiebreaker（product_id）：
--    Olist 里有 20 天存在「第 10 名与第 11 名 GMV 完全相同」的并列情况，
--    ROW_NUMBER() 对并列值的排名分配是任意的 —— 两次重跑会选出不同的商品入榜，
--    导致结果不可复现（幂等性验证会失败）。
--    加 product_id 兜底后，排序完全确定，同样输入必得同样输出。
CREATE TABLE ads_top_product(
    purchase_date date,
    product_id    varchar(100),
    category_name varchar(100),
    gmv           NUMERIC(14,2),
    rank_no       int,
    PRIMARY KEY (purchase_date, product_id)
);
INSERT INTO ads_top_product (purchase_date, product_id, category_name, gmv, rank_no)
WITH top_product as (
    SELECT
        product_id,
        category_name,
        purchase_date,
        sum(price) as gmv,
        ROW_NUMBER() OVER (
            PARTITION BY purchase_date
            ORDER BY sum(price) DESC, product_id   -- product_id 为并列兜底，保证可复现
        ) as rank_no
    FROM
        dwd_order_detail
    WHERE
        is_valid
    GROUP BY
        product_id,category_name,purchase_date
)
SELECT
    purchase_date,
    product_id,
    category_name,
    gmv,
    rank_no
FROM
    top_product
WHERE
    rank_no <= 10;

CREATE INDEX IF NOT EXISTS idx_ads_top_product_date_rank ON ads_top_product (purchase_date, rank_no);

-- select * FROM ads_top_product WHERE purchase_date = '2016-10-03' ORDER BY rank_no ASC;