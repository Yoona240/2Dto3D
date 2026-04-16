# 2D 到 3D 数据集流水线（v2）

本项目是一个**自动化流水线系统**，用于生成高质量的 2D–3D 配对数据集。它对从**文本生成图像**、**图像生成 3D 模型**到**自动化多视角渲染**的全过程进行了系统化整合与简化。

## 🚀 核心特性

* **批量处理**：支持大规模自动化生成 caption、图像和 3D 模型的工作流。
* **生成流程**：
1. 最关键的步骤：生成多样化的 文生图 prompt，即先生成高质量的图像caption，这是生成优质图像和3D模型的基础。
  2. **T2I（Text-to-Image）**：使用先进的 LLM / 图像生成模型（如 Flux、Gemini 等）生成高质量参考图像。
  3. **Gen3D（Image-to-3D）**：基于参考图像，通过当前主流的 3D 生成服务（Tripo、Hunyuan、Rodin）生成 3D 资产。
* **多视角渲染**：支持 Blender/WebGL 双后端渲染 6 个标准视角（前、后、左、右、上、下），并支持语义定向对齐（VLM 判定 + GLB 旋转 + 重渲染）。
* **编辑指令生成**：利用多模态大模型（MLLM）自动生成用于 3D 几何编辑的文本指令。
* **管理 UI**：基于 Flask 的现代化 Web 界面，用于管理 prompts、查看生成资产以及监控异步任务；支持 Edited/Failed Editing 分区、误杀恢复（Restore）与最新编辑结果优先展示（按生成时间降序）。
* **实验计划与执行**：支持通过 `run_full_experiment.py` 执行 `instruction_plan` 自适应 schema 的端到端实验；Web 端 **Batch Generation** 页面可生成 YAML、回填历史 YAML，并直接异步执行选中的实验计划。
* **实验统计与追踪**：新增 **Experiment Stats** 页面，可按 Provider Pair 或 YAML 维度查看 category/object/edit 统计，并查看各阶段的执行次数、总耗时、平均耗时；对于缺少最终记录文件的中断实验，页面会自动基于 partial records 恢复可见统计结果。
* **模型筛选增强**：**Models** 页面支持 `Provider Pair` / `YAML` 两种实验过滤模式，并可继续叠加 `status`、`category` 与左侧搜索，便于快速定位某条实验链路生成的模型。
* **模型对导出**：**Pairs** 页面支持按 YAML 实验计划多选过滤、LPIPS 阈值筛选，并一键生成可在服务器执行的 CLI 导出命令，产出标准 `manifest.json`；路径前缀可配置，支持新旧服务器迁移。
## 📍 环境信息

| 项目 | 值 |
|------|-----|
| **环境位置** | `/home/xiaoliang/local_envs/2d3d` |
| **Python 版本** | 3.11.14 |
| **infinigen 版本** | 1.19.0 (可编辑模式) |
| **包管理器** | uv (位于 `~/.local/bin/uv`) |

## 📂 项目结构

本项目采用扁平化、独立运行的架构（由 v1 迁移而来）：

```
2d3d_v2/
├── app.py                  # Web UI 主入口
├── config/                 # 配置文件
│   └── config.yaml         # API Key 与模型配置
├── core/                   # 核心业务逻辑
│   ├── gen3d/              # 3D 生成客户端（Tripo、Hunyuan、Rodin）
│   ├── image/              # 文生图生成与 Caption 模块
│   └── render/             # Blender 脚本与渲染逻辑
├── scripts/                # 独立 CLI 脚本
│   ├── batch_process.py    # ⭐ 统一批量处理脚本（gen3d/render/edit）
│   ├── gen3d.py            # 单个 3D 生成
│   ├── generate_prompts.py # 批量生成 T2I prompts
│   ├── render_views.py     # 单个渲染
│   └── run_render_batch.py # 批量渲染（底层）
├── utils/                  # 通用工具模块
│   ├── blender.py          # Blender 路径自动发现
│   ├── config.py           # 配置加载器
│   ├── llm_client.py       # 统一的文本 LLM 客户端
│   ├── image_api_client.py # 统一的图像 API 客户端
│   └── prompts.py          # 集中管理的 Prompt 模板
├── templates/              # HTML 模板
├── static/                 # CSS / JS 静态资源
└── data/                   # 数据存储目录（已加入 .gitignore）
    └── pipeline/
        ├── prompts/        # 批量 prompt 文件（.jsonl）
        ├── images/         # 生成的参考图像
        │   └── {id}/
        │       ├── image.png
        │       ├── meta.json
        │       └── instructions.json  # 编辑指令列表
        ├── models_src/     # 生成的 3D 模型（.glb）
        │   └── {id}/
        │       ├── model_hy3.glb
        │       ├── model_hy3_aligned.glb  # 语义对齐后（可选）
        │       └── meta.json
        └── triplets/       # 渲染视图及编辑结果
            └── {id}/
                ├── views/
                │   └── {provider_id}/
                │       ├── front.png
                │       ├── back.png
                │       ├── ...
                │       └── _semantic_tmp/   # 语义对齐调试资产（可选保留）
                └── edited/         # 编辑后的视图
                    └── {edit_id}/
                        ├── front.png
                        ├── back.png
                        ├── right.png
                        └── meta.json
```

