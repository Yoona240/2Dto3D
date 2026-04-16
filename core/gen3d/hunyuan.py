"""
Hunyuan 3D Generator Module (Response API)

Client for Hunyuan 3D generation via OpenAI-compatible Response API.
Supports hunyuan-3d-rapid, hunyuan-3d-pro, and hunyuan-3d-2.5 models.
"""

import httpx
import json
import base64
import tempfile
import shutil
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from .base import Base3DGenerator, TaskResult, TaskStatus
from config.config import HunyuanConfig
from core.image.processor import ImageProcessor


class HunyuanGenerator(Base3DGenerator):
    """
    Client for Hunyuan 3D via OpenAI-compatible Response API.

    Uses the /v1/responses endpoint for async 3D generation.
    """

    def __init__(self, config: HunyuanConfig, poll_interval: int, max_wait_time: int):
        """
        Initialize the Hunyuan generator.

        Args:
            config: Hunyuan API configuration
            poll_interval: Seconds between status checks
            max_wait_time: Maximum seconds to wait
        """
        super().__init__(poll_interval, max_wait_time)
        self.config = config
        # Use transport with retry to handle connection resets
        transport = httpx.HTTPTransport(retries=config.max_retries)
        # API submit/poll must not inherit shell proxy env vars.
        self.client = httpx.Client(
            timeout=float(config.timeout),
            transport=transport,
            trust_env=False,
        )

        # Initialize image processor for preprocessing
        self.image_processor = ImageProcessor()

        # Temporary directory for preprocessed images (cleaned up on close)
        self._temp_dir: Optional[str] = None

    def _get_headers(self) -> dict:
        """Get common headers for API requests."""
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _prepare_image(self, image_path: str) -> tuple[str, bool]:
        """
        Process and prepare image for API.

        IMPORTANT: Original images are NEVER modified. If preprocessing is enabled,
        processed images are saved to a temporary directory.

        Args:
            image_path: Path to image or URL

        Returns:
            Tuple of (image_data, is_url)
            - image_data: URL string or base64 data URI
            - is_url: True if URL, False if base64
        """
        # If already a URL, return as-is
        if image_path.startswith("http://") or image_path.startswith("https://"):
            return image_path, True

        # Check if preprocessing is enabled
        preprocess_cfg = self.config.preprocess
        processed_path = image_path

        if preprocess_cfg.enabled:
            # Create temp directory if not exists
            if self._temp_dir is None:
                self._temp_dir = tempfile.mkdtemp(prefix="hunyuan_preprocess_")

            # Build crop config with output_dir pointing to temp directory
            crop_cfg = {
                "enabled": True,
                "strategy": preprocess_cfg.strategy,
                "tolerance": preprocess_cfg.tolerance,
                "min_side": preprocess_cfg.min_side,
                "aspect_control": preprocess_cfg.aspect_control,
                "output_dir": self._temp_dir,  # Critical: save to temp, not overwrite original
            }
            processed_path = self.image_processor.process(image_path, crop_cfg)

        # Convert to base64 data URI
        path = Path(processed_path)
        with open(path, "rb") as f:
            raw_data = f.read()

        # Determine MIME type
        suffix = path.suffix.lower()
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }
        mime_type = mime_types.get(suffix, "image/png")

        # Build data URI
        b64_data = base64.b64encode(raw_data).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{b64_data}"

        return data_uri, False

    def submit_task(
        self,
        image_path: str,
        prompt: Optional[str] = None,
        multi_view_images: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """
        Submit an image-to-3D task to Hunyuan.

        Args:
            image_path: Path to the primary image (front view) or URL
            prompt: Optional text prompt (for text-to-3D mode)
            multi_view_images: Optional list of additional views
                Each dict should have: {"path": str, "view": "back"|"left"|"right"}

        Returns:
            Task ID for polling
        """
        url = f"{self.config.base_url.rstrip('/')}/v1/responses"

        # Build input content
        content = []

        if prompt and not image_path:
            # Text-to-3D mode
            content.append({"type": "input_text", "text": prompt})
        else:
            # Image-to-3D mode
            image_data, is_url = self._prepare_image(image_path)
            content.append({"type": "input_image", "image_url": image_data})

            # Add multi-view images if provided
            if multi_view_images:
                for view_img in multi_view_images:
                    view_data, _ = self._prepare_image(view_img["path"])
                    content.append(
                        {
                            "type": "input_image",
                            "image_url": view_data,
                            "view": view_img.get("view", "back"),
                        }
                    )

        # Build tools configuration
        tools_config: Dict[str, Any] = {
            "type": "3d_generation",
            "output_format": self.config.output_format,
        }

        # Add PBR for image mode
        if not (prompt and not image_path):
            tools_config["pbr"] = self.config.pbr

        # Pro-only parameters (applies to all pro variants)
        _PRO_MODELS = ("hunyuan-3d-pro", "hunyuan-3d-3.1-pro")
        if self.config.model in _PRO_MODELS:
            tools_config["face_count"] = self.config.face_count
            tools_config["generate_type"] = self.config.generate_type
            if self.config.generate_type == "LowPoly":
                tools_config["polygon_type"] = self.config.polygon_type

        # Build request payload
        payload = {
            "model": self.config.model,
            "background": True,
            "input": [{"role": "user", "type": "message", "content": content}],
            "tools": [tools_config],
        }

        # Submit request
        response = self.client.post(url, headers=self._get_headers(), json=payload)

        if response.status_code != 200:
            raise Exception(
                f"Hunyuan API error: {response.status_code} - {response.text}"
            )

        result = response.json()
        task_id = result.get("id")

        if not task_id:
            raise Exception(f"No task ID in response: {result}")

        return task_id

    def poll_status(self, task_id: str) -> TaskResult:
        """
        Check the status of a Hunyuan task.

        Args:
            task_id: The response ID from submit_task

        Returns:
            TaskResult with current status
        """
        url = f"{self.config.base_url.rstrip('/')}/v1/responses/{task_id}"

        response = self.client.get(url, headers=self._get_headers())

        if response.status_code != 200:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=f"Status check failed: {response.status_code} - {response.text}",
            )

        result = response.json()
        status = result.get("status", "unknown")

        # Map API status to TaskStatus
        if status == "completed":
            # Extract download URL from output
            # Hunyuan returns multiple formats (OBJ, GLB, etc.)
            # We need to find the one matching our output_format config
            download_url = None
            target_format = self.config.output_format.upper()  # e.g., "GLB"
            fallback_url = None

            output = result.get("output", [])
            for item in output:
                content = item.get("content", [])
                for content_item in content:
                    item_type = content_item.get("type", "").upper()
                    item_url = content_item.get("url")

                    if item_url:
                        # First match by format
                        if item_type == target_format:
                            download_url = item_url
                            break
                        # Keep first URL as fallback
                        if fallback_url is None:
                            fallback_url = item_url

                if download_url:
                    break

            # Use fallback if target format not found
            if not download_url:
                download_url = fallback_url

            # CRITICAL: Only return COMPLETED if download_url is actually found
            if not download_url:
                return TaskResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    error_message="Task completed but no download URL found in response",
                    remote_task_id=task_id,
                )

            return TaskResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                download_url=download_url,
                remote_task_id=task_id,
            )

        elif status == "failed":
            error_msg = result.get("error", {}).get("message", "Unknown error")
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=error_msg,
                remote_task_id=task_id,
            )

        else:
            # queued, incomplete, in_progress
            return TaskResult(
                task_id=task_id, status=TaskStatus.PROCESSING, remote_task_id=task_id
            )

    def download_result(self, task_id: str, output_path: str) -> str:
        """
        Download the generated 3D model.

        Hunyuan returns a ZIP file containing the model. We extract the GLB/FBX.

        Args:
            task_id: The response ID
            output_path: Path to save the model

        Returns:
            Path to the saved file
        """
        import zipfile
        import io
        import os

        result = self.poll_status(task_id)

        if result.status != TaskStatus.COMPLETED or not result.download_url:
            raise Exception(f"Result not ready or URL missing. Status: {result.status}")

        # Download the model (usually a ZIP file) — streaming with progress
        # 用独立 client，不受 API 轮询的 300s timeout 限制
        import sys as _sys
        _tty = _sys.stdout.isatty()
        print(f"  [Download] starting ... ({result.download_url})", flush=True)
        t_dl = time.time()
        chunks = []
        received = 0
        last_logged_mb = 0.0
        _LOG_INTERVAL_MB = 5.0
        dl_timeout = httpx.Timeout(connect=30.0, read=300.0, write=None, pool=30.0)
        dl_mounts = (
            {"https://": httpx.HTTPTransport(proxy=self.config.download_proxy)}
            if self.config.download_proxy
            else {}
        )
        with httpx.Client(timeout=dl_timeout, mounts=dl_mounts, trust_env=False) as dl_client:
            with dl_client.stream("GET", result.download_url) as response:
                if response.status_code != 200:
                    raise Exception(f"Download failed: {response.status_code}")
                content_length = int(response.headers.get("content-length", 0))
                if not _tty:
                    mb_total_str = f"{content_length/1024/1024:.0f} MB" if content_length else "? MB"
                    print(f"  [Download] fetching {mb_total_str} ...", flush=True)
                for chunk in response.iter_bytes(chunk_size=512 * 1024):
                    chunks.append(chunk)
                    received += len(chunk)
                    elapsed_dl = time.time() - t_dl
                    mb_recv = received / 1024 / 1024
                    speed = mb_recv / elapsed_dl if elapsed_dl > 0.1 else 0
                    if _tty:
                        if content_length:
                            pct = received / content_length * 100
                            mb_total = content_length / 1024 / 1024
                            print(
                                f"\r  [Download] {mb_recv:.1f}/{mb_total:.1f} MB"
                                f"  ({pct:.0f}%)  {speed:.1f} MB/s   ",
                                end="", flush=True,
                            )
                        else:
                            print(
                                f"\r  [Download] {mb_recv:.1f} MB received"
                                f"  {speed:.1f} MB/s   ",
                                end="", flush=True,
                            )
                    else:
                        if mb_recv - last_logged_mb >= _LOG_INTERVAL_MB:
                            last_logged_mb = mb_recv
                            if content_length:
                                pct = received / content_length * 100
                                mb_total = content_length / 1024 / 1024
                                print(
                                    f"  [Download] {mb_recv:.0f}/{mb_total:.0f} MB"
                                    f"  ({pct:.0f}%)  {speed:.1f} MB/s",
                                    flush=True,
                                )
                            else:
                                print(
                                    f"  [Download] {mb_recv:.0f} MB received"
                                    f"  {speed:.1f} MB/s",
                                    flush=True,
                                )
        content = b"".join(chunks)
        elapsed_dl = time.time() - t_dl
        if _tty:
            print(
                f"\r  [Download] done — {len(content)/1024/1024:.1f} MB"
                f" in {elapsed_dl:.1f}s{' ' * 20}",
                flush=True,
            )
        else:
            print(
                f"  [Download] done — {len(content)/1024/1024:.1f} MB"
                f" in {elapsed_dl:.1f}s",
                flush=True,
            )


        if not content:
            raise Exception(f"Download failed: empty response")
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Check if it's a ZIP file (Hunyuan returns ZIP containing GLB/FBX)
        if content[:4] == b"PK\x03\x04":  # ZIP magic number
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                # Find the model file inside ZIP
                model_file = None
                for name in zf.namelist():
                    lower_name = name.lower()
                    if (
                        lower_name.endswith(".glb")
                        or lower_name.endswith(".gltf")
                        or lower_name.endswith(".fbx")
                    ):
                        model_file = name
                        break

                if not model_file:
                    # No model found, just list files
                    raise Exception(
                        f"No GLB/FBX found in ZIP. Contents: {zf.namelist()}"
                    )

                # Extract and save the model file
                model_data = zf.read(model_file)

                # Determine output extension from the extracted file
                ext = Path(model_file).suffix.lower()
                if not path.suffix:
                    path = path.with_suffix(ext)

                with open(path, "wb") as f:
                    f.write(model_data)
        else:
            # Direct model file (not ZIP)
            if not path.suffix:
                path = path.with_suffix(f".{self.config.output_format.lower()}")

            with open(path, "wb") as f:
                f.write(content)

        # CRITICAL: Validate file content
        try:
            self.validate_file_content(str(path))
        except ValueError as e:
            if os.path.exists(path):
                os.unlink(path)
            raise e

        return str(path)

    def close(self):
        """Close the HTTP client and clean up temporary files."""
        self.client.close()

        # Clean up temporary preprocessing directory
        if self._temp_dir and Path(self._temp_dir).exists():
            try:
                shutil.rmtree(self._temp_dir)
            except Exception:
                pass  # Ignore cleanup errors
            self._temp_dir = None
