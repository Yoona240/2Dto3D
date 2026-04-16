"""
Base 3D Generator Module

Abstract base class for all 3D generation API clients.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Dict
import logging
import time

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """Status of a 3D generation task."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskResult:
    """Result of a 3D generation task."""

    task_id: str
    status: TaskStatus
    download_url: Optional[str] = None
    error_message: Optional[str] = None
    output_path: Optional[str] = None
    remote_task_id: Optional[str] = None  # Provider's task ID


class Base3DGenerator(ABC):
    """
    Abstract base class for 3D generation API clients.

    All 3D generators (Tripo, Hunyuan, Rodin) inherit from this class
    and implement the abstract methods.
    """

    # Download retry defaults.  Subclasses / callers may override via
    # constructor kwargs when the concrete config carries these values.
    DOWNLOAD_MAX_RETRIES = 3
    DOWNLOAD_RETRY_INTERVAL = 15  # seconds

    def __init__(
        self,
        poll_interval: int,
        max_wait_time: int,
        download_max_retries: int = DOWNLOAD_MAX_RETRIES,
        download_retry_interval: int = DOWNLOAD_RETRY_INTERVAL,
    ):
        """
        Initialize the generator.

        Args:
            poll_interval: Seconds between status checks
            max_wait_time: Maximum seconds to wait for generation
            download_max_retries: Max attempts for the download step alone
            download_retry_interval: Seconds between download retries
        """
        self.poll_interval = poll_interval
        self.max_wait_time = max_wait_time
        self.download_max_retries = download_max_retries
        self.download_retry_interval = download_retry_interval

    def validate_file_content(self, path: str) -> bool:
        """
        Validate that the file has expected magic numbers (GLB or ZIP).
        Raises ValueError if invalid.
        """
        import pathlib

        path_obj = pathlib.Path(path)
        if not path_obj.exists() or path_obj.stat().st_size == 0:
            raise ValueError(f"File is empty or missing: {path}")

        with open(path_obj, "rb") as f:
            header = f.read(4)

        # Magic numbers: GLB (glTF) or ZIP (PK..)
        if header == b"glTF" or header.startswith(b"PK"):
            return True

        # Check if it looks like HTML (common API error response saved as file)
        if header.strip().startswith(b"<") or b"<!DOCTYPE" in header:
            # Read a bit more to be sure
            with open(path_obj, "r", errors="ignore") as f:
                content = f.read(100)
                if "<html" in content.lower() or "<body" in content.lower():
                    raise ValueError(
                        f"File appears to be an HTML error page, not a 3D model: {path}"
                    )

        # If strict validation is needed for other formats, add here.
        # For now, just ensuring it's not HTML is a big improvement.
        return True

    @abstractmethod
    def submit_task(
        self,
        image_path: str,
        prompt: Optional[str] = None,
        multi_view_images: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """
        Submit an image-to-3D generation task.

        Args:
            image_path: Path to the input image
            prompt: Optional text prompt to guide generation
            multi_view_images: Optional list of additional view images,
                each dict with "path" and "view" (back/left/right)

        Returns:
            Task ID for polling status
        """
        pass

    @abstractmethod
    def poll_status(self, task_id: str) -> TaskResult:
        """
        Check the status of a generation task.

        Args:
            task_id: The task ID returned from submit_task

        Returns:
            TaskResult with current status and download URL if complete
        """
        pass

    @abstractmethod
    def download_result(self, task_id: str, output_path: str) -> str:
        """
        Download the generated 3D model.

        Args:
            task_id: The task ID
            output_path: Path to save the 3D model

        Returns:
            Path to the saved file
        """
        pass

    def generate(
        self,
        image_path: str,
        output_path: str,
        prompt: Optional[str] = None,
        multi_view_images: Optional[List[Dict[str, str]]] = None,
    ) -> TaskResult:
        """
        Complete generation workflow: submit, poll, download.

        Args:
            image_path: Path to the input image
            output_path: Path to save the 3D model
            prompt: Optional text prompt
            multi_view_images: Optional list of additional view images,
                each dict with "path" and "view" (back/left/right)

        Returns:
            TaskResult with final status and output path
        """
        # Submit the task
        task_id = self.submit_task(image_path, prompt, multi_view_images)
        logger.info("Task submitted: %s", task_id)

        # Wait and download
        return self.wait_and_download(task_id, output_path)

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"

    def wait_and_download(self, task_id: str, output_path: str) -> TaskResult:
        """
        Wait for task completion and download result.

        Download failures are retried independently — a download timeout will
        NOT cause the whole task to be re-submitted (Phase-0 fix for R3).

        Args:
            task_id: The task ID to poll
            output_path: Path to save the 3D model

        Returns:
            TaskResult with final status and output path
        """
        short_id = task_id[-16:] if len(task_id) > 16 else task_id
        # ---------- Poll until task completes ----------
        start_time = time.time()
        result: Optional[TaskResult] = None
        while True:
            result = self.poll_status(task_id)
            elapsed = time.time() - start_time
            logger.info("Task %s: %s", task_id, result.status.value)
            print(
                f"  [Poll] ...{short_id}  elapsed={self._fmt_elapsed(elapsed)}"
                f"  status={result.status.value}  (next in {self.poll_interval}s)",
                flush=True,
            )

            if result.status == TaskStatus.COMPLETED:
                break

            if result.status == TaskStatus.FAILED:
                return result

            elapsed = time.time() - start_time
            if elapsed > self.max_wait_time:
                result.status = TaskStatus.FAILED
                result.error_message = f"Timeout after {self.max_wait_time} seconds"
                return result

            time.sleep(self.poll_interval)

        # ---------- Download with independent retry ----------
        for dl_attempt in range(1, self.download_max_retries + 1):
            try:
                output_file = self.download_result(task_id, output_path)
                result.output_path = output_file
                return result
            except Exception as exc:
                if dl_attempt >= self.download_max_retries:
                    logger.error(
                        "Download failed after %d attempts for task %s: %s",
                        self.download_max_retries,
                        task_id,
                        exc,
                    )
                    raise
                exc_type = type(exc).__name__
                logger.warning(
                    "Download attempt %d/%d failed for task %s: %s (%s) — "
                    "re-polling for fresh URL and retrying in %ds",
                    dl_attempt,
                    self.download_max_retries,
                    task_id,
                    exc,
                    exc_type,
                    self.download_retry_interval,
                )
                print(
                    f"  [Download] attempt {dl_attempt}/{self.download_max_retries} failed"
                    f" ({exc_type}): {exc}",
                    flush=True,
                )
                # Re-poll to get a fresh download URL (CDN URLs can expire)
                refreshed = self.poll_status(task_id)
                if refreshed.status == TaskStatus.COMPLETED:
                    result = refreshed
                time.sleep(self.download_retry_interval)

        # Should be unreachable, but satisfy type checkers
        raise RuntimeError(f"Download loop exited unexpectedly for task {task_id}")

    @abstractmethod
    def close(self):
        """Clean up resources."""
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
