# Tripo 多视角生成增强 - 需求文档

## 项目背景

我们正在构建一个 **2D-to-3D 编辑数据集流水线**，产出三元组数据：Source 3D 模型 + 编辑指令 → Target 3D 模型。

数据目录结构：
```
data/pipeline/
├── models_src/{id}/model.glb           # Source 3D
├── triplets/{id}/views/                # 6视角渲染图
│   ├── front.png, back.png, left.png
│   └── right.png, top.png, bottom.png
└── triplets/{id}/edited/{edit_id}/     # 编辑后视图
    ├── front.png, back.png, ...
    └── meta.json
```

## 已验证的事实

### 实验1：Tripo 6视角输入测试

**测试时间**：2026-02-25  
**API 端点**：`POST https://api.tripo3d.ai/v2/openapi/task`  
**Payload**：`{"type": "multiview_to_model", "files": [6个file_token]}`

**结果**：`400 Bad Request`，响应 `"code": 1004, "message": "One or more of your parameter is invalid"`

**结论**：Tripo `multiview_to_model` **强制限制4个视角**，不接受6视角输入。

---

## 需要解决的三个问题

### 问题1：编辑对一致性（最高优先级）

**需求**：Source 和 Target 3D 生成必须可复现，非编辑区域保持一致。

**现状**：未固定 Tripo 的 `model_seed` 和 `texture_seed`，每次生成结果随机。

**期望**：
- Source 3D 生成时固定 seed
- Target 3D（从 edited views 生成）复用 Source 的相同 seed
- Seed 值持久化到 `meta.json`

### 问题2：视角信息利用（中优先级）

**需求**：从6个渲染视角中选择最优的4个送入 Tripo。

**现状**：代码硬编码只使用 `front + back/left/right`，丢弃 `top/bottom`。

**关键洞察**：视角标签是相对的。Tripo 要求 `[front, left, back, right]`，但可以将 `top` 旋转后作为 `front` 传入。

**对小提琴类物体的意义**：
- `top` 视角包含琴弦、琴桥等关键结构，信息密度极高
- 将 `top` 旋转后映射到 Tripo slot，可利用这些信息

**期望**：
- 支持评估6视角的信息密度（如图像熵、边缘密度）
- 支持将 `top/bottom` 旋转/翻转后映射到任意 Tripo slot
- 可配置开关，允许回退到原始4侧面方案

结果：上下视角差异很大
/home/xiaoliang/2d3d_v2/data/pipeline/models_src/0255e658900d_edit_135b8a33/model_tp3.glb

### 问题3：API 参数暴露（低优先级）

**需求**：支持 Tripo 的所有可选参数。

**Tripo API 文档支持但当前代码未支持的参数**：
- `model_seed`: 几何生成种子
- `texture_seed`: 纹理生成种子
- `texture`: 是否生成纹理（默认true）
- `pbr`: 是否生成PBR材质（默认true）
- `texture_quality`: `detailed` / `standard`
- `texture_alignment`: `original_image` / `geometry`
- `face_limit`: 面数限制 (1000-20000)
- `export_uv`: 是否导出UV

---

## 关键代码位置

### 配置相关
- `config/config.yaml` - 配置文件
- `config/config.py` - `TripoConfig` dataclass

### 3D 生成
- `core/gen3d/tripo.py` - Tripo API 客户端
  - `submit_multiview_task()` 提交任务
  - `generate()` 完整流程
- `scripts/gen3d.py` - CLI 入口
  - `generate_3d()` 主函数
  - 处理 edited views 路径解析（`{model_id}_edit_{edit_id}`）
- `scripts/batch_process.py` - 批量处理
  - `gen3d_from_edit_single()` 从编辑视图生成 Target 3D

### 当前视角选择逻辑
位置：`scripts/gen3d.py` 第 88-96 行
```python
if is_edited_view:
    multi_view_images = []
    for view_name in ["back", "left", "right"]:  # 只收集这3个
        view_path = edited_dir / f"{view_name}.png"
        if view_path.exists():
            multi_view_images.append({"path": str(view_path), "view": view_name})
```

### 当前 API 调用
位置：`core/gen3d/tripo.py` 第 197-202 行
```python
payload = {
    "type": "multiview_to_model",
    "model_version": self.config.model_version,
    "files": files_list,  # [front, left, back, right] 4个
}
```

---

## 约束条件

