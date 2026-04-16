# Changelog

All notable changes to the **2D-to-3D Pipeline (v2)** will be documented in this file.

## [v2.10.10] - 2026-04-13

### Fixed

#### 1. Stage2 API 错误不再被永久 ABORT，允许重试

- `scripts/run_full_experiment.py`
  - `_stage2_failure_payload()` 现在区分 API 执行错误（VLM 超时/5xx）和质量判定失败：
    - `error_quality_check`（API 错误）→ `terminal: False`，允许 stage-level 重试
    - `failed_quality`（VLM 判定不一致）→ `terminal: True`，立即 ABORT
  - 此前所有 Stage2 失败一律 `terminal: True`，导致 API 超时的 edit 被永久 ABORT（prompt-improve-test03 中有 20 条因此丢失）

### Added

#### 2. Models 页面新增 Stage2 Passed badge

- `app.py`
  - `_load_model_payload_by_id_live()` 新增 `stage2_passed_count` 字段：统计 `target_quality_checks_by_provider` 中 status=="passed" 的 edit 数
- `templates/models.html`
  - model card 在 "N Target 3D" badge 后显示绿色 "N Passed" badge（count > 0 时）

#### 3. Models 页面 Category 筛选改为后端批量加载

- `templates/models.html`
  - 新增 `getCategoryFilteredIds()` 和 `loadCategoryPriorityModels()` 函数
  - `setCategoryFilter()` 选中具体 category 时，从 TOC 筛出匹配的 model ID，通过 `/api/models/batch` 批量加载（与 YAML 筛选相同策略）
  - 修复：此前 category 筛选为纯客户端 show/hide，infinite scroll 分页不感知过滤器，导致选中某 category 后只显示少量 model 且无法加载更多

#### 4. Remove 指令新增 Surface Closure 规则

- `utils/prompts.py`
  - `INSTRUCTION_REMOVE_PROMPT` 和 `INSTRUCTION_ADAPTIVE_K_PROMPT` 的 REMOVE RULES 新增水密性约束：对封闭表面物体（容器、瓶子、箱体等），禁止移除密封部件（盖子、瓶盖、门板等），避免生成的 3D 模型出现开口/非水密
  - 更新 good/bad examples（"Remove the lid" 从 good 移到 bad）

### Changed

#### 4. Source 渲染跳过二次渲染，改用视图重映射

- `scripts/run_render_batch.py`
  - 新增 `_remap_views()` 函数：根据 VLM 语义决策，将 first-pass 渲染的 6 视图直接重映射为最终视图（侧视图重命名 + top/bottom 图像旋转），替代原有的二次 WebGL 渲染
  - Source + `normalize_geometry=true` 路径：当 `semantic_front_from` 为 front/back/left/right 时走 remap，为 top/bottom 时 fallback 到原有二次渲染
  - 预期将 source 渲染耗时从 ~286s 降至 ~150s（省去一次 Chromium 启动 + GLB 加载 + 6 视图渲染）
- `scripts/bpy_align_standalone.py`
  - `_compute_bbox()` 返回值扩展为 `(center, max_dim, dims)` 三元组
  - `--normalize` 模式下，归一化完成后直接从 aligned GLB 的包围盒计算 `webgl_safe_radius` 和 `webgl_center`（公式与 `webgl_script.py` JS 实现一致），写入 `norm_params.json`，不再依赖二次渲染提取这些参数

## [v2.10.9] - 2026-04-12

### Added

#### 1. Matrix Generation — 确定性 object+style 配对批量生成

- `scripts/run_full_experiment.py`
  - `CategoryPlan` 新增 `style_ids: Optional[List[str]]`，与 `objects` 一一对应
  - `ObjectJob` 新增 `style_id: Optional[str]`，透传到 `PromptOptimizer`
  - 向后兼容：不带 `style_ids` 的旧 YAML 行为不变
- `scripts/generate_matrix_pairs.py`
  - 从 objects JSON + styles JSON 生成有序配对列表（17,224 对）
  - 按 round-robin 展开：Round 0 全部 object × realistic，Round 1 × lowpoly，以此类推
  - 每个 category 的 style 优先级按 `(-weight, -coverage)` 排序
- `scripts/run_matrix_batch.py`
  - 从 `matrix_pairs.json` 取指定范围 → 按 category 分组 → 生成 YAML plan（含 `style_ids`）→ 启动 `run_full_experiment.py`
  - 支持 `--dry-run` 预览 YAML 不执行

#### 2. Matrix Generation 前端

- `app.py`
  - `GET /api/matrix/pairs-status`：返回配对列表元数据（总对数、轮次分布）
  - `POST /api/matrix/generate-plan`：从配对列表生成 YAML plan + 标准 nohup CLI 命令
- `templates/matrix_generation.html`
  - 三区块布局：Pair List Status / Run Matrix Batch / Resume Interrupted Experiment
  - Run Matrix Batch：填 start index、count、provider、edits → 生成标准 nohup CLI（与 Batch Generation 一致）
  - Resume：下拉选择实验 → 生成 resume CLI

## [v2.10.8] - 2026-04-11

### Fixed

#### 1. Web UI batch 加载容错

- `app.py`
  - `_ensure_target_render_grid`: catch `OSError`，当 OSS FUSE 报 `Device or resource busy` 时返回 None 优雅降级，不再崩溃整个 batch 请求
  - `api_get_models_batch`: 单个 model 加载异常时 `continue` 跳过，不影响 batch 中其他 model。修复 YAML 筛选时部分 model 失败导致后续 model 全部丢失的问题

#### 2. gen3d VLM top-as-front 判断 max_tokens 截断

- `scripts/gen3d.py`
  - `_vlm_judge_top_as_front` 的 `max_tokens` 从 200 提高到 1000，修复 VLM 返回的 JSON 响应被截断导致解析失败

### Added

#### 3. Stage2 一致性检查支持 3-image VLM 模式

- `core/render/recon_consistency_checker.py`
  - `VLMReconConsistencyChecker.check()` 新增 `source_views_dir` 参数
  - 当提供源模型视角目录时，将源渲染图拼接为 grid 一并传给 VLM，使其同时看到 编辑前 / 编辑参考 / 重建结果 三张图来判断一致性
- `scripts/batch_process.py`
  - `check-target-consistency` 命令调用 checker 时自动传入 `source_views_dir`

#### 4. Matrix Generation 页面 + 物体池筛选脚本

- `config/config.py`: 新增 `matrix_objects_file`、`matrix_styles_file` 配置项
- `templates/base.html`: 导航栏增加 Matrix Generation 入口
- `templates/matrix_generation.html`: 新增 Matrix Generation 页面
- `scripts/filter_objects_by_editability.py`: 基于 VLM 评估物体 3D 可编辑性并筛选
- `scripts/export_removed_objects.py`: 导出被筛除物体清单
- `scripts/_analyze_removed.py`: 分析被移除物体的辅助脚本

#### 5. 数据 manifest 生成脚本

- `scripts/generate_data_manifest.py`: 从实验日志生成 Stage2 通过的 triplet 清单 JSON，包含 source/target GLB (raw + aligned)、六视角渲染图、编辑图、mask 等全部资产路径

## [v2.10.7] - 2026-04-10

### Changed

#### 1. 语义对齐：关闭二次验证 + 渲染重试次数改为 1

- `config/config.yaml`
  - `render.semantic_alignment.verify_after_rerender`: `true` → `false`
    - 第一遍 VLM 判定正面方向后旋转模型并重新渲染，不再做第二遍 VLM 验证
    - 原因：两次 VLM 调用看到不同视觉输入，对形状歧义物体（faucet、horse buggy 等）经常产生矛盾判定，导致 source_render 永久失败
  - `run_full_experiment.retry.source_render.max_attempts`: `3` → `1`
    - 去掉二次验证后，渲染失败不再是 VLM 一致性问题，无需重试

#### 2. 语义对齐 VLM prompt 重构：按形状分类 + 双原则优先级

- `core/render/semantic_view_aligner.py`
  - 新增两个核心判定原则（按优先级）：
    1. **功能朝向（Functional facing）**：物体有明确"功能方向"时，该方向为正面（如手枪枪口、相机镜头）
    2. **最大可见面积（Maximum visibility）**：否则选展示最大特征面积的视角
  - 新增 7 类形状规则及示例：
    1. 细长形（violin, syringe）→ 侧面宽展面，绝不选窄端截面
    2. 有指向的（gun, camera）→ 指向用户的方向
    3. 盘状/容器（cake, cup, vase）→ top=俯视开口/圆面，front=侧面轮廓
    4. 有用户面的（faucet, vending machine）→ 使用时面对的那面
    5. 车辆（car, bus）→ 行驶方向
    6. 座椅/家具（chair, sofa）→ 坐下时面对的面
    7. 对称物体（ball, cube）→ 有特征选特征面，否则默认 front
  - 任务步骤要求 VLM 先识别物体类型、选规则、再判定

### Added

- `plans/analysis/2026-04-10_new-stage2-test02_result_analysis.md` — new-stage2-test02 实验完整分析

## [v2.10.6] - 2026-04-09

### Added

#### 1. UCO3D taxonomy 扩展：unmatched bases 分类 + 合并（Step B & C）

- 对 Step A 遗留的 184 个 unmatched base objects，通过 `classify_unmatched_bases.py` 批量分配 super_category（GPT-5 thinking），产出 `unmatched_bases_classification.json`
- 用 `clean_uco3d_taxonomy_gpt5.py --base-override` 对 184 个 base 的 UCO3D 候选做 4-shard LLM 细类筛选（v3），产出 v3 shard clean/review/stats/judgments 文件
- 修复 shard 2 崩溃：`sugarcane plant` 被 override 分到不存在的 `Plants` 超类，就地 patch 为 `Food & Beverage`
- 用 `merge_uco3d_taxonomy.py` 合并 `categorized_objects_new2.json` + v3 shards → `categorized_objects_final.json`（2640 + 179 = 2819 entries）
- 人工 review 39 条 review queue，新增 7 条规范名（jalapeño pepper, disposable/safety razor blade, cocker spaniel, corgi, pomeranian, buick lucerne）→ 2826 entries

#### 2. 人形对象扩充（+127 条）

- GPT-5 头脑风暴生成人形/类人形物体候选（661 条），经两轮去重精选至 127 条
- 覆盖：玩偶（fashion doll, ball jointed doll, amigurumi...）、action figure（astronaut, knight, samurai...）、figurine（ballerina, geisha, pharaoh...）、puppet（marionette, bunraku, shadow puppet...）、文化特色（matryoshka, kokeshi, daruma, kachina, dia de muertos...）、mannequin（store, artist, dress form）
- 按 taxonomy 风格分入 `Sports, Hobbies & Recreation`（57 条）和 `Decor, Art & Religion`（70 条）
- `categorized_objects_final.json` 最终 2953 entries，全部唯一（0 跨超类重复，0 超类内重复）

#### 3. 新增 `Animals` 超类 style mapping

- `data/captions/3d_style_final.json`：新建，与 `categorized_objects_final.json` 配套
  - 新增 `Animals` 的 `category_style_mapping`：realistic, lowpoly, voxel, cartoon, wooden, fantasy, artifact, chinese, japanese（9 种）
  - 其余 19 个超类 style mapping 与 `3d_style.json` 保持一致
  - 20 个超类全覆盖，0 缺失

**产出文件**（尚未启用，需要将代码中 `OBJECTS_FILE` / `STYLES_FILE` 指向 `_final` 文件后生效）：
- `data/captions/categorized_objects_final.json` — 20 super_categories, 2953 objects
- `data/captions/3d_style_final.json` — 14 styles, 20 super_categories

**容量**：18,639 个 source model 组合（object × style），74,556 个编辑对（× 4 edits/model）

## [v2.10.5] - 2026-04-09

### Added

#### 1. Draco mesh compression for aligned GLB — 加速 target_render WebGL 加载

- `config/config.yaml`
  - workspace 新增 `draco_library_path` 和 `draco_decoder_dir`（留空表示不启用，与旧行为一致）

- `config/config.py`
  - `WorkspaceConfig` 新增 `draco_library_path`、`draco_decoder_dir` 字段

- `scripts/bpy_align_standalone.py`
  - `export_scene.gltf()` 新增 `export_draco_mesh_compression_enable=True`（仅当 `BLENDER_EXTERN_DRACO_LIBRARY_PATH` env var 存在时启用）

- `scripts/run_render_batch.py`
  - `_run_alignment_subprocess()` 新增 `draco_library_path` 参数，注入子进程环境变量

- `scripts/webgl_render.py`
  - `run_webgl_render()` 在启动 HTTP server 前将 Draco decoder 文件复制到 GLB 同目录

- `core/render/webgl_script.py`
  - model-viewer HTML 注入 `dracoDecoderLocation`，指向 GLB 同目录的本地 decoder

- `static/js/draco/`
  - 新增 `draco_decoder.js`、`draco_decoder.wasm`、`draco_wasm_wrapper.js`（来自 Blender 4.2 官方 Draco 1.5.6）

**效果**：
- aligned GLB 从 ~55MB 降至 ~30MB（mesh 部分压缩比 ~10:1，纹理不受影响）
- target_render 第二次 WebGL 加载时间显著缩短
- 无 Draco 配置的环境（如 qh_4_4090）行为完全不变

## [v2.10.4] - 2026-04-08

### Fixed

#### 1. run_full_experiment 在 instruction_generation 失败时丢失 source_model_id，导致 YAML 筛选显示 `0 models`

- `scripts/run_full_experiment.py`
  - `_run_object_job()`：source pipeline 一旦成功返回，就立即把 `prompt_context_local` 同步到外层 `prompt_context`，避免后续 instruction_generation / edit pipeline 异常时上下文丢失
  - `_build_failed_object_record()`：失败对象记录新增从 `error.source_record` 回填 `prompt_id`、`image_id`、`source_model_id`
  - `source_pipeline_status` 改为根据回填后的 `source_model_id` 判断，不再把source 已成功但 instruction_generation 失败的对象误记为 source pipeline failed

**根因**：
- source pipeline 成功后，`prompt_context_local` 只存在于 `_inner()` 局部变量里
- 如果随后 `instruction_generation` 抛错，外层 `prompt_context` 还来不及赋值
- 失败记录因此被写成 `source_model_id=null`
- Models 页的 YAML filter 依赖 `object_records.jsonl` 建立 YAML -> model 关联，于是出现 `1 runs, 0 models`

**结果**：
- 像 `2fb3ec104198` 这种source model 已生成，但 instruction_generation 失败的对象，仍会被正确关联到对应 YAML
- Models 页 YAML 筛选统计不再漏掉已成功生成的 source model

#### 2. adaptive instruction generation 对 fenced JSON 响应兼容性不足，导致 instruction_generation 误失败

- `core/image/caption.py`
  - `_parse_adaptive_instruction_response()` 改为两阶段解析：先严格 `json.loads()`，失败后自动清洗模型输出再重试
  - 新增 `_clean_adaptive_instruction_response()`：自动去掉 ```json ... ``` Markdown code fence，以及 `json\n` 前缀
  - 新增 `_extract_first_json_object()`：当模型在 JSON 前后夹带少量说明文字时，提取首个完整 JSON object 再解析

- `utils/prompts.py`
  - `INSTRUCTION_ADAPTIVE_K_PROMPT` 的 RETURN FORMAT 区块进一步加强：
    - 输出必须以 `{` 开始、以 `}` 结束
    - 明确禁止 ```json / ``` Markdown code fence
    - 明确禁止在 JSON 前后附加解释性文字

**根因**：
- `gemini-3.1-pro-preview` 在 adaptive instruction generation 中有时会返回 fenced JSON（例如 ```json ... ```）
- 旧 parser 只接受裸 JSON 字符串，直接 `json.loads(response_text)`，没有做任何清洗
- 导致内容本身正确但格式略有偏差时，`instruction_generation` 仍被误判为失败

**结果**：
- instruction generation 对常见的 fenced JSON / 轻微前后缀噪声更稳健
- 同时保留 Fail Loudly：如果清洗后仍不是合法 JSON，依然会显式报错

### Changed

#### 3. stage2 一致性校验切换为 VLM 方法

- `config/config.yaml`
  - `edit_quality_check.two_stage_recon.stage2_method`: `lpips` -> `vlm`

## [v2.10.3] - 2026-04-07

### Fixed

#### 1. Models 页面误把 target/pair 目录当成 source model 展示

- `utils/pipeline_index.py`
  - `PipelineIndex._reconcile_models()` 新增过滤：跳过 `*_edit_*` 目录，不再把 target 3D / pair 目录写入 source-model 索引
  - `_SCHEMA_VERSION`: `3` -> `4`，强制重建 SQLite index，清理历史上已经错误写入的 `source_model_id_edit_edit_id` 记录

**根因**：
- `app.py` 的直接文件扫描路径本来只展示 source model，会显式跳过 `"_edit_"` 目录
- 但 Models 页面分页优先走 SQLite `pipeline_index`
- `pipeline_index` 旧逻辑只检查“目录内是否存在 `.glb`”，没有排除 `*_edit_*`，导致 target model 也进入索引

**结果**：
- `/models` 页面恢复只展示 source model
- 形如 `875f623a0c31_edit_0ff26206` 的 target/pair 卡片不再混入模型列表

## [v2.10.2] - 2026-04-07

### Changed

#### 1. Prompt 优化与模型切换（instruction generation / multiview edit / quality check）

- `config/config.yaml`
  - `tasks.text_generation.model`: `gpt-5` -> `gemini-3.1-pro-preview`
  - `tasks.edit_quality_check_unified.model`: `gemini-3-flash-preview` -> `gemini-3.1-pro-preview`
  - 新增 `tasks.target_consistency_judge`，默认使用 `gemini-3.1-pro-preview`
  - `tasks.multiview_editing.guardrail_prompt.version`: `mv_guardrail_v1` -> `mv_guardrail_v2`
  - `edit_quality_check.unified_judge` 新增 `require_non_weak_evidence`
  - `edit_quality_check.two_stage_recon` 新增 `stage2_method` 与 `vlm_recon.*`

- `config/config.py`
  - `GuardrailPromptConfig.text` 改为可选字段；`load_config()` 只强制要求 `version`
  - 新增 `VlmReconConfig`、`TwoStageReconConfig.stage2_method`、`TwoStageReconConfig.vlm_recon`
  - 新增 `Config.target_consistency_judge_mllm`
  - `unified_judge.require_non_weak_evidence` 纳入 Fail Loudly 解析

- `utils/prompts.py`
  - `PROMPT_OPTIMIZER_SYSTEM`、`IMAGE_REQUIREMENTS_PROMPT` 增强单主体约束，显式禁止 props / decorations / accessories / 额外 scene elements
  - `INSTRUCTION_REMOVE_PROMPT`、`INSTRUCTION_REPLACE_PROMPT`、`INSTRUCTION_ADAPTIVE_K_PROMPT` 全面强化 multiview-safe 与合法性约束：
    - 禁止 whole-object / main-body 编辑
    - 禁止 logo / label / seam line / material-only / texture-only 指令
    - Remove 新增“不得移除唯一连接/支撑件”约束
    - Replace 新增“多实例部件禁止拓扑变化”和“替换前后差异必须足够明显”约束
  - 新增 `UNIFIED_JUDGE_SYSTEM_PROMPT` / `UNIFIED_JUDGE_USER_PROMPT_TEMPLATE`，Method-3 统一判定 prompt 收口到公共 prompt 模块
  - 新增 `STAGE2_VLM_RECON_PROMPT`，用于 target 3D 重建一致性的 VLM 判定
  - 新增 `MULTIVIEW_GUARDRAIL_V2` 与 registry，multiview guardrail 文案统一维护在 `prompts.py`

- `utils/prompt_guardrail.py` / `core/image/multiview_editor.py`
  - multiview edit guardrail 改为通过 `version` 从 `utils/prompts.py` registry 解析，不再要求在 `config.yaml` 内内联完整 prompt
  - task context prompt 进一步强调：部分可见视角也必须做对应编辑，完全不可见视角保持不变

- `core/image/caption.py`
  - instruction generation 与 adaptive instruction generation 统一接入 `validate_instruction_legality()`
  - 生成到非法指令时不再静默接受或弱化 fallback，而是继续重试；多次失败后直接显性报错
  - rewrite prompt 明确禁止材质替换、整物替换、表面标记编辑

- `core/image/edit_quality_checker_unified.py`
  - unified_judge 改为引用 `utils/prompts.py` 中统一维护的 system/user prompt
  - legality 校验从 `validate_instruction_text()` 升级为 `validate_instruction_legality()`

- `core/render/recon_consistency_checker.py` / `scripts/batch_process.py`
  - Stage2 reconstruction consistency 新增 `VLMReconConsistencyChecker`
  - `batch_process` 按 `edit_quality_check.two_stage_recon.stage2_method` 自动切换 LPIPS / VLM checker
  - VLM 模式会将 edit instruction 一并传入评估 prompt，输出兼容现有 target quality result schema