## ⚙️ 配置说明

所有配置均集中在 `config/config.yaml` 中管理，可在此设置 API Key、默认模型及超时参数。

**关键配置模块包括：**

* **`oneapi.text_models` / `oneapi.image_models` / `oneapi.gen3d_models`**：统一网关下的模型配置。
* **`tasks`**：任务到 provider/model 的映射（如 `tasks.gen3d`、`tasks.render`、`tasks.guided_edit`）。
* **`tripo` / `rodin`**：外部 3D API 的直连配置。
* **`defaults`**：全局超时与轮询参数设置。
* **`render.semantic_alignment`**：语义定向开关、VLM 模型、置信度阈值、二次验证与 debug 资产保留策略（front-only 判定；最终渲染覆盖 `views/{provider_id}/` 同名文件）。

## 🛠️ 环境要求

* **Python 3.9+**
* **Blender 4.0+**（用于渲染，必需）

  * 系统会自动检测 Blender 的安装路径
  * 若未检测到，可通过设置环境变量 `BLENDER_PATH` 手动指定

## ▶️ 使用方式

### Web UI

1. **启动 Web UI**：

   ```bash
   python app.py
   ```

   在浏览器中访问：`http://127.0.0.1:10002`

2. **常用页面入口**：

   - `Batch Generation`：生成 `run_full_experiment.py` 的实验 YAML、回填历史 YAML、复制 CLI，并管理 experiment runs（execute / resume / repair）。页面生成的 CLI 会预创建日志文件，并使用 `tail --retry -f` 跟日志；当前默认附带 `--gpu-id 0`，执行前请先自己确认服务器上的 GPU 编号并按需手动修改。
   - `Experiment Stats`：按 `source provider + target provider` 或按 YAML 查看聚合统计；YAML 视图额外支持查看 stage timing summary（执行次数 / 总耗时 / 平均耗时）；若某次实验中断但缺少最终 `object_records/edit_records`，页面会尝试自动恢复并标记为 partial。
   - `Models`：支持按实验来源过滤 source model，可切换 `Provider Pair` 或 `YAML` 过滤模式，并与 `status` / `category` / 左侧搜索联动。
   - `Model Detail`：`Edited Versions` 支持 `Update LPIPS` 与 `Generate Missing Masks` 两个后端维护入口；点击后会弹出可复制的 CLI 命令窗口，便于手动到服务器执行按模型的 Stage-2 重算或缺失 mask 补算。

### 命令行工具

所有前端功能都可以通过命令行脚本独立执行：

#### 单个资产处理

1. **生成 3D 模型**：

   ```bash
   # 从 pipeline 图片生成
   python scripts/gen3d.py <image_id>
   python scripts/gen3d.py <image_id> --provider hunyuan
   
   # 从编辑后的视图生成（格式：{model_id}_edit_{edit_id}）
   python scripts/gen3d.py ff9912a426cb_edit_27597f6a --provider hunyuan
   
   # 从任意图片生成
   python scripts/gen3d.py --image path/to/image.png --output output_dir
   ```

