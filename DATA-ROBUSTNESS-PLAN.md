# 数据源加固方案 v1（待审核）

- 日期：2026-10-29
- 状态：**待用户审核**；审核通过后按 §7 分阶段实施
- 基线：`v0.4.0`（commit `d032dad`）
- 范围：Scout 取数层、raw/数仓/快照的完整性契约、Watchlist 归属
- 不在范围：Broker / Evaluator / Presenter 重设计；策略与因子本体；E00x 结论修复

---

## 0. 前提（用户已确认）

1. **E001–E007 不再作为结论依据**，框架完善后重做实验。
   → 本方案允许破坏性变更：不留兼容层、不迁移旧产物、不为旧 `plan.schema` / 旧 raw 命名保留回退路径。
2. **数据源是闲鱼转接**（15000 积分、双端点），超时、空壳、端点抖动是常态；各接口更新时间不同（以官方文档为起点，但文档与实测不一致，见 PRD-Scout-v1 §3.1：kpl 官方写 6:00，实测 23:37 空、次日上午 59 行）。
   → 目标不是"让源变稳"，而是"**让状态可见、失败可恢复、历史可冻结**"。
3. **Watchlist 语义**：Judge 每日维护 judge watchlist；用户每日维护 user watchlist 供 Judge 参考；两者都按日冻结、互不覆盖。
   → Watchlist 是 state，不是行情数据，**从 Scout 移出**，归 Curator + `StateStore`。

---

## 1. 结论摘要

现状的问题分三类，修法不同：

| 类别 | 条数 | 根因 | 修法 |
|---|---|---|---|
| A. 取数可靠性 | 14 | 把"未到时间 / 抓失败 / 空响应 / 残缺页"四种状态压成一个布尔值"文件在不在" | 契约层 + 账本层 + 就绪门（§3.2–3.6） |
| B. 数据正确性（PIT） | 8 | 时点状态与客观数据混在一起；窗口用事实表反推交易日 | Watchlist 移出（§3.8）+ 交易日维度表（§3.7） |
| C. 结构性 | 4 | 数仓定位模糊、无故障注入测试、历史依赖实时源 | schema v2 重建（§3.7）+ 历史 pack（§3.9）+ 测试（§3.10） |

**一句话**：先把"每个 `(date, api)` 处于什么状态"变成可查询的事实，再谈调度、重试、并发。没有账本，后面所有优化都是盲调。

预期结果：改完后 `auto-trader scout status --date D` 一屏能回答"数据齐不齐、缺什么、什么时候能齐、缺了影不影响结论"；历史数据打包冻结后，relay 全挂也能继续做研究。

---

## 2. 问题清单

### 2.1 A 类：取数可靠性

| ID | 现象 | 证据 | 影响 |
|---|---|---|---|
| **D-01** | "文件存在即成功" | `relay.py:263` `if path.exists() and not force: return None`；`scout.py:126` 落盘前无校验 | 任何一次不完整/污染的响应会被永久当成好数据，除非手工删文件 |
| **D-02** | 空响应被固化为好数据 | `relay.py:265` 无条件写 payload；kpl 23:37 返回 `items: []` 也会落盘并跳过 | 次日 8:30 数据到位后不会自动重抓；`finalize` 只能把它记成 gap（`scout.py:1561`），无法自愈 |
| **D-03** | "上游还没更新"被当成"缺失" | `scout.py:1543` `raw_specs` 只判断文件存在；`catchup --check` 同逻辑 | 19:00 跑必然 `degraded`；每天都要人工判断"这个缺口是真缺还是没到时间" |
| **D-04** | 失败不传播，退出码永远 0 | `scout.py:933` `except Exception: print("[失败]")` 后 `continue`；`fetch_daily` 无返回值 | 计划任务无法感知失败；`scout fetch` 全失败也返回 0 |
| **D-05** | 分页半途失败丢页，无行数自洽校验 | `relay.py:220-247` 全量累积在内存；某页抛异常则前面页全丢；不校验 `rows ≥ min_rows` 与页数 | 重复消耗积分；更危险的是"少一页但 code=0"被当成成功（PRD §3.2 已记录漏第二页会系统性丢科创板） |
| **D-06** | 40101 也重试 4 次 | `relay.py:160` 循环 + `:206` 分支只 `continue` 不 `break` | 对不支持的接口，每次浪费 4 次调用与退避时间（单接口最坏 ~13 分钟） |
| **D-07** | 熔断只在会话内 | `relay.py:99` `_endpoint_bad` 是实例字典，进程退出即忘 | 端点已挂，下一次运行仍从头撞墙；无半开探测 |
| **D-08** | 调用日志写 `temp/` 且每次覆盖 | `relay.py:276` `temp/call_log_<key>.json` | 没有"某天抓了什么、失败几次、从哪个端点"的持久审计 |
| **D-09** | 无配额守卫 | 全文无调用计数上限 | 转接积分是消耗品；重试风暴可能在不知情下烧掉一天预算 |
| **D-10** | raw 命名三套并存 | `data/raw/daily/daily_20260911.json`、`<api>_<date>.json`、`kpl_list_<date>_涨停.json` | 没法统一遍历、无法从命名反解 `(api, date, 分片)` |
| **D-11** | 数仓与快照覆盖不同步 | `fact_kpl_list` 7 天（缺 10-28）；`dim_board_member` 6 天（缺 10-26/27）；快照 8 天 | 查询在缺日静默少数据；跨表 join 结果不可解释 |
| **D-12** | 用事实表反推交易日 | `scout.py:1290` 与 `curator.py:186` 均为 `SELECT DISTINCT trade_date FROM fact_daily_bar` | 该表 88 个日期里 72 个只有 7 行指数（见附录 A），窗口会被算成"20 个交易日"实际只有 16 个截面 |
| **D-13** | 写库非原子 | `scout.py:626` `DELETE` 后 `con.append`，无事务 | 中断 = 该日数据凭空消失，且无告警 |
| **D-14** | schema 靠手写轻量迁移 | `scout.py:1002` `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` | 无法回答"这个库是哪个版本、谁改的、怎么重建" |

