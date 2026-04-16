#!/usr/bin/env python3
"""
Generate 3D model from an image.

CLI Usage:
    python scripts/gen3d.py <image_id>
    python scripts/gen3d.py <image_id> --provider tripo
    python scripts/gen3d.py --image <path_to_image> --output <output_dir>
"""

import argparse
import json
import math
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from PIL import Image, ImageFilter, ImageOps

# Setup project path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from core.gen3d import GENERATORS, get_model_id
from utils.config import load_config


VIEW_PAIRS: Dict[str, Tuple[str, str]] = {
    "front_back": ("front", "back"),
    "left_right": ("left", "right"),
    "top_bottom": ("top", "bottom"),
}

# =============================================================================
# TRIPO VIEW MAPPING CONFIGURATION
# =============================================================================
# Centralized configuration for mapping 6 rendered views to Tripo's 4-view input.
#
# HOW TO DEBUG:
# 1. Run: python scripts/test_tripo_view_selection.py <model_id>
# 2. Check the generated images in test_output/tripo_view_selection/
# 3. Adjust rotation angles below and re-run
#
# ROTATION ANGLE REFERENCE:
# - 0°: No rotation
# - 90°: Rotate clockwise 90°
# - 180°: Rotate 180° (upside down)
# - 270°: Rotate counter-clockwise 90° (or clockwise 270°)
#
# STRUCTURE:
# - Key: Which pair is EXCLUDED (the 2 views NOT used)
# - mapping: Which source view goes to which Tripo slot
# - rotations: Rotation angle for each slot
# - virtual_up: Which direction becomes "up" in the new orientation
# =============================================================================

TRIPO_VIEW_MAPPING_CONFIG: Dict[str, Dict] = {
    # Case 1: Exclude top/bottom (use front/back/left/right)
    # Usually when front/back views have more information
    "top_bottom": {
        "mapping": {
            "front": "front",  # front view -> front slot
            "back": "back",  # back view -> back slot
            "left": "left",  # left view -> left slot
            "right": "right",  # right view -> right slot
        },
        "rotations": {
            "front": 0,  # front slot: no rotation
            "back": 0,  # back slot: no rotation
            "left": 0,  # left slot: no rotation
            "right": 0,  # right slot: no rotation
        },
        "virtual_up": "top",  # virtual up direction
        "description": "Standard: front/back/left/right unchanged",
    },
    # Case 2: Exclude front/back (use top/bottom/left/right)
    # Usually when top/bottom views have more information (e.g., violin)
    "front_back": {
        "mapping": {
            "front": "top",  # top view -> front slot
            "back": "bottom",  # bottom view -> back slot
            "left": "left",  # left view -> left slot
            "right": "right",  # right view -> right slot
        },
        "rotations": {
            "front": 0,  # top->front: rotate 270° (top's up (+Y) -> slot's right)
            "back": 180,  # bottom->back: rotate 90° (bottom's up (-Y) -> slot's left)
            "left": 270,  # left->left: rotate 270° to stand upright
            "right": 90,  # right->right: rotate 270° to stand upright
        },
        "virtual_up": "bottom",
        "description": "Top/Bottom as front/back: left/right rotated 270°",
    },
    # Case 3: Exclude left/right (use front/back/top/bottom)
    # Usually when front/back have good info but we want to use top/bottom as sides
    "left_right": {
        "mapping": {
            "front": "front",  # front view -> front slot
            "back": "back",  # back view -> back slot
            "left": "bottom",  # bottom view -> left slot
            "right": "top",  # top view -> right slot
        },
        "rotations": {
            "front": 270,  # front->front: rotate 270°
            "back": 90,  # back->back: rotate 90°
            "left": 270,  # bottom->left: rotate 270°
            "right": 270,  # top->right: rotate 90°
        },
        "virtual_up": "left",
        "description": "Bottom/Top as left/right: all views rotated to stand upright",
    },
}


def _select_two_view_pairs(score_map: Dict[str, float]) -> Tuple[List[str], str]:
    pair_scores: List[Tuple[str, float]] = []
    for pair_name, (view_a, view_b) in VIEW_PAIRS.items():
        score = score_map.get(view_a, -1e9) + score_map.get(view_b, -1e9)
        pair_scores.append((pair_name, score))
    pair_scores.sort(key=lambda item: item[1], reverse=True)
    selected_pairs = [pair_scores[0][0], pair_scores[1][0]]
    excluded_pair = pair_scores[2][0]
    return selected_pairs, excluded_pair


