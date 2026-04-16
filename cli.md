# CLI 命令行工具指南

本文档详细介绍 `batch_process.py` 统一批量处理脚本的所有命令和参数。

## 概述

`scripts/batch_process.py` 是项目的核心批量处理工具，支持：
- **断点续执行**：自动跳过已完成的任务，支持中断后恢复
- **并发控制**：根据 `config/config.yaml` 配置的并发数执行
- **进度追踪**：实时显示处理进度
- **干运行模式**：预览待处理项目而不实际执行

---

## 通用参数

所有子命令都支持以下参数：

| 参数 | 简写 | 说明 |
|------|------|------|
| `--force` | `-f` | 强制重新执行，忽略已有结果 |
| `--dry-run` | `-n` | 仅显示待处理项，不实际执行 |
| `--ids ID1 ID2 ...` | - | 指定要处理的 ID 列表 |

---

## 命令详解

### 1. `gen3d` - 批量生成 3D 模型

从源图像批量生成 3D 模型。

```bash
python scripts/batch_process.py gen3d --provider <provider> [options]
```

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--provider`, `-p` | ✅ | 3D 生成服务商：`hunyuan`, `tripo`, `rodin` |
| `--ids` | - | 指定图像 ID 列表 |
| `--force`, `-f` | - | 强制重新生成（即使已有 3D 模型） |
| `--dry-run`, `-n` | - | 预览模式 |

**断点检测规则：**
- 检查 `models_src/{id}/` 目录下是否存在 `.glb` 文件
- 存在则跳过，不存在则生成

**示例：**

```bash
# 为所有未生成 3D 的图像生成模型
python scripts/batch_process.py gen3d --provider hunyuan

# 为指定图像生成
python scripts/batch_process.py gen3d --provider hunyuan --ids abc123 def456

# 强制重新生成
python scripts/batch_process.py gen3d --provider hunyuan --force

# 预览待处理项
python scripts/batch_process.py gen3d --provider hunyuan --dry-run
```

---

### 2. `render` - 批量渲染多视角图像

为 3D 模型渲染 6 个标准视角（front, back, left, right, top, bottom）。

```bash
python scripts/batch_process.py render [options]
```

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--provider` | - | 指定要渲染的 3D 模型 provider（`tripo`, `hunyuan`, `rodin`）。默认从 `config.tasks["gen3d"].provider` 读取 |
| `--ids` | - | 指定模型 ID 列表 |
| `--force`, `-f` | - | 强制重新渲染（即使已有视图） |
| `--dry-run`, `-n` | - | 预览模式 |

**Provider 选择：**

系统会为每个模型保存不同 provider 生成的 3D 模型（`model_tp3.glb`, `model_hy3.glb` 等）。渲染时需指定要渲染哪个 provider 的模型：

```bash
# 不指定 provider，使用 config 中的默认值（通常是 tripo）
python scripts/batch_process.py render

# 指定渲染 Hunyuan 生成的模型
python scripts/batch_process.py render --provider hunyuan

# 指定渲染 Tripo 生成的模型（默认）
python scripts/batch_process.py render --provider tripo
```

**渲染后端：**

渲染后端在 `config/config.yaml` 中配置：

```yaml
render:
  backend: "blender"  # 或 "webgl"
  rotation_z: 0       # 启用 semantic_alignment 时必须为 0
  semantic_alignment:
    enabled: true
    vlm_model: "gemini-3-flash-preview"
    min_confidence: 0.5
    verify_after_rerender: true
    save_aligned_glb: true
    aligned_glb_suffix: "_aligned"
    save_debug_assets: true
    temp_dir_name: "_semantic_tmp"
```

| 后端 | 引擎 | 特点 | 适用场景 |
|------|------|------|----------|
| `blender` | Blender + Cycles/Eevee | GPU 加速，功能完整 | 需要精细控制，已安装 Blender |
| `webgl` | Headless Chrome + model-viewer | PBR 光照效果更好，部署简单 | 追求渲染质量，无 Blender 环境 |

**WebGL 后端配置：**

```yaml
render:
  backend: "webgl"
  webgl:
    environment_image: "neutral"  # IBL 环境光照
    shadow_intensity: 0.0
    use_gpu: true
```

**安装 WebGL 依赖：**

```bash
# 安装 playwright
uv pip install playwright --python /home/xiaoliang/local_envs/2d3d/bin/python

# 安装 Chromium 浏览器
python -m playwright install chromium
```

**断点检测规则：**
- 检查 `triplets/{id}/views/{provider_id}/` 目录下是否存在 `.png` 文件
- 存在则跳过，不存在则渲染

**语义对齐渲染流程（`render.semantic_alignment.enabled=true`）：**
1. 首轮渲染输出到 `views/{provider_id}/_semantic_tmp/first_pass_views/`
2. VLM 判定语义 `front`（front-only）
3. 计算旋转矩阵并导出 `model_{provider_id}_aligned.glb`
4. 使用 aligned GLB 重渲染最终 6 视角到 `views/{provider_id}/`（覆盖同名旧图）
5. 可选二次验证，不通过则任务失败（Fail Loudly）

说明：前端始终展示 `views/{provider_id}/`，语义对齐开启后无需切换路径。

**示例：**

