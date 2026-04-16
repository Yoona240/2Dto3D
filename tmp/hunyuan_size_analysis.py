import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import trimesh


LOG = Path("/data-koolab-nas/xiaoliang/code3/2d3d_v2/logs/20260414_184703_hunyuan-300-399_categories-1_objects-100.log")
ROOT = Path("/data-oss/meiguang/xiaoliang/data/2d3d_data/models_src")
SEED = 20260415
SAMPLE_N = 50


def load_pairs():
    text = LOG.read_text(errors="ignore")
    pattern = re.compile(
        r"\[Timing\]\[END\]\s+scope=edit\s+stage=target_render\s+status=success.*?"
        r"category=(.+?)\s+object_name=(.+?)\s+source_model_id=([^\s]+)\s+"
        r"instruction_index=\d+\s+instruction_type=([^\s]+)\s+edit_id=([^\s]+).*?"
        r"target_model_id=([^\s]+)"
    )
    pairs = []
    seen = set()
    for category, obj, source_model_id, instruction_type, edit_id, _logged_target_model_id in pattern.findall(text):
        key = (source_model_id, edit_id)
        if key in seen:
            continue
        seen.add(key)
        target_model_id = f"{source_model_id}_edit_{edit_id}"
        paths = {
            "src_raw": ROOT / source_model_id / "model_hy3.glb",
            "src_aln": ROOT / source_model_id / "model_hy3_aligned.glb",
            "tgt_raw": ROOT / target_model_id / "model_hy3.glb",
            "tgt_aln": ROOT / target_model_id / "model_hy3_aligned.glb",
        }
        if all(path.exists() for path in paths.values()):
            pairs.append(
                {
                    "category": category.strip(),
                    "object": obj.strip(),
                    "instruction_type": instruction_type.strip(),
                    "source_model_id": source_model_id,
                    "target_model_id": target_model_id,
                    "edit_id": edit_id,
                    **paths,
                }
            )
    return pairs


def mesh_stats(path: Path):
    scene = trimesh.load(path, force="scene")
    bounds = scene.bounds
    size = (bounds[1] - bounds[0]).astype(float)
    center = ((bounds[1] + bounds[0]) / 2.0).astype(float)
    return {
        "bbox": [float(x) for x in size],
        "center": [float(x) for x in center],
        "max_dim": float(size.max()),
        "diag": float(np.linalg.norm(size)),
        "bbox_volume": float(np.prod(size)),
    }


def qstats(values):
    arr = np.array(list(values), dtype=float)
    if arr.size == 0:
        return None
    q = np.quantile(arr, [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0])
    return {
        "min": float(q[0]),
        "p10": float(q[1]),
        "p25": float(q[2]),
        "p50": float(q[3]),
        "p75": float(q[4]),
        "p90": float(q[5]),
        "max": float(q[6]),
        "mean": float(arr.mean()),
    }


