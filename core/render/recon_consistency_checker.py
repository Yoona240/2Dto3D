"""
Stage-2 Target 3D reconstruction consistency checker.

Supports two methods, selected via config.edit_quality_check.two_stage_recon.stage2_method:
- "lpips": Compares edited target images against rendered views using LPIPS perceptual distance.
- "vlm":   Sends a 3x2 render grid + edited reference grid + instruction to a VLM for pass/fail.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.image.edit_quality_checker import (
    EDIT_STATUS_FAILED_QUALITY,
    EDIT_STATUS_PASSED,
)

logger = logging.getLogger(__name__)


@dataclass
class TargetQualityCheckResult:
    """Written to target model meta.json under ``target_quality_check``."""

    status: str
    method: str  # Historical schema value, currently "two_stage_recon"
    metric: str  # "lpips" | "vlm"
    views: List[str]
    input_mode: str
    aggregate: str
    scores_by_view: Dict[str, float]
    score: float  # aggregated score (LPIPS) or VLM confidence (0~1)
    threshold: float
    reason: str
    target_image_paths: Dict[str, str] = field(default_factory=dict)
    target_render_paths: Dict[str, str] = field(default_factory=dict)
    vlm_reason: Optional[str] = None  # VLM method only: LLM's text explanation

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "method": self.method,
            "status": self.status,
            "metric": self.metric,
            "views": self.views,
            "input_mode": self.input_mode,
            "aggregate": self.aggregate,
            "scores_by_view": self.scores_by_view,
            "score": self.score,
            "threshold": self.threshold,
            "target_image_paths": self.target_image_paths,
            "target_render_paths": self.target_render_paths,
            "reason": self.reason,
        }
        if self.vlm_reason is not None:
            d["vlm_reason"] = self.vlm_reason
        return d


_lpips_cache: Dict[tuple[str, str], Any] = {}


def _load_lpips(device: str, net: str):
    """Load LPIPS model, cached by (device, net)."""
    cache_key = (device, net)
    if cache_key in _lpips_cache:
        return _lpips_cache[cache_key]

    try:
        import lpips
    except ImportError as exc:
        raise ImportError(
            "lpips package is required for recon consistency check. "
            "Install with: pip install lpips"
        ) from exc

    logger.info("Loading LPIPS model on device=%s net=%s ...", device, net)
    try:
        model = lpips.LPIPS(net=net, verbose=False)
    except Exception as exc:
        raise RuntimeError(
            "Failed to initialize LPIPS model. "
            "Ensure lpips dependencies and pretrained backbone weights are available."
        ) from exc

    model = model.to(device)
    model.eval()
    _lpips_cache[cache_key] = model
    logger.info("LPIPS model loaded.")
    return model


def compute_lpips_score(
    image_a_path: Path,
    image_b_path: Path,
    device: str,
    input_mode: str,
    net: str,
) -> float:
    """Compute LPIPS perceptual distance between two images."""
    import numpy as np
    import torch

    from utils.fs_retry import retry_io

    image_a_path = Path(image_a_path)
    image_b_path = Path(image_b_path)

    if not retry_io(lambda: image_a_path.exists(), description=f"exists {image_a_path}"):
        raise FileNotFoundError(f"Image A not found: {image_a_path}")
    if not retry_io(lambda: image_b_path.exists(), description=f"exists {image_b_path}"):
        raise FileNotFoundError(f"Image B not found: {image_b_path}")

    img_a = _load_metric_image(image_a_path, input_mode)
    img_b = _load_metric_image(image_b_path, input_mode)

    if img_a.size != img_b.size:
        raise ValueError(
            "LPIPS requires image pairs to have the same size. "
            f"Got {img_a.size} vs {img_b.size} for "
            f"{image_a_path} and {image_b_path}"
        )

    tensor_a = _pil_to_lpips_tensor(img_a, np, torch).to(device)
    tensor_b = _pil_to_lpips_tensor(img_b, np, torch).to(device)

    model = _load_lpips(device, net)
    with torch.no_grad():
        distance = model(tensor_a, tensor_b)

    score = float(distance.detach().cpu().item())
    if not isinstance(score, (int, float)):
        raise ValueError(f"LPIPS returned non-numeric score: {score!r}")
    return score


def _load_metric_image(image_path: Path, input_mode: str):
    """Load an image for Stage-2 LPIPS computation."""
    from utils.fs_retry import retry_open_image

    image = retry_open_image(image_path, mode="RGB")
    if input_mode == "rgb":
        return image
    if input_mode == "grayscale":
        return image.convert("L").convert("RGB")
    raise ValueError(f"Unknown Stage-2 input_mode: {input_mode}")


def _pil_to_lpips_tensor(image, np_module, torch_module):
    """Convert a PIL RGB image to LPIPS tensor in [-1, 1]."""
    array = np_module.asarray(image, dtype=np_module.float32)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError(f"Expected HxWx3 RGB image for LPIPS, got shape={array.shape!r}")
    tensor = torch_module.from_numpy(array).permute(2, 0, 1).contiguous().unsqueeze(0)
    return tensor / 127.5 - 1.0


class ReconConsistencyChecker:
    """Checks target 3D reconstruction consistency against target images."""

    def __init__(self, config: Any):
        self._config = config
        self._tsr = config.edit_quality_check.two_stage_recon
        if self._tsr is None:
            raise ValueError(
                "ReconConsistencyChecker requires "
                "edit_quality_check.two_stage_recon config"
            )

    def _get_views_to_check(self) -> List[str]:
        return list(self._tsr.recon_views)

    def _aggregate_scores(self, scores: List[float]) -> float:
        if not scores:
            raise ValueError("No scores to aggregate")
        if self._tsr.aggregate == "max":
            return max(scores)
        if self._tsr.aggregate == "mean":
            return sum(scores) / len(scores)
        raise ValueError(f"Unknown aggregate: {self._tsr.aggregate}")

    def check(
        self,
        target_image_dir: Path,
        target_render_dir: Path,
    ) -> TargetQualityCheckResult:
        """Run LPIPS consistency check."""
        target_image_dir = Path(target_image_dir)
        target_render_dir = Path(target_render_dir)

        views = self._get_views_to_check()
        scores_by_view: Dict[str, float] = {}
        img_paths: Dict[str, str] = {}
        render_paths: Dict[str, str] = {}

        from utils.fs_retry import retry_io

        for view_name in views:
            img_path = target_image_dir / f"{view_name}.png"
            render_path = target_render_dir / f"{view_name}.png"

            if not retry_io(lambda p=img_path: p.exists(), description=f"exists {img_path}"):
                raise FileNotFoundError(
                    f"Target image missing for view '{view_name}': {img_path}"
                )
            if not retry_io(
                lambda p=render_path: p.exists(),
                description=f"exists {render_path}",
            ):
                raise FileNotFoundError(
                    f"Target 3D render missing for view '{view_name}': {render_path}"
                )

            score = compute_lpips_score(
                image_a_path=img_path,
                image_b_path=render_path,
                device=self._tsr.device,
                input_mode=self._tsr.input_mode,
                net=self._tsr.lpips_net,
            )
            scores_by_view[view_name] = round(score, 6)
            img_paths[view_name] = str(img_path)
            render_paths[view_name] = str(render_path)

        aggregated = self._aggregate_scores(list(scores_by_view.values()))
        aggregated = round(aggregated, 6)
        threshold = self._tsr.threshold

        passed = aggregated <= threshold
        status = EDIT_STATUS_PASSED if passed else EDIT_STATUS_FAILED_QUALITY
        reason = (
            f"score {aggregated} <= threshold {threshold}"
            if passed
            else f"score {aggregated} > threshold {threshold}"
        )

        return TargetQualityCheckResult(
            status=status,
            method="two_stage_recon",
            metric=self._tsr.metric,
            views=list(views),
            input_mode=self._tsr.input_mode,
            aggregate=self._tsr.aggregate,
            scores_by_view=scores_by_view,
            score=aggregated,
            threshold=threshold,
            reason=reason,
            target_image_paths=img_paths,
            target_render_paths=render_paths,
        )


# ---------------------------------------------------------------------------
# VLM-based Stage-2 checker
# ---------------------------------------------------------------------------

_VIEW_ORDER = ["front", "back", "left", "right", "top", "bottom"]


def _stitch_views_3x2(views_dir: Path, label: str) -> "PIL.Image.Image":
    """Stitch 6 view images (front/back/left/right/top/bottom) into a 3×2 grid."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError("Pillow is required for VLM recon check.") from exc

    from utils.fs_retry import retry_open_image

    images = []
    missing = []
    for view in _VIEW_ORDER:
        p = views_dir / f"{view}.png"
        if not p.exists():
            missing.append(view)
        else:
            images.append(retry_open_image(p, mode="RGB"))

    if missing:
        raise FileNotFoundError(
            f"[VLMReconCheck] {label} missing views {missing} in {views_dir}"
        )

    w, h = images[0].size
    grid = Image.new("RGB", (w * 3, h * 2))
    for idx, img in enumerate(images):
        col = idx % 3
        row = idx // 3
        grid.paste(img, (col * w, row * h))
    return grid


