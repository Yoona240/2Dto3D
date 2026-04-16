"""
Configuration Loader Module (V2 - Refactored)

统一 OneAPI Gateway 配置，消除重复配置。
保持向后兼容性，提供与旧版相同的接口。

使用示例:
    from config.config_v2 import load_config

    config = load_config()

    # 获取文本生成配置
    text_config = config.get_text_provider_config()

    # 获取图像生成配置
    image_config = config.get_image_provider_config()

    # 获取3D生成配置
    gen3d_config = config.get_3d_provider_config()
"""

import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Union, List


# ==================== 基础配置类 ====================


@dataclass
class TaskConfig:
    """任务级配置 / Task-level config"""

    provider: str
    model: str
    aspect_ratio: Optional[str]
    guardrail_prompt_enabled: bool = False
    guardrail_prompt: Optional["GuardrailPromptConfig"] = None


@dataclass
class ViewSanityCheckConfig:
    """View Sanity Check（新 Stage 1A）配置：跨视角几何一致性，fail 直接退出"""

    enabled: bool


@dataclass
class VlmReconConfig:
    """Stage2 VLM 一致性检测专用配置（stage2_method = vlm 时生效）"""

    pass_threshold: float  # VLM confidence >= this to pass (when use_confidence=true)
    use_confidence: bool   # false = use pass field only; true = also gate on confidence


@dataclass
class TwoStageReconConfig:
    """Method-2 (two_stage_recon) 专用配置"""

    # Stage 1: edit correctness
    edit_view_policy: str  # "front_only" | "all_6" | "stitched_6"
    require_all_views_pass: bool
    diff_output_format: str

    # View Sanity Check (新 Stage 1A)
    view_sanity_check: ViewSanityCheckConfig

    # Stage 2: method selector
    stage2_method: str  # "lpips" | "vlm"

    # Stage 2 LPIPS specific
    metric: str  # "lpips"
    recon_views: List[str]
    input_mode: str  # "rgb" | "grayscale"
    aggregate: str  # "max" | "mean"
    threshold: float
    device: str  # "cuda" | "cpu"
    lpips_net: str  # "alex"

    # Stage 2 VLM specific (None when stage2_method = "lpips")
    vlm_recon: Optional[VlmReconConfig] = None


@dataclass
class UnifiedJudgeConfig:
    """Method-3 (unified_judge) 专用配置：单次 VLM 统一判定"""

    require_rejudge_after_relabel: bool
    require_non_weak_evidence: bool


@dataclass
class EditQualityCheckConfig:
    """编辑质量检测配置"""

    enabled: bool
    method: str  # "grid_vlm" | "two_stage_recon" | "unified_judge"
    save_debug_assets: bool
    temp_dir_name: str
    max_retries_on_quality_fail: int
    two_stage_recon: Optional[
        TwoStageReconConfig
    ]  # populated when method = two_stage_recon
    unified_judge: Optional[
        UnifiedJudgeConfig
    ]  # populated when method = unified_judge


@dataclass
class EditArtifactsConfig:
    """Edit artifact materialization config."""

    diff_threshold: int
    opening_kernel_size: int


@dataclass
class Stage1RelabelConfig:
    """Stage1 relabel configuration."""

    enabled: bool
    require_rejudge_pass: bool
    allow_image_input: bool
    save_raw_response: bool


@dataclass
class GuardrailPromptConfig:
    """任务级固定约束 Prompt 配置 / Task-level guardrail prompt config"""

    version: str
    text: str = ""  # Optional: if empty, text is loaded from utils/prompts.py by version key


@dataclass
class TextModelConfig:
    """文本模型配置"""

    temperature: float
    max_tokens: int
    base_url: Optional[str] = None  # 可选：覆盖全局 oneapi.base_url


@dataclass
class ImageModelConfig:
    """图像模型配置（Response API）"""

    api_type: str  # "response"
    size: str
    n: int
    poll_interval: int
    max_wait_time: int
    base_url: Optional[str] = None  # 可选：覆盖全局 oneapi.base_url


@dataclass
class AspectControlConfig:
    """图像宽高比控制配置"""

    enabled: bool = True
    min_aspect: float = 0.30
    max_aspect: float = 3.00


@dataclass
class PreprocessConfig:
    """图像预处理配置（用于 3D 生成前的前景裁剪）"""

    enabled: bool = False
    strategy: str = "foreground"
    tolerance: int = 15
    min_side: int = 128
    aspect_control: Dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": True,
            "min_aspect": 0.30,
            "max_aspect": 3.00,
        }
    )


@dataclass
class Gen3DModelConfig:
    """3D生成模型配置（Response API）"""

    api_type: str  # "response"
    output_format: str
    pbr: bool
    face_count: int
    generate_type: str
    polygon_type: str
    poll_interval: int
    max_wait_time: int
    preprocess: Optional[PreprocessConfig] = None
    base_url: Optional[str] = None  # 可选：覆盖全局 oneapi.base_url


@dataclass
class OneAPIConfig:
    """OneAPI 统一网关配置"""

    api_key: str
    base_url: str
    timeout: int
    max_retries: int
    text_models: Dict[str, TextModelConfig]
    image_models: Dict[str, ImageModelConfig]
    gen3d_models: Dict[str, Gen3DModelConfig]


@dataclass
class TripoConfig:
    """Tripo 3D 生成配置"""

    api_key: str
    base_url: str
    model_version: str
    geometry_quality: str
    output_format: str
    model_seed: int
    texture_seed: int
    texture: bool
    pbr: bool
    texture_quality: str
    texture_alignment: str
    face_limit: Optional[int]
    export_uv: bool
    enable_view_selection: bool
    multiview_strategy: str
    entropy_diff_threshold: float
    view_selection_vlm_model: str
    allow_top_bottom_remap: bool
    top_bottom_target_slot: str
    top_rotation: int
    bottom_rotation: int
    top_flip_horizontal: bool
    bottom_flip_horizontal: bool
    timeout: int
    max_retries: int


@dataclass
class RodinConfig:
    """Rodin 3D 生成配置"""

    api_key: str
    base_url: str
    tier: str
    output_format: str
    timeout: int
    max_retries: int


@dataclass
class WebGLRenderConfig:
    """WebGL 渲染配置（用于 headless Chrome + model-viewer）"""

    chrome_path: Optional[str]
    environment_image: str
    shadow_intensity: float
    use_gpu: bool
    subprocess_timeout_seconds: int


@dataclass
class BlenderRenderConfig:
    """Blender 渲染配置"""

    blender_path: Optional[str]
    use_bpy: bool
    device: str
    samples: int
    lighting_mode: str


@dataclass
class SemanticAlignmentConfig:
    """语义定向对齐配置"""

    enabled: bool
    vlm_model: str
    min_confidence: float
    verify_after_rerender: bool
    save_aligned_glb: bool
    aligned_glb_suffix: str
    save_debug_assets: bool
    temp_dir_name: str
    normalize_geometry: bool
    share_rotation_to_target: bool
    norm_params_filename: str


