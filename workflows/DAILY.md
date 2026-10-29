# 每日执行流程 v4（auto-trader 命令版）

每日链路分为 Scout 数据、Curator 上下文、Judge 复盘、Trader 订单意图、人工 Broker 执行和 Evaluator 后验评价。Judge 不再直接生成三价交易单。

> 迁移状态：Scout / Curator 已收口到 `auto-trader` 命令（第 6 步完成）；Judge / Trader /
> Evaluator / Presenter 仍在迁移中，第三~七节保留旧脚本命令，迁移完成后统一替换。

## 一、Scout：定时或手动准备标准数据

1. 确认交易日、最近收盘日、下一交易日和 `as_of`。
2. 一条命令跑完整数据链（取数→入库→筛选→冻结），任一关键步骤失败则停止：

```bash
auto-trader scout run --date YYYYMMDD
```

3. 检查 `data/snapshots/YYYY-MM-DD/snapshot.yaml` 的数据覆盖、版本和缺口。
4. Scout 不生成 Agent Digest，也不自动触发 Judge/Trader。

需要单步执行或排障时用：

```bash
auto-trader scout fetch --dates YYYYMMDD        # 只取数
auto-trader scout load --date YYYYMMDD          # 只入库
auto-trader scout build --date YYYYMMDD         # 只加工 curated
auto-trader scout select --date YYYYMMDD        # 只筛选
auto-trader scout finalize --date YYYYMMDD      # 只冻结清单
```

历史回补（核对缺口→补拉→可选链式重建）：

```bash
auto-trader scout catchup --start YYYYMMDD --end YYYYMMDD [--check|--replay]
```

板块成分（按需，非每日必跑）：`get_board_momentum` 查询依赖 `dim_board_member`，
由以下命令维护（raw 按快照日只追加，同戳 7 天内缓存；**历史回放某天 D 前须以 `--date D`
补盖快照**，因 ths_member 只暴露当前最新成分、接口无法恢复历史变更；Judge 预判板块不在
当日板块榜时用 `--boards 885xxx.TI` 显式补取）：

```bash
auto-trader scout members --date YYYYMMDD
```

## 二、Curator：生成 Judge 初始视图

```bash
auto-trader curator digest --date YYYYMMDD --module judge \
  --output runs/live/YYYY-MM-DD/judge/digest.md --run-id live-YYYYMMDD-judge
```

检查 Digest 与 `input_manifest.yaml`。更换 display_n 只改 View Recipe，不重跑 Scout。

> 过渡说明：run 日志登记（原 `run_logged.py` 外壳）将在第 7 步 `daily prepare` 统一接管，
> 此前 Judge/Trader 等模块的运行登记仍按第七节旧方式执行。

## 三、Judge：只复盘

1. 使用指定 Judge Profile；历史回放先冻结实验 Bundle。
2. 只读 manifest 中的 Digest、因子、历史 review、Judge watchlist，以及显式启用的 user watchlist。
3. 输出 `review.md` 和 `review.json`，不含买卖、三价、仓位或改单。
4. 校验：

```bash
uv run python scripts/common/validate_contract.py \
  runs/live/YYYY-MM-DD/judge/review.json contracts/review.schema.json
uv run python scripts/judge/validate_review.py \
  runs/live/YYYY-MM-DD/judge/review.json
```

第二条做语义校验（v1.1 结构化字段、首分 stage_basis、theme_id 引用完整性），与 schema 校验互补，不可互相替代。

5. 有 `watchlist_updates` 时，由 Curator 应用并冻结快照；user watchlist 只能由用户修改。

```bash
auto-trader curator watchlist apply \
  --review runs/live/YYYY-MM-DD/judge/review.json \
  --target state/watchlist/judge/current.yaml
```

user watchlist 只能由用户修改，CLI 同样不允许对其执行 apply。

## 四、Trader：按需查询并生成订单意图

1. Curator 组装 Judge review、Trader Profile、初始 Trader View 和 portfolio 快照。
2. Trader 仅通过受控查询拉取板块候选、日线或快照表：

```bash
auto-trader curator query --request <task.yaml>
```

> 过渡命令：`uv run python scripts/curator/query_data.py --request <task.yaml>`

3. `get_theme_members` 当前只返回涨跌停事件候选，不能声称完整板块成员覆盖。
4. **对 Judge 预判的明日首分板块，Trader 必须调用 `get_board_momentum`**（`board_codes` 传全部预判板块、`top_n=20`），取全量成分按两日涨幅排序的清单，作为择股与挂价预测的主输入；查询前确认 Scout 已用 `auto-trader scout members --date YYYYMMDD` 维护 `dim_board_member`。
5. 输出 `order_intents.json`；空仓必须填写 `no_trade_reason`。
6. 校验 schema、引用 hash 和三价关系：

```bash
uv run python scripts/trader/validate_order_intents.py \
  runs/live/YYYY-MM-DD/arms/<arm-id>/trader/order_intents.json
```

## 五、人工 Broker：执行与回填

1. 只执行已校验的订单意图，不自行改价或改仓位。
2. 委托与撤改单写 `orders.json`，成交写 `fills.json`，拒单/未提交写 `execution_receipt.json`。
3. 更新 `state/portfolio/current.yaml` 并冻结当日历史快照。
4. 未触发、未提交、未成交、部分成交和真实成交分开记录。

## 六、Evaluator：结果可见后评价

- 分开评价 Judge 复盘质量、Trader 决策质量、Broker 执行质量和整体结果。
- 机械对账使用冻结的 evaluation 配置；模型不得修改 Score。
- 正常亏损不直接生成新规则，候选 insight 进入周期归因与晋升流程。

```bash
uv run python scripts/evaluation/score_v3.py \
  --order-intents runs/live/YYYY-MM-DD/arms/<arm-id>/trader/order_intents.json \
  --broker-dir runs/live/YYYY-MM-DD/arms/<arm-id>/broker
```

最小评分器只回放 buy 意图；sell/hold/cancel 不推断收益。Judge 语义质量独立评价，Broker 实际成交单独汇总，不用单日盈亏替代复盘质量。E001–E005 继续使用旧 `scorer.py`。

## 七、每日日志与落档

每次脚本或 Agent 运行都应登记 run 日志。结束后刷新筛选页面：

```bash
uv run python scripts/presenter/render_log_viewer.py
```

打开 `logs/index.html`，按日期、实验、Run ID、arm、模块或状态筛选。

日常产物：

```text
runs/live/YYYY-MM-DD/
state/watchlist/{judge,user}/history/YYYY-MM-DD.yaml
state/portfolio/history/YYYY-MM-DD.yaml
logs/run_events.jsonl
```
