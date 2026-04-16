from __future__ import annotations

import re
from typing import Any, Dict, Optional

from core.image.edit_quality_checker import (
    EDIT_STATUS_ERROR_QUALITY_CHECK,
    EDIT_STATUS_FAILED_QUALITY,
    EDIT_STATUS_PASSED,
)


_FORBIDDEN_LATERAL_PATTERNS = [
    r"\bleft\b",
    r"\bright\b",
    r"\bfront\s*[- ]\s*left\b",
    r"\bfront\s*[- ]\s*right\b",
    r"\brear\s*[- ]\s*left\b",
    r"\brear\s*[- ]\s*right\b",
    r"\bport\b",
    r"\bstarboard\b",
    r"左",
    r"右",
]

_AMBIGUOUS_WHEEL_PATTERNS = [
    r"\bremove\s+(a|an|one)\s+\w*\s*wheel\b",
    r"\breplace\s+(a|an|one)\s+\w*\s*wheel\b",
    r"\bremove\s+(a|an|one)\s+wheel\b",
    r"\breplace\s+(a|an|one)\s+wheel\b",
    r"删除.*一个.*轮",
    r"移除.*一个.*轮",
    r"删(除|掉).*一个.*轮",
    r"删(除|掉).*轮.*(左|右)?(前|后)?侧?\s*一个",
    r"替换\s*一个.*轮",
    r"更换\s*一个.*轮",
]

_TEXTURE_KEYWORDS = [
    # 外观/材质操作名词（作为操作对象出现，通常意味着在操作外观本身）
    "color",
    "colour",
    "texture",
    "material",
    "finish",
    "pattern",
    "gloss",
    # 明确的操作动词/短语
    "paint",
    "repaint",
    "recolor",
    "recolour",
    "change the color",
    "change the colour",
    "change its color",
    "change its colour",
]

_MAIN_BODY_PATTERNS = [
    r"\breplace\s+the\s+entire\b",
    r"\breplace\s+the\s+whole\b",
    r"\bremove\s+the\s+entire\b",
    r"\bwhole\s+object\b",
    r"\bentire\s+object\b",
]

_APPEARANCE_ONLY_KEYWORDS = [
    "logo",
    "emblem",
    "label",
    "seam line",
]

_MATERIAL_SWAP_TERMS = [
    "wood",
    "wooden",
    "metal",
    "metallic",
    "fabric",
    "leather",
    "plastic",
    "glass",
]

