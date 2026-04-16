#!/usr/bin/env python3
"""
app_v2.py - New Visualization Backend

A clean, minimal Flask app for the batch pipeline.
Light theme, no emojis, async task management.
"""

import json
import math
import os
import shlex
import shutil
import statistics
import subprocess
import sys
import threading
import time
import traceback as _traceback
import uuid
import zipfile
import tempfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_from_directory,
    send_file,
)

# Setup paths
# Setup paths
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import load_config
from utils.pipeline_index import PipelineIndex
from utils.experiment_plan import (
    build_instruction_plan,
    normalize_instruction_plan_from_category,
)
from utils.experiment_concurrency import (
    derive_run_full_experiment_category_workers,
    describe_run_full_experiment_category_workers,
    get_run_full_experiment_concurrency_limits,
)
from utils.validation import require_non_empty, require_param
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
    build_instruction_display_payload,
    resolve_instruction_display_from_edit_meta,
    resolve_instruction_display_from_instruction_item,
    resolve_instruction_display_from_record,
)
from core.image.edit_artifact_builder import (
    build_edit_artifacts,
    materialize_missing_masks,
)
from core.image.view_stitcher import ViewStitcher
from scripts.run_full_experiment import recover_experiment_records

app = Flask(__name__, template_folder="templates", static_folder="static")

# Directories — initialized from config on startup (see init_semaphores)
PIPELINE_DIR = None
PROMPTS_DIR = None
IMAGES_DIR = None
MODELS_DIR = None
INSTRUCTIONS_DIR = None
TRIPLETS_DIR = None

# Task store for async operations
TASKS_FILE = PROJECT_ROOT / "workspace" / "tasks.jsonl"
task_store = {}
task_lock = threading.Lock()
tasks_file_lock = threading.Lock()
prompts_file_lock = threading.Lock()
pipeline_listing_cache_lock = threading.Lock()

# UI list pages hit a remote pipeline workspace very frequently. Keep a short
# in-memory cache so simple page navigation does not rescan the whole tree.
PIPELINE_LISTING_CACHE_TTL_SECONDS = 30.0
pipeline_listing_cache = {}
EXPERIMENT_METADATA_CACHE_TTL_SECONDS = 30.0

# SQLite-backed persistent index — replaces full-scan on cache miss.
_pipeline_index: PipelineIndex | None = None
_index_reconcile_lock = threading.Lock()
_index_reconcile_running = False

# Provider ID mapping: short filename ID -> human-readable provider name
PROVIDER_ID_TO_NAME = {
    "tp3": "tripo",
    "hy3": "hunyuan",
    "rd2": "rodin",
}


def _rel_path(path: Path) -> str:
    """Return a URL-friendly path string for API responses and frontend use.

    When pipeline_dir is inside PROJECT_ROOT (relative config), returns the path
    relative to PROJECT_ROOT (e.g. "data/pipeline/images/abc/image.png") so the
    existing /data/<filename> route can serve it.

    When pipeline_dir is an external absolute path, returns a path prefixed with
    "pipeline/" (e.g. "pipeline/images/abc/image.png") so the /pipeline/<filename>
    route can serve it. Falls back to the raw absolute path string if the file is
    outside PIPELINE_DIR as well.
    """
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        pass
    # External pipeline_dir: try to make path relative to PIPELINE_DIR
    if PIPELINE_DIR is not None:
        try:
            rel = str(path.relative_to(PIPELINE_DIR)).replace("\\", "/")
            return f"pipeline/{rel}"
        except ValueError:
            pass
    return str(path).replace("\\", "/")


def _resolve_api_path(api_path: str) -> Path:
    """Resolve an API path (from JSON response) back to a filesystem Path.

    Handles three formats:
    1. "data/pipeline/..." -> PROJECT_ROOT / data/pipeline/...
    2. "pipeline/..." -> PIPELINE_DIR / ...
    3. Absolute paths -> used as-is
    """
    if api_path.startswith("data/pipeline/"):
        return PROJECT_ROOT / api_path
    if api_path.startswith("pipeline/"):
        relative = api_path[len("pipeline/") :]
        return PIPELINE_DIR / relative
    # Absolute path or other format
    return Path(api_path)


def _glb_to_provider_info(glb: Path) -> dict:
    """Extract provider info from a GLB file path.

    Returns dict with: path, filename, provider, provider_id
    """
    provider_id = glb.stem.replace("model_", "")  # e.g., "tp3"
    return {
        "path": _rel_path(glb).replace("\\", "/"),
        "filename": glb.name,
        "provider": PROVIDER_ID_TO_NAME.get(provider_id, provider_id),
        "provider_id": provider_id,
    }


def _with_instruction_item_payload(item: dict | str) -> dict:
    payload = (
        {"text": item.strip()} if isinstance(item, str) else dict(item or {})
    )
    semantics = resolve_instruction_display_from_instruction_item(payload)
    payload.update(semantics)
    payload["text"] = payload.get("text") or semantics["instruction_display_text"]
    return payload


def _with_edit_meta_instruction_payload(edit_meta: dict) -> dict:
    payload = dict(edit_meta or {})
    semantics = resolve_instruction_display_from_edit_meta(payload)
    payload.update(semantics)
    payload["instruction"] = semantics["instruction_display_text"]
    return payload


# Semaphores will be initialized from config on startup
GEN3D_SEMAPHORES = {}
RENDER_SEMAPHORE = None
EDIT_SEMAPHORE = None
QUALITY_CHECK_SEMAPHORE = None
RECON_QUALITY_CHECK_SEMAPHORE = None
MASK_BACKFILL_SEMAPHORE = None
# Workspace paths — initialized from config in init_semaphores()
EXPERIMENT_PLANS_DIR = None
PYTHON_INTERPRETER = None


def init_semaphores():
    """Initialize concurrency semaphores from config."""
    global GEN3D_SEMAPHORES, RENDER_SEMAPHORE, EDIT_SEMAPHORE, QUALITY_CHECK_SEMAPHORE
    global RECON_QUALITY_CHECK_SEMAPHORE, MASK_BACKFILL_SEMAPHORE
    global \
        PIPELINE_DIR, \
        PROMPTS_DIR, \
        IMAGES_DIR, \
        MODELS_DIR, \
        INSTRUCTIONS_DIR, \
        TRIPLETS_DIR
    global EXPERIMENT_PLANS_DIR, PYTHON_INTERPRETER, LOGS_DIR
    global CATEGORIZED_OBJECTS_FILE
    config = load_config()

    # Resolve pipeline directory (relative paths are anchored to PROJECT_ROOT)
    pipeline_dir_raw = config.workspace.pipeline_dir
    pipeline_dir = Path(pipeline_dir_raw)
    if not pipeline_dir.is_absolute():
        pipeline_dir = PROJECT_ROOT / pipeline_dir
    PIPELINE_DIR = pipeline_dir
    PROMPTS_DIR = PIPELINE_DIR / "prompts"
    IMAGES_DIR = PIPELINE_DIR / "images"
    MODELS_DIR = PIPELINE_DIR / "models_src"
    INSTRUCTIONS_DIR = PIPELINE_DIR / "instructions"
    TRIPLETS_DIR = PIPELINE_DIR / "triplets"
    EXPERIMENT_PLANS_DIR = PIPELINE_DIR / "experiment_plans"
    PYTHON_INTERPRETER = config.workspace.python_interpreter
    LOGS_DIR = Path(config.workspace.logs_dir)

    # Resolve categorized objects file from config
    _objects_file = Path(config.workspace.matrix_objects_file)
    if not _objects_file.is_absolute():
        _objects_file = PROJECT_ROOT / _objects_file
    CATEGORIZED_OBJECTS_FILE = _objects_file

    GEN3D_SEMAPHORES = {
        "hunyuan": threading.Semaphore(config.concurrency.gen3d.hunyuan),
        "tripo": threading.Semaphore(config.concurrency.gen3d.tripo),
        "rodin": threading.Semaphore(config.concurrency.gen3d.rodin),
    }
    RENDER_SEMAPHORE = threading.Semaphore(config.concurrency.render)

    # Initialize edit semaphore
    EDIT_SEMAPHORE = threading.Semaphore(config.concurrency.image)
    QUALITY_CHECK_SEMAPHORE = threading.Semaphore(config.concurrency.edit_quality_check)
    RECON_QUALITY_CHECK_SEMAPHORE = threading.Semaphore(
        config.concurrency.recon_quality_check
    )
    MASK_BACKFILL_SEMAPHORE = threading.Semaphore(config.concurrency.mask_backfill)

    global _pipeline_index
    db_path = config.workspace.pipeline_index_db
    _pipeline_index = PipelineIndex(db_path)
    print(f"  [Index] db: {db_path}")

    print(f"  [Workspace] pipeline_dir: {PIPELINE_DIR}")
    print(
        f"  [Concurrency] gen3d: hunyuan={config.concurrency.gen3d.hunyuan}, "
        f"tripo={config.concurrency.gen3d.tripo}, rodin={config.concurrency.gen3d.rodin}"
    )
    print(f"  [Concurrency] render={config.concurrency.render}")
    print(f"  [Concurrency] edit={config.concurrency.image}")
    print(f"  [Concurrency] edit_quality_check={config.concurrency.edit_quality_check}")
    print(
        f"  [Concurrency] recon_quality_check={config.concurrency.recon_quality_check}"
    )
    print(f"  [Concurrency] mask_backfill={config.concurrency.mask_backfill}")


def safe_load_json(file_path, default=None):
    """Safely load JSON from file, returning default if file is empty or invalid."""
    if default is None:
        default = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
    except (json.JSONDecodeError, ValueError, IOError):
        return default


def _require_record_string(record: dict, key: str, record_type: str) -> str:
    """Require a record field to be a non-empty string."""
    value = require_param(record, key, record_type)
    if not isinstance(value, str):
        raise ValueError(f"{record_type} field '{key}' must be a string")
    return require_non_empty(value.strip(), f"{record_type} field '{key}'")


def _normalize_optional_record_string(
    record: dict, key: str, record_type: str
) -> str | None:
    """Normalize an optional string field and fail loudly on invalid types."""
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{record_type} field '{key}' must be a string")
    normalized = value.strip()
    return normalized or None


def _normalize_source_model_id_for_listing(record: dict, record_type: str) -> str | None:
    """
    Return a usable source_model_id for UI listings.

    Experiment history may contain dirty records with null / empty source_model_id.
    Those records should not inflate YAML filter counts or produce a fake "null" model.
    """
    value = record.get("source_model_id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{record_type} field 'source_model_id' must be a string")
    normalized = value.strip()
    return normalized or None


def _normalize_prompt_status_payload(
    *,
    status: str,
    status_label: str,
    status_badge_class: str,
) -> dict:
    return {
        "status": status,
        "status_label": status_label,
        "status_badge_class": status_badge_class,
    }


def _normalize_legacy_prompt_record(record: dict, source_file: str) -> dict:
    prompt_id = _require_record_string(record, "id", "legacy prompt record")
    prompt_text = _require_record_string(record, "prompt", "legacy prompt record")
    raw_status = _require_record_string(record, "status", "legacy prompt record")
    subject = _normalize_optional_record_string(
        record, "subject", "legacy prompt record"
    )
    image_path = _normalize_optional_record_string(
        record, "image_path", "legacy prompt record"
    )

    status_map = {
        "pending": _normalize_prompt_status_payload(
            status="pending",
            status_label="pending",
            status_badge_class="pending",
        ),
        "completed": _normalize_prompt_status_payload(
            status="completed",
            status_label="completed",
            status_badge_class="completed",
        ),
        "failed": _normalize_prompt_status_payload(
            status="failed",
            status_label="failed",
            status_badge_class="failed",
        ),
        "deleted": _normalize_prompt_status_payload(
            status="deleted",
            status_label="deleted",
            status_badge_class="failed",
        ),
        "running": _normalize_prompt_status_payload(
            status="running",
            status_label="running",
            status_badge_class="running",
        ),
    }
    if raw_status not in status_map:
        raise ValueError(
            f"Unsupported legacy prompt status '{raw_status}' in {source_file}"
        )
    status_payload = status_map[raw_status]

    return {
        "id": prompt_id,
        "ui_id": prompt_id,
        "raw_id": prompt_id,
        "schema": "legacy_prompt",
        "schema_label": "Legacy",
        "subject": subject,
        "prompt": prompt_text,
        "created_at": _normalize_optional_record_string(
            record, "created_at", "legacy prompt record"
        ),
        "updated_at": _normalize_optional_record_string(
            record, "updated_at", "legacy prompt record"
        ),
        "experiment_id": None,
        "source_model_id": None,
        "image_id": None,
        "image_path": image_path,
        "source_file": source_file,
        "can_generate_image": raw_status == "pending",
        "can_batch_generate": raw_status == "pending",
        "can_delete": True,
        "can_open_image": raw_status == "completed",
        **status_payload,
    }


def _resolve_experiment_prompt_image_path(record: dict) -> str | None:
    image_path = _normalize_optional_record_string(
        record, "image_path", "experiment prompt record"
    )
    if image_path:
        return image_path

    image_id = _normalize_optional_record_string(
        record, "image_id", "experiment prompt record"
    )
    if image_id:
        candidate = IMAGES_DIR / image_id / "image.png"
        if candidate.exists():
            return _rel_path(candidate)
    return None


def _normalize_experiment_prompt_record(record: dict, source_file: str) -> dict:
    prompt_id = _require_record_string(record, "prompt_id", "experiment prompt record")
    experiment_id = _require_record_string(
        record, "experiment_id", "experiment prompt record"
    )
    prompt_text = _require_record_string(record, "prompt", "experiment prompt record")
    subject = _require_record_string(
        record, "object_name", "experiment prompt record"
    )
    source_model_id = _normalize_optional_record_string(
        record, "source_model_id", "experiment prompt record"
    )
    image_id = _normalize_optional_record_string(
        record, "image_id", "experiment prompt record"
    )

    image_path = _resolve_experiment_prompt_image_path(record)
    if source_model_id:
        status_payload = _normalize_prompt_status_payload(
            status="experiment_source_model_ready",
            status_label="source model ready",
            status_badge_class="completed",
        )
    elif image_path or image_id:
        status_payload = _normalize_prompt_status_payload(
            status="experiment_image_ready",
            status_label="image ready",
            status_badge_class="completed",
        )
    else:
        status_payload = _normalize_prompt_status_payload(
            status="experiment_prompt_only",
            status_label="experiment prompt",
            status_badge_class="running",
        )

    return {
        "id": prompt_id,
        "ui_id": prompt_id,
        "raw_id": prompt_id,
        "schema": "experiment_prompt",
        "schema_label": "Experiment",
        "subject": subject,
        "prompt": prompt_text,
        "created_at": _normalize_optional_record_string(
            record, "created_at", "experiment prompt record"
        ),
        "updated_at": None,
        "experiment_id": experiment_id,
        "source_model_id": source_model_id,
        "image_id": image_id,
        "image_path": image_path,
        "source_file": source_file,
        "can_generate_image": False,
        "can_batch_generate": False,
        "can_delete": False,
        "can_open_image": bool(image_path),
        **status_payload,
    }


def normalize_prompt_record(record: dict, source_file: str) -> dict:
    """Normalize a prompt record from one of the two supported schemas."""
    if not isinstance(record, dict):
        raise ValueError(
            f"prompt record from {source_file} must be an object, got {type(record).__name__}"
        )
    if "id" in record:
        return _normalize_legacy_prompt_record(record, source_file)
    if "prompt_id" in record and "experiment_id" in record:
        return _normalize_experiment_prompt_record(record, source_file)
    raise ValueError(
        f"Unsupported prompt record schema in {source_file}: keys={sorted(record.keys())}"
    )


def _normalize_image_subject(image_id: str, meta: dict) -> tuple[str | None, str]:
    subject = meta.get("subject")
    if subject is not None and not isinstance(subject, str):
        raise ValueError(f"image meta for {image_id} field 'subject' must be a string")
    object_name = meta.get("object_name")
    if object_name is not None and not isinstance(object_name, str):
        raise ValueError(
            f"image meta for {image_id} field 'object_name' must be a string"
        )

    if isinstance(subject, str) and subject.strip():
        normalized = subject.strip()
        return normalized, normalized
    if isinstance(object_name, str) and object_name.strip():
        normalized = object_name.strip()
        return normalized, normalized

    parent_id = meta.get("parent_id")
    parent_context_id = (
        parent_id.strip() if isinstance(parent_id, str) and parent_id.strip() else image_id
    )
    object_context = _get_image_object_context(parent_context_id)
    if object_context["object_name"]:
        return object_context["object_name"], object_context["object_name"]
    return None, parent_context_id


def normalize_image_record(
    *,
    image_id: str,
    image_path: str,
    meta: dict,
    instruction: str | None,
    instructions_list: list,
    instruction_items: list,
    model_path: str | None,
    model_providers: list,
    has_views: bool,
) -> dict:
    """Normalize an image record from legacy, experiment, or variant schema."""
    if not isinstance(meta, dict):
        raise ValueError(f"image meta for {image_id} must be an object")

    if "parent_id" in meta:
        schema = "variant_image"
    elif "object_name" in meta or "experiment_id" in meta:
        schema = "experiment_image"
    elif "subject" in meta or "generated_at" in meta or "prompt" in meta:
        schema = "legacy_image"
    else:
        raise ValueError(
            f"Unsupported image meta schema for {image_id}: keys={sorted(meta.keys())}"
        )

    subject, display_subject = _normalize_image_subject(image_id, meta)
    created_at = meta.get("generated_at") or meta.get("created_at")
    if created_at is not None and not isinstance(created_at, str):
        raise ValueError(
            f"image meta for {image_id} datetime field must be a string when present"
        )
    prompt_text = meta.get("prompt")
    if prompt_text is not None and not isinstance(prompt_text, str):
        raise ValueError(f"image meta for {image_id} field 'prompt' must be a string")
    parent_id = meta.get("parent_id")
    if parent_id is not None and not isinstance(parent_id, str):
        raise ValueError(f"image meta for {image_id} field 'parent_id' must be a string")

    return {
        "id": image_id,
        "schema": schema,
        "path": image_path,
        "subject": subject,
        "display_subject": display_subject,
        "prompt": prompt_text,
        "instruction": instruction,
        "instructions": instructions_list,
        "instruction_items": instruction_items,
        "model_path": model_path,
        "model_providers": model_providers,
        "has_views": has_views,
        "created_at": created_at.strip() if isinstance(created_at, str) else None,
        "parent_id": parent_id.strip() if isinstance(parent_id, str) and parent_id.strip() else None,
    }


def _get_pipeline_listing_cache_version() -> tuple[int, int, int, int]:
    """Build a cheap cache version from list-page root directories."""
    versions = []
    for root in (PROMPTS_DIR, IMAGES_DIR, MODELS_DIR, TRIPLETS_DIR):
        if root is None:
            versions.append(-1)
            continue
        try:
            versions.append(root.stat().st_mtime_ns)
        except OSError:
            versions.append(-1)
    return tuple(versions)


def clear_pipeline_listing_cache():
    """Invalidate cached list-page payloads after filesystem writes."""
    with pipeline_listing_cache_lock:
        pipeline_listing_cache.clear()


def _refresh_model_in_index(model_id: str):
    """Update a single model's rows in the SQLite index (non-blocking background)."""
    if _pipeline_index is None or MODELS_DIR is None:
        return

    def _do():
        try:
            config = load_config()
            semantic_tmp = config.render.semantic_alignment.temp_dir_name
            model_dir = MODELS_DIR / model_id
            _pipeline_index.update_model(
                model_id,
                model_dir,
                build_index_entry=lambda d: _build_model_index_entry(d, semantic_tmp),
                build_payload=_load_model_payload_by_id_live,
            )
        except Exception:
            pass  # index update is best-effort

    threading.Thread(target=_do, daemon=True).start()


def _refresh_image_in_index(image_id: str):
    """Update a single image's row in the SQLite index."""
    if _pipeline_index is None or IMAGES_DIR is None:
        return

    def _do():
        try:
            image_dir = IMAGES_DIR / image_id
            _pipeline_index.update_image(
                image_id,
                image_dir,
                build_entry=_build_image_index_entry,
            )
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


def _start_index_reconcile():
    """Kick off a background reconcile of the SQLite index against the filesystem."""
    global _index_reconcile_running
    with _index_reconcile_lock:
        if _index_reconcile_running:
            return
        _index_reconcile_running = True

    def _do():
        global _index_reconcile_running
        try:
            if _pipeline_index is None or MODELS_DIR is None or IMAGES_DIR is None:
                return
            config = load_config()
            semantic_tmp = config.render.semantic_alignment.temp_dir_name
            _pipeline_index.reconcile(
                models_dir=MODELS_DIR,
                images_dir=IMAGES_DIR,
                triplets_dir=TRIPLETS_DIR,
                build_model_index_entry=lambda d: _build_model_index_entry(
                    d, semantic_tmp
                ),
                build_model_payload=_load_model_payload_by_id_live,
                build_image_index_entry=_build_image_index_entry,
                semantic_tmp_dir_name=semantic_tmp,
            )
            # Also invalidate in-memory cache so next request uses fresh DB data
            clear_pipeline_listing_cache()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "[pipeline_index] reconcile failed: %s", exc
            )
        finally:
            with _index_reconcile_lock:
                _index_reconcile_running = False

    threading.Thread(target=_do, daemon=True, name="index-reconcile").start()



def _get_cached_pipeline_listing(cache_key: str, loader):
    """Cache expensive pipeline scans for a short time window."""
    version = _get_pipeline_listing_cache_version()
    now = time.monotonic()

    with pipeline_listing_cache_lock:
        cached = pipeline_listing_cache.get(cache_key)
        if (
            cached
            and cached["version"] == version
            and now - cached["cached_at"] < PIPELINE_LISTING_CACHE_TTL_SECONDS
        ):
            return cached["value"]

    value = loader()

    with pipeline_listing_cache_lock:
        pipeline_listing_cache[cache_key] = {
            "version": version,
            "cached_at": now,
            "value": value,
        }

    return value


def _get_experiment_metadata_cache_version() -> int:
    """Build a cache version for experiment metadata scans."""
    experiments_dir = _get_pipeline_experiments_dir()
    try:
        return experiments_dir.stat().st_mtime_ns
    except OSError:
        return -1


def _get_cached_experiment_metadata(cache_key: str, loader):
    """Cache expensive experiment metadata scans for a short time window."""
    version = _get_experiment_metadata_cache_version()
    now = time.monotonic()

    with pipeline_listing_cache_lock:
        cached = pipeline_listing_cache.get(cache_key)
        if (
            cached
            and cached["version"] == version
            and now - cached["cached_at"] < EXPERIMENT_METADATA_CACHE_TTL_SECONDS
        ):
            return cached["value"]

    value = loader()

    with pipeline_listing_cache_lock:
        pipeline_listing_cache[cache_key] = {
            "version": version,
            "cached_at": now,
            "value": value,
        }

    return value