### Impact

- source prompt 更严格，生成带额外装饰物/陪体的 source image 概率进一步降低
- instruction generation 更保守，非法或多视角不安全的 Remove/Replace 指令会更早失败而不是流入后续 pipeline
- multiview editing guardrail 维护方式更清晰：配置只选版本，完整 prompt 只在 `utils/prompts.py` 单点维护
- text generation、unified_judge、Stage2 VLM consistency check 现在默认走 `gemini-3.1-pro-preview`
- Stage2 consistency check 保持兼容：默认仍可继续使用 LPIPS，切到 `stage2_method: "vlm"` 时才启用新的 VLM 路径

## [v2.10.1] - 2026-04-06

### Fixed

#### 1. target 模型渲染视角偏上（`bpy_align_standalone.py` 几何中心计算错误）

- `scripts/bpy_align_standalone.py`
  - **根因**：`--norm-center-from` 路径（对应 `share_rotation_to_target=false`）中，target 完成自身旋转后直接套用了 source 旋转后的 bbox center 做平移。由于 source 与 target 旋转矩阵不同（如 source 旋转 90°，target 用单位矩阵），source 的 center 偏移量在 target 坐标系中无效，导致 target 几何中心偏离原点（实测偏移 ~0.148 in Y），WebGL 相机对准 `webgl_center=[0,0,0]` 时物体出现在画面顶部
  - **修复**：`--norm-center-from` 模式下，在 target 应用自身旋转后重新计算 target 自己的 bbox center，以该 center 做平移归一（而非套用 source center）；仍借用 source 的 `max_dim` 保持尺寸一致。`--norm-from` 路径（`share_rotation=true`）不受影响，source center 仍可复用

#### 2. unified_judge Step 1 禁止用领域知识合理化视角不一致

- `utils/prompts.py`
  - `UNIFIED_JUDGE_USER_PROMPT_TEMPLATE` Step 1 矛盾检查部分重写
  - 新增明确禁止：`Do not use real-world knowledge about how this type of object normally looks to justify why a view shows no change`
  - 将 fail 条件从 "absent in one view but present in another" 改为更直接的几何可见性判断：`the edited region is geometrically visible from a view but that view shows no change`
  - 根因：实验案例 `986b1d75728c / 7033cccc`，指令为填补车轮中央开口，back 视角应同样封盖但未被修改，judge 以 "hub caps are typically single-sided" 为由判 pass

#### 3. unified_judge Step 1 禁止用 EDIT MASK 推断视角状态

- `utils/prompts.py`
  - `UNIFIED_JUDGE_USER_PROMPT_TEMPLATE` Step 1：present/absent/not_visible 定义中明确加入 "confirmed by the AFTER image"
  - 新增 IMPORTANT 约束：每个视角的标注必须基于 AFTER 图像的实际视觉内容，不得用 EDIT MASK 推断或猜测；mask 可能包含噪声、周边渲染差异等不代表目标编辑的像素
  - 根因：同一案例中 back 视图 mask 有白色像素，LLM 据此推断 "likely filling the hole from the other side" 并判 pass

### Changed

#### 4. unified_judge prompt 明确 EDIT MASK 仅作空间定位参考

- `utils/prompts.py`
  - `UNIFIED_JUDGE_USER_PROMPT_TEMPLATE` 头部 Image 3 描述重写：明确 mask 是 "pixel-level difference map for spatial reference only"，禁止用于判断变化是否发生或编辑是否正确执行
  - Step 2 Observation：将 "Use the EDIT MASK as supporting evidence" 改为 "Use the EDIT MASK only as spatial reference — to identify where changes are located and whether the mask extends clearly beyond the target area"
  - 至此 mask 在整个 prompt 中的合法用途统一为两类：①定位变化所在区域，②检测过度编辑（mask 超出目标范围）

#### 5. unified_judge prompt 新增过度编辑检查（over-edit scope check）

- `utils/prompts.py`
  - `UNIFIED_JUDGE_USER_PROMPT_TEMPLATE` Step 2 (Observation)：追加 `Also note if the EDIT MASK covers areas clearly outside the target part.`
  - Step 4 (Instruction Following and Evidence)：
    - pass 条件新增第 4 项：`the changes are reasonably confined to the target area`
    - 新增 fail 条件：若与目标无关的、空间上分离的其他部件被明显修改，则 fail；例外：编辑的直接结构性后果（相邻区域平滑、支撑结构随部件移除而坍塌等）允许
  - 根因：此前 prompt 不检查"改了不该改的地方"，图片编辑模型对 Remove/Replace 指令有时会过度修改整体形状或其他部件，但 instruction_following 仍会 pass

#### 6. 编辑生成 prompt 全面加固（mv_guardrail_v2 + task_context + 指令约束）

- `config/config.yaml`
  - `tasks.multiview_editing.guardrail_prompt.version`: `mv_guardrail_v1` → `mv_guardrail_v2`
  - 新增禁止过度编辑、字面执行、Replace 风格匹配、防光照飘移等规则
  - 不可见视角约束：`keep that view unchanged` → `keep that view pixel-identical to the input`
  - 物理自洽约束：`The edited result must be physically plausible. Do not produce results where parts of the object are disconnected, floating, or structurally unsupported as a consequence of the edit.`

- `core/image/multiview_editor.py`
  - `task_context_prompt`：明确"部分可见视角也要改"和"完全不可见视角保持不变"

- `utils/prompts.py`
  - `INSTRUCTION_REPLACE_PROMPT` 新增两条 IMPORTANT 规则：
    1. **Shape complexity**：多实例部件（多根杆、多条腿、多块面板）禁止拓扑变化，只允许单实例部件做拓扑替换
    2. **Visual difference**：替换前后必须有明显的轮廓/形状差异，轻微变化不允许
  - Examples 全部更新，移除拓扑复杂替换示例，新增反例
  - `INSTRUCTION_REMOVE_PROMPT` 新增规则：不得移除作为其他部件唯一连接件或支撑件的部分，新增反例

#### 7. source image prompt 禁止生成额外装饰物

- `utils/prompts.py`
  - `IMAGE_REQUIREMENTS_PROMPT`：末尾追加 `only the single subject object, no other objects, no props, no decorations, no accessories, no scene elements.`
  - `PROMPT_OPTIMIZER_SYSTEM` Forbidden Elements 区块：新增 `NO other objects, props, decorations, toys, or accessories alongside the subject`
  - 根因：部分 source image 中出现积木、摆件等装饰物，导致 3D model 将其一并重建

### Added

#### 8. 渲染对比测试工具

- `tests/compare_render_views.py`
  - 新增脚本：输入 `model_id` + `edit_id`，生成编辑前后 6 视角并排对比图
  - 输出到 `tests/data/test_data/compare_{model_id}_edit_{edit_id}/`：每视角单独一张 + 全视角汇总 `comparison.png`
  - 用途：用于验证渲染对齐修复效果及目视检查 source/target 视角一致性

## [v2.9.32] - 2026-04-05

### Changed
#### unified_judge prompt 重构为 5 步，以视角一致性作为 hard gate

- `utils/prompts.py`
  - `UNIFIED_JUDGE_USER_PROMPT_TEMPLATE` 从 7 步重构为 5 步
  - **Step 1（Hard Gate）**：Per-View Consistency Check — 要求 LLM 对每个 AFTER 视角逐一标注 `present / absent / not_visible`，并检查跨视角矛盾；放置在最前作为 hard gate，防止 LLM 在形成结论后对 view_sanity 产生确认偏差
  - **Step 2**：Observation — 基于 Step 1 描述整体变化，使用 EDIT MASK 作为辅助证据
  - **Step 3**：Instruction Legality — 沿用原 Step 3 逻辑（structural_part / appearance_only / material_only / main_body / unclear）
  - **Step 4**：Instruction Following and Evidence — 合并原 Step 4 + Step 6，统一判断 instruction_following、evidence_strength、supporting_views
  - **Step 5**：Relabel — 沿用原 Step 7 逻辑
  - 删除原冗余 Step 2（与 Step 4 重复的"mask noise"约束已移入 Step 4）
  - `view_sanity.reason` JSON 字段描述从 "explain whether the AFTER views are geometrically self-consistent" 改为 "summarize per-view consistency results and any contradictions found"
  - 根因：多条多视角不一致的 edit 通过了 Stage1 检查（正面视角保留部件、侧面视角消失等），根因是 view_sanity 在 Step 5 执行时 LLM 已形成结论导致确认偏差；重构后强制 LLM 先完成逐视角枚举再做其他判断

## [v2.9.30] - 2026-04-05

### Fixed
#### unified_judge prompt 补充 supporting_views 格式约束，修复 execution_error bug

- `utils/prompts.py`
  - Step 6 说明中新增：`Use view label strings (front / back / left / right / top / bottom), not numeric indices.`
  - JSON 模板中将 `"supporting_views": []` 改为含示例值及注释的形式：`"supporting_views": ["front", "left"],  // view label strings: front / back / left / right / top / bottom`
  - 根因：`gemini-3-flash-preview` 有时将 3×2 grid 格子理解为数字索引，返回 `[0, 1, 2, 3, 4]` 等整数列表；校验代码 `isinstance(0, str)` 为 False，抛出 `unified_judge: supporting_views[0] must be a non-empty string`，导致 Stage1 quality_check execution_error（20260404 实验中 6 条）

## [v2.9.29] - 2026-04-04

### Fixed
#### playwright_browsers_path 移至 data-koolab-nas，修复 WebGL 渲染 libnspr4.so 缺失问题

- `config/config.yaml`（server 端）
  - `workspace.playwright_browsers_path` 从 `/data-oss/.../code3/2d3d_v2/.playwright-browsers` 改为 `/data-koolab-nas/xiaoliang/code3/2d3d_v2/.playwright-browsers`
  - 根因：OSS FUSE 挂载不支持加载 shared library，导致 Chromium 启动时 `libnspr4.so: No such file or directory`，WebGL 渲染全部失败

#### utils/logger.py FileHandler 写入路径改为读 config.yaml 的 workspace.logs_dir

- `utils/logger.py`
  - 删除模块级硬编码 `LOG_DIR = Path(__file__).parent.parent / "logs"`
  - 新增 `_get_log_dir()` 懒加载函数：首次调用时从 `config.workspace.logs_dir` 读取路径（即 `data-koolab-nas`），config 加载失败时 fallback 到相对路径
  - 根因：FileHandler JSONL 日志写到 OSS，多线程并发 flush 触发 `OSError: [Errno 16] Device or resource busy`，产生大量日志噪音

## [v2.9.28] - 2026-04-03

### Fixed
#### run_full_experiment target_gen3d 阶段全部 ABORT 的 bug

- `scripts/run_full_experiment.py`
  - `_run_target_gen3d_once()` 中将 `_, target_gen_payload = self._run_in_lane(...)` 改为 `target_gen_payload = self._run_in_lane(...)`
  - 根因：v2.9.26 将 `gen3d_from_edit_single()` 返回值从 2-tuple 改为 4-key Dict（含 `target_gen3d_error_class` / `target_gen3d_error_message`），但 `run_full_experiment.py` 的解包代码未同步，导致 `ValueError: too many values to unpack (expected 2)`
  - 3D 模型实际已生成成功（GLB 已下载落盘），但 stage 被误判为 failed 并 ABORT

## [v2.9.27] - 2026-04-03

### Added
#### Stage2 VLM 一致性检测方法（可与 LPIPS 切换）

- `config/config.yaml`
  - `tasks` 新增 `target_consistency_judge`（provider/model 可配置，默认 gemini-3-flash-preview）
  - `edit_quality_check.two_stage_recon` 新增字段：
    - `stage2_method: "lpips" | "vlm"`（默认 `"lpips"`，不改配置则行为不变）
    - `vlm_recon.pass_threshold`：VLM 置信度阈值（`use_confidence=true` 时生效）
    - `vlm_recon.use_confidence`：false=只看 pass 字段；true=还检查置信度

- `config/config.py`
  - 新增 `VlmReconConfig` dataclass
  - `TwoStageReconConfig` 新增 `stage2_method`、`vlm_recon` 字段
  - `load_config()` 解析并校验新字段；`stage2_method="vlm"` 时强制要求 `vlm_recon` 和 `tasks.target_consistency_judge`
  - `Config` 新增 `target_consistency_judge_mllm` property

- `utils/prompts.py`
  - 新增 `STAGE2_VLM_RECON_PROMPT` 常量：双图（参考六视角网格 + 渲染六视角网格）+ 指令文字 → JSON 判断

- `core/render/recon_consistency_checker.py`
  - `TargetQualityCheckResult` 新增可选字段 `vlm_reason`（VLM 文字理由，仅 vlm 方法输出）
  - 新增 `VLMReconConsistencyChecker` 类：拼 3×2 网格、读 instruction、调 VLM、解析 JSON、返回兼容 schema
  - `metric="vlm"` 时 `score` 存置信度，`scores_by_view={}`, `aggregate="vlm"`

- `scripts/batch_process.py`
  - 新增 `_get_vlm_recon_checker_class()`（模块级缓存，与 LPIPS 同等模式）
  - 新增 `_build_recon_checker(config)` 工厂函数，按 `stage2_method` 分发
  - `check_target_consistency_single()`：vlm 方法额外读 edit `meta.json` 的 `instruction` 并传入
  - `batch_refresh_all_dreamsim()`：预加载阶段按 `stage2_method` 选择正确 checker 类

- `templates/edit_batch_card_macro.html`
  - 卡片摘要区 + 展开详情区按 `metric == 'vlm'` 条件分支：VLM 方法显示置信度 + `vlm_reason` 文字，隐藏 `scores_by_view` / `input_mode`；旧 LPIPS 显示不变

### Compatibility
- `stage2_method` 默认 `"lpips"`，不修改 config 时行为与之前完全一致
- `TargetQualityCheckResult` 新字段 `vlm_reason` 为可选，LPIPS 结果不输出该字段，历史数据不受影响
- 前端通过 `metric == 'vlm'` 判断，旧数据显示逻辑不变

## [v2.9.26] - 2026-04-03

### Changed
#### instruction legality 前置硬校验 + Stage1 后置 legality audit + Target 3D 失败原因分类

- `core/image/instruction_display_resolver.py`
  - 新增 `validate_instruction_legality()`，在原有语法与 multiview-safe 校验之上，进一步显式拒绝：
    - whole-object / main-body instruction
    - logo / emblem / label / seam line 这类 surface-only instruction
    - texture / color / material / finish / gloss 这类 appearance-only instruction
    - 明确 material swap（如 `wood -> metal`、`fabric -> leather`）
  - 现有材质/外观类报错文案统一明确为 `texture/color/material edit`
- `core/image/caption.py`
  - instruction generation 与 adaptive instruction validation 统一接入 `validate_instruction_legality()`
  - 生成阶段不再接受 prompt 软约束之外的非法 instruction；连续重试后若仍生成非法结果，直接显式失败
  - rewrite prompt 同步加强：明确禁止整物替换、表面标记编辑、材质替换
- `utils/prompts.py`
  - `INSTRUCTION_REMOVE_PROMPT`、`INSTRUCTION_REPLACE_PROMPT`、`INSTRUCTION_ADAPTIVE_K_PROMPT` 全部补充 whole-object / surface-only / material-swap 禁止项与正反例
  - `UNIFIED_JUDGE_*` prompt 新增 `instruction_legality` 步骤与 JSON 字段，要求基于 BEFORE / AFTER / MASK 判断“实际发生的编辑”是否合法
- `core/image/edit_quality_checker_unified.py`
  - unified judge 结果新增 `instruction_legality`
  - Stage1 判定顺序更新为：`view_sanity` -> `instruction_legality` -> `evidence_strength` -> `instruction_following`
  - 当 post-edit 结果属于 `main_body`、`appearance_only`、`material_only` 或 `unclear` 时，Stage1 直接失败
  - `relabel` 产出的 instruction 现在也必须通过 legality 校验，不再允许 rewrite 出表面/材质类 instruction
- `scripts/batch_process.py` / `scripts/run_full_experiment.py`
  - Target 3D 失败新增错误分类与落盘字段：
    - `target_gen3d_error_class`
    - `target_gen3d_error_message`
  - 当前分类值：`quota_limit`、`subject_too_small`、`upload_error`、`provider_failed`、`unknown`
  - 只增强可观测性，不改变现有 retry / fail 主流程
- `docs/guide/cli.md` / `cli.md`
  - 同步更新 `check-edit-quality`、`gen3d-from-edits`、`run_full_experiment` 文档，说明 legality gate、Stage1 post-edit legality audit 与 Target 3D 错误分类字段

## [v2.9.25] - 2026-04-01

### Changed
#### unified_judge 接入 Mask 证据 + 证据型 pass 语义 + 视角完整性约束

- `core/image/edit_quality_checker_unified.py`：
  - unified_judge 现在从编辑产物目录直接读取并传入第三张图 `edit_mask_grid.png`，不再只看 before/after 两张 6-view 拼图
  - VLM 输出新增 `supporting_views` 与 `evidence_strength` 字段；`evidence_strength` 必须为 `strong|medium|weak`
  - 当 `instruction_following=pass` 或 `evidence_strength in {strong, medium}` 时，`supporting_views` 不能为空；否则立即显式报错
  - 当 `edit_quality_check.unified_judge.require_non_weak_evidence=true` 且 `evidence_strength=weak` 时，Stage1 直接失败，不再接受“弱证据 pass”
  - unified_judge prompt 改为 evidence-based：明确要求基于可见证据判定，不允许依赖猜测；同时强调“小编辑可以通过，但必须在当前图片信息中真实可见并可定位”
  - `view_sanity` 新增完整性约束：如果 AFTER 图中的物体主体或关键编辑区域超出图像边界、被裁切到无法完整观察，则必须 fail
- `utils/prompts.py`：新增 Method-3 unified_judge 的 system/user prompt 模板，统一收口到公共 prompt 模块
- `config/config.yaml` / `config/config.py`：`edit_quality_check.unified_judge` 新增必填开关 `require_non_weak_evidence`
- `docs/guide/cli.md` / `cli.md`：同步更新 `check-edit-quality` 文档，说明 unified_judge 现在使用 before/after/mask 三图输入，并采用 evidence-based pass 规则

### Added
#### Models / Images 页面 SQLite 持久化索引，大幅提升加载速度

**背景**：随着 model 数量增大，Models 页面 TOC 和分页加载越来越慢，每次 30s 缓存失效后需对全量 N 个 model 目录做 5~10 次 I/O，NAS 上延迟极高，重启后无缓存冷启动更严重。

**新增文件**：
- `utils/pipeline_index.py` — SQLite-backed 持久化索引模块（`PipelineIndex` 类），支持启动时增量 reconcile（只重扫 mtime 变化的目录）、单 model 定点更新、WAL 模式线程安全、schema_version 自动 rebuild
- `tests/test_pipeline_index.py` — 13 个单元测试（init/reconcile/query/update/schema rebuild），本地 + 服务器全部通过

**修改文件**：
- `config/config.yaml` — `workspace` 节新增 `pipeline_index_db` 路径（指向 `/data-koolab-nas/xiaoliang/data/2d3d_data/.pipeline_index.db`，本地磁盘避免 OSS flush 问题）
- `config/config.py` — `WorkspaceConfig` 新增 `pipeline_index_db: str` 字段
- `app.py`：
  - 启动时初始化 `PipelineIndex` + 后台线程 reconcile（不阻塞服务）
  - `get_all_models_index()` / `_load_model_payload_by_id()` / `api_get_images_toc()` 优先读 DB，fallback 旧扫描
  - `api_get_models()` 分页支持 DB 快速路径 + 新 `priority_ids` 参数
  - 写操作（render/edit/gen3d/delete）完成后调 `_refresh_model_in_index()` 定点更新，不清全表
  - 新增 `GET /api/models/batch?ids=...` 接口（上限 50 个 ID）

### Added
#### Models 页面 YAML 筛选优先加载 + 初始加载提示

- `templates/models.html`：
  - 首次加载期间显示 "Loading models..." 占位提示，消除白屏无反馈
  - 新增 `getYamlFilteredIds()` / `loadYamlPriorityModels()` — 切换 YAML 筛选时立即调 `/api/models/batch` 批量加载该 YAML 内所有 model，不依赖分页顺序
  - `setYamlFilter()` 和页面初始化时均触发优先加载逻辑（已加载的 model 不重复请求）

### Fixed
#### Models 页面 YAML 计数脏数据过滤 + TOC/卡片可见性一致

- `app.py`：
  - Models 页实验过滤 payload 现在会过滤 `object_records.jsonl` 中 `source_model_id = null` / 空字符串的脏记录，避免 YAML 下拉把无效记录计入 `N models`
