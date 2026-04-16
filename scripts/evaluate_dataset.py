#!/usr/bin/env python3
"""
Evaluate dataset quality metrics from a manifest.

Supports two manifest schemas:
1) Experiment manifest from scripts/generate_data_manifest.py:
   - top-level key: "triplets" (list)
   - each item typically has: source_views, edited_views, target_views, per_view_masks, stage2_score, etc.
2) Export manifest from scripts/export_edit_pair_manifest.py:
   - top-level key: "samples" (list)
   - each item typically has: source_views, edited_views (often no target_views)

Outputs:
  - per_sample.csv: per-item metrics (and per-view breakdown in columns)
  - summary.json: aggregate stats, missing asset rates, and grouping stats

Notes:
  - LPIPS requires torch+lpips. If unavailable, the script falls back to pixel metrics (MSE/PSNR)
    and a lightweight SSIM implementation.
  - This script does NOT call any external API (offline evaluation).

中文说明（写论文时常用的“硬指标”）：
  - 2D↔3D 一致性：edited 六视图 vs target mesh 渲染六视图（LPIPS / SSIM / PSNR / MSE）
  - 编辑幅度（2D）：source 六视图 vs edited 六视图（MSE / PSNR，用作“改动强度”代理）
  - mask 指标：per-view mask 的编辑像素占比（越大表示改动区域越大）
  - mesh 指标：source/target mesh 的基础质量统计（顶点/面数、bbox、面积、连通分量、watertight/体积）
  - 几何差异（可选）：source↔target 的 Chamfer 近似（采样点最近邻距离的对称和；越大表示形变越大）
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Tuple


VIEW_NAMES = ["front", "back", "left", "right", "top", "bottom"]


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _p(views: Dict[str, Any], view: str) -> Optional[str]:
    """Get view path string or None from a view dict."""
    if not isinstance(views, dict):
        return None
    v = views.get(view)
    if isinstance(v, str) and v.strip():
        return v
    return None


def _exists(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        return Path(path).exists()
    except OSError:
        return False


def _load_rgb(path: str):
    from PIL import Image

    img = Image.open(path).convert("RGB")
    return img


def _load_luma(path: str):
    from PIL import Image

    img = Image.open(path).convert("L")
    return img


def _to_float_array_rgb(img):
    import numpy as np

    arr = np.asarray(img, dtype=np.float32)
    # H x W x 3 in [0,255]
    return arr


def _to_float_array_gray(img):
    import numpy as np

    arr = np.asarray(img, dtype=np.float32)
    # H x W in [0,255]
    return arr


def compute_mse_psnr(a_path: str, b_path: str) -> Tuple[float, float]:
    """Compute MSE and PSNR on RGB pixels (0..255)."""
    import numpy as np

    a = _to_float_array_rgb(_load_rgb(a_path))
    b = _to_float_array_rgb(_load_rgb(b_path))
    if a.shape != b.shape:
        raise ValueError(f"Image size mismatch: {a.shape} vs {b.shape}")
    diff = a - b
    mse = float(np.mean(diff * diff))
    if mse <= 0:
        psnr = float("inf")
    else:
        psnr = 20.0 * math.log10(255.0) - 10.0 * math.log10(mse)
    return mse, psnr


def compute_ssim_gray(a_path: str, b_path: str) -> float:
    """Lightweight SSIM on grayscale; not a full multiscale SSIM."""
    import numpy as np

    a = _to_float_array_gray(_load_luma(a_path))
    b = _to_float_array_gray(_load_luma(b_path))
    if a.shape != b.shape:
        raise ValueError(f"Image size mismatch: {a.shape} vs {b.shape}")

    # Global SSIM approximation (no gaussian window) to keep dependencies minimal.
    # Good enough for relative comparisons across runs.
    a_mean = float(a.mean())
    b_mean = float(b.mean())
    a_var = float(a.var())
    b_var = float(b.var())
    cov = float(((a - a_mean) * (b - b_mean)).mean())

    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    num = (2 * a_mean * b_mean + c1) * (2 * cov + c2)
    den = (a_mean * a_mean + b_mean * b_mean + c1) * (a_var + b_var + c2)
    if den == 0:
        return 0.0
    return float(num / den)


def compute_lpips_if_available(a_path: str, b_path: str, *, device: str, input_mode: str, net: str) -> Optional[float]:
    """Return LPIPS if torch+lpips available, else None."""
    try:
        # Reuse repo implementation for consistent preprocessing and caching.
        from core.render.recon_consistency_checker import compute_lpips_score
    except Exception:
        return None

    try:
        return float(
            compute_lpips_score(
                image_a_path=Path(a_path),
                image_b_path=Path(b_path),
                device=device,
                input_mode=input_mode,
                net=net,
            )
        )
    except ImportError:
        return None


def compute_mask_area_ratio(mask_path: str) -> float:
    """Compute fraction of edited pixels from a white-on-black mask image."""
    import numpy as np
    from PIL import Image

    m = Image.open(mask_path).convert("L")
    arr = np.asarray(m, dtype=np.uint8)
    # Convention: white = edited. Use > 128 threshold.
    edited = (arr > 128).mean()
    return float(edited)


def load_mesh_if_available(mesh_path: str):
    """mesh 加载：若服务器装了 trimesh 才能读取并计算 3D 指标；否则返回 None 并自动跳过 mesh 指标。"""
    try:
        import trimesh  # type: ignore
    except Exception:
        return None
    try:
        # force='mesh' avoids returning a Scene for some formats
        mesh = trimesh.load(mesh_path, force="mesh", process=False)
        return mesh
    except Exception:
        return None


def compute_mesh_basic_stats(mesh_path: str) -> Dict[str, Any]:
    """
    3D mesh 基础质量指标（无需 GT）：
      - 顶点数/面数：粗略反映复杂度
      - bbox 尺寸/对角线：反映尺度与规范化稳定性
      - 表面积：反映几何规模
      - 连通分量数：反映是否存在碎片/断裂
      - watertight/体积：若封闭可估计体积（不封闭则通常无意义）
    若 trimesh 不可用或加载失败，返回空 dict（脚本保持可跑）。
    """
    mesh = load_mesh_if_available(mesh_path)
    if mesh is None:
        return {}

    # Some loaders may return a Scene even with force="mesh" in edge cases.
    # Guard to keep script robust.
    if not hasattr(mesh, "vertices") or not hasattr(mesh, "faces"):
        return {}

    stats: Dict[str, Any] = {}
    try:
        stats["mesh_vertices"] = int(len(mesh.vertices))
        stats["mesh_faces"] = int(len(mesh.faces))
    except Exception:
        pass

    try:
        bounds = mesh.bounds  # (2,3)
        ext = (bounds[1] - bounds[0]).tolist()
        stats["mesh_bbox_dx"] = float(ext[0])
        stats["mesh_bbox_dy"] = float(ext[1])
        stats["mesh_bbox_dz"] = float(ext[2])
        stats["mesh_bbox_diag"] = float(math.sqrt(ext[0] ** 2 + ext[1] ** 2 + ext[2] ** 2))
    except Exception:
        pass

    try:
        stats["mesh_surface_area"] = float(mesh.area)
    except Exception:
        pass

    try:
        # Volume only meaningful for watertight meshes; trimesh returns 0/raises otherwise.
        stats["mesh_is_watertight"] = bool(getattr(mesh, "is_watertight", False))
        if stats["mesh_is_watertight"]:
            stats["mesh_volume"] = float(mesh.volume)
    except Exception:
        pass

    try:
        stats["mesh_components"] = int(len(mesh.split(only_watertight=False)))
    except Exception:
        pass

    return stats


def _try_cdist_nn(a_pts, b_pts) -> Tuple[Optional[float], Optional[float]]:
    """
    点集最近邻距离（A→B）：
      - mean_nn：A 中每个点到 B 最近点的平均距离
      - max_nn：A 中每个点到 B 最近点的最大距离
    用于 Chamfer 的组成部分。优先用 scipy.cKDTree（更快），缺失则小规模退化为暴力计算。
    """
    try:
        import numpy as np
    except Exception:
        return None, None

    # Prefer scipy for performance on server.
    try:
        from scipy.spatial import cKDTree  # type: ignore
    except Exception:
        cKDTree = None

    if cKDTree is not None:
        tree = cKDTree(b_pts)
        dists, _ = tree.query(a_pts, k=1)
        return float(np.mean(dists)), float(np.max(dists))

    # Fallback: chunked brute-force (can be slow; kept conservative).
    # Use only when point count is small.
    n_a = a_pts.shape[0]
    n_b = b_pts.shape[0]
    if n_a > 2000 or n_b > 2000:
        return None, None

    d_all: List[float] = []
    for i in range(n_a):
        diff = b_pts - a_pts[i]
        d2 = (diff * diff).sum(axis=1)
        d_all.append(float(math.sqrt(float(d2.min()))))
    return float(sum(d_all) / len(d_all)), float(max(d_all))


def compute_chamfer_if_available(mesh_a_path: str, mesh_b_path: str, *, n_samples: int) -> Dict[str, Any]:
    """
    Approximate symmetric Chamfer distance by sampling surface points.
    Returns empty dict if trimesh unavailable.
    """
    try:
        import numpy as np
    except Exception:
        return {}

    a = load_mesh_if_available(mesh_a_path)
    b = load_mesh_if_available(mesh_b_path)
    if a is None or b is None:
        return {}
    if not hasattr(a, "sample") or not hasattr(b, "sample"):
        return {}

    try:
        a_pts = np.asarray(a.sample(n_samples), dtype=np.float32)
        b_pts = np.asarray(b.sample(n_samples), dtype=np.float32)
    except Exception:
        return {}

    a2b_mean, a2b_max = _try_cdist_nn(a_pts, b_pts)
    b2a_mean, b2a_max = _try_cdist_nn(b_pts, a_pts)
    if a2b_mean is None or b2a_mean is None:
        return {}

    return {
        "chamfer_mean": float(a2b_mean + b2a_mean),
        "chamfer_max": float(max(a2b_max or 0.0, b2a_max or 0.0)),
        "nn_a2b_mean": float(a2b_mean),
        "nn_b2a_mean": float(b2a_mean),
    }


def _percentiles(values: List[float], ps: List[int]) -> Dict[str, float]:
    if not values:
        return {f"p{p}": float("nan") for p in ps}
    xs = sorted(values)
    n = len(xs)

    def pct(p: int) -> float:
        if n == 1:
            return float(xs[0])
        # nearest-rank interpolation
        k = (p / 100.0) * (n - 1)
        f = int(math.floor(k))
        c = int(math.ceil(k))
        if f == c:
            return float(xs[f])
        return float(xs[f] * (c - k) + xs[c] * (k - f))

    return {f"p{p}": pct(p) for p in ps}


@dataclass
class EvalConfig:
    views: List[str]
    lpips_device: str
    lpips_input_mode: str
    lpips_net: str
    compute_ssim: bool
    compute_mesh: bool
    chamfer_samples: int
    prefer_aligned_mesh: bool


def _detect_schema(manifest: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    if isinstance(manifest.get("triplets"), list):
        return "triplets", list(manifest["triplets"])
    if isinstance(manifest.get("samples"), list):
        return "samples", list(manifest["samples"])
    raise ValueError("Unknown manifest schema: expected top-level 'triplets' or 'samples'")


def _item_id(schema: str, item: Dict[str, Any]) -> str:
    if schema == "triplets":
        sid = item.get("source_model_id", "")
        eid = item.get("edit_id", "")
        return f"{sid}_edit_{eid}" if sid and eid else (item.get("target_model_id") or "unknown")
    # samples schema
    return str(item.get("sample_id") or item.get("target_model_id") or item.get("model_id") or "unknown")


def evaluate_manifest(manifest_path: Path, out_dir: Path, cfg: EvalConfig) -> None:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    schema, items = _detect_schema(manifest)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 资产缺失统计：用于衡量数据集“完整性”（论文里可报告缺失率）
    missing = Counter()
    groups = defaultdict(list)  # key -> list of item indices

    # Collect per-item rows
    rows: List[Dict[str, Any]] = []

    # 是否具备 target_views：决定能否计算“2D↔3D 一致性指标”（edited vs target renders）
    has_target_views = any(isinstance(it.get("target_views"), dict) for it in items)
    # 是否具备 mask：决定能否计算“编辑区域占比”等指标
    has_masks = any(isinstance(it.get("per_view_masks"), dict) for it in items) or any(_exists(it.get("edit_mask_grid")) for it in items)

    for it in items:
        row: Dict[str, Any] = {}
        row["schema"] = schema
        row["item_id"] = _item_id(schema, it)

        # Common metadata (best-effort)
        for k in ["source_model_id", "edit_id", "target_model_id", "category", "object_name", "instruction_type", "instruction_index"]:
            if k in it:
                row[k] = it.get(k)
        if schema == "samples":
            # Align naming for export schema
            row.setdefault("source_model_id", it.get("model_id"))
            row.setdefault("edit_id", it.get("edit_id"))
            row.setdefault("target_model_id", it.get("target_model_id"))
            row.setdefault("category", it.get("category"))
            row.setdefault("object_name", it.get("object_name"))

        instruction = it.get("instruction") or it.get("instruction_text") or ""
        row["instruction"] = instruction
        row["stage2_score_manifest"] = _safe_float(it.get("stage2_score"))

        # Mesh paths (best-effort, supports both schemas)
        # Triplets schema: usually has source_glb/target_glb + *_aligned.
        # Samples schema: may have source_glb/target_glb only.
        source_glb = it.get("source_glb") or (it.get("files", {}) or {}).get("source_glb")
        target_glb = it.get("target_glb") or (it.get("files", {}) or {}).get("target_glb")
        source_glb_aligned = it.get("source_glb_aligned") or (it.get("files", {}) or {}).get("source_glb_aligned")
        target_glb_aligned = it.get("target_glb_aligned") or (it.get("files", {}) or {}).get("target_glb_aligned")

        row["source_glb"] = source_glb
        row["target_glb"] = target_glb
        row["source_glb_aligned"] = source_glb_aligned
        row["target_glb_aligned"] = target_glb_aligned

        # Group keys for summary
        g_key = (row.get("category") or "", row.get("object_name") or "", str(row.get("instruction_type") or ""))
        groups[g_key].append(row["item_id"])

        # Paths
        source_views = it.get("source_views") if isinstance(it.get("source_views"), dict) else {}
        edited_views = it.get("edited_views") if isinstance(it.get("edited_views"), dict) else {}
        target_views = it.get("target_views") if isinstance(it.get("target_views"), dict) else {}
        per_view_masks = it.get("per_view_masks") if isinstance(it.get("per_view_masks"), dict) else {}

        # 缺失视图统计（只统计本次 cfg.views 指定的视角）
        for v in cfg.views:
            if not _exists(_p(source_views, v)):
                missing["source_view_missing"] += 1
                missing[f"source_view_missing_{v}"] += 1
            if not _exists(_p(edited_views, v)):
                missing["edited_view_missing"] += 1
                missing[f"edited_view_missing_{v}"] += 1
            if has_target_views and not _exists(_p(target_views, v)):
                missing["target_view_missing"] += 1
                missing[f"target_view_missing_{v}"] += 1

        # =========================
        # 指标 A：2D↔3D 一致性（Stage2 风格）
        #   edited_views[v]  vs  target_views[v]
        # 解释：衡量 target mesh 渲染是否“还原”编辑后的 2D 六视图。
        #   - LPIPS/MSE 越小越好；PSNR/SSIM 越大越好
        # =========================
        lpips_scores: Dict[str, float] = {}
        mse_scores: Dict[str, float] = {}
        psnr_scores: Dict[str, float] = {}
        ssim_scores: Dict[str, float] = {}

        if has_target_views:
            for v in cfg.views:
                a = _p(edited_views, v)
                b = _p(target_views, v)
                if not (_exists(a) and _exists(b)):
                    continue

                # Try LPIPS first
                lp = compute_lpips_if_available(
                    a, b, device=cfg.lpips_device, input_mode=cfg.lpips_input_mode, net=cfg.lpips_net
                )
                if lp is not None:
                    lpips_scores[v] = lp

                try:
                    mse, psnr = compute_mse_psnr(a, b)
                    mse_scores[v] = mse
                    psnr_scores[v] = psnr
                except Exception:
                    pass

                if cfg.compute_ssim:
                    try:
                        ssim_scores[v] = compute_ssim_gray(a, b)
                    except Exception:
                        pass

        # =========================
        # 指标 B：编辑幅度（2D 代理指标）
        #   source_views[v]  vs  edited_views[v]
        # 解释：衡量“改动强度/改动量”。不是质量好坏的充分条件，但可用于对比不同策略是否产生更明显/更集中的编辑。
        # =========================
        edit_mse_scores: Dict[str, float] = {}
        edit_psnr_scores: Dict[str, float] = {}
        for v in cfg.views:
            a = _p(source_views, v)
            b = _p(edited_views, v)
            if not (_exists(a) and _exists(b)):
                continue
            try:
                mse, psnr = compute_mse_psnr(a, b)
                edit_mse_scores[v] = mse
                edit_psnr_scores[v] = psnr
            except Exception:
                pass

        # =========================
        # 指标 C：Mask 面积比例（如果存在 per_view_masks）
        # 解释：每张 mask 白色像素占比（白=被编辑）。可用于分析编辑是否集中在局部、是否过度编辑。
        # =========================
        mask_ratio_by_view: Dict[str, float] = {}
        if has_masks and per_view_masks:
            for v in cfg.views:
                mp = per_view_masks.get(v)
                if _exists(mp):
                    try:
                        mask_ratio_by_view[v] = compute_mask_area_ratio(mp)
                    except Exception:
                        pass

        # 将每视角的细分指标写入 CSV 列，便于你做箱线图/直方图/按视角对比
        def put_view_metrics(prefix: str, d: Dict[str, float]):
            for v in cfg.views:
                row[f"{prefix}_{v}"] = d.get(v)

        put_view_metrics("tgt_lpips", lpips_scores)
        put_view_metrics("tgt_mse", mse_scores)
        put_view_metrics("tgt_psnr", psnr_scores)
        if cfg.compute_ssim:
            put_view_metrics("tgt_ssim", ssim_scores)

        put_view_metrics("edit_mse", edit_mse_scores)
        put_view_metrics("edit_psnr", edit_psnr_scores)
        put_view_metrics("mask_ratio", mask_ratio_by_view)

        # 聚合指标（mean/max/min）：论文里最常用的汇总口径
        def agg(d: Dict[str, float]) -> Tuple[Optional[float], Optional[float]]:
            if not d:
                return None, None
            xs = list(d.values())
            return float(mean(xs)), float(max(xs))

        row["tgt_lpips_mean"], row["tgt_lpips_max"] = agg(lpips_scores)
        row["tgt_mse_mean"], row["tgt_mse_max"] = agg(mse_scores)
        row["tgt_psnr_mean"], row["tgt_psnr_min"] = (None, None)
        if psnr_scores:
            row["tgt_psnr_mean"] = float(mean(psnr_scores.values()))
            row["tgt_psnr_min"] = float(min(psnr_scores.values()))
        if cfg.compute_ssim:
            row["tgt_ssim_mean"], row["tgt_ssim_min"] = (None, None)
            if ssim_scores:
                row["tgt_ssim_mean"] = float(mean(ssim_scores.values()))
                row["tgt_ssim_min"] = float(min(ssim_scores.values()))

        row["edit_mse_mean"], row["edit_mse_max"] = agg(edit_mse_scores)
        row["edit_psnr_mean"], row["edit_psnr_min"] = (None, None)
        if edit_psnr_scores:
            row["edit_psnr_mean"] = float(mean(edit_psnr_scores.values()))
            row["edit_psnr_min"] = float(min(edit_psnr_scores.values()))

        row["mask_ratio_mean"], row["mask_ratio_max"] = (None, None)
        if mask_ratio_by_view:
            row["mask_ratio_mean"] = float(mean(mask_ratio_by_view.values()))
            row["mask_ratio_max"] = float(max(mask_ratio_by_view.values()))

        # =========================
        # 指标 D：Mesh 硬指标（需要 trimesh；建议用于论文展示“数据质量/规范化收益/几何差异”）
        #   - src/tgt mesh 基础质量统计（复杂度、bbox、面积、连通性、封闭性）
        #   - 可选 Chamfer（采样点最近邻距离的对称和）：近似衡量 source↔target 的几何变化量
        # =========================
        if cfg.compute_mesh:
            # Prefer aligned meshes for comparability (SemanticAlign output).
            src_mesh_path = None
            tgt_mesh_path = None
            if cfg.prefer_aligned_mesh:
                if _exists(source_glb_aligned):
                    src_mesh_path = source_glb_aligned
                elif _exists(source_glb):
                    src_mesh_path = source_glb
                if _exists(target_glb_aligned):
                    tgt_mesh_path = target_glb_aligned
                elif _exists(target_glb):
                    tgt_mesh_path = target_glb
            else:
                if _exists(source_glb):
                    src_mesh_path = source_glb
                elif _exists(source_glb_aligned):
                    src_mesh_path = source_glb_aligned
                if _exists(target_glb):
                    tgt_mesh_path = target_glb
                elif _exists(target_glb_aligned):
                    tgt_mesh_path = target_glb_aligned

            row["mesh_src_path_used"] = src_mesh_path
            row["mesh_tgt_path_used"] = tgt_mesh_path

            # Compute basic stats for each mesh
            if src_mesh_path:
                src_stats = compute_mesh_basic_stats(src_mesh_path)
                for k, v in src_stats.items():
                    row[f"src_{k}"] = v
            if tgt_mesh_path:
                tgt_stats = compute_mesh_basic_stats(tgt_mesh_path)
                for k, v in tgt_stats.items():
                    row[f"tgt_{k}"] = v

            # Compare meshes via approximate Chamfer if both present
            if src_mesh_path and tgt_mesh_path:
                chamfer = compute_chamfer_if_available(
                    src_mesh_path, tgt_mesh_path, n_samples=cfg.chamfer_samples
                )
                for k, v in chamfer.items():
                    row[k] = v

        # Flags
        row["has_target_views"] = bool(has_target_views)
        row["has_lpips"] = bool(row.get("tgt_lpips_mean") is not None)
        row["has_masks"] = bool(bool(mask_ratio_by_view))
        row["has_mesh_eval"] = bool(cfg.compute_mesh)

        rows.append(row)

    # 输出 1：逐样本 CSV（你后续用 pandas/Excel 直接做图最方便）
    csv_path = out_dir / "per_sample.csv"
    # Stable field order: fixed prefix + dynamic view fields
    base_fields = [
        "schema",
        "item_id",
        "source_model_id",
        "edit_id",
        "target_model_id",
        "category",
        "object_name",
        "instruction_type",
        "instruction_index",
        "source_glb",
        "source_glb_aligned",
        "target_glb",
        "target_glb_aligned",
        "stage2_score_manifest",
        "has_target_views",
        "has_lpips",
        "has_masks",
        "has_mesh_eval",
        "tgt_lpips_mean",
        "tgt_lpips_max",
        "tgt_mse_mean",
        "tgt_mse_max",
        "tgt_psnr_mean",
        "tgt_psnr_min",
        "tgt_ssim_mean",
        "tgt_ssim_min",
        "edit_mse_mean",
        "edit_mse_max",
        "edit_psnr_mean",
        "edit_psnr_min",
        "mask_ratio_mean",
        "mask_ratio_max",
        "mesh_src_path_used",
        "mesh_tgt_path_used",
        "chamfer_mean",
        "chamfer_max",
        "nn_a2b_mean",
        "nn_b2a_mean",
        "instruction",
    ]
    view_fields: List[str] = []
    for prefix in ["tgt_lpips", "tgt_mse", "tgt_psnr", "tgt_ssim", "edit_mse", "edit_psnr", "mask_ratio"]:
        for v in cfg.views:
            view_fields.append(f"{prefix}_{v}")
    fieldnames = base_fields + view_fields

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # 输出 2：summary.json（汇总统计 + 分位数 + 缺失统计 + 分组计数，便于论文表格化展示）
    def collect(col: str) -> List[float]:
        xs: List[float] = []
        for r in rows:
            v = r.get(col)
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                xs.append(float(v))
        return xs

    summary: Dict[str, Any] = {
        "manifest_path": str(manifest_path),
        "schema": schema,
        "item_count": len(items),
        "views": list(cfg.views),
        "has_target_views_any": has_target_views,
        "has_masks_any": has_masks,
        "missing_counts": dict(missing),
        "group_counts": {
            "by_(category,object,instruction_type)": {
                f"{k[0]}|{k[1]}|{k[2]}": len(v) for k, v in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            }
        },
        "metrics": {},
    }

    # Add aggregate metric distributions
    metric_cols = [
        "stage2_score_manifest",
        "tgt_lpips_mean",
        "tgt_lpips_max",
        "tgt_mse_mean",
        "tgt_psnr_mean",
        "tgt_ssim_mean",
        "edit_mse_mean",
        "mask_ratio_mean",
        "chamfer_mean",
    ]
    for col in metric_cols:
        xs = collect(col)
        if not xs:
            summary["metrics"][col] = {"count": 0}
            continue
        summary["metrics"][col] = {
            "count": len(xs),
            "mean": float(mean(xs)),
            "min": float(min(xs)),
            "max": float(max(xs)),
            **_percentiles(xs, [10, 25, 50, 75, 90]),
        }

    summary_path = out_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"Written: {csv_path}")
    print(f"Written: {summary_path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate dataset metrics from a manifest")
    p.add_argument("manifest", type=str, help="Path to manifest.json")
    p.add_argument("--out-dir", type=str, default=None, help="Output directory (default: <manifest_dir>/evaluation_<name>)")
    p.add_argument("--views", type=str, default="front,back,left,right,top,bottom", help="Comma-separated views to evaluate")
    p.add_argument("--no-ssim", action="store_true", help="Disable SSIM computation")
    p.add_argument("--lpips-device", type=str, default="cuda", help="LPIPS device: cuda|cpu (used if available)")
    p.add_argument("--lpips-input-mode", type=str, default="grayscale", help="LPIPS input_mode: rgb|grayscale (matches Stage2 config)")
    p.add_argument("--lpips-net", type=str, default="alex", help="LPIPS backbone: alex|vgg|squeeze")
    p.add_argument("--mesh", action="store_true", help="Enable mesh metrics (requires trimesh for most stats)")
    p.add_argument("--chamfer-samples", type=int, default=20000, help="Surface samples per mesh for chamfer (requires trimesh; scipy recommended)")
    p.add_argument("--prefer-aligned-mesh", action="store_true", help="Prefer *_aligned.glb for mesh metrics when available")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    views = [v.strip() for v in args.views.split(",") if v.strip()]
    for v in views:
        if v not in VIEW_NAMES:
            raise ValueError(f"Unknown view name: {v}. Expected one of {VIEW_NAMES}")

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        # Derive a stable name from manifest filename
        name = manifest_path.stem
        out_dir = manifest_path.parent / f"evaluation_{name}"

    cfg = EvalConfig(
        views=views,
        lpips_device=args.lpips_device,
        lpips_input_mode=args.lpips_input_mode,
        lpips_net=args.lpips_net,
        compute_ssim=(not args.no_ssim),
        compute_mesh=bool(args.mesh),
        chamfer_samples=int(args.chamfer_samples),
        prefer_aligned_mesh=bool(args.prefer_aligned_mesh),
    )

    # 允许你在服务器任意目录执行脚本：保证能 import 到仓库里的 core.*（用于复用 LPIPS 预处理逻辑）
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in os.sys.path:
        os.sys.path.insert(0, str(repo_root))

    evaluate_manifest(manifest_path, out_dir, cfg)


if __name__ == "__main__":
    main()

