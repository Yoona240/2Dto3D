# 2D-3D 编辑数据集流水线 - 项目结构概览

> 快速参考：完整项目位于 `/home/xiaoliang/2d3d_v2`

## 📋 项目概览

**项目名称**：2D 到 3D 数据集流水线 (v2)  
**技术栈**：Python 3.11 + Flask + JavaScript (Vanilla)  
**核心功能**：文生图 → 图生3D → 多视角渲染 → 3D编辑指令生成 → 完整流水线管理

---

## 🏗️ 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      Flask Web UI (app.py)                   │
│         (负责路由、任务管理、API交互、页面渲染)              │
└─────────────────────────────────────────────────────────────┘
                              ↓
        ┌─────────────┬──────────────┬─────────────┐
        ↓             ↓              ↓             ↓
    前端页面    核心业务逻辑      数据存储      工具模块
   templates/   core/          data/         utils/
   static/      render/        pipeline/     config.py
               gen3d/                        llm_client.py
               image/                        image_api_client.py
```

---

## 📂 详细目录结构

### 1️⃣ **根目录**

| 文件 | 用途 |
|------|------|
| `app.py` (78KB) | **主应用入口** - Flask服务器，路由定义，任务管理 |
| `config/` | 配置管理（API Key、模型、并发控制） |
| `core/` | 核心业务逻辑模块 |
| `utils/` | 通用工具库 |
| `templates/` | HTML 模板 |
| `static/` | CSS、JavaScript、资源文件 |
| `data/` | 数据存储（.gitignore 隐藏） |
| `scripts/` | 独立 CLI 脚本 |

---

### 2️⃣ **前端 (Templates & Static)**

#### Templates (HTML)

```
templates/
├── base.html                      # 基础模板框架（导航、样式基础）
├── home.html                      # 首页
├── pairs.html                     # ⭐ Pair 页面：源3D和编辑后3D对比
├── models.html                    # ⭐ Models 页面：所有生成的3D模型
├── model_detail.html              # 单个模型详情页
├── model_detail_modals.html       # 模型详情页的模态框 (Target 3D、View等)
├── model_detail_scripts.html      # 模型详情页的JavaScript逻辑
├── images.html                    # 图像管理页面
├── prompts.html                   # Prompt 管理页面
└── tasks.html                     # 任务监控页面
```

**关键页面详解：**

##### **pairs.html** - 源-目标对比页面
- **功能**：显示源3D模型和编辑后的目标3D模型并排对比
- **关键组件**：
  - 左列：源3D模型查看器（model-viewer）
  - 右列：目标3D模型 + 编辑指令 + 预览图
  - 导航控制：当一个源模型有多个编辑结果时，支持切换
  - 统计信息：已编辑的模型数、编辑总数、有Target 3D的编辑数
- **API**：`/api/pairs` 获取所有编辑对
- **3D加载**：懒加载 + Intersection Observer 优化

##### **models.html** - 模型浏览与管理页面
- **功能**：浏览所有生成的3D模型，支持过滤、排序、批操作
- **关键组件**：
  - 卡片网格：3D模型预览（懒加载 + 点击加载）
  - 侧边栏TOC：快速导航、搜索、按 ID/Provider 排序
  - **过滤系统**（8个过滤器）：
    - `all` - 显示所有模型
    - `has-instr` - 有编辑指令
    - `no-instr` - 无编辑指令
    - `has-views` - 有渲染视图
    - `instr-no-views` - 有指令但无视图（需渲染）
    - `has-edits` - 有编辑结果
    - `views-no-edits` - 有视图但无编辑（需编辑）
    - **`edits-no-target`** ⭐ - **有编辑但无Target 3D**（需生成3D）
  - 排序：按 ID 或创建时间
  - 批操作：全选、批量渲染、批量编辑、批量生成3D、下载

#### Static (CSS & JavaScript)

```
static/
├── css/
│   └── style.css                  # 全局样式（Bootstrap 框架 + 自定义）
├── js/
│   ├── script.js                  # 通用 JavaScript（TOC、Tab切换、API等）
│   ├── modal_logic.js             # 模态框逻辑
│   └── download.js                # 文件下载逻辑
└── (model-viewer库通过CDN引入)     # Google Model Viewer for 3D preview
```

**关键JS函数：**
- `applyFilter(filter)` - models.html 的过滤器
- `checkCardFilter(card, filter)` - 检查卡片是否匹配过滤条件
- `hydrateModelViewer(viewer)` - 加载 model-viewer 元素
- `loadModelNow(btn)` - 点击"Load 3D"按钮时加载模型

---

### 3️⃣ **后端核心业务逻辑 (core/)**

```
core/
├── gen3d/                         # 3D 生成客户端
│   ├── base.py                    # Base3DGenerator 抽象类
│   ├── tripo.py                   # Tripo API 客户端
│   ├── hunyuan.py                 # Hunyuan API 客户端
│   ├── rodin.py                   # Rodin API 客户端
│   └── __init__.py
├── image/                         # 图像生成与编辑模块
│   ├── generator.py               # 文生图生成
│   ├── editor.py                  # 图像编辑（LLM 引导式编辑）
│   ├── guided_view_editor.py      # 视图引导编辑
│   ├── multiview_editor.py        # 多视图编辑
│   ├── view_stitcher.py           # 视图拼接
│   ├── view_analyzer.py           # 视图分析
│   ├── caption.py                 # Caption 生成
│   ├── processor.py               # 图像处理
│   ├── generate_prompts.py        # Prompt 生成
│   └── __init__.py
└── render/                        # 3D 模型渲染
    ├── blender_script.py          # Blender 渲染脚本
    ├── __init__.py
    └── (blender 脚本通过 subprocess 调用)
