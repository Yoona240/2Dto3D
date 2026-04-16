# 批量渲染实现指南

## 概述

本文档记录了在 Flask Web 服务中实现批量 Blender 渲染的过程，以及解决各种问题的方案。

## 技术栈

- **Flask**: Web 服务框架
- **bpy**: Blender Python 模块（`pip install bpy`）
- **Cycles**: Blender 的路径追踪渲染引擎
- **GPU**: NVIDIA RTX 4090 D × 4（OptiX）

## 语义定向对齐（2026-03-08 新增）

渲染入口 `scripts/run_render_batch.py` 新增语义定向流程（可配置开关）：

1. 首轮渲染到 `views/{provider_id}/_semantic_tmp/first_pass_views/`
2. 使用 VLM 判定语义 front（front-only，输入为固定 6 视角拼图）
3. 计算刚体旋转矩阵并通过 `scripts/bpy_align_standalone.py` 导出 aligned GLB
4. **Source 模型**：当 `semantic_front_from` 为 front/back/left/right 时，直接重映射 first-pass 视图（侧视图重命名 + top/bottom 图像旋转），跳过二次渲染；`webgl_params` 由 bpy 从 aligned GLB 包围盒直接计算。当 `semantic_front_from` 为 top/bottom 时 fallback 到二次渲染
5. **Target 模型**：使用 aligned GLB 重渲染最终 6 视角到 `views/{provider_id}/`（使用 source 的 `webgl_params` 保证 framing 一致）
6. 可选二次验证（失败即中断，不做静默回退）

元数据会写入 `views/{provider_id}/meta.json` 的 `semantic_alignment` 字段，包含：
- VLM 决策（semantic_front_from/confidence/reason）
- 旋转矩阵
- `source_glb` / `aligned_glb`
- `verify_passed`

前端展示仍读取 `views/{provider_id}/` 标准目录；语义对齐流程不会切换展示路径，只会覆盖该目录下同名视角图。

## 遇到的问题及解决方案

### 0. Top 视角“爆白/像蒙了一层雾”，切换为 ambient 正常 ✅（经验记录）

**现象**
- 6 视角里只有 `top` 视角出现大面积“全反光/变白/细节丢失”，其他视角正常。
- 这类问题在高反射材质（陶瓷、金属、玻璃、釉面等）更容易出现。

**快速验证（推荐的排查顺序）**
1. 将 `config/config.yaml` 中 `render.lighting_mode` 切换为 `ambient` 并强制重渲（`--force`）。
2. 若 `top` 立刻恢复细节，基本可以确认：问题主要来自“方向光/高光/反射叠加导致的顶视角高光压满”，而不是模型几何或贴图丢失。

**根因分析（为什么只影响 top）**
- `flat` 模式下使用了多盏 `SUN` 灯（平行光）。对于 `SUN` 灯：
  - **光照方向主要由灯的旋转（rotation）决定**，并非位置（location）。
  - 仅仅移动 `SUN` 灯的位置，往往无法改变高光反射进入相机的条件，因此“改位置不生效”是符合预期的。
- 顶视角相机正上往下看，上表面法线与主光方向/环境反射更容易对齐；对高反射材质，高光分量更容易把亮度顶到上限，细节被“冲掉”。
- 当 Color Management 设为 `Standard`（产品图更清晰）时，高光更容易发生硬剪裁（clipping），从而让 `top` 更明显地“爆白”。

**为什么 ambient 会“完美”**
- `ambient` 只使用世界环境光（World Background）照明，没有强方向性直射光，镜面高光不再被某个主光方向强行抬满。
- 对多视角数据集场景，`ambient` 往往更稳定、一致，特别适合做“几何/纹理可见性”优先的渲染。

**建议配置**
- 数据集渲染优先：`lighting_mode: ambient`（稳定、可复现，避免单视角爆白）
- 若需要更强立体感：再评估 `studio`，并确保方向光的旋转、强度、以及环境反射都被控制。

