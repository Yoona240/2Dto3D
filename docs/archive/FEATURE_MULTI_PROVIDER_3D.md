# 多 Provider 3D 模型生成支持

## 功能概述

改进了 3D 模型生成的 skip 判断逻辑，现在可以为同一个图像使用不同的 provider 生成多个 3D 模型，而不会被错误地跳过。

## 问题背景

之前的实现中，`has_3d_model()` 方法只检查是否存在任何 GLB 文件，导致：
- 如果已经用 tripo 生成了模型，再用 hunyuan 生成时会被跳过
- 无法为同一图像生成多个不同 provider 的 3D 模型进行对比

## 解决方案

### 1. 修改 `has_3d_model()` 方法

**位置**: `scripts/batch_process.py`

**改进**:
- 添加可选的 `provider` 参数
- 当指定 provider 时，检查特定 provider 的模型文件（如 `model_tp3.glb`、`model_hy3.glb`）
- 当不指定 provider 时，保持原有行为（检查是否存在任何 GLB 文件）

**实现**:
```python
def has_3d_model(self, image_id: str, provider: str = None) -> bool:
    """Check if image already has a 3D model.
    
    Args:
        image_id: The image ID to check
        provider: Optional provider name (tripo/hunyuan/rodin). 
                 If provided, checks for specific provider's model.
                 If None, checks if any GLB file exists.
    """
    model_dir = MODELS_DIR / image_id
    if not model_dir.exists():
        return False
    
    if provider:
        # Check for specific provider's model file
        from core.gen3d import get_model_id
        model_id = get_model_id(provider)
        pattern = f"model_{model_id}.glb"
        return any(model_dir.glob(pattern))
    else:
        # Check if any GLB file exists
        return any(model_dir.glob("*.glb"))
```

### 2. 更新 `generate_3d_single()` 方法

在检查是否跳过时，传入 provider 参数：

```python
# Check if already exists (for this specific provider)
if not force and self.has_3d_model(image_id, provider):
    from core.gen3d import get_model_id
    model_id = get_model_id(provider)
    self._record_result(image_id, "skipped", f"(already has {provider} 3D: model_{model_id}.glb)")
    return image_id, "skipped"
```

### 3. 更新 `batch_generate_3d()` 方法

在批量生成时，也检查特定 provider 的模型：

```python
# Filter if not forcing (check for specific provider's model)
if not force:
    skipped_ids = [i for i in all_ids if self.has_3d_model(i, provider)]
    # ...
    
print(f"Already have {provider} 3D (model_{model_id}.glb): {skipped_count}")
```

## Provider 模型文件命名规则

不同 provider 生成的模型文件名不同：

| Provider | Model ID | 文件名 |
|----------|----------|--------|
| tripo    | tp3      | model_tp3.glb |
| hunyuan  | hy3      | model_hy3.glb |
| rodin    | rd2      | model_rd2.glb |

这些映射定义在 `core/gen3d/__init__.py` 的 `MODEL_ID_MAP` 中。

## 使用示例

### 为同一图像生成多个 provider 的 3D 模型

```bash
# 先用 tripo 生成
python scripts/batch_process.py gen3d --provider tripo --ids 6010d3b2a963

# 再用 hunyuan 生成（不会被跳过）
python scripts/batch_process.py gen3d --provider hunyuan --ids 6010d3b2a963

# 再用 rodin 生成（不会被跳过）
python scripts/batch_process.py gen3d --provider rodin --ids 6010d3b2a963
```

### 输出示例

**之前的行为**（错误）:
```
============================================================
Batch 3D Generation
============================================================
Provider: hunyuan
Total images: 4
Already have 3D: 4  ← 错误：检测到任何 GLB 就跳过
To process: 0
============================================================
```

**现在的行为**（正确）:
```
============================================================
Batch 3D Generation
============================================================
Provider: hunyuan
Total images: 4
Already have hunyuan 3D (model_hy3.glb): 0  ← 正确：只检查 hunyuan 的模型
To process: 4
============================================================
```

## 文件结构示例

生成多个 provider 的模型后，目录结构如下：

```
data/pipeline/models_src/6010d3b2a963/
├── model_tp3.glb      # Tripo 生成的模型
├── model_hy3.glb      # Hunyuan 生成的模型
├── model_rd2.glb      # Rodin 生成的模型
└── meta.json          # 元数据（记录最后一次生成的信息）
```

## 注意事项

1. **meta.json 覆盖**: 每次生成都会更新 `meta.json`，只保留最后一次生成的元数据
2. **前端显示**: 前端目前只显示第一个找到的 GLB 文件，可能需要后续改进以支持多模型选择
3. **--force 标志**: 使用 `--force` 可以重新生成已存在的模型

## 后续改进方向

- [ ] 前端支持显示和切换多个 provider 的模型
- [ ] meta.json 支持记录多个 provider 的生成信息
- [ ] 添加模型对比功能

## 维护记录

**日期**: 2026-02-27
**修改人**: Kiro AI Assistant
**变更类型**: Bug Fix & Enhancement
**影响范围**: scripts/batch_process.py
