#!/usr/bin/env python3
"""
扫描 pipeline 数据并生成 edit-pair manifest。

支持两种模式：
1. 全量扫描模式（无 --plan-paths）：扫描文件系统，采样 15 个样本
2. YAML 过滤模式（--plan-paths）：从 edit_records.jsonl 筛选，导出所有满足条件的 pair

用法:
    # 全量扫描（兼容旧行为）
    python scripts/export_edit_pair_manifest.py

    # 按 YAML 过滤 + LPIPS 阈值
    python scripts/export_edit_pair_manifest.py \
        --plan-paths pipeline/experiment_plans/xxx.yaml,yyy.yaml \
        --lpips-max 0.15 \
        --dataset-name my_export \
        --path-prefix /seaweedfs/xiaoliang/data/2d3d_data
"""
import argparse
import json
import glob
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.config import load_config

VIEWS = ["front", "back", "left", "right", "top", "bottom"]
FAIL_STATUSES = {"failed_quality", "error_pipeline", "error_quality_check"}
DEFAULT_SAMPLE_COUNT = 15

PROVIDER_NAMES = {
    "hy3": "hunyuan",
    "tp3": "tripo",
    "tp": "tripo",
    "hy": "hunyuan",
    "rd2": "rodin",
}


def provider_name(pid):
    return PROVIDER_NAMES.get(pid, pid)


# ==================== 全量扫描模式（兼容旧行为） ====================


def scan_candidates_filesystem(root: str):
    """扫描文件系统获取所有合格候选。"""
    candidates = []
    pattern = f"{root}/triplets/*/edited/*/meta.json"
    meta_paths = glob.glob(pattern)
    print(f"Found {len(meta_paths)} edit meta.json files")

    for meta_path in meta_paths:
        parts = meta_path.split("/")
        model_id = parts[-4]
        edit_id = parts[-2]

        if "_edit_" in model_id:
            continue

        try:
            with open(meta_path) as f:
                em = json.load(f)
        except Exception:
            continue

        instruction = em.get("instruction", "").strip()
        if not instruction:
            continue

        edit_status = em.get("edit_status", "")
        if edit_status in FAIL_STATUSES:
            continue

        source_provider_id = em.get("source_provider_id", "")
        if not source_provider_id:
            continue

        edited_view_dir = f"{root}/triplets/{model_id}/edited/{edit_id}"
        if not all(os.path.exists(f"{edited_view_dir}/{v}.png") for v in VIEWS):
            continue

        source_view_dir = f"{root}/triplets/{model_id}/views/{source_provider_id}"
        if not all(os.path.exists(f"{source_view_dir}/{v}.png") for v in VIEWS):
            continue

        source_glb = f"{root}/models_src/{model_id}/model_{source_provider_id}.glb"
        if not os.path.exists(source_glb):
            continue

        target_model_dir = f"{root}/models_src/{model_id}_edit_{edit_id}"
        target_glbs = sorted(glob.glob(f"{target_model_dir}/model_*.glb"))
        if not target_glbs:
            continue
        target_glb = target_glbs[0]
        target_provider_id = Path(target_glb).stem.replace("model_", "")

        image_meta_path = f"{root}/images/{model_id}/meta.json"
        category = ""
        object_name = ""
        if os.path.exists(image_meta_path):
            try:
                with open(image_meta_path) as f:
                    im = json.load(f)
                category = im.get("category", "")
                object_name = im.get("object_name", "")
            except Exception:
                pass

        if not category or not object_name:
            continue

        candidates.append({
            "model_id": model_id,
            "edit_id": edit_id,
            "category": category,
            "object_name": object_name,
            "instruction": instruction,
            "edit_status": edit_status,
            "source_provider_id": source_provider_id,
            "target_provider_id": target_provider_id,
            "source_glb": source_glb,
            "target_glb": target_glb,
            "source_view_dir": source_view_dir,
            "edited_view_dir": edited_view_dir,
            "image_meta_path": image_meta_path,
            "target_model_dir": target_model_dir,
            "source_image": f"{root}/images/{model_id}/image.png",
            "instructions_json": f"{root}/images/{model_id}/instructions.json",
            "stage2_score": None,
        })

    return candidates