@dataclass
class RenderConfig:
    """渲染配置（支持 Blender 和 WebGL 双后端）"""

    backend: str  # "blender" or "webgl"
    # Shared settings
    image_size: int
    rotation_z: float
    # Mode-specific settings
    semantic_alignment: SemanticAlignmentConfig
    blender: BlenderRenderConfig
    webgl: WebGLRenderConfig

    # Backward-compatible accessors (kept for existing callers).
    @property
    def blender_path(self) -> Optional[str]:
        return self.blender.blender_path

    @property
    def use_bpy(self) -> bool:
        return self.blender.use_bpy

    @property
    def device(self) -> str:
        return self.blender.device

    @property
    def samples(self) -> int:
        return self.blender.samples

    @property
    def lighting_mode(self) -> str:
        return self.blender.lighting_mode


@dataclass
class Gen3dConcurrencyConfig:
    """3D生成并发限制"""

    hunyuan: int
    tripo: int
    rodin: int


@dataclass
class ConcurrencyConfig:
    """并发限制配置"""

    gen3d: Gen3dConcurrencyConfig
    render: int
    text: int
    image: int
    edit_quality_check: int
    recon_quality_check: int
    refresh_all_dreamsim: int
    mask_backfill: int


@dataclass
class StageRetryConfig:
    """run_full_experiment 单阶段重试配置"""

    max_attempts: int


@dataclass
class RunFullExperimentRetryConfig:
    """run_full_experiment 重试配置"""

    source_prompt_optimization: StageRetryConfig
    source_t2i: StageRetryConfig
    source_gen3d: StageRetryConfig
    source_render: StageRetryConfig
    instruction_generation: StageRetryConfig
    edit_apply: StageRetryConfig
    stage1_quality_check: StageRetryConfig
    target_gen3d: StageRetryConfig
    target_render: StageRetryConfig
    stage2: StageRetryConfig


@dataclass
class RunFullExperimentApiLaneControlConfig:
    """run_full_experiment API lane 调度配置"""

    enabled: bool
    cooldown_seconds: int
    recovery_probe_one_by_one: bool


@dataclass
class RunFullExperimentSchedulingConfig:
    """run_full_experiment 调度配置"""

    object_workers_strategy: str
    object_workers_cap: int
    provider_pressure_divisor: int


@dataclass
class RunFullExperimentConfig:
    """run_full_experiment 编排配置"""

    retry: RunFullExperimentRetryConfig
    api_lane_control: RunFullExperimentApiLaneControlConfig
    scheduling: RunFullExperimentSchedulingConfig


@dataclass
class DefaultsConfig:
    """默认设置"""

    poll_interval: int
    max_wait_time: int


@dataclass
class ExportConfig:
    """导出配置"""

    path_prefixes: List[str]  # manifest 中可选的路径前缀列表


@dataclass
class WorkspaceConfig:
    """工作区路径配置"""

    pipeline_dir: str  # relative to project root, or absolute
    python_interpreter: str  # Python interpreter used by Web UI to launch subprocesses
    playwright_browsers_path: str  # Browser cache for WebGL/Playwright renderer
    logs_dir: str  # Directory for experiment run logs generated by app.py
    pipeline_index_db: str  # SQLite index DB path (local disk, not OSS)
    matrix_objects_file: str  # Object pool JSON for matrix generation
    matrix_styles_file: str  # Style definitions JSON for matrix generation
    draco_library_path: str  # Blender Draco .so path (empty = disabled)
    draco_decoder_dir: str  # WebGL Draco decoder dir (empty = disabled)


# ==================== 向后兼容的配置类 ====================


@dataclass
class QnMllmConfig:
    """向后兼容：模拟旧版 qh_mllm 配置"""

    api_key: str
    base_url: str
    default_model: str
    temperature: float
    max_tokens: int
    timeout: int
    max_retries: int


@dataclass
class GeminiResponseConfig:
    """向后兼容：模拟旧版 gemini_response 配置"""

    api_key: str
    base_url: str
    model: str
    size: str
    n: int
    timeout: int
    poll_interval: int
    max_wait_time: int
    max_retries: int


@dataclass
class ImageApiConfig:
    """向后兼容：模拟旧版 qh_image 配置"""

    api_key: str
    base_url: str
    model: str
    timeout: int
    max_retries: int
    size: str
    n: int
    poll_interval: int
    max_wait_time: int


@dataclass
class HunyuanConfig:
    """向后兼容：Hunyuan 3D 配置"""

    api_key: str
    base_url: str
    output_format: str
    model: str
    pbr: bool
    face_count: int
    generate_type: str
    polygon_type: str
    timeout: int
    max_retries: int
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    download_proxy: Optional[str] = None


# ==================== 主配置类 ====================


