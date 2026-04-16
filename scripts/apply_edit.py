#!/usr/bin/env python3
"""
Apply editing instruction to an image or rendered view.

CLI Usage:
    python scripts/apply_edit.py --image <image_id> --instruction "Change color to red"
    python scripts/apply_edit.py --view <model_id> <view_name> --instruction "Add glow effect"
"""

import argparse
import json
import sys
from pathlib import Path

# Setup project path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from core.image.editor import ImageEditor
from utils.config import load_config


def apply_to_image(image_id: str, instruction: str, output_path: str = None) -> str:
    """
    Apply editing instruction to a source image.
    
    Args:
        image_id: ID of the image in pipeline/images
        instruction: Editing instruction text
        output_path: Optional output path (default: creates variant)
    
    Returns:
        Path to the edited image
    """
    config = load_config()
    
    images_dir = PROJECT_ROOT / "data" / "pipeline" / "images"
    image_dir = images_dir / image_id
    image_path = image_dir / "image.png"
    
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    
    if output_path is None:
        import uuid
        variant_id = uuid.uuid4().hex[:8]
        output_dir = images_dir / f"{image_id}_v_{variant_id}"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / "image.png")
        
        # Save meta
        meta = {
            "id": f"{image_id}_v_{variant_id}",
            "parent_id": image_id,
            "instruction": instruction
        }
        with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    
    with ImageEditor(config.gemini_response) as editor:
        result = editor.edit_image(str(image_path), instruction, output_path)
    
    print(f"Edited image saved to: {result}")
    return result


def apply_to_view(model_id: str, view_name: str, instruction: str, output_path: str = None) -> str:
    """
    Apply editing instruction to a rendered view.
    
    Args:
        model_id: ID of the model in pipeline/models_src
        view_name: Name of the view (e.g., 'front', 'back')
        instruction: Editing instruction text
        output_path: Optional output path
    
    Returns:
        Path to the edited view
    """
    config = load_config()
    
    triplets_dir = PROJECT_ROOT / "data" / "pipeline" / "triplets"
    view_path = triplets_dir / model_id / "views" / f"{view_name}.png"
    
    if not view_path.exists():
        raise FileNotFoundError(f"View not found: {view_path}")
    
    if output_path is None:
        import uuid
        edit_id = uuid.uuid4().hex[:8]
        edited_dir = triplets_dir / model_id / "edited"
        edited_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(edited_dir / f"{view_name}_{edit_id}.png")
        
        # Save meta
        meta = {
            "source_view": view_name,
            "model_id": model_id,
            "instruction": instruction
        }
        meta_path = edited_dir / f"{view_name}_{edit_id}_meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    
    with ImageEditor(config.gemini_response) as editor:
        result = editor.edit_image(str(view_path), instruction, output_path)
    
    print(f"Edited view saved to: {result}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Apply editing instruction to image or view")
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--image", type=str, help="Image ID to edit")
    group.add_argument("--view", nargs=2, metavar=("MODEL_ID", "VIEW_NAME"), 
                       help="Model ID and view name to edit")
    
    parser.add_argument("--instruction", "-i", type=str, required=True,
                        help="Editing instruction")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output path (optional)")
    
    args = parser.parse_args()
    
    try:
        if args.image:
            apply_to_image(args.image, args.instruction, args.output)
        else:
            model_id, view_name = args.view
            apply_to_view(model_id, view_name, args.instruction, args.output)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
