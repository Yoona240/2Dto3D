"""
Method-2 edit quality checker: Two-Stage LLM edit correctness.

Stage 1A (VLM diff): Compares before/after view images and produces a
structured diff description — without seeing the instruction.

Stage 1B (LLM judge): Receives the diff description + instruction and
decides pass/fail — without seeing the images.

Returns the same ``EditQualityCheckResult`` used by Method-1 so the
downstream pipeline treats both methods identically.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.image.edit_quality_checker import (
    EDIT_STATUS_ERROR_QUALITY_CHECK,
    EDIT_STATUS_FAILED_QUALITY,
    EDIT_STATUS_PASSED,
    EditQualityCheckResult,
)
from core.image.instruction_display_resolver import (
    build_instruction_display_payload,
    validate_instruction_text,
)
from core.image.view_stitcher import VIEW_ORDER, ViewStitcher
from utils.llm_client import get_llm_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclass for the richer Method-2 result (extends the base result with
# per-stage payloads that get written into meta.json).
# ---------------------------------------------------------------------------

@dataclass
class EditCorrectnessDetail:
    """Structured detail written to meta.json under quality_check.stage_edit_correctness."""

    status: str
    view_policy: str
    checked_views: List[str]
    view_sanity_result: Optional[Dict[str, Any]]
    diff_result: Optional[Dict[str, Any]]
    judge_result: Optional[Dict[str, Any]]
    relabel_result: Optional[Dict[str, Any]]
    rejudge_result: Optional[Dict[str, Any]]
    original_instruction: str
    candidate_rewrite_instruction: Optional[str]
    effective_instruction: str
    relabel_reason: Optional[str]
    instruction_display_source: str
    instruction_display_status: str
    reason: str
    # Method-3 (unified_judge) only: full unified VLM response
    unified_result: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------

_DIFF_SYSTEM_PROMPT = (
    ""
)

_DIFF_PROMPT_PREFIX = "Find the differences between these two images. "
_DIFF_PROMPT_ADD = (
    "What is expected is that a part is added to the object in the first image "
    "to create the second image."
)
_DIFF_PROMPT_REMOVE = (
    "What is expected is that a part in the first image is removed to create "
    "the second image."
)
_DIFF_PROMPT_REPLACE = (
    "What is expected is that an aspect or part of the object in the first image "
    "is replaced to create the second image."
)
_DIFF_PROMPT_TEXTURE = (
    "What is expected is that the color/texture of the object in the first image "
    "is modified to create the second image."
)

_JUDGE_SYSTEM_PROMPT = (
    ""
)

_JUDGE_USER_PROMPT_TEMPLATE = (
    '{diff}\n\nAssuming the first image is the starting point, does the above list '
    'contain changes that fulfill the instruction "{instruction}"? Ignore '
    "differences that are not required by the instruction, they are allowed as "
    "long as the changes required by the instruction are present. Also note that "
    "the differences could use different terms to describe the same thing. "
    "State your rationale briefly first and then output the final answer, either "
    "Yes or No, enclosed in <answer></answer>"
)

_RELABEL_USER_PROMPT_TEMPLATE = (
    "Original instruction:\n"
    "{instruction}\n\n"
    "Observed edit diff:\n"
    "{diff}\n\n"
    "You are auditing what edit was actually completed. "
    "If the observed edit can be cleanly rewritten as a single valid instruction, "
    "return JSON with status='rewrite'. Otherwise return JSON with "
    "status='cannot_rewrite'.\n\n"
    "Rules:\n"
    "1. Output one JSON object only.\n"
    "2. Allowed edit types: remove or replace.\n"
    "3. The instruction must be one sentence starting with 'Remove' or 'Replace'.\n"
    "4. Do not use left/right lateral terms.\n"
    "5. Do not describe texture/color/material changes.\n"
    "6. Do not invent edits not supported by the diff.\n"
    "7. If evidence is weak or the edit is off-topic, return cannot_rewrite.\n\n"
    "JSON schema:\n"
    "{{\n"
    '  "status": "rewrite" | "cannot_rewrite",\n'
    '  "edit_type": "remove" | "replace",\n'
    '  "instruction": "Replace the wheels with skids",\n'
    '  "reason": "brief rationale",\n'
    '  "evidence": ["fact 1", "fact 2"]\n'
    "}}"
)

_VIEW_SANITY_SYSTEM_PROMPT = ""

_VIEW_SANITY_USER_PROMPT = (
    "You are checking whether the AFTER image of a 3D object has any obviously wrong geometry.\n\n"
    "Image 1: BEFORE the edit (reference — shows the object's normal shape)\n"
    "Image 2: AFTER the edit (to be checked)\n\n"
    "View order is fixed:\n"
    "Row 1: front, back, right\n"
    "Row 2: left,  top,  bottom\n\n"
    "Your task: check whether any view in the AFTER image shows geometry or proportions "
    "that are obviously inconsistent with the other views in the AFTER image.\n\n"
    "Use the BEFORE image only as a reference for what the object looks like — "
    "do NOT flag changes that are simply the result of a valid edit.\n\n"
    "Focus on:\n"
    "- Does any single view show the object with wildly different proportions compared to "
    "other views? (e.g., the object appears 3x taller in the left view than in the front view)\n"
    "- Does any view show obviously broken geometry, floating parts, or missing major structure "
    "that contradicts what other views show?\n\n"
    "Do NOT flag:\n"
    "- Differences between BEFORE and AFTER that are consistent across all views (valid edit)\n"
    "- Minor rendering variations between views\n"
    "- Parts not visible from a given angle\n\n"
    "Decision rule:\n"
    'Output "pass" if all views in the AFTER image are geometrically self-consistent.\n'
    'Output "fail" if any view shows obvious geometric anomalies relative to the other views.\n\n'
    "Return JSON only:\n"
    '{"decision": "pass|fail", "reason": "short explanation", '
    '"problematic_views": ["left", ...]}'
)


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _build_steer3d_diff_prompt(instruction: str) -> str:
    """Build the Stage-1A prompt using Steer3D's exact branching logic."""
    instr = instruction.strip().lower()
    if instr.startswith("add"):
        suffix = _DIFF_PROMPT_ADD
    elif instr.startswith("remove"):
        suffix = _DIFF_PROMPT_REMOVE
    elif instr.startswith("replace"):
        suffix = _DIFF_PROMPT_REPLACE
    else:
        suffix = _DIFF_PROMPT_TEXTURE
    return _DIFF_PROMPT_PREFIX + suffix


