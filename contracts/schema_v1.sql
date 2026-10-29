-- ============================================================================
-- schema_v1.sql —— 首分机器人 DuckDB 数仓建表契约 v1.2
--   v1.1（2026-10-29）：新增 ⑪ dim_board_member
--   v1.2（2026-10-29）：⑪ 加 trade_date 列，成分按快照日存储（历史回测取 ≤ as_of 最新快照）
-- 日期：2026-10-27。本文件既是建表脚本也是契约：改结构必须 bump 版本并更新
-- ARCHITECTURE.md / PRD（一致性工程，见 ARCHITECTURE.md §四）。
--
-- 设计原则（PRD §十 / ARCHITECTURE.md）：
--   1. 主键 = 数据粒度（行情: 日×票；板块: 日×板块；细节: 日×票×类型）
--   2. 每表带 source_file + ingested_at 溯源列（审计要求）
--   3. trade_date 统一 DATE（YYYYMMDD 解析）；原始字符串留在 raw 证据层
--   4. 不建二级索引、不手动分区（列存自动微分区，当前量级十年无忧）
--   5. 单位保持 Tushare 原生口径：amount=千元, vol=手（见 snapshot.yaml 字段词典）
-- ============================================================================

-- ① 行情事实表：股票 + 指数共用（字段同构，ts_code 区分；指数代码 000001.SH 等）
CREATE TABLE IF NOT EXISTS fact_daily_bar (
    trade_date  DATE    NOT NULL,
    ts_code     VARCHAR NOT NULL,
    open        DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    pre_close   DOUBLE, change DOUBLE, pct_chg DOUBLE,
    vol         DOUBLE,          -- 手
    amount      DOUBLE,          -- 千元
    source_file VARCHAR,         -- 证据层文件名（可追溯）
    ingested_at TIMESTAMP,       -- 入库时间
    PRIMARY KEY (trade_date, ts_code)
);

-- ② 涨跌停价格（涨停判定基准）
CREATE TABLE IF NOT EXISTS fact_stk_limit (
    trade_date DATE NOT NULL,
    ts_code    VARCHAR NOT NULL,
    up_limit   DOUBLE,
    down_limit DOUBLE,
    source_file VARCHAR,
    ingested_at TIMESTAMP,
    PRIMARY KEY (trade_date, ts_code)
);

-- ③ Scout 加工产物：涨跌停标记（本地判定主口径，含 ST/北交所）
CREATE TABLE IF NOT EXISTS fact_limit_flag (
    trade_date DATE NOT NULL,
    ts_code    VARCHAR NOT NULL,
    flag       VARCHAR,          -- 涨停(一字) / 涨停(回封) / 跌停 / 正常
    source_file VARCHAR,
    ingested_at TIMESTAMP,
    PRIMARY KEY (trade_date, ts_code)
);

-- ④ 官方涨跌停结构（limit_list_d；注意：官方口径不含 ST，与③互为核对不混用）
CREATE TABLE IF NOT EXISTS fact_limit_detail (
    trade_date  DATE NOT NULL,
    ts_code     VARCHAR NOT NULL,
    limit_type  VARCHAR NOT NULL,   -- U涨停 / D跌停 / Z炸板
    industry    VARCHAR,
    close       DOUBLE, pct_chg DOUBLE, amount DOUBLE,
    fd_amount   DOUBLE,             -- 封单金额
    float_mv    DOUBLE,             -- 流通市值
    turnover_ratio DOUBLE,          -- 换手率
    first_time  VARCHAR, last_time VARCHAR,
    open_times  INTEGER,            -- 开板次数（0=未开板，竞价一字/T字由此区分）
    up_stat     VARCHAR,            -- X天Y板
    limit_times INTEGER,            -- 连板数
    name        VARCHAR,            -- 名称（官方源自带，避免 join dim_stock）
    source_file VARCHAR,
    ingested_at TIMESTAMP,
    PRIMARY KEY (trade_date, ts_code, limit_type)
);

-- ⑤ 连板天梯（limit_step）
CREATE TABLE IF NOT EXISTS fact_limit_step (
    trade_date DATE NOT NULL,
    ts_code    VARCHAR NOT NULL,
    name       VARCHAR,
    nums       INTEGER,             -- 连板次数
    source_file VARCHAR,
    ingested_at TIMESTAMP,
    PRIMARY KEY (trade_date, ts_code)
);

