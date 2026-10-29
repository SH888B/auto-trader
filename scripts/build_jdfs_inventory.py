"""Build a compact inventory and keyword digest for the JDFS meeting corpus."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "reference" / "jdfs_weekly"
OUT = CORPUS / "_inventory.md"
JSON_OUT = CORPUS / "_inventory.json"

THEMES = [
    "科技", "硬件", "光模块", "CPO", "LPO", "PCB", "CCL", "覆铜板", "铜箔", "电子布",
    "算力", "国产算力", "芯片", "半导体", "存储", "设备", "封测", "端侧",
    "AI", "物理AI", "具身", "机器人", "数据标注", "卫星", "商业航天", "低空", "飞车",
    "医药", "有色", "黄金", "铜", "铝", "稀土", "电力", "新能源", "固态电池",
    "锂电", "光伏", "储能", "消费电子", "家电", "军工", "农业", "消费", "食品", "红利",
]
PATTERNS = [
    ("priority", re.compile(r"重点|最重要|第一|核心|主线|切换日|载体|节奏|首分|分歧|修复|抱团|反抽|补跌")),
    ("forward", re.compile(r"明天|下周|接下来|后续|预期|可以(?:看|做)|关注")),
    ("process", re.compile(r"复盘|先看|然后|接下来|首先|最后|总结|判断")),
]
STOCK_NAME = re.compile(r"[\u4e00-\u9fa5]{2,7}(?:股份|科技|信息|电子|通信|精密|光电|新材|电气|智能|数据|国际|集团|能源|铜业|黄金|精密|网科|软件|材料|生物|化工)")

for label, pattern in PATTERNS:
    pass


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def top_matches(pattern: re.Pattern[str], text: str, limit: int) -> list[str]:
    lines = []
    for line in text.splitlines():
        if pattern.search(line):
            cleaned = re.sub(r"^\S+\s+\d{2}:\d{2}\s*", "", line).strip()
            if cleaned and cleaned not in lines:
                lines.append(cleaned)
        if len(lines) >= limit:
            break
    return lines


def main() -> None:
    files = sorted(p for p in CORPUS.glob("*.md") if not p.name.startswith("_"))
    hashes = {p: sha(p) for p in files}
    unique: list[Path] = []
    duplicate_groups: dict[str, list[str]] = {}
    for path in files:
        key = hashes[path]
        duplicate_groups.setdefault(key, []).append(path.name)
        if not any(hashes[p] == key for p in unique):
            unique.append(path)

    records = []
    for path in unique:
        text = path.read_text(encoding="utf-8")
        name_freq = Counter(STOCK_NAME.findall(text))
        theme_freq = Counter(theme for theme in THEMES if theme.lower() in text.lower())
        timestamps = re.findall(r"\b(\d{2}:\d{2})\b", text)
        rec = {
            "file": path.name,
            "chars": len(text),
            "first_time": timestamps[0] if timestamps else None,
            "last_time": timestamps[-1] if timestamps else None,
            "theme_counts": theme_freq.most_common(),
            "name_counts": name_freq.most_common(25),
            "priority_excerpts": top_matches(PATTERNS[0][1], text, 12),
            "forward_excerpts": top_matches(PATTERNS[1][1], text, 8),
            "process_excerpts": top_matches(PATTERNS[2][1], text, 8),
        }
        records.append(rec)

    duplicate_files = sorted({name for group in duplicate_groups.values() if len(group) > 1 for name in group})
    manifest = {
        "copied_files": len(files),
        "unique_documents": len(unique),
        "duplicate_file_names": duplicate_files,
        "unique_hashes": {p.name: hashes[p] for p in unique},
    }

    lines = [
        "# JDFS 会议文稿目录与关键词索引",
        "",
        f"- 复制文件数：{len(files)}",
        f"- 去重后文稿：{len(unique)}",
        "- 重复文件（同一内容多个日期名）不参与重复计权",
        "",
        "## 重复文件",
        "",
    ]
    for group in duplicate_groups.values():
        if len(group) > 1:
            lines.append("- " + " = ".join(sorted(group)))
    lines.extend(["", "## 每周摘要", ""])
    for rec in records:
        lines.extend([
            f"### {rec['file']}",
            "",
            f"- 长度：{rec['chars']} 字符；时间范围：{rec['first_time']}—{rec['last_time']}",
            "- 题材词：" + ("、".join(f"{k}×{v}" for k, v in rec["theme_counts"]) or "无"),
            "- 高频名称：" + ("、".join(f"{k}×{v}" for k, v in rec["name_counts"][:15]) or "无"),
            "- 关键句：",
        ])
        lines.extend(f"  - {x[:240]}" for x in rec["priority_excerpts"])
        lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    JSON_OUT.write_text(json.dumps({"manifest": manifest, "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