def _extract_answer_tag(text: str) -> str:
    """Extract Yes/No from <answer>...</answer>."""
    match = re.search(r"<answer>(.*?)</answer>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError("judge response missing <answer>...</answer> tag")
    return match.group(1).strip().lower()


def _validate_diff_result(data: Dict[str, Any]) -> None:
    """Fail Loudly if diff output is malformed."""
    if "diff_text" not in data:
        raise ValueError("diff result missing required field: diff_text")
    if not isinstance(data["diff_text"], str) or not data["diff_text"].strip():
        raise ValueError("diff result field 'diff_text' must be a non-empty string")
    if "prompt" not in data:
        raise ValueError("diff result missing required field: prompt")
    if not isinstance(data["prompt"], str) or not data["prompt"].strip():
        raise ValueError("diff result field 'prompt' must be a non-empty string")


def _validate_judge_result(data: Dict[str, Any]) -> None:
    """Fail Loudly if judge JSON is malformed."""
    decision = data.get("decision")
    if not isinstance(decision, str) or decision.strip().lower() not in ("pass", "fail"):
        raise ValueError(
            f"judge result 'decision' must be 'pass' or 'fail', got: {decision!r}"
        )
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("judge result 'reason' must be a non-empty string")
    for field in ("matched_requirements", "missing_requirements", "violations"):
        if field not in data:
            raise ValueError(f"judge result missing required field: {field}")
        if not isinstance(data[field], list):
            raise ValueError(f"judge result field '{field}' must be a list")


def _safe_parse_json_object(text: str, *, context: str) -> Dict[str, Any]:
    payload = (text or "").strip()
    if not payload:
        raise ValueError(f"{context} response is empty")
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?", "", payload, flags=re.IGNORECASE).strip()
        payload = re.sub(r"```$", "", payload).strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{context} response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{context} response must be a JSON object")
    return data


def _validate_relabel_result(data: Dict[str, Any]) -> Dict[str, Any]:
    status = data.get("status")
    if status not in {"rewrite", "cannot_rewrite"}:
        raise ValueError(f"relabel status must be 'rewrite' or 'cannot_rewrite', got: {status!r}")
    edit_type = data.get("edit_type")
    if edit_type not in {"remove", "replace"}:
        raise ValueError(
            f"relabel edit_type must be 'remove' or 'replace', got: {edit_type!r}"
        )
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("relabel reason must be a non-empty string")
    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("relabel evidence must be a list")
    normalized_evidence: List[str] = []
    for index, item in enumerate(evidence):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"relabel evidence[{index}] must be a non-empty string")
        normalized_evidence.append(item.strip())

    instruction = _clean_instruction_field(data.get("instruction"))
    if status == "rewrite":
        instruction = validate_instruction_text(
            instruction,
            expected_edit_type=edit_type,
        )
    elif instruction:
        instruction = validate_instruction_text(
            instruction,
            expected_edit_type=edit_type,
        )

    return {
        "status": status,
        "edit_type": edit_type,
        "instruction": instruction,
        "reason": reason.strip(),
        "evidence": normalized_evidence,
    }


