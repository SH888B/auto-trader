# -*- coding: utf-8 -*-
"""Resolve an experiment.yaml into immutable text assets and hash-locked data refs."""
from __future__ import annotations

import argparse
import hashlib
import shutil
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def ref(path: Path) -> dict:
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}


def scripts_hash() -> str:
    h = hashlib.sha256()
    for folder in ("scout", "curator", "common", "judge", "trader", "broker", "evaluation"):
        for path in sorted((ROOT / "scripts" / folder).glob("*.py")):
            h.update(path.relative_to(ROOT).as_posix().encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Curator：冻结实验 Context Bundle")
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    exp_dir = (ROOT / args.experiment).resolve()
    if not exp_dir.is_relative_to(ROOT):
        raise SystemExit("experiment 必须位于 workspace 内")
    experiment_path = exp_dir / "experiment.yaml"
    config = load_yaml(experiment_path)
    lock_path = exp_dir / "bundle" / "manifest.lock.yaml"
    if lock_path.exists():
        raise SystemExit(f"Bundle 已冻结，拒绝覆盖：{lock_path}")

    copied: dict[str, dict] = {}

    def freeze(rel: str, category: str = "configs") -> dict:
        if not rel:
            return {}
        src = (ROOT / rel).resolve()
        if not src.is_file() or not src.is_relative_to(ROOT):
            raise SystemExit(f"冻结资产不存在或越界：{rel}")
        key = src.relative_to(ROOT).as_posix()
        if key not in copied:
            dst = exp_dir / "bundle" / category / key
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied[key] = {**ref(src), "bundle_path": dst.relative_to(exp_dir).as_posix()}
        return copied[key]

    registry_path = ROOT / "factors" / "registry.yaml"
    registry = load_yaml(registry_path)
    factors = {f["id"]: f for f in registry.get("factors", [])}

    def freeze_profile(rel: str) -> dict:
        profile_ref = freeze(rel, "agents")
        profile = load_yaml(ROOT / rel)
        prompt_ref = freeze(profile["prompt"], "agents")
        output_ref = freeze(profile["output_contract"], "agents")
        input_ref = freeze(profile["input_view"], "agents")
        factor_refs = []
        for factor_key in profile.get("factors", []):
            factor_id, _, version = factor_key.partition("@")
            if factor_id not in factors:
                raise SystemExit(f"未注册因子：{factor_id}")
            factor = factors[factor_id]
            if factor.get("version") != version:
                raise SystemExit(f"因子版本不匹配：{factor_key} != {factor.get('version')}")
            factor_refs.append({"id": factor_id, "version": version, **freeze(factor["file"], "factors")})
        user_watchlist = profile.get("user_watchlist", "disabled")
        return {
            "profile": profile_ref, "prompt": prompt_ref, "input_view": input_ref,
            "output_contract": output_ref, "factors": factor_refs, "user_watchlist": user_watchlist,
            "queries": profile.get("queries", []), "data_requirements": profile.get("data_requirements", []),
        }

    lock = {
        "lock_version": "bundle3.0",
        "experiment_id": config.get("experiment_id"),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "experiment": ref(experiment_path),
        "data": freeze(config["data"]["recipe"]),
        "views": {k: freeze(v) for k, v in config.get("views", {}).items()},
        "judge": freeze_profile(config["judge"]["profile"]),
        "arms": [],
        "initial_state": {},
        "broker": freeze(config["broker"]["profile"]),
        "evaluation": freeze(config["evaluation"]["profile"]),
        "curator": freeze("agents/curator/profiles/default.yaml", "agents"),
        "query_templates": freeze("configs/queries/templates.yaml"),
        "factor_registry": ref(registry_path),
        "scripts_sha256": scripts_hash(),
        "runtime": config.get("runtime") or {},
        "snapshots": [],
    }
    for arm in config.get("arms", []):
        lock["arms"].append({"id": arm["id"], "trader": freeze_profile(arm["trader"]["profile"])})

    for key, rel in (config.get("initial_state") or {}).items():
        if not rel:
            lock["initial_state"][key] = None
            continue
        src = (ROOT / rel).resolve()
        dst = exp_dir / "state" / "initial" / f"{key}.yaml"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        lock["initial_state"][key] = {**ref(src), "frozen_path": dst.relative_to(exp_dir).as_posix(), "frozen_sha256": sha256(dst)}

    for day in config.get("snapshot_dates", []):
        snapshot = ROOT / "data" / "snapshots" / str(day) / "snapshot.yaml"
        if not snapshot.exists():
            raise SystemExit(f"snapshot 缺失：{snapshot}")
        lock["snapshots"].append(ref(snapshot))

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(yaml.safe_dump(lock, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"[bundle] {lock_path}")
    print(f"[assets] {len(copied)} frozen text files; snapshots={len(lock['snapshots'])}; arms={len(lock['arms'])}")


if __name__ == "__main__":
    main()