@lru_cache(maxsize=1)
def _load_category_object_lookup() -> tuple[dict[str, list[str]], dict[str, str]]:
    """Load category -> objects and reverse object -> category lookup."""
    if not CATEGORIZED_OBJECTS_FILE.exists():
        return {}, {}

    with open(CATEGORIZED_OBJECTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    reverse_lookup = {}
    if isinstance(data, dict):
        for category_name, object_names in data.items():
            if not isinstance(object_names, list):
                continue
            for object_name in object_names:
                if isinstance(object_name, str) and object_name not in reverse_lookup:
                    reverse_lookup[object_name] = category_name
    return data if isinstance(data, dict) else {}, reverse_lookup


def _get_image_object_context(image_id: str) -> dict[str, str | None]:
    """Resolve category/object display info for an image/model id."""
    if IMAGES_DIR is None:
        return {"object_name": None, "category_name": None}

    image_meta_path = IMAGES_DIR / image_id / "meta.json"
    meta = safe_load_json(image_meta_path, {}) if image_meta_path.exists() else {}

    object_name = meta.get("object_name") or meta.get("subject")
    category_name = meta.get("category")

    if not category_name and isinstance(object_name, str) and object_name:
        _, reverse_lookup = _load_category_object_lookup()
        category_name = reverse_lookup.get(object_name)

    if not isinstance(object_name, str) or not object_name.strip():
        object_name = None
    else:
        object_name = object_name.strip()

    if not isinstance(category_name, str) or not category_name.strip():
        category_name = None
    else:
        category_name = category_name.strip()

    return {
        "object_name": object_name,
        "category_name": category_name,
    }


# _build_quality_check_meta removed — use
# core.image.edit_quality_router.build_quality_check_meta instead.


# =============================================================================
# Task Management
# =============================================================================


def update_task_in_file(task_id: str, task_data: dict):
    """更新持久化文件中的任务状态"""
    with tasks_file_lock:
        tasks = load_jsonl(TASKS_FILE)
        for i, t in enumerate(tasks):
            if t.get("id") == task_id:
                tasks[i] = task_data
                break
        save_jsonl(TASKS_FILE, tasks)


# =============================================================================
# Helpers
# =============================================================================


def load_jsonl(filepath: Path) -> list:
    """Load records from a JSONL file."""
    if not filepath.exists():
        return []
    records = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def save_jsonl(filepath: Path, records: list):
    """Save records to a JSONL file (atomic write with unique temp file)."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    # Use unique temp filename to avoid race between threads
    tmp_path = filepath.parent / f".{filepath.name}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp_path, filepath)


def _write_json_atomic(filepath: Path, payload: dict):
    """Atomically write one JSON file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = filepath.parent / f".{filepath.name}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, filepath)


def _is_safe_asset_id(asset_id: str) -> bool:
    """Basic guard against path traversal."""
    if not asset_id or not isinstance(asset_id, str):
        return False
    if len(asset_id) > 128:
        return False
    if "/" in asset_id or "\\" in asset_id or ".." in asset_id:
        return False
    return True


def _has_active_task(task_type: str, model_id: str) -> bool:
    with task_lock:
        for task in task_store.values():
            if task.get("type") != task_type:
                continue
            params = task.get("params", {})
            if params.get("model_id") != model_id:
                continue
            if task.get("status") in {"pending", "running"}:
                return True
    return False


def _has_active_dreamsim_refresh_task(model_id: str = None) -> bool:
    """Check whether a DreamSim refresh task is active.

    If model_id is provided, also detect overlap with refresh-all requests.
    """
    with task_lock:
        for task in task_store.values():
            task_type = task.get("type")
            if task_type not in {"refresh_model_dreamsim", "refresh_all_models_dreamsim"}:
                continue
            if task.get("status") not in {"pending", "running"}:
                continue

            if model_id is None:
                return True

            params = task.get("params", {})
            if task_type == "refresh_model_dreamsim":
                if params.get("model_id") == model_id:
                    return True
                continue

            # refresh_all_models_dreamsim
            requested_model_ids = params.get("model_ids")
            if requested_model_ids is None:
                # refresh-all without filter means all models are covered.
                return True
            if isinstance(requested_model_ids, list):
                normalized_ids = {
                    str(item).strip() for item in requested_model_ids if str(item).strip()
                }
                if model_id in normalized_ids:
                    return True

    return False


def _delete_tree(path: Path) -> dict:
    """Delete a file or directory tree. Returns a status dict for API response."""
    rel = None
    try:
        rel = _rel_path(path).replace("\\", "/")
    except Exception:
        rel = str(path)

    if not path.exists():
        return {"path": rel, "deleted": False, "reason": "not_found"}

    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return {"path": rel, "deleted": True}
    except Exception as e:
        return {"path": rel, "deleted": False, "error": str(e)}


def _delete_prompt_from_jsonl(prompt_id: str) -> bool:
    """Remove a prompt record from any prompts batch JSONL file. Returns True if removed."""
    if not PROMPTS_DIR.exists():
        return False

    removed = False
    with prompts_file_lock:
        for batch_file in PROMPTS_DIR.glob("*.jsonl"):
            prompts = load_jsonl(batch_file)
            if not prompts:
                continue
            new_prompts = [p for p in prompts if p.get("id") != prompt_id]
            if len(new_prompts) != len(prompts):
                save_jsonl(batch_file, new_prompts)
                removed = True
    return removed


def _delete_assets(asset_id: str) -> list:
    """Cascade delete image/model/instruction/render assets for a given id."""
    targets = [
        IMAGES_DIR / asset_id,
        MODELS_DIR / asset_id,
        INSTRUCTIONS_DIR / asset_id,
        TRIPLETS_DIR / asset_id,
    ]
    results = []
    for p in targets:
        results.append(_delete_tree(p))
    return results


def update_prompt_status(prompt_id: str, status: str, image_path: str = None):
    """
    Update a prompt's status in its batch file.

    Args:
        prompt_id: The prompt ID to update
        status: New status (e.g., 'completed', 'failed')
        image_path: Path to generated image (optional)
    """
    if not PROMPTS_DIR.exists():
        return False

    with prompts_file_lock:
        for batch_file in PROMPTS_DIR.glob("*.jsonl"):
            prompts = load_jsonl(batch_file)
            updated = False

            for p in prompts:
                if p.get("id") == prompt_id:
                    p["status"] = status
                    if image_path:
                        p["image_path"] = image_path
                    p["updated_at"] = datetime.now().isoformat()
                    updated = True
                    break

            if updated:
                save_jsonl(batch_file, prompts)
                return True

    return False


def get_all_prompts() -> list:
    """Get all prompt records from all batch files using the supported schemas."""
    all_prompts = []
    if PROMPTS_DIR.exists():
        for f in sorted(PROMPTS_DIR.glob("*.jsonl"), reverse=True):
            prompts = load_jsonl(f)
            for p in prompts:
                all_prompts.append(normalize_prompt_record(p, f.name))
    return all_prompts


def _get_prompt_by_ui_id(prompt_id: str) -> dict | None:
    for prompt in get_all_prompts():
        if prompt.get("ui_id") == prompt_id:
            return prompt
    return None


def _scan_home_stats() -> dict:
    """Collect lightweight homepage counts without loading full list payloads."""
    errors = []
    prompts_count = None
    try:
        prompts_count = len(get_all_prompts())
    except OSError as exc:
        errors.append(f"Failed to scan prompts: {exc}")

    images_count = None
    try:
        images_count = 0
        if IMAGES_DIR.exists():
            for image_dir in IMAGES_DIR.iterdir():
                if image_dir.is_dir() and (image_dir / "image.png").exists():
                    images_count += 1
    except OSError as exc:
        errors.append(f"Failed to scan images: {exc}")

    models_count = None
    try:
        models_count = 0
        for model_dir in _iter_source_model_dirs():
            if any(model_dir.glob("*.glb")):
                models_count += 1
    except OSError as exc:
        errors.append(f"Failed to scan models: {exc}")

    stats = {
        "prompts": prompts_count,
        "images": images_count,
        "models": models_count,
    }
    if errors:
        stats["errors"] = errors
    return stats


def get_home_stats() -> dict:
    """Get lightweight homepage stats with a short in-memory cache."""
    return _get_cached_pipeline_listing("home_stats", _scan_home_stats)


def _scan_all_images() -> list:
    """Get all images from pipeline/images."""
    images = []
    if IMAGES_DIR.exists():
        for d in sorted(IMAGES_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            image_path = d / "image.png"
            if not image_path.exists():
                continue

            meta_path = d / "meta.json"
            meta = safe_load_json(meta_path, {}) if meta_path.exists() else {}

            # Check for instruction (support both .txt and .json)
            instruction = None
            instructions_list = []
            instruction_items = []

            # Try JSON format first (list of instructions)
            instruction_json_path = d / "instructions.json"
            if instruction_json_path.exists():
                with open(instruction_json_path, "r", encoding="utf-8") as f:
                    raw_list = json.load(f)
                    if isinstance(raw_list, list):
                        instruction_items = [
                            _with_instruction_item_payload(item)
                            for item in raw_list
                        ]
                        instructions_list = [
                            item["instruction_display_text"]
                            for item in instruction_items
                            if item.get("instruction_display_text")
                        ]
                    if instructions_list:
                        instruction = instructions_list[0]

            # Fallback to legacy .txt format
            instruction_path = d / "instruction.txt"
            if not instructions_list and instruction_path.exists():
                with open(instruction_path, "r", encoding="utf-8") as f:
                    instruction = f.read()
                    instructions_list = [instruction]
                    instruction_items = [
                        _with_instruction_item_payload(instruction)
                    ]

            # Check for 3D model(s) - collect all providers
            model_path = None
            model_providers = []
            model_dir = MODELS_DIR / d.name
            if model_dir.exists():
                for glb in model_dir.glob("*.glb"):
                    info = _glb_to_provider_info(glb)
                    model_providers.append(info)
                    if model_path is None:
                        model_path = info["path"]

            # Check for rendered views (provider subdirs or legacy flat PNGs)
            views_dir = TRIPLETS_DIR / d.name / "views"
            has_views = False
            if views_dir.exists():
                # Check provider subdirs
                for _sub in views_dir.iterdir():
                    if _sub.is_dir() and any(_sub.glob("*.png")):
                        has_views = True
                        break
                # Fallback: legacy flat PNGs
                if not has_views:
                    has_views = any(views_dir.glob("*.png"))

            images.append(
                normalize_image_record(
                    image_id=d.name,
                    image_path=_rel_path(image_path),
                    meta=meta,
                    instruction=instruction,
                    instructions_list=instructions_list,
                    instruction_items=instruction_items,
                    model_path=model_path,
                    model_providers=model_providers,
                    has_views=has_views,
                )
            )
    return images


def get_all_images() -> list:
    """Get all images from pipeline/images with a short in-memory cache."""
    return _get_cached_pipeline_listing("images", _scan_all_images)


def _build_image_index_entry(image_dir: Path) -> dict | None:
    """Build a lightweight image index entry for a single image directory."""
    image_path = image_dir / "image.png"
    if not image_path.exists():
        return None

    meta_path = image_dir / "meta.json"
    meta = safe_load_json(meta_path, {}) if meta_path.exists() else {}

    # Detect schema (mirrors normalize_image_record logic)
    if "parent_id" in meta:
        schema = "variant_image"
    elif "object_name" in meta or "experiment_id" in meta:
        schema = "experiment_image"
    elif "subject" in meta or "generated_at" in meta or "prompt" in meta:
        schema = "legacy_image"
    else:
        return None  # unknown schema — skip

    subject, display_subject = _normalize_image_subject(image_dir.name, meta)
    created_at = meta.get("generated_at") or meta.get("created_at")
    if isinstance(created_at, str):
        created_at = created_at.strip() or None

    instruction = None
    instruction_json_path = image_dir / "instructions.json"
    if instruction_json_path.exists():
        try:
            with open(instruction_json_path, "r", encoding="utf-8") as f:
                raw_list = json.load(f)
            if isinstance(raw_list, list) and raw_list:
                first = raw_list[0]
                if isinstance(first, dict):
                    instruction = first.get("text") or first.get("instruction")
                elif isinstance(first, str):
                    instruction = first
        except Exception:
            pass
    if not instruction:
        txt_path = image_dir / "instruction.txt"
        if txt_path.exists():
            try:
                instruction = txt_path.read_text(encoding="utf-8").strip() or None
            except Exception:
                pass

    model_path = None
    model_dir = MODELS_DIR / image_dir.name
    if model_dir.exists():
        for glb in model_dir.glob("*.glb"):
            model_path = _rel_path(glb)
            break

    return {
        "id": image_dir.name,
        "path": _rel_path(image_path),
        "schema": schema,
        "subject": subject,
        "display_subject": display_subject,
        "prompt": meta.get("prompt"),
        "instruction": instruction,
        "model_path": model_path,
        "created_at": created_at,
    }


# View order: front, back, right, left, top, bottom
VIEW_ORDER = ["front", "back", "right", "left", "top", "bottom"]


def sort_views(views: list) -> list:
    """Sort views in standard order: front, back, left, right, top, bottom."""
    order_map = {name: i for i, name in enumerate(VIEW_ORDER)}
    return sorted(views, key=lambda v: order_map.get(v["name"], 99))


def _ensure_target_render_grid(target_model_id: str, provider_id: str) -> str | None:
    if not isinstance(target_model_id, str) or not target_model_id.strip():
        return None
    if not isinstance(provider_id, str) or not provider_id.strip():
        return None

    render_dir = TRIPLETS_DIR / target_model_id / "views" / provider_id
    if not render_dir.exists() or not render_dir.is_dir():
        return None

    for view_name in VIEW_ORDER:
        if not (render_dir / f"{view_name}.png").is_file():
            return None

    output_path = render_dir / "target_render_grid.png"
    if not output_path.exists():
        try:
            stitcher = ViewStitcher()
            stitcher.stitch_views(
                views_dir=render_dir,
                output_path=output_path,
                view_names=VIEW_ORDER,
                pad_to_square=True,
            )
        except OSError:
            # OSS FUSE mount may report "Device or resource busy" when
            # another process is writing to the same directory.  Return
            # None so the caller degrades gracefully instead of crashing
            # the entire batch request.
            return None

    return str(_rel_path(output_path)).replace("\\", "/")


def _resolve_source_views_dir(model_id: str, source_provider_id: str) -> Path:
    if not isinstance(source_provider_id, str) or not source_provider_id.strip():
        raise ValueError("source_provider_id must be a non-empty string")
    views_base = TRIPLETS_DIR / model_id / "views"
    if not views_base.exists():
        raise FileNotFoundError(f"views directory not found: {views_base}")
    provider_dir = views_base / source_provider_id
    if not provider_dir.exists():
        raise FileNotFoundError(f"source provider views not found: {provider_dir}")
    if not any(provider_dir.glob("*.png")):
        raise FileNotFoundError(f"source provider views has no png files: {provider_dir}")
    return provider_dir


def _parse_created_at_to_epoch(created_at: str) -> float:
    """Parse ISO datetime to epoch seconds for robust sorting."""
    if not isinstance(created_at, str) or not created_at.strip():
        return float("-inf")
    raw = created_at.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, OSError, OverflowError):
        return float("-inf")


def sort_edit_batches_by_created_at_desc(batches: list) -> list:
    """Sort edit batches by created_at (newest first), then edit_id."""
    return sorted(
        batches,
        key=lambda b: (
            _parse_created_at_to_epoch(str(b.get("created_at") or "")),
            str(b.get("created_at") or ""),
            str(b.get("edit_id") or ""),
        ),
        reverse=True,
    )


def _is_temp_views_dir(dir_name: str, semantic_tmp_dir_name: str) -> bool:
    """Return True for non-provider temp/debug directories under views/."""
    return dir_name.startswith("_") or dir_name == semantic_tmp_dir_name


def _collect_canonical_view_payloads(
    views_dir: Path, provider_id: str, provider_name: str
) -> list:
    """Collect canonical six-view PNG payloads from a directory."""
    payloads = []
    for view_file in views_dir.glob("*.png"):
        view_name = view_file.stem
        if view_name not in VIEW_ORDER:
            continue
        payloads.append(
            {
                "name": view_name,
                "path": str(_rel_path(view_file)).replace("\\", "/"),
                "provider_id": provider_id,
                "provider": provider_name,
                "cache_key": int(view_file.stat().st_mtime_ns),
            }
        )
    return sort_views(payloads)


def _iter_source_model_dirs():
    """Iterate source model directories only, newest-first."""
    if not MODELS_DIR.exists():
        return
    for model_dir in sorted(MODELS_DIR.iterdir(), reverse=True):
        if not model_dir.is_dir():
            continue
        if "_edit_" in model_dir.name:
            continue
        yield model_dir


def _scan_source_model_ids() -> list[str]:
    """Collect source model IDs only, without reading per-model metadata."""
    return [model_dir.name for model_dir in _iter_source_model_dirs()]


def get_source_model_ids() -> list[str]:
    """Get source model IDs with a short in-memory cache."""
    return _get_cached_pipeline_listing("source_model_ids", _scan_source_model_ids)


def _get_instruction_summary(model_id: str) -> tuple[bool, int]:
    """Return whether source instructions exist and how many there are."""
    instruction_json_path = IMAGES_DIR / model_id / "instructions.json"
    if instruction_json_path.exists():
        try:
            with open(instruction_json_path, "r", encoding="utf-8") as f:
                instr_data = json.load(f)
            if isinstance(instr_data, list) and len(instr_data) > 0:
                return True, len(instr_data)
        except Exception:
            pass

    instruction_txt_path = IMAGES_DIR / model_id / "instruction.txt"
    if instruction_txt_path.exists():
        return True, 1
    return False, 0


def _get_model_views_summary(
    model_id: str, semantic_tmp_dir_name: str
) -> tuple[bool, list, dict]:
    """Return rendered-view summary for the source model."""
    views_dir = TRIPLETS_DIR / model_id / "views"
    rendered_views_by_provider = {}
    if views_dir.exists():
        for subdir in sorted(views_dir.iterdir()):
            if not subdir.is_dir():
                continue
            pid = subdir.name
            if _is_temp_views_dir(pid, semantic_tmp_dir_name):
                continue
            pviews = _collect_canonical_view_payloads(
                subdir, pid, PROVIDER_ID_TO_NAME.get(pid, pid)
            )
            if pviews:
                rendered_views_by_provider[pid] = pviews

        legacy_views = _collect_canonical_view_payloads(views_dir, "legacy", "legacy")
        if legacy_views and not rendered_views_by_provider:
            rendered_views_by_provider["legacy"] = legacy_views

    rendered_views = []
    if rendered_views_by_provider:
        first_pid = next(iter(rendered_views_by_provider))
        rendered_views = rendered_views_by_provider[first_pid]
    return len(rendered_views_by_provider) > 0, rendered_views, rendered_views_by_provider


def _scan_source_model_edit_summaries(model_id: str) -> list[dict]:
    """Collect lightweight edit-batch summaries for a source model."""
    edited_base = TRIPLETS_DIR / model_id / "edited"
    if not edited_base.exists():
        return []

    edit_summaries = []
    for batch_dir in sorted(edited_base.iterdir(), reverse=True):
        if not batch_dir.is_dir():
            continue

        batch_meta_path = batch_dir / "meta.json"
        if not batch_meta_path.exists():
            continue

        batch_meta = safe_load_json(batch_meta_path, {})
        if not batch_meta or not is_edit_batch_allowed(batch_meta):
            continue

        target_model_dir = MODELS_DIR / f"{model_id}_edit_{batch_dir.name}"
        has_target_3d = target_model_dir.exists() and any(target_model_dir.glob("*.glb"))

        edit_artifacts = batch_meta.get("edit_artifacts", {})
        if not isinstance(edit_artifacts, dict):
            edit_artifacts = {}
        target_info = edit_artifacts.get("target_image_grid", {})
        mask_info = edit_artifacts.get("edit_mask", {})
        target_image_grid = (
            target_info.get("path") if isinstance(target_info, dict) else None
        )
        edit_mask = mask_info.get("path") if isinstance(mask_info, dict) else None
        ready_pair = (
            has_target_3d
            and isinstance(target_image_grid, str)
            and bool(target_image_grid)
            and isinstance(edit_mask, str)
            and bool(edit_mask)
        )

        edit_summaries.append(
            {
                "edit_id": batch_meta.get("edit_id", batch_dir.name),
                "has_target_3d": has_target_3d,
                "ready_pair": ready_pair,
                "created_at": batch_meta.get("generated_at"),
            }
        )

    return sort_edit_batches_by_created_at_desc(edit_summaries)


def _build_model_index_entry(
    model_dir: Path, semantic_tmp_dir_name: str
) -> dict | None:
    """Build a lightweight model entry used for TOCs, filters, and pagination."""
    source_3d_models = [_glb_to_provider_info(glb) for glb in model_dir.glob("*.glb")]
    if not source_3d_models:
        return None

    model_id = model_dir.name
    meta_path = model_dir / "meta.json"
    meta = safe_load_json(meta_path, {}) if meta_path.exists() else {}
    has_views, rendered_views, _ = _get_model_views_summary(
        model_id, semantic_tmp_dir_name
    )
    has_instructions, instructions_count = _get_instruction_summary(model_id)
    edit_summaries = _scan_source_model_edit_summaries(model_id)
    edit_count = len(edit_summaries)
    target_ready_count = sum(1 for item in edit_summaries if item["has_target_3d"])
    ready_pair_count = sum(1 for item in edit_summaries if item["ready_pair"])
    object_context = _get_image_object_context(model_id)

    return {
        "id": model_id,
        "provider": meta.get("provider"),
        "created_at": meta.get("generated_at"),
        "has_views": has_views,
        "rendered_views_count": len(rendered_views),
        "first_rendered_view_path": (
            rendered_views[0]["path"] if rendered_views else None
        ),
        "has_instructions": has_instructions,
        "instructions_count": instructions_count,
        "has_edits": edit_count > 0,
        "edit_count": edit_count,
        "target_ready_count": target_ready_count,
        "ready_pair_count": ready_pair_count,
        "edits_without_target": edit_count > 0 and target_ready_count < edit_count,
        "edited_batches": [
            {
                "edit_id": item["edit_id"],
                "has_target_3d": item["has_target_3d"],
            }
            for item in edit_summaries
        ],
        "category_name": object_context["category_name"],
        "object_name": object_context["object_name"],
        "path": source_3d_models[0]["path"],
    }


def _scan_all_models_index() -> list[dict]:
    """Get lightweight source-model entries for list pages and filters."""
    config = load_config()
    semantic_tmp_dir_name = config.render.semantic_alignment.temp_dir_name

    items = []
    for model_dir in _iter_source_model_dirs():
        entry = _build_model_index_entry(model_dir, semantic_tmp_dir_name)
        if entry:
            items.append(entry)
    return items


def get_all_models_index() -> list[dict]:
    """Get lightweight model entries — from SQLite index when available, else scan."""
    if _pipeline_index is not None:
        items = _pipeline_index.get_models_index()
        if items:
            return items
    return _get_cached_pipeline_listing("models_index", _scan_all_models_index)


def _load_model_payload_by_id_live(model_id: str) -> dict | None:
    """Live filesystem scan for a single model payload (no cache/DB lookup)."""
    config = load_config()
    semantic_tmp_dir_name = config.render.semantic_alignment.temp_dir_name
    model_dir = MODELS_DIR / model_id
    if not model_dir.exists() or not model_dir.is_dir():
        return None

    source_3d_models = [_glb_to_provider_info(glb) for glb in model_dir.glob("*.glb")]
    if not source_3d_models:
        return None

    meta_path = model_dir / "meta.json"
    meta = safe_load_json(meta_path, {}) if meta_path.exists() else {}
    has_views, rendered_views, rendered_views_by_provider = _get_model_views_summary(
        model_id, semantic_tmp_dir_name
    )

    edited_batches = []
    failed_edit_batches = []
    edited_base = TRIPLETS_DIR / model_id / "edited"
    if edited_base.exists():
        for batch_dir in sorted(edited_base.iterdir(), reverse=True):
            if not batch_dir.is_dir():
                continue
            batch_meta_path = batch_dir / "meta.json"
            if not batch_meta_path.exists():
                continue
            raw_batch_meta = safe_load_json(batch_meta_path, {})
            if not raw_batch_meta:
                continue

            batch_meta = _with_edit_meta_instruction_payload(raw_batch_meta)
            batch_views = []
            meta_view_names = batch_meta.get("view_names")
            if isinstance(meta_view_names, list) and meta_view_names:
                candidate_names = meta_view_names
            else:
                candidate_names = VIEW_ORDER

            for view_name in candidate_names:
                png = batch_dir / f"{view_name}.png"
                if png.exists():
                    batch_views.append(
                        {
                            "name": png.stem,
                            "path": str(_rel_path(png)).replace("\\", "/"),
                        }
                    )
            batch_views = sort_views(batch_views)

            target_model_id = f"{model_id}_edit_{batch_dir.name}"
            target_model_dir = MODELS_DIR / target_model_id
            target_3d_models = []
            target_quality_check = {}
            target_quality_checks_by_provider = {}
            if target_model_dir.exists():
                target_meta_path = target_model_dir / "meta.json"
                if target_meta_path.exists():
                    try:
                        with open(target_meta_path, "r", encoding="utf-8") as f:
                            target_meta = json.load(f)
                        target_quality_check = target_meta.get(
                            "target_quality_check", {}
                        )
                        raw_checks = target_meta.get(
                            "target_quality_checks_by_provider", {}
                        )
                        if isinstance(raw_checks, dict):
                            target_quality_checks_by_provider = raw_checks
                    except Exception:
                        pass

                glb_files = list(target_model_dir.glob("*.glb"))
                single_target_glb = len(glb_files) == 1
                for glb in glb_files:
                    info = _glb_to_provider_info(glb)
                    info["id"] = target_model_id
                    provider_id = info.get("provider_id")

                    provider_tqc = {}
                    if (
                        isinstance(target_quality_checks_by_provider, dict)
                        and isinstance(provider_id, str)
                        and provider_id
                    ):
                        provider_tqc = target_quality_checks_by_provider.get(
                            provider_id, {}
                        )

                    if not provider_tqc and isinstance(target_quality_check, dict):
                        legacy_provider_id = target_quality_check.get("provider_id")
                        if legacy_provider_id == provider_id:
                            provider_tqc = target_quality_check
                        elif single_target_glb and not target_quality_checks_by_provider:
                            provider_tqc = target_quality_check

                    info["target_quality_check"] = (
                        provider_tqc if isinstance(provider_tqc, dict) else {}
                    )
                    info["target_render_grid"] = _ensure_target_render_grid(
                        target_model_id, provider_id
                    )
                    target_3d_models.append(info)

            target_3d = target_3d_models[0] if target_3d_models else None

            edit_artifacts = batch_meta.get("edit_artifacts", {})
            if not isinstance(edit_artifacts, dict):
                edit_artifacts = {}
            before_info = edit_artifacts.get("before_image_grid", {})
            target_info = edit_artifacts.get("target_image_grid", {})
            mask_info = edit_artifacts.get("edit_mask", {})
            before_image_grid = (
                before_info.get("path") if isinstance(before_info, dict) else None
            )
            target_image_grid = (
                target_info.get("path") if isinstance(target_info, dict) else None
            )
            edit_mask = mask_info.get("path") if isinstance(mask_info, dict) else None
            per_view_masks = (
                mask_info.get("view_paths")
                if isinstance(mask_info, dict)
                and isinstance(mask_info.get("view_paths"), dict)
                else {}
            )
            mask_missing = _has_missing_mask_artifacts(batch_dir, batch_meta)

            batch_payload = {
                "edit_id": batch_meta.get("edit_id", batch_dir.name),
                "instruction": batch_meta.get("instruction", ""),
                "instruction_display_text": batch_meta.get(
                    "instruction_display_text", batch_meta.get("instruction", "")
                ),
                "instruction_display_source": batch_meta.get(
                    "instruction_display_source"
                ),
                "instruction_display_status": batch_meta.get(
                    "instruction_display_status"
                ),
                "instruction_text_original": batch_meta.get(
                    "instruction_text_original"
                ),
                "instruction_text_effective": batch_meta.get(
                    "instruction_text_effective"
                ),
                "instruction_text_candidate_rewrite": batch_meta.get(
                    "instruction_text_candidate_rewrite"
                ),
                "instruction_rewrite_reason": batch_meta.get(
                    "instruction_rewrite_reason"
                ),
                "stage1_relabel_result": batch_meta.get("stage1_relabel_result"),
                "source_provider_id": batch_meta.get("source_provider_id"),
                "views": batch_views,
                "editor_metadata": batch_meta.get("editor_metadata", {}),
                "created_at": batch_meta.get("generated_at"),
                "before_image_grid": before_image_grid,
                "target_image_grid": target_image_grid,
                "target_render_grid": (
                    target_3d.get("target_render_grid") if isinstance(target_3d, dict) else None
                ),
                "target_render_grid_provider": (
                    target_3d.get("provider") if isinstance(target_3d, dict) else None
                ),
                "edit_mask": edit_mask,
                "per_view_masks": per_view_masks,
                "mask_missing": mask_missing,
                "target_3d": target_3d,
                "target_3d_models": target_3d_models,
                "edit_status": get_effective_edit_status(batch_meta),
                "quality_check": batch_meta.get("quality_check", {}),
                "target_quality_check": target_quality_check,
                "target_quality_checks_by_provider": target_quality_checks_by_provider,
            }

            if is_edit_batch_allowed(batch_meta):
                edited_batches.append(batch_payload)
            else:
                failed_edit_batches.append(batch_payload)

    edited_batches = sort_edit_batches_by_created_at_desc(edited_batches)
    failed_edit_batches = sort_edit_batches_by_created_at_desc(failed_edit_batches)
    has_instructions, instructions_count = _get_instruction_summary(model_id)
    object_context = _get_image_object_context(model_id)

    # Count edits where any provider's stage2 check passed
    stage2_passed_count = sum(
        1
        for batch in edited_batches
        if any(
            isinstance(check, dict) and check.get("status") == "passed"
            for check in (batch.get("target_quality_checks_by_provider") or {}).values()
        )
    )

    return {
        "id": model_id,
        "path": source_3d_models[0]["path"],
        "filename": source_3d_models[0]["filename"],
        "provider": meta.get("provider"),
        "source_image": _rel_path(_resolve_api_path(meta.get("source_image", "")))
        if meta.get("source_image")
        else None,
        "source_3d_models": source_3d_models,
        "created_at": meta.get("generated_at"),
        "rendered_views": rendered_views,
        "rendered_views_by_provider": rendered_views_by_provider,
        "has_views": has_views,
        "edited_batches": edited_batches,
        "failed_edit_batches": failed_edit_batches,
        "has_edits": len(edited_batches) > 0,
        "has_failed_edits": len(failed_edit_batches) > 0,
        "has_instructions": has_instructions,
        "instructions_count": instructions_count,
        "stage2_passed_count": stage2_passed_count,
        "object_name": object_context["object_name"],
        "category_name": object_context["category_name"],
    }


def _load_model_payload_by_id(model_id: str) -> dict | None:
    """Load full payload for a single model — from SQLite index when available."""
    if _pipeline_index is not None:
        payload = _pipeline_index.get_model_payload(model_id)
        if payload is not None:
            return payload
    return _load_model_payload_by_id_live(model_id)


def _scan_all_models() -> list:
    """Get all source 3D models from pipeline/models_src."""
    config = load_config()
    semantic_tmp_dir_name = config.render.semantic_alignment.temp_dir_name

    models = []
    for d in _iter_source_model_dirs():
            meta_path = d / "meta.json"
            meta = safe_load_json(meta_path, {}) if meta_path.exists() else {}

            # Check for rendered views — grouped by provider subdirectory
            views_dir = TRIPLETS_DIR / d.name / "views"
            rendered_views_by_provider = {}  # { provider_id: [{"name","path","provider_id"}, ...] }
            if views_dir.exists():
                # New structure: views/{provider_id}/*.png
                for subdir in sorted(views_dir.iterdir()):
                    if subdir.is_dir():
                        pid = subdir.name
                        if _is_temp_views_dir(pid, semantic_tmp_dir_name):
                            continue
                        pviews = _collect_canonical_view_payloads(
                            subdir, pid, PROVIDER_ID_TO_NAME.get(pid, pid)
                        )
                        if pviews:
                            rendered_views_by_provider[pid] = pviews
                # Legacy structure: views/*.png (flat)
                legacy_views = _collect_canonical_view_payloads(
                    views_dir, "legacy", "legacy"
                )
                # Always prefer provider subdirs. Legacy flat views are fallback only.
                if legacy_views and not rendered_views_by_provider:
                    rendered_views_by_provider["legacy"] = legacy_views

            # Flatten to first-provider views for backward compat
            rendered_views = []
            if rendered_views_by_provider:
                first_pid = next(iter(rendered_views_by_provider))
                rendered_views = rendered_views_by_provider[first_pid]

            # Check for edited view batches
            edited_batches = []
            failed_edit_batches = []
            edited_base = TRIPLETS_DIR / d.name / "edited"
            if edited_base.exists():
                for batch_dir in sorted(edited_base.iterdir(), reverse=True):
                    if not batch_dir.is_dir():
                        continue
                    batch_meta_path = batch_dir / "meta.json"
                    if batch_meta_path.exists():
                        raw_batch_meta = safe_load_json(batch_meta_path, {})
                        if not raw_batch_meta:
                            continue
                        batch_meta = _with_edit_meta_instruction_payload(
                            raw_batch_meta
                        )
                        batch_views = []
                        # Prefer explicit view_names from meta; fallback to known view set
                        meta_view_names = batch_meta.get("view_names")
                        if isinstance(meta_view_names, list) and meta_view_names:
                            candidate_names = meta_view_names
                        else:
                            candidate_names = [
                                "front",
                                "back",
                                "right",
                                "left",
                                "top",
                                "bottom",
                            ]
                        for view_name in candidate_names:
                            png = batch_dir / f"{view_name}.png"
                            if png.exists():
                                batch_views.append(
                                    {
                                        "name": png.stem,
                                        "path": str(_rel_path(png)).replace("\\", "/"),
                                    }
                                )
                        batch_views = sort_views(batch_views)

                        # Check if target 3D model exists for this edit
                        target_model_id = f"{d.name}_edit_{batch_dir.name}"
                        target_model_dir = MODELS_DIR / target_model_id
                        target_3d_models = []  # List of all Target 3D models (multiple providers)
                        target_quality_check = {}  # Legacy single Stage 2 result
                        target_quality_checks_by_provider = {}
                        if target_model_dir.exists():
                            # Load target_quality_check from target model's meta.json
                            target_meta_path = target_model_dir / "meta.json"
                            if target_meta_path.exists():
                                try:
                                    with open(target_meta_path) as f:
                                        target_meta = json.load(f)
                                    target_quality_check = target_meta.get(
                                        "target_quality_check", {}
                                    )
                                    raw_checks = target_meta.get(
                                        "target_quality_checks_by_provider", {}
                                    )
                                    if isinstance(raw_checks, dict):
                                        target_quality_checks_by_provider = raw_checks
                                except Exception:
                                    pass

                            glb_files = list(target_model_dir.glob("*.glb"))
                            single_target_glb = len(glb_files) == 1
                            for glb in glb_files:
                                info = _glb_to_provider_info(glb)
                                info["id"] = target_model_id
                                provider_id = info.get("provider_id")

                                provider_tqc = {}
                                if (
                                    isinstance(target_quality_checks_by_provider, dict)
                                    and isinstance(provider_id, str)
                                    and provider_id
                                ):
                                    provider_tqc = (
                                        target_quality_checks_by_provider.get(
                                            provider_id, {}
                                        )
                                    )

                                # Backward compatibility: if only legacy single result
                                # exists, bind it to matching provider_id.
                                if not provider_tqc and isinstance(
                                    target_quality_check, dict
                                ):
                                    legacy_provider_id = target_quality_check.get(
                                        "provider_id"
                                    )
                                    if legacy_provider_id == provider_id:
                                        provider_tqc = target_quality_check
                                    elif (
                                        single_target_glb
                                        and not target_quality_checks_by_provider
                                    ):
                                        provider_tqc = target_quality_check

                                info["target_quality_check"] = (
                                    provider_tqc
                                    if isinstance(provider_tqc, dict)
                                    else {}
                                )
                                info["target_render_grid"] = _ensure_target_render_grid(
                                    target_model_id, provider_id
                                )
                                target_3d_models.append(info)

                        # For backward compatibility, set target_3d to first model if exists
                        target_3d = target_3d_models[0] if target_3d_models else None

                        edit_artifacts = batch_meta.get("edit_artifacts", {})
                        if not isinstance(edit_artifacts, dict):
                            edit_artifacts = {}
                        before_info = edit_artifacts.get("before_image_grid", {})
                        target_info = edit_artifacts.get("target_image_grid", {})
                        mask_info = edit_artifacts.get("edit_mask", {})
                        before_image_grid = (
                            before_info.get("path")
                            if isinstance(before_info, dict)
                            else None
                        )
                        target_image_grid = (
                            target_info.get("path")
                            if isinstance(target_info, dict)
                            else None
                        )
                        edit_mask = (
                            mask_info.get("path") if isinstance(mask_info, dict) else None
                        )
                        per_view_masks = (
                            mask_info.get("view_paths")
                            if isinstance(mask_info, dict)
                            and isinstance(mask_info.get("view_paths"), dict)
                            else {}
                        )
                        mask_missing = _has_missing_mask_artifacts(batch_dir, batch_meta)

                        batch_payload = {
                            "edit_id": batch_meta.get("edit_id", batch_dir.name),
                            "instruction": batch_meta.get("instruction", ""),
                            "instruction_display_text": batch_meta.get(
                                "instruction_display_text", batch_meta.get("instruction", "")
                            ),
                            "instruction_display_source": batch_meta.get(
                                "instruction_display_source"
                            ),
                            "instruction_display_status": batch_meta.get(
                                "instruction_display_status"
                            ),
                            "instruction_text_original": batch_meta.get(
                                "instruction_text_original"
                            ),
                            "instruction_text_effective": batch_meta.get(
                                "instruction_text_effective"
                            ),
                            "instruction_text_candidate_rewrite": batch_meta.get(
                                "instruction_text_candidate_rewrite"
                            ),
                            "instruction_rewrite_reason": batch_meta.get(
                                "instruction_rewrite_reason"
                            ),
                            "stage1_relabel_result": batch_meta.get(
                                "stage1_relabel_result"
                            ),
                            "source_provider_id": batch_meta.get(
                                "source_provider_id"
                            ),  # which rendered views used
                            "views": batch_views,
                            "editor_metadata": batch_meta.get("editor_metadata", {}),
                            "created_at": batch_meta.get("generated_at"),
                            "before_image_grid": before_image_grid,
                            "target_image_grid": target_image_grid,
                            "target_render_grid": (
                                target_3d.get("target_render_grid")
                                if isinstance(target_3d, dict)
                                else None
                            ),
                            "target_render_grid_provider": (
                                target_3d.get("provider") if isinstance(target_3d, dict) else None
                            ),
                            "edit_mask": edit_mask,
                            "per_view_masks": per_view_masks,
                            "mask_missing": mask_missing,
                            "target_3d": target_3d,  # First model for backward compatibility
                            "target_3d_models": target_3d_models,  # All models by provider
                            "edit_status": get_effective_edit_status(batch_meta),
                            "quality_check": batch_meta.get("quality_check", {}),
                            "target_quality_check": target_quality_check,
                            "target_quality_checks_by_provider": target_quality_checks_by_provider,
                        }

                        if is_edit_batch_allowed(batch_meta):
                            edited_batches.append(batch_payload)
                        else:
                            failed_edit_batches.append(batch_payload)

            edited_batches = sort_edit_batches_by_created_at_desc(edited_batches)
            failed_edit_batches = sort_edit_batches_by_created_at_desc(
                failed_edit_batches
            )

            # Collect all source 3D models (multiple providers)
            source_3d_models = []
            for glb in d.glob("*.glb"):
                source_3d_models.append(_glb_to_provider_info(glb))

            if not source_3d_models:
                continue  # Skip directories without any GLB files

            # Check for instructions from source image
            has_instructions = False
            instructions_count = 0
            instruction_json_path = IMAGES_DIR / d.name / "instructions.json"
            if instruction_json_path.exists():
                try:
                    with open(instruction_json_path, "r", encoding="utf-8") as f:
                        instr_data = json.load(f)
                    if isinstance(instr_data, list) and len(instr_data) > 0:
                        has_instructions = True
                        instructions_count = len(instr_data)
                except Exception:
                    pass

            object_context = _get_image_object_context(d.name)

            # One entry per model directory (not per GLB file)
            models.append(
                {
                    "id": d.name,
                    "path": source_3d_models[0][
                        "path"
                    ],  # First GLB for backward compat
                    "filename": source_3d_models[0]["filename"],
                    "provider": meta.get("provider"),
                    "source_image": _rel_path(
                        _resolve_api_path(meta.get("source_image", ""))
                    )
                    if meta.get("source_image")
                    else None,
                    "source_3d_models": source_3d_models,  # All providers
                    "created_at": meta.get("generated_at"),
                    "rendered_views": rendered_views,  # backward compat: first provider
                    "rendered_views_by_provider": rendered_views_by_provider,  # all providers
                    "has_views": len(rendered_views_by_provider) > 0,
                    "edited_batches": edited_batches,
                    "failed_edit_batches": failed_edit_batches,
                    "has_edits": len(edited_batches) > 0,
                    "has_failed_edits": len(failed_edit_batches) > 0,
                    "has_instructions": has_instructions,
                    "instructions_count": instructions_count,
                    "object_name": object_context["object_name"],
                    "category_name": object_context["category_name"],
                }
            )
    return models


def get_all_models() -> list:
    """Get all source 3D models from pipeline/models_src with a short in-memory cache."""
    return _get_cached_pipeline_listing("models", _scan_all_models)


def create_task(task_type: str, params: dict) -> str:
    """Create a new async task."""
    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "type": task_type,
        "params": params,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "result": None,
        "error": None,
    }

    with task_lock:
        task_store[task_id] = task
        # Also persist to file
        with tasks_file_lock:
            tasks = load_jsonl(TASKS_FILE)
            tasks.append(task)
            save_jsonl(TASKS_FILE, tasks[-100:])  # Keep last 100

    # Start background processing
    thread = threading.Thread(target=process_task, args=(task_id,))
    thread.daemon = True
    thread.start()

    return task_id


