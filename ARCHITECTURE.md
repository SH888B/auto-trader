# 首分机器人架构 v5

- 版本：v5.0
- 日期：2026-10-30
- 状态：**设计定稿，分阶段实施**
- 取代：[docs/archive/ARCHITECTURE-v3.md](docs/archive/ARCHITECTURE-v3.md)
- 已落地：取数账本 / 就绪门（PR-1）、取数契约 `sources.yaml`、watchlist 每日全量表
- 过渡说明：迁移完成前，旧路径（`data/curated`、`data/snapshots`、`state/*.yaml`）仍然存在；以本文 §13 的阶段为准

---

## 0. 一句话

**外部数据落一次 raw → 全部进 DuckDB → 逻辑层按 `as_of` 查库加工 → 生成 prompt → 交给 LLM 复盘/交易。**

没有 curated，没有 derived，没有 snapshot 目录。只有两个持久层：**raw** 和 **DuckDB**；只有一个视图产物：**模型看到的 prompt 文本**。

---

## 1. 为什么改

| v3 的问题 | v5 的做法 |
|---|---|
| 四层存储：raw → curated → derived → 数仓 → snapshot | 两层：raw + DuckDB；中间结果只在内存/表里 |
| 同一份数据两条读路径（Digest 读 CSV，查询走 SQL） | 只有 SQL 一条路径 |
| `snapshot` 同时承担数据、版本、产物、门禁五种职责 | 不存中间产物；raw 冻结 + prompt 文本就是证据 |
| watchlist 是 YAML + history 目录 | 两张表：`watchlist_daily` / `watchlist_change` |
| 日期三种格式（`20261028` / `2026-10-28` / DATE） | 对外统一 `YYYYMMDD` |
| 指标散在 415 行的 `select_stocks` 里 | 指标是命名的 SQL/函数，输入 `as_of`，输出表或文本 |

---

## 2. 三条原则

1. **原始数据只落一次，不可变。** `data/raw/` 是唯一事实源；删了任何中间层都能从 raw 重建。
2. **除 raw 外不写中间文件。** 所有加工结果进 DuckDB；需要给模型看的，只生成 `prompt.md`。
3. **模型看到的文本是唯一需要冻结的视图产物。** 存 `prompt.md` 比存 16 张 CSV 更有用、更小、更直接。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ ① 源数据层                                                        │
│                                                                  │
│   外部接口 ──fetch──▶ data/raw/<api>/<yyyymmdd>.json              │
│                       （只增不改，一个文件一个 sha256）             │
│                              │                                   │
│                              ▼  build（幂等，可从 raw 全量重建）    │
│                       data/warehouse.duckdb                       │
│                       fact_* / dim_* / watchlist_*                │
└──────────────────────────────┬───────────────────────────────────┘
                               │ SQL（按 as_of 切一刀）
┌──────────────────────────────▼───────────────────────────────────┐
│ ② 逻辑层（全部在代码里，不落中间文件）                              │
│                                                                  │
│   metrics   指标/事实：涨停结构、板块阶段、载体状态、广度…          │
│   context   选哪些事实、怎么压缩、按什么顺序 → facts.json           │
│   render    facts → prompt.md（先表格；v5.1 加中文描述）            │
└──────────────────────────────┬───────────────────────────────────┘
                               │ prompt.md + facts.json
┌──────────────────────────────▼───────────────────────────────────┐
│ ③ 使用层                                                          │
│                                                                  │
│   Judge  ──▶ review.json ──▶ watchlist_daily（当晚全量）           │
│   Trader ──▶ order_intents.json                                   │
│   Broker（人工）──▶ orders / fills                                 │
│   Evaluator ──▶ scores                                            │
│                                                                  │
│   产物：runs/<yyyymmdd>/<module>/{prompt.md, facts.json, 输出}      │
└──────────────────────────────────────────────────────────────────┘
```

---

## 4. 目录结构（目标）

```text
data/
├── raw/<api>/<yyyymmdd>.json          # 原始响应（唯一事实源）
├── warehouse.duckdb                   # 所有表；可删可重建
├── meta/fetch_ledger.jsonl            # 取数账本（append-only）
├── meta/readiness/<yyyymmdd>.yaml     # 当日数据就绪度
└── packs/<pack_id>/warehouse.duckdb   # 实验冻结 = 数据库副本

