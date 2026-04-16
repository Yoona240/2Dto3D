"""
Quality check method router.

Dispatches to Method-1 (grid_vlm), Method-2 (two_stage_recon), or
Method-3 (unified_judge) based on ``config.edit_quality_check.method``.

All callers should go through ``create_quality_checker()`` and
``build_quality_check_meta()`` so that method-specific logic is
encapsulated here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from core.image.edit_quality_checker import (
    EDIT_STATUS_ERROR_QUALITY_CHECK,
    EDIT_STATUS_PASSED,
    EditQualityCheckResult,
    EditQualityChecker,
)
from core.image.edit_quality_checker_v2 import (
    EditCorrectnessDetail,
    EditQualityCheckerV2,
)
from core.image.edit_quality_checker_unified import EditQualityCheckerUnified

logger = logging.getLogger(__name__)

# Type alias for any checker
QualityChecker = Union[EditQualityChecker, EditQualityCheckerV2, EditQualityCheckerUnified]


def create_quality_checker(config: Any) -> QualityChecker:
    """Factory: return the appropriate checker based on config.edit_quality_check.method."""
    method = config.edit_quality_check.method
    if method == "grid_vlm":
        return EditQualityChecker(config)
    elif method == "two_stage_recon":
        return EditQualityCheckerV2(config)
    elif method == "unified_judge":
        return EditQualityCheckerUnified(config)
    else:
        raise ValueError(f"Unknown edit_quality_check.method: {method!r}")


def build_quality_check_meta(
    *,
    enabled: bool,
    result: Optional[EditQualityCheckResult],
    method: str,
    checker_provider: str,
    checker_model: str,
    error_message: str = "",
    path_formatter: Optional[Callable[[Path], str]] = None,
) -> Dict[str, Any]:
    """Build the ``quality_check`` dict for meta.json.

    This is the **single source of truth** for the quality_check meta schema.
    Both ``batch_process.py`` and ``app.py`` call this instead of maintaining
    their own local ``_build_quality_check_meta()`` helpers.

    Args:
        enabled: Whether quality checking was enabled.
        result: The checker result.  ``None`` when quality check is disabled
                (in that case *status* defaults to ``passed``).
        method: ``"grid_vlm"``, ``"two_stage_recon"``, or ``"unified_judge"``.
        checker_provider: Provider name from task config.
        checker_model: Model name from task config.
        error_message: Optional error string when status is ``error_quality_check``.
        path_formatter: Optional callable to convert ``Path`` to a string
                        suitable for meta.json (e.g. ``_rel_path``).  When
                        ``None``, ``str()`` is used.
    """
    fmt = path_formatter or str

    # Determine status & reason from result (or defaults when disabled)
    if result is not None:
        status = result.status
        reason = result.reason
    else:
        status = EDIT_STATUS_PASSED
        reason = "quality check disabled"

    base: Dict[str, Any] = {
        "enabled": enabled,
        "method": method,
        "status": status,
        "reason": reason,
        "checker_provider": checker_provider,
        "checker_model": checker_model,
        "checked_at": datetime.now().isoformat(),
        "error_message": error_message,
    }

    if result is not None:
        if method == "grid_vlm":
            base["raw_response"] = result.raw_response
            base["before_grid_path"] = (
                fmt(result.before_grid_path) if result.before_grid_path else None
            )
            base["after_grid_path"] = (
                fmt(result.after_grid_path) if result.after_grid_path else None
            )
        elif method in ("two_stage_recon", "unified_judge"):
            detail: Optional[EditCorrectnessDetail] = getattr(result, "detail", None)
            if detail is not None:
                sec: Dict[str, Any] = {
                    "status": detail.status,
                    "view_policy": detail.view_policy,
                    "checked_views": detail.checked_views,
                    "view_sanity_result": detail.view_sanity_result,
                    "relabel_result": detail.relabel_result,
                    "rejudge_result": detail.rejudge_result,
                    "original_instruction": detail.original_instruction,
                    "candidate_rewrite_instruction": detail.candidate_rewrite_instruction,
                    "effective_instruction": detail.effective_instruction,
                    "relabel_reason": detail.relabel_reason,
                    "instruction_display_source": detail.instruction_display_source,
                    "instruction_display_status": detail.instruction_display_status,
                    "reason": detail.reason,
                }
                if method == "two_stage_recon":
                    sec["diff_result"] = detail.diff_result
                    sec["judge_result"] = detail.judge_result
                elif method == "unified_judge":
                    sec["unified_result"] = detail.unified_result
                base["stage_edit_correctness"] = sec

    return base


def get_checker_info(config: Any) -> tuple[str, str]:
    """Return (provider, model) for the active quality check method.

    Used when building meta without a checker instance.
    """
    method = config.edit_quality_check.method
    if method == "grid_vlm":
        task = config.tasks["edit_quality_check"]
    elif method == "two_stage_recon":
        # Surface the diff model as the primary identifier
        task = config.tasks["edit_quality_check_diff"]
    elif method == "unified_judge":
        task = config.tasks["edit_quality_check_unified"]
    else:
        raise ValueError(f"Unknown edit_quality_check.method: {method!r}")
    return task.provider, task.model
