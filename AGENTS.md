# Agent Documentation & Developer Guide

This document outlines the development standards and documentation system for the `2d3d_v2` project.

## 📖 Documentation System

### 文档组织结构

```
2d3d_v2/
├── README.md                    # 项目主文档（用户入口）
├── AGENTS.md                    # 开发者指南（代码规范、架构）
├── CHANGELOG.md                 # 版本变更记录
│
└── docs/
    ├── INDEX.md                 # 文档索引和导航
    ├── guide/                   # 使用指南
    │   ├── cli.md               # CLI命令行指南
    │   ├── batch-render.md      # 批量渲染指南
    │   ├── http-client.md       # HTTP客户端指南
    │   └── uv-env.md            # UV环境指南
    ├── architecture/            # 架构设计文档
    │   ├── PROJECT_STRUCTURE_OVERVIEW.md
    │   ├── render-module.md
    │   ├── taxonomy-design.md
    │   └── style-diversity.md
    ├── reference/               # 快速参考
    │   └── QUICK_REFERENCE.md
    └── archive/                 # 历史归档
        └── [临时文档、历史记录]
```

### 文档管理规则

1. **根目录文档（永久）**：
   - `README.md`: 用户主要入口，功能介绍和使用指南
   - `AGENTS.md`: 开发者指南，代码规范和架构说明
   - `CHANGELOG.md`: 版本变更记录

2. **子目录分类**：
   - `docs/guide/`: 用户操作指南和教程
   - `docs/architecture/`: 系统架构、设计文档
   - `docs/reference/`: 快速参考手册、速查表
   - `docs/archive/`: 临时文档和历史归档

3. **命名规范**：
   - 根目录文档：大写（README.md, AGENTS.md）
   - 子目录文档：小写，连字符分隔（batch-render.md）

4. **维护原则**：
   - 临时文档及时归档到 archive/
   - 重大变更后在 AGENTS.md 中记录
   - 保持链接有效（移动文档时更新引用）

5. **文档更新强制规则（⚠️ 最高优先级）**：
   - **更新 AGENTS.md 时必须同步检查并更新以下文档**：
     - `CHANGELOG.md` - 版本变更记录（详细描述变更内容）
     - `docs/guide/cli.md` - CLI 命令行指南（参数、示例更新）
     - `docs/guide/batch-render.md` - 批量渲染指南（如渲染相关变更）
     - `README.md` - 项目主文档（如功能有重大变化）
     - 其他相关架构文档（如架构设计变更）
   - **检查清单**：
     - [ ] 是否新增了配置项？→ 更新所有相关文档
     - [ ] 是否修改了命令行参数？→ 更新 cli.md
     - [ ] 是否修复了 Bug？→ 更新 CHANGELOG.md
     - [ ] 是否改变了用户接口？→ 更新 README.md 和 guide/
   - **违规后果**：文档不一致会导致用户困惑，科研可复现性受损

## 💻 Code Standards

清晰简洁，避免冗余
避免错误隐藏或者降级，否则虽然可以暂时运行，但是这对我们的科研是致命的，直接导致系统行为不可控

注意越多和维护项目文档

---

## 🚨 重要原则（Critical Principles）

### 1. Fail Loudly（显性报错）- 最高优先级 ⚠️

**原则定义**：
系统**严禁**为了鲁棒性而掩盖错误（如默认降级、静默重试、返回空值、fallback 到默认值）。

**核心要求**：
1. **配置即契约**：所有参数必须在 `config.yaml` 中明确定义，代码中禁止硬编码默认值
2. **无静默降级**：配置缺失或错误时，必须立即抛出异常并终止执行，不得自动使用其他值替代
3. **单一定义源**：默认值只在 `config.yaml` 中配置，不在代码中使用 `.get(key, default)` 等形式
4. **立即失败原则**：运行时若遇到 API 失败、资源缺失、格式错误等，直接抛出异常或标记任务为 `Failed`，保留完整 Error Stack

**正反示例**：

❌ **错误做法（违反 Fail Loudly）**：
```python
# config.py - 硬编码默认值，配置缺失时静默使用默认值
render = RenderConfig(
    samples=render_data.get('samples', 64),  # 配置缺失时用 64
    timeout=render_data.get('timeout', 120),  # 配置缺失时用 120
)

# 业务代码 - fallback 到其他模型
model_config = self.oneapi.image_models.get(model_name)
if not model_config:
    # 静默 fallback 到其他模型
    model_name = "gemini-2.5-flash-image"
    model_config = self.oneapi.image_models[model_name]
```

✅ **正确做法（遵循 Fail Loudly）**：
```python
# config.py - 配置缺失时立即报错
render = RenderConfig(
    samples=_require_key(render_data, 'samples', 'render'),  # 缺失则抛异常
    timeout=_require_key(render_data, 'timeout', 'render'),
)

# 业务代码 - 直接访问，不存在则抛 KeyError
model_config = self.oneapi.image_models[model_name]  # KeyError if not exists
```

**为什么重要**：
- 科研数据生产需要**确定性**：我们知道系统在使用什么参数运行
- 及早发现问题：配置错误在启动时就暴露，而不是在运行 3 小时后才发现用了错误的模型
- 避免级联错误：使用错误参数产生的数据会污染整个数据集

**适用场景**：
- 配置解析（config loading）
- 模型选择（model selection）
- API 参数设置（API parameters）
- 文件路径解析（path resolution）

---

### 2. 单一配置原则（Single Source of Truth）- 最高优先级 ⚠️

**原则定义**：
如果一个参数在 `config.yaml` 中有定义，那么**代码中任何地方都不得设置该参数的默认值**。

**核心要求**：
1. **配置优先**：所有可调参数必须通过 `config.yaml` 控制，代码只读取配置值
2. **禁止代码默认值**：代码中禁止使用 `.get(key, default)`、`or default_value` 等形式为已在 config 中定义的参数设置默认值
3. **Fail Loudly + 单一配置**：两个原则结合确保配置完全可控
   - 配置缺失 → 立即报错（Fail Loudly）
   - 配置存在 → 代码不覆盖（单一配置）
4. **配置即文档**：`config.yaml` 中的值就是实际运行值，不存在"代码覆盖配置"的隐藏逻辑

**正反示例**：

❌ **错误做法（违反单一配置原则）**：
```python
# config.yaml 中已定义 render.samples = 64

# config.py - 代码中重复设置默认值（即使与配置相同也是错误的）
render = RenderConfig(
    samples=render_data.get('samples', 64),  # ❌ 错误：代码中设置了默认值
)

# 业务代码 - 使用配置值，但提供 fallback
timeout = config.tripo.timeout or 120  # ❌ 错误：覆盖了配置值
```

