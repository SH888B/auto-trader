# 人工 Broker 执行手册 v1

1. 只读取 Trader 已校验的 order_intents.json，不自行改变标的、价格或仓位。
2. 实际委托、撤单和改单写入 orders.json；无法提交或拒单写 execution_receipt.json。
3. 成交、费用和滑点写入 fills.json；部分成交与未成交不得省略。
4. 当日完成后更新 state/portfolio/current.yaml，并由 Curator 冻结历史快照。
5. 日志只记录索引与指标，订单和成交事实以对应 JSON 文件为准。
