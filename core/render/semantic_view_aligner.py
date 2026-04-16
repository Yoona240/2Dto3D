"""
Semantic view alignment for render pipeline.

Pipeline:
1) Analyze first-pass rendered views with VLM.
2) Compute rigid rotation matrix to map semantic front to canonical front, with
   an additional upright stabilization roll.
3) Optionally verify final rerendered views.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from config.config import QnMllmConfig
from core.image.view_stitcher import ViewStitcher
from utils.llm_client import get_llm_client


CANONICAL_VIEWS = ("front", "back", "left", "right", "top", "bottom")

VIEW_NORMALS: Dict[str, Tuple[int, int, int]] = {
    "front": (0, -1, 0),
    "back": (0, 1, 0),
    "left": (1, 0, 0),
    "right": (-1, 0, 0),
    "top": (0, 0, 1),
    "bottom": (0, 0, -1),
}

@dataclass
class SemanticDecision:
    semantic_front_from: str
    confidence: float
    reason: str


class SemanticViewAligner:
    """Analyze rendered views and compute semantic alignment rotation matrix."""

    def __init__(self, config, render_config):
        self._config = config
        self._render_config = render_config
        self._settings = render_config.semantic_alignment

    def ensure_required_views(self, views_dir: Path) -> None:
        missing = [name for name in CANONICAL_VIEWS if not (views_dir / f"{name}.png").exists()]
        if missing:
            raise FileNotFoundError(
                f"Missing required rendered views in {views_dir}: {missing}"
            )

    def analyze_views(self, views_dir: Path, debug_dir: Path) -> SemanticDecision:
        """Run VLM on stitched six-view image and return validated decision."""
        views_dir = Path(views_dir)
        debug_dir = Path(debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_required_views(views_dir)

        stitched_path = debug_dir / "stitched_views.png"
        print(
            f"[SemanticAlign][Decision] Stitching views from {views_dir}",
            flush=True,
        )
        stitcher = ViewStitcher()
        stitcher.stitch_views(
            views_dir=views_dir,
            output_path=stitched_path,
            view_names=list(CANONICAL_VIEWS),
            pad_to_square=True,
        )

        model_name = self._settings.vlm_model
        text_model = self._config.oneapi.text_models[model_name]
        base_url = text_model.base_url or self._config.oneapi.base_url
        client_config = QnMllmConfig(
            api_key=self._config.oneapi.api_key,
            base_url=f"{base_url.rstrip('/')}/v1",
            default_model=model_name,
            temperature=float(text_model.temperature),
            max_tokens=int(text_model.max_tokens),
            timeout=int(self._config.oneapi.timeout),
            max_retries=int(self._config.oneapi.max_retries),
        )

        system_prompt = (
            "You are a strict 3D orientation judge. "
            "Return JSON only. Do not output markdown."
        )
        user_prompt = (
            "Input image 1 is a stitched 3x2 grid with fixed labels/order:\n"
            "Row1: front, back, right\n"
            "Row2: left, top, bottom\n\n"
            "## Two core principles (in priority order)\n\n"
            "P1 — Functional facing (highest priority):\n"
            "  If the object has a clear 'business direction' (aims/points/faces toward "
            "the user during normal use), that direction is FRONT.\n"
            "P2 — Maximum visibility:\n"
            "  Otherwise, FRONT should maximize the visible surface area and show the "
            "most characteristic details of the object.\n"
            "When P1 and P2 conflict, P1 wins. For example a handgun's muzzle view has "
            "small area but is still FRONT because it is the aiming direction.\n\n"
            "## Shape-specific rules with examples\n\n"
            "1) Elongated objects (violin, syringe, sword, spear, flagpole, pen, rifle):\n"
            "   - FRONT = the broadside that shows the largest silhouette and key features.\n"
            "   - The narrow end-on view (looking down the length axis) is NEVER front.\n"
            "   - If there is a characteristic display face, pick that face.\n"
            "   - Example (violin): the flat side with strings facing camera is FRONT.\n"
            "   - Example (syringe): the side view showing full barrel + plunger + needle "
            "is FRONT, NOT the tiny circular end.\n\n"
            "2) Directional / pointed objects (gun, camera, flashlight, telescope, cannon):\n"
            "   - FRONT = the direction the object points at / aims at the viewer.\n"
            "   - Example (handgun): muzzle facing camera (as if the gun points at you) "
            "is FRONT.\n"
            "   - Example (camera): lens facing camera is FRONT.\n\n"
            "3) Flat / disc / container objects (cake, pizza, plate, cup, vase, bowl, "
            "bucket, bottle):\n"
            "   - Imagine the object placed naturally on a table.\n"
            "   - TOP = bird's-eye view looking straight down (sees the full circular "
            "face / opening).\n"
            "   - FRONT = side view showing the full profile, thickness/layers, or "
            "label/decoration.\n"
            "   - Do NOT mistake the bird's-eye circular face or the opening for FRONT.\n"
            "   - Example (round cake): TOP shows the full decorated circle; FRONT shows "
            "the layered cross-section from the side.\n"
            "   - Example (cup/vase): TOP looks into the opening; FRONT shows the side "
            "profile with handle or decoration.\n\n"
            "4) Objects with a clear user-facing side (faucet, ATM, vending machine, "
            "TV, monitor):\n"
            "   - FRONT = the side a person faces during normal use.\n"
            "   - Example (water faucet): the side you see while washing hands is FRONT; "
            "the water outlet faces down (BOTTOM).\n"
            "   - Example (vending machine): the panel with buttons/display is FRONT.\n\n"
            "5) Vehicles (car, bus, bicycle, boat):\n"
            "   - FRONT = headlight/grille side (the direction of travel).\n"
            "   - Example (car): headlight/grille side is FRONT, not the roof (TOP).\n\n"
            "6) Seating / furniture with a user-facing side (chair, sofa, toilet):\n"
            "   - FRONT = the side people face when sitting.\n\n"
            "7) Symmetric objects with no clear direction (ball, cube, sphere):\n"
            "   - If there is any distinguishing feature (logo, seam, pattern), pick the "
            "view showing it.\n"
            "   - Otherwise the current 'front' label is acceptable; use high confidence.\n\n"
            "## Task\n\n"
            "1) Identify the object type and pick the most applicable shape rule above.\n"
            "2) Determine which original labeled view best matches human-perceived FRONT.\n"
            "3) Return confidence [0,1].\n"
            "4) Briefly explain: which shape rule you applied, why the chosen view is "
            "FRONT, and why alternatives are less suitable.\n\n"
            "Output JSON schema:\n"
            '{'
            '"semantic_front_from":"front|back|left|right|top|bottom",'
            '"confidence":0.0,'
            '"reason":"short explanation"'
            '}'
        )

        image_inputs: List[Path] = [stitched_path]

        print(
            "[SemanticAlign][Decision] Requesting VLM "
            f"model={model_name} image={stitched_path}",
            flush=True,
        )
        client = get_llm_client(client_config)
        try:
            response = client.chat_with_images(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                images=image_inputs,
                temperature=float(text_model.temperature),
                max_tokens=int(text_model.max_tokens),
                log_dir=debug_dir / "vlm_logs",
            )
        finally:
            client.close()

        print("[SemanticAlign][Decision] VLM response received", flush=True)
        parsed = self._safe_parse_vlm_json(response)
        decision = self._validate_decision(parsed)
        with open(debug_dir / "decision.json", "w", encoding="utf-8") as f:
            json.dump(asdict(decision), f, ensure_ascii=False, indent=2)
        print(
            "[SemanticAlign][Decision] Decision persisted "
            f"semantic_front_from={decision.semantic_front_from} "
            f"confidence={decision.confidence:.3f}",
            flush=True,
        )
        return decision

    def verify_final_views(self, final_views_dir: Path, debug_dir: Path) -> bool:
        """Re-run VLM on final views and ensure canonical front orientation."""
        print(
            f"[SemanticAlign][Verify] Verifying final views from {final_views_dir}",
            flush=True,
        )
        decision = self.analyze_views(final_views_dir, debug_dir)
        return (
            decision.semantic_front_from == "front"
            and decision.confidence >= float(self._settings.min_confidence)
        )

    def compute_rotation_matrix(self, decision: SemanticDecision) -> List[List[float]]:
        """Map semantic front to canonical front, then stabilize roll to keep upright."""
        src_front = VIEW_NORMALS[decision.semantic_front_from]
        dst_front = VIEW_NORMALS["front"]
        rotation_front = self._rotation_from_vector_to_vector(src_front, dst_front)

        # Front-only is underconstrained in roll; stabilize by aligning transformed
        # source-top direction toward canonical top around the front axis.
        src_up_hint = VIEW_NORMALS["top"]
        if abs(self._dot(src_front, src_up_hint)) > 0.999999:
            src_up_hint = VIEW_NORMALS["back"]

        transformed_up_hint = self._matvec(rotation_front, src_up_hint)
        dst_up = VIEW_NORMALS["top"]
        axis = self._normalize((float(dst_front[0]), float(dst_front[1]), float(dst_front[2])))
        from_proj = self._project_onto_plane(transformed_up_hint, axis)
        to_proj = self._project_onto_plane(
            (float(dst_up[0]), float(dst_up[1]), float(dst_up[2])), axis
        )

        if self._norm(from_proj) > 1e-8 and self._norm(to_proj) > 1e-8:
            from_unit = self._normalize(from_proj)
            to_unit = self._normalize(to_proj)
            roll_angle = self._signed_angle_about_axis(from_unit, to_unit, axis)
            rotation_roll = self._axis_angle_to_matrix(axis, roll_angle)
            rotation = self._matmul(rotation_roll, rotation_front)
        else:
            rotation = rotation_front

        self._validate_rotation_matrix(rotation)
        return rotation

    def _validate_decision(self, data: dict) -> SemanticDecision:
        if not isinstance(data, dict):
            raise ValueError(f"VLM response must be a JSON object, got: {type(data)}")

        required_fields = (
            "semantic_front_from",
            "confidence",
            "reason",
        )
        for field in required_fields:
            if field not in data:
                raise ValueError(f"VLM response missing required field: {field}")

        front = data["semantic_front_from"]
        confidence = data["confidence"]
        reason = data["reason"]

        if front not in CANONICAL_VIEWS:
            raise ValueError(f"Invalid semantic_front_from: {front}")
        if not isinstance(confidence, (int, float)):
            raise ValueError(f"confidence must be number, got: {type(confidence)}")
        if not isinstance(reason, str):
            raise ValueError(f"reason must be string, got: {type(reason)}")

        confidence_value = float(confidence)
        if confidence_value < float(self._settings.min_confidence):
            raise ValueError(
                f"VLM confidence too low: {confidence_value} < {self._settings.min_confidence}"
            )
        if confidence_value > 1.0:
            raise ValueError(f"VLM confidence must be <= 1.0, got: {confidence_value}")

        return SemanticDecision(
            semantic_front_from=front,
            confidence=confidence_value,
            reason=reason.strip(),
        )

    @staticmethod
    def _safe_parse_vlm_json(text: str) -> dict:
        if not text:
            raise ValueError("VLM returned empty response")

        payload = text.strip()
        if payload.startswith("```"):
            payload = re.sub(r"^```(?:json)?", "", payload, flags=re.IGNORECASE).strip()
            payload = re.sub(r"```$", "", payload).strip()

        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", payload, flags=re.DOTALL)
        if not match:
            raise ValueError(f"VLM response contains no JSON object: {payload[:200]}")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse VLM JSON: {match.group(0)[:200]}") from exc

    @staticmethod
    def _cross(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> Tuple[int, int, int]:
        return (
            a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0],
        )

    @staticmethod
    def _matrix_from_columns(
        c0: Tuple[int, int, int],
        c1: Tuple[int, int, int],
        c2: Tuple[int, int, int],
    ) -> List[List[float]]:
        return [
            [float(c0[0]), float(c1[0]), float(c2[0])],
            [float(c0[1]), float(c1[1]), float(c2[1])],
            [float(c0[2]), float(c1[2]), float(c2[2])],
        ]

    @staticmethod
    def _transpose(m: List[List[float]]) -> List[List[float]]:
        return [[m[r][c] for r in range(3)] for c in range(3)]

    @staticmethod
    def _matmul(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
        result = [[0.0, 0.0, 0.0] for _ in range(3)]
        for row in range(3):
            for col in range(3):
                result[row][col] = (
                    a[row][0] * b[0][col]
                    + a[row][1] * b[1][col]
                    + a[row][2] * b[2][col]
                )
        return result

    @staticmethod
    def _determinant_3x3(m: List[List[float]]) -> float:
        return (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )

    @staticmethod
    def _dot(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> float:
        return float(a[0] * b[0] + a[1] * b[1] + a[2] * b[2])

    @staticmethod
    def _norm(v: Tuple[float, float, float]) -> float:
        return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])

    @staticmethod
    def _normalize(v: Tuple[float, float, float]) -> Tuple[float, float, float]:
        norm = SemanticViewAligner._norm(v)
        if norm <= 1e-12:
            raise ValueError(f"Cannot normalize zero vector: {v}")
        return (v[0] / norm, v[1] / norm, v[2] / norm)

    @staticmethod
    def _axis_angle_to_matrix(
        axis: Tuple[float, float, float],
        angle: float,
    ) -> List[List[float]]:
        x, y, z = axis
        c = math.cos(angle)
        s = math.sin(angle)
        one_minus_c = 1.0 - c
        return [
            [
                c + x * x * one_minus_c,
                x * y * one_minus_c - z * s,
                x * z * one_minus_c + y * s,
            ],
            [
                y * x * one_minus_c + z * s,
                c + y * y * one_minus_c,
                y * z * one_minus_c - x * s,
            ],
            [
                z * x * one_minus_c - y * s,
                z * y * one_minus_c + x * s,
                c + z * z * one_minus_c,
            ],
        ]

    @staticmethod
    def _matvec(
        m: List[List[float]], v: Tuple[int, int, int] | Tuple[float, float, float]
    ) -> Tuple[float, float, float]:
        return (
            m[0][0] * float(v[0]) + m[0][1] * float(v[1]) + m[0][2] * float(v[2]),
            m[1][0] * float(v[0]) + m[1][1] * float(v[1]) + m[1][2] * float(v[2]),
            m[2][0] * float(v[0]) + m[2][1] * float(v[1]) + m[2][2] * float(v[2]),
        )

    @staticmethod
    def _project_onto_plane(
        v: Tuple[float, float, float], normal: Tuple[float, float, float]
    ) -> Tuple[float, float, float]:
        # normal is expected normalized
        dot_vn = v[0] * normal[0] + v[1] * normal[1] + v[2] * normal[2]
        return (
            v[0] - dot_vn * normal[0],
            v[1] - dot_vn * normal[1],
            v[2] - dot_vn * normal[2],
        )

    @staticmethod
    def _signed_angle_about_axis(
        v_from: Tuple[float, float, float],
        v_to: Tuple[float, float, float],
        axis: Tuple[float, float, float],
    ) -> float:
        cross_ft = (
            v_from[1] * v_to[2] - v_from[2] * v_to[1],
            v_from[2] * v_to[0] - v_from[0] * v_to[2],
            v_from[0] * v_to[1] - v_from[1] * v_to[0],
        )
        sin_val = (
            axis[0] * cross_ft[0] + axis[1] * cross_ft[1] + axis[2] * cross_ft[2]
        )
        cos_val = (
            v_from[0] * v_to[0] + v_from[1] * v_to[1] + v_from[2] * v_to[2]
        )
        return math.atan2(sin_val, cos_val)

    def _rotation_from_vector_to_vector(
        self,
        src: Tuple[int, int, int],
        dst: Tuple[int, int, int],
    ) -> List[List[float]]:
        """Minimal rigid rotation mapping src direction to dst direction."""
        dot_val = self._dot(src, dst)
        if dot_val > 0.999999:
            return [
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ]

        if dot_val < -0.999999:
            # 180-degree rotation: choose a deterministic axis orthogonal to src.
            fallback_axis = (0.0, 0.0, 1.0)
            if abs(float(src[2])) > 0.9:
                fallback_axis = (1.0, 0.0, 0.0)
            axis_raw = self._cross(
                (float(src[0]), float(src[1]), float(src[2])),
                fallback_axis,
            )
            axis = self._normalize(axis_raw)
            return self._axis_angle_to_matrix(axis, math.pi)

        axis_raw = self._cross(src, dst)
        axis = self._normalize((float(axis_raw[0]), float(axis_raw[1]), float(axis_raw[2])))
        angle = math.acos(max(-1.0, min(1.0, dot_val)))
        return self._axis_angle_to_matrix(axis, angle)

    def _validate_rotation_matrix(self, m: List[List[float]]) -> None:
        tolerance = 1e-6
        mt_m = self._matmul(self._transpose(m), m)
        for row in range(3):
            for col in range(3):
                expected = 1.0 if row == col else 0.0
                if abs(mt_m[row][col] - expected) > tolerance:
                    raise ValueError(
                        "Computed rotation matrix is not orthogonal: "
                        f"mt_m[{row}][{col}]={mt_m[row][col]}"
                    )

        det = self._determinant_3x3(m)
        if abs(det - 1.0) > tolerance:
            raise ValueError(f"Computed rotation matrix determinant must be 1, got: {det}")