class VLMReconConsistencyChecker:
    """Stage-2 consistency checker using a VLM instead of LPIPS.

    Sends two 3×2 view grids (edited reference + target 3D renders) plus
    the editing instruction to a VLM and interprets the JSON response.
    """

    def __init__(self, config: Any):
        self._config = config
        self._tsr = config.edit_quality_check.two_stage_recon
        if self._tsr is None:
            raise ValueError(
                "VLMReconConsistencyChecker requires "
                "edit_quality_check.two_stage_recon config"
            )
        self._vlm_cfg = self._tsr.vlm_recon
        if self._vlm_cfg is None:
            raise ValueError(
                "VLMReconConsistencyChecker requires "
                "edit_quality_check.two_stage_recon.vlm_recon config "
                "(stage2_method must be 'vlm')"
            )

    def check(
        self,
        target_image_dir: Path,
        target_render_dir: Path,
        instruction: Optional[str] = None,
        source_views_dir: Optional[Path] = None,
    ) -> TargetQualityCheckResult:
        """Run VLM consistency check.

        Args:
            target_image_dir: Directory of edited reference view images.
            target_render_dir: Directory of target 3D rendered view images.
            instruction: The editing instruction text (optional; used in prompt).
            source_views_dir: Directory of source (pre-edit) 3D rendered view images.
                When provided, the VLM receives an additional image showing the
                original object so it can identify which part was edited.
        """
        import base64
        import io

        from utils.prompts import STAGE2_VLM_RECON_PROMPT

        target_image_dir = Path(target_image_dir)
        target_render_dir = Path(target_render_dir)

        # --- Build view path dicts for result schema ---
        img_paths: Dict[str, str] = {
            v: str(target_image_dir / f"{v}.png") for v in _VIEW_ORDER
        }
        render_paths: Dict[str, str] = {
            v: str(target_render_dir / f"{v}.png") for v in _VIEW_ORDER
        }

        # --- Stitch grids ---
        source_grid = None
        if source_views_dir is not None:
            source_views_dir = Path(source_views_dir)
            try:
                source_grid = _stitch_views_3x2(source_views_dir, label="source")
            except FileNotFoundError:
                logger.warning(
                    "[VLMReconCheck] source_views_dir provided but missing views, "
                    "falling back to 2-image mode: %s",
                    source_views_dir,
                )
        ref_grid = _stitch_views_3x2(target_image_dir, label="reference")
        render_grid = _stitch_views_3x2(target_render_dir, label="render")

        # --- Encode to base64 PNG ---
        def _to_b64(img) -> str:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("utf-8")

        ref_b64 = _to_b64(ref_grid)
        render_b64 = _to_b64(render_grid)

        # --- Build prompt ---
        instr_text = (instruction or "").strip() or "(no instruction provided)"
        if source_grid is not None:
            from utils.prompts import STAGE2_VLM_RECON_PROMPT_WITH_SOURCE
            prompt_text = STAGE2_VLM_RECON_PROMPT_WITH_SOURCE.format(
                instruction=instr_text
            )
        else:
            prompt_text = STAGE2_VLM_RECON_PROMPT.format(instruction=instr_text)

        # --- Call VLM via llm_client ---
        from utils.llm_client import get_llm_client

        mllm_cfg = self._config.target_consistency_judge_mllm
        client = get_llm_client(mllm_cfg)

        images: list = []
        if source_grid is not None:
            images.append({"data": _to_b64(source_grid), "media_type": "image/png"})
        images.append({"data": ref_b64, "media_type": "image/png"})
        images.append({"data": render_b64, "media_type": "image/png"})

        raw_response = client.chat_with_images(
            system_prompt="You are a 3D reconstruction quality evaluator.",
            user_prompt=prompt_text,
            images=images,
        )

        # --- Parse JSON response ---
        raw_response_stripped = raw_response.strip()
        # Strip optional markdown code fence
        if raw_response_stripped.startswith("```"):
            lines = raw_response_stripped.split("\n")
            raw_response_stripped = "\n".join(
                l for l in lines if not l.strip().startswith("```")
            ).strip()

        try:
            parsed = json.loads(raw_response_stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"[VLMReconCheck] VLM returned non-JSON response: "
                f"{raw_response_stripped[:200]!r}"
            ) from exc

        if "pass" not in parsed:
            raise ValueError(
                f"[VLMReconCheck] VLM response missing 'pass' field: {parsed}"
            )

        vlm_pass: bool = bool(parsed["pass"])
        confidence: float = float(parsed.get("confidence", 1.0 if vlm_pass else 0.0))
        confidence = round(max(0.0, min(1.0, confidence)), 4)
        vlm_reason_text: str = str(parsed.get("reason", "")).strip()

        # --- Apply pass threshold ---
        threshold = self._vlm_cfg.pass_threshold
        if self._vlm_cfg.use_confidence:
            passed = vlm_pass and confidence >= threshold
        else:
            passed = vlm_pass

        status = EDIT_STATUS_PASSED if passed else EDIT_STATUS_FAILED_QUALITY
        reason = f"pass={vlm_pass}, confidence={confidence}"

        logger.info(
            "[VLMReconCheck] result: pass=%s confidence=%.4f threshold=%.2f "
            "use_confidence=%s status=%s reason=%r",
            vlm_pass,
            confidence,
            threshold,
            self._vlm_cfg.use_confidence,
            status,
            vlm_reason_text,
        )

        return TargetQualityCheckResult(
            status=status,
            method="two_stage_recon",
            metric="vlm",
            views=list(_VIEW_ORDER),
            input_mode="rgb",
            aggregate="vlm",
            scores_by_view={},
            score=confidence,
            threshold=threshold,
            reason=reason,
            target_image_paths=img_paths,
            target_render_paths=render_paths,
            vlm_reason=vlm_reason_text if vlm_reason_text else None,
        )