def _find_best_geometric_mapping(
    score_map: Dict[str, float],
    selected_pairs: List[str],
    excluded_pair: str,
    target_slot: Optional[str] = None,
    target_source: Optional[str] = None,
) -> Tuple[Dict[str, str], str, Dict[str, int]]:
    """
    Get view mapping from centralized configuration (TRIPO_VIEW_MAPPING_CONFIG).

    To debug or modify mappings, edit TRIPO_VIEW_MAPPING_CONFIG at the top of this file.

    Args:
        score_map: Scores for each view (not used in this simplified version)
        selected_pairs: Selected view pairs (not used, kept for API compatibility)
        excluded_pair: Which pair is excluded (key to lookup in config)
        target_slot: (Optional) Not used in simplified version
        target_source: (Optional) Not used in simplified version

    Returns:
        (mapping, virtual_up, rotations) tuple
    """
    if excluded_pair not in TRIPO_VIEW_MAPPING_CONFIG:
        raise ValueError(
            f"Unknown excluded_pair: {excluded_pair}. "
            f"Valid options: {list(TRIPO_VIEW_MAPPING_CONFIG.keys())}"
        )

    config = TRIPO_VIEW_MAPPING_CONFIG[excluded_pair]
    return config["mapping"], config["virtual_up"], config["rotations"]


def _safe_parse_vlm_json(text: str) -> Optional[dict]:
    """Parse JSON from VLM response, with fallback to regex extraction."""
    if not text:
        raise ValueError("VLM returned empty response")

    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"VLM response contains no JSON object: {text[:200]}")

    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        raise ValueError(f"Failed to parse VLM JSON: {match.group(0)[:200]}")


def _vlm_judge_top_as_front(
    views_dir: Path,
    vlm_model: str,
    oneapi_config,
) -> bool:
    """
    Use VLM to determine if the top view can be considered the front face.

    Args:
        views_dir: Directory containing the original 6 rendered views
        vlm_model: VLM model name (e.g., "gemini-3-flash-preview")
        oneapi_config: OneAPI configuration for building VLM client

    Returns:
        True if top view can be considered front face, False otherwise

    Raises:
        ValueError: If VLM response is invalid
        RuntimeError: If VLM API call fails
    """
    from core.image.view_stitcher import ViewStitcher
    from utils.llm_client import get_llm_client

    stitcher = ViewStitcher()
    tmp_stitched = views_dir / "_tmp_vlm_stitched.png"

    try:
        print(f"  [VLM] Stitching 6 views from {views_dir}...")
        stitched_path, _ = stitcher.stitch_views(
            views_dir, tmp_stitched, pad_to_square=True
        )

        model_config = oneapi_config.text_models[vlm_model]
        resolved_base_url = model_config.base_url or oneapi_config.base_url
        vlm_client_config = type(
            "VLMConfig",
            (),
            {
                "api_key": oneapi_config.api_key,
                "base_url": f"{resolved_base_url}/v1",
                "default_model": vlm_model,
                "temperature": model_config.temperature,
                "max_tokens": 1000,
                "timeout": oneapi_config.timeout,
                "max_retries": oneapi_config.max_retries,
            },
        )()

        client = get_llm_client(vlm_client_config)

        system_prompt = (
            "You are a visual analyst specializing in 3D object orientation. "
            "You must respond with ONLY valid JSON, nothing else."
        )
        user_prompt = (
            "This is a 3x2 grid showing 6 views of a 3D object:\n"
            "Row 1: front, back, right\n"
            "Row 2: left, top, bottom\n\n"
            "Focus on the 'top' view (2nd image in the bottom row).\n\n"
            "Question: In everyday life, would the 'top' view of this object be considered its FRONT face? "
            "For example, the top view of a plate, a coin, a guitar, or a violin would be their front face "
            "because that's the side people normally see and identify the object from. "
            "However, for a chair, a car, or a cup, the top view is NOT the front face.\n\n"
            "Respond in JSON:\n"
            '{"top_is_front": true/false, "reason": "brief explanation"}'
        )

        print(f"  [VLM] Calling {vlm_model} to judge if top is front...")
        response = client.chat_with_images(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=[stitched_path],
            temperature=0.0,
            max_tokens=1000,
        )

        data = _safe_parse_vlm_json(response)

        if not isinstance(data.get("top_is_front"), bool):
            raise ValueError(f"VLM response missing 'top_is_front' boolean: {data}")

        result = data["top_is_front"]
        reason = data.get("reason", "")
        print(f"  [VLM] top_is_front={result}, reason: {reason}")
        return result

    finally:
        if tmp_stitched.exists():
            tmp_stitched.unlink()


