# 首分机器人架构

当前生效版本：**v3.0（2026-10-28）**。

完整架构、模块边界、文件契约、实验机制、日志系统与迁移记录见 `ARCHITECTURE-V3.md`。

- Scout 只生产标准数据；Curator 生成 Agent Digest 并执行受控查询。
- Judge 只复盘；Trader 生成订单意图；Broker 当前由人工执行。
- Evaluator 分层评价；Presenter 只读展示。
- 每次运行写入 `logs/run_events.jsonl`，可打开 `logs/index.html` 筛选。
- E001–E005 保留旧契约，迁移前回滚副本位于 `archive/pre-v3-20261028/`。
- 旧架构 v2 位于 `archive/legacy/ARCHITECTURE-v2.md`，仅作历史参考。