2. **渲染多视角图像**：

   ```bash
   # 渲染 pipeline 中的模型
   python scripts/render_views.py <model_id>
   
   # 渲染任意 GLB 文件
   python scripts/render_views.py --glb path/to/model.glb --output output_dir
   ```

#### 批量处理（推荐）

使用统一的 `scripts/batch_process.py` 脚本进行批量操作，支持并发控制和进度追踪：

1. **批量生成 3D 模型**：

   ```bash
   # 为所有没有 3D 的图片生成模型
   python scripts/batch_process.py gen3d --provider hunyuan
   
   # 为指定图片生成
   python scripts/batch_process.py gen3d --provider hunyuan --ids id1 id2 id3
   
   # 从编辑后的视图生成 Target 3D
   python scripts/batch_process.py gen3d --provider hunyuan --ids abc123_edit_xyz789 def456_edit_uvw123
   
   # 强制重新生成（即使已存在）
   python scripts/batch_process.py gen3d --provider hunyuan --force
   
   # 预览模式（不实际执行）
   python scripts/batch_process.py gen3d --provider hunyuan --dry-run
   ```

2. **批量渲染视图**：

   ```bash
   # 为所有没有视图的模型渲染
   python scripts/batch_process.py render
   
   # 为指定模型渲染
   python scripts/batch_process.py render --ids id1 id2 id3
   
   # 强制重新渲染
   python scripts/batch_process.py render --force
   
   # 预览模式
   python scripts/batch_process.py render --dry-run
   ```

   语义定向启用后，`render` 实际流程为：首轮渲染 -> VLM 判定 -> 生成 `model_{provider_id}_aligned.glb` -> 重渲染最终视图。

3. **批量编辑视图**（使用每个模型自己的指令）：

   ```bash
   # 使用第 1 条指令（Remove）编辑所有有视图和指令的模型
   # 自动跳过已使用相同指令编辑过的模型（断点续执行）
   python scripts/batch_process.py edit

   # 编辑指定模型
   python scripts/batch_process.py edit --ids id1 id2 id3

   # 使用第 2 条指令（Replace）
   python scripts/batch_process.py edit --instr-index 1

   # 使用所有指令（每条指令分别创建一批编辑）
   python scripts/batch_process.py edit --all-instructions

   # 使用所有指令，但每种类型(remove/replace)只处理1条
   python scripts/batch_process.py edit --all-instructions --max-per-type 1

   # 只编辑特定视图
   python scripts/batch_process.py edit --views front right

   # 使用多视角拼接模式（推荐用于需要一致性编辑的场景）
   python scripts/batch_process.py edit --mode multiview

   # 强制重新编辑（即使相同指令已编辑过）
   python scripts/batch_process.py edit --force

   # 预览模式
   python scripts/batch_process.py edit --dry-run
   ```

4. **批量从编辑视图生成 Target 3D**（新功能）：

   ```bash
   # 为所有编辑批次生成 Target 3D（自动跳过已有）
   python scripts/batch_process.py gen3d-from-edits --provider hunyuan

   # 只处理特定源模型的编辑
   python scripts/batch_process.py gen3d-from-edits --provider hunyuan --ids model1 model2

   # 每个模型只处理1个编辑批次
   python scripts/batch_process.py gen3d-from-edits --provider hunyuan --max-per-model 1

   # 强制重新生成
   python scripts/batch_process.py gen3d-from-edits --provider hunyuan --force

   # 预览模式
   python scripts/batch_process.py gen3d-from-edits --provider hunyuan --dry-run
   ```

5. **重检已有编辑结果质量**（Edit QC）：

   检查方法由 `edit_quality_check.method` 配置决定：`grid_vlm`（6-view 拼图 VLM）或 `two_stage_recon`（VLM diff + LLM judge 两阶段）。
   当 `method=two_stage_recon` 时，Stage1 视角策略由 `edit_quality_check.two_stage_recon.edit_view_policy` 控制：
   - `front_only`：仅检查 front
   - `all_6`：逐视角检查 6 张图
   - `stitched_6`：先拼接 before/after 六视角 3x2，再执行一次两阶段判断

   ```bash
   # 重检某个模型的所有 edit 批次
   python scripts/batch_process.py check-edit-quality --ids 22af6bb4d520

   # 只重检某一条 edit 批次
   python scripts/batch_process.py check-edit-quality --ids 22af6bb4d520 --edit-id 3def0294

   # 预览模式
   python scripts/batch_process.py check-edit-quality --ids 22af6bb4d520 --dry-run
   ```