1. **Fail Loudly 原则**：系统严禁掩盖错误，API 失败必须抛出异常
2. **元数据可追溯**：所有生成资产必须伴随 `meta.json`，记录 seed、配置等信息
3. **配置驱动**：关键参数通过 `config.yaml` 管理，支持环境变量覆盖
4. **向后兼容**：新增功能默认关闭或有合理的默认值，不影响现有流程

---

## 验收标准

1. **Seed 控制**：同 seed 两次生成同一模型，文件 MD5 相同或高度相似
2. **视角选择**：小提琴类物体的 `top` 视角信息能被有效利用
3. **参数完整**：Tripo 所有可选参数可通过配置控制
4. **可追溯**：`meta.json` 包含完整的生成参数记录

这是tripo的部分官方文档：
Request type: This field must be set to .multiview_to_model

model_version (Optional): Model version. Available versions are as below:

v3.1-20260211 v3.0-20250812 v2.5-20250123 v2.0-20240919 v1.4-20240625(Deprecated) If this option is not set, the default value will be .v2.5-20250123

files: Specifies the image inputs, this is a list contains following parameters. The list must contain exactly 4 items in the order [front, left, back, right]. You may omit certain input files by omitting the , but the front input cannot be omitted. Do not use less than two images to generate. The resolution of each image must be between 20 x 20px and 6000 x 6000px. The suggested resolution should be more than 256 x 256pxfile_token

type: Indicates the file type. Although currently not validated, specifying the correct file type is strongly advised. file_token: The identifier you get from upload, please read Docs/Upload. Mutually exclusive with and .urlobject url: A direct URL to the image. Supports JPEG and PNG formats with maximum size of 20MB. Mutually exclusive with and .file_tokenobject object (Strongly Recommended): The information you get from upload (STS), please read Docs/Upload (STS). Mutually exclusive with and . urlfile_token bucket: Normally should be .tripo-data key: The resource_uri got from session token. face_limit (Optional): Limits the number of faces on the output model. If this option is not set, the face limit will be adaptively determined. If , it should be 100020000, if as well, it should be 50010000.smart_low_poly=truequad=true

texture (Optional): A boolean option to enable texturing. The default value is , set to get a base model without any textures.truefalse

pbr (Optional): A boolean option to enable pbr. The default value is , set to get a model without pbr. If this option is set to , will be ignored and used as .truefalsetruetexturetrue

texture_seed (Optional): This is the random seed for texture generation. Using the same seed will produce identical textures. This parameter is an integer and is randomly chosen if not set. If you want a model with different textures, please use same and different .model_seedtexture_seed

texture_alignment (Optional): Determines the prioritization of texture alignment in the 3D model. The default value is .original_image

original_image: Prioritizes visual fidelity to the source image. This option produces textures that closely resemble the original image but may result in minor 3D inconsistencies. geometry: Prioritizes 3D structural accuracy. This option ensures better alignment with the model’s geometry but may cause slight deviations from the original image appearance. texture_quality (Optional): This parameter controls the texture quality. provides high-resolution textures, resulting in more refined and realistic representation of intricate parts. This option is ideal for models where fine details are crucial for visual fidelity. The default value is .detailedstandard

auto_size (Optional): Automatically scale the model to real-world dimensions, with the unit in meters. The default value is .false

orientation (Optional): Set to automatically rotate the model to align the original image. The default value is .orientation=align_imagedefault

quad (Optional): Set to enable quad mesh output. If and is not set, the default will be 10000.truequad=trueface_limitface_limit

Note: Enabling this option will force the output to be an FBX model. compress (Optional): Specifies the compression type to apply to the texture. Available values are:

geometry: Applies geometry-based compression to optimize the output, By default we use meshopt compression. smart_low_poly (Optional): Generate low-poly meshes with hand‑crafted topology. Inputs with less complexity work best. There is a possibility of failure for complex models. The default value is .false

generate_parts (Optional): Generate segmented 3D models and make each part editable. The default value is .false

Note: generate_parts is not compatible with or , if you want to generate parts, please set and ; generate_parts is not compatible with , if you want to generate parts, please set .texture=truepbr=truetexture=falsepbr=falsequad=truequad=false export_uv (Optional): Controls whether UV unwrapping is performed during generation. The default value is true. (When set to false, generation speed is significantly improved and model size is reduced. UV unwrapping will be handled during the texturing stage.)