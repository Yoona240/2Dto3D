"""
Build canonical edit artifacts for one edit batch.

Artifacts:
- before_image_grid.png
- target_image_grid.png
- front/back/right/left/top/bottom_mask.png
- edit_mask_grid.png
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Any

from PIL import Image, ImageChops, ImageFilter

from core.image.view_stitcher import VIEW_ORDER, ViewStitcher


def _require_file(path: Path, what: str) -> None:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Missing required {what}: {path}")


def _ensure_view_set(views_dir: Path, what: str) -> None:
    for view_name in VIEW_ORDER:
        _require_file(views_dir / f"{view_name}.png", f"{what} view '{view_name}'")


def _resolve_effective_after_view_paths(
    source_views_dir: Path,
    edited_dir: Path,
) -> Dict[str, Path]:
    effective_paths: Dict[str, Path] = {}
    for view_name in VIEW_ORDER:
        before_path = source_views_dir / f"{view_name}.png"
        _require_file(before_path, f"source view '{view_name}'")
        after_path = edited_dir / f"{view_name}.png"
        effective_paths[view_name] = after_path if after_path.exists() else before_path
    return effective_paths


def _stitch_views(views_dir: Path, output_path: Path) -> Path:
    stitcher = ViewStitcher()
    stitcher.stitch_views(
        views_dir=views_dir,
        output_path=output_path,
        view_names=VIEW_ORDER,
        pad_to_square=True,
    )
    return output_path


def _stitch_view_paths(view_paths: Dict[str, Path], output_path: Path) -> Path:
    stitcher = ViewStitcher()
    stitcher.stitch_view_paths(
        view_paths=view_paths,
        output_path=output_path,
        view_names=VIEW_ORDER,
        pad_to_square=True,
    )
    return output_path


def _stitch_mask_grid(per_view_mask_paths: Dict[str, Path], output_path: Path) -> Path:
    for view_name in VIEW_ORDER:
        _require_file(per_view_mask_paths[view_name], f"mask view '{view_name}'")
    return _stitch_view_paths(per_view_mask_paths, output_path)


def _materialize_target_grid(
    *,
    edited_dir: Path,
    edit_mode: str,
    editor_metadata: Dict[str, Any],
    effective_after_view_paths: Dict[str, Path],
) -> tuple[Path, str]:
    output_path = edited_dir / "target_image_grid.png"
    source = "stitched_from_effective_views"

    if edit_mode == "multiview":
        intermediate_files = editor_metadata.get("intermediate_files", {})
        edited_grid_raw = intermediate_files.get("edited_grid")
        if isinstance(edited_grid_raw, str) and edited_grid_raw.strip():
            edited_grid_path = Path(edited_grid_raw)
            _require_file(edited_grid_path, "multiview edited grid")
            with Image.open(edited_grid_path) as img:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                img.save(output_path, "PNG")
            return output_path, "multiview_direct_output"

    _stitch_view_paths(effective_after_view_paths, output_path)
    return output_path, source


def _materialize_before_grid(
    *,
    source_views_dir: Path,
    edited_dir: Path,
) -> tuple[Path, str]:
    output_path = edited_dir / "before_image_grid.png"
    _stitch_views(source_views_dir, output_path)
    return output_path, "stitched_from_source_views"


def _ensure_before_grid(
    *,
    source_views_dir: Path,
    edited_dir: Path,
) -> tuple[Path, str]:
    output_path = edited_dir / "before_image_grid.png"
    if output_path.exists() and output_path.is_file():
        return output_path, "existing_before_grid"
    return _materialize_before_grid(
        source_views_dir=source_views_dir,
        edited_dir=edited_dir,
    )


def _ensure_target_grid(
    *,
    edited_dir: Path,
    edit_mode: str,
    editor_metadata: Dict[str, Any],
    effective_after_view_paths: Dict[str, Path],
) -> tuple[Path, str]:
    output_path = edited_dir / "target_image_grid.png"
    if output_path.exists() and output_path.is_file():
        return output_path, "existing_target_grid"
    return _materialize_target_grid(
        edited_dir=edited_dir,
        edit_mode=edit_mode,
        editor_metadata=editor_metadata,
        effective_after_view_paths=effective_after_view_paths,
    )


def _compute_binary_mask(
    before_view_path: Path,
    after_view_path: Path,
    output_path: Path,
    threshold: int,
    opening_kernel_size: int,
) -> Dict[str, Any]:
    with Image.open(before_view_path) as before_img:
        before_rgb = before_img.convert("RGB")
    with Image.open(after_view_path) as after_img:
        after_rgb = after_img.convert("RGB")

    # Size alignment is explicit and deterministic for mask computation.
    if after_rgb.size != before_rgb.size:
        after_rgb = after_rgb.resize(before_rgb.size, Image.Resampling.LANCZOS)
        size_aligned = True
    else:
        size_aligned = False

    diff = ImageChops.difference(before_rgb, after_rgb)
    r, g, b = diff.split()
    max_diff = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mask = max_diff.point(lambda px: 255 if px > threshold else 0, mode="L")
    if opening_kernel_size > 1:
        mask = mask.filter(ImageFilter.MinFilter(opening_kernel_size))
        mask = mask.filter(ImageFilter.MaxFilter(opening_kernel_size))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mask.save(output_path, "PNG")

    hist = mask.histogram()
    changed_pixels = int(hist[255]) if len(hist) > 255 else 0
    total_pixels = mask.width * mask.height
    changed_ratio = (changed_pixels / total_pixels) if total_pixels > 0 else 0.0
    return {
        "changed_pixels": changed_pixels,
        "total_pixels": total_pixels,
        "changed_ratio": changed_ratio,
        "size_aligned": size_aligned,
    }


def _materialize_per_view_masks(
    *,
    source_views_dir: Path,
    effective_after_view_paths: Dict[str, Path],
    edited_dir: Path,
    threshold: int,
    opening_kernel_size: int,
) -> tuple[Dict[str, Path], Dict[str, Any]]:
    per_view_paths: Dict[str, Path] = {}
    changed_pixels_sum = 0
    total_pixels_sum = 0
    size_aligned_views = []

    for view_name in VIEW_ORDER:
        before_view_path = source_views_dir / f"{view_name}.png"
        after_view_path = effective_after_view_paths[view_name]
        _require_file(before_view_path, f"source view '{view_name}'")
        _require_file(after_view_path, f"effective after view '{view_name}'")

        mask_path = edited_dir / f"{view_name}_mask.png"
        stats = _compute_binary_mask(
            before_view_path=before_view_path,
            after_view_path=after_view_path,
            output_path=mask_path,
            threshold=threshold,
            opening_kernel_size=opening_kernel_size,
        )
        per_view_paths[view_name] = mask_path
        changed_pixels_sum += stats["changed_pixels"]
        total_pixels_sum += stats["total_pixels"]
        if stats["size_aligned"]:
            size_aligned_views.append(view_name)

    changed_ratio = (
        changed_pixels_sum / total_pixels_sum if total_pixels_sum > 0 else 0.0
    )
    return per_view_paths, {
        "changed_pixels": changed_pixels_sum,
        "changed_ratio": changed_ratio,
        "size_aligned_views": size_aligned_views,
    }


def _build_meta_artifacts(
    *,
    path_formatter: Callable[[Path], str],
    source_provider_id: str,
    before_grid_path: Path,
    before_source: str,
    target_grid_path: Path,
    target_source: str,
    per_view_mask_paths: Dict[str, Path],
    mask_grid_path: Path,
    threshold: int,
    opening_kernel_size: int,
    stats: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "before_image_grid": {
            "path": path_formatter(before_grid_path),
            "source": before_source,
            "source_provider_id": source_provider_id,
        },
        "target_image_grid": {
            "path": path_formatter(target_grid_path),
            "source": target_source,
        },
        "edit_mask": {
            "path": path_formatter(mask_grid_path),
            "primary_for_dataset": True,
            "view_paths": {
                view_name: path_formatter(per_view_mask_paths[view_name])
                for view_name in VIEW_ORDER
            },
            "encoding": "png_l_0_255",
            "semantic_values": [0, 1],
            "diff_method": "rgb_max_abs_threshold_with_opening",
            "threshold": threshold,
            "opening_kernel_size": opening_kernel_size,
            "changed_pixels": stats["changed_pixels"],
            "changed_ratio": stats["changed_ratio"],
            "size_aligned_views": stats["size_aligned_views"],
        },
    }


def build_edit_artifacts(
    *,
    model_id: str,
    source_provider_id: str,
    source_views_dir: Path,
    edited_dir: Path,
    edit_mode: str,
    editor_metadata: Dict[str, Any],
    path_formatter: Callable[[Path], str],
    diff_threshold: int,
    opening_kernel_size: int,
) -> Dict[str, Any]:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must be a non-empty string")
    if not isinstance(source_provider_id, str) or not source_provider_id.strip():
        raise ValueError("source_provider_id must be a non-empty string")
    if not isinstance(diff_threshold, int):
        raise ValueError("diff_threshold must be int")
    if diff_threshold < 0 or diff_threshold > 255:
        raise ValueError("diff_threshold must be within [0, 255]")
    if not isinstance(opening_kernel_size, int):
        raise ValueError("opening_kernel_size must be int")
    if opening_kernel_size < 1:
        raise ValueError("opening_kernel_size must be >= 1")
    if opening_kernel_size % 2 == 0:
        raise ValueError("opening_kernel_size must be odd")

    source_views_dir = Path(source_views_dir)
    edited_dir = Path(edited_dir)
    if not source_views_dir.exists():
        raise FileNotFoundError(f"source views directory not found: {source_views_dir}")
    if not edited_dir.exists():
        raise FileNotFoundError(f"edited directory not found: {edited_dir}")
    _ensure_view_set(source_views_dir, "source")

    effective_after_view_paths = _resolve_effective_after_view_paths(
        source_views_dir=source_views_dir,
        edited_dir=edited_dir,
    )

    before_grid_path = edited_dir / "before_image_grid.png"
    _stitch_views(source_views_dir, before_grid_path)
    before_source = "stitched_from_source_views"

    target_grid_path, target_source = _materialize_target_grid(
        edited_dir=edited_dir,
        edit_mode=edit_mode,
        editor_metadata=editor_metadata or {},
        effective_after_view_paths=effective_after_view_paths,
    )

    per_view_mask_paths, stats = _materialize_per_view_masks(
        source_views_dir=source_views_dir,
        effective_after_view_paths=effective_after_view_paths,
        edited_dir=edited_dir,
        threshold=diff_threshold,
        opening_kernel_size=opening_kernel_size,
    )

    mask_grid_path = edited_dir / "edit_mask_grid.png"
    _stitch_mask_grid(per_view_mask_paths, mask_grid_path)

    return {
        "before_image_grid_path": before_grid_path,
        "target_image_grid_path": target_grid_path,
        "per_view_mask_paths": per_view_mask_paths,
        "edit_mask_path": mask_grid_path,
        "meta_patch": {
            "edit_artifacts": _build_meta_artifacts(
                path_formatter=path_formatter,
                source_provider_id=source_provider_id,
                before_grid_path=before_grid_path,
                before_source=before_source,
                target_grid_path=target_grid_path,
                target_source=target_source,
                per_view_mask_paths=per_view_mask_paths,
                mask_grid_path=mask_grid_path,
                threshold=diff_threshold,
                opening_kernel_size=opening_kernel_size,
                stats=stats,
            )
        },
    }


def materialize_missing_masks(
    *,
    model_id: str,
    source_provider_id: str,
    source_views_dir: Path,
    edited_dir: Path,
    path_formatter: Callable[[Path], str],
    diff_threshold: int,
    edit_mode: str = "single",
    editor_metadata: Dict[str, Any] | None = None,
    opening_kernel_size: int = 1,
) -> Dict[str, Any]:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must be a non-empty string")
    if not isinstance(source_provider_id, str) or not source_provider_id.strip():
        raise ValueError("source_provider_id must be a non-empty string")
    if not isinstance(opening_kernel_size, int):
        raise ValueError("opening_kernel_size must be int")
    if opening_kernel_size < 1:
        raise ValueError("opening_kernel_size must be >= 1")
    if opening_kernel_size % 2 == 0:
        raise ValueError("opening_kernel_size must be odd")

    source_views_dir = Path(source_views_dir)
    edited_dir = Path(edited_dir)
    _ensure_view_set(source_views_dir, "source")

    effective_after_view_paths = _resolve_effective_after_view_paths(
        source_views_dir=source_views_dir,
        edited_dir=edited_dir,
    )
    before_grid_path, before_grid_source = _ensure_before_grid(
        source_views_dir=source_views_dir,
        edited_dir=edited_dir,
    )
    target_grid_path, target_source = _ensure_target_grid(
        edited_dir=edited_dir,
        edit_mode=edit_mode,
        editor_metadata=editor_metadata or {},
        effective_after_view_paths=effective_after_view_paths,
    )
    per_view_mask_paths, stats = _materialize_per_view_masks(
        source_views_dir=source_views_dir,
        effective_after_view_paths=effective_after_view_paths,
        edited_dir=edited_dir,
        threshold=diff_threshold,
        opening_kernel_size=opening_kernel_size,
    )
    mask_grid_path = edited_dir / "edit_mask_grid.png"
    _stitch_mask_grid(per_view_mask_paths, mask_grid_path)

    return {
        "per_view_mask_paths": per_view_mask_paths,
        "edit_mask_path": mask_grid_path,
        "meta_patch": {
            "edit_artifacts": _build_meta_artifacts(
                path_formatter=path_formatter,
                source_provider_id=source_provider_id,
                before_grid_path=before_grid_path,
                before_source=before_grid_source,
                target_grid_path=target_grid_path,
                target_source=target_source,
                per_view_mask_paths=per_view_mask_paths,
                mask_grid_path=mask_grid_path,
                threshold=diff_threshold,
                opening_kernel_size=opening_kernel_size,
                stats=stats,
            )
        },
        "recovered_artifacts": {
            "before_image_grid": before_grid_source != "existing_before_grid",
            "target_image_grid": target_source != "existing_target_grid",
        }
    }