def _entropy_edge_score(image_path: Path) -> float:
    with Image.open(image_path) as image:
        gray = image.convert("L")
        histogram = gray.histogram()
        total_pixels = float(gray.size[0] * gray.size[1])
        if total_pixels <= 0:
            return 0.0

        entropy = 0.0
        for count in histogram:
            if count <= 0:
                continue
            prob = count / total_pixels
            entropy -= prob * math.log2(prob)

        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_values = list(edges.getdata())
        edge_pixels = sum(1 for value in edge_values if value >= 32)
        edge_density = edge_pixels / total_pixels

    return entropy * 0.7 + edge_density * 100.0 * 0.3


def _view_transform_params(view_name: str, tripo_cfg) -> Tuple[int, bool]:
    if view_name == "top":
        return int(tripo_cfg.top_rotation), bool(tripo_cfg.top_flip_horizontal)
    if view_name == "bottom":
        return int(tripo_cfg.bottom_rotation), bool(tripo_cfg.bottom_flip_horizontal)
    return 0, False


def _materialize_view_if_needed(
    src_path: Path,
    view_name: str,
    slot_name: str,
    tripo_cfg,
    tmp_dir: Path,
    base_rotation: int = 0,
) -> Tuple[Path, Optional[Path]]:
    rotation, flip_horizontal = _view_transform_params(view_name, tripo_cfg)
    rotation = (int(base_rotation) + int(rotation)) % 360
    if rotation % 360 == 0 and not flip_horizontal:
        return src_path, None

    tmp_dir.mkdir(parents=True, exist_ok=True)
    output_path = tmp_dir / f"{slot_name}_{view_name}.png"
    with Image.open(src_path) as image:
        transformed = image.rotate(rotation, expand=False)
        if flip_horizontal:
            transformed = ImageOps.mirror(transformed)
        transformed.save(output_path)
    return output_path, output_path