✅ **正确做法（遵循单一配置原则）**：
```python
# config.yaml 中定义 render.samples = 64

# config.py - 严格读取配置，不设置任何默认值
render = RenderConfig(
    samples=_require_key(render_data, 'samples', 'render'),  # ✅ 只读取，不设置默认值
)

# 业务代码 - 直接使用配置值
timeout = config.tripo.timeout  # ✅ 配置是什么就用什么
```

**两个原则的结合效果**：

| 场景 | Fail Loudly | 单一配置 | 结果 |
|------|-------------|----------|------|
| `config.yaml` 中定义 `samples: 64` | ✅ 配置存在 | ✅ 代码不覆盖 | 使用 64 |
| `config.yaml` 中删除 `samples` | ❌ 配置缺失，立即报错 | - | 程序终止，错误提示 |
| `config.yaml` 中定义 `samples: 128` | ✅ 配置存在 | ✅ 代码不覆盖 | 使用 128（不会被代码改回 64）|

**为什么重要**：
- **配置完全可控**：修改 `config.yaml` 就能改变系统行为，不需要修改代码
- **避免配置漂移**：不会出现"配置写的是 A，实际运行的是 B"的情况
- **便于调试**：看到配置文件就知道实际运行参数，不需要追踪代码中的覆盖逻辑
- **科研可复现**：实验参数完全由配置文件决定，代码不引入额外变量

**常见违规场景**：
- 使用 `dict.get(key, default)` 为已在 config 中定义的参数设置默认值
- 使用 `value or default` 提供 fallback
- 在函数参数中设置默认值（如 `def func(timeout=120)`）
- 在类属性中设置默认值（如 `timeout = 120`）

---

## 🏗️ 项目架构
### 激活环境

```bash
# qh_k8s（新服务器，优先）
source /data-koolab-nas/xiaoliang/local_envs/2d3d/bin/activate

# qh_4_4090（旧服务器）
# source /home/xiaoliang/local_envs/2d3d/bin/activate
```

### 代码目录结构约束 ⚠️

项目代码按职责严格分离到以下目录，**禁止混放**：

```
2d3d_v2/
├── app.py                       # Flask 服务入口
├── config/                      # 配置系统
│   ├── config.py                # 配置解析器
│   └── config.yaml              # 配置文件（唯一参数定义源）
├── core/                        # 核心业务逻辑
│   ├── image/                   # 图像生成、编辑、caption
│   ├── gen3d/                   # 3D 生成（Tripo、Hunyuan、Rodin）
│   └── render/                  # 渲染（Blender、WebGL）
├── utils/                       # 公共工具层
│   ├── llm_client.py            # LLM 统一调用
│   ├── image_api_client.py      # 图像 API 统一调用
│   └── prompts.py               # Prompt 模板集中管理
├── scripts/                     # 生产脚本（仅限 pipeline 核心流程）
│   ├── batch_process.py         # 统一批量处理 CLI 入口
│   ├── gen3d.py                 # 3D 生成 + 视角选择
│   ├── run_render_batch.py      # 批量渲染调度
│   ├── webgl_render.py          # WebGL 渲染后端
│   ├── render_views.py          # 单模型渲染 CLI
│   ├── generate_prompts.py      # Prompt 生成 CLI
│   ├── apply_edit.py            # 图像编辑 CLI
│   ├── bpy_render_standalone.py # Blender 子进程隔离渲染
│   ├── cleanup_instructions.py  # 数据清理维护工具
│   └── download_tripo_task.py   # Tripo 任务手动下载工具
└── tests/                       # 测试与调试脚本
    ├── test_*.py                # 单元测试、集成测试、手动测试
    ├── *.sh                     # 测试运行脚本
    └── *.md / *.png             # 测试结果与报告
```

**`scripts/` 准入规则**：

| 条件 | 要求 |
|------|------|
| 用途 | 生产 pipeline 的一部分，或被其他生产代码 import |
| 稳定性 | 经过验证，可重复使用 |
| 接口 | 有规范的 CLI 参数（`argparse`）或被其他模块 import |
| 命名 | **禁止** `test_*` 前缀 |

**`tests/` 范围**：

| 类型 | 说明 |
|------|------|
| 单元测试 | pytest 格式，测试单个模块功能 |
| 集成测试 | 端到端验证多模块协作 |
| 手动测试 | 需要人工运行和检查结果的脚本 |
| 调试工具 | 可视化、dry-run、参数打印等调试辅助 |
| 一次性验证 | 重构验证、功能验证等临时脚本 |

**强制规则**：
1. **测试脚本禁止放在 `scripts/`**：所有 `test_*` 文件、调试脚本、一次性验证脚本必须放在 `tests/`
2. **`scripts/` 不得有 `test_` 前缀的文件**：如发现，必须立即移至 `tests/`
3. **新增脚本前先判断归属**：是生产流程的一部分 → `scripts/`；其他 → `tests/`

### API 调用统一管理

为避免重复代码和维护困难，项目使用**统一的客户端层**管理所有外部 API 调用：

```
utils/
├── llm_client.py         # 文本 LLM 统一调用（GPT、Gemini 等）
├── image_api_client.py   # 图像 API 统一调用（T2I、图像编辑）
└── config.py             # 配置加载器

core/image/
├── generator.py          # T2IGenerator - 薄包装层，调用 ImageApiClient
├── editor.py             # ImageEditor - 薄包装层，调用 ImageApiClient
└── caption.py            # 使用 llm_client
```

**ImageApiClient** 自动根据模型名称选择正确的 API：
- `gemini-*`, `imagen-*`, `doubao-*` → **Response API**（异步轮询模式）
- 其他模型 → **Chat Completions API**（同步模式）

### Prompt 模板统一管理

所有 prompt 模板集中在 `utils/prompts.py`：

```python
from utils.prompts import (
    EditType,                    # 编辑类型枚举：REMOVE, REPLACE, BOTH
    get_instruction_prompt,      # 单个编辑指令 prompt
    get_batch_instruction_prompts,  # 批量指令（1:1 比例）
    get_optimize_prompt,         # T2I prompt 优化
    PROMPT_OPTIMIZER_SYSTEM,     # 系统级 prompt
)
```

---

## 📋 维护记录(概要)->需要同步修改 CHANGELOG.md(Detail)

### 2026-03-31: Source pipeline 断点续跑 + WebGL render 日志精简 + qh_k8s 路径配置化 + Batch Generation Bug 修复（v2.9.23）

