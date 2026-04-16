"""
Tripo 3.0 Generator Module

Client for Tripo 3D generation API.
"""

import base64
import httpx
import time
from pathlib import Path
from typing import Optional, List, Dict

from .base import Base3DGenerator, TaskResult, TaskStatus
from utils.config import TripoConfig


class TripoGenerator(Base3DGenerator):
    """
    Client for Tripo 3.0 API.
    
    Supports image-to-3D generation with high-quality topology.
    """
    
    def __init__(self, config: TripoConfig, poll_interval: int, max_wait_time: int):
        """
        Initialize the Tripo generator.
        
        Args:
            config: Tripo API configuration
            poll_interval: Seconds between status checks
            max_wait_time: Maximum seconds to wait
        """
        super().__init__(poll_interval, max_wait_time)
        self.config = config
        self._task_options: Dict[str, object] = {}
        # Use transport with retry to handle connection resets.
        # Note: explicitly passing transport= disables httpx's env-var proxy detection,
        # so we manually read http_proxy / https_proxy here and pass them in.
        import os
        proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or \
                    os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
        transport = httpx.HTTPTransport(retries=config.max_retries, proxy=proxy_url)
        self.client = httpx.Client(timeout=float(config.timeout), transport=transport)

    def set_task_options(self, options: Optional[Dict[str, object]] = None):
        """Set per-task generation options (used for next submission only)."""
        self._task_options = dict(options or {})

    def _build_generation_options(self, task_options: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        """Build validated Tripo optional parameters from config + per-task overrides."""
        merged: Dict[str, object] = {
            "model_seed": self.config.model_seed,
            "texture_seed": self.config.texture_seed,
            "texture": self.config.texture,
            "pbr": self.config.pbr,
            "texture_quality": self.config.texture_quality,
            "texture_alignment": self.config.texture_alignment,
            "face_limit": self.config.face_limit,
            "export_uv": self.config.export_uv,
        }
        if task_options:
            merged.update(task_options)

        options: Dict[str, object] = {}
        valid_texture_quality = {"detailed", "standard"}
        valid_texture_alignment = {"original_image", "geometry"}

        texture_quality = merged.get("texture_quality")
        if texture_quality is not None and texture_quality not in valid_texture_quality:
            raise ValueError(
                f"Invalid tripo.texture_quality: {texture_quality}. "
                f"Expected one of {sorted(valid_texture_quality)}"
            )

        texture_alignment = merged.get("texture_alignment")
        if texture_alignment is not None and texture_alignment not in valid_texture_alignment:
            raise ValueError(
                f"Invalid tripo.texture_alignment: {texture_alignment}. "
                f"Expected one of {sorted(valid_texture_alignment)}"
            )

        face_limit = merged.get("face_limit")
        if face_limit is not None:
            if not isinstance(face_limit, int):
                raise ValueError(f"Invalid tripo.face_limit type: {type(face_limit).__name__}, expected int")
            if face_limit < 1000 or face_limit > 20000:
                raise ValueError("Invalid tripo.face_limit: expected in range [1000, 20000]")

        for key in [
            "model_seed",
            "texture_seed",
            "texture",
            "pbr",
            "texture_quality",
            "texture_alignment",
            "face_limit",
            "export_uv",
        ]:
            value = merged.get(key)
            if value is not None:
                options[key] = value

        return options
    
    def _get_headers(self) -> dict:
        """Get common headers for API requests."""
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json"
        }
    
    def _encode_image(self, image_path: str) -> str:
        """Encode image to base64."""
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    
    def submit_task(self, image_path: str, prompt: Optional[str] = None, multi_view_images: Optional[List[Dict[str, str]]] = None) -> str:
        """
        Submit an image-to-3D task to Tripo.
        
        If multi_view_images are provided, uses multiview_to_model mode.
        Otherwise uses single image_to_model mode.
        
        Args:
            image_path: Path to the input image (front view)
            prompt: Optional text prompt
            multi_view_images: Optional list of additional views, each dict with:
                - "path": path to image file
                - "view": view name (back/left/right)
            
        Returns:
            Task ID
        """
        task_options = self._task_options
        self._task_options = {}

        # If multi-view images provided, use multiview mode
        if multi_view_images:
            # Extract view paths from multi_view_images
            view_paths = {img["view"]: img["path"] for img in multi_view_images}
            print(f"  Using Tripo multiview mode with views: front + {list(view_paths.keys())}")
            return self.submit_multiview_task(
                front_path=image_path,
                left_path=view_paths.get("left"),
                back_path=view_paths.get("back"),
                right_path=view_paths.get("right"),
                task_options=task_options,
            )
        
        # Single image mode
        image_data = self._encode_image(image_path)
        
        payload = {
            "type": "image_to_model",
            "file": {
                "type": "base64",
                "data": image_data
            },
            "model_version": self.config.model_version
        }

        payload.update(self._build_generation_options(task_options))

        # Add geometry_quality if specified (for Ultra Mode)
        if hasattr(self.config, 'geometry_quality') and self.config.geometry_quality:
            payload["geometry_quality"] = self.config.geometry_quality
        
        if prompt:
            payload["prompt"] = prompt
        
        # Debug: print payload (mask sensitive data)
        debug_payload = dict(payload)
        if "file" in debug_payload:
            debug_payload["file"] = {"type": debug_payload["file"]["type"], "data": "[base64 data]"}
        print(f"  [Tripo Single] Payload: {debug_payload}")
        
        response = self.client.post(
            f"{self.config.base_url}/task",
            headers=self._get_headers(),
            json=payload
        )
        
        if response.status_code != 200:
            raise Exception(f"Tripo API error: {response.status_code} - {response.text}")
        
        result = response.json()
        
        if result.get('code') != 0:
            raise Exception(f"Tripo API error: {result.get('message')}")
        
        return result['data']['task_id']
    
    def upload_image(self, image_path: str) -> str:
        """
        Upload image to Tripo and get file_token.
        
        Args:
            image_path: Path to the image file
            
        Returns:
            file_token (image_token) for use in task submission
        """
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")
        
        upload_url = f"{self.config.base_url}/upload"
        
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
        }
        
        with open(path, 'rb') as f:
            files = {'file': (path.name, f, 'image/png')}
            response = self.client.post(upload_url, headers=headers, files=files)
        
        if response.status_code != 200:
            raise Exception(f"Upload failed: {response.status_code} - {response.text}")
        
        result = response.json()
        
        if result.get('code') != 0:
            raise Exception(f"Upload error: {result.get('message')}")
        
        return result['data']['image_token']
    
    def submit_multiview_task(
        self, 
        front_path: str,
        left_path: Optional[str] = None,
        back_path: Optional[str] = None,
        right_path: Optional[str] = None,
        task_options: Optional[Dict[str, object]] = None,
    ) -> str:
        """
        Submit a multi-view 3D generation task to Tripo.
        
        Args:
            front_path: Path to front view image (required)
            left_path: Path to left view image (optional)
            back_path: Path to back view image (optional)
            right_path: Path to right view image (optional)
            
        Returns:
            Task ID
        """
        # Upload images and build files array
        # Order: [Front, Left, Back, Right]
        files_list = []
        
        # Front (required)
        front_token = self.upload_image(front_path)
        files_list.append({"file_token": front_token})
        
        # Left (optional)
        if left_path:
            left_token = self.upload_image(left_path)
            files_list.append({"file_token": left_token})
        else:
            files_list.append({})
        
        # Back (optional)
        if back_path:
            back_token = self.upload_image(back_path)
            files_list.append({"file_token": back_token})
        else:
            files_list.append({})
        
        # Right (optional)
        if right_path:
            right_token = self.upload_image(right_path)
            files_list.append({"file_token": right_token})
        else:
            files_list.append({})
        
        # Build payload
        payload = {
            "type": "multiview_to_model",
            "model_version": self.config.model_version,
            "files": files_list,
        }
        payload.update(self._build_generation_options(task_options))
        
        # Add geometry_quality if specified (for Ultra Mode)
        if hasattr(self.config, 'geometry_quality') and self.config.geometry_quality:
            payload["geometry_quality"] = self.config.geometry_quality
        
        # Debug: print payload (mask sensitive data)
        debug_payload = dict(payload)
        debug_payload["files"] = f"[{len(files_list)} files]"
        print(f"  [Tripo Multiview] Payload: {debug_payload}")
        
        response = self.client.post(
            f"{self.config.base_url}/task",
            headers=self._get_headers(),
            json=payload
        )
        
        if response.status_code != 200:
            raise Exception(f"Tripo API error: {response.status_code} - {response.text}")
        
        result = response.json()
        
        if result.get('code') != 0:
            raise Exception(f"Tripo API error: {result.get('message')}")
        
        return result['data']['task_id']
    
    def poll_status(self, task_id: str) -> TaskResult:
        """
        Check the status of a Tripo task.
        
        Args:
            task_id: The task ID
            
        Returns:
            TaskResult with current status
        """
        response = self.client.get(
            f"{self.config.base_url}/task/{task_id}",
            headers=self._get_headers()
        )
        
        if response.status_code != 200:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=f"Status check failed: {response.status_code}"
            )
        
        result = response.json()
        
        if result.get('code') != 0:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=result.get('message')
            )
        
        data = result['data']
        status_map = {
            'queued': TaskStatus.PENDING,
            'running': TaskStatus.PROCESSING,
            'success': TaskStatus.COMPLETED,
            'failed': TaskStatus.FAILED
        }
        
        status = status_map.get(data.get('status'), TaskStatus.PENDING)
        download_url = None
        
        if status == TaskStatus.COMPLETED:
            # Get the model download URL
            # According to Tripo API docs, URLs are in 'output' field:
            # - output.pbr_model: URL for PBR model (expires after 5 min)
            # - output.model: URL for standard model
            # - output.base_model: URL for base model
            output_data = data.get('output', {})
            
            # Try pbr_model first (highest quality with PBR materials)
            if 'pbr_model' in output_data:
                download_url = output_data['pbr_model']
            # Fallback to standard model
            elif 'model' in output_data:
                download_url = output_data['model']
            # Fallback to base_model
            elif 'base_model' in output_data:
                download_url = output_data['base_model']
            # Legacy: check result field for backward compatibility
            else:
                result_data = data.get('result', {})
                if 'pbr_model' in result_data:
                    download_url = result_data['pbr_model'].get('url')
                elif 'model' in result_data:
                    download_url = result_data['model'].get('url')

            # CRITICAL: Only return COMPLETED if download_url is actually found
            if not download_url:
                print(f"Warning: Could not find download URL in data: {data.keys()}")
                return TaskResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    error_message="Task completed but no download URL found in response",
                    remote_task_id=task_id
                )

        return TaskResult(
            task_id=task_id,
            status=status,
            download_url=download_url,
            error_message=data.get('message') if status == TaskStatus.FAILED else None,
            remote_task_id=task_id
        )

    def download_result(self, task_id: str, output_path: str) -> str:
        """
        Download the generated 3D model.

        CRITICAL: Tripo download URLs expire after 5 minutes. We must re-fetch
        the URL on each retry to ensure we have a fresh URL.

        Args:
            task_id: The task ID
            output_path: Path to save the model

        Returns:
            Path to the saved file
        """
        import os

        # Ensure output directory exists
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Add extension if not present
        if not path.suffix:
            path = path.with_suffix(f".{self.config.output_format}")

        # Download with streaming + retries to avoid large-file read timeout
        # Note: request-level timeout overrides client default timeout.
        download_timeout = httpx.Timeout(
            connect=float(self.config.timeout),
            read=max(float(self.config.timeout), 300.0),
            write=float(self.config.timeout),
            pool=float(self.config.timeout),
        )
        attempts = max(1, int(self.config.max_retries) + 1)
        tmp_path = path.with_suffix(path.suffix + ".part")
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            # CRITICAL: Re-fetch URL on each attempt because Tripo URLs expire after 5 minutes
            result = self.poll_status(task_id)
            if not result.download_url:
                raise Exception("No download URL available")

            try:
                print(f"  Downloading from Tripo CDN... (attempt {attempt}/{attempts})")
                with self.client.stream(
                    "GET",
                    result.download_url,
                    timeout=download_timeout,
                    follow_redirects=True,
                ) as response:
                    if response.status_code != 200:
                        raise Exception(f"Download failed: {response.status_code}")

                    # Get total size if available
                    total_size = response.headers.get('content-length')
                    total_size = int(total_size) if total_size else None
                    
                    downloaded = 0
                    last_print_time = time.time()
                    with open(tmp_path, 'wb') as f:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                # Print progress every 5 seconds
                                if time.time() - last_print_time > 5:
                                    if total_size:
                                        pct = downloaded / total_size * 100
                                        print(f"  Download progress: {pct:.1f}% ({downloaded}/{total_size} bytes)")
                                    else:
                                        print(f"  Downloaded: {downloaded / 1024 / 1024:.1f} MB...")
                                    last_print_time = time.time()
                    
                    # Final progress
                    if total_size:
                        print(f"  Download complete: {downloaded / 1024 / 1024:.1f} MB")

                os.replace(tmp_path, path)
                break
            except (httpx.ReadTimeout, httpx.HTTPError, OSError) as e:
                last_error = e
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                if attempt >= attempts:
                    raise Exception(
                        f"Download failed after {attempts} attempts: {e}"
                    ) from e
                wait_seconds = min(2 ** (attempt - 1), 5)
                print(
                    f"Download attempt {attempt}/{attempts} failed: {e}; retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)

        if last_error and not os.path.exists(path):
            raise Exception(f"Download failed: {last_error}")

        # CRITICAL: Validate file content
        try:
            self.validate_file_content(str(path))
        except ValueError as e:
            if os.path.exists(path):
                os.unlink(path)
            raise e

        return str(path)
    
    def close(self):
        """Close the HTTP client."""
        self.client.close()