-- ⑥ 板块榜（limit_cpt_list，同花顺概念口径；板块规模阈值过滤在 select 层做）
CREATE TABLE IF NOT EXISTS fact_board_rank (
    trade_date DATE NOT NULL,
    board_code VARCHAR NOT NULL,    -- 885xxx.TI
    board_name VARCHAR,
    rank       INTEGER,
    pct_chg    DOUBLE,              -- 板块涨幅%
    up_nums    INTEGER,             -- 涨停家数
    cons_nums  INTEGER,             -- 连板家数
    up_stat    VARCHAR,             -- 板块内最高板（X天Y板）
    days       INTEGER,             -- 上榜天数
    source_file VARCHAR,
    ingested_at TIMESTAMP,
    PRIMARY KEY (trade_date, board_code)
);

-- ⑦ 开盘啦涨跌停榜（kpl_list；次日 8:30 更新；竞价/封单细节独家来源）
CREATE TABLE IF NOT EXISTS fact_kpl_list (
    trade_date    DATE NOT NULL,
    ts_code       VARCHAR NOT NULL,
    tag           VARCHAR,          -- 涨停/跌停/炸板...
    status        VARCHAR,          -- 首板 / N连板
    theme         VARCHAR,          -- 开盘啦题材标签（多题材顿号分隔）
    lu_desc       VARCHAR,          -- 涨停原因
    lu_time       VARCHAR, open_time VARCHAR, last_time VARCHAR,
    limit_order   DOUBLE,           -- 封单
    lu_limit_order DOUBLE,          -- 最大封单
    amount        DOUBLE, turnover_rate DOUBLE, free_float DOUBLE,
    net_change    DOUBLE,           -- 主力净额
    source_file VARCHAR,
    ingested_at TIMESTAMP,
    PRIMARY KEY (trade_date, ts_code, tag)
);

-- ⑧ 广度指标（每日一行；新指标先走 metric_kv 试错，稳定后 ALTER 进本表）
CREATE TABLE IF NOT EXISTS fact_breadth (
    trade_date            DATE PRIMARY KEY,
    up_cnt                INTEGER,   -- 上涨家数
    down_cnt              INTEGER,   -- 下跌家数
    limit_up_cnt          INTEGER,   -- 涨停家数（含ST，本地口径）
    limit_down_cnt        INTEGER,   -- 跌停家数（含ST）
    gt5_cnt               INTEGER,   -- 涨幅>5%家数
    total_amount_yi       DOUBLE,    -- 两市成交额（亿元）
    prev_lu_cnt           INTEGER,   -- 昨涨停家数
    prev_lu_open_premium  DOUBLE,    -- 昨涨停今开盘均价涨幅%
    prev_lu_close_premium DOUBLE,    -- 昨涨停今收盘均价涨幅%
    prev_ld_feedback      DOUBLE,    -- 昨跌停今收盘均价涨幅%
    source_file VARCHAR,
    ingested_at TIMESTAMP
);

-- ⑨ 实验指标长表：新指标试错区（验证有用后才升格进 fact_breadth）
CREATE TABLE IF NOT EXISTS metric_kv (
    trade_date DATE NOT NULL,
    metric_name VARCHAR NOT NULL,
    value       DOUBLE,
    source      VARCHAR,
    ingested_at TIMESTAMP,
    PRIMARY KEY (trade_date, metric_name, source)
);

-- ⑩ 股票维度表（周更；历史快照留在 raw 证据层，本表只存最新）
CREATE TABLE IF NOT EXISTS dim_stock (
    ts_code     VARCHAR PRIMARY KEY,
    name        VARCHAR,
    industry    VARCHAR,
    market      VARCHAR,            -- 主板/创业板/科创板/北交所
    list_date   DATE,
    list_status VARCHAR,
    updated_at  TIMESTAMP
);

-- ⑪ 同花顺板块成分（ths_member；**带 trade_date 的快照维度表**：每次取数盖决策日时间戳，
--     历史回测时查询取 trade_date <= as_of 的最新快照。raw 只追加（按日期命名），
--     供审计与重建。ths_member 只暴露"当前最新"成分（is_new=Y），成分调整的历史
--     变更无法从接口恢复——回测精度 = 快照粒度，见 metadata limitation）
CREATE TABLE IF NOT EXISTS dim_board_member (
    trade_date  DATE NOT NULL,      -- 本快照服务的决策日（取数时 --date）
    board_code  VARCHAR NOT NULL,   -- 885xxx.TI
    con_code    VARCHAR NOT NULL,   -- 成分股 ts_code
    con_name    VARCHAR,
    is_new      VARCHAR,            -- Y=当前最新成分（N=已剔除，查询只用 Y）
    source_file VARCHAR,
    ingested_at TIMESTAMP,
    PRIMARY KEY (trade_date, board_code, con_code)
);