**Source pipeline 断点续跑（`scripts/run_full_experiment.py`）：**
- `_run_source_pipeline`：prompt 优化完成后立即持久化 prompt_record，不再等整个 pipeline 成功
- 新增 `_resume_source_pipeline_if_needed`：existing_prompt 分支中按文件存在性补跑缺失阶段
- 任意子阶段（T2I / Gen3D / 渲染）中断后重启只补跑缺失部分

**WebGL render 日志精简（`run_render_batch.py` / `batch_process.py` / `run_full_experiment.py`）：**
- thread-local `_render_tls.quiet_subprocess` 控制输出模式
- `run_full_experiment` 传 `quiet_subprocess=True`：只输出关键节点（~8 行），失败时 dump 完整输出
- 单独跑 `batch_process.py render`：行为不变，原样输出

**qh_k8s 路径配置化（`config/config.yaml` / `config/config.py` / `app.py` / `scripts/webgl_render.py`）：**
- workspace 新增 `python_interpreter`、`playwright_browsers_path`、`logs_dir` 三个字段
- workspace 新增 `pipeline_index_db`：SQLite 索引 DB 路径（必须在本地磁盘，不可用 OSS/SeaweedFS）
- 服务器迁移只改 config.yaml 相关字段，无需改代码

**Batch Generation Bug 修复（`app.py` / `templates/batch_generation.html`）：**
- `random.object=true` 类型 YAML 不再校验误报
- Execute Selected YAML 只生成 CLI 命令，不自动执行

---

### 2026-03-31: 新服务器 qh_k8s 路径配置化 + Batch Generation Bug 修复

**路径配置化：**
- `config.yaml` `workspace` 新增 `python_interpreter`、`playwright_browsers_path`、`logs_dir`
- `config.py` `WorkspaceConfig` 对应新增三个字段（Fail Loudly 解析）
- `app.py` 删除 `EXPERIMENT_PLANS_DIR`、`PYTHON_INTERPRETER`、`LOGS_DIR` 硬编码，改为 `init_semaphores()` 从 config 初始化
- `scripts/webgl_render.py` Playwright 路径从 config 读取
- 日志目录统一写 `data-koolab-nas`，避免 data-oss OSError

**2026-04-01: Models 页面 SQLite 持久化索引 + YAML 优先加载（`utils/pipeline_index.py` / `config/` / `app.py` / `templates/models.html`）：**
- `config.yaml` `workspace` 新增 `pipeline_index_db`：SQLite DB 路径，必须指向本地磁盘（`data-koolab-nas`），不可用 OSS/SeaweedFS（SQLite WAL 在 FUSE 挂载上不稳定）
- `config.py` `WorkspaceConfig` 新增 `pipeline_index_db: str`
- `utils/pipeline_index.py`（新建）：`PipelineIndex` 类，启动时增量 reconcile，写操作后定点更新，WAL 模式线程安全
- `app.py`：`get_all_models_index()` / `_load_model_payload_by_id()` 优先读 DB；新增 `GET /api/models/batch` 接口；各写操作后触发 `_refresh_model_in_index()`
- `templates/models.html`：初始加载提示 + YAML 筛选后优先批量加载对应 model

**Batch Generation 修复：**
- `_normalize_experiment_plan_for_form`：`random.object=true` 时 `objects` 保持 `None`，消除校验误报
- `api_execute_existing_experiment_plan`：删除对已有 YAML 的表单级 `_validate_experiment_plan` 调用
- "Execute Selected YAML" 按钮：改为只生成 CLI 命令，不自动执行

**路径约束（更新）**:
- 路径配置化后：代码路径 `/data-oss/meiguang/xiaoliang/code3/2d3d_v2`
- 数据路径：`/data-oss/meiguang/xiaoliang/data/2d3d_data`
- Python 环境：`/data-koolab-nas/xiaoliang/local_envs/2d3d`
- 日志目录：`/data-koolab-nas/xiaoliang/code3/2d3d_v2/logs`

---

### 2026-03-16: 前端 Batch Generation 配置页面

**新增功能**: Web 前端可视化配置 `run_full_experiment.py` 实验计划，一键生成 YAML 和 CLI 命令。

**解决方案**:
1. **页面路由**：新增 `/batch-generation` 页面
2. **Options API**：`GET /api/experiment-plan/options` 返回 providers、edit_modes、categories、styles
3. **Generate API**：`POST /api/experiment-plan/generate` 校验参数、生成 YAML、返回 CLI
4. **前端表单**：基本设置 + 全局编辑比例 + 类别配置（可增删）+ YAML/CLI 预览

**新增文件**:
- `templates/batch_generation.html` — 完整配置表单 + 预览 + 操作按钮

**修改文件**:
- ✅ `app.py` — 新增页面路由和两个 API
- ✅ `templates/base.html` — 导航栏新增 "Batch Generation" 入口
- ✅ `CHANGELOG.md` — v2.9.4 变更记录

**路径约束**:
- 生成文件仅写入 `/seaweedfs/xiaoliang/data/2d3d_data/experiment_plans/`
- Python 解释器固定为 `/home/xiaoliang/local_envs/2d3d/bin/python`
- 文件名 slug 化，禁止路径穿越

---

### 2026-03-13: Method-2 编辑质量检查系统（Two-Stage LLM + Target 3D Consistency）

**新增功能**: 可切换的编辑质量检查方法，与 Method-1（`grid_vlm`）并行可选。通过 `edit_quality_check.method` 配置切换。

**解决方案**:
1. **Stage 1 (Two-Stage LLM)**：Stage 1A VLM diff + Stage 1B LLM judge，不再依赖 6-view grid 拼图
2. **Stage 2 (DreamSim)**：Target 3D 渲染视角与编辑后视角的感知一致性检查
3. **Quality Router**：统一方法调度，`create_quality_checker()` + `build_quality_check_meta()` 替代分散的局部 helper
4. **前端展示**：Method-2 结果详情（Stage 1A/1B 折叠面板、Target 3D 一致性分数面板）

**新增配置项**:
- `edit_quality_check.method`: `"grid_vlm"` | `"two_stage_recon"`
- `edit_quality_check.two_stage_recon.*`：6 个子配置项
- `tasks.edit_quality_check_diff` / `tasks.edit_quality_check_judge`
- `concurrency.recon_quality_check`

**新增文件**:
- `core/image/edit_quality_checker_v2.py` — Method-2 两阶段 LLM checker
- `core/render/recon_consistency_checker.py` — DreamSim 一致性 checker
- `core/image/edit_quality_router.py` — 方法调度 router