**备注**
- 若后续仍想保留 `flat`，建议在脚本中显式设置 `SUN` 灯的 `rotation_euler`（而不是仅设置 `location`），并考虑对 `top` 单独降低直射强度或使用更柔和的环境贴图。

### 1. 并发文件写入导致 JSON 损坏

**问题**: 多个线程同时写入 `tasks.jsonl` 文件，导致 `JSONDecodeError: Unterminated string`。

**解决方案**: 
- 使用 `threading.Lock()` 保护文件 I/O
- 使用原子写入：先写入临时文件，再用 `os.replace()` 替换

```python
tasks_file_lock = threading.Lock()

def save_task(task):
    with tasks_file_lock:
        # 写入临时文件
        temp_path = TASKS_FILE.with_suffix('.tmp')
        with open(temp_path, 'w') as f:
            # ... 写入内容
        # 原子替换
        os.replace(temp_path, TASKS_FILE)
```

### 2. API 限流 (429 Too Many Requests)

**问题**: Hunyuan 3D API 限制同时 10 个任务，超出会返回 429。

**解决方案**: 使用 `threading.Semaphore` 限制并发数

```python
GEN3D_SEMAPHORES = {
    "hunyuan": threading.Semaphore(10),
    "tripo": threading.Semaphore(5),
    "rodin": threading.Semaphore(3)
}
```

配置在 `config/config.yaml`:
```yaml
concurrency:
  gen3d:
    hunyuan: 10
    tripo: 5
    rodin: 3
  render: 1  # GPU 渲染建议设为 1
```

### 3. bpy 模块导致 Flask 进程崩溃 ⭐ 重要

**问题**: 批量渲染时，第一个模型渲染成功，第二个模型开始时 Flask 进程崩溃（Exit Code: 245）。

**原因分析**:
1. bpy 模块不是线程安全的
2. bpy 内部状态在连续渲染时可能污染
3. bpy 崩溃（segfault）会直接终止主进程

**解决方案**: 使用 **subprocess 隔离** bpy 渲染

创建独立渲染脚本 `scripts/bpy_render_standalone.py`:
```python
#!/usr/bin/env python3
"""独立的 bpy 渲染脚本，在子进程中运行"""

import argparse
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('glb_path')
    parser.add_argument('output_dir')
    parser.add_argument('--image-size', type=int, default=512)
    parser.add_argument('--samples', type=int, default=64)
    # ...
    
    args = parser.parse_args()
    
    import bpy
    # ... 执行渲染 ...
    
    try:
        exec(script_content, {'__name__': '__main__'})
        sys.exit(0)  # 明确成功退出
    except SystemExit as e:
        sys.exit(e.code if e.code is not None else 0)
    except Exception as e:
        print(f"Render error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
```

在主程序中用 subprocess 调用：
```python
def _run_bpy_subprocess(glb_path, output_dir, ...):
    cmd = [
        sys.executable,  # 使用相同的 Python 解释器
        str(PROJECT_ROOT / "scripts" / "bpy_render_standalone.py"),
        glb_path,
        output_dir,
        "--image-size", str(image_size),
        "--samples", str(samples),
        # ...
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    
    # 检查成功标志，而非仅依赖 returncode
    render_success = "[Success]" in result.stdout
    
    if result.returncode != 0 and not render_success:
        raise RuntimeError(f"Render failed: {error_msg}")
```

### 4. bpy 日志被误判为错误

**问题**: bpy 的 glTF 导入器将 INFO 日志输出到 stderr，被错误地当作失败。

**解决方案**: 过滤 INFO/WARNING 日志，只关注真正的错误

```python
# 检查 stdout 中是否有成功标志
render_success = "[Success]" in result.stdout

if result.returncode != 0 and not render_success:
    error_lines = []
    for line in result.stderr.split('\n'):
        # 跳过 INFO 和 WARNING 日志
        if '| INFO:' in line or '| WARNING:' in line:
            continue
        if line.strip():
            error_lines.append(line)
    
    error_msg = '\n'.join(error_lines)
    raise RuntimeError(f"Render failed: {error_msg}")
```

