#!/usr/bin/env python3
"""
Unified batch processing script for 3D generation, rendering, and editing.

Respects concurrency limits from config.yaml and provides:
- Parallel execution with thread pool
- Skip existing results (checkpoint/resume support)
- Progress tracking
- Error handling and reporting

Usage:
    # Generate 3D models for all images without 3D
    python scripts/batch_process.py gen3d --provider hunyuan

    # Generate 3D for specific IDs
    python scripts/batch_process.py gen3d --provider hunyuan --ids id1 id2 id3

    # Render all models without views
    python scripts/batch_process.py render

    # Render specific models
    python scripts/batch_process.py render --ids id1 id2 id3

    # Edit views using each model's own instructions (skips already edited)
    python scripts/batch_process.py edit --ids id1 id2 id3

    # Edit using specific instruction index (0=first, 1=second)
    python scripts/batch_process.py edit --ids id1 id2 --instr-index 0

    # Edit using all instructions for each model
    python scripts/batch_process.py edit --ids id1 id2 --all-instructions

    # Edit using all instructions, but limit to 1 per type (remove/replace)
    python scripts/batch_process.py edit --ids id1 id2 --all-instructions --max-per-type 1

    # Force re-edit even if same instruction was already applied
    python scripts/batch_process.py edit --ids id1 id2 --force

    # Generate Target 3D from edited views (skips already generated)
    python scripts/batch_process.py gen3d-from-edits --provider hunyuan

    # Generate Target 3D for specific source models
    python scripts/batch_process.py gen3d-from-edits --provider hunyuan --ids model1 model2

    # Check Target 3D consistency (Method-2 Stage 2)
    python scripts/batch_process.py check-target-consistency --provider hunyuan --ids model1 --edit-id a1b2c3d4

    # Force regenerate (ignore existing)
    python scripts/batch_process.py gen3d --provider hunyuan --force

    # Dry run (show what would be processed)
    python scripts/batch_process.py gen3d --provider hunyuan --dry-run
"""

import argparse
import json
import os
import re
import sys
import threading
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Setup project path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import load_config
from core.image.edit_quality_checker import (
    EDIT_STATUS_ERROR_QUALITY_CHECK,
    EDIT_STATUS_FAILED_QUALITY,
    EDIT_STATUS_PASSED,
    get_effective_edit_status,
    is_edit_batch_allowed,
)
from core.image.edit_quality_router import (
    build_quality_check_meta,
    create_quality_checker,
    get_checker_info,
)
from core.image.instruction_display_resolver import (
    resolve_instruction_display_from_edit_meta,
    resolve_instruction_display_from_instruction_item,
)
from core.image.edit_artifact_builder import (
    build_edit_artifacts,
    materialize_missing_masks,
)

# Directories — resolved from config at startup
_config = load_config()
_pipeline_dir_raw = _config.workspace.pipeline_dir
_PIPELINE_DIR = (
    Path(_pipeline_dir_raw)
    if Path(_pipeline_dir_raw).is_absolute()
    else PROJECT_ROOT / _pipeline_dir_raw
)
IMAGES_DIR = _PIPELINE_DIR / "images"
MODELS_DIR = _PIPELINE_DIR / "models_src"
TRIPLETS_DIR = _PIPELINE_DIR / "triplets"

PROVIDER_ID_TO_NAME = {
    "tp3": "tripo",
    "hy3": "hunyuan",
    "rd2": "rodin",
}

_RECON_CHECKER_CLASS = None
_VLM_RECON_CHECKER_CLASS = None


def _classify_target_gen3d_error(error_message: str) -> str:
    if not isinstance(error_message, str) or not error_message.strip():
        return "unknown"
    message = error_message.strip()
    lowered = message.lower()
    if "requestlimitexceeded.jobnumexceed" in lowered or "配额超限" in message:
        return "quota_limit"
    if "主体占比值低于阈值" in message:
        return "subject_too_small"
    if "upload" in lowered or "上传" in message:
        return "upload_error"
    if "generation failed:" in lowered or "status=failed" in lowered:
        return "provider_failed"
    return "unknown"


def _rel_path(path: Path) -> str:
    """Return a URL-friendly path string for CLI output and meta.json.

    When pipeline_dir is inside PROJECT_ROOT, returns relative path.
    When pipeline_dir is external absolute path, returns pipeline/... format.
    """
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        pass
    if _PIPELINE_DIR is not None:
        try:
            rel = str(path.relative_to(_PIPELINE_DIR)).replace("\\", "/")
            return f"pipeline/{rel}"
        except ValueError:
            pass
    return str(path).replace("\\", "/")


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _build_edit_scope_id(model_id: Optional[str], edit_id: Optional[str]) -> Optional[str]:
    if model_id is None or edit_id is None:
        return None
    model_text = str(model_id).strip()
    edit_text = str(edit_id).strip()
    if not model_text or not edit_text:
        return None
    return f"{model_text}_edited_{edit_text}"


def _get_recon_checker_class():
    """Import ReconConsistencyChecker (LPIPS) once in the main process.

    Re-importing the module inside worker threads repeatedly hits /seaweedfs and
    makes the refresh-all path much more likely to fail with transient I/O
    errors. Cache the class after the first successful import.
    """
    global _RECON_CHECKER_CLASS
    if _RECON_CHECKER_CLASS is None:
        from core.render.recon_consistency_checker import ReconConsistencyChecker

        _RECON_CHECKER_CLASS = ReconConsistencyChecker
    return _RECON_CHECKER_CLASS


def _get_vlm_recon_checker_class():
    """Import VLMReconConsistencyChecker once in the main process (cached)."""
    global _VLM_RECON_CHECKER_CLASS
    if _VLM_RECON_CHECKER_CLASS is None:
        from core.render.recon_consistency_checker import VLMReconConsistencyChecker

        _VLM_RECON_CHECKER_CLASS = VLMReconConsistencyChecker
    return _VLM_RECON_CHECKER_CLASS


def _build_recon_checker(config):
    """Factory: return the right Stage-2 checker instance based on config."""
    stage2_method = config.edit_quality_check.two_stage_recon.stage2_method
    if stage2_method == "lpips":
        return _get_recon_checker_class()(config)
    if stage2_method == "vlm":
        return _get_vlm_recon_checker_class()(config)
    raise ValueError(
        f"Unknown edit_quality_check.two_stage_recon.stage2_method: {stage2_method!r}"
    )


