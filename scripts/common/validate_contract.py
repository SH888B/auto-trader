# -*- coding: utf-8 -*-
"""Validate a JSON or YAML artifact against a versioned JSON Schema contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="JSON Schema 契约校验")
    parser.add_argument("instance")
    parser.add_argument("schema")
    args = parser.parse_args()
    instance_path = (ROOT / args.instance).resolve()
    schema_path = (ROOT / args.schema).resolve()
    text = instance_path.read_text(encoding="utf-8")
    if instance_path.suffix.lower() in {".yaml", ".yml"}:
        instance = yaml.safe_load(text)
    else:
        instance = json.loads(text)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(Draft7Validator(schema).iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        print(f"[校验失败] {instance_path}")
        for error in errors:
            location = ".".join(str(p) for p in error.path) or "<root>"
            print(f"  - {location}: {error.message}")
        raise SystemExit(1)
    print(f"[OK] {instance_path} 符合 {schema.get('title', schema_path.name)}")


if __name__ == "__main__":
    main()
