"""
Rodin Gen2 Generator Module

Client for Rodin 3D generation API via Hyper3D.
"""

import base64
import httpx
from pathlib import Path
from typing import Optional, List, Dict

from .base import Base3DGenerator, TaskResult, TaskStatus
from utils.config import RodinConfig
from utils.validation import require_api_field


class RodinGenerator(Base3DGenerator):
    """
    Client for Rodin Gen2 API via Hyper3D.

    Supports high-fidelity 3D generation with PBR materials.
    """

    def __init__(self, config: RodinConfig, poll_interval: int, max_wait_time: int):
        """
        Initialize the Rodin generator.

        Args:
            config: Rodin API configuration
            poll_interval: Seconds between status checks
            max_wait_time: Maximum seconds to wait
        """
        super().__init__(poll_interval, max_wait_time)
        self.config = config
        transport = httpx.HTTPTransport(retries=config.max_retries)
        self.client = httpx.Client(timeout=float(config.timeout), transport=transport)

    def _get_headers(self) -> dict:
        """Get common headers for API requests."""
        return {"Authorization": f"Bearer {self.config.api_key}"}

    def submit_task(
        self,
        image_path: str,
        prompt: Optional[str] = None,
        multi_view_images: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """
        Submit an image-to-3D task to Rodin.

        Args:
            image_path: Path to the input image
            prompt: Optional text prompt to guide generation
            multi_view_images: Not supported by Rodin, ignored

        Returns:
            Task UUID
        """
        path = Path(image_path)

        # Prepare multipart form data
        with open(path, "rb") as f:
            files = {"images": (path.name, f, "image/png")}

            data = {
                "tier": self.config.tier,
                "geometry_file_format": self.config.output_format,
                "material": "PBR",
                "quality": "high",
            }

            if prompt:
                data["prompt"] = prompt

            response = self.client.post(
                f"{self.config.base_url}/rodin",
                headers=self._get_headers(),
                data=data,
                files=files,
            )

        if response.status_code != 200:
            raise Exception(
                f"Rodin API error: {response.status_code} - {response.text}"
            )

        result = response.json()

        # Extract task UUID from response - must be present
        if "uuid" in result:
            task_uuid = result["uuid"]
        elif "task_uuid" in result:
            task_uuid = result["task_uuid"]
        elif "job_id" in result:
            task_uuid = result["job_id"]
        else:
            raise ValueError(
                f"Rodin API response missing required field: uuid/task_uuid/job_id. Response: {result}"
            )

        return task_uuid

    def poll_status(self, task_id: str) -> TaskResult:
        """
        Check the status of a Rodin task.

        Args:
            task_id: The task UUID

        Returns:
            TaskResult with current status
        """
        response = self.client.get(
            f"{self.config.base_url}/rodin/{task_id}", headers=self._get_headers()
        )

        if response.status_code != 200:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=f"Status check failed: {response.status_code}",
            )

        result = response.json()

        status_str = require_api_field(result, "status", "Rodin").lower()

        if status_str in ["completed", "done", "success"]:
            # Get download URLs - validate required fields
            jobs = require_api_field(result, "jobs", "Rodin")
            if not jobs:
                raise ValueError("Rodin API response has empty 'jobs' array")

            outputs = require_api_field(jobs[0], "output", "Rodin")

            # Check for model URL in either 'model' or 'glb' field
            if "model" in outputs:
                download_url = outputs["model"]
            elif "glb" in outputs:
                download_url = outputs["glb"]
            else:
                raise ValueError(
                    f"Rodin API response missing required field: model/glb in output. Output: {outputs}"
                )

            return TaskResult(
                task_id=task_id, status=TaskStatus.COMPLETED, download_url=download_url
            )
        elif status_str in ["failed", "error"]:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=result.get("error")
                or result.get("message"),  # Optional error details
            )
        else:
            return TaskResult(task_id=task_id, status=TaskStatus.PROCESSING)

    def download_result(self, task_id: str, output_path: str) -> str:
        """
        Download the generated 3D model.

        Args:
            task_id: The task UUID
            output_path: Path to save the model

        Returns:
            Path to the saved file
        """
        result = self.poll_status(task_id)

        if not result.download_url:
            raise Exception("No download URL available")

        response = self.client.get(result.download_url)

        if response.status_code != 200:
            raise Exception(f"Download failed: {response.status_code}")

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if not path.suffix:
            path = path.with_suffix(f".{self.config.output_format}")

        with open(path, "wb") as f:
            f.write(response.content)

        # CRITICAL: Validate file content
        try:
            self.validate_file_content(str(path))
        except ValueError as e:
            if path.exists():
                path.unlink()
            raise

        return str(path)

    def close(self):
        """Close the HTTP client."""
        self.client.close()
