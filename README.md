# Modex Seedance 视频生成小网页

使用 Python、Gradio、requests 和 python-dotenv，在本地网页中提交 Modex 视频生成任务，查看原始响应、任务状态和视频结果。

- **文生视频**：输入提示词，提交时不包含 `image` 字段。
- **图生视频**：输入提示词和公网图片 URL；同时填写 URL 和上传图片时，优先使用 URL。
- 提交后自动提取任务 ID，轮询查询任务，取得视频 URL 后展示结果。

## 1. 安装

需要 **Python 3.10 或更高版本**。在项目目录中执行以下命令。

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

后续直接使用虚拟环境中的 Python，无需激活虚拟环境，也无需修改 PowerShell 执行策略。

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

如果已经有 `.env`，直接编辑现有文件即可。

## 2. 配置 `.env`

编辑项目目录下的 `.env`，将三个占位值替换为实际配置：

```dotenv
MODEX_BASE_URL=https://your-modex-api.example.com
MODEX_API_KEY=replace-with-your-modex-api-key
DEFAULT_MODEL=your-seedance-model
```

| 配置 | 含义 |
| --- | --- |
| `MODEX_BASE_URL` | Modex 服务根地址，例如 `https://your-modex-api.example.com`。不要包含 `/v1` 或 `/v1/video/generations`，程序会追加接口路径。 |
| `MODEX_API_KEY` | 你的 Modex API Key。通过 `Authorization: Bearer ...` 请求头发送。 |
| `DEFAULT_MODEL` | 账号实际可用的 Seedance 模型标识，用作页面 `model` 的默认值，也可以在页面修改。 |

上述地址、密钥和模型名均为占位值，请以 Modex 提供的实际信息为准。应用读取 `app.py` 同目录的 `.env`，已有的系统环境变量优先。修改 `.env` 后重新启动应用。

