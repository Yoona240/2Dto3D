# 2D-3D 编辑数据集流水线 - 项目交接文档

> **项目位置**: `/home/xiaoliang/2d3d_v2`  
> **交接日期**: 2026年3月3日  
> **交接人**: [当前负责人]  
> **接收人**: [新负责人]

---

## 📋 目录

1. [Pipeline 说明](#pipeline-说明)
2. [项目规范](#项目规范)
3. [待检查或完成](#待检查或完成)
4. [快速开始](#快速开始)
5. [故障排查](#故障排查)
6. [关键文件索引](#关键文件索引)

---

## Pipeline 说明

### 1. 环境配置

**UV 虚拟环境**

```bash
# 环境位置
/home/xiaoliang/local_envs/2d3d

# 激活方式
source /home/xiaoliang/local_envs/2d3d/bin/activate

# 或直接运行
/home/xiaoliang/local_envs/2d3d/bin/python your_script.py
```

**核心依赖**
- Python 3.11.14
- Flask (Web UI)
- Playwright (WebGL 渲染)
- Pillow (图像处理)
- httpx (HTTP 客户端)

### 2. API 调用架构

**异步调用模式**

所有外部 API 调用均采用异步方式，分为两类：

| API 类型 | 代表服务 | 调用方式 | 特点 |
|---------|---------|---------|------|
| **普通 API** | GPT-5, Gemini-3-Flash | 同步 HTTP + 异步任务队列 | 快速响应 |
| **图生 3D API** | Tripo, Hunyuan, Rodin | 异步提交 + 轮询 | 耗时 5-30 分钟 |

**并发控制机制**

```yaml
# config.yaml - 并发限制配置
concurrency:
  gen3d:
    hunyuan: 5     # Hunyuan 严格限流
    tripo: 5       # Tripo 保守限制（API 昂贵！）
    rodin: 3
  render: 1        # Blender/WebGL 建议单线程
  image: 10        # 图像生成并发
```

**关键设计**：
- 生成环节（gen3d/image）有最大并行数限制，通过 `ThreadPoolExecutor` 实现
- 下载环节无限制，API 返回后直接下载，无需额外等待
- 任务状态持久化到 `workspace/tasks.jsonl`，支持断点续执行

### 3. Tripo API 注意事项 ⚠️

**昂贵警告**：Tripo API 调用成本较高，注意以下事项：

1. **严格限制并发**：默认最多 5 个并行任务
2. **启用视角选择**：`config.yaml` 中 `tripo.enable_view_selection: true`
3. **固定种子**：使用 `model_seed` 和 `texture_seed` 确保可复现性
4. **生产环境建议**：
   - 确认无误后再用 Tripo 生成最终质量模型
   - 使用 `--dry-run` 预览任务数量

### 4. 数据生成流程

**完整 Pipeline 数据流**：

```
┌─────────────────────────────────────────────────────────────────┐
│  Phase 1: Prompt & T2I                                          │
│  ├─ 分类体系 + 风格体系 → 结构化 Prompt                         │
│  └─ T2I 模型 (Gemini/Doubao) → image.png                        │
├─────────────────────────────────────────────────────────────────┤
│  Phase 2: Source Gen3D                                          │
│  └─ Image-to-3D API (Tripo/Hunyuan/Rodin) → model.glb           │
├─────────────────────────────────────────────────────────────────┤
│  Phase 3: Multiview Rendering                                   │
│  ├─ WebGL/Blender 渲染 → 6 视角 (front/back/left/right/top/bottom)│
│  └─ 视角拼接 → 3×2 网格图                                        │
├─────────────────────────────────────────────────────────────────┤
│  Phase 4: Instruction Generation & Editing                      │
│  ├─ MLLM 生成编辑指令 (Remove/Replace)                          │
│  └─ 多视角编辑 → 编辑后视图                                      │
├─────────────────────────────────────────────────────────────────┤
│  Phase 5: Target Gen3D                                          │
│  ├─ 视角选择：6 视角 → 4 视角 (Tripo 限制)                       │
│  └─ 编辑后视图 → Target 3D 模型                                  │
└─────────────────────────────────────────────────────────────────┘
```

**详细步骤说明**：

#### 4.1 文生图 Prompt 生成

- **输入**：分类体系（taxonomy）+ 风格体系（style diversity）
- **约束**：视角、背景、光照

#### 4.2 图像生成 (T2I)


#### 4.3 图生 3D (Gen3D)



#### 4.4 多视角渲染

**WebGL 渲染**：

- 使用 headless Chrome + Google Model Viewer
- 6 个标准视角：front, back, left, right, top, bottom

**渲染配置**：
```yaml
render:
  backend: "webgl"  # 或 "blender",blender的光照不好控制
  image_size: 512
  webgl:
    environment_image: "neutral"
    shadow_intensity: 0.0
```

#### 4.5 图像编辑

**编辑指令生成**：
```bash
# 为图像生成 Remove + Replace 指令
python scripts/generate_prompts.py --gen-instructions --ids <image_id>
```

**多视角编辑**：
```bash
# 单视角模式（逐个编辑）
python scripts/batch_process.py edit --ids <model_id>

# 多视角拼接模式（推荐，一致性更好）
python scripts/batch_process.py edit --ids <model_id> --mode multiview
```

- 将 6 视角拼接为 3×2 网格
- 调用 Gemini 一次性编辑整个拼接图
- 等比缩放 + 智能裁剪回 6 个视角

#### 4.6 Target Gen3D（视角选择关键逻辑）

**核心问题**：Tripo 只能接受 4 个视角（front/back/left/right），但渲染产生了 6 个视角

**视角选择策略**：

```python
# scripts/gen3d.py - 视角选择逻辑

1. 计算各视角的信息熵（edge detection score）
2. 比较 top vs front 得分差异
3. 如果 top_score - front_score > threshold (1.0):
   - 调用 VLM (gemini-3-flash-preview) 判断 "top 是否可视为正面"
4. 根据判断结果选择映射方案:
   - Case 1: 标准映射 (front/back/left/right)
   - Case 2: Top/Bottom 作为 front/back (适合顶部特征明显的物体，如小提琴)
```

**配置**：
```yaml
tripo:
  enable_view_selection: true
  entropy_diff_threshold: 1.0
  view_selection_vlm_model: "gemini-3-flash-preview"
```

**调试工具**：
```bash
# 测试视角选择逻辑
python tests/test_tripo_view_selection.py <model_id>

# 输出可视化结果到 test_output/tripo_view_selection/
```

### 5. 前端系统(不是pipeline的一部分，主要用于查看和调试pipeline的结果)

**技术栈**：
- 后端：Flask (app.py)
- 前端： vanilla JS + Bootstrap
- 3D 查看：Google Model Viewer (CDN)

**主要页面**：

| 页面 | 路由 | 功能 |
|-----|------|------|
| **Models** | `/models` | 3D 模型管理、过滤、批操作 |
| **Pairs** | `/pairs` | 源-目标 3D 对比查看 |
| **Images** | `/images` | 图像管理、编辑指令生成 |
| **Prompts** | `/prompts` | Prompt 管理 |
| **Tasks** | `/tasks` | 异步任务监控 |

**关键功能**：
- **过滤系统**：8 种过滤条件（包括 `edits-no-target`：有编辑但无 Target 3D）
- **懒加载**：3D 模型点击后加载，避免页面卡顿
- **批量操作**：全选、批量渲染、批量编辑、批量生成 3D

**启动前端**：
```bash
python app.py
# 访问 http://127.0.0.1:10002
```

---

## 项目规范

### 1. Agent 规则（AGENTS.md）

**Fail Loudly 原则** ⚠️（最高优先级）

```python
# ❌ 错误：静默使用默认值
samples = render_data.get('samples', 64)

# ✅ 正确：配置缺失时立即报错
samples = _require_key(render_data, 'samples', 'render')
```

**单一配置原则**
- 所有参数必须在 `config.yaml` 中明确定义
- 代码中**禁止**硬编码默认值
- 配置即契约：缺失配置 → 立即失败

**文档同步规则**
修改 AGENTS.md 时必须同步更新：
- [ ] CHANGELOG.md（详细描述变更）
- [ ] docs/guide/cli.md（参数、示例更新）
- [ ] docs/guide/batch-render.md（如渲染相关变更）
- [ ] README.md（如功能有重大变化）

### 2. 项目结构规范

**分层架构**：

```
2d3d_v2/
├── app.py                  # Flask 主入口（路由、任务管理）
├── config/                 # 配置管理
│   ├── config.yaml         # 所有配置（API Key、模型、并发）
│   └── config.py           # 配置解析器
├── core/                   # 核心业务逻辑
│   ├── gen3d/              # 3D 生成（Tripo/Hunyuan/Rodin）
│   ├── image/              # 图像生成/编辑
│   └── render/             # 渲染（Blender/WebGL）
├── utils/                  # 通用工具
│   ├── llm_client.py       # 统一 LLM 客户端
│   ├── image_api_client.py # 统一图像 API 客户端
│   └── prompts.py          # 集中 Prompt 模板
├── scripts/                # CLI 脚本
│   └── batch_process.py    # 统一批量处理入口
├── templates/              # HTML 模板
├── static/                 # CSS/JS 静态资源
└── data/pipeline/          # 数据存储（gitignore）
    ├── images/             # 参考图像
    ├── models_src/         # 3D 模型
    └── triplets/           # 渲染视图 + 编辑结果
```

**配置管理**：
- 单一定义源：`config.yaml`
- 环境特定配置通过环境变量注入（如 API Key）
- 工作区路径可配置：`workspace.pipeline_dir`

### 3. 避免冗余代码

**统一客户端层**：

```python
# ❌ 错误：每个模块重复实现 HTTP 请求
# generator.py 和 editor.py 各自调用 requests.post()

# ✅ 正确：统一客户端，业务模块只做薄包装
from utils.image_api_client import ImageApiClient

class T2IGenerator:
    def __init__(self, config):
        self.client = ImageApiClient(config)  # 统一客户端
```

**Prompt 模板集中管理**：

```python
from utils.prompts import (
    EditType,                    # REMOVE, REPLACE, BOTH
    get_instruction_prompt,      # 单个编辑指令
    get_batch_instruction_prompts,  # 批量指令
    get_optimize_prompt,         # T2I prompt 优化
)
```

### 4. Git 版本管理

**分支策略**：
- `main`：稳定分支，可直接用于生产
- 功能开发：从 main 切出 feature 分支
- 提交规范：`feat(scope): description` / `fix(scope): description`

**GitHub 同步**：
```bash
# 当前仓库已配置 GitHub 远程
git remote -v
# origin  git@github.com:yourusername/2d3d_v2.git (fetch)

# 推送更改
git add .
git commit -m "feat: 你的修改描述"
git push origin main
```

---

## 待检查或完成

### 🔴 高优先级

#### 1. 质量检测链路（当前状态与剩余工作）

**当前已实现**：
- Method-1：`grid_vlm`（编辑前后拼图质检）
- Method-2 Stage1：`two_stage_recon`（VLM diff + LLM judge）
- Method-2 Stage2：`check-target-consistency`（DreamSim target 一致性重检，支持多 provider）

**当前行为**：
- 编辑后质量门禁由 `check-edit-quality`（或编辑流程内）负责。
- Target 3D 一致性由 `check-target-consistency` 执行并回写 `target_quality_check`。
- 前端模型详情页支持按 provider 展示 Stage2 状态，且支持删除单 provider 的 target 模型。

**待完善（后续）**：
- Stage2 全自动触发链路（例如 target 3D 生成成功后自动执行一致性检测）
- 导出/筛选流程中把 Stage2 状态作为可配置 gate 条件

**相关文件**：
- `scripts/batch_process.py`
- `core/render/recon_consistency_checker.py`
- `app.py`
- `templates/edit_batch_card_macro.html`
- `templates/model_detail_scripts.html`

### 🟡 低优先级

#### 2. 视角选择初步实现待验证

**当前状态**：已实现基础逻辑，但未充分测试

```python
# scripts/gen3d.py - 视角选择逻辑
# 流程:
# 1. 计算 front/top 信息熵得分
# 2. diff > threshold (1.0) 时触发 VLM
# 3. VLM 判断 "top 视角是否可视为正面"
# 4. 根据结果选择 Case 1 或 Case 2 映射
```

**待验证场景**：
- [ ] 顶部平坦物体（桌子、椅子）→ 应该 Case 1
- [ ] 顶部特征明显物体（小提琴、锅）→ 应该 Case 2
- [ ] 对称物体（球体、圆柱）→ VLM 判断边界情况

**验证方法**：
```bash
# 运行测试脚本
python tests/test_tripo_view_selection.py <model_id>

# 检查 test_output/tripo_view_selection/ 下的可视化结果
# 对比不同映射方案的生成效果
```

#### 3. 调试架构优化

**当前状态**：以前端为核心，便于实时调试

**问题**：缺乏完整的自动化执行脚本

**建议实现**：
```bash
# 期望的自动化脚本
scripts/
  └── run_full_pipeline.py  # 一键执行完整流程

# 使用示例
python scripts/run_full_pipeline.py \
  --prompts-file prompts.jsonl \
  --num-prompts 100 \
  --gen3d-provider hunyuan \
  --edit-mode multiview \
  --output-dir data/experiment_v1
```

**脚本功能**：
1. 批量生成图像（T2I）
2. 批量生成 3D（Gen3D）
3. 批量渲染多视角
4. 批量生成编辑指令
5. 批量编辑视图
6. 批量生成 Target 3D
7. 生成统计报告

---

## 快速开始

### 环境初始化

```bash
# 1. 进入项目目录
cd /home/xiaoliang/2d3d_v2

# 2. 激活环境
source /home/xiaoliang/local_envs/2d3d/bin/activate

# 3. 安装 Playwright 浏览器（WebGL 渲染需要）
PLAYWRIGHT_BROWSERS_PATH=/home/xiaoliang/2d3d_v2/.playwright-browsers \
  playwright install chromium

# 4. 验证配置
python -c "from utils.config import load_config; print(load_config())"
```

### 完整流程示例

```bash
# 1. 生成 10 个 prompts
python scripts/generate_prompts.py --category furniture --count 10

# 2. 批量生成图像（使用 Hunyuan 便宜模型）
python scripts/batch_process.py t2i --provider oneapi \
  --model gemini-2.5-flash-image --dry-run  # 先预览

# 3. 批量生成 3D 模型（测试用 Hunyuan）
python scripts/batch_process.py gen3d --provider hunyuan

# 4. 批量渲染多视角
python scripts/batch_process.py render

# 5. 生成编辑指令
python scripts/generate_prompts.py --gen-instructions

# 6. 批量编辑视图（多视角模式）
python scripts/batch_process.py edit --mode multiview

# 7. 从编辑结果生成 Target 3D
python scripts/batch_process.py gen3d-from-edits --provider hunyuan

# 8. 查看结果
python app.py  # 打开浏览器访问 http://127.0.0.1:10002/pairs
```

---

## 故障排查

### 常见问题

#### Q1: API 调用失败

```bash
# 检查 API Key
cat config/config.yaml | grep api_key

# 检查网络代理
export https_proxy=http://127.0.0.1:38372

# 测试单个 API
python tests/test_multiview_stitch.py <views_dir> ./test_output \
  --gemini "Remove the handle."
```

#### Q2: WebGL 渲染失败

```bash
# 错误：browserType.launch: Executable doesn't exist
# 解决：安装 Playwright 浏览器
PLAYWRIGHT_BROWSERS_PATH=/home/xiaoliang/2d3d_v2/.playwright-browsers \
  playwright install chromium

# 错误：WebGL context lost
# 解决：切换到 Blender 后端
# 修改 config.yaml: render.backend: "blender"
```

#### Q3: Tripo 视角映射错误

**现象**：生成的 3D 模型方向不对（如顶部变成了正面）

**排查**：
```bash
# 1. 运行视角选择测试
python tests/test_tripo_view_selection.py <model_id>

# 2. 检查 test_output/tripo_view_selection/ 下的可视化
# 3. 调整 config.yaml 中的阈值
# 4. 手动指定映射方案（临时方案）
```

#### Q4: 配置错误导致程序静默失败

**现象**：程序运行但没有输出，或使用了错误的默认值

**解决**：
```python
# 启用 Fail Loudly 检查
# 在 config.yaml 中确保所有必需参数已定义
# 代码中禁止使用 .get(key, default)
```

#### Q5: Stage2 检测报 DreamSim/依赖/下载错误

**常见现象**：
- `ImportError: dreamsim package is required...`
- `ModuleNotFoundError: No module named 'transformers'`
- 首次运行卡在 `Downloading checkpoint` 或网络超时
- `cannot import name 'trunc_normal_' from 'utils'`（DINO hub 导入冲突）

**排查顺序**：
```bash
# 1) 确认运行解释器（必须和安装依赖一致）
/home/xiaoliang/local_envs/2d3d/bin/python -V
/home/xiaoliang/local_envs/2d3d/bin/python -m pip -V

# 2) 检查核心包
/home/xiaoliang/local_envs/2d3d/bin/python -m pip show torch torchvision dreamsim transformers

# 3) 纯检测模式运行 Stage2（不触发新渲染）
/home/xiaoliang/local_envs/2d3d/bin/python scripts/batch_process.py \
  check-target-consistency --provider hunyuan --ids <model_id> --edit-id <edit_id> --skip-render
```

**建议**：
- 国内网络下优先提前准备 dreamsim checkpoint 到 `torch.hub` checkpoints 目录。
- 首次 warmup 依赖下载完成后，后续 Stage2 检测可离线复用缓存。
- 若出现 DINO 的 `utils` 导入冲突，优先检查本地工程根目录同名包对 `torch.hub` 模块解析的污染。

---

## 关键文件索引

### 配置文件

| 文件 | 用途 |
|-----|------|
| `config/config.yaml` | 所有配置（API、模型、并发、渲染） |
| `config/config.py` | 配置解析和数据类定义 |

### 核心逻辑

| 文件 | 用途 |
|-----|------|
| `app.py` | Flask 主应用（路由、任务管理、API） |
| `core/gen3d/tripo.py` | Tripo API 客户端 |
| `core/gen3d/hunyuan.py` | Hunyuan API 客户端 |
| `core/image/multiview_editor.py` | 多视角拼接编辑器 |
| `core/render/webgl_script.py` | WebGL 渲染逻辑 |

### 脚本工具

| 文件 | 用途 |
|-----|------|
| `scripts/batch_process.py` | **统一批量处理入口** |
| `scripts/gen3d.py` | 视角选择逻辑（关键！） |
| `scripts/generate_prompts.py` | Prompt 和指令生成 |
| `tests/test_tripo_view_selection.py` | 视角选择调试工具 |

### 文档

| 文件 | 用途 |
|-----|------|
| `README.md` | 项目主文档 |
| `AGENTS.md` | 开发规范和维护记录 |
| `CHANGELOG.md` | 版本变更记录 |
| `docs/INDEX.md` | 完整文档导航 |
| `docs/guide/cli.md` | CLI 命令参考 |
| `docs/guide/batch-render.md` | 批量渲染指南 |

---

## 附录

### A. 数据目录结构

```
data/pipeline/
├── prompts/
│   └── {category}_{timestamp}.jsonl
├── images/
│   └── {id}/
│       ├── image.png
│       ├── meta.json
│       └── instructions.json
├── models_src/
│   └── {id}/
│       ├── model_hy3.glb
│       ├── model_tp3.glb
│       └── meta.json
└── triplets/
    └── {id}/
        ├── views/
        │   ├── front.png
        │   ├── back.png
        │   ├── left.png
        │   ├── right.png
        │   ├── top.png
        │   └── bottom.png
        └── edited/
            └── {edit_id}/
                ├── front.png
                ├── meta.json
                └── ...
```

### B. ID 命名规范

| 类型 | 格式 | 示例 |
|-----|------|------|
| 源图片 | `{12位十六进制}` | `abc123def456` |
| 源 3D 模型 | 同图片 ID | `abc123def456` |
| 编辑后视图 | `{source_id}_edit_{edit_id}` | `abc123_edit_xyz789` |

### C. 外部资源

- **Tripo API**: https://api.tripo3d.ai/v2/openapi
- **Hunyuan API**: 通过 OneAPI Gateway
- **Model Viewer**: https://modelviewer.dev/
- **UV 文档**: https://docs.astral.sh/uv/

---

**交接完成确认**：

- [ ] 环境配置已验证
- [ ] 配置文件已理解
- [ ] 数据流程已清楚
- [ ] 关键代码已阅读
- [ ] 待办事项已记录

**联系方式**：如有问题，请参考 `AGENTS.md` 中的维护记录或联系前任负责人。
