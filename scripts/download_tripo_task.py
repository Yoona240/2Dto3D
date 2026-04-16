#!/usr/bin/env python3
"""
Download a Tripo 3D generation task result by task ID.

Usage:
    python scripts/download_tripo_task.py <task_id> --output <output_path>
    
Example:
    /home/xiaoliang/local_envs/2d3d/bin/python scripts/download_tripo_task.py 78a58312-0004-4617-a295-7af0edfc5f15 \
        --output data/pipeline/models_src/0216736eeccf_edit_5507cf10/model_tp3.glb
            /home/xiaoliang/local_envs/2d3d/bin/python scripts/download_tripo_task.py a9eb50fa-ed1a-4491-b19b-8e9c63e21a76 \
        --output data/pipeline/models_src/0c5ec8bd33a7_edit_417e394c/model_tp3.glb
        78a58312-0004-4617-a295-7af0edfc5f15 /home/xiaoliang/2d3d_v2/data/pipeline/models_src/0216736eeccf_edit_5507cf10/model_tp3.glb
        a9eb50fa-ed1a-4491-b19b-8e9c63e21a76  /home/xiaoliang/2d3d_v2/data/pipeline/models_src/0c5ec8bd33a7_edit_417e394c/model_tp3.glb
"""

import argparse
import sys
from pathlib import Path

# Setup project path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from core.gen3d.tripo import TripoGenerator
from utils.config import load_config


def download_task(task_id: str, output_path: str):
    """Download a Tripo task result.
    
    Args:
        task_id: The Tripo task ID
        output_path: Path to save the downloaded model
    """
    config = load_config()
    
    print(f"Downloading Tripo task: {task_id}")
    print(f"Output path: {output_path}")
    
    # Initialize generator
    generator = TripoGenerator(
        config.tripo,
        config.defaults.poll_interval,
        config.defaults.max_wait_time
    )
    
    try:
        # First check task status
        result = generator.poll_status(task_id)
        print(f"Task status: {result.status.value}")
        
        if result.status.value == "failed":
            print(f"Task failed: {result.error_message}")
            return 1
            
        if not result.download_url:
            print("No download URL available yet. Task may still be processing.")
            return 1
        
        print(f"Download URL: {result.download_url[:60]}...")
        
        # Download the result
        downloaded_path = generator.download_result(task_id, output_path)
        print(f"✓ Downloaded to: {downloaded_path}")
        
        # Verify file
        file_size = Path(downloaded_path).stat().st_size
        print(f"File size: {file_size / 1024 / 1024:.2f} MB")
        
        return 0
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        generator.close()


def main():
    parser = argparse.ArgumentParser(
        description="Download a Tripo 3D generation task result"
    )
    parser.add_argument("task_id", help="The Tripo task ID",default="78a58312-0004-4617-a295-7af0edfc5f15")
    parser.add_argument(
        "--output", "-o",
        default="data/pipeline/models_src/0216736eeccf_edit_5507cf10/model_tp3.glb",
        help="Output path for the downloaded model"
    )
    
    args = parser.parse_args()
    
    return download_task(args.task_id, args.output)


if __name__ == "__main__":
    sys.exit(main())
