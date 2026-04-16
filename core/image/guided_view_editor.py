"""
Guided View Editor Module

Single View 编辑模式的增强版本：
1. 先在源图像 S_image 上应用编辑指令，得到目标图 T_image
2. 用 T_image 作为参考，引导每个渲染视角的编辑
3. 每次编辑一个视角时，输入：T_image + 编辑指令 + 该视角渲染图

模型默认使用 Gemini 2.5 Flash Image。
"""

import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, List, Optional, Union

from utils.config import GeminiResponseConfig, ImageApiConfig
from utils.image_api_client import ImageApiClient
from core.image.view_stitcher import ViewStitcher, VIEW_ORDER
from core.image.view_analyzer import ViewAnalyzer


# 默认编辑的视角
DEFAULT_VIEWS = VIEW_ORDER


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
                    print(f"API call failed (attempt {attempt + 1}/{max_retries}): {e}")
                    time.sleep(delay * (2**attempt))
            raise last_exception

        return wrapper

    return decorator


def _run_in_optional_lane(
    lane: Any,
    operation_name: str,
    func: Callable[[], Any],
) -> Any:
    """Run *func* inside *lane* if provided, otherwise call directly."""
    if lane is not None:
        return lane.run(operation_name, func)
    return func()


class GuidedViewEditor:
    """
    引导式视角编辑器

    工作流程：
    1. edit_source_image(): 编辑源图像，生成目标图 T_image
    2. edit_view_with_reference(): 用 T_image 引导编辑单个视角
    3. edit_all_views(): 完整流程，编辑所有指定视角
    """

    def __init__(
        self, config: Union[GeminiResponseConfig, ImageApiConfig], mllm_config=None
    ):
        """
        初始化编辑器

        Args:
            config: API 配置，模型应为 gemini-2.5-flash-image
            mllm_config: 视角分析所用的 MLLM 配置（可选）
        """
        self.config = config
        self.client = ImageApiClient(config)
        self.stitcher = ViewStitcher()
        self.view_analyzer = ViewAnalyzer(mllm_config) if mllm_config else None

    @retry_api_call()
    def edit_source_image(
        self, source_image_path: Path, instruction: str, output_path: Path
    ) -> Path:
        """
        编辑源图像，生成目标图 T_image

        这是第一步：在原始图像上应用编辑，作为后续视角编辑的参考。

        Args:
            source_image_path: 源图像路径 (S_image)
            instruction: 编辑指令
            output_path: 输出路径 (T_image)

        Returns:
            编辑后的目标图路径
        """
        source_image_path = Path(source_image_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        result = self.client.edit_image(
            str(source_image_path),
            instruction,
            str(output_path),
            size=self.config.size,
            auto_size=False,
        )

        return Path(result)

    @retry_api_call()
    def edit_view_with_reference(
        self,
        view_image_path: Path,
        reference_image_path: Path,
        instruction: str,
        output_path: Path,
    ) -> Path:
        """
        使用参考图引导编辑单个视角

        Args:
            view_image_path: 待编辑的视角渲染图
            reference_image_path: 参考图路径 (T_image)
            instruction: 编辑指令
            output_path: 输出路径

        Returns:
            编辑后的视角图路径
        """
        view_image_path = Path(view_image_path)
        reference_image_path = Path(reference_image_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        result = self.client.edit_image_with_reference(
            str(view_image_path),
            str(reference_image_path),
            instruction,
            str(output_path),
            size=self.config.size,
            auto_size=False,
        )

        return Path(result)

    def edit_all_views(
        self,
        source_image_path: Path,
        views_dir: Path,
        instruction: str,
        output_dir: Path,
        view_names: Optional[List[str]] = None,
        temp_dir: Optional[Path] = None,
        image_lane: Any = None,
        text_lane: Any = None,
    ) -> dict:
        """
        完整的引导式多视角编辑流程

        1. 编辑源图像 → T_image
        2. 对每个视角，用 T_image 引导编辑

        Args:
            source_image_path: 源图像路径 (通常是 images/{id}/image.png)
            views_dir: 视角渲染图目录 (通常是 triplets/{id}/views/)
            instruction: 编辑指令
            output_dir: 输出目录
            view_names: 要编辑的视角列表，默认 ["front", "back", "right"]
            temp_dir: 临时目录，用于保存 T_image
            image_lane: Optional ApiLane for image API calls (Phase 1).
                        Each individual image edit acquires/releases the lane
                        independently, keeping slot hold time short.
            text_lane: Optional ApiLane for VLM / text API calls (Phase 1).
                       Used by the view_analyzer (VLM view selection).

        Returns:
            Dict containing:
            - output_paths: List[Path] of edited views
            - metadata: Dict of process metadata
        """
        source_image_path = Path(source_image_path)
        views_dir = Path(views_dir)
        output_dir = Path(output_dir)
        temp_dir = Path(temp_dir) if temp_dir else (output_dir / "_tmp")

        if view_names is None:
            view_names = DEFAULT_VIEWS

        output_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: 编辑源图像，生成 T_image  (1 image API call)
        target_image_path = temp_dir / "_target_image.png"
        print(f"[GuidedEdit] Step 1: edit source image  instruction={instruction[:80]}")

        target_image_path = _run_in_optional_lane(
            image_lane,
            "edit_source_image",
            lambda: self.edit_source_image(
                source_image_path,
                instruction,
                target_image_path,
            ),
        )
        print(f"[GuidedEdit]   target_image={target_image_path}")

        # Step 2: 拼接多视角图用于分析 (local, no API)
        stitched_path = temp_dir / "_views_stitched.png"
        stitched_path, _ = self.stitcher.stitch_views(
            views_dir=views_dir,
            output_path=stitched_path,
            view_names=view_names,
        )

        # Step 3: 使用 MLLM 判断需要编辑的视角  (1 VLM call → text lane)
        selected_views = view_names
        if self.view_analyzer:
            log_dir = temp_dir / "_view_select"
            selected_views = _run_in_optional_lane(
                text_lane,
                "vlm_view_selection",
                lambda: self.view_analyzer.analyze_views(
                    target_image_path=target_image_path,
                    stitched_image_path=stitched_path,
                    instruction=instruction,
                    view_names=view_names,
                    log_dir=log_dir,
                ),
            )
            print(f"[GuidedEdit]   selected_views={selected_views}")
        else:
            print("[GuidedEdit]   no MLLM — editing all views")

        # Step 4: 对每个视角进行引导式编辑  (N image API calls)
        output_paths = []

        for i, view_name in enumerate(selected_views):
            view_path = views_dir / f"{view_name}.png"

            if not view_path.exists():
                print(f"[GuidedEdit]   view {view_name} not found, skipping")
                continue

            out_path = output_dir / f"{view_name}.png"
            print(f"[GuidedEdit] Step 4.{i + 1}: edit view {view_name}")

            edited_path = _run_in_optional_lane(
                image_lane,
                f"edit_view_{view_name}",
                lambda vp=view_path, op=out_path: self.edit_view_with_reference(
                    vp,
                    target_image_path,
                    instruction,
                    op,
                ),
            )

            output_paths.append(edited_path)

        print(f"[GuidedEdit] Done — edited {len(output_paths)} views")

        metadata = {
            "model": self.config.model,
            "source_image": str(source_image_path),
            "instruction": instruction,
            "selected_views": selected_views,
            "intermediate_files": {
                "target_image": str(target_image_path),
                "stitched_view": str(stitched_path),
            },
        }

        return {"output_paths": output_paths, "metadata": metadata}

    def close(self):
        """关闭客户端"""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
