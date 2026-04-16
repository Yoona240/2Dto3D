# Response API 文档

## 一、Response API 介绍

OpenAI 新的 API: https://platform.openai.com/docs/api-reference/responses/create

关于 Response API 与 Chat Completions 区别: https://platform.openai.com/docs/guides/migrate-to-responses#about-the-responses-api

Response API 目前看上去定位是一个 Agent API，而不仅是大模型的对话 API，Response API 增加异步任务执行，工具能力（除了传统的 function，还有 RAG、Web Search、Code 执行器、MCP）

- Response 偏 Agent，也能推理；Chat/Completions 偏推理
- 后续新的模型只能使用 Response API，而无法使用 Chat/Completions，比如 o 系列模型，gpt-image 系列

**API 申请工单**: https://coreland.qunhequnhe.com/task?id=initiate&processDefinitionKey=auto_generated_key5376

---

## 二、支持的模型列表

| 厂商 | 模型名称 | 是否异步 | 是否支持同步 | 说明 |
|------|----------|----------|--------------|------|
| **豆包** | doubao-seedance-pro | ✅ | ❌ | 文生视频，resolution: 480p/720p/1080p，时长：2-12s |
| | doubao-seedance-1.5-pro | ✅ | ❌ | resolution: 480p/720p，时长：4-12s [文档](https://www.volcengine.com/docs/82379/1520757?lang=zh) |
| | doubao-seedance-1.0-lite-i2v | ✅ | ❌ | 图生视频 |
| | doubao-seedream-3.0-t2i | ✅ | ✅ | 文生图 |
| | doubao-seededit-3.0-i2i | ✅ | ✅ | 文本+图像编辑 |
| | doubao-seedream-4.0 | ✅ | ✅ | 文生图+图像编辑，支持多种尺寸 [文档](https://www.volcengine.com/docs/82379/1541523) |
| | doubao-seedream-4.5 | ✅ | ✅ | 参考 doubao-seedream-4.0 |
| | doubao-seed3d-1.0 | ✅ | ❌ | 3D 生成，约 20 分钟 |
| **千问** | qwen-image-edit | ✅ | ✅ | 文本+图像编辑 |
| **Gemini** | gemini-2.5-flash-image | ✅ | ❌ | 图片生成+编辑 [文档](https://ai.google.dev/gemini-api/docs/image-generation) |
| | gemini-3-pro-image-preview | ✅ | ❌ | |
| | veo-3.1-fast-generate-preview | ✅ | ❌ | 视频生成，4/6/8s，720p/1080p，$0.1-0.15/秒 |
| | veo-3.1-generate-preview | ✅ | ❌ | 支持3张以上参考图，$0.2-0.4/秒 |
| | imagen-4.0-generate-001 | ✅ | ❌ | 纯文本生图 [文档](https://cloud.google.com/vertex-ai/generative-ai/docs/models/imagen/4-0-generate-001?hl=zh-cn) |
| **Hunyuan** | hunyuan-3d-rapid | ✅ | ❌ | 3D 生成，约 5 分钟 |
| | hunyuan-3d-pro | ✅ | ❌ | 3D 生成，约 17 分钟以上 |
| | hunyuan-3d-2.5 | ✅ | ❌ | 旧版本 |
| **OpenAI** | sora-2 | ✅ | ❌ | 视频生成，约 4 分钟，size: 1280x720/720x1280，$0.1/秒 |
| | sora-2-pro | ✅ | ❌ | 支持更大尺寸，$0.3-0.5/秒 |

---

## 三、请求参数

### URL 地址

- **国内**: https://oneapi.qunhequnhe.com
- **国外**: https://oneapi-sg.qunhequnhe.com

### 请求参数

| 名称 | 类型 | 备注 |
|------|------|------|
| model | string | 模型名称 |
| background | bool | 是否开启异步任务 |
| input | string/array | 输入，类似于 chat/completions messages 参数 |
| tools | array | 支持的工具类型：`function`、`image_generation`、`video_generation`、`3d_generation` |

### 返回值 status 枚举值

| 枚举值 | 说明 |
|--------|------|
| queued | 排队等待调度 |
| incomplete | 开始 |
| in_progress | 进行中 |
| completed | 完成 |
| failed | 失败 |
| cancelled | 取消 |

---

## 四、请求示例

### 4.1 Gemini 示例

#### 4.1.1 imagen 模型

**创建异步任务**

```bash
curl --location 'https://${ONEAPI_HOST}/v1/responses' \
--header 'Authorization: Bearer sk-{apikey}' \
--header 'Content-Type: application/json' \
--data '{
    "model": "imagen-4.0-generate-001",
    "background": true,
    "input": [
        {
            "role": "user",
            "type": "message",
            "content": "Generate a cat picture"
        }
    ],
    "tools": [
        {
            "type": "image_generation",
            "n": 1,
            "size":"1152x864"
        }
    ]
}'
```

**返回值**

```json
{
    "id": "gemini-img-1756970924185008971",
    "object": "response",
    "created_at": 1756970924,
    "status": "in_progress",
    "model": "imagen-4.0-generate-001"
}
```

**获取异步结果**

```bash
curl --location 'https://${ONEAPI_HOST}/v1/responses/gemini-img-1756970924185008971' \
--header 'Authorization: Bearer sk-{apikey}'
```

**返回值**

```json
{
    "id": "gemini-img-1756891977271516124",
    "object": "response",
    "status": "completed",
    "output": [
        {
            "type": "image_generation_call",
            "id": "gemini-flash-img-1756891977271516124-0-0",
            "status": "completed",
            "result": "data:image/png;base64,xxxxxx"
        }
    ]
}
```

#### 4.1.2 gemini-2.5-flash-image

**编辑图片**

```bash
curl --location 'https://oneapi.qunhequnhe.com/v1/responses' \
--header 'Authorization: {api-key}' \
--header 'Content-Type: application/json' \
--data '{
    "input": [
        {
            "type": "message",
            "content": [
                {
                    "type": "input_text",
                    "text": "你是图片编辑专家，图中有2只狗，去掉右边的一只狗"
                },
                {
                    "type": "input_image",
                    "image_url": "https://images.pexels.com/photos/1108099/pexels-photo-1108099.jpeg"
                }
            ],
            "role": "user"
        }
    ],
    "model": "gemini-2.5-flash-image",
    "tools": [
        {
            "type": "image_generation",
            "size": "720x1280",
            "n": 1
        }
    ],
    "background": true
}'
```

#### 4.1.3 veo 视频生成

**4.1.3.1 文本生成视频**

```bash
curl --location 'https://{API_BASE}/v1/responses' \
--header 'Authorization: {api-key}' \
--header 'Content-Type: application/json' \
--data '{
    "model": "veo-3.1-fast-generate-preview",
    "background": true,
    "input": [
        {
            "role": "user",
            "type": "message",
            "content": [
                {
                    "type": "input_text",
                    "text": "生成两只猫打架的视频"
                }
            ]
        }
    ],
    "tools": [
        {
            "type": "video_generation",
            "size": "1408x768",
            "seconds": "4",
            "resolution": "720p"
        }
    ]
}'
```

**4.1.3.2 文本+图片生成视频**

```bash
curl --location 'https://oneapi-sg.qunhequnhe.com/v1/responses' \
--header 'Authorization: {api-key}' \
--header 'Content-Type: application/json' \
--data '{
    "model": "veo-3.1-fast-generate-preview",
    "background": true,
    "input": [
        {
            "role": "user",
            "type": "message",
            "content": [
                {
                    "type": "input_text",
                    "text": "生成两只狗嬉闹的视频"
                },
                {
                    "type": "input_image",
                    "image_url": "https://images.pexels.com/photos/1108099/pexels-photo-1108099.jpeg"
                }
            ]
        }
    ],
    "tools": [
        {
            "type": "video_generation",
            "size": "2816x1536",
            "resolution": "1080p"
        }
    ]
}'
```

**4.1.3.3 首帧+尾帧**

通过数组顺序指定：第一张图片作为首帧，最后一张图片作为尾帧。

**4.1.3.4 参考图(3张以上)**

使用 `veo-3.1-generate-preview` 模型，支持 3 张以上参考图。

---

### 4.2 doubao

#### 4.2.1 视频生成

| 参数名 | 说明 |
|--------|------|
| type | `video_generation` |
| size | 视频尺寸，格式: width x height |
| aspect_ratio | 16:9, 4:3, 1:1, 3:4, 9:16, 21:9 |
| seconds | 生成视频的秒数 |
| resolution | 分辨率: 480p, 720p, 1080p |

**创建任务**

```bash
curl --location 'https://${ONEAPI_HOST}/v1/responses' \
--header 'Authorization: Bearer sk-{apikey}' \
--header 'Content-Type: application/json' \
--data '{
    "model": "doubao-seedance-pro",
    "background": true,
    "input": [
        {
            "role": "user",
            "type": "message",
            "content": [
                {
                    "type": "input_text",
                    "text": "请帮我基于一个图片生成一个视频"
                },
                {
                    "type": "input_image",
                    "image_url": "https://example.com/image.jpeg"
                }
            ]
        }
    ],
    "tools": [
        {
            "type": "video_generation",
            "size": "1280x720",
            "seconds": "8"
        }
    ]
}'
```

**获取结果**

```bash
curl --location 'https://${ONEAPI_HOST}/v1/responses/{response_id}' \
--header 'Authorization: Bearer sk-{apikey}'
```

> 注意：返回的视频链接非持久化，有有效期限制

#### 4.2.2 doubao-3d

```bash
curl --location 'http://{host}/v1/responses' \
--header 'Authorization: {api key}' \
--header 'Content-Type: application/json' \
--data '{
    "model": "doubao-seed3d-1.0",
    "background": true,
    "input": [
        {
            "role": "user",
            "type": "message",
            "content": [
                {
                    "type": "input_image",
                    "image_url": "https://images.pexels.com/photos/1108099/pexels-photo-1108099.jpeg"
                }
            ]
        }
    ],
    "tools": [
        {
            "type": "3d_generation",
            "output_format": "GLB"
        }
    ]
}'
```

#### 4.2.3 图片编辑/生成

```bash
curl --location 'https://oneapi.qunhequnhe.com/v1/responses' \
--header 'Authorization: {api_key}' \
--header 'Content-Type: application/json' \
--data '{
    "input": [
        {
            "type": "message",
            "content": [
                {
                    "type": "input_text",
                    "text": "基于提供的图片，处理成不同的风格"
                },
                {
                    "type": "input_image",
                    "image_url": "https://example.com/image.png"
                }
            ],
            "role": "user"
        }
    ],
    "model": "doubao-seedream-4.0",
    "tools": [
        {
            "type": "image_generation",
            "size": "720x1280",
            "n": 4
        }
    ],
    "background": true
}'
```

---

### 4.3 hunyuan 请求示例

#### 4.3.1 hunyuan3d

**模型列表**
- `hunyuan-3d-rapid` - 速度较快，约 5 分钟
- `hunyuan-3d-pro` - 速度较慢，约 18 分钟
- `hunyuan-3d-2.5` - 旧版本

**示例一 - 多视角图片**

```json
{
    "model": "hunyuan-3d-rapid",
    "background": true,
    "input": [
        {
            "role": "user",
            "type": "message",
            "content": [
                {
                    "type": "input_image",
                    "image_url": "https://example.com/front.jpeg"
                },
                {
                    "type": "input_image",
                    "image_url": "https://example.com/back.jpeg",
                    "view": "back"
                }
            ]
        }
    ],
    "tools": [
        {
            "type": "3d_generation",
            "pbr": true,
            "face_count": 1000000,
            "output_format": "GLB"
        }
    ]
}
```

**示例二 - 纯文本**

```json
{
    "model": "hunyuan-3d-rapid",
    "background": true,
    "input": [
        {
            "role": "user",
            "type": "message",
            "content": [
                {
                    "type": "input_text",
                    "text": "生成一个孙悟空的3d模型"
                }
            ]
        }
    ],
    "tools": [
        {
            "type": "3d_generation"
        }
    ]
}
```

---

### 4.4 OpenAI

#### 4.4.1 sora-2

**参数说明**

| 参数名 | 说明 |
|--------|------|
| size | 视频尺寸：纵向 720x1280，横向 1280x720 |
| seconds | 视频秒数：4、8 或 12 |
| input_image | 输入图片（尺寸会自动 resize） |

**4.4.1.1 异步请求示例**

```bash
curl --location 'http://{host}/v1/responses' \
--header 'Authorization: {api-key}' \
--header 'Content-Type: application/json' \
--data '{
    "model": "sora-2",
    "background": true,
    "input": [
        {
            "role": "user",
            "type": "message",
            "content": [
                {
                    "type": "input_text",
                    "text": "基于图片生成一段视频"
                },
                {
                    "type": "input_image",
                    "image_url": "https://images.pexels.com/photos/1108099/pexels-photo-1108099.jpeg"
                }
            ]
        }
    ],
    "tools": [
        {
            "type": "video_generation",
            "size": "1280x720",
            "seconds": "4"
        }
    ]
}'
```

**4.4.1.2 获取视频生成结果**

```bash
GET ${host}/v1/responses/{video_id}
```

**4.4.1.3 继续对生成视频编辑**

```bash
curl --location -X POST 'http://{host}/v1/responses' \
--header 'Authorization: {api-key}' \
--header 'Content-Type: application/json' \
--data '{
    "model": "sora-2",
    "background": true,
    "input": [
        {
            "role": "user",
            "type": "message",
            "content": [
                {
                    "type": "input_text",
                    "text": "视频的狗狗希望可以跑动起来"
                }
            ]
        }
    ],
    "tools": [
        {
            "type": "video_generation",
            "id": "video_6908183526ac819091366d5d65cb0380"
        }
    ]
}'
```