### 2.2 B 类：数据正确性（PIT）

| ID | 现象 | 证据 | 影响 |
|---|---|---|---|
| **C-01** | `watchlist_today` 读 `current.yaml` | `scout.py:1250` + `selectors.yaml:58`（`source: state/watchlist/judge/current.yaml`）；该表进入 Judge Digest（`configs/digest/judge_default.yaml:36`） | **实测**：10-19 / 10-23 / 10-28 三天快照的 `watchlist_today.csv` 是同一份 13 只票，而 watchlist 历史只有 10-27 一份 → 历史快照里装着未来状态，且重跑会变 |
| **C-02** | ST/名称用"今天"的主档 | `scout.py:216` `load_stock_names()` 读 `stock_basic/latest`；`compute_limit_flags` 用它 | 回填 10-19 时用"现在是否 ST"过滤当时 → 弱前视；`dim_stock` 每天整表覆盖（`scout.py:1042`） |
| **C-03** | 板块成分静默回退 | `curator.py:198-209` 取 `trade_date <= as_of` 的最新快照 | `dim_board_member` 缺 10-26/27，`get_board_momentum(as_of=10-27)` 实际用 10-23 的成分；metadata 有 `member_snapshot_date` 但不阻断 |
| **C-04** | `available_at` 只有交易日粒度 | `ARCHITECTURE-V3.md:658`；`snapshot.yaml` 的 `availability.granularity: trade_date` | 无法区分"收盘后 19:00 的 v1"与"次日 8:30 的 v2"；E007 的"10-22 是否首分"歧义正来自这里 |
| **C-05** | 冻结 hash 不可复现 | 9 张 derived 表含 `ingested_at`/`source_file`；`tests/test_scout.py:107` 只能对 7 张"稳定表"比 hash | "hash 冻结"对一半的表失效，快照无法逐字节重现 |
| **C-06** | 无覆盖度契约；`degraded` 只打印不阻断 | `scout.py:1581` 算 status；`daily.py:59` 不做任何检查；`finalize` 只打印 gaps | "看起来 complete 但少一天"无法被发现；实验可以在残缺数据上出结论 |
| **C-07** | `StateStore` 取"最近一份 <= as_of" | `core.py:230` `candidates[-1]` | 缺当日快照时静默使用更早状态；对 watchlist 这种"每日必更"的状态，应该报错而不是回退 |
| **C-08** | 评分层 `sell/hold` 记 0 | `ARCHITECTURE-V3.md:659`、`TODO.md` | 已知，**本方案不修**，但它是"策略结论不可用"的主因，需在数据层稳定后立刻排期 |

### 2.3 C 类：结构性

| ID | 现象 | 影响 |
|---|---|---|
| **S-01** | 数仓被当事实源用，但它其实是可重建的物化层；无重建校验、无版本 | 无法证明"同一天两次重建结果一致"；改动后只能靠 memory |
| **S-02** | 单文件 DuckDB、单写者、无事务、无 migration | 并发/中断即损坏；schema 漂移不可追溯 |
| **S-03** | 无故障注入测试、无 CI（无 `.github`） | "robust" 无法验证；29 个测试函数里只有 1 个快照回归且可 skip |
| **S-04** | 历史回补依赖 relay 实时可用 | 断网/端点挂 = 无法重建历史；研究被数据源绑架 |

---

## 3. 技术方案

### 3.1 总体：契约 / 账本 / 就绪门 三层