6. **执行 Stage2 Target 3D 一致性检测**（Method-2 / LPIPS）：

   ```bash
   # 推荐：纯检测模式，不触发新渲染/新模型副作用
   python scripts/batch_process.py check-target-consistency --provider hunyuan \
     --ids c51228e8ae96 --edit-id 81dfb743 --skip-render

   # 若确实要先重渲染 target 视图再检测
   python scripts/batch_process.py check-target-consistency --provider hunyuan \
     --ids c51228e8ae96 --edit-id 81dfb743 --force-render
   ```

   说明：
   - 结果写入 `models_src/{model_id}_edit_{edit_id}/meta.json`
   - 同时维护 `target_quality_check`（兼容）和 `target_quality_checks_by_provider`（多 provider）
   - 模型详情页会按 provider 展示 Stage2 状态/score/threshold
   - 当前命令允许在 `edit_quality_check.method = "two_stage_recon"` 或 `"unified_judge"` 下运行
   - Stage2 仍然共享 `edit_quality_check.two_stage_recon.*` 中的 LPIPS 配置

7. **仅补齐历史编辑缺失的 Mask 资产**（Fail Loudly）：

   ```bash
   # 为指定 source model 扫描并补齐缺失 mask
   python scripts/batch_process.py materialize-edit-artifacts --ids 0704f5e8bc6b

   # 仅补一个 edit batch
   python scripts/batch_process.py materialize-edit-artifacts --ids 0704f5e8bc6b --edit-id bc3f7932

   # 强制重算已有 mask（用于更新旧的过敏感结果）
   python scripts/batch_process.py materialize-edit-artifacts --ids 0704f5e8bc6b --force

   # 预览模式
   python scripts/batch_process.py materialize-edit-artifacts --ids 0704f5e8bc6b --dry-run
   ```

   说明：
   - 只补 `*_mask.png` 与 `edit_mask_grid.png`
   - 若 `before_image_grid.png` / `target_image_grid.png` 缺失，系统会使用当前统一的 `ViewStitcher` 流程自动重建后再补 mask
   - 若 source views 本身不完整，或重建所需的输入资产缺失，任务仍会直接报错（Fail Loudly）
   - CLI 并发度由 `config.yaml` 中的 `concurrency.mask_backfill` 控制
   - mask 生成当前使用 `RGB max-abs diff + threshold + morphological opening`；阈值与后处理核大小由 `edit_artifacts.diff_threshold` / `edit_artifacts.opening_kernel_size` 控制

8. **批量刷新 LPIPS Stage-2**（全量入口）：

   ```bash
   # 全量刷新所有可计算目标
   python scripts/batch_process.py refresh-all-lpips

   # 只刷新指定 source model
   python scripts/batch_process.py refresh-all-lpips --ids 137cd60ec929

   # 预览模式
   python scripts/batch_process.py refresh-all-lpips --dry-run
   ```

   说明：
   - 默认保守并发，适配远端文件系统 I/O 抖动场景
   - Web API 在已有 LPIPS 刷新任务运行时返回 `409`，避免重复并发提交

**并发控制**：并发数从 `config/config.yaml` 的 `concurrency` 部分读取：

```yaml
concurrency:
  gen3d:
    hunyuan: 10
    tripo: 5
    rodin: 3
  render: 1  # Blender 渲染建议单线程
  recon_quality_check: 1   # LPIPS Stage-2 建议串行
  refresh_all_dreamsim: 1  # 全量 LPIPS 刷新建议串行
  mask_backfill: 1         # 历史 mask 补算建议串行
```

**断点续执行**：所有批量命令默认支持断点续执行：

