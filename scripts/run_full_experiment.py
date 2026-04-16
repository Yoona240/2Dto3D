#!/usr/bin/env python3
from __future__ import annotations

"""
End-to-end experiment runner for:
prompt -> T2I -> source 3D -> source render -> instruction generation ->
edit QC -> target 3D -> target render -> Stage-2 consistency check.

This version uses an adaptive instruction plan schema:
- Each object first generates a full instruction batch from instruction_plan
- No target quotas / retry-until-pass loops
- No style_ids in plan
- Category concurrency comes from config
"""

import argparse
import ast
import contextlib
import csv
import json
import math
import os
import random
import re
import statistics
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))


def _normalize_gpu_id(raw_value) -> int:
    value = str(raw_value).strip()
    if not value:
        raise ValueError("gpu-id must be a non-negative integer")
    try:
        gpu_id = int(value)
    except ValueError as exc:
        raise ValueError("gpu-id must be a non-negative integer") from exc
    if gpu_id < 0:
        raise ValueError("gpu-id must be a non-negative integer")
    return gpu_id


def _apply_gpu_visibility(gpu_id: int) -> None:
    gpu_id_str = str(gpu_id)
    os.environ["CUDA_VISIBLE_DEVICES"] = gpu_id_str
    os.environ["NVIDIA_VISIBLE_DEVICES"] = gpu_id_str


