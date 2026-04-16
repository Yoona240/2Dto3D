# Tripo 视角选择与映射开关

## 配置参数

在 `config/config.yaml` 的 `tripo` 部分添加了一个新参数：

```yaml
tripo:
  # Enable view selection + geometric remapping (default: false for backward compatibility)
  enable_view_selection: false       # true = use entropy_edge + geometric mapping
                                    # false = use legacy front/back/left/right directly
```

## 两种模式对比

### 1. Legacy 模式 (默认, enable_view_selection: false)

- **行为**: 直接使用 front/back/left/right 四个视角
- **旋转**: 无旋转 (全部为 0°)
- **适用场景**: 大多数常规情况

```
Slot Assignment:
  front  <- front  (rotate:   0°)
  left   <- left   (rotate:   0°)
  back   <- back   (rotate:   0°)
  right  <- right  (rotate:   0°)
```

### 2. 视角选择 + 映射模式 (enable_view_selection: true)

- **行为**: 
  1. 使用 `entropy_edge` 策略选择信息最丰富的 4 个视角
  2. 应用几何映射将选中的视角映射到 Tripo 的 4 个 slot
  3. 根据映射关系应用旋转
- **适用场景**: 当某些视角（如 top/bottom）比 front/back 包含更多信息时

```
Case 1: 删除 top/bottom (保留 front/back/left/right)
  front  <- front  (0°)
  back   <- back   (0°)
  left   <- left   (0°)
  right  <- right  (0°)

Case 2: 删除 front/back (保留 top/bottom/left/right)
  front  <- top    (0°)
  back   <- bottom (180°)
  left   <- left   (270°)
  right  <- right  (90°)

Case 3: 删除 left/right (保留 front/back/top/bottom)
  front  <- front  (270°)
  back   <- back   (90°)
  left   <- bottom (270°)
  right  <- top    (90°)
```

## 配置修改方法

### 方法 1: 修改 config.yaml (推荐)

```bash
# 编辑配置文件
vim config/config.yaml

# 修改 tripo.enable_view_selection
tripo:
  enable_view_selection: true   # 启用视角选择
  # 或
  enable_view_selection: false  # 使用 legacy 模式 (默认)
```

### 方法 2: 运行时通过环境变量 (需要额外实现)

当前不支持，如需支持可在代码中添加环境变量读取逻辑。

## 测试命令

### 测试 Legacy 模式 (默认)
```bash
python tests/test_view_selection_toggle.py --model 0255e658900d_edit_135b8a33
```

### 测试视角选择模式
```bash
python tests/test_view_selection_toggle.py --model 0255e658900d_edit_135b8a33 --enable-selection
```

### 批量测试
```bash
# 测试所有模型，使用当前配置
python tests/test_tripo_view_selection.py --all
```

## 配置位置

### 集中配置区域

所有视角映射的配置都在 `scripts/gen3d.py` 中的 `TRIPO_VIEW_MAPPING_CONFIG`：

```python
TRIPO_VIEW_MAPPING_CONFIG: Dict[str, Dict] = {
    "top_bottom": {    # Case 1: 删除上下视角
        "mapping": {...},
        "rotations": {...},
        "virtual_up": "top",
    },
    "front_back": {    # Case 2: 删除前后视角
        "mapping": {...},
        "rotations": {...},
        "virtual_up": "bottom",
    },
    "left_right": {    # Case 3: 删除左右视角
        "mapping": {...},
        "rotations": {...},
        "virtual_up": "left",
    },
}
```

## 调试技巧

### 1. 查看当前配置
```bash
python scripts/debug_tripo_mapping.py --config
```

### 2. 预览特定 case
```bash
python scripts/debug_tripo_mapping.py --case front_back --preview
```

### 3. 干运行测试 (不调用 API)
```bash
python tests/test_gen3d_dry_run.py 0255e658900d_edit_135b8a33
```

## 默认行为

- **默认**: `enable_view_selection: false` (Legacy 模式)
- **原因**: 保持向后兼容性，现有代码无需修改即可正常工作
- **建议**: 对于新项目或特定场景，可设置为 `true` 以获得更好的视角选择效果

## 相关代码文件

1. `config/config.yaml` - 主配置文件
2. `config/config.py` - 配置解析
3. `scripts/gen3d.py` - 视角选择和映射逻辑
4. `scripts/batch_process.py` - 批量处理入口
