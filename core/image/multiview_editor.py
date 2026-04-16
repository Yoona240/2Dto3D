"""
Multiview Editor Module

将多个视角渲染图拼接为 3x2 网格，调用图像编辑模型编辑后裁剪回多个视角。
支持 Gemini 2.0 Flash / Gemini 3 Pro 等模型。

关键设计：
1. 拼接图填充为正方形，避免 Gemini 强制输出 1024x1024 导致的比例失真
2. 白色边框 + 浅灰标签，视觉清晰
3. 等比缩放处理 Gemini 返回的不同尺寸
"""

from pathlib import Path
from typing import Any, List, Optional, Union
from PIL import Image

from utils.config import GeminiResponseConfig, ImageApiConfig
from utils.image_api_client import ImageApiClient
from utils.prompt_guardrail import (
    ResolvedGuardrailPrompt,
    build_prompt_trace,
    compose_final_prompt,
    resolve_guardrail,
)
from core.image.view_stitcher import ViewStitcher, VIEW_ORDER


import time
from functools import wraps

def retry_api_call(max_retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    print(f"API call failed (attempt {attempt+1}/{max_retries}): {e}")
                    time.sleep(delay * (2 ** attempt))
            raise last_exception
        return wrapper
    return decorator


class MultiviewEditor:
    """
    多视角拼接编辑器

    1. 将 6 个视角图拼接为 3x2 网格（填充为正方形）
    2. 在每个视角区域添加标签（不遮挡图片）
    3. 调用图像编辑模型编辑整体图
    4. 将编辑后的图裁剪回 6 个独立视角
    """

    def __init__(
        self,
        config: Union[GeminiResponseConfig, ImageApiConfig],
        *,
        pipeline_config: Optional[Any] = None,
        task_name: str = "multiview_editing",
    ):
        """初始化编辑器"""
        self.config = config
        self.client = ImageApiClient(config)
        self.stitcher = ViewStitcher()
        self.task_name = task_name
        if pipeline_config is None:
            # 兼容旧调用：未传 pipeline_config 时，不启用 guardrail。
            # Backward-compatible fallback: no pipeline config => guardrail disabled.
            self.guardrail = ResolvedGuardrailPrompt(
                task_name=task_name,
                enabled=False,
                version=None,
                text="",
            )
        else:
            # 新流程：按 task_name 从 config.tasks 读取 guardrail 配置。
            # New flow: resolve guardrail from config.tasks by task_name.
            self.guardrail = resolve_guardrail(pipeline_config, task_name)

    def split_views(
        self,
        edited_image_path: Path,
        metadata: dict,
        output_dir: Path,
        original_views_dir: Optional[Path] = None
    ) -> List[Path]:
        """
        将编辑后的拼接图裁剪回多个视角（仅裁剪图片区域，不含标签）

        关键原则：分割出来的图和拼接进去的图的区域和大小完全一致
        - 如果 Gemini 返回的尺寸不同，先等比缩放回原尺寸
        - 裁剪区域 = image_positions（不含边框和标签）
        - 输出尺寸 = 原始图片尺寸

        Args:
            edited_image_path: 编辑后的拼接图路径
            metadata: stitch_views 返回的元数据
            output_dir: 输出目录
            original_views_dir: 原始视角图目录（可选，用于验证尺寸）

        Returns:
            裁剪后的视角图路径列表
        """
        edited_img = Image.open(edited_image_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        expected_size = (metadata['grid_width'], metadata['grid_height'])

        # 处理 Gemini 可能改变图像尺寸的情况
        if edited_img.size != expected_size:
            # 计算缩放比例
            scale_x = edited_img.width / expected_size[0]
            scale_y = edited_img.height / expected_size[1]

            # 如果比例相近（正方形到正方形），直接等比缩放
            if abs(scale_x - scale_y) < 0.01:
                edited_img = edited_img.resize(expected_size, Image.Resampling.LANCZOS)
            else:
                # 比例不一致，尝试智能处理
                # 先缩放到能包含所有内容的尺寸，再居中裁剪
                if scale_x > scale_y:
                    new_height = int(edited_img.height / scale_x)
                    new_width = expected_size[0]
                else:
                    new_width = int(edited_img.width / scale_y)
                    new_height = expected_size[1]

                edited_img = edited_img.resize((new_width, new_height), Image.Resampling.LANCZOS)

                # 居中裁剪到期望尺寸
                left = (new_width - expected_size[0]) // 2
                top = (new_height - expected_size[1]) // 2
                edited_img = edited_img.crop((
                    left, top,
                    left + expected_size[0],
                    top + expected_size[1]
                ))

        output_paths = []

        # 使用 image_positions（仅图片区域），不包含标签区域
        positions = metadata.get("image_positions", metadata.get("view_positions", {}))

        # 获取原始图片尺寸（用于验证）
        original_image_width = metadata.get("image_width")
        original_image_height = metadata.get("image_height")

        for view_name, pos in positions.items():
            # 裁剪区域（仅图片，不含标签）
            left = pos["x"]
            top = pos["y"]
            width = pos["width"]
            height = pos["height"]
            right = left + width
            bottom = top + height

            # 裁剪
            view_img = edited_img.crop((left, top, right, bottom))

            # 验证并调整尺寸（如果有原始视角图目录）
            if original_views_dir:
                original_path = Path(original_views_dir) / f"{view_name}.png"
                if original_path.exists():
                    original_img = Image.open(original_path)
                    if view_img.size != original_img.size:
                        view_img = view_img.resize(original_img.size, Image.Resampling.LANCZOS)

            # 保存
            output_path = output_dir / f"{view_name}.png"
            view_img.save(output_path, "PNG")
            output_paths.append(output_path)

        return output_paths

    def edit_multiview(
        self,
        views_dir: Path,
        instruction: str,
        output_dir: Path,
        temp_dir: Optional[Path] = None,
        view_names: Optional[List[str]] = None
    ) -> dict:
        """
        完整的多视角编辑流程：
        1. 拼接多视角图（填充为正方形）
        2. 调用编辑模型
        3. 裁剪回多视角（等比缩放处理尺寸差异）

        Args:
            views_dir: 包含原始视角图的目录
            instruction: 编辑指令
            output_dir: 输出编辑后视角图的目录
            temp_dir: 临时文件目录（可选，默认使用 output_dir/_tmp）
            view_names: 要编辑的视角名称列表

        Returns:
            Dict containing:
            - output_paths: List[Path] of edited views
            - metadata: Dict of process metadata
        """
        views_dir = Path(views_dir)
        output_dir = Path(output_dir)
        temp_dir = Path(temp_dir) if temp_dir else (output_dir / "_tmp")
        temp_dir.mkdir(parents=True, exist_ok=True)

        if view_names is None:
            view_names = VIEW_ORDER

        # Step 1: 拼接（自动填充为正方形）
        stitched_path = temp_dir / "_multiview_stitched.png"
        stitched_path, metadata = self.stitcher.stitch_views(views_dir, stitched_path, view_names)

        # Step 2: 编辑
        view_list = ", ".join(view_names)
        task_context_prompt = (
            "This is a 3x2 grid of view angles of the same 3D object. "
            f"The grid contains these views: {view_list}. "
            "Apply the requested edit to the target part in every view where it is geometrically visible, "
            "including views where it is only partially visible. "
            "In views where the target part is not visible at all, keep those views completely unchanged. "
            "Maintain the exact grid layout and view labels."
        )
        user_instruction_prompt = f"Edit instruction: {instruction}"
        # 统一组装：guardrail + task context + user instruction。
        # Deterministic composition for reproducibility.
        final_prompt = compose_final_prompt(
            guardrail=self.guardrail,
            task_context_prompt=task_context_prompt,
            user_instruction=user_instruction_prompt,
        )
        # 控制台打印关键 prompt 信息，便于批处理时快速确认本次约束是否生效。
        # Print key prompt info for batch-run observability.
        if self.guardrail.enabled:
            print(
                f"[MultiviewEditor] guardrail enabled "
                f"(task={self.task_name}, version={self.guardrail.version})"
            )
            print("[MultiviewEditor] guardrail_prompt:")
            print(self.guardrail.text)
        else:
            print(f"[MultiviewEditor] guardrail disabled (task={self.task_name})")
        print("[MultiviewEditor] final_prompt_preview:")
        print(final_prompt)

        edited_path = temp_dir / "_multiview_edited.png"

        @retry_api_call()
        def _call_api():
            self.client.edit_image(
                str(stitched_path),
                final_prompt,
                str(edited_path),
                size=self.config.size,
                auto_size=False
            )
        _call_api()

        # Step 3: 裁剪（split_views 内部会处理尺寸差异）
        output_paths = self.split_views(
            edited_path,
            metadata,
            output_dir,
            original_views_dir=views_dir  # 传递原始目录用于尺寸验证
        )

        prompt_trace = build_prompt_trace(
            guardrail=self.guardrail,
            task_context_prompt=task_context_prompt,
            user_instruction=user_instruction_prompt,
            final_prompt=final_prompt,
        )

        # Metadata to return
        process_metadata = {
            "model": self.config.model,
            "instruction": instruction,
            # 兼容历史字段名，实际内容已是 final_prompt。
            # Keep legacy key name for downstream compatibility.
            "enhanced_instruction": final_prompt,
            "final_prompt": final_prompt,
            "prompt_trace": prompt_trace,
            "guardrail_prompt_enabled": self.guardrail.enabled,
            "guardrail_prompt_version": self.guardrail.version,
            "view_names": view_names,
            "grid_layout": metadata,
            "intermediate_files": {
                "stitched_view": str(stitched_path),
                "edited_grid": str(edited_path)
            }
        }

        return {
            "output_paths": output_paths,
            "metadata": process_metadata
        }
    
    def close(self):
        self.client.close()
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
