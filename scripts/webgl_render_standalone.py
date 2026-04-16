#!/usr/bin/env python3
"""
Standalone WebGL render wrapper.

Runs the normal WebGL renderer in an isolated Python process so Playwright or
Chromium failures cannot wedge the parent scheduler process.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.webgl_render import run_webgl_render
from utils.config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Run WebGL render in isolation")
    parser.add_argument("glb_path", help="Path to GLB file")
    parser.add_argument("output_dir", help="Directory to save rendered images")
    parser.add_argument(
        "--fixed-radius", type=float, default=None,
        help="Fixed camera safe-radius (inject source value into target render)",
    )
    parser.add_argument("--fixed-center-x", type=float, default=None)
    parser.add_argument("--fixed-center-y", type=float, default=None)
    parser.add_argument("--fixed-center-z", type=float, default=None)
    parser.add_argument(
        "--output-params-file", default=None,
        help="Path to write webgl_params JSON (safeRadius+center) after source render",
    )
    args = parser.parse_args()

    # Validate fixed-center: all three components must be provided together
    center_args = [args.fixed_center_x, args.fixed_center_y, args.fixed_center_z]
    n_provided = sum(v is not None for v in center_args)
    if n_provided not in (0, 3):
        raise ValueError(
            "--fixed-center-x/y/z must all be provided together or not at all"
        )
    if args.fixed_radius is not None and n_provided == 0:
        raise ValueError("--fixed-center-x/y/z required when --fixed-radius is set")
    if n_provided == 3 and args.fixed_radius is None:
        raise ValueError("--fixed-radius required when --fixed-center-x/y/z are set")

    fixed_center = (
        {"x": args.fixed_center_x, "y": args.fixed_center_y, "z": args.fixed_center_z}
        if n_provided == 3
        else None
    )

    config = load_config()
    print(
        "[WebGL独立进程] 启动隔离渲染 "
        f"glb={args.glb_path} output_dir={args.output_dir}",
        flush=True,
    )
    run_webgl_render(
        glb_path=args.glb_path,
        output_dir=args.output_dir,
        render_config=config.render,
        fixed_radius=args.fixed_radius,
        fixed_center=fixed_center,
        output_params_file=args.output_params_file,
    )
    print("[WebGL独立进程] 渲染完成", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[WebGL独立进程] 渲染失败: {exc}", flush=True)
        raise