**修改文件**:
- ✅ `config/config.yaml` — 新增 method、two_stage_recon、task entries、concurrency
- ✅ `config/config.py` — TwoStageReconConfig、扩展 EditQualityCheckConfig、新属性
- ✅ `scripts/batch_process.py` — 切换到 router 调用
- ✅ `app.py` — 切换到 router 调用，新增 target_quality_check 加载和 semaphore
- ✅ `templates/edit_batch_card_macro.html` — Method-2 结果展示
- ✅ `templates/model_detail_scripts.html` — QC Cmd 方法感知
- ✅ `CHANGELOG.md` — v2.9.0 变更记录
- ✅ `docs/guide/cli.md` — check-edit-quality 行为说明更新

---

### 2026-03-03: scripts/ 与 tests/ 目录整理

**问题**: `scripts/` 中混放了 7 个测试/调试脚本（`test_config_v2.py`、`test_view_selection_toggle.py` 等），违反职责分离原则。

**解决方案**:
1. **移动文件**：7 个测试脚本从 `scripts/` 移至 `tests/`
2. **新增规范**：在 AGENTS.md 中制定代码目录结构约束（`scripts/` 准入规则 + `tests/` 范围定义）
3. **更新引用**：更新所有文档中的路径引用（README、CHANGELOG、HANDOVER、archive docs）

**文件变更**:
- ✅ 移动 `scripts/test_*.py`（6 个）和 `scripts/check_angles.py` → `tests/`
- ✅ 修改 `AGENTS.md` - 新增「代码目录结构约束」章节
- ✅ 修改 `CHANGELOG.md` - 新增 v2.7.2 变更记录
- ✅ 修改 `README.md`、`HANDOVER.md`、`docs/archive/*.md` - 更新路径引用

---

### 2026-03-03: 服务端静态文件缓存优化

**问题**: 前端加载 GLB 文件很慢（50MB+ 文件），每次访问都重新下载，无 HTTP 缓存。

**解决方案**:
1. **添加 HTTP 缓存头** - 根据文件类型设置不同的 `Cache-Control`
2. **添加 CORS 支持** - `Access-Control-Allow-Origin: *`
3. **统一缓存处理函数** - `_make_response_with_cache()`

**缓存策略**:
| 文件类型 | 缓存时间 | 说明 |
|----------|----------|------|
| `.glb` | 4 小时 | 3D 模型文件，测试期间缓存较短 |
| `.png/.jpg/.jpeg/.webp/.gif` | 1 天 | 图片文件 |
| `.js/.css/.woff/.woff2/.ttf` | 1 年 | 静态资源 |

**文件变更**:
- ✅ 修改 `app.py` - 新增缓存常量和 `_make_response_with_cache()`，修改 `serve_data()` 和 `serve_pipeline()`

---

### 2026-03-03: 配置系统改进 — Per-model base_url、Tripo 分区、Hunyuan 共享配置

**变更 1: Per-model base_url 覆盖机制**

**问题**: `gemini-2.5-flash-image` 和 `gemini-3-pro-image-preview` 需要使用不同的 API 网关（`model-link-alpha`），但 `oneapi.base_url` 是全局共享的，无法为单个模型指定不同的 base_url。

**解决方案**: 在 `ImageModelConfig`、`TextModelConfig`、`Gen3DModelConfig` 中添加可选的 `base_url` 字段。如果模型配置了 `base_url`，则覆盖全局 `oneapi.base_url`；否则继续使用全局值。

**配置示例**:
```yaml
oneapi:
  base_url: "https://oneapi.qunhequnhe.com"   # 全局默认
  image_models:
    gemini-2.5-flash-image:
      base_url: "http://model-link-alpha.k8s-qunhe.qunhequnhe.com"  # 覆盖全局
      # ...其他参数不变
    doubao-seedream-4.5:
      # 无 base_url → 使用全局 oneapi.base_url
      # ...
```

**解析逻辑**:
```python
# config.py — 所有 backward-compatible 属性统一使用
def _resolve_base_url(self, model_config) -> str:
    return model_config.base_url or self.oneapi.base_url
```

**变更 2: Tripo 配置分区**

**问题**: Tripo 配置项多达 20+ 个，API 连接参数与生成参数、视角选择参数混在一起，可读性差。

**解决方案**: 用注释将 tripo 配置分为三个逻辑区块（不改变数据结构，不影响代码）：
- `# ---------- API 连接配置 ----------`：api_key, base_url, timeout, max_retries
- `# ---------- 生成参数 ----------`：model_version, geometry_quality, seeds, texture 等
- `# ---------- 视角选择与重映射 ----------`：enable_view_selection, multiview_strategy 等

**变更 3: Hunyuan 模型共享配置**

**问题**: `hunyuan-3d-3.1-pro` 在 config.yaml 中存在严重缩进 bug（被解析为顶层 key，不在 `gen3d_models` 下），且配置内容与 `hunyuan-3d-pro` 完全重复。`Config.hunyuan` 属性硬编码 `model_name = "hunyuan-3d-pro"`，无法选择其他模型。

**解决方案**:
1. 使用 YAML anchor/alias 让两个模型共享同一份配置：
   ```yaml
   hunyuan-3d-pro: &hunyuan_pro_config
     api_type: "response"
     # ...
   hunyuan-3d-3.1-pro: *hunyuan_pro_config
   ```
2. 新增 `get_hunyuan_config(model_name)` 方法，支持显式选择模型；`@property hunyuan` 保持向后兼容
3. `hunyuan.py` 中 pro-only 参数检查扩展为匹配所有 pro 变体

**切换 Hunyuan 模型**:
```yaml
tasks:
  gen3d:
    provider: "oneapi"
    model: "hunyuan-3d-3.1-pro"   # 或 "hunyuan-3d-pro"
```

**文件变更**:
- ✅ 修改 `config/config.yaml` — 两个 Gemini 模型添加 `base_url`；Tripo 分区注释；Hunyuan anchor/alias + 缩进修复
- ✅ 修改 `config/config.py` — 三个 ModelConfig 添加 `base_url: Optional[str]`；解析循环读取可选 `base_url`；7 个 backward-compatible 属性改用 `_resolve_base_url()`；新增 `get_hunyuan_config()` 方法
- ✅ 修改 `core/gen3d/hunyuan.py` — pro-only 检查扩展为 `("hunyuan-3d-pro", "hunyuan-3d-3.1-pro")`
- ✅ 修改 `scripts/gen3d.py` — VLM 配置的 ad-hoc `base_url` 改为尊重 per-model 覆盖

---

### 2026-03-03: Fail Loudly 原则系统性修复与配置完善

**问题**: 多处违反 Fail Loudly 原则，使用 `getattr(config, 'key', default)` 和 `.get("key", default)` 提供硬编码默认值，导致配置缺失时静默降级。