| 命令 | 检测规则 | 跳过条件 |
|------|----------|----------|
| `gen3d` | `models_src/{id}/*.glb` 存在 | 已有 3D 模型 |
| `render` | `triplets/{id}/views/*.png` 非空 | 已有渲染视图 |
| `edit` | `triplets/{id}/edited/*/meta.json` 中指令匹配 | 同一指令已编辑 |
| `gen3d-from-edits` | `models_src/{model_id}_edit_{edit_id}/*.glb` 存在 | 已有 Target 3D |
| `check-edit-quality` | `triplets/{model_id}/edited/{edit_id}/meta.json` 存在 | 不适用（每次均执行重检） |
| `check-target-consistency` | `models_src/{model_id}_edit_{edit_id}/meta.json` 存在 | 不适用（每次均执行重检） |
| `materialize-edit-artifacts` | `triplets/{model_id}/edited/{edit_id}/edit_mask_grid.png` 存在 | 已有完整 mask 资产 |
| `refresh-all-lpips` | `models_src/{model_id}_edit_{edit_id}/model_*.glb` 存在 | 仅处理已有 target 3D 的 provider |

使用 `--force` 参数可强制重新执行，使用 `--dry-run` 预览待处理项目。

### 渲染观感经验（重要）

- 若出现“只有 `top` 视角爆白/像蒙了一层雾，但其他视角正常”，通常是方向光 + 高反射材质导致顶视角高光压满。
- 数据集渲染（稳定/一致性优先）推荐使用 `render.lighting_mode: "ambient"`。
- 详细记录与排查步骤见 [docs/guide/batch-render.md](docs/guide/batch-render.md)。
```

### 多视角编辑模式（Multiview Edit）

v2.2.0 新增的编辑模式，专为保证多视角一致性设计：

**原理**：
1. 将 6 个渲染视角拼接为 3×2 网格图
2. 自动填充为正方形（解决 Gemini 强制 1:1 输出问题）
3. 在网格外部添加视角标签（白色边框 + 浅灰标签）
4. 调用 Gemini 一次性编辑整个拼接图
5. 等比缩放 + 智能裁剪回 6 个独立视角

**v2.3.0 改进**：
- 正方形填充避免 Gemini 裁剪导致的视角丢失
- 等比缩放保证裁剪无损（1604×1604 → 1024×1024 → 1604×1604）
- 支持 Gemini 3 Pro Image Preview 模型

**使用场景**：
- 需要对所有视角应用一致编辑的情况
- 减少 API 调用次数（1 次 vs 3-6 次）
- 避免各视角编辑结果不一致

**Web UI 使用**：
1. 进入 Model Detail 页面
2. 点击 "+ New Edit" 按钮
3. 选择 "🔲 Multiview" 模式
4. 输入编辑指令并确认

**CLI 使用**：
```bash
# 对指定模型使用多视角模式编辑
python scripts/batch_process.py edit --ids model_id --mode multiview

# 使用 Gemini 3 Pro 模型
python scripts/batch_process.py edit --ids model_id --mode multiview --model gemini-3-pro-image-preview
```

**调试脚本**：
```bash
# 模拟测试（不调用 API）
python tests/test_multiview_stitch.py data/pipeline/triplets/{id}/views ./test_output

# 真实 Gemini 测试
python tests/test_multiview_stitch.py data/pipeline/triplets/{id}/views ./test_output \
  --gemini "Remove the handle from the object." \
  --model gemini-3-pro-image-preview
