"""
Prompt Optimizer Module

Uses MLLM (via QnMllmConfig) to expand simple object names into rich,
3D-optimized descriptions following the project's style guidelines.

Now uses unified LLM client from utils/llm_client.py.
Prompt templates are centralized in utils/prompts.py.
"""

import json
import random
from pathlib import Path
from typing import Dict, Optional

from utils.config import QnMllmConfig
from utils.llm_client import get_llm_client, OpenAICompatibleClient
from utils.logger import get_prompt_logger
from utils.prompts import (
    get_object_description_prompt,
    get_image_requirements_prompt,
    compose_t2i_prompt,
    get_fallback_object_description,
)

# 模块级 logger
_logger = get_prompt_logger()

# Project root for resolving relative paths
_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _resolve_data_path(raw_path: str) -> Path:
    """Resolve a data file path: absolute paths are used directly, relative
    paths are resolved against the project root."""
    p = Path(raw_path)
    return p if p.is_absolute() else _PROJECT_ROOT / p


def _get_config_data_paths() -> tuple:
    """Read objects/styles file paths from config.  Returns (objects_path, styles_path)."""
    from utils.config import load_config
    config = load_config()
    return (
        _resolve_data_path(config.workspace.matrix_objects_file),
        _resolve_data_path(config.workspace.matrix_styles_file),
    )


# Legacy module-level constants — kept for backward compatibility with
# one-off scripts in tests/ that may still import them.
_DATA_DIR = _PROJECT_ROOT / "data"
OBJECTS_FILE = _DATA_DIR / "captions" / "categorized_objects.json"
STYLES_FILE = _DATA_DIR / "captions" / "3d_style.json"