- `templates/models.html`：
  - `/api/models/batch`、`/api/models/search`、分页 `/api/models` 返回的完整 model payload 会同步回填 `allTocItems` / `allModelsData`
  - 修复“YAML priority 已加载卡片，但 TOC 仍用旧索引，`refreshModelFilters()` 又把卡片过滤掉”的问题
  - 页面初始化改为先加载 TOC，再执行 YAML priority 批量加载，避免后到的旧 TOC 覆盖前面补进来的新 model meta

## [v2.9.24] - 2026-03-31

### Added
#### Stage1 新增 unified_judge 质量检测方法（Method-3）

- 新增 `edit_quality_check.method = "unified_judge"` 选项，单次 VLM 调用同时完成观察、几何检查、指令遵循判定和 relabel
- 相比 `two_stage_recon`（4-5 次 LLM 调用），`unified_judge` 仅 1 次 VLM 调用，且 view sanity 检查带有编辑指令上下文，减少误杀
- VLM 输出统一 JSON：`observation`、`view_sanity`、`instruction_following`、`relabel`
- 判定逻辑：几何错误直接 fail → 指令遵循则 pass → 否则尝试 relabel → rewrite 成功则 pass
- 可选 `require_rejudge_after_relabel` 配置（默认关闭），对 relabel 结果做独立 rejudge
- 更新 unified_judge prompt 的显式思考顺序为 `observation -> 区分编辑变化/渲染变化 -> instruction_following -> view_sanity -> relabel`，要求模型在做 `view_sanity` 前先识别实际发生的编辑，减少把合理编辑变化误判为几何错误
- 新增文件：`core/image/edit_quality_checker_unified.py`
- 修改文件：`config/config.yaml`、`config/config.py`、`core/image/edit_quality_router.py`、`core/image/edit_quality_checker_v2.py`
- 完全向后兼容：通过 config method 切换，不影响已有 `grid_vlm` 和 `two_stage_recon`

### Fixed
#### run_full_experiment.py 支持 unified_judge + config.py Stage-2 参数解析修复

- `run_full_experiment.py` 原硬限 `edit_quality_check.method = 'two_stage_recon'`，切换到 `unified_judge` 后启动即报错；改为允许 `("two_stage_recon", "unified_judge")` 两种方法
- `config.py` 原仅在 `method = 'two_stage_recon'` 时解析 `two_stage_recon` 节，导致 `unified_judge` 下 `two_stage_recon_cfg = None`，Stage-2 LPIPS 一致性检查无法读取 `metric`/`recon_views`/`threshold` 等参数；改为 `unified_judge` 时也解析该节（Stage-2 参数共享），Stage-1 专属任务校验（diff/judge/view_sanity task）只在 `method = 'two_stage_recon'` 时做强制检查
- `scripts/batch_process.py` 原 `check-target-consistency` / `refresh-all-lpips` / `check_target_consistency_single` 仍硬限 `edit_quality_check.method = 'two_stage_recon'`，导致 `unified_judge` 下 Stage2 直接报错；改为允许 `("two_stage_recon", "unified_judge")`，但继续强制依赖共享的 `edit_quality_check.two_stage_recon` LPIPS 配置
- `docs/guide/cli.md`、`cli.md`、`README.md` 同步更新 Stage2 CLI 说明，明确 Stage1 可使用 `unified_judge`，但 Stage2 参数来源仍为 `edit_quality_check.two_stage_recon.*`
- 影响文件：`scripts/run_full_experiment.py`、`config/config.py`

### Fixed
#### instruction_display_resolver `_TEXTURE_KEYWORDS` 误杀修复

- `_TEXTURE_KEYWORDS` 包含了 `wooden`、`metallic`、`shiny`、`matte`、`glossy`、颜色形容词（`red`/`blue` 等）、`dark`/`light`/`bright` 等纯描述性形容词，导致 `"Remove the wooden board"` 这类合法的结构性删除指令被误判为 texture/color edit
- 修复原则：区分"描述物体属性的形容词"（应删，如 `wooden`/`red`/`dark`）和"操作对象名词"（应保留，如 `color`/`texture`/`material`）
- 保留关键词：`color`、`colour`、`texture`、`material`、`finish`、`pattern`、`paint`、`repaint`、`recolor`、`recolour`、`change the color` 系列短语
- 删除关键词：所有纯材质/颜色形容词（`wooden`、`metallic`、`shiny`、`matte`、`glossy`、`red`/`blue` 等色彩词、`dark`/`light`/`bright`、`style` 等）
- 影响文件：`core/image/instruction_display_resolver.py`

### Fixed
#### batch_process.py `recheck_edit_quality_single` KeyError 修复

- `recheck_edit_quality_single` 第 1081 行错误读取 `stage1_payload["edit_status"]`，但 `run_stage1_quality_check_single` 的返回 dict 从未包含该 key
- 修复为从 `refreshed_meta["edit_status"]` 读取（meta.json 中的字段）

### Fixed
#### relabel 展示优先级与 Target Render Grid UI 修复

- `instruction_display_resolver.py` 现在优先读取 `quality_check.stage_edit_correctness.*`，不再让旧的顶层 `instruction_text_effective` / `instruction_display_source` / `instruction_display_status` 覆盖已接受的 relabel 结果
- 修复后，像 `8fdcfe3d6c72/edited/a0782ada` 这类已成功 relabel 的 case，会在前端正确显示 rewrite 后的 `Effective Instruction`
- `app.py` 新增 target 3D 6 视角渲染拼图生成逻辑：从 `triplets/<target_model_id>/views/<provider_id>/` 读取 target render views，按需生成 `target_render_grid.png`
- `templates/edit_batch_card_macro.html` 中原先误用 edited views 拼图的 `Target Image` 卡片，改为展示 target 3D model 的 `Target Render Grid`，并标注 provider
- 同时补充 `unified_judge` 的前端方法标签，避免显示为 `unknown method`

## [v2.9.23] - 2026-03-31

### Changed
#### Semantic verify 置信度阈值下调

- `config/config.yaml` 将 `render.semantic_alignment.min_confidence` 从 `0.75` 下调到 `0.5`
- `cli.md`、`docs/guide/cli.md`、`docs/guide/batch-render.md` 中对应示例同步更新
- 目标是减少 `verify_after_rerender` 因 VLM confidence 轻微波动导致的非必要失败；语义 front 仍要求判为 `front`

### Fixed
#### Stage1 外层重试与错误归类收敛

- `scripts/run_full_experiment.py` 的 `_stage1_failure_payload()` 现在区分两类 Stage1 失败：
  - `failed_quality` 继续记为 `StageValidationFailed`，并在外层直接终止 retry
  - `error_quality_check` 改为 `StageQualityCheckExecutionError`，允许按现有 retry 配置继续重试
- `scripts/run_full_experiment.py` 的 `_stage_error_class()` 现在会优先识别 `stage1_status=error_quality_check`，将 `Server error 504`、timeout、request failed 等执行错误从普通 `quality_check_failed` 中分离
- Stage retry 日志的 `response_context` 预览新增 `stage1_status`、`stage1_reason`、`stage1_error_message`，便于直接区分内容失败和 QC 执行失败
- 这次修改不改变 Stage1 QC 算法本身，只修正外层编排与日志归类

### Fixed
#### Source pipeline 断点续跑：中断后重启不再从头重新生成 prompt

**问题：**
`run_full_experiment.py` 的 prompt_record 仅在整个 source pipeline（prompt优化 → T2I → Gen3D → 渲染）全部完成后才持久化。如果在 Gen3D 模型下载阶段中断，重启后 `existing_prompt` 为空，会从 prompt 优化重新开始，浪费已完成的工作。

**根因：**
`_remember_prompt_record` 在 `_run_object_job._inner()` 中仅在 `_run_source_pipeline` 成功返回后才调用，而 source pipeline 内部只有 stage 级别重试，没有跨重启的子阶段 checkpoint。

**修复方案：**
1. `_run_source_pipeline._inner()`：在 `_generate_prompt_record` 返回后立即调用 `_remember_prompt_record`，将 prompt_id / image_id / source_model_id / prompt 文本等核心字段持久化到 JSONL。
2. `_run_object_job._inner()` else 分支：移除原有的 `_remember_prompt_record` 调用（已移入 step 1）。
3. 新增 `_resume_source_pipeline_if_needed(prompt_context, job, object_index)`：在 `existing_prompt` 分支中，通过文件存在性检查（image.png、model GLB、views/）识别哪些 stage 未完成，并用 `_execute_stage_with_retry` 逐一补跑。
4. `_run_object_job._inner()` existing_prompt 分支：在重建 `prompt_context_local` 后立即调用新方法。

**行为变化：**
- 中断后重启：只补跑中断的子阶段（T2I / Gen3D / 渲染），不重新生成 prompt 和图像
- 完整运行（无中断）：行为不变，`_resume_source_pipeline_if_needed` 所有文件存在性检查通过，为完全 no-op
- 多次写入同一 prompt_record：`_latest_prompt_records` / `prompt_record_map` 保留最新版本，无副作用

**影响文件：**
- `scripts/run_full_experiment.py`

---

### Changed
#### WebGL render 日志精简（批量模式 vs 单独模式分离）

**问题：**
WebGL render 子进程的所有输出（Chrome 控制台、GPU 消息、每视角 Saved 行等）直接透传到父进程日志，批量运行时每次渲染产生 20+ 行噪音。

**实现方案：**
- `run_render_batch.py`：新增 `_render_tls`（thread-local）+ `_is_quiet_subprocess()` + `_WEBGL_QUIET_KEEP` 关键词列表 + `_filter_webgl_output()`；`_run_webgl_render` 根据 flag 决定 `PIPE` 捕获还是透传
- `batch_process.py`：`render_single` 新增 `quiet_subprocess: bool = False` 参数，入口处设置 thread-local
- `run_full_experiment.py`：调用 `render_single` 时传 `quiet_subprocess=True`

**行为变化：**
- `python scripts/batch_process.py render`（单独跑）：日志不变，原样输出
- `run_full_experiment` 渲染**成功**：只输出关键节点（启动参数、文件大小、model loaded / bbox / safeRadius、saved 视角数、完成摘要），共约 8 行
- `run_full_experiment` 渲染**失败**：完整子进程输出 dump，方便排查

**影响文件：**
- `scripts/run_render_batch.py`、`scripts/batch_process.py`、`scripts/run_full_experiment.py`

---

### Changed
#### 迁移到新服务器 qh_k8s：消除硬编码路径，全部配置化

**变更目标：**
- 将 `app.py` / `scripts/webgl_render.py` 中的绝对路径硬编码迁移到 `config/config.yaml`，实现服务器迁移只改配置文件
- 新服务器 qh_k8s：代码 `/data-oss/meiguang/xiaoliang/code3/2d3d_v2`，数据 `/data-oss/meiguang/xiaoliang/data/2d3d_data`，Python 环境 `/data-koolab-nas/xiaoliang/local_envs/2d3d`

**实现方案：**
1. `config/config.yaml`
   - `workspace.pipeline_dir`：更新为新服务器路径
   - 新增 `workspace.python_interpreter`：Web UI 启动实验子进程的 Python 解释器路径
   - 新增 `workspace.playwright_browsers_path`：WebGL 渲染器 Playwright 浏览器缓存路径（本地磁盘，避免 OSS flush 问题）
   - 新增 `workspace.logs_dir`：实验运行日志目录（写到 data-koolab-nas，避免 data-oss OSError: Device or resource busy）
   - `export.path_prefixes`：修正缺失的 `/` 前缀，新 data-oss 路径置首位
2. `config/config.py`
   - `WorkspaceConfig` 新增 `python_interpreter`、`playwright_browsers_path`、`logs_dir` 三个字段
   - 解析器对应增加 `_require_key` 调用（Fail Loudly）
3. `app.py`
   - 删除模块级硬编码 `EXPERIMENT_PLANS_DIR`、`PYTHON_INTERPRETER`；改为全局变量，在 `init_semaphores()` 中从 config 初始化
   - `EXPERIMENT_PLANS_DIR` 由 `PIPELINE_DIR / "experiment_plans"` 自动推导，与其他子目录一致
   - `LOGS_DIR` 改为 `Path(config.workspace.logs_dir)` 读取，删除 `PROJECT_ROOT / "logs"` 硬编码
4. `scripts/webgl_render.py`
   - 将 `load_config` 提前到 PLAYWRIGHT_BROWSERS_PATH 设置前，从 `config.workspace.playwright_browsers_path` 读取，删除硬编码

**影响文件：**
- `config/config.yaml`、`config/config.py`、`app.py`、`scripts/webgl_render.py`

**行为变化与兼容性：**
- 迁移服务器只需修改 `config.yaml` 中的四个 workspace 字段，代码无需改动
- 旧服务器 qh_4_4090 的对应路径已作为注释保留在 config.yaml 中

---

### Bug Fixes
#### Batch Generation：Execute Selected YAML 校验误报 & 按钮行为修正

**问题：**
1. 在 Batch Generation 页面选择已有 YAML 后点击 "Execute Selected YAML"，报 `categories[n].objects is not allowed when random.object=true` 误报，无法执行
2. "Execute Selected YAML" 按钮会直接在后台启动实验，用户期望只生成 CLI 命令

**根本原因：**
- `_normalize_experiment_plan_for_form` 在非 `random.category` 分支中，无论 `random.object` 是 true 还是 false，都将 `objects` 设为 `[]`（空列表）
- 后续 `_validate_experiment_plan` 检查 `objects is not None`，而 `[] is not None = True` → 误报
- `api_execute_existing_experiment_plan` 对已存在的 YAML 文件额外做了表单级校验，不应该

**修复：**
1. `app.py` `_normalize_experiment_plan_for_form`：`random.object=true` 时 `normalized_objects = None`（而非 `[]`）
2. `app.py` `api_execute_existing_experiment_plan`：删除 `_validate_experiment_plan` 调用，交由 `run_full_experiment.py` 做权威校验
3. `templates/batch_generation.html` `executeSelectedPlan()`：改为调用 `/api/experiment-plan/cli-command`（只生成命令），不再调用 `/api/experiment-plan/execute`（直接执行）

**影响文件：**
- `app.py`、`templates/batch_generation.html`

**行为变化：**
- 选中已有 YAML 点击按钮 → 生成可复制的 CLI 命令，不自动执行
- 选中 `random.object=true` 类型的 YAML 不再误报

---

## [v2.9.21] - 2026-03-30

## Improvements
- 优化pairs页面布局，还要继续优化

### Added
#### Pairs 页面新增 YAML 实验过滤与导出命令生成

**变更目标：**
- 在 Pairs 页面支持按 YAML 实验计划过滤模型对，便于快速查看某批实验产出的所有编辑前后 3D 模型对
- 支持按 LPIPS 分数阈值进一步筛选
- 一键生成可在服务器执行的 CLI 导出命令，产出与现有 `exports/` 兼容的 `manifest.json`

**实现方案：**
1. `config/config.yaml` 与 `config/config.py`
   - 新增 `export.path_prefixes` 配置项（列表类型），定义 manifest 中可选的路径前缀
   - 新增 `ExportConfig` dataclass 解析该配置
2. `app.py` 新增 4 个路由
   - `GET /api/pairs/export-config`：返回导出配置（path_prefixes）
   - `GET /api/pairs/yaml-options`：返回所有 YAML 实验计划列表，供前端多选
   - `GET /api/pairs/filter-by-yaml`：根据选中的 YAML plan_paths + LPIPS 阈值过滤模型对，数据来源为 `edit_records.jsonl`
   - `POST /api/pairs/generate-export-cmd`：根据筛选条件生成可复制的 CLI 导出命令
3. `templates/pairs.html`
   - 顶部新增折叠式 "YAML 实验过滤" 面板：YAML 多选列表、LPIPS 阈值输入、应用/清除按钮
   - 漏斗统计条（总记录 → 有 target → LPIPS 筛选后 → GLB 完整）
   - 导出区：数据集名称、路径前缀下拉、生成命令按钮 + 可复制命令行
4. `scripts/export_edit_pair_manifest.py`
   - 新增 `--plan-paths`、`--lpips-max`、`--dataset-name`、`--path-prefix` CLI 参数
   - 新增 `scan_candidates_from_yaml()` 函数：从 `edit_records.jsonl` 筛选 + 去重 + 文件完整性校验
   - `build_manifest()` 参数化路径前缀（支持新旧服务器迁移）
   - 不传 `--plan-paths` 时保持原有全量扫描 + 采样行为

**影响文件**
- `config/config.yaml`
- `config/config.py`
- `app.py`
- `templates/pairs.html`
- `scripts/export_edit_pair_manifest.py`

**行为变化与兼容性**
- Pairs 页面原有行为完全不变，YAML 过滤为可选的折叠面板
- `export_edit_pair_manifest.py` 无参数时行为与旧版完全兼容
- 路径前缀从 `export.path_prefixes` 配置读取，遵循 Fail Loudly 原则

**验证**
- 在 `qh_4_4090` 上以 `PORT=10099` 启动并验证：
  - `GET /api/pairs/yaml-options` 返回 31 个 YAML
  - `GET /api/pairs/filter-by-yaml?plan_paths=...&lpips_max=0.15` 正确返回过滤后的模型对
  - `POST /api/pairs/generate-export-cmd` 生成的命令可直接在服务器执行，产出 manifest.json

## [v2.9.20] - 2026-03-29

### Bug Fixes
#### Web UI 兼容旧版与实验版 prompt / image schema

**问题**
- `/prompts` 页面读取 `pipeline/prompts/*.jsonl` 时，旧版 prompt 记录（`id`、`subject`、`status`）与实验记录（`prompt_id`、`object_name`、`experiment_id`）混在一起，导致模板按旧字段渲染时直接报错。
- `/images` 页面也存在同类 schema 不一致，只是之前没有直接崩：旧版图片 meta 使用 `subject` / `generated_at`，实验图片 meta 使用 `object_name` / `created_at`，导致实验图片在页面上可能丢失标题显示。
- `models_src/` 目录同时包含 source model 与 `_edit_` target model，而部分列表与下载逻辑仍把它当成单一 source model 池来处理，语义不一致。

**修复方案**
1. 在 `app.py` 中增加显式 normalization / adapter 层：
   - `normalize_prompt_record()` 只支持两种已知 prompt schema：旧版 prompt 记录与实验 prompt 记录。
   - `normalize_image_record()` 将旧版 / 实验图片 meta 统一转换成页面使用的单一 payload。
   - 未知 schema 或字段类型错误时直接抛出 `ValueError`，不添加第三种静默 fallback。
2. 更新 `/prompts` 模板与 prompt 相关 API，统一消费标准化字段：
   - 统一 `ui_id`
   - 增加 schema badge（`Legacy` / `Experiment`）
   - 通过 `can_generate_image`、`can_batch_generate`、`can_delete`、`can_open_image` 控制按钮显隐
   - 实验记录可以展示，但不再错误暴露旧版 prompt 才具备的操作按钮
3. 更新 `/images` 页面与 `/api/images/toc`，统一使用标准化后的 `display_subject`。
4. 收紧 source model 语义：
   - source model 列表只遍历 source 目录，排除 `_edit_` target model 目录
   - 模型下载接口改为从 `triplets/<model_id>/views/` 打包渲染视图，不再错误地从 `models_src/<model_id>/views/` 读取

**影响文件**
- `app.py`
- `templates/prompts.html`
- `templates/images.html`
- `docs/architecture/PROJECT_STRUCTURE_OVERVIEW.md`

**行为变化与兼容性**
- 旧版 prompt / image 记录仍然可以在 Web UI 中正常展示。
- 实验版 prompt / image 记录也可以在同一页面中正常展示。
- 兼容逻辑集中在后端 normalization 层，不再分散写在模板 fallback 中。
- 若出现未知 prompt / image schema 变体，将直接显性报错，而不是静默兼容。

**验证**
- 在 `qh_4_4090` 上按以下方式验证：
  - `source /home/xiaoliang/local_envs/2d3d/bin/activate`
  - `PORT=10003 python app.py`
- 已确认 `GET /prompts`、`GET /api/prompts`、`GET /api/images?page=1&per_page=1`、`GET /api/models?page=1&per_page=1` 返回 `200`。

## [v2.9.19] - 2026-03-27

### Changed
#### Stage2 Target 3D 一致性指标从 DreamSim 切换为 LPIPS（保持现有并发架构不变）

**变更目标：**
- 保持 `two_stage_recon` 的任务编排、`ThreadPoolExecutor`、`recon_quality_check` semaphore、target meta 写回结构不变
- 仅替换 Stage2 的底层相似度计算，从 GPU 依赖较强的 DreamSim 切换为 CPU 可运行的 LPIPS
- 前端与统计页统一显示为 `LPIPS`

