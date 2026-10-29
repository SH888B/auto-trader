# -*- coding: utf-8 -*-
"""Judge review 语义校验：schema + v1.1/v1.2 结构化要求 + 主题引用完整性。

schema 校验走 contracts/review.schema.json（v1 历史产物天然通过，新增字段可选）；
本脚本额外检查 schema 表达不了的三类问题：
  1. review_version=v1.1/v1.2 时，themes/carriers 是否使用受控字段（rhythm_stage/carrier_class）；
  2. 引用完整性：carriers[].theme_id、switch_day.candidates、themes[].competitors、
     next_day_first_divergence_boards[].theme_id 必须存在于 themes[].theme_id；
  3. 首分严格定义的留痕：rhythm_stage=首分 必须有 stage_basis（schema 已强制，此处再查内容非平凡）；
     v1.2 起 next_day_first_divergence_boards[].basis 同样查非平凡。

用法：python scripts/judge/validate_review.py <review.json>
退出码：0=通过（可有警告）；1=失败。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts" / "review.schema.json"


def fail(errors: list[str], warnings: list[str], review_path: Path) -> int:
    for w in warnings:
        print(f"[警告] {w}")
    if errors:
        print(f"[校验失败] {review_path}")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"[OK] {review_path} 符合 review 语义校验（schema={SCHEMA_PATH.name}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge review 语义校验")
    parser.add_argument("review", help="review.json 路径（相对 workspace 根或绝对路径）")
    args = parser.parse_args()

    review_path = Path(args.review)
    if not review_path.is_absolute():
        review_path = (ROOT / review_path).resolve()
    review = json.loads(review_path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    errors: list[str] = []
    warnings: list[str] = []

    schema_errors = sorted(Draft7Validator(schema).iter_errors(review), key=lambda e: list(e.path))
    for e in schema_errors:
        location = ".".join(str(p) for p in e.path) or "<root>"
        errors.append(f"{location}: {e.message}")

    version = review.get("review_version", "v1")
    themes = review.get("themes", []) or []
    carriers = review.get("carriers", []) or []
    theme_ids = {t.get("theme_id") for t in themes}
    regime = review.get("market_regime", {}) or {}

    def check_refs(refs: list, owner: str, field: str) -> None:
        for r in refs or []:
            if r not in theme_ids:
                msg = f"{owner}.{field}: 引用了不存在的 theme_id '{r}'"
                (errors if version in ("v1.1", "v1.2") else warnings).append(msg)

    structured = version in ("v1.1", "v1.2")
    if structured:
        if regime.get("label"):
            warnings.append("market_regime.label 为 v1.1 弃用字段，不应再写自由文本 label")
        for i, t in enumerate(themes):
            tid = t.get("theme_id", f"#{i}")
            if t.get("stage"):
                warnings.append(f"themes[{tid}].stage 为 v1.1 弃用字段，改用 rhythm_stage")
            if not t.get("rhythm_stage"):
                errors.append(f"themes[{tid}]: v1.1 产物缺少 rhythm_stage")
            if t.get("rhythm_stage") == "首分":
                basis = (t.get("stage_basis") or "").strip()
                if len(basis) < 10:
                    errors.append(f"themes[{tid}]: 首分缺少有效的 stage_basis（三天组合K依据）")
                elif not re.search(r"阳线|大阳|涨", basis):
                    warnings.append(f"themes[{tid}]: stage_basis 未提及阳线组合，请核对首分严格定义")
        for i, c in enumerate(carriers):
            cid = c.get("ts_code", f"#{i}")
            if c.get("role"):
                warnings.append(f"carriers[{cid}].role 为 v1.1 弃用字段，改用 carrier_class")
            if not c.get("carrier_class"):
                errors.append(f"carriers[{cid}]: v1.1 产物缺少 carrier_class")
            if not c.get("capacity"):
                warnings.append(f"carriers[{cid}]: 未标注 capacity（成交额量级）")

    if version == "v1.2":
        fd_boards = review.get("next_day_first_divergence_boards") or []
        for i, fb in enumerate(fd_boards):
            code = fb.get("board_code", f"#{i}")
            check_refs([fb.get("theme_id")], f"next_day_first_divergence_boards[{code}]", "theme_id")
            basis = (fb.get("basis") or "").strip()
            if len(basis) < 10:
                errors.append(f"next_day_first_divergence_boards[{code}]: 预判依据 basis 过短（<10 字），须写明 stage/位置/量能/分歧迹象")

    for i, c in enumerate(carriers):
        cid = c.get("ts_code", f"#{i}")
        check_refs([c.get("theme_id")], f"carriers[{cid}]", "theme_id")
    for i, t in enumerate(themes):
        tid = t.get("theme_id", f"#{i}")
        check_refs(t.get("competitors"), f"themes[{tid}]", "competitors")
    check_refs(regime.get("switch_day", {}).get("candidates"), "market_regime.switch_day", "candidates")

    return fail(errors, warnings, review_path)


if __name__ == "__main__":
    sys.exit(main())