**解决方案**:

1. **ImageApiClient 配置访问** - 移除所有 `getattr(..., default)`，改为直接访问属性
2. **App 并发控制修复** - `concurrency.image` 直接访问，`provider` 默认值从配置读取
3. **t2i 任务并发控制** - 将 `t2i` 加入 `EDIT_SEMAPHORE` 控制
4. **guided_edit 配置化** - 新增 `tasks.guided_edit` 配置，`ImageApiConfig` 补充完整字段
5. **Render 命令改进** - `--provider` 改为可选，默认从 `config.tasks["gen3d"].provider` 读取
6. **其他修复** - `rodin.py` 添加文件验证，WebGL 修复左右视角和 bottom 阴影

**文件变更**:
- ✅ 修改 `config/config.yaml` - 新增 `tasks.guided_edit`
- ✅ 修改 `config/config.py` - 补充 `ImageApiConfig` 字段，新增 `guided_edit` 属性
- ✅ 修改 `utils/image_api_client.py` - 移除 `getattr` 默认值
- ✅ 修改 `app.py` - 修复 concurrency 和 provider 默认值，t2i 加入 semaphore 控制
- ✅ 修改 `scripts/batch_process.py` - render 命令 provider 可选
- ✅ 修改 `core/gen3d/rodin.py` - 添加文件验证
- ✅ 修改 `core/render/webgl_script.py` - 修复视角和阴影
- ✅ 删除 `tests/` 下 4 个无效测试文件

---

### 2026-03-03: 外部绝对路径兼容性修复

**问题**: 当 `workspace.pipeline_dir` 配置为外部绝对路径（如 `/seaweedfs/...`）时，系统多处崩溃：
1. `relative_to(PROJECT_ROOT)` 在外部路径时抛出 `ValueError`
2. 前端图片无法加载（路径格式错误）
3. gen3d 任务找不到源图片

**根本原因**: 代码中隐式假设 pipeline_dir 一定在 `PROJECT_ROOT` 内，违反单一配置原则。

**解决方案**:
1. **新增 `_rel_path()` helper**：统一路径格式化，外部路径返回 `pipeline/...` 格式
2. **新增 `_resolve_api_path()` helper**：将 API 路径解析回文件系统路径
3. **新增 `/pipeline/<filename>` Flask 路由**：直接从 `PIPELINE_DIR` 服务文件
4. **全局替换所有 `relative_to(PROJECT_ROOT)`**：共 29 处，全部改为 `_rel_path()`
5. **修复所有 `PROJECT_ROOT / params["..."]`**：共 5 处，改为 `_resolve_api_path()`

**文件变更**:
- ✅ 修改 `app.py` - 新增 `_rel_path()`, `_resolve_api_path()`, `/pipeline/` 路由，全局路径替换
- ✅ 修改 `templates/model_detail_scripts.html` - 实现 `showEditCmd()` 函数
- ✅ 修改 `scripts/batch_process.py` - 新增 `--provider-id` 参数，修复 CLI 编辑路径

**路径转换逻辑**:
```
内部路径 (data/pipeline/...) → 保持原样 → /data/<filename> 路由
外部绝对路径 (/seaweedfs/...) → pipeline/... → /pipeline/<filename> 路由
```

**旧数据兼容**: `meta.json` 中存储的绝对路径会自动转换为正确格式

---

### 2026-03-03: Pipeline 数据目录可配置化

**问题**: 所有脚本和 `app.py` 中 pipeline 数据目录（`data/pipeline`）均硬编码，无法通过配置文件切换工作区。

**解决方案**:
1. **新增 `workspace` 配置段**：在 `config.yaml` 顶层添加 `workspace.pipeline_dir`，支持相对路径（相对于项目根目录）或绝对路径
2. **新增 `WorkspaceConfig` 数据类**：`config/config.py` 中添加对应的 dataclass 和解析逻辑
3. **`app.py` 延迟初始化**：将目录常量的初始化移入 `init_semaphores()`（已在启动时调用），读取 config 后再解析路径
4. **`batch_process.py` 模块级初始化**：模块导入时调用 `load_config()` 获取路径，替换硬编码常量
5. **`run_render_batch.py` 和 `gen3d.py` 函数级初始化**：在已有的 `load_config()` 调用之后，立即推导路径变量

**文件变更**:
- ✅ 修改 `config/config.yaml` - 新增 `workspace.pipeline_dir: "data/pipeline"`
- ✅ 修改 `config/config.py` - 新增 `WorkspaceConfig` 类，`Config` 新增 `workspace` 字段，`load_config()` 新增解析逻辑
- ✅ 修改 `app.py` - 目录常量改为 `None` 占位，`init_semaphores()` 中从 config 解析并赋值
- ✅ 修改 `scripts/batch_process.py` - 模块级从 config 推导 `IMAGES_DIR`, `MODELS_DIR`, `TRIPLETS_DIR`
- ✅ 修改 `scripts/run_render_batch.py` - `process_rendering()` 中从 config 推导 `models_dir`, `triplets_dir`
- ✅ 修改 `scripts/gen3d.py` - `generate_3d_model()` 中从 config 推导 `images_dir`, `models_dir`, `triplets_dir`

**新增配置**:
```yaml
# config.yaml
workspace:
  # Pipeline 数据目录（相对于项目根目录，或绝对路径）
  pipeline_dir: "data/pipeline"
```

**切换工作区**:
```yaml
# 只需修改这一行即可切换整个数据目录
workspace:
  pipeline_dir: "data/pipeline_experiment_2"
  # 或使用绝对路径
  # pipeline_dir: "/mnt/nas/2d3d_data"
```

**行为说明**:
- 相对路径以项目根目录（`PROJECT_ROOT`）为基准解析
- 绝对路径直接使用
- 所有子目录（`images/`, `models_src/`, `triplets/`, `prompts/`, `instructions/`）均从 `pipeline_dir` 推导，无需额外配置

---

### 2026-03-02: Hunyuan 图像预处理配置重构

**问题**: `gen3d-from-edits` 命令执行后，编辑后的多视角视图被裁剪并覆盖原图。

**根本原因**:
1. `HunyuanGenerator._crop_cfg` 硬编码启用裁剪，无法通过配置控制
2. `ImageProcessor._save_processed_image()` 在没有 `output_dir` 时直接覆盖原图

**解决方案**:
1. **新增配置项**: 在 `oneapi.gen3d_models.hunyuan-3d-pro` 下添加 `preprocess` 配置
2. **默认禁用裁剪**: `preprocess.enabled = false`，保持原图不变
3. **临时目录隔离**: 启用裁剪时，`output_dir` 指向临时目录，永不覆盖原图
4. **自动清理**: `close()` 时自动清理临时文件

