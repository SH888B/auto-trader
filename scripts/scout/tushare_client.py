# -*- coding: utf-8 -*-
"""兼容垫片：Tushare 转接客户端已迁至 autotrader.relay（V4 切换后删除本文件）。

保留对本模块的既有 import（scripts/fetch_daily_by_date.py、temp 下的抢救/探针脚本）。
"""
from __future__ import annotations

from autotrader.core import WORKSPACE_ROOT as ROOT  # noqa: F401
from autotrader.relay import (  # noqa: F401
    FALLBACK,
    PRIMARY,
    AllEndpointsDownError,
    RelayError,
    TushareClient,
    load_config,
)