```bash
# 为所有未渲染的模型生成视图（使用 config 中的默认 provider）
python scripts/batch_process.py render

# 指定 provider 渲染（如果config中默认不是你要的）
python scripts/batch_process.py render --provider hunyuan

# 为指定模型渲染
python scripts/batch_process.py render --ids model1 model2

# 为指定模型渲染指定 provider
python scripts/batch_process.py render --provider hunyuan --ids model1 model2

# 强制重新渲染
python scripts/batch_process.py render --force
```

---

### 3. `edit` - 批量编辑视图

使用编辑指令对渲染视图进行编辑。

```bash
python scripts/batch_process.py edit [options]
```

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--ids` | - | 指定模型 ID 列表 |
| `--instr-index`, `-i` | - | 使用第 N 条指令（0=第一条，1=第二条） |
| `--instruction` | - | 自定义指令文本（覆盖 `--instr-index`） |
| `--all-instructions`, `-a` | - | 使用所有指令（每条分别创建编辑） |
| `--max-per-type`, `-m` | - | 每类指令（remove/replace）每模型最多处理数量。1=每类一条（默认），0=无限制 |
| `--views` | - | 指定要编辑的视角（默认全部 6 个） |
| `--mode` | - | 编辑模式：`multiview`（默认）或 `single` |
| `--force`, `-f` | - | 强制重新编辑（即使相同指令已编辑过） |
| `--dry-run`, `-n` | - | 预览模式 |

**断点检测规则：**
- 遍历 `triplets/{id}/edited/*/meta.json`
- 比较 `meta.instruction` 与当前指令（大小写不敏感）
- 相同则跳过，不同则创建新编辑

**编辑模式说明：**

| 模式 | 说明 | 模型 |
|------|------|------|
| `multiview` | 将 6 视角拼接为 3×2 网格，一次性编辑 | Gemini 3 Pro |
| `single` | 先编辑源图像，再用结果引导各视角编辑 | Gemini 2.5 Flash |

**图像尺寸策略（显式失败）：**
- 编辑链路统一显式传入 `size`，来源为当前任务所用模型在 `config.yaml` 中的 `oneapi.image_models.<model>.size`。
- 编辑链路统一使用 `auto_size=False`，不再按输入图分辨率自动覆盖输出尺寸。
- 当 `size` 缺失、为空或仅空白字符时，任务会立即报错，不会发送不完整请求到远端。
- `render.image_size` 仅影响渲染视图尺寸，不作为图像编辑 API 的 `size` 参数来源。

**示例：**

```bash
# 使用第一条指令编辑（自动跳过已编辑）
python scripts/batch_process.py edit

# 使用第二条指令（通常是 Replace）
python scripts/batch_process.py edit --instr-index 1

# 使用所有指令
python scripts/batch_process.py edit --all-instructions

# 使用所有指令，但每种类型(remove/replace)只处理1条
python scripts/batch_process.py edit --all-instructions --max-per-type 1

# 自定义指令
python scripts/batch_process.py edit --instruction "Remove the wheels from the vehicle."

# 强制重新编辑
python scripts/batch_process.py edit --force

# 使用单视角模式
python scripts/batch_process.py edit --mode single
```

---

### 4. `gen3d-from-edits` - 批量生成目标 3D 模型

从编辑后的视图批量生成目标 3D 模型（Target 3D）。

```bash
python scripts/batch_process.py gen3d-from-edits --provider <provider> [options]
```

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--provider`, `-p` | ✅ | 3D 生成服务商：`hunyuan`, `tripo`, `rodin` |
| `--ids` | - | 按源模型 ID 过滤（只处理这些模型的编辑批次） |
| `--max-per-model`, `-m` | - | 每个模型最多处理的编辑批次数量。1=每模型一个（默认），0=无限制，2=每模型两个 |
| `--force`, `-f` | - | 强制重新生成（即使已有 Target 3D） |
| `--dry-run`, `-n` | - | 预览模式 |

**断点检测规则：**
- 检查 `models_src/{model_id}_edit_{edit_id}/` 目录下是否存在 `.glb` 文件
- 存在则跳过，不存在则生成

**工作流程：**
1. 扫描所有 `triplets/*/edited/*/` 编辑批次
2. 检查每个批次是否已有对应的 Target 3D
3. 使用编辑后的 `front.png` 作为输入生成新 3D

**错误记录：**
- 不改变当前重试/失败主流程，只新增失败原因分类与落盘字段。
- `scripts/batch_process.py gen3d-from-edits` 和 `run_full_experiment.py` 会记录：
  - `target_gen3d_error_class`
  - `target_gen3d_error_message`
- 当前分类值：
  - `quota_limit`
  - `subject_too_small`
  - `upload_error`
  - `provider_failed`
  - `unknown`

**示例：**

```bash
# 为所有编辑批次生成 Target 3D
python scripts/batch_process.py gen3d-from-edits --provider hunyuan

# 只处理特定源模型的编辑
python scripts/batch_process.py gen3d-from-edits --provider hunyuan --ids model1 model2

# 每个模型只处理1个编辑批次
python scripts/batch_process.py gen3d-from-edits --provider hunyuan --max-per-model 1

# 强制重新生成
python scripts/batch_process.py gen3d-from-edits --provider hunyuan --force

# 预览待处理项
python scripts/batch_process.py gen3d-from-edits --provider hunyuan --dry-run
```

---

### 5. `check-edit-quality` - 重检已有编辑结果质量

对已有 `edited` 批次重新执行编辑质量检查，并将结果写回 `meta.json`。

```bash
python scripts/batch_process.py check-edit-quality [options]
```

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--ids` | - | 指定源模型 ID 列表 |
| `--edit-id` | - | 只重检指定 edit 批次 |
| `--dry-run`, `-n` | - | 预览模式 |

**行为说明：**
- 检查方法由 `config.yaml` 中的 `edit_quality_check.method` 决定：
  - `grid_vlm`（Method-1）：使用编辑前/编辑后 3×2 六视图拼图 + VLM 判定
  - `two_stage_recon`（Method-2）：
    - `edit_view_policy=front_only|all_6`：Stage 1A VLM diff（逐视角对比）+ Stage 1B LLM judge（文本判定）
    - `edit_view_policy=stitched_6`：先拼接 before/after 六视角，再做一次 Stage 1A + Stage 1B
  - `unified_judge`（Method-3）：
    - 单次 VLM 调用同时输入 before/after 六视图拼图 + `edit_mask_grid.png`
    - 输出 `observation`、`supporting_views`、`evidence_strength`、`instruction_legality`、`view_sanity`、`instruction_following`、`relabel`
    - pass 改为 evidence-based：小编辑可以通过，但必须在当前图片信息中真实可见、可定位；若只是弱噪点或主要依赖猜测，则不能通过
    - 若 `edit_quality_check.unified_judge.require_non_weak_evidence=true` 且 `evidence_strength=weak`，则直接判为失败
    - 如果 AFTER 图中物体主体或关键编辑区域超出图像边界、被裁切到无法完整观察，则 `view_sanity` 必须失败
    - 新增 post-edit legality audit：基于 BEFORE / AFTER / MASK 判断“实际发生的编辑”是否合法，而不是只看 instruction 文本
    - `instruction_legality=reject` 时，Stage1 直接失败；当前重点拦截三类结果：
      - `main_body`：实际结果接近整物或主体替换
      - `appearance_only`：实际结果主要是 logo / emblem / label / seam line 等表面变化
      - `material_only`：实际结果主要是材质、光泽、纹理、反射变化，几何主体基本不变
- 检查结果写入 `triplets/{model_id}/edited/{edit_id}/meta.json`：
  - `edit_status=passed`：在前端归入 Edited Versions
  - `edit_status=failed_quality` 或 `error_quality_check`：在前端归入 Failed Editing
- Method-2 / Method-3 会额外写入 `quality_check.stage_edit_correctness`；其中 Method-3 会保留完整 `unified_result`（含 `supporting_views` 与 `evidence_strength`）
- 终端日志会输出详细 `EditQC` 信息（`START/RESULT/ERROR`、reason、方法相关资产路径、raw_response）。

**instruction 生成前置约束：**
- instruction generation 现在在进入编辑前就做确定性 legality 校验。
- 前置规则分两层：
  - Layer A：语法/格式校验，只允许 `Remove ...` / `Replace ...`，并要求 `type` 与文本动作一致。
  - Layer B：高置信直拒，只拦明确非法的 instruction：
    - whole-object / main-body 编辑
    - logo / emblem / label / seam line 这类表面编辑
    - texture / color / material / finish / gloss 这类外观编辑
    - 明确 material swap，例如 `wood -> metal`、`fabric -> leather`
- prompt 也同步加强，不再鼓励：
  - 整物替换
  - 表面标记编辑
  - 材质替换
- 模糊 case 不在前置规则层硬拦，而是允许进入编辑，再由 Stage1 看实际结果是否合法。

**示例：**

```bash
# 重检某个模型的所有 edit 批次
python scripts/batch_process.py check-edit-quality --ids 22af6bb4d520

# 只重检某一条 edit 批次
python scripts/batch_process.py check-edit-quality --ids 22af6bb4d520 --edit-id 3def0294

# 预览将要重检的批次
python scripts/batch_process.py check-edit-quality --ids 22af6bb4d520 --dry-run
```

---

### 6. `check-target-consistency` - 执行 Stage2 LPIPS 一致性检测

对已存在的 Target 3D 执行 LPIPS 一致性检查，并将结果写回 target `meta.json`。

```bash
python scripts/batch_process.py check-target-consistency --provider <provider> [options]
```

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--provider`, `-p` | ✅ | 要检测的 Target provider：`tripo` / `hunyuan` / `rodin` |
| `--ids` | - | 按源模型 ID 过滤 |
| `--edit-id` | - | 只检测指定 edit 批次 |
| `--target-ids` | - | 直接指定 target id（格式：`<model_id>_edit_<edit_id>`） |
| `--skip-render` | - | 只做检测，不触发 target 重新渲染（推荐） |
| `--force-render`, `-f` | - | 检测前强制重渲染 target 视图 |
| `--dry-run`, `-n` | - | 预览模式 |

**行为说明：**
- 会读取 `triplets/{model_id}/edited/{edit_id}/` 作为 target image 参考视图。
- 会读取 `triplets/{model_id}_edit_{edit_id}/views/{provider_id}/` 作为 target 3D render 视图。
- 结果写入 `models_src/{model_id}_edit_{edit_id}/meta.json`：
  - `target_quality_check`（兼容单结果）
  - `target_quality_checks_by_provider[{provider_id}]`（多 provider 正确绑定）
- 终端会输出 `[Stage2] status/score/threshold` 和完整 JSON 结果。
- `--skip-render` 与 `--force-render` 互斥，不能同时传。
- 当前命令允许在 `edit_quality_check.method = "two_stage_recon"` 或 `"unified_judge"` 下运行。
- Stage2 仍然共享 `edit_quality_check.two_stage_recon.*` 中的 LPIPS 配置；即使 Stage1 使用 `unified_judge`，该配置段也必须存在且合法。
- Stage2 实际比较哪些视角由 `edit_quality_check.two_stage_recon.recon_views` 控制。
- Stage2 LPIPS 输入模式由 `edit_quality_check.two_stage_recon.input_mode` 控制：
  - `rgb`：直接比较渲染 RGB 图
  - `grayscale`：先转灰度，再扩展回 3 通道后送入 LPIPS，适合弱化颜色/纹理差异

**示例：**

```bash
# 常用：纯检测，不触发新渲染/新模型副作用
python scripts/batch_process.py check-target-consistency --provider hunyuan --ids c51228e8ae96 --edit-id 81dfb743 --skip-render

# 指定 target id 直接检测
python scripts/batch_process.py check-target-consistency --provider tripo --target-ids c51228e8ae96_edit_81dfb743 --skip-render

# 检测前强制重渲染 target 视图
python scripts/batch_process.py check-target-consistency --provider hunyuan --ids c51228e8ae96 --edit-id 81dfb743 --force-render
```

---

### 7. `refresh-all-lpips` - 全量重算可刷新的 Stage2 LPIPS

扫描整个 pipeline 中所有 source model，只对“真实存在 target 3D GLB 且已有 target render 视图”的 target/provider 组合重新执行 Stage2 LPIPS，并把新结果写回 target `meta.json`。

```bash
python scripts/batch_process.py refresh-all-lpips [options]
```

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--ids` | - | 可选的 source model ID 列表，只刷新指定模型范围 |
| `--dry-run`, `-n` | - | 仅打印会处理哪些 target/provider，不实际执行 |

**行为说明：**
- 会扫描 `triplets/{model_id}/edited/*/meta.json` 判断有哪些 edit batch。
- 对每个 edit batch，仅当 `models_src/{model_id}_edit_{edit_id}/model_*.glb` 真实存在，且 `triplets/{model_id}_edit_{edit_id}/views/{provider_id}/*.png` 已存在时，才会加入刷新队列。
- provider 由 target GLB 文件名自动推断，不需要手工按 `tripo / hunyuan / rodin` 分别跑一遍。
- 当前命令允许在 `edit_quality_check.method = "two_stage_recon"` 或 `"unified_judge"` 下运行。
- Stage2 仍然共享 `edit_quality_check.two_stage_recon.*` 中的 LPIPS 配置；Stage1 方法名不会改变 Stage2 的参数来源。
- 该命令默认使用更保守的低并发配置 `concurrency.refresh_all_dreamsim`，优先降低 `/seaweedfs` 读写压力。
- 实际执行时只复用已存在的 target render；缺少 render 视图的项会被显式记为 skipped，而不是在全量刷新里临时补渲染。
- 该命令适合在 LPIPS 逻辑、`recon_views`、`input_mode` 更新后，对历史结果做统一重算。

**示例：**

```bash
# 全量刷新所有可计算的 LPIPS
python scripts/batch_process.py refresh-all-lpips

# 只刷新指定 source model
python scripts/batch_process.py refresh-all-lpips --ids 18c3959209d2

# 先预览将会处理哪些 target
python scripts/batch_process.py refresh-all-lpips --dry-run
```

---

### `materialize-edit-artifacts` - 仅补齐缺失的 Mask 资产

对历史 edit batch 执行“仅 mask 补算”：
- 只会补齐 `front/back/right/left/top/bottom_mask.png` 与 `edit_mask_grid.png`
- 若 `before_image_grid.png`、`target_image_grid.png` 缺失，会复用当前统一的 `ViewStitcher` 流程自动重建后再补 mask
- source views 或其他重建所需输入资产缺失时仍会直接失败（Fail Loudly）
- CLI 并发度由 `config.yaml` 中的 `concurrency.mask_backfill` 控制
- 当前 mask 生成算法为 `RGB max-abs diff + threshold + morphological opening`，可通过 `edit_artifacts.diff_threshold` 与 `edit_artifacts.opening_kernel_size` 调节敏感度

```bash
python scripts/batch_process.py materialize-edit-artifacts [options]
```

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--ids` | - | 按 source model ID 过滤 |
| `--edit-id` | - | 只处理指定 edit batch |
| `--force`, `-f` | - | 即使 mask 已存在也强制重算 |
| `--dry-run`, `-n` | - | 仅预览，不实际写入 |

**示例：**

```bash
# 为某个 source model 扫描并补齐缺失 mask
python scripts/batch_process.py materialize-edit-artifacts --ids 0704f5e8bc6b

# 仅处理一个 edit batch
python scripts/batch_process.py materialize-edit-artifacts --ids 0704f5e8bc6b --edit-id bc3f7932

# 强制重算已有 mask
python scripts/batch_process.py materialize-edit-artifacts --ids 0704f5e8bc6b --force

# 预览模式
python scripts/batch_process.py materialize-edit-artifacts --ids 0704f5e8bc6b --dry-run
```

---

## 断点续执行总结

| 命令 | 检测位置 | 跳过条件 |
|------|----------|----------|
| `gen3d` | `models_src/{id}/*.glb` | 已有 3D 模型 |
| `render` | `triplets/{id}/views/{provider_id}/*.png` | 已有渲染视图 |
| `edit` | `triplets/{id}/edited/*/meta.json` | 同一指令已编辑 |
| `gen3d-from-edits` | `models_src/{model_id}_edit_{edit_id}/*.glb` | 已有 Target 3D |
| `check-edit-quality` | `triplets/{model_id}/edited/{edit_id}/meta.json` | 不适用（每次均执行重检） |
| `materialize-edit-artifacts` | `triplets/{model_id}/edited/{edit_id}/edit_mask_grid.png` | 已有完整 mask 资产 |
| `check-target-consistency` | `models_src/{model_id}_edit_{edit_id}/meta.json` | 不适用（每次均执行重检） |
| `refresh-all-lpips` | `models_src/{model_id}_edit_{edit_id}/model_*.glb` | 仅处理真实存在 target 3D 的 provider |

---

## 并发配置

并发数在 `config/config.yaml` 中配置：

```yaml
concurrency:
  gen3d:
    hunyuan: 10
    tripo: 5
    rodin: 3
  render: 1               # Blender/WebGL 渲染建议单线程
  edit_quality_check: 2    # 编辑质检并发数（Method-1 或 Method-2 Stage 1）
  recon_quality_check: 1   # Target 3D 一致性检查并发数（Method-2 Stage 2，建议串行）
  refresh_all_dreamsim: 1  # 全量刷新 LPIPS，建议保持串行以减轻 /seaweedfs 压力
  mask_backfill: 1         # 历史 mask 补算，建议串行执行
```

**注意：** 无论使用 Blender 还是 WebGL 后端，渲染任务都建议设置为单线程（`render: 1`），因为：
- **Blender 后端**：GPU 内存有限，多线程容易导致 OOM
- **WebGL 后端**：每个任务启动一个 headless Chrome 实例，多线程消耗大量内存
- **LPIPS Stage-2**：远端文件系统 I/O 抖动明显，建议 `recon_quality_check: 1` 保持串行

此外，Web API 的 LPIPS 刷新入口（`/api/models/<model_id>/refresh-lpips` 和 `/api/models/refresh-lpips-all`）也会复用同一并发闸门；旧 DreamSim 路径仍保留为兼容别名：
- 当已有 LPIPS 刷新任务在 `pending/running` 时，会返回 `409`，避免重复并发刷新。

---

## 完整工作流示例

```bash
# 1. 生成 3D 模型
python scripts/batch_process.py gen3d --provider hunyuan

# 2. 渲染多视角
python scripts/batch_process.py render

# 3. 编辑视图（使用所有指令）
python scripts/batch_process.py edit --all-instructions

# 4. 对已编辑结果执行质量重检
python scripts/batch_process.py check-edit-quality --ids <model_id>

# 5. 生成 Target 3D（仅针对通过质检或已恢复的 edit）
python scripts/batch_process.py gen3d-from-edits --provider hunyuan

# 6. 对已有 Target 3D 执行 Stage2 一致性检测（推荐纯检测模式）
python scripts/batch_process.py check-target-consistency --provider hunyuan --ids <model_id> --skip-render

# 7. 当 LPIPS 逻辑更新后，统一重算整个数据集的 Stage2 结果
python scripts/batch_process.py refresh-all-lpips

# 如果中途中断，重新运行相同命令即可继续（自动跳过已完成项）
```

---

### 8. `generate_prompts.py` - 批量生成 T2I Prompt（独立命令）

用于直接在终端批量生成 Prompt（不经过 Web 按钮触发）。

```bash
python scripts/generate_prompts.py [options]
```

**参数：**

| 参数 | 简写 | 必填 | 说明 |
|------|------|------|------|
| `--count` | `-n` | - | 生成数量（默认 10） |
| `--category` | `-c` | - | 类别过滤（如 `Furniture`） |
| `--output` | `-o` | - | 指定输出 JSONL 文件 |

**输出路径规则：**
- 传入 `--output`：写入指定文件
- 未传 `--output`：写入 `config.workspace.pipeline_dir/prompts/batch_*.jsonl`
- 与 Web `/prompts` 页面读取目录保持一致

**示例：**

```bash
# 随机类别生成 5 条
python scripts/generate_prompts.py --count 5

# 指定类别生成
python scripts/generate_prompts.py --count 1 --category Furniture

# 自定义输出文件
python scripts/generate_prompts.py --count 20 --output /tmp/my_prompts.jsonl
```

---

### 8. `run_full_experiment.py` - 端到端实验脚本

用于按固定 execution plan 执行完整实验链路；当前 instruction schema 为 `instruction_plan`（adaptive-k）：

```bash
python scripts/run_full_experiment.py --plan <plan.yaml>
```

```bash
python scripts/run_full_experiment.py --plan <plan.yaml> --gpu-id 0
```

**执行链路：**

1. 按类别生成 Prompt
2. T2I 生成源图像
3. 源图像生成 3D
4. 源 3D 渲染 6 视角
5. 一次性生成 adaptive instruction list
6. 对编辑结果执行 Stage 1 质检
7. 仅对通过 Stage 1 的 edit 生成 Target 3D
8. 渲染 Target 3D 并执行 Stage 2 一致性检查

**关键行为：**
- 如果编辑前后图片检查失败，当前 edit 会被丢弃，不会继续 target 3D，而是直接尝试下一个指令。
- instruction generation 会先做前置 legality gate：明确非法的整物替换、表面编辑、材质编辑不会进入 `edit_apply`。
- Stage1 现在同时承担 post-edit legality audit：即使 instruction 文本看起来像结构编辑，只要实际结果只是表面变化或材质变化，也会在 Stage1 被拦掉。
- Prompt / T2I / 3D / render / edit / Stage2 全部复用现有生产代码，不另造一套临时逻辑。
- 并发严格读取 `config.concurrency`：
  - 文本链路（Prompt 优化 / instruction generation）使用 `concurrency.text`
  - Stage-1 quality check（包括 Method-2 内部 diff / judge / relabel / rejudge 文本与 VLM 调用）也走 `concurrency.text`
  - 图像链路（T2I / image edit）使用 `concurrency.image`
  - 3D 生成使用 `concurrency.gen3d.<provider>`
  - 渲染使用 `concurrency.render`
  - Stage2 consistency check 使用 `concurrency.recon_quality_check`
- `run_full_experiment` 额外引入 lane 级 FIFO 排队与 cooldown：同一 lane 报错后会静默 `cooldown_seconds`，恢复时先放行一个 probe 请求。
- 调度器当前采用**保守版全局 object worker 池**：
  - 全实验的 object 会进入同一个 worker pool，而不是按 category 固定绑死 worker
  - 单个 object 内的 edit 仍保持串行执行，优先保证可恢复性与正确性，不做激进的 edit 级异步
- 运行日志会额外输出分阶段 timing，便于直接观察各环节耗时：
  - object 级：`source_prompt_optimization`、`source_t2i`、`source_gen3d`、`source_render`、`instruction_generation`、`source_pipeline_total`、`object_total`
  - edit 级：`edit_apply`、`mask_artifact_build`、`stage1_quality_check`、`stage1_total`、`target_gen3d`、`target_render`、`stage2_consistency_check`、`edit_pipeline_total`
- timing 统计的主口径按“阶段实际执行次数”计算：
  - `mean_seconds = total_seconds / sample_count`
  - 若某阶段发生 retry，则每个 attempt 都会计入 `sample_count` 与 `total_seconds`
- 每次启动时，日志开头都会打印一行 `[run_full_experiment] started pid=... mode=... gpu_id=... target=...`，便于后续按 PID 中断或排查对应进程
- 每次结束时，无论是正常完成、异常失败还是手动中断，日志结尾附近都会打印一行 `[run_full_experiment] finished ... status=...`，明确说明这次 YAML / resume / repair 已经结束

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `--plan` | ✅ | YAML/JSON 实验计划文件 |
| `--gpu-id` | ❌ | 将本次 `run_full_experiment` 进程及其子任务限制到单个可见 GPU；Batch Generation 生成的 CLI 默认会填 `0`，运行前请先自己确认服务器上的 GPU 编号 |

**plan 文件示例：**

```yaml
name: "hunyuan_category_eval"
source_provider: "hunyuan"
target_provider: "hunyuan"
edit_mode: "multiview"

categories:
  - category_name: "Furniture"
    random:
      category: false
      object: false
    objects:
      - "chair"
      - "cabinet"
    object_count: 2
    instruction_plan:
      mode: "adaptive_k"
      count: 4
      allowed_types:
        - "remove"
        - "replace"

  - category_name: "Vehicles"
    random:
      category: false
      object: true
    object_count: 5
    instruction_plan:
      mode: "adaptive_k"
      count: 2
      allowed_types:
        - "remove"
        - "replace"

  - random:
      category: true
      object: true
    object_count: 10
    instruction_plan:
      mode: "adaptive_k"
      count: 1
      allowed_types:
        - "remove"
```

**plan 字段说明：**

| 字段 | 说明 |
|------|------|
| `source_provider` | 源 3D provider：`hunyuan` / `tripo` / `rodin` |
| `target_provider` | 目标 3D provider；不写则与 `source_provider` 相同 |
| `edit_mode` | `single` 或 `multiview` |
| `categories[*].random.category` | 是否随机抽 category。若为 `true`，则 `random.object` 也必须为 `true` |
| `categories[*].random.object` | 是否在选定范围内随机抽具体物体 |
| `categories[*].category_name` | 固定 category 时必填；random category 时不允许填写 |
| `categories[*].objects` | 固定物体模式时必填；其长度必须等于 `object_count` |
| `categories[*].object_count` | 本条 category 配置实际要生成多少个物体 |
| `categories[*].instruction_plan.mode` | 当前固定为 `adaptive_k` |
| `categories[*].instruction_plan.count` | 该类别中每个物体总共要生成多少条 instruction |
| `categories[*].instruction_plan.allowed_types` | 允许 generator 使用的 instruction type 边界，取值为 `remove` / `replace` |

**执行逻辑：**

**run_full_experiment 编排配置：**

```yaml
run_full_experiment:
  retry:
    source_prompt_optimization:
      max_attempts: 2
    source_t2i:
      max_attempts: 2
    source_gen3d:
      max_attempts: 3
    source_render:
      max_attempts: 2
    instruction_generation:
      max_attempts: 2
    edit_apply:
      max_attempts: 2
    stage1:
      max_attempts: 2
    target_gen3d:
      max_attempts: 3
    target_render:
      max_attempts: 2
    stage2:
      max_attempts: 2
  api_lane_control:
    enabled: true
    cooldown_seconds: 5
    recovery_probe_one_by_one: true
  scheduling:
    object_workers_strategy: "provider_weighted"
    object_workers_cap: 6
    provider_pressure_divisor: 2
```

说明：
- 这里只放实验编排参数；模型、timeout、base_url 等 API 参数仍然放在各自 provider 配置段。
- `stage1` 的最终失败只影响当前 edit；`stage2` 的最终失败也只影响当前 edit，不会打断整个 object。
- 每个阶段是否重试、重试几次，完全由 `config.yaml` 决定。
- `stage1` 外层重试现在区分失败类型：`failed_quality` 会在首轮失败后直接停止；`error_quality_check` 这类执行错误才会继续按 `max_attempts` 重试。

- 全局 `object worker` 数不由 plan 控制，而是按当前 `source_provider` / `target_provider` 与 `config.run_full_experiment.scheduling` 动态推导
- 推导公式为：
  - 若 `source_provider == target_provider`：`gen3d_pressure = max(source_gen3d, target_gen3d)`
  - 否则：`gen3d_pressure = source_gen3d + target_gen3d`
  - `object_workers = min(image + ceil(gen3d_pressure / provider_pressure_divisor) + render_buffer, object_workers_cap)`
- 调度粒度为 object：重阶段 object 不会长期占住某个 category 的唯一 worker，其他 category 的 object 可以继续推进
- 每个 object 只走一遍固定流程：prompt -> T2I -> source 3D -> source render -> instruction batch generation -> edit -> Stage1 -> target 3D -> target render -> Stage2
- 不再存在“为了凑够通过数量而反复重试”的循环
- instruction generation 会先一次性返回完整 `count` 条 instruction；每条编辑指令只执行一次；Stage1 或 Stage2 失败会被记录为失败，然后继续执行下一条指令
- 同一个 YAML 内，固定配置的 `category_name` 也不能重复；重复固定 category 会直接报错，避免类别分布失衡
- 同一个 YAML 内，如果存在多条 `random.category=true` 的 category 配置，它们会被分配到**不同的 category**；不会重复落到同一个 category，否则会导致类别样本不平衡
- 不再支持旧字段：`category_workers`、`instruction_type_ratio`、`prompt_budget`、`target_source_models`、`accepted_edits_per_model`、`max_instruction_attempts`、`style_ids`

**输出：**
- `prompts/batch_<experiment_id>.jsonl`
- `experiments/<experiment_id>/events.jsonl`
- `experiments/<experiment_id>/object_records.jsonl`
- `experiments/<experiment_id>/edit_records.jsonl`
- `experiments/<experiment_id>/category_stats.json`
- `experiments/<experiment_id>/category_stats.csv`
- `experiments/<experiment_id>/summary.json`
- `experiments/<experiment_id>/summary.csv`
- `experiments/<experiment_id>/stage_timing_summary.csv`
- `experiments/<experiment_id>/manifest.json`

其中 timing 相关新增字段 / 产物为：
- `object_records.jsonl` / `edit_records.jsonl`
  - 每条记录新增 `timings` 与 `timing_attempts`
- `summary.json`
  - 新增 `stage_timing_summary`、`attempt_timing_summary`、`final_record_timing_summary`
- `stage_timing_summary.csv`
  - 每行一个阶段统计，包含 `sample_count`、`total_seconds`、`mean_seconds`、`p90_seconds`、`failed_count`、`skipped_count`
- `edit_records.jsonl`
  - Target 3D 失败记录新增 `target_gen3d_error_class` 与 `target_gen3d_error_message`

**指令生成 prompt 说明：**
- `run_full_experiment.py` 现在默认使用 adaptive prompt：
  - `utils/prompts.py` 中的 `INSTRUCTION_ADAPTIVE_K_PROMPT`
  - `core/image/caption.py` 中的 `generate_adaptive_instructions(...)`
- 旧的单条 prompt 仍保留给兼容入口：
  - `INSTRUCTION_REMOVE_PROMPT`
  - `INSTRUCTION_REPLACE_PROMPT`
- 运行时还会追加一段 multiview-safe 约束，定义在 `core/image/caption.py`
  - `INSTRUCTION_MULTIVIEW_EXTRA_CONSTRAINT`
- adaptive prompt 会显式要求返回严格 JSON，并在本地校验 `type_judgment`、`instructions`、`count`、`allowed_types`、重复项和 multiview-safe 规则。

**示例：**

```bash
python scripts/run_full_experiment.py --plan plans/experiment/hunyuan_category_eval.yaml
```

**前端配置页面：**

除了手动编写 YAML 文件，你还可以使用 Web 前端的 **Batch Generation** 页面来可视化配置实验计划：

1. 访问 `/batch-generation` 页面
2. 填写基本设置（计划名称、provider、edit mode 等）
3. 添加一个或多个 category 配置
4. 先设置 `Category Count`
   - 该值会按 `categorized_objects.json` 中的 category 总数自动限制最大值
5. 选择“固定 category 模板”或“随机挑选 N 个不同 category”
6. 填写 `object_count`、`Edit Count` 与 `Allowed Types`
7. 固定 category 模式下，必须把每个 category 模板都配置完成才能生成 YAML
8. 点击 "Generate YAML" 生成计划文件
9. 点击 "Copy CLI" 复制 CLI 命令
10. 前端生成的 CLI 会自动用 `nohup` 在后台启动实验，先 `mkdir -p` + `touch` 预创建日志文件，再在前台执行 `tail --retry -f` 跟日志；即使 SSH 断开，后台实验也会继续运行。日志文件名格式为：`时间 + yaml名称 + category数量 + 各category的object_count`
   运行启动后，日志开头还会写入本次 Python 进程的 PID，方便后续手动中断
11. 页面默认在 CLI 中追加 `--gpu-id 0`；复制命令前请先用 `nvidia-smi` 确认目标机器的 GPU 编号，如果不是 `0`，需要手动改掉再执行

生成的计划文件会保存到 `/seaweedfs/xiaoliang/data/2d3d_data/experiment_plans/` 目录下，CLI 命令可以直接在终端执行。实验运行日志会保存到项目根目录的 `logs/`，例如：`logs/20260317_235236_hunyuan-furniture-eval_categories-3_objects-2-5-1.log`。实验过程中的明细统计会保存到 `workspace.pipeline_dir/experiments/<experiment_id>/`，例如服务器上的 `/seaweedfs/xiaoliang/data/2d3d_data/pipeline/experiments/<experiment_id>/`。

**中断恢复：**

- `execution_plan.json` 会在实验启动初期就落盘，冻结 random category / random object 的具体执行结果，后续恢复会严格基于这份执行计划继续。
- 如果实验中断，可以先执行 repair 命令重建当前统计文件：

```bash
python scripts/run_full_experiment.py --repair-experiment-id <experiment_id>
```

- 如果要在同一个 `experiment_id` 下继续补跑剩余对象与 edit 任务，可以执行：

```bash
python scripts/run_full_experiment.py --resume-experiment-id <experiment_id>
```

**Batch Generation 运行实例管理：**

- `Experiment Runs` 区块专门展示已经启动过的实验实例，而不是 YAML 模板。
- `Resume CLI`：继续同一个 `experiment_id` 下尚未完成的 object/edit 槽位。
- `Repair CLI`：基于当前已落盘产物重建 `object_records` / `edit_records` / `summary` 等记录文件。
- 页面现在支持：查看 run 状态、复制 `Resume CLI` / `Repair CLI`、以及一键触发 resume / repair。

**Batch Generation 历史 YAML 复用：**

- `Existing YAML Plans` 会按 YAML 文件生成时间倒序列出 `/seaweedfs/xiaoliang/data/2d3d_data/experiment_plans/` 下已经生成过的 YAML。
- `Existing YAML Plans` 现在放在 Batch Generation 页面顶部，适合作为第一步入口，先选历史模板，再决定是否修改。
- 选中某个历史 YAML 后，页面会自动把该 YAML 的模板值回填到当前 Batch Generation 表单中，便于在原配置基础上直接修改。
- 历史 YAML 若仍使用旧的 `instruction_counts`，前端会通过显式归一化入口转换成新的 `instruction_plan` 进行展示；新保存的 YAML 始终写回新 schema。
- 加载历史 YAML 时，如果旧文件里存在 `random.category=true` 但仍残留了 `objects` / `category_name`，回填前会自动忽略这些旧字段。
- 历史 YAML 加载后，右侧会同步显示该 YAML 的原始内容，以及基于当前时间重新生成的 CLI / log 路径预览。
- `CLI Command` 不会在仅加载历史 YAML 时提前显示；只有重新点击 `Generate YAML` 之后，才会显示本次新 YAML 对应的 CLI。
- `Execute Selected YAML` 会直接在后端异步执行当前选中的 YAML，并返回 `task_id`；运行状态可在 `Tasks` 页面继续追踪。

**统计查看与恢复说明：**

- `Experiment Stats` 页面支持两种查看方式：
  - `Provider Summary`：按 `source provider + target provider` 聚合 category 指标
  - `YAML Results`：按 YAML 查看 run 列表、category summary、object summary 与 edit records
- 如果某次实验已经启动，但尚未产出最终 `object_records.jsonl` / `edit_records.jsonl`，统计接口会自动尝试基于 partial records 恢复结果，并在页面文案中明确标记为 recovered partial runs。
