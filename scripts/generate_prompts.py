#!/usr/bin/env python3
"""
CLI wrapper for generate_prompts in core.
"""
import sys
from pathlib import Path

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.image.generate_prompts import main

if __name__ == "__main__":
    main()