**实现方案：**
1. `config/config.yaml` 与 `config/config.py`
   - `edit_quality_check.two_stage_recon.metric` 强制改为 `lpips`
   - 删除 `dreamsim_cache_dir`
   - 新增 `lpips_net: "alex"`
   - 保留 `input_mode` / `aggregate` / `threshold` / `device` 语义，避免上层调用链变化
2. `core/render/recon_consistency_checker.py`
   - 保留 `ReconConsistencyChecker.check()` 和 `TargetQualityCheckResult` 结构
   - 内部 per-view 评分由 `compute_dreamsim_score()` 替换为 `compute_lpips_score()`
   - 继续输出 `scores_by_view`、聚合 `score`、`threshold`、`reason`
3. `scripts/batch_process.py`
   - 不改 Stage2 的 semaphore / executor 编排
   - `refresh-all-lpips` 作为新的主 CLI 命令名
   - `refresh-all-dreamsim` 继续保留为兼容别名
4. `app.py` / `templates/*`
   - 新增 `/api/models/<model_id>/refresh-lpips` 与 `/api/models/refresh-lpips-all`
   - 旧 DreamSim 路由继续保留为兼容别名
   - 模型详情页、Models 首页、Experiment Stats 页面统一显示 `LPIPS`
5. `scripts/run_full_experiment.py` / `app.py`
   - 新增统计字段 `stage2_lpips_mean` / `stage2_lpips_std`
   - 读取历史记录时兼容旧字段 `stage2_dreamsim_*`

**影响文件：**
- `config/config.yaml`
- `config/config.py`
- `core/render/recon_consistency_checker.py`
- `scripts/batch_process.py`
- `scripts/run_full_experiment.py`
- `app.py`
- `templates/model_detail.html`
- `templates/model_detail_scripts.html`
- `templates/models.html`
- `templates/experiment_stats.html`
- `README.md`
- `docs/guide/cli.md`
- `docs/guide/http-client.md`

**兼容性说明：**
- `two_stage_recon` 方法名、`check-target-consistency` 主命令、`target_quality_check` 主体 schema、`concurrency.refresh_all_dreamsim` 配置键均保持不变
- 历史 DreamSim 统计字段与旧 API 路由保留兼容读取/访问能力
- 前端默认展示 LPIPS，不再继续向用户暴露 DreamSim 作为当前 Stage2 指标

**注意：**
- LPIPS 阈值仍由 `config.yaml` 控制，但数值不能直接复用旧 DreamSim 经验阈值；需要后续按数据集继续标定
- 运行环境需显式安装 `lpips` 依赖；若缺失会 Fail Loudly 直接报错


## [v2.9.18] - 2026-03-26

### Bug Fixes
#### ApiLane slot 在 retry 期间被提前释放，导致配额超限与超时

**问题现象：**
并发运行时，Hunyuan 3D generation 阶段出现两类失败：
- `FailedOperation.InnerError message:配额超限`（第一次 attempt 刚提交就被 Hunyuan 拒绝）
- `Generation failed: Timeout after 900 seconds`（job 已提交并进入 processing，但 15 分钟未返回结果）

两类失败同时出现，成因相同：同一时刻提交给 Hunyuan 的并发 job 数超过账号配额上限。

**根因分析：**
`ApiLane.run()` 在单次 API 调用粒度上获取/释放 slot。`_execute_stage_with_retry` 的 retry 循环在 `lane.run()` 外层，每次 attempt 失败后 lane slot 被释放，backoff 等待期间 slot 空出，其他任务趁机占满，retry 重新提交时 Hunyuan 端已无可用配额。

具体场景：3 个对象并发运行，各自的 `target_gen3d` 同时占用 3 个 Hunyuan job slot（均为 processing 状态），第 4 个 retry job 趁 slot 释放提交进去，Hunyuan 端实际已满额，导致配额超限。

**修复方案：**

1. **`ApiLane` 新增 `hold()` context manager**（thread-local 跟踪）：
   - `hold()` 一次性 acquire slot，设 `_thread_local.holding = True`，yield 后还原并 release。
   - `run()` 检查 `_thread_local.holding`：若当前线程已持有 slot，跳过 acquire/release，避免双重占用。

2. **`_execute_stage_with_retry` 新增 `hold_slot_across_retries: bool = False` 参数**：
   - 为 True 时，通过 `contextlib.ExitStack` 在 for retry 循环前 acquire slot，直至最终成功（`return result`）或 ABORT（`raise StageExecutionExhaustedError`）才释放。
   - 中间 attempt 失败只触发 backoff sleep，slot 全程持有不释放。

3. **以下三个调用处启用 `hold_slot_across_retries=True`**：
   - `source_gen3d`：Hunyuan source 模型生成
   - `target_gen3d`：Hunyuan target 模型生成
   - `stage1_quality_check`：多视角编辑 QC（防止 retry 期间 oneapi_text slot 竞争）

**影响文件：**
`scripts/run_full_experiment.py`

**行为变化：**
- retry 期间 lane 在 flight 计数不降为 0，其他任务需等待该 slot 释放后才能进入，整体并发上限与 `config.yaml` 中配置的 lane 并发数保持一致。
- 首次 attempt 失败（如 Hunyuan 配额超限）后，系统等待 backoff 期间不再释放 slot，外部不会感知到一个"空闲名额"，从根本上避免 retry 与其他 job 抢额度。

**兼容性：**
- `ApiLane.run()` 行为对所有未启用 `hold_slot_across_retries` 的调用者完全不变（`holding` 默认 False）。
- `_execute_stage_with_retry` 的新参数默认值为 False，所有未修改的调用点行为不变。

## [v2.9.17] - 2026-03-26

### New Features
#### Batch Generation：Quick Tools 面板（均衡生成 + 获取 CLI 命令）

为 `/batch-generation` 页面新增 **Quick Tools** 区块，包含三个 tab，全部复用现有并发框架，无需修改 `run_full_experiment.py`。

**Panel A — Balanced Generate（均衡快速生成）：**
- 用户只需填写 `plan name`、`total_objects`、`edits_per_object`、provider、edit mode、GPU ID，系统自动将 total_objects 均衡分配到 `categorized_objects.json` 中全部 19 个 category。
- 分配算法：`base = total // 19`，`remainder = total % 19`，按 pool size 降序将 remainder 补 1 给最大的 category，同时尊重每个 category 的实际 pool 上限（最小 category Medical: 8, Lighting: 9）。
- 表单下方**实时预览**分配结果（纯 JS 计算，无 API 调用），显示每个 category 的分配数量与 pool 大小。
- 点击 "Generate Balanced Plan" 调用新 API，自动保存 YAML 到 experiment_plans_dir，在右侧显示 YAML 内容与可复制的 CLI 命令。

**Panel B — Get Plan CLI（获取已有 YAML 的执行命令）：**
- 从历史 YAML 列表中选择任意 plan，输入 GPU ID，点击 "Get Execute CLI" 获取 `nohup` 执行命令。
- 只返回命令字符串，**不启动任何 web 任务**，用户自行复制到服务器执行。

**Panel C — Get Resume CLI（获取断点继续命令）：**
- 从未完成的实验列表中选择（自动过滤，只显示 `status=running/interrupted` 的实验），输入 GPU ID 覆盖，点击 "Get Resume CLI"。
- 返回 `--resume-experiment-id` 形式的 `nohup` 命令，**不启动任务**，与现有 "Resume" 按钮（通过 web 任务系统触发）并存。

**新增后端 API（均在 `app.py`）：**
- `POST /api/experiment-plan/generate-balanced`：均衡分配 → 生成 YAML → 返回 plan_path / yaml_content / cli_command / log_path / distribution
- `POST /api/experiment-plan/cli-command`：给定 plan_path + gpu_id，返回执行 CLI，不启动任务
- `POST /api/experiment-plan/resume-cli-command`：给定 experiment_id + gpu_id，返回 resume CLI，不启动任务
- `_distribute_balanced()` 辅助函数（含 pool 上限溢出补偿逻辑）

**影响文件：**
`app.py`、`templates/batch_generation.html`

**兼容性：**
- 生成的 YAML 格式与现有 plan 格式完全一致（`random.category=false, random.object=true`），直接由现有 `run_full_experiment.py` 执行，并发逻辑零改动。
- 现有 "Execute Selected YAML" / "Resume" / "Repair" 按钮及其 web 任务触发路径保持不变。



### Configuration
#### Stage2 DreamSim 权重目录显式配置化
- `config/config.yaml` 与 `config/config.py` 为 `edit_quality_check.two_stage_recon` 新增必填配置 `dreamsim_cache_dir`，并要求使用绝对路径。
- 当前生产配置固定为 `/seaweedfs/xiaoliang/code3/2d3d_v2/models`，避免 DreamSim 因当前工作目录变化而把 `./models` 解析到错误位置。

### Fixes
#### DreamSim 权重缺失时不再隐式联网下载
- `core/render/recon_consistency_checker.py` 现在会在加载 DreamSim 前先检查 `dreamsim_cache_dir` 是否存在且包含 Stage2 默认所需权重文件。
- 若缓存目录缺失或不完整，会直接 Fail Loudly 报错；不再退回到 `dreamsim` 包内部的隐式联网下载，从而避免服务器无外网时出现模糊的 `urllib timeout`。
- DreamSim 进程内缓存与 inference lock 现在按 `(device, cache_dir)` 组合键区分，避免切换配置后误复用旧目录下的模型实例。

### Documentation
- `docs/guide/cli.md` 补充 `edit_quality_check.two_stage_recon.dreamsim_cache_dir` 的用途与绝对路径要求。

## [v2.9.15] - 2026-03-26

### Bug Fixes
#### WebGL target render 概率性失败：`--fixed-center-y: expected one argument`
- **现象**：edit 模型的 target render 子进程以 exit code 2 退出，报错 `argument --fixed-center-y: expected one argument`。
- **根因**：Blender 归一化后 WebGL 重新计算 bounding box 时，center 的 y/z 轴会产生极小浮点误差（如 `-8.74e-08`）。`str()` 对这类值输出科学计数法字符串（`"-8.74e-08"`），而 Python 3.11 的 argparse 负数识别正则 `^-\d+$|^-\d*\.\d+$` **不支持科学计数法**，导致该字符串被当作未知 optional flag 而非参数值，argparse 认为 `--fixed-center-y` 缺少值。
- **修复**：`scripts/run_render_batch.py` 的 `_run_webgl_render` 中，构建 cmd 时对含 `e`/`E` 的浮点字符串改用 `f"{v:.17f}"` 固定小数点格式，其余值保持 `str()` 不变。

**影响文件：** `scripts/run_render_batch.py`

## [v2.9.14] - 2026-03-26

### Improvements
#### run_full_experiment / edit pipeline 并发日志可读性增强
- `scripts/run_full_experiment.py` 为 object-level 与 edit-level 关键日志统一补充稳定实体标识：
  - source 链路统一使用 `source_model_id=<12位随机ID>`
  - edit 链路新增 `edit_scope_id=<source_model_id>_edited_<edit_id>`，并与现有 `edit_id` / `target_model_id` 同时输出
- `scripts/run_full_experiment.py` 的 stage retry 机制现在不再只输出 `Timing START/END`；当 stage 失败、重试、最终 exhaust 时，会显式打印：
  - `error_class`
  - `error_type`
  - `message`
  - `response_context` 摘要
  - `next_attempt` 与 `backoff`
- `scripts/batch_process.py` 的 edit 相关路径同步补齐实体标识：
  - `edit_apply` / `mask_artifact_build` timing 行新增 `edit_scope_id`
  - `run_stage1_quality_check_single()` 的 `EditQC START/RESULT/ERROR/SKIP` 日志新增 `source_model_id + edit_id + edit_scope_id`
  - `gen3d_from_edit_single()` 新增 `Target3D START/RESULT/ERROR/SKIP` 日志，并显式输出 `target_model_id`
  - `check_target_consistency_single()` 的 `Stage2 START/RESULT/ERROR` 日志新增 `edit_scope_id`
- `scripts/batch_process.py` 写入的 edit `meta.json` 新增 `edit_scope_id` 字段，便于后续 grep / 审计 / UI 对齐。

### Documentation
- `docs/guide/cli.md` 补充 `run_full_experiment.py` 与 edit pipeline 的日志标识规范，说明如何通过 `source_model_id` / `edit_scope_id` 在并发日志中定位单条链路。

**影响文件：**
`scripts/run_full_experiment.py`、`scripts/batch_process.py`、`docs/guide/cli.md`

**兼容性：**
- 本次变更只增强日志与元数据，不改变已有 `target_model_id=<source_model_id>_edit_<edit_id>` 的目录/数据命名规则。
- 新增 `edit_scope_id` 仅作为日志与调试标识，不替代现有 target model 主键。

### Tests
#### Hunyuan 下载代理诊断脚本
- 新增 `tests/test_hunyuan_download_proxy.py`，用于给定一个已知的下载 URL，直接复现生产环境中 `httpx + download_proxy` 的拉取行为。
- 脚本会显式记录 `configured_download_proxy`、`download_proxy`、`status_code`、`first_byte_seconds`、`bytes_downloaded`、`redirect_count`、`final_url` 等诊断字段，并将结果 JSON 写入项目 `logs/`。
- 当传入 `--output-file` 时，下载内容按 chunk 直接落盘，不再先全部累积到内存再写文件，便于复现大文件或长时下载场景。
- 新增 `--heartbeat-seconds`，当请求长时间卡在“等待响应头”或“等待下一个 body chunk”时，终端会持续打印心跳日志，避免误判为脚本无响应。

## [v2.9.13] - 2026-03-25

### New Features
#### Source-Target 3D 模型几何对齐（缩放 + 形状归一化）
解决编辑前后两个 GLB 模型由 API 独立生成、WebGL 各自 auto-fit 导致渲染图尺寸语义失真的问题（例如去掉帽子后，target 渲染图中主体反而变大）。

- `scripts/bpy_align_standalone.py` 新增三个互斥 CLI 参数：
  - `--normalize`：source 模式，旋转后计算 bbox，执行 center+scale 归一化，输出 `norm_params.json`（含 `rotation_matrix`、`center`、`max_dim`）。
  - `--norm-from <json>`：target 模式（`share_rotation=true`），从 `norm_params.json` 读取旋转 + center + max_dim，一次性全部应用。
  - `--norm-center-from <json>`：target 模式（`share_rotation=false`），从 json 只读 center + max_dim，旋转仍由 `--rotation-matrix` 传入。
- `scripts/webgl_render.py` 及 `scripts/webgl_render_standalone.py` 新增 `--fixed-radius` / `--fixed-center-x/y/z` 参数：source 渲染后从 Playwright 读取 `safeRadius` 和 `center` 并追加写入 `norm_params.json`；target 渲染时注入 source 的参数，禁止 WebGL 自动 auto-fit，确保两者渲染尺寸一致。
- `scripts/run_render_batch.py` 分叉 source / target 渲染路径：source 路径执行归一化并追加 webgl 参数到 `norm_params.json`；target 路径从 `norm_params.json` 加载参数并按 `share_rotation_to_target` 开关选择全复用或只复用 center+scale；`norm_params.json` 不存在时立即 Fail Loudly。

#### View Sanity Check（编辑后图片视角合理性检查）
- `core/image/edit_quality_checker_v2.py` 在 Method-2（two_stage_recon）的 Stage 1A（diff）之前新增独立 stage：**View Sanity Check**。检查编辑后图片的 6 个视角之间是否几何自洽，以 before 拼图作为形状参照，不依赖编辑指令。
- 触发条件：`edit_quality_check.two_stage_recon.view_sanity_check.enabled = true`。
- 控制流：检查 fail 时立即返回 `edit_status=failed_quality`，原因以 `[view_sanity]` 开头，不进入后续 diff/judge/relabel 步骤。
- `EditCorrectnessDetail` 新增 `view_sanity_result` 字段（含 `decision`、`reason`、`problematic_views`），写入 `meta.json` 和 `edit_correctness_detail.json`。
- `_build_effective_after_views` 调用从 stitched_6 分支内部提前至 `check()` 入口，供 View Sanity Check 和后续 diff/judge 共用，消除重复构建。

### Configuration
#### render.semantic_alignment 新增归一化参数
- `config/config.yaml` 新增 `normalize_geometry`（true = 旋转后执行 center+scale 并持久化）、`share_rotation_to_target`（false = target 独立走 VLM 定朝向）、`norm_params_filename`（默认 `norm_params.json`）。
- `config/config.py` 的 `SemanticAlignmentConfig` 新增对应字段，加载时强制校验：`normalize_geometry=true` 时 `save_aligned_glb` 必须为 `true`，否则 Fail Loudly。

#### edit_quality_check 新增 view_sanity_check 配置及 task
- `config/config.yaml` 新增 `tasks.edit_quality_check_view_sanity`（provider: oneapi，model: gemini-3-flash-preview）及 `edit_quality_check.two_stage_recon.view_sanity_check.enabled`。
- `config/config.py` 新增 `ViewSanityCheckConfig` dataclass；`TwoStageReconConfig` 新增 `view_sanity_check` 字段；新增 `edit_quality_view_sanity_mllm` property；`method=two_stage_recon` 时强制校验 `tasks.edit_quality_check_view_sanity` 存在，缺失则 Fail Loudly。

### Fixes
#### DreamSim 批量更新 SeaweedFS IO 稳定性
- `utils/fs_retry.py`（新文件）：封装 SeaweedFS FUSE 挂载下常见瞬态错误（`EIO`、`ESTALE`、`ETIMEDOUT`、`ECONNRESET` 等）的指数退避重试逻辑，提供 `retry_io()` 和 `retry_open_image()` 两个工具函数。
- `core/render/recon_consistency_checker.py` 将图片加载（`_load_metric_image`）从 inference lock 内部移到外部：原来 SeaweedFS IO 抖动会在持锁状态下阻塞 GPU 推理；现在先加载图片再进锁，IO 延迟和 GPU 推理互不阻塞。
- 文件存在性检查（`img_path.exists()`、`render_path.exists()`）换用 `retry_io()` 包装，短暂 IO 抖动不再直接抛 `FileNotFoundError`。

### Tests
- `tests/test_view_sanity_check.py`：新增独立测试脚本，支持三种调用方式：传入 before/after views 目录（自动拼图）、传入已拼好的 grid 图、指定模型覆盖。直接调用生产代码 `run_view_sanity_check()`，结果写入 `tests/logs/`。

### Documentation
- `docs/guide/cli.md` 更新 Method-2 two_stage_recon 流程描述，补充 View Sanity Check 步骤及 `view_sanity_result` 写入说明。

**影响文件：**
`config/config.yaml`、`config/config.py`、`scripts/bpy_align_standalone.py`、`scripts/webgl_render.py`、`scripts/webgl_render_standalone.py`、`scripts/run_render_batch.py`、`core/render/recon_consistency_checker.py`、`utils/fs_retry.py`、`core/image/edit_quality_checker_v2.py`、`tests/test_view_sanity_check.py`、`docs/guide/cli.md`

**兼容性：**
- `normalize_geometry=false` 时 render 对齐新路径均不触发，完全向后兼容。
- `view_sanity_check.enabled=false` 时跳过 View Sanity Check，行为与升级前一致。
- `tasks.edit_quality_check_view_sanity` 为必填项，升级后配置缺失将在启动时 Fail Loudly。

#### Hunyuan 3D 下载代理与 API 请求代理解耦
- `core/gen3d/hunyuan.py` 现在对 submit/poll 的 `httpx.Client` 显式设置 `trust_env=False`，不再继承 shell 中的 `ALL_PROXY` / `HTTPS_PROXY` / `HTTP_PROXY`。
- Hunyuan 模型文件下载仍只使用 `config.download_proxy`；下载 client 同样显式禁用环境代理，确保代理来源唯一且可控。
- `tests/test_batch_api_common.py` 与 `tests/test_batch_api_gen3d_hunyuan.py` 同步对齐生产行为：Response API submit/poll 不读取环境代理，GLB 下载仅在 `download_proxy` 存在时走显式代理。

## [v2.9.12] - 2026-03-24

### 修复
#### WebGL 渲染隔离与语义对齐可观测性
- `scripts/run_render_batch.py` 现在通过独立子进程启动 WebGL 渲染（`scripts/webgl_render_standalone.py`），应用 `render.webgl.subprocess_timeout_seconds` 硬超时，并在超时后强制终止渲染子进程树，避免卡死的 Playwright / Chromium 会话阻塞父级实验调度器。
- `scripts/run_render_batch.py` 现在为语义对齐各子阶段输出明确的日志：`first_pass_render`、`semantic_decision`、`compute_rotation`、`align_glb`、`final_render`、`verify_final_views`，方便区分失败来自 WebGL 启动/渲染还是语义 VLM / 验证路径。
- `scripts/webgl_render.py` 与 `scripts/run_render_batch.py` 现在即时刷新渲染日志，减少因 nohup/文件输出缓冲导致的误判”卡住”信号。
- `core/render/semantic_view_aligner.py` 现在记录拼图生成、VLM 请求发起、VLM 响应接收、决策持久化、验证启动等节点日志。