class PromptOptimizer:
    """
    Optimizes and expands prompts for 3D-oriented image generation.
    """

    def __init__(self, config: QnMllmConfig, objects_file: Optional[Path] = None, styles_file: Optional[Path] = None):
        """
        Initialize the prompt optimizer.

        Args:
            config: QnMllmConfig configuration for text generation
            objects_file: Path to categorized objects JSON (default: from config.yaml)
            styles_file: Path to 3D styles JSON (default: from config.yaml)
        """
        self.config = config
        self.client: OpenAICompatibleClient = get_llm_client(config)
        self._objects_cache = None
        self._styles_cache = None
        if objects_file is not None and styles_file is not None:
            self._objects_file = Path(objects_file)
            self._styles_file = Path(styles_file)
        else:
            self._objects_file, self._styles_file = _get_config_data_paths()

    def _load_objects(self) -> dict:
        """Load the objects dataset."""
        if self._objects_cache is None:
            if not self._objects_file.exists():
                raise FileNotFoundError(f"Objects data not found at {self._objects_file}")

            with open(self._objects_file, 'r', encoding='utf-8') as f:
                self._objects_cache = json.load(f)
        return self._objects_cache

    def _load_styles(self) -> dict:
        """Load the 3D styles dataset."""
        if self._styles_cache is None:
            if not self._styles_file.exists():
                raise FileNotFoundError(f"Styles data not found at {self._styles_file}")

            with open(self._styles_file, 'r', encoding='utf-8') as f:
                self._styles_cache = json.load(f)
        return self._styles_cache

    def pick_random_object(self, category: Optional[str] = None) -> tuple:
        """
        Pick a random object from the dataset.
        
        Args:
            category: Optional specific category (e.g., 'Furniture')
            
        Returns:
            Tuple of (object_name, category_name)
        """
        data = self._load_objects()
        
        if category and category in data:
            choices = data[category]
            chosen_category = category
        else:
            # Pick random category first, then random object
            categories = list(data.keys())
            if not categories:
                raise ValueError("No categories found in objects data")
            chosen_category = random.choice(categories)
            choices = data[chosen_category]

        if not choices:
            raise ValueError(f"No objects found in category: {chosen_category}")

        return (random.choice(choices), chosen_category)

    def pick_random_style(self, category: Optional[str] = None) -> dict:
        """
        Pick a random style from the styles dataset, filtered by category.

        Args:
            category: Optional category name to filter applicable styles

        Returns:
            A style dict with keys: id, name_zh, name_en, prefix
        """
        data = self._load_styles()
        styles = data.get("styles", []) if isinstance(data, dict) else []
        mapping = data.get("category_style_mapping", {}) if isinstance(data, dict) else {}
        
        if not styles:
            return {
                "id": "realistic",
                "name_zh": "写实",
                "name_en": "realistic",
                "prefix": "Photorealistic product render, hyper-detailed, natural materials,",
            }

        def _weighted_pick(candidates: list[dict]) -> dict:
            if not candidates:
                return random.choice(styles)
            weights = []
            for s in candidates:
                w = s.get("weight", 1)
                try:
                    w = float(w)
                except Exception:
                    w = 1.0
                weights.append(max(0.0, w))
            if all(w == 0.0 for w in weights):
                return random.choice(candidates)
            return random.choices(candidates, weights=weights, k=1)[0]

        # Candidate styles by category (if mapping exists)
        candidates = styles
        if category and category in mapping:
            allowed_ids = set(mapping[category])
            filtered = [s for s in styles if s.get("id") in allowed_ids]
            if filtered:
                candidates = filtered

        # Enforce: realistic takes 40% probability when available (and there are other choices)
        realistic_style = next((s for s in candidates if s.get("id") == "realistic"), None)
        if realistic_style is None:
            return _weighted_pick(candidates)
        if len(candidates) == 1:
            return realistic_style

        if random.random() < 0.40:
            return realistic_style

        others = [s for s in candidates if s.get("id") != "realistic"]
        return _weighted_pick(others)

    def optimize_prompt(
        self,
        subject: str,
        style_id: Optional[str] = None,
        category: Optional[str] = None
    ) -> str:
        """
        Generate a prompt using a 2-stage pipeline:
        1) Stage-1: Generate concise object description (50-80 words, no style)
        2) Stage-2: Compose final prompt = [Style prefix] + [Description] + [Image requirements]

        Args:
            subject: The base subject (e.g., "chair", "dragon")
            style_id: Optional style ID to use (random if not provided)
            category: Optional category for style filtering

        Returns:
            Optimized T2I prompt string
        """
        result = self.optimize_prompt_with_metadata(
            subject=subject,
            style_id=style_id,
            category=category,
        )
        return result["prompt"]

    def optimize_prompt_with_metadata(
        self,
        subject: str,
        style_id: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, object]:
        """Generate prompt and return the selected style plus intermediate text."""
        if style_id:
            style = self._get_style_by_id(style_id)
        else:
            style = self.pick_random_style(category)
        style_prefix = style.get("prefix", "")
        style_name = style.get("name_en", style.get("id", "unknown"))

        _logger.info(
            "Stage-1: Generating object description",
            extra={"subject": subject, "style": style_name},
        )

        system_prompt, user_prompt = get_object_description_prompt(subject)
        description = self.client.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.7,
            max_tokens=1000,
        )
        description = (description or "").strip()
        _logger.debug(
            "Stage-1 description generated",
            extra={"subject": subject, "description": description},
        )

        image_requirements = get_image_requirements_prompt()
        final_prompt = compose_t2i_prompt(description, style_prefix, image_requirements)

        _logger.info(
            "Stage-2: Prompt composed",
            extra={"subject": subject, "style": style_name, "prompt": final_prompt},
        )

        return {
            "subject": subject,
            "category": category,
            "style_id": style.get("id"),
            "style_name_en": style.get("name_en"),
            "style_name_zh": style.get("name_zh"),
            "style_prefix": style_prefix,
            "object_description": description,
            "image_requirements": image_requirements,
            "prompt": final_prompt,
        }

    def _get_style_by_id(self, style_id: str) -> dict:
        """Get a specific style by its ID."""
        data = self._load_styles()
        styles = data.get("styles", []) if isinstance(data, dict) else []
        for style in styles:
            if style.get("id") == style_id:
                return style
        # Fallback to random if not found
        return self.pick_random_style()

    def close(self):
        self.client.close()
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
