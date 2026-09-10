-- ====== ADS：卖家榜 ======
-- 服务看板「卖家与地区」section 的「卖家 Top」榜单
--
-- 为什么需要这张表：dws_sale_daily 的粒度只到 seller_state，没有卖家粒度，
-- 拿不到「卖家 Top」。这里单独落一张卖家粒度的表（仅 3000+ 行，很轻）。
--
-- ⚠️ 同 dws_sale_daily 的警告：order_cnt 跨卖家不可相加（一单可多卖家），
--    本表只能横向比排名，SUM(order_cnt) ≠ 平台有效订单数。

DROP TABLE IF EXISTS ads_top_seller CASCADE;

CREATE TABLE ads_top_seller(
    seller_id     varchar(100) PRIMARY KEY,
    seller_state  varchar(10),
    seller_city   varchar(100),
    order_cnt     int,
    item_cnt      int,
    gmv           NUMERIC(14,2),
    avg_item_price NUMERIC(10,2),
    create_date   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO ads_top_seller
(seller_id, seller_state, seller_city, order_cnt, item_cnt, gmv, avg_item_price)
SELECT
    seller_id,
    MAX(seller_state),                                   -- 卖家与州是 1:1，取 MAX 仅为满足 GROUP BY
    MAX(seller_city),
    COUNT(DISTINCT order_id)          AS order_cnt,
    COUNT(*)                          AS item_cnt,
    ROUND(SUM(price), 2)              AS gmv,
    ROUND(AVG(price), 2)              AS avg_item_price
FROM dwd_order_detail
WHERE is_valid
GROUP BY seller_id;

-- 看板按 gmv 倒序取 TopN，建索引
CREATE INDEX IF NOT EXISTS idx_ads_top_seller_gmv ON ads_top_seller (gmv DESC);


-- ============================== 校验 ==============================
-- 卖家数：全部卖家 3095 个，其中有有效订单的 3053 个（其余 42 个卖家只有取消/未支付订单）
-- SELECT COUNT(*), ROUND(SUM(gmv),2) FROM ads_top_seller;
-- Top10 榜单
-- SELECT seller_id, seller_state, order_cnt, gmv FROM ads_top_seller ORDER BY gmv DESC LIMIT 10;