```

4. **批量生成 Prompt**：

   ```bash
   python scripts/generate_prompts.py --category vehicle --count 10
   ```

   说明：
   - 默认写入 `config.workspace.pipeline_dir/prompts`（与 Web Prompts 页面一致）
   - 也可通过 `--output` 指定自定义输出文件
   - Web 端 `Prompts -> + Gen T2I Prompts` 支持 `Show CMD`，可直接复制同类命令到服务器执行

#### 编辑指令工作流

1. 在 Web UI 的 **Images** 页面点击 "Gen Instr (R+R)" 为图片生成编辑指令
2. 指令会保存为 `images/{id}/instructions.json`，包含 1 条 Remove + 1 条 Replace 指令
3. 在 **Models** 页面选择模型后：
   - **Batch Edit**: 使用各模型自己的指令编辑 front/back/right 视图
   - **Batch Gen 3D**: 从编辑后的视图生成新的 3D 模型
   - **Experiment Filter**: 可切换 `Provider Pair` 或 `YAML` 模式，仅查看某条实验链路或某个实验计划产生的模型

#### ID 格式说明

| 类型 | 格式 | 示例 | 说明 |
|------|------|------|------|
| 源图片 | `{image_id}` | `abc123def456` | 12 位十六进制 ID |
| 源 3D 模型 | `{model_id}` | `abc123def456` | 与源图片 ID 相同 |
| 编辑后视图 | `{model_id}_edit_{edit_id}` | `abc123_edit_xyz789` | 用于生成 Target 3D |

## 🔗 支持的 API / 服务

* **Tripo AI**（3D）
* **Hunyuan 3D**（腾讯）
* **Rodin**（HyperHuman）
* **SVD / Stable Video**（通过 OpenRouter / NewAPI）
* **Gemini / GPT-4o**（通过专用接口）

## 📚 技术文档

详细的技术实现文档位于 `docs/` 目录：

- **[CLI 命令行指南](docs/guide/cli.md)** - `batch_process.py` 与 `run_full_experiment.py` 命令参考、计划 YAML 结构、Batch Generation 联动与断点恢复说明
- **[批量渲染指南](docs/guide/batch-render.md)** - bpy 模块集成、subprocess 隔离、GPU 渲染优化
- **[HTTP 客户端指南](docs/guide/http-client.md)** - API 调用规范
- **[快速参考](docs/reference/QUICK_REFERENCE.md)** - Models / Experiment Stats 页面过滤条件、关键 API 端点与前端组件速查
- **[分类体系设计](docs/architecture/taxonomy-design.md)** - 数据分类方案
- **[风格多样性设计](docs/architecture/style-diversity.md)** - 确保生成多样性
- **[文档索引](docs/INDEX.md)** - 完整文档导航

---

# 项目目标与设计原则

## 核心目标

本项目旨在构建一个**高度自动化、可扩展的科研级数据生成流水线**，用于生产高质量的 **[源3D模型] <-> [编辑指令] <-> [目标3D模型]** 三元组数据。核心目的是解决当前 3D 编辑任务中缺乏高质量、多视角一致、语义明确的配对数据的问题。

### 数据流程（5个阶段）

1.  **Prompt & T2I**：利用 LLM 生成多样化的结构描述 Prompt，调用 T2I 模型生成参考图
2.  **Source Gen3D**：对接 SOTA 3D 生成 API（Hunyuan, Tripo, Rodin）生成源 3D 模型
3.  **Multiview Rendering**：Blender 后台无头渲染 6 个标准视角（Front, Back, Left, Right, Top, Bottom）
4.  **Instruction Generation & Editing**：MLLM 生成 `Remove <part>` 和 `Replace <part>` 指令，通过 Multiview Stitching 编辑多视角图
5.  **Target Gen3D**：从编辑后的视图生成目标 3D 模型

### 科研导向原则

*   **Fail Loudly (显性报错)**：系统**严禁**掩盖错误，运行时若遇到 API 失败、资源缺失、格式错误等，必须直接抛出异常或标记任务为 `Failed`，保留完整的 Error Stack。
*   **可追溯性 (Traceability)**：每个生成资产都必须伴随不可变的元数据文件 (`meta.json`)，记录 Source ID, Prompt, Provider Model Version 等关键信息。
*   **结构化数据优先**：数据存储严格遵循 `data/pipeline/{type}/{id}` 的层级结构，避免对文件名强依赖。

---

## 数据类型与编辑策略

### 编辑任务类型

*   **局部 Remove**：移除物体的某个部分（如"Remove the wheels"）
*   **局部 Replace**：替换物体的某个部分（如"Replace the wheels with golden ones"）
*   **为什么不包含 Add**：Add 操作很难控制多视角一致性，容易导致生成的 3D 模型出现问题。Remove 可视为 Add 的逆操作。

### 数据类型聚焦

数据类型由原始图像决定，归根到底由文生图 Prompt 决定。

*   **聚焦语义密集的高频细节**：如结构的复杂性（复杂几何结构、多个部件组合）
*   **避免无序纹理复杂性**：如磨损、裂纹、锈迹等（现有 3D 生成模型对此类细节还原能力较弱）
*   **多样化词表**：后续将引入词表确保数据多样性，避免遗漏长尾数据类型