def process_task(task_id: str):
    """Process a task in background thread."""
    task = task_store.get(task_id)
    if not task:
        return

    task_type = task["type"]
    params = task.get("params", {})

    # Acquire appropriate semaphore based on task type
    semaphore = None
    if task_type == "gen3d":
        provider = require_param(params, "provider", "task")
        semaphore = GEN3D_SEMAPHORES.get(provider)
    elif task_type == "render":
        semaphore = RENDER_SEMAPHORE
    elif task_type in ("edit", "edit_view", "t2i"):
        semaphore = EDIT_SEMAPHORE
    elif task_type == "materialize_missing_masks":
        semaphore = MASK_BACKFILL_SEMAPHORE
    elif task_type in ("refresh_model_dreamsim", "refresh_all_models_dreamsim"):
        # DreamSim refresh is very sensitive to remote FS I/O burst. Reuse the
        # Stage-2 semaphore as a global gate for these long-running refresh tasks.
        semaphore = RECON_QUALITY_CHECK_SEMAPHORE

    if semaphore:
        semaphore.acquire()

    try:
        task["status"] = "running"

        if task_type == "t2i":
            result = run_t2i_task(params)
        elif task_type == "gen3d":
            result = run_gen3d_task(params, task_id)
        elif task_type == "instruction":
            result = run_instruction_task(params)
        elif task_type == "render":
            result = run_render_task(params)
        elif task_type == "edit":
            result = run_edit_task(params)
        elif task_type == "edit_view":
            result = run_edit_view_task(params)
        elif task_type == "refresh_model_dreamsim":
            result = run_refresh_model_dreamsim_task(params)
        elif task_type == "refresh_all_models_dreamsim":
            result = run_refresh_all_models_dreamsim_task(params)
        elif task_type == "materialize_missing_masks":
            result = run_materialize_missing_masks_task(params)
        elif task_type == "run_full_experiment":
            result = run_full_experiment_task(params)
        else:
            raise ValueError(f"Unknown task type: {task_type}")

        task["status"] = "completed"
        task["result"] = result

    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)

    finally:
        if semaphore:
            semaphore.release()

    task["completed_at"] = datetime.now().isoformat()

    # 更新持久化文件
    update_task_in_file(task_id, task)
    clear_pipeline_listing_cache()
    # Best-effort: refresh affected model in SQLite index
    _model_id = task.get("params", {}).get("model_id")
    if _model_id:
        _refresh_model_in_index(_model_id)


def run_t2i_task(params: dict) -> dict:
    """Run T2I generation for a single prompt."""
    from core.image.generator import T2IGenerator

    config = load_config()
    prompt_id = params["prompt_id"]
    prompt_text = params["prompt"]

    output_dir = IMAGES_DIR / prompt_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "image.png"

    with T2IGenerator(config.get_image_provider_config()) as gen:
        gen.generate_image(prompt_text, str(output_path))

    # Save meta
    # Get provider config safely
    provider_config = config.get_image_provider_config()

    # Try to get model_dump if pydantic model, else stringify
    config_snapshot = {}
    if hasattr(provider_config, "model_dump"):
        config_snapshot = provider_config.model_dump()
    elif hasattr(provider_config, "__dict__"):
        config_snapshot = provider_config.__dict__
    else:
        config_snapshot = str(provider_config)

    meta = {
        "id": prompt_id,
        "prompt": prompt_text,
        "subject": params.get("subject"),  # Optional: subject metadata
        "generated_at": datetime.now().isoformat(),
        "provider": getattr(provider_config, "provider", "unknown"),
        "model": getattr(provider_config, "model", "unknown"),
        "config_snapshot": config_snapshot,
    }
    with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # Update prompt status in batch file
    rel_image_path = _rel_path(output_path)
    update_prompt_status(prompt_id, "completed", rel_image_path)

    return {"image_path": rel_image_path}


def run_full_experiment_task(params: dict) -> dict:
    """Run run_full_experiment.py for plan/repair/resume actions."""
    plan_path_raw = params.get("plan_path")
    resume_experiment_id = params.get("resume_experiment_id")
    repair_experiment_id = params.get("repair_experiment_id")
    gpu_id = _resolve_run_full_experiment_gpu_id(params.get("gpu_id"))
    selected_modes = [
        bool(plan_path_raw),
        bool(resume_experiment_id),
        bool(repair_experiment_id),
    ]
    if sum(selected_modes) != 1:
        raise ValueError(
            "Exactly one of plan_path, resume_experiment_id, or repair_experiment_id is required"
        )

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if plan_path_raw:
        plan_path = _require_experiment_plan_path(
            require_param(params, "plan_path", "task")
        )
        plan, _ = _read_experiment_plan_yaml(plan_path)
        errors = _validate_experiment_plan(
            plan,
            allow_legacy_instruction_counts=True,
        )
        if errors:
            raise ValueError("; ".join(errors))

        log_path_raw = params.get("log_path")
        log_path = (
            Path(log_path_raw)
            if log_path_raw
            else _build_run_full_experiment_log_path(plan_path, plan)
        )
        command = [
            PYTHON_INTERPRETER,
            str(RUN_FULL_EXPERIMENT_SCRIPT),
            "--plan",
            str(plan_path),
            "--gpu-id",
            str(gpu_id),
        ]
        cli_command = _build_run_full_experiment_cli_command(
            plan_path,
            log_path,
            gpu_id=gpu_id,
        )
        result_payload = {
            "plan_path": str(plan_path),
            "action": "plan",
        }
    else:
        experiment_id = str(resume_experiment_id or repair_experiment_id).strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        action = "resume" if resume_experiment_id else "repair"
        log_path_raw = params.get("log_path")
        log_path = (
            Path(log_path_raw)
            if log_path_raw
            else _build_run_full_experiment_action_log_path(experiment_id, action)
        )
        command = [
            PYTHON_INTERPRETER,
            str(RUN_FULL_EXPERIMENT_SCRIPT),
            "--resume-experiment-id" if action == "resume" else "--repair-experiment-id",
            experiment_id,
            "--gpu-id",
            str(gpu_id),
        ]
        cli_command = (
            _build_run_full_experiment_resume_cli_command(
                experiment_id,
                log_path,
                gpu_id=gpu_id,
            )
            if action == "resume"
            else _build_run_full_experiment_repair_cli_command(
                experiment_id,
                log_path,
                gpu_id=gpu_id,
            )
        )
        result_payload = {
            "experiment_id": experiment_id,
            "action": action,
        }

    with open(log_path, "w", encoding="utf-8") as log_file:
        process = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    if process.returncode != 0:
        raise RuntimeError(
            "run_full_experiment.py failed with exit code "
            f"{process.returncode}. See log: {log_path}"
        )

    return {
        **result_payload,
        "gpu_id": gpu_id,
        "log_path": str(log_path),
        "cli_command": cli_command,
        "return_code": process.returncode,
    }


def run_gen3d_task(params: dict, task_id: str = None) -> dict:
    """Run 3D generation for a single image.

    Supports task recovery: if remote_task_id is provided, will resume polling
    instead of submitting a new task.

    Args:
        params: Task parameters including image_id, image_path, provider
        task_id: Local task ID (for saving remote_task_id immediately after submit)
    """
    from core.gen3d import GENERATORS, get_model_id

    config = load_config()
    image_id = params["image_id"]
    image_path = _resolve_api_path(params["image_path"])
    provider = require_param(params, "provider", "gen3d task")
    remote_task_id = params.get("remote_task_id")  # Optional: for task recovery

    model_id = get_model_id(provider)
    output_dir = MODELS_DIR / image_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"model_{model_id}.glb"

    generator_class = GENERATORS[provider]
    if provider == "tripo":
        generator = generator_class(
            config.tripo, config.defaults.poll_interval, config.defaults.max_wait_time
        )
    elif provider == "hunyuan":
        generator = generator_class(
            config.hunyuan, config.defaults.poll_interval, config.defaults.max_wait_time
        )
    else:
        raise ValueError(f"Unsupported provider: {provider}")

    with generator:
        if remote_task_id:
            # Resume from existing remote task - just poll and download
            print(f"  [gen3d] Resuming polling for remote task {remote_task_id}")
            result = generator.wait_and_download(remote_task_id, str(output_path))
        else:
            # Submit new task
            remote_task_id = generator.submit_task(str(image_path))
            print(f"  [gen3d] Submitted new task, remote_task_id: {remote_task_id}")

            # Immediately save remote_task_id to task params for recovery
            if task_id:
                with task_lock:
                    task = task_store.get(task_id)
                    if task:
                        task["params"]["remote_task_id"] = remote_task_id
                        update_task_in_file(task_id, task)

            # Now wait for completion
            result = generator.wait_and_download(remote_task_id, str(output_path))

    if not result.output_path:
        raise Exception(result.error_message or "Generation failed")

    # Save meta
    # Get config snapshot
    config_snapshot = {}
    provider_config = None
    if provider == "tripo":
        provider_config = config.tripo
    elif provider == "hunyuan":
        provider_config = config.hunyuan

    if provider_config:
        if hasattr(provider_config, "model_dump"):
            config_snapshot = provider_config.model_dump()
        elif hasattr(provider_config, "__dict__"):
            config_snapshot = provider_config.__dict__

    meta = {
        "id": image_id,
        "provider": provider,
        "model_id": model_id,
        "source_image": params["image_path"],
        "remote_task_id": result.remote_task_id
        or result.task_id,  # Save remote task ID
        "generated_at": datetime.now().isoformat(),
        "config_snapshot": config_snapshot,
    }
    with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return {
        "model_path": _rel_path(output_path),
        "remote_task_id": result.remote_task_id or result.task_id,
    }


def run_instruction_task(params: dict) -> dict:
    """Generate editing instructions for an image.

    Supports two modes:
    - Single mode: Generate one instruction of specified type (remove/replace)
    - Batch mode (default): Generate one REMOVE + one REPLACE instruction (1:1 ratio)
    """
    from core.image.caption import InstructionGenerator
    from utils.prompts import EditType

    config = load_config()
    image_id = params["image_id"]
    image_path = _resolve_api_path(params["image_path"])

    # Mode selection: "batch" (default), "remove", or "replace"
    mode = require_param(params, "mode", "instruction task")

    # Load existing instructions (prefer JSON list with type info)
    existing_instructions = []
    instructions_data = []  # Full data with type info
    json_path = image_path.parent / "instructions.json"
    txt_path = image_path.parent / "instruction.txt"

    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        text = str(
                            item.get("text", item.get("instruction", ""))
                        ).strip()
                        if text:
                            existing_instructions.append(text)
                            instructions_data.append(item)
                    elif isinstance(item, str) and item.strip():
                        existing_instructions.append(item.strip())
                        # Convert legacy string to dict format
                        instructions_data.append(
                            {"text": item.strip(), "type": "unknown"}
                        )
    elif txt_path.exists():
        with open(txt_path, "r", encoding="utf-8") as f:
            legacy_text = f.read().strip()
            if legacy_text:
                existing_instructions = [legacy_text]
                instructions_data = [{"text": legacy_text, "type": "unknown"}]

    new_instructions = []

    with InstructionGenerator(config.qh_mllm) as gen:
        if mode == "batch":
            # Generate 1 REMOVE + 1 REPLACE (1:1 ratio)
            batch_results = gen.generate_batch_instructions(
                str(image_path), avoid_list=existing_instructions
            )
            for result in batch_results:
                inst_text = result["instruction"]
                if inst_text and inst_text not in existing_instructions:
                    new_instructions.append({"text": inst_text, "type": result["type"]})
                    existing_instructions.append(inst_text)
        else:
            # Single instruction mode
            edit_type = EditType.REMOVE if mode == "remove" else EditType.REPLACE
            instruction = gen.generate_instruction(
                str(image_path), edit_type=edit_type, avoid_list=existing_instructions
            )
            instruction = instruction.strip()
            if instruction and instruction not in existing_instructions:
                new_instructions.append({"text": instruction, "type": mode})

    # Merge new instructions with existing
    instructions_data.extend(new_instructions)

    # Save to image directory (JSON list with type info)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(instructions_data, f, ensure_ascii=False, indent=2)

    # Legacy txt file (latest instruction only)
    if new_instructions:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(new_instructions[-1]["text"])

    # Return format compatible with frontend
    all_texts = [
        item.get("text", item) if isinstance(item, dict) else item
        for item in instructions_data
    ]
    latest = (
        new_instructions[-1]["text"]
        if new_instructions
        else (all_texts[-1] if all_texts else "")
    )

    return {
        "instruction": latest,
        "instructions": all_texts,
        "new_count": len(new_instructions),
        "details": instructions_data,  # Full data with types
    }


def run_render_task(params: dict) -> dict:
    """Run multiview rendering for a model."""
    from scripts.run_render_batch import run_blender_render

    model_id = params["model_id"]
    glb_path = _resolve_api_path(params["glb_path"])
    provider_id = params["provider_id"]  # e.g. "tp3", "hy3"

    # Output to provider-specific subdirectory
    output_dir = TRIPLETS_DIR / model_id / "views" / provider_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # run_blender_render will auto-discover blender if path is None
    run_blender_render(str(glb_path), str(output_dir))

    # Save per-provider meta
    import json as _json

    meta = {
        "id": model_id,
        "provider_id": provider_id,
        "source_model": _rel_path(glb_path),
        "rendered_at": datetime.now().isoformat(),
    }
    with open(output_dir / "meta.json", "w") as f:
        _json.dump(meta, f, indent=2)

    # Return list of rendered images
    views = sorted([_rel_path(f) for f in output_dir.glob("*.png")])
    return {"views": views, "provider_id": provider_id}


def run_edit_task(params: dict) -> dict:
    """Run image editing task."""
    from core.image.editor import ImageEditor

    config = load_config()
    image_id = params["image_id"]
    instruction = params["instruction"]
    image_path = _resolve_api_path(params["image_path"])

    # Create variant directory
    variant_id = uuid.uuid4().hex[:8]
    output_dir = IMAGES_DIR / f"{image_id}_v_{variant_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "image.png"

    # Use gemini_response for editing
    # We could allow provider selection if needed
    with ImageEditor(config.gemini_response) as editor:
        editor.edit_image(str(image_path), instruction, str(output_path))

    # Save meta
    meta = {
        "id": f"{image_id}_v_{variant_id}",
        "parent_id": image_id,
        "instruction": instruction,
        "generated_at": datetime.now().isoformat(),
    }
    with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return {"image_path": _rel_path(output_path)}


def run_edit_view_task(params: dict) -> dict:
    """Run editing task on rendered views.

    Supports two modes:
    - single: 先编辑源图像得到 T_image，再用 T_image 引导各视角编辑 (Gemini 2.5 Flash)
    - multiview: Stitch views into 3x2 grid, edit once, then split back (Gemini 3 Pro)
    """
    config = load_config()
    model_id = params["model_id"]
    instruction = params["instruction"]
    edit_mode = require_param(params, "edit_mode", "edit_view task")

    # Determine which views to edit
    from core.image.view_stitcher import VIEW_ORDER

    view_names = params.get("view_names")  # Optional: defaults to VIEW_ORDER if None
    if view_names is None:
        view_names = VIEW_ORDER
    if isinstance(view_names, str):
        view_names = [view_names]

    # views_dir: use the explicit path from params if provided (provider-specific subdir),
    # otherwise fall back to the legacy flat views/ directory
    source_provider_id = params.get("source_provider_id")
    resolved_source_provider_id = source_provider_id
    if "views_dir" in params:
        requested_views_dir = _resolve_api_path(params["views_dir"])
    else:
        requested_views_dir = TRIPLETS_DIR / model_id / "views"

    if (
        (not isinstance(resolved_source_provider_id, str) or not resolved_source_provider_id.strip())
        and requested_views_dir.parent.name == "views"
        and requested_views_dir.name != "views"
    ):
        resolved_source_provider_id = requested_views_dir.name

    if not isinstance(resolved_source_provider_id, str) or not resolved_source_provider_id.strip():
        raise ValueError("source_provider_id is required for edit task")

    views_dir = _resolve_source_views_dir(model_id, resolved_source_provider_id)

    # Create output directory for this edit batch
    edit_id = uuid.uuid4().hex[:8]
    edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
    edited_dir.mkdir(parents=True, exist_ok=True)

    edited_paths = []
    output_paths = []

    def _fmt_path(p: Path) -> str:
        return _rel_path(p).replace("\\", "/")

    if edit_mode == "multiview":
        # Multiview mode: stitch -> edit -> split (Gemini 3 Pro)
        from core.image.multiview_editor import MultiviewEditor

        with MultiviewEditor(
            config.multiview_edit,
            # 传入完整配置 + 任务名，复用统一 guardrail 流程。
            # Pass pipeline config + task name to reuse shared guardrail flow.
            pipeline_config=config,
            task_name="multiview_editing",
        ) as editor:
            result = editor.edit_multiview(
                views_dir=views_dir,
                instruction=instruction,
                output_dir=edited_dir,
                view_names=view_names,
            )
            output_paths = result["output_paths"]
            editor_meta = result["metadata"]

            edited_paths = [_rel_path(p).replace("\\", "/") for p in output_paths]
    else:
        # Single mode: guided editing (Gemini 2.5 Flash)
        from core.image.guided_view_editor import GuidedViewEditor

        source_image_path = IMAGES_DIR / model_id / "image.png"
        if not source_image_path.exists():
            source_image_path = IMAGES_DIR / model_id / "image.jpg"
        if not source_image_path.exists():
            raise FileNotFoundError(f"Source image not found for model {model_id}")

        guided_config = config.qh_image
        guided_config.model = "gemini-2.5-flash-image"

        with GuidedViewEditor(guided_config, mllm_config=config.qh_mllm) as editor:
            result = editor.edit_all_views(
                source_image_path=source_image_path,
                views_dir=views_dir,
                instruction=instruction,
                output_dir=edited_dir,
                view_names=view_names,
                temp_dir=edited_dir / "_tmp",
            )
            output_paths = result["output_paths"]
            editor_meta = result["metadata"]

            edited_paths = [_rel_path(p).replace("\\", "/") for p in output_paths]

    artifact_result = build_edit_artifacts(
        model_id=model_id,
        source_provider_id=resolved_source_provider_id,
        source_views_dir=views_dir,
        edited_dir=edited_dir,
        edit_mode=edit_mode,
        editor_metadata=editor_meta,
        path_formatter=_fmt_path,
        diff_threshold=config.edit_artifacts.diff_threshold,
        opening_kernel_size=config.edit_artifacts.opening_kernel_size,
    )

    # Quality check gate: before -> after six-view collage
    method = config.edit_quality_check.method
    checker_provider, checker_model = get_checker_info(config)
    qc_enabled = config.edit_quality_check.enabled
    edit_status = EDIT_STATUS_PASSED

    quality_check = build_quality_check_meta(
        enabled=qc_enabled,
        result=None,
        method=method,
        checker_provider=checker_provider,
        checker_model=checker_model,
    )
    if qc_enabled:
        print(
            f"[EditQC] START model={model_id} edit_id={edit_id} "
            f"method={method} provider={checker_provider} model={checker_model}"
        )
        semaphore = QUALITY_CHECK_SEMAPHORE
        if semaphore:
            semaphore.acquire()
        try:
            with create_quality_checker(config) as checker:
                qc_result = checker.check(
                    before_views_dir=views_dir,
                    after_views_dir=edited_dir,
                    instruction=instruction,
                    work_root_dir=edited_dir,
                )
            edit_status = qc_result.status
            quality_check = build_quality_check_meta(
                enabled=True,
                result=qc_result,
                method=method,
                checker_provider=checker_provider,
                checker_model=checker_model,
                path_formatter=_fmt_path,
            )
            print(
                f"[EditQC] RESULT model={model_id} edit_id={edit_id} "
                f"status={edit_status} reason={quality_check.get('reason', '')}"
            )
            if quality_check.get("before_grid_path"):
                print(f"[EditQC] before_grid={quality_check.get('before_grid_path')}")
            if quality_check.get("after_grid_path"):
                print(f"[EditQC] after_grid={quality_check.get('after_grid_path')}")
            if quality_check.get("raw_response"):
                print("[EditQC] raw_response:")
                print(quality_check.get("raw_response"))
        except Exception as qc_err:
            edit_status = EDIT_STATUS_ERROR_QUALITY_CHECK
            quality_check = build_quality_check_meta(
                enabled=True,
                result=None,
                method=method,
                checker_provider=checker_provider,
                checker_model=checker_model,
                error_message=str(qc_err),
            )
            # Override status for error case (result=None defaults to passed)
            quality_check["status"] = EDIT_STATUS_ERROR_QUALITY_CHECK
            quality_check["reason"] = "quality check execution error"
            print(
                f"[EditQC] ERROR model={model_id} edit_id={edit_id} "
                f"error={quality_check.get('error_message', '')}"
            )
        finally:
            if semaphore:
                semaphore.release()
    else:
        print(
            f"[EditQC] SKIP model={model_id} edit_id={edit_id} "
            "(edit_quality_check.enabled=false)"
        )

    # Save meta for this edit batch
    edited_views = [Path(p).stem for p in output_paths]
    meta = {
        "edit_id": edit_id,
        "instruction": instruction,
        "model_id": model_id,
        "source_provider_id": resolved_source_provider_id,  # which provider's rendered views were used
        "view_names": view_names,
        "edited_views": edited_views,
        "edit_mode": edit_mode,
        "edited_paths": edited_paths,
        "editor_metadata": editor_meta,
        "edit_status": edit_status,
        "quality_check": quality_check,
        "generated_at": datetime.now().isoformat(),
    }
    meta.update(artifact_result["meta_patch"])
    meta.update(resolve_instruction_display_from_edit_meta(meta))
    meta["instruction"] = meta["instruction_display_text"]
    _write_json_atomic(edited_dir / "meta.json", meta)

    if edit_status == EDIT_STATUS_FAILED_QUALITY:
        raise RuntimeError(
            f"quality check failed: {quality_check.get('reason', '')} (edit_id={edit_id})"
        )
    if edit_status == EDIT_STATUS_ERROR_QUALITY_CHECK:
        raise RuntimeError(
            "quality check execution error: "
            f"{quality_check.get('error_message', '')} (edit_id={edit_id})"
        )

    return {"edit_id": edit_id, "edited_paths": edited_paths}


def run_refresh_model_dreamsim_task(params: dict) -> dict:
    """Recompute Stage-2 LPIPS for every refreshable target under one model."""
    model_id = require_param(params, "model_id", "refresh model dreamsim task")
    result = _refresh_model_dreamsim_for_model(model_id)
    if result["target_count"] == 0:
        raise ValueError(f"No refreshable LPIPS targets found for model {model_id}")
    return result


