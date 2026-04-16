"""
Guardrail Prompt 工具函数 / Utilities.

统一封装三件事（跨任务可复用，文案本身不共享）:
1) 读取并校验任务级 guardrail 配置 / resolve task-level config
2) 组装最终 prompt / compose final prompt
3) 生成追踪元数据 / build prompt trace metadata
"""

from dataclasses import dataclass
from typing import Any, Optional

from utils.prompts import get_guardrail_text


COMPOSE_STRATEGY = "guardrail+task_context+user_instruction"


@dataclass(frozen=True)
class ResolvedGuardrailPrompt:
    """已解析的 guardrail 配置快照 / Resolved guardrail snapshot."""

    task_name: str
    enabled: bool
    version: Optional[str]
    text: str


def _require_non_empty_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Config key must be a non-empty string: {path}")
    return value


def _get_task_config(config: Any, task_name: str) -> Any:
    """按任务名获取配置 / Fetch task config by task name."""
    tasks = getattr(config, "tasks", None)
    if not isinstance(tasks, dict):
        raise ValueError("Config key must be mapping: tasks")
    if task_name not in tasks:
        raise ValueError(f"Missing required task config: tasks.{task_name}")
    return tasks[task_name]


def resolve_guardrail(config: Any, task_name: str) -> ResolvedGuardrailPrompt:
    """解析任务级 guardrail 配置（Fail Loudly）/ Resolve with fail-loud validation."""
    task_config = _get_task_config(config, task_name)
    enabled = getattr(task_config, "guardrail_prompt_enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"Config key must be bool: tasks.{task_name}.guardrail_prompt_enabled")

    guardrail = getattr(task_config, "guardrail_prompt", None)
    if not enabled:
        # 关闭状态：不参与拼接；如果提供了文案，仅作为元数据透传。
        # Disabled mode: skip prompt prepend; keep values for trace if present.
        if guardrail is None:
            return ResolvedGuardrailPrompt(
                task_name=task_name, enabled=False, version=None, text=""
            )

        version = getattr(guardrail, "version", None)
        text = getattr(guardrail, "text", "")
        if version is not None and not isinstance(version, str):
            raise ValueError(
                f"Config key must be string: tasks.{task_name}.guardrail_prompt.version"
            )
        if not isinstance(text, str):
            raise ValueError(
                f"Config key must be string: tasks.{task_name}.guardrail_prompt.text"
            )
        return ResolvedGuardrailPrompt(
            task_name=task_name,
            enabled=False,
            version=version,
            text=text,
        )

    if guardrail is None:
        raise ValueError(f"Missing required config section: tasks.{task_name}.guardrail_prompt")

    version = _require_non_empty_str(
        getattr(guardrail, "version", None),
        f"tasks.{task_name}.guardrail_prompt.version",
    )
    # Use text from config if provided, otherwise look up from prompts registry by version.
    raw_text = getattr(guardrail, "text", None)
    if raw_text and isinstance(raw_text, str) and raw_text.strip():
        text = raw_text.strip()
    else:
        text = get_guardrail_text(version)
    return ResolvedGuardrailPrompt(
        task_name=task_name,
        enabled=True,
        version=version,
        text=text,
    )


def compose_final_prompt(
    *,
    guardrail: ResolvedGuardrailPrompt,
    task_context_prompt: str,
    user_instruction: str,
) -> str:
    """按固定顺序拼接 / Compose in deterministic order."""
    context = _require_non_empty_str(task_context_prompt, "task_context_prompt")
    instruction = _require_non_empty_str(user_instruction, "user_instruction")

    parts = []
    if guardrail.enabled:
        # 仅在开启时把约束前置，确保行为可配置、可回退。
        # Prepend guardrail only when enabled for configurable rollback.
        parts.append(_require_non_empty_str(guardrail.text, "guardrail_prompt.text"))
    parts.append(context)
    parts.append(instruction)
    return "\n\n".join(parts)


def build_prompt_trace(
    *,
    guardrail: ResolvedGuardrailPrompt,
    task_context_prompt: str,
    user_instruction: str,
    final_prompt: str,
) -> dict[str, Any]:
    """构建可追溯元数据 / Build prompt trace metadata."""
    return {
        "task_name": guardrail.task_name,
        "guardrail_prompt_enabled": guardrail.enabled,
        "guardrail_prompt_version": guardrail.version,
        "guardrail_prompt_text": guardrail.text if guardrail.enabled else "",
        "task_context_prompt": task_context_prompt,
        "user_instruction": user_instruction,
        "final_prompt": final_prompt,
        "compose_strategy": COMPOSE_STRATEGY,
    }