```

---

### 4️⃣ **Flask 应用 (app.py)**

**文件大小**：78KB | **主要职责**：路由、任务管理、API、数据操作

#### 关键函数

| 函数 | 功能 | 返回 |
|------|------|------|
| `get_all_models()` | 获取所有3D模型 | List[Dict] |
| `get_all_images()` | 获取所有图像 | List[Dict] |
| `get_all_prompts()` | 获取所有 prompt | List[Dict] |
| `api_get_pairs()` | 获取编辑对（含目标3D） | JSON |
| `create_task(type, params)` | 创建异步任务 | task_id |
| `process_task(task_id)` | 后台处理任务 | 更新 task_store |

#### 列表页 schema 兼容层（2026-03-29）

为避免旧版数据与实验数据混用时直接破坏 Web UI，`app.py` 的列表页读取链路增加了显式 normalization 层：

- `get_all_prompts()` 读取 `pipeline/prompts/*.jsonl` 后，不再把原始字典直接传给模板，而是先经过 `normalize_prompt_record()`。
- `get_all_images()` 在扫描 `images/*/meta.json` 时，统一经过 `normalize_image_record()`，生成页面稳定使用的 `display_subject`、`schema`、`created_at` 等字段。
- `get_all_models()` 明确只枚举 source model；`_edit_` target model 不再混入 source 列表。

这层兼容逻辑只支持当前项目内两种已知 prompt schema 与两种已知 image meta schema。若遇到未知 schema 或字段类型错误，系统会直接显性报错，而不是在模板层做静默 fallback。

#### 页面路由

```python
@app.route('/') → home()                      # 首页
@app.route('/models') → models_page()         # ⭐ 模型页
@app.route('/model/<id>') → model_detail_page()  # 模型详情
@app.route('/pairs') → pairs_page()           # ⭐ Pair 页面
@app.route('/api/pairs') → api_get_pairs()    # 获取编辑对
@app.route('/images') → images_page()
@app.route('/prompts') → prompts_page()
@app.route('/tasks') → tasks_page()
```

---

### 5️⃣ **"Edits w/o Target 3D" 过滤逻辑** ⭐

#### 数据标记 (Python 后端)

**位置**：`app.py` → `models_page()` 函数 (第 940-962 行)

```python
def models_page():
    models = get_all_models()
    
    for m in models:
        if "_edit_" in m["id"]:
            continue
        
        edit_ids = []
        for batch in m.get("edited_batches", []):
            edit_ids.append({
                "edit_id": batch.get("edit_id", ""),
                "has_target_3d": batch.get("target_3d") is not None  # ← 关键检查
            })
```

**条件判断**：

在模板中 (models.html line 126-127)：
```jinja2
{% set target_count = model.edited_batches | selectattr('target_3d') | list | length %}
{% set edits_without_target = (model.edited_batches|length > 0) and 
                               (target_count < model.edited_batches|length) %}
```

**含义**：
- 如果模型有编辑批次 (edited_batches)
- 且 **至少有一个编辑没有对应的目标3D** (target_3d is None)
- 则标记为 `data-edits-without-target="true"`

#### HTML 属性标记

```html
<div class="item-card" 
     data-model-id="{{ model.id }}"
     data-edits-without-target="{{ 'true' if edits_without_target else 'false' }}">
```

#### JavaScript 过滤逻辑

**位置**：models.html 第 867-869 行

```javascript
case 'edits-no-target':
    // Has edited views but some/all don't have Target 3D
    return card.dataset.editsWithoutTarget === 'true';
```

**工作流程**：
1. 用户点击 "Edits w/o Target 3D" 按钮
2. `applyFilter('edits-no-target')` 执行
3. 遍历所有卡片，检查 `card.dataset.editsWithoutTarget`
4. 隐藏不匹配的卡片 (`classList.add('filtered-out')`)
5. 显示匹配的卡片（有编辑但无对应Target 3D的模型）

---

### 6️⃣ **3D 模型加载**

#### Model-Viewer 集成

**HTML 元素**：
```html
<model-viewer 
    src="/path/to/model.glb"
    camera-controls
    auto-rotate
    shadow-intensity="1"
    environment-image="neutral">
