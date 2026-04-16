# 快速参考指南 - 关键功能查询表

## 🎯 按功能快速定位

### 📊 数据模型 & 数据结构

#### 模型对象 (Model Object)
```python
{
    "id": "model_id",                    # 模型唯一标识
    "path": "data/pipeline/models_src/model_id/model.glb",
    "rendered_views": [...],             # 渲染视图列表
    "has_views": true,
    "edited_batches": [                  # 编辑批次
        {
            "edit_id": "edit_batch_1",
            "instruction": "make it red",
            "views": [...],              # 编辑后的视图
            "target_3d": {               # ← 生成的目标3D模型
                "id": "model_id_edit_edit_batch_1",
                "path": "...",
                "filename": "model.glb"
            },
            "created_at": "..."
        }
    ],
    "has_edits": true,
    "has_instructions": true
}
```

#### 编辑对对象 (Pair Object)
```python
{
    "id": "source_model_id",
    "source_3d": {
        "path": "data/pipeline/models_src/id/model.glb",
        "filename": "model.glb"
    },
    "edit_batches": [                    # 该源模型的所有编辑
        {
            "edit_id": "batch_id",
            "instruction": "...",
            "target_3d": {...},          # 目标3D或 null
            "created_at": "..."
        }
    ]
}
```

---

### 🔍 过滤条件详解

| 过滤器 | 条件 | 用途 |
|-------|------|------|
| `all` | 无条件 | 显示所有模型 |
| `has-instr` | `has_instructions == true` | 找有编辑指令的模型 |
| `no-instr` | `has_instructions != true` | 找无编辑指令的模型 |
| `has-views` | `has_views == true` | 找有渲染视图的模型 |
| `instr-no-views` | `has_instructions && !has_views` | 有指令但未渲染，**需渲染** |
| `has-edits` | `has_edits == true` | 找有编辑的模型 |
| `views-no-edits` | `has_views && !has_edits` | 有视图但未编辑，**需编辑** |
| **`edits-no-target`** ⭐ | `has_edits && 存在target_3d为null的batch` | **有编辑但缺Target 3D，需生成3D** |

### 🧪 实验维度过滤（Models 页面）

`Models` 页面除了状态过滤外，还支持两种互斥的实验过滤模式：

| 过滤模式 | 条件 | 用途 |
|---------|------|------|
| `all` | 不按实验来源过滤 | 浏览所有模型 |
| `provider_pair` | 按 `source_provider + target_provider` 过滤 | 查看某条实验链路产生的 source models |
| `yaml` | 按实验 YAML 路径过滤 | 查看某个实验计划产生的 source models |

说明：

- `provider_pair` 模式下可单独设置 `source provider`、`target provider`，也可同时指定两者。
- `yaml` 模式下会显示每个 YAML 对应的模型数与 run 数，便于回溯某个实验计划的产出。
- 实验过滤仍可继续叠加 `status`、`category` 与左侧搜索。

---

### 📁 文件系统映射

#### 目标3D模型命名规则

```
源模型ID: {source_id}
编辑ID: {edit_id}
↓
目标模型ID: {source_id}_edit_{edit_id}
目标模型目录: data/pipeline/models_src/{source_id}_edit_{edit_id}/
↓
例如:
  源: "model_abc123"
  编辑: "edit_batch_001"
  目标: "model_abc123_edit_edit_batch_001"
  目标路径: data/pipeline/models_src/model_abc123_edit_edit_batch_001/*.glb
```

---

## 🔧 常见操作

### 操作 1: 如何判断一个编辑是否有Target 3D?

**位置**: `app.py` 第 368-379 行 (get_all_models 函数)

```python
target_model_id = f"{d.name}_edit_{batch_dir.name}"
target_model_dir = MODELS_DIR / target_model_id
target_3d = None
if target_model_dir.exists():
    target_glbs = list(target_model_dir.glob("*.glb"))
    if target_glbs:
        target_3d = {...}
```

**步骤**:
1. 拼接目标模型ID: `{源ID}_edit_{编辑ID}`
2. 检查 `models_src/` 中是否存在该目录
3. 检查目录中是否有 `.glb` 文件
4. 有则构建 `target_3d` 对象，无则 `target_3d = None`

---

### 操作 2: 如何识别需要生成Target 3D的模型?

**前端 JavaScript** (models.html 第 867-869 行):

```javascript
case 'edits-no-target':
    return card.dataset.editsWithoutTarget === 'true';
```

**模板逻辑** (models.html 第 126-127 行):

```jinja2
{% set target_count = model.edited_batches | selectattr('target_3d') | list | length %}
{% set edits_without_target = (model.edited_batches|length > 0) and 
                               (target_count < model.edited_batches|length) %}
```

