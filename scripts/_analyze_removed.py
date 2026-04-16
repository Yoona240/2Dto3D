#!/usr/bin/env python3
"""Quick analysis of removed objects."""
import json
from pathlib import Path

DATA = Path(__file__).parent.parent / "data" / "captions" / "removed_objects_with_reasons.json"

with open(DATA, "r", encoding="utf-8") as f:
    data = json.load(f)

# 1. Lighting & Ceiling - full list
print("=" * 70)
print("LIGHTING & CEILING (almost all removed)")
print("=" * 70)
for item in data.get("Lighting & Ceiling", []):
    obj = item["object"]
    cn = item["chinese_name"]
    score = item["editability_score"]
    reasons = item["removal_reasons"]
    unfriendly = item["unfriendly_reasons"]
    print(f"  {obj} ({cn}) | score={score} | {reasons} | {unfriendly}")

# 2. High-score removals (score >= 4, only 3d_unfriendly)
print()
print("=" * 70)
print("HIGH-SCORE REMOVALS (score>=4, only 3d_unfriendly) - possibly over-filtered")
print("=" * 70)
for cat, items in data.items():
    for item in items:
        if item["editability_score"] >= 4 and item["removal_reasons"] == ["3d_unfriendly"]:
            obj = item["object"]
            cn = item["chinese_name"]
            score = item["editability_score"]
            unfriendly = item["unfriendly_reasons"]
            print(f"  [{cat}] {obj} ({cn}) | score={score} | {unfriendly}")

# 3. Food & Beverage - what got kept vs removed breakdown
print()
print("=" * 70)
print("FOOD & BEVERAGE REMOVAL REASON BREAKDOWN")
print("=" * 70)
food_items = data.get("Food & Beverage", [])
by_reason = {"too_simple_only": [], "3d_unfriendly_only": [], "both": []}
for item in food_items:
    r = item["removal_reasons"]
    if "too_simple" in r and "3d_unfriendly" in r:
        by_reason["both"].append(item)
    elif "too_simple" in r:
        by_reason["too_simple_only"].append(item)
    elif "3d_unfriendly" in r:
        by_reason["3d_unfriendly_only"].append(item)

print(f"  Too simple only: {len(by_reason['too_simple_only'])}")
for item in by_reason["too_simple_only"][:15]:
    print(f"    - {item['object']} ({item['chinese_name']}) score={item['editability_score']}")
if len(by_reason["too_simple_only"]) > 15:
    print(f"    ... and {len(by_reason['too_simple_only']) - 15} more")

print(f"  3D unfriendly only: {len(by_reason['3d_unfriendly_only'])}")
for item in by_reason["3d_unfriendly_only"][:15]:
    print(f"    - {item['object']} ({item['chinese_name']}) score={item['editability_score']} | {item['unfriendly_reasons']}")
if len(by_reason["3d_unfriendly_only"]) > 15:
    print(f"    ... and {len(by_reason['3d_unfriendly_only']) - 15} more")

print(f"  Both: {len(by_reason['both'])}")

# 4. Unfriendly reason distribution
print()
print("=" * 70)
print("3D UNFRIENDLY REASON DISTRIBUTION (across all categories)")
print("=" * 70)
reason_counter = {}
for cat, items in data.items():
    for item in items:
        for r in item.get("unfriendly_reasons", []):
            # Normalize
            key = r.split("(")[0].strip().lower()
            reason_counter[key] = reason_counter.get(key, 0) + 1

for reason, count in sorted(reason_counter.items(), key=lambda x: -x[1]):
    print(f"  {reason}: {count}")