```
┌──────────────────────────────────────────────────────────────┐
│ 契约层  configs/data/sources.yaml                             │
│  每个接口：调用参数 / 分页 / 期望行数与字段 / 可用时间 / 重试    │
│           / 关键级别(block|enrich|optional) / 版本策略          │
└───────────────────────────┬──────────────────────────────────┘
                            │ 驱动
┌───────────────────────────▼──────────────────────────────────┐
│ 采集层  autotrader/fetch.py + ledger.py                       │
│  状态机：NOT_DUE → DUE → OK | EMPTY_CONFIRMED | FAILED         │
│  每条尝试写 data/meta/fetch_ledger.jsonl（append-only）        │
│  raw 原子写：.tmp → fsync → os.replace，带内容 hash            │
└───────────────────────────┬──────────────────────────────────┘
                            │ 折叠
┌───────────────────────────▼──────────────────────────────────┐
│ 就绪门  readiness.yaml（每 date 一份）                         │
│  status = ready | degraded | blocked                          │
│  Daily.prepare / Experiment.prepare 据此放行或拒绝              │
└──────────────────────────────────────────────────────────────┘
```

三条铁律（写进代码注释与测试）：

1. **文件存在 ≠ 成功**：所有跳过判断查账本 `status == "ok"`。
2. **空 ≠ 成功**：`allow_empty` 的接口要二次确认；确认前是 `EMPTY_PENDING`。
3. **先校验、后落盘、原子替换**：字段/行数/主键校验不过就 `SCHEMA_BAD`，不写 `ok`。

### 3.2 契约层：`configs/data/sources.yaml`

把现在的四处硬编码收成一份：`relay.py:34` `_API_PREFERENCE`、`relay.py:45` `_MIN_ROWS`、`scout.py:48` `DAILY_APIS`、`scout.py:1543` `raw_specs`。

```yaml
version: src1.0
timezone: Asia/Shanghai

defaults:
  retry: {attempts: 5, backoff: [20, 60, 180, 600, 1800], jitter: 0.3}
  http:  {connect_timeout: 5, read_timeout: 60}

apis:
  daily:
    call:   {api: daily, params: {trade_date: "{date}"}}
    page:   {size: 5000, max_pages: 4, pk: [ts_code]}
    expect: {min_rows: 4500, max_rows: 7000,
             fields: [ts_code, trade_date, open, high, low, close,
                      pre_close, change, pct_chg, vol, amount]}
    avail:  {day_offset: 0, after: "15:30", confidence: doc}
    retry:  {read_timeout: 180}
    class:  block

  stk_limit:
    call:   {api: stk_limit, params: {trade_date: "{date}"}}
    expect: {min_rows: 4000, fields: [ts_code, trade_date, up_limit, down_limit]}
    avail:  {day_offset: 0, after: "17:00", confidence: doc}
    class:  block

  kpl_list:
    call:   {api: kpl_list, params: {trade_date: "{date}", tag: "涨停"},
             endpoint: fallback}
    expect: {allow_empty: true, confirm_empty_after: "08:30", min_rows: 1}
    avail:  {day_offset: 1, after: "08:30", confidence: measured}
    revisions: {v1: "23:00", v2: "08:30"}    # 同一天允许两版，见 §3.3
    class:  enrich

  ths_member:
    call:   {api: ths_member, params: {ts_code: "{board}"}, per: board}
    expect: {allow_empty: false, min_rows: 1}
    avail:  {day_offset: 0, after: "18:00", confidence: doc}
    class:  enrich

  stock_basic:
    call:   {api: stock_basic, params: {list_status: "L"}}
    page:   {size: 5000, max_pages: 3, pk: [ts_code]}
    expect: {min_rows: 4000}
    avail:  {weekday: 6, after: "10:00", confidence: doc}
    class:  optional
```

字段语义：

| 字段 | 作用 |
|---|---|
| `avail` | 决定 `NOT_DUE`：`available_at = trade_date + day_offset 天的 after 时刻`。`confidence: doc` 表示照文档填的，`measured` 表示已用账本校准过（§3.5） |
| `class` | `block` 缺失 → `blocked`（不出快照）；`enrich` → `degraded`（出，但记缺口）；`optional` → 不标记 |
| `expect.allow_empty` | 只有显式声明的接口才允许空；空后必须过 `confirm_empty_after` 二次确认 |
| `revisions` | 同一天允许抓多版（见 §3.3），旧版不被覆盖 |
| `per` | 按维度展开的接口（`ths_member` 按板块），账本 key 带维度值 |

`sources.yaml` 自身纳入 `snapshot.yaml` / `pack_manifest.yaml` 的 hash 冻结。

### 3.3 账本层：`meta_fetch`（append-only JSONL）

**为什么不放 DuckDB 表**：数仓是可删除重建的物化层（§3.7），账本是不可丢的事实。放一起会出现"重建仓库把审计历史删了"。所以账本用 JSONL，和 `logs/run_events.jsonl` 同构，DuckDB 用 `read_json_auto` 建视图做分析。

