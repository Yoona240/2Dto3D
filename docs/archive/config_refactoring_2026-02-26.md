# 配置系统重构文档

**日期**: 2026-02-26  
**版本**: V2  
**状态**: ✅ 已完成

## 📋 重构目标

1. **消除配置重复**：统一 OneAPI Gateway 配置，避免多处重复 API key 和 base_url
2. **清晰的层次结构**：按功能分类（文本/图像/3D），易于理解和维护
3. **删除冗余配置**：移除 `newapi` 和 `openrouter`（项目中未实际使用）
4. **保持向后兼容**：确保所有现有代码无需修改即可运行

## 🔄 配置结构变化

### 旧配置结构（已废弃）

```yaml
# 多个独立的 API 配置，重复 API key 和 base_url
qh_mllm:
  api_key: "sk-xxx"
  base_url: "https://oneapi.qunhequnhe.com/v1"
  
qh_image:
  api_key: "sk-xxx"  # 重复
  base_url: "https://oneapi.qunhequnhe.com"  # 重复
  
gemini_response:
  api_key: "sk-xxx"  # 重复
  base_url: "https://oneapi.qunhequnhe.com"  # 重复
  
multiview_edit:
  api_key: "sk-xxx"  # 重复
  base_url: "https://oneapi.qunhequnhe.com"  # 重复
  
doubao_image:
  api_key: "sk-xxx"  # 重复
  base_url: "https://oneapi.qunhequnhe.com"  # 重复

newapi:  # 未使用
  api_key: "sk-yyy"
  base_url: "https://newapi.pockgo.com/v1"

openrouter:  # 未使用
  api_key: "sk-zzz"
  base_url: "https://openrouter.ai/api/v1"
```

### 新配置结构（V2）

```yaml
# 统一的 OneAPI Gateway 配置
oneapi:
  api_key: "sk-xxx"  # 只配置一次
  base_url: "https://oneapi.qunhequnhe.com"  # 只配置一次
  timeout: 120
  max_retries: 3
  
  # 文本模型配置
  text_models:
    gpt-5:
      temperature: 1.0
      max_tokens: 40000
    gemini-3-flash-preview:
      temperature: 1.0
      max_tokens: 40000
  
  # 图像模型配置
  image_models:
    gemini-2.5-flash-image:
      api_type: "response"
      size: "1024x1024"
      n: 1
      poll_interval: 10
      max_wait_time: 600
    # ... 其他图像模型
  
  # 3D 生成模型配置
  gen3d_models:
    hunyuan-3d-pro:
      api_type: "response"
      output_format: "GLB"
      pbr: true
      face_count: 1000000
      # ... 其他参数

# 任务配置（指定使用哪个 provider 和模型）
tasks:
  text_generation:
    provider: "oneapi"
    model: "gpt-5"
  
  image_generation:
    provider: "oneapi"
    model: "gemini-2.5-flash-image"
  
  image_editing:
    provider: "oneapi"
    model: "gemini-2.5-flash-image"
  
  multiview_editing:
    provider: "oneapi"
    model: "gemini-3-pro-image-preview"
  
  gen3d:
    provider: "tripo"  # 或 "oneapi" (for hunyuan)
    model: "v3.1-20260211"

# 外部 3D API（直接连接，不通过 OneAPI）
tripo:
  api_key: "tsk-xxx"
  base_url: "https://api.tripo3d.ai/v2/openapi"
  # ... Tripo 特定配置

rodin:
  api_key: "your_rodin_api_key_here"
  base_url: "https://api.hyper3d.ai/api/v2"
  # ... Rodin 特定配置
```

## 🔧 代码变更

### 1. 新增文件

- `config/config.py` (V2 重构版)
- `docs/config_refactoring_2026-02-26.md` (本文档)

### 2. 备份文件

- `config/config.py.backup` (旧版配置解析模块)
- `config/config.yaml.backup` (旧版配置文件)

### 3. 更新文件

- `config/config.yaml` (新配置结构)
- `config/__init__.py` (更新导出)
- `utils/config.py` (重新导出，保持向后兼容)

## ✅ 向后兼容性

新配置系统完全向后兼容，所有现有代码无需修改：

### 兼容的属性访问

```python
from config.config import load_config

config = load_config()

# 旧代码仍然可以使用这些属性
config.qh_mllm          # ✅ 自动从 oneapi 生成
config.qh_image         # ✅ 自动从 oneapi 生成
config.gemini_response  # ✅ 自动从 oneapi 生成
config.multiview_edit   # ✅ 自动从 oneapi 生成
config.doubao_image     # ✅ 自动从 oneapi 生成
config.hunyuan          # ✅ 自动从 oneapi 生成

config.text_gen         # ✅ 映射到 tasks["text_generation"]
config.image_gen        # ✅ 映射到 tasks["image_generation"]
config.gen_3d           # ✅ 映射到 tasks["gen3d"]

config.tripo            # ✅ 保持不变
config.rodin            # ✅ 保持不变
config.render           # ✅ 保持不变
config.concurrency      # ✅ 保持不变
```

