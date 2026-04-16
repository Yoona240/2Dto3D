# 2D→3D Triplet 数据生产流水线（`2d3d_v2`）技术报告

> 适用场景：毕业设计/论文撰写中的“系统设计与实现”章节材料。  
> 报告基于对仓库 `e:\zjucode\2d3d_v2\` 核心代码的阅读整理，重点覆盖：数据组织、端到端流程、关键算法与工程机制、质量检查（Stage1/Stage2）、可复现性与风险点。

---

## 目录

- [1. 系统目标与总体设计](#1-系统目标与总体设计)
- [2. 数据组织与命名规范](#2-数据组织与命名规范)
- [3. 端到端流水线流程（Stage0~Stage2）](#3-端到端流水线流程stage0stage2)
- [4. 核心模块与调用关系](#4-核心模块与调用关系)
- [5. 语义朝向对齐（SemanticAlign）机制](#5-语义朝向对齐semanticalign机制)
- [6. 多视角编辑（Multiview Edit）机制](#6-多视角编辑multiview-edit机制)
- [7. Stage1 编辑质量检查（Edit QC）](#7-stage1-编辑质量检查edit-qc)
- [8. Stage2 重建一致性检查（Target Consistency）](#8-stage2-重建一致性检查target-consistency)
- [9. Manifest 与实验追踪（Experiment Runner）](#9-manifest-与实验追踪experiment-runner)
- [10. 可复现性、失败显性化与工程稳健性](#10-可复现性失败显性化与工程稳健性)
- [11. 局限性与改进方向](#11-局限性与改进方向)
- [附录A：关键配置项速查（`config/config.yaml`）](#附录a关键配置项速查configconfigyaml)
- [附录B：关键脚本入口速查（CLI）](#附录b关键脚本入口速查cli)

---

## 1. 系统目标与总体设计

### 1.1 研究动机

面向 3D 几何/外观编辑任务（尤其是需要多视角一致性的 3D 编辑），高质量配对数据稀缺。常见难点包括：

- **视角不一致**：源/目标 3D 的朝向与坐标系不统一，导致多视角图像无法对齐比较。
- **编辑不一致**：单视角编辑会引入跨视角矛盾，削弱监督信号。
- **质量不可控**：2D 编辑结果可能偏离指令；3D 重建可能无法忠实还原编辑后的外观。
- **不可追溯**：缺乏统一的元数据与记录机制，难以复现与统计分析。

### 1.2 系统目标

本项目 `2d3d_v2` 的目标是构建一条工程化、可扩展、可追溯的流水线，用于生成高质量 triplet 数据：

\[
\textbf{(source 3D)} \;\;\leftrightarrow\;\; \textbf{(edited multi-view 2D)} \;\;\leftrightarrow\;\; \textbf{(target 3D)}
\]

并通过 Stage1/Stage2 的自动质量检查，筛选出可用样本写入 manifest。

### 1.3 总体架构（模块分层）

- **编排与批处理**
  - `scripts/run_full_experiment.py`：端到端实验执行、断点恢复、记录与统计、实验级 manifest。
  - `scripts/batch_process.py`：离线批处理 CLI，覆盖 gen3d/render/edit/质检/导出等子命令。
- **2D 侧（图像生成/编辑/拼接/Mask）**
  - `utils/image_api_client.py`：统一图像 API（Response API 轮询）。
  - `core/image/view_stitcher.py`：六视角拼图（3×2）+ 标签/边框 + pad-to-square。
  - `core/image/multiview_editor.py`：拼图一次编辑 + 精确裁回六视角。
  - `core/image/edit_artifact_builder.py`：生成 before/after grid + per-view mask + mask grid。
- **3D 侧（生成/渲染/对齐/一致性）**
  - `core/gen3d/*`：Tripo/Hunyuan/Rodin 生成与下载，统一抽象 `Base3DGenerator`。
  - `scripts/run_render_batch.py`：渲染调度（Blender/WebGL 双后端）+ SemanticAlign 总流程。
  - `core/render/semantic_view_aligner.py`：VLM 判定 semantic front + 旋转矩阵（含 roll 稳定）。
  - `scripts/bpy_align_standalone.py`：bpy 子进程 GLB 旋转/归一化导出 aligned GLB。
  - `core/render/recon_consistency_checker.py`：Stage2（LPIPS 或 VLM）一致性检查。

---

## 2. 数据组织与命名规范

### 2.1 数据根目录

数据根目录由配置项 `workspace.pipeline_dir` 决定（可为绝对路径或相对项目根目录）。

### 2.2 关键目录结构（生产主线）

以下结构与代码一致（`run_full_experiment.py`、`run_render_batch.py`、`generate_data_manifest.py` 均依赖该结构）：

```text
{pipeline_dir}/
├── images/{source_model_id}/
│   ├── image.png
│   ├── meta.json
│   └── instructions.json
│
├── models_src/{source_model_id}/
│   ├── model_{provider_id}.glb
│   ├── model_{provider_id}_aligned.glb              # 启用语义对齐时生成
│   ├── norm_params.json                             # 启用 normalize_geometry 时生成
│   └── meta.json
│
├── models_src/{source_model_id}_edit_{edit_id}/
│   ├── model_{provider_id}.glb
│   ├── model_{provider_id}_aligned.glb
│   └── meta.json                                    # 含 Stage2 结果等
│
├── triplets/{source_model_id}/
│   ├── views/{provider_id}/
│   │   ├── front.png ... bottom.png
│   │   ├── meta.json
│   │   └── _semantic_tmp/                            # debug 资产（可配置是否保留）
│   │
│   └── edited/{edit_id}/
│       ├── front.png ... bottom.png                  # edited views（缺失视角允许 fallback）
│       ├── front_mask.png ... bottom_mask.png
│       ├── edit_mask_grid.png
│       ├── before_image_grid.png
│       ├── target_image_grid.png
│       ├── meta.json                                 # 含 Stage1 结果与 prompt_trace
│       └── _tmp/                                     # 编辑过程临时文件
│
└── triplets/{source_model_id}_edit_{edit_id}/
    └── views/{provider_id}/
        ├── front.png ... bottom.png                  # target 3D renders
        └── target_render_grid.png                     # 视图网格（导出/可视化侧常用）
```

> 说明：`provider_id` 是内部缩写（例如 `hy3`/`tp3`/`rd2`），由 `core/gen3d.get_model_id(provider)` 映射得到。

---

## 3. 端到端流水线流程（Stage0~Stage2）

### 3.1 流程概览

```mermaid
flowchart TD
  A[T2I: 文本生成源图\nimages/{id}/image.png] --> B[Source Gen3D: 图生3D\nmodels_src/{id}/model_*.glb]
  B --> C[Render(First pass): 六视角渲染\ntriplets/{id}/views/{p}/_semantic_tmp/first_pass_views]
  C --> D[SemanticAlign: VLM判定 semantic front\n+ 旋转矩阵 + (可选)归一化]
  D --> E[Render(Final): 输出 canonical 六视角\ntriplets/{id}/views/{p}/front..bottom]
  E --> F[Multiview Edit: 拼图一次编辑\ntriplets/{id}/edited/{edit_id}/front..bottom]
  F --> G[Stage1: 编辑质量检查\n写入 edited meta.json]
  G -->|passed| H[Target Gen3D: 从 edited views 重建\nmodels_src/{id}_edit_{edit_id}/model_*.glb]
  H --> I[Target Render + (可选)共享对齐/归一化\ntriplets/{id}_edit_{edit_id}/views/{p}]
  I --> J[Stage2: 一致性检查 LPIPS/VLM\n写入 target meta.json]
  J -->|passed| K[写入数据集 manifest\nscripts/generate_data_manifest.py]
```

### 3.2 两条主入口路径

- **实验编排入口**：`scripts/run_full_experiment.py`
  - 优点：自动生成 prompt、T2I、指令、执行 Stage1/Stage2、统计与 manifest 全链路记录，支持 resume/repair。
- **离线批处理入口**：`scripts/batch_process.py`
  - 优点：针对已有数据可分阶段增量处理（例如只补 mask、只重算 Stage2、只渲染缺失视角等）。

---

## 4. 核心模块与调用关系

### 4.1 渲染模块调用链

渲染统一入口为 `scripts/run_render_batch.py: run_blender_render()`，其中：

- `render.backend="blender"`：通过 `scripts/bpy_render_standalone.py` 子进程导入 `bpy` 执行渲染脚本（崩溃隔离）。
- `render.backend="webgl"`：通过 `scripts/webgl_render_standalone.py` 子进程启动 Playwright/Chromium + `model-viewer` 渲染（防 hang）。

当 `render.semantic_alignment.enabled=true` 时，渲染会进入 `_run_render_with_semantic_alignment` 完成：first-pass→VLM决策→GLB对齐→final-pass（或 remap）全流程。

### 4.2 2D 编辑模块调用链

- 拼接：`core/image/view_stitcher.py`
- 编辑：`core/image/multiview_editor.py` 调用 `utils/image_api_client.py: edit_image()`
- 裁回视角：`MultiviewEditor.split_views()` 精确按 metadata 反算裁剪区域
- Mask/拼图资产：`core/image/edit_artifact_builder.py`

### 4.3 质检模块调用链

Stage1：
- router：`core/image/edit_quality_router.py`
  - Method-1：`edit_quality_checker.py`
  - Method-2：`edit_quality_checker_v2.py`
  - Method-3：`edit_quality_checker_unified.py`

Stage2：
- `core/render/recon_consistency_checker.py`（LPIPS or VLM）

---

## 5. 语义朝向对齐（SemanticAlign）机制

SemanticAlign 解决的问题：不同 provider 生成的 GLB 初始朝向不可控，导致“front/back/left/right/top/bottom”的语义不统一，进而影响编辑一致性与 Stage2 对比。

### 5.1 VLM 决策：semantic front

模块：`core/render/semantic_view_aligner.py`

关键步骤：

1. 使用 `ViewStitcher` 将六视角拼成固定顺序的 3×2 网格图（并落盘到 debug 目录）。
2. 调用 VLM（通过 `utils/llm_client.py`）返回严格 JSON：
   - `semantic_front_from`：从哪张视角应被视为“语义正面”
   - `confidence`：置信度 \([0,1]\)
   - `reason`：简短解释
3. Fail Loudly：
   - JSON 缺字段、字段类型不对、`confidence < min_confidence` 均直接抛错。

### 5.2 旋转矩阵计算（含 roll 稳定）

SemanticAlign 将 `semantic_front_from` 的法向量旋转到 canonical front（\((0,-1,0)\)），再绕 front 轴做 roll 稳定，使目标 upright 更稳定（front-only 的欠约束问题）。

### 5.3 GLB 对齐导出（bpy 子进程）

脚本：`scripts/bpy_align_standalone.py`

支持三种模式：

- **source 模式**：旋转 + `--normalize`
  - 计算 bbox center/max_dim，做 center+scale 归一化
  - 写出 `norm_params.json`（包含 rotation、center、max_dim、以及 WebGL framing 参数）
- **target 模式（share rotation）**：`--norm-from norm_params.json`
  - 直接复用 source 的 rotation+center+scale
- **target 模式（不 share rotation）**：`--norm-center-from norm_params.json`
  - target 使用自己的 rotation，但复用 source 的 scale（并在脚本中避免错误使用 source center 导致漂移）

### 5.4 final render：重渲染 vs remap

`scripts/run_render_batch.py` 中当满足一定条件时，会对 first-pass 输出做“视角 remap”（拷贝 + top/bottom 旋转）替代二次渲染，以降低成本；当语义 front 为 top/bottom 等无法 remap 的情形，会 fallback 重新渲染 aligned GLB。

### 5.5 framing 一致性（normalize_geometry + WebGL fixed params）

当 `normalize_geometry=true`：

- source 对齐阶段写入 `norm_params.json` 中的 `webgl_safe_radius/webgl_center`
- target WebGL 渲染会注入 fixed radius/center，减少视角裁剪与尺度差异，提升 Stage2 的可比性

---

## 6. 多视角编辑（Multiview Edit）机制

模块：`core/image/multiview_editor.py`

核心思想：把六视角拼接为 3×2 网格，一次编辑后再裁回各视角，从而提升跨视角一致性并降低 API 调用次数。

### 6.1 拼接策略与 pad-to-square

`core/image/view_stitcher.py`：

- 固定 3×2 布局、白色边框、浅灰标签；
- 支持 `pad_to_square=True`：将拼图内容居中填充到正方形画布，解决部分编辑模型强制 1:1 输出导致裁切的问题。

### 6.2 Prompt 组成与 guardrail

Multiview 编辑 prompt 由三部分确定性拼装：

1. **guardrail**（可配置）：`tasks.multiview_editing.guardrail_prompt_*`
2. **task context**：强调“同一物体多视角”、“可见性规则（不可见视角必须保持不变）”、“保持布局与标签”
3. **user instruction**

编辑模块会写入 `prompt_trace`，便于复现与审计。

### 6.3 精确裁剪回视角

`split_views()` 使用 `ViewStitcher` 返回的 `image_positions` 精确裁剪；若输出尺寸变化，则进行等比缩放/居中裁剪以对齐到原拼图尺寸，再裁回视角区域，保证“裁剪区域与拼接区域严格一致”。

---

## 7. Stage1 编辑质量检查（Edit QC）

Stage1 的目标：判断 edited views 是否**遵循编辑指令**、是否**跨视角几何一致**，并对“非法编辑”（材质/表面/整物替换等）进行拦截。

### 7.1 Method-1：`grid_vlm`

模块：`core/image/edit_quality_checker.py`

- 输入：before/after 六视角拼图（3×2）+ instruction
- 输出：`{"decision":"pass|fail","reason":"..."}`
- 特点：实现简单、依赖 VLM 的端到端判定

### 7.2 Method-2：`two_stage_recon`

模块：`core/image/edit_quality_checker_v2.py`

- 新增 **View Sanity Check**（Stage 1A）：先检查 AFTER 跨视角几何自洽，fail 直接终止
- Stage 1A diff（VLM）：看图不看指令，产出 diff_text
- Stage 1B judge（LLM）：看指令与 diff_text，不看图，判定 pass/fail
- 可选 relabel：把“实际发生的编辑”重写成合法指令；可配置是否 rejudge

该方法强调将“感知差异”和“指令符合性”拆分，便于定位错误来源并稳定输出 schema。

### 7.3 Method-3：`unified_judge`

模块：`core/image/edit_quality_checker_unified.py`

单次 VLM 输出统一结构化 JSON，包含：

- `view_sanity`：几何自洽（pass/fail + problematic_views）
- `instruction_legality`：后验合法性（allow/reject + category：`structural_part/material_only/appearance_only/main_body/...`）
- `instruction_following`：是否遵循指令
- `evidence_strength` + `supporting_views`：证据强度约束（可配置弱证据直接拒绝）
- `relabel`：若不符合但可重写则给出 rewrite 指令

该方法对“仅表面变化/材质变化/整物替换”等不符合数据集目标的样本具备更强拦截能力。

### 7.4 Stage1 输出落盘与统一 schema

router：`core/image/edit_quality_router.py`

`build_quality_check_meta()` 是 `meta.json` 中 `quality_check` 字段的单一真源（single source of truth），保证 `app.py`、`batch_process.py`、`run_full_experiment.py` 写入一致。

---

## 8. Stage2 重建一致性检查（Target Consistency）

Stage2 的目标：判断 target 3D 的渲染视图是否忠实还原 edited views 的外观变化（即 “edited 2D → target 3D” 的一致性）。

模块：`core/render/recon_consistency_checker.py`

### 8.1 LPIPS 方法

- 输入：edited views 与 target render views（按 `recon_views` 选择视角）
- 预处理：可选 `input_mode=grayscale` 以弱化颜色/纹理影响
- 输出：每视角分数 + 聚合分数（mean/max）+ 阈值判定

### 8.2 VLM 方法（网格对比）

- 将 edited views 与 target render views 各拼成 3×2 网格
- 可选提供 source 网格辅助定位被编辑部件
- VLM 输出 `pass/confidence/reason`

### 8.3 Stage2 结果写回 target meta

写入位置：`models_src/{source_id}_edit_{edit_id}/meta.json`

兼容字段：
- `target_quality_check`

多 provider 绑定字段：
- `target_quality_checks_by_provider[{provider_id}]`

---

## 9. Manifest 与实验追踪（Experiment Runner）

### 9.1 实验级记录与输出

`scripts/run_full_experiment.py` 在 `experiments/{experiment_id}/` 下维护：

- `events.jsonl`：事件流
- `object_records.jsonl` / `edit_records.jsonl`：对象级与编辑级记录（含 timings、错误分类）
- `summary.json/csv`、`category_stats.json/csv`、`stage_timing_summary.csv`
- `manifest.json`：实验快照（plan、config_snapshot、outputs 路径、progress、totals）

### 9.2 断点恢复（resume）与修复（repair）

- `execution_plan.json` 冻结随机抽样结果，保证 resume 可复现
- `--resume-experiment-id`：补跑缺失 object/edit 槽位
- `--repair-experiment-id`：基于已落盘产物重建 partial records/summary

### 9.3 数据集导出 manifest（训练数据口径）

脚本：`scripts/generate_data_manifest.py`

功能：从实验 log 中提取 Stage2 通过的 edit，汇总如下资产路径：

- source image、source glb（raw/aligned）
- source views
- edited views
- masks（per-view + grid）
- target glb（raw/aligned）
- target views + `target_render_grid.png`
- Stage2 score（LPIPS 或 VLM confidence）

该 manifest 更适合作为训练/评测数据集的资产清单。

---

## 10. 可复现性、失败显性化与工程稳健性

### 10.1 Fail Loudly

项目在多个层面贯彻 Fail Loudly：

- Response API 调用强制要求 `size`（`utils/image_api_client.py`）
- VLM 输出 schema 校验（SemanticAlign、Stage1 unified_judge、Stage2 VLM 等）
- 缺失关键资产（6 视角缺图、norm_params.json 缺字段）直接抛错

### 10.2 子进程隔离（避免主流程崩溃/卡死）

- bpy 渲染：`scripts/bpy_render_standalone.py`
- GLB 对齐：`scripts/bpy_align_standalone.py`
- WebGL 渲染：`scripts/webgl_render_standalone.py`（通过 `run_render_batch` 启动并带超时）

### 10.3 追溯与审计

- `meta.json`：每个资产的生成参数与来源记录
- `prompt_trace`：multiview guardrail + task context + user instruction 的组合可追溯
- `events.jsonl` + `[Stage]/[Timing]`：可用于离线分析失败模式与耗时瓶颈

---

## 11. 局限性与改进方向

### 11.1 潜在风险点

- **配置中包含明文密钥**：若用于论文/开源展示，需要将 `config.yaml` 中的 key 改为环境变量读取或私密配置文件，避免泄露。
- **视角顺序常量的统一性**：不同模块使用的 3×2 拼图顺序可能存在差异（代码中通常会在 prompt 里说明，但仍建议统一常量并在数据集规范中固定）。
- **Stage2 指标语义差异**：当 `stage2_method=vlm` 时 score 为 confidence；当 `lpips` 时 score 为距离值，统计与阈值解释需避免混用。

### 11.2 可扩展方向

- 引入更强的多视角输入重建（将 edited 的多视角图作为 Gen3D 输入，而不仅是 front）
- 使用更鲁棒的几何/语义一致性指标（例如结构相似/关键点一致性）辅助 Stage2
- 将 Stage1/Stage2 的失败样本自动分桶与可视化，形成闭环数据改进流程

---

## 附录A：关键配置项速查（`config/config.yaml`）

### A.1 渲染与语义对齐

- `render.backend`: `blender | webgl`
- `render.image_size`
- `render.semantic_alignment.enabled`
- `render.semantic_alignment.vlm_model`
- `render.semantic_alignment.min_confidence`
- `render.semantic_alignment.verify_after_rerender`
- `render.semantic_alignment.save_aligned_glb`
- `render.semantic_alignment.aligned_glb_suffix`（通常为 `_aligned`）
- `render.semantic_alignment.normalize_geometry`
- `render.semantic_alignment.share_rotation_to_target`
- `render.semantic_alignment.norm_params_filename`（通常 `norm_params.json`）
- `render.webgl.subprocess_timeout_seconds`

### A.2 Stage1/Stage2 质检

- `edit_quality_check.enabled`
- `edit_quality_check.method`: `grid_vlm | two_stage_recon | unified_judge`
- `edit_quality_check.two_stage_recon.stage2_method`: `lpips | vlm`
- `edit_quality_check.two_stage_recon.recon_views`
- `edit_quality_check.two_stage_recon.input_mode`: `rgb | grayscale`
- `edit_quality_check.two_stage_recon.aggregate`: `mean | max`
- `edit_quality_check.two_stage_recon.threshold`
- `edit_quality_check.unified_judge.require_non_weak_evidence`

### A.3 并发与 lane 控制

- `concurrency.gen3d.*`
- `concurrency.render`
- `concurrency.text`
- `concurrency.image`
- `concurrency.recon_quality_check`
- `run_full_experiment.api_lane_control.*`

---

## 附录B：关键脚本入口速查（CLI）

> 以下命令均在项目根目录执行（`2d3d_v2/`）。

- 端到端实验：
  - `python scripts/run_full_experiment.py --plan <plan.yaml> --gpu-id 0`
  - `python scripts/run_full_experiment.py --resume-experiment-id <experiment_id> --gpu-id 0`
  - `python scripts/run_full_experiment.py --repair-experiment-id <experiment_id> --gpu-id 0`

- 批处理：
  - gen3d：`python scripts/batch_process.py gen3d --provider hunyuan`
  - render：`python scripts/batch_process.py render --provider hunyuan`
  - edit：`python scripts/batch_process.py edit --mode multiview`
  - Stage1 重检：`python scripts/batch_process.py check-edit-quality --ids <model_id>`
  - target gen3d：`python scripts/batch_process.py gen3d-from-edits --provider hunyuan`
  - Stage2 检测：`python scripts/batch_process.py check-target-consistency --provider hunyuan --skip-render`
  - 补 mask：`python scripts/batch_process.py materialize-edit-artifacts --ids <model_id>`
  - 导出 triplet manifest：`python scripts/generate_data_manifest.py <log_file> --output <manifest.json>`

