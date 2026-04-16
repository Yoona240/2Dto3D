#!/usr/bin/env python3
"""
Render multiview images for a 3D model.

CLI Usage:
    python scripts/render_views.py <model_id>
    python scripts/render_views.py <model_id> --output <output_dir>
    python scripts/render_views.py --glb <path_to_glb> --output <output_dir>
"""

import argparse
import sys
from pathlib import Path

# Setup project path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_render_batch import run_blender_render


def render_model(model_id: str = None, glb_path: str = None, output_dir: str = None) -> list:
    """
    Render multiview images for a 3D model.
    
    Args:
        model_id: ID of the model in pipeline/models_src
        glb_path: Direct path to GLB file (alternative to model_id)
        output_dir: Output directory for rendered views
    
    Returns:
        List of rendered view paths
    """
    models_dir = PROJECT_ROOT / "data" / "pipeline" / "models_src"
    triplets_dir = PROJECT_ROOT / "data" / "pipeline" / "triplets"
    
    if glb_path:
        glb = Path(glb_path)
        if not glb.exists():
            raise FileNotFoundError(f"GLB file not found: {glb_path}")
        
        if output_dir is None:
            # Use glb stem as model_id
            output_dir = str(triplets_dir / glb.stem / "views")
    else:
        if not model_id:
            raise ValueError("Either model_id or glb_path must be provided")
        
        model_dir = models_dir / model_id
        if not model_dir.exists():
            raise FileNotFoundError(f"Model directory not found: {model_dir}")
        
        # Find GLB file
        glb_files = list(model_dir.glob("*.glb"))
        if not glb_files:
            raise FileNotFoundError(f"No GLB files found in {model_dir}")
        
        glb = glb_files[0]
        
        if output_dir is None:
            output_dir = str(triplets_dir / model_id / "views")
    
    # Create output directory
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    print(f"Rendering: {glb}")
    print(f"Output: {output_dir}")
    
    # Run blender render
    run_blender_render(str(glb), output_dir)
    
    # Return list of rendered files
    views = sorted(Path(output_dir).glob("*.png"))
    print(f"Rendered {len(views)} views:")
    for v in views:
        print(f"  - {v.name}")
    
    return [str(v) for v in views]


def main():
    parser = argparse.ArgumentParser(description="Render multiview images for a 3D model")
    
    parser.add_argument("model_id", nargs="?", type=str, 
                        help="Model ID to render")
    parser.add_argument("--glb", type=str, default=None,
                        help="Direct path to GLB file")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output directory")
    
    args = parser.parse_args()
    
    if not args.model_id and not args.glb:
        parser.error("Either model_id or --glb must be provided")
    
    try:
        render_model(args.model_id, args.glb, args.output)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