```jsonc
// data/meta/fetch_ledger.jsonl  一行一次尝试
{"ts":"2026-10-29T18:54:25+08:00","run_id":"scout-20261029","api":"daily",
 "trade_date":"2026-10-29","revision":1,"attempt":1,
 "endpoint":"primary","started_at":"...","finished_at":"...",
 "status":"failed","rows":0,"error_class":"timeout","error":"read timed out",
 "raw_path":null,"payload_sha256":null}
```

状态机：

```
            ┌───────────┐  now < available_at   ┌───────────┐
  create ──▶│  NOT_DUE  │──────────────────────▶│    DUE    │
            └───────────┘                       └─────┬─────┘
                                                      │ attempt
                    ┌─────────────────────────────────┼─────────────────────────┐
                    ▼                                 ▼                         ▼
              ┌──────────┐                   ┌────────────────┐          ┌────────────┐
              │    OK    │                   │ EMPTY_PENDING  │          │   FAILED   │
              │ 校验通过  │                   │ (allow_empty)  │          │ 分类决定    │
              └──────────┘                   └───────┬────────┘          └─────┬──────┘
                                                     │ 过了 confirm 时刻         │
                                                     │ 再抓一次，仍空             ├─ retryable → backoff → DUE
                                                     ▼                          └─ final → FAILED_FINAL
                                            ┌────────────────┐
                                            │ EMPTY_CONFIRMED│
                                            └────────────────┘
```

`FAILED_FINAL` 只给四类：参数错误、积分不足、接口下线（40101 且两端点都不支持）、schema 连续不符。**40101 不重试**，直接换端点；换完仍不支持就 `FAILED_FINAL`。

复查规则（关键）：**只有 `OK` 才允许被下游消费，且 `OK` 必须绑定 raw 文件的 `sha256`**。删了 raw 文件、改了 raw 内容，账本立刻对不上，`scout verify` 报错——这比"文件存在即成功"强得多。

原子写与 envelope：

```python
# autotrader/ledger.py（示意）
def put_raw(api: str, key: str, payload: dict, meta: dict) -> Path:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(body.encode()).hexdigest()
    path = RAW / api / f"{key}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"meta": meta, "sha256": digest, "payload": payload},
                              ensure_ascii=False), encoding="utf-8")
    with open(tmp, "rb+") as fh:
        fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)          # 原子替换
    append_ledger(...)             # 追加一行
    return path
```

**revision 模型**（修 C-04）：

- 账本 key = `(trade_date, api, revision)`；`revision` 从 1 开始。
- 当一次抓取发生在**更晚的可用窗口**（如 kpl 的 v2 窗口）时，`revision += 1`，写新文件 `<date>.v2.json`，**不覆盖 v1**。
- `snapshot.yaml` / `pack_manifest.yaml` 记录 `revision_used`。
- 回放时按"决策时点"解析版本：`--decision-at "2026-10-28T19:30+08:00"` → 只用该时刻前已 OK 的 revision；`--decision-at "2026-10-29T09:00+08:00"` → 用 v2。

这直接消灭 E007 那类"到底算不算首分"的歧义：不是数据变了，是你**在哪个时点看的**变了，而系统必须能复现这两个时点。

### 3.4 就绪门：`readiness.yaml`

每 `trade_date` 一份，由账本折叠生成（不手工维护）：

```yaml
trade_date: 2026-10-28
status: degraded            # ready | degraded | blocked
blocking: []                # block 级缺失清单；非空即 blocked
degraded_by: [kpl_list]
detail:
  daily:        {state: ok, revision: 1, rows: 5577, first_ok_at: "2026-10-28T15:41:02+08:00"}
  stk_limit:    {state: ok, revision: 1, rows: 5577, first_ok_at: "2026-10-28T17:03:11+08:00"}
  limit_list_d: {state: ok, revision: 1, rows: 77,   first_ok_at: "2026-10-28T18:12:40+08:00"}
  kpl_list:     {state: empty_pending, rows: 0, next_check: "2026-10-29T08:35:00+08:00"}
  ths_member:   {state: ok, revision: 1, rows: 13599, member_snapshot_date: "2026-10-28"}
decided_at: "2026-10-29T08:36:11+08:00"
```

行为：

- `Daily.prepare()`：`blocked` → 退出码 4，不渲染 digest；
- `degraded` → 必须 `--allow-degraded`，否则拒绝；允许时把 `degraded_by` 写入 `input_manifest.yaml`；
- `Experiment.prepare()`：默认拒绝 `degraded`，实验级可声明 `tolerate_degraded: [kpl_list]`（写进 bundle，作为实验变量之一）；
- `score` / `verdict`：拒绝在 `degraded` 上出结论（除非实验声明）。

**注意**：`degraded` 不是废数据。缺 KPL 的 10-28 是合法的"无题材增强"输入；真正危险的是"看起来 complete 却少一天"。所以门的目标是**可见、可比、可拒绝**，不是 100% 完整。

