# -*- coding: utf-8 -*-
"""核验 JDFS 深读笔记中的转写股票名：在 stock_basic 中精确+模糊匹配。"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
raw = json.load(open(ROOT / "data/probe/stock_basic.json", encoding="utf-8"))
text = raw[0]["result"]["content"][0]["text"]
rows = json.loads(text)

# 候选名：来自 _deep_notes.md 的待核验清单
candidates = [
    "中际旭创", "新易盛", "天孚通信", "东山精密", "太辰光", "光库科技",
    "多氟多", "中欣氟材", "利通电子", "宏和科技", "铜冠铜箔", "风华高科",
    "江海股份", "京东方A", "天娱数科", "索辰科技", "中船特气", "紫金矿业",
    "洛阳钼业", "沃格光电", "亨通光电", "长飞光纤", "金安国纪", "旭光电子",
    "华工科技", "兆易创新", "普冉股份", "德明利", "海南海药", "万邦医药",
    "舒泰神", "海辰药业", "药明康德", "华天科技", "华虹公司", "美丽云",
    "紫光股份", "华银电力", "立新能源", "长电科技", "澜起科技", "江波龙",
    "佰维存储", "香农芯创", "聚辰股份", "景旺电子", "鹏鼎控股", "寒武纪",
    "北方华创", "中微公司", "拓荆科技", "康龙化成", "凯莱英", "哈药股份",
    "金健米业", "万向德农", "秋乐种业", "神农种业", "天山生物", "晓程科技",
    "深中华", "国芳集团", "闽东电力", "楚天龙", "龙版传媒", "锦龙股份",
    "华瓷股份", "奥弘电子", "澳弘电子", "超声电子", "迅捷兴", "威尔高",
    "崇达技术", "锦波生物", "冠昊生物", "南华生物", "神奇制药", "百花医药",
    "百普赛斯", "时代万恒", "尚水智能", "善水科技", "九阳股份", "华斯股份",
    "上海洗霸", "传艺科技", "冰山冷热", "雪人股份", "万丰奥威", "吉鑫科技",
    "五洲新春", "大业股份", "凡拓数创", "华阳智能", "领湃科技", "海天瑞声",
    "新华传媒", "新华文轩", "慈文传媒", "芒果超媒", "绿盟科技", "中文在线",
    "国脉文化", "恒尚节能", "海兰信", "爱丽家居", "传智教育", "益民食品",
    "高争民爆", "五洲医疗", "长缆科技", "中天科技", "恒为科技", "润建股份",
    "鸿博股份", "真视通", "光迅科技", "宝馨科技", "卧龙电驱", "精艺股份",
    "金安国际",
]

by_name = {}
for r in rows:
    by_name.setdefault(r["name"].replace(" ", ""), []).append(r)

def find(q):
    q = q.replace(" ", "")
    hits = []
    for name, rs in by_name.items():
        if q in name or name in q:
            hits.extend(rs)
    if not hits:  # 双字尾缀模糊：取前两个字匹配
        for name, rs in by_name.items():
            if len(q) >= 2 and q[:2] in name:
                hits.extend(rs)
    return hits

lines = ["| 候选名 | 匹配结果 |", "|---|---|"]
for c in candidates:
    hits = find(c)
    if hits:
        uniq = sorted({f'{h["ts_code"]} {h["name"]}({h["industry"]})' for h in hits})
        lines.append(f"| {c} | {'; '.join(uniq[:6])} |")
    else:
        lines.append(f"| {c} | ❌无匹配 |")

out = ROOT / "reference/jdfs_weekly/_name_resolution.md"
out.write_text("# 股票名称核验表（stock_basic 匹配）\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
print(f"written {out}, {len(rows)} stocks loaded")
no = [l for l in lines if "❌" in l]
print("未匹配数:", len(no))