**文件变更**:
- ✅ 修改 `config/config.yaml` - 添加 `preprocess.enabled: false`
- ✅ 修改 `config/config.py` - 新增 `PreprocessConfig` 类，更新 `HunyuanConfig` 和 `Gen3DModelConfig`
- ✅ 修改 `core/gen3d/hunyuan.py` - 移除硬编码配置，从 config 读取，使用临时目录

**新增配置**:
```yaml
# config.yaml
hunyuan-3d-pro:
  preprocess:
    enabled: false  # 默认禁用，设为 true 启用前景裁剪
```

**行为变更**:

| 场景 | 修改前 | 修改后 |
|------|--------|--------|
| 默认行为 | 启用裁剪，覆盖原图 | 不裁剪 |
| 启用裁剪 | 覆盖原图 | 保存到临时目录，原图不变 |
| 配置控制 | 硬编码 | 通过 `config.yaml` 控制 |
| 清理 | 不清理 | `close()` 时自动清理临时文件 |

**关键代码改动**:
```python
# hunyuan.py - 从配置读取，使用临时目录
if preprocess_cfg.enabled:
    self._temp_dir = tempfile.mkdtemp(prefix="hunyuan_preprocess_")
    crop_cfg = {
        ...
        "output_dir": self._temp_dir,  # 关键：输出到临时目录
    }
```

---

### 2026-03-02: Tripo 视角选择逻辑优化 — Top vs Front + VLM 辅助判断

**问题**: 现有的 `enable_view_selection=true` 模式基于 3 对视角（front_back, left_right, top_bottom）的信息熵比较，可能导致侧面视角被映射到 front slot，生成效果很差。

**解决方案**: 
1. **简化决策**：只比较 top 和 front 的信息熵，不再做 3 对全排列比较
2. **VLM 辅助判断**：当 `top_score - front_score > threshold` 时，调用 `gemini-3-flash-preview` 判断 top 视角是否可被视为正面
3. **沿用现有映射**：VLM 确认后使用 Case 2 映射（top→front slot，left/right 旋转）

**文件变更**:
- ✅ 修改 `config/config.yaml` - 新增 `entropy_diff_threshold` 和 `view_selection_vlm_model`
- ✅ 修改 `config/config.py` - `TripoConfig` 新增字段
- ✅ 修改 `scripts/gen3d.py` - 新增 `_vlm_judge_top_as_front()` 函数，重写视角选择逻辑

**新增配置**:
```yaml
tripo:
  entropy_diff_threshold: 1.0        # top 得分需超过 front 此阈值才触发 VLM
  view_selection_vlm_model: "gemini-3-flash-preview"  # VLM 模型
```

**数据流**:
```
front_score, top_score = _entropy_edge_score()
diff = top_score - front_score
diff > 1.0? ──No──> Case 1 标准映射 (front→front)
       │
      Yes
       │
VLM 判断 "top 是正面吗?"
       │
top_is_front? ──No──> Case 1 标准映射
       │
      Yes
       │
Case 2 映射 (top→front, bottom→back, left/right 旋转)
```

**测试验证**:
- Case `0255e658900d`: `entropy_diff = -4.25` → 不触发 VLM，标准映射 ✅
- Case `99f8404cc8ed` (华夫饼): `entropy_diff = 2.84` → VLM 判断 `top_is_front=True` → Case 2 映射 ✅

---

### 2026-03-02: WebGL 渲染后端优化

**修复内容**:

1. **左右视角修正**：交换 left/right 的 theta 角度（left=90, right=-90），解决视角反了的问题
2. **底部视角阴影消除**：bottom 视角动态设置 `shadowIntensity=0`，避免地面阴影铺满画面
3. **浏览器缓存隔离**：使用项目本地目录 `.playwright-browsers/` 存放浏览器二进制，避免共享 `~/.cache/ms-playwright/` 被其他用户/环境污染

**文件变更**:
- ✅ 修改 `core/render/webgl_script.py` - 修正视角角度、添加 bottom 视角阴影消除逻辑
- ✅ 修改 `scripts/webgl_render.py` - 添加 `PLAYWRIGHT_BROWSERS_PATH` 环境变量配置

**浏览器安装**:
```bash
# 首次使用或更新后需要安装浏览器到项目本地
PLAYWRIGHT_BROWSERS_PATH=/home/xiaoliang/2d3d_v2/.playwright-browsers playwright install chromium
```

---

### 2026-03-02: 第一优先级代码修复

**修复问题**:

| 问题 | 文件 | 修复内容 |
|------|------|----------|
| 运行时修改共享 Config 对象 | `config.yaml`, `config.py`, `app.py`, `batch_process.py` | 新增 `tasks.guided_edit` 配置，添加 `config.guided_edit` 属性 |
| Fail Loudly 违反（硬编码默认值） | `image_api_client.py`, `app.py` | 移除所有 `getattr()` 默认值 |
| 默认 provider 硬编码 | `app.py` | 从 `config.tasks["gen3d"].provider` 读取 |
| rodin.py 下载后缺少文件验证 | `rodin.py` | 添加 `validate_file_content()` 调用 |
| 无效测试文件 | `tests/` | 删除 4 个调试脚本 |

**配置新增**:
- `tasks.guided_edit`: Guided view editing 任务配置
- `ImageApiConfig`: 补充 `size`, `n`, `poll_interval`, `max_wait_time` 字段

**删除文件**:
- `tests/test_direct_gen3d.py`
- `tests/test_hunyuan_actual_code.py`
- `tests/test_fault_condition.py`
- `tests/verify_gen3d_code.py`

---

### 2026-02-28: 文档系统重构

**问题**: 项目文档系统混乱，docs/目录下有18个文档混杂在一起，根目录文档堆积，临时文档与永久文档不分，缺少统一的文档管理规则。

**解决方案**:
1. **整合核心内容**：将"项目目标与设计文档.md"的核心内容（项目目标、5阶段数据流程、科研导向原则）整合到 README.md
2. **删除过时文档**：删除已整合的中文文档、清理临时任务文档
3. **创建新的文档结构**：
   - `docs/guide/`：使用指南（cli, batch-render, http-client, uv-env）
   - `docs/architecture/`：架构设计（project-structure, render-module, taxonomy-design, style-diversity）
   - `docs/reference/`：快速参考（QUICK_REFERENCE）
   - `docs/archive/`：历史归档（临时文档、测试报告、重构记录）
4. **创建文档索引**：新增 `docs/INDEX.md` 作为所有文档的导航中心
5. **更新 AGENTS.md**：添加完整的文档管理规则和维护原则

