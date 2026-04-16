#!/usr/bin/env python3
"""
Standalone GLB alignment script using bpy.

Applies a 3x3 rigid rotation matrix to all mesh objects, optionally performs
center+scale normalization, and exports an aligned GLB.
Runs in a subprocess for failure isolation.

Normalization modes (mutually exclusive):
  --normalize           Source mode: compute bbox, apply center+scale, save norm_params.json
  --norm-from <json>    Target mode (share_rotation=true): load rotation+center+max_dim from json
  --norm-center-from <json>  Target mode (share_rotation=false): load only center+max_dim from json;
                             rotation comes from --rotation-matrix
"""

import argparse
import json
import os
import sys
from pathlib import Path


def _parse_rotation_matrix(payload: str):
    try:
        matrix = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid --rotation-matrix JSON: {payload}") from exc

    if not isinstance(matrix, list) or len(matrix) != 3:
        raise ValueError("rotation matrix must be a 3x3 list")
    for row in matrix:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("rotation matrix must be a 3x3 list")
        for value in row:
            if not isinstance(value, (int, float)):
                raise ValueError("rotation matrix values must be numeric")
    return matrix


def _compute_bbox(mesh_objects):
    """Return (center_list, max_dim, dims_list) for all mesh objects in world space."""
    import mathutils

    min_co = [float("inf")] * 3
    max_co = [float("-inf")] * 3
    for obj in mesh_objects:
        for v in obj.data.vertices:
            world_co = obj.matrix_world @ mathutils.Vector(v.co)
            for i in range(3):
                if world_co[i] < min_co[i]:
                    min_co[i] = world_co[i]
                if world_co[i] > max_co[i]:
                    max_co[i] = world_co[i]
    center = [(min_co[i] + max_co[i]) / 2.0 for i in range(3)]
    dims = [max_co[i] - min_co[i] for i in range(3)]
    max_dim = max(dims)
    if max_dim <= 0:
        raise ValueError("Model has zero bounding box size")
    return center, max_dim, dims


def _apply_center_scale(mesh_objects, center, max_dim):
    """Translate bbox center to origin and scale to unit max-dim in world space."""
    import mathutils

    tx = mathutils.Matrix.Translation([-center[0], -center[1], -center[2]])
    scale_factor = 1.0 / max_dim
    sx = mathutils.Matrix.Scale(scale_factor, 4)
    norm = sx @ tx
    for obj in mesh_objects:
        obj.matrix_world = norm @ obj.matrix_world


