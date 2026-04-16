"""Generate ordered object+style pair list for matrix batch generation.

Pairs are ordered by round-robin: Round 0 gives every object its highest-priority
style (realistic), Round 1 the second-priority, and so on.  Executing pairs in
index order guarantees maximum object coverage at any stopping point.

Usage:
    python scripts/generate_matrix_pairs.py --output data/matrix_pairs.json
    python scripts/generate_matrix_pairs.py --output data/matrix_pairs.json --rounds 3
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.config import load_config


def load_sources(config):
    """Load object pool and style definitions from config-specified paths."""
    objects_path = Path(config.workspace.matrix_objects_file)
    styles_path = Path(config.workspace.matrix_styles_file)

    if not objects_path.exists():
        raise FileNotFoundError(f"Objects file not found: {objects_path}")
    if not styles_path.exists():
        raise FileNotFoundError(f"Styles file not found: {styles_path}")

    with open(objects_path, encoding="utf-8") as f:
        objects_by_category = json.load(f)
    with open(styles_path, encoding="utf-8") as f:
        styles_data = json.load(f)

    return objects_by_category, styles_data, str(objects_path), str(styles_path)


def build_style_priority(styles_data):
    """Build per-category style priority lists sorted by (-weight, -coverage).

    Returns:
        styles_by_id: dict of style_id -> style definition
        category_priority: dict of category -> [style_id, ...] sorted by priority
        style_coverage: dict of style_id -> number of categories that use it
    """
    styles_by_id = {s["id"]: s for s in styles_data["styles"]}
    mapping = styles_data["category_style_mapping"]

    # Count how many categories use each style
    style_coverage = {sid: 0 for sid in styles_by_id}
    for cat, sids in mapping.items():
        if cat.startswith("_"):
            continue
        for sid in sids:
            style_coverage[sid] = style_coverage.get(sid, 0) + 1

    # Sort each category's styles by (-weight, -coverage)
    category_priority = {}
    for cat, sids in mapping.items():
        if cat.startswith("_"):
            continue
        sorted_sids = sorted(
            sids,
            key=lambda s: (-styles_by_id[s]["weight"], -style_coverage.get(s, 0)),
        )
        category_priority[cat] = sorted_sids

    return styles_by_id, category_priority, style_coverage


def generate_pairs(objects_by_category, styles_by_id, category_priority, max_rounds=None):
    """Generate ordered (object, style) pairs in round-robin fashion.

    Args:
        objects_by_category: dict of category -> [object_name, ...]
        styles_by_id: dict of style_id -> style definition
        category_priority: dict of category -> [style_id, ...] sorted by priority
        max_rounds: limit number of rounds (None = all rounds)

    Returns:
        list of pair dicts with index, round, category, object_name, style_id, style_prefix
    """
    # Determine max rounds across all categories
    total_rounds = max(len(sids) for sids in category_priority.values())
    if max_rounds is not None:
        total_rounds = min(total_rounds, max_rounds)

    categories_sorted = sorted(category_priority.keys())
    pairs = []
    index = 0

    for round_num in range(total_rounds):
        for cat in categories_sorted:
            priority_list = category_priority[cat]
            if round_num >= len(priority_list):
                continue  # This category has no more styles

            style_id = priority_list[round_num]
            style = styles_by_id[style_id]

            objects = sorted(objects_by_category.get(cat, []))
            for obj_name in objects:
                pairs.append({
                    "index": index,
                    "round": round_num,
                    "category": cat,
                    "object_name": obj_name,
                    "style_id": style_id,
                    "style_prefix": style["prefix"],
                })
                index += 1

    return pairs


def main():
    parser = argparse.ArgumentParser(
        description="Generate ordered object+style pair list for matrix batch generation"
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Output JSON path",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=None,
        help="Limit number of rounds (default: all)",
    )
    args = parser.parse_args()

    config = load_config()
    objects_by_category, styles_data, objects_path, styles_path = load_sources(config)
    styles_by_id, category_priority, style_coverage = build_style_priority(styles_data)

    total_objects = sum(len(objs) for objs in objects_by_category.values())
    total_categories = len(category_priority)
    max_possible_rounds = max(len(sids) for sids in category_priority.values())

    print(f"Objects: {total_objects} across {total_categories} categories")
    print(f"Styles: {len(styles_by_id)}")
    print(f"Max rounds: {max_possible_rounds}")
    if args.rounds:
        print(f"Limiting to {args.rounds} rounds")

    pairs = generate_pairs(
        objects_by_category, styles_by_id, category_priority, max_rounds=args.rounds,
    )

    # Build round summary
    round_summary = {}
    for p in pairs:
        r = p["round"]
        if r not in round_summary:
            round_summary[r] = {"objects": 0, "categories": set()}
        round_summary[r]["objects"] += 1
        round_summary[r]["categories"].add(p["category"])
    round_stats = [
        {
            "round": r,
            "objects": round_summary[r]["objects"],
            "categories": len(round_summary[r]["categories"]),
        }
        for r in sorted(round_summary)
    ]

    # Build style priority report
    style_priority_report = {}
    for cat in sorted(category_priority):
        style_priority_report[cat] = [
            {"round": i, "style_id": sid, "weight": styles_by_id[sid]["weight"]}
            for i, sid in enumerate(category_priority[cat])
        ]

    output = {
        "metadata": {
            "total_pairs": len(pairs),
            "total_objects": total_objects,
            "total_categories": total_categories,
            "total_rounds": len(round_summary),
            "max_possible_rounds": max_possible_rounds,
            "sort_key": "(-weight, -coverage)",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "objects_source": objects_path,
            "styles_source": styles_path,
        },
        "round_summary": round_stats,
        "style_priority_by_category": style_priority_report,
        "pairs": pairs,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nGenerated {len(pairs)} pairs across {len(round_summary)} rounds")
    print(f"Written to {out_path}")
    print("\nRound summary:")
    cumulative = 0
    for rs in round_stats:
        cumulative += rs["objects"]
        print(f"  Round {rs['round']:2d}: {rs['objects']:5d} objects  "
              f"(cumulative: {cumulative:6d})  categories: {rs['categories']}/20")


if __name__ == "__main__":
    main()