### 配置
#### WebGL 子进程超时
- `config/config.yaml` 与 `config/config.py` 新增 `render.webgl.subprocess_timeout_seconds`，为必填项，须为正整数。

### 文档
#### WebGL 超时配置示例
- `docs/guide/cli.md` 在 WebGL 配置示例中补充 `render.webgl.subprocess_timeout_seconds`。

## [v2.9.11] - 2026-03-24

### Fixes
#### run_full_experiment 并发记账与 Stage-1 调度纠偏
- `scripts/batch_process.py` 将原本耦合在 `edit_single()` 黑箱中的两个阶段显式拆开：`apply_edit_single()` 只负责 image edit + mask artifact build，`run_stage1_quality_check_single()` 只负责 Stage-1 质检与 `meta.json` 更新。
- `scripts/run_full_experiment.py` 不再把 Stage-1 隐藏在 `edit_apply` 的 image lane 内部执行；实验编排现在先走 `oneapi_image` 的 edit apply，再单独走 `oneapi_text` 的 `stage1_quality_check`。
- `scripts/run_full_experiment.py` 将 `source_prompt_optimization` 从单次 timing 包装改为正式 retry stage；当上游文本网关出现 `SSLEOFError` / TLS EOF 这类瞬时连接抖动时，会按 stage retry 处理，而不是直接让 object 失败。
- `scripts/run_full_experiment.py` 同步修正 Stage-1 失败归因：Stage-1 质检失败、relabel 终止、judge/diff 侧异常现在统一标记到 `oneapi_text` lane；仅 edit apply 自身失败才记到 `oneapi_image`。
- `scripts/webgl_render.py` 为 Playwright WebGL 渲染补充显式 `page.close()` / `context.close()` / `browser.close()` 收尾日志，修复 `source_render` 卡在 `before_browser_close` 附近长期无进展的问题。
- `scripts/run_full_experiment.py` 的 `instruction_generation` retry 现在会把上一次失败里解析出的非法指令自动加入下一次 attempt 的 `avoid_list`；对“材质/颜色替换”这类被验证器拒绝的指令，不再原样重复重试。

### Configuration
#### run_full_experiment.retry 新增 Prompt Optimization 重试项
- `config/config.yaml` 与 `config/config.py` 新增 `run_full_experiment.retry.source_prompt_optimization.max_attempts`，继续遵循 Fail Loudly：配置缺失将直接报错，不再由代码私自补默认值。

### Documentation
#### CLI 文档补充并发语义
- `docs/guide/cli.md` 补充说明：Stage-1 quality check 的 Method-2 文本/VLM 扇出调用属于 `concurrency.text` / `oneapi_text` 资源槽位，而不是 `concurrency.image`。
- `docs/guide/cli.md` 的 `run_full_experiment.retry` 示例新增 `source_prompt_optimization`。

## [v2.9.10] - 2026-03-24

### Improvements
#### Batch Generation / run_full_experiment 分阶段耗时观测增强
- `scripts/run_full_experiment.py` 为 object-level 与 edit-level 阶段新增统一 timing 记录与终端日志输出；运行日志现在会显式打印各阶段 `START/END`、阶段名、状态、attempt 序号与耗时。
- timing 结果会写入 experiment 产物中的 `object_records.jsonl`、`edit_records.jsonl` 与 `summary.json`，并新增 `stage_timing_summary.csv` 便于后续直接做 Excel / pandas 统计。
- 统计口径按“阶段实际执行次数”计算：`mean_seconds = total_seconds / sample_count`；如果某阶段发生 retry，每次 attempt 都会计入总次数与总耗时。
- `scripts/batch_process.py` 的 `edit_single()` 现已拆分记录 `edit_apply`、`mask_artifact_build`、`stage1_quality_check` 三段子耗时，并把结构化 timing 写入每个 edit batch 的 `meta.json`。
- `app.py` 与 `templates/experiment_stats.html` 已为 YAML 统计视图新增 `Stage Timing Summary` 表，可直接查看各阶段的执行次数、总耗时、平均耗时、P90、失败次数与 skipped 次数。

## [v2.9.9] - 2026-03-23

### Fixes
#### Model Detail 的 DreamSim / Mask 维护入口改为 CLI 弹窗
- `templates/model_detail_scripts.html` 将 `Update DreamSim` 与 `Generate Missing Masks` 从“前端直接 POST 后台异步任务”改为“弹出可复制的后端 CLI 命令”，避免页面内长任务经常直接失败。
- `Update DreamSim` 现在生成按 source model 限定范围的命令：`refresh-all-dreamsim --ids <model_id>`。
- `Generate Missing Masks` 现在生成按 source model 限定范围的命令：`materialize-edit-artifacts --ids <model_id>`；弹窗同时提示可追加 `--edit-id` 或 `--dry-run`。
- 生成的命令会显式包含后端项目目录切换 `cd /seaweedfs/xiaoliang/code3/2d3d_v2` 与固定 Python 解释器 `/home/xiaoliang/local_envs/2d3d/bin/python`，复制后可直接在服务器执行。
- `templates/model_detail.html` 与 `README.md` 已同步更新为“显示 CLI 命令”语义，避免 UI 文案与实际行为不一致。

#### `materialize-edit-artifacts` 支持自动重建缺失 grid
- `core/image/edit_artifact_builder.py` 现在会在历史 edit batch 缺失 `before_image_grid.png` 或 `target_image_grid.png` 时，复用统一的 `ViewStitcher` + `VIEW_ORDER` + `pad_to_square=True` 流程自动重建，再继续补齐 mask。
- `scripts/batch_process.py` 与 `app.py` 已移除对这两个 grid 的前置硬失败检查，统一改为走底层恢复逻辑；只有 source views 或其他必要输入资产缺失时才会继续显式报错。

#### Mask 差分后处理收紧，减少整圈轮廓误检
- `config/config.yaml` 与 `config/config.py` 为 `edit_artifacts` 新增 `opening_kernel_size`，并将 `diff_threshold` 从 `12` 提高到 `20`。
- `core/image/edit_artifact_builder.py` 的 mask 生成不再是裸 `RGB` 像素差分二值化，而是改为 `RGB max-abs diff + threshold + morphological opening`，优先压制由轻微几何漂移、抗锯齿和边缘阴影引起的整圈轮廓噪声。
- `edit_artifacts.edit_mask` 元数据现在会显式记录 `opening_kernel_size` 与新的 `diff_method`，便于后续复现实验参数。
- `scripts/batch_process.py` 的 `materialize-edit-artifacts` 新增 `--force`，可对已有但质量不佳的历史 mask 直接重算。

### Documentation
#### 补齐 Mask / DreamSim 相关文档联动
- `README.md` 新增 `materialize-edit-artifacts` 与 `refresh-all-dreamsim` 的使用说明，并补充 Model Detail 页面 `Generate Missing Masks` / `Update DreamSim` 的入口说明。
- `docs/guide/cli.md` 新增 `materialize-edit-artifacts` 专节，明确“仅补 mask、非 mask 资产缺失即失败（Fail Loudly）”的行为契约，并同步 `mask_backfill` 并发配置说明。
- `docs/guide/http-client.md` 新增 DreamSim 与 mask 维护 API 文档：
  - `POST /api/models/{model_id}/refresh-dreamsim`
  - `POST /api/models/refresh-dreamsim-all`
  - `POST /api/models/{model_id}/materialize-missing-masks`
  同步记录 `409` 并发冲突语义与 fail-loudly 约束。

### Improvements
#### Web UI 首屏与列表页加载性能优化
- `app.py` 新增轻量 `home` 统计路径：首页不再通过 `get_all_images()` / `get_all_models()` 全量扫盘来渲染首屏，改为前端异步请求 `GET /api/home/stats`，并使用浅层目录统计替代重型 payload 组装。
- `app.py` 为 `models` 页新增轻量索引接口 `GET /api/models/toc`，并让 `GET /api/models?page=...` 直接基于 source model ID 列表分页加载当前页详情，不再为了第一页卡片去先构建整个模型全集详情。
- `templates/models.html` 改为“空壳首屏 + TOC 异步加载 + 模型卡片无限滚动分页加载”；首屏卡片与 TOC 并行请求，避免慢 TOC 阻塞第一页内容出现。
- `app.py` 的 `/model/<model_id>` 与 `GET /api/model/<model_id>` 改为按单个 `model_id` 直接加载详情，避免打开详情页时再次全量扫描所有模型目录。
- `app.py` 为 `pairs` 页新增 `GET /api/pairs/summary`，并让分页 `GET /api/pairs?target_only=1&page=...` 走“按页扫描 + 提前停止”的快速路径，避免为了第 1 页 pair 数据先构建整个 pair 索引。
- `templates/pairs.html` 将顶部统计与分页内容拆成两个异步请求，优先显示当前页 pair 内容，再补充 summary 统计。
- `static/js/download.js` 优化单项下载逻辑：单张图片下载改走轻量 `GET /api/images/toc`，单个模型下载改走 `GET /api/models/<model_id>/path`，避免下载按钮触发整表 payload 请求。
#### DreamSim 并发控制收紧（更保守）
- `config/config.yaml` 将 `concurrency.recon_quality_check` 默认值从 `2` 下调到 `1`，Stage-2 DreamSim 默认串行执行，降低远端文件系统 I/O 冲突风险。
- `app.py` 的异步任务调度现在对 `refresh_model_dreamsim` 与 `refresh_all_models_dreamsim` 复用 `RECON_QUALITY_CHECK_SEMAPHORE`，避免多个 DreamSim 刷新任务并发踩踏。
- `app.py` 新增 DreamSim 刷新任务冲突检测：当已有 DreamSim 刷新任务处于 `pending/running` 时，相关 API 返回 `409`，防止重复提交造成叠加压力。
- `docs/guide/cli.md` 已同步更新 DreamSim 并发建议与 API 并发门禁说明。

#### Stage-2 DreamSim 改为显式四视角 + 可选灰度输入
- `config/config.yaml` 与 `config/config.py` 将 Stage-2 配置从旧的 `recon_view_policy` 改为显式 `recon_views`，当前默认固定为 `front/back/right/left` 四视角。
- Stage-2 新增 `input_mode: rgb | grayscale` 配置，允许 DreamSim 在保留 RGB 比较的同时，支持“先转灰度、再扩展回 3 通道”的输入模式，用于弱化颜色/纹理变化对几何一致性判断的干扰。
- `core/render/recon_consistency_checker.py` 现在会按 `recon_views` 精确选择参与比较的视角，并把 `views`、`input_mode` 一起写入 Stage-2 结果。
- `scripts/batch_process.py` 的 Stage-2 错误回写 payload 已同步改为新字段，避免 `meta.json` 中混用旧的 `view_policy`。
- `scripts/run_full_experiment.py` 与 `app.py` 现在会在 edit-level 记录中持久化 `stage2_views`、`stage2_input_mode`，便于后续统计和排查。
- `templates/edit_batch_card_macro.html` 与 `templates/experiment_stats.html` 已同步展示 Stage-2 当前使用的是 `RGB` 还是 `GRAYSCALE`，以及实际比较了哪些视角。
- 新增 `tests/test_recon_consistency_checker.py`，并扩展 `tests/test_config_v2.py`，覆盖灰度图加载与 Stage-2 新配置字段。

#### Models 首页新增全量 DreamSim 命令入口 
- `scripts/batch_process.py` 新增 `refresh-all-dreamsim` CLI 子命令，可统一扫描全部 source model，并仅对真实存在 target 3D 的 provider 执行 Stage-2 DreamSim 重算。
- 该子命令不会像旧的按 provider 粗扫那样把无对应 target GLB 的 edit batch 一起带上；没有 edit、没有 target 3D、或 provider id 无法识别的项会被显式统计为 skipped。
- `templates/models.html` 在首页批量操作工具栏新增 `Update All DreamSim` 按钮，点击后会弹出可直接复制的后端执行命令，而不是在前端直接发起长任务。
- 该命令入口现在固定使用服务器项目目录 `/seaweedfs/xiaoliang/code3/2d3d_v2`、Python `/home/xiaoliang/local_envs/2d3d/bin/python`，并预置代理环境变量，便于直接复制到后端执行。
- `docs/guide/cli.md` 已同步补充 `refresh-all-dreamsim` 的参数、行为说明和示例命令。
- 为降低 `/seaweedfs` 的瞬时 I/O 压力，`refresh-all-dreamsim` 现在会先在主线程预加载 `ReconConsistencyChecker`，并只把“已有 target render 视图”的 target/provider 加入刷新队列。
- `config/config.yaml` 与 `config/config.py` 新增 `concurrency.refresh_all_dreamsim`，用于给全量 DreamSim 重算单独配置更保守的并发度；默认值为 `1`。

#### Model Detail 页面新增一键更新 DreamSim
- `app.py` 新增 `refresh_model_dreamsim` 异步任务与 `POST /api/models/<model_id>/refresh-dreamsim` 接口，可对单个 source model 下面所有“已存在 target 3D”的 edit batch/provider 重新执行 Stage-2 DreamSim。
- 重算逻辑会自动扫描 `triplets/<model_id>/edited/*` 与对应的 `models_src/<model_id>_edit_<edit_id>/model_*.glb`，仅处理当前确实可计算的 target；无 target 3D、无 edit meta、或 provider id 不受支持的项会被显式记为 skipped。
- `templates/model_detail.html` 在 `Edited Versions` 标题栏新增 `Update DreamSim` 按钮；`templates/model_detail_scripts.html` 接入任务轮询，完成后会提示 refreshed/failed/skipped 数量并自动刷新页面。
- 该入口的目标是快速覆盖历史上写入 `meta.json` 的旧 Stage-2 error/failed 结果，便于在当前 DreamSim 逻辑下按 model 粒度批量重算。

## [v2.9.8] - 2026-03-21

### Improvements
#### Stage-1 relabel 单 instruction 槽位锁定
- `scripts/run_full_experiment.py` 现在会把每个 instruction 槽位的 relabel 生命周期字段持久化到 `instructions.json`，包括 terminal relabel 结果。
- 当某个槽位的 Stage-1 relabel 已经得到 terminal 结果（`passed` 或 `failed`）后，外层 stage retry 会立即停止，避免同一条 instruction 再次触发 relabel，并减少重复 orphan edit batch。
- `scripts/batch_process.py` 与 `core/image/edit_quality_checker_v2.py` 新增 `allow_stage1_relabel` 控制开关，保证 resume 时只有在该槽位还没有 terminal relabel 标记时才会重新 relabel。
- `config/config.yaml` 删除 `stage1_relabel.max_attempts`，Stage-1 relabel 现在固定为单次执行，不再保留与当前业务语义冲突的多次 relabel 配置。
- 旧的 `instructions.json` / `meta.json` 仍然兼容：如果缺少新的生命周期字段，系统会根据已有的 `instruction_display_status` 和 `stage1_relabel_result` 自动推断 relabel 状态。

#### Web UI list pages loading optimization
- `app.py` 为 `images` / `models` / `pairs` 列表扫描新增短时内存缓存，避免每次页面切换都重新全量遍历远端 pipeline 目录。
- `app.py` 新增 `GET /api/images/toc` 轻量接口，`Images` 页不再用 `per_page=9999` 拉取整份重型图片 payload 只为生成左侧目录。
- `app.py` 的 `GET /api/pairs` 新增可选分页与 `target_only=1` 过滤；`templates/pairs.html` 改为按页无限滚动加载，首屏不再一次性请求全部 pair 数据。
- `templates/models.html` 为 source/front 缩略图与详情预览补上浏览器原生懒加载，减少首屏并发图片请求。
- `app.py` 的 `Models` 页面现在会显式隔离 experiment filter 元数据读取失败；当远端 `experiments/*/manifest.json` 出现 `OSError: [Errno 5] Input/output error` 时，页面主体继续可用，并在前端展示筛选器不可用提示而不是整页 500。
- `templates/images.html` 与 `templates/pairs.html` 现在会把已加载列表、分页进度与滚动位置暂存到 `sessionStorage`；从其他页面切回时会优先恢复本地快照，再继续按需加载。
- `templates/models.html` 现在会保留排序、筛选、目录搜索与滚动位置，避免从其他页面返回后丢失当前浏览上下文。


#### run_full_experiment 切换到 adaptive instruction plan
- `scripts/run_full_experiment.py` 的 category schema 从旧的 `instruction_counts.remove/replace` 切换到新的 `instruction_plan`：
  - `mode: "adaptive_k"`
  - `count`
  - `allowed_types`
- instruction generation 改为 object 级一次性生成完整 instruction list；每条 instruction 自带最终 `type`，后续 edit / Stage1 / target gen3d / Stage2 仍保持逐条串行消费。
- prompt/object/edit 记录新增 `instruction_plan`、`instruction_count_planned`、`generated_instruction_count`，并在 prompt record 中持久化 `generated_instructions`、`type_judgment` 与 `instruction_batch_generation_mode`，便于断点恢复按已生成 instruction list 继续执行。
- `scripts/run_full_experiment.py` 与 `app.py` 现统一对旧 `instruction_counts` YAML 做显式归一化兼容：历史 YAML 可继续加载、查看、执行；新生成 YAML 统一写入新 schema。

#### Adaptive instruction generator 与 Batch Generation 页面同步更新
- `core/image/caption.py` 新增 `generate_adaptive_instructions(...)`，使用单次多模态请求返回严格 JSON，并对 `type_judgment`、`instructions`、数量、类型、重复项与 multiview-safe 约束做显式校验；非法输出直接失败，不再回退到旧单条 prompt。
- `utils/prompts.py` 新增 adaptive-k prompt 模板与 `get_adaptive_instruction_prompt(...)`。
- `/batch-generation` 页面与相关 API 改为使用 `Edit Count + Allowed Types` 配置 `instruction_plan`，不再暴露 `Remove Count / Replace Count`。
- `templates/experiment_stats.html` 与 YAML summary 改为展示 `instruction_plan`，避免继续把新计划解释成固定 remove/replace 配额。

#### run_full_experiment 新增 GPU ID 限制入口
- `scripts/run_full_experiment.py` 新增 `--gpu-id` 参数，并会在加载主要业务模块前设置 `CUDA_VISIBLE_DEVICES` / `NVIDIA_VISIBLE_DEVICES`，让本次实验进程及其后续子任务默认只暴露指定 GPU。
- `app.py` 生成的 `plan / resume / repair` CLI 与后端异步执行入口现统一默认追加 `--gpu-id 0`，避免页面展示命令和实际执行参数不一致。
- `/batch-generation` 页面新增提示文案，明确要求运行前先手动检查目标机器上的 GPU 编号；实验 run detail 也会显示本次记录使用的 `gpu_id`。
- `run_full_experiment.py` 现在会在日志开头主动打印当前 Python 进程 PID、运行模式、`gpu_id` 与目标 plan/experiment_id，便于后续按 PID 中断对应实验。
- `run_full_experiment.py` 现在还会在退出前主动打印统一的 `finished` 日志行；无论是完整跑完、异常失败，还是手动中断，都能从日志里明确看出这次运行已经结束。

## [v2.9.7] - 2026-03-19

### Improvements

#### run_full_experiment 重试、lane cooldown 与断点续跑增强
- `config/config.yaml` 新增 `run_full_experiment` 编排配置段，集中定义各阶段 `max_attempts`、lane `cooldown_seconds` 与 `object_workers` 调度参数；不再把 API 参数混入实验编排段。
- `config/config.yaml` 新增 `concurrency.text`，文本链路并发不再复用 `concurrency.image`，而是显式从 `config.concurrency` 读取。
- `scripts/run_full_experiment.py` 现已为 `source_t2i`、`source_gen3d`、`source_render`、`instruction_generation`、`edit_apply`、`stage1`、`target_gen3d`、`target_render`、`stage2` 接入统一重试执行器。
- `scripts/run_full_experiment.py` 新增 lane 级 FIFO 排队与 cooldown 机制：同一 API lane 发生错误后会进入静默期，恢复时先放行单个 probe 请求，避免并发继续顶满故障链路。
- edit/object 记录新增 `retry_meta`、`attempt_errors`、`attempt_count`、`failed_stage`、`api_lane` 等字段，保留完整 attempt 错误链路，便于排障与科研追溯。

#### Hunyuan 并发压力下调
- `oneapi.timeout` 从 `120` 提升到 `300`，避免 Hunyuan submit 阶段在高压下过早超时。
- `concurrency.gen3d.hunyuan` 从 `5` 下调到 `3`，配合 lane cooldown 减少 `httpx.WriteTimeout` 风险。
- `utils/experiment_concurrency.py` 改为新的 provider-weighted `object_workers` 公式，并显式受 `object_workers_cap` 与 `provider_pressure_divisor` 控制。

