# 首分机器人架构 v3

- 版本：v3.0
- 日期：2026-10-28
- 状态：**已确认并生效**
- 生效规则：本文件为当前架构；E001–E005 保留旧契约与原始产物，迁移前文件见 `archive/pre-v3-20261028/`。
- 目标：把数据生产、上下文组装、市场复盘、交易决策、订单执行、结果评价和展示拆成可独立版本化的模块，使实验差异可归因、运行过程可复现、历史资料不污染决策。

## 〇、设计原则

1. **数据与视图分离**：同一份标准数据可以生成不同 Agent 视图；改视图不等于改数据。
2. **复盘与交易分离**：Judge 解释市场，Trader 决定交易，Broker 负责执行。
3. **静态摘要与动态查询并存**：Digest 是初始输入，不是 Agent 能获得的全部数据。
4. **模块间只通过文件与受控查询通信**：禁止跨模块读取内部状态或隐式补充上下文。
5. **机械工作脚本化**：取数、渲染、查询、校验和评分不依赖 LLM 自由发挥。
6. **全程可追溯**：每次运行冻结输入、查询、配置、代码、模型和输出的版本与 hash。
7. **时点隔离**：任何历史回放只能使用决策时点已经存在且允许使用的数据与标签。
8. **展示不反哺决策**：Presenter 只读，不向决策链路回写内容。

## 一、模块总览

新版包含七个职责模块：

| 模块 | 核心职责 | 不负责 |
|---|---|---|
| Scout | 外部取数、清洗、计算、筛选、入库，生产标准数据 | 面向 Agent 排版、行情判断、交易建议 |
| Curator | 生成 Agent Digest、组装 Context Bundle、执行受控数据查询、冻结输入 | 修改数据事实、自由摘要、市场判断 |
| Judge | 市场环境、结构、板块、载体、风险与数据缺口复盘 | 买卖、价格、仓位、kill_switch、持仓改单 |
| Trader | 根据复盘、交易策略、按需数据和账户状态生成订单意图 | 重写复盘、直接访问外部数据源、实际挂单 |
| Broker | 委托、撤改单、拒单与成交回报；当前由人工执行 | 修改交易策略或订单意图 |
| Evaluator | 分层评分、历史回放对账、周期归因与规则晋升提案 | 修改原始 review、order intent 或成交记录 |
| Presenter | 可视化、对比与展示 | 进入决策上下文、影响交易结果 |

Evaluator 沿用并扩展现有 Backtester 的职责；`backtests/` 仍可保存回测设计与历史研究资产。

## 二、三条端到端流程

三条链路分别演进，通过版本化数据与文件契约连接，不合并成一条不可拆分的大流程。

### 2.1 Scout 数据链路：每日定时或手动触发

```text
每日定时 / 手动触发 / 历史回补
    ↓
外部数据源
    ↓
Scout：raw → curated → derived → warehouse
    ↓
标准数据快照 + snapshot manifest + 数据缺口
```

Scout 只负责把数据准备好，不触发 Judge 或 Trader。实验和日常运行按需引用已经冻结的数据快照。

### 2.2 实验链路：E00x 从 Bundle 到 Score

```text
定义实验问题、假设与 arms
    ↓
Curator：组装并冻结 Context Bundle + 初始状态
    ↓
按决策日引用 Scout 快照，渲染 Judge Digest
    ↓
Judge：review.md + review.json + Judge watchlist 更新
    ↓
Curator：组装 Trader 输入（review + 策略 + 初始视图 + 账户状态）
    ↓
Trader ⇄ Curator：受控、逐条留痕的按需查询
    ↓
Trader：order_intents.json + 人读交易单
    ↓
Broker / 成交回放：orders + fills + execution receipt
    ↓
Evaluator：Judge / Trader / Broker / 整体四层 Score
    ↓
实验 summary + verdict
```

同一实验包含多个 Trader arms 时，Judge 只运行一次，各 arm 复用同一 review hash。非实验的日常实际运行复用同一模块契约，产物放入 `runs/live/`。

### 2.3 长期链路：周期归因与晋升

