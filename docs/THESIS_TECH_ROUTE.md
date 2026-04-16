# 2D-to-3D 多视角编辑 Triplet 数据集构建：毕业设计论文写作版报告（基于 `2d3d_v2`）

> 本文档面向论文撰写，按常见论文结构组织内容：**工作目的（背景）→国内外研究现状→理论分析→研究方法（计算方法）→实验装置与测试方法→实验结果分析与讨论→研究成果→结论及意义**。  
> 内容基于 `2d3d_v2` 的实际代码实现与数据产物（以及 `data_manifest_guide.md` 中定义的 manifest 口径）。

---

## 目录

- [1. 工作目的与研究背景](#1-工作目的与研究背景)
- [2. 国内外研究现状](#2-国内外研究现状)
- [3. 理论分析与问题定义](#3-理论分析与问题定义)
- [4. 研究方法（计算方法）](#4-研究方法计算方法)
  - [4.1 技术路线总览（Stage0～Stage2）](#41-技术路线总览stage0stage2)
  - [4.2 数据组织与 Manifest 规范（数据接口）](#42-数据组织与-manifest-规范数据接口)
  - [4.3 SemanticAlign：语义朝向对齐与几何归一化](#43-semanticalign语义朝向对齐与几何归一化)
  - [4.4 多视角编辑：拼图一次编辑与 Mask 构建](#44-多视角编辑拼图一次编辑与-mask-构建)
  - [4.5 Stage1 编辑质量检查（Edit QC）](#45-stage1-编辑质量检查edit-qc)
  - [4.6 Target 3D 重建与渲染](#46-target-3d-重建与渲染)
  - [4.7 Stage2 重建一致性检查（Target Consistency）](#47-stage2-重建一致性检查target-consistency)
  - [4.8 模型与外部服务调用清单（来自 `config/config.yaml`）](#48-模型与外部服务调用清单来自-configconfigyaml)
- [5. 实验装置和测试方法](#5-实验装置和测试方法)
- [6. 实验结果分析与讨论](#6-实验结果分析与讨论)
- [7. 研究成果（系统与数据集）](#7-研究成果系统与数据集)
- [8. 结论及意义](#8-结论及意义)
- [9. 局限性与改进方向](#9-局限性与改进方向)
- [附录A：代码与脚本索引](#附录a代码与脚本索引)

---

## 1. 工作目的与研究背景

### 1.1 工作目的

面向 3D 编辑/重建相关任务，尤其是需要 **多视角一致性** 的 3D 编辑模型训练，高质量配对数据（指令 + 多视角监督 + 3D 目标）稀缺。本毕设的目标是构建一条工程化数据生产流水线，自动生成 triplet：

\[
\textbf{source 3D} \;\;\leftrightarrow\;\; \textbf{edited multi-view 2D} \;\;\leftrightarrow\;\; \textbf{target 3D}
\]

并通过自动化质量控制筛选可用样本，最终以 **manifest** 形式输出，作为训练/评测侧的统一数据入口。

### 1.2 背景难点与工程约束

- **朝向与坐标系不一致**：外部 3D 生成服务输出的 GLB 朝向不可控，导致“front/back/left/right/top/bottom”语义混乱，无法跨样本对齐对比。
- **跨视角编辑一致性困难**：逐视角编辑会引入矛盾（同一部件在不同视角形变不一致），导致监督噪声。
- **质量不可控**：
  - 2D 编辑可能偏离指令，或发生“非法编辑”（整物替换、仅材质纹理变化、表面文字/Logo 改动等）。
  - 3D 重建可能无法忠实还原 edited views，使 edited 2D 与 target 3D 失配。
- **可复现与可追溯**：流水线依赖多个外部 API 与渲染工具（bpy/Chromium），必须具备断点恢复、事件记录、失败显性化机制。

---

## 2. 国内外研究现状

本课题处于 2D 生成/编辑与 3D 生成/重建的交叉点，相关研究可概括为以下方向（论文中可补充你引用的代表性论文/综述）：

### 2.1 Text/Image-to-3D 生成

国外研究中，基于扩散模型与 NeRF/mesh 的 Text-to-3D、Image-to-3D 方法迅速发展（例如 DreamFusion、Magic3D、Zero123 等系列思路）。这些方法推动了 3D 生成能力，但在工程落地时常见问题包括：姿态随机、纹理材质不稳定、跨视角细节不一致等。

国内与产业侧更常见的是 API 化的图生 3D 服务，具备较强的可用性与吞吐，但同样存在朝向随机、可控性不足、难以统一对齐/统计的问题。

### 2.2 多视角一致性与 3D 监督

多视角一致性是 3D 任务的核心约束。现有工作通常通过（1）模型侧跨视角一致性约束，（2）多视角条件输入，（3）高质量多视角数据监督来提升一致性。相比直接改模型，本毕设主要从 **数据侧** 提升监督质量。

### 2.3 指令驱动图像编辑

单视角指令编辑模型在“局部外观变化”上效果显著，但用于 3D/多视角场景容易产生：跨视角不一致、结构编辑退化为纹理变化、对不可见面出现幻觉修改等问题。因此需要专门的多视角编辑策略与过滤机制。

### 2.4 数据接口与工程化数据集（Manifest）

在工程数据生产中，manifest/schema 作为连接“生产系统”和“训练系统”的契约至关重要。其核心作用是：统一字段语义、保证资产完整性、形成可审计的筛选口径，从而使数据可复用、可统计、可复现。

---

## 3. 理论分析与问题定义

### 3.1 Triplet 样本定义

定义一条样本包含：

- 源 3D 模型 \(S\)
- 视角集合 \(\mathcal{V}=\{\text{front, back, left, right, top, bottom}\}\)
- 渲染算子 \(\mathcal{R}(\cdot)\)，得到 \(\mathcal{R}(S)=\{I^{src}_v\}_{v\in\mathcal{V}}\)
- 编辑指令文本 \(x\)（remove/replace）
- 编辑后的多视角图像 \(\{I^{edit}_v\}_{v\in\mathcal{V}}\)
- 目标 3D 模型 \(T\)，其渲染 \(\mathcal{R}(T)=\{I^{tgt}_v\}_{v\in\mathcal{V}}\)

### 3.2 两阶段质量目标

- **Stage1（Edit QC）**：\(\{I^{edit}_v\}\) 必须跨视角自洽，且对指令 \(x\) 的执行有明确且合法的证据（避免伪编辑/非法编辑）。
- **Stage2（Consistency）**：目标 3D 的渲染 \(\mathcal{R}(T)\) 需要与 \(\{I^{edit}_v\}\) 在感知或语义上匹配（确保 target 3D 忠实还原 edited views）。

### 3.3 对齐一致性的必要性（坐标系/朝向/尺度/framing）

若 source/target 的坐标系与朝向不一致，则“同名视角”之间不可比，导致 Stage2 失效。因此必须先将 GLB 输出对齐到 canonical 朝向；可选通过归一化参数（center/scale/framing）使 source 与 target 在相机 framing 上更一致，提高对比稳定性。

---

## 4. 研究方法（计算方法）

### 4.1 技术路线总览（Stage0～Stage2）

本毕设的端到端流水线可概括为：

```text
T2I → source_image
  → Gen3D → source_glb
  → SemanticAlign → source_glb_aligned (+norm_params.json 可选)
  → Render(6 views) → source_views
  → Multiview Edit → edited_views + per_view_masks
  → Stage1 Edit QC → passed?
    → (passed) Gen3D recon → target_glb
    → SemanticAlign (share rotation/norm 可选) → target_glb_aligned
    → Render(6 views) → target_views
    → Stage2 Consistency → passed?
      → (passed) 写入 dataset manifest
```

工程入口脚本：

- **端到端编排**：`2d3d_v2/scripts/run_full_experiment.py`
- **离线分阶段处理**：`2d3d_v2/scripts/batch_process.py`

### 4.2 数据组织与 Manifest 规范（数据接口）

#### 4.2.1 数据目录结构与命名（以 `data_manifest_guide.md` 为准）

数据根目录示例：

- `data_root = /data-oss/meiguang/xiaoliang/data/2d3d_data/`

关键结构（简化）：

```text
images/{source_model_id}/image.png
models_src/{source_model_id}/model_hy3.glb
models_src/{source_model_id}/model_hy3_aligned.glb
models_src/{source_model_id}_edit_{edit_id}/model_hy3.glb
models_src/{source_model_id}_edit_{edit_id}/model_hy3_aligned.glb
triplets/{source_model_id}/views/hy3/front.png ... bottom.png
triplets/{source_model_id}/edited/{edit_id}/front.png ... bottom.png
triplets/{source_model_id}_edit_{edit_id}/views/hy3/front.png ... bottom.png
```

命名规则：

- `source_model_id`：12 位 hex
- `edit_id`：8 位 hex
- `target_model_id = {source_model_id}_edit_{edit_id}`
- `provider_id`：如 `hy3`（决定 GLB 命名与视角目录）

#### 4.2.2 aligned 与非 aligned 的论文口径

- 原始 GLB：`model_*.glb`（生成服务直接输出，朝向不可控）
- 对齐 GLB：`model_*_aligned.glb`（SemanticAlign 统一 canonical 朝向）

当启用 `normalize_geometry` 时，会额外写出 `norm_params.json`，用于共享 bbox center/max_dim 等参数，使 source 与 target 在尺度与相机 framing 上更可比。

#### 4.2.3 manifest 生成与入库判定

入库口径：

- **Stage2 通过** 的样本才写入最终 manifest（确保 target 3D 与 edited views 匹配）

生成脚本：

- `2d3d_v2/scripts/generate_data_manifest.py`：解析实验日志提取 Stage2 passed 的 edit，汇总资产路径，并输出缺失资产统计 `missing_assets_summary`（应接近全 0）。

### 4.3 SemanticAlign：语义朝向对齐与几何归一化

#### 4.3.1 语义 front 判定（VLM）

模块：`2d3d_v2/core/render/semantic_view_aligner.py`

核心流程：

1. 将 first-pass 的六视角渲染拼成固定顺序的 3×2 grid。
2. 调用 VLM 返回严格 JSON：`semantic_front_from`、`confidence`、`reason`。
3. Fail Loudly：字段缺失/类型不对/置信度低于阈值直接判失败，避免脏数据进入后续。

#### 4.3.2 旋转矩阵与 roll 稳定

将 `semantic_front_from` 对应方向旋转到 canonical front，并进一步进行 roll 稳定，使 upright 更稳定（解决仅靠 front 约束导致的欠定问题）。

#### 4.3.3 GLB 对齐落盘（bpy 子进程）

脚本：`2d3d_v2/scripts/bpy_align_standalone.py`

- 输出 `model_*_aligned.glb`
- 可选 `--normalize`：计算 bbox center/max_dim，做 center+scale 归一化并输出 `norm_params.json`
- target 阶段可选择共享 source 的 rotation 与归一化参数（提高一致性与效率）

渲染调度入口：`2d3d_v2/scripts/run_render_batch.py`（first-pass → align → final-pass/remap）。

### 4.4 多视角编辑：拼图一次编辑与 Mask 构建

#### 4.4.1 拼图一次编辑（提升跨视角一致性）

- 拼图：`2d3d_v2/core/image/view_stitcher.py`（3×2 grid，支持 `pad_to_square`）
- 编辑：`2d3d_v2/core/image/multiview_editor.py`（拼图一次调用图像编辑模型）

核心思想：把六视角作为一个整体编辑，再按拼接元数据精确裁回视角，从而降低跨视角矛盾，同时减少 API 调用次数。

#### 4.4.2 mask 与可视化资产

`2d3d_v2/core/image/edit_artifact_builder.py` 会生成：

- `before_image_grid.png` / `target_image_grid.png`
- `front_mask.png ... bottom_mask.png`
- `edit_mask_grid.png`

mask 可用于训练时的 attention 引导或 loss 加权。

### 4.5 Stage1 编辑质量检查（Edit QC）

Stage1 的目标是判断 edited views 是否符合“可用监督信号”的要求，检查维度包括：

- **跨视角几何一致性**（view sanity）
- **指令遵循**（instruction following）
- **编辑合法性**（legality：拦截整物替换、材质/纹理变化、文字/Logo 改动等）

路由入口：`2d3d_v2/core/image/edit_quality_router.py`，支持三种方法：

- `grid_vlm`：`edit_quality_checker.py`（before/after grid 一次 VLM 判定）
- `two_stage_recon`：`edit_quality_checker_v2.py`（VLM diff → LLM judge，含 view sanity；可选 relabel）
- `unified_judge`：`edit_quality_checker_unified.py`（单次 VLM 输出结构化 JSON：sanity/legality/following/relabel）

### 4.6 Target 3D 重建与渲染

从 `edited_views` 触发 target 重建：

1. target Gen3D：输出 `models_src/{target_model_id}/model_*.glb`
2. target SemanticAlign：
   - 可选 `share_rotation_to_target=true`：复用 source rotation，减少随机性
   - 可选 `normalize_geometry=true`：读取 source `norm_params.json` 共享尺度/framing
3. target Render：输出 `triplets/{target_model_id}/views/{provider_id}/front..bottom.png` 与 `target_render_grid.png`

### 4.7 Stage2 重建一致性检查（Target Consistency）

模块：`2d3d_v2/core/render/recon_consistency_checker.py`

判定目标：target renders 是否忠实还原 edited views。支持两种实现口径：

- **LPIPS**：逐视角感知距离 → 聚合（mean/max）→ 阈值判定（可选 grayscale 弱化颜色差异）
- **VLM**：edited grid vs target grid（可选加 source grid）→ 输出 pass/confidence/reason

Stage2 结果写入 `models_src/{target_model_id}/meta.json`（含 reason/score 等），且只有 Stage2 passed 才会进入 manifest。

### 4.8 模型与外部服务调用清单（来自 `config/config.yaml`）

> 为避免泄露敏感信息，本文不展示任何 API Key，仅列模型与用途。

- **OneAPI 文本/判定（LLM/VLM）**
  - `gpt-5`：文本生成/文本判定（例如 Method-2 judge）
  - `gemini-3-flash-preview`：轻量 VLM（grid judge、diff、view sanity 等）
  - `gemini-3.1-pro-preview`：强 VLM（`unified_judge` 等结构化判定）
- **OneAPI 图像生成/编辑（Response API）**
  - `gemini-2.5-flash-image`：T2I、单视角编辑
  - `gemini-3-pro-image-preview`：多视角拼图编辑
- **3D 生成服务**
  - Tripo：`v3.1-20260211`（默认配置）
  - Hunyuan：`hunyuan-3d-pro` / `hunyuan-3d-3.1-pro`（可选，经 OneAPI）
  - Rodin：Gen-2（可选）
- **渲染后端**
  - Blender（bpy 子进程）
  - WebGL（Headless Chrome + Playwright + `model-viewer`）

---

## 5. 实验装置和测试方法

### 5.1 实验装置（软件与运行环境）

软件模块：

- pipeline：`2d3d_v2`（`run_full_experiment.py` / `batch_process.py`）
- 渲染：Blender 或 WebGL（Chromium + Playwright）
- 外部服务：OneAPI（LLM/VLM/图像）、Tripo/Hunyuan/Rodin（3D 生成）

工程装置要点：

- 渲染与对齐相关步骤通过子进程运行（bpy 与 Chromium），避免崩溃/hang 影响主流程。
- 关键步骤 Fail Loudly（schema 校验、缺失资产直接失败），保证数据可审计。

### 5.2 测试方法（质量控制与完整性检查）

#### 5.2.1 Stage1（Edit QC）测试方法

- 输入：before/after grid 或六视角图
- 输出：结构化判定（pass/fail + reason + supporting views + 可选 relabel）
- 覆盖：一致性（view sanity）+ 指令遵循 + 合法性

#### 5.2.2 Stage2（Consistency）测试方法

- LPIPS：以感知距离与阈值筛选（越小越一致）
- VLM：以 pass/confidence/reason 判定（越高越可信，取决于实现）

#### 5.2.3 入库与完整性测试

- 入库：仅 Stage2 passed 写入 manifest
- 完整性：`missing_assets_summary` 统计应接近全 0；若非 0 表明资产缺失，需要补渲染/补 mask/补 meta。

---

## 6. 实验结果分析与讨论

### 6.1 结果汇总（示例实验口径）

根据 `data_manifest_guide.md` 的实验说明：

- 实验：`prompt-improve-test03`
- 规模：20 类别 × 5 物体 × 4 指令（理论 400）
- Stage2 通过：224 条写入 manifest（`total_triplets=224`）

> 注：`stage2_score` 的方向性取决于 Stage2 方法：
> - 若为 LPIPS：越小越好；
> - 若为 VLM confidence：越大越好。  
> 论文中需要明确你采用的 Stage2 method，并统一解释指标含义。

### 6.2 误差来源与失败案例（讨论框架）

可按阶段归因并写入论文“讨论”：

- **Stage1 失败**
  - 多视角不自洽（部件漂移/断裂/相互矛盾）
  - 指令未执行（变化弱或与目标部件无关）
  - 非法编辑（纹理/材质变化替代结构编辑、整物替换、文字/Logo 改动）
- **Stage2 失败**
  - 3D 重建无法融合多视角局部变化（重建能力瓶颈）
  - framing/尺度差异导致对比误差（可通过共享 `norm_params.json` 缓解）
  - 2D 编辑引入的细粒度纹理变化难稳定重建（LPIPS 更敏感、VLM 更偏语义，需要解释差异）

### 6.3 关键设计的有效性（讨论框架）

- **SemanticAlign**：把随机姿态输出变为 canonical 视角语义，提升跨样本对齐与 Stage2 可比性。
- **拼图一次编辑**：显著降低跨视角矛盾风险，同时减少编辑 API 调用成本。
- **两阶段质检**：将“2D 编辑有效性”和“3D 还原一致性”解耦，提高筛选可解释性与可控性。

---

## 7. 研究成果（系统与数据集）

### 7.1 系统成果

- 实现了端到端 2D→3D 编辑数据集生产系统 `2d3d_v2`，具备：
  - 断点恢复与统计修复（resume/repair）
  - 关键资产 `meta.json/jsonl` 全链路可追溯
  - 可插拔的 Stage1/Stage2 质检策略
  - 子进程隔离保证渲染稳定性

### 7.2 数据集成果

- 统一的样本目录结构（images/models_src/triplets）
- aligned GLB 与可选 `norm_params.json` 作为对齐/归一化资产
- 统一的训练入口 manifest（字段口径见 `data_manifest_guide.md`）

---

## 8. 结论及意义

### 8.1 结论

本毕设通过工程化手段构建了一条可扩展、可追溯的 2D-to-3D 多视角编辑数据生产 pipeline。其核心策略为：

- 使用 SemanticAlign 统一姿态/朝向，保证视角语义一致；
- 采用拼图一次编辑，提升跨视角一致性；
- 引入 Stage1/Stage2 两阶段质量控制，过滤伪编辑与重建失配样本；
- 通过 manifest 输出统一数据接口，保证训练侧可复用与可统计。

### 8.2 意义

- **方法意义**：以数据工程方式提高多视角一致性监督质量，为 3D 编辑模型训练提供更干净的数据基础。
- **工程意义**：将多外部依赖的生成/渲染/判定过程封装为可诊断、可复现的生产系统，支持后续规模扩展与迭代。

---

## 9. 局限性与改进方向

- **外部服务波动**：API 版本与负载会影响产物质量，需在论文中强调 config snapshot 与日志固化。
- **Stage2 指标口径差异**：LPIPS 与 VLM score 不同口径，建议分别统计与报告。
- **重建输入利用不足**：未来可探索更强的多视角条件重建或显式几何一致性约束，提升 target 还原度。

---

## 附录A：代码与脚本索引

- **Manifest**
  - 规范说明：`data_manifest_guide.md`
  - 生成脚本：`2d3d_v2/scripts/generate_data_manifest.py`
- **编排与批处理**
  - 端到端：`2d3d_v2/scripts/run_full_experiment.py`
  - 分阶段：`2d3d_v2/scripts/batch_process.py`
- **对齐与渲染**
  - semantic front 判定：`2d3d_v2/core/render/semantic_view_aligner.py`
  - GLB 对齐：`2d3d_v2/scripts/bpy_align_standalone.py`
  - 渲染调度：`2d3d_v2/scripts/run_render_batch.py`
  - 渲染子进程：`2d3d_v2/scripts/bpy_render_standalone.py`、`2d3d_v2/scripts/webgl_render_standalone.py`
- **多视角编辑与质检**
  - 拼图/裁回：`2d3d_v2/core/image/view_stitcher.py`、`2d3d_v2/core/image/multiview_editor.py`
  - mask/grid：`2d3d_v2/core/image/edit_artifact_builder.py`
  - Stage1：`2d3d_v2/core/image/edit_quality_checker_*.py`
  - Stage2：`2d3d_v2/core/render/recon_consistency_checker.py`

