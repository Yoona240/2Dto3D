https://cf.qunhequnhe.com/display/Search/Responses%20openai%20compatiable#Responsesopenaicompatiable-4.2.2doubao-3d
# 3D Generation APIs Documentation

This document provides research details for **Rodin Gen2**, **Hunyuan 3D v3**, and **Tripo 3.0** APIs.

---

## 1. Rodin Gen2 (Hyper3D)

Designed for high-fidelity 3D generation from text or images.

### Authentication
- **Method:** API Key in HTTP Header.
- **Format:** `Authorization: Bearer YOUR_RODIN_API_KEY`
- **Management:** [Hyper3D API Key Page](https://www.hyper3d.ai/)

### API Endpoint
- **Base URL:** `https://api.hyper3d.ai/api/v2/rodin` (Note: Endpoint might vary by platform like wavespeed or fal.ai)

### Core Parameters
- `tier`: Set to `Gen-2`.
- `prompt`: Text description for Text-to-3D.
- `images`: Image file(s) for Image-to-3D.
- `geometry_file_format`: `glb` (default), `usdz`, `fbx`, `obj`, `stl`.
- `quality`: `high`, `medium`, `low`.
- `TAPose`: Boolean (T-pose/A-pose generation).

### Workflow (Asynchronous)
1. **Submit Task:** `POST` request to `/api/v2/rodin`. Returns a `task_uuid`.
2. **Poll Status:** `GET` request using the `task_uuid`.
3. **Download:** Retrieve assets once status is `completed`.

---

## 2. Hunyuan 3D v3 (Tencent Cloud)

支持高面数、高质量的 3D 模型生成。目前采用**OpenAI 兼容接口**风格（HTTP Bearer Auth）。

### Authentication
- **Method:** `Authorization: Bearer <API_KEY>`
- **API Key:** Apply via Tencent Cloud Console.

### API Endpoints
- **Base URL:** `https://api.ai3d.cloud.tencent.com`
- **Submit Task:** `POST /v1/ai3d/submit`
- **Query Status:** `POST /v1/ai3d/query`

### Core Parameters
- **`Prompt`**: 提示词（可选，但通常需要提供）。
- **`ImageUrl`**: 图片 URL。**注意**：仅支持 HTTP/HTTPS 链接。
- **`ImageBase64`**: 图片 Base64 数据。
    - **CRITICAL**: 必须是**Raw Base64**字符串（即**不包含** `data:image/jpeg;base64,` 前缀）。
    - 仅支持 JPG/PNG 等常见格式。
- **`FaceCount`**: 目标面数（默认 500k）。
- **`EnablePBR`**: 是否生成 PBR 材质（默认 False）。

### Payload Constraints
1.  **互斥规则**：`ImageBase64`、`ImageUrl` 和 `Prompt` 必填其一。
2.  **Web URL**: 使用 `{"ImageUrl": "https://..."}`。
3.  **Local File**: 使用 `{"ImageBase64": "<raw_base64_string>"}`。**不要**使用 data URI Scheme。

### Example (Python)

```python
# Submit Task (Local File)
import base64
with open("image.jpg", "rb") as f:
    raw_b64 = base64.b64encode(f.read()).decode()

payload = {
    "ImageBase64": raw_b64,  # No prefix!
}
headers = {"Authorization": "Bearer sk-..."}
requests.post("https://api.ai3d.cloud.tencent.com/v1/ai3d/submit", json=payload, headers=headers)
```

```python
# Submit Task (URL)
payload = {
    "ImageUrl": "https://example.com/image.jpg"
}
requests.post("https://api.ai3d.cloud.tencent.com/v1/ai3d/submit", json=payload, headers=headers)
```

---

2. 输入参数
以下请求参数列表仅列出了接口请求参数和部分公共参数，完整公共参数列表见 公共请求参数。

参数名称	必选	类型	描述
Action	是	String	公共参数，本接口取值：SubmitHunyuanTo3DProJob。
Version	是	String	公共参数，本接口取值：2025-05-13。
Region	是	String	公共参数，详见产品支持的 地域列表。
Prompt	否	String	文生3D，3D内容的描述，中文正向提示词。
最多支持1024个 utf-8 字符。
ImageBase64、ImageUrl和 Prompt必填其一，且Prompt和ImageBase64/ImageUrl不能同时存在。
示例值：一只小猫
ImageBase64	否	String	输入图 Base64 数据。
大小: 单边分辨率要求不小于128，不大于5000，大小≤6m (因base64编码后会大30%左右)
格式: jpg，png，jpeg，webp.
lmageBase64、lmageUr和 Prompt必填其一，且Prompt和lmageBase64/mageUr不能同时存在。
示例值：/9j/4QlQaHR0c...N6a2M5ZCI
ImageUrl	否	String	输入图Url
大小: 单边分辨率要求不小于128，不大于5000，大小≤8m
格式: jpg，png，jpeg，webp.
lmageBase64、lmageUr和 Prompt必填其一，且Prompt和lmageBase64/mageUr不能同时存在。
示例值：https://cos.ap-guangzhou.myqcloud.com/image.jpg
MultiViewImages.N	否	Array of ViewImage	多视角的模型图片，视角参考值：
left：左视图；
right：右视图；
back：后视图；

每个视角仅限制一张图片。
●图片大小限制：编码后所有图片大小总和不可超过8M。（base64编码下图片大小总和不超过6M，因base64编码后图片大小会大30%左右）
●图片分辨率限制：单边分辨率小于5000且大于128。
●支持图片格式：支持jpg或png
EnablePBR	否	Boolean	是否开启 PBR材质生成，默认 false。
示例值：true
FaceCount	否	Integer	生成3D模型的面数，默认值为500000。
可支持生成面数范围，参考值：40000-1500000。
示例值：400000
GenerateType	否	String	生成任务类型，默认Normal，参考值：
Normal：可生成带纹理的几何模型。
LowPoly：可生成智能减面后的模型。
Geometry：可生成不带纹理的几何模型（白模），选择此任务时，EnablePBR参数不生效。
Sketch：可输入草图或线稿图生成模型，此模式下prompt和ImageUrl/ImageBase64可一起输入。
示例值：Normal
PolygonType	否	String	该参数仅在GenerateType中选择LowPoly模式可生效。

多边形类型，表示模型的表面由几边形网格构成，默认为triangle,参考值:
triangle: 三角形面。
quadrilateral: 四边形面与三角形面混合生成。
示例值：triangle
3. 输出参数
参数名称	类型	描述
JobId	String	任务ID（有效期24小时）
示例值：1357237233311637504
RequestId	String	唯一请求 ID，由服务端生成，每次请求都会返回（若请求因其他原因未能抵达服务端，则该次请求不会获得 RequestId）。定位问题时需要提供该次请求的 RequestId。
4. 示例
示例1 提交生3D专业版示例
输入示例
POST / HTTP/1.1
Host: ai3d.tencentcloudapi.com
Content-Type: application/json
X-TC-Action: SubmitHunyuanTo3DProJob
<公共请求参数>

{
    "ImageUrl": "https://cos.ap-guangzhou.myqcloud.com/input.png"
}
输出示例
{
    "Response": {
        "JobId": "1357237233311637504",
        "RequestId": "173f8c3b-d559-4e17-aac7-4e42303773ac"
    }
}
5. 开发者资源
腾讯云 API 平台
腾讯云 API 平台 是综合 API 文档、错误码、API Explorer 及 SDK 等资源的统一查询平台，方便您从同一入口查询及使用腾讯云提供的所有 API 服务。

API Inspector
用户可通过 API Inspector 查看控制台每一步操作关联的 API 调用情况，并自动生成各语言版本的 API 代码，也可前往 API Explorer 进行在线调试。

SDK
云 API 3.0 提供了配套的开发工具集（SDK），支持多种编程语言，能更方便的调用 API。

Tencent Cloud SDK 3.0 for Python: CNB, GitHub, Gitee
Tencent Cloud SDK 3.0 for Java: CNB, GitHub, Gitee
Tencent Cloud SDK 3.0 for PHP: CNB, GitHub, Gitee
Tencent Cloud SDK 3.0 for Go: CNB, GitHub, Gitee
Tencent Cloud SDK 3.0 for Node.js: CNB, GitHub, Gitee
Tencent Cloud SDK 3.0 for .NET: CNB, GitHub, Gitee
Tencent Cloud SDK 3.0 for C++: CNB, GitHub, Gitee
Tencent Cloud SDK 3.0 for Ruby: CNB, GitHub, Gitee
命令行工具
Tencent Cloud CLI 3.0
6. 错误码
该接口暂无业务逻辑相关的错误码，其他错误码详见 公共错误码。