```text
实验 + 日常运行产生的 review / order intents / fills / scores
    ↓
Evaluator：周 / 月 / 季聚合、分层归因、错题与案例沉淀
    ↓
可证伪假说 + 支持证据 / 反例 / 适用环境
    ↓
历史回放 + 向前影子验证
    ↓
用户裁决
    ↓
晋升 framework / factor / Profile 新版本，或放弃、继续观察
    ↓
形成新基线，进入后续实验
```

周期归因不修改历史实验产物，也不因单笔盈亏直接晋升规则。Presenter 与日志系统横向只读三条链路，不反向影响决策。

### 单次运行与单次实验

- **Run**：某个决策日、某个配置组合的一次完整执行。
- **Experiment**：一个研究问题及其假设，可包含多个 Run 和多个对照 arm。
- **Arm**：实验中一个唯一、完整的配置组合。
- 日常非实验执行放入 `runs/live/`；实验执行放入对应 `experiments/E###/`。

## 三、Scout：只生产标准数据

### 3.1 输入与输出

输入：外部数据源、`configs/data/`、历史 raw、交易日历。

输出：

```text
data/raw/                         原始响应，只追加，不修改
data/curated/YYYY-MM-DD/          清洗、连接与基础计算结果
data/snapshots/YYYY-MM-DD/
├── derived/                      标准榜单、面板与特征表
└── snapshot.yaml                 数据覆盖、配置、代码 hash 与缺口
data/warehouse.duckdb              可重新由 raw/curated 构建的查询层
```

### 3.2 Data Recipe

Data Recipe 定义：

- 数据源及字段口径；
- universe、市场和 ST/停牌过滤；
- 指数集合、板块口径；
- derived 表及计算参数；
- `selection_n`：标准表实际保留多少条；
- 历史窗口和数据回补规则。

修改 Data Recipe 必须 bump 版本。Scout 不因某个 Agent 想看前 30 或前 50 而改变标准数据。

### 3.3 Scout 不再生成面向 Agent 的 Digest

现有面向四层复盘的 Markdown 渲染职责迁移到 Curator。数据快照不再绑定唯一一种 Agent 视图，因此同一快照可以服务多个实验、Judge 和 Trader。

## 四、Curator：视图、上下文与受控数据访问

Curator 是确定性脚本与服务，不是另一个做市场判断的 LLM。

### 4.1 Digest 是 Agent 输入视图

Digest 定义：Curator 按 View Recipe，从标准数据集中为某次 Agent 运行渲染的初始输入。

View Recipe 定义：

- 使用哪些表和 section；
- 排序与字段；
- `display_n`：每部分实际展开多少条；
- Markdown/JSON 模板版本；
- 是否附表及上下文预算。

例如，标准 `gain_top.csv` 已保留 100 行：

```text
E06：judge_view.display_n.gain_top = 30
E07：judge_view.display_n.gain_top = 50
```

这两次实验的数据 recipe 与 Scout snapshot 不变，只有 Judge View Recipe 改变。若标准表只有 30 行，则改为前 50 还需要修改 Data Recipe 并重建数据。

### 4.2 Curator 不做自由摘要

Curator 只能按模板和参数确定性渲染，不得：

- 自行判断哪些行情重要；
- 用 LLM 改写或概括事实；
- 静默删除异常或缺口；
- 将未来数据拼入当前视图。

数据缺口必须显式保留。

### 4.3 受控查询

静态 Digest 不能满足 Trader 的多跳钻取，因此 Curator 提供只读查询接口。Trader 具备查询能力，但不承担外部取数职责，也不能直接调用 Tushare、访问网页或读取未授权文件。

查询必须：

1. 使用 Profile 中声明的 query template；
2. 携带 `as_of` 决策时点；
3. 只访问允许的数据集与字段；
4. 保存请求、响应、行数、耗时与内容 hash；
5. 将返回结果纳入该 Run 的输入清单；
6. 拒绝返回决策时点之后的数据。

典型查询：

```yaml
query: get_theme_members
theme_id: robot
as_of: 2026-10-27
```

