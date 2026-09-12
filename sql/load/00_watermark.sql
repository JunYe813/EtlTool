CREATE TABLE IF NOT EXISTS etl_watermark (
    job_name    varchar(50) PRIMARY KEY,   -- 当前用 'incremental'；预留按层拆分（'dwd'/'dws'/'ads'）
    last_date   date NOT NULL,             -- 已处理到的购买日（只前进不后退，见 run_all.upsert_watermark）
    update_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);