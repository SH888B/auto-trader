# 首分机器人工作空间

> **V5 提示（2026-10-30）**：目录职责与数据链路以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准。
> 本文档里的 `data/curated`、`data/snapshots`、`state/*.yaml` 是过渡期旧路径，V5 迁移后删除；
> 旧架构见 [docs/archive/](docs/archive/)。

本工作空间服务一个目标：研究并执行“活跃题材的首次分歧—随后修复”交易，同时保证输入可追溯、无未来信息、规则可回滚。

当前架构见 `ARCHITECTURE.md`（V5）与 `docs/archive/ARCHITECTURE-v3.md`（历史）。本文件管资产边界和知识纪律。


## 一、目录职责

| 目录 | 放什么 | 不放什么 |
|---|---|---|
| `agents/` | Judge/Trader 的薄 prompt 与可执行 Profile；Broker runbook；Curator/Presenter 配置 | 每日观点、直接内嵌整套因子 |
| `factors/` | 可组合的 review/strategy/shared 因子及版本注册表 | 每日股票列表、未标版本的规则 |
| `configs/` | data/digest/query/broker/evaluation 配置 | 密钥、单日结论 |
| `config/` | 取数通道密钥与连接配置；只允许取数客户端读取 | 业务配置、实验配置 |
| `contracts/` | review、order intent、委托、成交、运行清单与日志 schema | 具体某天的数据 |
| `data/` | raw、标准快照（过渡期）、数仓和客观数据资产 | Agent 专属 Digest、手工覆盖 raw |
| `state/` | Judge watchlist、user watchlist、portfolio 的 current 与历史快照 | 长期稳定规则 |
| `experiments/` | 实验定义、冻结 Bundle、初始状态、Run、Score 与 verdict | 未声明输入、事后修改的原始判断 |
| `runs/live/` | 非实验的日常 Judge/Trader/Broker 产物 | 实验假设与对照配置 |
| `logs/` | 每次运行的 JSONL 事件与可筛选 HTML 视图 | 决策与成交事实的唯一副本 |
| `framework/` | 长期稳定原则、当前规则、讨论与研究提案 | 单日观点、未验证感悟 |
| `cases/` | 成功、失败、错过和外部案例，关联当时行情与原始资料 | 脱离行情的故事 |
| `reviews/` | 自身周/月/季周期评价与归因 | 实验原始输出的重复副本 |
| `backtests/` | 回测设计与历史研究资产 | 用未来信息修饰计划 |
| `reference/` | 老骚原文、外部资料、历史 PRD 与参考资料，保留来源和 available_at | 默认生效的机器人规则 |
| `workflows/` | 每日、每周怎样执行 | 某天的买卖结论 |
| `archive/` | 失效设计、旧版本和迁移前备份 | 仍生效的唯一事实源 |
| `inbox/`、`temp/` | 未分类输入与可删除试跑产物 | 需要长期留档的唯一资产 |

## 二、两条 Watchlist

- `state/watchlist/judge/`：Judge 自动维护的市场跟踪状态，Judge 输出 updates，Curator 校验落盘。
- `state/watchlist/user/`：用户维护的可选因子，只有 Profile 显式启用才能进入 Agent 输入。
- 两者均按日冻结；历史回放禁止用 current 替代当时快照。
- `state/portfolio/` 独立保存 positions、cash、open orders。

## 三、实验纪律

1. 每个实验有研究问题、假设、对照和评分口径。
2. 主要变量为 Data Recipe、View Recipe、Judge Profile、Trader Profile；其他状态、工程和评价配置全部锁定。
3. Curator 冻结小型文本资产，并用 hash 引用大体积数据快照。
4. 同一实验测试多个 Trader arms 时，共用同一 Judge review hash。
5. 每个决策日独立上下文；历史回放只使用已冻结且在实际决策前可用的输入。当前 `as_of` 是交易日粒度，精确时间戳隔离仍是实施限制，尤其要核查 KPL 次日增强数据。
6. 原始 review、order intent、委托、成交和 Score 锁定后不得事后覆盖。

## 四、知识晋升流程

```text
行情 + 原文 + 实际成交
        ↓
实验与日常运行的分层 Score
        ↓
周/月/季周期归因与案例
        ↓
可证伪 insight（证据、反例、适用环境）
        ↓
历史回放 + 向前影子验证
        ↓
用户裁决
        ↓
晋升 framework / factor / Profile 新版本，形成新基线
```

禁止路径：一次盈利/亏损或一段高手观点 → 直接修改实盘规则。

## 五、日常读取顺序

1. `workflows/DAILY.md`：确定当天链路。
2. 指定 Data Recipe 和 Scout snapshot：确认数据覆盖与缺口。
3. 指定 Judge/Trader Profile 和 Bundle：确认 prompt、因子、视图与状态版本。
4. Judge 只复盘；Trader 参考复盘、策略和受控查询生成订单意图。
5. Broker 执行并回填；Evaluator 在结果可见后对账。
6. 每次运行通过日志包装器登记，运行后刷新 `logs/index.html`。

## 六、历史与回滚

- E001–E005 是历史实验，保留旧目录内容和契约，不为对齐 v3 改写。
- 迁移前文件与 hash 清单位于 `archive/pre-v3-20261028/`。
- 旧 Scout Digest 实现、旧架构和旧注册表位于 `archive/legacy/`。
