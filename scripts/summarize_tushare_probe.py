"""Convert the small MCP probe responses into local CSVs and a coverage summary."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "data" / "probe"


def payload(path: Path):
    body = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(body, list):
        body = body[0]
    content = body.get("result", {}).get("content", [])
    if not content:
        return body
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": text, "isError": body.get("result", {}).get("isError", False)}


def main() -> None:
    calendar = pd.DataFrame(payload(PROBE / "trade_cal_20261005.json"))
    daily = pd.DataFrame(payload(PROBE / "daily_20260930.json"))
    basic = pd.DataFrame(payload(PROBE / "daily_basic_20260930.json"))
    limits = pd.DataFrame(payload(PROBE / "stk_limit_20260930.json"))
    limit_detail = payload(PROBE / "limit_list_d_20260930.json")

    daily.to_csv(PROBE / "daily_20260930.csv", index=False)
    basic.to_csv(PROBE / "daily_basic_20260930.csv", index=False)
    limits.to_csv(PROBE / "stk_limit_20260930.csv", index=False)

    daily.sort_values("amount", ascending=False).head(50).to_csv(
        PROBE / "amount_top50_20260930.csv", index=False
    )
    daily.sort_values("pct_chg", ascending=False).head(50).to_csv(
        PROBE / "pct_up_top50_20260930.csv", index=False
    )
    daily.sort_values("pct_chg", ascending=True).head(50).to_csv(
        PROBE / "pct_down_top50_20260930.csv", index=False
    )

    target = calendar.loc[calendar["cal_date"] == "20261005"].iloc[0]
    later_open = calendar.loc[
        (calendar["cal_date"] > "20261005") & (calendar["is_open"] == 1), "cal_date"
    ].min()
    summary = {
        "target_date": "20261005",
        "target_is_open": int(target["is_open"]),
        "previous_trade_date": target["pretrade_date"],
        "next_trade_date": later_open,
        "daily_rows": len(daily),
        "daily_unique_tickers": int(daily["ts_code"].nunique()),
        "daily_basic_rows": len(basic),
        "stk_limit_rows": len(limits),
        "amount_top50_generated": True,
        "pct_up_top50_generated": True,
        "pct_down_top50_generated": True,
        "limit_list_d": limit_detail,
    }
    (PROBE / "permission_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
