# -*- coding: utf-8 -*-
"""auto-trader：首分机器人核心程序。

模块边界沿用 ARCHITECTURE-V3：Scout 数据、Curator 上下文、Judge 复盘、
Trader 订单意图、Broker 执行、Evaluator 评价、Presenter 展示。
LLM 步骤（Judge/Trader/Evaluator 归因）现阶段由外部执行者（Kimi Work）
通过交接包（handoff）执行，prepare()/collect() 两阶段就是交接协议。
"""

__version__ = "0.4.0"