def _scan_model_dreamsim_refresh_targets(model_id: str) -> tuple[list[dict], list[dict]]:
    """Collect refreshable target/provider pairs for one source model."""
    if not _is_safe_asset_id(model_id):
        raise ValueError(f"Invalid model id: {model_id}")

    source_model_dir = MODELS_DIR / model_id
    if not source_model_dir.exists():
        raise FileNotFoundError(f"Model not found: {model_id}")

    edited_base = TRIPLETS_DIR / model_id / "edited"
    if not edited_base.exists():
        return [], [
            {
                "model_id": model_id,
                "reason": "no edited batches found",
            }
        ]

    refresh_targets = []
    skipped = []

    for edit_dir in sorted(edited_base.iterdir()):
        if not edit_dir.is_dir():
            continue

        edit_id = edit_dir.name
        edit_meta_path = edit_dir / "meta.json"
        if not edit_meta_path.exists():
            skipped.append(
                {
                    "edit_id": edit_id,
                    "reason": "edit meta not found",
                }
            )
            continue

        target_id = f"{model_id}_edit_{edit_id}"
        target_model_dir = MODELS_DIR / target_id
        if not target_model_dir.exists():
            skipped.append(
                {
                    "edit_id": edit_id,
                    "target_model_id": target_id,
                    "reason": "target model directory not found",
                }
            )
            continue

        glb_files = sorted(target_model_dir.glob("model_*.glb"))
        if not glb_files:
            skipped.append(
                {
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
                skipped.append(
                    {
                        "edit_id": edit_id,
                        "target_model_id": target_id,
                        "provider_id": provider_id,
                        "reason": "unsupported provider id",
                    }
                )
                continue

            refresh_targets.append(
                {
                    "edit_id": edit_id,
                    "target_model_id": target_id,
                    "provider": provider,
                    "provider_id": provider_id,
                }
            )

    return refresh_targets, skipped


def _refresh_model_dreamsim_for_model(model_id: str, config=None) -> dict:
    """Refresh LPIPS results for every refreshable target under one model."""
    from scripts.batch_process import BatchProcessor

    refresh_targets, skipped = _scan_model_dreamsim_refresh_targets(model_id)
    if config is None:
        config = load_config()
    processor = BatchProcessor(config)

    refreshed = []
    failed = []

    for item in refresh_targets:
        target_views_dir = (
            TRIPLETS_DIR / item["target_model_id"] / "views" / item["provider_id"]
        )
        skip_render = target_views_dir.exists() and any(target_views_dir.glob("*.png"))

        _, task_status = processor.check_target_consistency_single(
            model_id=model_id,
            edit_id=item["edit_id"],
            provider=item["provider"],
            skip_render=skip_render,
            force_render=False,
        )

        result_item = {
            **item,
            "task_status": task_status,
            "skip_render": skip_render,
        }
        if task_status == "success":
            refreshed.append(result_item)
        else:
            failed.append(result_item)

    return {
        "model_id": model_id,
        "target_count": len(refresh_targets),
        "refreshed_count": len(refreshed),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "refreshed": refreshed,
        "failed": failed,
        "skipped": skipped,
    }


def run_refresh_all_models_dreamsim_task(params: dict) -> dict:
    """Recompute Stage-2 LPIPS for all source models or a specified subset."""
    requested_model_ids = params.get("model_ids")
    if requested_model_ids is not None:
        if not isinstance(requested_model_ids, list):
            raise ValueError("model_ids must be a list when provided")
        model_ids = [str(model_id).strip() for model_id in requested_model_ids if str(model_id).strip()]
    else:
        model_ids = [
            model_dir.name
            for model_dir in sorted(MODELS_DIR.iterdir())
            if model_dir.is_dir() and "_edit_" not in model_dir.name
        ]

    if not model_ids:
        raise ValueError("No source models found")

    config = load_config()
    refreshed_models = []
    skipped_models = []
    failed_models = []

    total_targets = 0
    total_refreshed = 0
    total_failed = 0
    total_skipped = 0

    for model_id in model_ids:
        try:
            result = _refresh_model_dreamsim_for_model(model_id, config=config)
        except Exception as exc:
            failed_models.append(
                {
                    "model_id": model_id,
                    "error": str(exc),
                }
            )
            continue

        if result["target_count"] == 0:
            skipped_models.append(
                {
                    "model_id": model_id,
                    "reason": "no refreshable LPIPS targets found",
                    "skipped": result["skipped"],
                }
            )
            continue

        refreshed_models.append(
            {
                "model_id": model_id,
                "target_count": result["target_count"],
                "refreshed_count": result["refreshed_count"],
                "failed_count": result["failed_count"],
                "skipped_count": result["skipped_count"],
            }
        )
        total_targets += result["target_count"]
        total_refreshed += result["refreshed_count"]
        total_failed += result["failed_count"]
        total_skipped += result["skipped_count"]

    return {
        "requested_model_count": len(model_ids),
        "refreshed_model_count": len(refreshed_models),
        "skipped_model_count": len(skipped_models),
        "failed_model_count": len(failed_models),
        "target_count": total_targets,
        "refreshed_count": total_refreshed,
        "failed_count": total_failed,
        "skipped_count": total_skipped,
        "refreshed_models": refreshed_models,
        "skipped_models": skipped_models,
        "failed_models": failed_models,
    }


def _has_missing_mask_artifacts(edit_dir: Path, meta: dict) -> bool:
    required_mask_files = [edit_dir / f"{view_name}_mask.png" for view_name in VIEW_ORDER]
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
    for view_name in VIEW_ORDER:
        value = view_paths.get(view_name)
        if not isinstance(value, str) or not value:
            return True
    return False


def _scan_model_missing_mask_targets(model_id: str) -> tuple[list[dict], list[dict]]:
    if not _is_safe_asset_id(model_id):
        raise ValueError(f"Invalid model id: {model_id}")
    source_model_dir = MODELS_DIR / model_id
    if not source_model_dir.exists():
        raise FileNotFoundError(f"Model not found: {model_id}")

    edited_base = TRIPLETS_DIR / model_id / "edited"
    if not edited_base.exists():
        return [], [{"model_id": model_id, "reason": "no edited batches found"}]

    targets = []
    skipped = []
    for edit_dir in sorted(edited_base.iterdir()):
        if not edit_dir.is_dir():
            continue
        edit_id = edit_dir.name
        meta_path = edit_dir / "meta.json"
        if not meta_path.exists():
            skipped.append({"edit_id": edit_id, "reason": "edit meta not found"})
            continue
        meta = safe_load_json(meta_path, {})
        if not meta:
            skipped.append({"edit_id": edit_id, "reason": "edit meta invalid"})
            continue
        if not _has_missing_mask_artifacts(edit_dir, meta):
            skipped.append({"edit_id": edit_id, "reason": "mask already complete"})
            continue
        targets.append({"edit_id": edit_id})
    return targets, skipped


def run_materialize_missing_masks_task(params: dict) -> dict:
    """Materialize missing mask artifacts for one source model (conservative serial mode)."""
    model_id = require_param(params, "model_id", "materialize missing masks task")
    targets, skipped = _scan_model_missing_mask_targets(model_id)
    if not targets:
        raise ValueError(f"No edit batches with missing masks found for model {model_id}")

    config = load_config()
    materialized = []
    failed = []

    for item in targets:
        edit_id = item["edit_id"]
        edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
        meta_path = edited_dir / "meta.json"
        meta = safe_load_json(meta_path, {})
        try:
            instruction = meta.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                raise ValueError("instruction missing in meta.json")

            source_provider_id = meta.get("source_provider_id")
            if not isinstance(source_provider_id, str) or not source_provider_id.strip():
                raise ValueError("source_provider_id missing in meta.json")

            source_views_dir = _resolve_source_views_dir(model_id, source_provider_id)
            result = materialize_missing_masks(
                model_id=model_id,
                source_provider_id=source_provider_id,
                source_views_dir=source_views_dir,
                edited_dir=edited_dir,
                path_formatter=lambda p: _rel_path(p).replace("\\", "/"),
                diff_threshold=config.edit_artifacts.diff_threshold,
                opening_kernel_size=config.edit_artifacts.opening_kernel_size,
                edit_mode=str(meta.get("edit_mode") or "single"),
                editor_metadata=meta.get("editor_metadata", {}),
            )
            meta.update(result["meta_patch"])
            meta.update(resolve_instruction_display_from_edit_meta(meta))
            meta["instruction"] = meta["instruction_display_text"]
            _write_json_atomic(meta_path, meta)
            materialized.append({"edit_id": edit_id})
        except Exception as exc:
            failed.append({"edit_id": edit_id, "reason": str(exc)})

    return {
        "model_id": model_id,
        "edit_count": len(targets) + len(skipped),
        "missing_mask_count": len(targets),
        "materialized_count": len(materialized),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "materialized": materialized,
        "failed": failed,
        "skipped": skipped,
    }


# =============================================================================
# Routes - Pages
# =============================================================================


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/")
def home():
    """Home page with pipeline overview."""
    return render_template("home.html", stats=None)


@app.route("/prompts")
def prompts_page():
    """View all prompts."""
    prompts = get_all_prompts()
    return render_template("prompts.html", prompts=prompts)


@app.route("/images")
def images_page():
    """View all images (with dynamic loading)."""
    return render_template("images.html")


@app.route("/models")
def models_page():
    """View all 3D models."""
    experiment_filter_error = None
    try:
        experiment_filter_data = _build_models_experiment_filter_payload()
    except OSError as exc:
        experiment_filter_error = (
            "Experiment filters unavailable because experiment metadata could not "
            f"be read from the pipeline workspace: {exc}"
        )
        experiment_filter_data = _empty_models_experiment_filter_payload(
            experiment_filter_error
        )
    import json

    return render_template(
        "models.html",
        experiment_filter_data_json=json.dumps(
            experiment_filter_data, ensure_ascii=False
        ),
        experiment_filter_error=experiment_filter_error,
    )


@app.route("/model/<model_id>")
def model_detail_page(model_id):
    """View detailed info for a single model."""
    model = _load_model_payload_by_id(model_id)
    if not model:
        return "Model not found", 404
    response = app.make_response(render_template("model_detail.html", model=model))
    return _set_no_store_headers(response)


@app.route("/api/model/<model_id>")
def api_get_model_detail(model_id):
    """Get detailed info for a single model."""
    model = _load_model_payload_by_id(model_id)
    if not model:
        return jsonify({"error": "Model not found"}), 404
    return _set_no_store_headers(jsonify(model))


@app.route("/tasks")
def tasks_page():
    """View active and completed tasks."""
    return render_template("tasks.html")


@app.route("/pairs")
def pairs_page():
    """View all source-target 3D model pairs for editing results."""
    return render_template("pairs.html")


@app.route("/passed-pairs")
def passed_pairs_page():
    """View Stage2 VLM passed editing pairs for test set curation."""
    return render_template("passed_pairs.html")


@app.route("/batch-generation")
def batch_generation_page():
    """Batch generation configurator page."""
    return render_template("batch_generation.html", page="batch-generation")


@app.route("/matrix-generation")
def matrix_generation_page():
    """Matrix generation page — deterministic object+style pair coverage."""
    return render_template("matrix_generation.html", page="matrix-generation")


@app.route("/experiment-stats")
def experiment_stats_page():
    """Provider/category statistics viewer."""
    return render_template("experiment_stats.html", page="experiment-stats")


def _load_pair_payload_by_id(model_id: str, *, ready_only: bool) -> dict | None:
    """Load one source-target pair payload for the pairs page."""
    model_dir = MODELS_DIR / model_id
    if not model_dir.exists() or not model_dir.is_dir():
        return None
    if "_edit_" in model_id:
        return None

    source_3d_models = [_glb_to_provider_info(glb) for glb in model_dir.glob("*.glb")]
    if not source_3d_models:
        return None

    edited_base = TRIPLETS_DIR / model_id / "edited"
    if not edited_base.exists():
        return None

    edit_batches = []
    for batch_dir in sorted(edited_base.iterdir()):
        if not batch_dir.is_dir():
            continue

        batch_meta_path = batch_dir / "meta.json"
        if not batch_meta_path.exists():
            continue
        raw_batch_meta = safe_load_json(batch_meta_path, {})
        if not raw_batch_meta:
            continue
        batch_meta = _with_edit_meta_instruction_payload(raw_batch_meta)
        if not batch_meta or not is_edit_batch_allowed(batch_meta):
            continue

        front_view = None
        for view_name in ["front", "back", "right"]:
            view_path = batch_dir / f"{view_name}.png"
            if view_path.exists():
                front_view = _rel_path(view_path).replace("\\", "/")
                break

        target_model_id = f"{model_id}_edit_{batch_dir.name}"
        target_model_dir = MODELS_DIR / target_model_id
        target_3d_models = []
        if target_model_dir.exists():
            for glb in target_model_dir.glob("*.glb"):
                info = _glb_to_provider_info(glb)
                info["id"] = target_model_id
                info["target_render_grid"] = _ensure_target_render_grid(
                    target_model_id, info.get("provider_id")
                )
                target_3d_models.append(info)

        target_3d = target_3d_models[0] if target_3d_models else None

        edit_artifacts = batch_meta.get("edit_artifacts", {})
        if not isinstance(edit_artifacts, dict):
            edit_artifacts = {}
        before_info = edit_artifacts.get("before_image_grid", {})
        target_info = edit_artifacts.get("target_image_grid", {})
        mask_info = edit_artifacts.get("edit_mask", {})
        before_image_grid = (
            before_info.get("path") if isinstance(before_info, dict) else None
        )
        target_image_grid = (
            target_info.get("path") if isinstance(target_info, dict) else None
        )
        edit_mask = mask_info.get("path") if isinstance(mask_info, dict) else None
        per_view_masks = (
            mask_info.get("view_paths")
            if isinstance(mask_info, dict)
            and isinstance(mask_info.get("view_paths"), dict)
            else {}
        )
        mask_missing = _has_missing_mask_artifacts(batch_dir, batch_meta)

        batch_payload = {
            "edit_id": batch_meta.get("edit_id", batch_dir.name),
            "instruction": batch_meta.get("instruction", ""),
            "instruction_display_text": batch_meta.get(
                "instruction_display_text", batch_meta.get("instruction", "")
            ),
            "instruction_display_source": batch_meta.get(
                "instruction_display_source"
            ),
            "instruction_display_status": batch_meta.get(
                "instruction_display_status"
            ),
            "source_provider_id": batch_meta.get("source_provider_id"),
            "front_view": front_view,
            "before_image_grid": before_image_grid,
            "target_image_grid": target_image_grid,
            "target_render_grid": (
                target_3d.get("target_render_grid") if isinstance(target_3d, dict) else None
            ),
            "target_render_grid_provider": (
                target_3d.get("provider") if isinstance(target_3d, dict) else None
            ),
            "edit_mask": edit_mask,
            "per_view_masks": per_view_masks,
            "mask_missing": mask_missing,
            "target_3d": target_3d,
            "target_3d_models": target_3d_models,
            "created_at": batch_meta.get("generated_at"),
        }

        if ready_only and not _is_pair_batch_ready(batch_payload):
            continue
        edit_batches.append(batch_payload)

    edit_batches = sort_edit_batches_by_created_at_desc(edit_batches)
    if not edit_batches:
        return None

    meta_path = model_dir / "meta.json"
    meta = safe_load_json(meta_path, {}) if meta_path.exists() else {}
    source_image = None
    source_image_path = IMAGES_DIR / model_id / "image.png"
    if source_image_path.exists():
        source_image = _rel_path(source_image_path).replace("\\", "/")

    return {
        "id": model_id,
        "source_3d": source_3d_models[0],
        "source_3d_models": source_3d_models,
        "source_image": source_image,
        "provider": meta.get("provider"),
        "edit_batches": edit_batches,
        "edit_count": len(edit_batches),
    }


def _scan_all_pairs() -> list:
    """Collect all source-target pair payloads for the pairs page."""
    pairs = []

    if not MODELS_DIR.exists():
        return pairs

    for d in sorted(MODELS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue

        # Skip edit-generated models (they have _edit_ in name)
        if "_edit_" in d.name:
            continue

        # Check for edited batches
        edited_base = TRIPLETS_DIR / d.name / "edited"
        if not edited_base.exists():
            continue

        edit_batches = []
        for batch_dir in sorted(edited_base.iterdir()):
            if not batch_dir.is_dir():
                continue

            batch_meta_path = batch_dir / "meta.json"
            if not batch_meta_path.exists():
                continue
            raw_batch_meta = safe_load_json(batch_meta_path, {})
            if not raw_batch_meta:
                continue
            batch_meta = _with_edit_meta_instruction_payload(raw_batch_meta)
            if not batch_meta:
                continue
            if not is_edit_batch_allowed(batch_meta):
                continue

            # Get front view for preview
            front_view = None
            for view_name in ["front", "back", "right"]:
                view_path = batch_dir / f"{view_name}.png"
                if view_path.exists():
                    front_view = _rel_path(view_path).replace("\\", "/")
                    break

            # Check for target 3D models (multiple providers)
            target_model_id = f"{d.name}_edit_{batch_dir.name}"
            target_model_dir = MODELS_DIR / target_model_id
            target_3d_models = []  # List of all Target 3D models (multiple providers)
            if target_model_dir.exists():
                for glb in target_model_dir.glob("*.glb"):
                    info = _glb_to_provider_info(glb)
                    info["id"] = target_model_id
                    info["target_render_grid"] = _ensure_target_render_grid(
                        target_model_id, info.get("provider_id")
                    )
                    target_3d_models.append(info)

            # For backward compatibility, set target_3d to first model if exists
            target_3d = target_3d_models[0] if target_3d_models else None

            edit_artifacts = batch_meta.get("edit_artifacts", {})
            if not isinstance(edit_artifacts, dict):
                edit_artifacts = {}
            before_info = edit_artifacts.get("before_image_grid", {})
            target_info = edit_artifacts.get("target_image_grid", {})
            mask_info = edit_artifacts.get("edit_mask", {})
            before_image_grid = (
                before_info.get("path") if isinstance(before_info, dict) else None
            )
            target_image_grid = (
                target_info.get("path") if isinstance(target_info, dict) else None
            )
            edit_mask = mask_info.get("path") if isinstance(mask_info, dict) else None
            per_view_masks = (
                mask_info.get("view_paths")
                if isinstance(mask_info, dict)
                and isinstance(mask_info.get("view_paths"), dict)
                else {}
            )
            mask_missing = _has_missing_mask_artifacts(batch_dir, batch_meta)

            edit_batches.append(
                {
                    "edit_id": batch_meta.get("edit_id", batch_dir.name),
                    "instruction": batch_meta.get("instruction", ""),
                    "instruction_display_text": batch_meta.get(
                        "instruction_display_text", batch_meta.get("instruction", "")
                    ),
                    "instruction_display_source": batch_meta.get(
                        "instruction_display_source"
                    ),
                    "instruction_display_status": batch_meta.get(
                        "instruction_display_status"
                    ),
                    "source_provider_id": batch_meta.get("source_provider_id"),
                    "front_view": front_view,
                    "before_image_grid": before_image_grid,
                    "target_image_grid": target_image_grid,
                    "target_render_grid": (
                        target_3d.get("target_render_grid")
                        if isinstance(target_3d, dict)
                        else None
                    ),
                    "target_render_grid_provider": (
                        target_3d.get("provider") if isinstance(target_3d, dict) else None
                    ),
                    "edit_mask": edit_mask,
                    "per_view_masks": per_view_masks,
                    "mask_missing": mask_missing,
                    "target_3d": target_3d,  # First model for backward compatibility
                    "target_3d_models": target_3d_models,  # All models by provider
                    "created_at": batch_meta.get("generated_at"),
                }
            )

        edit_batches = sort_edit_batches_by_created_at_desc(edit_batches)
        if not edit_batches:
            continue

        # Get source model info - collect all providers
        source_3d_models = [_glb_to_provider_info(glb) for glb in d.glob("*.glb")]
        if not source_3d_models:
            continue

        meta_path = d / "meta.json"
        meta = safe_load_json(meta_path, {}) if meta_path.exists() else {}

        # Get source image for preview
        source_image = None
        source_image_path = IMAGES_DIR / d.name / "image.png"
        if source_image_path.exists():
            source_image = _rel_path(source_image_path).replace("\\", "/")

        pairs.append(
            {
                "id": d.name,
                "source_3d": source_3d_models[0],  # First for backward compat
                "source_3d_models": source_3d_models,  # All providers
                "source_image": source_image,
                "provider": meta.get("provider"),
                "edit_batches": edit_batches,
                "edit_count": len(edit_batches),
            }
        )

    return pairs


def _is_pair_batch_ready(batch: dict) -> bool:
    instruction_text = batch.get("instruction_display_text") or batch.get("instruction")
    if not isinstance(instruction_text, str) or not instruction_text.strip():
        return False
    if not batch.get("target_3d_models"):
        return False
    if not isinstance(batch.get("target_image_grid"), str) or not batch.get("target_image_grid"):
        return False
    if not isinstance(batch.get("edit_mask"), str) or not batch.get("edit_mask"):
        return False
    return True


def get_all_pairs() -> list:
    """Get all source-target pairs with a short in-memory cache."""
    return _get_cached_pipeline_listing("pairs", _scan_all_pairs)


def _load_pairs_page_fast(page: int, per_page: int, *, ready_only: bool) -> tuple[list[dict], bool]:
    """Load just one page of pairs without building a full filtered index first."""
    start = max(page - 1, 0) * per_page
    end = start + per_page
    items = []
    matched_count = 0
    has_more = False

    for model_id in get_source_model_ids():
        pair = _load_pair_payload_by_id(model_id, ready_only=ready_only)
        if not pair:
            continue
        if matched_count >= start and matched_count < end:
            items.append(pair)
        matched_count += 1
        if matched_count > end:
            has_more = True
            break

    return items, has_more


@app.route("/api/pairs/summary", methods=["GET"])
def api_get_pairs_summary():
    """Get summary counts for the pairs page."""
    target_only = request.args.get("target_only", "0") == "1"

    model_index = get_all_models_index()
    total = 0
    total_edits = 0
    total_with_target = 0
    for entry in model_index:
        edit_count = int(entry.get("edit_count") or 0)
        ready_pair_count = int(entry.get("ready_pair_count") or 0)
        if target_only:
            if ready_pair_count <= 0:
                continue
            total += 1
            total_edits += ready_pair_count
            total_with_target += ready_pair_count
        else:
            if edit_count <= 0:
                continue
            total += 1
            total_edits += edit_count
            total_with_target += ready_pair_count

    return jsonify(
        {
            "total": total,
            "total_edits": total_edits,
            "total_with_target_3d": total_with_target,
        }
    )


@app.route("/api/pairs", methods=["GET"])
def api_get_pairs():
    """Get source-target pair payloads, optionally paginated for the UI."""
    page = request.args.get("page", type=int)
    per_page = request.args.get("per_page", 20, type=int)
    target_only = request.args.get("target_only", "0") == "1"

    if page is not None:
        items, has_more = _load_pairs_page_fast(page, per_page, ready_only=target_only)
        return jsonify(
            {
                "items": items,
                "page": page,
                "per_page": per_page,
                "total": None,
                "has_more": has_more,
                "total_edits": None,
                "total_with_target_3d": None,
            }
        )

    model_index = get_all_models_index()
    filtered_index = []
    total_edits = 0
    total_with_target = 0
    for entry in model_index:
        edit_count = int(entry.get("edit_count") or 0)
        ready_pair_count = int(entry.get("ready_pair_count") or 0)
        if target_only:
            if ready_pair_count <= 0:
                continue
            total_edits += ready_pair_count
            total_with_target += ready_pair_count
        else:
            if edit_count <= 0:
                continue
            total_edits += edit_count
            total_with_target += ready_pair_count
        filtered_index.append(entry)

    target_ids = [entry["id"] for entry in filtered_index]

    items = []
    for model_id in target_ids:
        pair = _load_pair_payload_by_id(model_id, ready_only=target_only)
        if pair:
            items.append(pair)

    if page is None:
        if not target_only:
            return jsonify(items)
        return jsonify(
            {
                "items": items,
                "total": len(filtered_index),
                "total_edits": total_edits,
                "total_with_target_3d": total_with_target,
                "has_more": False,
            }
        )


# =============================================================================
# Pairs - YAML 过滤与导出命令生成
# =============================================================================


def _collect_edit_records_for_plans(plan_paths: list[str]) -> list[dict]:
    """从 experiments 目录收集匹配 plan_paths 的所有 edit_records。

    返回 edit_records.jsonl 中的原始记录列表。
    """
    experiments_dir = _get_pipeline_experiments_dir()
    if not experiments_dir.exists():
        return []

    # 找到匹配的 experiment 目录
    matched_experiments = []
    for experiment_dir in experiments_dir.iterdir():
        if not experiment_dir.is_dir():
            continue
        manifest_path = experiment_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = safe_load_json(manifest_path)
        if not manifest:
            continue
        manifest_plan_path = manifest.get("plan_path", "")
        if manifest_plan_path in plan_paths:
            matched_experiments.append(experiment_dir)

    # 读取所有 edit_records.jsonl
    records = []
    for experiment_dir in matched_experiments:
        edit_records_path = experiment_dir / "edit_records.jsonl"
        if not edit_records_path.exists():
            continue
        try:
            with open(edit_records_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue

    return records


def _filter_edit_records_to_pair_keys(
    records: list[dict],
    *,
    lpips_max: float | None = None,
) -> tuple[dict[tuple[str, str], dict], dict]:
    """从 edit_records 中筛选出合格的 (source_model_id, edit_id) 集合。

    返回:
        - allowed: dict of (source_model_id, edit_id) -> record (去重取最新)
        - stats: 漏斗统计
    """
    stats = {
        "total_records": len(records),
        "has_target_model": 0,
        "lpips_filtered": 0,
    }

    # 按 (source_model_id, edit_id) 去重，取 created_at 最新的
    best: dict[tuple[str, str], dict] = {}
    for record in records:
        target_model_id = record.get("target_model_id")
        if not target_model_id:
            continue
        source_model_id = record.get("source_model_id", "")
        edit_id = record.get("edit_id", "")
        if not source_model_id or not edit_id:
            continue

        stats["has_target_model"] += 1

        key = (source_model_id, edit_id)
        existing = best.get(key)
        if existing is None or (record.get("created_at", "") > existing.get("created_at", "")):
            best[key] = record

    # LPIPS 过滤
    allowed = {}
    for key, record in best.items():
        if lpips_max is not None:
            score = record.get("stage2_score")
            if score is None or score > lpips_max:
                continue
        stats["lpips_filtered"] += 1
        allowed[key] = record

    return allowed, stats


@app.route("/api/pairs/export-config", methods=["GET"])
def api_pairs_export_config():
    """返回导出相关配置（path_prefixes 等）。"""
    try:
        config = load_config()
        return jsonify({
            "path_prefixes": config.export.path_prefixes,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/pairs/yaml-options", methods=["GET"])
def api_pairs_yaml_options():
    """返回所有 YAML 实验计划列表，供前端多选。"""
    try:
        entries, io_errors = _collect_experiment_entries()
        grouped = {}
        for entry in entries:
            manifest = entry["manifest"]
            plan_path = manifest["plan_path"]
            current = grouped.setdefault(
                plan_path,
                {
                    "plan_path": plan_path,
                    "yaml_name": Path(plan_path).name,
                    "run_count": 0,
                    "latest_run_time": None,
                },
            )
            current["run_count"] += 1
            run_time = manifest.get("finished_at") or manifest.get("started_at")
            if run_time and (
                current["latest_run_time"] is None
                or run_time > current["latest_run_time"]
            ):
                current["latest_run_time"] = run_time

        yaml_files = sorted(
            grouped.values(),
            key=lambda item: (item["latest_run_time"] or "", item["yaml_name"]),
            reverse=True,
        )
        return jsonify({"yaml_files": yaml_files})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# Cache for filter-by-yaml results (avoids repeated filesystem scans)
_filter_by_yaml_cache: dict = {}
_filter_by_yaml_cache_lock = threading.Lock()
_FILTER_BY_YAML_CACHE_TTL = 120  # seconds


@app.route("/api/pairs/filter-by-yaml", methods=["GET"])
def api_pairs_filter_by_yaml():
    """根据选中的 YAML plan_paths 返回过滤后的模型对列表。"""
    plan_paths_raw = request.args.get("plan_paths", "").strip()
    if not plan_paths_raw:
        return jsonify({"error": "Missing required parameter: plan_paths"}), 400

    plan_paths = [p.strip() for p in plan_paths_raw.split(",") if p.strip()]
    lpips_max_raw = request.args.get("lpips_max", "").strip()
    lpips_max = float(lpips_max_raw) if lpips_max_raw else None

    # Check cache
    cache_key = f"{','.join(sorted(plan_paths))}|{lpips_max_raw}"
    with _filter_by_yaml_cache_lock:
        cached = _filter_by_yaml_cache.get(cache_key)
        if cached and time.monotonic() - cached["at"] < _FILTER_BY_YAML_CACHE_TTL:
            return jsonify(cached["value"])

    try:
        # 从 edit_records 收集并筛选
        records = _collect_edit_records_for_plans(plan_paths)
        allowed, stats = _filter_edit_records_to_pair_keys(records, lpips_max=lpips_max)

        if not allowed:
            return jsonify({
                "items": [],
                "total": 0,
                "stats": stats,
            })

        # 用白名单过滤 pairs
        source_ids = set(key[0] for key in allowed)
        allowed_edit_ids_by_source = {}
        for (source_id, edit_id) in allowed:
            allowed_edit_ids_by_source.setdefault(source_id, set()).add(edit_id)

        items = []
        for source_id in source_ids:
            pair = _load_pair_payload_by_id(source_id, ready_only=False)
            if not pair:
                continue
            # 只保留白名单中的 edit_batch
            allowed_edits = allowed_edit_ids_by_source.get(source_id, set())
            filtered_batches = [
                batch for batch in pair["edit_batches"]
                if batch.get("edit_id") in allowed_edits
            ]
            if not filtered_batches:
                continue
            pair["edit_batches"] = filtered_batches
            pair["edit_count"] = len(filtered_batches)

            # 附加 stage2 分数到每个 batch
            for batch in filtered_batches:
                key = (source_id, batch["edit_id"])
                record = allowed.get(key)
                if record:
                    batch["stage2_score"] = record.get("stage2_score")
                    batch["stage2_status"] = record.get("stage2_status")
                    batch["final_status"] = record.get("final_status")
                    batch["category"] = record.get("category", "")
                    batch["object_name"] = record.get("object_name", "")

            items.append(pair)

        stats["pairs_with_glb"] = sum(
            1 for item in items
            for batch in item["edit_batches"]
            if batch.get("target_3d")
        )

        result = {
            "items": items,
            "total": len(items),
            "total_edits": sum(item["edit_count"] for item in items),
            "stats": stats,
        }
        with _filter_by_yaml_cache_lock:
            _filter_by_yaml_cache[cache_key] = {"at": time.monotonic(), "value": result}
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/pairs/generate-export-cmd", methods=["POST"])
def api_pairs_generate_export_cmd():
    """根据筛选条件生成可在服务器执行的导出命令。"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    plan_paths = data.get("plan_paths", [])
    if not plan_paths:
        return jsonify({"error": "Missing required field: plan_paths"}), 400

    lpips_max = data.get("lpips_max")
    dataset_name = data.get("dataset_name", "export")
    path_prefix = data.get("path_prefix", "")

    # 验证 path_prefix 必须在配置的 path_prefixes 列表中
    config = load_config()
    if path_prefix and path_prefix not in config.export.path_prefixes:
        return jsonify({
            "error": f"path_prefix '{path_prefix}' not in configured export.path_prefixes: {config.export.path_prefixes}"
        }), 400

    # 统计匹配的 pair 数量
    records = _collect_edit_records_for_plans(plan_paths)
    allowed, stats = _filter_edit_records_to_pair_keys(records, lpips_max=lpips_max)

    # 构建命令
    parts = [
        f"cd {PROJECT_ROOT}",
        "&&",
        PYTHON_INTERPRETER,
        "scripts/export_edit_pair_manifest.py",
        f"--plan-paths '{','.join(plan_paths)}'",
    ]
    if lpips_max is not None:
        parts.append(f"--lpips-max {lpips_max}")
    if dataset_name:
        parts.append(f"--dataset-name '{dataset_name}'")
    if path_prefix:
        parts.append(f"--path-prefix '{path_prefix}'")

    command = " ".join(str(p) for p in parts)

    return jsonify({
        "command": command,
        "pair_count": len(allowed),
        "stats": stats,
    })


# =============================================================================
# Routes - API
# =============================================================================

# Path to categorized objects file — initialized from config in init_semaphores()
CATEGORIZED_OBJECTS_FILE = None
# run_full_experiment.py script path
RUN_FULL_EXPERIMENT_SCRIPT = PROJECT_ROOT / "scripts" / "run_full_experiment.py"
DEFAULT_RUN_FULL_EXPERIMENT_GPU_ID = 0
LOGS_DIR = None  # initialized from config in init_semaphores()


# =============================================================================
# Experiment Plan APIs
# =============================================================================


def _resolve_run_full_experiment_gpu_id(gpu_id_raw) -> int:
    if gpu_id_raw is None or str(gpu_id_raw).strip() == "":
        return DEFAULT_RUN_FULL_EXPERIMENT_GPU_ID
    try:
        gpu_id = int(str(gpu_id_raw).strip())
    except ValueError as exc:
        raise ValueError("gpu_id must be a non-negative integer") from exc
    if gpu_id < 0:
        raise ValueError("gpu_id must be a non-negative integer")
    return gpu_id


def _slugify(text: str) -> str:
    """Convert text to slug format for filenames."""
    import re

    slug = re.sub(r"[^A-Za-z0-9]+", "-", text.strip()).strip("-").lower()
    return slug or "experiment"


def _is_path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _build_run_full_experiment_log_path(
    plan_path: Path, plan: dict, *, timestamp: str = None
) -> Path:
    timestamp_value = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_name = str(plan.get("name") or plan_path.stem)
    slug = _slugify(plan_name)
    categories = require_param(plan, "categories", "experiment plan")
    category_count = len(categories)
    object_count_signature = "-".join(
        str(require_param(category, "object_count", "experiment plan category"))
        for category in categories
    )
    log_filename = (
        f"{timestamp_value}_{slug}_categories-{category_count}"
        f"_objects-{object_count_signature}.log"
    )
    return LOGS_DIR / log_filename


def _build_run_full_experiment_cli_command(
    plan_path: Path,
    log_path: Path,
    *,
    gpu_id: int = DEFAULT_RUN_FULL_EXPERIMENT_GPU_ID,
) -> str:
    return _build_run_full_experiment_background_cli_command(
        ["--plan", str(plan_path), "--gpu-id", str(gpu_id)],
        log_path,
        "Started run_full_experiment with PID",
    )


def _build_run_full_experiment_resume_cli_command(
    experiment_id: str,
    log_path: Path,
    *,
    gpu_id: int = DEFAULT_RUN_FULL_EXPERIMENT_GPU_ID,
) -> str:
    return _build_run_full_experiment_background_cli_command(
        ["--resume-experiment-id", experiment_id, "--gpu-id", str(gpu_id)],
        log_path,
        "Started run_full_experiment resume with PID",
    )


def _build_run_full_experiment_repair_cli_command(
    experiment_id: str,
    log_path: Path,
    *,
    gpu_id: int = DEFAULT_RUN_FULL_EXPERIMENT_GPU_ID,
) -> str:
    return _build_run_full_experiment_background_cli_command(
        ["--repair-experiment-id", experiment_id, "--gpu-id", str(gpu_id)],
        log_path,
        "Started run_full_experiment repair with PID",
    )


def _build_run_full_experiment_background_cli_command(
    command_args: list[str], log_path: Path, start_message: str
) -> str:
    quoted_args = " ".join(shlex.quote(str(arg)) for arg in command_args)
    return (
        f"LOG_FILE={shlex.quote(str(log_path))}; "
        f'mkdir -p "$(dirname "$LOG_FILE")"; '
        f'touch "$LOG_FILE"; '
        f"nohup {shlex.quote(PYTHON_INTERPRETER)} "
        f"{shlex.quote(str(RUN_FULL_EXPERIMENT_SCRIPT))} "
        f"{quoted_args} "
        f'>> "$LOG_FILE" 2>&1 < /dev/null & disown; '
        f'echo "{start_message} $!"; '
        f'tail --retry -f "$LOG_FILE"'
    )


def _build_run_full_experiment_action_log_path(
    experiment_id: str, action: str, *, timestamp: str = None
) -> Path:
    timestamp_value = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(experiment_id)
    return LOGS_DIR / f"{timestamp_value}_{slug}_{action}.log"


def _require_experiment_plan_path(plan_path_raw: str) -> Path:
    if not isinstance(plan_path_raw, str) or not plan_path_raw.strip():
        raise ValueError("plan_path is required")

    raw = plan_path_raw.strip()
    plan_path = Path(raw)
    if not plan_path.is_absolute():
        plan_path = EXPERIMENT_PLANS_DIR / raw

    if plan_path.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError(f"Unsupported plan file extension: {plan_path.name}")
    if not _is_path_within(plan_path, EXPERIMENT_PLANS_DIR):
        raise ValueError(f"plan_path must stay under {EXPERIMENT_PLANS_DIR}")
    if not plan_path.exists():
        raise FileNotFoundError(f"Plan file does not exist: {plan_path}")
    return plan_path


def _read_experiment_plan_yaml(plan_path: Path) -> tuple[dict, str]:
    import yaml, time as _time

    for _attempt in range(5):
        try:
            with open(plan_path, "rb") as f:
                yaml_content = f.read().decode("utf-8")
            break
        except OSError as e:
            if e.errno == 5 and _attempt < 4:
                _time.sleep(0.2 * (2 ** _attempt))
                continue
            raise

    plan = yaml.safe_load(yaml_content)
    if not isinstance(plan, dict):
        raise ValueError(f"Plan file must contain a mapping: {plan_path}")
    return plan, yaml_content


def _normalize_experiment_plan_for_form(plan: dict) -> dict:
    categories = require_param(plan, "categories", "experiment plan")
    if not isinstance(categories, list) or not categories:
        raise ValueError("Experiment plan must contain at least one category entry")

    normalized_categories = []
    for index, category in enumerate(categories, start=1):
        if not isinstance(category, dict):
            raise ValueError(f"Category #{index} must be a mapping")
        random_cfg = require_param(category, "random", "experiment plan category")
        instruction_plan = normalize_instruction_plan_from_category(
            category,
            "experiment plan category",
            allow_legacy_counts=True,
        )
        random_category = bool(
            require_param(random_cfg, "category", "experiment plan category.random")
        )
        random_object = bool(
            require_param(random_cfg, "object", "experiment plan category.random")
        )
        if random_category:
            normalized_category_name = ""
            normalized_objects = None
        else:
            normalized_category_name = category.get("category_name") or ""
            # random.object=true means no fixed objects; keep as None so
            # _validate_experiment_plan's "objects is not None" check stays correct.
            if random_object:
                normalized_objects = None
            else:
                normalized_objects = list(category.get("objects") or [])

        normalized = {
            "category_name": normalized_category_name,
            "random": {
                "category": random_category,
                "object": random_object,
            },
            "objects": normalized_objects,
            "object_count": int(
                require_param(category, "object_count", "experiment plan category")
            ),
            "instruction_plan": instruction_plan,
        }
        normalized_categories.append(normalized)

    has_random_category = any(
        category["random"]["category"] for category in normalized_categories
    )
    has_fixed_category = any(
        not category["random"]["category"] for category in normalized_categories
    )

    if has_random_category and has_fixed_category:
        raise ValueError(
            "This YAML mixes fixed categories and random categories. "
            "The current Batch Generation form only supports one mode at a time."
        )

    global_random_categories = False
    categories_ui = normalized_categories

    if has_random_category:
        first = normalized_categories[0]
        if not all(category["random"]["category"] for category in normalized_categories):
            raise ValueError(
                "All categories must use random.category=true when loading a random-category YAML."
            )
        if not all(category["random"]["object"] for category in normalized_categories):
            raise ValueError(
                "random.category=true entries must also use random.object=true."
            )
        comparable_first = {
            "object_count": first["object_count"],
            "instruction_plan": first["instruction_plan"],
        }
        for category in normalized_categories[1:]:
            comparable_current = {
                "object_count": category["object_count"],
                "instruction_plan": category["instruction_plan"],
            }
            if comparable_current != comparable_first:
                raise ValueError(
                    "This YAML uses multiple different random-category templates. "
                    "The current Batch Generation form can only load the shared-template variant."
                )
        global_random_categories = True
        categories_ui = [first]

    form_data = {
        "name": str(require_param(plan, "name", "experiment plan")).strip(),
        "source_provider": require_param(plan, "source_provider", "experiment plan"),
        "target_provider": plan.get("target_provider")
        or require_param(plan, "source_provider", "experiment plan"),
        "edit_mode": require_param(plan, "edit_mode", "experiment plan"),
        "category_count": len(normalized_categories),
        "global_random_categories": global_random_categories,
        "categories": categories_ui,
    }

    validation_payload = {
        "name": form_data["name"],
        "source_provider": form_data["source_provider"],
        "target_provider": form_data["target_provider"],
        "edit_mode": form_data["edit_mode"],
        "categories": normalized_categories,
    }
    errors = _validate_experiment_plan(validation_payload)
    if errors:
        raise ValueError("; ".join(errors))

    return form_data


def _validate_experiment_plan(
    data: dict,
    *,
    allow_legacy_instruction_counts: bool = False,
) -> list:
    """Validate experiment plan data. Returns list of error messages."""
    errors = []
    objects_by_category = {}
    if CATEGORIZED_OBJECTS_FILE.exists():
        with open(CATEGORIZED_OBJECTS_FILE, "r", encoding="utf-8") as f:
            objects_by_category = json.load(f)
    valid_categories = set(objects_by_category.keys())
    valid_providers = {"hunyuan", "tripo", "rodin"}
    valid_edit_modes = {"single", "multiview"}
    deprecated_top_fields = {"category_workers", "instruction_type_ratio"}
    deprecated_category_fields = {
        "name",
        "prompt_budget",
        "target_source_models",
        "accepted_edits_per_model",
        "max_instruction_attempts",
        "style_ids",
        "instruction_type_ratio",
    }

    # Validate basic fields
    name = data.get("name", "").strip()
    if not name:
        errors.append("Plan name is required")
    elif ".." in name or "/" in name or "\\" in name:
        errors.append("Plan name contains invalid characters (path traversal attempt)")

    source_provider = data.get("source_provider", "")
    if not source_provider:
        errors.append("source_provider is required")
    elif source_provider not in valid_providers:
        errors.append(
            f"source_provider must be one of {sorted(valid_providers)}, got: {source_provider}"
        )

    target_provider = data.get("target_provider", "")
    if not target_provider:
        errors.append("target_provider is required")
    elif target_provider not in valid_providers:
        errors.append(
            f"target_provider must be one of {sorted(valid_providers)}, got: {target_provider}"
        )

    edit_mode = data.get("edit_mode", "")
    if not edit_mode:
        errors.append("edit_mode is required")
    elif edit_mode not in valid_edit_modes:
        errors.append(
            f"edit_mode must be one of {sorted(valid_edit_modes)}, got: {edit_mode}"
        )

    deprecated_top = sorted(field for field in data.keys() if field in deprecated_top_fields)
    if deprecated_top:
        errors.append(
            "Deprecated top-level fields are not supported: "
            + ", ".join(deprecated_top)
        )

    # Validate categories
    categories = data.get("categories", [])
    if not isinstance(categories, list):
        errors.append("categories must be a list")
    elif len(categories) == 0:
        errors.append("At least one category is required")
    else:
        fixed_category_names = []
        random_object_counts = []
        for idx, cat in enumerate(categories):
            prefix = f"categories[{idx}]"
            if not isinstance(cat, dict):
                errors.append(f"{prefix} must be a mapping")
                continue

            deprecated_fields = sorted(
                field for field in cat.keys() if field in deprecated_category_fields
            )
            if deprecated_fields:
                errors.append(
                    f"{prefix} contains deprecated fields: {', '.join(deprecated_fields)}"
                )

            random_cfg = cat.get("random")
            if not isinstance(random_cfg, dict):
                errors.append(f"{prefix}.random must be a mapping")
                continue
            random_category = random_cfg.get("category")
            random_object = random_cfg.get("object")
            if not isinstance(random_category, bool):
                errors.append(f"{prefix}.random.category must be a boolean")
            if not isinstance(random_object, bool):
                errors.append(f"{prefix}.random.object must be a boolean")
            if not isinstance(random_category, bool) or not isinstance(random_object, bool):
                continue
            if random_category and not random_object:
                errors.append(
                    f"{prefix} does not support random.category=true with random.object=false"
                )

            object_count = cat.get("object_count")
            if not isinstance(object_count, int) or object_count < 1:
                errors.append(f"{prefix}.object_count must be a positive integer")

            try:
                normalize_instruction_plan_from_category(
                    cat,
                    prefix,
                    allow_legacy_counts=allow_legacy_instruction_counts,
                )
            except ValueError as exc:
                errors.append(str(exc))

            if random_category:
                if isinstance(object_count, int) and object_count > 0:
                    random_object_counts.append(object_count)
                if cat.get("category_name"):
                    errors.append(f"{prefix}.category_name is not allowed when random.category=true")
                if cat.get("objects") is not None:
                    errors.append(f"{prefix}.objects is not allowed when random.category=true")
                continue

            category_name = cat.get("category_name", "")
            if not category_name:
                errors.append(f"{prefix}.category_name is required when random.category=false")
                continue
            if category_name not in valid_categories:
                errors.append(f"{prefix}.category_name '{category_name}' is not a valid category")
                continue
            fixed_category_names.append(category_name)

            category_objects = objects_by_category[category_name]
            objects = cat.get("objects")

            if random_object:
                if objects is not None:
                    errors.append(f"{prefix}.objects is not allowed when random.object=true")
                if isinstance(object_count, int) and object_count > len(category_objects):
                    errors.append(
                        f"{prefix}.object_count exceeds available objects in '{category_name}' ({len(category_objects)})"
                    )
                continue

            if not isinstance(objects, list) or len(objects) == 0:
                errors.append(f"{prefix}.objects is required when random.object=false")
                continue
            if isinstance(object_count, int) and object_count != len(objects):
                errors.append(
                    f"{prefix}.object_count must equal len(objects) when random.object=false"
                )

            duplicates = sorted({item for item in objects if objects.count(item) > 1})
            if duplicates:
                errors.append(f"{prefix}.objects contains duplicates: {duplicates}")

            invalid_objects = [
                item
                for item in objects
                if not isinstance(item, str) or item not in category_objects
            ]
            if invalid_objects:
                errors.append(
                    f"{prefix}.objects contains invalid objects for '{category_name}': {invalid_objects}"
                )

        duplicate_categories = sorted(
            {name for name in fixed_category_names if fixed_category_names.count(name) > 1}
        )
        if duplicate_categories:
            errors.append(
                "Duplicate fixed category_name values are not allowed: "
                + ", ".join(duplicate_categories)
            )

        unique_fixed_count = len(set(fixed_category_names))
        if unique_fixed_count + len(random_object_counts) > len(valid_categories):
            errors.append(
                "Requested fixed categories plus random category entries exceed the total "
                f"available categories ({len(valid_categories)})"
            )

        remaining_category_capacities = {
            category_name: len(objects_by_category[category_name])
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
                errors.append(
                    "Random category entries cannot be assigned to distinct categories with "
                    f"enough objects for object_count={object_count}"
                )
                break
            del remaining_category_capacities[eligible_categories[0]]

    return errors


@app.route("/api/experiment-plan/options", methods=["GET"])
def api_experiment_plan_options():
    """Return available options for experiment plan configuration.

    Returns:
        providers: List of valid 3D providers
        edit_modes: List of valid edit modes
        categories: List of category names from categorized_objects.json
        objects_by_category: Full category -> objects mapping
    """
    providers = ["hunyuan", "tripo", "rodin"]
    edit_modes = ["single", "multiview"]

    objects_by_category = {}
    categories = []
    if CATEGORIZED_OBJECTS_FILE.exists():
        with open(CATEGORIZED_OBJECTS_FILE, "r", encoding="utf-8") as f:
            objects_by_category = json.load(f)
            categories = sorted(objects_by_category.keys())

    return jsonify(
        {
            "providers": providers,
            "edit_modes": edit_modes,
            "categories": categories,
            "objects_by_category": objects_by_category,
        }
    )


@app.route("/api/experiment-plan/history", methods=["GET"])
def api_experiment_plan_history():
    """Return previously generated YAML plans sorted by file generation time desc."""
    try:
        EXPERIMENT_PLANS_DIR.mkdir(parents=True, exist_ok=True)
        entries = []
        io_errors = []
        for plan_path in list(EXPERIMENT_PLANS_DIR.glob("*.yaml")) + list(
            EXPERIMENT_PLANS_DIR.glob("*.yml")
        ):
            try:
                if plan_path.stat().st_size == 0:
                    continue
                plan, _ = _read_experiment_plan_yaml(plan_path)
                stat = plan_path.stat()
                categories = require_param(plan, "categories", "experiment plan")
                generated_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
                entries.append(
                    {
                        "plan_path": str(plan_path),
                        "yaml_name": plan_path.name,
                        "generated_at": generated_at,
                        "plan_name": plan.get("name"),
                        "source_provider": plan.get("source_provider"),
                        "target_provider": plan.get("target_provider")
                        or plan.get("source_provider"),
                        "edit_mode": plan.get("edit_mode"),
                        "category_count": len(categories) if isinstance(categories, list) else None,
                    }
                )
            except OSError as e:
                io_errors.append({
                    "file": str(plan_path),
                    "operation": "read/stat YAML plan",
                    "errno": e.errno,
                    "error": str(e),
                    "traceback": _traceback.format_exc(),
                })
                continue

        entries.sort(
            key=lambda item: (item["generated_at"], item["yaml_name"]),
            reverse=True,
        )
        return jsonify(_attach_io_errors({"yaml_files": entries}, io_errors))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-plan/load", methods=["GET"])
def api_experiment_plan_load():
    """Load an existing YAML plan and normalize it for the Batch Generation form."""
    try:
        plan_path = _require_experiment_plan_path(request.args.get("plan_path", ""))
        plan, yaml_content = _read_experiment_plan_yaml(plan_path)
        form_data = _normalize_experiment_plan_for_form(plan)
        log_path = _build_run_full_experiment_log_path(plan_path, plan)
        cli_command = _build_run_full_experiment_cli_command(plan_path, log_path)
        stat = plan_path.stat()
        return jsonify(
            {
                "plan_path": str(plan_path),
                "yaml_name": plan_path.name,
                "generated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "form_data": form_data,
                "yaml_content": yaml_content,
                "cli_command": cli_command,
                "log_path": str(log_path),
            }
        )
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-plan/execute", methods=["POST"])
def api_experiment_plan_execute():
    """Execute an existing YAML plan asynchronously."""
    try:
        data = request.get_json()
        if not data:
            raise ValueError("Request body is required")
        gpu_id = _resolve_run_full_experiment_gpu_id(data.get("gpu_id"))
        plan_path = _require_experiment_plan_path(data.get("plan_path", ""))
        plan, _ = _read_experiment_plan_yaml(plan_path)
        # 不在此处调用 _validate_experiment_plan —— 该校验针对新建表单设计，
        # 对已有 YAML 文件可能误报（如 random.object=true 无 objects 时）。
        # run_full_experiment.py 内部有完整校验，是权威的。

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _build_run_full_experiment_log_path(plan_path, plan)
        cli_command = _build_run_full_experiment_cli_command(
            plan_path,
            log_path,
            gpu_id=gpu_id,
        )
        task_id = create_task(
            "run_full_experiment",
            {
                "plan_path": str(plan_path),
                "log_path": str(log_path),
                "gpu_id": gpu_id,
            },
        )
        return jsonify(
            {
                "task_id": task_id,
                "plan_path": str(plan_path),
                "gpu_id": gpu_id,
                "cli_command": cli_command,
                "log_path": str(log_path),
            }
        )
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-plan/runs", methods=["GET"])
def api_experiment_plan_runs():
    try:
        plan_path = request.args.get("plan_path", "").strip() or None
        entries, io_errors = _collect_experiment_entries(plan_path=plan_path)
        return jsonify(
            _attach_io_errors(
                {
                    "runs": [
                        _build_experiment_run_management_summary(entry)
                        for entry in entries
                    ]
                },
                io_errors,
            )
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-plan/run-detail", methods=["GET"])
def api_experiment_plan_run_detail():
    try:
        experiment_id = request.args.get("experiment_id", "").strip()
        if not experiment_id:
            raise ValueError("Missing required query parameter: experiment_id")
        entry = _get_experiment_entry_by_id(experiment_id)
        manifest = entry["manifest"]
        record_bundle = _load_experiment_records_for_stats(entry)
        state = _build_experiment_run_state(entry, record_bundle)
        summary_path = entry["experiment_dir"] / "summary.json"
        summary = _load_json_file(summary_path) if summary_path.exists() else None
        events_path = entry["experiment_dir"] / "events.jsonl"
        return jsonify(
            {
                "run": _build_experiment_run_management_summary(entry),
                "manifest": manifest,
                "summary": summary,
                "progress": manifest.get("progress") or {},
                "totals": manifest.get("totals") or {},
                "state": state,
                "recent_events": _tail_jsonl_records(events_path, 50),
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-plan/resume", methods=["POST"])
def api_experiment_plan_resume():
    try:
        data = request.get_json()
        if not data:
            raise ValueError("Request body is required")
        gpu_id = _resolve_run_full_experiment_gpu_id(data.get("gpu_id"))
        experiment_id = str(data.get("experiment_id", "")).strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        entry = _get_experiment_entry_by_id(experiment_id)
        run_summary = _build_experiment_run_management_summary(entry)
        if not run_summary["resumable"]:
            raise ValueError(
                f"Experiment {experiment_id} is not resumable (status={run_summary['status']})"
            )
        log_path = _build_run_full_experiment_action_log_path(experiment_id, "resume")
        task_id = create_task(
            "run_full_experiment",
            {
                "resume_experiment_id": experiment_id,
                "log_path": str(log_path),
                "gpu_id": gpu_id,
            },
        )
        return jsonify(
            {
                "task_id": task_id,
                "experiment_id": experiment_id,
                "gpu_id": gpu_id,
                "cli_command": _build_run_full_experiment_resume_cli_command(
                    experiment_id,
                    log_path,
                    gpu_id=gpu_id,
                ),
                "log_path": str(log_path),
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-plan/repair", methods=["POST"])
def api_experiment_plan_repair():
    try:
        data = request.get_json()
        if not data:
            raise ValueError("Request body is required")
        gpu_id = _resolve_run_full_experiment_gpu_id(data.get("gpu_id"))
        experiment_id = str(data.get("experiment_id", "")).strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        _get_experiment_entry_by_id(experiment_id)
        log_path = _build_run_full_experiment_action_log_path(experiment_id, "repair")
        task_id = create_task(
            "run_full_experiment",
            {
                "repair_experiment_id": experiment_id,
                "log_path": str(log_path),
                "gpu_id": gpu_id,
            },
        )
        return jsonify(
            {
                "task_id": task_id,
                "experiment_id": experiment_id,
                "gpu_id": gpu_id,
                "cli_command": _build_run_full_experiment_repair_cli_command(
                    experiment_id,
                    log_path,
                    gpu_id=gpu_id,
                ),
                "log_path": str(log_path),
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-plan/derived-category-workers", methods=["GET"])
def api_experiment_plan_derived_category_workers():
    """Return derived category worker count for a provider pair."""
    source_provider = request.args.get("source_provider", "").strip()
    target_provider = request.args.get("target_provider", "").strip()
    valid_providers = {"hunyuan", "tripo", "rodin"}

    if source_provider not in valid_providers:
        return jsonify({"error": f"Invalid source_provider: {source_provider}"}), 400
    if target_provider not in valid_providers:
        return jsonify({"error": f"Invalid target_provider: {target_provider}"}), 400

    config = load_config()
    limits = get_run_full_experiment_concurrency_limits(
        config=config,
        source_provider=source_provider,
        target_provider=target_provider,
    )
    derived = derive_run_full_experiment_category_workers(
        config=config,
        source_provider=source_provider,
        target_provider=target_provider,
    )
    formula = describe_run_full_experiment_category_workers(
        config=config,
        source_provider=source_provider,
        target_provider=target_provider,
    )
    return jsonify(
        {
            "source_provider": source_provider,
            "target_provider": target_provider,
            "category_workers_derived": derived,
            "category_workers_breakdown": limits,
            "category_workers_formula": formula,
        }
    )


@app.route("/api/experiment-plan/generate", methods=["POST"])
def api_experiment_plan_generate():
    """Validate, generate YAML plan file, and return CLI command.

    Request body: Full experiment plan JSON matching run_full_experiment.py format.

    Returns:
        plan_path: Relative path to generated file
        yaml_content: Generated YAML content
        cli_command: CLI command to run the experiment
    """
    import yaml
    from datetime import datetime

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    # Validate (Fail Loudly)
    errors = _validate_experiment_plan(data)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    # Ensure output directory exists
    EXPERIMENT_PLANS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    target_provider = data.get("target_provider") or data["source_provider"]

    # Build plan dict (matching run_full_experiment.py load_plan format)
    plan = {
        "name": data["name"].strip(),
        "source_provider": data["source_provider"],
        "target_provider": target_provider,
        "edit_mode": data["edit_mode"],
        "categories": [],
    }

    for cat in data["categories"]:
        cat_entry = {
            "random": {
                "category": cat["random"]["category"],
                "object": cat["random"]["object"],
            },
            "object_count": cat["object_count"],
            "instruction_plan": build_instruction_plan(
                count=cat["instruction_plan"]["count"],
                allowed_types=list(cat["instruction_plan"]["allowed_types"]),
            ),
        }
        if not cat["random"]["category"]:
            cat_entry["category_name"] = cat["category_name"]
        if not cat["random"]["object"] and cat.get("objects"):
            cat_entry["objects"] = cat["objects"]
        plan["categories"].append(cat_entry)

    # Generate YAML content
    yaml_content = yaml.safe_dump(plan, sort_keys=False, allow_unicode=True)

    # Generate filename: <timestamp>_<slug(name)>.yaml
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(plan["name"])
    filename = f"{timestamp}_{slug}.yaml"
    file_path = EXPERIMENT_PLANS_DIR / filename

    # Write file
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    # Generate CLI command
    log_path = _build_run_full_experiment_log_path(file_path, plan, timestamp=timestamp)
    cli_command = _build_run_full_experiment_cli_command(file_path, log_path)

    return jsonify(
        {
            "plan_path": str(file_path),
            "yaml_content": yaml_content,
            "cli_command": cli_command,
            "log_path": str(log_path),
        }
    )


def _distribute_balanced(total_objects: int, objects_by_category: dict) -> list:
    """Distribute total_objects evenly across categories, respecting pool size caps.

    Returns list of {category_name, object_count, pool_size} dicts sorted by pool_size desc.
    """
    sorted_cats = sorted(objects_by_category.items(), key=lambda x: -len(x[1]))
    n = len(sorted_cats)
    if n == 0 or total_objects <= 0:
        return []
    base = total_objects // n
    remainder = total_objects % n
    result = []
    assigned_total = 0
    for i, (cat_name, objects) in enumerate(sorted_cats):
        pool_size = len(objects)
        count = base + (1 if i < remainder else 0)
        count = min(count, pool_size)
        if count > 0:
            result.append({"category_name": cat_name, "object_count": count, "pool_size": pool_size})
            assigned_total += count
    # If any pool caps caused under-assignment, redistribute leftover to categories with spare capacity
    leftover = total_objects - assigned_total
    for item in result:
        if leftover <= 0:
            break
        spare = item["pool_size"] - item["object_count"]
        add = min(spare, leftover)
        item["object_count"] += add
        leftover -= add
    return [item for item in result if item["object_count"] > 0]


@app.route("/api/experiment-plan/generate-balanced", methods=["POST"])
def api_experiment_plan_generate_balanced():
    """Generate a YAML plan with objects balanced across all categories.

    Request body:
        name: Plan name (required)
        total_objects: Total number of objects to generate (required)
        edits_per_object: Number of edit instructions per object (required)
        source_provider: 3D generation provider (required)
        target_provider: 3D generation provider for target (defaults to source_provider)
        edit_mode: Edit mode - single or multiview (required)
        allowed_types: List of allowed instruction types (default: ["remove", "replace"])
        gpu_id: GPU index for CLI command (default: 0)

    Returns:
        plan_path, yaml_content, cli_command, log_path, distribution, total_objects, total_edits
    """
    import yaml as _yaml

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    errors = []
    name = str(data.get("name", "")).strip()
    if not name:
        errors.append("name is required")
    elif ".." in name or "/" in name or "\\" in name:
        errors.append("name contains invalid characters")

    total_objects = data.get("total_objects")
    if not isinstance(total_objects, int) or total_objects < 1:
        errors.append("total_objects must be a positive integer")

    edits_per_object = data.get("edits_per_object")
    if not isinstance(edits_per_object, int) or edits_per_object < 1:
        errors.append("edits_per_object must be a positive integer")

    valid_providers = {"hunyuan", "tripo", "rodin"}
    source_provider = str(data.get("source_provider", "")).strip()
    if source_provider not in valid_providers:
        errors.append(f"source_provider must be one of {sorted(valid_providers)}")

    target_provider = str(data.get("target_provider") or data.get("source_provider", "")).strip()
    if target_provider not in valid_providers:
        errors.append(f"target_provider must be one of {sorted(valid_providers)}")

    edit_mode = str(data.get("edit_mode", "")).strip()
    if edit_mode not in {"single", "multiview"}:
        errors.append("edit_mode must be 'single' or 'multiview'")

    allowed_types_raw = data.get("allowed_types", ["remove", "replace"])
    if not isinstance(allowed_types_raw, list) or not allowed_types_raw:
        errors.append("allowed_types must be a non-empty list")
    else:
        invalid_types = [t for t in allowed_types_raw if t not in {"remove", "replace"}]
        if invalid_types:
            errors.append(f"allowed_types contains invalid values: {invalid_types}")

    try:
        gpu_id = _resolve_run_full_experiment_gpu_id(data.get("gpu_id"))
    except ValueError as exc:
        errors.append(str(exc))
        gpu_id = DEFAULT_RUN_FULL_EXPERIMENT_GPU_ID

    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    if not CATEGORIZED_OBJECTS_FILE.exists():
        return jsonify({"error": "categorized_objects.json not found"}), 500
    with open(CATEGORIZED_OBJECTS_FILE, "r", encoding="utf-8") as f:
        objects_by_category = json.load(f)

    total_capacity = sum(len(objs) for objs in objects_by_category.values())
    if total_objects > total_capacity:
        return jsonify({
            "error": f"total_objects ({total_objects}) exceeds total available objects ({total_capacity})"
        }), 400

    distribution = _distribute_balanced(total_objects, objects_by_category)
    actual_total = sum(item["object_count"] for item in distribution)

    instruction_plan = {
        "mode": "adaptive_k",
        "count": edits_per_object,
        "allowed_types": list(allowed_types_raw),
    }
    plan = {
        "name": name,
        "source_provider": source_provider,
        "target_provider": target_provider,
        "edit_mode": edit_mode,
        "categories": [
            {
                "category_name": item["category_name"],
                "random": {"category": False, "object": True},
                "object_count": item["object_count"],
                "instruction_plan": instruction_plan,
            }
            for item in distribution
        ],
    }

    yaml_content = _yaml.safe_dump(plan, sort_keys=False, allow_unicode=True)

    EXPERIMENT_PLANS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(name)
    filename = f"{timestamp}_{slug}.yaml"
    file_path = EXPERIMENT_PLANS_DIR / filename
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    log_path = _build_run_full_experiment_log_path(file_path, plan, timestamp=timestamp)
    cli_command = _build_run_full_experiment_cli_command(file_path, log_path, gpu_id=gpu_id)

    return jsonify({
        "plan_path": str(file_path),
        "yaml_content": yaml_content,
        "cli_command": cli_command,
        "log_path": str(log_path),
        "distribution": distribution,
        "total_objects": actual_total,
        "total_edits": actual_total * edits_per_object,
    })


@app.route("/api/experiment-plan/cli-command", methods=["POST"])
def api_experiment_plan_cli_command():
    """Return the CLI command to execute an existing YAML plan without starting a task."""
    try:
        data = request.get_json()
        if not data:
            raise ValueError("Request body is required")
        gpu_id = _resolve_run_full_experiment_gpu_id(data.get("gpu_id"))
        plan_path = _require_experiment_plan_path(data.get("plan_path", ""))
        plan, _ = _read_experiment_plan_yaml(plan_path)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _build_run_full_experiment_log_path(plan_path, plan)
        cli_command = _build_run_full_experiment_cli_command(plan_path, log_path, gpu_id=gpu_id)
        return jsonify({
            "plan_path": str(plan_path),
            "cli_command": cli_command,
            "log_path": str(log_path),
        })
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-plan/resume-cli-command", methods=["POST"])
def api_experiment_plan_resume_cli_command():
    """Return the resume CLI command for an experiment without creating a task."""
    try:
        data = request.get_json()
        if not data:
            raise ValueError("Request body is required")
        gpu_id = _resolve_run_full_experiment_gpu_id(data.get("gpu_id"))
        experiment_id = str(data.get("experiment_id", "")).strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        entry = _get_experiment_entry_by_id(experiment_id)
        run_summary = _build_experiment_run_management_summary(entry)
        log_path = _build_run_full_experiment_action_log_path(experiment_id, "resume")
        cli_command = _build_run_full_experiment_resume_cli_command(
            experiment_id, log_path, gpu_id=gpu_id
        )
        return jsonify({
            "experiment_id": experiment_id,
            "cli_command": cli_command,
            "log_path": str(log_path),
            "resumable": run_summary["resumable"],
            "status": run_summary["status"],
            "name": run_summary.get("name"),
        })
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _get_pipeline_experiments_dir() -> Path:
    config = load_config()
    pipeline_dir_raw = config.workspace.pipeline_dir
    pipeline_dir = (
        Path(pipeline_dir_raw)
        if Path(pipeline_dir_raw).is_absolute()
        else PROJECT_ROOT / pipeline_dir_raw
    )
    return pipeline_dir / "experiments"


def _read_jsonl_records(path: Path) -> list:
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


VALID_EXPERIMENT_PROVIDERS = {"hunyuan", "tripo", "rodin"}

# ---------------------------------------------------------------------------
# SeaweedFS IO helpers – retry on errno 5 with full diagnostic context
# ---------------------------------------------------------------------------
_SEAWEEDFS_IO_RETRIES = 3


def _seaweedfs_read_bytes(path: Path, *, label: str = "") -> bytes:
    """Read bytes from a SeaweedFS-hosted file with errno-5 retry.

    Args:
        path: File to read.
        label: Human-readable context for error messages, e.g.
               "manifest.json for experiment 20260325_060256".
    Raises:
        FileNotFoundError: path does not exist.
        OSError: read failed after all retries (original errno preserved,
                 message enriched with label/path/attempt info).
    """
    context = label or str(path)
    if not path.exists():
        raise FileNotFoundError(
            f"[seaweedfs] file not found: {path} (context: {context})"
        )
    for attempt in range(_SEAWEEDFS_IO_RETRIES):
        try:
            return path.read_bytes()
        except OSError as e:
            if e.errno == 5 and attempt < _SEAWEEDFS_IO_RETRIES - 1:
                time.sleep(0.2 * (2 ** attempt))
                continue
            raise OSError(
                e.errno,
                f"[seaweedfs] read_bytes failed after {attempt + 1} attempt(s) "
                f"| file: {path} | context: {context} | original: {e}",
            ) from e
    raise RuntimeError("unreachable")  # satisfies type checker


def _seaweedfs_read_text(
    path: Path, *, label: str = "", encoding: str = "utf-8"
) -> str:
    """Like _seaweedfs_read_bytes but returns decoded text."""
    return _seaweedfs_read_bytes(path, label=label).decode(encoding)


def _load_json_file(path: Path, *, label: str = "") -> dict:
    text = _seaweedfs_read_text(path, label=label or path.name)
    return json.loads(text)


def _require_jsonl_records(path: Path, label: str) -> list:
    if not path.exists():
        raise FileNotFoundError(f"Missing required {label}: {path}")
    return _read_jsonl_records(path)


def _load_experiment_records_for_stats(entry: dict) -> dict:
    experiment_dir = entry["experiment_dir"]
    object_records_path = experiment_dir / "object_records.jsonl"
    edit_records_path = experiment_dir / "edit_records.jsonl"
    partial_recovered = False

    if object_records_path.exists() and edit_records_path.exists():
        return {
            "object_records": _require_jsonl_records(
                object_records_path, "object_records.jsonl"
            ),
            "edit_records": _require_jsonl_records(
                edit_records_path, "edit_records.jsonl"
            ),
            "partial_recovered": partial_recovered,
        }

    recovered = recover_experiment_records(
        entry["experiment_id"],
        write_files=False,
    )
    return {
        "object_records": recovered["object_records"],
        "edit_records": recovered["edit_records"],
        "partial_recovered": True,
    }


def _resolve_recorded_path(recorded_path: str) -> Path:
    path = Path(recorded_path)
    if path.is_absolute():
        return path
    if recorded_path.startswith("pipeline/"):
        relative = recorded_path[len("pipeline/") :]
        return PIPELINE_DIR / relative
    return PROJECT_ROOT / recorded_path


def _validate_experiment_provider(provider_name: str, provider_value: str) -> None:
    if provider_value not in VALID_EXPERIMENT_PROVIDERS:
        raise ValueError(f"Invalid {provider_name}: {provider_value}")


def _collect_experiment_entries(
    *,
    source_provider: str = None,
    target_provider: str = None,
    plan_path: str = None,
) -> tuple:
    """Return ``(entries, io_errors)`` – Fail Loudly compatible.

    IO errors (typically SeaweedFS errno 5) are collected per-experiment with
    full diagnostic context instead of crashing the whole listing.  Callers
    MUST surface ``io_errors`` to the frontend so failures are never hidden.
    """
    experiments_dir = _get_pipeline_experiments_dir()
    if not experiments_dir.exists():
        return [], []

    entries = []
    io_errors = []
    for experiment_dir in experiments_dir.iterdir():
        if not experiment_dir.is_dir():
            continue

        manifest_path = experiment_dir / "manifest.json"
        if not manifest_path.exists():
            continue

        try:
            manifest = _load_json_file(
                manifest_path,
                label=f"manifest.json for experiment {experiment_dir.name}",
            )
        except OSError as e:
            io_errors.append({
                "experiment_dir": str(experiment_dir),
                "experiment_id": experiment_dir.name,
                "file": str(manifest_path),
                "operation": "read manifest.json",
                "errno": e.errno,
                "error": str(e),
                "traceback": _traceback.format_exc(),
            })
            continue

        experiment_id = require_param(manifest, "experiment_id", "experiment manifest")
        manifest_source = require_param(
            manifest, "source_provider", "experiment manifest"
        )
        manifest_target = require_param(
            manifest, "target_provider", "experiment manifest"
        )
        manifest_plan_path = require_param(manifest, "plan_path", "experiment manifest")

        if source_provider and manifest_source != source_provider:
            continue
        if target_provider and manifest_target != target_provider:
            continue
        if plan_path and manifest_plan_path != plan_path:
            continue

        entries.append(
            {
                "experiment_id": experiment_id,
                "experiment_dir": experiment_dir,
                "manifest_path": manifest_path,
                "manifest": manifest,
            }
        )

    entries.sort(
        key=lambda entry: (
            entry["manifest"].get("finished_at") or "",
            entry["manifest"].get("started_at") or "",
            entry["experiment_id"],
        ),
        reverse=True,
    )
    return entries, io_errors


def _attach_io_errors(response: dict, io_errors: list) -> dict:
    """Merge io_errors into a JSON response dict (Fail Loudly helper)."""
    if io_errors:
        response["io_errors"] = io_errors
        response["warning"] = (
            f"{len(io_errors)} experiment(s) could not be read due to "
            "SeaweedFS IO errors. Details in io_errors field."
        )
    return response


def _empty_models_experiment_filter_payload(error_message: str = None) -> dict:
    payload = {
        "providers": sorted(VALID_EXPERIMENT_PROVIDERS),
        "provider_pairs": [],
        "yaml_files": [],
        "model_index": {},
    }
    if error_message:
        payload["error"] = error_message
    return payload


def _scan_models_experiment_filter_payload() -> dict:
    """Build source-model -> experiment associations for Models page filters."""
    entries, _io_errors = _collect_experiment_entries()
    model_index = {}
    yaml_groups = {}
    provider_pair_keys = set()

    for entry in entries:
        manifest = entry["manifest"]
        experiment_dir = entry["experiment_dir"]
        source_provider = require_param(
            manifest, "source_provider", "experiment manifest"
        )
        target_provider = require_param(
            manifest, "target_provider", "experiment manifest"
        )
        plan_path = require_param(manifest, "plan_path", "experiment manifest")
        yaml_name = Path(plan_path).name
        run_time = manifest.get("finished_at") or manifest.get("started_at") or ""

        provider_pair_keys.add((source_provider, target_provider))

        yaml_bucket = yaml_groups.setdefault(
            plan_path,
            {
                "plan_path": plan_path,
                "yaml_name": yaml_name,
                "run_count": 0,
                "latest_run_time": None,
                "model_ids": set(),
                "source_provider": source_provider,
                "target_provider": target_provider,
            },
        )
        yaml_bucket["run_count"] += 1
        if run_time and (
            yaml_bucket["latest_run_time"] is None
            or run_time > yaml_bucket["latest_run_time"]
        ):
            yaml_bucket["latest_run_time"] = run_time

        outputs = manifest.get("outputs")
        object_like_records = []
        object_records_path = experiment_dir / "object_records.jsonl"
        if object_records_path.exists():
            object_like_records = _require_jsonl_records(
                object_records_path, "object_records.jsonl"
            )
        else:
            prompt_records_path = None
            if isinstance(outputs, dict):
                prompt_records_raw = outputs.get("prompt_records")
                if isinstance(prompt_records_raw, str) and prompt_records_raw.strip():
                    prompt_records_path = _resolve_recorded_path(prompt_records_raw)
            if prompt_records_path and prompt_records_path.exists():
                object_like_records = _require_jsonl_records(
                    prompt_records_path, "prompt_records.jsonl"
                )

        for record in object_like_records:
            source_model_id = _normalize_source_model_id_for_listing(
                record, "object/prompt record"
            )
            if source_model_id is None:
                continue
            bucket = model_index.setdefault(
                source_model_id,
                {
                    "source_providers": set(),
                    "target_providers": set(),
                    "provider_pairs": set(),
                    "plan_paths": set(),
                    "yaml_names": set(),
                    "experiment_ids": set(),
                },
            )
            bucket["source_providers"].add(source_provider)
            bucket["target_providers"].add(target_provider)
            bucket["provider_pairs"].add(f"{source_provider}::{target_provider}")
            bucket["plan_paths"].add(plan_path)
            bucket["yaml_names"].add(yaml_name)
            bucket["experiment_ids"].add(entry["experiment_id"])
            yaml_bucket["model_ids"].add(source_model_id)

    yaml_files = []
    for bucket in yaml_groups.values():
        model_count = len(bucket["model_ids"])
        yaml_files.append(
            {
                "plan_path": bucket["plan_path"],
                "yaml_name": bucket["yaml_name"],
                "run_count": bucket["run_count"],
                "latest_run_time": bucket["latest_run_time"],
                "model_count": model_count,
                "source_provider": bucket["source_provider"],
                "target_provider": bucket["target_provider"],
                "label": (
                    f"{bucket['yaml_name']} ({model_count} models, "
                    f"{bucket['run_count']} runs)"
                ),
            }
        )

    yaml_files.sort(
        key=lambda item: (item["latest_run_time"] or "", item["yaml_name"]),
        reverse=True,
    )

    serialized_model_index = {}
    for model_id, bucket in model_index.items():
        serialized_model_index[model_id] = {
            "source_providers": sorted(bucket["source_providers"]),
            "target_providers": sorted(bucket["target_providers"]),
            "provider_pairs": sorted(bucket["provider_pairs"]),
            "plan_paths": sorted(bucket["plan_paths"]),
            "yaml_names": sorted(bucket["yaml_names"]),
            "experiment_ids": sorted(bucket["experiment_ids"]),
        }

    provider_pairs = [
        {
            "source_provider": source_provider,
            "target_provider": target_provider,
            "label": f"{source_provider} -> {target_provider}",
        }
        for source_provider, target_provider in sorted(provider_pair_keys)
    ]

    return {
        "providers": sorted(VALID_EXPERIMENT_PROVIDERS),
        "provider_pairs": provider_pairs,
        "yaml_files": yaml_files,
        "model_index": serialized_model_index,
    }


def _build_models_experiment_filter_payload() -> dict:
    """Load Models page experiment filter payload with a short cache."""
    return _get_cached_experiment_metadata(
        "models_experiment_filters", _scan_models_experiment_filter_payload
    )


def _get_stage2_scores(edit_records: list) -> list:
    return [
        float(record["stage2_score"])
        for record in edit_records
        if isinstance(record.get("stage2_score"), (int, float))
    ]


def _collect_timing_entries(records: list, field_name: str) -> list:
    entries = []
    for record in records:
        container = record.get(field_name, {})
        if not isinstance(container, dict):
            continue
        if field_name == "timings":
            for timing in container.values():
                if isinstance(timing, dict):
                    entries.append(timing)
            continue
        for timing_list in container.values():
            if not isinstance(timing_list, list):
                continue
            for timing in timing_list:
                if isinstance(timing, dict):
                    entries.append(timing)
    return entries


def _summarize_timing_entries(entries: list, aggregation_basis: str) -> list:
    buckets = {}
    for entry in entries:
        stage_name = entry.get("stage_name")
        scope = entry.get("scope")
        elapsed_seconds = entry.get("elapsed_seconds")
        if not isinstance(stage_name, str) or not stage_name:
            continue
        if not isinstance(scope, str) or not scope:
            scope = "unknown"
        if not isinstance(elapsed_seconds, (int, float)):
            continue
        buckets.setdefault((scope, stage_name), []).append(entry)

    rows = []
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


def _build_stage_timing_summary(object_records: list, edit_records: list) -> dict:
    merged_records = [*object_records, *edit_records]
    attempt_summary = _summarize_timing_entries(
        _collect_timing_entries(merged_records, "timing_attempts"),
        "attempt",
    )
    final_record_summary = _summarize_timing_entries(
        _collect_timing_entries(merged_records, "timings"),
        "final_record",
    )
    return {
        "attempt_timing_summary": attempt_summary,
        "final_record_timing_summary": final_record_summary,
        "stage_timing_summary": attempt_summary,
    }


def _aggregate_category_stats(object_records: list, edit_records: list) -> list:
    category_data = {}

    for record in object_records:
        category = record.get("category")
        if not category:
            continue
        bucket = category_data.setdefault(
            category,
            {
                "objects": set(),
                "sample_count": 0,
                "edit_attempts_total": 0,
                "stage1_failed_count": 0,
                "stage2_entered_count": 0,
                "stage2_passed_count": 0,
                "stage2_failed_count": 0,
                "scores": [],
            },
        )
        object_name = record.get("object_name")
        if isinstance(object_name, str) and object_name:
            bucket["objects"].add(object_name)
        bucket["sample_count"] += 1

    for record in edit_records:
        category = record.get("category")
        if not category:
            continue
        bucket = category_data.setdefault(
            category,
            {
                "objects": set(),
                "sample_count": 0,
                "edit_attempts_total": 0,
                "stage1_failed_count": 0,
                "stage2_entered_count": 0,
                "stage2_passed_count": 0,
                "stage2_failed_count": 0,
                "scores": [],
            },
        )
        bucket["edit_attempts_total"] += 1
        stage1_status = record.get("stage1_status")
        if stage1_status in {
            EDIT_STATUS_FAILED_QUALITY,
            EDIT_STATUS_ERROR_QUALITY_CHECK,
        }:
            bucket["stage1_failed_count"] += 1
        if record.get("entered_stage2"):
            bucket["stage2_entered_count"] += 1
        if record.get("stage2_status") == EDIT_STATUS_PASSED:
            bucket["stage2_passed_count"] += 1
        if record.get("stage2_status") == EDIT_STATUS_FAILED_QUALITY:
            bucket["stage2_failed_count"] += 1
        score = record.get("stage2_score")
        if isinstance(score, (int, float)):
            bucket["scores"].append(float(score))

    rows = []
    for category, bucket in sorted(category_data.items()):
        edit_attempts_total = bucket["edit_attempts_total"]
        stage2_entered_count = bucket["stage2_entered_count"]
        scores = bucket["scores"]
        rows.append(
            {
                "category": category,
                "distinct_objects": sorted(bucket["objects"]),
                "sample_count": bucket["sample_count"],
                "edit_attempts_total": edit_attempts_total,
                "stage1_failed_count": bucket["stage1_failed_count"],
                "stage1_failed_rate": round(
                    bucket["stage1_failed_count"] / edit_attempts_total, 6
                )
                if edit_attempts_total
                else None,
                "stage2_entered_count": stage2_entered_count,
                "stage2_entered_rate": round(
                    stage2_entered_count / edit_attempts_total, 6
                )
                if edit_attempts_total
                else None,
                "stage2_passed_count": bucket["stage2_passed_count"],
                "stage2_failed_count": bucket["stage2_failed_count"],
                "stage2_lpips_mean": round(sum(scores) / len(scores), 6)
                if scores
                else None,
                "stage2_lpips_std": round(statistics.pstdev(scores), 6)
                if scores
                else None,
            }
        )

    rows.sort(key=lambda item: (-item["sample_count"], item["category"]))
    return rows


def _build_object_stats(object_records: list) -> list:
    def _get_stage2_metric_value(record: dict, lpips_key: str, legacy_key: str):
        value = record.get(lpips_key)
        if value is None:
            value = record.get(legacy_key)
        return value

    rows = []
    for record in object_records:
        rows.append(
            {
                "experiment_id": record.get("experiment_id"),
                "plan_index": record.get("plan_index"),
                "object_index": record.get("object_index"),
                "selection_mode": record.get("selection_mode"),
                "category": record.get("category"),
                "object_name": record.get("object_name"),
                "prompt_id": record.get("prompt_id"),
                "image_id": record.get("image_id"),
                "source_model_id": record.get("source_model_id"),
                "edit_attempts_total": record.get("attempts_total"),
                "stage1_failed_count": record.get("stage1_failed_count"),
                "stage2_entered_count": record.get("stage2_entered_count"),
                "stage2_passed_count": record.get("stage2_passed_count"),
                "stage2_failed_count": record.get("stage2_failed_count"),
                "stage2_lpips_mean": _get_stage2_metric_value(
                    record, "stage2_lpips_mean", "stage2_dreamsim_mean"
                ),
                "stage2_lpips_std": _get_stage2_metric_value(
                    record, "stage2_lpips_std", "stage2_dreamsim_std"
                ),
                "source_pipeline_status": record.get("source_pipeline_status"),
            }
        )
    rows.sort(
        key=lambda row: (
            row["category"] or "",
            row["object_name"] or "",
            row["experiment_id"] or "",
            row["object_index"] or 0,
        )
    )
    return rows


def _build_edit_rows(edit_records: list) -> list:
    rows = []
    for record in edit_records:
        instruction_payload = resolve_instruction_display_from_record(record)
        rows.append(
            {
                "experiment_id": record.get("experiment_id"),
                "category": record.get("category"),
                "object_name": record.get("object_name"),
                "instruction_type": record.get("instruction_type"),
                "instruction_index": record.get("instruction_index"),
                "instruction_text": instruction_payload.get("instruction_display_text"),
                "prompt_id": record.get("prompt_id"),
                "image_id": record.get("image_id"),
                "source_model_id": record.get("source_model_id"),
                "edit_id": record.get("edit_id"),
                "target_model_id": record.get("target_model_id"),
                "stage1_status": record.get("stage1_status"),
                "stage1_reason": record.get("stage1_reason"),
                "stage2_status": record.get("stage2_status"),
                "stage2_score": record.get("stage2_score"),
                "stage2_input_mode": record.get("stage2_input_mode"),
                "stage2_views": record.get("stage2_views"),
                "final_status": record.get("final_status"),
                "exclusion_reason": record.get("exclusion_reason"),
                "created_at": record.get("created_at"),
            }
        )
    rows.sort(
        key=lambda row: (
            row["category"] or "",
            row["object_name"] or "",
            row["experiment_id"] or "",
            row["instruction_index"] or 0,
        )
    )
    return rows


def _build_yaml_summary(entries: list) -> dict:
    if not entries:
        raise ValueError("No experiment entries found for the requested YAML.")

    manifest = entries[0]["manifest"]
    plan = require_param(manifest, "plan", "experiment manifest")
    categories = require_param(plan, "categories", "experiment plan")
    plan_path = require_param(manifest, "plan_path", "experiment manifest")
    plan_file_path = _resolve_recorded_path(plan_path)
    plan_file_exists = plan_file_path.exists()
    raw_yaml = None
    if plan_file_exists:
        with open(plan_file_path, "r", encoding="utf-8") as f:
            raw_yaml = f.read()

    latest_run_time = max(
        entry["manifest"].get("finished_at") or entry["manifest"].get("started_at") or ""
        for entry in entries
    )

    plan_categories = []
    for index, category in enumerate(categories, start=1):
        random_cfg = require_param(category, "random", "experiment plan category")
        instruction_plan = normalize_instruction_plan_from_category(
            category,
            "experiment plan category",
            allow_legacy_counts=True,
        )
        plan_categories.append(
            {
                "index": index,
                "category_name": category.get("category_name"),
                "random": {
                    "category": random_cfg["category"],
                    "object": random_cfg["object"],
                },
                "object_count": category.get("object_count"),
                "objects": category.get("objects") or [],
                "instruction_plan": instruction_plan,
            }
        )

    return {
        "plan_path": plan_path,
        "yaml_name": Path(plan_path).name,
        "plan_name": manifest.get("name"),
        "source_provider": manifest.get("source_provider"),
        "target_provider": manifest.get("target_provider"),
        "edit_mode": manifest.get("edit_mode"),
        "category_count": len(categories),
        "experiment_run_count": len(entries),
        "latest_run_time": latest_run_time or None,
        "plan_file_exists": plan_file_exists,
        "plan_file_path": str(plan_file_path),
        "plan_categories": plan_categories,
        "raw_yaml": raw_yaml,
    }


def _build_experiment_run_summary(entry: dict) -> dict:
    manifest = entry["manifest"]
    record_bundle = _load_experiment_records_for_stats(entry)
    object_records = record_bundle["object_records"]
    edit_records = record_bundle["edit_records"]
    stage2_scores = _get_stage2_scores(edit_records)
    totals = manifest.get("totals") or {}
    summary_path = entry["experiment_dir"] / "summary.json"
    summary_payload = _load_json_file(summary_path) if summary_path.exists() else {}
    if not isinstance(summary_payload, dict):
        summary_payload = {}
    stage_timing_summary = (
        summary_payload.get("stage_timing_summary")
        if isinstance(summary_payload.get("stage_timing_summary"), list)
        else _build_stage_timing_summary(object_records, edit_records)[
            "stage_timing_summary"
        ]
    )

    return {
        "experiment_id": entry["experiment_id"],
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "object_count": totals.get("object_count", len(object_records)),
        "edit_attempts_total": totals.get("edit_attempts_total", len(edit_records)),
        "stage1_failed_count": totals.get(
            "stage1_failed_count",
            sum(
                1
                for record in edit_records
                if record.get("stage1_status")
                in {EDIT_STATUS_FAILED_QUALITY, EDIT_STATUS_ERROR_QUALITY_CHECK}
            ),
        ),
        "stage2_entered_count": totals.get(
            "stage2_entered_count",
            sum(1 for record in edit_records if record.get("entered_stage2")),
        ),
        "stage2_passed_count": totals.get(
            "stage2_passed_count",
            sum(
                1
                for record in edit_records
                if record.get("stage2_status") == EDIT_STATUS_PASSED
            ),
        ),
        "stage2_failed_count": totals.get(
            "stage2_failed_count",
            sum(
                1
                for record in edit_records
                if record.get("stage2_status") == EDIT_STATUS_FAILED_QUALITY
            ),
        ),
        "stage2_lpips_mean": round(statistics.mean(stage2_scores), 6)
        if stage2_scores
        else None,
        "stage2_lpips_std": round(statistics.pstdev(stage2_scores), 6)
        if stage2_scores
        else None,
        "manifest_path": _rel_path(entry["manifest_path"]),
        "is_partial": record_bundle["partial_recovered"],
        "status": manifest.get("status") or ("completed" if manifest.get("finished_at") else "running"),
        "stage_timing_summary": stage_timing_summary,
    }


def _get_experiment_entry_by_id(experiment_id: str) -> dict:
    entries, _io_errors = _collect_experiment_entries()
    for entry in entries:
        if entry["experiment_id"] == experiment_id:
            return entry
    raise FileNotFoundError(f"Experiment not found: {experiment_id}")


def _build_experiment_run_state(entry: dict, record_bundle: dict) -> dict:
    manifest = entry["manifest"]
    object_records = record_bundle["object_records"]
    edit_records = record_bundle["edit_records"]
    totals = manifest.get("totals") or {}
    progress = manifest.get("progress") or {}
    planned_object_count = progress.get("planned_object_count")
    planned_edit_count = progress.get("planned_edit_count")
    object_record_count = progress.get("object_record_count", len(object_records))
    edit_record_count = progress.get("edit_record_count", len(edit_records))
    source_pipeline_failed_count = sum(
        1
        for record in object_records
        if record.get("source_pipeline_status") == "failed"
    )
    stage1_failed_count = totals.get(
        "stage1_failed_count",
        sum(
            1
            for record in edit_records
            if record.get("stage1_status")
            in {EDIT_STATUS_FAILED_QUALITY, EDIT_STATUS_ERROR_QUALITY_CHECK}
        ),
    )
    stage2_failed_count = totals.get(
        "stage2_failed_count",
        sum(
            1
            for record in edit_records
            if record.get("stage2_status") == EDIT_STATUS_FAILED_QUALITY
        ),
    )
    stage2_error_count = sum(
        1
        for record in edit_records
        if record.get("stage2_status") == EDIT_STATUS_ERROR_QUALITY_CHECK
    )
    has_failures = any(
        [
            source_pipeline_failed_count > 0,
            stage1_failed_count > 0,
            stage2_failed_count > 0,
            stage2_error_count > 0,
        ]
    )
    raw_status = str(manifest.get("status") or "").strip() or None
    is_finished = bool(manifest.get("finished_at")) or raw_status == "completed"
    repair_recommended = bool(record_bundle["partial_recovered"])
    if repair_recommended:
        derived_status = "repair_needed"
    elif is_finished and has_failures:
        derived_status = "completed_with_failures"
    elif is_finished:
        derived_status = "completed"
    elif raw_status in {"running", "interrupted"}:
        derived_status = "interrupted"
    else:
        derived_status = raw_status or "interrupted"

    resumable = derived_status in {"interrupted", "repair_needed"}
    return {
        "raw_status": raw_status,
        "status": derived_status,
        "resumable": resumable,
        "repair_recommended": repair_recommended,
        "planned_object_count": planned_object_count,
        "planned_edit_count": planned_edit_count,
        "object_record_count": object_record_count,
        "edit_record_count": edit_record_count,
        "source_pipeline_failed_count": source_pipeline_failed_count,
        "stage1_failed_count": stage1_failed_count,
        "stage2_failed_count": stage2_failed_count,
        "stage2_error_count": stage2_error_count,
    }


def _build_experiment_run_management_summary(entry: dict) -> dict:
    manifest = entry["manifest"]
    record_bundle = _load_experiment_records_for_stats(entry)
    state = _build_experiment_run_state(entry, record_bundle)
    experiment_id = entry["experiment_id"]
    gpu_id = manifest.get("gpu_id", DEFAULT_RUN_FULL_EXPERIMENT_GPU_ID)
    plan_path = require_param(manifest, "plan_path", "experiment manifest")
    resume_log_path = _build_run_full_experiment_action_log_path(experiment_id, "resume")
    repair_log_path = _build_run_full_experiment_action_log_path(experiment_id, "repair")
    return {
        "experiment_id": experiment_id,
        "plan_path": plan_path,
        "yaml_name": Path(plan_path).name,
        "name": manifest.get("name"),
        "source_provider": manifest.get("source_provider"),
        "target_provider": manifest.get("target_provider"),
        "edit_mode": manifest.get("edit_mode"),
        "gpu_id": gpu_id,
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "manifest_path": _rel_path(entry["manifest_path"]),
        "resume_cli_command": _build_run_full_experiment_resume_cli_command(
            experiment_id,
            resume_log_path,
            gpu_id=gpu_id,
        ),
        "repair_cli_command": _build_run_full_experiment_repair_cli_command(
            experiment_id,
            repair_log_path,
            gpu_id=gpu_id,
        ),
        "resume_log_path": str(resume_log_path),
        "repair_log_path": str(repair_log_path),
        "is_partial": record_bundle["partial_recovered"],
        **state,
    }


def _tail_jsonl_records(path: Path, limit: int) -> list:
    records = _read_jsonl_records(path)
    if limit <= 0:
        return records
    return records[-limit:]


# ==================== Matrix Generation API ====================


@app.route("/api/matrix/gen-pairs-cli", methods=["GET"])
def api_matrix_gen_pairs_cli():
    """Return the CLI command for generating the object-style pairs JSON file."""
    config = load_config()
    objects_file = config.workspace.matrix_objects_file
    styles_file = config.workspace.matrix_styles_file

    # Resolve relative paths against project root
    objects_path = Path(objects_file)
    styles_path = Path(styles_file)
    if not objects_path.is_absolute():
        objects_path = PROJECT_ROOT / objects_path
    if not styles_path.is_absolute():
        styles_path = PROJECT_ROOT / styles_path

    output_path = objects_path.parent / "categorized_object_style_pairs.json"

    cli_command = (
        f"{config.workspace.python_interpreter} "
        f"{PROJECT_ROOT / 'scripts' / 'batch_process.py'} gen-matrix --init "
        f"--objects-file {objects_path} "
        f"--styles-file {styles_path} "
        f"--output {output_path}"
    )

    return jsonify({
        "cli_command": cli_command,
        "objects_file": str(objects_path),
        "styles_file": str(styles_path),
        "output_file": str(output_path),
    })


@app.route("/api/matrix/pairs-status", methods=["GET"])
def api_matrix_pairs_status():
    """Return metadata about the current matrix_pairs.json file."""
    config = load_config()
    pairs_file = PROJECT_ROOT / "data" / "matrix_pairs.json"
    if not pairs_file.exists():
        return jsonify({
            "error": "Pairs file not found. Generate it first.",
            "pairs_file": str(pairs_file),
            "python_interpreter": config.workspace.python_interpreter,
            "project_root": str(PROJECT_ROOT),
        })

    try:
        with open(pairs_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("metadata", {})
        return jsonify({
            "pairs_file": str(pairs_file),
            "total_pairs": meta.get("total_pairs", len(data.get("pairs", []))),
            "total_objects": meta.get("total_objects", 0),
            "total_categories": meta.get("total_categories", 0),
            "total_rounds": meta.get("total_rounds", 0),
            "sort_key": meta.get("sort_key", ""),
            "generated_at": meta.get("generated_at", ""),
            "round_summary": data.get("round_summary", []),
            "python_interpreter": config.workspace.python_interpreter,
            "project_root": str(PROJECT_ROOT),
        })
    except Exception as exc:
        return jsonify({"error": f"Failed to read pairs file: {exc}"}), 500


@app.route("/api/matrix/generate-plan", methods=["POST"])
def api_matrix_generate_plan():
    """Generate a YAML plan from matrix_pairs.json and return CLI command.

    Request body:
        start: Start index in pairs list (required)
        count: Number of pairs (required)
        edits_per_object: Edit instructions per object (default: 4)
        source_provider: 3D provider (default: hunyuan)
        target_provider: 3D provider (default: hunyuan)
        edit_mode: single or multiview (default: multiview)
        gpu_id: GPU index (default: 0)
        name: Batch name (optional, auto-generated if empty)
    """
    import yaml as _yaml
    from collections import OrderedDict

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    errors = []
    start = data.get("start")
    if not isinstance(start, int) or start < 0:
        errors.append("start must be a non-negative integer")

    count = data.get("count")
    if not isinstance(count, int) or count < 1:
        errors.append("count must be a positive integer")

    edits_per_object = data.get("edits_per_object", 4)
    if not isinstance(edits_per_object, int) or edits_per_object < 1:
        errors.append("edits_per_object must be a positive integer")

    valid_providers = {"hunyuan", "tripo", "rodin"}
    source_provider = str(data.get("source_provider", "hunyuan")).strip()
    if source_provider not in valid_providers:
        errors.append(f"source_provider must be one of {sorted(valid_providers)}")

    target_provider = str(data.get("target_provider", "hunyuan")).strip()
    if target_provider not in valid_providers:
        errors.append(f"target_provider must be one of {sorted(valid_providers)}")

    edit_mode = str(data.get("edit_mode", "multiview")).strip()
    if edit_mode not in {"single", "multiview"}:
        errors.append("edit_mode must be 'single' or 'multiview'")

    try:
        gpu_id = _resolve_run_full_experiment_gpu_id(data.get("gpu_id", 0))
    except Exception as exc:
        errors.append(str(exc))
        gpu_id = DEFAULT_RUN_FULL_EXPERIMENT_GPU_ID

    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    # Load pairs
    pairs_file = PROJECT_ROOT / "data" / "matrix_pairs.json"
    if not pairs_file.exists():
        return jsonify({"error": "matrix_pairs.json not found. Generate it first."}), 404

    try:
        with open(pairs_file, "r", encoding="utf-8") as f:
            pairs_data = json.load(f)
        all_pairs = pairs_data.get("pairs", [])
    except Exception as exc:
        return jsonify({"error": f"Failed to read pairs file: {exc}"}), 500

    end = min(start + count, len(all_pairs))
    selected = all_pairs[start:end]
    if not selected:
        return jsonify({
            "error": f"No pairs in range [{start}, {end}). Total: {len(all_pairs)}"
        }), 400

    # Group by category
    groups = OrderedDict()
    for p in selected:
        cat = p["category"]
        if cat not in groups:
            groups[cat] = {"objects": [], "style_ids": []}
        groups[cat]["objects"].append(p["object_name"])
        groups[cat]["style_ids"].append(p["style_id"])

    start_round = selected[0].get("round", "?")
    end_round = selected[-1].get("round", "?")

    # Build name
    name_raw = str(data.get("name", "")).strip()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = name_raw or f"matrix-r{start_round}-i{start}"

    # Build YAML plan
    instruction_plan = {
        "mode": "adaptive_k",
        "count": edits_per_object,
        "allowed_types": ["remove", "replace"],
    }
    plan = {
        "name": name,
        "source_provider": source_provider,
        "target_provider": target_provider,
        "edit_mode": edit_mode,
        "categories": [
            {
                "category_name": cat,
                "random": {"category": False, "object": False},
                "object_count": len(g["objects"]),
                "objects": g["objects"],
                "style_ids": g["style_ids"],
                "instruction_plan": instruction_plan,
            }
            for cat, g in groups.items()
        ],
    }

    yaml_content = _yaml.safe_dump(plan, sort_keys=False, allow_unicode=True)

    EXPERIMENT_PLANS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(name)
    filename = f"{timestamp}_{slug}.yaml"
    file_path = EXPERIMENT_PLANS_DIR / filename
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    log_path = _build_run_full_experiment_log_path(file_path, plan, timestamp=timestamp)
    cli_command = _build_run_full_experiment_cli_command(
        file_path, log_path, gpu_id=gpu_id
    )

    actual_objects = len(selected)
    category_summary = [
        {"category": cat, "objects": len(g["objects"]), "styles": sorted(set(g["style_ids"]))}
        for cat, g in groups.items()
    ]

    return jsonify({
        "plan_path": str(file_path),
        "yaml_content": yaml_content,
        "cli_command": cli_command,
        "log_path": str(log_path),
        "start": start,
        "end": end,
        "actual_count": actual_objects,
        "rounds": f"{start_round} ~ {end_round}",
        "categories": len(groups),
        "category_summary": category_summary,
        "total_edits": actual_objects * edits_per_object,
    })


# ==================== Experiment Stats API ====================


@app.route("/api/experiment-stats/options", methods=["GET"])
def api_experiment_stats_options():
    """Return available provider pairs from recorded experiments."""
    try:
        entries, io_errors = _collect_experiment_entries()
        provider_pairs = []
        pair_keys = set()
        for entry in entries:
            manifest = entry["manifest"]
            key = (manifest["source_provider"], manifest["target_provider"])
            if key in pair_keys:
                continue
            pair_keys.add(key)
            provider_pairs.append(
                {
                    "source_provider": manifest["source_provider"],
                    "target_provider": manifest["target_provider"],
                    "label": f"{manifest['source_provider']} -> {manifest['target_provider']}",
                }
            )

        return jsonify(
            _attach_io_errors(
                {
                    "providers": sorted(VALID_EXPERIMENT_PROVIDERS),
                    "provider_pairs": sorted(
                        provider_pairs,
                        key=lambda item: (item["source_provider"], item["target_provider"]),
                    ),
                },
                io_errors,
            )
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-stats/category-summary", methods=["GET"])
def api_experiment_stats_category_summary():
    """Aggregate category statistics across experiments for a provider pair."""
    try:
        source_provider = request.args.get("source_provider", "").strip()
        target_provider = request.args.get("target_provider", "").strip()
        _validate_experiment_provider("source_provider", source_provider)
        _validate_experiment_provider("target_provider", target_provider)

        entries, io_errors = _collect_experiment_entries(
            source_provider=source_provider,
            target_provider=target_provider,
        )
        object_records = []
        edit_records = []
        partial_experiment_ids = []
        for entry in entries:
            record_bundle = _load_experiment_records_for_stats(entry)
            object_records.extend(record_bundle["object_records"])
            edit_records.extend(record_bundle["edit_records"])
            if record_bundle["partial_recovered"]:
                partial_experiment_ids.append(entry["experiment_id"])

        return jsonify(
            _attach_io_errors(
                {
                    "source_provider": source_provider,
                    "target_provider": target_provider,
                    "matched_experiments": [entry["experiment_id"] for entry in entries],
                    "partial_experiments": partial_experiment_ids,
                    "category_stats": _aggregate_category_stats(
                        object_records, edit_records
                    ),
                },
                io_errors,
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-stats/yaml-options", methods=["GET"])
def api_experiment_stats_yaml_options():
    try:
        source_provider = request.args.get("source_provider", "").strip()
        target_provider = request.args.get("target_provider", "").strip()
        _validate_experiment_provider("source_provider", source_provider)
        _validate_experiment_provider("target_provider", target_provider)

        entries, io_errors = _collect_experiment_entries(
            source_provider=source_provider,
            target_provider=target_provider,
        )
        grouped = {}
        for entry in entries:
            manifest = entry["manifest"]
            plan_path = manifest["plan_path"]
            current = grouped.setdefault(
                plan_path,
                {
                    "plan_path": plan_path,
                    "yaml_name": Path(plan_path).name,
                    "run_count": 0,
                    "latest_run_time": None,
                },
            )
            current["run_count"] += 1
            run_time = manifest.get("finished_at") or manifest.get("started_at")
            if run_time and (
                current["latest_run_time"] is None
                or run_time > current["latest_run_time"]
            ):
                current["latest_run_time"] = run_time

        yaml_files = sorted(
            grouped.values(),
            key=lambda item: (item["latest_run_time"] or "", item["yaml_name"]),
            reverse=True,
        )
        return jsonify(
            _attach_io_errors(
                {
                    "source_provider": source_provider,
                    "target_provider": target_provider,
                    "yaml_files": yaml_files,
                },
                io_errors,
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-stats/yaml-summary", methods=["GET"])
def api_experiment_stats_yaml_summary():
    try:
        plan_path = request.args.get("plan_path", "").strip()
        if not plan_path:
            raise ValueError("Missing required query parameter: plan_path")

        entries, io_errors = _collect_experiment_entries(plan_path=plan_path)
        if not entries:
            return jsonify(
                _attach_io_errors(
                    {"error": f"No experiments found for plan_path: {plan_path}"},
                    io_errors,
                )
            ), 404

        return jsonify(
            _attach_io_errors(
                {
                    "yaml_summary": _build_yaml_summary(entries),
                    "experiments": [
                        _build_experiment_run_summary(entry) for entry in entries
                    ],
                },
                io_errors,
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/experiment-stats/yaml-details", methods=["GET"])
def api_experiment_stats_yaml_details():
    try:
        plan_path = request.args.get("plan_path", "").strip()
        experiment_id = request.args.get("experiment_id", "").strip()
        if not plan_path:
            raise ValueError("Missing required query parameter: plan_path")

        entries, io_errors = _collect_experiment_entries(plan_path=plan_path)
        if not entries:
            return jsonify(
                _attach_io_errors(
                    {"error": f"No experiments found for plan_path: {plan_path}"},
                    io_errors,
                )
            ), 404

        if experiment_id:
            entries = [
                entry for entry in entries if entry["experiment_id"] == experiment_id
            ]
            if not entries:
                return (
                    jsonify(
                        {
                            "error": f"Experiment {experiment_id} not found under plan_path: {plan_path}"
                        }
                    ),
                    404,
                )

        object_records = []
        edit_records = []
        partial_experiment_ids = []
        for entry in entries:
            record_bundle = _load_experiment_records_for_stats(entry)
            object_records.extend(record_bundle["object_records"])
            edit_records.extend(record_bundle["edit_records"])
            if record_bundle["partial_recovered"]:
                partial_experiment_ids.append(entry["experiment_id"])

        return jsonify(
            _attach_io_errors(
                {
                    "plan_path": plan_path,
                    "selected_experiment_id": experiment_id or None,
                    "matched_experiments": [entry["experiment_id"] for entry in entries],
                    "partial_experiments": partial_experiment_ids,
                    **_build_stage_timing_summary(object_records, edit_records),
                    "category_stats": _aggregate_category_stats(
                        object_records, edit_records
                    ),
                    "object_stats": _build_object_stats(object_records),
                    "edit_records": _build_edit_rows(edit_records),
                },
                io_errors,
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/categories", methods=["GET"])
def api_get_categories():
    """Get all object categories from categorized_objects.json."""
    if not CATEGORIZED_OBJECTS_FILE.exists():
        return jsonify({"categories": []})

    with open(CATEGORIZED_OBJECTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Return sorted category names
    categories = sorted(data.keys())
    return jsonify({"categories": categories})


@app.route("/api/prompts", methods=["GET"])
def api_get_prompts():
    """Get all prompts."""
    return jsonify(get_all_prompts())


@app.route("/api/home/stats", methods=["GET"])
def api_get_home_stats():
    """Get lightweight homepage stats."""
    return jsonify(get_home_stats())


@app.route("/api/prompts/generate", methods=["POST"])
def api_generate_prompts():
    """Generate new T2I prompts from object categories.

    Request body:
        category: Optional category filter (vehicle, animal, etc.)
        count: Number of prompts to generate (default: 5)
    """
    from core.image.prompt_optimizer import PromptOptimizer
    import uuid
    from datetime import datetime

    data = request.get_json() or {}
    category = data.get("category", "")  # Empty = random from all
    count = min(int(data.get("count", 5)), 50)  # Max 50

    config = load_config()
    generated = []

    try:
        with PromptOptimizer(config.qh_mllm) as optimizer:
            for _ in range(count):
                # Pick random object from category (returns tuple)
                subject, obj_category = optimizer.pick_random_object(
                    category if category else None
                )

                # Optimize to full prompt (pass category for style filtering)
                prompt = optimizer.optimize_prompt(subject, category=obj_category)

                # Create prompt entry
                prompt_id = uuid.uuid4().hex[:12]
                entry = {
                    "id": prompt_id,
                    "subject": subject,
                    "category": obj_category,
                    "prompt": prompt,
                    "status": "pending",
                    "created_at": datetime.now().isoformat(),
                }
                generated.append(entry)

        # Save to JSONL file
        if generated:
            batch_file = (
                PROMPTS_DIR / f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            )
            PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
            with open(batch_file, "a", encoding="utf-8") as f:
                for entry in generated:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return jsonify({"success": True, "count": len(generated), "prompts": generated})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/images", methods=["GET"])
def api_get_images():
    """Get images with pagination support."""
    page = request.args.get("page", type=int)
    per_page = request.args.get("per_page", 20, type=int)

    all_images = get_all_images()

    # If no page specified, return all (backward compatible)
    if page is None:
        return jsonify(all_images)

    # Paginate
    total = len(all_images)
    start = (page - 1) * per_page
    end = start + per_page
    items = all_images[start:end]

    return jsonify(
        {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "has_more": end < total,
        }
    )


@app.route("/api/images/toc", methods=["GET"])
def api_get_images_toc():
    """Get lightweight image payloads for the Images page TOC."""
    # Use SQLite index when available (fast path)
    if _pipeline_index is not None:
        db_items = _pipeline_index.get_images_index()
        if db_items:
            return jsonify({"items": db_items, "total": len(db_items)})

    items = []
    for image in get_all_images():
        items.append(
            {
                "id": image.get("id"),
                "path": image.get("path"),
                "schema": image.get("schema"),
                "subject": image.get("subject"),
                "display_subject": image.get("display_subject"),
                "prompt": image.get("prompt"),
                "instruction": image.get("instruction"),
                "model_path": image.get("model_path"),
                "created_at": image.get("created_at"),
            }
        )
    return jsonify({"items": items, "total": len(items)})


@app.route("/api/models", methods=["GET"])
def api_get_models():
    """Get models, optionally paginated for the Models page."""
    page = request.args.get("page", type=int)
    per_page = request.args.get("per_page", 20, type=int)
    priority_ids_raw = request.args.get("priority_ids", "")
    priority_ids = [i.strip() for i in priority_ids_raw.split(",") if i.strip()] or None

    if page is None:
        return jsonify(get_all_models())

    # Fast path: use SQLite index for pagination
    if _pipeline_index is not None and _pipeline_index.model_count() > 0:
        items, total = _pipeline_index.get_models_page(
            page, per_page, priority_ids=priority_ids
        )
        end = max(page - 1, 0) * per_page + per_page
        return jsonify(
            {
                "items": items,
                "page": page,
                "per_page": per_page,
                "total": total,
                "has_more": end < total,
            }
        )

    # Fallback: in-memory scan + paginate
    index_items = get_all_models_index()
    source_model_ids = [
        item["id"]
        for item in sorted(
            index_items,
            key=lambda x: x.get("created_at") or "",
            reverse=True,
        )
    ]
    if priority_ids:
        priority_set = set(priority_ids)
        source_model_ids = priority_ids + [
            mid for mid in source_model_ids if mid not in priority_set
        ]
    total = len(source_model_ids)
    start = max(page - 1, 0) * per_page
    end = start + per_page
    page_ids = source_model_ids[start:end]
    items = []
    for model_id in page_ids:
        model = _load_model_payload_by_id(model_id)
        if model:
            items.append(model)

    return jsonify(
        {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "has_more": end < total,
        }
    )


@app.route("/api/models/toc", methods=["GET"])
def api_get_models_toc():
    """Get lightweight source-model payloads for the Models page TOC and filters."""
    items = get_all_models_index()
    return jsonify({"items": items, "total": len(items)})


@app.route("/api/models/batch", methods=["GET"])
def api_get_models_batch():
    """Load full payloads for a specific list of model IDs (used by YAML priority loading)."""
    ids_param = request.args.get("ids", "")
    ids = [i.strip() for i in ids_param.split(",") if i.strip()][:50]  # cap at 50
    if not ids:
        return jsonify({"items": [], "total": 0})

    items = []
    for model_id in ids:
        if not _is_safe_asset_id(model_id):
            continue
        try:
            model = _load_model_payload_by_id(model_id)
        except Exception:
            continue
        if model:
            items.append(model)
    return jsonify({"items": items, "total": len(items)})


@app.route("/api/models/search", methods=["GET"])
def api_search_models():
    """Search models by ID prefix, object name, or category name."""
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"items": [], "total": 0})

    index_items = get_all_models_index()
    exact: list[dict] = []
    prefix: list[dict] = []
    fuzzy: list[dict] = []

    for item in index_items:
        item_id = (item.get("id") or "").lower()
        obj_name = (item.get("object_name") or "").lower()
        category = (item.get("category_name") or "").lower()
        if item_id == q:
            exact.append(item)
        elif item_id.startswith(q):
            prefix.append(item)
        elif q in obj_name or q in category:
            fuzzy.append(item)

    results = (exact + prefix + fuzzy)[:20]
    items = []
    for item in results:
        model = _load_model_payload_by_id(item["id"])
        if model:
            items.append(model)

    return jsonify({"items": items, "total": len(items)})


@app.route("/api/models/<model_id>/path", methods=["GET"])
def api_get_model_absolute_path(model_id):
    """Get absolute path for a model's GLB file."""
    if not _is_safe_asset_id(model_id):
        return jsonify({"error": "Invalid model ID"}), 400

    model_dir = MODELS_DIR / model_id
    if not model_dir.exists():
        return jsonify({"error": "Model not found"}), 404

    glb_files = list(model_dir.glob("*.glb"))
    if not glb_files:
        return jsonify({"error": "No GLB file found"}), 404

    glb_path = glb_files[0]
    return jsonify(
        {
            "model_id": model_id,
            "absolute_path": str(glb_path.resolve()),
            "relative_path": _rel_path(glb_path),
            "filename": glb_path.name,
        }
    )


@app.route("/api/tasks", methods=["POST"])
def api_create_task():
    """Create a new async task."""
    data = request.get_json()
    task_type = data.get("type")
    params = data.get("params", {})

    if not task_type:
        return jsonify({"error": "Missing task type"}), 400

    task_id = create_task(task_type, params)
    return jsonify({"task_id": task_id, "status": "pending"})


@app.route("/api/tasks/<task_id>", methods=["GET"])
def api_get_task(task_id):
    """Get task status."""
    task = task_store.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)


@app.route("/api/tasks/list", methods=["GET"])
def api_list_tasks():
    """Get all tasks (from memory + file)."""
    # Combine memory and file tasks (deduplicate by ID)
    # Since task_store has latest state of running tasks, prefer that.

    # Load historical
    with tasks_file_lock:
        file_tasks = load_jsonl(TASKS_FILE)

    # helper to convert list to dict
    all_tasks_map = {t["id"]: t for t in file_tasks}

    # Update/Overwrite with memory tasks (which might be fresher)
    with task_lock:
        for tid, t in task_store.items():
            all_tasks_map[tid] = t

    # Sort by created_at desc
    sorted_tasks = sorted(
        all_tasks_map.values(), key=lambda x: x.get("created_at", ""), reverse=True
    )
    return jsonify(sorted_tasks[:100])  # limit to 100 recent


@app.route("/api/models/<model_id>/render", methods=["POST"])
def api_render_model(model_id):
    """Trigger multiview rendering for a model.

    Request body (optional):
    {
        "provider": "tripo"  // Which provider's GLB to render. Required when
                             // the model directory has multiple GLBs.
    }
    """
    data = request.get_json() or {}
    provider = data.get("provider")

    model_dir = MODELS_DIR / model_id
    if not model_dir.exists():
        return jsonify({"error": "Model not found"}), 404

    from core.gen3d import get_model_id as _get_model_id

    if provider:
        provider_id = _get_model_id(provider)
        glb_path = model_dir / f"model_{provider_id}.glb"
        if not glb_path.exists():
            return jsonify(
                {
                    "error": f"GLB not found for provider '{provider}' (model_{provider_id}.glb)"
                }
            ), 404
    else:
        glb_files = list(model_dir.glob("*.glb"))
        if not glb_files:
            return jsonify({"error": "No GLB file found"}), 404
        if len(glb_files) > 1:
            names = [f.name for f in glb_files]
            return jsonify(
                {
                    "error": f"Multiple GLBs found {names}. Specify 'provider' in request body."
                }
            ), 400
        glb_path = glb_files[0]
        provider_id = glb_path.stem.replace("model_", "")

    task_id = create_task(
        "render",
        {
            "model_id": model_id,
            "glb_path": _rel_path(glb_path),
            "provider_id": provider_id,
        },
    )
    return jsonify(
        {"task_id": task_id, "status": "pending", "provider_id": provider_id}
    )


@app.route("/api/models/<model_id>/views", methods=["GET"])
def api_get_model_views(model_id):
    """Get rendered views for a model.

    Query params:
    - provider_id: e.g. "tp3" or "hy3" — return views from that provider's subdir.
                   If omitted, returns all provider subdirs grouped.
    """
    config = load_config()
    semantic_tmp_dir_name = config.render.semantic_alignment.temp_dir_name

    provider_id = request.args.get("provider_id")
    views_dir = TRIPLETS_DIR / model_id / "views"

    if not views_dir.exists():
        return jsonify({"views": [], "has_views": False, "by_provider": {}})

    if provider_id:
        # Return views for a specific provider
        pdir = views_dir / provider_id
        views = (
            _collect_canonical_view_payloads(
                pdir, provider_id, PROVIDER_ID_TO_NAME.get(provider_id, provider_id)
            )
            if pdir.exists() and pdir.is_dir()
            else []
        )
        return _set_no_store_headers(
            jsonify({"views": views, "has_views": len(views) > 0})
        )

    # Return all providers grouped
    by_provider = {}
    for subdir in sorted(views_dir.iterdir()):
        if subdir.is_dir():
            pid = subdir.name
            if _is_temp_views_dir(pid, semantic_tmp_dir_name):
                continue
            pviews = _collect_canonical_view_payloads(
                subdir, pid, PROVIDER_ID_TO_NAME.get(pid, pid)
            )
            if pviews:
                by_provider[pid] = pviews

    # Legacy: flat PNGs directly in views/
    legacy_views = _collect_canonical_view_payloads(views_dir, "legacy", "legacy")
    if legacy_views and not by_provider:
        by_provider["legacy"] = legacy_views

    all_views = [v for pvs in by_provider.values() for v in pvs]
    return _set_no_store_headers(
        jsonify(
            {
                "views": all_views,
                "has_views": len(all_views) > 0,
                "by_provider": by_provider,
            }
        )
    )


@app.route("/api/models/<model_id>/edits/<edit_id>/generate-3d", methods=["POST"])
def api_generate_3d_from_edit(model_id, edit_id):
    """Generate 3D model from an edited view batch (uses front view).

    默认使用源模型的同一个 3D 生成器，保证一致性。
    可通过 request body 的 provider 字段覆盖。
    """
    data = request.get_json() or {}
    config = load_config()

    # Find the edited batch
    edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
    if not edited_dir.exists():
        return jsonify({"error": "Edit batch not found"}), 404
    edit_meta_path = edited_dir / "meta.json"
    edit_meta = safe_load_json(edit_meta_path, {}) if edit_meta_path.exists() else {}
    if edit_meta and not is_edit_batch_allowed(edit_meta):
        status = get_effective_edit_status(edit_meta)
        return (
            jsonify({"error": f"Edit batch blocked by quality check: status={status}"}),
            409,
        )

    # 获取源模型的 provider，保证一致性
    source_model_dir = MODELS_DIR / model_id
    source_meta_path = source_model_dir / "meta.json"
    default_provider = config.tasks["gen3d"].provider
    source_provider = default_provider
    if source_meta_path.exists():
        source_meta = safe_load_json(source_meta_path, {})
        source_provider = source_meta.get("provider", default_provider)

    # 允许前端覆盖，但默认使用源模型的 provider
    provider = data.get("provider", source_provider)

    # Use front view as the source image for 3D generation
    front_view = edited_dir / "front.png"
    if not front_view.exists():
        # Fallback to any available real view (exclude stitched composites)
        preferred = ["front", "back", "right", "left", "top", "bottom"]
        views = [
            edited_dir / f"{name}.png"
            for name in preferred
            if (edited_dir / f"{name}.png").exists()
        ]
        if not views:
            return jsonify({"error": "No edited views found"}), 404
        front_view = views[0]

    # Create a new image entry for the edited view
    new_image_id = f"{model_id}_edit_{edit_id}"
    new_image_dir = IMAGES_DIR / new_image_id
    new_image_dir.mkdir(parents=True, exist_ok=True)

    # Copy the front view as the source image
    import shutil

    dest_image = new_image_dir / "image.png"
    shutil.copy(front_view, dest_image)

    # Save meta
    meta = {
        "id": new_image_id,
        "parent_model": model_id,
        "edit_id": edit_id,
        "instruction": edit_meta.get(
            "instruction_display_text", edit_meta.get("instruction", "")
        ),
        "generated_at": datetime.now().isoformat(),
    }
    with open(new_image_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # Create 3D generation task
    task_id = create_task(
        "gen3d",
        {
            "image_id": new_image_id,
            "image_path": _rel_path(dest_image),
            "provider": provider,
        },
    )

    return jsonify(
        {"task_id": task_id, "status": "pending", "new_image_id": new_image_id}
    )


@app.route("/api/models/<model_id>/views/<view_name>/edit", methods=["POST"])
def api_edit_view(model_id, view_name):
    """Apply editing instruction to rendered views.

    Supports two edit modes:
    - single: Edit each view independently (default)
    - multiview: Stitch views into 3x2 grid, edit once, then split back

    Request body:
    {
        "instruction": "...",
        "edit_mode": "multiview",   // or "single"
        "provider_id": "tp3"        // which provider's rendered views to edit
    }
    """
    data = request.get_json() or {}
    instruction = data.get("instruction")
    edit_mode = data.get("edit_mode", "multiview")  # 'multiview' or 'single'
    provider_id = data.get("provider_id")  # e.g. "tp3", "hy3"

    if not instruction:
        return jsonify({"error": "Instruction required"}), 400

    # Resolve the views directory
    views_base = TRIPLETS_DIR / model_id / "views"
    if not views_base.exists():
        return jsonify({"error": "Model views not found"}), 404

    resolved_provider_id = provider_id
    if resolved_provider_id:
        views_dir = views_base / resolved_provider_id
        if not views_dir.exists():
            return jsonify({"error": f"No views for provider '{resolved_provider_id}'"}), 404
    else:
        provider_dirs = [
            subdir
            for subdir in sorted(views_base.iterdir())
            if subdir.is_dir() and any(subdir.glob("*.png")) and not _is_temp_views_dir(subdir.name, "_semantic_tmp")
        ]
        if len(provider_dirs) == 1:
            views_dir = provider_dirs[0]
            resolved_provider_id = views_dir.name
        elif len(provider_dirs) > 1:
            pids = ", ".join(subdir.name for subdir in provider_dirs)
            return jsonify({"error": f"provider_id is required; multiple providers found: {pids}"}), 400
        else:
            return jsonify({"error": "provider_id is required; no provider subdirectories found"}), 400

    # Determine view names to edit
    if view_name.lower() in ("_batch", "batch", "_default", "default"):
        if edit_mode == "multiview":
            view_names = ["front", "back", "right", "left", "top", "bottom"]
        else:
            view_names = ["front", "back", "right"]
    else:
        view_names = [view_name]

    # Validate at least one view exists
    existing_views = [v for v in view_names if (views_dir / f"{v}.png").exists()]
    if not existing_views:
        return jsonify({"error": f"No valid views found in {views_dir}"}), 404

    task_id = create_task(
        "edit_view",
        {
            "model_id": model_id,
            "view_names": existing_views,
            "instruction": instruction,
            "edit_mode": edit_mode,
            "views_dir": str(_rel_path(views_dir)),  # exact dir to read from
            "source_provider_id": resolved_provider_id,  # recorded in edit meta
        },
    )

    return jsonify({"task_id": task_id, "status": "pending"})


@app.route("/api/models/<model_id>/refresh-lpips", methods=["POST"])
@app.route("/api/models/<model_id>/refresh-dreamsim", methods=["POST"])
def api_refresh_model_dreamsim(model_id):
    """Recompute Stage-2 LPIPS for all refreshable targets under one model."""
    if not _is_safe_asset_id(model_id):
        return jsonify({"error": "Invalid model id"}), 400

    source_model_dir = MODELS_DIR / model_id
    if not source_model_dir.exists():
        return jsonify({"error": "Model not found"}), 404

    edited_base = TRIPLETS_DIR / model_id / "edited"
    if not edited_base.exists():
        return jsonify({"error": "No edited batches found for this model"}), 400

    if _has_active_dreamsim_refresh_task(model_id=model_id):
        return (
            jsonify(
                {
                    "error": (
                        f"LPIPS refresh already running for model {model_id} "
                        "or covered by an active refresh-all task"
                    )
                }
            ),
            409,
        )

    target_count = 0
    for edit_dir in edited_base.iterdir():
        if not edit_dir.is_dir():
            continue
        target_model_dir = MODELS_DIR / f"{model_id}_edit_{edit_dir.name}"
        if not target_model_dir.exists():
            continue
        target_count += len(list(target_model_dir.glob("model_*.glb")))

    if target_count == 0:
        return jsonify(
            {"error": "No refreshable target 3D models found for this model"}
        ), 400

    task_id = create_task(
        "refresh_model_dreamsim",
        {
            "model_id": model_id,
        },
    )

    return jsonify(
        {
            "task_id": task_id,
            "status": "pending",
            "model_id": model_id,
            "target_count": target_count,
        }
    )


@app.route("/api/models/<model_id>/materialize-missing-masks", methods=["POST"])
def api_materialize_missing_masks(model_id):
    """Materialize missing mask artifacts for all edit batches under one source model."""
    if not _is_safe_asset_id(model_id):
        return jsonify({"error": "Invalid model id"}), 400

    source_model_dir = MODELS_DIR / model_id
    if not source_model_dir.exists():
        return jsonify({"error": "Model not found"}), 404

    if _has_active_task("materialize_missing_masks", model_id):
        return (
            jsonify({"error": f"Mask materialization already running for model {model_id}"}),
            409,
        )

    try:
        targets, skipped = _scan_model_missing_mask_targets(model_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    if not targets:
        return jsonify({"error": "No edit batches with missing masks found"}), 400

    task_id = create_task(
        "materialize_missing_masks",
        {
            "model_id": model_id,
        },
    )

    return jsonify(
        {
            "task_id": task_id,
            "status": "pending",
            "model_id": model_id,
            "missing_mask_count": len(targets),
            "skipped_count": len(skipped),
        }
    )


@app.route("/api/models/refresh-lpips-all", methods=["POST"])
@app.route("/api/models/refresh-dreamsim-all", methods=["POST"])
def api_refresh_all_models_dreamsim():
    """Recompute Stage-2 LPIPS for all source models or a requested subset."""
    if _has_active_dreamsim_refresh_task():
        return (
            jsonify({"error": "Another LPIPS refresh task is already running"}),
            409,
        )

    data = request.get_json(silent=True) or {}
    requested_model_ids = data.get("model_ids")

    if requested_model_ids is not None:
        if not isinstance(requested_model_ids, list):
            return jsonify({"error": "model_ids must be a list when provided"}), 400
        model_ids = [
            str(model_id).strip()
            for model_id in requested_model_ids
            if str(model_id).strip()
        ]
    else:
        model_ids = [
            model_dir.name
            for model_dir in sorted(MODELS_DIR.iterdir())
            if model_dir.is_dir() and "_edit_" not in model_dir.name
        ]

    if not model_ids:
        return jsonify({"error": "No source models found"}), 400

    refreshable_model_count = 0
    target_count = 0
    for model_id in model_ids:
        try:
            refresh_targets, _ = _scan_model_dreamsim_refresh_targets(model_id)
        except Exception:
            continue
        if not refresh_targets:
            continue
        refreshable_model_count += 1
        target_count += len(refresh_targets)

    if target_count == 0:
        return jsonify(
            {"error": "No refreshable LPIPS targets found in the requested models"}
        ), 400

    task_id = create_task(
        "refresh_all_models_dreamsim",
        {
            "model_ids": model_ids,
        },
    )

    return jsonify(
        {
            "task_id": task_id,
            "status": "pending",
            "model_count": len(model_ids),
            "refreshable_model_count": refreshable_model_count,
            "target_count": target_count,
        }
    )


# =============================================================================
# Batch Operations API
# =============================================================================


@app.route("/api/batch/generate-3d", methods=["POST"])
def api_batch_generate_3d():
    """Batch generate 3D models for multiple images.

    Request body:
    {
        "image_ids": ["id1", "id2", ...],
        "provider": "tripo" | "hunyuan"
    }
    """
    data = request.get_json() or {}
    image_ids = data.get("image_ids", [])
    config = load_config()
    provider = data.get("provider", config.tasks["gen3d"].provider)

    if not image_ids:
        return jsonify({"error": "No image_ids provided"}), 400

    task_ids = []
    errors = []

    for image_id in image_ids:
        image_dir = IMAGES_DIR / image_id
        image_path = image_dir / "image.png"

        if not image_path.exists():
            errors.append({"image_id": image_id, "error": "Image not found"})
            continue

        task_id = create_task(
            "gen3d",
            {
                "image_id": image_id,
                "image_path": _rel_path(image_path),
                "provider": provider,
            },
        )
        task_ids.append({"image_id": image_id, "task_id": task_id})

    return jsonify({"created": len(task_ids), "tasks": task_ids, "errors": errors})


@app.route("/api/batch/render", methods=["POST"])
def api_batch_render():
    """Batch render multiview for multiple models.

    Request body:
    {
        "model_ids": ["id1", "id2", ...],
        "provider": "tripo"   // Required when any model has multiple GLBs
    }
    """
    data = request.get_json() or {}
    model_ids = data.get("model_ids", [])
    provider = data.get("provider")

    if not model_ids:
        return jsonify({"error": "No model_ids provided"}), 400

    from core.gen3d import get_model_id as _get_model_id

    task_ids = []
    errors = []

    for model_id in model_ids:
        model_dir = MODELS_DIR / model_id
        if not model_dir.exists():
            errors.append({"model_id": model_id, "error": "Model not found"})
            continue

        # Resolve GLB path
        if provider:
            provider_id = _get_model_id(provider)
            glb_path = model_dir / f"model_{provider_id}.glb"
            if not glb_path.exists():
                errors.append(
                    {
                        "model_id": model_id,
                        "error": f"GLB not found for provider '{provider}'",
                    }
                )
                continue
        else:
            glb_files = list(model_dir.glob("*.glb"))
            if not glb_files:
                errors.append({"model_id": model_id, "error": "No GLB file found"})
                continue
            if len(glb_files) > 1:
                errors.append(
                    {
                        "model_id": model_id,
                        "error": f"Multiple GLBs found; specify 'provider'",
                    }
                )
                continue
            glb_path = glb_files[0]
            provider_id = glb_path.stem.replace("model_", "")

        task_id = create_task(
            "render",
            {
                "model_id": model_id,
                "glb_path": _rel_path(glb_path),
                "provider_id": provider_id,
            },
        )
        task_ids.append(
            {"model_id": model_id, "task_id": task_id, "provider_id": provider_id}
        )

    return jsonify({"created": len(task_ids), "tasks": task_ids, "errors": errors})


@app.route("/api/batch/edit", methods=["POST"])
def api_batch_edit():
    """Batch apply edit instructions to multiple model views.

    Request body:
    {
        "edits": [
            {"model_id": "id1", "view_name": "front", "instruction": "..."},
            {"model_id": "id1", "view_name": "back", "instruction": "..."},
            ...
        ]
    }

    Or simplified format for applying same instruction to default views:
    {
        "model_ids": ["id1", "id2"],
        "instruction": "Remove background",  // Direct instruction text
        "views": ["front", "back", "right"]  // Default views
    }

    Or use instruction from source image:
    {
        "model_ids": ["id1", "id2"],
        "instruction_index": 0,  // Use first instruction from source image
        "views": ["front", "back", "right"]  // Default views
    }
    """
    data = request.get_json() or {}

    # Check which format
    if "edits" in data:
        # Explicit format
        edits = data["edits"]
    else:
        # Simplified format
        model_ids = data.get("model_ids", [])
        direct_instruction = data.get("instruction")  # Direct instruction text
        instruction_index = data.get("instruction_index")  # Or use index from source
        views = data.get("views", ["front", "back", "right"])

        edits = []
        for model_id in model_ids:
            instruction = direct_instruction

            # If no direct instruction, try to get from source image
            if not instruction and instruction_index is not None:
                # Source image might be the model_id itself
                source_image_id = model_id
                image_dir = IMAGES_DIR / source_image_id
                instr_path = image_dir / "instructions.json"

                if instr_path.exists():
                    with open(instr_path, "r", encoding="utf-8") as f:
                        instructions_data = json.load(f)

                    if instruction_index < len(instructions_data):
                        instr_item = _with_instruction_item_payload(
                            instructions_data[instruction_index]
                        )
                        instruction = instr_item.get("instruction_display_text", "")

            if not instruction:
                continue

            for view in views:
                edits.append(
                    {
                        "model_id": model_id,
                        "view_name": view,
                        "instruction": instruction,
                    }
                )

    if not edits:
        return jsonify({"error": "No edits provided"}), 400

    task_ids = []
    errors = []

    for edit in edits:
        model_id = edit.get("model_id")
        view_name = edit.get("view_name")
        instruction = edit.get("instruction")

        if not all([model_id, view_name, instruction]):
            errors.append({"edit": edit, "error": "Missing required fields"})
            continue

        # Find view image
        triplet_dir = TRIPLETS_DIR / model_id
        view_path = triplet_dir / f"{view_name}.png"

        if not view_path.exists():
            errors.append(
                {"model_id": model_id, "view": view_name, "error": "View not found"}
            )
            continue

        task_id = create_task(
            "edit_view",
            {
                "model_id": model_id,
                "view_name": view_name,
                "view_path": _rel_path(view_path),
                "instruction": instruction,
            },
        )
        task_ids.append(
            {"model_id": model_id, "view_name": view_name, "task_id": task_id}
        )

    return jsonify({"created": len(task_ids), "tasks": task_ids, "errors": errors})


@app.route("/api/batch/generate-instruction", methods=["POST"])
def api_batch_generate_instruction():
    """Batch generate editing instructions for multiple images.

    Request body:
    {
        "image_ids": ["id1", "id2", ...],
        "mode": "batch" | "remove" | "replace"
    }
    """
    data = request.get_json() or {}
    image_ids = data.get("image_ids", [])
    mode = data.get("mode", "batch")

    if not image_ids:
        return jsonify({"error": "No image_ids provided"}), 400

    task_ids = []
    errors = []

    for image_id in image_ids:
        image_dir = IMAGES_DIR / image_id
        image_path = image_dir / "image.png"

        if not image_path.exists():
            errors.append({"image_id": image_id, "error": "Image not found"})
            continue

        task_id = create_task(
            "instruction",
            {
                "image_id": image_id,
                "image_path": _rel_path(image_path),
                "mode": mode,
            },
        )
        task_ids.append({"image_id": image_id, "task_id": task_id})

    return jsonify({"created": len(task_ids), "tasks": task_ids, "errors": errors})


@app.route("/api/images/<image_id>/instruction", methods=["GET"])
def api_get_instruction(image_id):
    """Get instruction text for an image (supports both list and single)."""
    image_dir = IMAGES_DIR / image_id

    # Try JSON format first
    json_path = image_dir / "instructions.json"
    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            raw_list = json.load(f)
            instruction_items = []
            if isinstance(raw_list, list):
                instruction_items = [
                    _with_instruction_item_payload(item)
                    for item in raw_list
                ]
            instructions = [
                item["instruction_display_text"]
                for item in instruction_items
                if item.get("instruction_display_text")
            ]
            return jsonify(
                {
                    "instruction_items": instruction_items,
                    "instructions": instructions,
                    "instruction": instructions[0] if instructions else None,
                }
            )

    # Fallback to legacy txt format
    txt_path = image_dir / "instruction.txt"
    if txt_path.exists():
        with open(txt_path, "r", encoding="utf-8") as f:
            instruction = f.read()
            instruction_item = _with_instruction_item_payload(instruction)
            return jsonify(
                {
                    "instruction": instruction_item["instruction_display_text"],
                    "instructions": [instruction_item["instruction_display_text"]],
                    "instruction_items": [instruction_item],
                }
            )

    return jsonify({"instruction": None, "instructions": [], "instruction_items": []})


@app.route("/api/images/<image_id>/instruction/<int:instr_index>", methods=["DELETE"])
def api_delete_instruction(image_id, instr_index):
    """Delete a specific instruction by index.

    Args:
        image_id: The image ID
        instr_index: 0-based index of the instruction to delete
    """
    image_dir = IMAGES_DIR / image_id
    json_path = image_dir / "instructions.json"

    if not json_path.exists():
        return jsonify({"error": "No instructions found"}), 404

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            instructions_data = json.load(f)

        if not isinstance(instructions_data, list):
            return jsonify({"error": "Invalid instructions format"}), 500

        if instr_index < 0 or instr_index >= len(instructions_data):
            return jsonify(
                {
                    "error": f"Invalid index {instr_index}, only {len(instructions_data)} instructions"
                }
            ), 400

        # Remove the instruction at the specified index
        deleted_item = instructions_data.pop(instr_index)
        deleted_text = (
            deleted_item.get("text", deleted_item)
            if isinstance(deleted_item, dict)
            else str(deleted_item)
        )

        # Save updated instructions
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(instructions_data, f, ensure_ascii=False, indent=2)

        # Update legacy txt file
        txt_path = image_dir / "instruction.txt"
        if instructions_data:
            # Write the first remaining instruction
            first_text = (
                instructions_data[0].get("text", instructions_data[0])
                if isinstance(instructions_data[0], dict)
                else str(instructions_data[0])
            )
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(first_text)
        elif txt_path.exists():
            # No instructions left, remove txt file
            txt_path.unlink()

        clear_pipeline_listing_cache()
        return jsonify(
            {
                "success": True,
                "deleted": deleted_text,
                "remaining_count": len(instructions_data),
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/images/<image_id>/generate-3d", methods=["POST"])
def api_generate_3d(image_id):
    """Generate 3D model for an image."""
    data = request.get_json() or {}
    config = load_config()
    provider = data.get("provider", config.tasks["gen3d"].provider)

    # Find image
    image_dir = IMAGES_DIR / image_id
    image_path = image_dir / "image.png"
    if not image_path.exists():
        return jsonify({"error": "Image not found"}), 404

    task_id = create_task(
        "gen3d",
        {
            "image_id": image_id,
            "image_path": _rel_path(image_path),
            "provider": provider,
        },
    )

    return jsonify({"task_id": task_id, "status": "pending"})


@app.route("/api/images/<image_id>/generate-instruction", methods=["POST"])
def api_generate_instruction(image_id):
    """Generate editing instruction(s) for an image.

    Supports mode parameter:
    - 'batch' (default): Generate 1 REMOVE + 1 REPLACE instruction
    - 'remove': Generate 1 REMOVE instruction only
    - 'replace': Generate 1 REPLACE instruction only
    """
    image_dir = IMAGES_DIR / image_id
    image_path = image_dir / "image.png"
    if not image_path.exists():
        return jsonify({"error": "Image not found"}), 404

    # Get mode from request body
    data = request.get_json() or {}
    mode = data.get("mode", "batch")  # Default to batch (1 remove + 1 replace)

    task_id = create_task(
        "instruction",
        {
            "image_id": image_id,
            "image_path": _rel_path(image_path),
            "mode": mode,
        },
    )

    return jsonify({"task_id": task_id, "status": "pending"})


@app.route("/api/images/<image_id>/edit", methods=["POST"])
def api_edit_image(image_id):
    """Edit an image with instruction."""
    image_dir = IMAGES_DIR / image_id
    image_path = image_dir / "image.png"

    if not image_path.exists():
        return jsonify({"error": "Image not found"}), 404

    # Support both JSON and form data
    if request.is_json:
        data = request.get_json()
        instruction = data.get("instruction")
    else:
        instruction = request.form.get("instruction")

    if not instruction:
        return jsonify({"error": "Instruction is required"}), 400

    task_id = create_task(
        "edit",
        {
            "image_id": image_id,
            "image_path": _rel_path(image_path),
            "instruction": instruction,
        },
    )

    return jsonify(
        {
            "task_id": task_id,
            "status": "pending",
            "success": True,  # For modal_logic.js compatibility
        }
    )


@app.route("/api/prompts/<prompt_id>/generate-image", methods=["POST"])
def api_generate_image(prompt_id):
    """Generate image from a prompt."""
    prompt = _get_prompt_by_ui_id(prompt_id)

    if not prompt:
        return jsonify({"error": "Prompt not found"}), 404
    if not prompt.get("can_generate_image"):
        return jsonify({"error": "Prompt does not support image generation"}), 400

    task_id = create_task(
        "t2i",
        {
            "prompt_id": prompt["ui_id"],
            "prompt": prompt["prompt"],
            "subject": prompt.get("subject"),
        },
    )

    return jsonify({"task_id": task_id, "status": "pending"})


@app.route("/api/prompts/batch-generate", methods=["POST"])
def api_batch_generate_images():
    """Generate images for multiple prompts."""
    data = request.get_json() or {}
    prompt_ids = data.get("prompt_ids", [])

    if not prompt_ids:
        return jsonify({"error": "No prompt IDs provided"}), 400

    all_prompts = get_all_prompts()
    prompts_map = {p["ui_id"]: p for p in all_prompts}
    unsupported_ids = [
        pid
        for pid in prompt_ids
        if pid not in prompts_map or not prompts_map[pid].get("can_batch_generate")
    ]
    if unsupported_ids:
        return jsonify(
            {
                "error": "Some prompts do not support batch image generation",
                "prompt_ids": unsupported_ids,
            }
        ), 400

    created_tasks = []

    for pid in prompt_ids:
        prompt = prompts_map.get(pid)
        task_id = create_task(
            "t2i",
            {
                "prompt_id": pid,
                "prompt": prompt["prompt"],
                "subject": prompt.get("subject"),
            },
        )
        created_tasks.append(task_id)

    return jsonify(
        {
            "status": "batch_started",
            "count": len(created_tasks),
            "task_ids": created_tasks,
        }
    )


@app.route("/api/prompts/<prompt_id>", methods=["DELETE"])
def api_delete_prompt(prompt_id):
    """Delete a prompt and cascade delete its generated assets (image/instructions/3d/renders)."""
    if not _is_safe_asset_id(prompt_id):
        return jsonify({"error": "Invalid prompt id"}), 400
    prompt = _get_prompt_by_ui_id(prompt_id)
    if not prompt:
        return jsonify({"error": "Prompt not found"}), 404
    if not prompt.get("can_delete"):
        return jsonify({"error": "Prompt does not support delete"}), 400

    prompt_removed = _delete_prompt_from_jsonl(prompt_id)
    deleted = _delete_assets(prompt_id)

    return jsonify(
        {
            "success": True,
            "prompt_removed": prompt_removed,
            "deleted": deleted,
        }
    )


# =============================================================================
# Batch Download API
# =============================================================================


@app.route("/api/download/images", methods=["POST"])
def api_download_images():
    """Download selected images as a ZIP file."""
    data = request.get_json() or {}
    image_ids = data.get("ids", [])

    if not image_ids:
        # Download all images if no IDs specified
        all_images = get_all_images()
        image_ids = [img["id"] for img in all_images]

    if not image_ids:
        return jsonify({"error": "No images to download"}), 400

    # Create temporary ZIP file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for img_id in image_ids:
                if not _is_safe_asset_id(img_id):
                    continue
                img_dir = IMAGES_DIR / img_id
                if img_dir.exists():
                    for f in img_dir.iterdir():
                        if f.is_file() and f.suffix.lower() in (
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".webp",
                        ):
                            # Archive as: image_id/filename
                            arcname = f"{img_id}/{f.name}"
                            zf.write(f, arcname)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            tmp_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"images_{timestamp}.zip",
        )
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return jsonify({"error": str(e)}), 500


@app.route("/api/download/models", methods=["POST"])
def api_download_models():
    """Download selected 3D models as a ZIP file."""
    data = request.get_json() or {}
    model_ids = data.get("ids", [])
    include_views = data.get("include_views", False)

    if not model_ids:
        # Download all models if no IDs specified
        all_models = get_all_models()
        model_ids = [m["id"] for m in all_models]

    if not model_ids:
        return jsonify({"error": "No models to download"}), 400

    # Create temporary ZIP file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for model_id in model_ids:
                if not _is_safe_asset_id(model_id):
                    continue
                model_dir = MODELS_DIR / model_id
                if model_dir.exists():
                    # Add 3D model files
                    for f in model_dir.iterdir():
                        if f.is_file() and f.suffix.lower() in (
                            ".glb",
                            ".gltf",
                            ".obj",
                            ".fbx",
                            ".usdz",
                        ):
                            arcname = f"{model_id}/{f.name}"
                            zf.write(f, arcname)

                    # Optionally add rendered views
                    if include_views:
                        views_root = TRIPLETS_DIR / model_id / "views"
                        if views_root.exists():
                            for f in views_root.rglob("*"):
                                if f.is_file() and f.suffix.lower() in (
                                    ".png",
                                    ".jpg",
                                    ".jpeg",
                                ):
                                    rel = f.relative_to(views_root)
                                    arcname = f"{model_id}/views/{rel.as_posix()}"
                                    zf.write(f, arcname)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            tmp_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"models_{timestamp}.zip",
        )
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return jsonify({"error": str(e)}), 500


@app.route("/api/download/all", methods=["POST"])
def api_download_all():
    """Download all assets (images + models + views) as a ZIP file."""
    data = request.get_json() or {}

    all_images = get_all_images()
    all_models = get_all_models()

    # Create temporary ZIP file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Add images
            for img in all_images:
                img_id = img["id"]
                if not _is_safe_asset_id(img_id):
                    continue
                img_dir = IMAGES_DIR / img_id
                if img_dir.exists():
                    for f in img_dir.iterdir():
                        if f.is_file() and f.suffix.lower() in (
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".webp",
                        ):
                            arcname = f"images/{img_id}/{f.name}"
                            zf.write(f, arcname)

            # Add models with views
            for model in all_models:
                model_id = model["id"]
                if not _is_safe_asset_id(model_id):
                    continue
                model_dir = MODELS_DIR / model_id
                if model_dir.exists():
                    for f in model_dir.iterdir():
                        if f.is_file() and f.suffix.lower() in (
                            ".glb",
                            ".gltf",
                            ".obj",
                            ".fbx",
                            ".usdz",
                        ):
                            arcname = f"models/{model_id}/{f.name}"
                            zf.write(f, arcname)

                    views_root = TRIPLETS_DIR / model_id / "views"
                    if views_root.exists():
                        for f in views_root.rglob("*"):
                            if f.is_file() and f.suffix.lower() in (
                                ".png",
                                ".jpg",
                                ".jpeg",
                            ):
                                rel = f.relative_to(views_root)
                                arcname = f"models/{model_id}/views/{rel.as_posix()}"
                                zf.write(f, arcname)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            tmp_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"pipeline_assets_{timestamp}.zip",
        )
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return jsonify({"error": str(e)}), 500


@app.route("/api/assets/<asset_id>", methods=["DELETE"])
def api_delete_assets(asset_id):
    """Delete an image/model asset id and downstream artifacts.

    This keeps the prompt record but marks its status as 'deleted'.
    """
    if not _is_safe_asset_id(asset_id):
        return jsonify({"error": "Invalid asset id"}), 400

    # Mark prompt status as deleted if it exists
    try:
        update_prompt_status(asset_id, "deleted")
    except Exception:
        pass

    deleted = _delete_assets(asset_id)
    clear_pipeline_listing_cache()
    _refresh_model_in_index(asset_id)
    _refresh_image_in_index(asset_id)
    return jsonify(
        {
            "success": True,
            "deleted": deleted,
        }
    )


@app.route("/api/models/<model_id>/edits/<edit_id>", methods=["DELETE"])
def api_delete_edit_batch(model_id, edit_id):
    """Delete an edit batch and its associated target 3D model.

    This removes:
    1. Edited views: TRIPLETS_DIR/<model_id>/edited/<edit_id>/
    2. Target 3D model: MODELS_DIR/<model_id>_edit_<edit_id>/
    """
    if not _is_safe_asset_id(model_id) or not _is_safe_asset_id(edit_id):
        return jsonify({"error": "Invalid model or edit id"}), 400

    deleted = []

    # Delete edited views
    edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
    if edited_dir.exists():
        _delete_tree(edited_dir)
        deleted.append(str(edited_dir))

    # Delete target 3D model
    target_model_id = f"{model_id}_edit_{edit_id}"
    target_model_dir = MODELS_DIR / target_model_id
    if target_model_dir.exists():
        _delete_tree(target_model_dir)
        deleted.append(str(target_model_dir))

    if not deleted:
        return jsonify({"error": "Edit batch not found"}), 404

    clear_pipeline_listing_cache()
    _refresh_model_in_index(model_id)
    return jsonify(
        {
            "success": True,
            "deleted": deleted,
        }
    )


@app.route(
    "/api/models/<model_id>/edits/<edit_id>/targets/<provider_id>",
    methods=["DELETE"],
)
def api_delete_edit_target_provider(model_id, edit_id, provider_id):
    """Delete one target 3D provider result for an edit batch.

    This removes:
    1. Target GLB: MODELS_DIR/<model_id>_edit_<edit_id>/model_<provider_id>.*
    2. Target rendered views: TRIPLETS_DIR/<model_id>_edit_<edit_id>/views/<provider_id>/
    3. Stage2 provider-scoped check result from target meta.json
    """
    if (
        not _is_safe_asset_id(model_id)
        or not _is_safe_asset_id(edit_id)
        or not _is_safe_asset_id(provider_id)
    ):
        return jsonify({"error": "Invalid model, edit, or provider id"}), 400

    target_model_id = f"{model_id}_edit_{edit_id}"
    target_model_dir = MODELS_DIR / target_model_id
    target_views_dir = TRIPLETS_DIR / target_model_id / "views" / provider_id

    deleted = []
    meta_updated = False

    if target_model_dir.exists():
        provider_model_prefix = f"model_{provider_id}."
        for provider_model_path in target_model_dir.iterdir():
            if not provider_model_path.is_file():
                continue
            if not provider_model_path.name.startswith(provider_model_prefix):
                continue
            result = _delete_tree(provider_model_path)
            if result.get("deleted"):
                deleted.append(result.get("path"))

        target_meta_path = target_model_dir / "meta.json"
        if target_meta_path.exists():
            meta = safe_load_json(target_meta_path, {})
            if not isinstance(meta, dict):
                meta = {}

            checks_by_provider = meta.get("target_quality_checks_by_provider")
            if (
                isinstance(checks_by_provider, dict)
                and provider_id in checks_by_provider
            ):
                checks_by_provider.pop(provider_id, None)
                meta["target_quality_checks_by_provider"] = checks_by_provider
                meta_updated = True

            legacy_check = meta.get("target_quality_check")
            if (
                isinstance(legacy_check, dict)
                and legacy_check.get("provider_id") == provider_id
            ):
                replacement = {}
                if isinstance(checks_by_provider, dict):
                    for payload in checks_by_provider.values():
                        if isinstance(payload, dict):
                            replacement = payload
                            break
                meta["target_quality_check"] = replacement
                meta_updated = True

            if meta_updated:
                with open(target_meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)

    if target_views_dir.exists():
        result = _delete_tree(target_views_dir)
        if result.get("deleted"):
            deleted.append(result.get("path"))

    # Clean empty views/container directories.
    target_views_root = TRIPLETS_DIR / target_model_id / "views"
    if target_views_root.exists() and not any(target_views_root.iterdir()):
        result = _delete_tree(target_views_root)
        if result.get("deleted"):
            deleted.append(result.get("path"))
    target_triplet_dir = TRIPLETS_DIR / target_model_id
    if target_triplet_dir.exists() and not any(target_triplet_dir.iterdir()):
        result = _delete_tree(target_triplet_dir)
        if result.get("deleted"):
            deleted.append(result.get("path"))

    # Clean empty target model directory once all provider model files are gone.
    has_any_target_model = target_model_dir.exists() and any(
        p.is_file() and p.name.startswith("model_") for p in target_model_dir.iterdir()
    )
    if target_model_dir.exists() and not has_any_target_model:
        result = _delete_tree(target_model_dir)
        if result.get("deleted"):
            deleted.append(result.get("path"))

    if not deleted and not meta_updated:
        return jsonify({"error": "Target provider not found"}), 404

    return jsonify(
        {
            "success": True,
            "model_id": model_id,
            "edit_id": edit_id,
            "provider_id": provider_id,
            "deleted": deleted,
            "meta_updated": meta_updated,
        }
    )


@app.route("/api/models/<model_id>/edits/<edit_id>/restore", methods=["POST"])
def api_restore_failed_edit(model_id, edit_id):
    """Manually restore a failed edit batch (override quality gate)."""
    if not _is_safe_asset_id(model_id) or not _is_safe_asset_id(edit_id):
        return jsonify({"error": "Invalid model or edit id"}), 400

    edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
    if not edited_dir.exists():
        return jsonify({"error": "Edit batch not found"}), 404

    meta_path = edited_dir / "meta.json"
    if not meta_path.exists():
        return jsonify({"error": "Edit meta not found"}), 404

    meta = safe_load_json(meta_path, {})
    if not isinstance(meta, dict) or not meta:
        return jsonify({"error": "Invalid edit meta"}), 500

    data = request.get_json() or {}
    reviewer = data.get("reviewer", "manual")
    reason = data.get("reason", "manual restore from UI")

    quality_check = meta.get("quality_check")
    if not isinstance(quality_check, dict):
        quality_check = {}
    quality_check["manual_override"] = {
        "approved": True,
        "reviewer": str(reviewer),
        "reason": str(reason),
        "reviewed_at": datetime.now().isoformat(),
    }
    meta["quality_check"] = quality_check

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return jsonify({"success": True, "edit_id": edit_id, "restored": True})


@app.route("/api/models/<model_id>/edits/<edit_id>/unrestore", methods=["POST"])
def api_unrestore_failed_edit(model_id, edit_id):
    """Revoke manual restore flag for an edit batch."""
    if not _is_safe_asset_id(model_id) or not _is_safe_asset_id(edit_id):
        return jsonify({"error": "Invalid model or edit id"}), 400

    edited_dir = TRIPLETS_DIR / model_id / "edited" / edit_id
    if not edited_dir.exists():
        return jsonify({"error": "Edit batch not found"}), 404

    meta_path = edited_dir / "meta.json"
    if not meta_path.exists():
        return jsonify({"error": "Edit meta not found"}), 404

    meta = safe_load_json(meta_path, {})
    if not isinstance(meta, dict) or not meta:
        return jsonify({"error": "Invalid edit meta"}), 500

    data = request.get_json() or {}
    reviewer = data.get("reviewer", "manual")
    reason = data.get("reason", "manual restore revoked")

    quality_check = meta.get("quality_check")
    if not isinstance(quality_check, dict):
        quality_check = {}
    quality_check["manual_override"] = {
        "approved": False,
        "reviewer": str(reviewer),
        "reason": str(reason),
        "reviewed_at": datetime.now().isoformat(),
    }
    meta["quality_check"] = quality_check

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return jsonify({"success": True, "edit_id": edit_id, "restored": False})


# =============================================================================
# Routes - Static Files
# =============================================================================

# Cache durations (in seconds)
CACHE_STATIC = 31536000  # 1 year for JS/CSS
CACHE_GLB = 14400  # 4 hours for GLB files
CACHE_IMAGES = 86400  # 1 day for images


def _get_cache_max_age(filename: str) -> int:
    """Resolve cache duration by file type/path.

    Rendered view images are set to no-cache so re-renders are visible immediately.
    """
    normalized = filename.replace("\\", "/")
    ext = Path(normalized).suffix.lower()

    if ext == ".glb":
        return CACHE_GLB
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if "/triplets/" in f"/{normalized}" and "/views/" in f"/{normalized}":
            return 0
        if "/_semantic_tmp/" in f"/{normalized}":
            return 0
        return CACHE_IMAGES
    if ext in (".js", ".css", ".woff", ".woff2", ".ttf"):
        return CACHE_STATIC
    return CACHE_IMAGES


def _make_response_with_cache(directory: Path, filename: str, max_age: int):
    """Create a response with Cache-Control header."""
    from flask import make_response

    try:
        response = make_response(send_from_directory(directory, filename))
    except Exception:
        raise

    # Add cache header
    if max_age <= 0:
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    else:
        response.headers["Cache-Control"] = f"public, max-age={max_age}"

    # Add CORS headers for cross-origin access
    response.headers["Access-Control-Allow-Origin"] = "*"

    return response


def _set_no_store_headers(response):
    """Force dynamic responses to bypass any browser/proxy cache."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/data/<path:filename>")
def serve_data(filename):
    """Serve files from data directory with caching.

    Cache durations:
    - GLB files: 4 hours
    - Images (png/jpg/jpeg/webp): 1 day
    - Static assets (js/css): 1 year
    """
    max_age = _get_cache_max_age(filename)
    return _make_response_with_cache(PROJECT_ROOT / "data", filename, max_age)


@app.route("/workspace/<path:filename>")
def serve_workspace(filename):
    """Serve files from workspace directory."""
    return send_from_directory(PROJECT_ROOT / "workspace", filename)


@app.route("/pipeline/<path:filename>")
def serve_pipeline(filename):
    """Serve files directly from PIPELINE_DIR with caching.

    Used when pipeline_dir is an external absolute path (outside PROJECT_ROOT),
    where the /data/<filename> route cannot reach the files.

    Cache durations:
    - GLB files: 4 hours
    - Images (png/jpg/jpeg/webp): 1 day
    - Static assets (js/css): 1 year
    """
    max_age = _get_cache_max_age(filename)
    return _make_response_with_cache(PIPELINE_DIR, filename, max_age)


# =============================================================================
# Task Recovery
# =============================================================================


def recover_interrupted_tasks():
    """Recover gen3d tasks that were interrupted.

    On startup, finds tasks that:
    1. Were 'running' with remote_task_id -> resume polling only
    2. Were 'pending' without remote_task_id -> restart from scratch
    """
    with tasks_file_lock:
        tasks = load_jsonl(TASKS_FILE)

    resumed_count = 0
    restarted_count = 0

    for task in tasks:
        task_id = task.get("id")
        task_type = task.get("type")
        status = task.get("status")
        params = task.get("params", {})

        if task_type != "gen3d":
            continue

        if status in ("running", "pending"):
            remote_task_id = params.get("remote_task_id")  # Optional: for task recovery

            if remote_task_id:
                # Has remote_task_id - just resume polling (don't resubmit)
                print(
                    f"  [Recovery] Resuming gen3d task {task_id}, polling remote {remote_task_id}"
                )
                task["status"] = "pending"  # Will become running when processed
                with task_lock:
                    task_store[task_id] = task

                thread = threading.Thread(target=process_task, args=(task_id,))
                thread.daemon = True
                thread.start()
                resumed_count += 1
            elif status == "pending":
                # No remote_task_id and was pending - needs fresh submit
                print(
                    f"  [Recovery] Restarting pending gen3d task {task_id} (no remote_task_id)"
                )
                with task_lock:
                    task_store[task_id] = task

                thread = threading.Thread(target=process_task, args=(task_id,))
                thread.daemon = True
                thread.start()
                restarted_count += 1
            else:
                # Was running but no remote_task_id saved (shouldn't happen with new logic)
                # Mark as failed since we can't recover
                print(
                    f"  [Recovery] Cannot recover task {task_id} - no remote_task_id saved"
                )
                task["status"] = "failed"
                task["error"] = "Task interrupted before remote_task_id was saved"
                update_task_in_file(task_id, task)

    if resumed_count or restarted_count:
        print(
            f"  [Recovery] Resumed polling: {resumed_count}, Restarted: {restarted_count}"
        )
    else:
        print("  [Recovery] No interrupted gen3d tasks found")


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    print("Starting Pipeline Visualization Server...")
    print(f"Project Root: {PROJECT_ROOT}")

    # Initialize concurrency limits and directory paths from config (must be first)
    print("Initializing concurrency limits...")
    init_semaphores()

    # Ensure directories exist (after init_semaphores which sets dir globals from config)
    for d in [PROMPTS_DIR, IMAGES_DIR, MODELS_DIR, INSTRUCTIONS_DIR, TRIPLETS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # Kick off background SQLite index reconcile (non-blocking)
    print("Starting background index reconcile...")
    _start_index_reconcile()

    print(f"Pipeline Dir: {PIPELINE_DIR}")

    # Recover interrupted tasks from previous session
    print("Checking for interrupted tasks...")
    recover_interrupted_tasks()

    # Only watch data/pipeline for changes (not source code to avoid frequent reloads)
    # Set USE_RELOADER=0 to disable auto-reload completely
    import os

    use_reloader = os.getenv("USE_RELOADER", "0") == "1"

    extra_files = []
    if use_reloader:
        # Only watch data/pipeline directory for data changes
        pipeline_path = PROJECT_ROOT / "data" / "pipeline"
        if pipeline_path.exists():
            for dirname, dirs, files in os.walk(str(pipeline_path)):
                # Only watch metadata files, not images/models
                for filename in files:
                    if filename.endswith((".json", ".jsonl")):
                        extra_files.append(os.path.join(dirname, filename))

    port = int(os.getenv("PORT", "10002"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=True,
        extra_files=extra_files if extra_files else None,
        use_reloader=use_reloader,
    )