### 3.5 调度：分窗 + 轮询 + 全局 deadline

```bash
auto-trader scout sync   --date 20261028 --window evening   # 15:40 起：block 级 + 收盘后可得
auto-trader scout sync   --date 20261028 --window morning   # 08:35 起：kpl / ths_member
auto-trader scout status --date 20261028                    # readiness 矩阵
auto-trader scout verify --date 20261028                    # 账本 vs raw vs 数仓 三方对账
```

- 每个接口先查 `avail`：未到点 → `NOT_DUE`，**不调 API、不计缺口**；
- 到点未 OK → 按 per-API backoff 重试，只重试该接口；
- 全局 deadline（evening 19:30 / morning 09:15）到了退出，退出码 3 = still pending，打印还差谁；
- Windows 计划任务在 15:40 / 17:00 / 19:00 / 08:35 / 09:05 各跑一次，**不起 daemon**；
- `catchup` 复用同一账本，按 `(date × api)` 队列推进，断点续跑。

**可用时间靠实测校准，不靠文档**：

1. 第一版按文档填，`confidence: doc`；
2. 跑两周，账本里每个 `first_ok_at` 就有 10 个样本；
3. 取 p95 回填 `after`，改 `confidence: measured`；
4. 从此不再猜，也不再因为"19:00 必然缺 kpl"而误报。

### 3.6 传输层加固

| 项 | 现状 | 改成 |
|---|---|---|
| 超时 | 全局 60 / daily 180 | connect 5 + read per-API（daily 180 / kpl 90 / 其余 60） |
| 重试分类 | 除业务错误外一律重试 | 可重试：timeout / 5xx / 429 / invalid_params / 空壳；**不重试**：40101、积分不足、参数错 |
| 40101 | 重试 4 次 | 立即换端点；两端点都不支持 → `FAILED_FINAL` |
| 熔断 | 会话内字典 | 持久化 `data/meta/endpoint_state.json`：连续失败 N 次 → `open_until`，半开探测 |
| 预算 | 无 | 账本按日计数；超 80% 停止并告警（退出码 5） |
| 分页 | 内存累积，失败丢页 | 记录 `pages_done`；失败只重试该页；落盘前校验 `rows ∈ [min_rows, max_rows]` 且页数自洽 |
| 交叉校验 | 无 | 每日校验涨停家数四口径（本地 `stk_limit` / `limit_list_d` U / `kpl_list` / `fact_breadth`），差 > 阈值 → `crosscheck_fail` → 降级 |
| 退出码 | 永远 0 | 0 成功 / 3 pending / 4 blocked / 5 配额耗尽 / 1 参数或内部错误 |

交叉校验阈值先按"绝对差 > 5 只或相对差 > 5%"起，跑两周后按实际分布调整。

### 3.7 数仓：schema v2 + 交易日维度 + 原子写

**定位**：数仓是可重建的物化层，事实源是 `data/raw/` + `data/packs/`。这条要写进 `ARCHITECTURE-V3.md` §16。

改动：

1. **`dim_trade_date`**（由 `trade_cal` 生成）：`trade_date DATE PRIMARY KEY, is_open, prev_trade_date, next_trade_date, week_no`。所有窗口查询改用它，禁止再用 `DISTINCT trade_date FROM fact_daily_bar`（修 D-12）。
2. **`meta_coverage`**：`(trade_date, table, rows, built_at, source_sha256)`；查询前先 `JOIN v_coverage` 判断该日是否完整。
3. **原子 upsert**：`BEGIN; DELETE; INSERT; COMMIT;`（DuckDB 支持事务）；失败自动回滚，不再留下"删了一半"的日期。
4. **`dim_stock_history` SCD2**：`(ts_code, valid_from, valid_to, name, industry, market, list_status)`；`universe` 过滤与 ST 判断都按 `valid_from <= date < valid_to`，消除 C-02。
5. **schema 版本化**：`contracts/schema_v2.sql` + `data/schema_version` 表；**直接重建**（旧实验不作数），不写迁移脚本。
6. **derived 去审计列**：`ingested_at` / `source_file` 不进 `derived/*.csv`；输出按显式 `ORDER BY` 稳定排序；hash 恢复可复现（C-05）。

`meta_coverage` 由 `scout verify` 维护，`select_stocks` 产出的每张表都要登记行数与源 hash。

### 3.8 Watchlist：从数据层移出

```
Scout（只产客观数据，不再读任何 state）
   ↓
Curator.render_digest
   ├─ 读 data/snapshots/<date>/derived/*.csv
   └─ 读 state/watchlist/{judge,user}/history/<date>.yaml
        → join 当日 bars → 渲染原"关注列表"段
```

具体动作：

