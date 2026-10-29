-- ============================================================================
-- schema_v5.sql —— V5 数据层建表脚本（逐步补齐）
--
-- V5 约定：
--   1. trade_date 一律 VARCHAR(8) 的 YYYYMMDD（如 20261028），与接口参数、
--      raw 文件名、目录名完全一致；交易日历运算查 dim_trade_date。
--   2. 事实表只存客观数据；watchlist 是“每天全量”的状态表，按
--      (experiment_id, owner, trade_date) 唯一，历史只写一次。
--   3. 除 raw 外不写中间文件；所有加工结果落表。
--
-- 当前实现：watchlist_daily / watchlist_change。
-- 后续补齐：dim_trade_date、fact_*（行情/涨跌停/板块）、metric_*。
-- ============================================================================

-- 每天全量清单：一个实验 + 一个所有者 + 一个交易日 = 一份不可变快照
CREATE TABLE IF NOT EXISTS watchlist_daily (
    experiment_id VARCHAR NOT NULL,          -- 'live' 或实验号 'E008'
    owner         VARCHAR NOT NULL,          -- 'judge' / 'user'
    trade_date    VARCHAR NOT NULL,          -- 哪天收盘后定的（YYYYMMDD）
    ts_code       VARCHAR NOT NULL,
    name          VARCHAR,
    role          VARCHAR,                   -- 龙头/中军/弹性/前排…
    level         VARCHAR,                   -- anchor / core / npc …
    theme_id      VARCHAR,
    reason        VARCHAR,
    first_added   VARCHAR,                   -- 首次进入清单的日期（YYYYMMDD）
    gmt_created   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (experiment_id, owner, trade_date, ts_code),
    CHECK (length(trade_date) = 8 AND length(first_added) = 8),
    CHECK (owner IN ('judge', 'user'))
);

CREATE INDEX IF NOT EXISTS watchlist_daily_lookup
    ON watchlist_daily (experiment_id, owner, trade_date);

-- 变更日志：只记“为什么变”，不参与“看到什么”的查询
CREATE TABLE IF NOT EXISTS watchlist_change (
    experiment_id VARCHAR NOT NULL,
    owner         VARCHAR NOT NULL,
    trade_date    VARCHAR NOT NULL,
    ts_code       VARCHAR NOT NULL,
    action        VARCHAR NOT NULL,          -- add / remove / update
    role          VARCHAR,
    level         VARCHAR,
    reason        VARCHAR,
    gmt_created   TIMESTAMP DEFAULT current_timestamp,
    CHECK (action IN ('add', 'remove', 'update')),
    CHECK (owner IN ('judge', 'user'))
);

CREATE INDEX IF NOT EXISTS watchlist_change_lookup
    ON watchlist_change (experiment_id, owner, trade_date);