def main():
    usable_pairs = load_pairs()
    random.seed(SEED)
    sample = usable_pairs if len(usable_pairs) <= SAMPLE_N else random.sample(usable_pairs, SAMPLE_N)

    records = []
    for pair in sample:
        src_raw = mesh_stats(pair["src_raw"])
        src_aln = mesh_stats(pair["src_aln"])
        tgt_raw = mesh_stats(pair["tgt_raw"])
        tgt_aln = mesh_stats(pair["tgt_aln"])
        records.append(
            {
                "object": pair["object"],
                "category": pair["category"],
                "instruction_type": pair["instruction_type"],
                "source_model_id": pair["source_model_id"],
                "target_model_id": pair["target_model_id"],
                "edit_id": pair["edit_id"],
                "src_raw_bbox": src_raw["bbox"],
                "tgt_raw_bbox": tgt_raw["bbox"],
                "src_aln_bbox": src_aln["bbox"],
                "tgt_aln_bbox": tgt_aln["bbox"],
                "src_raw_max_dim": src_raw["max_dim"],
                "tgt_raw_max_dim": tgt_raw["max_dim"],
                "src_aln_max_dim": src_aln["max_dim"],
                "tgt_aln_max_dim": tgt_aln["max_dim"],
                "src_raw_diag": src_raw["diag"],
                "tgt_raw_diag": tgt_raw["diag"],
                "src_aln_diag": src_aln["diag"],
                "tgt_aln_diag": tgt_aln["diag"],
                "raw_max_dim_ratio": tgt_raw["max_dim"] / src_raw["max_dim"],
                "aligned_max_dim_ratio": tgt_aln["max_dim"] / src_aln["max_dim"],
                "raw_diag_ratio": tgt_raw["diag"] / src_raw["diag"],
                "aligned_diag_ratio": tgt_aln["diag"] / src_aln["diag"],
                "raw_bbox_vol_ratio": tgt_raw["bbox_volume"] / src_raw["bbox_volume"],
                "aligned_bbox_vol_ratio": tgt_aln["bbox_volume"] / src_aln["bbox_volume"],
                "raw_max_dim_abs_pct": abs(tgt_raw["max_dim"] / src_raw["max_dim"] - 1.0) * 100.0,
                "aligned_max_dim_abs_pct": abs(tgt_aln["max_dim"] / src_aln["max_dim"] - 1.0) * 100.0,
                "raw_diag_abs_pct": abs(tgt_raw["diag"] / src_raw["diag"] - 1.0) * 100.0,
                "aligned_diag_abs_pct": abs(tgt_aln["diag"] / src_aln["diag"] - 1.0) * 100.0,
                "raw_center_delta": float(
                    np.linalg.norm(np.array(tgt_raw["center"]) - np.array(src_raw["center"]))
                ),
                "aligned_center_delta": float(
                    np.linalg.norm(np.array(tgt_aln["center"]) - np.array(src_aln["center"]))
                ),
            }
        )

    raw_abs = [record["raw_max_dim_abs_pct"] for record in records]
    aligned_abs = [record["aligned_max_dim_abs_pct"] for record in records]
    raw_diag_abs = [record["raw_diag_abs_pct"] for record in records]
    aligned_diag_abs = [record["aligned_diag_abs_pct"] for record in records]
    raw_ratio = [record["raw_max_dim_ratio"] for record in records]
    aligned_ratio = [record["aligned_max_dim_ratio"] for record in records]
    raw_center = [record["raw_center_delta"] for record in records]
    aligned_center = [record["aligned_center_delta"] for record in records]
    raw_vol_ratio = [record["raw_bbox_vol_ratio"] for record in records]

    threshold_counts = {}
    for threshold in [1, 3, 5, 10]:
        threshold_counts[f"raw_abs_pct_le_{threshold}"] = sum(v <= threshold for v in raw_abs)
        threshold_counts[f"aligned_abs_pct_le_{threshold}"] = sum(v <= threshold for v in aligned_abs)

    by_object = defaultdict(list)
    for record in records:
        by_object[record["object"]].append(record)

    object_summary = []
    for obj, group in by_object.items():
        object_summary.append(
            {
                "object": obj,
                "count": len(group),
                "raw_max_dim_ratio_mean": float(np.mean([x["raw_max_dim_ratio"] for x in group])),
                "raw_max_dim_abs_pct_mean": float(np.mean([x["raw_max_dim_abs_pct"] for x in group])),
                "aligned_max_dim_ratio_mean": float(np.mean([x["aligned_max_dim_ratio"] for x in group])),
                "aligned_max_dim_abs_pct_mean": float(np.mean([x["aligned_max_dim_abs_pct"] for x in group])),
            }
        )
    object_summary.sort(key=lambda x: (-x["count"], -x["raw_max_dim_abs_pct_mean"]))

    by_instruction = defaultdict(list)
    for record in records:
        by_instruction[record["instruction_type"]].append(record)

    instruction_summary = []
    for instruction_type, group in by_instruction.items():
        instruction_summary.append(
            {
                "instruction_type": instruction_type,
                "count": len(group),
                "raw_max_dim_ratio_mean": float(np.mean([x["raw_max_dim_ratio"] for x in group])),
                "raw_max_dim_abs_pct_mean": float(np.mean([x["raw_max_dim_abs_pct"] for x in group])),
                "aligned_max_dim_ratio_mean": float(np.mean([x["aligned_max_dim_ratio"] for x in group])),
                "aligned_max_dim_abs_pct_mean": float(np.mean([x["aligned_max_dim_abs_pct"] for x in group])),
                "raw_diag_abs_pct_mean": float(np.mean([x["raw_diag_abs_pct"] for x in group])),
            }
        )
    instruction_summary.sort(key=lambda x: x["instruction_type"])

    result = {
        "log": str(LOG),
        "usable_pairs_in_log": len(usable_pairs),
        "sample_size": len(sample),
        "sample_object_counts": Counter(record["object"] for record in records).most_common(),
        "sample_instruction_counts": Counter(record["instruction_type"] for record in records).most_common(),
        "overall": {
            "raw_max_dim_ratio": qstats(raw_ratio),
            "aligned_max_dim_ratio": qstats(aligned_ratio),
            "raw_max_dim_abs_pct": qstats(raw_abs),
            "aligned_max_dim_abs_pct": qstats(aligned_abs),
            "raw_diag_abs_pct": qstats(raw_diag_abs),
            "aligned_diag_abs_pct": qstats(aligned_diag_abs),
            "raw_center_delta": qstats(raw_center),
            "aligned_center_delta": qstats(aligned_center),
            "raw_bbox_vol_ratio": qstats(raw_vol_ratio),
            "counts": threshold_counts,
        },
        "largest_raw_size_changes": sorted(records, key=lambda x: x["raw_max_dim_abs_pct"], reverse=True)[:12],
        "smallest_raw_size_changes": sorted(records, key=lambda x: x["raw_max_dim_abs_pct"])[:12],
        "by_object": object_summary,
        "by_instruction_type": instruction_summary,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
