"""
Method-3 edit quality checker: Unified VLM Judge.

Single VLM call with before/after 6-view grids + edit instruction.
Outputs observation, view sanity, instruction following, and relabel
in one structured JSON response.

Returns the same ``EditQualityCheckResult`` used by Method-1/2 so the
downstream pipeline treats all methods identically.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.image.edit_quality_checker import (
    EDIT_STATUS_ERROR_QUALITY_CHECK,
    EDIT_STATUS_FAILED_QUALITY,
    EDIT_STATUS_PASSED,
    EditQualityCheckResult,
)
from core.image.edit_quality_checker_v2 import (
    EditCorrectnessDetail,
    run_stage1_judge,
)
from core.image.instruction_display_resolver import (
    build_instruction_display_payload,
    validate_instruction_legality,
)
from core.image.view_stitcher import VIEW_ORDER, ViewStitcher
from utils.llm_client import get_llm_client
from utils.prompts import (
    UNIFIED_JUDGE_SYSTEM_PROMPT,
    UNIFIED_JUDGE_USER_PROMPT_TEMPLATE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

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


_LEGALITY_CATEGORIES = {
    "structural_part",
    "appearance_only",
    "main_body",
    "material_only",
    "unclear",
}


def _validate_unified_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the unified judge JSON. Fail Loudly on malformed output."""

    # --- observation ---
    observation = data.get("observation")
    if not isinstance(observation, str) or not observation.strip():
        raise ValueError("unified_judge: 'observation' must be a non-empty string")

    # --- supporting_views ---
    supporting_views = data.get("supporting_views")
    if not isinstance(supporting_views, list):
        raise ValueError("unified_judge: 'supporting_views' must be a list")
    normalized_supporting_views: List[str] = []
    for index, view_name in enumerate(supporting_views):
        if not isinstance(view_name, str) or not view_name.strip():
            raise ValueError(
                f"unified_judge: supporting_views[{index}] must be a non-empty string"
            )
        normalized_view_name = view_name.strip().lower()
        if normalized_view_name not in VIEW_ORDER:
            raise ValueError(
                f"unified_judge: supporting_views[{index}] must be one of {VIEW_ORDER}, "
                f"got: {view_name!r}"
            )
        normalized_supporting_views.append(normalized_view_name)

    # --- evidence_strength ---
    evidence_strength = data.get("evidence_strength")
    if (
        not isinstance(evidence_strength, str)
        or evidence_strength.strip().lower() not in ("strong", "medium", "weak")
    ):
        raise ValueError(
            "unified_judge: 'evidence_strength' must be 'strong', 'medium', or 'weak'"
        )
    evidence_strength_normalized = evidence_strength.strip().lower()

    # --- instruction_legality ---
    instruction_legality = data.get("instruction_legality")
    if not isinstance(instruction_legality, dict):
        raise ValueError("unified_judge: 'instruction_legality' must be a JSON object")
    il_decision = instruction_legality.get("decision")
    if (
        not isinstance(il_decision, str)
        or il_decision.strip().lower() not in ("allow", "reject")
    ):
        raise ValueError(
            "unified_judge: instruction_legality.decision must be 'allow' or 'reject'"
        )
    il_category = instruction_legality.get("category")
    if (
        not isinstance(il_category, str)
        or il_category.strip().lower() not in _LEGALITY_CATEGORIES
    ):
        raise ValueError(
            "unified_judge: instruction_legality.category must be one of "
            f"{sorted(_LEGALITY_CATEGORIES)}"
        )
    il_reason = instruction_legality.get("reason")
    if not isinstance(il_reason, str) or not il_reason.strip():
        raise ValueError(
            "unified_judge: instruction_legality.reason must be a non-empty string"
        )
    normalized_legality_decision = il_decision.strip().lower()
    normalized_legality_category = il_category.strip().lower()
    if (
        normalized_legality_decision == "allow"
        and normalized_legality_category != "structural_part"
    ):
        raise ValueError(
            "unified_judge: instruction_legality.category must be 'structural_part' "
            "when decision='allow'"
        )

    # --- view_sanity ---
    view_sanity = data.get("view_sanity")
    if not isinstance(view_sanity, dict):
        raise ValueError("unified_judge: 'view_sanity' must be a JSON object")
    vs_decision = view_sanity.get("decision")
    if not isinstance(vs_decision, str) or vs_decision.strip().lower() not in ("pass", "fail"):
        raise ValueError(
            f"unified_judge: view_sanity.decision must be 'pass' or 'fail', got: {vs_decision!r}"
        )
    vs_reason = view_sanity.get("reason")
    if not isinstance(vs_reason, str) or not vs_reason.strip():
        raise ValueError("unified_judge: view_sanity.reason must be a non-empty string")
    vs_problematic = view_sanity.get("problematic_views")
    if not isinstance(vs_problematic, list):
        raise ValueError("unified_judge: view_sanity.problematic_views must be a list")

    # --- instruction_following ---
    instr_follow = data.get("instruction_following")
    if not isinstance(instr_follow, dict):
        raise ValueError("unified_judge: 'instruction_following' must be a JSON object")
    if_decision = instr_follow.get("decision")
    if not isinstance(if_decision, str) or if_decision.strip().lower() not in ("pass", "fail"):
        raise ValueError(
            f"unified_judge: instruction_following.decision must be 'pass' or 'fail', "
            f"got: {if_decision!r}"
        )
    if_reason = instr_follow.get("reason")
    if not isinstance(if_reason, str) or not if_reason.strip():
        raise ValueError(
            "unified_judge: instruction_following.reason must be a non-empty string"
        )
    if if_decision.strip().lower() == "pass" and not normalized_supporting_views:
        raise ValueError(
            "unified_judge: supporting_views must be non-empty when "
            "instruction_following.decision='pass'"
        )
    if evidence_strength_normalized in ("strong", "medium") and not normalized_supporting_views:
        raise ValueError(
            "unified_judge: supporting_views must be non-empty when evidence_strength "
            "is 'strong' or 'medium'"
        )

    # --- relabel ---
    relabel = data.get("relabel")
    if not isinstance(relabel, dict):
        raise ValueError("unified_judge: 'relabel' must be a JSON object")
    rl_status = relabel.get("status")
    if not isinstance(rl_status, str) or rl_status.strip().lower() not in (
        "none", "rewrite", "cannot_rewrite",
    ):
        raise ValueError(
            f"unified_judge: relabel.status must be 'none', 'rewrite', or 'cannot_rewrite', "
            f"got: {rl_status!r}"
        )

    rl_instruction = (relabel.get("instruction") or "").strip()
    rl_reason = (relabel.get("reason") or "").strip()

    # If rewrite, instruction is mandatory and must be valid
    if rl_status.strip().lower() == "rewrite":
        if not rl_instruction:
            raise ValueError(
                "unified_judge: relabel.instruction is required when status='rewrite'"
            )
        rl_instruction = validate_instruction_legality(rl_instruction)

    return {
        "observation": observation.strip(),
        "supporting_views": normalized_supporting_views,
        "evidence_strength": evidence_strength_normalized,
        "instruction_legality": {
            "decision": normalized_legality_decision,
            "category": normalized_legality_category,
            "reason": il_reason.strip(),
        },
        "view_sanity": {
            "decision": vs_decision.strip().lower(),
            "reason": vs_reason.strip(),
            "problematic_views": vs_problematic,
        },
        "instruction_following": {
            "decision": if_decision.strip().lower(),
            "reason": if_reason.strip(),
        },
        "relabel": {
            "status": rl_status.strip().lower(),
            "instruction": rl_instruction,
            "reason": rl_reason,
        },
    }


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def run_unified_judge(
    *,
    before_grid_path: Path,
    after_grid_path: Path,
    edit_mask_grid_path: Path,
    instruction: str,
    vlm_client: Any,
    log_dir: Optional[Path],
) -> Dict[str, Any]:
    """Single VLM call: observation + legality + view_sanity + instruction_following + relabel."""
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction must be a non-empty string")
    if not before_grid_path.exists():
        raise FileNotFoundError(f"Before grid not found: {before_grid_path}")
    if not after_grid_path.exists():
        raise FileNotFoundError(f"After grid not found: {after_grid_path}")
    if not edit_mask_grid_path.exists():
        raise FileNotFoundError(f"Edit mask grid not found: {edit_mask_grid_path}")

    user_prompt = UNIFIED_JUDGE_USER_PROMPT_TEMPLATE.format(instruction=instruction)
    raw = vlm_client.chat_with_images(
        system_prompt=UNIFIED_JUDGE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        images=[before_grid_path, after_grid_path, edit_mask_grid_path],
        log_dir=log_dir,
    )

    parsed = _safe_parse_json_object(raw, context="unified_judge")
    validated = _validate_unified_result(parsed)
    validated["raw_response"] = (raw or "").strip()
    return validated


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class EditQualityCheckerUnified:
    """Method-3 unified VLM judge."""

    def __init__(self, config: Any):
        self._config = config
        self._settings = config.edit_quality_check
        self._uj = config.edit_quality_check.unified_judge
        if self._uj is None:
            raise ValueError(
                "EditQualityCheckerUnified requires edit_quality_check.unified_judge config"
            )
        self._vlm_client = get_llm_client(config.edit_quality_unified_mllm)
        # Optional: judge client for rejudge (only if require_rejudge_after_relabel)
        self._judge_client = None
        if self._uj.require_rejudge_after_relabel:
            self._judge_client = get_llm_client(config.edit_quality_judge_mllm)

    def close(self) -> None:
        self._vlm_client.close()
        if self._judge_client is not None:
            self._judge_client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

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

    def check(
        self,
        before_views_dir: Path,
        after_views_dir: Path,
        instruction: str,
        work_root_dir: Path,
        allow_stage1_relabel: Optional[bool] = None,
    ) -> EditQualityCheckResult:
        """Run unified VLM judge.

        Signature mirrors ``EditQualityChecker.check`` / ``EditQualityCheckerV2.check``
        so the router can call any checker interchangeably.
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

        # Build effective after views (fallback missing to before)
        effective_after_dir = self._build_effective_after_views(
            before_views_dir=before_views_dir,
            after_views_dir=after_views_dir,
            output_dir=qc_dir / "_effective_after_views",
        )

        # Stitch 6-view grids
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
        edit_mask_grid_path = work_root_dir / "edit_mask_grid.png"

        # Single VLM call
        vlm_log = qc_dir / "unified_judge_logs" if self._settings.save_debug_assets else None
        unified_result = run_unified_judge(
            before_grid_path=before_grid_path,
            after_grid_path=after_grid_path,
            edit_mask_grid_path=edit_mask_grid_path,
            instruction=instruction,
            vlm_client=self._vlm_client,
            log_dir=vlm_log,
        )

        # --- Decision logic ---
        view_sanity = unified_result["view_sanity"]
        instr_follow = unified_result["instruction_following"]
        relabel = unified_result["relabel"]
        evidence_strength = unified_result["evidence_strength"]
        instruction_legality = unified_result["instruction_legality"]

        overall_pass = False
        reason_parts: List[str] = []
        effective_instruction = instruction
        candidate_rewrite_instruction: Optional[str] = None
        rejudge_result: Optional[Dict[str, Any]] = None

        if view_sanity["decision"] == "fail":
            # Geometry error — immediate fail
            reason_parts.append(f"[view_sanity] {view_sanity['reason']}")
        elif instruction_legality["decision"] == "reject":
            reason_parts.append(
                f"[instruction_legality] {instruction_legality['category']}: "
                f"{instruction_legality['reason']}"
            )
        elif self._uj.require_non_weak_evidence and evidence_strength == "weak":
            reason_parts.append("[evidence] weak visual evidence for the requested edit")
        elif instr_follow["decision"] == "pass":
            # Instruction followed — pass
            overall_pass = True
        else:
            # Instruction not followed — try relabel
            reason_parts.append(
                f"[instruction_following] {instr_follow['reason']}"
            )
            if relabel["status"] == "rewrite" and relabel["instruction"]:
                candidate_rewrite_instruction = relabel["instruction"]

                if self._uj.require_rejudge_after_relabel and self._judge_client:
                    # Use observation as diff_text for rejudge
                    rejudge_log = (
                        qc_dir / "rejudge_logs"
                        if self._settings.save_debug_assets
                        else None
                    )
                    rejudge_result = run_stage1_judge(
                        instruction=candidate_rewrite_instruction,
                        diff_text=unified_result["observation"],
                        judge_client=self._judge_client,
                        log_dir=rejudge_log,
                    )
                    if rejudge_result["decision"] == "pass":
                        overall_pass = True
                        effective_instruction = candidate_rewrite_instruction
                    else:
                        reason_parts.append(
                            f"[rejudge] {rejudge_result['reason']}"
                        )
                else:
                    # No rejudge required — trust the VLM relabel
                    overall_pass = True
                    effective_instruction = candidate_rewrite_instruction
            elif relabel["status"] == "cannot_rewrite":
                reason_parts.append(
                    f"[relabel] cannot_rewrite: {relabel['reason']}"
                )
            else:
                # status == "none" but instruction_following == fail — contradiction, treat as fail
                reason_parts.append("[relabel] status=none but instruction not followed")

        status = EDIT_STATUS_PASSED if overall_pass else EDIT_STATUS_FAILED_QUALITY
        if overall_pass and candidate_rewrite_instruction and effective_instruction != instruction:
            reason = "relabel succeeded" + (
                " and rejudge passed" if rejudge_result else ""
            )
        else:
            reason = (
                "unified judge passed" if overall_pass else "; ".join(reason_parts)
            )

        # Build instruction display payload
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
            instruction_rewrite_reason=relabel.get("reason") if relabel["status"] == "rewrite" else None,
        )

        # Build relabel payload for meta compatibility
        relabel_payload = None
        if relabel["status"] != "none":
            relabel_payload = {
                "status": relabel["status"],
                "instruction": relabel["instruction"],
                "reason": relabel["reason"],
            }
            if rejudge_result is not None:
                relabel_payload["rejudge_result"] = rejudge_result

        # Build detail
        detail = EditCorrectnessDetail(
            status=status,
            view_policy="stitched_6",
            checked_views=["stitched_6"],
            view_sanity_result=view_sanity,
            diff_result=None,
            judge_result=None,
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
            unified_result=unified_result,
        )

        # Persist debug JSON
        if self._settings.save_debug_assets:
            (qc_dir / "edit_correctness_detail.json").write_text(
                json.dumps({
                    "status": detail.status,
                    "view_policy": detail.view_policy,
                    "checked_views": detail.checked_views,
                    "view_sanity_result": detail.view_sanity_result,
                    "unified_result": detail.unified_result,
                    "relabel_result": detail.relabel_result,
                    "rejudge_result": detail.rejudge_result,
                    "edit_mask_grid_path": str(edit_mask_grid_path),
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

        result = EditQualityCheckResult(
            status=status,
            reason=reason,
            before_grid_path=before_grid_path,
            after_grid_path=after_grid_path,
            raw_response=json.dumps(unified_result, ensure_ascii=False),
        )
        result.detail = detail  # type: ignore[attr-defined]
        return result