1. 删 `scout.py:1250` 段与 `selectors.yaml:58` 的 `groups.watchlist`；`watchlist_today` 不再进 `derived/`；
2. `Curator` 新增 `render_watchlist(state, date)`，读**当日 history**，读不到直接报错；
3. 新增 `auto-trader state freeze --date D`：把 `current.yaml` 冻结为 `history/D.yaml`；已存在且内容不同 → 拒绝（`StateStore.save` 已有此语义，`core.py:256`）；
4. Judge 的 `watchlist apply` 落盘时自动 freeze；user watchlist 由用户当日改 `current.yaml` 后跑 `state freeze`（或 `daily prepare` 开头自动 freeze 并记 warning）；
5. `StateStore` 增加 `strict=True` 模式：要求**恰好**当日快照，缺则报错（修 C-07）；
6. `configs/queries/templates.yaml` 的 `allowed_snapshot_tables` 去掉 `watchlist_today`。

这样"Judge 每日维护、用户每日维护、Judge 参考 user"的语义完全保留，但 PIT 变成结构性保证。

### 3.9 历史数据包（pack）

```
data/packs/p2026-01-02_2026-10-28/
├── raw/<api>/<date>[.v2].json
├── snapshots/<date>/derived/*.csv
├── readiness/<date>.yaml
├── pack_manifest.yaml     # 每文件 sha256 + sources.yaml hash + git commit + schema 版本
└── pack.lock              # 冻结标记（含 manifest hash）
```

- `auto-trader scout pack --start ... --end ... --out ...`：从账本驱动抓取，缺 `block` 就停在该日并报告，不产出半包；
- 冻结后**只读**：实验只引用 `pack_id`，`Curator` 拒绝 `as_of` 落在 pack 之外的查询；
- relay 后续改版、端点失效都不影响已有 pack；
- `data/snapshots/` 保留为"当日实盘产物"，pack 是"研究基线"，两者用同一套 schema。

这一条是"研究不被数据源绑架"的根本解，也是把 88 天里 72 天只有指数这种历史垃圾一次性清理掉的机会。

### 3.10 测试与 CI

- **录制回放**：把真实 relay 响应脱敏后存 `tests/fixtures/relay/<api>/<date>.json`；
- **故障注入**：`FakeRelay` 实现与 `TushareClient` 相同接口，按场景 YAML 返回：`timeout` / `429` / `40101` / `empty` / `truncated_page` / `dup_rows` / `field_drift`；
- **断言账本转移**，不只是结果：`NOT_DUE → DUE → EMPTY_PENDING → EMPTY_CONFIRMED`、`FAILED_FINAL`、`SCHEMA_BAD`、断点续跑不重复调用；
- **CI**（GitHub Actions）：`uv sync --group dev` → `ruff check .` → `pytest -m "not network"`；network 用例只在本地手动跑。

用例清单（至少）：

| 场景 | 期望 |
|---|---|
| 19:00 抓 kpl（未到确认时刻） | `EMPTY_PENDING`，不写磁盘、不计缺口 |
| 次日 08:36 再抓仍空 | `EMPTY_CONFIRMED`，readiness 记 `degraded` |
| 次日 08:36 抓到 59 行 | revision=2，`OK`，v1 保留 |
| daily 第 2 页超时 | 不写 `ok`，下次只重试第 2 页 |
| daily 返回 4000 行（<min） | `SCHEMA_BAD`，不写 ok |
| 40101 | 切端点；两端点都 40101 → `FAILED_FINAL`，不重试 |
| raw 文件被手改 | `scout verify` 报 hash 不符 |
| 账本有 ok 但 raw 缺失 | `scout verify` 报错，readiness 降级 |

---

## 4. 破坏性变更清单（确认后用）

| # | 变更 | 影响 |
|---|---|---|
| 1 | 删除 `derived/watchlist_today.csv` 与 Scout 的 watchlist 读取 | Digest 改由 Curator 渲染；`allowed_snapshot_tables` 去掉该项 |
| 2 | raw 统一为 `data/raw/<api>/<key>.json` + envelope（不再是裸 `{fields, items}`） | 所有 `load_*_raw` 改写；旧文件不兼容（直接删，旧实验不作数） |
| 3 | `snapshot.yaml` 升 `s4.0`：新增 `readiness`、`sources_hash`、`revision_used`，去掉 `gaps`（移到 readiness） | Curator / Judge / 测试同步 |
| 4 | `contracts/schema_v2.sql`，重建 `warehouse.duckdb` | 不写迁移，直接重建 |
| 5 | 删除 `contracts/plan.schema.json`、`scripts/evaluation/legacy_validate_plan.py` 等 E001–E005 兼容件 | 旧实验归档后删除 |
| 6 | `score` / `logs` 仍为占位，不动 | — |
| 7 | CLI 新增 `scout sync/status/verify/pack/rebuild-warehouse/ledger`、`state freeze` | `workflows/DAILY.md` 同步改写 |

---

## 5. 新 CLI 与文件布局