```yaml
query: get_daily_bars
ts_codes: ["300XXX.SZ", "301XXX.SZ"]
lookback_days: 60
as_of: 2026-10-27
```

```yaml
query: get_board_momentum          # v1.1（2026-10-29）新增
board_codes: ["885517.TI"]         # Judge 预判的明日首分板块
top_n: 20                          # 全量成分按两日涨幅排序取前 N
as_of: 2026-10-27
```

`get_board_momentum` 依赖 `dim_board_member`（schema_v1.sql ⑪，同花顺 ths_member 成分，
由 `scripts/scout/fetch_theme_members.py` 取数入库）。**ths_member 只暴露"当前最新"成分，
接口无法恢复历史变更**；因此维度表按快照日存储（每次取数盖 `--date` 决策日时间戳，
raw 按日期命名只追加），查询取 `trade_date <= as_of` 的最新快照——回测某天 D 前须先以
`--date D` 跑一次取数，回测精度 = 快照粒度。查询响应 metadata 必须携带
`member_snapshot_date` 与该 limitation。

Agent 的完整输入是：

```text
初始 Digest + 所有查询结果 + 允许的历史状态 + 账户状态（仅 Trader）
```

### 4.4 两份运行清单分离

- `snapshot.yaml`：Scout 证明“数据是什么”。
- `input_manifest.yaml`：Curator 证明“Agent 实际看到了什么”。

`input_manifest` 至少记录：初始 Digest、所有查询请求与响应、状态文件、prompt、profile、因子、schema 和模型信息。

## 五、Judge：只负责市场复盘

### 5.1 输入

- Judge Profile 指定的 Digest；
- 指定版本复盘因子；
- 不晚于 T 的历史 Judge review；
- T 时点 Judge watchlist 快照；
- Profile 显式启用时，T 时点 user watchlist 因子快照；
- 经声明的补充查询结果。

Judge 默认不读取实时 positions、cash 和 open orders，避免账户状态锚定市场判断。

### 5.2 输出

`review.md`：人读复盘。

`review.json`：Trader 与 Curator 可稳定消费的结构化复盘，至少包括：

```text
as_of
market_regime
risks[]
themes[]
  theme_id / name / stage / evidence / invalid_if / confidence
carriers[]
  ts_code / theme_id / role / evidence / confidence
data_gaps[]
watchlist_updates[]
```

`theme_id` 必须来自受控字典，不能只依赖“机器人”等自由文本完成板块成员查询。

### 5.3 禁止输出

Judge 不输出：

- buy / sell / hold / avoid 等交易动作；
- 挂价、止损、止盈和仓位；
- kill_switch 订单指令；
- 持仓改单；
- 实际订单或成交判断。

Judge 可以描述板块是否完成起爆—加强、首分条件是否具备、载体地位和风险反证，因为这些属于市场事实与结构判断；是否交易以及怎样交易由 Trader 决定。

## 六、Trader：从复盘到订单意图

### 6.1 输入

- 指定 Judge review 及其 hash；
- Trader Profile：prompt + 交易策略因子组合；
- 初始 Trader View；
- 受控查询能力及 query templates；
- T 时点 positions、cash、open orders；
- 适用的交易规则与合约约束。

### 6.2 工作方式

Trader 可以：

1. 根据结构化 `theme_id` 找板块成员；
2. 按策略因子筛选候选；
3. 查询候选日线、成交额等已声明数据；
4. 决定不交易或生成具体订单意图；
5. 对现有持仓和未成交订单提出撤改单意图。

Trader 不得重新生成一套市场复盘替代 Judge，也不得临时读取未声明的数据源。

### 6.3 输出

`order_intents.json` 至少包括：

```text
as_of / valid_for
judge_review_ref + hash
trader_profile + version
account_state_ref + hash
query_manifest_ref
actions[]
  ts_code / action / order_type / limit_price
  stop_loss / take_profit / position_pct
  time_in_force / cancel_or_replace / reason / strategy_factor_refs
no_trade_reason
```

订单意图是 Trader 的最终决策，不再由 Judge 直接生成。