**含义**: 
- 计算有 `target_3d` 的编辑数
- 如果该数少于总编辑数，则标记为 `edits_without_target`
- 点击过滤器后，只显示这些模型

---

### 操作 3: 加载3D模型（Models页面）

**位置**: models.html 第 800-804 行

```javascript
function loadModelNow(btn) {
    const container = btn.closest('.lazy-model-container');
    if (!container) return;
    const viewer = container.querySelector('model-viewer.lazy-model');
    if (viewer) hydrateModelViewer(viewer);  // data-src → src
}
```

**工作流**:
1. 用户点击 "Load 3D" 按钮
2. 触发 `loadModelNow(btn)`
3. 从 `data-src` 属性读取模型路径
4. 设置 `src` 属性
5. Model Viewer 加载并显示3D模型

---

### 操作 4: 加载3D模型（Pairs页面）

**位置**: pairs.html 第 386-425 行

```javascript
function loadSource3D(pairId, sourcePath) {
    const container = document.getElementById(`source-container-${pairId}`);
    const viewerId = `source-viewer-${pairId}`;
    
    container.innerHTML = `
        <model-viewer id="${viewerId}" src="${sourcePath}" 
                     camera-controls auto-rotate></model-viewer>
    `;
}

function loadTarget3D(pairId, targetPath) {
    const container = document.getElementById(`target-container-${pairId}`);
    const viewerId = `target-viewer-${pairId}`;
    
    container.innerHTML = `
        <model-viewer id="${viewerId}" src="${targetPath}" 
                     camera-controls auto-rotate></model-viewer>
    `;
}
```

---

## 📡 API 端点速查

### 获取模型列表
```http
GET /models
返回: models.html 页面
```

### 获取编辑对
```http
GET /api/pairs
返回: 
[
  {
    "id": "model_id",
    "source_3d": {...},
    "edit_batches": [
      {"edit_id": "...", "target_3d": {...} or null, ...}
    ]
  }
]
```

### 获取单个模型详情
```http
GET /api/model/{model_id}
返回: 完整模型对象
```

### 获取 Experiment Stats 过滤选项
```http
GET /api/experiment-stats/options
返回:
{
  "provider_pairs": [
    {"source_provider": "hunyuan", "target_provider": "hunyuan", "label": "hunyuan -> hunyuan"}
  ]
}
```

### 获取 Provider Summary 统计
```http
GET /api/experiment-stats/category-summary?source_provider=hunyuan&target_provider=hunyuan
返回:
{
  "matched_experiments": ["20260318_xxx"],
  "partial_experiments": ["20260318_partial_xxx"],
  "category_stats": [...]
}
```

### 获取 YAML Results 选项与详情
```http
GET /api/experiment-stats/yaml-options
GET /api/experiment-stats/yaml-summary?plan_path=...
GET /api/experiment-stats/yaml-details?plan_path=...&experiment_id=...
```

说明：

- `partial_experiments` 表示该统计结果中包含了基于 partial records 恢复出的中断实验。
- `yaml-details` 同时返回 `category_stats`、`object_stats`、`edit_records`，用于前端 `YAML Results` 三块表格联动展示。

---

## 🎨 前端UI元素映射

### Models 页面组件

| 组件 | HTML元素 | 职责 |
|------|---------|------|
| 过滤栏 | `<div class="filter-bar">` | 状态过滤、实验过滤、Category 过滤与清空按钮 |
| 实验过滤模式 | `<select id="experimentFilterMode">` | 在 `All Models` / `Provider Pair` / `YAML` 间切换 |
| Provider Pair 过滤 | `<select id="sourceProviderFilterSelect">` + `<select id="targetProviderFilterSelect">` | 按实验链路过滤 source model |
| YAML 过滤 | `<select id="yamlFilterSelect">` | 按实验 YAML 过滤 source model |
| 排序控制 | `<button onclick="sortCards()">` | ID/时间排序 |
| 卡片网格 | `<div class="grid grid-3">` | 3列模型卡片 |
| 模型卡片 | `<div class="item-card">` | 单个模型预览 + 操作 |
| 懒加载按钮 | `<button onclick="loadModelNow()">` | "Load 3D" 按钮 |
| 3D查看器 | `<model-viewer>` | Google Model Viewer |

### Experiment Stats 页面组件

