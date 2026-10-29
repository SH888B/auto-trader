# -*- coding: utf-8 -*-
"""validate_review 语义校验测试：v1 兼容、v1.1 强制、引用完整性、反向验证。"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "judge" / "validate_review.py"

V1_REVIEW = {
    "as_of": "2026-10-22",
    "market_regime": {"label": "震荡", "evidence": ["e1"], "confidence": 0.6},
    "risks": [],
    "themes": [
        {"theme_id": "T-robots", "name": "机器人", "stage": "分歧", "evidence": ["e"],
         "invalid_if": "触发器", "confidence": 0.7}
    ],
    "carriers": [
        {"ts_code": "603278.SH", "theme_id": "T-robots", "role": "高危龙头",
         "evidence": ["e"], "confidence": 0.5}
    ],
    "data_gaps": [],
    "watchlist_updates": [],
}

V11_REVIEW = {
    "as_of": "2026-10-22",
    "review_version": "v1.1",
    "market_regime": {
        "trend_segment": "下降缩量", "position": "左脚",
        "index_space": "反弹压力位 2900（前平台低点）",
        "tolerance_level": "低", "tolerance_evidence": ["创业板跌25%且放量"],
        "switch_day": {"is_switch_day": False, "evidence": [], "candidates": []},
        "evidence": ["e1"], "confidence": 0.6,
    },
    "risks": [],
    "themes": [
        {"theme_id": "T-robots", "name": "机器人", "rhythm_stage": "首分",
         "stage_basis": "D1 大阳启动、D2 大阳延续、D3 盘中分歧为三天组合K中两根大阳线后的第一次分歧",
         "board_position": "主线", "strength_evidence": "强度票顶一字",
         "is_switch_window": True, "competitors": [],
         "evidence": ["e"], "invalid_if": "次日不能修复", "confidence": 0.7}
    ],
    "carriers": [
        {"ts_code": "603278.SH", "theme_id": "T-robots", "carrier_class": "强度票",
         "capacity": "中", "divergence_proxy": True, "evidence": ["e"], "confidence": 0.5}
    ],
    "data_gaps": [],
    "watchlist_updates": [],
}


def run_validator(review: dict) -> subprocess.CompletedProcess:
    tmp = ROOT / "temp" / "_test_review.json"
    tmp.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    try:
        return subprocess.run(
            [sys.executable, str(VALIDATOR), str(tmp)],
            capture_output=True, text=True, encoding="utf-8",
        )
    finally:
        tmp.unlink(missing_ok=True)


def test_v1_legacy_passes():
    r = run_validator(V1_REVIEW)
    assert r.returncode == 0, r.stdout + r.stderr


def test_v11_complete_passes():
    r = run_validator(V11_REVIEW)
    assert r.returncode == 0, r.stdout + r.stderr


def test_v11_requires_structured_regime():
    bad = copy.deepcopy(V11_REVIEW)
    del bad["market_regime"]["trend_segment"]
    r = run_validator(bad)
    assert r.returncode == 1


def test_v11_first_divergence_requires_basis():
    bad = copy.deepcopy(V11_REVIEW)
    del bad["themes"][0]["stage_basis"]
    r = run_validator(bad)
    assert r.returncode == 1
    assert "stage_basis" in r.stdout


def test_dangling_theme_id_rejected():
    bad = copy.deepcopy(V11_REVIEW)
    bad["carriers"][0]["theme_id"] = "T-ghost"
    r = run_validator(bad)
    assert r.returncode == 1
    assert "T-ghost" in r.stdout


def test_switch_day_candidate_must_exist():
    bad = copy.deepcopy(V11_REVIEW)
    bad["market_regime"]["switch_day"]["candidates"] = ["T-ghost"]
    r = run_validator(bad)
    assert r.returncode == 1


def test_v11_legacy_fields_warn_but_pass():
    bad = copy.deepcopy(V11_REVIEW)
    bad["market_regime"]["label"] = "震荡"
    bad["themes"][0]["stage"] = "分歧"
    bad["carriers"][0]["role"] = "高危龙头"
    r = run_validator(bad)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("[警告]") == 3


def test_enum_rejects_free_text():
    bad = copy.deepcopy(V11_REVIEW)
    bad["themes"][0]["rhythm_stage"] = "主升加速"
    r = run_validator(bad)
    assert r.returncode == 1


def test_real_historical_reviews_still_pass():
    """E006/live 历史 review（v1 自由文本）在新 schema 下必须通过 —— 保护 score_v3 回放链路。"""
    reviews = sorted(
        (ROOT / "experiments/E006_v3历史回放/runs").glob("*/judge/review.json")
    ) + sorted((ROOT / "runs/live").glob("*/judge/review.json"))
    assert reviews, "未找到历史 review"
    for path in reviews:
        r = subprocess.run(
            [sys.executable, str(VALIDATOR), str(path)],
            capture_output=True, text=True, encoding="utf-8",
        )
        assert r.returncode == 0, f"{path}:\n{r.stdout}{r.stderr}"
