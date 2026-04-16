"""
Image API Client - 统一的图像 API 调用层

提供统一接口，支持：
1. Response API (Gemini/Doubao 图像生成/编辑) - 异步轮询模式
2. Chat Completions API (OpenRouter/Flux) - 同步模式

使用示例:
    from utils.image_api_client import ImageApiClient
    from utils.config import load_config

    config = load_config()
    client = ImageApiClient(config.qh_image)

    # T2I 生成
    result = client.generate_image(prompt="A futuristic robot")

    # 图像编辑
    result = client.edit_image(image_path="input.png", instruction="Remove background")
"""

import base64
import time
from pathlib import Path
from typing import Optional, Union
from dataclasses import dataclass

import httpx

from utils.logger import get_image_api_logger

# 模块级 logger
_logger = get_image_api_logger()


@dataclass
class ImageResult:
    """图像生成/编辑结果"""

    image_data: str  # base64 or URL
    response_id: Optional[str] = None
    elapsed_time: float = 0.0


class ImageApiClient:
    """
    统一的图像 API 客户端

    自动检测 API 类型并使用正确的调用方式：
    - gemini-* / imagen-* / doubao-* → Response API (异步轮询)
    - 其他模型 → Chat Completions API (同步)
    """

    # 需要使用 Response API 的模型前缀
    RESPONSE_API_MODELS = (
        "gemini-",
        "imagen-",
        "doubao-",
    )

    def __init__(self, config):
        """
        初始化客户端

        Args:
            config: ImageApiConfig 或 GeminiResponseConfig
        """
        self.config = config
        self.model = config.model
        self.api_key = config.api_key
        self.base_url = config.base_url

        # Response API 轮询设置
        self.poll_interval = config.poll_interval
        self.max_wait_time = config.max_wait_time

        # HTTP 客户端
        transport = httpx.HTTPTransport(retries=config.max_retries)
        self.client = httpx.Client(timeout=float(config.timeout), transport=transport)

    def _use_response_api(self) -> bool:
        """判断是否使用 Response API"""
        return any(self.model.startswith(prefix) for prefix in self.RESPONSE_API_MODELS)

    def _get_headers(self) -> dict:
        """获取请求头"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _require_size(self, size: Optional[str], context: str) -> str:
        """Validate size and fail loudly when missing."""
        if size is None:
            raise ValueError(f"{context}: size is required and cannot be None")
        if not isinstance(size, str) or not size.strip():
            raise ValueError(f"{context}: size must be a non-empty string, got: {size!r}")
        return size

    def _encode_image(self, image_path: str) -> str:
        """编码图片为 base64 data URL"""
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        # 检测图片类型
        suffix = Path(image_path).suffix.lower()
        mime_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix, "image/png")
        return f"data:{mime_type};base64,{b64}"

    def _save_image(self, image_data: str, output_path: str) -> str:
        """解码并保存图片"""
        if image_data.startswith("http"):
            # URL - 下载
            with httpx.Client(timeout=60) as client:
                resp = client.get(image_data)
                resp.raise_for_status()
                data = resp.content
        elif image_data.startswith("data:"):
            # Data URL - 提取 base64
            if "," in image_data:
                image_data = image_data.split(",", 1)[1]
            data = base64.b64decode(image_data)
        else:
            # 原始 base64
            data = base64.b64decode(image_data)

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return str(path)

    # ==================== Response API ====================

    def _create_response_task(self, payload: dict) -> str:
        """创建 Response API 异步任务，返回 response_id"""
        response = self.client.post(
            f"{self.base_url}/v1/responses", headers=self._get_headers(), json=payload
        )

        if response.status_code != 200:
            raise Exception(
                f"Response API failed: {response.status_code} - {response.text}"
            )

        result = response.json()
        response_id = result.get("id")

        if not response_id:
            raise Exception(f"No response ID returned: {result}")

        return response_id

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"

    def _poll_response(self, response_id: str) -> ImageResult:
        """轮询 Response API 直到完成"""
        start_time = time.time()
        short_id = response_id[-12:] if len(response_id) > 12 else response_id

        while True:
            elapsed = time.time() - start_time
            if elapsed > self.max_wait_time:
                raise Exception(f"Task timeout after {self.max_wait_time}s")

            poll_resp = self.client.get(
                f"{self.base_url}/v1/responses/{response_id}",
                headers=self._get_headers(),
            )

            if poll_resp.status_code != 200:
                raise Exception(
                    f"Poll failed: {poll_resp.status_code} - {poll_resp.text}"
                )

            result = poll_resp.json()
            status = result.get("status", "")

            _logger.debug(
                "Polling status",
                extra={
                    "status": status,
                    "elapsed_time": elapsed,
                    "response_id": response_id,
                },
            )
            print(
                f"  [Poll] ...{short_id}  elapsed={self._fmt_elapsed(elapsed)}"
                f"  status={status}  (next in {self.poll_interval:.0f}s)",
                flush=True,
            )

            if status == "completed":
                # 提取图片
                output = result.get("output", [])
                for item in output:
                    if (
                        item.get("type") == "image_generation_call"
                        and item.get("status") == "completed"
                    ):
                        image_data = item.get("result", "")
                        if image_data:
                            return ImageResult(
                                image_data=image_data,
                                response_id=response_id,
                                elapsed_time=elapsed,
                            )

                raise Exception(f"No image found in completed response: {result}")

            elif status == "failed":
                error = result.get("error", "Unknown error")
                raise Exception(f"Task failed: {error}")

            elif status in ("queued", "incomplete", "in_progress", ""):
                time.sleep(self.poll_interval)
            else:
                _logger.warning(f"Unknown status '{status}', continuing to poll")
                time.sleep(self.poll_interval)

    def _generate_via_response_api(
        self, prompt: str, image_path: Optional[str] = None, size: Optional[str] = None
    ) -> ImageResult:
        """使用 Response API 生成/编辑图片

        Args:
            prompt: 提示词
            image_path: 输入图片路径（可选，用于编辑）
            size: 输出尺寸，格式 "WxH"（可选，默认使用 config 中的值）
        """
        # 构建 content
        content = [{"type": "input_text", "text": prompt}]

        if image_path:
            # 图像编辑模式
            image_data_url = self._encode_image(image_path)
            content.append({"type": "input_image", "image_url": image_data_url})

        # 确定输出尺寸（Fail Loudly）
        output_size = self._require_size(size, "_generate_via_response_api")

        payload = {
            "model": self.model,
            "background": True,
            "input": [{"type": "message", "role": "user", "content": content}],
            "tools": [
                {
                    "type": "image_generation",
                    "size": output_size,
                    "n": getattr(self.config, "n", 1),
                }
            ],
        }

        _logger.debug(
            "Response API request", extra={"model": self.model, "size": output_size}
        )

        response_id = self._create_response_task(payload)
        _logger.debug(
            "Response API task created",
            extra={"response_id": response_id, "model": self.model},
        )

        return self._poll_response(response_id)

    # ==================== Chat Completions API ====================

    def _generate_via_chat_api(self, prompt: str) -> ImageResult:
        """使用 Chat Completions API 生成图片（用于 OpenRouter/Flux 等）"""
        start_time = time.time()

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": f"Generate a high-quality image of: {prompt}\nRespond ONLY with the image, no text.",
                }
            ],
        }

        response = self.client.post(
            f"{self.base_url}/chat/completions",
            headers=self._get_headers(),
            json=payload,
        )

        if response.status_code != 200:
            raise Exception(
                f"Chat API failed: {response.status_code} - {response.text}"
            )

        result = response.json()
        elapsed = time.time() - start_time

        # 解析响应
        if "choices" in result and result["choices"]:
            message = result["choices"][0].get("message", {})
            content = message.get("content", "")

            # 尝试提取图片 URL 或 base64
            if content.startswith("data:") or content.startswith("http"):
                return ImageResult(image_data=content, elapsed_time=elapsed)

            # 检查是否有 image_url 字段
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        url = item.get("image_url", {}).get("url", "")
                        if url:
                            return ImageResult(image_data=url, elapsed_time=elapsed)
            elif isinstance(content, dict) and content.get("type") == "image_url":
                url = content.get("image_url", {}).get("url", "")
                if url:
                    return ImageResult(image_data=url, elapsed_time=elapsed)

        # DALL-E 风格响应
        if "data" in result and result["data"]:
            img_obj = result["data"][0]
            url = img_obj.get("url") or img_obj.get("b64_json")
            if url:
                return ImageResult(image_data=url, elapsed_time=elapsed)

        raise Exception(f"No image found in API response: {result}")

    # ==================== 公共接口 ====================

    def generate_image(
        self, prompt: str, output_path: Optional[str] = None
    ) -> Union[ImageResult, str]:
        """
        生成图片 (T2I)

        Args:
            prompt: 文本描述
            output_path: 保存路径（可选）

        Returns:
            如果提供 output_path，返回保存的路径
            否则返回 ImageResult
        """
        _logger.info(
            "T2I generation started", extra={"prompt": prompt, "model": self.model}
        )

        if self._use_response_api():
            try:
                config_size = self.config.size
            except AttributeError as e:
                raise ValueError("generate_image: config.size is required for Response API models") from e
            result = self._generate_via_response_api(
                prompt,
                size=self._require_size(config_size, "generate_image"),
            )
        else:
            result = self._generate_via_chat_api(prompt)

        if output_path:
            saved_path = self._save_image(result.image_data, output_path)
            _logger.info(
                "T2I generation completed",
                extra={
                    "prompt": prompt,
                    "model": self.model,
                    "output_path": saved_path,
                    "elapsed_time": result.elapsed_time,
                    "response_id": result.response_id,
                },
            )
            return saved_path

        _logger.info(
            "T2I generation completed",
            extra={
                "prompt": prompt,
                "model": self.model,
                "elapsed_time": result.elapsed_time,
                "response_id": result.response_id,
            },
        )
        return result

    def edit_image(
        self,
        image_path: str,
        instruction: str,
        output_path: Optional[str] = None,
        size: Optional[str] = None,
        auto_size: bool = True,
    ) -> Union[ImageResult, str]:
        """
        编辑图片

        Args:
            image_path: 源图片路径
            instruction: 编辑指令
            output_path: 保存路径（可选）
            size: 输出尺寸，格式 "WxH"（可选）
            auto_size: 如果 size 未指定，是否自动使用输入图片的尺寸

        Returns:
            如果提供 output_path，返回保存的路径
            否则返回 ImageResult
        """
        _logger.info(
            "Image edit started",
            extra={
                "prompt": instruction,
                "image_path": image_path,
                "model": self.model,
            },
        )

        if not self._use_response_api():
            raise Exception(
                f"Model {self.model} does not support image editing via Chat API"
            )

        # 自动获取输入图片尺寸
        if size is None and auto_size:
            try:
                from PIL import Image

                with Image.open(image_path) as img:
                    size = f"{img.width}x{img.height}"
                    _logger.debug("Auto-detected image size", extra={"size": size})
            except Exception as e:
                _logger.warning(f"Failed to detect image size: {e}")
                size = None

        result = self._generate_via_response_api(instruction, image_path, size)

        if output_path:
            saved_path = self._save_image(result.image_data, output_path)
            _logger.info(
                "Image edit completed",
                extra={
                    "prompt": instruction,
                    "image_path": image_path,
                    "model": self.model,
                    "output_path": saved_path,
                    "elapsed_time": result.elapsed_time,
                    "response_id": result.response_id,
                    "size": size,
                },
            )
            return saved_path

        _logger.info(
            "Image edit completed",
            extra={
                "prompt": instruction,
                "image_path": image_path,
                "model": self.model,
                "elapsed_time": result.elapsed_time,
                "response_id": result.response_id,
                "size": size,
            },
        )
        return result

    def edit_image_with_reference(
        self,
        image_path: str,
        reference_image_path: str,
        instruction: str,
        output_path: Optional[str] = None,
        size: Optional[str] = None,
        auto_size: bool = True,
    ) -> Union[ImageResult, str]:
        """
        使用参考图引导编辑图片

        用于 Single View 模式：先编辑源图得到目标图 T_image，
        再用 T_image 作为参考引导各视角渲染图的编辑。

        Args:
            image_path: 待编辑的图片路径（视角渲染图）
            reference_image_path: 参考图路径（已编辑的目标图 T_image）
            instruction: 编辑指令
            output_path: 保存路径（可选）
            size: 输出尺寸，格式 "WxH"（可选）
            auto_size: 如果 size 未指定，是否自动使用输入图片的尺寸

        Returns:
            如果提供 output_path，返回保存的路径
            否则返回 ImageResult
        """
        _logger.info(
            "Guided image edit started",
            extra={
                "prompt": instruction,
                "image_path": image_path,
                "reference_image_path": reference_image_path,
                "model": self.model,
            },
        )

        if not self._use_response_api():
            raise Exception(
                f"Model {self.model} does not support image editing via Chat API"
            )

        # 自动获取输入图片尺寸
        if size is None and auto_size:
            try:
                from PIL import Image

                with Image.open(image_path) as img:
                    size = f"{img.width}x{img.height}"
                    _logger.debug("Auto-detected image size", extra={"size": size})
            except Exception as e:
                _logger.warning(f"Failed to detect image size: {e}")
                size = None

        # 构建包含两张图的 prompt
        # 参考图在前，待编辑图在后
        enhanced_prompt = (
            f"I have two images:\n"
            f"1. The first image is a REFERENCE showing the desired editing result on the source image.\n"
            f"2. The second image is a 3D rendered view that needs to be edited.\n\n"
            f"Please apply the same edit to the second image, using the first image as a reference for what the result should look like.\n"
            f"Edit instruction: {instruction}\n\n"
            f"Important: Only output the edited version of the SECOND image (the 3D rendered view). "
            f"Maintain the exact viewpoint and style of the second image while applying the edit shown in the reference."
        )

        result = self._generate_via_response_api_multi_image(
            enhanced_prompt, [reference_image_path, image_path], size
        )

        if output_path:
            saved_path = self._save_image(result.image_data, output_path)
            _logger.info(
                "Guided image edit completed",
                extra={
                    "prompt": instruction,
                    "image_path": image_path,
                    "reference_image_path": reference_image_path,
                    "model": self.model,
                    "output_path": saved_path,
                    "elapsed_time": result.elapsed_time,
                    "size": size,
                },
            )
            return saved_path

        return result

    def _generate_via_response_api_multi_image(
        self, prompt: str, image_paths: list, size: Optional[str] = None
    ) -> ImageResult:
        """使用 Response API 生成/编辑图片（支持多图输入）

        Args:
            prompt: 提示词
            image_paths: 输入图片路径列表
            size: 输出尺寸，格式 "WxH"（可选，默认使用 config 中的值）
        """
        # 构建 content，先放文本，再放图片
        content = [{"type": "input_text", "text": prompt}]

        for image_path in image_paths:
            image_data_url = self._encode_image(image_path)
            content.append({"type": "input_image", "image_url": image_data_url})

        # 确定输出尺寸（Fail Loudly）
        output_size = self._require_size(size, "_generate_via_response_api_multi_image")

        payload = {
            "model": self.model,
            "background": True,
            "input": [{"type": "message", "role": "user", "content": content}],
            "tools": [
                {
                    "type": "image_generation",
                    "size": output_size,
                    "n": getattr(self.config, "n", 1),
                }
            ],
        }

        _logger.debug(
            "Response API multi-image request",
            extra={
                "model": self.model,
                "size": output_size,
                "num_images": len(image_paths),
            },
        )

        response_id = self._create_response_task(payload)
        _logger.debug(
            "Response API task created",
            extra={"response_id": response_id, "model": self.model},
        )

        return self._poll_response(response_id)

    def close(self):
        """关闭客户端"""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
