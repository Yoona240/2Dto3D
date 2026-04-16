#!/usr/bin/env python3
"""
Standalone bpy rendering script.

This script is designed to be run as a separate subprocess to isolate
bpy crashes from the main Flask process.

Usage:
    python bpy_render_standalone.py <glb_path> <output_dir> [options]
    
Options:
    --image-size SIZE    Output image size (default: 512)
    --samples SAMPLES    Render samples (default: 64)
    --rotation-z DEG     Z rotation in degrees (default: 0)
    --lighting MODE      Lighting mode: flat, studio, hdri (default: flat)
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    parser = argparse.ArgumentParser(description='Render GLB to multiview images using bpy')
    parser.add_argument('glb_path', help='Path to GLB file')
    parser.add_argument('output_dir', help='Output directory for rendered images')
    parser.add_argument('--image-size', type=int, default=512, help='Output image size')
    parser.add_argument('--samples', type=int, default=64, help='Render samples')
    parser.add_argument('--rotation-z', type=float, default=0, help='Z rotation in degrees')
    parser.add_argument('--lighting', default='flat', help='Lighting mode: flat, studio, hdri')
    
    args = parser.parse_args()
    
    # Import bpy (this will initialize Blender)
    try:
        import bpy
    except ImportError as e:
        print(f"Error: Failed to import bpy: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Generate and execute render script
    from core.render.blender_script import generate_2d_render_script
    
    script_content = generate_2d_render_script(
        glb_path_arg_index=0,
        output_dir_arg_index=1,
        image_size_arg=str(args.image_size),
        samples_arg=str(args.samples),
        rotation_arg=str(args.rotation_z),
        lighting_mode_arg=f"'{args.lighting}'"
    )
    
    # Setup sys.argv for the script
    old_argv = sys.argv
    sys.argv = ['blender', '--', args.glb_path, args.output_dir]
    
    try:
        exec(script_content, {'__name__': '__main__'})
        # Render completed successfully
        sys.exit(0)
    except SystemExit as e:
        # Re-raise with the same exit code
        sys.exit(e.code if e.code is not None else 0)
    except Exception as e:
        print(f"Render error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        sys.argv = old_argv


if __name__ == '__main__':
    main()