## 七、Broker：执行与成交事实

当前 Broker 由人工担任，未来可替换为程序化交易通道，契约不变。

输入仅为 Trader 的订单意图、当时账户状态和交易通道约束。

输出：

- `orders.json`：实际提交、撤销和修改的委托；
- `fills.json`：实际成交、费用和滑点；
- `execution_receipt.json`：未提交、拒单、部分成交及人工备注。

Broker 不得自行优化价格、改变仓位或忽略订单意图。无法执行时记录原因，不静默修改策略。

## 八、Evaluator：分层评价与知识晋升

| 层 | 评价内容 | 不应混入 |
|---|---|---|
| Judge | 市场状态、板块阶段、载体定位、覆盖率、概率校准、缺口识别 | 用单笔订单收益代替全部复盘质量 |
| Trader | 候选转换、定价、仓位、进出时点、订单意图质量、组合收益 | 人工漏挂、拒单等执行问题 |
| Broker | 委托正确性、成交率、滑点、拒单、撤改单与延迟 | 市场判断质量 |
| 整体 | 复盘→策略→执行的端到端结果 | 用单笔盈亏直接晋升规则 |

Evaluator 保留现有纪律：机械对账走 scorer；周期归因和错题本由独立评价上下文生成；规则只形成晋升提案，不直接改 framework。

## 九、Prompt、Profile 与因子库

### 9.1 Prompt 变薄

Prompt 只保存：角色边界、推理步骤、输入输出格式和禁止事项，不再内嵌整套因子。

### 9.2 Profile 是可执行版本

```yaml
id: judge-first-divergence
version: j4.0
prompt: ../prompts/v4.md
factors:
  - F006@v1.0
  - F008@v1.1
input_view: judge-default@v2
queries: []
output_contract: review@v1
```

Trader Profile 额外声明 `data_requirements` 和允许的 query templates。

- 改 prompt 内容：bump prompt 版本。
- 改因子内容：bump 因子版本。
- 改因子组合、输入视图或查询能力：bump Profile 版本。

### 9.3 因子库

保留现有 F 编号谱系，并补充 `version` 与 `applies_to`：

- `review/`：市场观察、板块阶段、载体定位；
- `strategy/`：入场、出场、定价、仓位与风控；
- `shared/`：同时服务复盘与交易的定义；
- `registry.yaml`：状态、证据、版本、消费者与实验引用。

`framework/` 保存长期稳定原则与治理规则；`factors/` 保存可组合操作模块；`agents/` 保存执行规范。同一规则不在这三处重复维护。

## 十、Judge Watchlist、User Watchlist 与账户状态

系统保留两类来源、用途和写者都不同的关注列表，不得合并为一个文件或互相覆盖。

### 10.1 Judge watchlist：Judge 自动维护的市场跟踪状态

当前由 Judge 自动生成和维护的关注股票列表属于这一类。它记录 Judge 认为值得持续跟踪的载体、角色、理由和状态，是复盘链路的状态产物。

```text
state/watchlist/judge/
├── current.yaml
└── history/YYYY-MM-DD.yaml
```

- Judge 是唯一语义写入者，在 `review.json` 中输出 `watchlist_updates[]`。
- Curator 只校验并应用更新、冻结每日快照，不自行增删股票。
- Scout 和其他 Agent 只读指定时点版本。
- 现有 `data/watchlist.yaml` 在迁移时归入此类。

### 10.2 User watchlist：用户提供的可选因子

`user watchlist` 是用户维护的股票关注清单及标签，作为独立的用户先验输入。Judge 或 Trader Profile 可以选择是否启用它，从而实验“加入用户关注清单是否改善结果”。

```text
state/watchlist/user/
├── current.yaml
└── history/YYYY-MM-DD.yaml
```

- 用户是唯一写入者，Judge、Trader、Scout 和 Curator 均不修改。
- 因子库登记 `user_watchlist` 的定义、标签语义、版本和使用方式；Profile 声明是否启用。
- 具体股票及标签按日期保存在上述状态文件中，Bundle 引用指定快照及 hash，不把每日列表硬编码进 prompt。
- 启用时，它是因子输入；未启用时，Agent 不可隐式读取。