**文件变更**:
- ✅ 更新 `README.md` - 整合项目目标与设计原则
- ✅ 更新 `AGENTS.md` - 添加文档系统规范
- ✅ 重命名 `changelog.md` → `CHANGELOG.md`（统一大写命名）
- ✅ 创建 `docs/INDEX.md` - 文档索引
- ✅ 移动并重命名文档到正确位置
- ✅ 归档临时文档到 `docs/archive/`
- ✅ 删除已整合的中文文档 "项目目标与设计文档.md"
- ✅ 删除根目录堆积的临时文档（continue.md, QUICK_REFERENCE.md 等移动到 docs/）

**文档统计**:
- 根目录：3个核心文档（README, AGENTS, CHANGELOG）
- docs/guide/：4个使用指南
- docs/architecture/：5个架构文档
- docs/reference/：1个快速参考
- docs/archive/：11个历史文档

---

### 2026-02-28: WebGL 渲染后端 - 完全修复（所有 6 视角独立 + 对齐）

**问题**: WebGL 渲染后端实现后，遇到重大 bug：6 个视角渲染结果完全相同，且上下视角严重偏心。

**根本原因分析**:
1. **JS 数据结构不匹配** - Python 生成 `theta/phi/radius`，JS 访问不存在的 `rotX/rotY/rotZ` → 导致方向 undefined
2. **属性选择错误** - 使用了 `orientation`（旋转模型），而非 `camera-orbit`（移动相机）
3. **WebGL 缓冲区问题** - 直接从 shadow DOM canvas 截图，但 `preserveDrawingBuffer=false` 导致部分视角截到已清空的缓冲区
4. **固定相机距离** - 105% 对 105% 无法适配不同尺寸/形状模型，导致裁剪
5. **目标点偏离中心** - `cameraTarget` 默认在原点，模型中心不在原点 → 上下视角"歪着看"
6. **极点钳位** - model-viewer 默认限制 phi ∈ [~22.5°, ~157.5°]，phi=0.001° 被钳位回 22.5°
7. **透视畸变** - 即使所有参数正确，透视相机也会导致视觉中心下移

**解决方案** ✅:

| # | 问题 | 修复 |
|----|------|------|
| 1 | JS 数据结构不匹配 | 统一使用 `theta/phi/radius` |
| 2 | 使用错误属性 | 改用 `viewer.cameraOrbit` + `jumpCameraToGoal()` |
| 3 | WebGL 缓冲区问题 | **架构改写**：Playwright 驱动循环 → `page.screenshot()` 直接捕获屏幕 |
| 4 | 固定相机距离 | 运行时计算：`radius = (bbox_diagonal/2) / tan(fov/2) * 1.2` |
| 5 | 目标点偏离 | 读取 `viewer.getBoundingBoxCenter()`，每次 `setView` 同步更新 |
| 6 | 极点钳位 | 添加 `min-camera-orbit="auto 0deg auto"` + `max-camera-orbit="Infinity 180deg Infinity"` |
| 7 | 透视畸变 | Top/bottom 使用 `orthographic` 投影，前后左右保持 `perspective` |

**文件变更**:
- ✅ `core/render/webgl_script.py` - 完全重写（简化架构，修复 JS 逻辑，添加正交投影、动态相机距离、bbox 中心对齐）
- ✅ `scripts/webgl_render.py` - Playwright 驱动循环，`page.screenshot()` 捕获
- 无需修改 `scripts/run_render_batch.py`（已兼容新签名）

**关键改进**:
- ✅ 6 个视角全部独立渲染（MD5 全不相同）
- ✅ 所有视角模型完整可见（动态距离计算）
- ✅ Top/bottom 视角正上/正下方向完全对齐（极点解锁 + 正交投影）
- ✅ 前后左右视角自然透视效果（perspective projection）
- ✅ 支持任意 bbox 偏离的模型（动态 cameraTarget）

**性能**:
- 首次加载：~25-27s（包括 GLB 加载和 Chromium 启动）
- 6 视角截图：~26s（parallel screenshots via Playwright）
- 总耗时：~1min（单模型完整渲染）

**验证输出**:
- `0c5ec8bd33a7`: `/home/xiaoliang/2d3d_v2/data/pipeline/triplets/0c5ec8bd33a7/views/` ✅
- `6010d3b2a963`: `/home/xiaoliang/2d3d_v2/data/pipeline/triplets/6010d3b2a963/views/` ✅

---

### 2026-02-27: WebGL 渲染后端 - Model Viewer 集成

**问题**: 现有的 Blender 渲染虽然功能完整，但光照效果不如前端 model-viewer 查看器自然（IBL 环境光照 vs 人工灯光）。

**解决方案**: 新增 WebGL 渲染后端，使用 headless Chrome + Google Model Viewer 进行后端渲染，与前端展示效果一致。

**实现细节**:
1. **双后端架构**: 通过 `config.yaml` 中的 `render.backend` 参数选择
   - `blender`: 传统的 Blender + Cycles/Eevee 渲染（保留所有现有代码）
   - `webgl`: 新的 headless Chrome + model-viewer 渲染
2. **6视角保持一致**: WebGL 渲染使用与 Blender 相同的 6 个标准视角
3. **PBR 质量提升**: 使用 model-viewer 的 `neutral` 环境光照，效果更自然
4. **配置完全兼容**: 新增 WebGL 专属配置段，不影响 Blender 配置

**文件变更**:
- ✅ 修改 `config/config.yaml` - 添加 `backend` 参数和 `webgl` 配置段
- ✅ 修改 `config/config.py` - 新增 `WebGLRenderConfig` 类，更新 `RenderConfig`
- ✅ 新增 `core/render/webgl_script.py` - HTML/JS 生成器
- ✅ 新增 `scripts/webgl_render.py` - Playwright 渲染主模块
- ✅ 修改 `scripts/run_render_batch.py` - 集成双后端路由逻辑

**使用方法**:
```yaml
# config.yaml
render:
  backend: "webgl"  # 切换为 webgl 后端
  # ... 保留所有 Blender 配置，可随时切换回 blender
  
  webgl:
    environment_image: "neutral"  # IBL 环境光照
    shadow_intensity: 1.0
    render_timeout: 10000
```

**安装依赖**:
```bash
pip install playwright
playwright install chromium
```

**关键改进**:
- 渲染质量: Blender 人工灯光 → WebGL IBL 环境光照
- 部署简化: 无需安装 Blender（仅需 Chrome）
- 效果一致性: 后端渲染与前端查看器完全一致
- 向后兼容: 100%（所有 Blender 代码完整保留）

---

