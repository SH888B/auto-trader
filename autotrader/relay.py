"""Tushare 转接客户端 —— Scout 数据管道共用层。

由 scripts/scout/tushare_client.py 迁入（DESIGN-V4 §六-4），逻辑逐行等价；
变更仅限：工作区根目录统一取自 core.WORKSPACE_ROOT。

职责（对应 PRD-Scout-v1 §3.1、FR-1）：
- 从 config/tushare_relay.yaml 读取 token 与双端点；密钥绝不打印、不回显；
- 按"接口 → 首选端点"路由：两个第三方转接端点覆盖的接口集不同，
  primary 通核心行情，fallback 通打板专题（kpl / limit_cpt_list / limit_step 等）；
- 429 按 retry_after 退避、传输错误指数退避、端点级故障自动 failover；
- 原始响应只追加落盘 data/raw/<api>/，已存在则跳过（幂等）。

调用纪律：所有请求串行，调用间隔默认 1.5 秒，不触发上游限速。
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .core import WORKSPACE_ROOT

# 端点名与配置文件中 url_primary / url_fallback 的顺序对应
PRIMARY, FALLBACK = "primary", "fallback"

# 接口首选端点路由表：不在表内的接口默认走 primary。
# 依据 2026-10-26 实测：fallback 支持打板专题（40101 只出现在 primary 调这些接口时）。
_API_PREFERENCE: dict[str, str] = {
    "kpl_list": FALLBACK,
    "kpl_concept_cons": FALLBACK,
    "limit_cpt_list": FALLBACK,
    "limit_step": FALLBACK,
    "limit_list_ths": FALLBACK,
    "ths_daily": FALLBACK,
}

# 全量型接口的最小合理行数：低于此值视为端点返回了空壳/截断数据（failover 依据）。
# 2026-10-26 实测：fallback 对 stock_basic 返回 code=0 但 0 行——空结果≠真成功。
_MIN_ROWS: dict[str, int] = {
    "stock_basic": 4000,
    "trade_cal": 200,
    "ths_index": 800,
    "daily": 4000,
    "stk_limit": 4000,
    "daily_basic": 4000,
    "limit_cpt_list": 15,
    "index_daily": 3,
}


class RelayError(Exception):
    """转接层错误基类。"""


class AllEndpointsDownError(RelayError):
    """所有端点均不可用。"""


def load_config() -> tuple[str, dict[str, str]]:
    """读取 token 与端点 URL。

    Returns:
        (token, {"primary": url, "fallback": url})

    Raises:
        RelayError: 配置缺失或格式不对。
    """
    text = (WORKSPACE_ROOT / "config" / "tushare_relay.yaml").read_text(encoding="utf-8")
    token_match = re.search(r"^token:\s*(\S+)", text, re.M)
    urls = re.findall(r"^url_\w+:\s*(\S+)", text, re.M)
    if not token_match or len(urls) < 2:
        raise RelayError("config/tushare_relay.yaml 中 token 或端点缺失")
    return token_match.group(1), {PRIMARY: urls[0], FALLBACK: urls[1]}


class TushareClient:
    """带路由、退避、failover 的 Tushare 转接客户端。

    Attributes:
        pace: 相邻两次调用的最短间隔（秒），串行纪律。
        max_retries: 单接口最大尝试次数（含端点切换）。
        timeout: 单次请求超时（秒）；kpl 等大 payload 接口可调高。
        call_log: 调用记录列表，元素为 {api, endpoint, params, rows, seconds, error}。
    """

    def __init__(self, pace: float = 1.5, max_retries: int = 4, timeout: int = 60) -> None:
        self._token, self._endpoints = load_config()
        self.pace = pace
        self.max_retries = max_retries
        self.timeout = timeout
        self._last_call_ts = 0.0
        # 会话级端点故障表：某端点连续失败后本会话内降级，优先走另一端点
        self._endpoint_bad: dict[str, float] = {}
        self.call_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 底层通信
    # ------------------------------------------------------------------ #
    def _post_once(self, base: str, api: str, params: dict[str, Any]) -> dict[str, Any]:
        """发一次请求并返回官方协议响应（含 code/msg/data）。"""
        body = json.dumps({
            "api_name": api,
            "token": self._token,
            "params": params,
            "fields": "",
        }).encode("utf-8")
        req = urllib.request.Request(
            base, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _sleep_pace(self) -> float:
        """保证相邻调用间隔不小于 pace 秒，返回本次调用开始时间戳。"""
        now = time.monotonic()
        wait = self.pace - (now - self._last_call_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_call_ts = time.monotonic()
        return self._last_call_ts

    def call(self, api: str, params: dict[str, Any], *,
             timeout: int | None = None) -> dict[str, Any]:
        """调用指定接口，自动完成端点路由、429 退避与 failover。

        Args:
            api: Tushare 接口名，如 "daily"。
            params: 接口参数，全部以字符串值传入。
            timeout: 覆盖默认超时时长（大 payload 接口用）。

        Returns:
            官方协议 dict，code == 0 表示成功。

        Raises:
            AllEndpointsDownError: 所有端点尝试后仍失败。
        """
        if timeout:
            old, self.timeout = self.timeout, timeout
        else:
            old = None
        try:
            return self._call_inner(api, params)
        finally:
            if old:
                self.timeout = old

    def _call_inner(self, api: str, params: dict[str, Any]) -> dict[str, Any]:
        pref = _API_PREFERENCE.get(api, PRIMARY)
        order = [pref, FALLBACK if pref == PRIMARY else PRIMARY]
        # 故障端点排后
        order.sort(key=lambda b: self._endpoint_bad.get(b, 0.0))

        last_error = ""
        for attempt in range(self.max_retries):
            base_key = order[attempt % len(order)]
            base = self._endpoints[base_key]
            start = self._sleep_pace()
            entry: dict[str, Any] = {"api": api, "endpoint": base_key, "params": params}
            try:
                data = self._post_once(base, api, params)
            except (urllib.error.URLError, TimeoutError, OSError,
                    http.client.HTTPException) as exc:
                entry.update(seconds=round(time.monotonic() - start, 2),
                             error=str(exc)[:120])
                self.call_log.append(entry)
                # 传输层错误：标记端点故障，指数退避后换端点
                self._endpoint_bad[base_key] = time.monotonic()
                last_error = f"{base_key} 传输错误: {exc}"
                time.sleep(min(3 * (2 ** attempt), 24))
                continue

            code = data.get("code")
            entry["rows"] = len((data.get("data") or {}).get("items", []))
            entry["seconds"] = round(time.monotonic() - start, 2)
            self.call_log.append(entry)

            if code == 0:
                # 空壳守卫：全量型接口返回行数过少，视为端点数据不完整。
                # 仅对"完整请求"生效（无 offset）；分页尾页行数天然可能不足，不适用。
                if api in _MIN_ROWS and entry["rows"] < _MIN_ROWS[api] \
                        and "offset" not in params:
                    self._endpoint_bad[base_key] = time.monotonic()
                    last_error = (f"{base_key} 返回行数 {entry['rows']} < 期望 "
                                  f"{_MIN_ROWS[api]}（空壳/截断）")
                    time.sleep(min(3 * (2 ** attempt), 12))
                    continue
                return data
            if code == 400 and "invalid_params" in str(data.get("msg")):
                # 转接高负载时会把健康请求随机判为 invalid_params（2026-10-26 实测）
                self._endpoint_bad[base_key] = time.monotonic()
                last_error = f"{base_key} 偶发 invalid_params（疑似高负载）"
                time.sleep(min(3 * (2 ** attempt), 12))
                continue
            if code == 429:
                # 上游限速：按指引退避后原端点重试
                retry_after = ((data.get("help") or {}).get("retry_after_seconds")) or 2
                time.sleep(min(float(retry_after) + 1.0, 10.0))
                last_error = f"429 限速: {data.get('msg')}"
                continue
            if code == 40101:
                # token 被拒：对该接口而言此端点不可用，切换另一端点
                self._endpoint_bad[base_key] = time.monotonic()
                last_error = f"{base_key} 不支持该接口(40101)"
                continue
            # 其他业务错误（参数错误、积分不足等）：不重试，直接抛
            raise RelayError(f"{api} 调用失败 code={code}: {data.get('msg')}")

        raise AllEndpointsDownError(
            f"{api} 重试 {self.max_retries} 次均失败。最后错误: {last_error}")

    # ------------------------------------------------------------------ #
    # 分页拉取（daily 单端点单次上限 5000 行，见 PRD §3.2）
    # ------------------------------------------------------------------ #
    def call_paginated(
        self,
        api: str,
        params: dict[str, Any],
        *,
        page_size: int = 5000,
        max_pages: int = 10,
    ) -> tuple[list[str], list[list[Any]]]:
        """循环 offset 分页拉取全量，直到返回行数不足一页。

        Returns:
            (fields, items)：字段名列表与全部行。
        """
        all_items: list[list[Any]] = []
        fields: list[str] = []
        for page in range(max_pages):
            paged = dict(params)
            if page > 0:
                paged["offset"] = str(page * page_size)  # 部分接口不接受 offset=0
            data = self.call(api, paged)
            body = data.get("data") or {}
            items = body.get("items", [])
            if not fields:
                fields = body.get("fields", [])
            all_items.extend(items)
            if len(items) < page_size:
                break
        return fields, all_items

    # ------------------------------------------------------------------ #
    # 原始数据落盘（只追加，不修改 —— WORKSPACE.md §五）
    # ------------------------------------------------------------------ #
    @staticmethod
    def save_raw(api: str, name_key: str, payload: dict[str, Any], *,
                 force: bool = False) -> Path | None:
        """原始响应落盘 data/raw/<api>/<api>_<name_key>.json。

        Returns:
            文件路径；若已存在且未 force，返回 None 表示跳过。
        """
        out_dir = WORKSPACE_ROOT / "data" / "raw" / api
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{api}_{name_key}.json"
        if path.exists() and not force:
            return None
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def load_raw(api: str, name_key: str) -> dict[str, Any] | None:
        """读取已落盘的原始响应；不存在返回 None。"""
        path = WORKSPACE_ROOT / "data" / "raw" / api / f"{api}_{name_key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_call_log(self, name_key: str) -> Path:
        """调用记录落盘 temp/call_log_<name_key>.json，供积分与稳定性监控。"""
        out = WORKSPACE_ROOT / "temp" / f"call_log_{name_key}.json"
        out.write_text(
            json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                        "calls": self.call_log}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        return out