## 3. 启动

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe app.py
```

macOS / Linux：

```bash
.venv/bin/python app.py
```

如果已经激活虚拟环境，也可以直接运行：

```bash
python app.py
```

浏览器打开 [本地网页](http://127.0.0.1:7860)。应用默认绑定 `127.0.0.1:7860`，关闭 Gradio 公网分享（`share=False`）。在启动终端按 `Ctrl+C` 停止服务。

### 取回已有任务的视频

如果之前已经生成成功，但没有显示视频，或刷新页面后丢失结果：在“查询已有任务”区域粘贴原来的 `task_id`，点击查询按钮。程序只发送一次 GET 查询，识别视频地址后直接显示播放器及可复制的 URL，不会新建生成任务。如果任务仍在处理中，可稍后再查询。

## 4. 测试文生视频

1. 确认 `model` 是当前账号可用的模型。
2. 将 `mode` 选择为 `text2video`。
3. 在 `prompt` 中输入，例如：`清晨的海边，一只海鸥掠过浪尖，镜头缓慢向前推进。`
4. 设置 `width`、`height`、`duration` 和 `seed`，点击 `submit`。
5. 查看状态区域和接口响应；任务生成成功且返回视频 URL 后，在视频结果区域播放。

页面初始参数为宽 `1280`、高 `720`、时长 `5` 秒、随机种子 `0`；这些值仅为界面默认值，模型支持的尺寸、时长及 seed 范围以 Modex 文档为准。`n` 固定为 `1`。

文生视频模式下，即使保留了图片 URL 或上传图片，提交请求也不会包含 `image` 字段。

## 5. 测试图生视频

### 使用图片 URL

1. 将 `mode` 选择为 `image2video`。
2. 填写描述图片如何运动的 `prompt`。
3. 在 `image_url` 中填写 Modex 服务可以直接访问的 HTTP(S) 图片 URL。
4. 设置模型和生成参数，点击 `submit`。
5. 等待轮询结果，查看返回内容和视频。

图片地址应直接返回图片内容。`localhost`、本地文件路径，以及需要浏览器登录才能访问的页面通常无法供 Modex 服务读取。同时提供 `image_url` 和 `image_upload` 时，仅使用 `image_url`。

### 上传图片的当前限制

`image_upload` 可选择本地图片并预览，但当前提供的接口示例没有确认文件上传、Base64 或文件 ID 协议。因此，当 `image_url` 为空且仅上传图片时，应用会显示：

> 当前接口示例需要公网 URL；如果不能直接传文件，请先保留 image_url 方式

随后终止本次提交，不会将本地路径发送给 Modex，也不会自动将图片上传到第三方图床。请填写可访问的公网图片 URL 后重新提交。

如果后续确认 Modex 支持直接传文件，可按官方协议修改 `app.py` 中的 `prepare_uploaded_image()`；当前实现保留了这个扩展位置。图生视频模式下，未填写 URL 且未上传图片时也不会提交任务。

## 6. 接口与响应兼容

提交任务：

```text
POST {MODEX_BASE_URL}/v1/video/generations
Authorization: Bearer {MODEX_API_KEY}
Content-Type: application/json
```

请求体包含 `model`、`prompt`、`width`、`height`、`duration`、`n: 1`、`seed`；仅图生视频时包含 `image`，默认使用图片 URL。

默认查询接口：

```text
GET {MODEX_BASE_URL}/v1/video/generations/{task_id}
```

查询路径封装在 `app.py` 的 `build_query_url()` 中。如果你的 Modex 服务使用其他查询路径，请在该函数中修改。

| 数据 | 兼容的返回字段 |
| --- | --- |
| 任务 ID | `id`、`task_id`、`data.id`、`data.task_id` |
| 状态 | `status`、`data.status` |
| 视频地址 | `video_url`、`data.video_url`、`data.result_url`、`data.data.content.video_url`、`url`、`data.url` |

对于 Modex 包装后的响应，也支持从 `data.data` 读取状态与结果字段。任务失败时，状态区域会直接显示 `fail_reason` 或 `error.message` 中的具体原因，同时保留完整接口响应；外层 `code: "success"` 仅表示本次查询成功，不代表视频生成成功。

解析时优先使用明确的 `video_url`（含 `content.video_url`），再读取 `result_url` 和通用 `url`。`result_url` 中的错误文字会被跳过；查询用的任务 ID 优先读取 `task_id`，避免误用 Modex 任务记录的数字 `id`。

接口响应会打印到启动终端，并显示在网页中；响应中出现当前 API Key 时会脱敏。获得任务 ID 后默认每 **5 秒**查询一次，轮询时限为 **15 分钟**，可在 `app.py` 中调整轮询常量。请求连接超时为 **10 秒**，读取超时为 **60 秒**；时限到达时，正在进行的请求仍受单次请求超时控制。程序不会自动重试创建任务的 POST 请求，避免重复创建视频任务。

视频结果使用 Gradio HTML 区域中的浏览器播放器，直接加载返回的 HTTP(S) 视频地址。即使视频地址失效或浏览器不支持其编码，任务状态、接口响应和可复制的视频 URL 仍然显示。

如果没有提取到任务 ID 或视频 URL，原始响应仍可查看，便于根据服务实际协议调整解析逻辑。

## 7. 本地验证

安装依赖后，运行基于模拟响应的单元测试：

```bash
python -m unittest discover -s tests -v
```

Windows 未激活虚拟环境时使用：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

macOS / Linux 未激活虚拟环境时使用：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

这些测试不调用真实的 Modex 生成接口。项目尚未使用真实 Modex 凭据完成端到端验证；实际模型名、支持的参数、查询路径和上传协议仍应以你的服务配置为准。

## 8. 常见问题

| 现象 | 排查方式 |
| --- | --- |
| 提示缺少配置 | 检查项目目录是否有 `.env`，确认三个配置项已填写并重启应用。 |
| `401` / `403` | 检查 API Key、账号权限以及模型访问权限。 |
| `404` | 检查服务根地址是否重复包含 `/v1`；若只在轮询时报错，确认查询路径并修改 `build_query_url()`。 |
| `400` / `422` | 根据显示的响应检查模型名、尺寸、时长、seed，以及图片 URL 和模型支持的输入格式。 |
| `429` | 服务请求频率或额度受限，根据接口返回说明处理后再提交。 |
| 上传图片后没有创建任务 | 当前文件直传协议尚未确认，请改用 `image_url`。 |
| 接口返回非 JSON | 网页会显示可读错误及响应内容；检查地址、代理和服务状态。 |
| 提交连接失败或超时 | 检查网络和服务状态；读取超时不一定代表服务未创建任务，重新提交前先检查服务端任务记录。 |
| 轮询超时 | 本地等待达到上限，不代表服务端任务已取消。保留任务 ID，在 Modex 端检查任务；需要时调整轮询时限。 |
| 有视频 URL，但无法播放 | 打开返回的视频地址检查可访问性、有效期及浏览器对视频编码的支持。 |
| `output audio may be related to copyright restrictions` | 服务端提示生成的音频可能涉及版权限制，任务已失败。若只需要无声视频，请先向 Modex 确认是否支持关闭音频，以及对应参数是否会传递给上游；当前页面未发送音频开关。也可提供任务 ID 和错误原文给 Modex 排查。 |
| 端口 `7860` 被占用 | 停止占用端口的旧实例，或修改 `app.py` 中启动时的端口。 |

## 文件结构

```text
app.py              配置、请求构造、响应解析、轮询与 Gradio 页面
requirements.txt    Python 依赖
.env.example        环境变量模板
README.md           安装与使用说明
tests/              模拟接口测试
```