@dataclass
class Config:
    """主配置对象（V2 重构版）"""

    # 核心配置
    oneapi: OneAPIConfig
    tripo: TripoConfig
    rodin: RodinConfig

    # 任务配置
    tasks: Dict[str, TaskConfig]
    edit_quality_check: EditQualityCheckConfig
    edit_artifacts: EditArtifactsConfig
    stage1_relabel: Stage1RelabelConfig

    # 系统配置
    render: RenderConfig
    concurrency: ConcurrencyConfig
    run_full_experiment: RunFullExperimentConfig
    defaults: DefaultsConfig
    workspace: WorkspaceConfig
    export: ExportConfig
    language: str

    # 原始数据（用于动态访问）
    _raw_data: Dict[str, Any] = field(repr=False)

    # 可选项（有默认值，必须放在所有无默认值字段之后）
    download_proxy: Optional[str] = None

    # ==================== 向后兼容属性 ====================

    def _resolve_base_url(self, model_config) -> str:
        """解析模型的 base_url：优先使用模型级别覆盖，否则使用全局 oneapi.base_url"""
        return model_config.base_url or self.oneapi.base_url

    @property
    def text_gen(self) -> TaskConfig:
        """向后兼容：text_gen 任务配置"""
        return self.tasks["text_generation"]

    @property
    def image_gen(self) -> TaskConfig:
        """向后兼容：image_gen 任务配置"""
        return self.tasks["image_generation"]

    @property
    def gen_3d(self) -> TaskConfig:
        """向后兼容：gen_3d 任务配置"""
        return self.tasks["gen3d"]

    @property
    def qh_mllm(self) -> QnMllmConfig:
        """向后兼容：qh_mllm 配置（从 oneapi 生成）"""
        model_name = self.tasks["text_generation"].model
        model_config = self.oneapi.text_models[model_name]

        return QnMllmConfig(
            api_key=self.oneapi.api_key,
            base_url=f"{self._resolve_base_url(model_config)}/v1",  # 添加 /v1 后缀
            default_model=model_name,
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
        )

    @property
    def edit_quality_mllm(self) -> QnMllmConfig:
        """编辑质量检测使用的 VLM 配置（从 oneapi 生成）— Method-1 grid_vlm"""
        task = self.tasks["edit_quality_check"]
        if task.provider != "oneapi":
            raise ValueError(
                f"edit_quality_check provider must be oneapi, got: {task.provider}"
            )
        model_name = task.model
        model_config = self.oneapi.text_models[model_name]
        return QnMllmConfig(
            api_key=self.oneapi.api_key,
            base_url=f"{self._resolve_base_url(model_config)}/v1",
            default_model=model_name,
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
        )

    @property
    def edit_quality_diff_mllm(self) -> QnMllmConfig:
        """Method-2 Stage 1A: VLM diff description 配置"""
        task = self.tasks["edit_quality_check_diff"]
        if task.provider != "oneapi":
            raise ValueError(
                f"edit_quality_check_diff provider must be oneapi, got: {task.provider}"
            )
        model_name = task.model
        model_config = self.oneapi.text_models[model_name]
        return QnMllmConfig(
            api_key=self.oneapi.api_key,
            base_url=f"{self._resolve_base_url(model_config)}/v1",
            default_model=model_name,
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
        )

    @property
    def edit_quality_judge_mllm(self) -> QnMllmConfig:
        """Method-2 Stage 1B: LLM judge (text-only) 配置"""
        task = self.tasks["edit_quality_check_judge"]
        if task.provider != "oneapi":
            raise ValueError(
                f"edit_quality_check_judge provider must be oneapi, got: {task.provider}"
            )
        model_name = task.model
        model_config = self.oneapi.text_models[model_name]
        return QnMllmConfig(
            api_key=self.oneapi.api_key,
            base_url=f"{self._resolve_base_url(model_config)}/v1",
            default_model=model_name,
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
        )

    @property
    def edit_quality_unified_mllm(self) -> QnMllmConfig:
        """Method-3 unified_judge: 单次 VLM 统一判定配置"""
        task = self.tasks["edit_quality_check_unified"]
        if task.provider != "oneapi":
            raise ValueError(
                f"edit_quality_check_unified provider must be oneapi, got: {task.provider}"
            )
        model_name = task.model
        model_config = self.oneapi.text_models[model_name]
        return QnMllmConfig(
            api_key=self.oneapi.api_key,
            base_url=f"{self._resolve_base_url(model_config)}/v1",
            default_model=model_name,
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
        )

    @property
    def target_consistency_judge_mllm(self) -> QnMllmConfig:
        """Stage2 VLM 一致性检测配置（stage2_method = vlm 时使用）"""
        task = self.tasks["target_consistency_judge"]
        if task.provider != "oneapi":
            raise ValueError(
                f"target_consistency_judge provider must be oneapi, got: {task.provider}"
            )
        model_name = task.model
        model_config = self.oneapi.text_models[model_name]
        return QnMllmConfig(
            api_key=self.oneapi.api_key,
            base_url=f"{self._resolve_base_url(model_config)}/v1",
            default_model=model_name,
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
        )

    @property
    def edit_quality_view_sanity_mllm(self) -> QnMllmConfig:
        """Method-2 View Sanity Check（新 Stage 1A）VLM 配置"""
        task = self.tasks["edit_quality_check_view_sanity"]
        if task.provider != "oneapi":
            raise ValueError(
                f"edit_quality_check_view_sanity provider must be oneapi, got: {task.provider}"
            )
        model_name = task.model
        model_config = self.oneapi.text_models[model_name]
        return QnMllmConfig(
            api_key=self.oneapi.api_key,
            base_url=f"{self._resolve_base_url(model_config)}/v1",
            default_model=model_name,
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
        )

    @property
    def qh_image(self) -> ImageApiConfig:
        """向后兼容：qh_image 配置（从 oneapi 生成）"""
        model_name = self.tasks["image_generation"].model
        model_config = self.oneapi.image_models[model_name]

        return ImageApiConfig(
            api_key=self.oneapi.api_key,
            base_url=self._resolve_base_url(model_config),
            model=model_name,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
            size=model_config.size,
            n=model_config.n,
            poll_interval=model_config.poll_interval,
            max_wait_time=model_config.max_wait_time,
        )

    @property
    def gemini_response(self) -> GeminiResponseConfig:
        """向后兼容：gemini_response 配置（从 oneapi 生成）"""
        model_name = self.tasks["image_editing"].model
        model_config = self.oneapi.image_models[model_name]

        return GeminiResponseConfig(
            api_key=self.oneapi.api_key,
            base_url=self._resolve_base_url(model_config),
            model=model_name,
            size=model_config.size,
            n=model_config.n,
            timeout=self.oneapi.timeout,
            poll_interval=model_config.poll_interval,
            max_wait_time=model_config.max_wait_time,
            max_retries=self.oneapi.max_retries,
        )

    @property
    def guided_edit(self) -> ImageApiConfig:
        """Guided view editing 配置（从 oneapi 生成）"""
        model_name = self.tasks["guided_edit"].model
        model_config = self.oneapi.image_models[model_name]

        return ImageApiConfig(
            api_key=self.oneapi.api_key,
            base_url=self._resolve_base_url(model_config),
            model=model_name,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
            size=model_config.size,
            n=model_config.n,
            poll_interval=model_config.poll_interval,
            max_wait_time=model_config.max_wait_time,
        )

    @property
    def multiview_edit(self) -> GeminiResponseConfig:
        """向后兼容：multiview_edit 配置（从 oneapi 生成）"""
        model_name = self.tasks["multiview_editing"].model
        model_config = self.oneapi.image_models[model_name]

        return GeminiResponseConfig(
            api_key=self.oneapi.api_key,
            base_url=self._resolve_base_url(model_config),
            model=model_name,
            size=model_config.size,
            n=model_config.n,
            timeout=self.oneapi.timeout,
            poll_interval=model_config.poll_interval,
            max_wait_time=model_config.max_wait_time,
            max_retries=self.oneapi.max_retries,
        )

    @property
    def doubao_image(self) -> GeminiResponseConfig:
        """向后兼容：doubao_image 配置（从 oneapi 生成）"""
        # 使用配置的 doubao 模型
        model_name = "doubao-seedream-4.5"
        model_config = self.oneapi.image_models[model_name]

        return GeminiResponseConfig(
            api_key=self.oneapi.api_key,
            base_url=self._resolve_base_url(model_config),
            model=model_name,
            size=model_config.size,
            n=model_config.n,
            timeout=self.oneapi.timeout,
            poll_interval=model_config.poll_interval,
            max_wait_time=model_config.max_wait_time,
            max_retries=self.oneapi.max_retries,
        )

    def get_hunyuan_config(self, model_name: Optional[str] = None) -> HunyuanConfig:
        """获取 Hunyuan 配置

        Args:
            model_name: 模型名称（如 "hunyuan-3d-pro" 或 "hunyuan-3d-3.1-pro"）。
                         如果未指定，从 tasks.gen3d.model 读取（仅当该值是已知 hunyuan 模型时），
                         否则默认使用 "hunyuan-3d-pro"。
        """
        if model_name is None:
            task_model = self.tasks["gen3d"].model
            if task_model in self.oneapi.gen3d_models:
                model_name = task_model
            else:
                model_name = "hunyuan-3d-pro"

        model_config = self.oneapi.gen3d_models[model_name]

        # Use preprocess config from model config, or default
        preprocess = (
            model_config.preprocess if model_config.preprocess else PreprocessConfig()
        )

        return HunyuanConfig(
            api_key=self.oneapi.api_key,
            base_url=self._resolve_base_url(model_config),
            output_format=model_config.output_format,
            model=model_name,
            pbr=model_config.pbr,
            face_count=model_config.face_count,
            generate_type=model_config.generate_type,
            polygon_type=model_config.polygon_type,
            timeout=self.oneapi.timeout,
            max_retries=self.oneapi.max_retries,
            preprocess=preprocess,
            download_proxy=self.download_proxy,
        )

    @property
    def hunyuan(self) -> HunyuanConfig:
        """向后兼容：hunyuan 配置（从 oneapi 生成）"""
        return self.get_hunyuan_config()

    # ==================== 向后兼容方法 ====================

    def get_text_provider_config(self) -> QnMllmConfig:
        """获取文本生成 provider 配置"""
        provider = self.tasks["text_generation"].provider
        if provider == "oneapi":
            return self.qh_mllm
        raise ValueError(f"Unknown text provider: {provider}")

    def get_image_provider_config(self) -> ImageApiConfig:
        """获取图像生成 provider 配置"""
        provider = self.tasks["image_generation"].provider
        if provider == "oneapi":
            return self.qh_image
        raise ValueError(f"Unknown image provider: {provider}")

    def get_3d_provider_config(self) -> Union[TripoConfig, HunyuanConfig, RodinConfig]:
        """获取3D生成 provider 配置"""
        provider = self.tasks["gen3d"].provider
        if provider == "tripo":
            return self.tripo
        elif provider == "oneapi":
            return self.hunyuan
        elif provider == "rodin":
            return self.rodin
        raise ValueError(f"Unknown 3D provider: {provider}")


