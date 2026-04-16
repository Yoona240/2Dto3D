"""
View Analyzer Module

使用 MLLM 判断哪些视角需要编辑。
输入：T_image + 编辑指令 + 多视角拼接图
输出：JSON，包含需要编辑/保留的视角
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from utils.llm_client import get_llm_client
from core.image.view_stitcher import VIEW_ORDER


ALLOWED_VIEWS = set(VIEW_ORDER)


class ViewAnalyzer:
    def __init__(self, mllm_config):
        self.client = get_llm_client(mllm_config)

    def analyze_views(
        self,
        target_image_path: Path,
        stitched_image_path: Path,
        instruction: str,
        view_names: Optional[List[str]] = None,
        log_dir: Optional[Path] = None,
    ) -> List[str]:
        if view_names is None:
            view_names = VIEW_ORDER

        view_names = [v for v in view_names if v in ALLOWED_VIEWS]
        if not view_names:
            return VIEW_ORDER

        system_prompt = (
            "You are a visual analyst. Determine which view images need editing to match the instruction. "
            "Respond ONLY in valid JSON."
        )

        user_prompt = (
            "You are given two images: \n"
            "1) T_image: the edited target of the source image.\n"
            "2) A stitched 3x2 multiview grid of the object, with labels (front, back, right, left, top, bottom).\n"
            "Instruction: "
            f"{instruction}\n\n"
            "Task: decide which view angles must be edited so the multiview set is consistent with T_image and the instruction. "
            "Only choose from this allowed list: "
            f"{view_names}.\n\n"
            "Return JSON with this schema: \n"
            "{\n"
            "  \"edit_views\": [\"front\", \"back\"],\n"
            "  \"keep_views\": [\"left\", \"right\"],\n"
            "  \"reason\": \"short reason\"\n"
            "}\n"
            "Ensure JSON is the only output."
        )

        response_text = self.client.chat_with_images(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=[target_image_path, stitched_image_path],
            log_dir=log_dir,
        )

        data = _safe_parse_json(response_text)
        if not data:
            return view_names

        edit_views = _normalize_views(data.get("edit_views"), view_names)
        keep_views = _normalize_views(data.get("keep_views"), view_names)

        if not edit_views and keep_views:
            edit_views = [v for v in view_names if v not in keep_views]

        if not edit_views:
            return view_names

        return _sort_views(edit_views)


def _safe_parse_json(text: str) -> Optional[dict]:
    if not text:
        return None

    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON substring
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None

    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _normalize_views(views: Optional[List[str]], allowed: List[str]) -> List[str]:
    if not views:
        return []
    normalized = [v for v in views if v in ALLOWED_VIEWS and v in allowed]
    return list(dict.fromkeys(normalized))


def _sort_views(views: List[str]) -> List[str]:
    order = {v: i for i, v in enumerate(VIEW_ORDER)}
    return sorted(views, key=lambda v: order.get(v, 999))