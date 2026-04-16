"""
Blender Utility Module

Helper functions for Blender rendering.
"""

from pathlib import Path
import shutil
import os
from typing import Optional


def find_blender(config_path: Optional[str] = None) -> Optional[str]:
    """
    Find Blender executable.
    
    Priority order:
    1. config_path parameter (from config.yaml render.blender_path)
    2. BLENDER_PATH environment variable
    3. blender in PATH
    4. Common installation locations
    
    Args:
        config_path: Path from config.yaml (can be None for auto-detect)
        
    Returns:
        Path to Blender executable, or None if not found
    """
    # 1. Check config path if provided and valid
    if config_path and Path(config_path).exists():
        return config_path
    
    # 2. Check environment variable
    env_path = os.environ.get('BLENDER_PATH')
    if env_path and Path(env_path).exists():
        return env_path
    
    # 3. Check PATH
    blender_in_path = shutil.which('blender')
    if blender_in_path:
        return blender_in_path
    
    # 4. Common locations (cross-platform)
    common_paths = [
        # Linux
        "/usr/bin/blender",
        "/snap/bin/blender",
        "/usr/local/bin/blender",
        # macOS
        "/Applications/Blender.app/Contents/MacOS/Blender",
        # Windows
        r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 4.1\blender.exe",
        r"D:\APPS\blender\blender.exe",
        r"E:\app\blender\blender.exe",
    ]
    
    for path in common_paths:
        if Path(path).exists():
            return path
    
    return None


def check_bpy_available() -> bool:
    """
    Check if bpy module is available for direct Python rendering.
    
    Returns:
        True if bpy can be imported, False otherwise
    """
    try:
        import bpy  # noqa: F401
        return True
    except ImportError:
        return False


def get_render_backend(use_bpy: bool = False, blender_path: Optional[str] = None) -> tuple[str, Optional[str]]:
    """
    Determine which rendering backend to use.
    
    Args:
        use_bpy: Prefer bpy module if available
        blender_path: Configured Blender path
        
    Returns:
        Tuple of (backend_type, path)
        - ("bpy", None) if using bpy module
        - ("subprocess", blender_path) if using subprocess
        
    Raises:
        RuntimeError: If no rendering backend is available
    """
    if use_bpy and check_bpy_available():
        return ("bpy", None)
    
    # Fall back to subprocess
    found_path = find_blender(blender_path)
    if found_path:
        return ("subprocess", found_path)
    
    # If use_bpy was requested but not available, and no subprocess fallback
    if use_bpy:
        raise RuntimeError(
            "bpy module not available and Blender executable not found. "
            "Either install bpy (pip install bpy) or install Blender."
        )
    
    raise RuntimeError(
        "Blender executable not found. Please install Blender, "
        "set BLENDER_PATH environment variable, or configure render.blender_path in config.yaml"
    )
