#!/usr/bin/env python3
"""Export removed objects with Chinese names and removal reasons."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import load_config
from utils.llm_client import get_llm_client

ASSESSMENT_FILE = PROJECT_ROOT / "data" / "captions" / "object_editability_assessment.json"
OUTPUT_FILE = PROJECT_ROOT / "data" / "captions" / "removed_objects_with_reasons.json"


def main():
    with open(ASSESSMENT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Collect all removed objects
    removed_names = []
    removed_by_cat: dict[str, list[dict]] = {}
    for cat, items in data.items():
        for name, info in items.items():
            if info.get("verdict") == "remove":
                removed_names.append(name)
                removed_by_cat.setdefault(cat, []).append(info)

    print(f"Total removed: {len(removed_names)}")

    # Batch translate via GPT-5
    config = load_config()
    client = get_llm_client(config.qh_mllm)

    translations = {}
    batch_size = 80
    for i in range(0, len(removed_names), batch_size):
        batch = removed_names[i : i + batch_size]
        prompt = (
            "Translate each English object name to Chinese (concise, common name). "
            "Return JSON object mapping English to Chinese. No markdown fences.\n\n"
            + json.dumps(batch, ensure_ascii=False)
        )
        resp = client.chat(
            system_prompt="You are a translator. Return only a JSON object.",
            user_prompt=prompt,
            max_tokens=10000,
        )
        text = resp.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        batch_trans = json.loads(text)
        translations.update(batch_trans)
        print(f"  Translated {i + len(batch)}/{len(removed_names)}", flush=True)

    # Build output
    output = {}
    for cat in sorted(removed_by_cat.keys()):
        items = removed_by_cat[cat]
        cat_list = []
        for info in sorted(items, key=lambda x: x["object"]):
            reasons = []
            if info.get("editability_score", 5) < 3:
                reasons.append("too_simple")
            if info.get("has_3d_unfriendly_attrs"):
                reasons.append("3d_unfriendly")
            cat_list.append({
                "object": info["object"],
                "chinese_name": translations.get(info["object"], ""),
                "editability_score": info.get("editability_score"),
                "edit_examples": info.get("edit_examples", []),
                "has_3d_unfriendly_attrs": info.get("has_3d_unfriendly_attrs", False),
                "unfriendly_reasons": info.get("unfriendly_reasons", []),
                "removal_reasons": reasons,
            })
        output[cat] = cat_list

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Stats
    reason_counts = {"too_simple_only": 0, "3d_unfriendly_only": 0, "both": 0}
    for items in output.values():
        for item in items:
            r = item["removal_reasons"]
            if "too_simple" in r and "3d_unfriendly" in r:
                reason_counts["both"] += 1
            elif "too_simple" in r:
                reason_counts["too_simple_only"] += 1
            elif "3d_unfriendly" in r:
                reason_counts["3d_unfriendly_only"] += 1

    print(f"\nRemoval reason breakdown:")
    print(f"  Too simple only (score<3): {reason_counts['too_simple_only']}")
    print(f"  3D unfriendly only:        {reason_counts['3d_unfriendly_only']}")
    print(f"  Both:                      {reason_counts['both']}")
    print(f"  Total:                     {sum(reason_counts.values())}")
    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