### 兼容的方法调用

```python
# 旧代码仍然可以使用这些方法
text_config = config.get_text_provider_config()    # ✅ 返回 QnMllmConfig
image_config = config.get_image_provider_config()  # ✅ 返回 ImageApiConfig
gen3d_config = config.get_3d_provider_config()     # ✅ 返回 TripoConfig/HunyuanConfig
```

## 📊 配置对比

| 项目 | 旧配置 | 新配置 | 说明 |
|------|--------|--------|------|
| API Key 重复次数 | 6次 | 1次 | 消除重复 |
| Base URL 重复次数 | 6次 | 1次 | 消除重复 |
| 配置文件行数 | ~300行 | ~250行 | 减少 17% |
| 未使用的配置 | newapi, openrouter | 已删除 | 清理冗余 |
| 配置层次 | 扁平 | 分层 | 更清晰 |

## 🎯 优势

1. **维护性提升**：
   - API key 只需在一处修改
   - 添加新模型只需在对应的 models 段添加配置
   - 配置结构清晰，易于理解

2. **可扩展性**：
   - 添加新的文本/图像/3D模型非常简单
   - 统一的配置格式，易于自动化处理

3. **安全性**：
   - 减少 API key 暴露点
   - 集中管理敏感信息

4. **向后兼容**：
   - 所有现有代码无需修改
   - 渐进式迁移，风险可控

## 🧪 测试验证

所有测试已通过：

```bash
# 配置加载测试
✓ Config loaded successfully
✓ OneAPI base_url: https://oneapi.qunhequnhe.com
✓ Text model: gpt-5
✓ Image model: gemini-2.5-flash-image
✓ 3D provider: tripo

# 向后兼容性测试
✓ qh_mllm.default_model: gpt-5
✓ qh_mllm.base_url: https://oneapi.qunhequnhe.com/v1
✓ gemini_response.model: gemini-2.5-flash-image
✓ multiview_edit.model: gemini-3-pro-image-preview
✓ get_text_provider_config(): gpt-5
✓ get_image_provider_config(): gemini-2.5-flash-image
✓ get_3d_provider_config(): TripoConfig
```

## 📝 迁移指南

### 对于用户

**无需任何操作**，配置系统自动向后兼容。

### 对于开发者

如果要添加新模型，请按以下步骤操作：

#### 添加文本模型

```yaml
oneapi:
  text_models:
    new-model-name:
      temperature: 1.0
      max_tokens: 40000
```

#### 添加图像模型

```yaml
oneapi:
  image_models:
    new-image-model:
      api_type: "response"
      size: "1024x1024"
      n: 1
      poll_interval: 10
      max_wait_time: 600
```

#### 添加3D模型

```yaml
oneapi:
  gen3d_models:
    new-3d-model:
      api_type: "response"
      output_format: "GLB"
      pbr: true
      face_count: 1000000
      generate_type: "Normal"
      polygon_type: "triangle"
      poll_interval: 30
      max_wait_time: 1800
```

#### 切换任务使用的模型

```yaml
tasks:
  text_generation:
    model: "new-model-name"  # 修改这里
  
  image_generation:
    model: "new-image-model"  # 修改这里
```

## 🔍 故障排查

### 问题：配置加载失败

**原因**：配置文件格式错误或缺少必需字段

**解决**：
1. 检查 YAML 语法是否正确
2. 确保所有必需字段都存在
3. 参考 `config/config.yaml` 的完整示例

### 问题：找不到模型配置

**原因**：任务配置中指定的模型在 oneapi 中不存在

**解决**：
1. 检查 `tasks.*.model` 是否在对应的 `oneapi.*_models` 中定义
2. 确保模型名称拼写正确

### 问题：API 调用失败

**原因**：API key 或 base_url 配置错误

**解决**：
1. 检查 `oneapi.api_key` 是否正确
2. 检查 `oneapi.base_url` 是否正确
3. 确认 API key 有足够的权限

## 📚 相关文档

- [AGENTS.md](../AGENTS.md) - 项目架构和开发规范
- [README.md](../README.md) - 用户使用指南
- [config/config.yaml](../config/config.yaml) - 配置文件示例

## 🎉 总结

本次重构成功实现了：

1. ✅ 消除配置重复（API key 和 base_url 只配置一次）
2. ✅ 清晰的配置层次（按功能分类）
3. ✅ 删除未使用的配置（newapi, openrouter）
4. ✅ 完全向后兼容（所有现有代码无需修改）
5. ✅ 提升可维护性和可扩展性

配置系统现在更加简洁、清晰、易于维护，为项目的长期发展奠定了良好的基础。