| 组件 | HTML元素 | 职责 |
|------|---------|------|
| Provider Pair 选择 | `<select id="providerPairSelect">` | 选择要聚合查看的 `source -> target` |
| YAML 选择 | `<select id="yamlSelect">` | 选择某个实验 YAML |
| Run 选择 | `<select id="yamlRunSelect">` | 选择单次 run 或查看该 YAML 下的聚合结果 |
| Category Summary 表 | `<table class="category-summary-table">` | 展示 category 统计，并支持按列排序 |
| Object Summary 表 | `<table id="yamlObjectTable">` | 展示 object 级汇总 |
| Edit Records 表 | `<table id="yamlEditTable">` | 展示 edit 级明细 |

说明：

- `Category Summary` 表支持点击列头排序，适合快速比较 `sample_count`、`stage1_failed_rate`、`stage2_entered_rate`、`stage2_lpips_mean` 等指标。
- 如果结果中包含中断实验恢复出的 partial records，页面顶部会明确提示。

### Pairs 页面组件

| 组件 | HTML元素 | 职责 |
|------|---------|------|
| 统计栏 | `<div class="stats-bar">` | 显示对数、编辑数、Target数 |
| 对容器 | `<div class="pair-item">` | 单个编辑对 |
| 源3D区 | `<div class="source-column">` | 源模型显示 |
| 目标区 | `<div class="target-column">` | 目标模型显示 |
| 导航控制 | `<button onclick="navigateEdit()">` | 上一个/下一个编辑 |

---

## 🧭 数据流查询

### 查询所有"Edits w/o Target 3D"的模型

**步骤**:
1. 调用 `get_all_models()` 获取所有模型
2. 对于每个模型，检查 `edited_batches`
3. 统计有 `target_3d` 的编辑数 `target_count`
4. 如果 `target_count < len(edited_batches)`，则该模型需要生成Target 3D

**示例**:
```python
models = get_all_models()
needs_target_3d = []

for m in models:
    if "_edit_" not in m["id"]:
        target_count = len([b for b in m.get("edited_batches", []) 
                           if b.get("target_3d")])
        total_edits = len(m.get("edited_batches", []))
        
        if total_edits > 0 and target_count < total_edits:
            needs_target_3d.append({
                "model_id": m["id"],
                "total_edits": total_edits,
                "without_target": total_edits - target_count
            })
```

---

## 📊 状态转换图

```
创建模型
  ↓
Source 3D (model_id)
  ↓
渲染视图
  ├─ has_views = true
  └─ rendered_views = [front, back, left, right, top, bottom]
  ↓
生成编辑指令 (instructions.json)
  ├─ has_instructions = true
  └─ instructions_count = N
  ↓
应用编辑指令
  ├─ has_edits = true
  └─ edited_batches[0] = {edit_id, instruction, views, target_3d: null}
  ↓
生成Target 3D
  ├─ 创建模型目录: {source_id}_edit_{edit_id}
  ├─ 生成目标3D: {source_id}_edit_{edit_id}/model.glb
  └─ 更新 edited_batches[0].target_3d = {...}
  ↓
完成编辑流程
  └─ 该模型不再出现在 "edits-no-target" 过滤器中
```

---

## 🐛 常见问题排查

### Q1: 过滤器显示不正确的模型

**排查步骤**:
1. 检查 `data-edits-without-target` 属性是否正确设置
   - 位置: models.html 第 132 行 HTML属性
2. 检查 `checkCardFilter()` 逻辑
   - 位置: models.html 第 867-869 行
3. 检查后端数据计算
   - 位置: app.py 第 126-127 行 (模板逻辑)

### Q2: 3D模型加载缓慢

**优化方案**:
1. 确认懒加载是否启用
   - models.html: 按需点击加载
   - pairs.html: Intersection Observer 自动加载
2. 检查 GLB 文件大小

### Q3: 目标3D显示为 "No Target 3D"

**原因分析**:
1. 目标模型目录不存在
   - 应为: `models_src/{source_id}_edit_{edit_id}/`
2. 目录存在但无 `.glb` 文件
3. 文件损坏或无效

---

## ✅ 测试检查清单

- [ ] 创建新编辑但不生成Target 3D
- [ ] 验证模型在 "Edits w/o Target 3D" 过滤器中出现
- [ ] 生成Target 3D后，验证模型从过滤器中移除
- [ ] 验证Pairs页面正确显示源3D和Target 3D
- [ ] 验证3D模型的懒加载功能
- [ ] 测试多编辑模型的导航控制
- [ ] 检查统计数据准确性

---

## 📚 相关文档

- **项目结构概览**：PROJECT_STRUCTURE_OVERVIEW.md
- **项目目标与设计**：项目目标与设计文档.md
- **任务恢复指南**：TASK_RECOVERY.md
- **代理指南**：AGENT.md