### 10.3 共同约束

两类列表的记录至少包括：`ts_code`、`theme_id`、`role`、`level`、`status`、`reason`、`label_source`、`added_date`、`available_at`。

标签使用受控字典，避免“龙头 / anchor / 核心票”等同义或近义标签无定义混用。历史回放只能读取对应时点快照，不能用 current 代替历史状态。

### 10.4 账户状态独立

```text
state/portfolio/
├── current.yaml
└── history/YYYY-MM-DD.yaml
```

positions、cash 和 open orders 与两类 watchlist 分开。实验分别锁定初始 Judge watchlist、可选 user watchlist 和账户状态。

## 十一、实验机制与 Context Bundle

### 11.1 四个主要决策变量

1. Data Recipe：选择和生产什么数据。
2. View Recipe：Agent 初始看到哪些内容及展示多少。
3. Judge Profile：prompt、复盘因子和输入/查询能力。
4. Trader Profile：prompt、策略因子和输入/查询能力。

Broker Profile 只在研究成交规则时作为实验变量；Presenter 默认不是交易实验变量。

### 11.2 三类复现锁

- **状态锁**：日期窗口、snapshot hash、初始 Judge watchlist、可选 user watchlist、positions、cash、open orders、模型版本与采样参数。
- **工程锁**：脚本 hash、schema、数据口径、query template 和 Curator 渲染器版本。
- **评价锁**：scorer、成交假设、滑点、同柱触发顺序、指标与基准。

### 11.3 实验目录

```text
experiments/E006_<名称>/
├── experiment.yaml
├── hypothesis.md
├── bundle/
│   ├── manifest.lock.yaml
│   ├── agents/
│   ├── configs/
│   └── factors/
├── state/initial/
├── runs/YYYY-MM-DD/
│   ├── judge/
│   │   ├── digest.md
│   │   ├── input_manifest.yaml
│   │   ├── review.md
│   │   └── review.json
│   └── arms/<arm-id>/
│       ├── trader/
│       │   ├── initial_digest.md
│       │   ├── queries/
│       │   ├── input_manifest.yaml
│       │   └── order_intents.json
│       └── broker/
│           ├── orders.json
│           ├── fills.json
│           └── execution_receipt.json
├── scores/
├── summary.md
└── verdict.md
```

小型文本资产在 Bundle 中保存冻结副本；大体积数据通过不可变 snapshot ID/hash 引用。`.runtime/` 只能是可删除缓存，不能作为事实源。

### 11.4 同一复盘测试多个策略

同一实验、同一日期、同一 Judge 配置只运行一次 Judge，冻结 review。所有 Trader arm 必须引用同一个 review hash。

跨实验复用 review 时也只引用既有产物，不重跑后声称输入相同。这样结果差异才能归因于 Trader，而不是 Judge 的随机波动。

## 十二、文件契约

| 契约 | 生产者 → 消费者 | 作用 |
|---|---|---|
| snapshot.yaml | Scout → 所有人 | 数据覆盖、口径、代码与缺口 |
| digest.md + input_manifest | Curator → Judge/Trader | 初始视图与实际输入证明 |
| review.md/json | Judge → Curator/Trader/Evaluator | 市场复盘，不含交易指令 |
| query request/response | Trader ↔ Curator | 按需数据访问与留痕 |
| order_intents.json | Trader → Broker/Evaluator | 交易决策与撤改单意图 |
| orders.json / fills.json | Broker → Evaluator/下一 Run | 执行与账户事实 |
| scores | Evaluator → 用户/Presenter | 分层评价 |
| manifest.lock.yaml | Curator → Run | 冻结所有版本和 hash |

修改契约必须同步修改 schema 并 bump 版本。

## 十三、知识资产与历史资料

历史资料不因时间久而进入 archive；只有失效配置、废弃设计和被替换版本才归档。

