from __future__ import annotations

from typing import Any, Dict, List


VALID_INSTRUCTION_TYPES = ("remove", "replace")
INSTRUCTION_PLAN_MODE_ADAPTIVE_K = "adaptive_k"


def require_mapping(data: Any, path: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a mapping")
    return data


def require_list(data: Any, path: str) -> List[Any]:
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a list")
    return data


def require_non_empty_str(data: Any, path: str) -> str:
    if not isinstance(data, str) or not data.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return data.strip()


def require_positive_int(data: Any, path: str) -> int:
    if not isinstance(data, int) or data <= 0:
        raise ValueError(f"{path} must be a positive integer")
    return data


def require_non_negative_int(data: Any, path: str) -> int:
    if not isinstance(data, int) or data < 0:
        raise ValueError(f"{path} must be a non-negative integer")
    return data


def build_instruction_plan(
    *,
    count: int,
    allowed_types: List[str],
) -> Dict[str, Any]:
    normalized_types = normalize_allowed_instruction_types(
        allowed_types,
        "instruction_plan.allowed_types",
    )
    return {
        "mode": INSTRUCTION_PLAN_MODE_ADAPTIVE_K,
        "count": require_positive_int(count, "instruction_plan.count"),
        "allowed_types": normalized_types,
    }


def normalize_allowed_instruction_types(data: Any, path: str) -> List[str]:
    allowed_types: List[str] = []
    for index, raw_value in enumerate(require_list(data, path)):
        value = require_non_empty_str(raw_value, f"{path}[{index}]")
        if value not in VALID_INSTRUCTION_TYPES:
            raise ValueError(
                f"{path}[{index}] must be one of {list(VALID_INSTRUCTION_TYPES)}"
            )
        if value in allowed_types:
            raise ValueError(f"{path} contains duplicate value {value!r}")
        allowed_types.append(value)
    if not allowed_types:
        raise ValueError(f"{path} must contain at least one instruction type")
    return allowed_types


def instruction_counts_to_plan(data: Any, path: str) -> Dict[str, Any]:
    counts = require_mapping(data, path)
    for key in counts.keys():
        if key not in VALID_INSTRUCTION_TYPES:
            raise ValueError(
                f"{path} has invalid key {key!r}; expected one of {list(VALID_INSTRUCTION_TYPES)}"
            )

    parsed_counts: Dict[str, int] = {}
    for key in VALID_INSTRUCTION_TYPES:
        parsed_counts[key] = require_non_negative_int(counts.get(key, 0), f"{path}.{key}")

    count = sum(parsed_counts.values())
    if count <= 0:
        raise ValueError(f"{path} must contain at least one positive count")

    allowed_types = [
        instruction_type
        for instruction_type in VALID_INSTRUCTION_TYPES
        if parsed_counts[instruction_type] > 0
    ]
    return {
        "mode": INSTRUCTION_PLAN_MODE_ADAPTIVE_K,
        "count": count,
        "allowed_types": allowed_types,
    }


def normalize_instruction_plan_from_category(
    category: Dict[str, Any],
    path: str,
    *,
    allow_legacy_counts: bool,
) -> Dict[str, Any]:
    has_instruction_plan = "instruction_plan" in category
    has_instruction_counts = "instruction_counts" in category

    if has_instruction_plan and has_instruction_counts:
        raise ValueError(
            f"{path} cannot contain both instruction_plan and legacy instruction_counts"
        )

    if has_instruction_plan:
        instruction_plan = require_mapping(
            category.get("instruction_plan"),
            f"{path}.instruction_plan",
        )
        mode = require_non_empty_str(
            instruction_plan.get("mode"),
            f"{path}.instruction_plan.mode",
        )
        if mode != INSTRUCTION_PLAN_MODE_ADAPTIVE_K:
            raise ValueError(
                f"{path}.instruction_plan.mode must be {INSTRUCTION_PLAN_MODE_ADAPTIVE_K!r}"
            )
        return {
            "mode": mode,
            "count": require_positive_int(
                instruction_plan.get("count"),
                f"{path}.instruction_plan.count",
            ),
            "allowed_types": normalize_allowed_instruction_types(
                instruction_plan.get("allowed_types"),
                f"{path}.instruction_plan.allowed_types",
            ),
        }

    if has_instruction_counts:
        if not allow_legacy_counts:
            raise ValueError(
                f"{path}.instruction_counts is deprecated; use {path}.instruction_plan"
            )
        return instruction_counts_to_plan(
            category.get("instruction_counts"),
            f"{path}.instruction_counts",
        )

    raise ValueError(f"{path}.instruction_plan is required")
