#!/usr/bin/env python3
"""Derive a business-form schema (what the page needs) from an exported
type-tagged dataset document."""
from __future__ import annotations

import argparse
import json
import os


def build_schema(dataset: dict, *, title: str | None = None, login: dict | None = None) -> dict:
    meta = dataset["dataset"]
    business_key = list(meta.get("business_key") or (meta.get("key_fields") or []) + (
        [meta["index_field"]] if meta.get("index_field") else []))
    fields = []
    for f in meta["fields"]:
        item = {"name": f["name"], "type": f["type"]}
        if f["name"] in business_key:
            item["required"] = True
        fields.append(item)
    return {
        "title": title or f"{meta.get('name', 'Business')} records",
        "dataset": meta.get("name"),
        "key_fields": business_key,
        "index_field": meta.get("index_field"),
        "fields": fields,
        "login": login or {"username": "operator", "password": "operator"},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-json", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title")
    ap.add_argument("--username", default="operator")
    ap.add_argument("--password", default="operator")
    args = ap.parse_args()
    with open(args.dataset_json, encoding="utf-8") as fh:
        dataset = json.load(fh)
    schema = build_schema(dataset, title=args.title,
                          login={"username": args.username, "password": args.password})
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(schema, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {args.out}: {len(schema['fields'])} fields, key={schema['key_fields']}")


if __name__ == "__main__":
    main()