def _prepare_tripo_multiview(
    edited_dir: Path,
    fallback_front: Path,
    tripo_cfg,
    tmp_dir: Path,
    config,
    views_dir: Optional[Path] = None,
) -> Tuple[Path, List[Dict[str, str]], Dict[str, object], List[Path]]:
    available: Dict[str, Path] = {}
    for name in ["front", "left", "back", "right", "top", "bottom"]:
        view_path = edited_dir / f"{name}.png"
        if view_path.exists():
            available[name] = view_path

    if not available:
        raise FileNotFoundError(f"No edited views found in: {edited_dir}")

    score_map = {name: _entropy_edge_score(path) for name, path in available.items()}
    strategy = tripo_cfg.multiview_strategy

    slot_to_view: Dict[str, Optional[str]] = {
        "front": None,
        "left": None,
        "back": None,
        "right": None,
    }
    created_tmp_files: List[Path] = []
    virtual_up: Optional[str] = None
    base_rotations: Dict[str, int] = {"front": 0, "left": 0, "back": 0, "right": 0}

    enable_view_selection = getattr(tripo_cfg, "enable_view_selection", False)

    if not enable_view_selection:
        for slot in ["front", "left", "back", "right"]:
            if slot in available:
                slot_to_view[slot] = slot
        if not slot_to_view.get("front"):
            slot_to_view["front"] = max(score_map.items(), key=lambda item: item[1])[0]
        virtual_up = "top"
        base_rotations = {"front": 0, "left": 0, "back": 0, "right": 0}
    elif strategy == "entropy_edge":
        front_score = score_map.get("front", -1e9)
        top_score = score_map.get("top", -1e9)
        entropy_diff = top_score - front_score

        use_top_as_front = False
        entropy_threshold = float(tripo_cfg.entropy_diff_threshold)

        if entropy_diff > entropy_threshold:
            if views_dir and views_dir.exists():
                vlm_model = str(tripo_cfg.view_selection_vlm_model)
                print(
                    f"  [ViewSelection] entropy_diff={entropy_diff:.2f} > threshold={entropy_threshold}"
                )
                use_top_as_front = _vlm_judge_top_as_front(
                    views_dir=views_dir,
                    vlm_model=vlm_model,
                    oneapi_config=config.oneapi,
                )
                print(
                    f"  [ViewSelection] VLM verdict: use_top_as_front={use_top_as_front}"
                )
            else:
                print(
                    f"  [ViewSelection] WARNING: views_dir not found or not provided: {views_dir}"
                )
        else:
            print(
                f"  [ViewSelection] entropy_diff={entropy_diff:.2f} <= threshold={entropy_threshold}, keeping front"
            )

        if use_top_as_front:
            excluded_pair = "front_back"
        else:
            excluded_pair = "top_bottom"

        mapping, virtual_up, computed_rotations = _find_best_geometric_mapping(
            score_map, [], excluded_pair, None, None
        )

        for slot_name in ["front", "left", "back", "right"]:
            slot_to_view[slot_name] = mapping[slot_name]
            base_rotations[slot_name] = computed_rotations[slot_name]

        if not virtual_up:
            raise ValueError("virtual_up is required for entropy_edge strategy")
    else:
        raise ValueError(
            f"Invalid tripo.multiview_strategy: {strategy}. Expected one of legacy/entropy_edge"
        )

    front_view_name = slot_to_view.get("front")
    if not front_view_name:
        raise ValueError("Tripo multiview requires a non-empty front slot")

    front_source_path = available.get(front_view_name, fallback_front)
    front_final_path, tmp_file = _materialize_view_if_needed(
        front_source_path,
        front_view_name,
        "front",
        tripo_cfg,
        tmp_dir,
        base_rotation=base_rotations.get("front", 0),
    )
    if tmp_file:
        created_tmp_files.append(tmp_file)

    additional_views: List[Dict[str, str]] = []
    for side_slot in ["left", "back", "right"]:
        side_view_name = slot_to_view.get(side_slot)
        if not side_view_name:
            continue
        side_src_path = available[side_view_name]
        side_final_path, side_tmp_file = _materialize_view_if_needed(
            side_src_path,
            side_view_name,
            side_slot,
            tripo_cfg,
            tmp_dir,
            base_rotation=base_rotations.get(side_slot, 0),
        )
        if side_tmp_file:
            created_tmp_files.append(side_tmp_file)
        additional_views.append({"path": str(side_final_path), "view": side_slot})

    total_inputs = 1 + len(additional_views)
    if total_inputs < 2:
        raise ValueError(
            "Tripo multiview requires at least 2 images (front + at least one side view)"
        )

    selection_meta = {
        "strategy": strategy,
        "score_by_view": {name: round(score, 4) for name, score in score_map.items()},
        "slot_assignment": slot_to_view,
        "virtual_up": virtual_up,
        "slot_rotation": base_rotations,
        "input_count": total_inputs,
    }
    return front_final_path, additional_views, selection_meta, created_tmp_files


def _extract_tripo_seeds_from_meta(meta_path: Path) -> Optional[Tuple[int, int]]:
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as file:
            meta = json.load(file)
        tripo_meta = meta.get("generation_params", {}).get("tripo", {})
        model_seed = tripo_meta.get("model_seed")
        texture_seed = tripo_meta.get("texture_seed")
        if isinstance(model_seed, int) and isinstance(texture_seed, int):
            return model_seed, texture_seed
    except Exception:
        return None
    return None


def _resolve_tripo_task_options(
    effective_image_id: str,
    is_edited_view: bool,
    source_model_id: Optional[str],
    models_dir: Path,
    tripo_cfg,
) -> Tuple[Dict[str, int], str]:
    if is_edited_view and source_model_id:
        source_meta_path = models_dir / source_model_id / "meta.json"
        source_seeds = _extract_tripo_seeds_from_meta(source_meta_path)
        if source_seeds:
            model_seed, texture_seed = source_seeds
            return {
                "model_seed": model_seed,
                "texture_seed": texture_seed,
            }, "source_meta"
    model_seed = tripo_cfg.model_seed
    texture_seed = tripo_cfg.texture_seed
    if not isinstance(model_seed, int) or not isinstance(texture_seed, int):
        raise ValueError(
            "tripo.model_seed and tripo.texture_seed must be fixed integers in config.yaml"
        )
    return {"model_seed": model_seed, "texture_seed": texture_seed}, "config"