configs/
├── sources.yaml                       # 取数契约（接口/可用时间/重试/分级）
├── metrics.yaml                       # 指标定义（v5.1）
├── views/judge.yaml                   # 复盘看什么、多少条、什么顺序
├── views/trader.yaml                  # 交易看什么
└── narrator.yaml                      # 中文描述模板（v5.1）

runs/<yyyymmdd>/<module>/
├── prompt.md                          # 模型看到的全部内容（含头部溯源）
├── facts.json                         # 结构化事实（可选但建议）
├── manifest.yaml                      # data_cutoff / pack / git commit / query 列表
└── review.json | order_intents.json   # 模型输出

experiments/<id>/
├── experiment.yaml
├── bundle/                            # profile / 因子 / 视图配置（文本冻结）
├── runs/<yyyymmdd>/...                # 同 runs 结构
└── scores/

docs/archive/                          # 旧架构文档（只读）
```

不再有：`data/curated/`、`data/snapshots/`、`state/watchlist/*.yaml`、`state/portfolio/*.yaml`、`snapshot.yaml`。

---

## 5. 数据库设计

### 5.1 约定

| 约定 | 说明 |
|---|---|
| `trade_date` | `VARCHAR(8)` 的 `YYYYMMDD`（如 `20261028`）。与接口参数、raw 文件名、目录名完全一致 |
| 主键 | 等于数据粒度：行情=日×票；涨跌停=日×票×类型；板块=日×板块 |
| 审计列 | 事实表**不放** `source_file` / `ingested_at`；溯源在账本与 raw 文件 |
| 时间 | 统一 `+08:00`；`gmt_created TIMESTAMP` |
| 追加 | `fact_*` 只按 `trade_date` 追加/替换整日；不改历史行 |
| 单位 | 沿用 Tushare：`amount` 千元、`vol` 手；在 `metrics.yaml`/字段词典里写清 |

### 5.2 表清单

| 表 | 粒度 / 主键 | 状态 | 说明 |
|---|---|---|---|
| `dim_trade_date` | `trade_date` | 待建 | 交易日历；`prev_trade_date` / `next_trade_date`；所有窗口运算用它 |
| `dim_stock` | `ts_code, valid_from` | 待建 | SCD2 主档；ST/名称按历史生效，不用"今天"的名字 |
| `dim_board_member` | `trade_date, board_code, con_code` | 待建 | 同花顺板块成分快照 |
| `fact_daily_bar` | `trade_date, ts_code` | 待建 | 股票 + 指数日线 |
| `fact_stk_limit` | `trade_date, ts_code` | 待建 | 涨跌停价（本地判定基准） |
| `fact_limit_detail` | `trade_date, ts_code, limit_type` | 待建 | 涨停/跌停/炸板结构（封单、开板次数、连板数） |
| `fact_board_rank` | `trade_date, board_code` | 待建 | 板块榜（涨幅/涨停家数/最高板/持续天数） |
| `fact_kpl` | `trade_date, ts_code, tag` | 待建 | 开盘啦涨停细节 + 题材标签 |
| `fact_breadth` | `trade_date` | 待建 | 广度指标（涨跌家数、昨涨停溢价、成交额） |
| `watchlist_daily` | `experiment_id, owner, trade_date, ts_code` | **已建** | 每日全量清单（不可变） |
| `watchlist_change` | 无主键（日志） | **已建** | 当日变更：add/remove/update + 原因 |
| `portfolio_daily` | `experiment_id, trade_date` | 待建 | 现金/总资产快照 |
| `position_daily` | `experiment_id, trade_date, ts_code` | 待建 | 持仓快照 |
| `order_intent` | `run_id, ts_code` | 待建 | Trader 订单意图（可由 JSON 落表） |
| `fill` | `run_id, ts_code, fill_time` | 待建 | 人工 Broker 成交回填 |
| `metric_theme_state` | `trade_date, theme_id` | v5.1 | 题材阶段/强度/切换窗口 |
| `metric_carrier_state` | `trade_date, ts_code` | v5.1 | 载体角色/连板/开板/封单 |

### 5.3 核心 DDL

```sql
-- 交易日历：所有 "前一日 / 近 5 日 / 区间" 运算都查它
CREATE TABLE dim_trade_date (
    trade_date      VARCHAR PRIMARY KEY,   -- YYYYMMDD
    is_open         BOOLEAN NOT NULL,
    prev_trade_date VARCHAR,
    next_trade_date VARCHAR,
    CHECK (length(trade_date) = 8)
);

-- 行情：股票与指数同构
CREATE TABLE fact_daily_bar (
    trade_date  VARCHAR NOT NULL,
    ts_code     VARCHAR NOT NULL,
    open        DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    pre_close   DOUBLE, change DOUBLE, pct_chg DOUBLE,
    vol         DOUBLE,      -- 手
    amount      DOUBLE,      -- 千元
    available_at TIMESTAMP,  -- 该行最早可用的时刻（+08:00）
    PRIMARY KEY (trade_date, ts_code)
);

-- 涨跌停结构（官方 limit_list_d，U/D/Z）
CREATE TABLE fact_limit_detail (
    trade_date  VARCHAR NOT NULL,
    ts_code     VARCHAR NOT NULL,
    limit_type  VARCHAR NOT NULL,   -- U / D / Z
    name        VARCHAR,
    industry    VARCHAR,
    close       DOUBLE, pct_chg DOUBLE, amount DOUBLE,
    fd_amount   DOUBLE,             -- 封单额
    float_mv    DOUBLE,
    turnover_ratio DOUBLE,
    first_time  VARCHAR, last_time VARCHAR,
    open_times  INTEGER,
    up_stat     VARCHAR,            -- X天Y板
    limit_times INTEGER,
    available_at TIMESTAMP,
    PRIMARY KEY (trade_date, ts_code, limit_type)
);

-- 每日全量 watchlist（已实现，见 contracts/schema_v5.sql）
CREATE TABLE watchlist_daily (
    experiment_id VARCHAR NOT NULL,
    owner         VARCHAR NOT NULL,          -- judge / user
    trade_date    VARCHAR NOT NULL,
    ts_code       VARCHAR NOT NULL,
    name          VARCHAR,
    role          VARCHAR,
    level         VARCHAR,
    theme_id      VARCHAR,
    reason        VARCHAR,
    first_added   VARCHAR,
    gmt_created   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (experiment_id, owner, trade_date, ts_code)
);
```

### 5.4 时点隔离：开库先切一刀

所有逻辑只拿到 `as_of` 之前的数据，不靠每个查询自觉：

```python
con = duckdb.connect("data/warehouse.duckdb", read_only=True)

# 1) 行情：T 日收盘后可以看 T（当晚复盘）
con.execute("""
    CREATE TEMP TABLE fact_daily_bar AS
    SELECT * FROM fact_daily_bar
    WHERE trade_date <= ? AND available_at <= ?
""", [as_of, cutoff_ts])

# 2) watchlist：Judge 只能看前一晚决定的清单（<= T-1）
con.execute("""
    CREATE TEMP TABLE watchlist_daily AS
    SELECT * FROM watchlist_daily
    WHERE trade_date <= ? AND experiment_id = ? AND owner = ?
""", [prev_day, experiment, "judge"])
```

临时表会遮住同名主表；逻辑层里写 `SELECT * FROM fact_daily_bar` 就自动只剩历史。**这是物理隔离，不靠 code review。**

两个截止时间必须分清：

| 数据 | 截止 | 原因 |
|---|---|---|
| 行情 / 涨跌停 / 板块 | `<= T` | 复盘在 T 日收盘后，当晚数据已出 |
| Judge watchlist（输入） | `<= T-1` | T 日的清单是 Judge 当晚才产出的，不能自我引用 |
| User watchlist（输入） | `<= T-1` | 与 Judge 同规则；用户收盘后维护，当晚生效于下一交易日 |
| 当晚输出 | `trade_date = T` | 记录"哪晚定的" |

### 5.5 重建与校验

- `data build --date D`：raw → 表，幂等；同一天重跑覆盖该日分区。
- `data rebuild --start A --end B`：按交易日顺序重建；先删该区间分区，再插入。
- `data verify --date D`：逐表核对 `raw 行数 = 表行数`、主键无重复、`available_at` 不为空；输出 `readiness`。
- 确定性要求：同一份 raw 重跑两次，**按业务列排序后 hash 相同**。因此事实表不放时间戳列。

---

## 6. 数据源接口

### 6.1 契约文件：`configs/sources.yaml`

每个接口声明"怎么调、期望什么、何时可用、缺了算几级"，代码不再硬编码。

```yaml
apis:
  daily:
    api: daily
    window: evening
    key: "{date}"                 # raw 文件名：daily_20261028.json
    params: {trade_date: "{date}"}
    page: {size: 5000, max_pages: 4, pk: [ts_code]}
    expect:
      min_rows: 4500
      max_rows: 7000
      fields: [ts_code, trade_date, close, pre_close, pct_chg, amount]
    avail: {day_offset: 0, after: "15:30", confidence: doc}
    retry: {read_timeout: 180}
    class: block                  # block / enrich / optional
```

| 字段 | 作用 |
|---|---|
| `key` / `params` | `{date}` 替换为 `YYYYMMDD`；决定 raw 文件名 |
| `page` | 分页大小、最大页数、主键（查重） |
| `expect` | 最小/最大行数、必需字段、是否允许空 |
| `avail` | 该交易日的数据最早何时可用；决定 `not_due` |
| `retry` | 单接口超时与退避 |
| `class` | `block` 缺失→不出 prompt；`enrich`→降级但继续；`optional`→忽略 |

### 6.2 接口清单

| 接口 | 内容 | 窗口 | 可用时间 | 分级 |
|---|---|---|---|---|
| `daily` | 全市场日线（分页 5000） | 收盘后 | T 15:30 | block |
| `stk_limit` | 涨跌停价 | 收盘后 | T 17:00 | block |
| `limit_list_d`（U/D） | 涨停/跌停结构 | 收盘后 | T 17:00 | U=block / D=enrich |
| `limit_cpt_list` | 同花顺板块榜 | 收盘后 | T 18:00 | enrich |
| `kpl_list` | 开盘啦涨停细节+题材 | 次日早 | T+1 08:30 | enrich |
| `ths_member` | 板块成分（按板块） | 收盘后 | T 18:00 | enrich |
| `daily_basic` | 换手/市值 | 收盘后 | T 17:00 | optional |
| `limit_step` | 连板天梯 | 收盘后 | T 18:00 | optional |
| `stock_basic` | 证券主档 | 周更 | 周六 | optional |
| `trade_cal` | 交易日历 | 年更 | — | block（本地缓存） |

### 6.3 失败处理

每次尝试写 `data/meta/fetch_ledger.jsonl`，状态机：

```text
NOT_DUE → DUE → OK | EMPTY_PENDING → EMPTY_CONFIRMED | FAILED → FAILED_FINAL
```

- `not_due`（没到可用时间）**不算缺口**；
- 空响应只有 `allow_empty` 的接口允许，且要过二次确认；
- 40101 不重试，直接换端点；参数错/积分不足不重试；
- 退出码：`0` 成功 / `3` pending 或 degraded / `4` blocked / `5` 配额耗尽。

`data/meta/readiness/<date>.yaml` 汇总：`ready / degraded / blocked`，`Daily.prepare` 与实验入口据此放行。

### 6.4 新增一个数据源

1. 在 `sources.yaml` 加一段（key/params/expect/avail/class）；
2. 如需入库，加一个 `build_<table>(date)` 函数，只读 raw、返回 DataFrame；
3. 在 `TABLE_BUILDERS` 注册（目标：自动扫描）；
4. 加一条 fixture 测试（超时/空/字段缺失）；
5. 更新本节接口清单。

---

## 7. 链路

### 7.1 数据链（无 LLM）

```bash
auto-trader data sync   --date 20261028          # 抓取 + 账本 + readiness
auto-trader data build  --date 20261028          # raw → DuckDB（幂等）
auto-trader data verify --date 20261028          # 行数/主键/覆盖度
```

产物：`data/raw/`、`warehouse.duckdb`、`meta/readiness/20261028.yaml`。
失败：`blocked` 停在此处，不生成 prompt。

### 7.2 复盘链（Judge）

```bash
auto-trader context build --date 20261028 --view judge
auto-trader judge prepare --date 20261028         # 出 handoff（含 prompt）
# 外部执行者读 prompt，落盘 review.md / review.json
auto-trader judge collect --date 20261028         # schema + 语义校验
auto-trader watchlist apply --review ... --date 20261028 --experiment live --owner judge
```

输入：`<= T` 的行情/结构 + `<= T-1` 的 watchlist + profile/因子。
输出：`runs/20261028/judge/{prompt.md, facts.json, manifest.yaml, review.json}` + 当日 watchlist 全量。

### 7.3 交易链（Trader）

```bash
auto-trader context build --date 20261028 --view trader
auto-trader trader prepare --date 20261028         # review + watchlist + 持仓
# 外部执行者落盘 order_intents.json
auto-trader trader collect --date 20261028
```

输入：当日 review（hash 引用）、Trader view、`<= T-1` 的 watchlist、`<= T` 的持仓。
输出：`order_intents.json`（三价、仓位、理由、引用的 review hash）。

### 7.4 执行链（人工 Broker）

- 只执行已校验的订单意图，不改价、不改仓位；
- `orders.json` / `fills.json` / `execution_receipt.json` 回填；
- `broker record --date D` 落表（v5.1），供 Evaluator 对账。

### 7.5 评价链

- `evaluator score --date D`：按冻结的评分口径机械对账；
- `evaluator attribute --period week|month`：周期归因（LLM 交接包）；
- 只形成晋升提案，不直接改 `framework/`。

### 7.6 实验链

```bash
auto-trader experiment setup E008
auto-trader experiment run E008 --start 20261001 --end 20261029
```

- 实验私有 `experiment_id`：watchlist / portfolio 都带这一列；
- 数据引用 pack（DuckDB 副本 hash），不复制数据；
- 同一 Judge review 可被多个 Trader arm 引用，arm 只换 Trader profile。

---

## 8. 交互与契约

### 8.1 `prompt.md`

```markdown
# Judge Prompt — 20261028
> as_of: 20261028 ｜ 行情截止: 20261028 收盘 ｜ watchlist 截止: 20261027
> view: judge@v1 ｜ data: p20260102_20261029@sha256:… ｜ git: abc1234

## 市场广度
…

## 板块与题材
…

## 涨停结构
…

## Judge 关注列表（<= 20261027）
…

## 数据缺口
- kpl_list: empty_confirmed（次日增强未到位）
```

规则：

- 头部必须有 `as_of`、两个截止时间、`view`、`data`（pack 或 raw manifest hash）、`git commit`；
- 所有数字来自 SQL，不得由 LLM 生成；
- 缺口显式写出，不隐藏。

### 8.2 `manifest.yaml`

```yaml
run_id: live-20261028-judge
module: judge
as_of: '20261028'
data_cutoff: '20261028T15:30:00+08:00'
watchlist_cutoff: '20261027'
view: judge@v1
git_commit: abc1234
prompt_sha256: '…'
pack: {id: p20260102_20261029, sha256: '…'}
queries:
  - {name: q_theme_state, rows: 8, sha256: '…'}
inputs:
  - {table: fact_daily_bar, rows: 5577}
  - {table: watchlist_daily, rows: 13}
```

### 8.3 模型输出契约

- `review.json`：`review.schema.json`（v1.1）+ 语义校验（首分必须有 `stage_basis`、`theme_id` 引用完整）；
- `order_intents.json`：`order_intents.schema.json` + 三价关系 + review hash 引用；
- 两者都保留 `contracts/` 校验，失败拒绝落盘。

### 8.4 两阶段交接

`prepare()` 出任务包（prompt + 输入清单 + 输出路径），`collect()` 验收产物。
失败：写 `logs/run_events.jsonl` 的 `error`，停在原地，重跑同一命令从断点继续。

---

## 9. Watchlist

### 9.1 语义

- 一个 `(experiment_id, owner, trade_date)` = 一份**不可变的全量清单**；
- 每天写全量；两天的差异就是"谁进了、谁出了、谁改了"；
- `trade_date` = **哪天收盘后定的**（`YYYYMMDD`）；
- Judge 在 T 日读 `<= T-1`，写 `T`；User 同规则；
- `owner` 只有 `judge` / `user`；`experiment_id` 用 `live` 或实验号。

### 9.2 表

见 `contracts/schema_v5.sql`：`watchlist_daily`（全量）+ `watchlist_change`（变更日志）。

### 9.3 操作

| 操作 | SQL | 说明 |
|---|---|---|
| 加 | INSERT 一行全量日 + change(add) | 全量日由"前一天 + 变化"生成 |
| 移 | 全量日不再包含该票 + change(remove) | 历史日仍在 |
| 改 | 全量日该票字段变化 + change(update) | `first_added` 不变 |
| 查某天 | `WHERE trade_date = D` | 一条 SQL |
| 查时点 | `max(trade_date) <= cutoff` | Judge 传 T-1 |
| 看差异 | 两天 `FULL OUTER JOIN` | 见 §9.4 |

### 9.4 关键查询

```sql
-- Judge 在 20261028 读到的清单（<= 20261027）
SELECT ts_code, name, role, reason
FROM watchlist_daily
WHERE experiment_id='live' AND owner='judge'
  AND trade_date = (
      SELECT max(trade_date) FROM watchlist_daily
      WHERE experiment_id='live' AND owner='judge' AND trade_date <= '20261027');

-- 20261027 → 20261028 的变化
SELECT coalesce(a.ts_code, b.ts_code) ts_code,
       CASE WHEN b.ts_code IS NULL THEN 'removed'
            WHEN a.ts_code IS NULL THEN 'added' ELSE 'updated' END change
FROM (SELECT ts_code FROM watchlist_daily WHERE ... trade_date='20261027') a
FULL OUTER JOIN
     (SELECT ts_code FROM watchlist_daily WHERE ... trade_date='20261028') b
ON a.ts_code = b.ts_code;
```

### 9.5 写保护

- 同一天重复写且内容不同 → 拒绝（`force` 才允许）；
- 单日删除超过 50% 且原清单 ≥ 10 只 → 拒绝（防写空）；
- 行数必须 1–200；
- 事务写入：全量日 + 变更日志同生共死。

---

## 10. 日期与时间

### 10.1 唯一格式

**`YYYYMMDD`，例如 `20261028`。** 适用于：

- CLI 参数：`--date 20261028`
- 目录名：`runs/20261028/`
- raw 文件名：`daily_20261028.json`
- 数据库 `trade_date` 列
- prompt 头部

不再使用 `2026-10-28`。旧文档里的连字符格式仅存在于 `docs/archive/`。

### 10.2 交易日语义

| 场景 | 读 | 写 |
|---|---|---|
| Judge 在 T 晚复盘 | 行情 `<= T`；watchlist `<= T-1` | `review` + watchlist `trade_date=T` |
| Trader 在 T 晚决策 | review(T)；行情 `<= T`；持仓 `<= T` | `order_intents` |
| 你早上看盘前清单 | `trade_date=T-1`（昨晚定的） | — |
| 周末补数据 | 按交易日历，非交易日不生成 | — |

---

## 11. 冻结与复现

### 11.1 需要冻结什么

1. **raw**：文件不可变 + sha256（`data/meta/fetch_ledger.jsonl` 记录每次尝试）；
2. **prompt**：每次运行把 `prompt.md` 原样存下，并记 `prompt_sha256`；
3. **配置**：`sources.yaml` / `views/*.yaml` / profile / 因子的 hash 进 `manifest.yaml`；
4. **pack**：实验用 `data/packs/<id>/warehouse.duckdb`（DB 副本 + sha256），不复制 CSV。

### 11.2 为什么不需要 snapshot

- raw 已冻结 → 数据可重建；
- prompt 已存文本 → 模型看到了什么一目了然；
- git commit + 配置 hash → 逻辑可追溯。

三者相加，比"16 张派生 CSV + 五合一的 snapshot.yaml"更完整，也更小。

### 11.3 sha256 是什么

不是加密，是**指纹**：

- 同样内容 → 同样 64 位字符串；改一个字节 → 完全不同；
- 没有密钥，不能解密；
- 用途只有两个：证明 raw 没被改过、证明 prompt 是当时那份。

---

## 12. 描述层（v5.1）

目标：把结构化事实翻译成中文，给 LLM 读。

```text
表/视图 ──SQL──▶ facts.json ──模板──▶ prompt.md 的中文段落
                     │
                     └──（可选）LLM 改写 → 缓存，永不覆盖 facts
```

- **事实先行**：`facts.json` 是唯一数字来源，每段文字标注 `fact_ids`；
- **模板优先**：v5.1 第一版只用确定性模板（同 facts 同输出），LLM 改写作为第二版；
- **缓存**：LLM 改写按 `(facts_hash, prompt_version, model)` 缓存，重跑不重复花钱；
- **可审计**：逐句可回溯到表、行、查询；
- **评估**：数字正确性在 facts 上评，叙述质量单独评。

`facts.json` 一条事实的形状：

```json
{
  "id": "theme.robot.stage",
  "kind": "theme_state",
  "grain": "theme_id",
  "key": "T-robot",
  "as_of": "20261028",
  "values": {"rhythm_stage": "首分", "strength": "强", "switch_window": true},
  "unit": null,
  "source": {"table": "metric_theme_state", "query": "q_theme_state", "rows": 8}
}
```

---

## 13. 迁移计划

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1（已完成） | 取数账本 + 原子 raw + readiness + sources.yaml | 19:00 不再误报 kpl；故障注入测试 |
| M2（已完成） | watchlist 每日全量表 + `YYYYMMDD` 日期工具 | 能查任意日全量、差异、时点 |
| M3 | 删 `curated/`：`build` 直接算完写 DuckDB | 同一日期重跑，表内容一致 |
| M4 | 删 `derived/`：prompt 直接查 DuckDB | 新旧 prompt 内容一致（列序允许不同） |
| M5 | schema v5 事实表 + `dim_trade_date` + `available_at`；日期切到 `YYYYMMDD` | `data verify` 全绿；`runs/20261028` 路径生效 |
| M6 | 删 `snapshot.yaml` / `state/*.yaml`；pack = DB 副本 | relay 断开仍能生成 prompt |
| M7（v5.1） | metrics + 描述层（模板版） | facts 可追溯到表；同 facts 同文本 |

每个阶段独立可回滚；旧数据先备份到 `archive/pre-v5-20261030/`（本地，不进 git）。

---

## 14. 非目标

- 不做 Web 服务、调度框架、消息队列、ORM；
- 不换成 Postgres/ClickHouse（单人、< 1 亿行，DuckDB 够）；
- 不让 LLM 直接写 SQL 或直连数据库；
- 不让 Scout 读写任何 state；
- 不用单笔盈亏直接改规则。

---

## 15. 术语表

| 词 | 人话 |
|---|---|
| raw | 接口返回的原始 JSON，只增不改 |
| fact | 客观事实表（行情、涨跌停、板块） |
| dim | 维度表（交易日历、股票主档、板块成分） |
| watchlist | 关注清单，每天全量一张，按实验/人/日期隔离 |
| prompt | 模型实际看到的那段文字 |
| facts.json | prompt 里的数字对应的结构化事实 |
| pack | 冻结的 DuckDB 文件（实验用） |
| run | 某天某模块的一次执行，产物在 `runs/<date>/<module>/` |
| metric | 指标：一段有名字、有版本的 SQL/函数 |
| 描述层 | 把 facts 写成中文的函数（模板优先） |
