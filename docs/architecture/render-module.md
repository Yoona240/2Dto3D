# 渲染模块说明文档

本文档详细说明渲染模块的组成、依赖关系、封装方式和使用方法，便于在其他项目中复用。

## 📁 文件清单

渲染模块涉及以下文件：

```
2d3d_v2/
├── core/render/                    # 核心渲染逻辑
│   ├── __init__.py                 # 模块入口（当前为空）
│   └── blender_script.py           # ⭐ Blender Python 脚本生成器
│
├── scripts/                        # CLI 入口脚本
│   ├── bpy_render_standalone.py    # ⭐ 独立渲染进程（subprocess 隔离）
│   ├── run_render_batch.py         # 批量渲染调度器
│   ├── render_views.py             # 单模型渲染 CLI
│   └── batch_process.py            # 统一批处理入口（含 render 子命令）
│
├── utils/                          # 工具模块
│   ├── blender.py                  # ⭐ Blender 路径发现 & 后端选择
│   └── config.py                   # 配置加载器
│
└── config/
    └── config.yaml                 # 渲染参数配置
```

### 核心文件说明

| 文件 | 职责 | 是否必须 |
|------|------|----------|
| `core/render/blender_script.py` | 生成 Blender Python 渲染脚本（灯光、相机、6 视角、GPU 等） | ✅ 必须 |
| `scripts/bpy_render_standalone.py` | 在独立子进程中执行渲染，隔离 bpy 崩溃 | ✅ 必须 |
| `utils/blender.py` | 自动发现 Blender/bpy，选择渲染后端 | ✅ 必须 |
| `scripts/run_render_batch.py` | 批量渲染调度（调用上述模块） | 可选（封装时可替换） |
| `config/config.yaml` | 渲染参数（samples、lighting_mode 等） | 可选（可硬编码或自定义） |

---

## 🔗 依赖关系

```
batch_process.py (CLI 入口)
    └── run_render_batch.py (调度)
            ├── utils/blender.py (后端选择)
            │       └── 检测 bpy 或 Blender 可执行文件
            │
            └── bpy_render_standalone.py (subprocess 执行)
                    └── core/render/blender_script.py (生成脚本)
                            └── 生成完整的 Blender Python 代码
```

**关键设计**：
- `bpy_render_standalone.py` 在**独立子进程**中运行，即使 bpy 崩溃（segfault）也不会影响主进程。
- `blender_script.py` 生成的脚本是**纯字符串**，可以传给 bpy 的 `exec()` 或 Blender subprocess。

---

## 🎨 渲染参数

在 `config/config.yaml` 的 `render` 段配置：

```yaml
render:
  blender_path: null        # null = 自动检测
  use_bpy: true             # 优先使用 pip install bpy
  device: "auto"            # auto, OPTIX, CUDA, CPU
  image_size: 512           # 输出图像尺寸
  samples: 64               # Cycles 采样数（越高越慢越清晰）
  rotation_z: 0             # 模型 Z 轴旋转校正
  lighting_mode: "ambient"  # 推荐 ambient（稳定），可选 flat/studio
```

### lighting_mode 说明

| 模式 | 特点 | 适用场景 |
|------|------|----------|
| `ambient` | 纯环境光，无方向光 | ⭐ 数据集渲染（稳定、所有视角一致） |
| `flat` | 环境光 + 6 盏无阴影太阳灯 | 需要轻微立体感，但高反射材质可能顶视角爆白 |
| `studio` | 传统三点布光（有阴影） | 产品展示、单张渲染 |

> **经验**：若出现"只有 top 视角爆白"，切换为 `ambient` 即可解决。

---

## 📦 封装建议

### 最小化独立模块（推荐）

若要在其他项目复用，只需复制以下文件：

```
your_project/
├── render/
│   ├── __init__.py              # 导出 render_glb_to_views()
│   ├── blender_script.py        # 从 core/render/ 复制
│   ├── bpy_render_standalone.py # 从 scripts/ 复制
│   └── blender_utils.py         # 从 utils/blender.py 复制
```

### 封装后的统一接口

创建 `render/__init__.py`：