def _clean_instruction_field(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def run_stage1_diff(
    *,
    before_image_path: Path,
    after_image_path: Path,
    instruction: str,
    diff_client: Any,
    log_dir: Optional[Path],
) -> Dict[str, Any]:
    """Stage 1A: Steer3D-style before/after difference description."""
    before_image_path = Path(before_image_path)
    after_image_path = Path(after_image_path)
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction must be a non-empty string")

    if not before_image_path.exists():
        raise FileNotFoundError(f"Before image not found: {before_image_path}")
    if not after_image_path.exists():
        raise FileNotFoundError(f"After image not found: {after_image_path}")

    prompt = _build_steer3d_diff_prompt(instruction)
    raw = diff_client.chat_with_images(
        system_prompt=_DIFF_SYSTEM_PROMPT,
        user_prompt=prompt,
        images=[before_image_path, after_image_path],
        log_dir=log_dir,
    )
    result = {
        "prompt": prompt,
        "diff_text": raw.strip(),
    }
    _validate_diff_result(result)
    return result


def run_stage1_judge(
    *,
    instruction: str,
    diff_text: str,
    judge_client: Any,
    log_dir: Optional[Path],
) -> Dict[str, Any]:
    """Stage 1B: Steer3D-style text judge with <answer>Yes/No</answer>."""
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction must be a non-empty string")
    if not isinstance(diff_text, str) or not diff_text.strip():
        raise ValueError("diff_text must be a non-empty string")

    user_prompt = _JUDGE_USER_PROMPT_TEMPLATE.format(
        instruction=instruction,
        diff=diff_text,
    )

    raw = judge_client.chat(
        system_prompt=_JUDGE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        log_dir=log_dir,
    )
    answer = _extract_answer_tag(raw)
    is_yes = "yes" in answer
    parsed = {
        "decision": "pass" if is_yes else "fail",
        "reason": raw.strip(),
        "matched_requirements": [instruction] if is_yes else [],
        "missing_requirements": [] if is_yes else [instruction],
        "violations": [],
    }
    _validate_judge_result(parsed)
    return parsed


def run_stage1_relabel(
    *,
    instruction: str,
    diff_text: str,
    relabel_client: Any,
    log_dir: Optional[Path],
    allow_image_input: bool,
    before_image_path: Optional[Path] = None,
    after_image_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Stage 1 relabel: rewrite the instruction to match the observed edit."""
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction must be a non-empty string")
    if not isinstance(diff_text, str) or not diff_text.strip():
        raise ValueError("diff_text must be a non-empty string")

    user_prompt = _RELABEL_USER_PROMPT_TEMPLATE.format(
        instruction=instruction,
        diff=diff_text,
    )

    raw_response = ""
    if allow_image_input and before_image_path and after_image_path:
        raw_response = relabel_client.chat_with_images(
            system_prompt="",
            user_prompt=user_prompt,
            images=[before_image_path, after_image_path],
            log_dir=log_dir,
        )
    else:
        raw_response = relabel_client.chat(
            system_prompt="",
            user_prompt=user_prompt,
            log_dir=log_dir,
        )

    parsed = _safe_parse_json_object(raw_response, context="stage1_relabel")
    validated = _validate_relabel_result(parsed)
    validated["raw_response"] = (raw_response or "").strip()
    return validated


def _validate_view_sanity_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """Fail loudly if view sanity check JSON is malformed."""
    decision = data.get("decision")
    if not isinstance(decision, str) or decision.strip().lower() not in ("pass", "fail"):
        raise ValueError(
            f"view_sanity decision must be 'pass' or 'fail', got: {decision!r}"
        )
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("view_sanity reason must be a non-empty string")
    problematic_views = data.get("problematic_views")
    if not isinstance(problematic_views, list):
        raise ValueError("view_sanity problematic_views must be a list")
    return {
        "decision": decision.strip().lower(),
        "reason": reason.strip(),
        "problematic_views": problematic_views,
    }


def run_view_sanity_check(
    *,
    before_grid_path: Path,
    after_grid_path: Path,
    sanity_client: Any,
    log_dir: Optional[Path],
) -> Dict[str, Any]:
    """View Sanity Check（新 Stage 1A）：检查 after 拼图的 6 个视角是否几何自洽。

    before 拼图作为形状参照，不做 before vs after 的差量比较。
    fail 时调用方应立即退出，不进入后续 diff/judge/relabel。

    返回: {"decision": "pass"|"fail", "reason": str, "problematic_views": list}
    """
    raw = sanity_client.chat_with_images(
        system_prompt=_VIEW_SANITY_SYSTEM_PROMPT,
        user_prompt=_VIEW_SANITY_USER_PROMPT,
        images=[before_grid_path, after_grid_path],
        log_dir=log_dir,
    )
    data = _safe_parse_json_object(raw, context="view_sanity_check")
    return _validate_view_sanity_result(data)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class EditQualityCheckerV2:
    """Method-2 two-stage edit correctness checker."""

    def __init__(self, config: Any):
        self._config = config
        self._settings = config.edit_quality_check
        self._tsr = config.edit_quality_check.two_stage_recon
        self._relabel = config.stage1_relabel
        if self._tsr is None:
            raise ValueError(
                "EditQualityCheckerV2 requires edit_quality_check.two_stage_recon config"
            )
        self._view_sanity_client = get_llm_client(config.edit_quality_view_sanity_mllm)
        self._diff_client = get_llm_client(config.edit_quality_diff_mllm)
        self._judge_client = get_llm_client(config.edit_quality_judge_mllm)

    def close(self) -> None:
        self._view_sanity_client.close()
        self._diff_client.close()
        self._judge_client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ----- view policy helpers -----

    def _get_views_to_check(self) -> List[str]:
        """Return list of view names based on edit_view_policy."""
        if self._tsr.edit_view_policy == "front_only":
            return ["front"]
        elif self._tsr.edit_view_policy == "all_6":
            return list(VIEW_ORDER)
        elif self._tsr.edit_view_policy == "stitched_6":
            return ["stitched_6"]
        else:
            raise ValueError(
                f"Unknown edit_view_policy: {self._tsr.edit_view_policy}"
            )

    @staticmethod
    def _build_effective_after_views(
        before_views_dir: Path,
        after_views_dir: Path,
        output_dir: Path,
    ) -> Path:
        """Materialize effective AFTER views: missing edited views fallback to BEFORE."""
        output_dir.mkdir(parents=True, exist_ok=True)
        for view_name in VIEW_ORDER:
            before_path = before_views_dir / f"{view_name}.png"
            if not before_path.exists():
                raise FileNotFoundError(f"Missing before view: {before_path}")
            after_path = after_views_dir / f"{view_name}.png"
            source_path = after_path if after_path.exists() else before_path
            shutil.copy2(source_path, output_dir / f"{view_name}.png")
        return output_dir

    # ----- main entry -----

    def check(
        self,
        before_views_dir: Path,
        after_views_dir: Path,
        instruction: str,
        work_root_dir: Path,
        allow_stage1_relabel: Optional[bool] = None,
    ) -> EditQualityCheckResult:
        """Run two-stage edit correctness check.

        Signature mirrors ``EditQualityChecker.check`` so the router can
        call either one interchangeably.
        """
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

        views_to_check = self._get_views_to_check()
        require_all = self._tsr.require_all_views_pass
        relabel_enabled = self._relabel.enabled
        if allow_stage1_relabel is not None:
            relabel_enabled = bool(allow_stage1_relabel)

        all_diff_results: Dict[str, Any] = {}
        all_judge_results: Dict[str, Any] = {}
        overall_pass = True
        overall_reason_parts: List[str] = []
        relabel_result: Optional[Dict[str, Any]] = None
        rejudge_result: Optional[Dict[str, Any]] = None
        candidate_rewrite_instruction: Optional[str] = None
        effective_instruction = instruction
        before_relabel_image: Optional[Path] = None
        after_relabel_image: Optional[Path] = None

        # Always build effective_after_views (missing edited views fallback to before).
        # Used by both View Sanity Check and stitched_6 diff/judge.
        effective_after_dir = self._build_effective_after_views(
            before_views_dir=before_views_dir,
            after_views_dir=after_views_dir,
            output_dir=qc_dir / "_effective_after_views",
        )

        # ── View Sanity Check（新 Stage 1A）──────────────────────────────────
        # Check cross-view geometric consistency of the after image.
        # Uses 6-view grids regardless of edit_view_policy.
        # fail → immediate exit, no diff/judge/relabel.
        view_sanity_result: Optional[Dict[str, Any]] = None
        if self._tsr.view_sanity_check.enabled:
            _stitcher = ViewStitcher()
            _sanity_before_grid = qc_dir / "view_sanity_before_grid.png"
            _sanity_after_grid = qc_dir / "view_sanity_after_grid.png"
            _stitcher.stitch_views(
                views_dir=before_views_dir,
                output_path=_sanity_before_grid,
                view_names=VIEW_ORDER,
                pad_to_square=True,
            )
            _stitcher.stitch_views(
                views_dir=effective_after_dir,
                output_path=_sanity_after_grid,
                view_names=VIEW_ORDER,
                pad_to_square=True,
            )
            _sanity_log = (
                qc_dir / "view_sanity_logs" if self._settings.save_debug_assets else None
            )
            view_sanity_result = run_view_sanity_check(
                before_grid_path=_sanity_before_grid,
                after_grid_path=_sanity_after_grid,
                sanity_client=self._view_sanity_client,
                log_dir=_sanity_log,
            )
            if view_sanity_result["decision"] == "fail":
                _reason = f"[view_sanity] {view_sanity_result['reason']}"
                _status = EDIT_STATUS_FAILED_QUALITY
                _instruction_payload = build_instruction_display_payload(
                    instruction_text_original=instruction,
                    instruction_text_effective=instruction,
                    instruction_text_candidate_rewrite=None,
                    stage1_status=_status,
                    instruction_rewritten_by_stage1=False,
                    instruction_rewrite_reason=None,
                )
                _detail = EditCorrectnessDetail(
                    status=_status,
                    view_policy=self._tsr.edit_view_policy,
                    checked_views=[],
                    view_sanity_result=view_sanity_result,
                    diff_result={},
                    judge_result={},
                    relabel_result=None,
                    rejudge_result=None,
                    original_instruction=_instruction_payload["instruction_text_original"],
                    candidate_rewrite_instruction=None,
                    effective_instruction=instruction,
                    relabel_reason=None,
                    instruction_display_source=_instruction_payload[
                        "instruction_display_source"
                    ],
                    instruction_display_status=_instruction_payload[
                        "instruction_display_status"
                    ],
                    reason=_reason,
                )
                if self._settings.save_debug_assets:
                    (qc_dir / "edit_correctness_detail.json").write_text(
                        json.dumps({
                            "status": _detail.status,
                            "view_policy": _detail.view_policy,
                            "checked_views": _detail.checked_views,
                            "view_sanity_result": _detail.view_sanity_result,
                            "diff_result": _detail.diff_result,
                            "judge_result": _detail.judge_result,
                            "relabel_result": _detail.relabel_result,
                            "rejudge_result": _detail.rejudge_result,
                            "original_instruction": _detail.original_instruction,
                            "candidate_rewrite_instruction": _detail.candidate_rewrite_instruction,
                            "effective_instruction": _detail.effective_instruction,
                            "relabel_reason": _detail.relabel_reason,
                            "instruction_display_source": _detail.instruction_display_source,
                            "instruction_display_status": _detail.instruction_display_status,
                            "reason": _detail.reason,
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                else:
                    shutil.rmtree(qc_dir, ignore_errors=True)
                _result = EditQualityCheckResult(
                    status=_status,
                    reason=_reason,
                    before_grid_path=_sanity_before_grid,
                    after_grid_path=_sanity_after_grid,
                    raw_response=json.dumps(
                        {"view_sanity_result": view_sanity_result}, ensure_ascii=False
                    ),
                )
                _result.detail = _detail  # type: ignore[attr-defined]
                return _result
        # ─────────────────────────────────────────────────────────────────────

        if self._tsr.edit_view_policy == "stitched_6":
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
            before_relabel_image = before_grid_path
            after_relabel_image = after_grid_path

            view_name = "stitched_6"
            diff_log = qc_dir / "diff_logs_stitched_6" if self._settings.save_debug_assets else None
            judge_log = qc_dir / "judge_logs_stitched_6" if self._settings.save_debug_assets else None

            diff_result = run_stage1_diff(
                before_image_path=before_grid_path,
                after_image_path=after_grid_path,
                instruction=instruction,
                diff_client=self._diff_client,
                log_dir=diff_log,
            )
            all_diff_results[view_name] = diff_result

            judge_result = run_stage1_judge(
                instruction=instruction,
                diff_text=diff_result["diff_text"],
                judge_client=self._judge_client,
                log_dir=judge_log,
            )
            all_judge_results[view_name] = judge_result

            if judge_result["decision"] != "pass":
                overall_pass = False
                overall_reason_parts.append(f"{view_name}: {judge_result['reason']}")
        else:
            for view_name in views_to_check:
                before_path = before_views_dir / f"{view_name}.png"
                after_path = after_views_dir / f"{view_name}.png"

                if not before_path.exists():
                    raise FileNotFoundError(f"Missing before view: {before_path}")
                # If after view is missing, use before (no edit applied to this view)
                if not after_path.exists():
                    after_path = before_path
                if before_relabel_image is None:
                    before_relabel_image = before_path
                    after_relabel_image = after_path

                diff_log = (
                    qc_dir / f"diff_logs_{view_name}"
                    if self._settings.save_debug_assets
                    else None
                )
                judge_log = (
                    qc_dir / f"judge_logs_{view_name}"
                    if self._settings.save_debug_assets
                    else None
                )

                # Stage 1A
                diff_result = run_stage1_diff(
                    before_image_path=before_path,
                    after_image_path=after_path,
                    instruction=instruction,
                    diff_client=self._diff_client,
                    log_dir=diff_log,
                )
                all_diff_results[view_name] = diff_result

                # Stage 1B
                judge_result = run_stage1_judge(
                    instruction=instruction,
                    diff_text=diff_result["diff_text"],
                    judge_client=self._judge_client,
                    log_dir=judge_log,
                )
                all_judge_results[view_name] = judge_result

                view_passed = judge_result["decision"] == "pass"
                if not view_passed:
                    overall_pass = False
                    overall_reason_parts.append(
                        f"{view_name}: {judge_result['reason']}"
                    )

                # If any view fails and we don't require all, we can stop early
                if not view_passed and not require_all and len(views_to_check) > 1:
                    break

        if not overall_pass and relabel_enabled:
            diff_chunks: List[str] = []
            for view_name in all_judge_results.keys():
                view_diff = all_diff_results.get(view_name, {})
                diff_text = _clean_instruction_field(view_diff.get("diff_text"))
                if not diff_text:
                    continue
                if len(all_judge_results) == 1:
                    diff_chunks.append(diff_text)
                else:
                    diff_chunks.append(f"[{view_name}]\n{diff_text}")
            aggregate_diff_text = "\n\n".join(diff_chunks).strip()
            if not aggregate_diff_text:
                raise ValueError("stage1 relabel requires a non-empty aggregated diff_text")

            relabel_log = (
                qc_dir / "relabel_logs_attempt_1"
                if self._settings.save_debug_assets
                else None
            )
            relabel_result = run_stage1_relabel(
                instruction=instruction,
                diff_text=aggregate_diff_text,
                relabel_client=(
                    self._diff_client
                    if self._relabel.allow_image_input
                    and before_relabel_image
                    and after_relabel_image
                    else self._judge_client
                ),
                log_dir=relabel_log,
                allow_image_input=self._relabel.allow_image_input,
                before_image_path=before_relabel_image,
                after_image_path=after_relabel_image,
            )
            relabel_result["attempt_index"] = 1
            candidate_rewrite_instruction = _clean_instruction_field(
                relabel_result.get("instruction")
            ) or None
            if not self._relabel.save_raw_response:
                relabel_result.pop("raw_response", None)

            if relabel_result["status"] != "rewrite":
                overall_reason_parts.append(
                    f"relabel: {relabel_result['reason']}"
                )
            else:
                if not candidate_rewrite_instruction:
                    raise ValueError("stage1 relabel returned rewrite without instruction")

                if not self._relabel.require_rejudge_pass:
                    overall_pass = True
                    effective_instruction = candidate_rewrite_instruction
                else:
                    rejudge_log = (
                        qc_dir / "rejudge_logs_attempt_1"
                        if self._settings.save_debug_assets
                        else None
                    )
                    rejudge_result = run_stage1_judge(
                        instruction=candidate_rewrite_instruction,
                        diff_text=aggregate_diff_text,
                        judge_client=self._judge_client,
                        log_dir=rejudge_log,
                    )
                    if rejudge_result["decision"] == "pass":
                        overall_pass = True
                        effective_instruction = candidate_rewrite_instruction
                    else:
                        overall_reason_parts.append(
                            f"rejudge: {rejudge_result['reason']}"
                        )

        status = EDIT_STATUS_PASSED if overall_pass else EDIT_STATUS_FAILED_QUALITY
        if overall_pass and relabel_result and effective_instruction != instruction:
            if rejudge_result is not None:
                reason = "relabel succeeded and rejudge passed"
            else:
                reason = "relabel succeeded"
        else:
            reason = "all checked views passed" if overall_pass else "; ".join(
                overall_reason_parts
            )
        relabel_payload = None
        if relabel_result is not None:
            relabel_payload = dict(relabel_result)
            if rejudge_result is not None:
                relabel_payload["rejudge_result"] = rejudge_result
        instruction_payload = build_instruction_display_payload(
            instruction_text_original=instruction,
            instruction_text_effective=effective_instruction,
            instruction_text_candidate_rewrite=candidate_rewrite_instruction,
            stage1_status=status,
            instruction_rewritten_by_stage1=(
                bool(candidate_rewrite_instruction)
                and effective_instruction == candidate_rewrite_instruction
                and effective_instruction != instruction
            ),
            instruction_rewrite_reason=(
                relabel_result.get("reason") if relabel_result else None
            ),
            stage1_relabel_result=relabel_payload,
        )

        # Build detail payload for meta.json
        detail = EditCorrectnessDetail(
            status=status,
            view_policy=self._tsr.edit_view_policy,
            checked_views=list(all_judge_results.keys()),
            view_sanity_result=view_sanity_result,
            diff_result=all_diff_results,
            judge_result=all_judge_results,
            relabel_result=relabel_payload,
            rejudge_result=rejudge_result,
            original_instruction=instruction_payload["instruction_text_original"],
            candidate_rewrite_instruction=instruction_payload[
                "instruction_text_candidate_rewrite"
            ],
            effective_instruction=instruction_payload["instruction_text_effective"],
            relabel_reason=instruction_payload["instruction_rewrite_reason"],
            instruction_display_source=instruction_payload[
                "instruction_display_source"
            ],
            instruction_display_status=instruction_payload[
                "instruction_display_status"
            ],
            reason=reason,
        )

        # Persist debug JSON
        if self._settings.save_debug_assets:
            (qc_dir / "edit_correctness_detail.json").write_text(
                json.dumps({
                    "status": detail.status,
                    "view_policy": detail.view_policy,
                    "checked_views": detail.checked_views,
                    "view_sanity_result": detail.view_sanity_result,
                    "diff_result": detail.diff_result,
                    "judge_result": detail.judge_result,
                    "relabel_result": detail.relabel_result,
                    "rejudge_result": detail.rejudge_result,
                    "original_instruction": detail.original_instruction,
                    "candidate_rewrite_instruction": detail.candidate_rewrite_instruction,
                    "effective_instruction": detail.effective_instruction,
                    "relabel_reason": detail.relabel_reason,
                    "instruction_display_source": detail.instruction_display_source,
                    "instruction_display_status": detail.instruction_display_status,
                    "reason": detail.reason,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            shutil.rmtree(qc_dir, ignore_errors=True)

        # Pack into the common result type.
        # Method-2 does not produce grid images; keep those None.
        result = EditQualityCheckResult(
            status=status,
            reason=reason,
            before_grid_path=None,
            after_grid_path=None,
            raw_response=json.dumps({
                "view_sanity_result": view_sanity_result,
                "diff_result": all_diff_results,
                "judge_result": all_judge_results,
                "relabel_result": relabel_payload,
                "rejudge_result": rejudge_result,
            }, ensure_ascii=False),
        )
        # Attach detail for meta.json builder
        result.detail = detail  # type: ignore[attr-defined]
        return result