_REFUSAL_PHRASES = [
    "i cannot",
    "i can't",
    "i am unable",
    "sorry",
    "i apologize",
    "as an ai",
    "cannot assist",
    "cannot help",
    "against my policy",
    "unable to generate",
]


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _first_clean_text(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _normalize_status(value: Any) -> Optional[str]:
    text = _clean_text(value)
    return text or None


def _normalize_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _contains_forbidden_lateral_terms(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    return any(
        re.search(pattern, lowered, flags=re.IGNORECASE)
        for pattern in _FORBIDDEN_LATERAL_PATTERNS
    )


def _contains_ambiguous_symmetric_part(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    return any(
        re.search(pattern, lowered, flags=re.IGNORECASE)
        for pattern in _AMBIGUOUS_WHEEL_PATTERNS
    )


def _is_texture_edit(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    return any(
        re.search(r"\b" + re.escape(keyword) + r"\b", lowered)
        for keyword in _TEXTURE_KEYWORDS
    )


def _is_main_body_edit(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    return any(
        re.search(pattern, lowered, flags=re.IGNORECASE)
        for pattern in _MAIN_BODY_PATTERNS
    )


def _is_appearance_only_edit(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    return any(
        re.search(r"\b" + re.escape(keyword) + r"\b", lowered)
        for keyword in _APPEARANCE_ONLY_KEYWORDS
    )


def _is_material_swap_edit(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    if not lowered.startswith("replace "):
        return False
    matched_terms = {
        term
        for term in _MATERIAL_SWAP_TERMS
        if re.search(r"\b" + re.escape(term) + r"\b", lowered)
    }
    return len(matched_terms) >= 2


def _is_refusal(text: str) -> bool:
    if not text:
        return False
    lowered = text.strip().lower()
    return any(phrase in lowered for phrase in _REFUSAL_PHRASES)


def infer_instruction_edit_type(instruction_text: str) -> Optional[str]:
    lowered = _clean_text(instruction_text).lower()
    if lowered.startswith("remove "):
        return "remove"
    if lowered.startswith("replace "):
        return "replace"
    return None


def validate_instruction_text(
    instruction_text: str,
    *,
    expected_edit_type: Optional[str] = None,
) -> str:
    normalized = _clean_text(instruction_text)
    if not normalized:
        raise ValueError("instruction must be a non-empty string")

    inferred = infer_instruction_edit_type(normalized)
    if inferred not in {"remove", "replace"}:
        raise ValueError(
            "instruction must start with 'Remove' or 'Replace', "
            f"got: {normalized!r}"
        )
    if expected_edit_type and inferred != expected_edit_type:
        raise ValueError(
            f"instruction edit type mismatch: expected {expected_edit_type!r}, "
            f"got {inferred!r}"
        )
    if _contains_forbidden_lateral_terms(normalized):
        raise ValueError("instruction contains forbidden lateral terms")
    if _contains_ambiguous_symmetric_part(normalized):
        raise ValueError("instruction contains ambiguous symmetric-part edit")
    if _is_texture_edit(normalized):
        raise ValueError("instruction is a texture/color/material edit")
    if _is_refusal(normalized):
        raise ValueError("instruction is a refusal response")
    return normalized


def validate_instruction_legality(
    instruction_text: str,
    *,
    expected_edit_type: Optional[str] = None,
) -> str:
    normalized = validate_instruction_text(
        instruction_text,
        expected_edit_type=expected_edit_type,
    )
    if _is_main_body_edit(normalized):
        raise ValueError("instruction is a whole-object/main-body edit")
    if _is_appearance_only_edit(normalized):
        raise ValueError("instruction is an appearance-only/surface-only edit")
    if _is_material_swap_edit(normalized):
        raise ValueError("instruction is a material-swap edit")
    return normalized


def build_instruction_display_payload(
    *,
    instruction_text_original: str,
    instruction_text_effective: Optional[str] = None,
    instruction_text_candidate_rewrite: Optional[str] = None,
    stage1_status: Optional[str] = None,
    instruction_rewritten_by_stage1: Optional[bool] = None,
    instruction_rewrite_reason: Optional[str] = None,
    stage1_relabel_result: Optional[Dict[str, Any]] = None,
    instruction_display_source: Optional[str] = None,
    instruction_display_status: Optional[str] = None,
) -> Dict[str, Any]:
    original = _clean_text(instruction_text_original)
    candidate = _clean_text(instruction_text_candidate_rewrite)
    relabel_result = _normalize_dict(stage1_relabel_result)
    if not instruction_rewrite_reason and relabel_result:
        instruction_rewrite_reason = _clean_text(relabel_result.get("reason"))
    rewrite_reason = _clean_text(instruction_rewrite_reason)

    effective = _clean_text(instruction_text_effective) or original
    relabel_attempted = bool(relabel_result) or bool(candidate)
    if instruction_rewritten_by_stage1 is None:
        instruction_rewritten_by_stage1 = bool(
            candidate and effective and candidate == effective and candidate != original
        )
    stage1_status = _normalize_status(stage1_status)

    if not instruction_display_source:
        instruction_display_source = (
            "relabel" if instruction_rewritten_by_stage1 else "original"
        )

    if not instruction_display_status:
        if stage1_status is None:
            instruction_display_status = "legacy"
        elif instruction_rewritten_by_stage1 and stage1_status == EDIT_STATUS_PASSED:
            instruction_display_status = "relabel_passed"
        elif stage1_status == EDIT_STATUS_PASSED:
            instruction_display_status = "original_passed"
        elif relabel_attempted:
            instruction_display_status = "relabel_failed"
        elif stage1_status in {
            EDIT_STATUS_FAILED_QUALITY,
            EDIT_STATUS_ERROR_QUALITY_CHECK,
        }:
            instruction_display_status = "stage1_failed"
        else:
            instruction_display_status = "pending"

    return {
        "instruction_text_original": original,
        "instruction_text_candidate_rewrite": candidate or None,
        "instruction_text_effective": effective,
        "instruction_rewritten_by_stage1": bool(instruction_rewritten_by_stage1),
        "instruction_rewrite_reason": rewrite_reason or None,
        "stage1_relabel_result": relabel_result or None,
        "instruction_display_text": effective,
        "instruction_display_source": instruction_display_source,
        "instruction_display_status": instruction_display_status,
    }


def resolve_instruction_display_from_edit_meta(edit_meta: Dict[str, Any]) -> Dict[str, Any]:
    meta = _normalize_dict(edit_meta)
    quality_check = _normalize_dict(meta.get("quality_check"))
    detail = _normalize_dict(quality_check.get("stage_edit_correctness"))
    relabel_result = _normalize_dict(
        detail.get("relabel_result") or meta.get("stage1_relabel_result")
    )
    candidate = _first_clean_text(
        detail.get("candidate_rewrite_instruction"),
        relabel_result.get("instruction"),
        meta.get("instruction_text_candidate_rewrite"),
    )
    original = _first_clean_text(
        detail.get("original_instruction"),
        meta.get("instruction_text_original"),
        meta.get("instruction"),
    )
    effective = _first_clean_text(
        detail.get("effective_instruction"),
        meta.get("instruction_text_effective"),
        meta.get("instruction_display_text"),
        meta.get("instruction"),
    )
    rewrite_reason = _first_clean_text(
        detail.get("relabel_reason"),
        relabel_result.get("reason"),
        meta.get("instruction_rewrite_reason"),
    )
    stage1_status = (
        detail.get("status") or quality_check.get("status") or meta.get("edit_status")
    )
    instruction_rewritten_by_stage1 = (
        None if detail else meta.get("instruction_rewritten_by_stage1")
    )
    return build_instruction_display_payload(
        instruction_text_original=original,
        instruction_text_effective=effective,
        instruction_text_candidate_rewrite=candidate,
        stage1_status=stage1_status,
        instruction_rewritten_by_stage1=instruction_rewritten_by_stage1,
        instruction_rewrite_reason=rewrite_reason,
        stage1_relabel_result=relabel_result,
        instruction_display_source=(
            detail.get("instruction_display_source")
            or meta.get("instruction_display_source")
        ),
        instruction_display_status=(
            detail.get("instruction_display_status")
            or meta.get("instruction_display_status")
        ),
    )


def resolve_instruction_display_from_instruction_item(item: Any) -> Dict[str, Any]:
    if isinstance(item, str):
        return build_instruction_display_payload(
            instruction_text_original=item,
            instruction_text_effective=item,
            instruction_display_status="legacy",
        )
    payload = _normalize_dict(item)
    return build_instruction_display_payload(
        instruction_text_original=(
            payload.get("instruction_text_original")
            or payload.get("text")
            or payload.get("instruction")
            or ""
        ),
        instruction_text_effective=(
            payload.get("instruction_text_effective")
            or payload.get("instruction_display_text")
            or payload.get("text")
            or payload.get("instruction")
            or ""
        ),
        instruction_text_candidate_rewrite=payload.get(
            "instruction_text_candidate_rewrite"
        ),
        stage1_status=payload.get("stage1_status"),
        instruction_rewritten_by_stage1=payload.get("instruction_rewritten_by_stage1"),
        instruction_rewrite_reason=payload.get("instruction_rewrite_reason"),
        stage1_relabel_result=payload.get("stage1_relabel_result"),
        instruction_display_source=payload.get("instruction_display_source"),
        instruction_display_status=payload.get("instruction_display_status"),
    )


def resolve_instruction_display_from_record(record: Dict[str, Any]) -> Dict[str, Any]:
    payload = _normalize_dict(record)
    return build_instruction_display_payload(
        instruction_text_original=(
            payload.get("instruction_text_original")
            or payload.get("instruction_text")
            or ""
        ),
        instruction_text_effective=(
            payload.get("instruction_text_effective")
            or payload.get("instruction_display_text")
            or payload.get("instruction_text")
            or ""
        ),
        instruction_text_candidate_rewrite=payload.get(
            "instruction_text_candidate_rewrite"
        ),
        stage1_status=payload.get("stage1_status"),
        instruction_rewritten_by_stage1=payload.get("instruction_rewritten_by_stage1"),
        instruction_rewrite_reason=payload.get("instruction_rewrite_reason"),
        stage1_relabel_result=payload.get("stage1_relabel_result"),
        instruction_display_source=payload.get("instruction_display_source"),
        instruction_display_status=payload.get("instruction_display_status"),
    )