```bash
auto-trader scout sync    --date D --window evening|morning|weekly
auto-trader scout status  --date D [--json]
auto-trader scout verify  --date D
auto-trader scout pack    --start D1 --end D2 [--out data/packs/pXXX]
auto-trader scout rebuild-warehouse --from-pack pXXX
auto-trader state freeze  --date D [--judge|--user|--portfolio]
auto-trader daily prepare|continue --as-of D [--allow-degraded]
```

```
data/
├── raw/<api>/<date>[.v2].json        # envelope + 内容 hash
├── meta/fetch_ledger.jsonl           # 账本（append-only）
├── meta/endpoint_state.json          # 熔断状态
├── meta/quota.json                   # 当日调用计数
├── snapshots/<date>/derived/*.csv
├── packs/<pack_id>/...
└── warehouse.duckdb                  # 可删可重建
```

---

## 6. 目标数据模型（摘要）

```sql
-- 交易日维度：所有窗口唯一依据
CREATE TABLE dim_trade_date (
  trade_date DATE PRIMARY KEY, is_open BOOLEAN,
  prev_trade_date DATE, next_trade_date DATE, week_no INTEGER
);

-- 覆盖度：查询前先看它
CREATE TABLE meta_coverage (
  trade_date DATE, table_name VARCHAR, rows BIGINT,
  built_at TIMESTAMP, source_sha256 VARCHAR,
  PRIMARY KEY (trade_date, table_name)
);

-- SCD2 主档
CREATE TABLE dim_stock_history (
  ts_code VARCHAR, valid_from DATE, valid_to DATE,
  name VARCHAR, industry VARCHAR, market VARCHAR, list_status VARCHAR,
  PRIMARY KEY (ts_code, valid_from)
);

-- 账本视图（JSONL 折叠）
CREATE VIEW v_fetch AS
SELECT * FROM read_json_auto('data/meta/fetch_ledger.jsonl');
```

`fact_*` 表结构与现 `schema_v1.sql` 基本一致，仅去掉 `source_file` / `ingested_at`（溯源改由 raw envelope + 账本承担），主键与约束保留。

---

## 7. 实施阶段（5 个 PR）

| PR | 内容 | 文件 | 规模 | 验收 |
|---|---|---|---|---|
| **PR-1** | 账本 + 原子 raw + `sources.yaml`（先覆盖 block 级 5 个接口）+ readiness + 硬门 | 新增 `sources.py` / `ledger.py` / `fetch.py`；改 `scout.py` / `daily.py` / `cli.py` | ~700 行 | 10-28 重跑产出一致（除 hash）；8 个故障注入用例绿；19:00 跑不再误报 kpl 缺口 |
| **PR-2** | 分窗调度 + 可用时间实测回填 + 交叉校验 + 预算/熔断持久化 | `fetch.py` / `sources.yaml` / `meta` | ~300 行 | 计划任务连续 3 天；退出码语义正确；账本可回答"首 OK 时刻" |
| **PR-3** | schema v2：`dim_trade_date` / `meta_coverage` / 原子写 / SCD2 / 去审计列 / 重建仓库 | `schema_v2.sql` / `scout.py` / `curator.py` | ~500 行 | 同一天两次重建，derived hash 完全一致；窗口查询不再受指数日污染 |
| **PR-4** | Watchlist 移出 + `state freeze` + `strict` 读取 + Digest 改造 | `scout.py` / `curator.py` / `core.py` / `cli.py` | ~250 行 | 回放 10-19 不再出现 10-27 的关注列表；缺当日快照时明确报错 |
| **PR-5** | 历史 pack（120 天）+ 删旧 `scripts/` + CI + 文档 | `pack.py` / `.github/` / `ARCHITECTURE-V3.md` / `workflows/DAILY.md` | ~200 行 + 跑数 | pack hash 固定；`pytest` 在 CI 绿；relay 断开仍能跑 Curator/Judge |

依赖关系：PR-1 → PR-2 → PR-3 →（PR-4 可与 PR-3 并行）→ PR-5。

**为什么把 watchlist 排到 PR-4**：它是正确性问题，但依赖 `state freeze` 与 Curator 改造；而 PR-1 的账本不依赖它。如果你希望先堵 PIT 泄漏，可以把 PR-4 提到 PR-1 之后——只需两天，我建议提。

---

## 8. 验收标准（Definition of Done）

1. `scout status --date D` 一屏说清每个接口的状态/行数/首次成功时间/下次检查时间；
2. 任意接口连挂 3 天，流程不挂死，只产出 `degraded`，缺口进 `input_manifest`；
3. 断点重跑，已 `ok` 的接口零调用；
4. 历史 pack 的 hash 固定，实验只引用 pack，relay 全挂也能跑 Curator/Judge；
5. 同一日期两次运行，除账本时间戳外 diff 为空；
6. 删掉任一 raw 文件，`scout verify` 能定位到具体 `(date, api, revision)`；
7. `pytest -m "not network"` 在 CI 绿，含 8 个故障注入用例。

