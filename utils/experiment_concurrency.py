from __future__ import annotations

import math
from typing import Any, Dict


VALID_PROVIDERS = {"hunyuan", "tripo", "rodin"}


def get_run_full_experiment_concurrency_limits(
    config: Any,
    source_provider: str,
    target_provider: str,
) -> Dict[str, int]:
    if source_provider not in VALID_PROVIDERS:
        raise ValueError(f"Invalid source_provider: {source_provider!r}")
    if target_provider not in VALID_PROVIDERS:
        raise ValueError(f"Invalid target_provider: {target_provider!r}")

    return {
        "text": config.concurrency.text,
        "image": config.concurrency.image,
        "edit_quality_check": config.concurrency.edit_quality_check,
        "recon_quality_check": config.concurrency.recon_quality_check,
        "source_gen3d": getattr(config.concurrency.gen3d, source_provider),
        "target_gen3d": getattr(config.concurrency.gen3d, target_provider),
        "render": config.concurrency.render,
    }


def derive_run_full_experiment_category_workers(
    config: Any,
    source_provider: str,
    target_provider: str,
) -> int:
    limits = get_run_full_experiment_concurrency_limits(
        config=config,
        source_provider=source_provider,
        target_provider=target_provider,
    )
    scheduling = config.run_full_experiment.scheduling
    render_buffer = max(limits["render"], 2)
    if source_provider == target_provider:
        gen3d_pressure = max(limits["source_gen3d"], limits["target_gen3d"])
    else:
        gen3d_pressure = limits["source_gen3d"] + limits["target_gen3d"]
    raw = (
        limits["image"]
        + math.ceil(gen3d_pressure / scheduling.provider_pressure_divisor)
        + render_buffer
    )
    return min(raw, scheduling.object_workers_cap)


def describe_run_full_experiment_category_workers(
    config: Any,
    source_provider: str,
    target_provider: str,
) -> str:
    limits = get_run_full_experiment_concurrency_limits(
        config=config,
        source_provider=source_provider,
        target_provider=target_provider,
    )
    scheduling = config.run_full_experiment.scheduling
    render_buffer = max(limits["render"], 2)
    if source_provider == target_provider:
        gen3d_pressure = max(limits["source_gen3d"], limits["target_gen3d"])
        gen3d_expr = (
            f"max(source_gen3d({limits['source_gen3d']}), "
            f"target_gen3d({limits['target_gen3d']}))"
        )
    else:
        gen3d_pressure = limits["source_gen3d"] + limits["target_gen3d"]
        gen3d_expr = (
            f"source_gen3d({limits['source_gen3d']}) + "
            f"target_gen3d({limits['target_gen3d']})"
        )
    raw = (
        limits["image"]
        + math.ceil(gen3d_pressure / scheduling.provider_pressure_divisor)
        + render_buffer
    )
    derived = min(raw, scheduling.object_workers_cap)
    return (
        f"min(image({limits['image']}) + ceil(({gen3d_expr}) / "
        f"provider_pressure_divisor({scheduling.provider_pressure_divisor})) + "
        f"render_buffer({render_buffer}), object_workers_cap({scheduling.object_workers_cap})) = {derived}"
    )