def main():
    parser = argparse.ArgumentParser(description="Align GLB orientation using bpy")
    parser.add_argument("source_glb", help="Source GLB path")
    parser.add_argument("target_glb", help="Target aligned GLB path")
    parser.add_argument(
        "--rotation-matrix",
        default=None,
        help='JSON encoded 3x3 matrix, e.g. "[[1,0,0],[0,1,0],[0,0,1]]"',
    )
    # Normalization mode flags (mutually exclusive)
    norm_group = parser.add_mutually_exclusive_group()
    norm_group.add_argument(
        "--normalize",
        action="store_true",
        help="Source mode: compute bbox center+scale, apply, save norm_params.json",
    )
    norm_group.add_argument(
        "--norm-from",
        metavar="JSON_PATH",
        default=None,
        help="Target mode (share_rotation=true): load rotation+center+max_dim from norm_params.json",
    )
    norm_group.add_argument(
        "--norm-center-from",
        metavar="JSON_PATH",
        default=None,
        help="Target mode (share_rotation=false): load only center+max_dim from norm_params.json",
    )
    parser.add_argument(
        "--norm-params-filename",
        default="norm_params.json",
        help="Filename for norm_params.json (default: norm_params.json)",
    )
    args = parser.parse_args()

    # Validate: --rotation-matrix required unless --norm-from is used
    if args.norm_from is None and args.rotation_matrix is None:
        parser.error("--rotation-matrix is required unless --norm-from is specified")

    source_path = Path(args.source_glb)
    if not source_path.exists():
        raise FileNotFoundError(f"Source GLB not found: {source_path}")

    target_path = Path(args.target_glb)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import bpy
        import mathutils
    except ImportError as exc:
        raise RuntimeError("Failed to import bpy. Install with: pip install bpy") from exc

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source_path))

    mesh_objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not mesh_objects:
        raise ValueError(f"No mesh objects found in source GLB: {source_path}")

    # --- Rotation stage ---
    if args.norm_from:
        # Load everything from norm_params.json
        norm_path = Path(args.norm_from)
        if not norm_path.exists():
            raise FileNotFoundError(f"norm_params.json not found: {norm_path}")
        norm_params = json.loads(norm_path.read_text())
        rotation_matrix = norm_params["rotation_matrix"]
    else:
        rotation_matrix = _parse_rotation_matrix(args.rotation_matrix)

    rot3 = mathutils.Matrix(rotation_matrix)
    rot4 = rot3.to_4x4()
    for obj in mesh_objects:
        obj.matrix_world = rot4 @ obj.matrix_world
    bpy.context.view_layer.update()

    # --- Normalization stage ---
    if args.normalize:
        # Source mode: compute own bbox, apply, save norm_params.json
        center, max_dim, _ = _compute_bbox(mesh_objects)
        _apply_center_scale(mesh_objects, center, max_dim)
        bpy.context.view_layer.update()

        # Compute webgl_params from the normalized geometry (matches webgl_script.py computeSafeRadius)
        import math
        center_final, _, dims_final = _compute_bbox(mesh_objects)
        diagonal = math.sqrt(sum(d ** 2 for d in dims_final))
        fov_deg = 30.0  # model-viewer default FOV
        safe_radius = (diagonal / 2) / math.tan(fov_deg * math.pi / 360) * 1.2

        norm_params_path = target_path.parent / args.norm_params_filename
        norm_params = {
            "rotation_matrix": rotation_matrix,
            "center": center,
            "max_dim": max_dim,
            "source_glb": str(source_path),
            "webgl_safe_radius": safe_radius,
            "webgl_center": {
                "x": center_final[0],
                "y": center_final[1],
                "z": center_final[2],
            },
        }
        norm_params_path.write_text(json.dumps(norm_params, indent=2))
        print(f"[归一化] norm_params.json 已保存: {norm_params_path}")

    elif args.norm_from or args.norm_center_from:
        # Target mode: apply source's center+max_dim
        if args.norm_center_from:
            norm_path = Path(args.norm_center_from)
            if not norm_path.exists():
                raise FileNotFoundError(f"norm_params.json not found: {norm_path}")
            norm_params = json.loads(norm_path.read_text())

        # norm_params already loaded above (either from norm_from or norm_center_from)
        if args.norm_center_from:
            # share_rotation=false: target has its own rotation (different from source).
            # The source's "center" was computed in the source's post-rotation coordinate
            # frame and must NOT be applied to a differently-rotated target — doing so
            # shifts the geometry off-center, making the rendered object appear at the
            # top/bottom of the frame.  Compute the target's own bbox center instead,
            # and only borrow the source's max_dim for consistent scale.
            center, _, _ = _compute_bbox(mesh_objects)
            max_dim = norm_params["max_dim"]
        else:
            # share_rotation=true: rotation is shared, so source's center is valid.
            center = norm_params["center"]
            max_dim = norm_params["max_dim"]
        _apply_center_scale(mesh_objects, center, max_dim)
        bpy.context.view_layer.update()

    draco_enabled = bool(os.environ.get("BLENDER_EXTERN_DRACO_LIBRARY_PATH"))
    bpy.ops.export_scene.gltf(
        filepath=str(target_path),
        export_format="GLB",
        use_selection=False,
        export_draco_mesh_compression_enable=draco_enabled,
    )

    if not target_path.exists():
        raise FileNotFoundError(f"Aligned GLB export failed: {target_path}")
    if target_path.stat().st_size <= 0:
        raise ValueError(f"Aligned GLB is empty: {target_path}")

    print(f"[Success] Aligned GLB exported: {target_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Alignment error: {exc}", file=sys.stderr)
        sys.exit(1)