### 2026-02-26: 配置系统重构 - 统一 OneAPI Gateway

**问题**: 配置文件中存在大量重复配置，`qh_mllm`、`qh_image`、`gemini_response`、`multiview_edit`、`doubao_image` 都使用相同的 API key 和 base_url，维护困难且容易出错。

**解决方案**:
1. 创建统一的 `oneapi` 配置段，集中管理 API key 和 base_url
2. 按功能分类模型配置（text_models、image_models、gen3d_models）
3. 删除未使用的 `newapi` 和 `openrouter` 配置
4. 保持完全向后兼容，所有现有代码无需修改

**文件变更**:
- ✅ 重构 `config/config.yaml` - 新配置结构
- ✅ 重构 `config/config.py` - 新配置解析模块
- ✅ 更新 `config/__init__.py` - 导出新配置类
- ✅ 更新 `utils/config.py` - 重新导出保持兼容
- ✅ 新增 `docs/config_refactoring_2026-02-26.md` - 重构文档

**关键改进**:
- API key 配置次数：6次 → 1次
- 配置文件行数：~300行 → ~250行（减少17%）
- 配置层次：扁平 → 分层（更清晰）
- 向后兼容：100%（所有现有代码无需修改）

**详细文档**: [docs/config_refactoring_2026-02-26.md](docs/config_refactoring_2026-02-26.md)

### 2026-01-30: 渲染 top 视角爆白问题定位与经验沉淀

**现象**: 6 视角渲染中仅 `top` 视角出现大面积高光压满（“爆白/像蒙了一层雾/细节不清晰”），其余视角正常。

**关键结论**:
- 将 `render.lighting_mode` 切换为 `ambient` 后问题消失，说明主因是方向光/高反射材质在顶视角条件下导致的高光剪裁。
- `SUN` 灯（平行光）的照明方向主要由灯的旋转（rotation）决定，而不是位置（location）；仅调整 `location` 往往无法改变高光进入相机的条件。

**落地动作**:
- 将经验与排查步骤记录在 [docs/batch_render_guide.md](docs/batch_render_guide.md)。

### 2026-01-25: 重构图像 API 调用层

**问题**: `T2IGenerator` 和 `ImageEditor` 各自独立实现 HTTP 请求逻辑，修改时容易产生代码重复或破坏。

**解决方案**: 
1. 创建 `utils/image_api_client.py` 统一管理图像 API 调用
2. 重构 `generator.py`（从 183 行 → 47 行）
3. 重构 `editor.py`（从 177 行 → 44 行）
4. 自动检测模型类型，选择正确的 API 调用方式

**文件变更**:
- ✅ 新增 `utils/image_api_client.py`
- ✅ 简化 `core/image/generator.py`
- ✅ 简化 `core/image/editor.py`

### 2026-01-25: 集中 Prompt 模板

**问题**: Prompt 模板分散在多个文件，修改时容易遗漏。

**解决方案**: 
1. 创建 `utils/prompts.py` 集中所有 prompt 模板
2. 添加 `EditType` 枚举支持 REMOVE/REPLACE 分离
3. 更新所有引用处使用集中模板

---

## ⚠️ 经验教训

### 1. 避免代码重复 - 使用统一客户端层

❌ **错误做法**: 每个模块自己实现 HTTP 请求
```python
# generator.py
response = self.client.post(f"{self.config.base_url}/v1/responses", ...)

# editor.py  
response = self.client.post(f"{self.config.base_url}/v1/responses", ...)  # 重复！
```

✅ **正确做法**: 统一客户端，业务模块只做薄包装
```python
# generator.py
class T2IGenerator:
    def __init__(self, config):
        self.client = ImageApiClient(config)  # 使用统一客户端
    
    def generate_image(self, prompt, output_path):
        return self.client.generate_image(prompt, output_path)
```

### 2. 修改前先理解调用关系

❌ **错误做法**: 直接修改文件，不检查是否影响其他模块
✅ **正确做法**: 
1. 先 `grep_search` 查找所有引用
2. 理解调用链路
3. 确认修改影响范围
4. 一次性修改所有相关处

### 3. 配置与逻辑分离

❌ **错误做法**: 在代码中硬编码 API 端点、模型名称
```python
response = self.client.post("https://oneapi.qunhequnhe.com/v1/responses", ...)
```

✅ **正确做法**: 从配置读取，逻辑自动适配
```python
# ImageApiClient 根据 model 名称自动选择正确的 API
if self._use_response_api():  # gemini-*, imagen-*, doubao-*
    return self._generate_via_response_api(prompt)
else:
    return self._generate_via_chat_api(prompt)
```

### 4. 新功能先设计接口，再实现

在添加新功能前，先确定：
1. **输入/输出** - 函数签名是什么
2. **调用方** - 谁会调用这个功能
3. **依赖** - 需要哪些配置和工具
4. **集成点** - 如何与现有代码整合

### 5. 文档即代码

每次重大修改后，同步更新：
- `README.md` - 用户可见的功能变更
- `AGENTS.md` - 开发者维护记录
- 代码注释 - 关键逻辑说明

---

## 🔧 常见问题排查

### API 调用失败

1. **检查模型配置** - `config/config.yaml` 中的 `model` 字段
2. **检查 API 类型匹配** - Gemini 模型必须使用 Response API
3. **查看日志** - Flask 终端会打印 `[ImageAPI]` 状态

### 图像生成返回空

通常是 API 类型不匹配：
- `gemini-2.5-flash-image` 需要 Response API（异步轮询）
- 使用 Chat Completions 会返回空响应

### 任务超时

调整 `config/config.yaml` 中的：
- `timeout`: HTTP 请求超时
- `poll_interval`: 轮询间隔
- `max_wait_time`: 最大等待时间

---

## 🔄 异步任务管理

### 任务状态持久化

任务完成后会自动更新 `tasks.jsonl` 文件：

```python
def process_task(task_id: str):
    # ... 执行任务 ...
    task["completed_at"] = datetime.now().isoformat()
    update_task_in_file(task_id, task)  # 更新持久化文件
```

### 重启行为

应用重启时**不会**自动恢复任务：
- 大多数任务（t2i、instruction、render）是瞬时的，重新执行即可
- 只有 `gen3d` 任务可能需要恢复（因为耗时 10-30 分钟）

### gen3d 任务恢复（待实现）

对于长时间运行的 3D 生成任务，未来可以：
1. 记录远程任务 ID（Tripo task_id / Hunyuan request_id）
2. 重启后查询远程 API 状态，而不是重新生成

### 后续改进方向
- [ ] 为 gen3d 任务增加远程状态查询（Tripo/Hunyuan API）
