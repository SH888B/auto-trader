# -*- coding: utf-8 -*-
"""从 _inventory.json 抽取轻量时间线 _timeline.md。
名称列为正则粗抽，含转写噪声，仅作定位线索，使用前必须 Tushare 校验。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INV = ROOT / "reference" / "jdfs_weekly" / "_inventory.json"
OUT = ROOT / "reference" / "jdfs_weekly" / "_timeline.md"

d = json.load(open(INV, encoding="utf-8"))
records = d["records"]
# 已知重复
DUP = {"0525.md": "0517.md"}

lines = ["# JDFS 每周会议时间线（机抽，名称待核验）", "",
         "> 名称列由正则从语音转写稿中粗抽，噪声大（如“红和科技”疑为“鸿合科技”），",
         "> 仅用于定位深读目标，任何引用前必须用 Tushare stock_basic 核验。", ""]

for r in records:
    f = r["file"]
    dup = f"　⚠️与 {DUP[f]} 完全重复" if f in DUP else ""
    lines.append(f"## {f[:-3]}{dup}")
    lines.append(f"- 篇幅 {r['chars']} 字符，时长 {r['first_time']}–{r['last_time']}")
    themes = "、".join(t for t, _ in r["theme_counts"][:10])
    lines.append(f"- 题材词：{themes}")
    names = "、".join(n for n, _ in r["name_counts"][:8])
    lines.append(f"- 高频名（待核验）：{names}")
    for ex in r.get("priority_excerpts", [])[:2]:
        ex = ex.strip().replace("\n", " ")
        if len(ex) > 180:
            ex = ex[:180] + "…"
        lines.append(f"- 摘录：{ex}")
    lines.append("")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"written {OUT}, {len(records)} records")