| 目录 | 保存内容 |
|---|---|
| `reference/` | 老骚盘前计划、复盘、会议、外部文章及用户参考资料；保留来源与 available_at |
| `cases/` | 从原始资料提炼的成功、失败、错过和外部案例 |
| `reviews/` | 自身周期性日/周/月评价与归因，不复制实验原始输出 |
| `framework/` | 已验证、当前生效的稳定原则与规则 |
| `data/` | 客观行情、数据快照与数仓 |
| `state/` | 随时间变化的 Judge watchlist、user watchlist 与账户状态 |
| `experiments/` | 实验定义、冻结 Bundle、Run、评分与裁决 |
| `runs/live/` | 非实验的日常实际运行产物 |
| `archive/` | 已失效的设计、配置与版本 |
| `inbox/` | 尚未分类的新材料 |

外部资料不会因为存在就默认进入 Agent 上下文。实验或日常运行必须显式引用，并满足 `available_at <= as_of`。

## 十四、目标 workspace

```text
首分机器人-kimi/
├── ARCHITECTURE.md               # 当前架构；v3 生效时更新
├── ARCHITECTURE-V3.md            # 本文件
├── WORKSPACE.md
├── agents/
│   ├── judge/{prompts,profiles}/
│   ├── trader/{prompts,profiles}/
│   ├── broker/runbooks/
│   ├── curator/profiles/
│   └── presenter/profiles/
├── factors/{review,strategy,shared}/
├── configs/
│   ├── data/
│   ├── digest/
│   ├── queries/
│   ├── broker/
│   └── evaluation/
├── contracts/
├── data/
├── state/{watchlist,portfolio}/
├── experiments/
├── runs/live/
├── logs/                         # 轻量运行日志与可筛选索引
├── scripts/{scout,curator,judge,trader,broker,evaluation}/
├── framework/
├── cases/
├── reviews/
├── backtests/
├── reference/
├── workflows/
├── archive/
├── inbox/
└── temp/
```

### 现有资产映射

| 现有位置 | v3 去向 |
|---|---|
| `scripts/scout/annotate.py` 的 Agent Markdown 渲染 | Curator Digest 渲染器 |
| `selectors.yaml` | Data Recipe；展示参数拆入 View Recipe |
| `context/templates/judge_prompt*.md` | `agents/judge/prompts/`，再拆出 Trader 与执行内容 |
| `context/factors/` | `factors/`，保留 F 编号并补版本与 applies_to |
| `context/exp/` | `experiments/`，历史实验只读冻结 |
| `context/reference/` | `reference/` 对应来源目录 |
| `data/watchlist.yaml` | 当前 Judge 自动维护列表，归入 `state/watchlist/judge/`，补每日历史快照 |
| `data/positions/` | `state/portfolio/` |
| Judge 的 `plan.json` | Trader 的 `order_intents.json` |
| 当前人工 Trader 执行 | Broker runbook、orders、fills 与回执 |

## 十五、运行日志与筛选

当前阶段只建设轻量、可筛选的日志系统，**不新增 Operator 模块或 Agent**。日志是所有模块共用的基础能力，由流程执行器自动写入，Presenter 提供只读筛选视图。

### 15.1 最小存储

```text
logs/
└── run_events.jsonl
```

每次 Run 必须有唯一 `run_id`。Run 本身及其 Scout、Curator、Judge、Trader、Broker、Evaluator 环节至少写入 `start` 和 `end`；失败时写入 `error`。即使中途异常，也能在日志中看到这次运行及停止位置。

采用一行一个 JSON 事件的 JSONL，当前规模不引入日志服务器或独立数据库。详细决策产物仍保存在对应 Run 目录，日志只保存索引、指标和相对路径，不复制 prompt、行情内容或敏感信息。

### 15.2 事件字段

```json
{
  "timestamp": "2026-10-28T11:00:00+08:00",
  "run_id": "E006-20261019-t001",
  "experiment_id": "E006",
  "arm_id": "t001",
  "as_of": "2026-10-19",
  "module": "trader",
  "event": "end",
  "status": "success",
  "duration_ms": 12345,
  "input_tokens": null,
  "output_tokens": null,
  "cost": null,
  "query_count": 3,
  "artifact_path": "experiments/E006/runs/2026-10-19/arms/t001/trader/order_intents.json",
  "error": null
}
```

