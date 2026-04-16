#!/usr/bin/env python3
"""
gen_multiview_batch.py - Batch Multiview Rendering

Generate 2D multiview renders from 3D models using Blender.
Supports both subprocess (Blender executable) and bpy module modes.
"""

import argparse
import os
import signal
import sys
import subprocess
import tempfile
import json
import shutil
from datetime import datetime
import copy
import threading
import multiprocessing
from pathlib import Path
from typing import Any, Dict, Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.render.blender_script import generate_2d_render_script
from utils.config import load_config


def _rel_path(path: Path, pipeline_dir: Path) -> str:
    """Return a URL-friendly path string for meta.json."""
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        pass
    try:
        rel = str(path.relative_to(pipeline_dir)).replace("\\", "/")
        return f"pipeline/{rel}"
    except ValueError:
        pass
    return str(path).replace("\\", "/")


# bpy is not thread-safe, use a lock to serialize bpy operations
_bpy_lock = threading.Lock()

# Thread-local flag: set to True by run_full_experiment to suppress verbose
# WebGL subprocess output (only failures are printed).
# Standalone render calls leave this False so full output is preserved.
_render_tls = threading.local()


def _is_quiet_subprocess() -> bool:
    return getattr(_render_tls, "quiet_subprocess", False)


# Keywords for lines to keep when quiet_subprocess=True.
# Covers: launch params, file size, model load milestones, completion summary, errors.
_WEBGL_QUIET_KEEP = (
    "启动隔离渲染",           # subprocess entry: glb path + output dir
    "[WebGL Render] File size:",  # file size + timeout budget
    "[WebGL] model loaded",       # model load milestone
    "[WebGL] bbox",               # bounding box / dimensions
    "[WebGL] ready",              # safeRadius + center
    "Successfully saved",         # final view count summary
    "error", "Error",
    "failed", "Failed",
    "Exception", "Traceback",
)


def _filter_webgl_output(output: str) -> str:
    """Keep only key milestone lines from WebGL subprocess output."""
    return "\n".join(
        line for line in output.splitlines()
        if any(kw in line for kw in _WEBGL_QUIET_KEEP)
    )


# Disable multiprocessing - use direct bpy with thread lock instead
# Multiprocessing with spawn mode has issues finding bpy module
# and fork mode can cause issues with Flask debug mode
_USE_MULTIPROCESSING = False


