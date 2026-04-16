#!/usr/bin/env python3
"""
generate_prompts.py - Batch Prompt Generation

Generate optimized T2I prompts from object categories.

Usage:
    python scripts/generate_prompts.py --count 10
    python scripts/generate_prompts.py --count 5 --category animals
"""

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import load_config
from core.image.prompt_optimizer import PromptOptimizer


def generate_prompts(count: int, category: str = None, output_file: str = None):
    """
    Generate optimized prompts and save to JSONL.
    
    Args:
        count: Number of prompts to generate
        category: Optional category filter
        output_file: Optional output file path
    """
    config = load_config()
    
    # Determine output path (keep consistent with web app: workspace.pipeline_dir/prompts)
    pipeline_dir = Path(config.workspace.pipeline_dir)
    if not pipeline_dir.is_absolute():
        pipeline_dir = PROJECT_ROOT / pipeline_dir
    output_dir = pipeline_dir / "prompts"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    if output_file:
        output_path = Path(output_file)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"batch_{timestamp}.jsonl"
    
    print(f"Generating {count} prompts...")
    if category:
        print(f"Category filter: {category}")
    
    generated = []
    
    with PromptOptimizer(config.qh_mllm) as optimizer:
        for i in range(count):
            # Pick random object (returns tuple: subject, category)
            # Keep behavior consistent with web API:
            # empty category means random from all categories.
            subject, obj_category = optimizer.pick_random_object(category if category else None)
            print(f"  [{i+1}/{count}] Subject: {subject} (category: {obj_category})")

            # Optimize prompt with category for style filtering
            prompt = optimizer.optimize_prompt(subject, category=obj_category)

            # Create record
            # Match app.py:/api/prompts/generate output schema.
            record = {
                "id": uuid.uuid4().hex[:12],
                "subject": subject,
                "category": obj_category,
                "prompt": prompt,
                "status": "pending",
                "created_at": datetime.now().isoformat(),
            }
            generated.append(record)
    
    # Write to JSONL
    with open(output_path, "w", encoding="utf-8") as f:
        for record in generated:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    
    print(f"\nGenerated {len(generated)} prompts")
    print(f"Output: {output_path}")
    
    return str(output_path)


def main():
    parser = argparse.ArgumentParser(description="Generate optimized T2I prompts")
    parser.add_argument("--count", "-n", type=int, default=10, help="Number of prompts")
    parser.add_argument("--category", "-c", help="Category filter (e.g., animals)")
    parser.add_argument("--output", "-o", help="Output file path")
    
    args = parser.parse_args()
    generate_prompts(args.count, args.category, args.output)


if __name__ == "__main__":
    main()