#### Batch Generation 页面新增运行实例管理
- `/batch-generation` 页面新增 `Experiment Runs` 区块，和 YAML 模板列表分离展示。
- 新增 `GET /api/experiment-plan/runs`、`GET /api/experiment-plan/run-detail`、`POST /api/experiment-plan/resume`、`POST /api/experiment-plan/repair`。
- 前端可直接查看 run 状态、复制 `Resume CLI` / `Repair CLI`，并从页面一键触发恢复。
- Batch Generation 页面生成的 `plan / resume / repair` CLI 现在会先 `mkdir -p` + `touch` 预创建日志文件，并改用 `tail --retry -f` 跟随日志，避免 `/seaweedfs` 上日志文件延迟可见时立刻报 `tail: cannot open ...`。

#### WebGL 渲染 GPU 监控增强
- `scripts/webgl_render.py` 现在会在 WebGL 渲染日志中输出 `nvidia-smi` GPU 快照，包含浏览器启动前、启动后、渲染完成后、关闭前的显存与 `pmon` 信息，便于排查 WebGL 是否受 GPU/显存竞争影响。

#### 编辑 instruction prompt 收敛
- `utils/prompts.py` 中的 remove / replace instruction prompt 现更强地强调大而可见的部位、禁止 texture / color 修改、`replace` 禁止 intangible replacement、`remove` 需产生更明显的构图变化。
- instruction examples 已收敛为更稳定的 `Remove the <part> from the <object>` / `Replace the <part> with <part>` 风格，并补充了应避免的反例，减少模型继续输出小装饰或抽象替换的概率。

## [v2.9.6] - 2026-03-18

### ✨ Improvements

#### run_full_experiment 并发调度优化（保守版）
- `scripts/run_full_experiment.py` 的顶层调度从 `category worker` 进一步调整为**全局 object worker 池**，不再让慢速 object 长时间绑死某个 category 的唯一 worker。
- 本次优化仍保持**单个 object 内 edit 串行**，不引入 edit 级 fully async，优先保证断点恢复语义、路径稳定性与记录落盘的一致性。
- `manifest.json` 与 `summary.json` 新增 `object_workers`、`scheduler_mode` 等信息，便于后续排查吞吐瓶颈与确认实际运行的调度策略。

#### Models 页面实验过滤增强
- `Models` 页面新增实验维度过滤入口，可在 `All Models`、`Provider Pair`、`YAML` 三种模式间切换。
- 支持按 **source provider + target provider** 过滤模型，便于查看某条 provider 链路生成的 source models。
- 支持按 **YAML 文件** 过滤模型，便于回溯某个实验计划对应的全部产出。
- 实验过滤仍可继续叠加 `status`、`category` 与左侧搜索；左侧目录与右侧模型卡片保持同一套过滤结果，不再出现显示不一致。

#### Experiment Stats 页面交互增强
- `Experiment Stats` 页面中的两个 `Category Summary` 表现在支持按列点击排序，可对 `Category`、`Samples`、`Edit Attempts`、`Stage1 Fail Rate`、`Stage2 Entered Rate`、`DreamSim Mean`、`DreamSim Std` 做升序/降序切换。
- Provider Summary 与 YAML Results 共用同一套前端排序状态与交互，重新加载当前统计结果后仍会按当前选中的排序列重新渲染。
- 当前激活的排序列会显示方向指示，便于快速比较不同 category 在失败率、进入率与 DreamSim 指标上的相对表现。

### 🔧 Changes

- `scripts/run_full_experiment.py`
  - 新增保守版全局 `object worker` 调度
  - 补充 prompt/record 相关并发保护
  - 在统计输出中写入 `object_workers` 与 `scheduler_mode`
- `app.py`
  - 为 `Models` 页面构建 experiment filter payload
  - `Experiment Stats` 在缺少最终 records 时支持基于 partial records 恢复统计
- `templates/models.html`
  - 新增 `Provider Pair` / `YAML` 互斥实验过滤模式
- `templates/experiment_stats.html`
  - Category Summary 支持按列排序

## [v2.9.5] - 2026-03-17
- Batch Generation 新增 `Existing YAML Plans`，按 YAML 生成时间降序列出历史 plan，并支持将历史 YAML 回填到当前表单中直接修改。
- 新增 `GET /api/experiment-plan/history` 和 `GET /api/experiment-plan/load`，用于列出与回填已生成的 YAML。
- 新增 `POST /api/experiment-plan/execute`，可在后端异步执行选中的 YAML，并返回 `task_id` 供 `Tasks` 页面追踪。
- `Existing YAML Plans` 已调整到 Batch Generation 页面顶部，方便先选择历史模板再继续配置。
- 加载历史 YAML 时，会兼容清洗旧版 `random.category=true` 条目中残留的 `objects` 与 `category_name` 字段，再回填到当前表单。
- `CLI Command` 不会在仅加载历史 YAML 时提前显示；只有再次点击 `Generate YAML` 后，才会显示本次新 YAML 对应的 CLI。

### ✨ Improvements

#### run_full_experiment 批量实验流程重构
- `scripts/run_full_experiment.py` 切换到新的固定数量 schema，不再使用 quota 驱动的重试式实验逻辑。
- 删除旧字段：`category_workers`、全局 `instruction_type_ratio`、`prompt_budget`、`target_source_models`、`accepted_edits_per_model`、`max_instruction_attempts`、`style_ids`。
- `category` 级并发统一改为按 provider 组合动态推导，不再单独配置固定值。
- 每个 category 现在支持三种模式：
  - 固定 category + 固定 objects
  - 固定 category + random objects
  - random category + random objects
- 每个 object 按配置的 `instruction_counts.remove` / `instruction_counts.replace` 固定生成编辑指令数量，每条指令只执行一次；失败直接记录，不再补跑。

#### 实验统计与明细落盘增强
- `run_full_experiment.py` 新增以下实验输出：
  - `experiments/<experiment_id>/object_records.jsonl`
  - `experiments/<experiment_id>/edit_records.jsonl`
- `experiments/<experiment_id>/category_stats.json`
- `experiments/<experiment_id>/category_stats.csv`
- `run_full_experiment.py` 现在会在实验启动初期就写出 `execution_plan.json`，将 random category / random object 的实际执行结果冻结下来，便于后续断点恢复与复现。
- 实验运行过程中会持续增量写入 `object_records.jsonl`、`edit_records.jsonl`、`summary.json` 与 `manifest.json`，中断后不必等到整轮结束才能恢复统计。
- 新增 `--repair-experiment-id`，可为中断实验重建当前可见的统计文件。
- 新增 `--resume-experiment-id`，可在同一个 `experiment_id` 下继续补跑未完成的对象与编辑任务。
- 每条 edit 现在会记录 `edit_id`、Stage1 状态、Stage2 DreamSim 分数、失败原因等关键信息，便于后续按 provider / category / object 做能力统计。
- `summary.json`、`summary.csv`、`manifest.json` 会同步反映新的固定数量执行逻辑和统计输出。
- `run_full_experiment.py` 的顶层调度器进一步切到保守版**全局 object worker 池**：不再让 worker 长时间被单个 category 绑死，但单个 object 内 edit 仍保持串行，优先保证恢复语义和正确性。

#### Batch Generation 前端配置页更新
- `/batch-generation` 页面改为生成新 schema 的 YAML：
  - 去掉 `category workers` 可编辑输入，改为只读显示动态推导值
  - 去掉全局编辑比例、style 相关配置
  - 新增 `random.category` / `random.object` 配置
  - 新增固定 objects 多选、`object_count`、每物体 remove/replace 数量输入
- Batch Generation 页面进一步改为“category 数量驱动”：
  - 新增 `Category Count`
  - 新增全局 `Randomly choose N distinct categories`
  - `Category Count` 会按 `categorized_objects.json` 中的 category 总数自动限制最大值
  - 固定 category 模式下会生成 N 个必须填完的 category 模板
  - 随机 category 模式下会用一个共享模板复制 N 次，并在运行时挑选 N 个不同 category
  - 固定配置的 category_name 也不允许重复
  - 同一个 YAML 内多条 random category 配置保证落到不同 category，避免类别分布失衡
- CLI Command 面板新增一键复制入口，配置变化时会自动重置 YAML/CLI 预览
- 前端生成的 CLI 命令现在会自动以 `nohup` 形式在后台启动实验，并在前台执行 `tail -f log`；即使 SSH 断开，后台实验也会继续运行。日志文件名包含时间、yaml 名称、category 数量以及各 category 的 `object_count`
- `GET /api/experiment-plan/options` 现在返回 `objects_by_category`
- 新增 `GET /api/experiment-plan/derived-category-workers`，按 provider 组合返回动态推导的 category worker 数和 breakdown
- `POST /api/experiment-plan/generate` 与后端校验逻辑同步到新 schema，并显式拒绝旧字段

#### Experiment Stats 页面
- 新增 `/experiment-stats` 页面，可按 `source_provider` + `target_provider` 显式查看聚合后的 category 统计值。
- 新增 API：
  - `GET /api/experiment-stats/options`
  - `GET /api/experiment-stats/category-summary`
- 统计页默认展示最实用的代表性指标：`sample_count`、`stage1_failed_rate`、`stage2_entered_rate`、`stage2_dreamsim_mean`、`stage2_dreamsim_std`
- `Experiment Stats` 现在支持双模式查看：
  - `Provider Summary`：按 provider 组合查看聚合 category 指标
  - `YAML Results`：按 YAML 查看对应的 experiment runs、category summary、object summary、edit records
- 新增 YAML 结果查看 API：
- `GET /api/experiment-stats/yaml-options`
- `GET /api/experiment-stats/yaml-summary`
- `GET /api/experiment-stats/yaml-details`
- 对于缺少最终 `object_records/edit_records` 的中断实验，后端现在会尝试从 partial records 恢复统计结果，并在页面文案中明确提示 recovered partial runs。
- `Experiment Stats` 页面布局收紧到卡片内部滚动，避免 YAML 视图把整页撑出水平滚动条

#### Models 页面筛选增强
- `Models` 页面新增实验维度过滤与 `Category` 下拉筛选，可按实验来源与物体类别组合过滤相关模型。
- 左侧 `Directory` 列表会与右侧模型卡片保持同一套筛选结果，不再出现左右显示不一致。
- 左侧搜索现在会同时匹配 `model id`、`provider`、`category` 与 `object name`，便于快速定位指定物体。
- 新增 `Clear Filters`，可一键重置状态筛选、provider 筛选与 category 筛选。
- `Models` 页面进一步改为两种互斥的实验过滤模式：
  - `Provider Pair`：按 `source provider + target provider` 过滤该实验链路生成的 source models
  - `YAML`：按实验 YAML 过滤该 YAML 产生的全部 source models
- `Provider Pair` 与 `YAML` 过滤互斥，但仍可继续叠加 `status`、`category` 与左侧搜索。

### 🔧 Changes

- `scripts/run_full_experiment.py`
  - 重写 plan 解析、执行逻辑、失败记录和实验统计落盘
- `config/config.yaml`
  - 移除 `concurrency.run_full_experiment.category_workers`
- `config/config.py`
  - 移除 `RunFullExperimentConcurrencyConfig`
  - `ConcurrencyConfig` 不再包含 `run_full_experiment`
- `app.py`
  - Batch Generation 相关 API 校验与 YAML 生成切换到新 schema
- `templates/batch_generation.html`
  - 表单重构为 fixed-count 配置模式
- `templates/experiment_stats.html`
  - 新增 provider-pair 统计查看页
- `templates/models.html`
  - 新增 `Provider Pair` / `YAML` 互斥实验过滤、左侧目录联动过滤与扩展搜索
- `templates/base.html`
  - 导航栏新增 "Experiment Stats" 入口
- `docs/guide/cli.md`
  - 更新 `run_full_experiment.py` 的 plan 示例、字段说明、输出说明

## [v2.9.4] - 2026-03-16

### ✨ New Features

#### 前端 Batch Generation 配置页面
- 新增 `/batch-generation` 页面，用于可视化配置 `run_full_experiment.py` 的实验计划参数。
- 支持配置的参数包括：
  - 基本设置：计划名称、source/target provider、edit mode、category workers
  - 全局编辑类型比例：remove/replace 权重
  - 类别配置（可增删多行）：
    - 类别名称（下拉选择，数据来自 `categorized_objects.json`）
    - prompt_budget、target_source_models、accepted_edits_per_model、max_instruction_attempts
    - 可选 style_ids 多选（数据来自 `3d_style.json`）
    - 可选覆盖全局 instruction_type_ratio
- 一键生成功能：
  - 校验参数（Fail Loudly，任何必填字段缺失或非法直接报错）
  - 生成 YAML 并保存到 `/seaweedfs/xiaoliang/data/2d3d_data/experiment_plans/`
  - 生成 CLI 命令并支持一键复制
- 导航栏新增 "Batch Generation" 入口

### 🔧 Changes

- `app.py`
  - 新增页面路由：`GET /batch-generation`
  - 新增 API：`GET /api/experiment-plan/options`
    - 返回 providers、edit_modes、categories、styles 选项
  - 新增 API：`POST /api/experiment-plan/generate`
    - 严格校验参数（分类名、style_id、provider 必须有效）
    - 生成 YAML 文件并返回 CLI 命令
    - 文件命名格式：`<timestamp>_<slug(name)>.yaml`
- `templates/base.html`
  - 导航栏新增 "Batch Generation" 链接
- `templates/batch_generation.html`（新建）
  - 完整的配置表单 + YAML/CLI 预览 + 操作按钮

### 📋 Technical Details