def generate_3d(
    image_id: str = None,
    image_path: str = None,
    provider: str = "tripo",
    output_dir: str = None,
    skip_existing: bool = False,
) -> str:
    """
    Generate 3D model from an image.

    Supports two input modes:
    1. Source image: image_id like "abc123" -> uses images/{id}/image.png
    2. Edited views: image_id like "abc123_edit_xyz789" -> uses triplets/{id}/edited/{edit_id}/front.png

    Args:
        image_id: ID of the image in pipeline/images, or {model_id}_edit_{edit_id} for edited views
        image_path: Direct path to image file (alternative to image_id)
        provider: 3D generation provider (tripo, hunyuan, rodin)
        output_dir: Output directory for the model
        skip_existing: Skip if a .glb file already exists in output_dir

    Returns:
        Path to the generated GLB file
    """
    config = load_config()

    pipeline_dir_raw = config.workspace.pipeline_dir
    pipeline_dir = (
        Path(pipeline_dir_raw)
        if Path(pipeline_dir_raw).is_absolute()
        else PROJECT_ROOT / pipeline_dir_raw
    )
    images_dir = pipeline_dir / "images"
    models_dir = pipeline_dir / "models_src"
    triplets_dir = pipeline_dir / "triplets"

    def _rel_path(p: Path) -> str:
        """Return URL-friendly path for meta.json."""
        try:
            return str(p.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:
            pass
        try:
            rel = str(p.relative_to(pipeline_dir)).replace("\\", "/")
            return f"pipeline/{rel}"
        except ValueError:
            pass
        return str(p).replace("\\", "/")

    # Check if this is an edited view ID (format: {model_id}_edit_{edit_id})
    is_edited_view = False
    source_model_id = None
    edit_id = None
    edited_dir: Optional[Path] = None
    multi_view_images: List[Dict[str, str]] = []
    tripo_selection_meta: Dict[str, object] = {}
    tripo_task_options: Dict[str, int] = {}
    tripo_seed_origin: Optional[str] = None
    temp_files_to_cleanup: List[Path] = []

    if image_id and "_edit_" in image_id:
        parts = image_id.rsplit("_edit_", 1)
        if len(parts) == 2:
            source_model_id, edit_id = parts
            is_edited_view = True

    if image_path:
        img = Path(image_path)
        if not img.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        if output_dir is None:
            # Use image stem as model_id
            output_dir = str(models_dir / img.stem)

        effective_image_id = img.stem
    elif is_edited_view:
        # Use front.png from edited views as the source image
        edited_dir = triplets_dir / source_model_id / "edited" / edit_id
        img = edited_dir / "front.png"
        if not img.exists():
            # Try other views as fallback
            for view_name in ["back", "right", "left", "top", "bottom"]:
                alt_img = edited_dir / f"{view_name}.png"
                if alt_img.exists():
                    img = alt_img
                    break

        if not img.exists():
            raise FileNotFoundError(f"No edited view found in: {edited_dir}")

        if output_dir is None:
            # Save as {source_model_id}_edit_{edit_id}
            output_dir = str(models_dir / image_id)

        effective_image_id = image_id
    else:
        if not image_id:
            raise ValueError("Either image_id or image_path must be provided")

        img = images_dir / image_id / "image.png"
        if not img.exists():
            raise FileNotFoundError(f"Image not found: {img}")

        if output_dir is None:
            output_dir = str(models_dir / image_id)

        effective_image_id = image_id

    # Check if already has 3D model for this specific provider
    output_path = Path(output_dir)
    model_short_id = get_model_id(provider)
    target_glb = output_path / f"model_{model_short_id}.glb"

    if skip_existing and target_glb.exists():
        print(
            f"Skipping {effective_image_id}: already has {provider} 3D model ({target_glb.name})"
        )
        return str(target_glb)

    # Create output directory
    output_path.mkdir(parents=True, exist_ok=True)

    glb_path = target_glb

    print(f"Generating 3D model...")
    print(f"  Provider: {provider}")
    print(f"  Image: {img}")
    print(f"  Output: {glb_path}")

    # Get generator
    generator_class = GENERATORS.get(provider)
    if not generator_class:
        raise ValueError(
            f"Unknown provider: {provider}. Available: {list(GENERATORS.keys())}"
        )

    # Initialize generator with appropriate config
    provider_config = None
    if provider == "tripo":
        provider_config = config.tripo
        generator = generator_class(
            config.tripo, config.defaults.poll_interval, config.defaults.max_wait_time
        )
    elif provider == "hunyuan":
        provider_config = config.hunyuan
        generator = generator_class(
            config.hunyuan, config.defaults.poll_interval, config.defaults.max_wait_time
        )
    elif provider == "rodin":
        provider_config = config.rodin
        generator = generator_class(
            config.rodin, config.defaults.poll_interval, config.defaults.max_wait_time
        )
    else:
        raise ValueError(f"Provider {provider} not configured")

    if provider == "tripo":
        tripo_task_options, tripo_seed_origin = _resolve_tripo_task_options(
            effective_image_id=effective_image_id,
            is_edited_view=is_edited_view,
            source_model_id=source_model_id,
            models_dir=models_dir,
            tripo_cfg=config.tripo,
        )
        if hasattr(generator, "set_task_options"):
            generator.set_task_options(tripo_task_options)

        if is_edited_view and edited_dir:
            tmp_dir = output_path / "_tmp_tripo_views"
            views_dir = (
                triplets_dir / source_model_id / "views" if source_model_id else None
            )
            img, multi_view_images, tripo_selection_meta, temp_files_to_cleanup = (
                _prepare_tripo_multiview(
                    edited_dir=edited_dir,
                    fallback_front=img,
                    tripo_cfg=config.tripo,
                    tmp_dir=tmp_dir,
                    config=config,
                    views_dir=views_dir,
                )
            )
            if multi_view_images:
                print(
                    f"  Found {len(multi_view_images)} additional views: {[v['view'] for v in multi_view_images]}"
                )
                print(
                    f"  Tripo multiview slot assignment: {tripo_selection_meta.get('slot_assignment', {})}"
                )
    elif is_edited_view and edited_dir:
        # For non-Tripo providers (Hunyuan, Rodin), collect all 6 views
        for view_name in ["back", "left", "right", "top", "bottom"]:
            view_path = edited_dir / f"{view_name}.png"
            if view_path.exists():
                multi_view_images.append({"path": str(view_path), "view": view_name})
        if multi_view_images:
            print(
                f"  Found {len(multi_view_images)} additional views: {[v['view'] for v in multi_view_images]}"
            )

    multi_view_param = (
        multi_view_images if is_edited_view and multi_view_images else None
    )
    try:
        result = generator.generate(
            str(img), str(glb_path), multi_view_images=multi_view_param
        )
    finally:
        for file_path in temp_files_to_cleanup:
            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception:
                pass
        if temp_files_to_cleanup:
            tmp_dir = output_path / "_tmp_tripo_views"
            try:
                tmp_dir.rmdir()
            except Exception:
                pass

    if result.status.value != "completed":
        raise Exception(f"Generation failed: {result.error_message}")

    # Save meta (use relative path for source_image)
    source_image_rel = _rel_path(img)

    # Capture config snapshot
    config_snapshot = asdict(provider_config) if provider_config else {}
    # Mask API key in snapshot
    if "api_key" in config_snapshot:
        config_snapshot["api_key"] = "***"

    generation_params: Dict[str, object] = {}
    if provider == "tripo":
        generation_params["tripo"] = {
            "model_seed": tripo_task_options.get("model_seed"),
            "texture_seed": tripo_task_options.get("texture_seed"),
            "seed_origin": tripo_seed_origin,
        }
        if tripo_selection_meta:
            generation_params["multiview_selection"] = tripo_selection_meta

    meta = {
        "id": effective_image_id,
        "provider": provider,
        "model_id": model_short_id,
        "source_image": source_image_rel,
        "remote_task_id": result.remote_task_id,
        "config_snapshot": config_snapshot,
        "generation_params": generation_params,
        "generated_at": datetime.now().isoformat(),
    }
    with open(output_path / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"3D model generated: {result.output_path}")
    return str(result.output_path)


def main():
    parser = argparse.ArgumentParser(description="Generate 3D model from image")

    parser.add_argument(
        "image_id", nargs="?", type=str, help="Image ID to convert to 3D"
    )
    parser.add_argument(
        "--image", type=str, default=None, help="Direct path to image file"
    )
    parser.add_argument(
        "--provider",
        "-p",
        type=str,
        default="tripo",
        choices=["tripo", "hunyuan", "rodin"],
        help="3D generation provider",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None, help="Output directory"
    )
    parser.add_argument(
        "--skip-existing",
        "-s",
        action="store_true",
        help="Skip if 3D model already exists",
    )

    args = parser.parse_args()

    if not args.image_id and not args.image:
        parser.error("Either image_id or --image must be provided")

    try:
        generate_3d(
            args.image_id, args.image, args.provider, args.output, args.skip_existing
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
