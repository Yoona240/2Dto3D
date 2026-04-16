"""Generate a YAML experiment plan from matrix_pairs.json and run it.

Reads a slice of pairs from the ordered matrix_pairs.json, groups them by
category, generates a YAML experiment plan with style_ids, and invokes
run_full_experiment.py through the same subprocess mechanism used by app.py.

Usage:
    # Dry run: generate YAML only, don't execute
    python scripts/run_matrix_batch.py --start 0 --count 100 --dry-run

    # Execute
    python scripts/run_matrix_batch.py --start 0 --count 100

    # Custom parameters
    python scripts/run_matrix_batch.py \\
        --pairs data/matrix_pairs.json \\
        --start 0 --count 50 \\
        --edits-per-object 4 \\
        --source-provider hunyuan --target-provider hunyuan \\
        --edit-mode multiview \\
        --name my-batch
"""
import argparse
import json
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.config import load_config


def load_pairs(pairs_path: str, start: int, count: int) -> list:
    with open(pairs_path, encoding="utf-8") as f:
        data = json.load(f)
    pairs = data["pairs"]
    end = min(start + count, len(pairs))
    selected = pairs[start:end]
    if not selected:
        raise ValueError(
            f"No pairs in range [{start}, {end}). "
            f"Total pairs: {len(pairs)}"
        )
    return selected


def group_by_category(pairs: list) -> OrderedDict:
    """Group pairs by category, preserving insertion order within each group."""
    groups = OrderedDict()
    for p in pairs:
        cat = p["category"]
        if cat not in groups:
            groups[cat] = {"objects": [], "style_ids": []}
        groups[cat]["objects"].append(p["object_name"])
        groups[cat]["style_ids"].append(p["style_id"])
    return groups


def build_yaml_plan(
    groups: OrderedDict,
    name: str,
    source_provider: str,
    target_provider: str,
    edit_mode: str,
    edits_per_object: int,
) -> dict:
    categories = []
    for cat, group in groups.items():
        categories.append({
            "category_name": cat,
            "random": {"category": False, "object": False},
            "object_count": len(group["objects"]),
            "objects": group["objects"],
            "style_ids": group["style_ids"],
            "instruction_plan": {
                "mode": "adaptive_k",
                "count": edits_per_object,
                "allowed_types": ["remove", "replace"],
            },
        })
    return {
        "name": name,
        "source_provider": source_provider,
        "target_provider": target_provider,
        "edit_mode": edit_mode,
        "categories": categories,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run a batch of matrix pairs through the full experiment pipeline"
    )
    parser.add_argument("--pairs", help="Path to matrix_pairs.json (default: from config)")
    parser.add_argument("--start", type=int, required=True, help="Start index in pairs list")
    parser.add_argument("--count", type=int, required=True, help="Number of pairs to process")
    parser.add_argument("--edits-per-object", type=int, default=4, help="Edit instructions per object (default: 4)")
    parser.add_argument("--source-provider", default="hunyuan", help="Source 3D provider (default: hunyuan)")
    parser.add_argument("--target-provider", default="hunyuan", help="Target 3D provider (default: hunyuan)")
    parser.add_argument("--edit-mode", default="multiview", help="Edit mode (default: multiview)")
    parser.add_argument("--name", help="Experiment name (default: auto-generated)")
    parser.add_argument("--dry-run", action="store_true", help="Generate YAML only, don't execute")
    parser.add_argument("--gpu-id", type=int, default=0, help="GPU ID for rendering (default: 0)")
    args = parser.parse_args()

    config = load_config()
    pipeline_dir = Path(config.workspace.pipeline_dir)
    plans_dir = pipeline_dir / "experiment_plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    # Resolve pairs path
    pairs_path = args.pairs
    if not pairs_path:
        pairs_path = getattr(config.workspace, "matrix_pairs_file", None)
    if not pairs_path:
        # Default fallback
        pairs_path = str(Path(__file__).resolve().parent.parent / "data" / "matrix_pairs.json")
    if not Path(pairs_path).exists():
        print(f"ERROR: pairs file not found: {pairs_path}")
        sys.exit(1)

    # Load and slice pairs
    selected = load_pairs(pairs_path, args.start, args.count)
    actual_count = len(selected)
    start_round = selected[0]["round"]
    end_round = selected[-1]["round"]

    print(f"Selected {actual_count} pairs: index [{args.start}, {args.start + actual_count})")
    print(f"Rounds: {start_round} ~ {end_round}")

    # Group by category
    groups = group_by_category(selected)
    print(f"Categories: {len(groups)}")
    for cat, g in groups.items():
        styles = set(g["style_ids"])
        print(f"  {cat}: {len(g['objects'])} objects, styles: {sorted(styles)}")

    # Build YAML plan
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = args.name or f"matrix-r{start_round}-{timestamp}"
    plan = build_yaml_plan(
        groups=groups,
        name=name,
        source_provider=args.source_provider,
        target_provider=args.target_provider,
        edit_mode=args.edit_mode,
        edits_per_object=args.edits_per_object,
    )

    plan_filename = f"{timestamp}_{name}.yaml"
    plan_path = plans_dir / plan_filename
    with open(plan_path, "w", encoding="utf-8") as f:
        yaml.dump(plan, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"\nYAML plan written: {plan_path}")

    if args.dry_run:
        print("\n[dry-run] YAML content:")
        with open(plan_path, "r") as f:
            print(f.read())
        print("[dry-run] Exiting without execution.")
        return

    # Execute via run_full_experiment.py
    python_interpreter = config.workspace.python_interpreter
    experiment_script = str(Path(__file__).resolve().parent / "run_full_experiment.py")
    logs_dir = Path(config.workspace.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        python_interpreter,
        experiment_script,
        "--plan", str(plan_path),
        "--gpu-id", str(args.gpu_id),
    ]
    print(f"\nExecuting: {' '.join(cmd)}")

    log_file = logs_dir / f"{plan_filename.replace('.yaml', '')}.log"
    print(f"Log file: {log_file}")

    with open(log_file, "w") as lf:
        process = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
    print(f"Started PID={process.pid}")
    print(f"\nTo follow logs: tail -f {log_file}")


if __name__ == "__main__":
    main()