def sample_candidates(candidates, target_count=DEFAULT_SAMPLE_COUNT):
    STATUS_PRIORITY = {"passed": 0, "": 1}
    candidates.sort(key=lambda x: (
        STATUS_PRIORITY.get(x["edit_status"], 2),
        x["model_id"],
        x["edit_id"],
    ))

    selected = []
    seen_categories = set()
    seen_objects = set()

    for c in candidates:
        if c["category"] not in seen_categories and c["object_name"] not in seen_objects:
            selected.append(c)
            seen_categories.add(c["category"])
            seen_objects.add(c["object_name"])
        if len(selected) == target_count:
            break

    if len(selected) < target_count:
        for c in candidates:
            if c["object_name"] not in seen_objects:
                selected.append(c)
                seen_objects.add(c["object_name"])
            if len(selected) == target_count:
                break

    return selected


# ==================== YAML 过滤模式 ====================


def scan_candidates_from_yaml(root: str, plan_paths: list[str], lpips_max: float | None):
    """从 edit_records.jsonl 筛选候选，校验文件完整性。"""
    experiments_dir = Path(root) / "experiments"
    if not experiments_dir.exists():
        print(f"Experiments directory not found: {experiments_dir}")
        return []

    # 收集匹配的 experiment 记录
    records = []
    for experiment_dir in experiments_dir.iterdir():
        if not experiment_dir.is_dir():
            continue
        manifest_path = experiment_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except Exception:
            continue
        if manifest.get("plan_path") not in plan_paths:
            continue

        edit_records_path = experiment_dir / "edit_records.jsonl"
        if not edit_records_path.exists():
            continue
        try:
            with open(edit_records_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue

    print(f"Collected {len(records)} edit records from {len(plan_paths)} YAML plan(s)")

    # 去重取最新
    best: dict[tuple[str, str], dict] = {}
    for record in records:
        target_model_id = record.get("target_model_id")
        if not target_model_id:
            continue
        source_model_id = record.get("source_model_id", "")
        edit_id = record.get("edit_id", "")
        if not source_model_id or not edit_id:
            continue

        # LPIPS 过滤
        if lpips_max is not None:
            score = record.get("stage2_score")
            if score is None or score > lpips_max:
                continue

        key = (source_model_id, edit_id)
        existing = best.get(key)
        if existing is None or (record.get("created_at", "") > existing.get("created_at", "")):
            best[key] = record

    print(f"After dedup & LPIPS filter: {len(best)} unique pairs")

    # 文件完整性校验
    candidates = []
    skipped_reasons = Counter()
    for (source_model_id, edit_id), record in best.items():
        source_provider_id = record.get("source_provider_id", "")
        target_provider_id = record.get("target_provider_id", source_provider_id)

        # source GLB
        source_glb = f"{root}/models_src/{source_model_id}/model_{source_provider_id}.glb"
        if not os.path.exists(source_glb):
            skipped_reasons["source_glb_missing"] += 1
            continue

        # target GLB
        target_model_id = record["target_model_id"]
        target_model_dir = f"{root}/models_src/{target_model_id}"
        target_glbs = sorted(glob.glob(f"{target_model_dir}/model_*.glb"))
        if not target_glbs:
            skipped_reasons["target_glb_missing"] += 1
            continue
        target_glb = target_glbs[0]
        actual_target_provider_id = Path(target_glb).stem.replace("model_", "")

        # source views
        source_view_dir = f"{root}/triplets/{source_model_id}/views/{source_provider_id}"
        if not all(os.path.exists(f"{source_view_dir}/{v}.png") for v in VIEWS):
            skipped_reasons["source_views_incomplete"] += 1
            continue

        # edited views
        edited_view_dir = f"{root}/triplets/{source_model_id}/edited/{edit_id}"
        if not all(os.path.exists(f"{edited_view_dir}/{v}.png") for v in VIEWS):
            skipped_reasons["edited_views_incomplete"] += 1
            continue

        # category/object from record or image meta
        category = record.get("category", "")
        object_name = record.get("object_name", "")
        if not category or not object_name:
            image_meta_path = f"{root}/images/{source_model_id}/meta.json"
            if os.path.exists(image_meta_path):
                try:
                    with open(image_meta_path) as f:
                        im = json.load(f)
                    category = category or im.get("category", "")
                    object_name = object_name or im.get("object_name", "")
                except Exception:
                    pass

        candidates.append({
            "model_id": source_model_id,
            "edit_id": edit_id,
            "category": category,
            "object_name": object_name,
            "instruction": record.get("instruction_text", ""),
            "edit_status": record.get("final_status", ""),
            "source_provider_id": source_provider_id,
            "target_provider_id": actual_target_provider_id,
            "source_glb": source_glb,
            "target_glb": target_glb,
            "source_view_dir": source_view_dir,
            "edited_view_dir": edited_view_dir,
            "image_meta_path": f"{root}/images/{source_model_id}/meta.json",
            "target_model_dir": target_model_dir,
            "source_image": f"{root}/images/{source_model_id}/image.png",
            "instructions_json": f"{root}/images/{source_model_id}/instructions.json",
            "stage2_score": record.get("stage2_score"),
        })

    if skipped_reasons:
        print(f"Skipped {sum(skipped_reasons.values())} pairs:")
        for reason, count in skipped_reasons.most_common():
            print(f"  {reason}: {count}")

    return candidates


# ==================== 构建 manifest ====================


def build_manifest(candidates, selected, *, root: str, path_prefix: str,
                   dataset_name: str, selection_policy: dict):
    """构建 manifest，路径使用 path_prefix。"""
    samples = []
    for idx, c in enumerate(selected, 1):
        sid = f"{idx:04d}"
        model_id = c["model_id"]
        edit_id = c["edit_id"]
        spid = c["source_provider_id"]
        tpid = c["target_provider_id"]

        # 路径替换：将 root 替换为 path_prefix
        def rebase(path: str) -> str:
            if path.startswith(root):
                return path_prefix + path[len(root):]
            return path

        sample = {
            "sample_id": sid,
            "model_id": model_id,
            "edit_id": edit_id,
            "target_model_id": f"{model_id}_edit_{edit_id}",
            "object_name": c["object_name"],
            "category": c["category"],
            "instruction": c["instruction"],
            "instruction_source": f"triplets/{model_id}/edited/{edit_id}/meta.json",
            "source_provider_id": spid,
            "source_provider_name": provider_name(spid),
            "target_provider_id": tpid,
            "target_provider_name": provider_name(tpid),
            "stage2_score": c.get("stage2_score"),
            "status": {
                "edit_status": c["edit_status"],
                "source_views_complete": True,
                "edited_views_complete": True,
                "source_glb_exists": True,
                "target_glb_exists": True,
            },
            "files": {
                "source_image": rebase(c["source_image"]),
                "source_image_meta": rebase(c["image_meta_path"]),
                "instructions_json": rebase(c["instructions_json"]),
                "source_glb": rebase(c["source_glb"]),
                "source_glb_meta": rebase(f"{root}/models_src/{model_id}/meta.json"),
                "target_glb": rebase(c["target_glb"]),
                "target_glb_meta": rebase(f"{root}/models_src/{model_id}_edit_{edit_id}/meta.json"),
                "edit_meta": rebase(f"{root}/triplets/{model_id}/edited/{edit_id}/meta.json"),
            },
            "source_glb": rebase(c["source_glb"]),
            "target_glb": rebase(c["target_glb"]),
            "source_views": {v: rebase(f"{c['source_view_dir']}/{v}.png") for v in VIEWS},
            "edited_views": {v: rebase(f"{c['edited_view_dir']}/{v}.png") for v in VIEWS},
        }
        samples.append(sample)

    categories_covered = sorted(set(s["category"] for s in samples if s["category"]))

    manifest = {
        "dataset_name": dataset_name,
        "dataset_version": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_root": path_prefix,
        "selection_policy": selection_policy,
        "view_order": VIEWS,
        "total_candidates_scanned": len(candidates),
        "categories_covered": categories_covered,
        "sample_count": len(samples),
        "samples": samples,
    }
    return manifest


def write_outputs(manifest, root: str, dataset_name: str):
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = f"{root}/exports/{dataset_name}_{date_str}"
    os.makedirs(out_dir, exist_ok=True)

    manifest_path = f"{out_dir}/manifest.json"
    jsonl_path = f"{out_dir}/manifest.jsonl"

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for s in manifest["samples"]:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    print(f"\nWritten: {manifest_path}")
    print(f"Written: {jsonl_path}")
    return out_dir


def parse_args():
    parser = argparse.ArgumentParser(description="导出 edit-pair manifest")
    parser.add_argument(
        "--plan-paths",
        type=str,
        default=None,
        help="逗号分隔的 YAML plan_path 列表（相对 pipeline_dir），指定后从 edit_records 筛选",
    )
    parser.add_argument(
        "--lpips-max",
        type=float,
        default=None,
        help="LPIPS 分数上限，仅保留 stage2_score <= 此值的 pair",
    )
    parser.add_argument(
        "--dataset-name",
        type=str,
        default=None,
        help="导出数据集名称（默认: edit_pairs_<count>_samples 或 edit_pairs_export）",
    )
    parser.add_argument(
        "--path-prefix",
        type=str,
        default=None,
        help="manifest 中路径前缀（默认使用 pipeline_dir）",
    )
    parser.add_argument(
        "--sample-count",
        type=int,
        default=DEFAULT_SAMPLE_COUNT,
        help=f"全量扫描模式的采样数量（默认 {DEFAULT_SAMPLE_COUNT}，仅无 --plan-paths 时生效）",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    config = load_config()
    root = config.workspace.pipeline_dir
    path_prefix = args.path_prefix if args.path_prefix else root

    if args.plan_paths:
        # YAML 过滤模式
        plan_paths = [p.strip() for p in args.plan_paths.split(",") if p.strip()]
        print(f"=== YAML 过滤模式: {len(plan_paths)} 个 plan ===")
        for p in plan_paths:
            print(f"  {p}")

        candidates = scan_candidates_from_yaml(root, plan_paths, args.lpips_max)
        selected = candidates  # 不采样，全部导出
        dataset_name = args.dataset_name or "edit_pairs_export"
        selection_policy = {
            "mode": "yaml_filter",
            "plan_paths": plan_paths,
            "lpips_max": args.lpips_max,
            "require_source_glb": True,
            "require_target_glb": True,
            "require_6_views": True,
        }
    else:
        # 全量扫描模式（兼容旧行为）
        print("=== 全量扫描模式 ===")
        candidates = scan_candidates_filesystem(root)
        selected = sample_candidates(candidates, args.sample_count)
        dataset_name = args.dataset_name or f"edit_pairs_{args.sample_count}_samples"
        selection_policy = {
            "mode": "filesystem_scan",
            "target_sample_count": args.sample_count,
            "distinct_object_required": True,
            "prefer_distinct_categories": True,
            "single_edit_per_object": True,
            "require_source_glb": True,
            "require_target_glb": True,
            "require_separate_multiview_images": True,
        }

    print(f"\nTotal valid candidates: {len(candidates)}")
    print(f"Selected: {len(selected)} samples")

    if not selected:
        print("No samples to export. Exiting.")
        return

    # 打印分布
    cat_dist = Counter(c["category"] for c in selected if c["category"])
    if cat_dist:
        print("\nCategory distribution:")
        for cat, cnt in sorted(cat_dist.items(), key=lambda x: -x[1]):
            print(f"  {cat}: {cnt}")

    print(f"\n=== Building manifest ===")
    manifest = build_manifest(
        candidates, selected,
        root=root,
        path_prefix=path_prefix,
        dataset_name=dataset_name,
        selection_policy=selection_policy,
    )

    print(f"\nSample preview (first 10):")
    for s in manifest["samples"][:10]:
        score_str = f"{s['stage2_score']:.4f}" if s.get("stage2_score") is not None else "N/A"
        print(f"  {s['sample_id']} | {s.get('category', ''):<28} | {s.get('object_name', ''):<20} | LPIPS={score_str} | {s['instruction'][:50]}")
    if len(manifest["samples"]) > 10:
        print(f"  ... and {len(manifest['samples']) - 10} more")

    print(f"\n=== Writing output ===")
    out_dir = write_outputs(manifest, root, dataset_name)
    print(f"Output directory: {out_dir}")
    print(f"Total samples: {len(manifest['samples'])}")
    print("Done.")


if __name__ == "__main__":
    main()
