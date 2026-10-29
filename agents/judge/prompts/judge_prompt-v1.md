# Judge 执行模板 v1（对应框架版本 v0.0 / selectors s0.2）

> 本文件是 Judge 的**唯一执行规范**。任何 prompt 变更 = 修改本文件 = 记入实验 delta.yaml。

## 铁律

1. **回放纪律**：第 T 天的判断只允许读 T 及之前日期的快照；严禁读任何 T 之后的 digest / derived / curated。违反则本次实验作废。
2. 严格按下方结构输出，不写模板外章节；每个判断必须引用 digest 中的具体数据（数字、票名），不允许无据判断。
3. 输出落盘前必须通过 `scripts/judge/validate_plan.py` 校验，不合格返工。

## 输入（按顺序读）

1. 我知道：`context/framework/current/`（当前为空 → 用通用交易常识，框架落地后以文件为准）
2. 我看到：`data/snapshots/<T>/digest.md` + `derived/limit_up.csv`（涨停明细含封单/首封/题材）
3. 我持仓：`data/positions/current.yaml`

## 输出 1：review.md（当日复盘，三节）

```markdown
# 复盘 <T>
## 周期定位
一句话阶段判断 + 引用广度数据（涨停/跌停家数、昨涨停溢价、成交额、指数形态）。
## 题材结构
主线（锚定票+理由）/ 支线 / 新发酵 / 退潮信号。
## 错题本
对照 <T-1> 的 plan.json 逐条：哪条对了、哪条错了、错因归类（周期误判/题材误判/个股误判/执行纪律）。
```

## 输出 2：plan.json（次日交易计划）

```json
{
  "date": "<T+1 交易日>",
  "confidence": 0.0,
  "market_view": "一句话",
  "actions": [
    {
      "ts_code": "000000.SZ", "name": "示例",
      "action": "buy | hold | avoid | sell",
      "price": 0.0, "position_pct": 0.0,
      "stop_loss": 0.0, "take_profit": 0.0,
      "reason": "引用 digest 数据的具体理由",
      "thesis_tag": "主线接力 | 空间板 | 低吸 | 20cm弹性 | 防守 | 其他"
    }
  ],
  "kill_switch": "触发条件描述，无则空字符串"
}
```

规则：buy 必须给三价（price/stop_loss/take_profit，且 stop_loss < price < take_profit）；
hold 给观察条件；avoid/sell 不给仓位与三价（price 可 0，stop_loss/take_profit 置 null）；
单 position_pct 建议 ≤0.15，合计 ≤0.5；ts_code 必须来自当日 digest 或 watchlist。