- **路径约束**：生成的 YAML 文件仅写入固定目录 `/seaweedfs/xiaoliang/data/2d3d_data/experiment_plans/`，禁止自定义绝对路径
- **Python 解释器**：CLI 命令固定使用 `/home/xiaoliang/local_envs/2d3d/bin/python`
- **文件名安全**：slug 化处理，禁止路径穿越（`..`、`/`、`\`）
- **Fail Loudly**：任何必填参数缺失或非法，后端直接 400 返回明确错误

## [v2.9.3] - 2026-03-16

### ✨ Improvements

#### 模型详情页：编辑质检展示简化并增强可读性
- 减少模型详情编辑卡片中的重复 QC 区块，Stage 2 结果不再在三个位置重复展示。
- 新增紧凑的 Stage 1 摘要，明确显示是通过哪种方法完成质检：
  - `Method-1 (Grid VLM)`
  - `Method-2 (Two-Stage Recon)`
- 新增紧凑的 Stage 2 摘要，在卡片头部直接展示各 provider 的目标模型结果，`tripo` 与 `hunyuan` 可清晰区分。
- 保留每个 Target 3D 面板中的 provider 级 Stage 2 状态展示，并与当前激活的 provider tab 对齐。
- 将详细 QC 诊断收敛到一个统一的 `QC Details` 折叠区：
  - Stage 1A 解释与原始 diff 输出
  - Stage 1B 解释与原始 judge 输出
  - Stage 2 按 provider 展示的一致性细节
- 修复 Stage 1 详情渲染逻辑，使其读取 Method-2 的真实 payload 结构：
  - Stage 1A 现在按视角显示 `diff_text`
  - Stage 1B 现在按视角显示 `decision` 和 `reason`

### 🔧 Changes

- `templates/edit_batch_card_macro.html`
  - 重构编辑批次 QC 布局为：
    - 紧凑的 Stage 1 / Stage 2 摘要行
    - 感知 provider 的 Target 3D Stage 2 状态徽标
    - 统一的 `QC Details` 诊断折叠区
  - 删除左侧重复的 `Stage2 Check (by target model)` 摘要区块
  - 删除卡片底部重复的独立 `Target 3D Consistency` 区块

#### 新增实验 CLI：从 prompt 到 target 的全流程管线
- 新增 `scripts/run_full_experiment.py`，用于按类别分层实验，覆盖以下流程：
  - prompt 生成
  - T2I 图像生成
  - source 3D 生成
  - source 多视角渲染
  - 可配置的 remove/replace 指令生成
  - 编辑质量检查
  - target 3D 生成
  - target 渲染 + Method-2 Stage 2 一致性检查
- 当 before/after 图像 QC 失败时，脚本会停止当前候选编辑并继续下一个 instruction 候选。
- 脚本要求显式提供 YAML/JSON 计划文件定义数量与比例，不再把实验配额隐藏在代码中。

#### 面向实验的 Prompt / Instruction 溯源增强
- `core/image/prompt_optimizer.py`
  - 新增 `optimize_prompt_with_metadata(...)`，暴露以下元信息：
    - `style_id`
    - `style_name_en`
    - `style_name_zh`
    - `style_prefix`
    - `object_description`
- `core/image/caption.py`
  - 将运行期多视角安全 instruction 后缀提取为：
    - `INSTRUCTION_MULTIVIEW_EXTRA_CONSTRAINT`
- 以上改动让实验清单能够更准确记录真实 prompt 溯源和 instruction prompt payload。

## [v2.9.2] - 2026-03-15

### ✨ Improvements

#### Method-2 Stage1 新增 `stitched_6` 视角策略
- `edit_quality_check.two_stage_recon.edit_view_policy` 新增可选值：`stitched_6`。
- 当使用 `stitched_6` 时，Stage1 会先将 before/after 六视角分别拼接成 3x2 图，再执行一次 Stage1A（VLM diff）+ Stage1B（LLM judge）。
- 保持原有策略不变：
  - `front_only`：只检查 front
  - `all_6`：逐视角检查 6 张图

### 🔧 Changes

- `config/config.py`
  - 放宽 `edit_view_policy` 校验，支持 `front_only|all_6|stitched_6`
- `core/image/edit_quality_checker_v2.py`
  - 新增 `stitched_6` 执行分支
  - 复用 `ViewStitcher` 生成 before/after 六视角拼图并执行单次两阶段判定
- `config/config.yaml`
  - `edit_view_policy` 注释更新为 `front_only | all_6 | stitched_6`
- `docs/guide/cli.md`
  - 更新 Method-2 Stage1 行为说明（补充 `stitched_6`）

## [v2.9.1] - 2026-03-14

### ✨ Improvements

#### Method-2 Stage2 CLI 与终端输出完善
- `check-target-consistency` 完整接入批处理主流程，可按 `--provider / --ids / --edit-id / --target-ids` 精确过滤。
- 新增并稳定支持 `--skip-render` 纯检测模式（不触发新渲染、不生成新模型副作用）。
- Stage2 执行时终端打印结构化结果：
  - `[Stage2] status/provider/provider_id/score/threshold`
  - 完整 JSON payload（便于排查和复核）。

#### 多 Provider Stage2 结果落盘与前端绑定
- Stage2 结果同时写入：
  - `target_quality_check`（兼容旧字段）
  - `target_quality_checks_by_provider[{provider_id}]`（多 provider 精确绑定）
- `app.py` 读取 target meta 时按 `provider_id` 回填到每个 `target_3d_models[i].target_quality_check`，避免 hunyuan / tripo 混用同一结果。

#### 模型详情页 Stage2 可视化增强
- Target provider tab 名称去除 `S2 Err` / `S2 pass` / `S2 fail` 后缀，避免污染 provider 命名。
- 编辑卡片左侧（Edited Views 下方）新增 Stage2 汇总区：
  - 按 target provider 展示 `status / score / threshold`
  - 无结果明确显示 `none`。

### 🐛 Bug Fixes

#### 新增按 Provider 删除 Target 模型能力（修复 aligned 目标残留）
- 新增后端接口：
  - `DELETE /api/models/<model_id>/edits/<edit_id>/targets/<provider_id>`
- 删除行为：
  - 删除 `models_src/{model_id}_edit_{edit_id}/model_{provider_id}.*`
  - 删除 `triplets/{model_id}_edit_{edit_id}/views/{provider_id}/`
  - 清理 target meta 中该 provider 的 Stage2 结果
  - 在无剩余 provider 模型时清理空目录。
- 前端 `Del` 按钮已接入上述接口，可直接删除单个 target provider（如 `hy3_aligned`）。

### ✅ Validation

- `python3 -m py_compile app.py` 通过
- `python3 -m py_compile scripts/batch_process.py` 通过


## [v2.9.0] - 2026-03-13

### ✨ New Features

#### Method-2 编辑质量检查系统（Two-Stage LLM + Target 3D Consistency）

新增可切换的编辑质量检查方法，与现有 Method-1（`grid_vlm`）并行可选：

**Stage 1: Two-Stage LLM Edit Correctness**
- Stage 1A（VLM Diff）：将编辑前/后的视角图像发送给 VLM，生成结构化差异描述（不含 instruction）
- Stage 1B（LLM Judge）：将差异描述 + instruction 发送给 LLM 文本模型，判定 pass/fail（不见图像）
- 支持 `front_only` 和 `all_6` 视角策略，`require_all_views_pass` 控制多视角判定逻辑
- 新增模块：`core/image/edit_quality_checker_v2.py`

**Stage 2: DreamSim Target 3D Reconstruction Consistency**
- 对 Target 3D 渲染视角与编辑后视角计算 DreamSim 感知距离
- 支持 `front_only` 和 `matched_views` 策略，`max`/`mean` 聚合方式
- DreamSim 模型懒加载，per-device 单例缓存，支持 `cuda`/`cpu` 配置
- 新增模块：`core/render/recon_consistency_checker.py`

**Quality Router**
- 新增 `core/image/edit_quality_router.py`：统一方法调度
  - `create_quality_checker(config)`：根据 `edit_quality_check.method` 返回对应 checker
  - `build_quality_check_meta(*)`：统一构建 meta.json 的 `quality_check` 字段（替换 app.py 和 batch_process.py 中的局部 helper）
  - `get_checker_info(config)`：获取当前方法的 provider/model
- `app.py` 和 `scripts/batch_process.py` 中原有的 `_build_quality_check_meta()` 已移除，统一调用 router

### 🔧 Changes

#### 配置扩展
- `config.yaml` 新增：
  - `edit_quality_check.method`: `"grid_vlm"` (默认) 或 `"two_stage_recon"`
  - `edit_quality_check.two_stage_recon` 子配置（edit_view_policy, require_all_views_pass, diff_output_format, metric, recon_view_policy, aggregate, threshold, device）
  - `tasks.edit_quality_check_diff` / `tasks.edit_quality_check_judge`：Stage 1A/1B 使用的模型配置
  - `concurrency.recon_quality_check`：Stage 2 并发控制
- `config.py` 新增：
  - `TwoStageReconConfig` dataclass
  - `EditQualityCheckConfig` 扩展 `method` 和 `two_stage_recon` 字段
  - `Config.edit_quality_diff_mllm` / `Config.edit_quality_judge_mllm` 属性
  - 全部使用 `_require_key()` / `_require_section()` 严格校验（Fail Loudly）

#### 前端 Method-2 结果展示
- 编辑卡片显示 `method` 标签（purple badge：`grid_vlm` 或 `two_stage_recon`）
- Method-2 失败卡片展示 Two-Stage Edit Correctness 详情：
  - Stage 1A VLM Diff 结果（可折叠）
  - Stage 1B LLM Judge 判定与推理（可折叠）
  - checked_views / view_policy 信息
- Target 3D Consistency 分数面板（DreamSim metric / threshold / per-view scores）
- QC Cmd 弹窗显示当前方法信息
- `app.py` 新增 `target_quality_check` 字段到 batch_payload（从 target model meta.json 加载）

#### 并发控制
- `app.py` 新增 `RECON_QUALITY_CHECK_SEMAPHORE` 初始化
- `scripts/batch_process.py` 新增 `self.recon_quality_check_semaphore`

### ✅ Validation

- `python -m compileall core/image/edit_quality_checker_v2.py` 通过
- `python -m compileall core/render/recon_consistency_checker.py` 通过
- `python -m compileall core/image/edit_quality_router.py` 通过
- `python -m compileall app.py` 通过
- `python -m compileall scripts/batch_process.py` 通过
- Method-1 (`grid_vlm`) 路径不受影响，向后兼容


## [v2.8.3] - 2026-03-11

### ✨ New Features

#### 编辑结果质量重检命令（支持历史 edited 批次）
- 新增 CLI 子命令：`python scripts/batch_process.py check-edit-quality`
- 支持按 `--ids` / `--edit-id` 过滤重检范围，支持 `--dry-run` 预览。
- 重检会读取历史 `meta.json` 中的 `instruction`，并对“编辑前/编辑后六视角拼图”重新判定。
- 结果写回 `triplets/{model_id}/edited/{edit_id}/meta.json`：
  - `edit_status=passed` -> 归入 Edited Versions
  - `edit_status=failed_quality` / `error_quality_check` -> 归入 Failed Editing

#### 模型详情页新增编辑质检命令入口
- 在 `Edited Versions` 与 `Failed Editing` 每条记录新增 `QC Cmd` 按钮。
- 点击后可直接生成后端执行命令（`check-edit-quality`），用于对该条 edit 批次复检。

### 🔧 Changes

#### 编辑检测 Prompt 细化
- `core/image/edit_quality_checker.py` 中的检测 `user_prompt` 升级为分步评估模板：
  - target 识别
  - 同视角前后对比
  - 跨视角一致性
  - 非目标区域保持
  - 可见性规则
  - artifact 检查
- 保持二元判定输出：`{"decision":"pass|fail","reason":"..."}`。

#### 编辑列表排序规则强化（最新优先）
- 模型详情页的 `Edited Versions`/`Failed Editing` 列表按 `created_at` 降序展示（最新在最前）。
- `app.py` 排序逻辑改为优先按解析后的 ISO 时间戳排序，兼容 `Z` 时区与异常时间格式回退，降低历史数据格式不一致导致的错序风险。

#### 前端模板去重复（可维护性提升）
- 将编辑卡片渲染抽取为共享宏：`templates/edit_batch_card_macro.html`
- `Edited Versions` 与 `Failed Editing` 共用同一套卡片结构，仅通过参数控制差异行为（如 `Restore` 按钮仅失败卡片显示）。

### ✅ Validation

- `python -m compileall scripts/batch_process.py` 通过
- `python -m compileall app.py` 通过
- `python -m compileall core/image/edit_quality_checker.py` 通过
- `python scripts/batch_process.py check-edit-quality --help` 可正常输出
- `model_detail.html` Jinja 模板加载验证通过


## [v2.8.2] - 2026-03-10

### ✨ New Features

#### 多视角编辑支持任务级 Guardrail Prompt（配置驱动）
- 在 `tasks.multiview_editing` 新增：
  - `guardrail_prompt_enabled`
  - `guardrail_prompt.version`
  - `guardrail_prompt.text`
- 行为：当 `guardrail_prompt_enabled=true` 时，编辑请求会将固定约束 Prompt 拼接到最终调用 Prompt 前缀。

### 🔧 Changes

#### 新增可复用 Prompt Guardrail 组装器
- 新增 `utils/prompt_guardrail.py`，统一提供：
  - 任务配置读取与校验（按 `task_name`）
  - `final_prompt` 拼接（`guardrail + task_context + user_instruction`）
  - `prompt_trace` 元数据构建
- 该流程可在后续任务中复用，只需切换任务名与任务配置。

#### 多视角编辑链路接入 guardrail
- `core/image/multiview_editor.py` 接入统一组装器。
- API 调用改为使用 `final_prompt`。
- `editor_metadata` 新增：
  - `final_prompt`
  - `prompt_trace`
  - `guardrail_prompt_enabled`
  - `guardrail_prompt_version`
- 保留 `enhanced_instruction` 字段并映射到 `final_prompt`，确保兼容历史字段读取。

### ✅ Validation

- `python3` 配置解析验证通过（可读取 `multiview_editing` guardrail 配置并成功解析）。
- `python3 -m compileall` 通过（`config/config.py`, `utils/prompt_guardrail.py`, `core/image/multiview_editor.py`, `app.py`, `scripts/batch_process.py`）。
- 运行级联调未执行：当前环境缺少 `PIL` 依赖（`ModuleNotFoundError: No module named 'PIL'`）。

## [v2.8.1] - 2026-03-09

### 🐛 Bug Fixes

#### CLI 生成 Prompt 与 Web Prompts 页面目录不一致
- **问题**: `scripts/generate_prompts.py` 默认写入 `PROJECT_ROOT/data/pipeline/prompts`，而 Web `/prompts` 读取 `config.workspace.pipeline_dir/prompts`，导致命令行生成结果在网页不可见。
- **解决方案**: `core/image/generate_prompts.py` 默认输出目录改为 `config.workspace.pipeline_dir/prompts`（相对路径仍按项目根目录解析）。
- **结果**: 命令行生成的 prompt 可直接在 Web Prompts 页面展示。

#### CLI Prompt 生成时配置字段不匹配导致崩溃
- **问题**: CLI 路径访问 `config.qh_mllm.model`，但 `QnMllmConfig` 实际字段为 `default_model`，触发 `AttributeError`。
- **解决方案**: 对齐 Web API `/api/prompts/generate` 的记录结构，移除 CLI 专有的 `generator_model` / `generator_config_snapshot` 字段。
- **结果**: `scripts/generate_prompts.py` 不再在写入元数据阶段崩溃。

#### 图像编辑尺寸参数未统一透传导致配置尺寸不稳定
- **问题**:
  - 部分编辑链路仅设置 `auto_size=False`，但未显式传入 `size`，请求层可能收到空尺寸参数。
  - 单图编辑、引导编辑、多视角编辑在尺寸透传上不一致，导致输出尺寸行为不稳定。
- **解决方案**:
  - 在编辑入口统一显式传入 `size=self.config.size`，并固定 `auto_size=False`。
  - 在 `ImageApiClient` 增加 `size` 必填校验，`size` 为 `None` 或空字符串时立即抛出 `ValueError`（显式失败）。
  - T2I 的 Response API 路径同步改为显式使用 `config.size`，避免遗漏尺寸参数。
- **结果**:
  - 图像输出尺寸统一由 `config.yaml` 中模型对应的 `size` 驱动。
  - 尺寸配置缺失时本地立刻报错，不再静默降级到远端默认行为。
- **修改文件**:
  - `utils/image_api_client.py`
  - `core/image/editor.py`
  - `core/image/guided_view_editor.py`
  - `core/image/multiview_editor.py`

### ✨ Improvements

#### Prompts 页面新增 Show CMD（Generate T2I Prompts）
- 在 `templates/prompts.html` 的 Generate T2I Prompts 弹窗中新增 `Show CMD` 按钮。
- 支持按当前表单参数生成并复制命令（`--count` + 可选 `--category`），便于在服务器终端直接执行。

## [v2.8.0] - 2026-03-08

### 🚀 New Features

#### 渲染语义定向对齐流程（Semantic Alignment）
- **问题**: 固定六视角渲染在部分模型上会出现“front 语义错误”，影响后续编辑与评估一致性。
- **解决方案**: 在 `scripts/run_render_batch.py` 增加语义对齐流程（Fail Loudly，不做静默回退）。

**流程**:
1. 首轮渲染到 `views/{provider_id}/_semantic_tmp/first_pass_views/`
2. VLM 判定语义 front（front-only）
3. 计算刚体旋转矩阵并导出 `model_{provider_id}_aligned.glb`
4. 基于 aligned GLB 重渲染最终 6 视角到 `views/{provider_id}/`（覆盖同名旧图）
5. 可选二次验证（`verify_after_rerender=true` 时不通过即失败）

**元数据写入**:
- 渲染后写入 `views/{provider_id}/meta.json.semantic_alignment`
- 包含字段：`decision.semantic_front_from`、`confidence`、`reason`、`rotation_matrix`、`source_glb`、`aligned_glb`、`verify_passed`

**文件变更**:
- `core/render/semantic_view_aligner.py` - VLM 判定、严格 JSON 校验、旋转矩阵计算与合法性校验（正交 + `det=1`）
- `scripts/bpy_align_standalone.py` - GLB 旋转对齐子进程脚本
- `scripts/run_render_batch.py` - 语义对齐流程接入与 `meta.json` 写入
- `config/config.yaml` - 新增 `render.semantic_alignment` 配置段
- `config/config.py` - 新增 `SemanticAlignmentConfig` 并使用 `_require_key()` 严格解析

### 🔧 Changes

#### 配置约束与校验（Fail Loudly）
- `render.semantic_alignment.vlm_model` 必须存在于 `oneapi.text_models`
- `render.semantic_alignment.min_confidence` 必须在 `[0,1]`
- `render.semantic_alignment.enabled=true` 时要求 `render.rotation_z == 0`

### 🐛 Bug Fixes

#### 前端渲染视图展示一致性修复
- **问题**: 语义对齐后前端可能继续展示旧图（缓存命中 / 临时目录干扰）。
- **解决方案**:
  - 前端仍固定读取 `views/{provider_id}/` 标准目录，语义对齐结果覆盖原图，不切换展示路径
  - 过滤 `_semantic_tmp` 等临时目录，避免调试资产进入展示列表
  - 详情页与视图 API 增加 no-store/no-cache，图片 URL 增加版本参数，强制刷新最新图

**文件变更**:
- `app.py` - 视图收集过滤临时目录、动态响应 no-store/no-cache、渲染图缓存策略修正
- `templates/model_detail.html` - 渲染图 URL 增加版本参数（防浏览器缓存）

## [v2.7.2] - 2026-03-03

### 🔧 Refactor

#### scripts/ 与 tests/ 目录整理
- **问题**: `scripts/` 中混放了 7 个测试/调试脚本，违反职责分离原则
- **解决方案**: 将所有测试脚本移至 `tests/`，在 AGENTS.md 中制定目录结构约束

**移动文件**（`scripts/` → `tests/`）:
- `test_config_v2.py` - 配置 V2 验证脚本
- `test_view_selection_toggle.py` - 视角选择开关测试
- `test_gen3d_dry_run.py` - gen3d dry-run 测试
- `test_tripo_view_selection.py` - Tripo 视角选择调试工具
- `test_multiview_stitch.py` - 多视角拼接调试脚本
- `test_guided_view_editor.py` - GuidedViewEditor 测试
- `check_angles.py` - 角度打印小工具

**新增规范**（AGENTS.md）:
- `scripts/` 准入规则：仅限生产 pipeline 脚本，禁止 `test_*` 前缀
- `tests/` 范围定义：单元测试、集成测试、手动测试、调试工具、一次性验证
- 强制规则：新增脚本前必须先判断归属

**文档路径更新**:
- 更新 README.md、CHANGELOG.md、HANDOVER.md 及 docs/archive/ 中所有对移动文件的引用

---

## [v2.7.1] - 2026-03-03

### 🚀 Performance

#### 服务端静态文件缓存优化
- **问题**: 前端加载 GLB 文件很慢（50MB+ 文件），每次访问都重新下载，无 HTTP 缓存
- **解决方案**:
  - 添加 HTTP 缓存头 (`Cache-Control`)
  - 添加 CORS 支持 (`Access-Control-Allow-Origin: *`)
  - 统一缓存处理函数 `_make_response_with_cache()`

**缓存策略**:
| 文件类型 | 缓存时间 | 说明 |
|----------|----------|------|
| `.glb` | 4 小时 | 3D 模型文件 |
| `.png/.jpg/.jpeg/.webp/.gif` | 1 天 | 图片文件 |
| `.js/.css/.woff/.woff2/.ttf` | 1 年 | 静态资源 |

**修改文件**:
- `app.py` - 新增缓存常量、`_make_response_with_cache()` 函数，修改 `serve_data()` 和 `serve_pipeline()`

---

## [v2.7.0] - 2026-03-03

### 🚀 New Features

#### Per-model base_url 覆盖机制
- **问题**: `gemini-2.5-flash-image` 和 `gemini-3-pro-image-preview` 需要使用不同的 API 网关（`model-link-alpha`），但 `oneapi.base_url` 是全局共享的
- **解决方案**: 在 `ImageModelConfig`、`TextModelConfig`、`Gen3DModelConfig` 中添加可选的 `base_url` 字段，模型级覆盖全局值

**新增配置字段**:
```yaml
oneapi:
  base_url: "https://oneapi.qunhequnhe.com"   # 全局默认
  image_models:
    gemini-2.5-flash-image:
      base_url: "http://model-link-alpha.k8s-qunhe.qunhequnhe.com"  # 覆盖全局
```

**修改文件**:
- `config/config.yaml` - 两个 Gemini 模型添加 `base_url` 字段
- `config/config.py` - 三个 ModelConfig dataclass 添加 `base_url: Optional[str]`；解析循环读取可选 `base_url`；新增 `_resolve_base_url()` helper；7 个 backward-compatible 属性改用 `_resolve_base_url()`
- `scripts/gen3d.py` - VLM 配置的 ad-hoc `base_url` 改为尊重 per-model 覆盖

#### Hunyuan 模型共享配置 + 动态选择
- **问题**: `hunyuan-3d-3.1-pro` 在 config.yaml 中存在缩进 bug（被解析为顶层 key），配置与 `hunyuan-3d-pro` 完全重复，且 `Config.hunyuan` 属性硬编码模型名
- **解决方案**:
  - 使用 YAML anchor/alias (`&hunyuan_pro_config` / `*hunyuan_pro_config`) 让两个模型共享配置
  - 新增 `Config.get_hunyuan_config(model_name)` 方法支持显式选择模型
  - `@property hunyuan` 保持向后兼容，自动从 `tasks.gen3d.model` 推断

**切换模型**:
```yaml
tasks:
  gen3d:
    provider: "oneapi"
    model: "hunyuan-3d-3.1-pro"   # 或 "hunyuan-3d-pro"
```

**修改文件**:
- `config/config.yaml` - 修复缩进，YAML anchor/alias 共享配置
- `config/config.py` - 新增 `get_hunyuan_config()` 方法，`hunyuan` property 委托给它
- `core/gen3d/hunyuan.py` - pro-only 参数检查扩展为 `("hunyuan-3d-pro", "hunyuan-3d-3.1-pro")`

### 🔧 Changes

#### Tripo 配置分区（仅注释，不影响代码）
- 将 tripo 20+ 个配置项分为三个逻辑区块：API 连接配置、生成参数、视角选择与重映射
- `timeout` 和 `max_retries` 移至 API 连接区块顶部（紧跟 `api_key` 和 `base_url`）
- 数据结构不变，`TripoConfig` dataclass 和所有消费者代码无需修改

---

## [v2.6.0] - 2026-03-03

### 🚀 New Features

#### Guided View Editing 任务配置化
- **问题**: `guided_edit` 模型名在代码中硬编码为 `"gemini-2.5-flash-image"`，且直接修改共享的 config 对象
- **解决方案**: 新增 `tasks.guided_edit` 配置，代码中使用 `config.guided_edit` 获取独立配置对象

**新增配置**:
```yaml
tasks:
  guided_edit:
    provider: "oneapi"
    model: "gemini-2.5-flash-image"
