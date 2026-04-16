"""
Path utilities for the 2D-3D pipeline.

New Directory Structure (v2):
  data/assets/{asset_id}/
    ├── source/              # 源文件
    │   ├── image.png
    │   ├── caption.txt
    │   └── model_{provider}.glb
    └── edits/               # 编辑版本
        └── e{id}/
            ├── image.png
            ├── instruction.txt
            └── model_{provider}.glb
"""

import re
from pathlib import Path
from typing import Optional, List, Dict

# =============================================================================
# New Directory Structure (preferred)
# =============================================================================
DATA_DIR = Path("data")
DATA_ASSETS_DIR = DATA_DIR / "assets"
DATA_IMPORTS_DIR = DATA_DIR / "imports"
DATA_TEMP_DIR = DATA_DIR / "temp"

CACHE_DIR = Path("cache")
CACHE_UPLOADS_DIR = CACHE_DIR / "uploads"
CACHE_API_RESULTS_DIR = CACHE_DIR / "api_results"

WORKSPACE_DIR = Path("workspace")
WORKSPACE_VIS_HISTORY_DIR = WORKSPACE_DIR / "vis_history"

# =============================================================================
# Legacy Directory Structure (for backward compatibility)
# =============================================================================
IMAGE_DIR = Path("image")
THREE_D_DIR = Path("3d")

SOURCE_IMAGE_DIR = IMAGE_DIR / "source"  # Deprecated
EDITED_IMAGE_DIR = IMAGE_DIR / "edited"  # Deprecated
RAW_IMAGE_DIR = IMAGE_DIR / "raw"  # Deprecated

SOURCE_3D_DIR = THREE_D_DIR / "source"  # Deprecated
EDITED_3D_DIR = THREE_D_DIR / "edited"  # Deprecated

# =============================================================================
# Common Constants
# =============================================================================
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MODEL_EXTENSIONS = {".glb", ".gltf", ".obj", ".fbx"}

# Pattern for edited files: {name}_e{id}.ext
EDITED_PATTERN = re.compile(r"^(.+)_e([a-zA-Z0-9]+)$")


# =============================================================================
# New Asset Path Functions
# =============================================================================
def get_asset_dir(asset_id: str, base_dir: Path = Path(".")) -> Path:
    """Get the directory for an asset."""
    return base_dir / DATA_ASSETS_DIR / asset_id


def get_asset_source_dir(asset_id: str, base_dir: Path = Path(".")) -> Path:
    """Get the source directory for an asset."""
    return get_asset_dir(asset_id, base_dir) / "source"


def get_asset_edit_dir(asset_id: str, edit_id: str, base_dir: Path = Path(".")) -> Path:
    """Get the edit directory for an asset edit."""
    return get_asset_dir(asset_id, base_dir) / "edits" / edit_id


def get_asset_image_path(asset_id: str, base_dir: Path = Path("."), edit_id: str = None) -> Path:
    """Get the image path for an asset (source or edit)."""
    if edit_id:
        return get_asset_edit_dir(asset_id, edit_id, base_dir) / "image.png"
    return get_asset_source_dir(asset_id, base_dir) / "image.png"


def get_asset_model_path(asset_id: str, provider: str, base_dir: Path = Path("."), edit_id: str = None) -> Path:
    """Get the 3D model path for an asset."""
    if edit_id:
        return get_asset_edit_dir(asset_id, edit_id, base_dir) / f"model_{provider}.glb"
    return get_asset_source_dir(asset_id, base_dir) / f"model_{provider}.glb"


def list_assets(base_dir: Path = Path(".")) -> List[str]:
    """List all asset IDs in the assets directory."""
    assets_dir = base_dir / DATA_ASSETS_DIR
    if not assets_dir.exists():
        return []
    return [d.name for d in assets_dir.iterdir() if d.is_dir()]


def list_asset_edits(asset_id: str, base_dir: Path = Path(".")) -> List[str]:
    """List all edit IDs for an asset."""
    edits_dir = get_asset_dir(asset_id, base_dir) / "edits"
    if not edits_dir.exists():
        return []
    return [d.name for d in edits_dir.iterdir() if d.is_dir()]


# =============================================================================
# Legacy Functions (for backward compatibility)
# =============================================================================
def is_source_image(path: Path) -> bool:
    """Check if path is in source image directory. DEPRECATED."""
    return SOURCE_IMAGE_DIR.name in path.parts


def is_edited_image(path: Path) -> bool:
    """Check if path is in edited image directory. DEPRECATED."""
    return EDITED_IMAGE_DIR.name in path.parts


def parse_image_path(image_path: Path) -> dict:
    """
    Parse an image path to extract asset info. DEPRECATED.
    
    Returns:
        Dict with: name, variant_id (if edited), is_edited, is_source
    """
    stem = image_path.stem
    is_edited = is_edited_image(image_path)
    
    if is_edited:
        match = EDITED_PATTERN.match(stem)
        if match:
            return {
                "name": match.group(1),
                "variant_id": match.group(2),
                "is_edited": True,
                "is_source": False, 
            }
    
    return {
        "name": stem,
        "variant_id": None,
        "is_edited": False,
        "is_source": is_source_image(image_path),
    }


def get_output_3d_path(image_path: Path, model_id: str, base_dir: Path = Path(".")) -> Path:
    """
    Get the output 3D model path for an image. DEPRECATED - use get_asset_model_path instead.
    
    Args:
        image_path: Path to input image
        model_id: Short model ID (tp3, hy3, rd2)
        base_dir: Project base directory
        
    Returns:
        Path to output .glb file
    """
    info = parse_image_path(image_path)
    
    if info["is_edited"] and info["variant_id"]:
        # New structure: data/assets/{name}/edits/e{id}/model_{model}.glb
        return get_asset_model_path(info["name"], model_id, base_dir, f"e{info['variant_id']}")
    else:
        # New structure: data/assets/{name}/source/model_{model}.glb
        return get_asset_model_path(info["name"], model_id, base_dir)


def find_existing_3d(image_path: Path, model_id: str, base_dir: Path = Path(".")) -> Optional[Path]:
    """
    Check if 3D model already exists for this image and model.
    
    Returns:
        Path if exists, None otherwise
    """
    expected_path = get_output_3d_path(image_path, model_id, base_dir)
    if expected_path.exists():
        return expected_path
    return None


def collect_images(input_path: Path, base_dir: Path = Path(".")) -> List[Path]:
    """
    Collect image paths from input (single file, directory, or list file).
    
    Args:
        input_path: Path to image, directory, or .txt list file
        base_dir: Project base directory
        
    Returns:
        List of image paths
    """
    if not input_path.exists():
        # Check relative to base_dir
        abs_path = base_dir / input_path
        if abs_path.exists():
            input_path = abs_path
        else:
            return []
    
    # Single image file
    if input_path.is_file():
        if input_path.suffix.lower() == ".txt":
            # List file
            images = []
            with open(input_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        img_path = Path(line)
                        if not img_path.is_absolute():
                            img_path = base_dir / img_path
                        if img_path.exists() and img_path.suffix.lower() in IMAGE_EXTENSIONS:
                            images.append(img_path)
            return images
        elif input_path.suffix.lower() in IMAGE_EXTENSIONS:
            return [input_path]
        return []
    
    # Directory
    if input_path.is_dir():
        images = []
        for ext in IMAGE_EXTENSIONS:
            images.extend(input_path.glob(f"*{ext}"))
        return sorted(images)
    
    return []
