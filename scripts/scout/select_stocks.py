# -*- coding: utf-8 -*-
"""Scout 筛选层（M2，对应 PRD FR-3）。

用法：
    uv run python scripts/scout/select_stocks.py --date 20261026

行为：
- 从数仓（data/warehouse.duckdb）读取行情与标记，按 selectors.yaml 配置筛选；
- universe 过滤（universe.yaml：市场前缀、ST、上市天数——维度数据缺失时自动降级并记录）；
- 产出 data/snapshots/YYYY-MM-DD/derived/*.csv，每组文件带 selectors 版本号（可复现）。

注意：Scout 只生产标准数据，不生成 Agent Digest；完成后运行 finalize_snapshot.py 冻结清单。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tushare_client import ROOT  # noqa: E402

DB_PATH = ROOT / "data" / "warehouse.duckdb"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def universe_filter(df: pd.DataFrame, universe: dict, names: dict[str, str] | None) -> pd.DataFrame:
    """universe.yaml 过滤：市场前缀 + ST（有名称数据时）+ 上市天数（有维度数据时）。

    Returns:
        过滤后的 DataFrame；过滤动作记入调用方日志。
    """
    prefixes = [p for m in universe.get("markets", {}).values() for p in m.get("code_prefix", [])]
    if prefixes:
        df = df[df["ts_code"].str[:3].isin(prefixes) | df["ts_code"].str[:2].isin(prefixes)]
    if universe.get("exclusions", {}).get("st_stock") and names:
        mask = df["ts_code"].map(lambda c: "ST" in (names.get(c) or "").upper())
        df = df[~mask]
    # 停牌：当日无成交（amount==0 或无行）天然被排除在 derived 之外
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout 筛选层")
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    args = parser.parse_args()

    date_dash = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
    out_dir = ROOT / "data" / "snapshots" / date_dash / "derived"
    out_dir.mkdir(parents=True, exist_ok=True)

    selectors = load_yaml(ROOT / "configs" / "data" / "selectors.yaml")
    universe = load_yaml(ROOT / "configs" / "data" / "universe.yaml")
    sel_ver = selectors.get("version", "?")

    con = duckdb.connect(str(DB_PATH), read_only=True)
    today = con.execute(
        # 数仓 fact_daily_bar 混有指数行（7 只），全部显式排除（000688 会误过 universe 前缀过滤）
        "SELECT b.* FROM fact_daily_bar b WHERE b.trade_date = ? AND b.ts_code NOT IN "
        "('000001.SH','399001.SZ','399006.SZ','399317.SZ','000688.SH','399986.SZ','932000.CSI')",
        [date_dash]).fetchdf()
    if today.empty:
        raise SystemExit(f"{args.date}: 数仓中无当日行情，请先运行 build + load_to_warehouse")

    names: dict[str, str] = {}
    dim = con.execute("SELECT ts_code, name FROM dim_stock").fetchdf()
    if not dim.empty:
        names = dict(zip(dim["ts_code"], dim["name"]))
    mapped = today["ts_code"].map(names)
    today["name"] = mapped.where(mapped.notna(), "")  # 避免 float 列 fillna(str) 的 Pandas4Warning

    pool = universe_filter(today, universe, names)
    print(f"[universe] {len(today)} -> {len(pool)} 只（ST过滤 {'启用' if names else '降级:无名称数据'}）")

    def save(df: pd.DataFrame, name: str, note: str) -> None:
        df = df.copy()
        df.insert(0, "selectors_version", sel_ver)
        df.to_csv(out_dir / f"{name}.csv", index=False, encoding="utf-8-sig")
        print(f"[derived] {name}.csv: {len(df)} 行（{note}）")

    groups = selectors.get("groups", {})
    # 成交额 / 涨幅 / 跌幅 Top
    save(pool.nlargest(groups["amount_top"]["n"], "amount"), "amount_top", "成交额榜")
    gain = pool[pool["pct_chg"] > 0].nlargest(groups["gain_top"]["n"], "pct_chg")
    save(gain, "gain_top", "涨幅榜")
    loss = pool.nsmallest(groups["loss_top"]["n"], "pct_chg")
    save(loss, "loss_top", "跌幅榜")
    # E003 新增三组（s0.3）
    pit_cfg = groups.get("pit_top", {})
    if pit_cfg:
        min_amt = float(pit_cfg.get("min_amount_yi", 1.0)) * 1e5  # 亿元→千元
        cand = pool[pool["amount"] >= min_amt].copy()
        # E006 人工审核修订：坑 = 收盘距高点回落（负值越大越坑），而非高低振幅
        cand["收盘回落%"] = ((cand["close"] - cand["high"]) / cand["high"] * 100).round(2)
        save(cand.nsmallest(int(pit_cfg.get("n", 20)), "收盘回落%"),
             "pit_top", "当日坑货榜（收盘距高点回落%）")
    gp_path = ROOT / "data" / "curated" / date_dash / "gain_periods.csv"
    gp = pd.read_csv(gp_path) if gp_path.exists() else pd.DataFrame()
    gw_cfg = groups.get("gain_week", {})
    gw_col = f"近{gw_cfg.get('window', 5)}日涨幅%"
    if gw_cfg and not gp.empty and gw_col in gp.columns:
        gw = gp.nlargest(int(gw_cfg.get("n", 30)), gw_col).merge(
            pool[["ts_code", "name", "pct_chg", "amount"]], on="ts_code", how="left")
        save(gw, "gain_week", "周涨幅榜（查漏补缺近期强弱）")
    cc_cfg = groups.get("cm20_core", {})
    if cc_cfg and not gp.empty:
        m = gp[gp["ts_code"].str[:3].isin([str(p) for p in cc_cfg["prefix"]])]
        m = m[(m["近20日涨幅%"] >= float(cc_cfg.get("min_gain20_pct", 20)))
              & (m["近5日均成交额(亿)"] >= float(cc_cfg.get("min_amount5_yi", 5.0)))]
        cm = m.nlargest(int(cc_cfg.get("n", 20)), "近20日涨幅%").merge(
            pool[["ts_code", "name", "pct_chg", "amount"]], on="ts_code", how="left")
        save(cm, "cm20_core", "20cm题材中军候选（F007）")
    # 涨停 / 跌停：口径由 selectors.yaml limit_source 决定（用户拍板 2026-10-27）
    limit_cfg = selectors.get("limit_source", {})
    primary = limit_cfg.get("primary", "official")
    kpl_enrich = limit_cfg.get("kpl_enrich", True)

    def kpl_join(df: pd.DataFrame) -> pd.DataFrame:
        """kpl 增强：题材/封单/最大封单/连板状态（仅增强已有代码，不补 ST，用户拍板）。"""
        if not kpl_enrich:
            return df
        kpl = con.execute(
            "SELECT ts_code, theme, status, limit_order, lu_limit_order FROM fact_kpl_list "
            "WHERE trade_date = ? AND tag = '涨停'", [date_dash]).fetchdf()
        if kpl.empty:
            return df
        return df.merge(kpl, on="ts_code", how="left")

    if primary == "official":
        # 官方 limit_list_d 口径（非 ST）：字段现成——开板次数/几天几板/封单/封板时间/市值/换手
        lu = con.execute(
            "SELECT * FROM fact_limit_detail WHERE trade_date = ? AND limit_type = 'U'", [date_dash]).fetchdf()
        if not lu.empty:
            lu["amount"] = lu["amount"] / 1000  # 官方 amount 单位是元，统一为千元（daily 口径）
            lu["structure"] = lu.apply(
                lambda r: "未开板(一字/T字)" if r["open_times"] == 0 else f"开板{int(r['open_times'])}次回封", axis=1)
            lu = lu.sort_values("fd_amount", ascending=False)
        save(kpl_join(lu), "limit_up", "官方口径+kpl增强" if kpl_enrich else "官方口径(非ST)")
        # 涨停聚焦表（jv1.2 用户拍板 2026-10-28）：涨停太多时按规则聚焦，替代全量平铺
        focus_cfg = selectors.get("groups", {}).get("limit_up_focus", {})
        if not lu.empty and focus_cfg:
            picks: list[tuple[str, str]] = []
            used: set[str] = set()

            def pick(df_: pd.DataFrame, tag: str, n: int) -> None:
                for code in df_.head(n)["ts_code"]:
                    if code not in used:
                        used.add(code)
                        picks.append((code, tag))

            fd_n = int(focus_cfg.get("fd_top_n", 10))
            pick(lu.nlargest(fd_n, "fd_amount"), "封单最大", fd_n)
            max_lt = int(lu["limit_times"].max())
            pick(lu[lu["limit_times"] == max_lt], f"最高板{max_lt}", 10)
            cm = lu[lu["ts_code"].str[:3].isin(["300", "301", "688", "689"])]
            pick(cm[cm["limit_times"] >= 2].sort_values("limit_times", ascending=False), "20cm连板", 20)
            leaders = (lu.sort_values(["limit_times", "fd_amount"], ascending=[False, False])
                       .groupby("industry").head(1))
            pick(leaders, "行业最高板(题材代理)", int(focus_cfg.get("industry_leader_n", 15)))
            fb = lu[lu["limit_times"] == 1]
            each_n = int(focus_cfg.get("first_board_each_n", 2))
            for col, tag in (("fd_amount", "首板封单王"), ("amount", "首板成交王"), ("float_mv", "首板市值王")):
                pick(fb.nlargest(each_n, col), tag, each_n)
            pick(cm.sort_values(["limit_times", "fd_amount"], ascending=[False, False]), "20cm", 20)
            focus = lu.merge(pd.DataFrame(picks, columns=["ts_code", "关注类别"]), on="ts_code")
            focus = focus.set_index("ts_code").loc[[p[0] for p in picks]].reset_index()
            save(focus, "limit_up_focus", "涨停聚焦：封单最大/最高板/20cm连板/行业最高板(题材代理)/首板三王/20cm")
        ld = con.execute(
            "SELECT * FROM fact_limit_detail WHERE trade_date = ? AND limit_type = 'D'", [date_dash]).fetchdf()
        if not ld.empty:
            ld["amount"] = ld["amount"] / 1000  # 同上，元→千元
        save(ld.sort_values("amount", ascending=False), "limit_down", "官方口径(非ST)")
    else:
        # local：本地 stk_limit 比对判定（含 ST/北交所，字段粗，保留作 fallback）
        flags = con.execute("SELECT ts_code, flag FROM fact_limit_flag WHERE trade_date = ?", [date_dash]).fetchdf()
        pool = pool.merge(flags, on="ts_code", how="left")
        lu = pool[pool["flag"].str.startswith("涨停", na=False)].sort_values(["flag", "amount"], ascending=[True, False])
        save(lu, "limit_up", "本地判定，含ST/北交所")
        ld = pool[pool["flag"] == "跌停"].sort_values("amount", ascending=False)
        save(ld, "limit_down", "本地判定，含ST/北交所")

    # 昨日涨停/跌停今日反馈（与涨停口径同源）
    prev = con.execute("SELECT trade_date FROM fact_daily_bar WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1", [date_dash]).fetchone()
    if prev:
        if primary == "official":
            prev_lu = set(r[0] for r in con.execute(
                "SELECT ts_code FROM fact_limit_detail WHERE trade_date = ? AND limit_type = 'U'", [prev[0]]).fetchall())
            prev_ld = set(r[0] for r in con.execute(
                "SELECT ts_code FROM fact_limit_detail WHERE trade_date = ? AND limit_type = 'D'", [prev[0]]).fetchall())
        else:
            prev_flags = con.execute(
                "SELECT ts_code, flag FROM fact_limit_flag WHERE trade_date = ?", [prev[0]]).fetchdf()
            prev_lu = set(prev_flags[prev_flags["flag"].str.startswith("涨停", na=False)]["ts_code"])
            prev_ld = set(prev_flags[prev_flags["flag"] == "跌停"]["ts_code"])
        fb = pool[pool["ts_code"].isin(prev_lu)][["ts_code", "name", "open", "pre_close", "pct_chg", "close", "amount"]]
        fb["开盘溢价%"] = ((fb["open"] / fb["pre_close"] - 1) * 100).round(2)
        save(fb.sort_values("pct_chg", ascending=False), "prev_lu_feedback", f"昨涨停今日反馈（{prev[0]} 集合）")
        fb2 = pool[pool["ts_code"].isin(prev_ld)][["ts_code", "name", "pct_chg", "amount"]]
        save(fb2.sort_values("pct_chg"), "prev_ld_feedback", "昨跌停今日反馈")
    # 指数面板：复制 curated 产物
    panel = ROOT / "data" / "curated" / date_dash / "index_panel.csv"
    if panel.exists():
        save(pd.read_csv(panel), "index_panel", "第一层指数量能")
    # 关注列表（无则建空模板，Judge 每日维护；路径只来自 Data Recipe）
    wl_path = ROOT / selectors["groups"]["watchlist"]["source"]
    if not wl_path.exists():
        wl_path.parent.mkdir(parents=True, exist_ok=True)
        wl_path.write_text(
            "# 每日关注列表（30-50只）：由 Judge 更新，Scout 只读\n"
            "# 字段：ts_code, name, theme, role(空间板/中军/20cm弹性/前排/后排), reason, added_date, status(关注/移出留痕)\n"
            "version: w0.0\nstocks: []\n",
            encoding="utf-8")
    wl = load_yaml(wl_path)
    wl_codes = [s["ts_code"] for s in wl.get("stocks", []) if s.get("status", "关注") == "关注"]
    if wl_codes:
        save(pool[pool["ts_code"].isin(wl_codes)], "watchlist_today", "关注列表今日走势")
    # 板块榜（含规模阈值过滤）
    boards = con.execute("SELECT * FROM fact_board_rank WHERE trade_date = ?", [date_dash]).fetchdf()
    min_cons = selectors.get("concept_min_cons", 15)
    top_n = selectors.get("boards_selection_n", 10)
    ths = ROOT / "data" / "raw" / "ths_index" / "ths_index_concept_N.json"
    if not boards.empty and ths.exists():
        import json
        payload = json.loads(ths.read_text(encoding="utf-8"))
        tdf = pd.DataFrame(payload["items"], columns=payload["fields"])
        size_map = dict(zip(tdf["ts_code"], tdf["count"]))
        boards["cons_count"] = boards["board_code"].map(size_map)
        kept = boards[boards["cons_count"] >= min_cons].nsmallest(top_n, "rank")
        dropped = len(boards) - len(kept)
        # E003：板块三分法标注（theme_class.yaml 是事实源；未收录的板块标"未分类"）
        tc_path = ROOT / "data" / "assets" / "theme_class.yaml"
        tc = load_yaml(tc_path) if tc_path.exists() else {}
        cmap = {c["board"]: c for c in (tc or {}).get("classes", [])}
        kept["板块类型"] = kept["board_name"].map(lambda b: (cmap.get(b) or {}).get("type", "未分类"))
        kept["周期位置"] = kept["board_name"].map(lambda b: (cmap.get(b) or {}).get("lifecycle", "—"))
        save(kept, "boards", f"板块榜，规模>={min_cons} 过滤掉 {dropped} 个，含三分法分类")

    # ---- E004 延续：天花板两行（F013 客观层）+ 载体生命周期表（F012 记录层客观列）----
    lc_cfg = selectors.get("lifecycle", {})
    ce_cfg = selectors.get("ceiling", {})
    n_back = max(int(lc_cfg.get("lookback_days", 12)), int(ce_cfg.get("window_days", 20)))
    days_back = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM fact_daily_bar WHERE trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?", [date_dash, n_back + 1]).fetchall()]

    def _ph(seq: list) -> str:
        return ",".join("?" * len(seq))

    # A. 天花板：滚动窗口内最高连板（10cm/20cm 双轨），记录保持者与今日突破标记
    if ce_cfg and days_back:
        win = days_back[:int(ce_cfg.get("window_days", 20))]
        cm_min = float(ce_cfg.get("cm_board_min_pct", 15))
        det = con.execute(
            f"SELECT d.trade_date, d.ts_code, d.limit_times, b.pct_chg "
            f"FROM fact_limit_detail d JOIN fact_daily_bar b "
            f"  ON b.trade_date = d.trade_date AND b.ts_code = d.ts_code "
            f"WHERE d.limit_type = 'U' AND d.trade_date IN ({_ph(win)})", win).fetchdf()
        rows = []
        for track, sub in (("10cm", det[det["pct_chg"] < cm_min]),
                           ("20cm", det[det["pct_chg"] >= cm_min])):
            if sub.empty:
                rows.append({"track": track, "window_max": 0, "record_holder": "", "record_date": "",
                             "today_max": 0, "breaker": ""})
                continue
            peak = int(sub["limit_times"].max())
            rec = sub[sub["limit_times"] == peak].sort_values("trade_date").iloc[0]  # 最早达到日
            same_day = sub[(sub["limit_times"] == peak) & (sub["trade_date"] == rec["trade_date"])]
            holder = "、".join(dict.fromkeys(names.get(c, c) for c in same_day["ts_code"]))
            today_sub = sub[sub["trade_date"] == date_dash]
            today_max = int(today_sub["limit_times"].max()) if not today_sub.empty else 0
            prior_max = sub[sub["trade_date"] != date_dash]["limit_times"].max()
            prior = int(prior_max) if pd.notna(prior_max) else 0
            breaker = ""
            if today_max > prior and today_max >= 2:
                who = "、".join(names.get(c, c) for c in
                               today_sub[today_sub["limit_times"] == today_max]["ts_code"])
                breaker = f"★{who} {today_max} 板，首破近 {len(win)-1} 日纪录（{prior} 板）"
            rows.append({"track": track, "window_max": peak,
                         "record_holder": holder,
                         "record_date": str(rec["trade_date"])[:10],
                         "today_max": today_max, "breaker": breaker})
        save(pd.DataFrame(rows), "ceiling", "天花板双轨（F013）：滚动最高连板/记录保持者/今日突破")

    # B. 载体生命周期表：剧幕规则判定（客观列），模仿关系列留空由 Judge 维护
    if lc_cfg and days_back:
        lb = days_back[:int(lc_cfg.get("lookback_days", 12))]
        det = con.execute(
            f"SELECT trade_date, ts_code, limit_times, fd_amount, open_times, up_stat "
            f"FROM fact_limit_detail WHERE limit_type='U' AND trade_date IN ({_ph(lb)})", lb).fetchdf()
        today_det = det[det["trade_date"] == date_dash] if not det.empty else pd.DataFrame()
        cand: set[str] = set(wl_codes)
        if not today_det.empty:
            cand |= set(today_det[today_det["limit_times"] >= 2]["ts_code"])
        recent5 = det[det["trade_date"].isin(lb[:5])] if not det.empty else pd.DataFrame()
        if not recent5.empty:
            cand |= set(recent5[recent5["limit_times"] >= int(lc_cfg.get("break_min_boards", 3))]["ts_code"])
        cand = {c for c in cand if c} - {'000001.SH', '399001.SZ', '399006.SZ'}
        rows = []
        if cand and not det.empty:
            bars = con.execute(
                f"SELECT trade_date, ts_code, open, high, low, close, pct_chg, amount "
                f"FROM fact_daily_bar WHERE trade_date IN ({_ph(lb)}) AND ts_code IN ({_ph(sorted(cand))})",
                lb + sorted(cand)).fetchdf()
            lt_map = {(r.trade_date, r.ts_code): r for r in det.itertuples()}
            band = float(lc_cfg.get("platform_band_pct", 8))
            shrink = float(lc_cfg.get("platform_shrink", 0.6))
            w2g = float(lc_cfg.get("wave2_min_gain", 8))
            exit_dd = float(lc_cfg.get("drawdown_exit_pct", 12))
            stag = int(lc_cfg.get("stagnate_days", 3))
            dv = float(lc_cfg.get("diverge_min_pct", -2.5))
            so_fd = float(lc_cfg.get("soft_open_fd_ratio", 0.5))
            so_amt = float(lc_cfg.get("soft_open_amt_ratio", 2.0))
            for code in sorted(cand):
                b = bars[bars["ts_code"] == code].sort_values("trade_date").reset_index(drop=True)
                if b.empty:
                    continue
                b["lt"] = [lt_map.get((d, code), None).limit_times if lt_map.get((d, code)) else 0
                           for d in b["trade_date"]]
                t = b.iloc[-1]
                lt_today, max_lt = int(t["lt"]), int(b["lt"].max())
                # 断板日：最后一次"连板≥break_min 且次日无涨停"
                brk = None
                for i in range(len(b) - 1):
                    if b["lt"][i] >= int(lc_cfg.get("break_min_boards", 3)) and b["lt"][i + 1] == 0:
                        brk = b.iloc[i]
                # 二波启动日：断板日后首个涨幅 ≥wave2_min 的阳线
                ws = None
                if brk is not None:
                    hit = b[(b["trade_date"] > brk["trade_date"]) & (b["pct_chg"] >= w2g)]
                    if not hit.empty:
                        ws = hit.iloc[0]
                high_close = float(b["close"].max())
                high_dt = str(b[b["close"] == high_close]["trade_date"].iloc[-1])
                n_since_high = len(b) - 1 - b.index[b["close"] == high_close].max()
                limit_days = b[b["lt"] >= 1]
                last_limit_dt = str(limit_days["trade_date"].iloc[-1]) if not limit_days.empty else ""
                # 事件列
                ev = []
                if len(b) >= 2 and b["lt"][len(b) - 2] >= 1 and lt_today == 0:
                    ev.append("断板第一天")
                rec = lt_map.get((t["trade_date"], code))
                # 参照日：连板中的票取上一个涨停日（封单/量能环比）；已断板的取断板日
                prev_fd, prev_amt = None, None
                if lt_today >= 1 and len(limit_days) >= 2:
                    pl = limit_days.iloc[-2]
                    prev_rec = lt_map.get((pl["trade_date"], code))
                    prev_fd = float(prev_rec.fd_amount) if prev_rec is not None and prev_rec.fd_amount else None
                    prev_amt = float(pl["amount"])
                elif brk is not None:
                    prev_rec = lt_map.get((brk["trade_date"], code))
                    prev_fd = float(prev_rec.fd_amount) if prev_rec is not None and prev_rec.fd_amount else None
                    prev_amt = float(brk["amount"])
                if rec is not None:
                    if int(rec.open_times) >= 1:
                        ev.append(f"开板{int(rec.open_times)}次回封")
                    amt_today = float(t["amount"])
                    if prev_fd and float(rec.fd_amount or 0) / prev_fd < so_fd \
                            and prev_amt and amt_today / prev_amt > so_amt:
                        ev.append("软开板?")
                    if len(b) >= 2 and prev_amt and amt_today / prev_amt > 3:
                        ev.append(f"巨量×{amt_today / prev_amt:.1f}")
                # 剧幕判定（顺序即优先级；lc0.2：单波边界=5日线，用户拍板 2026-10-28）
                ma_n = int(lc_cfg.get("wave_ma", 5))
                b["ma"] = b["close"].rolling(ma_n).mean()
                t = b.iloc[-1]
                above = bool(float(t["close"]) >= float(t["ma"])) if pd.notna(t["ma"]) else True
                # 本波启动日（窗口内首个涨停日）；破线只认"启动之后"的收盘破均线
                prior_break = False
                reclaim_dt = ""
                if not limit_days.empty:
                    w0 = str(limit_days.iloc[0]["trade_date"])
                    mid = b[(b["trade_date"] > w0)].iloc[:-1]
                    if not mid.empty:
                        brk_mask = mid["close"] < mid["ma"]
                        prior_break = bool(brk_mask.any())
                        if prior_break:
                            reclaim_dt = str(mid["trade_date"].iloc[-1])  # 破线前最后一日=重新站上起点
                n_since_brk = (int((pd.Timestamp(t["trade_date"]) - pd.Timestamp(brk["trade_date"])).days)
                               if brk is not None else 0)
                if max_lt >= int(lc_cfg.get("board_min", 2)) and lt_today == 0 and not above:
                    stage, stay = "退潮·破5日线", "1天"   # 主条件：收盘破线=波结束（不分断板与否）
                elif max_lt >= int(lc_cfg.get("board_min", 2)) and lt_today == 0 and brk is not None \
                        and (float(t["close"]) < float(brk["low"])
                             or (high_close - float(t["close"])) / high_close * 100 > exit_dd):
                    stage, stay = "退潮", f"断板{n_since_brk}天"
                elif prior_break and above:
                    stage = "二波·新高" if float(t["close"]) >= high_close else "二波·重启"
                    stay = f"{(pd.Timestamp(t['trade_date']) - pd.Timestamp(reclaim_dt)).days}天"
                elif lt_today >= 2 and above:
                    stage, stay = "一波加速", f"{lt_today}连板"
                elif brk is not None and above and ws is not None:
                    stage, stay = "单波·二波尝试", f"断板{n_since_brk}天"   # 未破5日线=仍是同一波
                elif brk is not None and lt_today == 0 and last_limit_dt >= str(lb[min(4, len(lb) - 1)]) \
                        and abs(float(t["close"]) / float(brk["close"]) - 1) * 100 <= band \
                        and float(t["amount"]) < float(brk["amount"]) * shrink:
                    n = int((pd.Timestamp(t["trade_date"]) - pd.Timestamp(brk["trade_date"])).days)
                    stage, stay = "高位平台", f"{n}天"
                elif max_lt >= int(lc_cfg.get("board_min", 2)) and lt_today == 0 and above \
                        and n_since_high >= stag and (float(t["close"]) / high_close - 1) * 100 < 3:
                    stage, stay = "滞涨", f"高点后{n_since_high}天"
                elif lt_today == 0 and float(t["pct_chg"]) <= dv \
                        and (last_limit_dt >= str(lb[min(4, len(lb) - 1)])
                             or (float(t["close"]) / float(b["close"].iloc[0]) - 1) * 100 >= 20):
                    stage = "主升分歧"
                    stay = (f"{(pd.Timestamp(t['trade_date']) - pd.Timestamp(last_limit_dt)).days}天"
                            if last_limit_dt else "")
                else:
                    stage, stay = ("观察" if above else "观察·线下"), ""
                rows.append({
                    "ts_code": code, "name": names.get(code, ""),
                    "载体类型": "20cm" if code[:3] in ("300", "301", "302", "688", "689") else "10cm",
                    "up_stat": rec.up_stat if rec is not None else "",
                    "连板数": lt_today, "剧幕": stage, "停留": stay,
                    "五日线": "线上" if above else "破线",
                    "今日事件": " / ".join(ev), "涨跌幅": round(float(t["pct_chg"]), 2),
                    "成交额亿": round(float(t["amount"]) / 1e5, 2),
                    "封单亿": round(float(rec.fd_amount) / 1e8, 2) if rec is not None and rec.fd_amount else 0,
                    "模仿自": "", "模板剩余": "",
                })
        save(pd.DataFrame(rows), "carrier_lifecycle",
             f"载体生命周期表（F012 记录层，规则 {lc_cfg.get('version', '?')}）；模仿关系列由 Judge 维护")
    print(f"\n-> {out_dir}")


if __name__ == "__main__":
    main()