```

**修复内容**:
- `config/config.yaml` - 新增 `tasks.guided_edit` 配置段
- `config/config.py` - 补充 `ImageApiConfig` 字段（size, n, poll_interval, max_wait_time），新增 `guided_edit` 属性
- `app.py:879-880` - 使用 `config.guided_edit` 替代修改 `config.qh_image`
- `scripts/batch_process.py:560-561` - 使用 `config.guided_edit` 替代修改 `config.qh_image`

#### Render 命令 provider 参数可选
- **问题**: `batch_process.py render` 命令的 `--provider` 参数为必填，但前端生成的命令未包含
- **解决方案**: `--provider` 改为可选，默认从 `config.tasks["gen3d"].provider` 读取

**文件变更**:
- `scripts/batch_process.py` - `--provider` 改为 `required=False`，默认值从配置读取

### 🐛 Bug Fixes

#### Fail Loudly 原则系统性修复
- **问题**: 多处使用 `getattr(config, 'key', default)` 和 `.get("key", default)` 提供硬编码默认值，违反 Fail Loudly 原则
- **解决方案**: 移除所有默认值，改为直接访问配置属性

**修复文件**:
- `utils/image_api_client.py:73-79` - 移除 `getattr` 默认值，直接访问 `config.poll_interval`, `config.max_wait_time`, `config.max_retries`, `config.timeout`
- `app.py:67-68` - `concurrency.image` 直接访问，移除 `getattr` 默认值
- `app.py:1357,1472,1786` - provider 默认值改为从 `config.tasks["gen3d"].provider` 读取

#### t2i 任务加入并发控制
- **问题**: t2i 任务在 `app.py` 的 semaphore 分配逻辑中被遗漏，导致无并发限制，触发 API 429 错误
- **解决方案**: 将 `t2i` 加入 `EDIT_SEMAPHORE` 控制，与 `edit`, `edit_view` 共用 `concurrency.image` 限制

**修复文件**:
- `app.py:619-627` - `t2i` 加入 `("edit", "edit_view", "t2i")` semaphore 控制

#### rodin.py 下载后缺少文件验证
- **问题**: `tripo.py` 和 `hunyuan.py` 下载后都调用 `validate_file_content()`，但 `rodin.py` 没有
- **解决方案**: `rodin.py` 下载完成后添加 `validate_file_content()` 调用

**修复文件**:
- `core/gen3d/rodin.py:184-191` - 添加文件验证，失败时清理文件

#### WebGL 渲染视角和阴影修复
- **问题 1**: 左右视角角度反了（left/right 互换）
- **问题 2**: bottom 视角地面阴影铺满画面
- **解决方案**:
  - 交换 left/right 的 theta 角度（left=90, right=-90）
  - bottom 视角动态设置 `shadowIntensity=0`

**修复文件**:
- `core/render/webgl_script.py` - 更新 `DEFAULT_VIEWS`，`setView()` 中添加 shadow 控制逻辑

#### 外部绝对路径兼容性修复
- **问题**: 当 `workspace.pipeline_dir` 配置为外部绝对路径（如 `/seaweedfs/...`）时，系统多处崩溃：
  - `relative_to(PROJECT_ROOT)` 抛出 `ValueError`
  - 前端图片无法加载（路径格式错误）
  - gen3d 任务找不到源图片
- **根本原因**: 代码中隐式假设 pipeline_dir 一定在 `PROJECT_ROOT` 内，违反单一配置原则
- **解决方案**:
  - 新增 `_rel_path()` helper：统一路径格式化，外部路径返回 `pipeline/...` 格式
  - 新增 `_resolve_api_path()` helper：将 API 路径解析回文件系统路径
  - 新增 `/pipeline/<filename>` Flask 路由：直接从 `PIPELINE_DIR` 服务文件
  - 全局替换 29 处 `relative_to(PROJECT_ROOT)` 为 `_rel_path()`
  - 修复 5 处 `PROJECT_ROOT / params["..."]` 为 `_resolve_api_path()`

**路径转换逻辑**:
```
内部路径 (data/pipeline/...) → 保持原样 → /data/<filename> 路由
外部绝对路径 (/seaweedfs/...) → pipeline/... → /pipeline/<filename> 路由
```

**修复文件**:
- `app.py` - 新增 `_rel_path()`, `_resolve_api_path()`, `/pipeline/` 路由，全局路径替换
- `templates/model_detail_scripts.html` - 实现 `showEditCmd()` 函数

#### CLI 编辑命令支持 --provider-id
- **问题**: `batch_process.py edit` 命令不感知 provider 子目录，多 provider 场景下找不到渲染图
- **解决方案**: 新增 `--provider-id` 参数，支持指定编辑哪个 provider 的渲染视图

**新增参数**:
```bash
python scripts/batch_process.py edit --provider-id tp3 --ids model1 model2
```

**修复文件**:
- `scripts/batch_process.py` - `edit_single()` 新增 `provider_id` 参数，`batch_edit()` 透传，argparse 新增 `--provider-id`

### 🔧 Changes

#### 配置访问方式统一
- `ImageApiConfig` dataclass 补充完整字段，确保与 `ImageModelConfig` 字段一致
- 所有 provider 默认值统一从 `config.tasks["gen3d"].provider` 读取
- 删除无效测试文件（`test_direct_gen3d.py`, `test_hunyuan_actual_code.py`, `test_fault_condition.py`, `verify_gen3d_code.py`）

---

## [v2.5.0] - 2026-03-03

### 🚀 New Features

#### Pipeline 数据目录可配置化
- **问题**: `app.py`、`batch_process.py`、`run_render_batch.py`、`gen3d.py` 中 pipeline 数据目录均硬编码为 `data/pipeline`，无法通过配置切换工作区
- **解决方案**: 新增 `workspace.pipeline_dir` 配置项，所有路径统一从 config 推导

**新增配置**:
```yaml
workspace:
  pipeline_dir: "data/pipeline"  # 相对于项目根目录，或绝对路径
```

**切换工作区只需改一行**:
```yaml
workspace:
  pipeline_dir: "data/pipeline_experiment_2"
  # 或绝对路径: pipeline_dir: "/mnt/nas/2d3d_data"
```

#### 文件变更
- `config/config.yaml` - 新增 `workspace` 配置段
- `config/config.py` - 新增 `WorkspaceConfig` 数据类，`Config` 新增 `workspace` 字段，`load_config()` 新增解析逻辑
- `app.py` - 目录常量延迟到 `init_semaphores()` 中从 config 初始化
- `scripts/batch_process.py` - 模块导入时从 config 推导 `IMAGES_DIR`, `MODELS_DIR`, `TRIPLETS_DIR`
- `scripts/run_render_batch.py` - `process_rendering()` 中从 config 推导路径
- `scripts/gen3d.py` - `generate_3d_model()` 中从 config 推导路径

---

## [v2.4.1] - 2026-03-02

### 🐛 Bug Fixes

#### Hunyuan 图像预处理配置重构
- **问题**: `gen3d-from-edits` 命令会裁剪并覆盖原始编辑后的多视角视图
- **根本原因**:
  - `HunyuanGenerator._crop_cfg` 硬编码启用裁剪
  - `ImageProcessor._save_processed_image()` 在没有 `output_dir` 时直接覆盖原图
- **解决方案**:
  - 新增 `preprocess` 配置项，默认禁用裁剪
  - 启用裁剪时，输出到临时目录，永不覆盖原图
  - `close()` 时自动清理临时文件

### 🔧 Changes

#### 新增配置
```yaml
# config.yaml
hunyuan-3d-pro:
  preprocess:
    enabled: false  # 默认禁用，设为 true 启用前景裁剪
```

#### 文件变更
- `config/config.yaml` - 添加 `preprocess` 配置段
- `config/config.py` - 新增 `PreprocessConfig` 类，更新 `HunyuanConfig` 和 `Gen3DModelConfig`
- `core/gen3d/hunyuan.py` - 移除硬编码配置，从 config 读取，使用临时目录

---

## [v2.4.0] - 2026-02-28

### 🚀 New Features

#### WebGL 渲染后端（双后端架构）
- **双后端支持**: 新增 WebGL 渲染后端，与 Blender 后端并存
  - 通过 `config.yaml` 中的 `render.backend` 切换（`blender` 或 `webgl`）
  - WebGL 后端使用 Headless Chrome + Google Model Viewer
  - 渲染效果与前端展示完全一致（IBL 环境光照）
- **6 视角完全修复**: 解决 WebGL 渲染中 6 个视角相同和偏心问题
  - 统一使用 `theta/phi/radius` 数据结构
  - 改用 `camera-orbit` 属性控制相机
  - 动态计算相机距离，适配不同尺寸模型
  - Top/Bottom 视角使用正交投影，避免透视畸变
- **文件变更**:
  - `core/render/webgl_script.py` - HTML/JS 生成器（完全重写）
  - `scripts/webgl_render.py` - Playwright 渲染主模块
  - `scripts/run_render_batch.py` - 集成双后端路由逻辑

#### Emit 光照模式（Blender）
- **新的光照模式**: `render.lighting_mode: "emit"`
  - 使用 Emission shader 替代物理光照
  - 保留原始纹理颜色，避免高光/反射造成的颜色失真
  - 白色背景，适合数据集生成

#### CLI 渲染参数覆盖
- **运行时参数覆盖**: `scripts/run_render_batch.py` 新增 CLI 选项
  - `--backend {blender,webgl}` - 临时切换渲染后端
  - `--lighting-mode {emit,ambient,flat,studio,hdri}` - 临时切换光照模式
  - `--force` - 强制重新渲染

### 🔧 Improvements

#### Blender 兼容性修复
- **渲染引擎自动检测**: `emit` 模式自动尝试 `BLENDER_EEVEE_NEXT` (Blender 4.x)，失败则回退到 `BLENDER_EEVEE`
- 修复 Blender 版本差异导致的 `enum "BLENDER_EEVEE" not found` 错误

### 🏗️ Architecture

#### 渲染配置重构
- **分层配置结构**: 渲染配置分为 `shared`, `blender`, `webgl` 三个部分
  - `shared`: `backend`, `image_size`, `rotation_z`
  - `blender`: `blender_path`, `use_bpy`, `device`, `samples`, `lighting_mode`
  - `webgl`: `chrome_path`, `environment_image`, `shadow_intensity`, `camera_distance`, `render_timeout`, `use_gpu`
- 完全向后兼容，旧配置代码无需修改

---

## [v2.3.4] - 2026-02-26

### 🚀 New Features

#### 配置系统重构（统一 OneAPI Gateway）
- **消除配置重复**: API key 和 base_url 从 6 处重复减少到 1 处
- **分层配置结构**:
  - `oneapi`: 统一的 API key 和 base_url
  - `text_models`: 文本生成模型配置
  - `image_models`: 图像生成模型配置
  - `gen3d_models`: 3D 生成模型配置
  - `tasks`: 任务到模型的映射
- **100% 向后兼容**: 所有旧代码无需修改即可运行
- **文件变更**:
  - `config/config.yaml` - 新配置结构
  - `config/config.py` - 新配置解析模块（带兼容层）
  - `config/__init__.py` - 更新导出
  - `utils/config.py` - 重新导出保持兼容

#### Tripo 3D 生成增强
- **视角智能选择**: 新增 `entropy_edge` 策略，自动选择信息最丰富的 4 个视角
- **视角映射算法**: 基于立方体旋转的几何映射算法，确保视角方向正确
- **种子固定**: `model_seed` 和 `texture_seed` 可配置，保证可复现性
- **文件变更**:
  - `core/gen3d/tripo.py` - 视角选择和映射逻辑
  - `scripts/gen3d.py` - 立方体旋转算法实现

### 🔧 Improvements

#### 配置验证
- `render.backend` 增加验证，必须是 `"blender"` 或 `"webgl"`
- 使用 `_require_key()` 替代 `dict.get(key, default)`，配置缺失时立即报错

### 🧹 Cleanup
- 删除未使用的配置: `newapi`, `openrouter`
- 删除无效测试文件（同上 v2.3.3）

### 📊 Metrics
- API Key 配置次数: 6次 → 1次 (-83%)
- Base URL 配置次数: 6次 → 1次 (-83%)
- 配置文件行数: ~300行 → ~250行 (-17%)

---

## [v2.3.3] - 2026-02-26

### 🐛 Bug Fixes

#### Fail Loudly 原则修复
- **`utils/image_api_client.py`**: 移除 4 处 `getattr(..., default)` 硬编码默认值
  - `poll_interval`, `max_wait_time`, `max_retries`, `timeout` 现在直接从配置读取
  - 配置缺失时将抛出异常，而非静默使用默认值
- **`app.py`**: 
  - `concurrency.image` 改为直接访问，不再使用 `getattr` 默认值
  - 3 处 `provider` 默认值改为从 `config.tasks["gen3d"].provider` 读取

#### 配置对象污染修复
- **`config/config.yaml`**: 新增 `tasks.guided_edit` 任务配置
  - 显式配置 `gemini-2.5-flash-image` 模型用于 guided view editing
- **`config/config.py`**: 
  - `ImageApiConfig` 新增 `size`, `n`, `poll_interval`, `max_wait_time` 字段
  - 新增 `guided_edit` 属性，返回独立的配置对象
- **`scripts/batch_process.py`**: 使用 `config.guided_edit` 替代运行时修改配置对象

#### 文件验证补充
- **`core/gen3d/rodin.py`**: `download_result()` 新增文件内容验证
  - 下载后调用 `validate_file_content()` 验证文件格式
  - 无效文件自动删除并抛出异常

### 📝 Code Quality

- 符合 AGENTS.md 中的 **Fail Loudly** 和 **单一配置源** 原则
- 所有可配置参数通过 `config.yaml` 控制，代码不再包含硬编码默认值

---

## [v2.3.2] - 2026-01-29

### 🚀 New Features

#### MLLM 视角智能选择
- **视角分析器**: 新增 `core/image/view_analyzer.py` - `ViewAnalyzer` 类
  - 使用 MLLM 判断哪些视角需要编辑
  - 输入：T_image + 编辑指令 + 多视角拼接图
  - 输出：JSON 格式的视角选择结果
- **可复用拼接模块**: 新增 `core/image/view_stitcher.py` - `ViewStitcher` 类
  - 从 MultiviewEditor 中提取，供多个模块共用
  - 支持 3×2 网格拼接，可选正方形填充

#### Single 模式增强
- **默认 6 视角**: Single 模式默认考虑所有 6 个视角
- **智能视角选择**: 编辑前调用 MLLM 判断需要编辑的视角
- **减少 API 调用**: 只编辑必要的视角，节省时间和成本

### 🔄 Changes
- **MultiviewEditor**: 重构为使用 `ViewStitcher`，减少重复代码
- **GuidedViewEditor**: 
  - 接受 `mllm_config` 参数用于视角分析
  - 输出元数据包含 `edited_views` 字段
- **app.py**: 
  - Single 模式默认使用 6 视角
  - 注入 MLLM 配置用于视角选择

### 🧪 Testing
- 新增测试脚本 `tests/test_guided_view_editor.py`
  - 支持 `--dry-run` 干运行模式
  - 支持 `--test stitch/analyze/full/all` 分步测试
  - 支持 `--no-mllm` 跳过视角分析

---

## [v2.3.1] - 2026-01-29

### 🚀 New Features

#### Guided View Editor (Single Mode)
- **引导式编辑流程**: 新的 Single View 编辑模式
  1. 先编辑源图像 S_image → 获得目标图 T_image
  2. 用 T_image 作为参考，引导各视角渲染图的编辑
- **新增模块**: `core/image/guided_view_editor.py` - `GuidedViewEditor` 类
- **新增 API 方法**: `edit_image_with_reference()` - 支持双图输入（参考图 + 源图）
- **模型分工**:
  - Single 模式: 使用 `gemini-2.5-flash-image`（快速、一致性好）
  - Multiview 模式: 使用 `gemini-3-pro-image-preview`（高质量）

### 🔄 Changes
- **app.py**: 更新 `run_edit_view_task()` 函数，Single 模式切换为引导式编辑

---

## [v2.3.0] - 2026-01-29

### 🚀 New Features

#### Multiview Editor Improvements
- **正方形填充**: 拼接图自动填充为正方形，解决 Gemini 强制输出 1024×1024 导致的比例失真问题
- **等比缩放裁剪**: `split_views()` 智能处理 Gemini 返回的不同尺寸，保证裁剪无损
- **样式优化**:
  - 白色边框 (8px) 包围每个视角
  - 浅灰色标签背景，深灰色文字
  - 字体大小增至 36pt（1.5 倍）
  - 单元格间距 10px

#### Model Support
- **Gemini 3 Pro Image Preview**: 新增支持，编辑质量更好
- **自动尺寸检测**: `edit_image()` 支持 `auto_size=True`，自动使用输入图片尺寸

### 🐛 Bug Fixes
- **裁剪位置错误**: 修复非正方形拼接图在 Gemini 编辑后裁剪位置不正确的问题
- **视角丢失**: 通过正方形填充解决 Gemini 裁剪导致的视角丢失问题

### 🧪 Testing
- 新增调试脚本 `tests/test_multiview_stitch.py`，支持：
  - 模拟测试（不调用 API）
  - 真实 Gemini 测试（`--gemini` 参数）
  - 模型选择（`--model` 参数）
- 测试输出统一整理到 `test_output/` 目录

### 📁 Test Output Structure
```
test_output/
├── test_multiview_output/        # Gemini 2.5 Flash 测试（非正方形，有问题）
├── test_multiview_output_gemini3/ # Gemini 3 Pro 测试（非正方形）
├── test_multiview_output_square/  # 正方形填充测试（修复后）
├── test_canister_knob/           # 罐子旋钮移除测试
├── test_canister_handle/         # 罐子把手移除测试
├── test_wagon_remove_wheel/      # 马车轮子移除测试
├── test_wagon_replace_wheels/    # 马车轮子替换测试
└── test_pipeline_output/         # Pipeline 集成测试
```

---

## [v2.2.0] - 2026-01-27

### 🚀 New Features

#### Multiview Editing Mode
- **New Module**: `core/image/multiview_editor.py` - 多视角拼接编辑器
  - 将 6 个渲染视角（front/back/right/left/top/bottom）拼接为 3×2 网格
  - 标签放置在图片外部区域，不影响 3D 重建
  - 调用 Gemini 一次性编辑所有视角，保证一致性
  - 编辑后自动裁剪回独立视角图
- **UI**: 编辑模态框新增模式选择
  - `Single View`: 独立编辑 3 个视角（原有方式）
  - `Multiview`: 拼接 6 视角 → 编辑一次 → 裁剪回

#### Delete Functionality
- **Delete Edit Batches**: 新增 `/api/models/<model_id>/edits/<edit_id>` DELETE 端点
  - 删除编辑视图目录和对应的 Target 3D 模型
  - 每个 Edit Batch 卡片右上角新增 🗑️ 删除按钮

#### Card Sorting
- **Images Page**: 新增按 ID/时间排序按钮，可对页面卡片进行排序
- **Models Page**: 同上，默认按时间倒序（最新在前）

#### Download Improvements
- **Per-Card Download**: 每个图片/模型卡片右上角新增下载按钮
- **Modular Code**: 创建 `static/js/download.js` 统一下载功能
- **Model Detail Page**: GLB 下载按钮、源图片下载按钮

#### Navigation
- **Images → Model Detail**: "View 3D" 按钮直接跳转到对应模型详情页

### 🔧 Improvements

#### 渲染优化
- **Top 视角爆白问题**: 定位并记录渲染问题
  - 问题：6 视角中仅 `top` 视角出现大面积高光压满（"爆白"）
  - 原因：方向光 + 高反射材质导致顶视角高光剪裁
  - 解决方案：使用 `render.lighting_mode: "ambient"`
  - 经验沉淀到 `docs/batch_render_guide.md`

### 🐛 Bug Fixes
- **API JSON Support**: `/api/images/<id>/edit` 现在同时支持 JSON 和 Form 数据格式
- **Copy Button**: 修复 HTTP 环境下复制按钮失效问题，使用 `execCommand` 回退方案
- **Route Fix**: 修复 `/models/<id>` → `/model/<id>` 路由问题

### 🛠️ API Changes
- `POST /api/models/<model_id>/views/<view_name>/edit` 新增 `edit_mode` 参数
  - `single`: 独立编辑每个视角（默认）
  - `multiview`: 拼接编辑模式

---

## [v2.1.1] - 2026-01-25

### 🚀 New Features

#### 统一的客户端层
- **Image API Client**: 新增 `utils/image_api_client.py`
  - 统一管理所有图像 API 调用（T2I、图像编辑）
  - 自动检测模型类型，选择正确的 API（Response API vs Chat Completions）
  - 支持 `gemini-*`, `imagen-*`, `doubao-*` 等模型的异步轮询
- **重构 Generator 和 Editor**:
  - `core/image/generator.py`: 从 183 行简化到 47 行
  - `core/image/editor.py`: 从 177 行简化到 44 行
  - 两者均改为调用 `ImageApiClient` 的薄包装层

#### 集中 Prompt 管理
- **Prompts 模块**: 新增 `utils/prompts.py`
  - 集中所有 prompt 模板
  - 新增 `EditType` 枚举（REMOVE, REPLACE, BOTH）
  - 支持批量指令生成（1:1 比例）

### 🔧 Improvements

#### LLM Client 统一
- **Text LLM 客户端**: `utils/llm_client.py`
  - 统一文本 LLM 调用（GPT、Gemini 等）
  - 所有 caption 生成使用此客户端

---

## [v2.1.0] - 2026-01-25

### 🚀 New Features
- **Image Editing Module**: Added a full-stack image editing feature using Multimodal LLMs (Gemini/Doubao).
  - Backend: Implemented `core/image/editor.py` and `/api/images/<id>/edit` endpoint.
  - Frontend: Added "Edit" button and modal dialog in `images.html` for text-guided image manipulation.
- **Provider-Agnostic T2I**: Refactored `T2IGenerator` to support dynamic configuration defined in `config.yaml`.
  - Now correctly uses `qh_image` (Gemini) or other configured providers instead of hardcoded OpenRouter.

### 🐛 Bug Fixes
- **UI Provider Selection**: 
  - Fixed `images.html` modal to correctly display all configured 3D providers (Tripo, Hunyuan, Rodin).
  - Restored missing buttons for "Hunyuan 3D (Pro)" and "Rodin (Gen2)".
- **Dependency Integrity**:
  - Validated and fixed dependencies in `core/gen3d/rodin.py` and `base.py`.
  - Restored missing `scripts/generate_prompts.py` logic.
  - Fixed `core/image/processor.py` imports.
- **Code Cleanup**:
  - Removed duplicated buttons and malformed JavaScript in `templates/images.html`.
  - Removed dead code in `utils/config.py`.

### 🛠️ Maintenance
- Conducted a comprehensive **Deep Audit** of the system to ensure alignment between `config.yaml`, Backend routes, and Frontend UI.
- Standardized API key handling across all generator modules.

---

## [v2.0.0] - 2024-03-20

### ✨ Major Update
- Initial release of the refactored **v2 Pipeline**.
- **Architecture**: Flattened directory structure for better maintainability.
- **Web UI**: Brand new Flask-based dashboard (`app.py`) replacing the old Gradio interface.
- **Async Processing**: Implemented background task queue for generation jobs.
- **Configuration**: Centralized management via `config/config.yaml`.