</model-viewer>
```

#### 懒加载策略 (models.html)

```html
<!-- 初始状态：显示"Load 3D"按钮 -->
<div class="lazy-model-container">
    <model-viewer class="lazy-model" data-src="/path" ></model-viewer>
    <button onclick="loadModelNow(this)">Load 3D</button>
</div>
```

**JavaScript**：
```javascript
function loadModelNow(btn) {
    const container = btn.closest('.lazy-model-container');
    if (!container) return;
    const viewer = container.querySelector('model-viewer.lazy-model');
    if (viewer) hydrateModelViewer(viewer);  // data-src → src
}
```

#### Pairs 页面的 3D 加载

**位置**: pairs.html - 使用 Intersection Observer 进行优化：
```javascript
let observer = null;

function setupLazyLoading() {
    observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                loadSource3D(entry.target.id);  // 自动加载可见的模型
            }
        });
    });
}
```

---

### 7️⃣ **数据存储结构 (data/pipeline/)**

```
data/
└── pipeline/
    ├── models_src/
    │   └── {model_id}/
    │       ├── model_*.glb        # 3D 模型文件
    │       └── meta.json
    └── triplets/
        └── {model_id}/
            ├── views/             # 原始渲染视图
            │   ├── front.png
            │   ├── back.png
            │   └── ...
            └── edited/            # 编辑后的视图
                └── {edit_id}/
                    ├── front.png
                    ├── back.png
                    └── meta.json
```

**目标3D模型命名规则**：
```
源模型ID: {source_id}
编辑ID: {edit_id}
↓
目标模型ID: {source_id}_edit_{edit_id}
目标路径: data/pipeline/models_src/{source_id}_edit_{edit_id}/*.glb
```

---

## 🔄 关键数据流

### 流程 1: 获取所有模型 (Get All Models)

```
用户访问 /models
    ↓
models_page()
    ↓
get_all_models()
    ├─ 遍历 models_src/
    ├─ 检查 triplets/{id}/views/ (渲染视图)
    ├─ 检查 triplets/{id}/edited/ (编辑批次)
    │   └─ 检查 models_src/{id}_edit_{edit_id}/ (目标3D)
    └─ 返回完整模型列表
    ↓
models.html (前端)
    ├─ 计算 edits_without_target
    ├─ 渲染卡片 + 数据属性
    └─ 支持过滤、排序、批操作
```

### 流程 2: 查看编辑对 (Get Pairs)

```
用户访问 /pairs
    ↓
前端 JavaScript: loadPairs()
    ↓
/api/pairs (API)
    ├─ 遍历有 edited_batches 的模型
    ├─ 获取源3D路径
    ├─ 检查目标3D (models_src/{id}_edit_{edit_id}/)
    └─ 返回对列表
    ↓
前端渲染
    ├─ 源3D | 目标3D (并排)
    ├─ 编辑指令
    └─ 导航控制 (多编辑时)
```

### 流程 3: 应用过滤 (Apply Filter)

```
用户点击 "Edits w/o Target 3D" 按钮
    ↓
JavaScript: applyFilter('edits-no-target')
    ↓
checkCardFilter(card, 'edits-no-target')
    ├─ 读取 card.dataset.editsWithoutTarget
    └─ 返回 true/false
    ↓
更新 UI
    ├─ 隐藏/显示卡片
    ├─ 更新计数
    └─ 重置选择
```

---

## 🎯 关键文件位置速查表

| 需求 | 文件位置 | 行号 |
|------|---------|------|
| **过滤按钮定义** | templates/models.html | 98 |
| **过滤逻辑** | templates/models.html | 817-873 |
| **edits-no-target 检查** | templates/models.html | 867-869 |
| **数据标记** | templates/models.html | 126-127 |
| **模型数据收集** | app.py | 320-418 |
| **Pair API** | app.py | 997-1094 |
| **Models 页面** | app.py | 940-962 |
| **Pairs 页面逻辑** | templates/pairs.html | 322-417 |
| **3D 懒加载** | templates/models.html | 800-804 |

---

## 🔧 技术栈总结

| 层级 | 技术 | 版本 |
|------|------|------|
| **后端** | Python Flask | 3.11 |
| **前端** | HTML5 + CSS3 + Vanilla JS | - |
| **3D 显示** | Google Model Viewer | CDN |
| **任务管理** | Threading + JSONL | Python stdlib |
| **3D 生成** | Tripo/Hunyuan/Rodin API | 外部服务 |
| **图像编辑** | LLM 引导 + 图像处理 | core/image/ |
| **渲染** | Blender 4.0+ | 外部工具 |

---

## 📝 快速导航

- 🔗 **Pair 页面**：`templates/pairs.html`
- 🔗 **Models 页面**：`templates/models.html`
- 🔗 **过滤逻辑**：`templates/models.html` 867-869
- 🔗 **后端数据**：`app.py` get_all_models() 320-418
- 🔗 **3D加载**：`templates/models.html` 800-804
- 🔗 **API端点**：`app.py` routes (916+)