def safe_print(text):
    """Safely print text handling potential console encoding issues on Windows."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        try:
            sys.stdout.buffer.write(
                text.encode(sys.stdout.encoding or "utf-8", errors="replace")
            )
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
        except Exception:
            pass


def run_blender_render(
    glb_path: str,
    output_dir: str,
    blender_path: Optional[str] = None,
    render_config=None,
):
    """
    Run rendering for a GLB file using the configured backend (Blender or WebGL).

    Args:
        glb_path: Path to GLB file
        output_dir: Output directory for rendered images
        blender_path: Explicit Blender path (overrides config, only for blender backend)
        render_config: RenderConfig object (if None, loads from config.yaml)
    """
    config = load_config()
    if render_config is None:
        render_config = config.render

    semantic_cfg = render_config.semantic_alignment
    if semantic_cfg.enabled:
        return _run_render_with_semantic_alignment(
            source_glb_path=Path(glb_path),
            output_dir=Path(output_dir),
            blender_path=blender_path,
            config=config,
            render_config=render_config,
        )

    _run_backend_once(glb_path, output_dir, blender_path, render_config)
    return None


def _run_backend_once(
    glb_path: str,
    output_dir: str,
    blender_path: Optional[str],
    render_config,
    fixed_radius: Optional[float] = None,
    fixed_center: Optional[dict] = None,
    output_params_file: Optional[str] = None,
) -> Optional[dict]:
    """Run one render pass. Returns webgl_params dict if output_params_file given, else None."""
    backend = render_config.backend
    if backend == "webgl":
        safe_print("[Render] Using WebGL backend")
        return _run_webgl_render(
            glb_path,
            output_dir,
            render_config,
            fixed_radius=fixed_radius,
            fixed_center=fixed_center,
            output_params_file=output_params_file,
        )
    elif backend == "blender":
        _run_blender_backend(glb_path, output_dir, blender_path, render_config)
        return None
    else:
        raise ValueError(f"Invalid render backend: {backend}")


def _ensure_views_complete(views_dir: Path) -> None:
    required = ["front", "back", "left", "right", "top", "bottom"]
    missing = [name for name in required if not (views_dir / f"{name}.png").exists()]
    if missing:
        raise FileNotFoundError(f"Missing required rendered views in {views_dir}: {missing}")


def _run_alignment_subprocess(
    source_glb: Path,
    target_glb: Path,
    rotation_matrix: Optional[list] = None,
    normalize: bool = False,
    norm_from: Optional[Path] = None,
    norm_center_from: Optional[Path] = None,
    norm_params_filename: str = "norm_params.json",
    draco_library_path: str = "",
) -> None:
    align_script = PROJECT_ROOT / "scripts" / "bpy_align_standalone.py"
    cmd = [
        sys.executable,
        str(align_script),
        str(source_glb),
        str(target_glb),
    ]
    if norm_from is not None:
        cmd += ["--norm-from", str(norm_from)]
    else:
        if rotation_matrix is None:
            raise ValueError("rotation_matrix required when norm_from is not specified")
        cmd += ["--rotation-matrix", json.dumps(rotation_matrix)]

    if normalize:
        cmd += ["--normalize", "--norm-params-filename", norm_params_filename]
    elif norm_from is not None:
        pass  # norm_from already added above, no extra flag needed
    elif norm_center_from is not None:
        cmd += ["--norm-center-from", str(norm_center_from)]

    safe_print(f"[SemanticAlign] Running GLB alignment subprocess: {align_script.name}")
    env = None
    if draco_library_path:
        env = os.environ.copy()
        env["BLENDER_EXTERN_DRACO_LIBRARY_PATH"] = draco_library_path
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    success_marker = "[Success] Aligned GLB exported:"
    aligned_success = success_marker in (result.stdout or "")
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                safe_print(line)

    if result.returncode != 0 and not aligned_success:
        error_lines = []
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if "| INFO:" in line or "| WARNING:" in line:
                    continue
                if line.strip():
                    error_lines.append(line)
        error_message = "\n".join(error_lines) if error_lines else (
            result.stderr.strip() if result.stderr else "unknown error"
        )
        raise RuntimeError(f"GLB alignment subprocess failed: {error_message}")

    if not target_glb.exists():
        raise FileNotFoundError(f"Aligned GLB missing after subprocess: {target_glb}")
    if target_glb.stat().st_size <= 0:
        raise ValueError(f"Aligned GLB is empty: {target_glb}")


def _load_norm_params(norm_params_path: Path, context: str) -> dict:
    """Load and validate norm_params.json. Raises loudly on any issue."""
    if not norm_params_path.exists():
        raise FileNotFoundError(
            f"[{context}] norm_params.json 不存在: {norm_params_path}。"
            "normalize_geometry=true 时必须先完成 source 渲染。"
        )
    try:
        params = json.loads(norm_params_path.read_text())
    except Exception as exc:
        raise ValueError(
            f"[{context}] norm_params.json 解析失败: {norm_params_path}"
        ) from exc
    required = ["rotation_matrix", "center", "max_dim", "webgl_safe_radius", "webgl_center"]
    missing = [k for k in required if k not in params]
    if missing:
        raise KeyError(
            f"[{context}] norm_params.json 缺少字段 {missing}: {norm_params_path}。"
            "source 渲染可能未完整完成。"
        )
    return params


# Side-view remapping: final_view <- first_pass_view for each semantic_front_from
_SIDE_REMAP = {
    "front": {"front": "front", "back": "back", "left": "left", "right": "right"},
    "left":  {"front": "left",  "back": "right", "left": "back", "right": "front"},
    "back":  {"front": "back",  "back": "front", "left": "right", "right": "left"},
    "right": {"front": "right", "back": "left",  "left": "front", "right": "back"},
}
# Top/bottom image rotation (PIL.Image.rotate angle, CCW positive)
_TB_ROTATE = {
    "front": {"top": 0,   "bottom": 0},
    "right": {"top": 90,  "bottom": 270},
    "back":  {"top": 180, "bottom": 180},
    "left":  {"top": 270, "bottom": 90},
}


def _remap_views(
    first_pass_dir: Path,
    output_dir: Path,
    semantic_front_from: str,
) -> None:
    """Remap first-pass rendered views to final views based on semantic decision.

    For side views (front/back/left/right): copy the corresponding source file.
    For top/bottom: copy and rotate the image by the appropriate angle.
    """
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)

    side_map = _SIDE_REMAP[semantic_front_from]
    tb_map = _TB_ROTATE[semantic_front_from]

    # Side views: simple file copy with rename
    for final_name, source_name in side_map.items():
        src = first_pass_dir / f"{source_name}.png"
        dst = output_dir / f"{final_name}.png"
        shutil.copy2(src, dst)

    # Top/bottom: copy with optional image rotation
    for view_name in ("top", "bottom"):
        src = first_pass_dir / f"{view_name}.png"
        dst = output_dir / f"{view_name}.png"
        angle = tb_map[view_name]
        if angle == 0:
            shutil.copy2(src, dst)
        else:
            img = Image.open(src)
            img.rotate(angle, expand=False).save(dst)

    safe_print(
        f"[SemanticAlign] Views remapped from first pass "
        f"(semantic_front_from={semantic_front_from})"
    )


def _run_render_with_semantic_alignment(
    source_glb_path: Path,
    output_dir: Path,
    blender_path: Optional[str],
    config,
    render_config,
) -> Dict[str, Any]:
    from core.render.semantic_view_aligner import SemanticViewAligner

    semantic_cfg = render_config.semantic_alignment
    if float(render_config.rotation_z) != 0.0:
        raise ValueError(
            "render.rotation_z must be 0 when render.semantic_alignment.enabled is true"
        )

    # Determine whether this is a source or target render from the directory name
    model_id = source_glb_path.parent.name
    is_target = "_edit_" in model_id
    models_dir = source_glb_path.parent.parent

    started_at = datetime.now()
    output_dir.mkdir(parents=True, exist_ok=True)
    semantic_tmp_dir = output_dir / semantic_cfg.temp_dir_name
    first_pass_dir = semantic_tmp_dir / "first_pass_views"

    if semantic_tmp_dir.exists():
        shutil.rmtree(semantic_tmp_dir)
    first_pass_dir.mkdir(parents=True, exist_ok=True)

    # --- Load norm_params for target (fail loudly before any rendering) ---
    norm_params = None
    if is_target and semantic_cfg.normalize_geometry:
        source_model_id = model_id.split("_edit_")[0]
        norm_params_path = models_dir / source_model_id / semantic_cfg.norm_params_filename
        norm_params = _load_norm_params(norm_params_path, context=model_id)

    safe_print("[SemanticAlign] Stage=first_pass_render start")
    safe_print(f"[SemanticAlign] First-pass render from source GLB: {source_glb_path}")
    _run_backend_once(
        glb_path=str(source_glb_path),
        output_dir=str(first_pass_dir),
        blender_path=blender_path,
        render_config=render_config,
    )
    _ensure_views_complete(first_pass_dir)
    safe_print("[SemanticAlign] Stage=first_pass_render done")

    aligner = SemanticViewAligner(config=config, render_config=render_config)

    # --- Rotation decision ---
    if is_target and semantic_cfg.normalize_geometry and semantic_cfg.share_rotation_to_target:
        # Reuse source rotation: skip VLM entirely
        rotation_matrix = norm_params["rotation_matrix"]
        decision = None
        safe_print(
            "[语义对齐] 阶段=语义决策 跳过"
            "（share_rotation_to_target=true，复用 source 旋转矩阵）"
        )
    else:
        safe_print("[SemanticAlign] Stage=semantic_decision start")
        decision = aligner.analyze_views(
            views_dir=first_pass_dir,
            debug_dir=semantic_tmp_dir / "decision",
        )
        safe_print(
            "[SemanticAlign] Stage=semantic_decision done "
            f"semantic_front_from={decision.semantic_front_from} "
            f"confidence={decision.confidence:.3f}"
        )
        safe_print("[SemanticAlign] Stage=compute_rotation start")
        rotation_matrix = aligner.compute_rotation_matrix(decision)
        safe_print("[SemanticAlign] Stage=compute_rotation done")

    aligned_suffix = semantic_cfg.aligned_glb_suffix
    aligned_name = f"{source_glb_path.stem}{aligned_suffix}{source_glb_path.suffix}"
    if semantic_cfg.save_aligned_glb:
        aligned_glb_path = source_glb_path.with_name(aligned_name)
    else:
        aligned_glb_path = semantic_tmp_dir / aligned_name

    # --- GLB alignment subprocess ---
    safe_print("[SemanticAlign] Stage=align_glb start")
    _draco_lib = config.workspace.draco_library_path
    if not is_target and semantic_cfg.normalize_geometry:
        # Source: rotate + center+scale normalize, save norm_params.json (geometry part)
        _run_alignment_subprocess(
            source_glb=source_glb_path,
            target_glb=aligned_glb_path,
            rotation_matrix=rotation_matrix,
            normalize=True,
            norm_params_filename=semantic_cfg.norm_params_filename,
            draco_library_path=_draco_lib,
        )
    elif is_target and semantic_cfg.normalize_geometry and semantic_cfg.share_rotation_to_target:
        # Target share_rotation=true: load rotation+center+scale all from norm_params
        _run_alignment_subprocess(
            source_glb=source_glb_path,
            target_glb=aligned_glb_path,
            norm_from=norm_params_path,
            norm_params_filename=semantic_cfg.norm_params_filename,
            draco_library_path=_draco_lib,
        )
    elif is_target and semantic_cfg.normalize_geometry:
        # Target share_rotation=false: own rotation, source center+scale
        _run_alignment_subprocess(
            source_glb=source_glb_path,
            target_glb=aligned_glb_path,
            rotation_matrix=rotation_matrix,
            norm_center_from=norm_params_path,
            norm_params_filename=semantic_cfg.norm_params_filename,
            draco_library_path=_draco_lib,
        )
    else:
        # normalize_geometry=false: original behavior (rotation only)
        _run_alignment_subprocess(
            source_glb=source_glb_path,
            target_glb=aligned_glb_path,
            rotation_matrix=rotation_matrix,
            draco_library_path=_draco_lib,
        )
    safe_print(f"[SemanticAlign] Stage=align_glb done aligned_glb={aligned_glb_path}")

    # --- Final render / remap ---
    for view_name in ["front", "back", "left", "right", "top", "bottom"]:
        old_file = output_dir / f"{view_name}.png"
        if old_file.exists():
            old_file.unlink()

    # Source with normalize_geometry: remap first-pass views instead of re-rendering
    # (skip re-render because WebGL auto-framing produces visually identical output;
    #  webgl_params are now computed by bpy_align_standalone and included in norm_params.json)
    _can_remap = (
        not is_target
        and semantic_cfg.normalize_geometry
        and decision is not None
        and decision.semantic_front_from in _SIDE_REMAP
    )
    if _can_remap:
        safe_print(
            "[SemanticAlign] Stage=final_render start "
            f"(remap from first pass, semantic_front_from={decision.semantic_front_from})"
        )
        _remap_views(first_pass_dir, output_dir, decision.semantic_front_from)
        _ensure_views_complete(output_dir)
        safe_print("[SemanticAlign] Stage=final_render done (remapped)")
    elif not is_target and semantic_cfg.normalize_geometry:
        # Fallback: re-render (e.g. semantic_front_from = "top"/"bottom")
        safe_print("[SemanticAlign] Stage=final_render start")
        safe_print(f"[SemanticAlign] Final render from aligned GLB: {aligned_glb_path}")
        import tempfile
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, prefix="webgl_params_"
        ) as tf:
            webgl_params_tmp = tf.name
        try:
            _run_backend_once(
                glb_path=str(aligned_glb_path),
                output_dir=str(output_dir),
                blender_path=blender_path,
                render_config=render_config,
                output_params_file=webgl_params_tmp,
            )
            _ensure_views_complete(output_dir)
            # Overwrite bpy-computed webgl_params with rendering-derived values
            norm_params_path_src = aligned_glb_path.parent / semantic_cfg.norm_params_filename
            if not norm_params_path_src.exists():
                raise FileNotFoundError(
                    f"bpy 对齐后 norm_params.json 不存在: {norm_params_path_src}"
                )
            existing = json.loads(norm_params_path_src.read_text())
            webgl_data = json.loads(Path(webgl_params_tmp).read_text())
            existing.update(webgl_data)
            norm_params_path_src.write_text(json.dumps(existing, indent=2))
            safe_print(
                f"[语义对齐] norm_params.json 已追加 webgl_params (fallback): {norm_params_path_src}"
            )
        finally:
            try:
                Path(webgl_params_tmp).unlink()
            except Exception:
                pass
        safe_print("[SemanticAlign] Stage=final_render done")
    elif is_target and semantic_cfg.normalize_geometry:
        # Target: inject source's webgl_params into render
        _run_backend_once(
            glb_path=str(aligned_glb_path),
            output_dir=str(output_dir),
            blender_path=blender_path,
            render_config=render_config,
            fixed_radius=norm_params["webgl_safe_radius"],
            fixed_center=norm_params["webgl_center"],
        )
        _ensure_views_complete(output_dir)
    else:
        # normalize_geometry=false: original behavior
        _run_backend_once(
            glb_path=str(aligned_glb_path),
            output_dir=str(output_dir),
            blender_path=blender_path,
            render_config=render_config,
        )
        _ensure_views_complete(output_dir)

    safe_print("[SemanticAlign] Stage=final_render done")

    verify_passed = None
    if semantic_cfg.verify_after_rerender:
        verify_debug_dir = semantic_tmp_dir / "verify"
        safe_print("[SemanticAlign] Stage=verify_final_views start")
        verify_passed = aligner.verify_final_views(
            final_views_dir=output_dir,
            debug_dir=verify_debug_dir,
        )
        safe_print(
            f"[SemanticAlign] Stage=verify_final_views done verify_passed={verify_passed}"
        )
        if not verify_passed:
            raise ValueError(
                "Semantic verify_after_rerender failed: final views are not canonical front. "
                f"See verification decision: {verify_debug_dir / 'decision.json'}"
            )

    if not semantic_cfg.save_debug_assets and semantic_tmp_dir.exists():
        safe_print("[SemanticAlign] Stage=cleanup_debug_assets start")
        shutil.rmtree(semantic_tmp_dir)
        safe_print("[SemanticAlign] Stage=cleanup_debug_assets done")

    return {
        "enabled": True,
        "vlm_model": semantic_cfg.vlm_model,
        "decision": {
            "semantic_front_from": decision.semantic_front_from if decision else None,
            "confidence": decision.confidence if decision else None,
            "reason": decision.reason if decision else "skipped (share_rotation_to_target=true)",
        },
        "rotation_matrix": rotation_matrix,
        "source_glb": str(source_glb_path),
        "aligned_glb": str(aligned_glb_path),
        "normalize_geometry": semantic_cfg.normalize_geometry,
        "is_target": is_target,
        "verify_after_rerender": semantic_cfg.verify_after_rerender,
        "verify_passed": verify_passed,
        "downstream_views_source": "final_aligned_views",
        "timestamp": datetime.now().isoformat(),
        "duration_seconds": (datetime.now() - started_at).total_seconds(),
        "backend": render_config.backend,
    }


def _run_blender_backend(
    glb_path: str,
    output_dir: str,
    blender_path: Optional[str] = None,
    render_config=None,
):
    """
    Internal function to run Blender rendering.
    Preserves all original Blender rendering logic.
    """
    from utils.blender import get_render_backend

    # Ensure render_config is not None
    if render_config is None:
        raise ValueError("render_config is required for _run_blender_backend")

    # Get render parameters from config
    image_size = render_config.image_size
    samples = render_config.blender.samples
    rotation_z = render_config.rotation_z
    lighting_mode = render_config.blender.lighting_mode

    # Determine rendering backend
    config_blender_path = blender_path or render_config.blender.blender_path

    # Validate Blender path if explicitly provided or configured
    if config_blender_path:
        path_obj = Path(config_blender_path)
        if not path_obj.exists():
            raise FileNotFoundError(
                f"Configured Blender executable not found at: {config_blender_path}"
            )

    backend_type, backend_path = get_render_backend(
        use_bpy=render_config.blender.use_bpy, blender_path=config_blender_path
    )

    if backend_type == "bpy":
        # Use bpy via subprocess to isolate crashes from main process
        safe_print("Using bpy module (subprocess isolated)")
        _run_bpy_subprocess(
            glb_path, output_dir, image_size, samples, rotation_z, lighting_mode
        )
    else:
        # Use subprocess to call Blender
        safe_print(f"Using Blender subprocess: {backend_path}")
        _run_subprocess_render(
            glb_path,
            output_dir,
            backend_path,
            image_size,
            samples,
            rotation_z,
            lighting_mode,
        )


def _run_webgl_render(
    glb_path: str,
    output_dir: str,
    render_config,
    fixed_radius: Optional[float] = None,
    fixed_center: Optional[dict] = None,
    output_params_file: Optional[str] = None,
) -> Optional[dict]:
    """
    Internal function to run WebGL rendering.
    Runs WebGL rendering in an isolated subprocess so Playwright/Chromium hangs
    or crashes cannot wedge the main experiment process.

    Returns webgl_params dict (read from output_params_file) when output_params_file
    is provided (source render), otherwise None.
    Concurrency safety: each call uses its own unique output_params_file path (caller
    responsibility) — no shared state between concurrent renders.
    """
    standalone_script = PROJECT_ROOT / "scripts" / "webgl_render_standalone.py"
    python_exe = sys.executable
    timeout_seconds = int(render_config.webgl.subprocess_timeout_seconds)
    cmd = [
        python_exe,
        str(standalone_script),
        glb_path,
        output_dir,
    ]
    if fixed_radius is not None:
        # Use fixed-point notation to avoid scientific notation (e.g. -8.74e-08).
        # Python 3.11 argparse does not recognise negative scientific notation as a
        # valid float value — it treats "-1e-08" as an unknown optional flag and
        # raises "expected one argument". Reformat only when str() produces "e"/"E".
        def _fmtf(v: float) -> str:
            s = str(v)
            return f"{v:.17f}" if ("e" in s or "E" in s) else s

        cmd += [
            "--fixed-radius", _fmtf(fixed_radius),
            "--fixed-center-x", _fmtf(fixed_center["x"]),
            "--fixed-center-y", _fmtf(fixed_center["y"]),
            "--fixed-center-z", _fmtf(fixed_center["z"]),
        ]
    if output_params_file is not None:
        cmd += ["--output-params-file", output_params_file]

    safe_print(
        "[Render][WebGL] Launching isolated subprocess "
        f"timeout={timeout_seconds}s cmd={standalone_script.name}"
    )
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    quiet = _is_quiet_subprocess()
    process = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.STDOUT if quiet else None,
        start_new_session=(os.name != "nt"),
    )
    safe_print(f"[Render][WebGL] Subprocess started pid={process.pid}")
    captured_output: str = ""
    try:
        if quiet:
            stdout_bytes, _ = process.communicate(timeout=timeout_seconds)
            captured_output = stdout_bytes.decode("utf-8", errors="replace")
            return_code = process.returncode
        else:
            return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        safe_print(
            "[Render][WebGL] Subprocess timeout exceeded; terminating process tree "
            f"pid={process.pid} timeout={timeout_seconds}s"
        )
        if quiet and captured_output:
            safe_print(f"[Render][WebGL] Captured output before timeout:\n{captured_output}")
        _terminate_subprocess_tree(process)
        raise RuntimeError(
            "WebGL render subprocess timed out after "
            f"{timeout_seconds}s for glb={glb_path}"
        ) from exc

    if return_code != 0:
        if quiet and captured_output:
            safe_print(
                f"[Render][WebGL] Subprocess failed (exit={return_code}), full output:\n"
                f"{captured_output}"
            )
        safe_print(
            f"[Render][WebGL] Subprocess finished pid={process.pid} return_code={return_code}"
        )
        raise RuntimeError(
            f"WebGL render subprocess failed with exit code {return_code} for glb={glb_path}"
        )
    if quiet and captured_output:
        filtered = _filter_webgl_output(captured_output)
        if filtered:
            safe_print(filtered)
    safe_print(f"[Render][WebGL] Subprocess completed successfully pid={process.pid}")

    # Read webgl_params written by subprocess (source render only)
    if output_params_file is not None:
        params_path = Path(output_params_file)
        if not params_path.exists():
            raise FileNotFoundError(
                f"子进程未写入 webgl_params 文件: {output_params_file}"
            )
        return json.loads(params_path.read_text())
    return None


def _terminate_subprocess_tree(process: subprocess.Popen) -> None:
    """Terminate a standalone render subprocess and its children."""
    if process.poll() is not None:
        return

    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
        return
    except Exception:
        pass

    if process.poll() is not None:
        return

    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)
    except Exception:
        pass


def _run_bpy_subprocess(
    glb_path: str,
    output_dir: str,
    image_size: int,
    samples: int,
    rotation_z: float,
    lighting_mode: str,
):
    """Run bpy rendering in a separate subprocess to isolate crashes.

    This spawns a new Python process that imports bpy and runs the render.
    If bpy crashes, only the subprocess dies, keeping Flask alive.
    """
    import sys

    # Path to standalone render script
    standalone_script = PROJECT_ROOT / "scripts" / "bpy_render_standalone.py"

    # Use the same Python interpreter as the current process
    python_exe = sys.executable

    cmd = [
        python_exe,
        str(standalone_script),
        glb_path,
        output_dir,
        "--image-size",
        str(image_size),
        "--samples",
        str(samples),
        "--rotation-z",
        str(rotation_z),
        "--lighting",
        lighting_mode,
    ]

    safe_print(f"Running: {' '.join(cmd[:3])}...")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,  # 10 minute timeout
    )

    # Print stdout (render progress)
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            safe_print(line)

    # Check if render was successful by looking for success message in stdout
    # bpy outputs INFO logs to stderr which are NOT errors
    render_success = "[Success]" in result.stdout if result.stdout else False

    # Only treat as error if returncode is non-zero AND no success message
    if result.returncode != 0 and not render_success:
        # Filter out INFO/WARNING lines from stderr, only show actual errors
        error_lines = []
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                # Skip INFO and WARNING lines from bpy/glTF importer
                if "| INFO:" in line or "| WARNING:" in line:
                    continue
                if line.strip():
                    error_lines.append(line)

        error_msg = (
            "\n".join(error_lines) if error_lines else f"Exit code {result.returncode}"
        )
        safe_print(f"Render failed: {error_msg}")
        raise RuntimeError(f"Render subprocess failed: {error_msg}")

    # Log stderr INFO messages at debug level (not as errors)
    if result.stderr:
        for line in result.stderr.strip().split("\n"):
            if "| INFO:" in line or "| WARNING:" in line:
                pass  # Silently ignore INFO/WARNING logs from bpy
            elif line.strip():
                safe_print(f"[bpy] {line}")


def _bpy_render_worker(
    glb_path: str,
    output_dir: str,
    image_size: int,
    samples: int,
    rotation_z: float,
    lighting_mode: str,
    result_queue,
):
    """Worker function that runs in a separate process to isolate bpy crashes.

    This function runs bpy rendering in an isolated process. If bpy crashes
    (segfault, etc.), only this subprocess dies, not the main Flask process.
    """
    try:
        import bpy
        import sys

        # Add project root to path for imports
        sys.path.insert(0, str(PROJECT_ROOT))
        from core.render.blender_script import generate_2d_render_script

        # Reset bpy state
        try:
            bpy.ops.wm.read_factory_settings(use_empty=True)
        except:
            pass

        # Generate and execute render script
        script_content = generate_2d_render_script(
            glb_path_arg_index=0,
            output_dir_arg_index=1,
            image_size_arg=str(image_size),
            samples_arg=str(samples),
            rotation_arg=str(rotation_z),
            lighting_mode_arg=f"'{lighting_mode}'",
        )

        old_argv = sys.argv
        sys.argv = ["blender", "--", glb_path, output_dir]
        try:
            exec(script_content, {"__name__": "__main__"})
            result_queue.put({"success": True})
        except SystemExit as e:
            if e.code != 0 and e.code is not None:
                result_queue.put(
                    {"success": False, "error": f"Script exited with code {e.code}"}
                )
            else:
                result_queue.put({"success": True})
        except Exception as e:
            result_queue.put({"success": False, "error": str(e)})
        finally:
            sys.argv = old_argv

    except Exception as e:
        result_queue.put({"success": False, "error": str(e)})


def _run_bpy_render(
    glb_path: str,
    output_dir: str,
    image_size: int,
    samples: int,
    rotation_z: float,
    lighting_mode: str,
):
    """Render using bpy module in an isolated subprocess.

    Uses multiprocessing to isolate bpy from the main process.
    If bpy crashes (segfault, OOM, etc.), only the subprocess dies,
    keeping the Flask server alive.
    """
    # Use lock to serialize renders (GPU can only handle one at a time)
    with _bpy_lock:
        if _USE_MULTIPROCESSING:
            # Use spawn context for better isolation (required for bpy)
            ctx = multiprocessing.get_context("spawn")
            result_queue = ctx.Queue()
            process = ctx.Process(
                target=_bpy_render_worker,
                args=(
                    glb_path,
                    output_dir,
                    image_size,
                    samples,
                    rotation_z,
                    lighting_mode,
                    result_queue,
                ),
            )
            process.start()

            # Wait for process to complete (timeout: 10 minutes per render)
            process.join(timeout=600)

            if process.is_alive():
                # Process timed out, kill it
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                raise RuntimeError("Render timed out after 10 minutes")

            # Check exit code
            if process.exitcode != 0:
                # Process crashed (segfault, etc.)
                raise RuntimeError(
                    f"Render process crashed with exit code {process.exitcode}"
                )

            # Get result from queue
            try:
                result = result_queue.get_nowait()
                if not result.get("success"):
                    raise RuntimeError(result.get("error", "Unknown render error"))
            except Exception as e:
                if "Empty" in str(type(e).__name__):
                    # Queue is empty but process succeeded - render was successful
                    pass
                else:
                    raise
        else:
            # Direct bpy mode (not recommended - can crash main process)
            _run_bpy_render_direct(
                glb_path, output_dir, image_size, samples, rotation_z, lighting_mode
            )


def _run_bpy_render_direct(
    glb_path: str,
    output_dir: str,
    image_size: int,
    samples: int,
    rotation_z: float,
    lighting_mode: str,
):
    """Direct bpy rendering (legacy, can crash main process)."""
    try:
        import bpy
    except ImportError:
        raise RuntimeError("bpy module not available. Install with: pip install bpy")

    # CRITICAL: Reset bpy state before each render to avoid state pollution
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    except Exception as e:
        safe_print(f"Warning: Failed to reset bpy state: {e}")

    from core.render.blender_script import generate_2d_render_script

    script_content = generate_2d_render_script(
        glb_path_arg_index=0,
        output_dir_arg_index=1,
        image_size_arg=str(image_size),
        samples_arg=str(samples),
        rotation_arg=str(rotation_z),
        lighting_mode_arg=f"'{lighting_mode}'",
    )

    import sys

    old_argv = sys.argv
    sys.argv = ["blender", "--", glb_path, output_dir]
    try:
        exec(script_content, {"__name__": "__main__"})
    except SystemExit as e:
        if e.code != 0 and e.code is not None:
            raise RuntimeError(f"Blender script exited with code {e.code}")
    except Exception as e:
        safe_print(f"Render script error: {e}")
        raise RuntimeError(f"Render failed: {e}")
    finally:
        sys.argv = old_argv
        try:
            bpy.ops.wm.read_factory_settings(use_empty=True)
            import gc

            gc.collect()
        except Exception:
            pass


def _run_subprocess_render(
    glb_path: str,
    output_dir: str,
    blender_path: str,
    image_size: int,
    samples: int,
    rotation_z: float,
    lighting_mode: str,
):
    """Render using Blender subprocess."""
    # Generate the python script content
    script_content = generate_2d_render_script(
        glb_path_arg_index=0,
        output_dir_arg_index=1,
        image_size_arg=str(image_size),
        samples_arg=str(samples),
        rotation_arg=str(rotation_z),
        lighting_mode_arg=f"'{lighting_mode}'",
    )

    # Save script to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script_content)
        script_path = f.name

    try:
        cmd = [blender_path, "-b", "-P", script_path, "--", glb_path, output_dir]

        # safe_print(f"Running Blender: {' '.join(cmd)}")

        # Run with encoding handling matches stage_render.py pattern
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )

        if result.returncode != 0:
            safe_print(f"Blender Error Code: {result.returncode}")
            # Print last few lines of stderr for debugging
            if result.stderr:
                safe_print("Blender Stderr (last 20 lines):")
                for line in result.stderr.splitlines()[-20:]:
                    safe_print(f"  {line}")
            raise RuntimeError(f"Blender rendering failed (code {result.returncode})")

        # Success - VERIFY outputs exist
        # Don't just trust exit code 0
        output_path_obj = Path(output_dir)
        generated_images = list(output_path_obj.glob("*.png"))
        if not generated_images:
            # Check for common failure patterns even with code 0
            if result.stderr and "Error" in result.stderr:
                safe_print(
                    f"Blender stderr contains errors despite code 0: {result.stderr[-500:]}"
                )
            raise RuntimeError(
                f"Blender exited successfully but no images were generated in {output_dir}. Check logs."
            )

        # safe_print("Blender render completed.")

    finally:
        # Cleanup temp script
        try:
            Path(script_path).unlink()
        except:
            pass


def process_rendering(
    target_id: str = None,
    provider: str = None,
    force: bool = False,
    backend_override: Optional[str] = None,
    lighting_mode_override: Optional[str] = None,
):
    """
    Scan models_src for GLBs and render them into triplets/[id]/views/{provider_id}/

    Args:
        target_id: Specific model ID to render (None = all models)
        provider: Provider name (tripo/hunyuan/rodin). Must be specified to select
                  which GLB file to render. Raises ValueError if not provided and
                  the model directory contains multiple GLBs.
        force: If True, re-render even if views already exist
    """
    from core.gen3d import get_model_id as _get_model_id

    config = load_config()
    render_config = copy.deepcopy(config.render)
    if backend_override is not None:
        render_config.backend = backend_override
    if lighting_mode_override is not None:
        render_config.blender.lighting_mode = lighting_mode_override

    pipeline_dir_raw = config.workspace.pipeline_dir
    pipeline_dir = (
        Path(pipeline_dir_raw)
        if Path(pipeline_dir_raw).is_absolute()
        else PROJECT_ROOT / pipeline_dir_raw
    )
    models_dir = pipeline_dir / "models_src"
    triplets_dir = pipeline_dir / "triplets"

    # Identify items to process
    items = []
    if target_id:
        items = [models_dir / target_id]
    else:
        if not models_dir.exists():
            safe_print(f"Models dir not found: {models_dir}")
            return
        items = [d for d in models_dir.iterdir() if d.is_dir()]

    for model_dir in items:
        if not model_dir.exists():
            continue

        item_id = model_dir.name

        # Select the GLB to render based on provider
        if provider:
            provider_id = _get_model_id(provider)
            glb_path = model_dir / f"model_{provider_id}.glb"
            if not glb_path.exists():
                safe_print(
                    f"Skipping {item_id}: no GLB for provider '{provider}' (model_{provider_id}.glb not found)"
                )
                continue
        else:
            # No provider specified: require exactly one GLB to avoid ambiguity
            glb_files = list(model_dir.glob("*.glb"))
            if not glb_files:
                continue
            if len(glb_files) > 1:
                raise ValueError(
                    f"Model {item_id} has multiple GLBs {[f.name for f in glb_files]}. "
                    f"Specify --provider to select which one to render."
                )
            glb_path = glb_files[0]
            # Infer provider_id from filename (e.g. model_hy3.glb -> hy3)
            provider_id = glb_path.stem.replace("model_", "")

        # Output directory: views/{provider_id}/
        output_dir = triplets_dir / item_id / "views" / provider_id
        if not force and output_dir.exists() and any(output_dir.glob("*.png")):
            safe_print(f"Skipping {item_id}/{provider_id}: Views already exist")
            continue

        safe_print(f"Processing {item_id} [{provider_id}]...")
        try:
            semantic_alignment_meta = run_blender_render(
                str(glb_path), str(output_dir), None, render_config
            )

            # Save per-provider meta inside the provider subdirectory
            triplets_dir.mkdir(parents=True, exist_ok=True)
            (triplets_dir / item_id).mkdir(exist_ok=True)
            meta = {
                "id": item_id,
                "provider_id": provider_id,
                "source_model": _rel_path(glb_path, pipeline_dir),
                "rendered_at": datetime.now().isoformat(),
            }
            if semantic_alignment_meta is not None:
                semantic_meta = dict(semantic_alignment_meta)
                semantic_meta["source_glb"] = _rel_path(
                    Path(semantic_meta["source_glb"]), pipeline_dir
                )
                semantic_meta["aligned_glb"] = _rel_path(
                    Path(semantic_meta["aligned_glb"]), pipeline_dir
                )
                meta["semantic_alignment"] = semantic_meta
            with open(output_dir / "meta.json", "w") as f:
                json.dump(meta, f, indent=2)

            safe_print(f"Successfully rendered {item_id} [{provider_id}]")

        except Exception as e:
            safe_print(f"Error rendering {item_id}: {e}")
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", help="Target model ID to render")
    parser.add_argument(
        "--provider",
        choices=["tripo", "hunyuan", "rodin"],
        help="Which provider's GLB to render (required when model has multiple GLBs)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-render even if views exist"
    )
    parser.add_argument(
        "--backend",
        choices=["blender", "webgl"],
        help="Override render backend for this run",
    )
    parser.add_argument(
        "--lighting-mode",
        choices=["emit", "ambient", "flat", "studio", "hdri"],
        help="Override Blender lighting mode for this run",
    )
    args = parser.parse_args()
    process_rendering(
        target_id=args.id,
        provider=args.provider,
        force=args.force,
        backend_override=args.backend,
        lighting_mode_override=args.lighting_mode,
    )
