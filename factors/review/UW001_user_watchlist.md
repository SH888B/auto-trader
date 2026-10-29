# UW001 —— user watchlist

- 版本：v1.0
- 类型：用户先验因子
- 状态：待验证（当前仅建空清单与接入契约，尚未维护股票或开展对照实验）

用户维护的股票关注清单及标签，作为可选输入注入 Judge 或 Trader Profile。它不与 Judge 自动维护的 watchlist 合并，也不赋予 Judge 修改权限。

实验启用时必须引用 as_of 时点快照及 hash；比较启用/停用两臂时，其他配置保持不变。标签语义见 state/watchlist/labels.yaml。列表本身不是长期规则，标签定义与使用方式的修改才 bump 本因子版本。
