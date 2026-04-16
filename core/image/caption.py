"""
Instruction Generator Module

Uses MLLM (Multimodal Large Language Model) to automatically generate 
editing instructions and captions for images.

Now uses unified LLM client from utils/llm_client.py.
Prompt templates are centralized in utils/prompts.py.

Supports:
- Single instruction generation (REMOVE or REPLACE)
- Batch instruction generation (1 REMOVE + 1 REPLACE for 1:1 ratio)
- Adaptive instruction generation (exactly k JSON instructions with type judgment)
"""

import base64
import json
from pathlib import Path
import re
from typing import Any, Optional, List, Dict

from utils.config import QnMllmConfig
from utils.llm_client import get_llm_client, OpenAICompatibleClient
from utils.prompts import (
    EditType,
    get_instruction_prompt,
    get_adaptive_instruction_prompt,
    get_batch_instruction_prompts,
    CAPTION_GENERATOR_PROMPT,
)
from core.image.instruction_display_resolver import validate_instruction_legality


INSTRUCTION_MULTIVIEW_EXTRA_CONSTRAINT = (
    "\n\nIMPORTANT (Multiview-safe): Do NOT use left/right lateral direction terms. "
    "Forbidden: left, right, front-left, front right, rear-left, rear right, "
    "port, starboard, 左, 右, 左侧, 右侧, 左边, 右边, 左前, 右前, 左后, 右后. "
    "If you need to specify location, prefer up/down (top/bottom) or a uniquely "
    "identifiable part name. Also avoid ambiguous 'one of many' edits on "
    "symmetric repeated parts (e.g., wheels). If wheels are mentioned, "
    "remove/replace ALL wheels."
)