def _apply_gpu_visibility_from_argv(argv) -> Optional[int]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--gpu-id")
    early_args, _ = parser.parse_known_args(argv)
    if early_args.gpu_id is None:
        return None
    try:
        gpu_id = _normalize_gpu_id(early_args.gpu_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _apply_gpu_visibility(gpu_id)
    return gpu_id


EARLY_GPU_ID = _apply_gpu_visibility_from_argv(sys.argv[1:])

from core.gen3d import get_model_id
from core.image.caption import InstructionGenerator
from core.image.edit_quality_checker import (
    EDIT_STATUS_ERROR_QUALITY_CHECK,
    EDIT_STATUS_FAILED_QUALITY,
    EDIT_STATUS_PASSED,
    get_effective_edit_status,
)
from core.image.instruction_display_resolver import (
    build_instruction_display_payload,
    resolve_instruction_display_from_edit_meta,
    resolve_instruction_display_from_instruction_item,
    resolve_instruction_display_from_record,
)
from core.image.generator import T2IGenerator
from core.image.prompt_optimizer import PromptOptimizer
from scripts.batch_process import BatchProcessor
from utils.config import load_config
from utils.experiment_plan import (
    VALID_INSTRUCTION_TYPES,
    normalize_instruction_plan_from_category,
)
from utils.experiment_concurrency import (
    derive_run_full_experiment_category_workers,
    get_run_full_experiment_concurrency_limits,
)

VALID_PROVIDERS = {"hunyuan", "tripo", "rodin"}
VALID_EDIT_MODES = {"single", "multiview"}
LEGACY_TOP_LEVEL_FIELDS = {"category_workers", "instruction_type_ratio"}
LEGACY_CATEGORY_FIELDS = {
    "name",
    "prompt_budget",
    "target_source_models",
    "accepted_edits_per_model",
    "max_instruction_attempts",

}
RELABEL_STATE_NOT_STARTED = "not_started"
RELABEL_STATE_IN_PROGRESS = "in_progress"
RELABEL_STATE_PASSED = "passed"
RELABEL_STATE_FAILED = "failed"
RELABEL_TERMINAL_STATES = {RELABEL_STATE_PASSED, RELABEL_STATE_FAILED}
RELABEL_DISPLAY_STATUS_TO_STATE = {
    "relabel_passed": RELABEL_STATE_PASSED,
    "relabel_failed": RELABEL_STATE_FAILED,
}


def _normalize_relabel_state(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {
        RELABEL_STATE_NOT_STARTED,
        RELABEL_STATE_IN_PROGRESS,
        RELABEL_STATE_PASSED,
        RELABEL_STATE_FAILED,
    }:
        return normalized
    return None


def _resolve_relabel_lifecycle_payload(
    payload: Optional[Dict[str, Any]],
    *,
    edit_id: Optional[str] = None,
) -> Dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    display_status = str(source.get("instruction_display_status") or "").strip()
    relabel_result = source.get("stage1_relabel_result")
    if not isinstance(relabel_result, dict):
        relabel_result = {}

    state = _normalize_relabel_state(source.get("relabel_lifecycle_state"))
    if state is None:
        if display_status in RELABEL_DISPLAY_STATUS_TO_STATE:
            state = RELABEL_DISPLAY_STATUS_TO_STATE[display_status]
        elif relabel_result:
            state = RELABEL_STATE_IN_PROGRESS
        else:
            state = RELABEL_STATE_NOT_STARTED

    terminal = state in RELABEL_TERMINAL_STATES
    terminal_outcome = _normalize_relabel_state(source.get("relabel_terminal_outcome"))
    if terminal:
        if terminal_outcome not in RELABEL_TERMINAL_STATES:
            terminal_outcome = state
    else:
        terminal_outcome = None

    last_edit_id = source.get("relabel_last_edit_id") or edit_id
    terminal_edit_id = source.get("relabel_terminal_edit_id")
    if terminal and not terminal_edit_id:
        terminal_edit_id = edit_id
    if not terminal:
        terminal_edit_id = None

    relabel_attempted = bool(relabel_result) or state in {
        RELABEL_STATE_IN_PROGRESS,
        RELABEL_STATE_PASSED,
        RELABEL_STATE_FAILED,
    }

    return {
        "relabel_lifecycle_state": state,
        "relabel_terminal": terminal,
        "relabel_terminal_outcome": terminal_outcome,
        "relabel_attempted": relabel_attempted,
        "relabel_last_edit_id": last_edit_id,
        "relabel_terminal_edit_id": terminal_edit_id,
    }


@dataclass(frozen=True)
class RandomSelectionPlan:
    category: bool
    object: bool


@dataclass(frozen=True)
class InstructionPlan:
    mode: str
    count: int
    allowed_types: List[str]


@dataclass(frozen=True)
class CategoryPlan:
    category_name: Optional[str]
    random: RandomSelectionPlan
    objects: Optional[List[str]]
    object_count: int
    instruction_plan: InstructionPlan
    style_ids: Optional[List[str]] = None


@dataclass(frozen=True)
class ExperimentPlan:
    name: str
    source_provider: str
    target_provider: str
    edit_mode: str
    categories: List[CategoryPlan]


@dataclass(frozen=True)
class ObjectJob:
    plan_index: int
    selection_mode: str
    category: str
    object_name: str
    instruction_plan: InstructionPlan
    requested_category_name: Optional[str]
    style_id: Optional[str] = None


class LaneOperationError(RuntimeError):
    def __init__(self, lane_name: str, operation_name: str, original_error: Exception):
        super().__init__(str(original_error))
        self.lane_name = lane_name
        self.operation_name = operation_name
        self.original_error = original_error


class StageExecutionExhaustedError(RuntimeError):
    def __init__(
        self,
        stage_name: str,
        attempt_errors: List[Dict[str, Any]],
        *,
        last_result: Any = None,
    ):
        message = (
            attempt_errors[-1]["error_message"]
            if attempt_errors
            else f"{stage_name} exhausted all attempts"
        )
        super().__init__(message)
        self.stage_name = stage_name
        self.attempt_errors = attempt_errors
        self.last_result = last_result


def _extract_instruction_retry_hint(error_message: str) -> Optional[str]:
    if not isinstance(error_message, str) or "instruction=" not in error_message:
        return None
    instruction_literal = error_message.split("instruction=", 1)[1].strip()
    if not instruction_literal:
        return None
    try:
        value = ast.literal_eval(instruction_literal)
    except Exception:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _build_edit_scope_id(
    source_model_id: Optional[Any],
    edit_id: Optional[Any],
) -> Optional[str]:
    if source_model_id is None or edit_id is None:
        return None
    source_text = str(source_model_id).strip()
    edit_text = str(edit_id).strip()
    if not source_text or not edit_text:
        return None
    return f"{source_text}_edited_{edit_text}"


class ApiLane:
    """Concurrency limiter for a class of API calls.

    Each lane enforces a maximum number of concurrent in-flight operations
    using a ticket-queue for FIFO ordering.

    Phase-3 redesign: the global cooldown / recovery-probe mechanism has been
    removed.  When a single request fails the lane no longer freezes all
    queued callers.  Instead, retry back-off is handled per-request inside
    ``_execute_stage_with_retry`` (exponential back-off + jitter).

    The ``cooldown_seconds``, ``recovery_probe_one_by_one``, and ``enabled``
    constructor parameters are accepted but **ignored** so the existing
    config schema stays backward-compatible.
    """

    def __init__(
        self,
        *,
        name: str,
        concurrency: int,
        cooldown_seconds: int = 0,
        recovery_probe_one_by_one: bool = False,
        enabled: bool = True,
    ):
        self.name = name
        self.concurrency = concurrency
        self._condition = threading.Condition()
        self._in_flight = 0
        self._next_ticket = 0
        self._active_ticket = 0
        # ---------- observability counters (Phase 7) ----------
        self._total_acquired = 0
        self._total_released = 0
        self._total_failures = 0
        self._peak_in_flight = 0
        self._total_wait_seconds = 0.0
        # ---------- per-thread hold tracking ----------
        self._thread_local = threading.local()

    # ---- public API ----

    @contextlib.contextmanager
    def hold(self):
        """Acquire the slot once and hold it across multiple run() calls on
        this thread.  While held, run() skips acquire/release so the slot is
        never temporarily freed between retry attempts."""
        self._acquire_slot()
        self._thread_local.holding = True
        try:
            yield
        finally:
            self._thread_local.holding = False
            self._release_slot(success=True)

    def run(self, operation_name: str, func: Callable[[], Any]) -> Any:
        already_held = getattr(self._thread_local, "holding", False)
        if not already_held:
            self._acquire_slot()
        try:
            result = func()
        except Exception:
            if not already_held:
                self._release_slot(success=False)
            raise
        if not already_held:
            self._release_slot(success=True)
        return result

    def snapshot(self) -> Dict[str, Any]:
        """Return a point-in-time snapshot of lane metrics."""
        with self._condition:
            return {
                "name": self.name,
                "concurrency": self.concurrency,
                "in_flight": self._in_flight,
                "peak_in_flight": self._peak_in_flight,
                "total_acquired": self._total_acquired,
                "total_released": self._total_released,
                "total_failures": self._total_failures,
                "avg_wait_seconds": (
                    round(self._total_wait_seconds / max(self._total_acquired, 1), 4)
                ),
            }

    # ---- internal ----

    def _acquire_slot(self) -> None:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            wait_start = time.monotonic()
            while True:
                if ticket != self._active_ticket:
                    self._condition.wait()
                    continue
                if self._in_flight >= self.concurrency:
                    self._condition.wait()
                    continue
                self._in_flight += 1
                self._active_ticket += 1
                self._total_acquired += 1
                if self._in_flight > self._peak_in_flight:
                    self._peak_in_flight = self._in_flight
                self._total_wait_seconds += time.monotonic() - wait_start
                self._condition.notify_all()
                return

    def _release_slot(self, *, success: bool) -> None:
        with self._condition:
            self._in_flight -= 1
            self._total_released += 1
            if not success:
                self._total_failures += 1
            self._condition.notify_all()


def _slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text.strip()).strip("-").lower()
    return slug or "experiment"


def _load_structured_file(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"Plan file not found: {path}")
    suffix = path.suffix.lower()
    with open(path, "r", encoding="utf-8") as f:
        if suffix == ".json":
            return json.load(f)
        return yaml.safe_load(f)


def _require_mapping(data: Any, path: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a mapping")
    return data


def _require_list(data: Any, path: str) -> List[Any]:
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a list")
    return data


def _require_non_empty_str(data: Any, path: str) -> str:
    if not isinstance(data, str) or not data.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return data.strip()


def _require_positive_int(data: Any, path: str) -> int:
    if not isinstance(data, int) or data <= 0:
        raise ValueError(f"{path} must be a positive integer")
    return data


def _require_non_negative_int(data: Any, path: str) -> int:
    if not isinstance(data, int) or data < 0:
        raise ValueError(f"{path} must be a non-negative integer")
    return data


def _require_bool(data: Any, path: str) -> bool:
    if not isinstance(data, bool):
        raise ValueError(f"{path} must be a boolean")
    return data


def _reject_legacy_fields(raw: Dict[str, Any]) -> None:
    legacy_top = sorted(key for key in raw.keys() if key in LEGACY_TOP_LEVEL_FIELDS)
    if legacy_top:
        raise ValueError(
            "Legacy run_full_experiment plan format is no longer supported. "
            f"Remove top-level fields: {legacy_top}"
        )

    categories_raw = raw.get("categories")
    if isinstance(categories_raw, list):
        for index, item in enumerate(categories_raw):
            if not isinstance(item, dict):
                continue
            legacy_category = sorted(
                key for key in item.keys() if key in LEGACY_CATEGORY_FIELDS
            )
            if legacy_category:
                raise ValueError(
                    "Legacy run_full_experiment category format is no longer supported. "
                    f"plan.categories[{index}] contains deprecated fields: {legacy_category}"
                )


def _parse_instruction_plan(data: Dict[str, Any], path: str) -> InstructionPlan:
    normalized = normalize_instruction_plan_from_category(
        data,
        path,
        allow_legacy_counts=True,
    )
    return InstructionPlan(
        mode=str(normalized["mode"]),
        count=_require_positive_int(
            normalized["count"], f"{path}.instruction_plan.count"
        ),
        allowed_types=[
            _require_non_empty_str(
                instruction_type,
                f"{path}.instruction_plan.allowed_types[{index}]",
            )
            for index, instruction_type in enumerate(normalized["allowed_types"])
        ],
    )


def _instruction_plan_payload(instruction_plan: InstructionPlan) -> Dict[str, Any]:
    return {
        "mode": instruction_plan.mode,
        "count": instruction_plan.count,
        "allowed_types": list(instruction_plan.allowed_types),
    }


def _load_plan_from_mapping(
    raw: Dict[str, Any],
    *,
    default_name: str,
    path_label: str,
) -> ExperimentPlan:
    _reject_legacy_fields(raw)

    name = _require_non_empty_str(raw.get("name", default_name), f"{path_label}.name")
    source_provider = _require_non_empty_str(
        raw.get("source_provider"), f"{path_label}.source_provider"
    )
    if source_provider not in VALID_PROVIDERS:
        raise ValueError(
            f"{path_label}.source_provider must be one of {sorted(VALID_PROVIDERS)}"
        )

    target_provider = raw.get("target_provider")
    if target_provider is None:
        target_provider = source_provider
    target_provider = _require_non_empty_str(
        target_provider, f"{path_label}.target_provider"
    )
    if target_provider not in VALID_PROVIDERS:
        raise ValueError(
            f"{path_label}.target_provider must be one of {sorted(VALID_PROVIDERS)}"
        )

    edit_mode = _require_non_empty_str(raw.get("edit_mode"), f"{path_label}.edit_mode")
    if edit_mode not in VALID_EDIT_MODES:
        raise ValueError(
            f"{path_label}.edit_mode must be one of {sorted(VALID_EDIT_MODES)}"
        )

    categories_raw = _require_list(raw.get("categories"), f"{path_label}.categories")
    categories: List[CategoryPlan] = []
    for index, item in enumerate(categories_raw):
        path_prefix = f"{path_label}.categories[{index}]"
        item_map = _require_mapping(item, path_prefix)
        random_map = _require_mapping(item_map.get("random"), f"{path_prefix}.random")
        random_selection = RandomSelectionPlan(
            category=_require_bool(
                random_map.get("category"), f"{path_prefix}.random.category"
            ),
            object=_require_bool(
                random_map.get("object"), f"{path_prefix}.random.object"
            ),
        )

        category_name_raw = item_map.get("category_name")
        category_name: Optional[str] = None
        if category_name_raw is not None:
            category_name = _require_non_empty_str(
                category_name_raw, f"{path_prefix}.category_name"
            )

        objects_raw = item_map.get("objects")
        objects: Optional[List[str]] = None
        if objects_raw is not None:
            objects = []
            for object_index, object_name in enumerate(
                _require_list(objects_raw, f"{path_prefix}.objects")
            ):
                objects.append(
                    _require_non_empty_str(
                        object_name, f"{path_prefix}.objects[{object_index}]"
                    )
                )

        style_ids_raw = item_map.get("style_ids")
        style_ids: Optional[List[str]] = None
        if style_ids_raw is not None:
            style_ids = [
                _require_non_empty_str(s, f"{path_prefix}.style_ids[{si}]")
                for si, s in enumerate(
                    _require_list(style_ids_raw, f"{path_prefix}.style_ids")
                )
            ]
            if objects is not None and len(style_ids) != len(objects):
                raise ValueError(
                    f"{path_prefix}.style_ids length ({len(style_ids)}) must match "
                    f"objects length ({len(objects)})"
                )

        categories.append(
            CategoryPlan(
                category_name=category_name,
                random=random_selection,
                objects=objects,
                object_count=_require_positive_int(
                    item_map.get("object_count"), f"{path_prefix}.object_count"
                ),
                instruction_plan=_parse_instruction_plan(
                    item_map,
                    path_prefix,
                ),
                style_ids=style_ids,
            )
        )

    if not categories:
        raise ValueError(f"{path_label}.categories must contain at least one category")

    return ExperimentPlan(
        name=name,
        source_provider=source_provider,
        target_provider=target_provider,
        edit_mode=edit_mode,
        categories=categories,
    )


def load_plan(path: Path) -> ExperimentPlan:
    raw = _require_mapping(_load_structured_file(path), "plan")
    return _load_plan_from_mapping(raw, default_name=path.stem, path_label="plan")


def _resolve_recorded_path(recorded_path: str, pipeline_dir: Path) -> Path:
    path = Path(recorded_path)
    if path.is_absolute():
        return path
    if recorded_path.startswith("pipeline/"):
        relative = recorded_path[len("pipeline/") :]
        return pipeline_dir / relative
    return PROJECT_ROOT / recorded_path


class ExperimentRunner:
    def __init__(
        self,
        plan: ExperimentPlan,
        plan_path: Path,
        *,
        experiment_id: Optional[str] = None,
        gpu_id: Optional[int] = None,
    ):
        self.plan = plan
        self.plan_path = plan_path
        self.gpu_id = gpu_id
        self.config = load_config()
        self.batch_processor = BatchProcessor(self.config)
        self.category_workers_limits = get_run_full_experiment_concurrency_limits(
            config=self.config,
            source_provider=plan.source_provider,
            target_provider=plan.target_provider,
        )
        self.category_workers = derive_run_full_experiment_category_workers(
            config=self.config,
            source_provider=plan.source_provider,
            target_provider=plan.target_provider,
        )
        self.object_workers = self.category_workers
        self.scheduler_mode = "global_object_workers_conservative"

        _allowed_full_exp_methods = ("two_stage_recon", "unified_judge")
        if self.config.edit_quality_check.method not in _allowed_full_exp_methods:
            raise ValueError(
                f"run_full_experiment.py requires edit_quality_check.method to be one of "
                f"{_allowed_full_exp_methods} because Stage-2 consistency check is mandatory "
                "in this pipeline"
            )
        if self.config.edit_quality_check.two_stage_recon is None:
            raise ValueError(
                "run_full_experiment.py requires edit_quality_check.two_stage_recon "
                "(needed for Stage-2 LPIPS consistency check parameters)"
            )

        pipeline_dir_raw = self.config.workspace.pipeline_dir
        self.pipeline_dir = (
            Path(pipeline_dir_raw)
            if Path(pipeline_dir_raw).is_absolute()
            else PROJECT_ROOT / pipeline_dir_raw
        )
        self.prompts_dir = self.pipeline_dir / "prompts"
        self.images_dir = self.pipeline_dir / "images"
        self.models_dir = self.pipeline_dir / "models_src"
        self.triplets_dir = self.pipeline_dir / "triplets"

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.experiment_id = experiment_id or f"{timestamp}_{_slugify(self.plan.name)}"
        self.experiment_dir = self.pipeline_dir / "experiments" / self.experiment_id
        self.events_path = self.experiment_dir / "events.jsonl"
        self.summary_json_path = self.experiment_dir / "summary.json"
        self.summary_csv_path = self.experiment_dir / "summary.csv"
        self.manifest_path = self.experiment_dir / "manifest.json"
        self.execution_plan_path = self.experiment_dir / "execution_plan.json"
        self.object_records_path = self.experiment_dir / "object_records.jsonl"
        self.edit_records_path = self.experiment_dir / "edit_records.jsonl"
        self.category_stats_json_path = self.experiment_dir / "category_stats.json"
        self.category_stats_csv_path = self.experiment_dir / "category_stats.csv"
        self.stage_timing_summary_csv_path = (
            self.experiment_dir / "stage_timing_summary.csv"
        )
        self.prompt_records_path = (
            self.prompts_dir / f"batch_{self.experiment_id}.jsonl"
        )

        self.experiment_dir.mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)

        self.events_lock = threading.Lock()
        self.prompts_lock = threading.Lock()
        self.records_lock = threading.Lock()
        self.manifest_lock = threading.Lock()

        lane_control = self.config.run_full_experiment.api_lane_control
        self.api_lanes = {
            "oneapi_text": ApiLane(
                name="oneapi_text",
                concurrency=self.config.concurrency.text,
                cooldown_seconds=lane_control.cooldown_seconds,
                recovery_probe_one_by_one=lane_control.recovery_probe_one_by_one,
                enabled=lane_control.enabled,
            ),
            "oneapi_image": ApiLane(
                name="oneapi_image",
                concurrency=self.config.concurrency.image,
                cooldown_seconds=lane_control.cooldown_seconds,
                recovery_probe_one_by_one=lane_control.recovery_probe_one_by_one,
                enabled=lane_control.enabled,
            ),
            "render": ApiLane(
                name="render",
                concurrency=self.config.concurrency.render,
                cooldown_seconds=lane_control.cooldown_seconds,
                recovery_probe_one_by_one=lane_control.recovery_probe_one_by_one,
                enabled=lane_control.enabled,
            ),
            "recon_quality_check": ApiLane(
                name="recon_quality_check",
                concurrency=self.config.concurrency.recon_quality_check,
                cooldown_seconds=lane_control.cooldown_seconds,
                recovery_probe_one_by_one=lane_control.recovery_probe_one_by_one,
                enabled=lane_control.enabled,
            ),
            "hunyuan_gen3d": ApiLane(
                name="hunyuan_gen3d",
                concurrency=self.config.concurrency.gen3d.hunyuan,
                cooldown_seconds=lane_control.cooldown_seconds,
                recovery_probe_one_by_one=lane_control.recovery_probe_one_by_one,
                enabled=lane_control.enabled,
            ),
            "tripo_gen3d": ApiLane(
                name="tripo_gen3d",
                concurrency=self.config.concurrency.gen3d.tripo,
                cooldown_seconds=lane_control.cooldown_seconds,
                recovery_probe_one_by_one=lane_control.recovery_probe_one_by_one,
                enabled=lane_control.enabled,
            ),
            "rodin_gen3d": ApiLane(
                name="rodin_gen3d",
                concurrency=self.config.concurrency.gen3d.rodin,
                cooldown_seconds=lane_control.cooldown_seconds,
                recovery_probe_one_by_one=lane_control.recovery_probe_one_by_one,
                enabled=lane_control.enabled,
            ),
        }

        objects_file = Path(self.config.workspace.matrix_objects_file)
        if not objects_file.is_absolute():
            objects_file = Path(__file__).parent.parent / objects_file
        with open(objects_file, "r", encoding="utf-8") as f:
            self.objects_data = _require_mapping(
                json.load(f), str(objects_file.name)
            )

        self._validate_plan_against_datasets()
        self.random_category_assignments = self._assign_random_categories()
        self.prompt_records = self._read_jsonl(self.prompt_records_path)
        self.object_records = self._read_jsonl(self.object_records_path)
        self.edit_records = self._read_jsonl(self.edit_records_path)
        self.object_record_index: Dict[str, int] = {}
        self.edit_record_index: Dict[Tuple[str, int], int] = {}
        self._rebuild_record_indexes()
        self.started_at: Optional[str] = None
        if self.manifest_path.exists():
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                existing_manifest = json.load(f)
            started_at_raw = existing_manifest.get("started_at")
            if isinstance(started_at_raw, str) and started_at_raw.strip():
                self.started_at = started_at_raw
        self.execution_plan = self._load_or_build_execution_plan()
        self.prompt_record_map: Dict[Tuple[int, int], Dict[str, Any]] = {
            (
                int(record["plan_index"]),
                int(record["object_index"]),
            ): record
            for record in self._latest_prompt_records()
        }

    def _read_jsonl(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        records: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records

    def _latest_prompt_records(self) -> List[Dict[str, Any]]:
        latest: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for record in self.prompt_records:
            key = (
                int(record["plan_index"]),
                int(record["object_index"]),
            )
            latest[key] = record
        return [
            latest[key]
            for key in sorted(latest.keys(), key=lambda item: (item[0], item[1]))
        ]

    def _object_record_key(self, record: Dict[str, Any]) -> str:
        source_model_id = record.get("source_model_id")
        if isinstance(source_model_id, str) and source_model_id.strip():
            return source_model_id.strip()
        return (
            f"{record.get('plan_index')}::{record.get('object_index')}::"
            f"{record.get('object_key')}"
        )

    def _edit_record_key(self, record: Dict[str, Any]) -> Tuple[str, int]:
        source_model_id = str(record.get("source_model_id") or "")
        instruction_index = int(record.get("instruction_index") or 0)
        return (source_model_id, instruction_index)

    def _rebuild_record_indexes(self) -> None:
        self.object_record_index = {
            self._object_record_key(record): index
            for index, record in enumerate(self.object_records)
        }
        self.edit_record_index = {
            self._edit_record_key(record): index
            for index, record in enumerate(self.edit_records)
            if record.get("source_model_id") is not None
            and record.get("instruction_index") is not None
        }

    def _upsert_object_record_locked(self, record: Dict[str, Any]) -> None:
        key = self._object_record_key(record)
        existing_index = self.object_record_index.get(key)
        if existing_index is None:
            self.object_record_index[key] = len(self.object_records)
            self.object_records.append(record)
            return
        self.object_records[existing_index] = record

    def _upsert_edit_records_locked(self, records: List[Dict[str, Any]]) -> None:
        for record in records:
            key = self._edit_record_key(record)
            existing_index = self.edit_record_index.get(key)
            if existing_index is None:
                self.edit_record_index[key] = len(self.edit_records)
                self.edit_records.append(record)
                continue
            self.edit_records[existing_index] = record

    def _planned_object_count(self) -> int:
        return sum(category.object_count for category in self.plan.categories)

    def _planned_edit_count(self) -> int:
        return sum(
            category.object_count * category.instruction_plan.count
            for category in self.plan.categories
        )

    def _totals_from_records(
        self,
        object_records: List[Dict[str, Any]],
        edit_records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "object_count": len(object_records),
            "edit_attempts_total": len(edit_records),
            "stage1_failed_count": sum(
                1
                for record in edit_records
                if record.get("stage1_status")
                in {EDIT_STATUS_FAILED_QUALITY, EDIT_STATUS_ERROR_QUALITY_CHECK}
            ),
            "stage2_entered_count": sum(
                1 for record in edit_records if record.get("entered_stage2")
            ),
            "stage2_passed_count": sum(
                1
                for record in edit_records
                if record.get("stage2_status") == EDIT_STATUS_PASSED
            ),
            "stage2_failed_count": sum(
                1
                for record in edit_records
                if record.get("stage2_status") == EDIT_STATUS_FAILED_QUALITY
            ),
        }

    def _progress_payload(
        self,
        object_records: List[Dict[str, Any]],
        edit_records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "planned_object_count": self._planned_object_count(),
            "planned_edit_count": self._planned_edit_count(),
            "prompt_record_count": len(self._latest_prompt_records()),
            "object_record_count": len(object_records),
            "edit_record_count": len(edit_records),
        }

    def _validate_plan_against_datasets(self) -> None:
        valid_categories = set(self.objects_data.keys())
        fixed_category_names: List[str] = []
        random_object_counts: List[int] = []

        for index, category in enumerate(self.plan.categories):
            path_prefix = f"plan.categories[{index}]"
            random_category = category.random.category
            random_object = category.random.object

            if random_category and not random_object:
                raise ValueError(
                    f"{path_prefix} does not support random.category=true with "
                    "random.object=false"
                )

            if random_category:
                random_object_counts.append(category.object_count)
                if category.category_name is not None:
                    raise ValueError(
                        f"{path_prefix}.category_name is not allowed when random.category=true"
                    )
                if category.objects is not None:
                    raise ValueError(
                        f"{path_prefix}.objects is not allowed when random.category=true"
                    )
                continue

            if category.category_name is None:
                raise ValueError(
                    f"{path_prefix}.category_name is required when random.category=false"
                )
            if category.category_name not in valid_categories:
                raise ValueError(
                    f"Unknown category {category.category_name!r}; "
                    f"valid categories: {sorted(valid_categories)}"
                )
            fixed_category_names.append(category.category_name)

            category_objects = self.objects_data[category.category_name]
            if random_object:
                if category.objects is not None:
                    raise ValueError(
                        f"{path_prefix}.objects is not allowed when random.object=true"
                    )
                if category.object_count > len(category_objects):
                    raise ValueError(
                        f"{path_prefix}.object_count={category.object_count} exceeds "
                        f"available objects in {category.category_name!r}: {len(category_objects)}"
                    )
                continue

            if category.objects is None or len(category.objects) == 0:
                raise ValueError(
                    f"{path_prefix}.objects is required when random.object=false"
                )
            if category.object_count != len(category.objects):
                raise ValueError(
                    f"{path_prefix}.object_count must equal len(objects) when random.object=false"
                )
            duplicates = sorted(
                {name for name in category.objects if category.objects.count(name) > 1}
            )
            if duplicates:
                raise ValueError(
                    f"{path_prefix}.objects contains duplicates: {duplicates}"
                )
            invalid_objects = [
                object_name
                for object_name in category.objects
                if object_name not in category_objects
            ]
            if invalid_objects:
                raise ValueError(
                    f"{path_prefix}.objects contains invalid objects for "
                    f"{category.category_name!r}: {invalid_objects}"
                )

        duplicates = sorted(
            {
                name
                for name in fixed_category_names
                if fixed_category_names.count(name) > 1
            }
        )
        if duplicates:
            raise ValueError(
                "Duplicate fixed category_name values are not allowed: "
                + ", ".join(duplicates)
            )

        unique_fixed_count = len(set(fixed_category_names))
        if unique_fixed_count + len(random_object_counts) > len(valid_categories):
            raise ValueError(
                "Requested fixed categories plus random category entries exceed the total "
                f"available categories ({len(valid_categories)})"
            )

        remaining_category_capacities = {
            category_name: len(self.objects_data[category_name])
            for category_name in valid_categories
            if category_name not in set(fixed_category_names)
        }
        for object_count in sorted(random_object_counts, reverse=True):
            eligible_categories = sorted(
                [
                    category_name
                    for category_name, capacity in remaining_category_capacities.items()
                    if capacity >= object_count
                ],
                key=lambda category_name: (
                    remaining_category_capacities[category_name],
                    category_name,
                ),
            )
            if not eligible_categories:
                raise ValueError(
                    "Random category entries cannot be assigned to distinct categories with "
                    f"enough objects for object_count={object_count}"
                )
            del remaining_category_capacities[eligible_categories[0]]

    def _assign_random_categories(self) -> Dict[int, str]:
        fixed_categories = {
            category.category_name
            for category in self.plan.categories
            if category.category_name is not None
        }
        random_indices = [
            index
            for index, category in enumerate(self.plan.categories)
            if category.random.category
        ]
        if not random_indices:
            return {}

        remaining_categories = {
            category_name: len(object_names)
            for category_name, object_names in self.objects_data.items()
            if category_name not in fixed_categories
        }
        assignments: Dict[int, str] = {}
        random_entries = sorted(
            (
                (index, self.plan.categories[index].object_count)
                for index in random_indices
            ),
            key=lambda item: item[1],
            reverse=True,
        )

        for plan_index, object_count in random_entries:
            eligible_categories = [
                category_name
                for category_name, capacity in remaining_categories.items()
                if capacity >= object_count
            ]
            if not eligible_categories:
                raise ValueError(
                    "Random category entries exceed the remaining categories that can "
                    f"satisfy object_count={object_count}"
                )
            selected_category = random.choice(eligible_categories)
            assignments[plan_index] = selected_category
            del remaining_categories[selected_category]

        return assignments

    def _lane_name_for_gen3d(self, provider: str) -> str:
        return f"{provider}_gen3d"

    def _run_in_lane(
        self,
        lane_name: str,
        operation_name: str,
        func: Callable[[], Any],
    ) -> Any:
        lane = self.api_lanes[lane_name]
        try:
            return lane.run(operation_name, func)
        except Exception as exc:
            raise LaneOperationError(lane_name, operation_name, exc) from exc

    def _extract_response_context(self, exc: Exception) -> Optional[Dict[str, Any]]:
        response_context: Dict[str, Any] = {}
        response = getattr(exc, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", None)
            if status_code is not None:
                response_context["status_code"] = status_code
            try:
                response_context["response_text"] = response.text[:2000]
            except Exception:
                pass
        request = getattr(exc, "request", None)
        if request is not None:
            method = getattr(request, "method", None)
            if method is not None:
                response_context["request_method"] = method
            url = getattr(request, "url", None)
            if url is not None:
                response_context["request_url"] = str(url)
        return response_context or None

    def _build_attempt_error_from_exception(
        self,
        *,
        stage_name: str,
        attempt_index: int,
        exc: Exception,
    ) -> Dict[str, Any]:
        api_lane = None
        operation_name = stage_name
        original_error = exc
        if isinstance(exc, LaneOperationError):
            api_lane = exc.lane_name
            operation_name = exc.operation_name
            original_error = exc.original_error
        return {
            "stage_name": stage_name,
            "attempt_index": attempt_index,
            "api_lane": api_lane,
            "operation_name": operation_name,
            "error_type": type(original_error).__name__,
            "error_message": str(original_error),
            "traceback": traceback.format_exc(),
            "response_context": self._extract_response_context(original_error),
            "timestamp": datetime.now().isoformat(),
        }

    def _build_attempt_error_from_failure(
        self,
        *,
        stage_name: str,
        attempt_index: int,
        error_type: str,
        error_message: str,
        api_lane: Optional[str],
        response_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "stage_name": stage_name,
            "attempt_index": attempt_index,
            "api_lane": api_lane,
            "operation_name": stage_name,
            "error_type": error_type,
            "error_message": error_message,
            "traceback": None,
            "response_context": response_context,
            "timestamp": datetime.now().isoformat(),
        }

    def _ensure_timing_fields(self, record: Dict[str, Any]) -> None:
        record.setdefault("timings", {})
        record.setdefault("timing_attempts", {})

    def _public_timing_stage_name(self, stage_name: str) -> str:
        aliases = {
            "stage2": "stage2_consistency_check",
        }
        return aliases.get(stage_name, stage_name)

    def _timing_scope_for_stage(self, stage_name: str) -> str:
        stage_name = self._public_timing_stage_name(stage_name)
        if stage_name in {
            "source_prompt_optimization",
            "source_t2i",
            "source_gen3d",
            "source_render",
            "instruction_generation",
            "source_pipeline_total",
            "object_total",
        }:
            return "object"
        if stage_name in {
            "edit_apply",
            "mask_artifact_build",
            "stage1_quality_check",
            "stage1_total",
            "target_gen3d",
            "target_render",
            "stage2_consistency_check",
            "edit_pipeline_total",
        }:
            return "edit"
        return "experiment"

    def _timing_context_from_record(
        self,
        record: Optional[Dict[str, Any]],
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        context: Dict[str, Any] = {}
        if isinstance(record, dict):
            for key in [
                "experiment_id",
                "plan_index",
                "object_index",
                "category",
                "object_name",
                "source_model_id",
                "instruction_index",
                "instruction_type",
                "edit_id",
                "target_model_id",
            ]:
                value = record.get(key)
                if value is not None:
                    context[key] = value
        if extra_context:
            for key, value in extra_context.items():
                if value is not None:
                    context[key] = value
        if context.get("edit_scope_id") is None:
            edit_scope_id = _build_edit_scope_id(
                context.get("source_model_id"),
                context.get("edit_id"),
            )
            if edit_scope_id is not None:
                context["edit_scope_id"] = edit_scope_id
        return context

    @staticmethod
    def _response_context_preview(
        response_context: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not isinstance(response_context, dict) or not response_context:
            return None
        preview_parts: List[str] = []
        for key in [
            "status",
            "status_code",
            "request_method",
            "request_url",
            "target_model_id",
            "target_gen3d_error_class",
            "target_gen3d_error_message",
            "stage1_status",
            "stage1_reason",
            "stage1_error_message",
            "stage2_status",
            "stage2_result",
            "edit_result",
            "response_text",
        ]:
            value = response_context.get(key)
            if value is None:
                continue
            text = str(value).strip().replace("\n", "\\n")
            if len(text) > 180:
                text = text[:180] + "...<truncated>"
            preview_parts.append(f"{key}={text}")
        if not preview_parts:
            return None
        return " | ".join(preview_parts)

    @staticmethod
    def _stage1_execution_error_message(
        response_context: Optional[Dict[str, Any]],
    ) -> str:
        if not isinstance(response_context, dict):
            return ""
        if response_context.get("stage1_status") != EDIT_STATUS_ERROR_QUALITY_CHECK:
            return ""
        for key in ["stage1_error_message", "stage1_reason"]:
            value = response_context.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raw_response = response_context.get("stage1_raw_response")
        if isinstance(raw_response, dict):
            for key in ["error_message", "reason"]:
                value = raw_response.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    @staticmethod
    def _stage_error_class(
        *,
        error_type: str,
        error_message: str,
        response_context: Optional[Dict[str, Any]],
    ) -> str:
        status_code = None
        if isinstance(response_context, dict):
            raw_status_code = response_context.get("status_code")
            if isinstance(raw_status_code, int):
                status_code = raw_status_code
        stage1_status = None
        if isinstance(response_context, dict):
            raw_stage1_status = response_context.get("stage1_status")
            if isinstance(raw_stage1_status, str):
                stage1_status = raw_stage1_status.strip()
        effective_message = error_message or ""
        if stage1_status == EDIT_STATUS_ERROR_QUALITY_CHECK:
            stage1_message = ExperimentRunner._stage1_execution_error_message(
                response_context
            )
            if stage1_message:
                effective_message = stage1_message
        message = effective_message.lower()
        if status_code is not None:
            if status_code >= 500:
                return "api_server_error"
            if status_code >= 400:
                return "api_http_error"
        if stage1_status == EDIT_STATUS_ERROR_QUALITY_CHECK:
            if re.search(r"server error 5\d\d", message):
                return "api_server_error"
            if "timeout" in message:
                return "api_timeout"
            if "request failed" in message:
                return "api_request_exception"
            if "not valid json" in message:
                return "response_not_json"
            return "quality_check_execution_error"
        if stage1_status == EDIT_STATUS_FAILED_QUALITY:
            return "quality_check_failed"
        if "not valid json" in message:
            return "response_not_json"
        if "must contain exactly" in message or "missing required field" in message:
            return "response_schema_invalid"
        if "forbidden lateral terms" in message:
            return "response_rule_forbidden_lateral"
        if "ambiguous symmetric-part edit" in message:
            return "response_rule_ambiguous_symmetric_part"
        if "texture/color/material edit" in message:
            return "response_rule_texture_material_edit"
        if "whole-object/main-body edit" in message:
            return "response_rule_main_body_edit"
        if "appearance-only/surface-only edit" in message:
            return "response_rule_surface_edit"
        if "material-swap edit" in message:
            return "response_rule_material_swap"
        if "refusal response" in message:
            return "response_rule_refusal"
        if "duplicate instruction" in message:
            return "response_rule_duplicate_instruction"
        if "repeats an avoided instruction" in message:
            return "response_rule_avoid_list_repeat"
        if "timeout" in message:
            return "api_timeout"
        if "request failed" in message:
            return "api_request_exception"
        if error_type == "StageValidationFailed":
            return "quality_check_failed"
        return "unknown"

    def _emit_stage_attempt_log(
        self,
        *,
        phase: str,
        stage_name: str,
        scope: str,
        attempt_index: int,
        api_lane: Optional[str],
        context: Optional[Dict[str, Any]],
        error_type: str,
        error_message: str,
        response_context: Optional[Dict[str, Any]],
        max_attempts: Optional[int] = None,
        next_attempt: Optional[int] = None,
        backoff_seconds: Optional[float] = None,
    ) -> None:
        parts = [f"[Stage][{phase}]", f"scope={scope}", f"stage={stage_name}"]
        parts.append(f"attempt={attempt_index}")
        if max_attempts is not None:
            parts.append(f"max_attempts={max_attempts}")
        if api_lane:
            parts.append(f"lane={api_lane}")
        if context:
            for key in [
                "plan_index",
                "object_index",
                "category",
                "object_name",
                "source_model_id",
                "instruction_index",
                "instruction_type",
                "edit_id",
                "edit_scope_id",
                "target_model_id",
            ]:
                value = context.get(key)
                if value is not None:
                    parts.append(f"{key}={value}")
        parts.append(
            "error_class="
            + self._stage_error_class(
                error_type=error_type,
                error_message=error_message,
                response_context=response_context,
            )
        )
        parts.append(f"error_type={error_type}")
        normalized_message = str(error_message).strip().replace("\n", "\\n")
        if len(normalized_message) > 240:
            normalized_message = normalized_message[:240] + "...<truncated>"
        parts.append(f"message={normalized_message}")
        response_preview = self._response_context_preview(response_context)
        if response_preview:
            parts.append(f"response_context={response_preview}")
        if next_attempt is not None:
            parts.append(f"next_attempt={next_attempt}")
        if backoff_seconds is not None:
            parts.append(f"backoff={backoff_seconds:.2f}s")
        print(" ".join(parts), flush=True)

    def _emit_timing_log(
        self,
        *,
        phase: str,
        stage_name: str,
        scope: str,
        status: Optional[str] = None,
        elapsed_seconds: Optional[float] = None,
        attempt_index: Optional[int] = None,
        api_lane: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        stage_name = self._public_timing_stage_name(stage_name)
        parts = [f"[Timing][{phase}]", f"scope={scope}", f"stage={stage_name}"]
        if status is not None:
            parts.append(f"status={status}")
        if elapsed_seconds is not None:
            parts.append(f"elapsed={elapsed_seconds:.3f}s")
        if attempt_index is not None:
            parts.append(f"attempt={attempt_index}")
        if api_lane:
            parts.append(f"lane={api_lane}")
        if context:
            for key in [
                "plan_index",
                "object_index",
                "category",
                "object_name",
                "source_model_id",
                "instruction_index",
                "instruction_type",
                "edit_id",
                "edit_scope_id",
                "target_model_id",
            ]:
                value = context.get(key)
                if value is not None:
                    parts.append(f"{key}={value}")
        print(" ".join(parts))

    def _record_timing_event(
        self,
        *,
        event_type: str,
        stage_name: str,
        scope: str,
        status: Optional[str] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        elapsed_seconds: Optional[float] = None,
        attempt_index: Optional[int] = None,
        api_lane: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        stage_name = self._public_timing_stage_name(stage_name)
        payload: Dict[str, Any] = {
            "scope": scope,
            "stage_name": stage_name,
        }
        if status is not None:
            payload["status"] = status
        if started_at is not None:
            payload["started_at"] = started_at
        if finished_at is not None:
            payload["finished_at"] = finished_at
        if elapsed_seconds is not None:
            payload["elapsed_seconds"] = round(float(elapsed_seconds), 6)
        if attempt_index is not None:
            payload["attempt_index"] = attempt_index
        if api_lane is not None:
            payload["api_lane"] = api_lane
        if context:
            payload.update(context)
        self._log_event(event_type, **payload)

    def _build_timing_entry(
        self,
        *,
        stage_name: str,
        scope: str,
        status: str,
        started_at: str,
        finished_at: str,
        elapsed_seconds: float,
        attempt_index: Optional[int],
        api_lane: Optional[str],
    ) -> Dict[str, Any]:
        stage_name = self._public_timing_stage_name(stage_name)
        return {
            "scope": scope,
            "stage_name": stage_name,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": round(float(elapsed_seconds), 6),
            "attempt_index": attempt_index,
            "api_lane": api_lane,
        }

    def _append_stage_timing_attempt(
        self,
        record: Dict[str, Any],
        stage_name: str,
        timing_entry: Dict[str, Any],
    ) -> None:
        self._ensure_timing_fields(record)
        stage_name = self._public_timing_stage_name(stage_name)
        attempts = record["timing_attempts"].setdefault(stage_name, [])
        attempts.append(dict(timing_entry))

    def _set_stage_timing(
        self,
        record: Dict[str, Any],
        stage_name: str,
        timing_entry: Dict[str, Any],
    ) -> None:
        self._ensure_timing_fields(record)
        stage_name = self._public_timing_stage_name(stage_name)
        record["timings"][stage_name] = dict(timing_entry)

    def _store_stage_timing(
        self,
        record: Dict[str, Any],
        *,
        stage_name: str,
        status: str,
        started_at: str,
        finished_at: str,
        elapsed_seconds: float,
        attempt_index: Optional[int],
        api_lane: Optional[str],
        extra_context: Optional[Dict[str, Any]] = None,
        store_attempt: bool = True,
        store_final: bool = True,
    ) -> Dict[str, Any]:
        scope = self._timing_scope_for_stage(stage_name)
        context = self._timing_context_from_record(record, extra_context)
        timing_entry = self._build_timing_entry(
            stage_name=stage_name,
            scope=scope,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
            attempt_index=attempt_index,
            api_lane=api_lane,
        )
        if store_attempt:
            self._append_stage_timing_attempt(record, stage_name, timing_entry)
        if store_final:
            self._set_stage_timing(record, stage_name, timing_entry)
        self._emit_timing_log(
            phase="END",
            stage_name=stage_name,
            scope=scope,
            status=status,
            elapsed_seconds=elapsed_seconds,
            attempt_index=attempt_index,
            api_lane=api_lane,
            context=context,
        )
        self._record_timing_event(
            event_type="stage_timing_end",
            stage_name=stage_name,
            scope=scope,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
            attempt_index=attempt_index,
            api_lane=api_lane,
            context=context,
        )
        return timing_entry

    def _run_timed_non_retry_stage(
        self,
        *,
        record: Dict[str, Any],
        stage_name: str,
        func: Callable[[], Any],
        api_lane: Optional[str] = None,
        extra_context: Optional[Dict[str, Any]] = None,
        success_status: str = "success",
    ) -> Any:
        scope = self._timing_scope_for_stage(stage_name)
        context = self._timing_context_from_record(record, extra_context)
        started_at = datetime.now().isoformat()
        started_perf = time.perf_counter()
        self._emit_timing_log(
            phase="START",
            stage_name=stage_name,
            scope=scope,
            attempt_index=1,
            api_lane=api_lane,
            context=context,
        )
        self._record_timing_event(
            event_type="stage_timing_start",
            stage_name=stage_name,
            scope=scope,
            started_at=started_at,
            attempt_index=1,
            api_lane=api_lane,
            context=context,
        )
        try:
            result = func()
        except Exception:
            finished_at = datetime.now().isoformat()
            elapsed_seconds = time.perf_counter() - started_perf
            self._store_stage_timing(
                record,
                stage_name=stage_name,
                status="failed",
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed_seconds,
                attempt_index=1,
                api_lane=api_lane,
                extra_context=extra_context,
            )
            raise
        finished_at = datetime.now().isoformat()
        elapsed_seconds = time.perf_counter() - started_perf
        self._store_stage_timing(
            record,
            stage_name=stage_name,
            status=success_status,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
            attempt_index=1,
            api_lane=api_lane,
            extra_context=extra_context,
        )
        return result

    def _timing_summary_from_entries(
        self,
        entries: Iterable[Dict[str, Any]],
        *,
        aggregation_basis: str,
    ) -> List[Dict[str, Any]]:
        buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for entry in entries:
            stage_name = entry.get("stage_name")
            scope = entry.get("scope")
            if not isinstance(stage_name, str) or not stage_name:
                continue
            if not isinstance(scope, str) or not scope:
                scope = self._timing_scope_for_stage(stage_name)
            elapsed_seconds = entry.get("elapsed_seconds")
            if not isinstance(elapsed_seconds, (int, float)):
                continue
            buckets.setdefault((scope, stage_name), []).append(entry)

        rows: List[Dict[str, Any]] = []
        for (scope, stage_name), bucket in sorted(buckets.items()):
            values = sorted(float(item["elapsed_seconds"]) for item in bucket)
            sample_count = len(values)
            if sample_count == 0:
                continue
            success_count = sum(1 for item in bucket if item.get("status") == "success")
            skipped_count = sum(1 for item in bucket if item.get("status") == "skipped")
            failed_count = sample_count - success_count - skipped_count
            percentile_index = max(0, math.ceil(sample_count * 0.9) - 1)
            total_seconds = sum(values)
            rows.append(
                {
                    "stage_name": stage_name,
                    "scope": scope,
                    "aggregation_basis": aggregation_basis,
                    "sample_count": sample_count,
                    "success_count": success_count,
                    "skipped_count": skipped_count,
                    "failed_count": failed_count,
                    "total_seconds": round(total_seconds, 6),
                    "mean_seconds": round(total_seconds / sample_count, 6),
                    "median_seconds": round(statistics.median(values), 6),
                    "p90_seconds": round(values[percentile_index], 6),
                    "min_seconds": round(values[0], 6),
                    "max_seconds": round(values[-1], 6),
                }
            )
        return rows

    def _collect_attempt_timing_entries(
        self,
        object_records: List[Dict[str, Any]],
        edit_records: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for record in [*object_records, *edit_records]:
            if not isinstance(record, dict):
                continue
            timing_attempts = record.get("timing_attempts", {})
            if not isinstance(timing_attempts, dict):
                continue
            for stage_attempts in timing_attempts.values():
                if not isinstance(stage_attempts, list):
                    continue
                for attempt in stage_attempts:
                    if isinstance(attempt, dict):
                        entries.append(attempt)
        return entries

    def _collect_final_timing_entries(
        self,
        object_records: List[Dict[str, Any]],
        edit_records: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for record in [*object_records, *edit_records]:
            if not isinstance(record, dict):
                continue
            timings = record.get("timings", {})
            if not isinstance(timings, dict):
                continue
            for timing in timings.values():
                if isinstance(timing, dict):
                    entries.append(timing)
        return entries

    def _merge_timing_payload_into_record(
        self,
        record: Dict[str, Any],
        *,
        timings: Optional[Dict[str, Any]],
        timing_attempts: Optional[Dict[str, Any]],
    ) -> None:
        self._ensure_timing_fields(record)
        if isinstance(timings, dict):
            for stage_name, timing_entry in timings.items():
                if isinstance(timing_entry, dict):
                    self._set_stage_timing(record, stage_name, timing_entry)
        if isinstance(timing_attempts, dict):
            for stage_name, entries in timing_attempts.items():
                if not isinstance(entries, list):
                    continue
                existing = record["timing_attempts"].setdefault(
                    self._public_timing_stage_name(stage_name),
                    [],
                )
                existing.clear()
                existing.extend(
                    dict(entry) for entry in entries if isinstance(entry, dict)
                )

    def _ensure_retry_fields(self, record: Dict[str, Any]) -> None:
        record.setdefault("retry_meta", {})
        record.setdefault("attempt_errors", [])
        record.setdefault("attempt_count", 0)
        record.setdefault("last_error_type", None)
        record.setdefault("last_error_message", None)
        record.setdefault("failed_stage", None)
        record.setdefault("api_lane", None)
        self._ensure_timing_fields(record)

    def _store_stage_retry_meta(
        self,
        record: Dict[str, Any],
        *,
        stage_name: str,
        max_attempts: int,
        actual_attempts: int,
        attempt_errors: List[Dict[str, Any]],
        success: bool,
        api_lane: Optional[str],
    ) -> None:
        self._ensure_retry_fields(record)
        existing_meta = record["retry_meta"].get(stage_name)
        merged_attempt_errors = list(attempt_errors)
        merged_attempt_count = actual_attempts
        merged_success = success
        if existing_meta is not None:
            merged_attempt_errors = list(existing_meta["attempt_errors"]) + list(
                attempt_errors
            )
            merged_attempt_count = int(existing_meta["attempt_count"]) + actual_attempts
            merged_success = bool(existing_meta["success"]) or success
        record["retry_meta"][stage_name] = {
            "max_attempts": max_attempts,
            "attempt_count": merged_attempt_count,
            "success": merged_success,
            "api_lane": api_lane,
            "attempt_errors": merged_attempt_errors,
        }
        record["attempt_errors"] = [
            error
            for stage_meta in record["retry_meta"].values()
            for error in stage_meta["attempt_errors"]
        ]
        record["attempt_count"] = sum(
            int(stage_meta["attempt_count"])
            for stage_meta in record["retry_meta"].values()
        )
        if attempt_errors:
            last_error = attempt_errors[-1]
            record["last_error_type"] = last_error["error_type"]
            record["last_error_message"] = last_error["error_message"]
            record["api_lane"] = last_error.get("api_lane") or api_lane
        elif api_lane is not None:
            record["api_lane"] = api_lane
        if not success:
            record["failed_stage"] = stage_name

    def _max_attempts_for_stage(self, stage_name: str) -> int:
        return getattr(self.config.run_full_experiment.retry, stage_name).max_attempts

    def _execute_stage_with_retry(
        self,
        *,
        stage_name: str,
        record: Dict[str, Any],
        runner: Callable[[int], Any],
        success_evaluator: Callable[[Any], Optional[Dict[str, Any]]],
        default_lane: Optional[str],
        hold_slot_across_retries: bool = False,
    ) -> Any:
        max_attempts = self._max_attempts_for_stage(stage_name)
        attempt_errors: List[Dict[str, Any]] = []
        last_result: Any = None
        actual_attempts = 0
        stop_retry = False

        # When hold_slot_across_retries is True the lane slot is acquired once
        # before the first attempt and released only after the final outcome
        # (success or ABORT).  This prevents retry attempts from competing with
        # other in-flight jobs for the same quota slot.
        hold_lane = (
            self.api_lanes.get(default_lane)
            if hold_slot_across_retries and default_lane
            else None
        )
        _hold_stack = contextlib.ExitStack()
        if hold_lane is not None:
            _hold_stack.enter_context(hold_lane.hold())

        for attempt_index in range(1, max_attempts + 1):
            actual_attempts = attempt_index
            stage_api_lane = default_lane
            started_at = datetime.now().isoformat()
            started_perf = time.perf_counter()
            scope = self._timing_scope_for_stage(stage_name)
            context = self._timing_context_from_record(record)
            self._emit_timing_log(
                phase="START",
                stage_name=stage_name,
                scope=scope,
                attempt_index=attempt_index,
                api_lane=stage_api_lane,
                context=context,
            )
            self._record_timing_event(
                event_type="stage_timing_start",
                stage_name=stage_name,
                scope=scope,
                started_at=started_at,
                attempt_index=attempt_index,
                api_lane=stage_api_lane,
                context=context,
            )
            try:
                result = runner(attempt_index)
                last_result = result
                failure = success_evaluator(result)
                if failure is None:
                    stage_api_lane = default_lane or (
                        attempt_errors[-1].get("api_lane") if attempt_errors else None
                    )
                    finished_at = datetime.now().isoformat()
                    elapsed_seconds = time.perf_counter() - started_perf
                    self._store_stage_retry_meta(
                        record,
                        stage_name=stage_name,
                        max_attempts=max_attempts,
                        actual_attempts=actual_attempts,
                        attempt_errors=attempt_errors,
                        success=True,
                        api_lane=stage_api_lane,
                    )
                    result_status = (
                        str(result.get("edit_result"))
                        if isinstance(result, dict)
                        and isinstance(result.get("edit_result"), str)
                        else (
                            str(result)
                            if isinstance(result, str)
                            and result in {"success", "skipped"}
                            else "success"
                        )
                    )
                    self._store_stage_timing(
                        record,
                        stage_name=stage_name,
                        status=result_status,
                        started_at=started_at,
                        finished_at=finished_at,
                        elapsed_seconds=elapsed_seconds,
                        attempt_index=attempt_index,
                        api_lane=stage_api_lane,
                        extra_context=None,
                    )
                    _hold_stack.close()
                    return result
                attempt_errors.append(
                    self._build_attempt_error_from_failure(
                        stage_name=stage_name,
                        attempt_index=attempt_index,
                        error_type=str(failure["error_type"]),
                        error_message=str(failure["error_message"]),
                        api_lane=failure.get("api_lane") or default_lane,
                        response_context=failure.get("response_context"),
                    )
                )
                last_error = attempt_errors[-1]
                stage_api_lane = last_error.get("api_lane") or default_lane
                finished_at = datetime.now().isoformat()
                elapsed_seconds = time.perf_counter() - started_perf
                self._store_stage_timing(
                    record,
                    stage_name=stage_name,
                    status="failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=elapsed_seconds,
                    attempt_index=attempt_index,
                    api_lane=stage_api_lane,
                    extra_context=None,
                )
                self._emit_stage_attempt_log(
                    phase="FAIL",
                    stage_name=self._public_timing_stage_name(stage_name),
                    scope=scope,
                    attempt_index=attempt_index,
                    api_lane=stage_api_lane,
                    context=context,
                    error_type=last_error["error_type"],
                    error_message=last_error["error_message"],
                    response_context=last_error.get("response_context"),
                    max_attempts=max_attempts,
                )
                stop_retry = bool(failure.get("terminal"))
            except Exception as exc:
                attempt_errors.append(
                    self._build_attempt_error_from_exception(
                        stage_name=stage_name,
                        attempt_index=attempt_index,
                        exc=exc,
                    )
                )
                last_error = attempt_errors[-1]
                stage_api_lane = last_error.get("api_lane") or default_lane
                finished_at = datetime.now().isoformat()
                elapsed_seconds = time.perf_counter() - started_perf
                self._store_stage_timing(
                    record,
                    stage_name=stage_name,
                    status="failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=elapsed_seconds,
                    attempt_index=attempt_index,
                    api_lane=stage_api_lane,
                    extra_context=None,
                )
                self._emit_stage_attempt_log(
                    phase="FAIL",
                    stage_name=self._public_timing_stage_name(stage_name),
                    scope=scope,
                    attempt_index=attempt_index,
                    api_lane=stage_api_lane,
                    context=context,
                    error_type=last_error["error_type"],
                    error_message=last_error["error_message"],
                    response_context=last_error.get("response_context"),
                    max_attempts=max_attempts,
                )
                stop_retry = False

            if stop_retry:
                break

            if attempt_index < max_attempts:
                last_error = attempt_errors[-1]
                # Per-request exponential back-off with jitter.
                # Base 30 s gives enough breathing room for rate-limited APIs
                # (e.g. Hunyuan JobNumExceed).
                _RETRY_BASE_DELAY = 30.0  # seconds
                _RETRY_MAX_DELAY = 90.0   # seconds
                _RETRY_JITTER = 10.0      # seconds
                backoff = min(
                    _RETRY_BASE_DELAY * (2 ** (attempt_index - 1)),
                    _RETRY_MAX_DELAY,
                )
                backoff += random.uniform(0, _RETRY_JITTER)
                self._log_event(
                    "stage_retry",
                    stage_name=stage_name,
                    attempt_index=attempt_index,
                    max_attempts=max_attempts,
                    api_lane=last_error.get("api_lane"),
                    error_type=last_error["error_type"],
                    error_message=last_error["error_message"],
                    backoff_seconds=round(backoff, 2),
                    **context,
                )
                self._emit_stage_attempt_log(
                    phase="RETRY",
                    stage_name=self._public_timing_stage_name(stage_name),
                    scope=scope,
                    attempt_index=attempt_index,
                    api_lane=last_error.get("api_lane"),
                    context=context,
                    error_type=last_error["error_type"],
                    error_message=last_error["error_message"],
                    response_context=last_error.get("response_context"),
                    max_attempts=max_attempts,
                    next_attempt=attempt_index + 1,
                    backoff_seconds=backoff,
                )
                time.sleep(backoff)

        stage_api_lane = default_lane or (
            attempt_errors[-1].get("api_lane") if attempt_errors else None
        )
        self._store_stage_retry_meta(
            record,
            stage_name=stage_name,
            max_attempts=max_attempts,
            actual_attempts=actual_attempts,
            attempt_errors=attempt_errors,
            success=False,
            api_lane=stage_api_lane,
        )
        if attempt_errors:
            last_error = attempt_errors[-1]
            self._emit_stage_attempt_log(
                phase="ABORT",
                stage_name=self._public_timing_stage_name(stage_name),
                scope=scope,
                attempt_index=actual_attempts,
                api_lane=last_error.get("api_lane") or stage_api_lane,
                context=context,
                error_type=last_error["error_type"],
                error_message=last_error["error_message"],
                response_context=last_error.get("response_context"),
                max_attempts=max_attempts,
            )
        _hold_stack.close()
        raise StageExecutionExhaustedError(
            stage_name,
            attempt_errors,
            last_result=last_result,
        )

    def _status_failure_payload(
        self,
        *,
        stage_name: str,
        status: str,
        api_lane: Optional[str],
        failure_message: str,
        response_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if status in {"success", "skipped"}:
            return None
        payload = {
            "error_type": "StageStatusError",
            "error_message": failure_message,
            "api_lane": api_lane,
            "response_context": {"status": status},
        }
        if response_context:
            payload["response_context"].update(response_context)
        return payload

    def _rel_path(self, path: Optional[Path]) -> Optional[str]:
        if path is None:
            return None
        try:
            return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:
            pass
        try:
            rel = str(path.relative_to(self.pipeline_dir)).replace("\\", "/")
            return f"pipeline/{rel}"
        except ValueError:
            pass
        return str(path).replace("\\", "/")

    def _json_default(self, value: Any) -> Any:
        if isinstance(value, Path):
            return self._rel_path(value)
        raise TypeError(
            f"Object of type {type(value).__name__} is not JSON serializable"
        )

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                payload,
                f,
                ensure_ascii=False,
                indent=2,
                default=self._json_default,
            )

    def _append_jsonl(self, path: Path, payload: Any, lock: threading.Lock) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        default=self._json_default,
                    )
                    + "\n"
                )

    def _write_jsonl(self, path: Path, records: Iterable[Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        default=self._json_default,
                    )
                    + "\n"
                )

    def _log_event(self, event_type: str, **payload: Any) -> None:
        event = {
            "timestamp": datetime.now().isoformat(),
            "experiment_id": self.experiment_id,
            "event_type": event_type,
            **payload,
        }
        self._append_jsonl(self.events_path, event, self.events_lock)

    def _selection_mode(self, category: CategoryPlan) -> str:
        if category.random.category and category.random.object:
            return "random_category_and_object"
        if (not category.random.category) and category.random.object:
            return "fixed_category_random_object"
        return "fixed_category_fixed_object"

    def _build_execution_plan(self) -> List[Dict[str, Any]]:
        execution_plan: List[Dict[str, Any]] = []
        for plan_index, category in enumerate(self.plan.categories):
            selection_mode = self._selection_mode(category)
            if category.random.category:
                resolved_category_name = self.random_category_assignments[plan_index]
                sampled_objects = random.sample(
                    self.objects_data[resolved_category_name], category.object_count
                )
                execution_plan.append(
                    {
                        "plan_index": plan_index,
                        "selection_mode": selection_mode,
                        "resolved_category_name": resolved_category_name,
                        "requested_category_name": None,
                        "objects": sampled_objects,
                        "instruction_plan": _instruction_plan_payload(
                            category.instruction_plan
                        ),
                        "random": asdict(category.random),
                        "recovered": False,
                    }
                )
                continue

            assert category.category_name is not None
            if category.random.object:
                sampled_objects = random.sample(
                    self.objects_data[category.category_name], category.object_count
                )
            else:
                assert category.objects is not None
                sampled_objects = list(category.objects)
            entry = {
                    "plan_index": plan_index,
                    "selection_mode": selection_mode,
                    "resolved_category_name": category.category_name,
                    "requested_category_name": category.category_name,
                    "objects": sampled_objects,
                    "instruction_plan": _instruction_plan_payload(
                        category.instruction_plan
                    ),
                    "random": asdict(category.random),
                    "recovered": False,
                }
            if category.style_ids is not None:
                entry["style_ids"] = list(category.style_ids)
            execution_plan.append(entry)
        return execution_plan

    def _reconstruct_execution_plan(self) -> List[Dict[str, Any]]:
        prompt_records = self._latest_prompt_records()
        prompts_by_plan_index: Dict[int, List[Dict[str, Any]]] = {}
        for record in prompt_records:
            prompts_by_plan_index.setdefault(int(record["plan_index"]), []).append(
                record
            )

        used_categories = {
            records[0]["category"]
            for records in prompts_by_plan_index.values()
            if records
        }
        remaining_categories = [
            category_name
            for category_name in self.objects_data.keys()
            if category_name not in used_categories
        ]

        execution_plan: List[Dict[str, Any]] = []
        for plan_index, category in enumerate(self.plan.categories):
            selection_mode = self._selection_mode(category)
            prompt_group = sorted(
                prompts_by_plan_index.get(plan_index, []),
                key=lambda item: int(item.get("object_index") or 0),
            )
            if prompt_group:
                execution_plan.append(
                    {
                        "plan_index": plan_index,
                        "selection_mode": selection_mode,
                        "resolved_category_name": prompt_group[0]["category"],
                        "requested_category_name": prompt_group[0].get(
                            "requested_category_name"
                        ),
                        "objects": [record["object_name"] for record in prompt_group],
                        "instruction_plan": _instruction_plan_payload(
                            self._instruction_plan_from_mapping(
                                prompt_group[0],
                                "prompt_record",
                            )
                        ),
                        "random": asdict(category.random),
                        "recovered": True,
                    }
                )
                continue

            if category.random.category:
                if not remaining_categories:
                    raise ValueError(
                        "Cannot reconstruct execution plan: no remaining categories available"
                    )
                resolved_category_name = remaining_categories.pop(0)
                sampled_objects = random.sample(
                    self.objects_data[resolved_category_name], category.object_count
                )
                execution_plan.append(
                    {
                        "plan_index": plan_index,
                        "selection_mode": selection_mode,
                        "resolved_category_name": resolved_category_name,
                        "requested_category_name": None,
                        "objects": sampled_objects,
                        "instruction_plan": _instruction_plan_payload(
                            category.instruction_plan
                        ),
                        "random": asdict(category.random),
                        "recovered": True,
                    }
                )
                continue

            assert category.category_name is not None
            if category.random.object:
                sampled_objects = random.sample(
                    self.objects_data[category.category_name], category.object_count
                )
            else:
                assert category.objects is not None
                sampled_objects = list(category.objects)
            execution_plan.append(
                {
                    "plan_index": plan_index,
                    "selection_mode": selection_mode,
                    "resolved_category_name": category.category_name,
                    "requested_category_name": category.category_name,
                    "objects": sampled_objects,
                    "instruction_plan": _instruction_plan_payload(
                        category.instruction_plan
                    ),
                    "random": asdict(category.random),
                    "recovered": True,
                }
            )
        return execution_plan

    def _load_or_build_execution_plan(self) -> List[Dict[str, Any]]:
        if self.execution_plan_path.exists():
            with open(self.execution_plan_path, "r", encoding="utf-8") as f:
                execution_plan = json.load(f)
            if not isinstance(execution_plan, list):
                raise ValueError(
                    f"execution_plan.json must be a list: {self.execution_plan_path}"
                )
            return execution_plan

        if self.prompt_records:
            execution_plan = self._reconstruct_execution_plan()
        else:
            execution_plan = self._build_execution_plan()
        self._write_json(self.execution_plan_path, execution_plan)
        return execution_plan

    def _resolve_object_jobs(
        self, category: CategoryPlan, plan_index: int
    ) -> List[ObjectJob]:
        entry = self.execution_plan[plan_index]
        style_ids = entry.get("style_ids")
        return [
            ObjectJob(
                plan_index=plan_index,
                selection_mode=str(entry["selection_mode"]),
                category=str(entry["resolved_category_name"]),
                object_name=object_name,
                instruction_plan=self._instruction_plan_from_mapping(
                    entry,
                    f"execution_plan[{plan_index}]",
                ),
                requested_category_name=entry.get("requested_category_name"),
                style_id=style_ids[i] if style_ids else None,
            )
            for i, object_name in enumerate(entry["objects"])
        ]

    def _scheduled_object_jobs(self) -> List[Tuple[int, ObjectJob]]:
        scheduled_jobs: List[Tuple[int, ObjectJob]] = []
        for plan_index, category in enumerate(self.plan.categories):
            for object_index, job in enumerate(
                self._resolve_object_jobs(category, plan_index),
                start=1,
            ):
                scheduled_jobs.append((object_index, job))
        return scheduled_jobs

    def _instruction_plan_from_mapping(
        self,
        data: Dict[str, Any],
        path: str,
    ) -> InstructionPlan:
        return _parse_instruction_plan(data, path)

    def _config_snapshot(self) -> Dict[str, Any]:
        image_task = self.config.tasks["image_generation"]
        text_task = self.config.tasks["text_generation"]
        guided_edit_task = self.config.tasks["guided_edit"]
        diff_task = self.config.tasks["edit_quality_check_diff"]
        judge_task = self.config.tasks["edit_quality_check_judge"]
        tsr_cfg = self.config.edit_quality_check.two_stage_recon
        return {
            "gpu_id": self.gpu_id,
            "category_workers": self.category_workers,
            "object_workers": self.object_workers,
            "category_workers_breakdown": self.category_workers_limits,
            "scheduler_mode": self.scheduler_mode,
            "run_full_experiment": {
                "retry": {
                    "source_prompt_optimization": self.config.run_full_experiment.retry.source_prompt_optimization.max_attempts,
                    "source_t2i": self.config.run_full_experiment.retry.source_t2i.max_attempts,
                    "source_gen3d": self.config.run_full_experiment.retry.source_gen3d.max_attempts,
                    "source_render": self.config.run_full_experiment.retry.source_render.max_attempts,
                    "instruction_generation": self.config.run_full_experiment.retry.instruction_generation.max_attempts,
                    "edit_apply": self.config.run_full_experiment.retry.edit_apply.max_attempts,
                    "stage1_quality_check": self.config.run_full_experiment.retry.stage1_quality_check.max_attempts,
                    "target_gen3d": self.config.run_full_experiment.retry.target_gen3d.max_attempts,
                    "target_render": self.config.run_full_experiment.retry.target_render.max_attempts,
                    "stage2": self.config.run_full_experiment.retry.stage2.max_attempts,
                },
                "api_lane_control": {
                    "enabled": self.config.run_full_experiment.api_lane_control.enabled,
                    "cooldown_seconds": self.config.run_full_experiment.api_lane_control.cooldown_seconds,
                    "recovery_probe_one_by_one": self.config.run_full_experiment.api_lane_control.recovery_probe_one_by_one,
                },
                "scheduling": {
                    "object_workers_strategy": self.config.run_full_experiment.scheduling.object_workers_strategy,
                    "object_workers_cap": self.config.run_full_experiment.scheduling.object_workers_cap,
                    "provider_pressure_divisor": self.config.run_full_experiment.scheduling.provider_pressure_divisor,
                },
            },
            "text_generation": {
                "provider": text_task.provider,
                "model": text_task.model,
            },
            "image_generation": {
                "provider": image_task.provider,
                "model": image_task.model,
            },
            "guided_edit": {
                "provider": guided_edit_task.provider,
                "model": guided_edit_task.model,
            },
            "stage1_diff": {
                "provider": diff_task.provider,
                "model": diff_task.model,
            },
            "stage1_judge": {
                "provider": judge_task.provider,
                "model": judge_task.model,
            },
            "stage2": {
                "metric": tsr_cfg.metric,
                "threshold": tsr_cfg.threshold,
                "views": list(tsr_cfg.recon_views),
                "input_mode": tsr_cfg.input_mode,
                "aggregate": tsr_cfg.aggregate,
            },
        }

    def _write_manifest(
        self,
        *,
        started_at: str,
        finished_at: Optional[str],
        totals: Optional[Dict[str, Any]],
        status: str,
        progress: Dict[str, Any],
    ) -> None:
        manifest = {
            "experiment_id": self.experiment_id,
            "name": self.plan.name,
            "gpu_id": self.gpu_id,
            "plan_path": self._rel_path(self.plan_path),
            "plan": asdict(self.plan),
            "started_at": started_at,
            "finished_at": finished_at,
            "source_provider": self.plan.source_provider,
            "target_provider": self.plan.target_provider,
            "edit_mode": self.plan.edit_mode,
            "category_workers": self.category_workers,
            "object_workers": self.object_workers,
            "category_workers_breakdown": self.category_workers_limits,
            "scheduler_mode": self.scheduler_mode,
            "config_snapshot": self._config_snapshot(),
            "outputs": {
                "execution_plan": self._rel_path(self.execution_plan_path),
                "prompt_records": self._rel_path(self.prompt_records_path),
                "events": self._rel_path(self.events_path),
                "object_records": self._rel_path(self.object_records_path),
                "edit_records": self._rel_path(self.edit_records_path),
                "summary_json": self._rel_path(self.summary_json_path),
                "summary_csv": self._rel_path(self.summary_csv_path),
                "category_stats_json": self._rel_path(self.category_stats_json_path),
                "category_stats_csv": self._rel_path(self.category_stats_csv_path),
                "stage_timing_summary_csv": self._rel_path(
                    self.stage_timing_summary_csv_path
                ),
            },
            "status": status,
            "progress": progress,
            "totals": totals,
        }
        with self.manifest_lock:
            self._write_json(self.manifest_path, manifest)

    def _persist_progress(
        self,
        *,
        object_record: Optional[Dict[str, Any]] = None,
        edit_records: Optional[List[Dict[str, Any]]] = None,
        finished_at: Optional[str] = None,
        status: str = "running",
    ) -> Dict[str, Any]:
        if self.started_at is None:
            raise RuntimeError(
                "started_at must be initialized before persisting progress"
            )

        with self.records_lock:
            if edit_records:
                self._upsert_edit_records_locked(edit_records)
            if object_record is not None:
                self._upsert_object_record_locked(object_record)

            self._write_jsonl(self.object_records_path, self.object_records)
            self._write_jsonl(self.edit_records_path, self.edit_records)
            category_stats = self._summarize_categories(
                self.object_records, self.edit_records
            )
            summary = self._write_summary_outputs(
                self.object_records,
                self.edit_records,
                category_stats,
            )
            totals = self._totals_from_records(self.object_records, self.edit_records)
            progress = self._progress_payload(self.object_records, self.edit_records)
            self._write_manifest(
                started_at=self.started_at,
                finished_at=finished_at,
                totals=totals,
                status=status,
                progress=progress,
            )
            return summary

    def _generate_prompt_record(
        self, job: ObjectJob, object_index: int
    ) -> Dict[str, Any]:
        prompt_id = uuid.uuid4().hex[:12]
        image_id = uuid.uuid4().hex[:12]
        source_model_id = image_id

        prompt_record: Dict[str, Any] = {
            "experiment_id": self.experiment_id,
            "prompt_id": prompt_id,
            "image_id": image_id,
            "source_model_id": source_model_id,
            "plan_index": job.plan_index,
            "object_index": object_index,
            "selection_mode": job.selection_mode,
            "requested_category_name": job.requested_category_name,
            "category": job.category,
            "object_name": job.object_name,
            "object_key": f"{job.category}::{job.object_name}",
        }
        optimized = self._execute_stage_with_retry(
            stage_name="source_prompt_optimization",
            record=prompt_record,
            runner=lambda _attempt_index: self._run_in_lane(
                "oneapi_text",
                "source_prompt_optimization",
                lambda: self._optimize_prompt(job),
            ),
            success_evaluator=lambda _result: None,
            default_lane="oneapi_text",
        )

        prompt_record.update(
            {
                "prompt": optimized["prompt"],
                "style_id": optimized.get("style_id"),
                "style_name_en": optimized.get("style_name_en"),
                "style_name_zh": optimized.get("style_name_zh"),
                "style_prefix": optimized.get("style_prefix"),
                "object_description": optimized.get("object_description"),
                "image_requirements": optimized.get("image_requirements"),
                "instruction_plan": _instruction_plan_payload(job.instruction_plan),
                "created_at": datetime.now().isoformat(),
            }
        )
        return prompt_record

    def _optimize_prompt(self, job: ObjectJob) -> Dict[str, Any]:
        with PromptOptimizer(self.config.get_text_provider_config()) as optimizer:
            return optimizer.optimize_prompt_with_metadata(
                subject=job.object_name,
                category=job.category,
                style_id=job.style_id,
            )

    def _get_existing_prompt_record(
        self,
        plan_index: int,
        object_index: int,
    ) -> Optional[Dict[str, Any]]:
        with self.prompts_lock:
            prompt_record = self.prompt_record_map.get((plan_index, object_index))
            return dict(prompt_record) if prompt_record is not None else None

    def _remember_prompt_record(
        self,
        plan_index: int,
        object_index: int,
        prompt_context: Dict[str, Any],
        job: ObjectJob,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        prompt_record = {
            **prompt_context,
            "plan_index": plan_index,
            "object_index": object_index,
            "selection_mode": job.selection_mode,
            "requested_category_name": job.requested_category_name,
            "category": job.category,
            "object_name": job.object_name,
            "instruction_plan": _instruction_plan_payload(job.instruction_plan),
        }
        if extra_fields:
            prompt_record.update(extra_fields)
        self._append_jsonl(self.prompt_records_path, prompt_record, self.prompts_lock)
        with self.prompts_lock:
            self.prompt_records.append(prompt_record)
            self.prompt_record_map[(plan_index, object_index)] = prompt_record

    def _run_t2i(self, prompt_record: Dict[str, Any]) -> Dict[str, Any]:
        image_id = prompt_record["image_id"]
        image_dir = self.images_dir / image_id
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / "image.png"

        self._run_in_lane(
            "oneapi_image",
            "source_t2i",
            lambda: self._generate_image(prompt_record["prompt"], image_path),
        )

        image_meta = {
            **prompt_record,
            "image_path": self._rel_path(image_path),
            "source_provider": self.plan.source_provider,
            "target_provider": self.plan.target_provider,
        }
        self._write_json(image_dir / "meta.json", image_meta)
        return {
            "image_id": image_id,
            "source_model_id": prompt_record["source_model_id"],
            "image_path": image_path,
        }

    def _generate_image(self, prompt: str, image_path: Path) -> None:
        with T2IGenerator(self.config.get_image_provider_config()) as generator:
            generator.generate_image(prompt, str(image_path))

    def _run_source_gen3d_once(
        self,
        source_model_id: str,
        *,
        force: bool,
    ) -> str:
        lane_name = self._lane_name_for_gen3d(self.plan.source_provider)
        _, gen_status = self._run_in_lane(
            lane_name,
            "source_gen3d",
            lambda: self.batch_processor.generate_3d_single(
                source_model_id,
                self.plan.source_provider,
                force=force,
            ),
        )
        return gen_status

    def _run_render_once(
        self,
        model_id: str,
        provider: str,
        *,
        force: bool,
        operation_name: str,
    ) -> str:
        _, render_status = self._run_in_lane(
            "render",
            operation_name,
            lambda: self.batch_processor.render_single(
                model_id,
                provider,
                force=force,
                quiet_subprocess=True,
            ),
        )
        return render_status

    def _run_source_pipeline(self, job: ObjectJob, object_index: int) -> Dict[str, Any]:
        source_record: Dict[str, Any] = {
            "experiment_id": self.experiment_id,
            "plan_index": job.plan_index,
            "object_index": object_index,
            "selection_mode": job.selection_mode,
            "requested_category_name": job.requested_category_name,
            "category": job.category,
            "object_name": job.object_name,
            "object_key": f"{job.category}::{job.object_name}",
            "source_provider": self.plan.source_provider,
            "target_provider": self.plan.target_provider,
            "edit_mode": self.plan.edit_mode,
            "retry_meta": {},
            "attempt_errors": [],
            "attempt_count": 0,
            "last_error_type": None,
            "last_error_message": None,
            "failed_stage": None,
            "api_lane": None,
        }
        try:

            def _inner() -> Dict[str, Any]:
                prompt_record = self._generate_prompt_record(job, object_index)
                # Persist immediately after prompt optimization so the pipeline
                # can be resumed if T2I / Gen3D / render fails mid-way.
                self._remember_prompt_record(job.plan_index, object_index, prompt_record, job)
                source_record.update(
                    {
                        "prompt_id": prompt_record["prompt_id"],
                        "image_id": prompt_record["image_id"],
                        "source_model_id": prompt_record["source_model_id"],
                    }
                )
                self._merge_timing_payload_into_record(
                    source_record,
                    timings=prompt_record.get("timings"),
                    timing_attempts=prompt_record.get("timing_attempts"),
                )
                source_stage = self._execute_stage_with_retry(
                    stage_name="source_t2i",
                    record=source_record,
                    runner=lambda _attempt_index: self._run_source_t2i_stage(
                        prompt_record
                    ),
                    success_evaluator=lambda _result: None,
                    default_lane="oneapi_image",
                )
                image_info = source_stage["image_info"]
                source_model_id = image_info["source_model_id"]
                source_provider_id = get_model_id(self.plan.source_provider)

                self._execute_stage_with_retry(
                    stage_name="source_gen3d",
                    record=source_record,
                    runner=lambda attempt_index: self._run_source_gen3d_once(
                        source_model_id,
                        force=attempt_index > 1,
                    ),
                    success_evaluator=lambda status: self._status_failure_payload(
                        stage_name="source_gen3d",
                        status=status,
                        api_lane=self._lane_name_for_gen3d(self.plan.source_provider),
                        failure_message=(
                            f"Source 3D generation failed for {source_model_id} "
                            f"with provider {self.plan.source_provider}"
                        ),
                    ),
                    default_lane=self._lane_name_for_gen3d(self.plan.source_provider),
                    hold_slot_across_retries=True,
                )

                self._execute_stage_with_retry(
                    stage_name="source_render",
                    record=source_record,
                    runner=lambda attempt_index: self._run_render_once(
                        source_model_id,
                        self.plan.source_provider,
                        force=attempt_index > 1,
                        operation_name="source_render",
                    ),
                    success_evaluator=lambda status: self._status_failure_payload(
                        stage_name="source_render",
                        status=status,
                        api_lane="render",
                        failure_message=(
                            f"Source render failed for {source_model_id} "
                            f"with provider {self.plan.source_provider}"
                        ),
                    ),
                    default_lane="render",
                )

                return {
                    **prompt_record,
                    **image_info,
                    "source_retry_meta": dict(source_record["retry_meta"]),
                    "source_attempt_errors": list(source_record["attempt_errors"]),
                    "source_attempt_count": source_record["attempt_count"],
                    "source_last_error_type": source_record["last_error_type"],
                    "source_last_error_message": source_record["last_error_message"],
                    "source_failed_stage": source_record["failed_stage"],
                    "source_api_lane": source_record["api_lane"],
                    "source_timings": dict(source_record["timings"]),
                    "source_timing_attempts": dict(source_record["timing_attempts"]),
                    "source_provider_id": source_provider_id,
                    "source_image_path": image_info["image_path"],
                    "source_views_dir": self.triplets_dir
                    / source_model_id
                    / "views"
                    / source_provider_id,
                }

            return self._run_timed_non_retry_stage(
                record=source_record,
                stage_name="source_pipeline_total",
                func=_inner,
            )
        except Exception as exc:
            exc.source_record = dict(source_record)
            raise

    def _run_source_t2i_stage(
        self,
        prompt_record: Dict[str, Any],
    ) -> Dict[str, Any]:
        image_info = self._run_t2i(prompt_record)
        return {
            "image_info": image_info,
        }

    def _resume_source_pipeline_if_needed(
        self,
        prompt_context: Dict[str, Any],
        job: ObjectJob,
        object_index: int,
    ) -> None:
        """Resume any unfinished source pipeline stages from a recovered prompt record.

        Called in the existing_prompt branch of _run_object_job when the prompt
        record was persisted early (after prompt optimization) but the pipeline may
        have been interrupted before T2I / Gen3D / render completed.
        Each stage is checked by file-existence; already-done stages are no-ops.
        """
        source_model_id = str(prompt_context["source_model_id"])
        image_id = str(prompt_context["image_id"])
        image_path = self.images_dir / image_id / "image.png"

        resume_record: Dict[str, Any] = {
            "experiment_id": self.experiment_id,
            "plan_index": job.plan_index,
            "object_index": object_index,
            "selection_mode": job.selection_mode,
            "requested_category_name": job.requested_category_name,
            "category": job.category,
            "object_name": job.object_name,
            "object_key": f"{job.category}::{job.object_name}",
            "source_provider": self.plan.source_provider,
            "retry_meta": {},
            "attempt_errors": [],
            "attempt_count": 0,
            "last_error_type": None,
            "last_error_message": None,
            "failed_stage": None,
            "api_lane": None,
        }

        if not image_path.exists():
            self._log_event(
                "source_resume_t2i",
                source_model_id=source_model_id,
                reason="image missing",
            )
            self._execute_stage_with_retry(
                stage_name="source_t2i",
                record=resume_record,
                runner=lambda _attempt_index: self._run_source_t2i_stage(prompt_context),
                success_evaluator=lambda _result: None,
                default_lane="oneapi_image",
            )

        if not self.batch_processor.has_3d_model(
            source_model_id, self.plan.source_provider
        ):
            self._log_event(
                "source_resume_gen3d",
                source_model_id=source_model_id,
                reason="model missing",
            )
            self._execute_stage_with_retry(
                stage_name="source_gen3d",
                record=resume_record,
                runner=lambda attempt_index: self._run_source_gen3d_once(
                    source_model_id,
                    force=attempt_index > 1,
                ),
                success_evaluator=lambda status: self._status_failure_payload(
                    stage_name="source_gen3d",
                    status=status,
                    api_lane=self._lane_name_for_gen3d(self.plan.source_provider),
                    failure_message=(
                        f"Source 3D generation failed for {source_model_id} "
                        f"with provider {self.plan.source_provider}"
                    ),
                ),
                default_lane=self._lane_name_for_gen3d(self.plan.source_provider),
                hold_slot_across_retries=True,
            )

        if not self.batch_processor.has_rendered_views(
            source_model_id, self.plan.source_provider
        ):
            self._log_event(
                "source_resume_render",
                source_model_id=source_model_id,
                reason="views missing",
            )
            self._execute_stage_with_retry(
                stage_name="source_render",
                record=resume_record,
                runner=lambda attempt_index: self._run_render_once(
                    source_model_id,
                    self.plan.source_provider,
                    force=attempt_index > 1,
                    operation_name="source_render",
                ),
                success_evaluator=lambda status: self._status_failure_payload(
                    stage_name="source_render",
                    status=status,
                    api_lane="render",
                    failure_message=(
                        f"Source render failed for {source_model_id} "
                        f"with provider {self.plan.source_provider}"
                    ),
                ),
                default_lane="render",
            )

    def _generate_instruction_batch_with_retry(
        self,
        *,
        source_context: Dict[str, Any],
        instruction_plan: InstructionPlan,
        avoid_list: List[str],
    ) -> Dict[str, Any]:
        record = {
            "experiment_id": self.experiment_id,
            "plan_index": source_context.get("plan_index"),
            "object_index": source_context.get("object_index"),
            "selection_mode": source_context.get("selection_mode"),
            "requested_category_name": source_context.get("requested_category_name"),
            "category": source_context.get("category"),
            "object_name": source_context.get("object_name"),
            "object_key": source_context.get("object_key"),
            "source_provider": self.plan.source_provider,
            "target_provider": self.plan.target_provider,
            "edit_mode": self.plan.edit_mode,
            "prompt_id": source_context.get("prompt_id"),
            "image_id": source_context.get("image_id"),
            "source_model_id": source_context.get("source_model_id"),
            "retry_meta": {},
            "attempt_errors": [],
            "attempt_count": 0,
            "last_error_type": None,
            "last_error_message": None,
            "failed_stage": None,
            "api_lane": None,
        }
        retry_avoid_list = list(avoid_list)

        def _runner(_attempt_index: int) -> Dict[str, Any]:
            try:
                return self._run_in_lane(
                    "oneapi_text",
                    "instruction_generation",
                    lambda: self._generate_instruction_batch(
                        source_context,
                        instruction_plan,
                        retry_avoid_list,
                    ),
                )
            except Exception as exc:
                candidate_error = exc
                if isinstance(exc, LaneOperationError):
                    candidate_error = exc.original_error
                hint = _extract_instruction_retry_hint(str(candidate_error))
                if hint and hint.lower() not in {
                    item.strip().lower()
                    for item in retry_avoid_list
                    if isinstance(item, str) and item.strip()
                }:
                    retry_avoid_list.append(hint)
                raise

        try:
            result = self._execute_stage_with_retry(
                stage_name="instruction_generation",
                record=record,
                runner=_runner,
                success_evaluator=lambda payload: None,
                default_lane="oneapi_text",
            )
            if isinstance(result, dict):
                payload = dict(result)
                payload["_timings"] = dict(record.get("timings", {}))
                payload["_timing_attempts"] = dict(record.get("timing_attempts", {}))
                payload["_retry_avoid_list"] = list(retry_avoid_list)
                return payload
            return result
        except StageExecutionExhaustedError as exc:
            exc.source_record = dict(record)
            raise

    def _generate_instruction_batch(
        self,
        source_context: Dict[str, Any],
        instruction_plan: InstructionPlan,
        avoid_list: List[str],
    ) -> Dict[str, Any]:
        with InstructionGenerator(self.config.get_text_provider_config()) as generator:
            return generator.generate_adaptive_instructions(
                image_path=str(source_context["source_image_path"]),
                count=instruction_plan.count,
                allowed_types=list(instruction_plan.allowed_types),
                avoid_list=avoid_list,
            )

    def _persist_generated_instruction_batch(
        self,
        *,
        job: ObjectJob,
        object_index: int,
        prompt_context: Dict[str, Any],
        batch_payload: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        persisted_batch_payload = {
            key: value
            for key, value in batch_payload.items()
            if key not in {"_timings", "_timing_attempts"}
        }
        generated_instructions = persisted_batch_payload["instructions"]
        planned_entries = self._generated_instruction_entries(
            generated_instructions,
            job.instruction_plan,
            "instruction_generation.instructions",
        )
        self._remember_prompt_record(
            job.plan_index,
            object_index,
            prompt_context,
            job,
            extra_fields={
                "instruction_batch_generation_mode": "adaptive_k",
                "type_judgment": persisted_batch_payload["type_judgment"],
                "generated_instructions": generated_instructions,
                "instruction_batch_raw": persisted_batch_payload,
            },
        )
        prompt_context.update(
            {
                "instruction_batch_generation_mode": "adaptive_k",
                "type_judgment": persisted_batch_payload["type_judgment"],
                "generated_instructions": generated_instructions,
                "instruction_batch_raw": persisted_batch_payload,
            }
        )
        return planned_entries

    def _find_any_edit_batch(self, model_id: str, instruction: str) -> Optional[str]:
        edited_base = self.triplets_dir / model_id / "edited"
        if not edited_base.exists():
            return None
        normalized = instruction.strip().lower()
        for edit_dir in edited_base.iterdir():
            if not edit_dir.is_dir():
                continue
            meta_path = edit_dir / "meta.json"
            if not meta_path.exists():
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                existing = meta.get("instruction", "")
                if isinstance(existing, str) and existing.strip().lower() == normalized:
                    return edit_dir.name
            except Exception:
                continue
        return None

    def _load_edit_meta(self, model_id: str, edit_id: Optional[str]) -> Dict[str, Any]:
        if not edit_id:
            return {}
        meta_path = self.triplets_dir / model_id / "edited" / edit_id / "meta.json"
        if not meta_path.exists():
            return {}
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_target_meta(self, target_model_id: Optional[str]) -> Dict[str, Any]:
        from utils.fs_retry import retry_io

        if not target_model_id:
            return {}
        meta_path = self.models_dir / target_model_id / "meta.json"
        if not retry_io(lambda: meta_path.exists(), description=f"exists {meta_path}"):
            return {}
        raw = retry_io(
            lambda: meta_path.read_text(encoding="utf-8"),
            description=f"read {meta_path}",
        )
        return json.loads(raw)

    def _extract_stage1_fields(self, edit_meta: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(edit_meta, dict) or not edit_meta:
            return {
                "stage1_status": None,
                "stage1_reason": None,
                "stage1_error_message": None,
                "stage1_method": None,
                "stage1_checked_views": None,
                "stage1_diff_result": None,
                "stage1_judge_result": None,
                "stage1_raw_response": None,
                **_resolve_relabel_lifecycle_payload(None),
            }
        quality_check = edit_meta.get("quality_check")
        if not isinstance(quality_check, dict):
            quality_check = {}
        detail = quality_check.get("stage_edit_correctness")
        if not isinstance(detail, dict):
            detail = {}
        instruction_payload = resolve_instruction_display_from_edit_meta(edit_meta)
        relabel_payload = _resolve_relabel_lifecycle_payload(
            {
                **instruction_payload,
                "relabel_lifecycle_state": edit_meta.get("relabel_lifecycle_state"),
                "relabel_terminal_outcome": edit_meta.get("relabel_terminal_outcome"),
                "relabel_last_edit_id": edit_meta.get("relabel_last_edit_id"),
                "relabel_terminal_edit_id": edit_meta.get("relabel_terminal_edit_id"),
            },
            edit_id=edit_meta.get("edit_id"),
        )
        return {
            "stage1_status": get_effective_edit_status(edit_meta),
            "stage1_reason": detail.get("reason") or quality_check.get("reason"),
            "stage1_error_message": quality_check.get("error_message"),
            "stage1_method": quality_check.get("method"),
            "stage1_checked_views": detail.get("checked_views"),
            "stage1_diff_result": detail.get("diff_result"),
            "stage1_judge_result": detail.get("judge_result"),
            "stage1_raw_response": quality_check or None,
            **instruction_payload,
            **relabel_payload,
        }

    def _get_stage2_meta(self, target_meta: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(target_meta, dict):
            return {}
        target_provider_id = get_model_id(self.plan.target_provider)
        checks_by_provider = target_meta.get("target_quality_checks_by_provider")
        if isinstance(checks_by_provider, dict):
            scoped = checks_by_provider.get(target_provider_id)
            if isinstance(scoped, dict):
                return scoped
        single = target_meta.get("target_quality_check")
        if isinstance(single, dict):
            provider_id = single.get("provider_id")
            if provider_id in (None, target_provider_id):
                return single
        return {}

    def _extract_stage2_fields(self, target_meta: Dict[str, Any]) -> Dict[str, Any]:
        stage2 = self._get_stage2_meta(target_meta)
        return {
            "stage2_status": stage2.get("status"),
            "stage2_score": stage2.get("score"),
            "stage2_threshold": stage2.get("threshold"),
            "stage2_metric": stage2.get("metric"),
            "stage2_views": stage2.get("views"),
            "stage2_input_mode": stage2.get("input_mode"),
            "stage2_aggregate": stage2.get("aggregate"),
            "stage2_scores_by_view": stage2.get("scores_by_view"),
            "stage2_provider": stage2.get("provider"),
            "stage2_provider_id": stage2.get("provider_id"),
            "stage2_reason": stage2.get("reason"),
            "stage2_raw_response": stage2 or None,
        }

    def _write_image_instruction_records(
        self, image_id: str, edit_records: List[Dict[str, Any]]
    ) -> None:
        output_path = self.images_dir / image_id / "instructions.json"
        payload = []
        for record in edit_records:
            instruction_payload = resolve_instruction_display_from_record(record)
            relabel_payload = _resolve_relabel_lifecycle_payload(
                record,
                edit_id=record.get("edit_id"),
            )
            payload.append(
                {
                    "instruction_index": record["instruction_index"],
                    "type": record["instruction_type"],
                    "text": instruction_payload["instruction_display_text"],
                    **instruction_payload,
                    **relabel_payload,
                    "edit_id": record.get("edit_id"),
                    "target_model_id": record.get("target_model_id"),
                    "stage1_status": record.get("stage1_status"),
                    "stage2_status": record.get("stage2_status"),
                    "stage2_score": record.get("stage2_score"),
                    "final_status": record.get("final_status"),
                    "exclusion_reason": record.get("exclusion_reason"),
                }
            )
        self._write_json(output_path, payload)

    def _job_from_prompt_record(self, prompt_record: Dict[str, Any]) -> ObjectJob:
        return ObjectJob(
            plan_index=int(prompt_record["plan_index"]),
            selection_mode=str(prompt_record["selection_mode"]),
            category=str(prompt_record["category"]),
            object_name=str(prompt_record["object_name"]),
            instruction_plan=self._instruction_plan_from_mapping(
                prompt_record,
                "prompt_record",
            ),
            requested_category_name=prompt_record.get("requested_category_name"),
            style_id=prompt_record.get("style_id"),
        )

    def _source_context_from_prompt_record(
        self, prompt_record: Dict[str, Any]
    ) -> Dict[str, Any]:
        source_model_id = str(prompt_record["source_model_id"])
        image_id = str(prompt_record["image_id"])
        source_provider_id = get_model_id(self.plan.source_provider)
        return {
            **prompt_record,
            "source_provider_id": source_provider_id,
            "source_image_path": self.images_dir / image_id / "image.png",
            "source_views_dir": self.triplets_dir
            / source_model_id
            / "views"
            / source_provider_id,
        }

    def _legacy_instruction_entries_from_counts(
        self,
        counts: Any,
        path: str,
    ) -> List[Dict[str, Any]]:
        counts_map = _require_mapping(counts, path)
        entries: List[Dict[str, Any]] = []
        for instruction_type in VALID_INSTRUCTION_TYPES:
            total = _require_non_negative_int(
                counts_map.get(instruction_type, 0),
                f"{path}.{instruction_type}",
            )
            for index in range(1, total + 1):
                entries.append(
                    {
                        "instruction_index": len(entries) + 1,
                        "instruction_type": instruction_type,
                        "type_index": index,
                        "instruction_text": "",
                    }
                )
        if not entries:
            raise ValueError(f"{path} must contain at least one positive count")
        return entries

    def _generated_instruction_entries(
        self,
        generated_instructions: Any,
        instruction_plan: InstructionPlan,
        path: str,
    ) -> List[Dict[str, Any]]:
        payload = _require_list(generated_instructions, path)
        if len(payload) != instruction_plan.count:
            raise ValueError(
                f"{path} must contain exactly {instruction_plan.count} instructions"
            )

        type_indexes: Dict[str, int] = {}
        entries: List[Dict[str, Any]] = []
        for index, item in enumerate(payload, start=1):
            item_map = _require_mapping(item, f"{path}[{index - 1}]")
            instruction_type = _require_non_empty_str(
                item_map.get("type"),
                f"{path}[{index - 1}].type",
            )
            if instruction_type not in instruction_plan.allowed_types:
                raise ValueError(
                    f"{path}[{index - 1}].type must be one of {instruction_plan.allowed_types}"
                )
            instruction_text = _require_non_empty_str(
                item_map.get("instruction"),
                f"{path}[{index - 1}].instruction",
            )
            type_indexes[instruction_type] = type_indexes.get(instruction_type, 0) + 1
            entries.append(
                {
                    "instruction_index": index,
                    "instruction_type": instruction_type,
                    "type_index": type_indexes[instruction_type],
                    "instruction_text": instruction_text,
                }
            )
        return entries

    def _planned_instruction_entries_from_record(
        self,
        record: Dict[str, Any],
        path: str,
    ) -> List[Dict[str, Any]]:
        instruction_plan = self._instruction_plan_from_mapping(record, path)
        generated_instructions = record.get("generated_instructions")
        if generated_instructions is not None:
            return self._generated_instruction_entries(
                generated_instructions,
                instruction_plan,
                f"{path}.generated_instructions",
            )
        legacy_counts = record.get("instruction_counts")
        if legacy_counts is not None:
            return self._legacy_instruction_entries_from_counts(
                legacy_counts,
                f"{path}.instruction_counts",
            )
        return []

    def _generated_instruction_count(
        self,
        record: Dict[str, Any],
        edit_records: List[Dict[str, Any]],
    ) -> int:
        if record.get("generated_instructions") is not None:
            return len(
                self._generated_instruction_entries(
                    record["generated_instructions"],
                    self._instruction_plan_from_mapping(record, "record"),
                    "record.generated_instructions",
                )
            )
        return len(edit_records)

    def _finalize_recovered_record(
        self,
        record: Dict[str, Any],
        edit_meta: Dict[str, Any],
        target_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        if isinstance(edit_meta, dict) and edit_meta:
            record.update(self._extract_stage1_fields(edit_meta))
        record.update(
            _resolve_relabel_lifecycle_payload(
                record,
                edit_id=record.get("edit_id"),
            )
        )
        if record.get("instruction_display_text"):
            record["instruction_text"] = record["instruction_display_text"]
        stage1_status = record.get("stage1_status")
        if stage1_status != EDIT_STATUS_PASSED:
            if stage1_status in {
                EDIT_STATUS_FAILED_QUALITY,
                EDIT_STATUS_ERROR_QUALITY_CHECK,
            }:
                record["final_status"] = "stage1_failed"
                record["exclusion_reason"] = "stage1_failed"
            else:
                record["final_status"] = "in_progress_or_unresolved"
                record["exclusion_reason"] = "recovered_partial"
            return record

        record["is_stage2_eligible"] = True
        record["target_model_id"] = (
            record.get("target_model_id")
            or f"{record['source_model_id']}_edit_{record['edit_id']}"
        )
        target_model_dir = self.models_dir / record["target_model_id"]
        if target_model_dir.exists():
            record["target_gen3d_status"] = "passed"
        target_views_dir = (
            self.triplets_dir
            / record["target_model_id"]
            / "views"
            / get_model_id(self.plan.target_provider)
        )
        if target_views_dir.exists():
            record["target_render_status"] = "passed"
            record["target_render_provider_id"] = get_model_id(
                self.plan.target_provider
            )

        record.update(self._extract_stage2_fields(target_meta))
        record["entered_stage2"] = record.get("stage2_status") is not None

        if record["stage2_status"] == EDIT_STATUS_PASSED:
            record["final_status"] = "stage2_passed"
            record["exclusion_reason"] = None
            return record
        if record["stage2_status"] == EDIT_STATUS_FAILED_QUALITY:
            record["final_status"] = "stage2_failed"
            record["exclusion_reason"] = "stage2_failed"
            return record
        if record["stage2_status"] == EDIT_STATUS_ERROR_QUALITY_CHECK:
            record["final_status"] = "stage2_error"
            record["exclusion_reason"] = "stage2_error"
            return record
        if record.get("target_render_status") == "passed":
            record["final_status"] = "in_progress_or_unresolved"
            record["exclusion_reason"] = "recovered_partial"
            return record
        if record.get("target_gen3d_status") == "passed":
            record["final_status"] = "target_render_failed"
            record["exclusion_reason"] = "target_render_failed"
            return record
        record["final_status"] = "target_gen3d_failed"
        record["exclusion_reason"] = "target_gen3d_failed"
        return record

    def _recover_edit_records_for_prompt(
        self, prompt_record: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        job = self._job_from_prompt_record(prompt_record)
        source_context = self._source_context_from_prompt_record(prompt_record)
        planned_entries = self._planned_instruction_entries_from_record(
            prompt_record,
            "prompt_record",
        )
        recovered: List[Dict[str, Any]] = []
        instructions_path = (
            self.images_dir / source_context["image_id"] / "instructions.json"
        )

        if instructions_path.exists():
            with open(instructions_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, list):
                raise ValueError(
                    f"Expected instructions.json to be a list: {instructions_path}"
                )
            payload = sorted(
                payload,
                key=lambda item: (
                    int(item.get("instruction_index") or 0)
                    if isinstance(item, dict)
                    else 0
                ),
            )
            for index, item in enumerate(payload, start=1):
                if index > len(planned_entries):
                    break
                planned_entry = planned_entries[index - 1]
                item_map = item if isinstance(item, dict) else {"text": str(item)}
                instruction_payload = resolve_instruction_display_from_instruction_item(
                    item
                )
                record = self._base_edit_record(
                    job,
                    source_context,
                    int(item_map.get("instruction_index") or index),
                    str(item_map.get("type") or planned_entry["instruction_type"]),
                    int(planned_entry["type_index"]),
                    str(
                        instruction_payload.get("instruction_text_original")
                        or item_map.get("text")
                        or planned_entry["instruction_text"]
                        or ""
                    ),
                )
                record.update(instruction_payload)
                record.update(
                    _resolve_relabel_lifecycle_payload(
                        item_map,
                        edit_id=item_map.get("edit_id"),
                    )
                )
                record["instruction_text"] = instruction_payload[
                    "instruction_display_text"
                ]
                record["edit_id"] = item_map.get("edit_id")
                record["target_model_id"] = item_map.get("target_model_id")
                record["stage1_status"] = item_map.get("stage1_status")
                record["stage2_status"] = item_map.get("stage2_status")
                record["stage2_score"] = item_map.get("stage2_score")
                record["final_status"] = item_map.get("final_status")
                record["exclusion_reason"] = item_map.get("exclusion_reason")
                edit_meta = self._load_edit_meta(
                    source_context["source_model_id"], record.get("edit_id")
                )
                target_meta = self._load_target_meta(record.get("target_model_id"))
                record = self._finalize_recovered_record(record, edit_meta, target_meta)
                recovered.append(record)
            return recovered

        edited_base = self.triplets_dir / source_context["source_model_id"] / "edited"
        if not edited_base.exists():
            return []

        edit_dirs = []
        for edit_dir in edited_base.iterdir():
            if not edit_dir.is_dir():
                continue
            meta_path = edit_dir / "meta.json"
            if not meta_path.exists():
                continue
            with open(meta_path, "r", encoding="utf-8") as f:
                edit_meta = json.load(f)
            created_at = edit_meta.get("created_at") or ""
            edit_dirs.append((created_at, edit_dir.name, edit_meta))

        edit_dirs.sort(key=lambda item: (item[0], item[1]))
        for index, (_, edit_id, edit_meta) in enumerate(edit_dirs, start=1):
            if index > len(planned_entries):
                break
            planned_entry = planned_entries[index - 1]
            instruction_payload = resolve_instruction_display_from_edit_meta(edit_meta)
            record = self._base_edit_record(
                job,
                source_context,
                index,
                str(planned_entry["instruction_type"]),
                int(planned_entry["type_index"]),
                str(
                    instruction_payload.get("instruction_text_original")
                    or edit_meta.get("instruction")
                    or planned_entry["instruction_text"]
                    or ""
                ),
            )
            record.update(instruction_payload)
            record["instruction_text"] = instruction_payload["instruction_display_text"]
            record["edit_id"] = edit_id
            record["edit_status"] = "recovered"
            record["target_model_id"] = (
                f"{source_context['source_model_id']}_edit_{edit_id}"
            )
            target_meta = self._load_target_meta(record["target_model_id"])
            record = self._finalize_recovered_record(record, edit_meta, target_meta)
            recovered.append(record)
        return recovered

    def recover_partial_outputs(self, *, write_files: bool) -> Dict[str, Any]:
        if self.started_at is None:
            self.started_at = datetime.now().isoformat()
        self.prompt_records = self._read_jsonl(self.prompt_records_path)
        self.prompt_record_map = {
            (
                int(record["plan_index"]),
                int(record["object_index"]),
            ): record
            for record in self._latest_prompt_records()
        }
        recovered_object_records: List[Dict[str, Any]] = []
        recovered_edit_records: List[Dict[str, Any]] = []

        for fallback_index, prompt_record in enumerate(
            self._latest_prompt_records(),
            start=1,
        ):
            job = self._job_from_prompt_record(prompt_record)
            source_context = self._source_context_from_prompt_record(prompt_record)
            object_index = int(prompt_record.get("object_index") or fallback_index)
            object_edit_records = self._recover_edit_records_for_prompt(prompt_record)
            source_views_exist = source_context["source_views_dir"].exists()

            if source_views_exist:
                object_record = self._build_object_record(
                    job,
                    object_index,
                    source_context,
                    object_edit_records,
                )
            else:
                object_record = {
                    "experiment_id": self.experiment_id,
                    "plan_path": self._rel_path(self.plan_path),
                    "plan_index": job.plan_index,
                    "object_index": object_index,
                    "selection_mode": job.selection_mode,
                    "requested_category_name": job.requested_category_name,
                    "category": job.category,
                    "object_name": job.object_name,
                    "object_key": f"{job.category}::{job.object_name}",
                    "source_provider": self.plan.source_provider,
                    "target_provider": self.plan.target_provider,
                    "edit_mode": self.plan.edit_mode,
                    "prompt_id": source_context["prompt_id"],
                    "image_id": source_context["image_id"],
                    "source_model_id": source_context["source_model_id"],
                    "instruction_plan": _instruction_plan_payload(job.instruction_plan),
                    "instruction_count_planned": job.instruction_plan.count,
                    "attempts_total": len(object_edit_records),
                    "generated_instruction_count": self._generated_instruction_count(
                        source_context,
                        object_edit_records,
                    ),
                    "edit_ids": [
                        record["edit_id"]
                        for record in object_edit_records
                        if record.get("edit_id")
                    ],
                    "target_model_ids": [
                        record["target_model_id"]
                        for record in object_edit_records
                        if record.get("target_model_id")
                    ],
                    "stage1_failed_count": sum(
                        1
                        for record in object_edit_records
                        if record.get("stage1_status")
                        in {EDIT_STATUS_FAILED_QUALITY, EDIT_STATUS_ERROR_QUALITY_CHECK}
                    ),
                    "stage2_entered_count": sum(
                        1
                        for record in object_edit_records
                        if record.get("entered_stage2")
                    ),
                    "stage2_passed_count": sum(
                        1
                        for record in object_edit_records
                        if record.get("stage2_status") == EDIT_STATUS_PASSED
                    ),
                    "stage2_failed_count": sum(
                        1
                        for record in object_edit_records
                        if record.get("stage2_status") == EDIT_STATUS_FAILED_QUALITY
                    ),
                    "stage2_error_count": sum(
                        1
                        for record in object_edit_records
                        if record.get("stage2_status")
                        == EDIT_STATUS_ERROR_QUALITY_CHECK
                    ),
                    "stage2_lpips_mean": None,
                    "stage2_lpips_std": None,
                    "final_status_counts": {},
                    "source_pipeline_status": "incomplete_or_failed",
                    "retry_meta": {},
                    "attempt_errors": [],
                    "attempt_count": 0,
                    "last_error_type": None,
                    "last_error_message": None,
                    "failed_stage": None,
                    "api_lane": None,
                    "created_at": datetime.now().isoformat(),
                }

            recovered_object_records.append(object_record)
            recovered_edit_records.extend(object_edit_records)

        with self.records_lock:
            self.object_records = recovered_object_records
            self.edit_records = recovered_edit_records
            self._rebuild_record_indexes()

        category_stats = self._summarize_categories(
            self.object_records, self.edit_records
        )
        summary = {
            "experiment_id": self.experiment_id,
            "name": self.plan.name,
            "source_provider": self.plan.source_provider,
            "target_provider": self.plan.target_provider,
            "edit_mode": self.plan.edit_mode,
            "category_workers": self.category_workers,
            "object_workers": self.object_workers,
            "category_workers_breakdown": self.category_workers_limits,
            "scheduler_mode": self.scheduler_mode,
            "planned_category_count": len(self.plan.categories),
            **self._totals_from_records(self.object_records, self.edit_records),
            "category_stats": category_stats,
        }
        if write_files:
            self._write_jsonl(self.object_records_path, self.object_records)
            self._write_jsonl(self.edit_records_path, self.edit_records)
            self._write_summary_outputs(
                self.object_records,
                self.edit_records,
                category_stats,
            )
            self._write_manifest(
                started_at=self.started_at,
                finished_at=None,
                totals=self._totals_from_records(
                    self.object_records, self.edit_records
                ),
                status="interrupted",
                progress=self._progress_payload(self.object_records, self.edit_records),
            )
        return summary

    def resume_experiment(self) -> Dict[str, Any]:
        if self.started_at is None:
            self.started_at = datetime.now().isoformat()
        self.recover_partial_outputs(write_files=True)
        self._log_event(
            "experiment_resume_start",
            plan_path=self._rel_path(self.plan_path),
            category_workers=self.category_workers,
            object_workers=self.object_workers,
            category_workers_breakdown=self.category_workers_limits,
            scheduler_mode=self.scheduler_mode,
        )
        scheduled_jobs = self._scheduled_object_jobs()
        self._log_event(
            "scheduler_resume_start",
            scheduler_mode=self.scheduler_mode,
            object_workers=self.object_workers,
            scheduled_object_count=len(scheduled_jobs),
        )

        with ThreadPoolExecutor(max_workers=self.object_workers) as executor:
            futures = {
                executor.submit(self._run_object_job, job, object_index): (
                    job.plan_index,
                    object_index,
                )
                for object_index, job in scheduled_jobs
            }
            for future in as_completed(futures):
                future.result()

        summary = self._persist_progress(
            status="completed",
            finished_at=datetime.now().isoformat(),
        )
        self._log_event(
            "experiment_resume_complete",
            object_count=summary["object_count"],
            edit_attempts_total=summary["edit_attempts_total"],
            stage2_passed_count=summary["stage2_passed_count"],
        )
        return {
            "status": "completed",
            "object_count": summary["object_count"],
            "edit_attempts_total": summary["edit_attempts_total"],
        }

    def _base_edit_record(
        self,
        job: ObjectJob,
        source_context: Dict[str, Any],
        instruction_index: int,
        instruction_type: str,
        type_index: int,
        instruction_text: str,
    ) -> Dict[str, Any]:
        snapshot = self._config_snapshot()
        source_provider_id = get_model_id(self.plan.source_provider)
        target_provider_id = get_model_id(self.plan.target_provider)
        instruction_payload = build_instruction_display_payload(
            instruction_text_original=instruction_text,
            instruction_text_effective=instruction_text,
            instruction_display_status="pending",
        )
        relabel_payload = _resolve_relabel_lifecycle_payload(instruction_payload)
        return {
            "experiment_id": self.experiment_id,
            "plan_path": self._rel_path(self.plan_path),
            "plan_index": job.plan_index,
            "selection_mode": job.selection_mode,
            "requested_category_name": job.requested_category_name,
            "category": job.category,
            "object_name": job.object_name,
            "object_key": f"{job.category}::{job.object_name}",
            "source_provider": self.plan.source_provider,
            "source_provider_id": source_provider_id,
            "target_provider": self.plan.target_provider,
            "target_provider_id": target_provider_id,
            "edit_mode": self.plan.edit_mode,
            "prompt_id": source_context["prompt_id"],
            "image_id": source_context["image_id"],
            "source_model_id": source_context["source_model_id"],
            "instruction_index": instruction_index,
            "instruction_type": instruction_type,
            "instruction_type_index": type_index,
            "instruction_text": instruction_payload["instruction_display_text"],
            **instruction_payload,
            **relabel_payload,
            "instruction_plan": _instruction_plan_payload(job.instruction_plan),
            "instruction_count_planned": job.instruction_plan.count,
            "image_generation_model": snapshot["image_generation"]["model"],
            "edit_model": snapshot["guided_edit"]["model"],
            "checker_model": snapshot["stage1_diff"]["model"],
            "stage1_judge_model": snapshot["stage1_judge"]["model"],
            "stage2_metric_config": snapshot["stage2"],
            "edit_id": None,
            "edit_scope_id": None,
            "target_model_id": None,
            "target_render_provider_id": None,
            "source_gen3d_status": "passed",
            "source_render_status": "passed",
            "edit_status": None,
            "target_gen3d_status": None,
            "target_render_status": None,
            "target_gen3d_error_class": None,
            "target_gen3d_error_message": None,
            "stage1_status": None,
            "stage1_reason": None,
            "stage1_method": None,
            "stage1_checked_views": None,
            "stage1_diff_result": None,
            "stage1_judge_result": None,
            "stage1_raw_response": None,
            "stage2_status": None,
            "stage2_score": None,
            "stage2_threshold": None,
            "stage2_metric": None,
            "stage2_views": None,
            "stage2_input_mode": None,
            "stage2_aggregate": None,
            "stage2_scores_by_view": None,
            "stage2_provider": None,
            "stage2_provider_id": None,
            "stage2_reason": None,
            "stage2_raw_response": None,
            "entered_stage2": False,
            "is_stage2_eligible": False,
            "exclusion_reason": None,
            "final_status": None,
            "retry_meta": {},
            "attempt_errors": [],
            "attempt_count": 0,
            "last_error_type": None,
            "last_error_message": None,
            "failed_stage": None,
            "api_lane": None,
            "timings": {},
            "timing_attempts": {},
            "created_at": datetime.now().isoformat(),
        }

    def _run_edit_apply_once(
        self,
        source_context: Dict[str, Any],
        instruction_text: str,
        *,
        force: bool,
        allow_stage1_relabel: bool,
    ) -> Dict[str, Any]:
        # Phase 1: pass lane references into apply_edit_single so each
        # individual API call acquires/releases the slot independently,
        # instead of holding one slot for the entire edit_apply duration.
        image_lane = self.api_lanes.get("oneapi_image")
        text_lane = self.api_lanes.get("oneapi_text")
        apply_payload = self.batch_processor.apply_edit_single(
            source_context["source_model_id"],
            instruction_text,
            mode=self.plan.edit_mode,
            force=force,
            provider_id=source_context["source_provider_id"],
            image_lane=image_lane,
            text_lane=text_lane,
        )
        edit_result = apply_payload["edit_result"]
        edit_id = apply_payload.get("edit_id")
        return {
            "edit_result": edit_result,
            "edit_id": edit_id,
            "edit_meta": apply_payload.get("edit_meta", {}),
            "edit_timings": {},
            "edit_timing_attempts": {},
        }

    def _edit_apply_failure_payload(
        self,
        source_context: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        edit_result = payload["edit_result"]
        edit_id = payload["edit_id"]
        if edit_result == "skipped" and edit_id:
            return None
        if edit_result == "failed":
            return {
                "error_type": "StageStatusError",
                "error_message": (
                    f"Edit apply failed for {source_context['source_model_id']}"
                ),
                "api_lane": "oneapi_image",
                "response_context": {
                    "edit_result": edit_result,
                    "edit_id": edit_id,
                },
            }
        if not edit_id:
            return {
                "error_type": "StageArtifactMissing",
                "error_message": "edit_id missing after edit apply",
                "api_lane": "oneapi_image",
                "response_context": {
                    "edit_result": edit_result,
                },
            }
        return None

    def _stage1_failure_payload(
        self, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        stage1_fields = self._extract_stage1_fields(payload["edit_meta"])
        payload["stage1_fields"] = stage1_fields
        stage1_status = stage1_fields["stage1_status"]
        if stage1_status == EDIT_STATUS_PASSED:
            return None
        if stage1_status == EDIT_STATUS_ERROR_QUALITY_CHECK:
            qc_error_message = stage1_fields.get("stage1_error_message")
            return {
                "error_type": "StageQualityCheckExecutionError",
                "error_message": (
                    str(qc_error_message).strip()
                    if isinstance(qc_error_message, str) and qc_error_message.strip()
                    else "stage1 quality check execution error"
                ),
                "api_lane": "oneapi_text",
                "terminal": False,
                "response_context": {
                    "edit_id": payload["edit_id"],
                    **stage1_fields,
                },
            }
        return {
            "error_type": "StageValidationFailed",
            "error_message": (
                "stage1 quality check terminally failed after relabel"
                if stage1_fields.get("relabel_terminal")
                else "stage1 quality check did not pass"
            ),
            "api_lane": "oneapi_text",
            "terminal": True,
            "response_context": {
                "edit_id": payload["edit_id"],
                **stage1_fields,
            },
        }

    def _run_target_gen3d_once(
        self,
        source_context: Dict[str, Any],
        edit_id: str,
        *,
        force: bool,
    ) -> Dict[str, Any]:
        lane_name = self._lane_name_for_gen3d(self.plan.target_provider)
        target_gen_payload = self._run_in_lane(
            lane_name,
            "target_gen3d",
            lambda: self.batch_processor.gen3d_from_edit_single(
                source_context["source_model_id"],
                edit_id,
                self.plan.target_provider,
                force=force,
            ),
        )
        if not isinstance(target_gen_payload, dict):
            raise ValueError("target_gen3d runner must return a dict payload")
        return target_gen_payload

    def _run_stage2_once(
        self,
        source_context: Dict[str, Any],
        edit_id: str,
        target_model_id: str,
    ) -> Dict[str, Any]:
        _, stage2_result = self._run_in_lane(
            "recon_quality_check",
            "stage2",
            lambda: self.batch_processor.check_target_consistency_single(
                source_context["source_model_id"],
                edit_id,
                self.plan.target_provider,
                skip_render=True,
                force_render=False,
                skip_semaphore=True,  # Phase 4: lane already controls concurrency
            ),
        )
        target_meta = self._load_target_meta(target_model_id)
        return {
            "stage2_result": stage2_result,
            "target_meta": target_meta,
        }

    def _stage2_failure_payload(
        self, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        stage2_fields = self._extract_stage2_fields(payload["target_meta"])
        payload["stage2_fields"] = stage2_fields
        if (
            payload["stage2_result"] != "failed"
            and stage2_fields.get("stage2_status") == EDIT_STATUS_PASSED
        ):
            return None
        # API/execution errors (VLM timeout, 5xx, etc.) are retryable;
        # quality failures (VLM judged inconsistent) are terminal.
        is_api_error = (
            stage2_fields.get("stage2_status") == EDIT_STATUS_ERROR_QUALITY_CHECK
        )
        return {
            "error_type": (
                "StageQualityCheckExecutionError" if is_api_error
                else "StageValidationFailed"
            ),
            "error_message": (
                "stage2 consistency check execution error"
                if is_api_error
                else "stage2 consistency check did not pass"
            ),
            "api_lane": "recon_quality_check",
            "terminal": not is_api_error,
            "response_context": {
                "stage2_result": payload["stage2_result"],
                **stage2_fields,
            },
        }

    def _run_edit_pipeline(
        self,
        job: ObjectJob,
        source_context: Dict[str, Any],
        instruction_index: int,
        instruction_type: str,
        type_index: int,
        instruction_text: str,
    ) -> Dict[str, Any]:
        record = self._base_edit_record(
            job,
            source_context,
            instruction_index,
            instruction_type,
            type_index,
            instruction_text,
        )
        allow_stage1_relabel = not bool(record.get("relabel_terminal"))

        def _inner() -> Dict[str, Any]:
            # ----- Phase 2: edit_apply and stage1_quality_check are now
            # independent retry scopes.  A stage1 QC failure no longer
            # re-runs the expensive edit_apply (image generation). -----

            # Step 1: edit_apply (independent retry)
            try:
                edit_payload = self._execute_stage_with_retry(
                    stage_name="edit_apply",
                    record=record,
                    runner=lambda attempt_index: self._run_edit_apply_once(
                        source_context,
                        instruction_text,
                        force=attempt_index > 1,
                        allow_stage1_relabel=allow_stage1_relabel,
                    ),
                    success_evaluator=lambda result: self._edit_apply_failure_payload(
                        source_context,
                        result,
                    ),
                    default_lane="oneapi_image",
                )
            except StageExecutionExhaustedError as exc:
                if isinstance(exc.last_result, dict):
                    record["edit_id"] = exc.last_result.get("edit_id")
                    record["edit_scope_id"] = _build_edit_scope_id(
                        record.get("source_model_id"),
                        record.get("edit_id"),
                    )
                    record["edit_status"] = exc.last_result.get("edit_result")
                record["final_status"] = "edit_apply_failed"
                record["exclusion_reason"] = "edit_apply_failed"
                return record

            # Merge edit_apply timing into record
            edit_meta = edit_payload.get("edit_meta", {})
            if isinstance(edit_meta, dict):
                self._merge_timing_payload_into_record(
                    record,
                    timings=edit_meta.get("timings"),
                    timing_attempts=edit_meta.get("timing_attempts"),
                )

            record["edit_id"] = edit_payload["edit_id"]
            record["edit_scope_id"] = _build_edit_scope_id(
                record.get("source_model_id"),
                record.get("edit_id"),
            )
            record["edit_status"] = edit_payload["edit_result"]

            # Step 2: stage1_quality_check (independent retry — does NOT
            # re-run edit_apply on failure, saving 2-3 min + 7 API calls)
            try:
                stage1_qc_payload = self._execute_stage_with_retry(
                    stage_name="stage1_quality_check",
                    record=record,
                    runner=lambda _attempt_index: self._run_stage1_qc_once(
                        source_context,
                        edit_payload["edit_id"],
                        allow_stage1_relabel=allow_stage1_relabel,
                        record=record,
                    ),
                    success_evaluator=self._stage1_failure_payload,
                    default_lane="oneapi_text",
                    hold_slot_across_retries=True,
                )
            except StageExecutionExhaustedError as exc:
                if isinstance(exc.last_result, dict):
                    if exc.last_result.get("stage1_fields"):
                        record.update(exc.last_result["stage1_fields"])
                        if record.get("instruction_display_text"):
                            record["instruction_text"] = record[
                                "instruction_display_text"
                            ]
                record["final_status"] = "stage1_failed"
                record["exclusion_reason"] = "stage1_failed"
                return record

            record.update(stage1_qc_payload["stage1_fields"])
            if record.get("instruction_display_text"):
                record["instruction_text"] = record["instruction_display_text"]
            record["is_stage2_eligible"] = True
            target_provider_id = get_model_id(self.plan.target_provider)
            record["target_render_provider_id"] = target_provider_id

            try:
                target_gen_payload = self._execute_stage_with_retry(
                    stage_name="target_gen3d",
                    record=record,
                    runner=lambda attempt_index: self._run_target_gen3d_once(
                        source_context,
                        edit_payload["edit_id"],
                        force=attempt_index > 1,
                    ),
                    success_evaluator=lambda payload: self._status_failure_payload(
                        stage_name="target_gen3d",
                        status=payload["target_gen3d_status"],
                        api_lane=self._lane_name_for_gen3d(self.plan.target_provider),
                        failure_message=(
                            f"Target 3D generation failed for edit {edit_payload['edit_id']}"
                        ),
                        response_context={
                            "target_model_id": payload["target_model_id"],
                            "target_gen3d_error_class": payload.get("target_gen3d_error_class"),
                            "target_gen3d_error_message": payload.get("target_gen3d_error_message"),
                        },
                    ),
                    default_lane=self._lane_name_for_gen3d(self.plan.target_provider),
                    hold_slot_across_retries=True,
                )
            except StageExecutionExhaustedError as exc:
                if isinstance(exc.last_result, dict):
                    record["target_gen3d_status"] = exc.last_result.get(
                        "target_gen3d_status"
                    )
                    record["target_model_id"] = exc.last_result.get("target_model_id")
                    record["target_gen3d_error_class"] = exc.last_result.get(
                        "target_gen3d_error_class"
                    )
                    record["target_gen3d_error_message"] = exc.last_result.get(
                        "target_gen3d_error_message"
                    )
                record["final_status"] = "target_gen3d_failed"
                record["exclusion_reason"] = "target_gen3d_failed"
                return record

            record["target_gen3d_status"] = target_gen_payload["target_gen3d_status"]
            record["target_model_id"] = target_gen_payload["target_model_id"]
            record["target_gen3d_error_class"] = target_gen_payload.get(
                "target_gen3d_error_class"
            )
            record["target_gen3d_error_message"] = target_gen_payload.get(
                "target_gen3d_error_message"
            )

            try:
                target_render_status = self._execute_stage_with_retry(
                    stage_name="target_render",
                    record=record,
                    runner=lambda attempt_index: self._run_render_once(
                        record["target_model_id"],
                        self.plan.target_provider,
                        force=attempt_index > 1,
                        operation_name="target_render",
                    ),
                    success_evaluator=lambda status: self._status_failure_payload(
                        stage_name="target_render",
                        status=status,
                        api_lane="render",
                        failure_message=(
                            f"Target render failed for {record['target_model_id']}"
                        ),
                        response_context={"target_model_id": record["target_model_id"]},
                    ),
                    default_lane="render",
                )
            except StageExecutionExhaustedError:
                record["final_status"] = "target_render_failed"
                record["exclusion_reason"] = "target_render_failed"
                return record

            record["target_render_status"] = target_render_status

            try:
                stage2_payload = self._execute_stage_with_retry(
                    stage_name="stage2",
                    record=record,
                    runner=lambda _attempt_index: self._run_stage2_once(
                        source_context,
                        edit_payload["edit_id"],
                        record["target_model_id"],
                    ),
                    success_evaluator=self._stage2_failure_payload,
                    default_lane="recon_quality_check",
                )
            except StageExecutionExhaustedError as exc:
                if isinstance(exc.last_result, dict):
                    target_meta = exc.last_result.get("target_meta", {})
                    record.update(self._extract_stage2_fields(target_meta))
                    record["entered_stage2"] = record["stage2_status"] is not None
                if record["stage2_status"] == EDIT_STATUS_ERROR_QUALITY_CHECK:
                    record["final_status"] = "stage2_error"
                    record["exclusion_reason"] = "stage2_error"
                else:
                    record["final_status"] = "stage2_failed"
                    record["exclusion_reason"] = "stage2_failed"
                return record

            record.update(stage2_payload["stage2_fields"])
            record["entered_stage2"] = record["stage2_status"] is not None
            if record["stage2_status"] == EDIT_STATUS_PASSED:
                record["final_status"] = "stage2_passed"
                record["exclusion_reason"] = None
                return record
            if record["stage2_status"] == EDIT_STATUS_ERROR_QUALITY_CHECK:
                record["final_status"] = "stage2_error"
                record["exclusion_reason"] = "stage2_error"
                return record
            record["final_status"] = "stage2_failed"
            record["exclusion_reason"] = "stage2_failed"
            return record

        return self._run_timed_non_retry_stage(
            record=record,
            stage_name="edit_pipeline_total",
            func=_inner,
        )

    def _run_stage1_qc_once(
        self,
        source_context: Dict[str, Any],
        edit_id: str,
        *,
        allow_stage1_relabel: bool,
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run stage1 quality check only (Phase 2 — decoupled from edit_apply)."""
        stage1_payload = self._run_in_lane(
            "oneapi_text",
            "stage1_quality_check",
            lambda: self.batch_processor.run_stage1_quality_check_single(
                source_context["source_model_id"],
                edit_id,
                allow_stage1_relabel=allow_stage1_relabel,
                skip_semaphore=True,  # Phase 4: lane already controls concurrency
            ),
        )
        stage1_meta = stage1_payload.get("edit_meta", {})
        if isinstance(stage1_meta, dict):
            self._merge_timing_payload_into_record(
                record,
                timings=stage1_meta.get("timings"),
                timing_attempts=stage1_meta.get("timing_attempts"),
            )
        return {
            "edit_id": edit_id,
            "edit_result": stage1_payload.get("edit_result", "success"),
            "edit_meta": stage1_meta,
        }

    def _run_edit_pipeline_safe(
        self,
        job: ObjectJob,
        source_context: Dict[str, Any],
        instruction_index: int,
        instruction_type: str,
        type_index: int,
        instruction_text: str,
    ) -> Dict[str, Any]:
        try:
            return self._run_edit_pipeline(
                job,
                source_context,
                instruction_index,
                instruction_type,
                type_index,
                instruction_text,
            )
        except Exception as exc:
            record = self._base_edit_record(
                job,
                source_context,
                instruction_index,
                instruction_type,
                type_index,
                instruction_text,
            )
            unexpected_error = self._build_attempt_error_from_exception(
                stage_name="edit_pipeline_unexpected",
                attempt_index=1,
                exc=exc,
            )
            self._store_stage_retry_meta(
                record,
                stage_name="edit_pipeline_unexpected",
                max_attempts=1,
                actual_attempts=1,
                attempt_errors=[unexpected_error],
                success=False,
                api_lane=unexpected_error.get("api_lane"),
            )
            record["final_status"] = "error"
            record["exclusion_reason"] = f"exception:{type(exc).__name__}"
            record["error_message"] = str(exc)
            record["error_traceback"] = traceback.format_exc()
            return record

    def _build_failed_object_record(
        self,
        job: ObjectJob,
        object_index: int,
        prompt_context: Optional[Dict[str, Any]],
        error: Exception,
    ) -> Dict[str, Any]:
        source_context = prompt_context or {}
        source_retry_record = getattr(error, "source_record", {})
        prompt_id = source_context.get("prompt_id") or source_retry_record.get("prompt_id")
        image_id = source_context.get("image_id") or source_retry_record.get("image_id")
        source_model_id = (
            source_context.get("source_model_id")
            or source_retry_record.get("source_model_id")
        )
        source_pipeline_passed = bool(source_model_id)
        return {
            "experiment_id": self.experiment_id,
            "plan_path": self._rel_path(self.plan_path),
            "plan_index": job.plan_index,
            "object_index": object_index,
            "selection_mode": job.selection_mode,
            "requested_category_name": job.requested_category_name,
            "category": job.category,
            "object_name": job.object_name,
            "object_key": f"{job.category}::{job.object_name}",
            "source_provider": self.plan.source_provider,
            "target_provider": self.plan.target_provider,
            "edit_mode": self.plan.edit_mode,
            "prompt_id": prompt_id,
            "image_id": image_id,
            "source_model_id": source_model_id,
            "instruction_plan": _instruction_plan_payload(job.instruction_plan),
            "instruction_count_planned": job.instruction_plan.count,
            "attempts_total": 0,
            "generated_instruction_count": self._generated_instruction_count(
                source_context,
                [],
            )
            if source_context
            else 0,
            "edit_ids": [],
            "target_model_ids": [],
            "stage1_failed_count": 0,
            "stage2_entered_count": 0,
            "stage2_passed_count": 0,
            "stage2_failed_count": 0,
            "stage2_error_count": 0,
            "stage2_lpips_mean": None,
            "stage2_lpips_std": None,
            "final_status_counts": {},
            "source_pipeline_status": "passed" if source_pipeline_passed else "failed",
            "retry_meta": source_context.get("source_retry_meta")
            or source_retry_record.get("retry_meta", {}),
            "attempt_errors": source_context.get("source_attempt_errors")
            or source_retry_record.get("attempt_errors", []),
            "attempt_count": source_context.get("source_attempt_count")
            or source_retry_record.get("attempt_count", 0),
            "last_error_type": source_context.get("source_last_error_type")
            or source_retry_record.get("last_error_type")
            or type(error).__name__,
            "last_error_message": source_context.get("source_last_error_message")
            or source_retry_record.get("last_error_message")
            or str(error),
            "failed_stage": source_context.get("source_failed_stage")
            or source_retry_record.get("failed_stage"),
            "api_lane": source_context.get("source_api_lane")
            or source_retry_record.get("api_lane"),
            "timings": dict(source_context.get("source_timings", {}))
            if isinstance(source_context.get("source_timings"), dict)
            else dict(source_retry_record.get("timings", {}))
            if isinstance(source_retry_record.get("timings"), dict)
            else {},
            "timing_attempts": dict(source_context.get("source_timing_attempts", {}))
            if isinstance(source_context.get("source_timing_attempts"), dict)
            else dict(source_retry_record.get("timing_attempts", {}))
            if isinstance(source_retry_record.get("timing_attempts"), dict)
            else {},
            "error_message": str(error),
            "error_traceback": traceback.format_exc(),
            "created_at": datetime.now().isoformat(),
        }

    def _build_object_record(
        self,
        job: ObjectJob,
        object_index: int,
        source_context: Dict[str, Any],
        edit_records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        stage2_scores = [
            float(record["stage2_score"])
            for record in edit_records
            if isinstance(record.get("stage2_score"), (int, float))
        ]
        final_status_counts: Dict[str, int] = {}
        for record in edit_records:
            status = record.get("final_status") or "unknown"
            final_status_counts[status] = final_status_counts.get(status, 0) + 1

        return {
            "experiment_id": self.experiment_id,
            "plan_path": self._rel_path(self.plan_path),
            "plan_index": job.plan_index,
            "object_index": object_index,
            "selection_mode": job.selection_mode,
            "requested_category_name": job.requested_category_name,
            "category": job.category,
            "object_name": job.object_name,
            "object_key": f"{job.category}::{job.object_name}",
            "source_provider": self.plan.source_provider,
            "target_provider": self.plan.target_provider,
            "edit_mode": self.plan.edit_mode,
            "prompt_id": source_context["prompt_id"],
            "image_id": source_context["image_id"],
            "source_model_id": source_context["source_model_id"],
            "source_provider_id": source_context["source_provider_id"],
            "target_provider_id": get_model_id(self.plan.target_provider),
            "source_image_path": self._rel_path(source_context["source_image_path"]),
            "source_views_dir": self._rel_path(source_context["source_views_dir"]),
            "prompt": source_context["prompt"],
            "style_id": source_context.get("style_id"),
            "style_name_en": source_context.get("style_name_en"),
            "instruction_plan": _instruction_plan_payload(job.instruction_plan),
            "instruction_count_planned": job.instruction_plan.count,
            "attempts_total": len(edit_records),
            "generated_instruction_count": self._generated_instruction_count(
                source_context,
                edit_records,
            ),
            "edit_ids": [
                record["edit_id"] for record in edit_records if record.get("edit_id")
            ],
            "target_model_ids": [
                record["target_model_id"]
                for record in edit_records
                if record.get("target_model_id")
            ],
            "stage1_failed_count": sum(
                1
                for record in edit_records
                if record.get("stage1_status")
                in {EDIT_STATUS_FAILED_QUALITY, EDIT_STATUS_ERROR_QUALITY_CHECK}
            ),
            "stage2_entered_count": sum(
                1 for record in edit_records if record.get("entered_stage2")
            ),
            "stage2_passed_count": sum(
                1
                for record in edit_records
                if record.get("stage2_status") == EDIT_STATUS_PASSED
            ),
            "stage2_failed_count": sum(
                1
                for record in edit_records
                if record.get("stage2_status") == EDIT_STATUS_FAILED_QUALITY
            ),
            "stage2_error_count": sum(
                1
                for record in edit_records
                if record.get("stage2_status") == EDIT_STATUS_ERROR_QUALITY_CHECK
            ),
            "stage2_lpips_mean": round(statistics.mean(stage2_scores), 6)
            if stage2_scores
            else None,
            "stage2_lpips_std": round(statistics.pstdev(stage2_scores), 6)
            if stage2_scores
            else None,
            "final_status_counts": final_status_counts,
            "source_pipeline_status": "passed",
            "retry_meta": source_context.get("source_retry_meta", {}),
            "attempt_errors": source_context.get("source_attempt_errors", []),
            "attempt_count": source_context.get("source_attempt_count", 0),
            "last_error_type": source_context.get("source_last_error_type"),
            "last_error_message": source_context.get("source_last_error_message"),
            "failed_stage": source_context.get("source_failed_stage"),
            "api_lane": source_context.get("source_api_lane"),
            "timings": dict(source_context.get("source_timings", {})),
            "timing_attempts": dict(source_context.get("source_timing_attempts", {})),
            "created_at": datetime.now().isoformat(),
        }

    def _flatten_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        flattened: List[Dict[str, Any]] = []
        for row in rows:
            current = {}
            for key, value in row.items():
                if isinstance(value, (dict, list)):
                    current[key] = json.dumps(value, ensure_ascii=False)
                else:
                    current[key] = value
            flattened.append(current)
        return flattened

    def _summarize_categories(
        self,
        object_records: List[Dict[str, Any]],
        edit_records: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        categories = sorted({record["category"] for record in object_records})
        results: List[Dict[str, Any]] = []
        for category in categories:
            category_objects = [
                record for record in object_records if record["category"] == category
            ]
            category_edits = [
                record for record in edit_records if record["category"] == category
            ]
            if not category_objects:
                continue

            stage2_scores = [
                float(record["stage2_score"])
                for record in category_edits
                if isinstance(record.get("stage2_score"), (int, float))
            ]
            remove_edits = [
                record
                for record in category_edits
                if record["instruction_type"] == "remove"
            ]
            replace_edits = [
                record
                for record in category_edits
                if record["instruction_type"] == "replace"
            ]

            def _stage1_failed_rate(records: List[Dict[str, Any]]) -> Optional[float]:
                if not records:
                    return None
                failed = sum(
                    1
                    for record in records
                    if record.get("stage1_status")
                    in {EDIT_STATUS_FAILED_QUALITY, EDIT_STATUS_ERROR_QUALITY_CHECK}
                )
                return round(failed / len(records), 6)

            def _lpips_mean(records: List[Dict[str, Any]]) -> Optional[float]:
                scores = [
                    float(record["stage2_score"])
                    for record in records
                    if isinstance(record.get("stage2_score"), (int, float))
                ]
                return round(statistics.mean(scores), 6) if scores else None

            edit_attempts_total = len(category_edits)
            stage1_failed_count = sum(
                1
                for record in category_edits
                if record.get("stage1_status")
                in {EDIT_STATUS_FAILED_QUALITY, EDIT_STATUS_ERROR_QUALITY_CHECK}
            )
            stage2_entered_count = sum(
                1 for record in category_edits if record.get("entered_stage2")
            )
            stage2_failed_count = sum(
                1
                for record in category_edits
                if record.get("stage2_status") == EDIT_STATUS_FAILED_QUALITY
            )

            results.append(
                {
                    "experiment_id": self.experiment_id,
                    "source_provider": self.plan.source_provider,
                    "target_provider": self.plan.target_provider,
                    "edit_mode": self.plan.edit_mode,
                    "category": category,
                    "object_count": len(category_objects),
                    "distinct_objects": sorted(
                        {record["object_name"] for record in category_objects}
                    ),
                    "sample_count": len(category_objects),
                    "edit_attempts_total": edit_attempts_total,
                    "stage1_failed_count": stage1_failed_count,
                    "stage1_failed_rate": round(
                        stage1_failed_count / edit_attempts_total, 6
                    )
                    if edit_attempts_total
                    else None,
                    "stage2_entered_count": stage2_entered_count,
                    "stage2_entered_rate": round(
                        stage2_entered_count / edit_attempts_total, 6
                    )
                    if edit_attempts_total
                    else None,
                    "stage2_passed_count": sum(
                        1
                        for record in category_edits
                        if record.get("stage2_status") == EDIT_STATUS_PASSED
                    ),
                    "stage2_failed_count": stage2_failed_count,
                    "stage2_failed_rate": round(
                        stage2_failed_count / stage2_entered_count, 6
                    )
                    if stage2_entered_count
                    else None,
                    "stage2_lpips_mean": round(statistics.mean(stage2_scores), 6)
                    if stage2_scores
                    else None,
                    "stage2_lpips_std": round(statistics.pstdev(stage2_scores), 6)
                    if stage2_scores
                    else None,
                    "stage2_lpips_min": round(min(stage2_scores), 6)
                    if stage2_scores
                    else None,
                    "stage2_lpips_max": round(max(stage2_scores), 6)
                    if stage2_scores
                    else None,
                    "remove_attempts_total": len(remove_edits),
                    "replace_attempts_total": len(replace_edits),
                    "remove_stage1_failed_rate": _stage1_failed_rate(remove_edits),
                    "replace_stage1_failed_rate": _stage1_failed_rate(replace_edits),
                    "remove_lpips_mean": _lpips_mean(remove_edits),
                    "replace_lpips_mean": _lpips_mean(replace_edits),
                }
            )
        return results

    def _write_csv(self, path: Path, rows: List[Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["empty"])
            return
        fieldnames: List[str] = []
        seen = set()
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _write_summary_outputs(
        self,
        object_records: List[Dict[str, Any]],
        edit_records: List[Dict[str, Any]],
        category_stats: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        attempt_timing_summary = self._timing_summary_from_entries(
            self._collect_attempt_timing_entries(object_records, edit_records),
            aggregation_basis="attempt",
        )
        final_record_timing_summary = self._timing_summary_from_entries(
            self._collect_final_timing_entries(object_records, edit_records),
            aggregation_basis="final_record",
        )
        summary = {
            "experiment_id": self.experiment_id,
            "name": self.plan.name,
            "source_provider": self.plan.source_provider,
            "target_provider": self.plan.target_provider,
            "edit_mode": self.plan.edit_mode,
            "category_workers": self.category_workers,
            "object_workers": self.object_workers,
            "category_workers_breakdown": self.category_workers_limits,
            "scheduler_mode": self.scheduler_mode,
            "planned_category_count": len(self.plan.categories),
            "object_count": len(object_records),
            "edit_attempts_total": len(edit_records),
            "stage1_failed_count": sum(
                1
                for record in edit_records
                if record.get("stage1_status")
                in {EDIT_STATUS_FAILED_QUALITY, EDIT_STATUS_ERROR_QUALITY_CHECK}
            ),
            "stage2_entered_count": sum(
                1 for record in edit_records if record.get("entered_stage2")
            ),
            "stage2_passed_count": sum(
                1
                for record in edit_records
                if record.get("stage2_status") == EDIT_STATUS_PASSED
            ),
            "stage2_failed_count": sum(
                1
                for record in edit_records
                if record.get("stage2_status") == EDIT_STATUS_FAILED_QUALITY
            ),
            "stage_timing_summary": attempt_timing_summary,
            "attempt_timing_summary": attempt_timing_summary,
            "final_record_timing_summary": final_record_timing_summary,
            "category_stats": category_stats,
        }
        self._write_json(self.summary_json_path, summary)
        self._write_csv(self.summary_csv_path, self._flatten_rows(object_records))
        self._write_json(self.category_stats_json_path, category_stats)
        self._write_csv(
            self.category_stats_csv_path, self._flatten_rows(category_stats)
        )
        self._write_csv(
            self.stage_timing_summary_csv_path,
            self._flatten_rows([*attempt_timing_summary, *final_record_timing_summary]),
        )
        return summary

    def _existing_edit_records_for_source_model(
        self, source_model_id: str
    ) -> List[Dict[str, Any]]:
        with self.records_lock:
            records = [
                record
                for record in self.edit_records
                if record.get("source_model_id") == source_model_id
            ]
        return sorted(
            records, key=lambda record: int(record.get("instruction_index") or 0)
        )

    def _existing_object_record_for_source_model(
        self, source_model_id: str
    ) -> Optional[Dict[str, Any]]:
        with self.records_lock:
            for record in self.object_records:
                if record.get("source_model_id") == source_model_id:
                    return dict(record)
        return None

    def _run_object_job(
        self,
        job: ObjectJob,
        object_index: int,
    ) -> Dict[str, Any]:
        existing_prompt = self._get_existing_prompt_record(job.plan_index, object_index)
        if existing_prompt:
            prompt_context = self._source_context_from_prompt_record(existing_prompt)
            existing_object_record = self._existing_object_record_for_source_model(
                prompt_context["source_model_id"]
            )
            existing_edit_records = self._existing_edit_records_for_source_model(
                prompt_context["source_model_id"]
            )
            planned_entries = self._planned_instruction_entries_from_record(
                existing_prompt,
                "prompt_record",
            )
            planned_edit_count = (
                len(planned_entries) if planned_entries else job.instruction_plan.count
            )
            if (
                existing_object_record
                and existing_object_record.get("source_pipeline_status") == "failed"
            ):
                return {
                    "object_record": existing_object_record,
                    "edit_records": [],
                }
            if len(existing_edit_records) >= planned_edit_count:
                return {
                    "object_record": existing_object_record
                    or self._build_object_record(
                        job,
                        object_index,
                        prompt_context,
                        existing_edit_records,
                    ),
                    "edit_records": existing_edit_records,
                }

        self._log_event(
            "object_start",
            plan_index=job.plan_index,
            object_index=object_index,
            category=job.category,
            object_name=job.object_name,
            selection_mode=job.selection_mode,
        )

        prompt_context: Optional[Dict[str, Any]] = None
        object_timing_record: Dict[str, Any] = {
            "experiment_id": self.experiment_id,
            "plan_index": job.plan_index,
            "object_index": object_index,
            "selection_mode": job.selection_mode,
            "requested_category_name": job.requested_category_name,
            "category": job.category,
            "object_name": job.object_name,
        }
        try:

            def _inner() -> Dict[str, Any]:
                nonlocal prompt_context
                if existing_prompt:
                    prompt_context_local = self._source_context_from_prompt_record(
                        existing_prompt
                    )
                    prompt_context = prompt_context_local
                    # Resume any source pipeline stages that didn't complete before
                    # the previous run was interrupted (prompt was saved early).
                    self._resume_source_pipeline_if_needed(
                        prompt_context_local, job, object_index
                    )
                    existing_object_record = (
                        self._existing_object_record_for_source_model(
                            prompt_context_local["source_model_id"]
                        )
                    )
                    if existing_object_record:
                        for key_source, key_target in [
                            ("retry_meta", "source_retry_meta"),
                            ("attempt_errors", "source_attempt_errors"),
                            ("attempt_count", "source_attempt_count"),
                            ("last_error_type", "source_last_error_type"),
                            ("last_error_message", "source_last_error_message"),
                            ("failed_stage", "source_failed_stage"),
                            ("api_lane", "source_api_lane"),
                            ("timings", "source_timings"),
                            ("timing_attempts", "source_timing_attempts"),
                        ]:
                            if (
                                key_target not in prompt_context_local
                                and existing_object_record.get(key_source) is not None
                            ):
                                prompt_context_local[key_target] = (
                                    existing_object_record.get(key_source)
                                )
                    object_timing_record.update(
                        {
                            "prompt_id": prompt_context_local.get("prompt_id"),
                            "image_id": prompt_context_local.get("image_id"),
                            "source_model_id": prompt_context_local.get(
                                "source_model_id"
                            ),
                        }
                    )
                    object_edit_records_local = (
                        self._existing_edit_records_for_source_model(
                            prompt_context_local["source_model_id"]
                        )
                    )
                    existing_instruction_texts = [
                        str(record.get("instruction_text") or "")
                        for record in object_edit_records_local
                        if str(record.get("instruction_text") or "").strip()
                    ]
                    planned_entries_local = (
                        self._planned_instruction_entries_from_record(
                            existing_prompt,
                            "prompt_record",
                        )
                    )
                    if not planned_entries_local:
                        batch_payload = self._generate_instruction_batch_with_retry(
                            source_context=prompt_context_local,
                            instruction_plan=job.instruction_plan,
                            avoid_list=existing_instruction_texts,
                        )
                        self._merge_timing_payload_into_record(
                            prompt_context_local,
                            timings=batch_payload.get("_timings"),
                            timing_attempts=batch_payload.get("_timing_attempts"),
                        )
                        prompt_context_local["source_timings"] = dict(
                            prompt_context_local.get("timings", {})
                        )
                        prompt_context_local["source_timing_attempts"] = dict(
                            prompt_context_local.get("timing_attempts", {})
                        )
                        planned_entries_local = (
                            self._persist_generated_instruction_batch(
                                job=job,
                                object_index=object_index,
                                prompt_context=prompt_context_local,
                                batch_payload=batch_payload,
                            )
                        )
                    start_instruction_index_local = len(object_edit_records_local) + 1
                else:
                    prompt_context_local = self._run_source_pipeline(job, object_index)
                    prompt_context = prompt_context_local
                    object_timing_record.update(
                        {
                            "prompt_id": prompt_context_local.get("prompt_id"),
                            "image_id": prompt_context_local.get("image_id"),
                            "source_model_id": prompt_context_local.get(
                                "source_model_id"
                            ),
                        }
                    )
                    object_edit_records_local = []
                    batch_payload = self._generate_instruction_batch_with_retry(
                        source_context=prompt_context_local,
                        instruction_plan=job.instruction_plan,
                        avoid_list=[],
                    )
                    self._merge_timing_payload_into_record(
                        prompt_context_local,
                        timings=batch_payload.get("_timings"),
                        timing_attempts=batch_payload.get("_timing_attempts"),
                    )
                    prompt_context_local["source_timings"] = dict(
                        prompt_context_local.get("timings", {})
                    )
                    prompt_context_local["source_timing_attempts"] = dict(
                        prompt_context_local.get("timing_attempts", {})
                    )
                    planned_entries_local = self._persist_generated_instruction_batch(
                        job=job,
                        object_index=object_index,
                        prompt_context=prompt_context_local,
                        batch_payload=batch_payload,
                    )
                    start_instruction_index_local = 1

                # Phase 5: run edit pipelines concurrently within one object.
                # Each edit is independent (different instruction on same source).
                # Concurrency is bounded by the lane semaphores, so we submit
                # all entries and let lane back-pressure limit actual parallelism.
                remaining_entries = planned_entries_local[
                    start_instruction_index_local - 1 :
                ]
                if len(remaining_entries) <= 1:
                    # Single edit — no need for thread overhead
                    for entry in remaining_entries:
                        record = self._run_edit_pipeline_safe(
                            job=job,
                            source_context=prompt_context_local,
                            instruction_index=int(entry["instruction_index"]),
                            instruction_type=entry["instruction_type"],
                            type_index=entry["type_index"],
                            instruction_text=entry["instruction_text"],
                        )
                        object_edit_records_local.append(record)
                        self._write_image_instruction_records(
                            prompt_context_local["image_id"],
                            object_edit_records_local,
                        )
                        object_record_local = self._build_object_record(
                            job,
                            object_index,
                            prompt_context_local,
                            object_edit_records_local,
                        )
                        self._persist_progress(
                            object_record=object_record_local,
                            edit_records=[record],
                            status="running",
                        )
                else:
                    # Multiple edits — run concurrently
                    edit_futures = {}
                    _edit_lock = threading.Lock()
                    with ThreadPoolExecutor(
                        max_workers=len(remaining_entries)
                    ) as edit_executor:
                        for entry in remaining_entries:
                            fut = edit_executor.submit(
                                self._run_edit_pipeline_safe,
                                job=job,
                                source_context=prompt_context_local,
                                instruction_index=int(entry["instruction_index"]),
                                instruction_type=entry["instruction_type"],
                                type_index=entry["type_index"],
                                instruction_text=entry["instruction_text"],
                            )
                            edit_futures[fut] = entry

                        for fut in as_completed(edit_futures):
                            entry = edit_futures[fut]
                            record = fut.result()
                            with _edit_lock:
                                object_edit_records_local.append(record)
                                self._write_image_instruction_records(
                                    prompt_context_local["image_id"],
                                    object_edit_records_local,
                                )
                                object_record_local = self._build_object_record(
                                    job,
                                    object_index,
                                    prompt_context_local,
                                    object_edit_records_local,
                                )
                                self._persist_progress(
                                    object_record=object_record_local,
                                    edit_records=[record],
                                    status="running",
                                )

                object_record_local = self._build_object_record(
                    job,
                    object_index,
                    prompt_context_local,
                    object_edit_records_local,
                )
                self._log_event(
                    "object_complete",
                    plan_index=job.plan_index,
                    object_index=object_index,
                    category=job.category,
                    object_name=job.object_name,
                    attempts_total=len(object_edit_records_local),
                    stage2_passed_count=object_record_local["stage2_passed_count"],
                )
                return {
                    "prompt_context": prompt_context_local,
                    "object_record": object_record_local,
                    "edit_records": object_edit_records_local,
                }

            result = self._run_timed_non_retry_stage(
                record=object_timing_record,
                stage_name="object_total",
                func=_inner,
            )
            prompt_context = result["prompt_context"]
            object_record = result["object_record"]
            edit_records = result["edit_records"]
            object_record["timings"]["object_total"] = object_timing_record["timings"][
                "object_total"
            ]
            object_record["timing_attempts"]["object_total"] = list(
                object_timing_record["timing_attempts"]["object_total"]
            )
            return {
                "object_record": object_record,
                "edit_records": edit_records,
            }
        except Exception as exc:
            failed_object_record = self._build_failed_object_record(
                job,
                object_index,
                prompt_context,
                exc,
            )
            if object_timing_record.get("timings", {}).get("object_total"):
                failed_object_record["timings"]["object_total"] = object_timing_record[
                    "timings"
                ]["object_total"]
            if object_timing_record.get("timing_attempts", {}).get("object_total"):
                failed_object_record["timing_attempts"]["object_total"] = list(
                    object_timing_record["timing_attempts"]["object_total"]
                )
            self._persist_progress(
                object_record=failed_object_record,
                status="running",
            )
            self._log_event(
                "object_error",
                plan_index=job.plan_index,
                object_index=object_index,
                category=job.category,
                object_name=job.object_name,
                error_message=str(exc),
            )
            return {
                "object_record": failed_object_record,
                "edit_records": [],
            }

    def run_category(self, category: CategoryPlan, plan_index: int) -> Dict[str, Any]:
        object_jobs = self._resolve_object_jobs(category, plan_index)
        object_records: List[Dict[str, Any]] = []
        edit_records: List[Dict[str, Any]] = []

        for object_index, job in enumerate(object_jobs, start=1):
            result = self._run_object_job(job, object_index)
            object_records.append(result["object_record"])
            edit_records.extend(result["edit_records"])

        return {
            "plan_index": plan_index,
            "category_name": category.category_name,
            "object_records": object_records,
            "edit_records": edit_records,
        }

    def run(self) -> None:
        self.started_at = datetime.now().isoformat()
        self._write_jsonl(self.object_records_path, [])
        self._write_jsonl(self.edit_records_path, [])
        self._write_manifest(
            started_at=self.started_at,
            finished_at=None,
            totals=self._totals_from_records([], []),
            status="running",
            progress=self._progress_payload([], []),
        )
        self._log_event(
            "experiment_start",
            plan_path=self._rel_path(self.plan_path),
            category_workers=self.category_workers,
            object_workers=self.object_workers,
            category_workers_breakdown=self.category_workers_limits,
            scheduler_mode=self.scheduler_mode,
            source_provider=self.plan.source_provider,
            target_provider=self.plan.target_provider,
        )
        scheduled_jobs = self._scheduled_object_jobs()
        self._log_event(
            "scheduler_start",
            scheduler_mode=self.scheduler_mode,
            object_workers=self.object_workers,
            scheduled_object_count=len(scheduled_jobs),
        )

        with ThreadPoolExecutor(max_workers=self.object_workers) as executor:
            futures = {
                executor.submit(self._run_object_job, job, object_index): (
                    job.plan_index,
                    object_index,
                )
                for object_index, job in scheduled_jobs
            }
            for future in as_completed(futures):
                future.result()

        summary = self._persist_progress(
            status="completed",
            finished_at=datetime.now().isoformat(),
        )
        self._log_event(
            "experiment_complete",
            object_count=summary["object_count"],
            edit_attempts_total=summary["edit_attempts_total"],
            stage2_passed_count=summary["stage2_passed_count"],
        )


def _load_existing_runner(
    experiment_id: str, *, gpu_id: Optional[int] = None
) -> ExperimentRunner:
    config = load_config()
    pipeline_dir_raw = config.workspace.pipeline_dir
    pipeline_dir = (
        Path(pipeline_dir_raw)
        if Path(pipeline_dir_raw).is_absolute()
        else PROJECT_ROOT / pipeline_dir_raw
    )
    experiment_dir = pipeline_dir / "experiments" / experiment_id
    manifest_path = experiment_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Experiment manifest not found: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    raw_plan = _require_mapping(manifest.get("plan"), "experiment manifest.plan")
    plan = _load_plan_from_mapping(
        raw_plan,
        default_name=str(manifest.get("name") or experiment_id),
        path_label="experiment_manifest.plan",
    )
    plan_path_raw = _require_non_empty_str(
        manifest.get("plan_path"), "experiment manifest.plan_path"
    )
    plan_path = _resolve_recorded_path(plan_path_raw, pipeline_dir)
    return ExperimentRunner(
        plan,
        plan_path,
        experiment_id=experiment_id,
        gpu_id=gpu_id,
    )


def recover_experiment_records(
    experiment_id: str, *, write_files: bool, gpu_id: Optional[int] = None
) -> Dict[str, Any]:
    runner = _load_existing_runner(experiment_id, gpu_id=gpu_id)
    summary = runner.recover_partial_outputs(write_files=write_files)
    return {
        "experiment_id": experiment_id,
        "object_records": runner.object_records,
        "edit_records": runner.edit_records,
        "summary": summary,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full prompt -> source 3D -> edit -> target 3D experiment."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--plan",
        help="Path to experiment plan YAML/JSON file",
    )
    mode.add_argument(
        "--repair-experiment-id",
        help="Reconstruct partial records/summary for an interrupted experiment",
    )
    mode.add_argument(
        "--resume-experiment-id",
        help="Resume missing edit slots for an interrupted experiment",
    )
    parser.add_argument(
        "--gpu-id",
        type=_normalize_gpu_id,
        help="Restrict this run to a single visible GPU index",
    )
    return parser.parse_args()


def _describe_process_target(args: argparse.Namespace) -> Tuple[str, str]:
    if args.plan:
        mode = "plan"
        target = str(Path(args.plan).expanduser().resolve())
    elif args.repair_experiment_id:
        mode = "repair"
        target = str(args.repair_experiment_id)
    else:
        mode = "resume"
        target = str(args.resume_experiment_id)
    return mode, target


def _log_process_header(args: argparse.Namespace, gpu_id: Optional[int]) -> None:
    mode, target = _describe_process_target(args)
    print(
        "[run_full_experiment] started "
        f"pid={os.getpid()} "
        f"mode={mode} "
        f"gpu_id={gpu_id if gpu_id is not None else 'all'} "
        f"target={target}",
        flush=True,
    )


def _log_process_footer(
    *,
    mode: str,
    target: str,
    gpu_id: Optional[int],
    status: str,
    experiment_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    message = (
        "[run_full_experiment] finished "
        f"pid={os.getpid()} "
        f"mode={mode} "
        f"gpu_id={gpu_id if gpu_id is not None else 'all'} "
        f"target={target} "
        f"status={status}"
    )
    if experiment_id:
        message += f" experiment_id={experiment_id}"
    if error:
        message += f" error={error}"
    print(message, flush=True)


def main() -> None:
    args = parse_args()
    gpu_id = args.gpu_id if args.gpu_id is not None else EARLY_GPU_ID
    if gpu_id is not None:
        _apply_gpu_visibility(gpu_id)
    _log_process_header(args, gpu_id)
    mode, target = _describe_process_target(args)
    experiment_id: Optional[str] = None
    try:
        if args.plan:
            plan_path = Path(args.plan).expanduser().resolve()
            plan = load_plan(plan_path)
            runner = ExperimentRunner(plan, plan_path, gpu_id=gpu_id)
            experiment_id = runner.experiment_id
            runner.run()
            _log_process_footer(
                mode=mode,
                target=target,
                gpu_id=gpu_id,
                status="completed",
                experiment_id=experiment_id,
            )
            return

        if args.repair_experiment_id:
            result = recover_experiment_records(
                args.repair_experiment_id,
                write_files=True,
                gpu_id=gpu_id,
            )
            experiment_id = result["experiment_id"]
            print(
                json.dumps(
                    {
                        "experiment_id": result["experiment_id"],
                        "object_count": len(result["object_records"]),
                        "edit_count": len(result["edit_records"]),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            _log_process_footer(
                mode=mode,
                target=target,
                gpu_id=gpu_id,
                status="completed",
                experiment_id=experiment_id,
            )
            return

        runner = _load_existing_runner(args.resume_experiment_id, gpu_id=gpu_id)
        experiment_id = runner.experiment_id
        result = runner.resume_experiment()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        _log_process_footer(
            mode=mode,
            target=target,
            gpu_id=gpu_id,
            status="completed",
            experiment_id=experiment_id,
        )
    except KeyboardInterrupt:
        _log_process_footer(
            mode=mode,
            target=target,
            gpu_id=gpu_id,
            status="interrupted",
            experiment_id=experiment_id,
            error="KeyboardInterrupt",
        )
        raise
    except Exception as exc:
        _log_process_footer(
            mode=mode,
            target=target,
            gpu_id=gpu_id,
            status="failed",
            experiment_id=experiment_id,
            error=str(exc),
        )
        raise


if __name__ == "__main__":
    main()