# ==================== 配置加载函数 ====================


def _require_key(data: dict, key: str, path: str, *, allow_none: bool = False) -> Any:
    """检查必需的配置键"""
    if key not in data:
        raise ValueError(f"Missing required config key: {path}.{key}")
    value = data[key]
    if value is None and not allow_none:
        raise ValueError(f"Config key cannot be null: {path}.{key}")
    return value


def _require_section(data: dict, key: str) -> dict:
    """检查必需的配置段"""
    if key not in data or data[key] is None:
        raise ValueError(f"Missing required config section: {key}")
    section = data[key]
    if not isinstance(section, dict):
        raise ValueError(f"Config section must be a mapping: {key}")
    return section


def _require_non_empty_string_list(data: dict, key: str, path: str) -> List[str]:
    """Validate a required non-empty list of non-empty strings."""
    value = _require_key(data, key, path)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{path}.{key} must be a non-empty list")
    normalized: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{path}.{key}[{index}] must be a non-empty string, got: {item!r}"
            )
        normalized.append(item.strip())
    return normalized


def _parse_preprocess_config(data: dict, path: str) -> Optional[PreprocessConfig]:
    """解析预处理配置（可选字段）"""
    if "preprocess" not in data or data["preprocess"] is None:
        return None

    preprocess_data = data["preprocess"]
    if not isinstance(preprocess_data, dict):
        raise ValueError(f"Config {path}.preprocess must be a mapping")

    aspect_control = preprocess_data.get(
        "aspect_control", {"enabled": True, "min_aspect": 0.30, "max_aspect": 3.00}
    )

    return PreprocessConfig(
        enabled=preprocess_data.get("enabled", False),
        strategy=preprocess_data.get("strategy", "foreground"),
        tolerance=preprocess_data.get("tolerance", 15),
        min_side=preprocess_data.get("min_side", 128),
        aspect_control=aspect_control,
    )


def _parse_guardrail_prompt_config(
    task_data: dict, path: str, *, enabled: bool
) -> Optional[GuardrailPromptConfig]:
    """解析任务级 guardrail 配置 / Parse task-level guardrail config."""
    if "guardrail_prompt" not in task_data or task_data["guardrail_prompt"] is None:
        if enabled:
            raise ValueError(
                f"Missing required config section: {path}.guardrail_prompt"
            )
        # 关闭状态下允许缺省 / Allowed when disabled.
        return None

    guardrail_data = task_data["guardrail_prompt"]
    if not isinstance(guardrail_data, dict):
        raise ValueError(f"Config section must be a mapping: {path}.guardrail_prompt")

    version = _require_key(guardrail_data, "version", f"{path}.guardrail_prompt")

    if not isinstance(version, str) or not version.strip():
        raise ValueError(
            f"Config key must be a non-empty string: {path}.guardrail_prompt.version"
        )

    # text is optional — if absent or empty, prompt_guardrail.py loads it from prompts.py by version.
    text = guardrail_data.get("text", "") or ""

    return GuardrailPromptConfig(version=version, text=text)


def get_default_config_path() -> str:
    """获取默认配置文件路径"""
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        return str(config_path)
    raise FileNotFoundError(f"Config file not found: {config_path}")