Token 和费用由运行环境提供时记录；无法取得则为 `null`，不估算或伪造。耗时、查询次数、状态和错误必须记录。

### 15.3 可筛选视图

Presenter 读取 `run_events.jsonl`，提供一个简单表格，支持：

- 按运行日期、实验、Run ID、arm、模块和状态筛选；
- 按开始时间、耗时、Token 或费用排序；
- 查看某次 Run 的全部模块事件与错误；
- 通过 `artifact_path` 打开对应输出。

当前不做复杂监控平台、自动告警、预算熔断或运行编排。出现并发调度、自动重试和预算控制需求后，再评估是否设立独立 Operator。

## 十六、回放与运行隔离

1. 每个决策日使用独立决策上下文，禁止携带后续走势记忆。
2. 每次 Run 只读 Bundle、允许的 snapshot 和 Curator 返回结果。
3. 所有文件和查询均满足 `available_at <= as_of`。
4. Judge watchlist、启用的 user watchlist 和账户状态均使用当日快照，禁止读取实时 current 代替历史状态。
5. Judge、Trader 和 Evaluator 不共享隐式结论；跨模块只传契约产物。
6. 评分只走冻结的评价配置，模型不得修改机械对账结果。
7. 原始 review、order intent、委托与成交记录锁定后不得事后覆盖。
8. 日志只是运行索引，不得替代或事后修改决策与成交事实文件。

## 十七、迁移顺序与记录

2026-10-28 已完成首轮工程重组、目录迁移和结构验收；后续真实 LLM 实验沿用同一契约：

1. 备份并记录当前文件清单与 hash，冻结 E001–E005。
2. 创建 v3 目录、schema 和 registry，不先删除旧文件。
3. 迁移数据配置与 Curator 渲染器，验证同一 snapshot 可生成不同 Digest。
4. 拆分 Judge/Trader prompt 与 Profile，分类现有因子。
5. 建立 Judge/user 两类 watchlist 与 portfolio 状态目录、标签字典和每日快照。
6. 建立受控查询接口、输入清单和查询日志。
7. 建立 Trader order intent 与人工 Broker 回执契约。
8. 建立 JSONL 运行日志与最小可筛选视图，成功、失败和中断 Run 均可见。
9. 用合成样本验证“一次 Judge + 两个 Trader arms”和 order intent 引用锁定。
10. 新增 Scout 快照完整性清单，冻结配置、代码 hash、derived/raw hash 与缺口。
11. 新增最小 v3 评分器，用合成样本分别验证止盈、止损、未成交和 Broker 执行事实。
12. 切换日常链路，更新 ARCHITECTURE.md、WORKSPACE.md 和 workflows。
13. 旧路径在引用更新后归档；历史实验不为了对齐新格式而改写。

当前状态：v3 工程链路可用于真实运行，但尚未完成真实 LLM 实验验证。明确的实施边界如下：

- `get_theme_members` 仍只是涨跌停事件候选，完整同花顺板块成员尚未接入。
- `available_at` 目前是交易日粒度；KPL 次日增强数据在历史回放时必须额外核查实际决策前是否已可用。
- 最小评分器机械回放 buy；sell/hold/cancel 不推断收益，Judge 语义质量单独评价。
- Broker 由人工执行，程序不从少量 fills 推断完整账户收益。
- user watchlist 已有因子定义、状态目录和手动维护契约，但尚无股票样本与启用它的对照实验。

## 十八、本版明确不做

- 不让 Trader 直接访问外部数据源或任意文件。
- 不让 Curator 用 LLM 自由摘要行情。
- 不让 Judge 继续输出三价和持仓改单。
- 不要求现阶段自动挂单，Broker 继续人工执行。
- 不让 Presenter 影响实验或交易结果。
- 不用单笔盈亏直接修改框架或晋升因子。
- 不在当前阶段引入独立 Operator 或复杂日志平台。
- 不在未验证的情况下删除历史实验或迁移前备份。