```python
"""
GLB 多视角渲染模块

Usage:
    from render import render_glb_to_views
    
    render_glb_to_views(
        glb_path="/path/to/model.glb",
        output_dir="/path/to/output",
        image_size=512,
        samples=64,
        lighting_mode="ambient"
    )
"""

import subprocess
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).parent


def render_glb_to_views(
    glb_path: str,
    output_dir: str,
    image_size: int = 512,
    samples: int = 64,
    rotation_z: float = 0,
    lighting_mode: str = "ambient",
    timeout: int = 600
) -> list[Path]:
    """
    渲染 GLB 模型为 6 个标准视角图像。
    
    Args:
        glb_path: GLB 模型文件路径
        output_dir: 输出目录（会生成 front.png, back.png, ...）
        image_size: 输出图像尺寸（正方形）
        samples: Cycles 采样数（64-128 推荐）
        rotation_z: Z 轴旋转校正（度）
        lighting_mode: 灯光模式 (ambient/flat/studio)
        timeout: 渲染超时（秒）
    
    Returns:
        生成的图像路径列表
        
    Raises:
        RuntimeError: 渲染失败时抛出
    """
    standalone_script = MODULE_DIR / "bpy_render_standalone.py"
    
    cmd = [
        sys.executable,
        str(standalone_script),
        str(glb_path),
        str(output_dir),
        "--image-size", str(image_size),
        "--samples", str(samples),
        "--rotation-z", str(rotation_z),
        "--lighting", lighting_mode
    ]
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout
    )
    
    # 检查成功标志
    if "[Success]" not in (result.stdout or ""):
        error_lines = [
            line for line in (result.stderr or "").split('\n')
            if line.strip() and '| INFO:' not in line and '| WARNING:' not in line
        ]
        raise RuntimeError(f"Render failed: {' '.join(error_lines)}")
    
    # 返回生成的文件
    output_path = Path(output_dir)
    view_names = ["front", "back", "left", "right", "top", "bottom"]
    return [output_path / f"{name}.png" for name in view_names]
```

### 修改 bpy_render_standalone.py 的路径引用

复制后需要调整 `bpy_render_standalone.py` 中的 import：

```python
# 原来（相对于 2d3d_v2 项目）
from core.render.blender_script import generate_2d_render_script

# 改为（相对于 render 模块）
from .blender_script import generate_2d_render_script
```

或者直接把脚本改成接收脚本内容作为参数，解耦依赖。

---

## 🚀 使用示例

### 方式一：作为独立模块使用

```python
from render import render_glb_to_views

# 渲染单个模型
views = render_glb_to_views(
    glb_path="models/chair.glb",
    output_dir="output/chair_views",
    image_size=1024,
    samples=128,
    lighting_mode="ambient"
)

print(f"Generated {len(views)} views:")
for v in views:
    print(f"  - {v}")
```

### 方式二：直接调用脚本（无需封装）

```bash
# 基本用法
python bpy_render_standalone.py model.glb ./output

# 自定义参数
python bpy_render_standalone.py model.glb ./output \
    --image-size 1024 \
    --samples 128 \
    --lighting ambient
```

### 方式三：在 Web 服务中使用

```python
from flask import Flask
from render import render_glb_to_views
import threading

app = Flask(__name__)

@app.route('/render/<model_id>')
def render_model(model_id):
    # subprocess 隔离，即使 bpy 崩溃也不会影响 Flask
    try:
        views = render_glb_to_views(
            glb_path=f"models/{model_id}/model.glb",
            output_dir=f"output/{model_id}/views"
        )
        return {"status": "success", "views": [str(v) for v in views]}
    except Exception as e:
        return {"status": "error", "message": str(e)}
```

---

## ⚠️ 注意事项

### 1. bpy 模块安装

```bash
pip install bpy
```

首次运行会下载 Blender 核心组件（~200MB），需要等待。

### 2. GPU 渲染

脚本会自动检测 GPU（OptiX > CUDA > HIP > Metal > CPU）。确保：
- NVIDIA 驱动已安装
- 对于 OptiX，需要 RTX 系列显卡

### 3. 并发限制

GPU 渲染时建议 `concurrency=1`，避免 OOM。CPU 渲染可适当提高。

### 4. subprocess 隔离的必要性

**不要在主进程中直接 `import bpy` 并循环渲染**：
- bpy 状态难以完全重置
- 崩溃会直接终止主进程
- 内存可能累积泄漏

始终使用 `subprocess` 隔离每次渲染。

---

## 📝 输出格式

渲染输出 6 个 PNG 文件：

```
output_dir/
├── front.png    # 正面视图 (Y-)
├── back.png     # 背面视图 (Y+)
├── left.png     # 左侧视图 (X+)
├── right.png    # 右侧视图 (X-)
├── top.png      # 俯视图 (Z+)
└── bottom.png   # 仰视图 (Z-)
```

- 格式：PNG（RGBA 或 RGB，取决于 `film_transparent` 设置）
- 尺寸：`image_size × image_size`（正方形）
- 相机：正交投影，自动适配模型边界

---

## 🔧 自定义扩展

### 添加新视角

修改 `blender_script.py` 中的 `views_data`：

```python
views_data = [
    ("front",  (0, -2, 0), (90, 0, 0)),
    ("back",   (0, 2, 0), (90, 0, 180)),
    # ... 现有视角
    ("front_45", (1.4, -1.4, 0.5), (80, 0, 45)),  # 新增 45° 视角
]
```

### 自定义灯光

在 `blender_script.py` 的 `lighting_mode` 分支中添加新模式，或修改现有模式的灯光参数。

### 透明背景

设置 `bpy.context.scene.render.film_transparent = True`。

---

## 更新历史

- **2026-01-30**: 初始版本，从 2d3d_v2 项目提取