class BatchProcessor:
    """Unified batch processor with concurrency control."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.results = {"success": [], "failed": [], "skipped": []}
        self.quality_check_semaphore = threading.Semaphore(
            config.concurrency.edit_quality_check
        )
        self.recon_quality_check_semaphore = threading.Semaphore(
            config.concurrency.recon_quality_check
        )

    def _emit_timing_log(
        self,
        *,
        phase: str,
        stage_name: str,
        status: Optional[str] = None,
        elapsed_seconds: Optional[float] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        parts = ["[Timing][%s]" % phase, "scope=edit", f"stage={stage_name}"]
        if status is not None:
            parts.append(f"status={status}")
        if elapsed_seconds is not None:
            parts.append(f"elapsed={elapsed_seconds:.3f}s")
        if context and context.get("edit_scope_id") is None:
            edit_scope_id = _build_edit_scope_id(
                context.get("source_model_id") or context.get("model_id"),
                context.get("edit_id"),
            )
            if edit_scope_id is not None:
                context = {**context, "edit_scope_id": edit_scope_id}
        if context:
            for key in [
                "source_model_id",
                "model_id",
                "edit_id",
                "edit_scope_id",
                "instruction",
                "provider_id",
            ]:
                value = context.get(key)
                if value is not None:
                    parts.append(f"{key}={value}")
        print(" ".join(parts))

    def _build_timing_entry(
        self,
        *,
        stage_name: str,
        status: str,
        started_at: str,
        finished_at: str,
        elapsed_seconds: float,
    ) -> Dict[str, Any]:
        return {
            "scope": "edit",
            "stage_name": stage_name,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": round(float(elapsed_seconds), 6),
            "attempt_index": 1,
            "api_lane": None,
        }

    def _run_timed_edit_stage(
        self,
        *,
        stage_name: str,
        context: Dict[str, Any],
        func,
        success_status: str = "success",
    ) -> Tuple[Any, Dict[str, Any]]:
        started_at = datetime.now().isoformat()
        started_perf = time.perf_counter()
        self._emit_timing_log(
            phase="START",
            stage_name=stage_name,
            context=context,
        )
        try:
            result = func()
        except Exception:
            finished_at = datetime.now().isoformat()
            elapsed_seconds = time.perf_counter() - started_perf
            timing_entry = self._build_timing_entry(
                stage_name=stage_name,
                status="failed",
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed_seconds,
            )
            self._emit_timing_log(
                phase="END",
                stage_name=stage_name,
                status="failed",
                elapsed_seconds=elapsed_seconds,
                context=context,
            )
            raise
        finished_at = datetime.now().isoformat()
        elapsed_seconds = time.perf_counter() - started_perf
        timing_entry = self._build_timing_entry(
            stage_name=stage_name,
            status=success_status,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
        )
        self._emit_timing_log(
            phase="END",
            stage_name=stage_name,
            status=success_status,
            elapsed_seconds=elapsed_seconds,
            context=context,
        )
        return result, timing_entry

    def _resolve_edit_views(
        self,
        model_id: str,
        views: List[str],
        provider_id: Optional[str],
    ) -> Tuple[Path, str, List[str]]:
        """Resolve source views directory and the valid views to edit."""
        views_base = TRIPLETS_DIR / model_id / "views"
        resolved_source_provider_id = provider_id

        if provider_id:
            views_dir = views_base / provider_id
        else:
            if not views_base.exists():
                raise FileNotFoundError(f"views directory not found: {views_base}")
            provider_dirs = [
                d for d in views_base.iterdir() if d.is_dir() and any(d.glob("*.png"))
            ]
            if provider_dirs:
                views_dir = provider_dirs[0]
                resolved_source_provider_id = views_dir.name
            else:
                raise ValueError(
                    "source_provider_id is required when views are not organized by provider subdirectory"
                )

        if not views_dir.exists():
            raise FileNotFoundError(f"views directory not found: {views_dir}")

        existing_views = [v.stem for v in views_dir.glob("*.png")]
        valid_views = [v for v in views if v in existing_views]
        if not valid_views:
            raise FileNotFoundError(
                f"no matching views found in {views_dir} for requested views: {views}"
            )

        if (
            not isinstance(resolved_source_provider_id, str)
            or not resolved_source_provider_id
        ):
            raise ValueError("resolved source_provider_id must be a non-empty string")

        return views_dir, resolved_source_provider_id, valid_views

    def _build_pending_quality_check_meta(self) -> Dict[str, Any]:
        """Build placeholder Stage-1 metadata before quality checking runs."""
        method = self.config.edit_quality_check.method
        checker_provider, checker_model = get_checker_info(self.config)
        if not self.config.edit_quality_check.enabled:
            return build_quality_check_meta(
                enabled=False,
                result=None,
                method=method,
                checker_provider=checker_provider,
                checker_model=checker_model,
            )
        return {
            "enabled": True,
            "method": method,
            "status": "pending_quality_check",
            "reason": "stage1 quality check pending",
            "checker_provider": checker_provider,
            "checker_model": checker_model,
            "checked_at": None,
            "error_message": "",
        }

    def apply_edit_single(
        self,
        model_id: str,
        instruction: str,
        views: List[str] = None,
        mode: str = "single",
        force: bool = False,
        provider_id: str = None,
        image_lane: Any = None,
        text_lane: Any = None,
    ) -> Dict[str, Any]:
        """Apply an edit and persist intermediate artifacts, without Stage-1 QC."""
        try:
            from core.image.view_stitcher import VIEW_ORDER

            views = views or VIEW_ORDER
            views_dir, resolved_source_provider_id, valid_views = (
                self._resolve_edit_views(model_id, views, provider_id)
            )

            existing_edit_id = self.find_existing_edit(model_id, instruction)
            if existing_edit_id:
                if force:
                    import shutil

                    existing_dir = TRIPLETS_DIR / model_id / "edited" / existing_edit_id
                    shutil.rmtree(existing_dir, ignore_errors=True)
                    print(f"  [Force] Deleted existing edit {existing_edit_id}")
                else:
                    existing_meta_path = (
                        TRIPLETS_DIR
                        / model_id
                        / "edited"
                        / existing_edit_id
                        / "meta.json"
                    )
                    existing_meta = {}
                    if existing_meta_path.exists():
                        with open(existing_meta_path, "r", encoding="utf-8") as f:
                            existing_meta = json.load(f)
                    return {
                        "edit_result": "skipped",
                        "edit_id": existing_edit_id,
                        "edit_meta": existing_meta,
                    }

            import uuid

            edit_id = uuid.uuid4().hex[:8]
            edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
            edited_dir.mkdir(parents=True, exist_ok=True)
            edit_scope_id = _build_edit_scope_id(model_id, edit_id)
            timing_context = {
                "source_model_id": model_id,
                "model_id": model_id,
                "edit_id": edit_id,
                "edit_scope_id": edit_scope_id,
                "instruction": instruction[:80] + "..."
                if len(instruction) > 80
                else instruction,
                "provider_id": resolved_source_provider_id,
            }
            timings: Dict[str, Dict[str, Any]] = {}
            timing_attempts: Dict[str, List[Dict[str, Any]]] = {}
            edited_paths: List[str] = []

            edit_apply_started_at = datetime.now().isoformat()
            edit_apply_started_perf = time.perf_counter()
            self._emit_timing_log(
                phase="START",
                stage_name="edit_apply",
                context=timing_context,
            )
            editor_result = {}
            if mode == "multiview":
                from core.image.multiview_editor import MultiviewEditor

                with MultiviewEditor(
                    self.config.multiview_edit,
                    pipeline_config=self.config,
                    task_name="multiview_editing",
                ) as editor:
                    editor_result = editor.edit_multiview(
                        views_dir=views_dir,
                        instruction=instruction,
                        output_dir=edited_dir,
                        view_names=valid_views,
                    )
            else:
                from core.image.guided_view_editor import GuidedViewEditor

                source_image_path = IMAGES_DIR / model_id / "image.png"
                if not source_image_path.exists():
                    source_image_path = IMAGES_DIR / model_id / "image.jpg"
                if not source_image_path.exists():
                    raise FileNotFoundError(f"no source image for model {model_id}")

                guided_config = self.config.guided_edit
                with GuidedViewEditor(
                    guided_config, mllm_config=self.config.qh_mllm
                ) as editor:
                    editor_result = editor.edit_all_views(
                        source_image_path=source_image_path,
                        views_dir=views_dir,
                        instruction=instruction,
                        output_dir=edited_dir,
                        view_names=valid_views,
                        temp_dir=edited_dir / "_tmp",
                        image_lane=image_lane,
                        text_lane=text_lane,
                    )

            edit_apply_finished_at = datetime.now().isoformat()
            edit_apply_elapsed_seconds = time.perf_counter() - edit_apply_started_perf
            edit_apply_timing = self._build_timing_entry(
                stage_name="edit_apply",
                status="success",
                started_at=edit_apply_started_at,
                finished_at=edit_apply_finished_at,
                elapsed_seconds=edit_apply_elapsed_seconds,
            )
            timings["edit_apply"] = edit_apply_timing
            timing_attempts["edit_apply"] = [dict(edit_apply_timing)]
            self._emit_timing_log(
                phase="END",
                stage_name="edit_apply",
                status="success",
                elapsed_seconds=edit_apply_elapsed_seconds,
                context=timing_context,
            )

            output_paths = editor_result.get("output_paths", [])
            editor_metadata = editor_result.get("metadata", {})
            edited_paths = [_rel_path(p) for p in output_paths]

            artifact_started_at = datetime.now().isoformat()
            artifact_started_perf = time.perf_counter()
            self._emit_timing_log(
                phase="START",
                stage_name="mask_artifact_build",
                context=timing_context,
            )
            artifact_result = build_edit_artifacts(
                model_id=model_id,
                source_provider_id=resolved_source_provider_id,
                source_views_dir=views_dir,
                edited_dir=edited_dir,
                edit_mode=mode,
                editor_metadata=editor_metadata,
                path_formatter=_rel_path,
                diff_threshold=self.config.edit_artifacts.diff_threshold,
                opening_kernel_size=self.config.edit_artifacts.opening_kernel_size,
            )
            artifact_finished_at = datetime.now().isoformat()
            artifact_elapsed_seconds = time.perf_counter() - artifact_started_perf
            artifact_timing = self._build_timing_entry(
                stage_name="mask_artifact_build",
                status="success",
                started_at=artifact_started_at,
                finished_at=artifact_finished_at,
                elapsed_seconds=artifact_elapsed_seconds,
            )
            timings["mask_artifact_build"] = artifact_timing
            timing_attempts["mask_artifact_build"] = [dict(artifact_timing)]
            self._emit_timing_log(
                phase="END",
                stage_name="mask_artifact_build",
                status="success",
                elapsed_seconds=artifact_elapsed_seconds,
                context=timing_context,
            )

            qc_enabled = self.config.edit_quality_check.enabled
            edited_views = [Path(p).stem for p in output_paths] if output_paths else []
            meta = {
                "edit_id": edit_id,
                "edit_scope_id": edit_scope_id,
                "instruction": instruction,
                "model_id": model_id,
                "view_names": valid_views,
                "edited_views": edited_views,
                "edit_mode": mode,
                "edited_paths": edited_paths,
                "source_provider_id": resolved_source_provider_id,
                "editor_metadata": editor_metadata,
                "edit_status": "pending_quality_check"
                if qc_enabled
                else EDIT_STATUS_PASSED,
                "quality_check": self._build_pending_quality_check_meta(),
                "timings": timings,
                "timing_attempts": timing_attempts,
                "generated_at": datetime.now().isoformat(),
            }
            meta.update(artifact_result["meta_patch"])
            meta.update(resolve_instruction_display_from_edit_meta(meta))
            meta["instruction"] = meta["instruction_display_text"]
            _write_json_atomic(edited_dir / "meta.json", meta)
            return {
                "edit_result": "success",
                "edit_id": edit_id,
                "edit_meta": meta,
            }
        except Exception as exc:
            if "timings" in locals() and isinstance(timings, dict):
                if "edit_apply_started_at" in locals() and "edit_apply" not in timings:
                    failed_elapsed = time.perf_counter() - edit_apply_started_perf
                    failed_timing = self._build_timing_entry(
                        stage_name="edit_apply",
                        status="failed",
                        started_at=edit_apply_started_at,
                        finished_at=datetime.now().isoformat(),
                        elapsed_seconds=failed_elapsed,
                    )
                    timings["edit_apply"] = failed_timing
                    timing_attempts["edit_apply"] = [dict(failed_timing)]
                    self._emit_timing_log(
                        phase="END",
                        stage_name="edit_apply",
                        status="failed",
                        elapsed_seconds=failed_elapsed,
                        context=timing_context
                        if "timing_context" in locals()
                        else None,
                    )
                if (
                    "artifact_started_at" in locals()
                    and "mask_artifact_build" not in timings
                ):
                    failed_elapsed = time.perf_counter() - artifact_started_perf
                    failed_timing = self._build_timing_entry(
                        stage_name="mask_artifact_build",
                        status="failed",
                        started_at=artifact_started_at,
                        finished_at=datetime.now().isoformat(),
                        elapsed_seconds=failed_elapsed,
                    )
                    timings["mask_artifact_build"] = failed_timing
                    timing_attempts["mask_artifact_build"] = [dict(failed_timing)]
                    self._emit_timing_log(
                        phase="END",
                        stage_name="mask_artifact_build",
                        status="failed",
                        elapsed_seconds=failed_elapsed,
                        context=timing_context
                        if "timing_context" in locals()
                        else None,
                    )
            failed_meta: Dict[str, Any] = {}
            if "edited_dir" in locals() and isinstance(edited_dir, Path):
                edited_views = (
                    [Path(p).stem for p in output_paths]
                    if "output_paths" in locals() and isinstance(output_paths, list)
                    else []
                )
                failed_meta = {
                    "edit_id": edit_id if "edit_id" in locals() else None,
                    "edit_scope_id": _build_edit_scope_id(
                        model_id,
                        edit_id if "edit_id" in locals() else None,
                    ),
                    "instruction": instruction,
                    "model_id": model_id,
                    "view_names": valid_views if "valid_views" in locals() else [],
                    "edited_views": edited_views,
                    "edit_mode": mode,
                    "edited_paths": edited_paths if "edited_paths" in locals() else [],
                    "source_provider_id": resolved_source_provider_id
                    if "resolved_source_provider_id" in locals()
                    else provider_id,
                    "editor_metadata": editor_metadata
                    if "editor_metadata" in locals()
                    and isinstance(editor_metadata, dict)
                    else {},
                    "edit_status": "error_pipeline",
                    "quality_check": self._build_pending_quality_check_meta(),
                    "timings": timings if "timings" in locals() else {},
                    "timing_attempts": timing_attempts
                    if "timing_attempts" in locals()
                    else {},
                    "generated_at": datetime.now().isoformat(),
                    "error_message": str(exc),
                }
                if "artifact_result" in locals() and isinstance(artifact_result, dict):
                    failed_meta.update(artifact_result.get("meta_patch", {}))
                failed_meta.update(
                    resolve_instruction_display_from_edit_meta(failed_meta)
                )
                failed_meta["instruction"] = failed_meta["instruction_display_text"]
                _write_json_atomic(edited_dir / "meta.json", failed_meta)
            return {
                "edit_result": "failed",
                "edit_id": edit_id if "edit_id" in locals() else None,
                "edit_meta": failed_meta,
                "error_message": str(exc),
            }

    def run_stage1_quality_check_single(
        self,
        model_id: str,
        edit_id: str,
        *,
        allow_stage1_relabel: Optional[bool] = None,
        skip_semaphore: bool = False,
    ) -> Dict[str, Any]:
        """Run Stage-1 quality check for an existing edit batch and update meta."""
        meta_path = TRIPLETS_DIR / model_id / "edited" / edit_id / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"edit meta not found: {meta_path}")

        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        instruction = meta.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("meta.json missing non-empty instruction")

        source_provider_id = meta.get("source_provider_id")
        if source_provider_id is not None and not isinstance(source_provider_id, str):
            raise ValueError("meta.json source_provider_id must be string when present")

        before_views_dir = self._resolve_before_views_dir(model_id, source_provider_id)
        edited_dir = meta_path.parent
        edit_scope_id = _build_edit_scope_id(model_id, edit_id)
        method = self.config.edit_quality_check.method
        checker_provider, checker_model = get_checker_info(self.config)
        timings = (
            dict(meta.get("timings", {}))
            if isinstance(meta.get("timings"), dict)
            else {}
        )
        timing_attempts = (
            dict(meta.get("timing_attempts", {}))
            if isinstance(meta.get("timing_attempts"), dict)
            else {}
        )
        timing_context = {
            "source_model_id": model_id,
            "model_id": model_id,
            "edit_id": edit_id,
            "edit_scope_id": edit_scope_id,
            "instruction": instruction[:80] + "..."
            if len(instruction) > 80
            else instruction,
            "provider_id": source_provider_id,
        }

        qc_started_at = datetime.now().isoformat()
        qc_started_perf = time.perf_counter()
        qc_timing_status = "skipped"
        self._emit_timing_log(
            phase="START",
            stage_name="stage1_quality_check",
            context=timing_context,
        )
        if self.config.edit_quality_check.enabled:
            print(
                f"[EditQC] START source_model_id={model_id} edit_id={edit_id} "
                f"edit_scope_id={edit_scope_id} "
                f"method={method} provider={checker_provider} model={checker_model}"
            )
            try:
                # Phase 4: skip semaphore when caller already holds a lane
                # slot (run_full_experiment path) to avoid double-throttling.
                if not skip_semaphore:
                    self.quality_check_semaphore.acquire()
                try:
                    with create_quality_checker(self.config) as checker:
                        qc_result = checker.check(
                            before_views_dir=before_views_dir,
                            after_views_dir=edited_dir,
                            instruction=instruction,
                            work_root_dir=edited_dir,
                            allow_stage1_relabel=allow_stage1_relabel,
                        )
                finally:
                    if not skip_semaphore:
                        self.quality_check_semaphore.release()
                edit_status = qc_result.status
                qc_timing_status = (
                    "success" if edit_status == EDIT_STATUS_PASSED else "failed"
                )
                quality_check = build_quality_check_meta(
                    enabled=True,
                    result=qc_result,
                    method=method,
                    checker_provider=checker_provider,
                    checker_model=checker_model,
                    path_formatter=_rel_path,
                )
                print(
                    f"[EditQC] RESULT source_model_id={model_id} edit_id={edit_id} "
                    f"edit_scope_id={edit_scope_id} "
                    f"status={edit_status} reason={quality_check.get('reason', '')}"
                )
            except Exception as qc_err:
                edit_status = EDIT_STATUS_ERROR_QUALITY_CHECK
                qc_timing_status = "failed"
                quality_check = build_quality_check_meta(
                    enabled=True,
                    result=None,
                    method=method,
                    checker_provider=checker_provider,
                    checker_model=checker_model,
                    error_message=str(qc_err),
                )
                quality_check["status"] = EDIT_STATUS_ERROR_QUALITY_CHECK
                quality_check["reason"] = "quality check execution error"
                print(
                    f"[EditQC] ERROR source_model_id={model_id} edit_id={edit_id} "
                    f"edit_scope_id={edit_scope_id} "
                    f"error={quality_check.get('error_message', '')}"
                )
        else:
            edit_status = EDIT_STATUS_PASSED
            quality_check = build_quality_check_meta(
                enabled=False,
                result=None,
                method=method,
                checker_provider=checker_provider,
                checker_model=checker_model,
            )
            print(
                f"[EditQC] SKIP source_model_id={model_id} edit_id={edit_id} "
                f"edit_scope_id={edit_scope_id} "
                "(edit_quality_check.enabled=false)"
            )

        qc_finished_at = datetime.now().isoformat()
        qc_elapsed_seconds = time.perf_counter() - qc_started_perf
        qc_timing = self._build_timing_entry(
            stage_name="stage1_quality_check",
            status=qc_timing_status,
            started_at=qc_started_at,
            finished_at=qc_finished_at,
            elapsed_seconds=qc_elapsed_seconds,
        )
        timings["stage1_quality_check"] = qc_timing
        timing_attempts["stage1_quality_check"] = [dict(qc_timing)]
        self._emit_timing_log(
            phase="END",
            stage_name="stage1_quality_check",
            status=qc_timing_status,
            elapsed_seconds=qc_elapsed_seconds,
            context=timing_context,
        )

        meta["edit_status"] = edit_status
        meta["quality_check"] = quality_check
        meta["timings"] = timings
        meta["timing_attempts"] = timing_attempts
        meta.update(resolve_instruction_display_from_edit_meta(meta))
        meta["instruction"] = meta["instruction_display_text"]
        _write_json_atomic(meta_path, meta)
        return {
            "edit_result": "success",
            "edit_id": edit_id,
            "edit_meta": meta,
        }

    def get_all_image_ids(self) -> List[str]:
        """Get all image IDs from pipeline/images."""
        if not IMAGES_DIR.exists():
            return []
        return [
            d.name
            for d in sorted(IMAGES_DIR.iterdir(), reverse=True)
            if d.is_dir() and (d / "image.png").exists()
        ]

    def get_all_model_ids(self) -> List[str]:
        """Get all model IDs from pipeline/models_src."""
        if not MODELS_DIR.exists():
            return []
        return [
            d.name
            for d in sorted(MODELS_DIR.iterdir(), reverse=True)
            if d.is_dir() and any(d.glob("*.glb"))
        ]

    def get_all_source_model_ids(self) -> List[str]:
        """Get all source model IDs (exclude target models with `_edit_`)."""
        return [
            model_id
            for model_id in self.get_all_model_ids()
            if "_edit_" not in model_id
        ]

    def has_3d_model(self, image_id: str, provider: str = None) -> bool:
        """Check if image already has a 3D model.

        Args:
            image_id: The image ID to check
            provider: Optional provider name (tripo/hunyuan/rodin).
                     If provided, checks for specific provider's model.
                     If None, checks if any GLB file exists.
        """
        model_dir = MODELS_DIR / image_id
        if not model_dir.exists():
            return False

        if provider:
            # Check for specific provider's model file
            from core.gen3d import get_model_id

            model_id = get_model_id(provider)
            pattern = f"model_{model_id}.glb"
            return any(model_dir.glob(pattern))
        else:
            # Check if any GLB file exists
            return any(model_dir.glob("*.glb"))

    def has_rendered_views(self, model_id: str, provider: str = None) -> bool:
        """Check if model already has rendered views.

        Args:
            model_id: Model ID
            provider: Provider name (tripo/hunyuan/rodin). If given, checks the
                      provider-specific subdirectory views/{provider_id}/.
                      If None, checks both provider subdirs and legacy views/*.png.
        """
        views_dir = TRIPLETS_DIR / model_id / "views"
        if not views_dir.exists():
            return False

        if provider:
            from core.gen3d import get_model_id

            provider_id = get_model_id(provider)
            provider_dir = views_dir / provider_id
            return provider_dir.exists() and any(provider_dir.glob("*.png"))
        else:
            # Check provider subdirectories first
            for subdir in views_dir.iterdir():
                if subdir.is_dir() and any(subdir.glob("*.png")):
                    return True
            # Fallback: legacy flat PNGs directly in views/
            return any(views_dir.glob("*.png"))

    # _build_quality_check_meta removed — use
    # core.image.edit_quality_router.build_quality_check_meta instead.

    def find_existing_edit(self, model_id: str, instruction: str) -> Optional[str]:
        """Find an existing edit batch with the same instruction.

        Returns:
            edit_id if found, None otherwise
        """
        edited_base = TRIPLETS_DIR / model_id / "edited"
        if not edited_base.exists():
            return None

        # Normalize instruction for comparison
        instruction_normalized = instruction.strip().lower()

        for edit_dir in edited_base.iterdir():
            if not edit_dir.is_dir():
                continue
            meta_path = edit_dir / "meta.json"
            if not meta_path.exists():
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                existing_instr = meta.get("instruction", "").strip().lower()
                if existing_instr == instruction_normalized and is_edit_batch_allowed(
                    meta
                ):
                    return edit_dir.name
            except Exception:
                continue
        return None

    def has_target_3d(self, model_id: str, edit_id: str, provider: str = None) -> bool:
        """Check if target 3D model exists for an edit batch.

        Args:
            model_id: Source model ID
            edit_id: Edit batch ID
            provider: Optional provider name (tripo/hunyuan/rodin).
                     If provided, checks for specific provider's model.
                     If None, checks if any GLB file exists.
        """
        target_id = f"{model_id}_edit_{edit_id}"
        target_dir = MODELS_DIR / target_id
        if not target_dir.exists():
            return False

        if provider:
            # Check for specific provider's model file
            from core.gen3d import get_model_id

            model_id_short = get_model_id(provider)
            pattern = f"model_{model_id_short}.glb"
            return any(target_dir.glob(pattern))
        else:
            # Check if any GLB file exists
            return any(target_dir.glob("*.glb"))

    def get_all_edit_batches(
        self, include_blocked: bool = False
    ) -> List[Tuple[str, str, str]]:
        """Get all edit batches across all models.

        Returns:
            List of (model_id, edit_id, instruction) tuples
        """
        batches = []
        if not TRIPLETS_DIR.exists():
            return batches

        for model_dir in TRIPLETS_DIR.iterdir():
            if not model_dir.is_dir():
                continue
            # Skip models with _edit_ in name (they are target models)
            if "_edit_" in model_dir.name:
                continue

            edited_base = model_dir / "edited"
            if not edited_base.exists():
                continue

            for edit_dir in sorted(edited_base.iterdir(), reverse=True):
                if not edit_dir.is_dir():
                    continue
                meta_path = edit_dir / "meta.json"
                if not meta_path.exists():
                    continue
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if not include_blocked and not is_edit_batch_allowed(meta):
                        continue
                    instruction = meta.get("instruction", "")
                    batches.append((model_dir.name, edit_dir.name, instruction))
                except Exception:
                    continue

        return batches

    @staticmethod
    def _resolve_before_views_dir(
        model_id: str, source_provider_id: Optional[str]
    ) -> Path:
        """Resolve before-edit views directory for quality check."""
        views_base = TRIPLETS_DIR / model_id / "views"
        if not views_base.exists():
            raise FileNotFoundError(f"views directory not found: {views_base}")

        if source_provider_id:
            provider_dir = views_base / source_provider_id
            if not provider_dir.exists():
                raise FileNotFoundError(
                    f"source provider views not found: {provider_dir}"
                )
            if not any(provider_dir.glob("*.png")):
                raise FileNotFoundError(
                    f"source provider views has no png files: {provider_dir}"
                )
            return provider_dir

        provider_dirs = [
            d
            for d in sorted(views_base.iterdir())
            if d.is_dir() and any(d.glob("*.png"))
        ]
        if len(provider_dirs) == 1:
            return provider_dirs[0]
        if len(provider_dirs) > 1:
            provider_ids = ", ".join(d.name for d in provider_dirs)
            raise ValueError(
                "cannot infer source_provider_id from meta.json; "
                f"multiple view providers exist: {provider_ids}"
            )

        if any(views_base.glob("*.png")):
            return views_base

        raise FileNotFoundError(f"no usable views found in: {views_base}")

    def recheck_edit_quality_single(
        self, model_id: str, edit_id: str
    ) -> Tuple[str, str]:
        """Run quality check for an existing edit batch and update meta.json."""
        item_id = f"{model_id}_edit_{edit_id}"
        edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
        if not edited_dir.exists():
            self._record_result(item_id, "failed", "(edit directory not found)")
            return item_id, "failed"

        meta_path = edited_dir / "meta.json"
        if not meta_path.exists():
            self._record_result(item_id, "failed", "(meta.json not found)")
            return item_id, "failed"

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            previous_status = get_effective_edit_status(meta)
            if not self.config.edit_quality_check.enabled:
                self._record_result(
                    item_id,
                    "skipped",
                    "(edit_quality_check.enabled=false)",
                )
                print(
                    f"[EditQC] SKIP model={model_id} edit_id={edit_id} "
                    "(edit_quality_check.enabled=false)"
                )
                return item_id, "skipped"

            print(
                f"[EditQC] RECHECK START model={model_id} edit_id={edit_id} "
                f"prev_status={previous_status} "
                f"method={self.config.edit_quality_check.method}"
            )
            stage1_payload = self.run_stage1_quality_check_single(model_id, edit_id)
            refreshed_meta = stage1_payload["edit_meta"]
            edit_status = refreshed_meta["edit_status"]
            quality_check = refreshed_meta.get("quality_check", {})

            print(
                f"[EditQC] RECHECK RESULT model={model_id} edit_id={edit_id} "
                f"status={edit_status} reason={quality_check.get('reason', '')}"
            )
            if quality_check.get("before_grid_path"):
                print(f"[EditQC] before_grid={quality_check.get('before_grid_path')}")
            if quality_check.get("after_grid_path"):
                print(f"[EditQC] after_grid={quality_check.get('after_grid_path')}")
            if quality_check.get("raw_response"):
                print("[EditQC] raw_response:")
                print(quality_check.get("raw_response"))

            if edit_status == EDIT_STATUS_FAILED_QUALITY:
                self._record_result(
                    item_id,
                    "failed",
                    f"(quality check failed: {quality_check.get('reason', '')})",
                )
                return item_id, "failed"
            if edit_status == EDIT_STATUS_ERROR_QUALITY_CHECK:
                self._record_result(
                    item_id,
                    "failed",
                    f"(quality check execution error: {quality_check.get('error_message', '')})",
                )
                return item_id, "failed"

            self._record_result(item_id, "success", "(quality check passed)")
            return item_id, "success"

        except Exception as e:
            traceback.print_exc()
            self._record_result(item_id, "failed", f"({str(e)})")
            return item_id, "failed"

    @staticmethod
    def _has_missing_mask_artifacts(edit_dir: Path, meta: Dict[str, Any]) -> bool:
        required_mask_files = [
            edit_dir / f"{view_name}_mask.png"
            for view_name in [
                "front",
                "back",
                "right",
                "left",
                "top",
                "bottom",
            ]
        ]
        required_mask_files.append(edit_dir / "edit_mask_grid.png")
        if any(not p.exists() for p in required_mask_files):
            return True

        artifacts = meta.get("edit_artifacts", {})
        if not isinstance(artifacts, dict):
            return True
        edit_mask = artifacts.get("edit_mask", {})
        if not isinstance(edit_mask, dict):
            return True
        if not isinstance(edit_mask.get("path"), str) or not edit_mask.get("path"):
            return True
        view_paths = edit_mask.get("view_paths", {})
        if not isinstance(view_paths, dict):
            return True
        return any(
            not isinstance(view_paths.get(view_name), str)
            or not view_paths.get(view_name)
            for view_name in ["front", "back", "right", "left", "top", "bottom"]
        )

    def materialize_missing_masks_single(
        self,
        model_id: str,
        edit_id: str,
        force: bool = False,
    ) -> Tuple[str, str]:
        item_id = f"{model_id}_edit_{edit_id}"
        try:
            edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
            if not edited_dir.exists():
                self._record_result(item_id, "failed", "(edit directory not found)")
                return item_id, "failed"

            meta_path = edited_dir / "meta.json"
            if not meta_path.exists():
                self._record_result(item_id, "failed", "(edit meta not found)")
                return item_id, "failed"

            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            instruction = meta.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                self._record_result(item_id, "failed", "(instruction missing)")
                return item_id, "failed"

            source_provider_id = meta.get("source_provider_id")
            if (
                not isinstance(source_provider_id, str)
                or not source_provider_id.strip()
            ):
                self._record_result(item_id, "failed", "(source_provider_id missing)")
                return item_id, "failed"

            source_views_dir = TRIPLETS_DIR / model_id / "views" / source_provider_id
            if not source_views_dir.exists():
                self._record_result(
                    item_id,
                    "failed",
                    f"(source views not found for provider {source_provider_id})",
                )
                return item_id, "failed"

            if not force and not self._has_missing_mask_artifacts(edited_dir, meta):
                self._record_result(
                    item_id, "skipped", "(mask artifacts already complete)"
                )
                return item_id, "skipped"

            result = materialize_missing_masks(
                model_id=model_id,
                source_provider_id=source_provider_id,
                source_views_dir=source_views_dir,
                edited_dir=edited_dir,
                path_formatter=_rel_path,
                diff_threshold=self.config.edit_artifacts.diff_threshold,
                opening_kernel_size=self.config.edit_artifacts.opening_kernel_size,
                edit_mode=str(meta.get("edit_mode") or "single"),
                editor_metadata=meta.get("editor_metadata", {}),
            )

            meta.update(result["meta_patch"])
            meta.update(resolve_instruction_display_from_edit_meta(meta))
            meta["instruction"] = meta["instruction_display_text"]
            _write_json_atomic(meta_path, meta)

            self._record_result(item_id, "success", "(mask artifacts materialized)")
            return item_id, "success"
        except Exception as exc:
            traceback.print_exc()
            self._record_result(item_id, "failed", f"({str(exc)})")
            return item_id, "failed"

    def batch_materialize_missing_masks(
        self,
        ids: Optional[List[str]] = None,
        edit_id: Optional[str] = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> None:
        concurrency = self.config.concurrency.mask_backfill
        all_batches = self.get_all_edit_batches(include_blocked=True)
        if ids:
            ids_set = set(ids)
            all_batches = [item for item in all_batches if item[0] in ids_set]
        if edit_id:
            all_batches = [item for item in all_batches if item[1] == edit_id]

        candidates: List[Tuple[str, str]] = []
        for model_id, current_edit_id, _ in all_batches:
            meta_path = (
                TRIPLETS_DIR / model_id / "edited" / current_edit_id / "meta.json"
            )
            if not meta_path.exists():
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                if force or self._has_missing_mask_artifacts(meta_path.parent, meta):
                    candidates.append((model_id, current_edit_id))
            except Exception:
                continue

        print(f"\n{'=' * 60}")
        print("Materialize Missing Masks")
        print(f"{'=' * 60}")
        print(f"Concurrency: {concurrency}")
        print(f"Total matched edit batches: {len(all_batches)}")
        print(
            f"{'Force candidates' if force else 'Missing-mask candidates'}: {len(candidates)}"
        )
        if ids:
            print(f"Filter model ids: {', '.join(ids)}")
        if edit_id:
            print(f"Filter edit id: {edit_id}")
        if force:
            print(
                "Force mode: ON (will recompute even if mask artifacts already exist)"
            )
        print(f"{'=' * 60}\n")

        if dry_run:
            print("DRY RUN - Would materialize masks for:")
            for index, (model_id, current_edit_id) in enumerate(candidates, 1):
                print(f"  {index}. {model_id}_edit_{current_edit_id}")
            return

        if not candidates:
            print("Nothing to process!")
            return

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    self.materialize_missing_masks_single,
                    model_id,
                    current_edit_id,
                    force,
                ): (model_id, current_edit_id)
                for model_id, current_edit_id in candidates
            }
            for future in as_completed(futures):
                future.result()

        self._print_summary()

    def batch_recheck_edit_quality(
        self,
        ids: Optional[List[str]] = None,
        edit_id: Optional[str] = None,
        dry_run: bool = False,
    ):
        """Batch recheck quality for existing edit batches."""
        concurrency = self.config.concurrency.edit_quality_check
        all_batches = self.get_all_edit_batches(include_blocked=True)

        if ids:
            ids_set = set(ids)
            all_batches = [(m, e, i) for m, e, i in all_batches if m in ids_set]
        if edit_id:
            all_batches = [(m, e, i) for m, e, i in all_batches if e == edit_id]

        print(f"\n{'=' * 60}")
        print("Batch Edit Quality Recheck")
        print(f"{'=' * 60}")
        print(f"Concurrency: {concurrency}")
        print(f"Total edit batches: {len(all_batches)}")
        if ids:
            print(f"Filter model ids: {', '.join(ids)}")
        if edit_id:
            print(f"Filter edit id: {edit_id}")
        print(f"{'=' * 60}\n")

        if dry_run:
            print("DRY RUN - Would recheck these edit batches:")
            for i, (model_id, current_edit_id, instruction) in enumerate(
                all_batches, 1
            ):
                short_instr = (
                    instruction[:50] + "..." if len(instruction) > 50 else instruction
                )
                print(f"  {i}. {model_id}_edit_{current_edit_id}: {short_instr}")
            return

        if not all_batches:
            print("Nothing to process!")
            return

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    self.recheck_edit_quality_single,
                    model_id,
                    current_edit_id,
                ): (model_id, current_edit_id)
                for model_id, current_edit_id, _ in all_batches
            }
            for future in as_completed(futures):
                future.result()

        self._print_summary()

    def get_model_instructions(self, model_id: str) -> List[dict]:
        """Get instructions for a model from its source image."""
        # Instructions are stored with the source image (same ID)
        instr_path = IMAGES_DIR / model_id / "instructions.json"
        if not instr_path.exists():
            return []

        try:
            with open(instr_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, list):
                instructions = []
                for item in data:
                    normalized = resolve_instruction_display_from_instruction_item(item)
                    if normalized.get("instruction_display_text"):
                        instr_type = (
                            item.get("type", "unknown")
                            if isinstance(item, dict)
                            else "unknown"
                        )
                        instructions.append(
                            {
                                "text": normalized["instruction_display_text"],
                                "type": instr_type,
                                **normalized,
                            }
                        )
                return instructions
        except Exception:
            pass
        return []

    def _record_result(self, item_id: str, status: str, message: str = ""):
        """Thread-safe result recording."""
        with self.lock:
            self.results[status].append({"id": item_id, "message": message})
            total = (
                len(self.results["success"])
                + len(self.results["failed"])
                + len(self.results["skipped"])
            )
            print(f"[{total}] {status.upper()}: {item_id} {message}")

    def generate_3d_single(
        self, image_id: str, provider: str, force: bool = False
    ) -> Tuple[str, str]:
        """Generate 3D model for a single image."""
        try:
            # Check if already exists (for this specific provider)
            if not force and self.has_3d_model(image_id, provider):
                from core.gen3d import get_model_id

                model_id = get_model_id(provider)
                self._record_result(
                    image_id,
                    "skipped",
                    f"(already has {provider} 3D: model_{model_id}.glb)",
                )
                return image_id, "skipped"

            # Check image exists - support both regular images and edited views
            # Edited view format: {model_id}_edit_{edit_id}
            if "_edit_" in image_id:
                parts = image_id.rsplit("_edit_", 1)
                if len(parts) == 2:
                    source_model_id, edit_id = parts
                    edited_dir = TRIPLETS_DIR / source_model_id / "edited" / edit_id
                    meta_path = edited_dir / "meta.json"
                    if meta_path.exists():
                        with open(meta_path, "r", encoding="utf-8") as f:
                            edit_meta = json.load(f)
                        if not is_edit_batch_allowed(edit_meta):
                            self._record_result(
                                image_id,
                                "failed",
                                "(blocked by quality check)",
                            )
                            return image_id, "failed"
                    # Check for front.png or any other view
                    image_path = edited_dir / "front.png"
                    if not image_path.exists():
                        for view_name in ["back", "right", "left", "top", "bottom"]:
                            alt_path = edited_dir / f"{view_name}.png"
                            if alt_path.exists():
                                image_path = alt_path
                                break
                else:
                    image_path = IMAGES_DIR / image_id / "image.png"
            else:
                image_path = IMAGES_DIR / image_id / "image.png"

            if not image_path.exists():
                self._record_result(
                    image_id, "failed", f"(image not found: {image_path.name})"
                )
                return image_id, "failed"

            # Import here to avoid circular imports and reduce startup time
            from scripts.gen3d import generate_3d

            result_path = generate_3d(
                image_id=image_id, provider=provider, skip_existing=not force
            )

            self._record_result(image_id, "success", f"-> {Path(result_path).name}")
            return image_id, "success"

        except Exception as e:
            traceback.print_exc()
            self._record_result(image_id, "failed", f"({str(e)})")
            return image_id, "failed"

    def render_single(
        self, model_id: str, provider: str, force: bool = False,
        quiet_subprocess: bool = False,
    ) -> Tuple[str, str]:
        """Render views for a single model with the specified provider's GLB.

        Args:
            model_id: Model ID
            provider: Provider name (tripo/hunyuan/rodin) — selects which GLB to render
            force: If True, re-render even if views already exist
            quiet_subprocess: If True, suppress verbose WebGL subprocess output;
                              only print a summary line on success and full output on failure.
                              Set by run_full_experiment; leave False for standalone CLI use.
        """
        from scripts.run_render_batch import _render_tls
        _render_tls.quiet_subprocess = quiet_subprocess
        try:
            # Check if already has views for this provider
            if not force and self.has_rendered_views(model_id, provider):
                from core.gen3d import get_model_id

                provider_id = get_model_id(provider)
                self._record_result(
                    model_id,
                    "skipped",
                    f"(already has {provider} views in views/{provider_id}/)",
                )
                return model_id, "skipped"

            # Check the provider's GLB exists
            from core.gen3d import get_model_id

            provider_id = get_model_id(provider)
            model_dir = MODELS_DIR / model_id
            glb_path = model_dir / f"model_{provider_id}.glb"
            if not glb_path.exists():
                self._record_result(
                    model_id,
                    "failed",
                    f"(no GLB for {provider}: model_{provider_id}.glb not found)",
                )
                return model_id, "failed"

            from scripts.run_render_batch import process_rendering

            process_rendering(model_id, provider=provider, force=force)

            self._record_result(model_id, "success", f"[{provider_id}]")
            return model_id, "success"

        except Exception as e:
            if "timings" in locals() and isinstance(timings, dict):
                if "edit_apply_started_at" in locals() and "edit_apply" not in timings:
                    failed_elapsed = time.perf_counter() - edit_apply_started_perf
                    failed_timing = self._build_timing_entry(
                        stage_name="edit_apply",
                        status="failed",
                        started_at=edit_apply_started_at,
                        finished_at=datetime.now().isoformat(),
                        elapsed_seconds=failed_elapsed,
                    )
                    self._emit_timing_log(
                        phase="END",
                        stage_name="edit_apply",
                        status="failed",
                        elapsed_seconds=failed_elapsed,
                        context=timing_context
                        if "timing_context" in locals()
                        else None,
                    )
                if (
                    "artifact_started_at" in locals()
                    and "mask_artifact_build" not in timings
                ):
                    failed_elapsed = time.perf_counter() - artifact_started_perf
                    self._emit_timing_log(
                        phase="END",
                        stage_name="mask_artifact_build",
                        status="failed",
                        elapsed_seconds=failed_elapsed,
                        context=timing_context
                        if "timing_context" in locals()
                        else None,
                    )
                if (
                    "qc_started_at" in locals()
                    and "stage1_quality_check" not in timings
                ):
                    failed_elapsed = time.perf_counter() - qc_started_perf
                    self._emit_timing_log(
                        phase="END",
                        stage_name="stage1_quality_check",
                        status="failed",
                        elapsed_seconds=failed_elapsed,
                        context=timing_context
                        if "timing_context" in locals()
                        else None,
                    )
            traceback.print_exc()
            self._record_result(model_id, "failed", f"({str(e)})")
            return model_id, "failed"

    def batch_generate_3d(
        self,
        provider: str,
        ids: Optional[List[str]] = None,
        force: bool = False,
        dry_run: bool = False,
    ):
        """Batch generate 3D models with concurrency control."""
        # Get concurrency limit from config
        concurrency = getattr(self.config.concurrency.gen3d, provider, 5)

        # Get IDs to process
        if ids:
            all_ids = ids
        else:
            all_ids = self.get_all_image_ids()

        # Filter if not forcing (check for specific provider's model)
        if not force:
            skipped_ids = [i for i in all_ids if self.has_3d_model(i, provider)]
            skipped_set = set(skipped_ids)
            to_process = [i for i in all_ids if i not in skipped_set]
            skipped_count = len(skipped_ids)
        else:
            skipped_ids = []
            to_process = all_ids
            skipped_count = 0

        from core.gen3d import get_model_id

        model_id = get_model_id(provider)

        print(f"\n{'=' * 60}")
        print(f"Batch 3D Generation")
        print(f"{'=' * 60}")
        print(f"Provider: {provider}")
        print(f"Concurrency: {concurrency}")
        print(f"Total images: {len(all_ids)}")
        print(f"Already have {provider} 3D (model_{model_id}.glb): {skipped_count}")
        print(f"To process: {len(to_process)}")
        print(f"{'=' * 60}\n")

        if skipped_ids:
            print(f"Skipped IDs (already have {provider} 3D):")
            for item_id in skipped_ids:
                print(f"  - {item_id}")
            print("")

        if dry_run:
            print("DRY RUN - Would process these IDs:")
            for i, item_id in enumerate(to_process, 1):
                print(f"  {i}. {item_id}")
            return

        if not to_process:
            print("Nothing to process!")
            return

        # Process with thread pool
        total = len(to_process)
        completed = 0

        def _print_progress():
            bar_len = 30
            filled = int(bar_len * (completed / total)) if total else bar_len
            bar = "#" * filled + "-" * (bar_len - filled)
            print(f"Progress: [{bar}] {completed}/{total}", end="\r", flush=True)

        _print_progress()
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    self.generate_3d_single, item_id, provider, force
                ): item_id
                for item_id in to_process
            }

            for future in as_completed(futures):
                try:
                    future.result()
                finally:
                    completed += 1
                    _print_progress()

        if total:
            print("")

        self._print_summary()

    def batch_render(
        self,
        provider: str,
        ids: Optional[List[str]] = None,
        force: bool = False,
        dry_run: bool = False,
    ):
        """Batch render views with concurrency control.

        Args:
            provider: Provider name (tripo/hunyuan/rodin) — which GLB to render
            ids: Specific model IDs to process (default: all models)
            force: Re-render even if views already exist for this provider
            dry_run: Show what would be done without doing it
        """
        from core.gen3d import get_model_id

        provider_id = get_model_id(provider)

        # Get concurrency limit from config
        concurrency = self.config.concurrency.render

        # Get IDs to process
        if ids:
            all_ids = ids
        else:
            all_ids = self.get_all_model_ids()

        # Filter if not forcing (check provider-specific views subdir)
        if not force:
            to_process = [
                i for i in all_ids if not self.has_rendered_views(i, provider)
            ]
            skipped_count = len(all_ids) - len(to_process)
        else:
            to_process = all_ids
            skipped_count = 0

        print(f"\n{'=' * 60}")
        print(f"Batch Rendering")
        print(f"{'=' * 60}")
        print(f"Provider: {provider} (views/{provider_id}/)")
        print(f"Concurrency: {concurrency}")
        print(f"Total models: {len(all_ids)}")
        print(f"Already have {provider} views: {skipped_count}")
        print(f"To process: {len(to_process)}")
        print(f"{'=' * 60}\n")

        if dry_run:
            print("DRY RUN - Would process these IDs:")
            for i, item_id in enumerate(to_process, 1):
                print(f"  {i}. {item_id}")
            return

        if not to_process:
            print("Nothing to process!")
            return

        # Process with thread pool
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(self.render_single, item_id, provider, force): item_id
                for item_id in to_process
            }

            for future in as_completed(futures):
                # Results are recorded in the worker function
                pass

        self._print_summary()

    def _print_summary(self):
        """Print processing summary."""
        print(f"\n{'=' * 60}")
        print("SUMMARY")
        print(f"{'=' * 60}")
        print(f"Success: {len(self.results['success'])}")
        print(f"Failed:  {len(self.results['failed'])}")
        print(f"Skipped: {len(self.results['skipped'])}")

        if self.results["failed"]:
            print(f"\nFailed items:")
            for item in self.results["failed"]:
                print(f"  - {item['id']}: {item['message']}")

        print(f"{'=' * 60}\n")

    def edit_single(
        self,
        model_id: str,
        instruction: str,
        views: List[str] = None,
        mode: str = "single",
        force: bool = False,
        provider_id: str = None,
        allow_stage1_relabel: Optional[bool] = None,
    ) -> Tuple[str, str]:
        """Edit views for a single model with given instruction.

        Args:
            model_id: Model ID
            instruction: Edit instruction
            views: Which views to edit (default: all 6 views)
            mode: 'single' or 'multiview'
            force: If True, delete existing edit with same instruction and redo
            provider_id: Provider ID whose rendered views to use (e.g. 'tp3', 'hy3').
                         If None, falls back to legacy flat views/ directory.
        """
        try:
            apply_payload = self.apply_edit_single(
                model_id,
                instruction,
                views=views,
                mode=mode,
                force=force,
                provider_id=provider_id,
            )
            edit_result = apply_payload["edit_result"]
            edit_id = apply_payload["edit_id"]
            edit_meta = apply_payload["edit_meta"]

            if edit_result == "skipped":
                short_instr = (
                    instruction[:30] + "..." if len(instruction) > 30 else instruction
                )
                self._record_result(
                    model_id,
                    "skipped",
                    f"(already edited: {edit_id}, instr: {short_instr})",
                )
                return model_id, "skipped"

            if edit_result == "failed":
                self._record_result(
                    model_id,
                    "failed",
                    f"({apply_payload.get('error_message', 'edit apply failed')})",
                )
                return model_id, "failed"

            if not edit_id:
                raise ValueError("edit apply succeeded but edit_id is missing")

            stage1_payload = self.run_stage1_quality_check_single(
                model_id,
                edit_id,
                allow_stage1_relabel=allow_stage1_relabel,
            )
            refreshed_meta = stage1_payload["edit_meta"]
            edit_status = stage1_payload["edit_status"]
            quality_check = refreshed_meta.get("quality_check", {})

            if edit_status == EDIT_STATUS_FAILED_QUALITY:
                self._record_result(
                    model_id,
                    "failed",
                    f"(quality check failed: {quality_check.get('reason', '')}, edit_id={edit_id})",
                )
                return model_id, "failed"
            if edit_status == EDIT_STATUS_ERROR_QUALITY_CHECK:
                self._record_result(
                    model_id,
                    "failed",
                    f"(quality check execution error: {quality_check.get('error_message', '')}, edit_id={edit_id})",
                )
                return model_id, "failed"

            short_instr = (
                instruction[:30] + "..." if len(instruction) > 30 else instruction
            )
            mode_label = "[MV]" if mode == "multiview" else ""
            self._record_result(
                model_id, "success", f"-> {edit_id} {mode_label}({short_instr})"
            )
            return model_id, "success"

        except Exception as e:
            traceback.print_exc()
            self._record_result(model_id, "failed", f"({str(e)})")
            return model_id, "failed"

    def batch_edit(
        self,
        ids: Optional[List[str]] = None,
        instr_index: Optional[int] = None,
        instruction: Optional[str] = None,
        all_instructions: bool = False,
        max_per_type: int = 0,
        views: List[str] = None,
        mode: str = "multiview",
        force: bool = False,
        dry_run: bool = False,
        provider_id: str = None,
    ):
        """Batch edit views using each model's own instructions.

        Args:
            ids: Specific model IDs to process (default: all models with views)
            instr_index: Use specific instruction index (0=first, 1=second, etc.)
            instruction: Custom instruction text (overrides instr_index)
            all_instructions: If True, apply all instructions separately
            max_per_type: Limit per instruction type (remove/replace). 0=no limit, 1=one of each type
            views: Which views to edit (default: all 6 views, MLLM will select)
            mode: 'single' or 'multiview' - multiview stitches 6 views into grid
            force: If True, redo even if already edited with same instruction
            dry_run: Just show what would be done
            provider_id: Provider ID whose rendered views to edit (e.g. 'tp3', 'hy3').
                         If None, auto-detects the first available provider subdir.
        """
        from core.image.view_stitcher import VIEW_ORDER

        # Get concurrency limit (use gen3d.hunyuan as reference for edit)
        concurrency = getattr(self.config.concurrency.gen3d, "hunyuan", 5)

        # 默认使用所有 6 视角，Single 模式由 MLLM 决定编辑哪些
        views = views or VIEW_ORDER

        # Get IDs to process
        if ids:
            all_ids = ids
        else:
            all_ids = self.get_all_model_ids()

        # Filter to only models with views and instructions
        edit_tasks = []  # List of (model_id, instruction_text)
        skipped_tasks = []  # List of (model_id, instruction_text, edit_id) - already edited
        limited_tasks = []  # List of (model_id, instruction_text, type) - skipped due to max_per_type
        models_no_views = 0
        models_no_instructions = 0

        for model_id in all_ids:
            # Check has views
            if not self.has_rendered_views(model_id):
                models_no_views += 1
                continue

            # Collect instructions to process for this model
            instrs_to_add = []
            if instruction:
                instrs_to_add = [{"text": instruction, "type": "custom"}]
            else:
                # Get instructions from model metadata
                instructions_list = self.get_model_instructions(model_id)
                if not instructions_list:
                    models_no_instructions += 1
                    continue

                if all_instructions:
                    instrs_to_add = instructions_list
                elif instr_index is not None:
                    if instr_index < len(instructions_list):
                        instrs_to_add = [instructions_list[instr_index]]
                    else:
                        models_no_instructions += 1
                        continue
                else:
                    instrs_to_add = [instructions_list[0]]

            # Apply max_per_type limit if specified
            if max_per_type > 0 and not instruction:
                type_counts = {}  # Track count per type for this model
                filtered_instrs = []
                for instr in instrs_to_add:
                    instr_type = instr.get("type", "unknown")
                    current_count = type_counts.get(instr_type, 0)
                    if current_count < max_per_type:
                        filtered_instrs.append(instr)
                        type_counts[instr_type] = current_count + 1
                    else:
                        limited_tasks.append((model_id, instr["text"], instr_type))
                instrs_to_add = filtered_instrs

            # Check each instruction for existing edits
            for instr in instrs_to_add:
                instr_text = instr["text"]
                existing_edit_id = self.find_existing_edit(model_id, instr_text)
                if existing_edit_id and not force:
                    skipped_tasks.append((model_id, instr_text, existing_edit_id))
                else:
                    edit_tasks.append((model_id, instr_text))

        print(f"\n{'=' * 60}")
        print(f"Batch View Editing")
        print(f"{'=' * 60}")
        print(f"Edit mode: {mode}")
        print(f"Concurrency: {concurrency}")
        print(f"Views to edit: {', '.join(views)}")
        print(f"Provider: {provider_id if provider_id else '(auto-detect)'}")
        print(f"Total models: {len(all_ids)}")
        print(f"Models without views: {models_no_views}")
        if instruction:
            print(
                f"Using custom instruction: {instruction[:50]}..."
                if len(instruction) > 50
                else f"Using custom instruction: {instruction}"
            )
        else:
            print(f"Models without instructions: {models_no_instructions}")
        if max_per_type > 0:
            print(f"Max per type (remove/replace): {max_per_type}")
            print(f"Limited by max_per_type: {len(limited_tasks)}")
        print(f"Already edited (skip): {len(skipped_tasks)}")
        print(f"Edit tasks to run: {len(edit_tasks)}")
        if force:
            print(f"Force mode: ON (will overwrite existing edits)")
        if instruction:
            print(f"Instruction source: Custom (command line)")
        elif all_instructions:
            print(
                f"Instruction source: All model instructions (each creates separate edit)"
            )
        elif instr_index is not None:
            print(f"Instruction source: Instruction #{instr_index + 1}")
        else:
            print(f"Instruction source: First instruction (default)")
        print(f"{'=' * 60}\n")

        if dry_run:
            if skipped_tasks:
                print("Skipped (already edited):")
                for model_id, instr_text, edit_id in skipped_tasks:
                    short_instr = (
                        instr_text[:40] + "..." if len(instr_text) > 40 else instr_text
                    )
                    print(f"  - {model_id} [{edit_id}]: {short_instr}")
                print("")
            if limited_tasks:
                print(f"Skipped (max_per_type={max_per_type} limit):")
                for model_id, instr_text, instr_type in limited_tasks:
                    short_instr = (
                        instr_text[:40] + "..." if len(instr_text) > 40 else instr_text
                    )
                    print(f"  - {model_id} [{instr_type}]: {short_instr}")
                print("")
            print("DRY RUN - Would process these edits:")
            for i, (model_id, instr_text) in enumerate(edit_tasks, 1):
                short_instr = (
                    instr_text[:50] + "..." if len(instr_text) > 50 else instr_text
                )
                print(f"  {i}. {model_id}: {short_instr}")
            return

        if not edit_tasks:
            print("Nothing to process!")
            return

        # Process with thread pool
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    self.edit_single,
                    model_id,
                    instr_text,
                    views,
                    mode,
                    force,
                    provider_id,
                ): (model_id, instr_text)
                for model_id, instr_text in edit_tasks
            }

            for future in as_completed(futures):
                # Results are recorded in the worker function
                pass

        self._print_summary()

    def gen3d_from_edit_single(
        self, model_id: str, edit_id: str, provider: str, force: bool = False
    ) -> Dict[str, Optional[str]]:
        """Generate target 3D from an edit batch.

        Args:
            model_id: Source model ID
            edit_id: Edit batch ID
            provider: 3D generation provider
            force: If True, regenerate even if exists

        Returns:
            Dict with target id, status, and optional classified error metadata.
        """
        target_id = f"{model_id}_edit_{edit_id}"
        edit_scope_id = _build_edit_scope_id(model_id, edit_id)
        try:
            print(
                f"[Target3D] START source_model_id={model_id} edit_id={edit_id} "
                f"edit_scope_id={edit_scope_id} target_model_id={target_id} "
                f"provider={provider}"
            )
            # Check if already exists (for this specific provider)
            if not force and self.has_target_3d(model_id, edit_id, provider):
                self._record_result(
                    target_id, "skipped", f"(already has {provider} Target 3D)"
                )
                print(
                    f"[Target3D] SKIP source_model_id={model_id} edit_id={edit_id} "
                    f"edit_scope_id={edit_scope_id} target_model_id={target_id} "
                    f"provider={provider} reason=already_exists"
                )
                return {
                    "target_model_id": target_id,
                    "target_gen3d_status": "skipped",
                    "target_gen3d_error_class": None,
                    "target_gen3d_error_message": None,
                }

            # Check edit directory exists and has views
            edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
            if not edited_dir.exists():
                self._record_result(target_id, "failed", "(edit directory not found)")
                return {
                    "target_model_id": target_id,
                    "target_gen3d_status": "failed",
                    "target_gen3d_error_class": "unknown",
                    "target_gen3d_error_message": "edit directory not found",
                }
            meta_path = edited_dir / "meta.json"
            if not meta_path.exists():
                self._record_result(target_id, "failed", "(edit meta not found)")
                return {
                    "target_model_id": target_id,
                    "target_gen3d_status": "failed",
                    "target_gen3d_error_class": "unknown",
                    "target_gen3d_error_message": "edit meta not found",
                }
            with open(meta_path, "r", encoding="utf-8") as f:
                edit_meta = json.load(f)
            if not is_edit_batch_allowed(edit_meta):
                status = edit_meta.get("edit_status", EDIT_STATUS_PASSED)
                self._record_result(
                    target_id,
                    "failed",
                    f"(blocked by quality check: status={status})",
                )
                return {
                    "target_model_id": target_id,
                    "target_gen3d_status": "failed",
                    "target_gen3d_error_class": "unknown",
                    "target_gen3d_error_message": (
                        f"blocked by quality check: status={status}"
                    ),
                }

            # Find front view (or fallback to other views)
            front_view = edited_dir / "front.png"
            if not front_view.exists():
                for view_name in ["back", "right", "left", "top", "bottom"]:
                    alt_path = edited_dir / f"{view_name}.png"
                    if alt_path.exists():
                        front_view = alt_path
                        break

            if not front_view.exists():
                self._record_result(target_id, "failed", "(no edited views found)")
                return {
                    "target_model_id": target_id,
                    "target_gen3d_status": "failed",
                    "target_gen3d_error_class": "unknown",
                    "target_gen3d_error_message": "no edited views found",
                }

            # Import and call gen3d
            from scripts.gen3d import generate_3d

            result_path = generate_3d(
                image_id=target_id, provider=provider, skip_existing=not force
            )

            self._record_result(target_id, "success", f"-> {Path(result_path).name}")
            print(
                f"[Target3D] RESULT source_model_id={model_id} edit_id={edit_id} "
                f"edit_scope_id={edit_scope_id} target_model_id={target_id} "
                f"provider={provider} status=success output={result_path}"
            )
            return {
                "target_model_id": target_id,
                "target_gen3d_status": "success",
                "target_gen3d_error_class": None,
                "target_gen3d_error_message": None,
            }

        except Exception as e:
            error_message = str(e)
            error_class = _classify_target_gen3d_error(error_message)
            traceback.print_exc()
            print(
                f"[Target3D] ERROR source_model_id={model_id} edit_id={edit_id} "
                f"edit_scope_id={edit_scope_id} target_model_id={target_id} "
                f"provider={provider} error_class={error_class} error={error_message}"
            )
            self._record_result(target_id, "failed", f"({error_message})")
            return {
                "target_model_id": target_id,
                "target_gen3d_status": "failed",
                "target_gen3d_error_class": error_class,
                "target_gen3d_error_message": error_message,
            }

    def batch_gen3d_from_edits(
        self,
        provider: str,
        ids: Optional[List[str]] = None,
        edit_id: Optional[str] = None,
        max_per_model: int = 0,
        force: bool = False,
        dry_run: bool = False,
    ):
        """Batch generate target 3D models from all edit batches.

        Args:
            provider: 3D generation provider (hunyuan, tripo, rodin)
            ids: Specific model IDs to process (default: all models with edits)
            edit_id: Specific edit batch ID to process (exact match)
            max_per_model: Max edit batches to process per model. 0=no limit (default)
            force: If True, regenerate even if target 3D exists
            dry_run: Just show what would be done
        """
        # Get concurrency limit from config
        concurrency = getattr(self.config.concurrency.gen3d, provider, 5)

        # Get all edit batches
        all_batches = self.get_all_edit_batches()

        # Filter by IDs if specified
        if ids:
            ids_set = set(ids)
            all_batches = [(m, e, i) for m, e, i in all_batches if m in ids_set]

        # Filter by specific edit_id if specified
        if edit_id:
            all_batches = [(m, e, i) for m, e, i in all_batches if e == edit_id]

        # Separate into to_process, skipped, and limited
        to_process = []  # List of (model_id, edit_id, instruction)
        skipped = []  # List of (model_id, edit_id) - already have Target 3D
        limited = []  # List of (model_id, edit_id, instruction) - exceeded max_per_model

        # Group by model for max_per_model limiting
        model_counts = {}  # Track how many edits per model

        for model_id, edit_id, instruction in all_batches:
            if not force and self.has_target_3d(model_id, edit_id, provider):
                skipped.append((model_id, edit_id))
            else:
                # Check max_per_model limit
                current_count = model_counts.get(model_id, 0)
                if max_per_model > 0 and current_count >= max_per_model:
                    limited.append((model_id, edit_id, instruction))
                else:
                    to_process.append((model_id, edit_id, instruction))
                    model_counts[model_id] = current_count + 1

        print(f"\n{'=' * 60}")
        print(f"Batch Target 3D Generation (from edited views)")
        print(f"{'=' * 60}")
        print(f"Provider: {provider}")
        print(f"Concurrency: {concurrency}")
        print(f"Total edit batches: {len(all_batches)}")
        print(f"Already have Target 3D: {len(skipped)}")
        if max_per_model > 0:
            print(f"Max per model: {max_per_model}")
            print(f"Limited by max_per_model: {len(limited)}")
        print(f"To process: {len(to_process)}")
        if force:
            print(f"Force mode: ON (will regenerate existing)")
        print(f"{'=' * 60}\n")

        if dry_run:
            if skipped:
                print("Skipped (already have Target 3D):")
                for model_id, edit_id in skipped:
                    print(f"  - {model_id}_edit_{edit_id}")
                print("")
            if limited:
                print(f"Skipped (max_per_model={max_per_model} limit):")
                for model_id, edit_id, instruction in limited:
                    short_instr = (
                        instruction[:40] + "..."
                        if len(instruction) > 40
                        else instruction
                    )
                    print(f"  - {model_id}_edit_{edit_id}: {short_instr}")
                print("")
            print("DRY RUN - Would generate Target 3D for:")
            for i, (model_id, edit_id, instruction) in enumerate(to_process, 1):
                short_instr = (
                    instruction[:40] + "..." if len(instruction) > 40 else instruction
                )
                print(f"  {i}. {model_id}_edit_{edit_id}: {short_instr}")
            return

        if not to_process:
            print("Nothing to process!")
            return

        # Process with thread pool
        total = len(to_process)
        completed = 0

        def _print_progress():
            bar_len = 30
            filled = int(bar_len * (completed / total)) if total else bar_len
            bar = "#" * filled + "-" * (bar_len - filled)
            print(f"Progress: [{bar}] {completed}/{total}", end="\r", flush=True)

        _print_progress()
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    self.gen3d_from_edit_single, model_id, edit_id, provider, force
                ): (model_id, edit_id)
                for model_id, edit_id, _ in to_process
            }

            for future in as_completed(futures):
                try:
                    future.result()
                finally:
                    completed += 1
                    _print_progress()

        if total:
            print("")

        self._print_summary()

    @staticmethod
    def _parse_target_model_id(target_id: str) -> Tuple[str, str]:
        """Parse target model id ``<model_id>_edit_<edit_id>``."""
        if "_edit_" not in target_id:
            raise ValueError(
                f"invalid target model id: {target_id!r} "
                "(expected '<model_id>_edit_<edit_id>')"
            )
        model_id, edit_id = target_id.rsplit("_edit_", 1)
        if not model_id or not edit_id:
            raise ValueError(
                f"invalid target model id: {target_id!r} "
                "(expected '<model_id>_edit_<edit_id>')"
            )
        return model_id, edit_id

    def _ensure_target_views_rendered(
        self, target_id: str, provider: str, force_render: bool
    ) -> str:
        """Ensure target model views exist for provider and return provider_id."""
        from core.gen3d import get_model_id
        from scripts.run_render_batch import process_rendering

        provider_id = get_model_id(provider)
        target_model_dir = MODELS_DIR / target_id
        glb_path = target_model_dir / f"model_{provider_id}.glb"
        if not glb_path.exists():
            raise FileNotFoundError(
                f"target GLB not found for provider={provider}: {glb_path}"
            )

        if force_render or not self.has_rendered_views(target_id, provider):
            process_rendering(target_id, provider=provider, force=force_render)

        return provider_id

    def _write_target_quality_check(
        self, target_id: str, target_quality_check: Dict[str, Any]
    ) -> Path:
        """Write target_quality_check to target model meta.json."""
        from utils.fs_retry import retry_io

        target_meta_path = MODELS_DIR / target_id / "meta.json"
        meta = {}
        if retry_io(lambda: target_meta_path.exists(), description=f"exists {target_meta_path}"):
            try:
                raw = retry_io(
                    lambda: target_meta_path.read_text(encoding="utf-8"),
                    description=f"read {target_meta_path}",
                )
                meta = json.loads(raw)
            except Exception:
                # If existing meta is broken, still keep stage-2 result.
                meta = {}

        # Keep legacy single-result field for backward compatibility.
        meta["target_quality_check"] = target_quality_check

        # Also keep provider-scoped Stage2 results so multi-provider target models
        # can display the correct pass/fail per provider in UI.
        provider_id = target_quality_check.get("provider_id")
        if isinstance(provider_id, str) and provider_id:
            checks_by_provider = meta.get("target_quality_checks_by_provider")
            if not isinstance(checks_by_provider, dict):
                checks_by_provider = {}
            checks_by_provider[provider_id] = target_quality_check
            meta["target_quality_checks_by_provider"] = checks_by_provider

        # Atomic write (tempfile + os.replace) with retry for SeaweedFS stability.
        retry_io(
            lambda: _write_json_atomic(target_meta_path, meta),
            description=f"write {target_meta_path}",
        )
        return target_meta_path

    def check_target_consistency_single(
        self,
        model_id: str,
        edit_id: str,
        provider: str,
        skip_render: bool = False,
        force_render: bool = False,
        skip_semaphore: bool = False,
    ) -> Tuple[str, str]:
        """Run Method-2 Stage-2 check for one target model."""
        target_id = f"{model_id}_edit_{edit_id}"
        edit_scope_id = _build_edit_scope_id(model_id, edit_id)
        item_id = f"{target_id}[{provider}]"

        # Keep defaults for error payload.
        tsr_cfg = self.config.edit_quality_check.two_stage_recon
        provider_id = "unknown"
        allowed_stage2_methods = ("two_stage_recon", "unified_judge")

        try:
            print(
                f"[Stage2] START source_model_id={model_id} edit_id={edit_id} "
                f"edit_scope_id={edit_scope_id} target_model_id={target_id} "
                f"provider={provider} skip_render={skip_render} force_render={force_render}"
            )
            if self.config.edit_quality_check.method not in allowed_stage2_methods:
                raise ValueError(
                    "check-target-consistency requires "
                    "edit_quality_check.method to be one of "
                    f"{allowed_stage2_methods}"
                )
            if tsr_cfg is None:
                raise ValueError(
                    "check-target-consistency requires "
                    "edit_quality_check.two_stage_recon config "
                    "(shared Stage-2 LPIPS settings)"
                )

            from utils.fs_retry import retry_io

            target_image_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
            if not retry_io(lambda: target_image_dir.exists(), description=f"exists {target_image_dir}"):
                raise FileNotFoundError(
                    f"edited view directory not found: {target_image_dir}"
                )

            if skip_render:
                from core.gen3d import get_model_id

                provider_id = get_model_id(provider)
            else:
                provider_id = self._ensure_target_views_rendered(
                    target_id, provider, force_render
                )

            target_render_dir = TRIPLETS_DIR / target_id / "views" / provider_id
            if not retry_io(lambda: target_render_dir.exists(), description=f"exists {target_render_dir}") or not any(
                retry_io(lambda: list(target_render_dir.glob("*.png")), description=f"glob {target_render_dir}")
            ):
                raise FileNotFoundError(
                    f"target render views not found: {target_render_dir}"
                )

            # Phase 4: skip semaphore when caller already holds a lane
            # slot (run_full_experiment path) to avoid double-throttling.
            if not skip_semaphore:
                self.recon_quality_check_semaphore.acquire()
            try:
                checker = _build_recon_checker(self.config)
                stage2_method = self.config.edit_quality_check.two_stage_recon.stage2_method
                if stage2_method == "vlm":
                    # Read instruction from edit meta for VLM prompt context
                    edit_meta_path = target_image_dir / "meta.json"
                    if not edit_meta_path.exists():
                        raise FileNotFoundError(
                            f"edit meta.json not found: {edit_meta_path}"
                        )
                    edit_meta = json.loads(edit_meta_path.read_text())
                    instruction = edit_meta.get("instruction") or ""
                    # Resolve source views dir for 3-image VLM mode
                    source_views_dir = TRIPLETS_DIR / model_id / "views" / provider_id
                    result = checker.check(
                        target_image_dir=target_image_dir,
                        target_render_dir=target_render_dir,
                        instruction=instruction,
                        source_views_dir=source_views_dir,
                    )
                else:
                    result = checker.check(
                        target_image_dir=target_image_dir,
                        target_render_dir=target_render_dir,
                    )
            finally:
                if not skip_semaphore:
                    self.recon_quality_check_semaphore.release()

            target_quality_check = result.to_dict()
            target_quality_check["provider"] = provider
            target_quality_check["provider_id"] = provider_id
            target_quality_check["target_model_id"] = target_id
            target_quality_check["checked_at"] = datetime.now().isoformat()

            target_meta_path = self._write_target_quality_check(
                target_id, target_quality_check
            )

            print(
                f"[Stage2] RESULT source_model_id={model_id} edit_id={edit_id} "
                f"edit_scope_id={edit_scope_id} target_model_id={target_id} "
                f"status={result.status} provider={provider} "
                f"provider_id={provider_id} score={result.score} "
                f"threshold={result.threshold}"
            )
            print("[Stage2] result:")
            print(json.dumps(target_quality_check, ensure_ascii=False, indent=2))

            if result.status == EDIT_STATUS_PASSED:
                self._record_result(
                    item_id,
                    "success",
                    f"(score={result.score}, threshold={result.threshold})",
                )
                print(f"[Stage2] meta={target_meta_path}")
                return item_id, "success"

            if result.status == EDIT_STATUS_FAILED_QUALITY:
                self._record_result(
                    item_id,
                    "failed",
                    (
                        f"(quality check failed: score={result.score}, "
                        f"threshold={result.threshold})"
                    ),
                )
                print(f"[Stage2] meta={target_meta_path}")
                return item_id, "failed"

            self._record_result(
                item_id,
                "failed",
                f"(unexpected status: {result.status})",
            )
            print(f"[Stage2] meta={target_meta_path}")
            return item_id, "failed"

        except Exception as e:
            traceback.print_exc()
            error_payload = {
                "method": "two_stage_recon",
                "status": EDIT_STATUS_ERROR_QUALITY_CHECK,
                "metric": tsr_cfg.metric if tsr_cfg is not None else None,
                "views": list(tsr_cfg.recon_views) if tsr_cfg is not None else [],
                "input_mode": tsr_cfg.input_mode if tsr_cfg is not None else None,
                "aggregate": tsr_cfg.aggregate if tsr_cfg is not None else None,
                "scores_by_view": {},
                "score": None,
                "threshold": tsr_cfg.threshold if tsr_cfg is not None else None,
                "target_image_paths": {},
                "target_render_paths": {},
                "reason": "target consistency execution error",
                "error_message": str(e),
                "provider": provider,
                "provider_id": provider_id,
                "target_model_id": target_id,
                "checked_at": datetime.now().isoformat(),
            }
            try:
                self._write_target_quality_check(target_id, error_payload)
            except Exception:
                traceback.print_exc()

            print(
                f"[Stage2] ERROR source_model_id={model_id} edit_id={edit_id} "
                f"edit_scope_id={edit_scope_id} target_model_id={target_id} "
                f"status={EDIT_STATUS_ERROR_QUALITY_CHECK} "
                f"provider={provider} provider_id={provider_id}"
            )
            print("[Stage2] error result:")
            print(json.dumps(error_payload, ensure_ascii=False, indent=2))

            self._record_result(item_id, "failed", f"({str(e)})")
            return item_id, "failed"

    def batch_check_target_consistency(
        self,
        provider: str,
        ids: Optional[List[str]] = None,
        edit_id: Optional[str] = None,
        target_ids: Optional[List[str]] = None,
        skip_render: bool = False,
        force_render: bool = False,
        dry_run: bool = False,
    ):
        """Batch run Method-2 Stage-2 target consistency check."""
        allowed_stage2_methods = ("two_stage_recon", "unified_judge")
        if self.config.edit_quality_check.method not in allowed_stage2_methods:
            raise ValueError(
                "check-target-consistency requires "
                "edit_quality_check.method to be one of "
                f"{allowed_stage2_methods}"
            )
        if self.config.edit_quality_check.two_stage_recon is None:
            raise ValueError(
                "check-target-consistency requires "
                "edit_quality_check.two_stage_recon config "
                "(shared Stage-2 LPIPS settings)"
            )

        concurrency = self.config.concurrency.recon_quality_check
        candidates: List[Tuple[str, str]] = []

        if target_ids:
            for target_id in target_ids:
                candidates.append(self._parse_target_model_id(target_id))
        else:
            all_batches = self.get_all_edit_batches(include_blocked=True)
            if ids:
                ids_set = set(ids)
                all_batches = [(m, e, i) for m, e, i in all_batches if m in ids_set]
            if edit_id:
                all_batches = [(m, e, i) for m, e, i in all_batches if e == edit_id]
            candidates = [(m, e) for m, e, _ in all_batches]

        # Deduplicate while preserving order.
        seen = set()
        unique_candidates = []
        for model_id, current_edit_id in candidates:
            key = (model_id, current_edit_id)
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append(key)

        print(f"\n{'=' * 60}")
        print("Batch Target 3D Consistency Check (Method-2 Stage-2)")
        print(f"{'=' * 60}")
        print(f"Provider: {provider}")
        print(f"Concurrency: {concurrency}")
        print(f"Total targets: {len(unique_candidates)}")
        print(f"Skip render: {skip_render}")
        print(f"Force render: {force_render}")
        if ids:
            print(f"Filter model ids: {', '.join(ids)}")
        if edit_id:
            print(f"Filter edit id: {edit_id}")
        if target_ids:
            print("Filter target ids: " + ", ".join(target_ids))
        print(f"{'=' * 60}\n")

        if dry_run:
            print("DRY RUN - Would check these target models:")
            for i, (model_id, current_edit_id) in enumerate(unique_candidates, 1):
                print(f"  {i}. {model_id}_edit_{current_edit_id}")
            return

        if not unique_candidates:
            print("Nothing to process!")
            return

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    self.check_target_consistency_single,
                    model_id,
                    current_edit_id,
                    provider,
                    skip_render,
                    force_render,
                ): (model_id, current_edit_id)
                for model_id, current_edit_id in unique_candidates
            }
            for future in as_completed(futures):
                future.result()

        self._print_summary()

    def _scan_refresh_all_dreamsim_targets(
        self,
        ids: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
        """Scan all refreshable target/provider pairs for LPIPS recomputation."""
        model_ids = ids or self.get_all_source_model_ids()
        refresh_targets: List[Dict[str, str]] = []
        skipped_models: List[Dict[str, str]] = []
        skipped_targets: List[Dict[str, str]] = []

        for model_id in model_ids:
            source_model_dir = MODELS_DIR / model_id
            if not source_model_dir.exists():
                skipped_models.append(
                    {
                        "model_id": model_id,
                        "reason": "model directory not found",
                    }
                )
                continue

            edited_base = TRIPLETS_DIR / model_id / "edited"
            if not edited_base.exists():
                skipped_models.append(
                    {
                        "model_id": model_id,
                        "reason": "no edited batches found",
                    }
                )
                continue

            before_count = len(refresh_targets)

            for edit_dir in sorted(edited_base.iterdir()):
                if not edit_dir.is_dir():
                    continue

                edit_id = edit_dir.name
                if not (edit_dir / "meta.json").exists():
                    skipped_targets.append(
                        {
                            "model_id": model_id,
                            "edit_id": edit_id,
                            "reason": "edit meta not found",
                        }
                    )
                    continue

                target_id = f"{model_id}_edit_{edit_id}"
                target_model_dir = MODELS_DIR / target_id
                if not target_model_dir.exists():
                    skipped_targets.append(
                        {
                            "model_id": model_id,
                            "edit_id": edit_id,
                            "target_model_id": target_id,
                            "reason": "target model directory not found",
                        }
                    )
                    continue

                glb_files = sorted(target_model_dir.glob("model_*.glb"))
                if not glb_files:
                    skipped_targets.append(
                        {
                            "model_id": model_id,
                            "edit_id": edit_id,
                            "target_model_id": target_id,
                            "reason": "target GLB not found",
                        }
                    )
                    continue

                for glb_path in glb_files:
                    provider_id = glb_path.stem.replace("model_", "")
                    provider = PROVIDER_ID_TO_NAME.get(provider_id)
                    if provider not in {"tripo", "hunyuan", "rodin"}:
                        skipped_targets.append(
                            {
                                "model_id": model_id,
                                "edit_id": edit_id,
                                "target_model_id": target_id,
                                "provider_id": provider_id,
                                "reason": "unsupported provider id",
                            }
                        )
                        continue

                    target_render_dir = TRIPLETS_DIR / target_id / "views" / provider_id
                    if not target_render_dir.exists() or not any(
                        target_render_dir.glob("*.png")
                    ):
                        skipped_targets.append(
                            {
                                "model_id": model_id,
                                "edit_id": edit_id,
                                "target_model_id": target_id,
                                "provider": provider,
                                "provider_id": provider_id,
                                "reason": "target render views not found",
                            }
                        )
                        continue

                    refresh_targets.append(
                        {
                            "model_id": model_id,
                            "edit_id": edit_id,
                            "target_model_id": target_id,
                            "provider": provider,
                            "provider_id": provider_id,
                        }
                    )

            if len(refresh_targets) == before_count:
                skipped_models.append(
                    {
                        "model_id": model_id,
                        "reason": "no refreshable LPIPS targets found",
                    }
                )

        return refresh_targets, skipped_models, skipped_targets

    def batch_refresh_all_dreamsim(
        self,
        ids: Optional[List[str]] = None,
        dry_run: bool = False,
    ):
        """Refresh Stage-2 LPIPS for all refreshable target models."""
        allowed_stage2_methods = ("two_stage_recon", "unified_judge")
        if self.config.edit_quality_check.method not in allowed_stage2_methods:
            raise ValueError(
                "refresh-all-lpips requires "
                "edit_quality_check.method to be one of "
                f"{allowed_stage2_methods}"
            )
        if self.config.edit_quality_check.two_stage_recon is None:
            raise ValueError(
                "refresh-all-lpips requires "
                "edit_quality_check.two_stage_recon config "
                "(shared Stage-2 LPIPS settings)"
            )

        concurrency = self.config.concurrency.refresh_all_dreamsim
        refresh_targets, skipped_models, skipped_targets = (
            self._scan_refresh_all_dreamsim_targets(ids=ids)
        )

        # Preload the Stage-2 checker module once before starting worker threads.
        stage2_method = self.config.edit_quality_check.two_stage_recon.stage2_method
        if stage2_method == "vlm":
            _get_vlm_recon_checker_class()
        else:
            _get_recon_checker_class()

        print(f"\n{'=' * 60}")
        print("Batch Refresh All LPIPS (Stage-2)")
        print(f"{'=' * 60}")
        print(f"Concurrency: {concurrency}")
        print(
            f"Source models requested: {len(ids) if ids else len(self.get_all_source_model_ids())}"
        )
        print(f"Refreshable targets: {len(refresh_targets)}")
        print(f"Skipped models: {len(skipped_models)}")
        print(f"Skipped targets: {len(skipped_targets)}")
        if ids:
            print(f"Filter model ids: {', '.join(ids)}")
        print(f"{'=' * 60}\n")

        if dry_run:
            print("DRY RUN - Would refresh these target models:")
            for index, item in enumerate(refresh_targets, 1):
                print(
                    f"  {index}. {item['target_model_id']} "
                    f"[provider={item['provider']}]"
                )
            if skipped_models:
                print("\nSkipped models:")
                for item in skipped_models:
                    print(f"  - {item['model_id']}: {item['reason']}")
            return

        if not refresh_targets:
            print("Nothing to process!")
            return

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    self.check_target_consistency_single,
                    item["model_id"],
                    item["edit_id"],
                    item["provider"],
                    True,
                    False,
                ): item
                for item in refresh_targets
            }
            for future in as_completed(futures):
                future.result()

        self._print_summary()
        if skipped_models:
            print("Skipped models:")
            for item in skipped_models:
                print(f"  - {item['model_id']}: {item['reason']}")


def main():
    parser = argparse.ArgumentParser(
        description="Unified batch processing for 3D generation, rendering, and editing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate 3D for all images without 3D models
  python scripts/batch_process.py gen3d --provider hunyuan
  
  # Generate 3D for specific images
  python scripts/batch_process.py gen3d --provider tripo --ids abc123 def456
  
  # Render all models without views
  python scripts/batch_process.py render
  
  # Edit views using first instruction of each model
  python scripts/batch_process.py edit --ids id1 id2 id3
  
  # Edit using second instruction (Replace)
  python scripts/batch_process.py edit --ids id1 id2 --instr-index 1
  
  # Edit using all instructions for each model
  python scripts/batch_process.py edit --ids id1 id2 --all-instructions
  
  # Dry run to see what would be processed
  python scripts/batch_process.py edit --dry-run
  
  # Force regenerate even if exists
  python scripts/batch_process.py render --force

  # Recompute LPIPS Stage-2 for all refreshable target models
  python scripts/batch_process.py refresh-all-lpips

  # Materialize missing mask artifacts for existing edit batches
  python scripts/batch_process.py materialize-edit-artifacts --ids model1 --edit-id a1b2c3d4
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # gen3d subcommand
    gen3d_parser = subparsers.add_parser("gen3d", help="Generate 3D models")
    gen3d_parser.add_argument(
        "--provider",
        "-p",
        required=True,
        choices=["tripo", "hunyuan", "rodin"],
        help="3D generation provider",
    )
    gen3d_parser.add_argument("--ids", nargs="+", help="Specific image IDs to process")
    gen3d_parser.add_argument(
        "--force", "-f", action="store_true", help="Force regenerate even if 3D exists"
    )
    gen3d_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be processed without doing it",
    )

    # render subcommand
    render_parser = subparsers.add_parser("render", help="Render model views")
    render_parser.add_argument(
        "--provider",
        required=False,
        choices=["tripo", "hunyuan", "rodin"],
        help="Which provider's GLB to render (selects model_{provider_id}.glb). Defaults to config.tasks['gen3d'].provider",
    )
    render_parser.add_argument("--ids", nargs="+", help="Specific model IDs to process")
    render_parser.add_argument(
        "--force", "-f", action="store_true", help="Force re-render even if views exist"
    )
    render_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be processed without doing it",
    )

    # edit subcommand
    edit_parser = subparsers.add_parser(
        "edit", help="Edit rendered views using model instructions"
    )
    edit_parser.add_argument("--ids", nargs="+", help="Specific model IDs to process")
    edit_parser.add_argument(
        "--instr-index",
        "-i",
        type=int,
        default=None,
        help="Use specific instruction index (0=first, 1=second)",
    )
    edit_parser.add_argument(
        "--instruction",
        type=str,
        default=None,
        help="Custom instruction text to use (overrides --instr-index)",
    )
    edit_parser.add_argument(
        "--all-instructions",
        "-a",
        action="store_true",
        help="Apply all instructions separately",
    )
    edit_parser.add_argument(
        "--max-per-type",
        "-m",
        type=int,
        default=1,
        help="Max instructions per type (remove/replace) per model. 0=no limit, 1=one of each type (default)",
    )
    edit_parser.add_argument(
        "--views",
        nargs="+",
        default=None,
        help="Which views to edit (default: all 6 views, MLLM will select which to actually edit)",
    )
    edit_parser.add_argument(
        "--mode",
        choices=["single", "multiview"],
        default="multiview",
        help="Edit mode: 'multiview' stitches views into 3x2 grid (default), 'single' uses guided editing with MLLM view selection",
    )
    edit_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force re-edit even if same instruction already applied",
    )
    edit_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be processed without doing it",
    )
    edit_parser.add_argument(
        "--provider-id",
        type=str,
        default=None,
        help="Provider ID whose rendered views to edit (e.g. 'tp3', 'hy3', 'rd2'). "
        "If omitted, auto-detects the first available provider subdir.",
    )

    # gen3d-from-edits subcommand
    gen3d_edits_parser = subparsers.add_parser(
        "gen3d-from-edits", help="Generate target 3D models from edited views"
    )
    gen3d_edits_parser.add_argument(
        "--provider",
        "-p",
        required=True,
        choices=["tripo", "hunyuan", "rodin"],
        help="3D generation provider",
    )
    gen3d_edits_parser.add_argument(
        "--ids",
        nargs="+",
        help="Specific model IDs to process (filters edit batches by source model)",
    )
    gen3d_edits_parser.add_argument(
        "--edit-id",
        type=str,
        default=None,
        help="Specific edit batch ID to process (exact match, e.g. 'a1b2c3d4')",
    )
    gen3d_edits_parser.add_argument(
        "--max-per-model",
        "-m",
        type=int,
        default=1,
        help="Max edit batches to process per model. 0=no limit, 1=one per model (default), 2=two per model",
    )
    gen3d_edits_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force regenerate even if Target 3D exists",
    )
    gen3d_edits_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be processed without doing it",
    )

    # check-edit-quality subcommand
    check_quality_parser = subparsers.add_parser(
        "check-edit-quality",
        help="Run quality check for existing edit batches and update meta.json",
    )
    check_quality_parser.add_argument(
        "--ids",
        nargs="+",
        help="Specific model IDs to process (source model ids)",
    )
    check_quality_parser.add_argument(
        "--edit-id",
        type=str,
        default=None,
        help="Specific edit batch ID to process (exact match, e.g. 'a1b2c3d4')",
    )
    check_quality_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be processed without doing it",
    )

    materialize_masks_parser = subparsers.add_parser(
        "materialize-edit-artifacts",
        help="Materialize missing mask artifacts for existing edit batches",
    )
    materialize_masks_parser.add_argument(
        "--ids",
        nargs="+",
        help="Specific source model IDs to process",
    )
    materialize_masks_parser.add_argument(
        "--edit-id",
        type=str,
        default=None,
        help="Specific edit batch ID to process",
    )
    materialize_masks_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be processed without doing it",
    )
    materialize_masks_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Recompute mask artifacts even if they already exist",
    )

    # check-target-consistency subcommand (Method-2 Stage-2)
    check_target_parser = subparsers.add_parser(
        "check-target-consistency",
        help=(
            "Run Target 3D consistency check (Method-2 Stage-2) and "
            "write target_quality_check to target meta.json"
        ),
    )
    check_target_parser.add_argument(
        "--provider",
        "-p",
        required=True,
        choices=["tripo", "hunyuan", "rodin"],
        help="Which provider's target GLB/render to evaluate",
    )
    check_target_parser.add_argument(
        "--ids",
        nargs="+",
        help="Specific source model IDs to process",
    )
    check_target_parser.add_argument(
        "--edit-id",
        type=str,
        default=None,
        help="Specific edit batch ID to process",
    )
    check_target_parser.add_argument(
        "--target-ids",
        nargs="+",
        default=None,
        help=("Directly specify target model IDs (format: <model_id>_edit_<edit_id>)"),
    )
    check_target_parser.add_argument(
        "--skip-render",
        action="store_true",
        help="Skip rendering step and only use existing target render views",
    )
    check_target_parser.add_argument(
        "--force-render",
        "-f",
        action="store_true",
        help="Force re-render target views before consistency check",
    )
    check_target_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be processed without doing it",
    )

    refresh_all_lpips_parser = subparsers.add_parser(
        "refresh-all-lpips",
        aliases=["refresh-all-dreamsim"],
        help=(
            "Recompute LPIPS Stage-2 for all refreshable target models "
            "across all source models"
        ),
    )
    refresh_all_lpips_parser.add_argument(
        "--ids",
        nargs="+",
        help="Optional source model IDs to limit the refresh scope",
    )
    refresh_all_lpips_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be processed without doing it",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = load_config()
    processor = BatchProcessor(config)

    if args.command == "gen3d":
        processor.batch_generate_3d(
            provider=args.provider, ids=args.ids, force=args.force, dry_run=args.dry_run
        )
    elif args.command == "render":
        # Use config default if provider not specified
        provider = args.provider
        if provider is None:
            provider = config.tasks["gen3d"].provider
        processor.batch_render(
            provider=provider, ids=args.ids, force=args.force, dry_run=args.dry_run
        )
    elif args.command == "edit":
        processor.batch_edit(
            ids=args.ids,
            instr_index=args.instr_index,
            instruction=args.instruction,
            all_instructions=args.all_instructions,
            max_per_type=args.max_per_type,
            views=args.views,
            mode=args.mode,
            force=args.force,
            dry_run=args.dry_run,
            provider_id=args.provider_id,
        )
    elif args.command == "gen3d-from-edits":
        processor.batch_gen3d_from_edits(
            provider=args.provider,
            ids=args.ids,
            edit_id=args.edit_id,
            max_per_model=args.max_per_model,
            force=args.force,
            dry_run=args.dry_run,
        )
    elif args.command == "check-edit-quality":
        processor.batch_recheck_edit_quality(
            ids=args.ids,
            edit_id=args.edit_id,
            dry_run=args.dry_run,
        )
    elif args.command == "materialize-edit-artifacts":
        processor.batch_materialize_missing_masks(
            ids=args.ids,
            edit_id=args.edit_id,
            force=args.force,
            dry_run=args.dry_run,
        )
    elif args.command == "check-target-consistency":
        if args.skip_render and args.force_render:
            raise ValueError("--skip-render and --force-render cannot be used together")
        processor.batch_check_target_consistency(
            provider=args.provider,
            ids=args.ids,
            edit_id=args.edit_id,
            target_ids=args.target_ids,
            skip_render=args.skip_render,
            force_render=args.force_render,
            dry_run=args.dry_run,
        )
    elif args.command in {"refresh-all-lpips", "refresh-all-dreamsim"}:
        processor.batch_refresh_all_dreamsim(
            ids=args.ids,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
