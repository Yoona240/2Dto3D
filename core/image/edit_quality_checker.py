"""
Edit quality checker for multiview editing results.

The checker compares two stitched 3x2 collages:
1) Before-edit six views
2) After-edit six views (edited views override before views)

It asks a VLM to return a strict binary decision:
{"decision":"pass|fail","reason":"..."}
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from core.image.view_stitcher import VIEW_ORDER, ViewStitcher
from utils.llm_client import get_llm_client


EDIT_STATUS_PASSED = "passed"
EDIT_STATUS_FAILED_QUALITY = "failed_quality"
EDIT_STATUS_ERROR_QUALITY_CHECK = "error_quality_check"


def get_effective_edit_status(meta: dict[str, Any]) -> str:
    """Return edit status, defaulting legacy entries to passed."""
    status = meta.get("edit_status")
    if not isinstance(status, str) or not status.strip():
        return EDIT_STATUS_PASSED
    return status.strip()


def is_manual_override_approved(meta: dict[str, Any]) -> bool:
    """True when manual override is explicitly approved."""
    quality_check = meta.get("quality_check")
    if not isinstance(quality_check, dict):
        return False
    override = quality_check.get("manual_override")
    if not isinstance(override, dict):
        return False
    return override.get("approved") is True


def is_edit_batch_allowed(meta: dict[str, Any]) -> bool:
    """Gate for downstream 3D generation."""
    status = get_effective_edit_status(meta)
    if status == EDIT_STATUS_PASSED:
        return True
    return is_manual_override_approved(meta)


@dataclass
class EditQualityCheckResult:
    status: str
    reason: str
    before_grid_path: Optional[Path]
    after_grid_path: Optional[Path]
    raw_response: str


class EditQualityChecker:
    """Runs VLM-based quality check on before/after six-view collages."""

    def __init__(self, config: Any):
        self._config = config
        self._settings = config.edit_quality_check
        task_cfg = config.tasks["edit_quality_check"]
        self.checker_provider = task_cfg.provider
        self.checker_model = task_cfg.model
        self._client = get_llm_client(config.edit_quality_mllm)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @staticmethod
    def _safe_parse_json(text: str) -> dict[str, Any]:
        payload = text.strip()
        if payload.startswith("```"):
            payload = re.sub(r"^```(?:json)?", "", payload, flags=re.IGNORECASE).strip()
            payload = re.sub(r"```$", "", payload).strip()

        try:
            data = json.loads(payload)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", payload, flags=re.DOTALL)
        if not match:
            raise ValueError("Quality checker response does not contain JSON object")
        data = json.loads(match.group(0))
        if not isinstance(data, dict):
            raise ValueError("Quality checker JSON must be an object")
        return data

    def _build_effective_after_views(
        self,
        before_views_dir: Path,
        after_views_dir: Path,
        output_dir: Path,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)

        for view_name in VIEW_ORDER:
            before_path = before_views_dir / f"{view_name}.png"
            if not before_path.exists():
                raise FileNotFoundError(
                    f"Missing required before view: {before_path}"
                )

            after_path = after_views_dir / f"{view_name}.png"
            source_path = after_path if after_path.exists() else before_path
            shutil.copy2(source_path, output_dir / f"{view_name}.png")

        return output_dir

    def check(
        self,
        before_views_dir: Path,
        after_views_dir: Path,
        instruction: str,
        work_root_dir: Path,
        allow_stage1_relabel: Optional[bool] = None,
    ) -> EditQualityCheckResult:
        if not self._settings.enabled:
            return EditQualityCheckResult(
                status=EDIT_STATUS_PASSED,
                reason="quality check disabled",
                before_grid_path=None,
                after_grid_path=None,
                raw_response="",
            )

        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction must be a non-empty string")

        before_views_dir = Path(before_views_dir)
        after_views_dir = Path(after_views_dir)
        work_root_dir = Path(work_root_dir)

        qc_dir = work_root_dir / self._settings.temp_dir_name
        if qc_dir.exists():
            shutil.rmtree(qc_dir, ignore_errors=True)
        qc_dir.mkdir(parents=True, exist_ok=True)

        effective_after_dir = self._build_effective_after_views(
            before_views_dir=before_views_dir,
            after_views_dir=after_views_dir,
            output_dir=qc_dir / "_effective_after_views",
        )

        stitcher = ViewStitcher()
        before_grid_path = qc_dir / "before_grid.png"
        after_grid_path = qc_dir / "after_grid.png"
        stitcher.stitch_views(
            views_dir=before_views_dir,
            output_path=before_grid_path,
            view_names=VIEW_ORDER,
            pad_to_square=True,
        )
        stitcher.stitch_views(
            views_dir=effective_after_dir,
            output_path=after_grid_path,
            view_names=VIEW_ORDER,
            pad_to_square=True,
        )

        system_prompt = (
            "You are a strict image edit quality checker. "
            "Return JSON only. Do not output markdown."
        )
        user_prompt = (
            "You will receive two stitched 3x2 multiview images of the same object.\n"
            "Focus on visual evidence only. Do not guess missing details.\n\n"
            "Image 1: BEFORE edit\n"
            "Image 2: AFTER edit\n\n"
            "View order is fixed:\n\n"
            "Row1: front, back, right\n"
            "Row2: left, top, bottom\n\n"
            "Edit instruction:\n"
            f"{instruction}\n\n"
            "Your task is to determine whether the AFTER image correctly applies the instruction while preserving unrelated content.\n\n"
            "First analyze the images step-by-step before giving the final decision.\n\n"
            "Step 1 - Identify the target\n"
            "Determine which object or region the instruction intends to modify.\n\n"
            "Only the target region is allowed to change.\n\n"
            "Step 2 - Same-view comparison\n"
            "For each view in the following order:\n\n"
            "front, back, right, left, top, bottom\n\n"
            "Compare BEFORE and AFTER for that view and check:\n\n"
            "- Is the edit target visible in this view?\n"
            "- If visible: is the requested edit correctly applied?\n"
            "- Are there unintended changes outside the target region?\n\n"
            "Step 3 - Cross-view consistency\n\n"
            "If the target is visible in multiple views:\n\n"
            "- The edited appearance should be consistent across views\n"
            "- Color/material changes should appear consistently\n"
            "- Geometry changes should not contradict across views\n\n"
            "Step 4 - Non-target preservation\n\n"
            "Regions unrelated to the instruction should remain visually consistent between BEFORE and AFTER, including:\n\n"
            "- shape\n"
            "- texture\n"
            "- material\n"
            "- color\n"
            "- layout\n\n"
            "Step 5 - Visibility rule\n\n"
            "If the target is not visible in a view, that view should remain unchanged.\n\n"
            "Step 6 - Artifact check\n\n"
            "Ensure there are no artifacts such as:\n\n"
            "- duplicated parts\n"
            "- missing unrelated parts\n"
            "- broken geometry\n"
            "- floating fragments\n"
            "- corrupted textures\n"
            "- unintended global style drift\n\n"
            "Decision rule:\n\n"
            'Output "pass" ONLY if all requirements are satisfied.\n'
            'If any requirement fails, output "fail".\n\n'
            "Return JSON:\n\n"
            '{"decision":"pass|fail","reason":"short explanation"}'
        )

        log_dir = qc_dir / "vlm_logs" if self._settings.save_debug_assets else None
        response_text = self._client.chat_with_images(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=[before_grid_path, after_grid_path],
            log_dir=log_dir,
        )
        parsed = self._safe_parse_json(response_text)

        decision = parsed.get("decision")
        if not isinstance(decision, str):
            raise ValueError("quality checker JSON missing string field: decision")
        decision = decision.strip().lower()
        if decision not in {"pass", "fail"}:
            raise ValueError(f"invalid decision value: {decision}")

        reason = parsed.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("quality checker JSON missing non-empty string field: reason")
        reason = reason.strip()

        status = (
            EDIT_STATUS_PASSED
            if decision == "pass"
            else EDIT_STATUS_FAILED_QUALITY
        )

        if self._settings.save_debug_assets:
            result_before = before_grid_path
            result_after = after_grid_path
        else:
            result_before = None
            result_after = None
            shutil.rmtree(qc_dir, ignore_errors=True)

        return EditQualityCheckResult(
            status=status,
            reason=reason,
            before_grid_path=result_before,
            after_grid_path=result_after,
            raw_response=response_text,
        )
