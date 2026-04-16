# HTTP 与 API 客户端使用指南

## 概述

本项目提供三层统一的客户端模块，实现层次隔离和封装：

| 用途 | 模块 | 工厂函数 | 默认模型 |
|------|------|----------|----------|
| **文本生成** | `utils/llm_client.py` | `get_llm_client()` | gpt-5 |
| **图像生成/编辑** | `utils/image_client.py` | `get_image_client()` | gemini-2.5-flash-image |
| **3D 生成** | `utils/gen3d_client.py` | `get_3d_client()` | hunyuan-3d-pro |

所有客户端都支持零配置调用，自动加载 `config.yaml` 配置。

---

## 1. LLM 客户端 (`get_llm_client`)

用于文本生成、对话、VLM 等任务。

```python
from utils.llm_client import get_llm_client

# 零配置调用（推荐）
client = get_llm_client()

# 纯文本对话
response = client.chat(
    system_prompt="You are a helpful assistant.",
    user_prompt="Hello!",
)
print(response)

# 带图片的对话 (VLM)
response = client.chat_with_images(
    system_prompt="Describe this image.",
    user_prompt="What do you see?",
    images=["path/to/image.png"],
)

client.close()
```

### 内置特性
- ✅ **自动重试**: 连接失败自动重试 3 次（指数退避）
- ✅ **温度参数容错**: 某些模型不支持 temperature，会自动去掉重试
- ✅ **统一接口**: 支持 OpenAI、群核 OneAPI、NewAPI、OpenRouter 等
- ✅ **VLM 支持**: `chat_with_images()` 支持多模态输入

---

## 2. Image 客户端 (`get_image_client`)

用于图像生成和编辑任务。

```python
from utils.image_client import get_image_client

client = get_image_client()

# 文生图
result = client.generate_image(
    prompt="A dragon on white background, 3D render",
    output_path="output/dragon.png"
)

# 图像编辑
result = client.edit_image(
    image_path="input.png",
    instruction="Add golden wings to the character",
    output_path="output/edited.png"
)

client.close()
```

### 内置特性
- ✅ **统一入口**: 所有图像任务使用 `gemini-2.5-flash-image`
- ✅ **httpx + 重试**: 自动重试 3 次
- ✅ **自动保存**: 支持 base64 和 URL 响应自动保存

---

## 3. 3D 客户端 (`get_3d_client`)

用于 Image-to-3D 生成任务。

```python
from utils.gen3d_client import get_3d_client

# 默认使用 hunyuan
client = get_3d_client()

# 或指定 provider
client = get_3d_client(provider="tripo")

# 完整生成流程（提交 → 轮询 → 下载）
result = client.generate(
    image_path="input.png",
    output_path="output/model.glb"
)

# 或分步操作
task_id = client.submit_task("input.png")
status = client.poll_status(task_id)
client.download_result(task_id, "output/model.glb")

client.close()
```

### 支持的 Provider
- `hunyuan` - 混元 3D（默认）
- `tripo` - Tripo 3D
- `rodin` - Rodin

---

## 统一导入

所有客户端都可以从 `utils` 直接导入：

```python
from utils import get_llm_client, get_image_client, get_3d_client

# 所有客户端都支持零配置调用
llm = get_llm_client()
img = get_image_client()
gen3d = get_3d_client()
```

---

## 迁移状态

| 模块 | 状态 | 说明 |
|------|------|------|
| `core/image/caption.py` | ✅ 已迁移 | 使用 `get_llm_client` |
| `core/image/prompt_optimizer.py` | ✅ 已迁移 | 使用 `get_llm_client` |
| `core/image/editor.py` | ✅ 已添加重试 | httpx + HTTPTransport(retries=3) |
| `core/image/generator.py` | ✅ 已添加重试 | httpx + HTTPTransport(retries=3) |
| `core/image/response_editor.py` | ✅ 已添加重试 | httpx + HTTPTransport(retries=3) |
| `core/gen3d/hunyuan.py` | ✅ 已添加重试 | httpx + HTTPTransport(retries=3) |
| `core/gen3d/tripo.py` | ✅ 已有重试 | 原本就有 HTTPTransport(retries=3) |

---

## 常见问题

### Q: 应该用哪个客户端？

- 需要**文本输出** → `get_llm_client()`
- 需要**图像输出** → `get_image_client()`
- 需要**3D 模型输出** → `get_3d_client()`

### Q: 如何指定自定义配置？

```python
from utils.config import load_config

config = load_config()

# 指定配置
llm = get_llm_client(config.qh_mllm)
img = get_image_client(config.qh_image)
gen3d = get_3d_client(provider="tripo", config=config)
```


---

## 编辑质检相关 API（Web 后端）

以下接口用于编辑质检失败样本的人工恢复与回退：

### 恢复误杀样本

```http
POST /api/models/{model_id}/edits/{edit_id}/restore
Content-Type: application/json

{
  "reviewer": "ui",
  "reason": "manual restore from UI"
}
```

- 作用：将失败编辑标记为人工通过（`manual_override.approved=true`）
- 结果：该编辑会重新出现在 Edited Versions，并允许后续生成 3D

### 取消恢复（打回失败）

```http
POST /api/models/{model_id}/edits/{edit_id}/unrestore
Content-Type: application/json

{
  "reviewer": "ui",
  "reason": "manual restore revoked"
}
```

- 作用：撤销人工恢复标记
- 结果：该编辑会回到 Failed Editing，并再次受质量门控限制

---

## LPIPS 与 Mask 维护 API（Web 后端）

以下接口用于模型级 LPIPS 重算与历史 mask 补算。

### 单模型刷新 LPIPS

```http
POST /api/models/{model_id}/refresh-lpips
Content-Type: application/json
```

- 作用：为指定 source model 下所有可刷新的 target/provider 重新执行 Stage-2 LPIPS。
- 失败条件：
  - `404`：模型不存在
  - `400`：无可刷新目标
  - `409`：已有该模型相关 LPIPS 刷新任务在 `pending/running`
  - 兼容性：旧路径 `POST /api/models/{model_id}/refresh-dreamsim` 仍保留可用

### 全量刷新 LPIPS

```http
POST /api/models/refresh-lpips-all
Content-Type: application/json

{
  "model_ids": ["137cd60ec929", "319f0060ab82"]
}
```

- 作用：按全量或指定 model 列表刷新 Stage-2 LPIPS。
- 说明：
  - `model_ids` 可选；不传表示扫描全部 source model。
  - 当已有 LPIPS 刷新任务在运行时会返回 `409`，避免重复并发刷新。
  - 兼容性：旧路径 `POST /api/models/refresh-dreamsim-all` 仍保留可用

### 单模型补齐缺失 Mask

```http
POST /api/models/{model_id}/materialize-missing-masks
Content-Type: application/json
```

- 作用：为该 source model 下所有缺失 mask 的 edit batch 触发补算。
- 行为约束：
  - 仅补算 mask（`*_mask.png` + `edit_mask_grid.png`）。
  - 非 mask 关键资产缺失（如 `before_image_grid.png`、`target_image_grid.png`）时显式失败，不做兜底。
  - 同一模型若已有 mask 补算任务在运行，返回 `409`。