---

## 9. 需要你拍板（6 项）

| # | 决策 | 我的建议 |
|---|---|---|
| 1 | **历史起点**：`2026-01-02`（约 200 交易日）/ `2026-04-01`（约 140 天）/ 只补 120 天 | 从 `2026-01-02` 起，覆盖足够多情绪周期；夜间跑，断点续 |
| 2 | **schema v2 直接重建**，不写迁移 | 同意（旧实验不作数；迁移成本 > 重建） |
| 3 | **`degraded` 是否允许研究** | 允许，但 `score/verdict` 拒绝；实验可在 bundle 里声明 `tolerate_degraded` |
| 4 | **是否给 block 级接口配官方 token 兜底** | 先不配。跑两周账本后看失败率，若 `daily/stk_limit` 月失败 > 3 天再上 |
| 5 | **pack 与 snapshots 的关系** | snapshots 保留为当日实盘产物；pack 只用于实验/回放 |
| 6 | **`scripts/` 删除时机** | PR-5 一起删；`scripts/evaluation`、`scripts/presenter` 保留到重设计完成 |

另外两个小确认：

7. 文档位置：`DATA-ROBUSTNESS-PLAN.md`（不入 `.gitignore`）是否合适？还是改名 `docs/…`？
8. PR-4（watchlist）是否提前到 PR-1 之后？

---

## 10. 风险与回滚

| 风险 | 应对 |
|---|---|
| 改造期间 relay 长时间不可用 | PR-1/2 不依赖全量数据；pack 分批；block 级优先 |
| 破坏性变更导致旧产物不可读 | 旧实验作废；`main` 保持 `v0.4.0` 可用；新链路在新分支验收后再合 |
| schema v2 重建耗时 | 先并行建 `warehouse_v2.duckdb`，验收后切换；旧库保留一周 |
| 积分超支 | 预算守卫 + 分批 + 断点；pack 只抓必要接口 |
| Windows 文件锁 | 原子替换 + 单写者；`scout verify` 检测 `.tmp` 残留 |
| 改造范围膨胀 | 本方案不含评分器、因子回测、Broker/Evaluator 重设计 |

回滚：每个 PR 独立可 revert；`v0.4.0` tag 保留改造前基线。

---

## 附录 A：证据复现

```powershell
# 1) 数仓覆盖：88 个日期里 72 个只有 7 行指数
.\.venv\Scripts\python.exe -c "
import duckdb; con=duckdb.connect('data/warehouse.duckdb', read_only=True)
print(con.execute('''SELECT CASE WHEN n>1000 THEN 'full' ELSE 'index_only' END k,
COUNT(*) FROM (SELECT trade_date, COUNT(*) n FROM fact_daily_bar GROUP BY 1) GROUP BY 1''').fetchall())
"
# → [('full', 16), ('index_only', 72)]

# 2) 快照里的 watchlist 是同一份
.\.venv\Scripts\python.exe -c "
import pandas as pd
for d in ['2026-10-19','2026-10-23','2026-10-28']:
    df=pd.read_csv(f'data/snapshots/{d}/derived/watchlist_today.csv', encoding='utf-8-sig')
    print(d, len(df), ','.join(df.ts_code.head(3)))
"
# → 三天都是 13 行、000058.SZ,000678.SZ,002242.SZ

# 3) 板块成分缺两天
.\.venv\Scripts\python.exe -c "
import duckdb; con=duckdb.connect('data/warehouse.duckdb', read_only=True)
print(con.execute('SELECT trade_date, COUNT(*) FROM dim_board_member GROUP BY 1 ORDER BY 1').fetchall())
"
# → 10-19..10-23, 10-28；缺 10-26/27
```

## 附录 B：现状资产清单（改造时会重建）

| 资产 | 规模 | 备注 |
|---|---|---|
| `data/raw/` | 182 个文件，11 个 api 目录 | 命名三套并存 |
| `data/curated/` | 10 个日期目录，约 4.9 MB | 含无快照的 2026-09-30 / 2026-10-29 |
| `data/snapshots/` | 8 天，16 张表，725 KB | 10-28 `status: degraded`（缺 kpl） |
| `data/warehouse.duckdb` | 40.9 MB，11 张表 | `fact_daily_bar` 89,632 行 / 88 日期；`dim_board_member` 82,146 行 / 6 日期 |
| `state/watchlist/judge/` | current + 1 天 history（10-27，两个版本） | 与 8 天快照不匹配 |
| `state/portfolio/` | current + 1 天 history（10-27） | 同上 |
| `tests/` | 9 文件 / 29 个测试函数 | 无 CI；1 个快照回归可 skip |
| git | `main` @ `d032dad`，tag `v0.4.0` | 无 remote |