### 5. GPU 内存不足 (OOM)

**问题**: 多个渲染任务同时运行导致 GPU 内存耗尽。

**解决方案**:

1. **限制渲染并发数为 1**:
   ```yaml
   concurrency:
     render: 1  # 同时只运行一个渲染任务
   ```

2. **降低渲染参数**:
   ```yaml
   render:
     samples: 64      # 从 128 降低
     image_size: 512
   ```

3. **GPU 内存优化** (在 `blender_script.py`):
   ```python
   # 使用更小的瓦片减少峰值内存
   bpy.context.scene.cycles.tile_size = 256
   
   # 启用持久数据避免重复加载
   bpy.context.scene.render.use_persistent_data = True
   
   # 限制纹理大小
   bpy.context.scene.cycles.texture_limit = '2048'
   ```

### 6. 前端 3D 模型加载过多

**问题**: models 页面自动加载所有 3D 模型，导致浏览器卡顿。

**解决方案**: 移除 IntersectionObserver 自动加载，改为点击按钮加载

```javascript
// 只有点击 "Load 3D" 按钮时才加载模型
function loadModelNow(btn) {
    const container = btn.closest('.lazy-model-container');
    const viewer = container.querySelector('model-viewer.lazy-model');
    if (viewer) hydrateModelViewer(viewer);
}

// 移除了 IntersectionObserver 自动加载逻辑
```

## 配置参考

### config/config.yaml

```yaml
render:
  backend: "webgl"        # blender 或 webgl
  image_size: 512
  rotation_z: 0
  semantic_alignment:
    enabled: true
    vlm_model: "gemini-3-flash-preview"
    min_confidence: 0.75
    verify_after_rerender: true
    save_aligned_glb: true
    aligned_glb_suffix: "_aligned"
    save_debug_assets: true
    temp_dir_name: "_semantic_tmp"
  blender:
    blender_path: null
    use_bpy: true
    device: "auto"
    samples: 64
    lighting_mode: "emit"
  webgl:
    chrome_path: null
    environment_image: "neutral"
    shadow_intensity: 0.0
    use_gpu: true

concurrency:
  gen3d:
    hunyuan: 10
    tripo: 5
    rodin: 3
  render: 1               # GPU 渲染建议设为 1
  image: 5
```

## 文件结构

```
scripts/
├── run_render_batch.py       # 渲染任务入口，选择渲染后端
├── bpy_render_standalone.py  # 独立渲染脚本（subprocess 调用）
├── bpy_align_standalone.py   # GLB 语义对齐脚本（subprocess 调用）
└── ...

core/render/
├── __init__.py
├── blender_script.py         # 生成 Blender Python 脚本
└── semantic_view_aligner.py  # VLM 语义判定 + 矩阵计算
```

## 调试技巧

### 手动测试渲染

```bash
/home/xiaoliang/local_envs/2d3d/bin/python \
  scripts/bpy_render_standalone.py \
  data/pipeline/models_src/MODEL_ID/model.glb \
  /tmp/test_render \
  --image-size 512 --samples 64 --lighting flat 2>&1
```

### 查看任务状态

```bash
curl http://localhost:5001/api/tasks/list | jq
```

### 批量渲染测试

```bash
curl -X POST http://localhost:5001/api/batch/render \
  -H "Content-Type: application/json" \
  -d '{"model_ids": ["id1", "id2", "id3"]}'
```

## 注意事项

1. **不要在 Flask 主进程中直接 import bpy** - bpy 初始化后状态难以重置
2. **subprocess 隔离是关键** - 每个渲染在独立进程中运行
3. **检查 stdout 中的成功标志** - 不要仅依赖 returncode
4. **过滤 bpy 的 INFO 日志** - 它们会输出到 stderr
5. **GPU 渲染限制并发为 1** - 避免 OOM
6. **明确调用 sys.exit(0)** - 确保成功时返回正确的退出码

## 更新历史

- 2026-01-25: 初始版本，解决批量渲染崩溃问题
