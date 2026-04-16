"""Generate a JSON manifest of all Stage2-passed triplets with asset paths.

Usage:
    python scripts/generate_data_manifest.py <log_file> [--output <output_path>] [--provider <provider_id>]

Example:
    python scripts/generate_data_manifest.py \\
        /data-koolab-nas/xiaoliang/code3/2d3d_v2/logs/20260410_163947_prompt-improve-test03_....log \\
        --output /data-oss/meiguang/xiaoliang/data/2d3d_data/experiment_manifests/prompt-improve-test03_manifest.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Add project root to path so we can import config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.config import load_config

VIEW_NAMES = ["front", "back", "left", "right", "top", "bottom"]


def parse_field(line: str, key: str) -> str:
    m = re.search(rf'{key}=([^ ]+(?:\s[^ ]+)*?)(?=\s+\w+=|\s*$)', line)
    return m.group(1).strip() if m else ""


def path_if_exists(p: Path) -> str | None:
    return str(p) if p.exists() else None


def collect_views(views_dir: Path) -> dict:
    result = {}
    for vn in VIEW_NAMES:
        p = views_dir / f"{vn}.png"
        result[vn] = str(p) if p.exists() else None
    return result


def collect_per_view_masks(edit_dir: Path) -> dict | None:
    masks = {}
    for vn in VIEW_NAMES:
        mp = edit_dir / f"{vn}_mask.png"
        if mp.exists():
            masks[vn] = str(mp)
    return masks if masks else None


def parse_experiment_id(log_path: str) -> str:
    """Extract experiment_id from log — look for the [run_full_experiment] started line."""
    with open(log_path) as f:
        for line in f:
            if "[run_full_experiment] finished" in line:
                m = re.search(r"experiment_id=(\S+)", line)
                if m:
                    return m.group(1)
            if "[run_full_experiment] started" in line:
                m = re.search(r"target=.*/(\S+)\.yaml", line)
                if m:
                    fallback = m.group(1)
    return fallback if "fallback" in dir() else "unknown"


def build_manifest(log_path: str, data_root: Path, provider: str) -> dict:
    images_dir = data_root / "images"
    models_dir = data_root / "models_src"
    triplets_dir = data_root / "triplets"

    # 1. Collect Stage2 passed edits
    print("Parsing log for Stage2 passed edits...")
    stage2_passed = {}
    with open(log_path) as f:
        for line in f:
            if "stage=stage2_consistency_check" in line and "status=success" in line and "[Timing][END]" in line:
                eid = parse_field(line, "edit_id")
                if eid and eid not in stage2_passed:
                    stage2_passed[eid] = {
                        "source_model_id": parse_field(line, "source_model_id"),
                        "edit_id": eid,
                        "target_model_id": parse_field(line, "target_model_id"),
                        "category": parse_field(line, "category"),
                        "object_name": parse_field(line, "object_name"),
                        "instruction_type": parse_field(line, "instruction_type"),
                        "instruction_index": parse_field(line, "instruction_index"),
                    }
    print(f"  Found {len(stage2_passed)} Stage2-passed edits")

    # 2. Collect instructions
    print("Collecting instructions...")
    instructions = {}
    with open(log_path) as f:
        for line in f:
            if "stage=edit_apply" in line:
                m_eid = re.search(r"edit_id=(\w+)", line)
                m_instr = re.search(r"instruction=(.+?)(?:\s+provider_id)", line)
                if m_eid and m_instr:
                    instructions[m_eid.group(1)] = m_instr.group(1).strip()

    # 3. Collect Stage2 scores
    print("Collecting Stage2 scores...")
    s2_scores = {}
    with open(log_path) as f:
        for line in f:
            if "[Stage2] RESULT" in line and "status=passed" in line:
                m_eid = re.search(r"edit_id=(\w+)", line)
                m_score = re.search(r"score=([\d.]+)", line)
                if m_eid:
                    s2_scores[m_eid.group(1)] = float(m_score.group(1)) if m_score else None

    # 4. Build entries
    print("Building manifest...")
    manifest = []
    missing_count = {
        "source_glb": 0, "target_glb": 0,
        "source_views": 0, "edited_views": 0, "target_views": 0,
        "edit_mask_grid": 0, "per_view_masks": 0,
        "before_image_grid": 0, "target_image_grid": 0,
    }

    for eid, info in sorted(
        stage2_passed.items(),
        key=lambda x: (x[1]["category"], x[1]["object_name"], x[1]["instruction_index"]),
    ):
        src_id = info["source_model_id"]
        tgt_id = info["target_model_id"]

        source_glb_raw = models_dir / src_id / f"model_{provider}.glb"
        source_glb_aligned = models_dir / src_id / f"model_{provider}_aligned.glb"
        target_glb_raw = models_dir / tgt_id / f"model_{provider}.glb"
        target_glb_aligned = models_dir / tgt_id / f"model_{provider}_aligned.glb"
        source_image = images_dir / src_id / "image.png"

        source_views_dir = triplets_dir / src_id / "views" / provider
        edited_views_dir = triplets_dir / src_id / "edited" / eid
        target_views_dir = triplets_dir / tgt_id / "views" / provider

        edit_mask = edited_views_dir / "edit_mask_grid.png"
        before_grid_path = edited_views_dir / "before_image_grid.png"
        target_grid_path = edited_views_dir / "target_image_grid.png"
        target_render_grid = target_views_dir / "target_render_grid.png"

        per_view_masks = collect_per_view_masks(edited_views_dir)

        # Track missing
        if not source_glb_raw.exists(): missing_count["source_glb"] += 1
        if not target_glb_raw.exists(): missing_count["target_glb"] += 1
        if not source_views_dir.exists(): missing_count["source_views"] += 1
        if not edited_views_dir.exists(): missing_count["edited_views"] += 1
        if not target_views_dir.exists(): missing_count["target_views"] += 1
        if not edit_mask.exists(): missing_count["edit_mask_grid"] += 1
        if not per_view_masks: missing_count["per_view_masks"] += 1
        if not before_grid_path.exists(): missing_count["before_image_grid"] += 1
        if not target_grid_path.exists(): missing_count["target_image_grid"] += 1

        entry = {
            "source_model_id": src_id,
            "edit_id": eid,
            "target_model_id": tgt_id,
            "category": info["category"],
            "object_name": info["object_name"],
            "instruction_type": info["instruction_type"],
            "instruction_index": int(info["instruction_index"]) if info["instruction_index"].isdigit() else info["instruction_index"],
            "instruction": instructions.get(eid, ""),
            "stage2_score": s2_scores.get(eid),

            "source_image": path_if_exists(source_image),
            "source_glb": path_if_exists(source_glb_raw),
            "source_glb_aligned": path_if_exists(source_glb_aligned),
            "target_glb": path_if_exists(target_glb_raw),
            "target_glb_aligned": path_if_exists(target_glb_aligned),

            "source_views_dir": str(source_views_dir),
            "source_views": collect_views(source_views_dir),

            "edited_views_dir": str(edited_views_dir),
            "edited_views": collect_views(edited_views_dir),

            "target_views_dir": str(target_views_dir),
            "target_views": collect_views(target_views_dir),

            "edit_mask_grid": path_if_exists(edit_mask),
            "per_view_masks": per_view_masks,

            "before_image_grid": path_if_exists(before_grid_path),
            "target_image_grid": path_if_exists(target_grid_path),
            "target_render_grid": path_if_exists(target_render_grid),

            "edit_meta_path": path_if_exists(edited_views_dir / "meta.json"),
        }
        manifest.append(entry)

    experiment_id = parse_experiment_id(log_path)

    return {
        "experiment_id": experiment_id,
        "data_root": str(data_root),
        "provider": provider,
        "total_triplets": len(manifest),
        "missing_assets_summary": missing_count,
        "triplets": manifest,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate data manifest for Stage2-passed triplets")
    parser.add_argument("log_file", help="Path to the experiment log file")
    parser.add_argument("--output", "-o", help="Output JSON path (default: <data_root>/experiment_manifests/<experiment_name>_manifest.json)")
    parser.add_argument("--provider", default="hy3", help="Provider ID (default: hy3)")
    args = parser.parse_args()

    config = load_config()
    data_root = Path(config.workspace.pipeline_dir)

    result = build_manifest(args.log_file, data_root, args.provider)

    if args.output:
        out_path = Path(args.output)
    else:
        out_path = data_root / "experiment_manifests" / f"{result['experiment_id']}_manifest.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\nDone! {result['total_triplets']} triplets written to {out_path}")
    print(f"Missing assets: {result['missing_assets_summary']}")


if __name__ == "__main__":
    main()