class InstructionGenerator:
    """
    Client for generating editing instructions using MLLM.
    """
    
    def __init__(self, config: QnMllmConfig):
        """
        Initialize the instruction generator.
        
        Args:
            config: QnMllmConfig configuration with API key and settings
        """
        self.config = config
        self.client: OpenAICompatibleClient = get_llm_client(config)

    _FORBIDDEN_LATERAL_PATTERNS = [
        # English
        r"\bleft\b",
        r"\bright\b",
        r"\bfront\s*[- ]\s*left\b",
        r"\bfront\s*[- ]\s*right\b",
        r"\brear\s*[- ]\s*left\b",
        r"\brear\s*[- ]\s*right\b",
        r"\bport\b",
        r"\bstarboard\b",
        # Chinese (any occurrence is considered ambiguous in multiview edits)
        r"左",
        r"右",
    ]

    def _contains_forbidden_lateral_terms(self, text: str) -> bool:
        if not text:
            return False
        t = text.strip().lower()
        for pat in self._FORBIDDEN_LATERAL_PATTERNS:
            if re.search(pat, t, flags=re.IGNORECASE):
                return True
        return False

    _AMBIGUOUS_WHEEL_PATTERNS = [
        # English: singular selection of a wheel is ambiguous in multiview edits
        r"\bremove\s+(a|an|one)\s+\w*\s*wheel\b",
        r"\breplace\s+(a|an|one)\s+\w*\s*wheel\b",
        r"\bremove\s+(a|an|one)\s+wheel\b",
        r"\breplace\s+(a|an|one)\s+wheel\b",
        # Chinese: "一个/一只" + 轮
        r"删除.*一个.*轮",
        r"移除.*一个.*轮",
        r"删(除|掉).*一个.*轮",
        r"删(除|掉).*轮.*(左|右)?(前|后)?侧?\s*一个",
        r"替换\s*一个.*轮",
        r"更换\s*一个.*轮",
    ]

    _TEXTURE_KEYWORDS = [
        # 颜色/材质操作名词（作为操作对象出现，通常意味着在操作外观本身）
        "color",
        "colour",
        "texture",
        "material",
        "finish",
        "pattern",
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

    _REFUSAL_PHRASES = [
        "i cannot", "i can't", "i am unable", "sorry", "i apologize", "as an ai",
        "cannot assist", "cannot help", "against my policy", "unable to generate"
    ]

    def _is_texture_edit(self, text: str) -> bool:
        if not text: return False
        t = text.strip().lower()
        for kw in self._TEXTURE_KEYWORDS:
            if re.search(r"\b" + re.escape(kw) + r"\b", t):
                return True
        return False

    def _is_refusal(self, text: str) -> bool:
        if not text: return False
        t = text.strip().lower()
        for phrase in self._REFUSAL_PHRASES:
            if phrase in t:
                return True
        return False

    def _contains_ambiguous_symmetric_part(self, text: str) -> bool:
        if not text:
            return False
        t = text.strip().lower()
        for pat in self._AMBIGUOUS_WHEEL_PATTERNS:
            if re.search(pat, t, flags=re.IGNORECASE):
                return True
        return False

    def _debug_instruction_excerpt(self, instruction_type: str, instruction_text: str) -> str:
        normalized_type = (instruction_type or "").strip()
        normalized_text = (instruction_text or "").strip()
        if len(normalized_text) > 240:
            normalized_text = normalized_text[:240] + "...<truncated>"
        return f"type={normalized_type!r}, instruction={normalized_text!r}"

    def _encode_image_data(self, image_path: str) -> Dict[str, str]:
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        with open(path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        suffix = path.suffix.lower()
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix, "image/png")

        return {"data": img_b64, "media_type": media_type}

    def _generate_validated_instruction(
        self,
        *,
        image_path: str,
        edit_type: EditType,
        avoid_list: Optional[List[str]] = None,
        prompt: Optional[str] = None,
        max_attempts: int = 3,
        image_data: Optional[Dict[str, str]] = None,
    ) -> str:
        local_avoid = list(avoid_list) if avoid_list else []
        base_prompt = prompt

        last = ""
        # Use pre-encoded data if available
        images_input = [image_data] if image_data else [image_path]

        for attempt in range(1, max_attempts + 1):
            if base_prompt:
                p = base_prompt
            else:
                p = get_instruction_prompt(edit_type, local_avoid)
            p = p + INSTRUCTION_MULTIVIEW_EXTRA_CONSTRAINT

            instr = self.client.chat_with_images(
                system_prompt="",
                user_prompt=p,
                images=images_input,
            )
            instr = (instr or "").strip()
            last = instr

            try:
                return validate_instruction_legality(
                    instr,
                    expected_edit_type=edit_type.value,
                )
            except ValueError:
                pass

            # Add the bad output to avoid list and try again
            if instr:
                local_avoid.append(instr)

        # Final fallback: rewrite the last instruction to remove forbidden terms / ambiguity
        if last:
            try:
                return validate_instruction_legality(
                    last,
                    expected_edit_type=edit_type.value,
                )
            except ValueError:
                rewrite_prompt = (
                    "Rewrite this edit instruction to remove any left/right lateral direction terms and remove ambiguity for multiview editing. "
                    "Ensure it is NOT a texture/color/material change but a geometric change (remove/replace parts). "
                    "Do not edit the entire object or the main body. "
                    "Do not target logos, emblems, labels, seam lines, or material swaps. "
                    "while keeping it a single clear sentence starting with 'Remove' or 'Replace'. "
                    "Do not introduce left/right terms. Up/down is allowed. "
                    "If the instruction mentions wheels, make it remove/replace ALL wheels (not a single wheel).\n\n"
                    f"Instruction: {last}"
                )
                rewritten = (self.client.chat(system_prompt="", user_prompt=rewrite_prompt) or "").strip()
                return validate_instruction_legality(
                    rewritten,
                    expected_edit_type=edit_type.value,
                )

        raise ValueError(
            f"failed to generate a legal instruction after {max_attempts} attempts"
        )

    def generate_instruction(
        self,
        image_path: str,
        edit_type: EditType = EditType.REMOVE,
        prompt: Optional[str] = None,
        avoid_list: Optional[List[str]] = None
    ) -> str:
        """
        Generate a single editing instruction for the given image.

        Args:
            image_path: Path to the source image
            edit_type: Type of edit (REMOVE or REPLACE)
            prompt: Optional custom prompt to override default
            avoid_list: List of instruction strings to avoid (to ensure variety)

        Returns:
            Generated instruction string
        """
        try:
            return self._generate_validated_instruction(
                image_path=image_path,
                edit_type=edit_type,
                avoid_list=avoid_list,
                prompt=prompt,
            )
        except Exception as e:
            raise Exception(f"Failed to generate instruction: {str(e)}")

    def generate_batch_instructions(
        self,
        image_path: str,
        avoid_list: Optional[List[str]] = None
    ) -> List[Dict[str, str]]:
        """
        Generate a batch of instructions (1 REMOVE + 1 REPLACE).

        This ensures a 1:1 ratio of remove:replace instructions.

        Args:
            image_path: Path to the source image
            avoid_list: List of instruction strings to avoid

        Returns:
            List of dicts with 'type' and 'instruction' keys
        """
        image_data = self._encode_image_data(image_path)

        results = []
        # Create a copy of avoid_list to prevent mutating the caller's list
        local_avoid_list = list(avoid_list) if avoid_list else []
        prompts = get_batch_instruction_prompts(local_avoid_list)

        for edit_type, prompt in prompts:
            try:
                instruction = self._generate_validated_instruction(
                    image_path=image_path,  # kept for logging if needed
                    image_data=image_data,  # Pass pre-encoded data
                    edit_type=edit_type,
                    avoid_list=local_avoid_list,
                    prompt=prompt,
                )
                results.append({
                    "type": edit_type.value,
                    "instruction": instruction.strip()
                })
                # Add to local avoid list for next iteration to prevent similar ideas
                local_avoid_list.append(instruction.strip())
            except Exception as e:
                print(f"Warning: Failed to generate {edit_type.value} instruction: {e}")

        return results

    def _parse_adaptive_instruction_response(self, response_text: str) -> Any:
        raw_text = (response_text or "").strip()
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            cleaned_text = self._clean_adaptive_instruction_response(raw_text)
            if cleaned_text != raw_text:
                try:
                    return json.loads(cleaned_text)
                except json.JSONDecodeError:
                    pass

            preview = raw_text
            if len(preview) > 400:
                preview = preview[:400] + "...<truncated>"
            raise ValueError(
                "Adaptive instruction response is not valid JSON: "
                f"{exc}; raw_response={preview!r}"
            ) from exc

    def _clean_adaptive_instruction_response(self, response_text: str) -> str:
        text = (response_text or "").strip()
        if not text:
            return text

        fenced_match = re.match(
            r"^```(?:json)?\s*(.*?)\s*```$",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced_match:
            text = fenced_match.group(1).strip()

        if text.lower().startswith("json\n"):
            text = text.split("\n", 1)[1].strip()

        extracted = self._extract_first_json_object(text)
        if extracted:
            return extracted
        return text

    def _extract_first_json_object(self, text: str) -> Optional[str]:
        start = text.find("{")
        if start < 0:
            return None

        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]

            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]

        return None

    def _validate_adaptive_instruction_payload(
        self,
        payload: Any,
        *,
        count: int,
        allowed_types: List[str],
        avoid_list: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Adaptive instruction payload must be a JSON object")

        expected_top_level_keys = {"type_judgment", "instructions"}
        actual_top_level_keys = set(payload.keys())
        if actual_top_level_keys != expected_top_level_keys:
            raise ValueError(
                "Adaptive instruction payload must contain exactly "
                f"{sorted(expected_top_level_keys)}, got {sorted(actual_top_level_keys)}"
            )

        type_judgment = payload.get("type_judgment")
        if not isinstance(type_judgment, dict):
            raise ValueError("Adaptive instruction payload.type_judgment must be an object")
        expected_judgment_keys = {"allowed_types_used", "preferred_types", "reason_short"}
        actual_judgment_keys = set(type_judgment.keys())
        if actual_judgment_keys != expected_judgment_keys:
            raise ValueError(
                "Adaptive instruction payload.type_judgment must contain exactly "
                f"{sorted(expected_judgment_keys)}, got {sorted(actual_judgment_keys)}"
            )

        reason_short = type_judgment.get("reason_short")
        if not isinstance(reason_short, str) or not reason_short.strip():
            raise ValueError("Adaptive instruction payload.type_judgment.reason_short must be non-empty")

        normalized_type_judgment = {
            "allowed_types_used": [],
            "preferred_types": [],
            "reason_short": reason_short.strip(),
        }
        for key in ("allowed_types_used", "preferred_types"):
            values = type_judgment.get(key)
            if not isinstance(values, list):
                raise ValueError(f"Adaptive instruction payload.type_judgment.{key} must be a list")
            normalized_values = []
            for index, raw_value in enumerate(values):
                if not isinstance(raw_value, str) or not raw_value.strip():
                    raise ValueError(
                        f"Adaptive instruction payload.type_judgment.{key}[{index}] must be a non-empty string"
                    )
                value = raw_value.strip()
                if value not in allowed_types:
                    raise ValueError(
                        f"Adaptive instruction payload.type_judgment.{key}[{index}] must be one of {allowed_types}"
                    )
                if value in normalized_values:
                    raise ValueError(
                        f"Adaptive instruction payload.type_judgment.{key} contains duplicate value {value!r}"
                    )
                normalized_values.append(value)
            normalized_type_judgment[key] = normalized_values

        instructions = payload.get("instructions")
        if not isinstance(instructions, list):
            raise ValueError("Adaptive instruction payload.instructions must be a list")
        if len(instructions) != count:
            raise ValueError(
                f"Adaptive instruction payload.instructions must contain exactly {count} items"
            )

        normalized_instructions: List[Dict[str, str]] = []
        seen_instructions = set()
        normalized_avoid = {
            instruction.strip().lower()
            for instruction in (avoid_list or [])
            if isinstance(instruction, str) and instruction.strip()
        }
        for index, item in enumerate(instructions):
            if not isinstance(item, dict):
                raise ValueError(f"Adaptive instruction payload.instructions[{index}] must be an object")
            expected_item_keys = {"type", "instruction"}
            actual_item_keys = set(item.keys())
            if actual_item_keys != expected_item_keys:
                raise ValueError(
                    f"Adaptive instruction payload.instructions[{index}] must contain exactly "
                    f"{sorted(expected_item_keys)}, got {sorted(actual_item_keys)}"
                )

            instruction_type = item.get("type")
            if not isinstance(instruction_type, str) or not instruction_type.strip():
                raise ValueError(f"Adaptive instruction payload.instructions[{index}].type must be non-empty")
            instruction_type = instruction_type.strip()
            if instruction_type not in allowed_types:
                raise ValueError(
                    f"Adaptive instruction payload.instructions[{index}].type must be one of {allowed_types}"
                )

            instruction_text = item.get("instruction")
            if not isinstance(instruction_text, str) or not instruction_text.strip():
                raise ValueError(
                    f"Adaptive instruction payload.instructions[{index}].instruction must be non-empty"
                )
            instruction_text = instruction_text.strip()
            debug_excerpt = self._debug_instruction_excerpt(
                instruction_type,
                instruction_text,
            )

            try:
                instruction_text = validate_instruction_legality(
                    instruction_text,
                    expected_edit_type=instruction_type,
                )
            except ValueError as exc:
                raise ValueError(
                    "Adaptive instruction payload.instructions["
                    f"{index}] is illegal: {debug_excerpt}; reason={exc}"
                ) from exc
            if instruction_text.lower() in normalized_avoid:
                raise ValueError(
                    "Adaptive instruction payload.instructions["
                    f"{index}] repeats an avoided instruction: {debug_excerpt}"
                )

            dedupe_key = (instruction_type, instruction_text.lower())
            if dedupe_key in seen_instructions:
                raise ValueError(
                    "Adaptive instruction payload.instructions contains duplicate instruction: "
                    f"{debug_excerpt}"
                )
            seen_instructions.add(dedupe_key)
            normalized_instructions.append(
                {
                    "type": instruction_type,
                    "instruction": instruction_text,
                }
            )

        return {
            "type_judgment": normalized_type_judgment,
            "instructions": normalized_instructions,
        }

    def generate_adaptive_instructions(
        self,
        image_path: str,
        count: int,
        allowed_types: List[str],
        avoid_list: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(count, int) or count <= 0:
            raise ValueError("count must be a positive integer")
        if not isinstance(allowed_types, list) or not allowed_types:
            raise ValueError("allowed_types must be a non-empty list")
        normalized_allowed_types: List[str] = []
        for index, raw_type in enumerate(allowed_types):
            if not isinstance(raw_type, str) or not raw_type.strip():
                raise ValueError(f"allowed_types[{index}] must be a non-empty string")
            value = raw_type.strip()
            if value not in {EditType.REMOVE.value, EditType.REPLACE.value}:
                raise ValueError(
                    f"allowed_types[{index}] must be one of {[EditType.REMOVE.value, EditType.REPLACE.value]}"
                )
            if value in normalized_allowed_types:
                raise ValueError(f"allowed_types contains duplicate value {value!r}")
            normalized_allowed_types.append(value)

        prompt = get_adaptive_instruction_prompt(
            count=count,
            allowed_types=normalized_allowed_types,
            avoid_list=avoid_list,
        )
        response_text = self.client.chat_with_images(
            system_prompt="",
            user_prompt=prompt + INSTRUCTION_MULTIVIEW_EXTRA_CONSTRAINT,
            images=[self._encode_image_data(image_path)],
        )
        payload = self._parse_adaptive_instruction_response((response_text or "").strip())
        return self._validate_adaptive_instruction_payload(
            payload,
            count=count,
            allowed_types=normalized_allowed_types,
            avoid_list=avoid_list,
        )

    def generate_caption(self, image_path: str) -> str:
        """
        Generate a descriptive caption for the image.
        
        Args:
            image_path: Path to the source image
            
        Returns:
            Generated caption string
        """
        try:
            return self.client.chat_with_images(
                system_prompt="",
                user_prompt=CAPTION_GENERATOR_PROMPT,
                images=[image_path],
            )
        except Exception as e:
            raise Exception(f"Failed to generate caption: {str(e)}")
            
    def close(self):
        """Close the client."""
        self.client.close()
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
