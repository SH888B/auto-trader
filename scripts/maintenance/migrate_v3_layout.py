# -*- coding: utf-8 -*-
"""Move existing workspace assets into the approved v3 layout, without overwriting."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DIRS = [
    "agents/judge/prompts", "agents/judge/profiles",
    "agents/trader/prompts", "agents/trader/profiles",
    "agents/broker/runbooks", "agents/curator/profiles", "agents/presenter/profiles",
    "factors/review", "factors/strategy", "factors/shared",
    "configs/data", "configs/digest", "configs/queries", "configs/broker", "configs/evaluation",
    "state/watchlist/judge/history", "state/watchlist/user/history", "state/portfolio/history",
    "experiments", "runs/live", "logs",
    "scripts/curator", "scripts/trader", "scripts/broker", "scripts/evaluation",
    "reference/design", "reference/laosao/plans",
]

MOVES = [
    ("context/exp", "experiments"),
    ("context/templates", "agents/judge/prompts"),
    ("context/reference/老骚盘前计划", "reference/laosao/plans"),
    ("data/watchlist.yaml", "state/watchlist/judge/current.yaml"),
    ("data/watchlist-w1.0-20261027.yaml", "state/watchlist/judge/history/2026-10-27-w1.0.yaml"),
    ("data/positions/current.yaml", "state/portfolio/current.yaml"),
    ("selectors.yaml", "configs/data/selectors.yaml"),
    ("universe.yaml", "configs/data/universe.yaml"),
    ("scripts/scout/annotate.py", "scripts/curator/render_digest.py"),
    ("scripts/judge/scorer.py", "scripts/evaluation/scorer.py"),
    ("scripts/judge/laosao_scorer.py", "scripts/evaluation/laosao_scorer.py"),
    ("scripts/judge/validate_plan.py", "scripts/evaluation/legacy_validate_plan.py"),
    ("ARCHITECTURE-V3-PROPOSAL.md", "archive/ARCHITECTURE-V3-PROPOSAL.md"),
    ("PRD-E002-老骚对照-v1.md", "reference/design/PRD-E002-老骚对照-v1.md"),
    ("PRD-E003-复盘框架v2-v1.md", "reference/design/PRD-E003-复盘框架v2-v1.md"),
    ("PRD-Judge-实验机制-v1.md", "reference/design/PRD-Judge-实验机制-v1.md"),
    ("PRD-Scout-v1.md", "reference/design/PRD-Scout-v1.md"),
]

FACTORS = {
    "F001": "review", "F002": "strategy", "F003": "strategy", "F004": "strategy",
    "F005": "strategy", "F006": "review", "F007": "shared", "F008": "shared",
    "F009": "review", "F010": "strategy", "F011": "review", "F012": "review", "F013": "review",
}


def move(src: Path, dst: Path) -> None:
    if not src.exists():
        print(f"[skip] missing {src.relative_to(ROOT)}")
        return
    if dst.exists():
        raise SystemExit(f"Refusing to overwrite: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    print(f"[move] {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")


def main() -> None:
    for rel in DIRS:
        (ROOT / rel).mkdir(parents=True, exist_ok=True)

    # Experiment directories become children of experiments/, not experiments/exp/.
    for src, dst in MOVES:
        s, d = ROOT / src, ROOT / dst
        if src == "context/exp" and s.exists():
            for child in s.iterdir():
                move(child, d / child.name)
        elif src in {"context/templates", "context/reference/老骚盘前计划"} and s.exists():
            for child in s.iterdir():
                move(child, d / child.name)
        else:
            move(s, d)

    factor_root = ROOT / "context" / "factors"
    if factor_root.exists():
        for src in factor_root.iterdir():
            if src.name == "registry.yaml":
                move(src, ROOT / "factors" / "registry.yaml")
            elif src.is_file():
                target = FACTORS.get(src.name[:4], "shared")
                move(src, ROOT / "factors" / target / src.name)

    # Keep a dated copy of the live states when their metadata identifies the date.
    judge_current = ROOT / "state/watchlist/judge/current.yaml"
    if judge_current.exists():
        snapshot = judge_current.parent / "history" / "2026-10-27.yaml"
        if not snapshot.exists():
            shutil.copy2(judge_current, snapshot)
    portfolio_current = ROOT / "state/portfolio/current.yaml"
    if portfolio_current.exists():
        snapshot = portfolio_current.parent / "history" / "2026-10-27.yaml"
        if not snapshot.exists():
            shutil.copy2(portfolio_current, snapshot)

    # Remove only empty legacy directories.
    for rel in ["context/exp", "context/templates", "context/reference", "context/factors", "context",
                "data/positions", "scripts/judge"]:
        path = ROOT / rel
        if path.exists():
            try:
                path.rmdir()
            except OSError:
                pass

    (ROOT / "logs" / "run_events.jsonl").touch(exist_ok=True)
    print("[done] v3 layout migration")


if __name__ == "__main__":
    main()