def load_config(config_path: Optional[str] = None) -> Config:
    """
    加载配置文件

    Args:
        config_path: 配置文件路径（可选，默认使用 config_new.yaml）

    Returns:
        Config 对象
    """
    if config_path is None:
        config_path = get_default_config_path()

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # SeaweedFS FUSE 偶发 EIO (errno 5)，加重试
    import time as _time
    for _attempt in range(5):
        try:
            with open(path, "rb") as f:
                _raw = f.read()
            data = yaml.safe_load(_raw.decode("utf-8"))
            break
        except OSError as e:
            if e.errno == 5 and _attempt < 4:
                _time.sleep(0.2 * (2 ** _attempt))
                continue
            raise

    if not isinstance(data, dict):
        raise ValueError("Config file must contain a mapping at the top level")

    # ==================== 解析 OneAPI 配置 ====================
    oneapi_data = _require_section(data, "oneapi")

    # 解析文本模型
    text_models = {}
    text_models_data = _require_section(oneapi_data, "text_models")
    for model_name, model_data in text_models_data.items():
        text_models[model_name] = TextModelConfig(
            temperature=_require_key(
                model_data, "temperature", f"oneapi.text_models.{model_name}"
            ),
            max_tokens=_require_key(
                model_data, "max_tokens", f"oneapi.text_models.{model_name}"
            ),
            base_url=model_data.get("base_url"),
        )

    # 解析图像模型
    image_models = {}
    image_models_data = _require_section(oneapi_data, "image_models")
    for model_name, model_data in image_models_data.items():
        image_models[model_name] = ImageModelConfig(
            api_type=_require_key(
                model_data, "api_type", f"oneapi.image_models.{model_name}"
            ),
            size=_require_key(model_data, "size", f"oneapi.image_models.{model_name}"),
            n=_require_key(model_data, "n", f"oneapi.image_models.{model_name}"),
            poll_interval=_require_key(
                model_data, "poll_interval", f"oneapi.image_models.{model_name}"
            ),
            max_wait_time=_require_key(
                model_data, "max_wait_time", f"oneapi.image_models.{model_name}"
            ),
            base_url=model_data.get("base_url"),
        )

    # 解析3D生成模型
    gen3d_models = {}
    gen3d_models_data = _require_section(oneapi_data, "gen3d_models")
    for model_name, model_data in gen3d_models_data.items():
        gen3d_models[model_name] = Gen3DModelConfig(
            api_type=_require_key(
                model_data, "api_type", f"oneapi.gen3d_models.{model_name}"
            ),
            output_format=_require_key(
                model_data, "output_format", f"oneapi.gen3d_models.{model_name}"
            ),
            pbr=_require_key(model_data, "pbr", f"oneapi.gen3d_models.{model_name}"),
            face_count=_require_key(
                model_data, "face_count", f"oneapi.gen3d_models.{model_name}"
            ),
            generate_type=_require_key(
                model_data, "generate_type", f"oneapi.gen3d_models.{model_name}"
            ),
            polygon_type=_require_key(
                model_data, "polygon_type", f"oneapi.gen3d_models.{model_name}"
            ),
            poll_interval=_require_key(
                model_data, "poll_interval", f"oneapi.gen3d_models.{model_name}"
            ),
            max_wait_time=_require_key(
                model_data, "max_wait_time", f"oneapi.gen3d_models.{model_name}"
            ),
            preprocess=_parse_preprocess_config(
                model_data, f"oneapi.gen3d_models.{model_name}"
            ),
            base_url=model_data.get("base_url"),
        )

    oneapi = OneAPIConfig(
        api_key=_require_key(oneapi_data, "api_key", "oneapi"),
        base_url=_require_key(oneapi_data, "base_url", "oneapi"),
        timeout=_require_key(oneapi_data, "timeout", "oneapi"),
        max_retries=_require_key(oneapi_data, "max_retries", "oneapi"),
        text_models=text_models,
        image_models=image_models,
        gen3d_models=gen3d_models,
    )

    # ==================== 解析 Tripo 配置 ====================
    tripo_data = _require_section(data, "tripo")
    tripo = TripoConfig(
        api_key=_require_key(tripo_data, "api_key", "tripo"),
        base_url=_require_key(tripo_data, "base_url", "tripo"),
        model_version=_require_key(tripo_data, "model_version", "tripo"),
        geometry_quality=_require_key(tripo_data, "geometry_quality", "tripo"),
        output_format=_require_key(tripo_data, "output_format", "tripo"),
        model_seed=_require_key(tripo_data, "model_seed", "tripo"),
        texture_seed=_require_key(tripo_data, "texture_seed", "tripo"),
        texture=_require_key(tripo_data, "texture", "tripo"),
        pbr=_require_key(tripo_data, "pbr", "tripo"),
        texture_quality=_require_key(tripo_data, "texture_quality", "tripo"),
        texture_alignment=_require_key(tripo_data, "texture_alignment", "tripo"),
        face_limit=_require_key(tripo_data, "face_limit", "tripo", allow_none=True),
        export_uv=_require_key(tripo_data, "export_uv", "tripo"),
        enable_view_selection=_require_key(
            tripo_data, "enable_view_selection", "tripo"
        ),
        multiview_strategy=_require_key(tripo_data, "multiview_strategy", "tripo"),
        entropy_diff_threshold=_require_key(
            tripo_data, "entropy_diff_threshold", "tripo"
        ),
        view_selection_vlm_model=_require_key(
            tripo_data, "view_selection_vlm_model", "tripo"
        ),
        allow_top_bottom_remap=_require_key(
            tripo_data, "allow_top_bottom_remap", "tripo"
        ),
        top_bottom_target_slot=_require_key(
            tripo_data, "top_bottom_target_slot", "tripo"
        ),
        top_rotation=_require_key(tripo_data, "top_rotation", "tripo"),
        bottom_rotation=_require_key(tripo_data, "bottom_rotation", "tripo"),
        top_flip_horizontal=_require_key(tripo_data, "top_flip_horizontal", "tripo"),
        bottom_flip_horizontal=_require_key(
            tripo_data, "bottom_flip_horizontal", "tripo"
        ),
        timeout=_require_key(tripo_data, "timeout", "tripo"),
        max_retries=_require_key(tripo_data, "max_retries", "tripo"),
    )

    # ==================== 解析 Rodin 配置 ====================
    rodin_data = _require_section(data, "rodin")
    rodin = RodinConfig(
        api_key=_require_key(rodin_data, "api_key", "rodin"),
        base_url=_require_key(rodin_data, "base_url", "rodin"),
        tier=_require_key(rodin_data, "tier", "rodin"),
        output_format=_require_key(rodin_data, "output_format", "rodin"),
        timeout=_require_key(rodin_data, "timeout", "rodin"),
        max_retries=_require_key(rodin_data, "max_retries", "rodin"),
    )

    # ==================== 解析任务配置 ====================
    tasks_data = _require_section(data, "tasks")
    tasks = {}
    for task_name, task_data in tasks_data.items():
        # 任务级开关：控制是否启用固定约束前缀。
        # Task-level switch for guardrail prefix.
        guardrail_prompt_enabled = task_data.get("guardrail_prompt_enabled", False)
        if not isinstance(guardrail_prompt_enabled, bool):
            raise ValueError(
                f"Config key must be bool: tasks.{task_name}.guardrail_prompt_enabled"
            )
        guardrail_prompt = _parse_guardrail_prompt_config(
            task_data,
            f"tasks.{task_name}",
            enabled=guardrail_prompt_enabled,
        )

        tasks[task_name] = TaskConfig(
            provider=_require_key(task_data, "provider", f"tasks.{task_name}"),
            model=_require_key(task_data, "model", f"tasks.{task_name}"),
            aspect_ratio=_require_key(
                task_data, "aspect_ratio", f"tasks.{task_name}", allow_none=True
            ),
            guardrail_prompt_enabled=guardrail_prompt_enabled,
            guardrail_prompt=guardrail_prompt,
        )

    # ==================== 解析编辑质量检测配置 ====================
    edit_quality_check_data = _require_section(data, "edit_quality_check")

    eqc_method = _require_key(edit_quality_check_data, "method", "edit_quality_check")
    _ALLOWED_EQC_METHODS = ("grid_vlm", "two_stage_recon", "unified_judge")
    if eqc_method not in _ALLOWED_EQC_METHODS:
        raise ValueError(
            f"edit_quality_check.method must be one of {_ALLOWED_EQC_METHODS}, "
            f"got: {eqc_method!r}"
        )

    # Parse two_stage_recon sub-config.
    # Required when method == "two_stage_recon" (full Stage-1 + Stage-2 params).
    # Also parsed when method == "unified_judge", because Stage-2 LPIPS consistency
    # check always reads metric/recon_views/threshold/device/lpips_net from here.
    two_stage_recon_cfg: Optional[TwoStageReconConfig] = None
    if eqc_method in ("two_stage_recon", "unified_judge"):
        tsr_data = _require_section(edit_quality_check_data, "two_stage_recon")
        tsr_path = "edit_quality_check.two_stage_recon"

        _edit_view_policy = _require_key(tsr_data, "edit_view_policy", tsr_path)
        if _edit_view_policy not in ("front_only", "all_6", "stitched_6"):
            raise ValueError(
                f"{tsr_path}.edit_view_policy must be "
                "'front_only', 'all_6', or 'stitched_6', "
                f"got: {_edit_view_policy!r}"
            )

        _metric = _require_key(tsr_data, "metric", tsr_path)
        if _metric != "lpips":
            raise ValueError(f"{tsr_path}.metric must be 'lpips', got: {_metric!r}")

        _recon_views = _require_non_empty_string_list(tsr_data, "recon_views", tsr_path)
        _allowed_recon_views = ("front", "back", "right", "left", "top", "bottom")
        for _view in _recon_views:
            if _view not in _allowed_recon_views:
                raise ValueError(
                    f"{tsr_path}.recon_views contains invalid view: {_view!r}. "
                    f"Allowed values: {_allowed_recon_views}"
                )

        _input_mode = _require_key(tsr_data, "input_mode", tsr_path)
        if _input_mode not in ("rgb", "grayscale"):
            raise ValueError(
                f"{tsr_path}.input_mode must be 'rgb' or 'grayscale', "
                f"got: {_input_mode!r}"
            )

        _aggregate = _require_key(tsr_data, "aggregate", tsr_path)
        if _aggregate not in ("max", "mean"):
            raise ValueError(
                f"{tsr_path}.aggregate must be 'max' or 'mean', got: {_aggregate!r}"
            )

        _threshold = float(_require_key(tsr_data, "threshold", tsr_path))
        if _threshold < 0:
            raise ValueError(f"{tsr_path}.threshold must be >= 0, got: {_threshold}")

        _device = _require_key(tsr_data, "device", tsr_path)
        if _device not in ("cuda", "cpu"):
            raise ValueError(
                f"{tsr_path}.device must be 'cuda' or 'cpu', got: {_device!r}"
            )

        _lpips_net = _require_key(tsr_data, "lpips_net", tsr_path)
        if _lpips_net != "alex":
            raise ValueError(
                f"{tsr_path}.lpips_net must be 'alex', got: {_lpips_net!r}"
            )

        # Parse stage2_method
        _stage2_method = _require_key(tsr_data, "stage2_method", tsr_path)
        if _stage2_method not in ("lpips", "vlm"):
            raise ValueError(
                f"{tsr_path}.stage2_method must be 'lpips' or 'vlm', "
                f"got: {_stage2_method!r}"
            )

        # Parse vlm_recon sub-config (required when stage2_method = "vlm")
        _vlm_recon_cfg: Optional[VlmReconConfig] = None
        if _stage2_method == "vlm":
            vlm_data = _require_section(tsr_data, "vlm_recon")
            vlm_path = f"{tsr_path}.vlm_recon"
            _pass_threshold = float(_require_key(vlm_data, "pass_threshold", vlm_path))
            if not (0.0 <= _pass_threshold <= 1.0):
                raise ValueError(
                    f"{vlm_path}.pass_threshold must be between 0.0 and 1.0, "
                    f"got: {_pass_threshold}"
                )
            _use_confidence = _require_key(vlm_data, "use_confidence", vlm_path)
            if not isinstance(_use_confidence, bool):
                raise ValueError(
                    f"{vlm_path}.use_confidence must be a boolean, "
                    f"got: {_use_confidence!r}"
                )
            # Require target_consistency_judge task when vlm stage2 is selected
            if "target_consistency_judge" not in tasks:
                raise ValueError(
                    "tasks.target_consistency_judge is required when "
                    "edit_quality_check.two_stage_recon.stage2_method = 'vlm'"
                )
            _vlm_recon_cfg = VlmReconConfig(
                pass_threshold=_pass_threshold,
                use_confidence=_use_confidence,
            )

        # Validate Stage-1 task configs only when method == "two_stage_recon"
        if eqc_method == "two_stage_recon":
            if "edit_quality_check_diff" not in tasks:
                raise ValueError(
                    "tasks.edit_quality_check_diff is required when "
                    "edit_quality_check.method = 'two_stage_recon'"
                )
            if "edit_quality_check_judge" not in tasks:
                raise ValueError(
                    "tasks.edit_quality_check_judge is required when "
                    "edit_quality_check.method = 'two_stage_recon'"
                )
            if "edit_quality_check_view_sanity" not in tasks:
                raise ValueError(
                    "tasks.edit_quality_check_view_sanity is required when "
                    "edit_quality_check.method = 'two_stage_recon'"
                )

        vsc_path = f"{tsr_path}.view_sanity_check"
        vsc_data = _require_section(tsr_data, "view_sanity_check")
        view_sanity_check_cfg = ViewSanityCheckConfig(
            enabled=_require_key(vsc_data, "enabled", vsc_path),
        )

        two_stage_recon_cfg = TwoStageReconConfig(
            edit_view_policy=_edit_view_policy,
            require_all_views_pass=_require_key(
                tsr_data, "require_all_views_pass", tsr_path
            ),
            diff_output_format=_require_key(tsr_data, "diff_output_format", tsr_path),
            view_sanity_check=view_sanity_check_cfg,
            stage2_method=_stage2_method,
            metric=_metric,
            recon_views=_recon_views,
            input_mode=_input_mode,
            aggregate=_aggregate,
            threshold=_threshold,
            device=_device,
            lpips_net=_lpips_net,
            vlm_recon=_vlm_recon_cfg,
        )

    # Parse unified_judge sub-config (only when method == "unified_judge")
    unified_judge_cfg: Optional[UnifiedJudgeConfig] = None
    if eqc_method == "unified_judge":
        uj_data = _require_section(edit_quality_check_data, "unified_judge")
        uj_path = "edit_quality_check.unified_judge"

        if "edit_quality_check_unified" not in tasks:
            raise ValueError(
                "tasks.edit_quality_check_unified is required when "
                "edit_quality_check.method = 'unified_judge'"
            )

        unified_judge_cfg = UnifiedJudgeConfig(
            require_rejudge_after_relabel=_require_key(
                uj_data, "require_rejudge_after_relabel", uj_path
            ),
            require_non_weak_evidence=_require_key(
                uj_data, "require_non_weak_evidence", uj_path
            ),
        )

    edit_quality_check = EditQualityCheckConfig(
        enabled=_require_key(edit_quality_check_data, "enabled", "edit_quality_check"),
        method=eqc_method,
        save_debug_assets=_require_key(
            edit_quality_check_data, "save_debug_assets", "edit_quality_check"
        ),
        temp_dir_name=_require_key(
            edit_quality_check_data, "temp_dir_name", "edit_quality_check"
        ),
        max_retries_on_quality_fail=_require_key(
            edit_quality_check_data,
            "max_retries_on_quality_fail",
            "edit_quality_check",
        ),
        two_stage_recon=two_stage_recon_cfg,
        unified_judge=unified_judge_cfg,
    )
    if (
        not isinstance(edit_quality_check.temp_dir_name, str)
        or not edit_quality_check.temp_dir_name.strip()
    ):
        raise ValueError("edit_quality_check.temp_dir_name must be a non-empty string")
    if edit_quality_check.max_retries_on_quality_fail < 0:
        raise ValueError("edit_quality_check.max_retries_on_quality_fail must be >= 0")

    # ==================== 解析 edit_artifacts 配置 ====================
    edit_artifacts_data = _require_section(data, "edit_artifacts")
    edit_artifacts = EditArtifactsConfig(
        diff_threshold=int(
            _require_key(edit_artifacts_data, "diff_threshold", "edit_artifacts")
        ),
        opening_kernel_size=int(
            _require_key(edit_artifacts_data, "opening_kernel_size", "edit_artifacts")
        ),
    )
    if not (0 <= edit_artifacts.diff_threshold <= 255):
        raise ValueError("edit_artifacts.diff_threshold must be within [0, 255]")
    if edit_artifacts.opening_kernel_size < 1:
        raise ValueError("edit_artifacts.opening_kernel_size must be >= 1")
    if edit_artifacts.opening_kernel_size % 2 == 0:
        raise ValueError("edit_artifacts.opening_kernel_size must be odd")

    # ==================== 解析 Stage1 relabel 配置 ====================
    stage1_relabel_data = _require_section(data, "stage1_relabel")
    stage1_relabel = Stage1RelabelConfig(
        enabled=_require_key(stage1_relabel_data, "enabled", "stage1_relabel"),
        require_rejudge_pass=_require_key(
            stage1_relabel_data, "require_rejudge_pass", "stage1_relabel"
        ),
        allow_image_input=_require_key(
            stage1_relabel_data, "allow_image_input", "stage1_relabel"
        ),
        save_raw_response=_require_key(
            stage1_relabel_data, "save_raw_response", "stage1_relabel"
        ),
    )

    # ==================== 解析渲染配置 ====================
    render_data = _require_section(data, "render")

    semantic_alignment_data = _require_section(render_data, "semantic_alignment")
    semantic_alignment_config = SemanticAlignmentConfig(
        enabled=_require_key(
            semantic_alignment_data, "enabled", "render.semantic_alignment"
        ),
        vlm_model=_require_key(
            semantic_alignment_data, "vlm_model", "render.semantic_alignment"
        ),
        min_confidence=float(
            _require_key(
                semantic_alignment_data, "min_confidence", "render.semantic_alignment"
            )
        ),
        verify_after_rerender=_require_key(
            semantic_alignment_data,
            "verify_after_rerender",
            "render.semantic_alignment",
        ),
        save_aligned_glb=_require_key(
            semantic_alignment_data, "save_aligned_glb", "render.semantic_alignment"
        ),
        aligned_glb_suffix=_require_key(
            semantic_alignment_data, "aligned_glb_suffix", "render.semantic_alignment"
        ),
        save_debug_assets=_require_key(
            semantic_alignment_data, "save_debug_assets", "render.semantic_alignment"
        ),
        temp_dir_name=_require_key(
            semantic_alignment_data, "temp_dir_name", "render.semantic_alignment"
        ),
        normalize_geometry=_require_key(
            semantic_alignment_data, "normalize_geometry", "render.semantic_alignment"
        ),
        share_rotation_to_target=_require_key(
            semantic_alignment_data,
            "share_rotation_to_target",
            "render.semantic_alignment",
        ),
        norm_params_filename=_require_key(
            semantic_alignment_data, "norm_params_filename", "render.semantic_alignment"
        ),
    )
    if semantic_alignment_config.normalize_geometry and not semantic_alignment_config.save_aligned_glb:
        raise ValueError(
            "render.semantic_alignment.normalize_geometry=true requires save_aligned_glb=true"
        )

    blender_data = _require_section(render_data, "blender")
    blender_config = BlenderRenderConfig(
        blender_path=_require_key(
            blender_data, "blender_path", "render.blender", allow_none=True
        ),
        use_bpy=_require_key(blender_data, "use_bpy", "render.blender"),
        device=_require_key(blender_data, "device", "render.blender"),
        samples=_require_key(blender_data, "samples", "render.blender"),
        lighting_mode=_require_key(blender_data, "lighting_mode", "render.blender"),
    )

    # 解析 WebGL 配置
    webgl_data = _require_section(render_data, "webgl")
    webgl_config = WebGLRenderConfig(
        chrome_path=_require_key(
            webgl_data, "chrome_path", "render.webgl", allow_none=True
        ),
        environment_image=_require_key(webgl_data, "environment_image", "render.webgl"),
        shadow_intensity=float(
            _require_key(webgl_data, "shadow_intensity", "render.webgl")
        ),
        use_gpu=_require_key(webgl_data, "use_gpu", "render.webgl"),
        subprocess_timeout_seconds=_require_key(
            webgl_data, "subprocess_timeout_seconds", "render.webgl"
        ),
    )

    render = RenderConfig(
        backend=_require_key(render_data, "backend", "render"),
        image_size=_require_key(render_data, "image_size", "render"),
        rotation_z=_require_key(render_data, "rotation_z", "render"),
        semantic_alignment=semantic_alignment_config,
        blender=blender_config,
        webgl=webgl_config,
    )
    if render.backend not in ("blender", "webgl"):
        raise ValueError(f"Invalid render.backend: {render.backend}")
    if int(render.webgl.subprocess_timeout_seconds) <= 0:
        raise ValueError("render.webgl.subprocess_timeout_seconds must be > 0")
    if not (0.0 <= render.semantic_alignment.min_confidence <= 1.0):
        raise ValueError(
            "render.semantic_alignment.min_confidence must be within [0.0, 1.0]"
        )
    if render.semantic_alignment.vlm_model not in oneapi.text_models:
        raise ValueError(
            "render.semantic_alignment.vlm_model must be a key in oneapi.text_models, "
            f"got: {render.semantic_alignment.vlm_model}"
        )
    if render.semantic_alignment.enabled and float(render.rotation_z) != 0.0:
        raise ValueError(
            "render.rotation_z must be 0 when render.semantic_alignment.enabled is true"
        )

    # ==================== 解析并发配置 ====================
    concurrency_data = _require_section(data, "concurrency")
    gen3d_concurrency_data = _require_section(concurrency_data, "gen3d")
    concurrency = ConcurrencyConfig(
        gen3d=Gen3dConcurrencyConfig(
            hunyuan=_require_key(
                gen3d_concurrency_data, "hunyuan", "concurrency.gen3d"
            ),
            tripo=_require_key(gen3d_concurrency_data, "tripo", "concurrency.gen3d"),
            rodin=_require_key(gen3d_concurrency_data, "rodin", "concurrency.gen3d"),
        ),
        render=_require_key(concurrency_data, "render", "concurrency"),
        text=_require_key(concurrency_data, "text", "concurrency"),
        image=_require_key(concurrency_data, "image", "concurrency"),
        edit_quality_check=_require_key(
            concurrency_data, "edit_quality_check", "concurrency"
        ),
        recon_quality_check=_require_key(
            concurrency_data, "recon_quality_check", "concurrency"
        ),
        refresh_all_dreamsim=_require_key(
            concurrency_data, "refresh_all_dreamsim", "concurrency"
        ),
        mask_backfill=_require_key(concurrency_data, "mask_backfill", "concurrency"),
    )
    if concurrency.render <= 0:
        raise ValueError("concurrency.render must be > 0")
    if concurrency.text <= 0:
        raise ValueError("concurrency.text must be > 0")
    if concurrency.image <= 0:
        raise ValueError("concurrency.image must be > 0")
    if concurrency.edit_quality_check <= 0:
        raise ValueError("concurrency.edit_quality_check must be > 0")
    if concurrency.recon_quality_check <= 0:
        raise ValueError("concurrency.recon_quality_check must be > 0")
    if concurrency.refresh_all_dreamsim <= 0:
        raise ValueError("concurrency.refresh_all_dreamsim must be > 0")
    if concurrency.mask_backfill <= 0:
        raise ValueError("concurrency.mask_backfill must be > 0")

    # ==================== 解析 run_full_experiment 配置 ====================
    run_full_experiment_data = _require_section(data, "run_full_experiment")
    run_full_experiment_retry_data = _require_section(run_full_experiment_data, "retry")

    def _parse_stage_retry(stage_key: str) -> StageRetryConfig:
        stage_data = _require_section(
            run_full_experiment_retry_data,
            stage_key,
        )
        max_attempts = _require_key(
            stage_data,
            "max_attempts",
            f"run_full_experiment.retry.{stage_key}",
        )
        if max_attempts <= 0:
            raise ValueError(
                f"run_full_experiment.retry.{stage_key}.max_attempts must be > 0"
            )
        return StageRetryConfig(max_attempts=max_attempts)

    api_lane_control_data = _require_section(
        run_full_experiment_data, "api_lane_control"
    )
    api_lane_control = RunFullExperimentApiLaneControlConfig(
        enabled=_require_key(
            api_lane_control_data,
            "enabled",
            "run_full_experiment.api_lane_control",
        ),
        cooldown_seconds=_require_key(
            api_lane_control_data,
            "cooldown_seconds",
            "run_full_experiment.api_lane_control",
        ),
        recovery_probe_one_by_one=_require_key(
            api_lane_control_data,
            "recovery_probe_one_by_one",
            "run_full_experiment.api_lane_control",
        ),
    )
    if api_lane_control.cooldown_seconds < 0:
        raise ValueError(
            "run_full_experiment.api_lane_control.cooldown_seconds must be >= 0"
        )

    scheduling_data = _require_section(run_full_experiment_data, "scheduling")
    scheduling = RunFullExperimentSchedulingConfig(
        object_workers_strategy=_require_key(
            scheduling_data,
            "object_workers_strategy",
            "run_full_experiment.scheduling",
        ),
        object_workers_cap=_require_key(
            scheduling_data,
            "object_workers_cap",
            "run_full_experiment.scheduling",
        ),
        provider_pressure_divisor=_require_key(
            scheduling_data,
            "provider_pressure_divisor",
            "run_full_experiment.scheduling",
        ),
    )
    if scheduling.object_workers_strategy not in ("provider_weighted",):
        raise ValueError(
            "run_full_experiment.scheduling.object_workers_strategy must be 'provider_weighted'"
        )
    if scheduling.object_workers_cap <= 0:
        raise ValueError(
            "run_full_experiment.scheduling.object_workers_cap must be > 0"
        )
    if scheduling.provider_pressure_divisor <= 0:
        raise ValueError(
            "run_full_experiment.scheduling.provider_pressure_divisor must be > 0"
        )

    run_full_experiment = RunFullExperimentConfig(
        retry=RunFullExperimentRetryConfig(
            source_prompt_optimization=_parse_stage_retry("source_prompt_optimization"),
            source_t2i=_parse_stage_retry("source_t2i"),
            source_gen3d=_parse_stage_retry("source_gen3d"),
            source_render=_parse_stage_retry("source_render"),
            instruction_generation=_parse_stage_retry("instruction_generation"),
            edit_apply=_parse_stage_retry("edit_apply"),
            stage1_quality_check=_parse_stage_retry("stage1_quality_check"),
            target_gen3d=_parse_stage_retry("target_gen3d"),
            target_render=_parse_stage_retry("target_render"),
            stage2=_parse_stage_retry("stage2"),
        ),
        api_lane_control=api_lane_control,
        scheduling=scheduling,
    )

    # ==================== 解析默认配置 ====================
    defaults_data = _require_section(data, "defaults")
    defaults = DefaultsConfig(
        poll_interval=_require_key(defaults_data, "poll_interval", "defaults"),
        max_wait_time=_require_key(defaults_data, "max_wait_time", "defaults"),
    )

    # ==================== 解析工作区配置 ====================
    workspace_data = _require_section(data, "workspace")
    workspace = WorkspaceConfig(
        pipeline_dir=_require_key(workspace_data, "pipeline_dir", "workspace"),
        python_interpreter=_require_key(
            workspace_data, "python_interpreter", "workspace"
        ),
        playwright_browsers_path=_require_key(
            workspace_data, "playwright_browsers_path", "workspace"
        ),
        logs_dir=_require_key(workspace_data, "logs_dir", "workspace"),
        pipeline_index_db=_require_key(workspace_data, "pipeline_index_db", "workspace"),
        matrix_objects_file=_require_key(
            workspace_data, "matrix_objects_file", "workspace"
        ),
        matrix_styles_file=_require_key(
            workspace_data, "matrix_styles_file", "workspace"
        ),
        draco_library_path=workspace_data.get("draco_library_path", ""),
        draco_decoder_dir=workspace_data.get("draco_decoder_dir", ""),
    )

    # ==================== 解析导出配置 ====================
    export_data = _require_section(data, "export")
    export = ExportConfig(
        path_prefixes=_require_key(export_data, "path_prefixes", "export"),
    )

    return Config(
        oneapi=oneapi,
        tripo=tripo,
        rodin=rodin,
        tasks=tasks,
        edit_quality_check=edit_quality_check,
        edit_artifacts=edit_artifacts,
        stage1_relabel=stage1_relabel,
        render=render,
        concurrency=concurrency,
        run_full_experiment=run_full_experiment,
        defaults=defaults,
        workspace=workspace,
        export=export,
        language=_require_key(data, "language", "root"),
        download_proxy=data.get("download_proxy"),
        _raw_data=data,
    )


def validate_api_keys(config: Config) -> list[str]:
    """
    检查 API keys 是否正确配置

    Returns:
        警告列表
    """
    warnings = []

    if "your_" in config.tripo.api_key.lower():
        warnings.append("Tripo API key is not configured")
    if "your_" in config.rodin.api_key.lower():
        warnings.append("Rodin API key is not configured")

    return warnings
