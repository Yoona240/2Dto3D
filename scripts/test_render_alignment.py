#!/usr/bin/env python3
"""
Test script for source-target 3D alignment rendering.

Simulates the full render alignment flow on a given source+target GLB pair,
outputs rendered images to a test directory for visual inspection.

Usage:
    python scripts/test_render_alignment.py \\
        --source-glb /path/to/source/model_hy3.glb \\
        --target-glb /path/to/target/model_hy3.glb \\
        --output-dir /path/to/output

    # Or using model IDs (looks up GLB in pipeline models_src):
    python scripts/test_render_alignment.py \\
        --source-model-id abc123 \\
        --target-model-id abc123_edit_xyz456 \\
        --provider hunyuan \\
        --output-dir /path/to/output
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import load_config
from scripts.run_render_batch import run_blender_render


def _find_glb(models_dir: Path, model_id: str, provider: str) -> Path:
    from core.gen3d import get_model_id
    provider_id = get_model_id(provider)
    glb = models_dir / model_id / f"model_{provider_id}.glb"
    if not glb.exists():
        raise FileNotFoundError(
            f"GLB not found for model_id={model_id} provider={provider}: {glb}"
        )
    return glb


def _copy_aligned_glb(
    original_glb: Path,
    aligned_glb_suffix: str,
    dest_dir: Path,
    label: str,
) -> Optional[Path]:
    """找到 aligned GLB 并拷贝到输出目录，返回目标路径或 None。"""
    aligned_name = f"{original_glb.stem}{aligned_glb_suffix}{original_glb.suffix}"
    src = original_glb.parent / aligned_name
    if not src.exists():
        print(f"  [{label}] aligned GLB 不存在（可能 save_aligned_glb=false）: {src}")
        return None
    dst = dest_dir / aligned_name
    shutil.copy2(src, dst)
    print(f"  [{label}] aligned GLB 已拷贝: {dst}")
    return dst


def _print_norm_params(norm_params_path: Path) -> None:
    if not norm_params_path.exists():
        print(f"  [!] norm_params.json not found: {norm_params_path}")
        return
    params = json.loads(norm_params_path.read_text())
    print(f"  norm_params.json @ {norm_params_path}")
    print(f"    center:           {params.get('center')}")
    print(f"    max_dim:          {params.get('max_dim')}")
    print(f"    webgl_safe_radius:{params.get('webgl_safe_radius')}")
    wc = params.get('webgl_center', {})
    print(f"    webgl_center:     x={wc.get('x'):.4f} y={wc.get('y'):.4f} z={wc.get('z'):.4f}"
          if wc else "    webgl_center:     (missing)")


def main():
    parser = argparse.ArgumentParser(
        description="Test source-target GLB render alignment"
    )
    # GLB path mode
    parser.add_argument("--source-glb", default=None, help="source GLB 文件的绝对路径")
    parser.add_argument("--target-glb", default=None, help="target GLB 文件的绝对路径")
    # Model ID mode
    parser.add_argument(
        "--source-model-id", default=None,
        help="source 模型 ID（非路径，如 abc123），从 pipeline models_src 下自动查找 GLB",
    )
    parser.add_argument(
        "--target-model-id", default=None,
        help="target 模型 ID（非路径，如 abc123_edit_xyz456）",
    )
    parser.add_argument(
        "--provider", default="hunyuan",
        choices=["tripo", "hunyuan", "rodin"],
        help="3D provider，与 --source/target-model-id 配合使用（默认 hunyuan）",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="渲染图输出目录（默认：tests/render_alignment/{source_model_id}/）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="强制重渲（忽略已有渲染图）",
    )
    args = parser.parse_args()

    # Resolve GLB paths
    config = load_config()
    pipeline_dir = Path(config.workspace.pipeline_dir)
    models_dir = pipeline_dir / "models_src"

    if args.source_glb and args.target_glb:
        source_glb = Path(args.source_glb)
        target_glb = Path(args.target_glb)
        if not source_glb.exists():
            print(f"错误：source GLB 不存在: {source_glb}", file=sys.stderr)
            sys.exit(1)
        if not target_glb.exists():
            print(f"错误：target GLB 不存在: {target_glb}", file=sys.stderr)
            sys.exit(1)
        run_label = source_glb.parent.name
    elif args.source_model_id and args.target_model_id:
        source_glb = _find_glb(models_dir, args.source_model_id, args.provider)
        target_glb = _find_glb(models_dir, args.target_model_id, args.provider)
        run_label = args.source_model_id
    else:
        parser.error(
            "请提供 --source-glb + --target-glb，"
            "或 --source-model-id + --target-model-id（+ --provider）"
        )

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = PROJECT_ROOT / "tests" / "render_alignment" / run_label
    source_out = output_dir / "source"
    target_out = output_dir / "target"

    print("=" * 60)
    print("测试：Source-Target 渲染对齐")
    print("=" * 60)
    print(f"  source GLB    : {source_glb}")
    print(f"  target GLB    : {target_glb}")
    print(f"  输出目录       : {output_dir}")
    print(f"  归一化几何     : {config.render.semantic_alignment.normalize_geometry}")
    print(f"  共享旋转到target: {config.render.semantic_alignment.share_rotation_to_target}")
    print()

    # --- 渲染 source ---
    if not args.force and source_out.exists() and any(source_out.glob("*.png")):
        print("[Source] 渲染图已存在，跳过（使用 --force 强制重渲）")
    else:
        if source_out.exists():
            shutil.rmtree(source_out)
        source_out.mkdir(parents=True, exist_ok=True)
        print("[Source] 开始渲染...")
        try:
            meta = run_blender_render(
                str(source_glb), str(source_out),
                render_config=config.render,
            )
            print(f"[Source] 完成，渲染图: {sorted(p.name for p in source_out.glob('*.png'))}")
            _copy_aligned_glb(
                source_glb,
                config.render.semantic_alignment.aligned_glb_suffix,
                source_out,
                label="Source",
            )
            norm_params_path = source_glb.parent / config.render.semantic_alignment.norm_params_filename
            _print_norm_params(norm_params_path)
        except Exception as exc:
            print(f"[Source] 失败: {exc}", file=sys.stderr)
            raise

    # --- 渲染 target ---
    if not args.force and target_out.exists() and any(target_out.glob("*.png")):
        print("[Target] 渲染图已存在，跳过（使用 --force 强制重渲）")
    else:
        if target_out.exists():
            shutil.rmtree(target_out)
        target_out.mkdir(parents=True, exist_ok=True)
        print("[Target] 开始渲染...")
        try:
            meta = run_blender_render(
                str(target_glb), str(target_out),
                render_config=config.render,
            )
            print(f"[Target] 完成，渲染图: {sorted(p.name for p in target_out.glob('*.png'))}")
            _copy_aligned_glb(
                target_glb,
                config.render.semantic_alignment.aligned_glb_suffix,
                target_out,
                label="Target",
            )
        except Exception as exc:
            print(f"[Target] 失败: {exc}", file=sys.stderr)
            raise

    # --- 汇总 ---
    print()
    print("=" * 60)
    print("结果")
    print("=" * 60)
    suffix = config.render.semantic_alignment.aligned_glb_suffix
    source_pngs = sorted(source_out.glob("*.png"))
    target_pngs = sorted(target_out.glob("*.png"))
    source_aligned = source_out / f"{source_glb.stem}{suffix}{source_glb.suffix}"
    target_aligned = target_out / f"{target_glb.stem}{suffix}{target_glb.suffix}"

    print(f"  Source 渲染图 ({len(source_pngs)} 张) : {source_out}")
    print(f"  Source aligned GLB               : {source_aligned if source_aligned.exists() else '（未找到）'}")
    print(f"  Target 渲染图 ({len(target_pngs)} 张) : {target_out}")
    print(f"  Target aligned GLB               : {target_aligned if target_aligned.exists() else '（未找到）'}")

    norm_params_path = source_glb.parent / config.render.semantic_alignment.norm_params_filename
    if norm_params_path.exists():
        print()
        print("  norm_params.json（用于 target 相机注入）:")
        _print_norm_params(norm_params_path)

    print()
    print("目视检验：对比 source/front.png 与 target/front.png。")
    print("预期效果：物体主体大小一致，仅编辑区域有差异。")


if __name__ == "__main__":
    main()
