# Modex Seedance 视频生成小网页

使用 Python、Gradio、requests 和 python-dotenv，在本地网页中提交 Modex 视频生成任务，查看原始响应、任务状态和视频结果。

- **文生视频**：输入提示词，使用 Seedance 2.0 格式时不发送 `content`。
- **图生视频（首帧）**：输入提示词和公网图片 URL，将图片作为 `first_frame`。
- **参考视频生成**：输入提示词和公网视频直链（本页面支持 1–3 个），可再添加一张图片参考，用视频的动作、节奏或运镜指导生成。
- 默认使用当前文档的 Seedance 2.0 接口，也可切换到旧版 Modex 格式，生成或取回原有任务。
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
| `MODEX_BASE_URL` | Modex 服务根地址，例如 `https://your-modex-api.example.com`。不要包含 `/v1`、`/v1/videos` 或 `/v1/video/generations`，程序会根据所选接口格式追加路径。 |
| `MODEX_API_KEY` | 你的 Modex API Key。通过 `Authorization: Bearer ...` 请求头发送。 |
| `DEFAULT_MODEL` | 账号实际可用的 Seedance 模型标识，用作页面 `model` 的默认值，也可以在页面修改。参考视频功能需支持多模态输入的 Seedance 2.0 模型，例如文档中的 `doubao-seedance-2-0-260128`；实际以账号开放的模型为准。 |

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

如果之前已经生成成功，但没有显示视频，或刷新页面后丢失结果：先将“接口格式（生成与查询共用）”设为创建该任务时使用的格式，再在“查询已有任务”区域粘贴原来的 `task_id`，点击查询按钮。程序只查询原任务，识别视频地址后直接显示播放器及可复制的 URL，不会新建生成任务。如果任务仍在处理中或已完成但地址尚未就绪，页面每 30 秒自动检查一次，最多继续 2 小时。

新提交的任务会自动填入“已有 task_id”。快速轮询超时或网络查询暂时失败后，不必复制 ID 或重新生成：保持页面打开即可继续自动取回结果，也可点击“立即查询当前任务”马上检查。“暂停自动查询”只停止本页查询，不取消服务端生成；点击“立即查询当前任务”可恢复。当前任务的恢复查询沿用创建/首次查询时的接口格式，不受之后修改表单下拉框影响。任务信息仅保留在当前页面会话中，刷新或关闭页面前请保留 ID，重新打开后可通过“查询已有任务”恢复。

## 4. 测试文生视频

1. 确认 `model` 是当前账号可用的模型，接口格式选择默认的“Seedance 2.0（当前文档）”。
2. 将“生成方式”选择为“文生视频”（`text2video`）。
3. 在 `prompt` 中输入，例如：`清晨的海边，一只海鸥掠过浪尖，镜头缓慢向前推进。`
4. 选择清晰度、画面比例，设置 `duration`、`seed`、生成音频和添加水印选项，点击 `submit · 生成视频`。
5. 查看状态区域和接口响应；任务生成成功且返回视频 URL 后，在视频结果区域播放。

Seedance 2.0 初始参数为清晰度 `720p`、画面比例 `16:9`、时长 `5` 秒、随机种子 `0`，开启生成音频、关闭水印。清晰度下拉框包含 `480p`、`720p`、`1080p`；比例下拉框包含 `16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`21:9`、`adaptive`。具体模型支持的清晰度、比例、时长和 seed 范围以模型文档为准。

文生视频模式下，保留的图片或参考视频输入不会进入请求。选择旧版 Modex 格式后，页面改为宽高输入，初始宽 `1280`、高 `720`，请求中 `n` 固定为 `1`。

## 5. 测试图生视频

### 使用图片 URL

1. 将“生成方式”选择为“图生视频（首帧）”（`image2video`）。
2. 填写描述图片如何运动的 `prompt`。
3. 在 `image_url` 中填写 Modex 服务可以直接访问的 HTTP(S) 图片 URL。
4. 设置模型和生成参数，点击 `submit`。
5. 等待轮询结果，查看返回内容和视频。

图片地址应直接返回图片内容。`localhost`、本地文件路径，以及需要浏览器登录才能访问的页面通常无法供 Modex 服务读取。同时提供 `image_url` 和 `image_upload` 时，仅使用 `image_url`。Seedance 2.0 请求将图片放入 `content`，使用 `type: "image_url"`、`role: "first_frame"` 和嵌套的 `image_url.url`；旧版 Modex 格式仍发送原有 `image` 字段。

### 上传图片的当前限制

`image_upload` 可选择本地图片并预览，但当前提供的接口示例没有确认文件上传、Base64 或文件 ID 协议。因此，当 `image_url` 为空且仅上传图片时，应用会显示：

> 当前接口示例需要公网 URL；如果不能直接传文件，请先保留 image_url 方式

随后终止本次提交，不会将本地路径发送给 Modex，也不会自动将图片上传到第三方图床。请填写可访问的公网图片 URL 后重新提交。

如果后续确认 Modex 支持直接传文件，可按官方协议修改 `app.py` 中的 `prepare_uploaded_image()`；当前实现保留了这个扩展位置。图生视频模式下，未填写 URL 且未上传图片时也不会提交任务。

## 6. 使用参考视频生成

1. 接口格式选择“Seedance 2.0（当前文档）”，填写账号可用且支持多模态输入的 Seedance 2.0 `model`。
2. 将“生成方式”选择为“参考视频生成（文字 / 图片 + 视频）”（`reference2video`）。
3. 在“参考视频 URL”中填写公网视频直链，**每行一个，本页面要求 1–3 个**。
4. 填写 `prompt`，说明希望参考哪些动作、节奏、镜头或风格。例如：`参考视频1的运镜和动作节奏，生成一个人在海边奔跑的镜头。`
5. 如需保留主体外观或图片风格，在 `image_url` 中添加一张公网图片；提示词可补充：`保持图片1中的主体外观。` 仅使用文字和视频时留空即可。
6. 设置清晰度、比例、时长、seed、音频及水印，点击 `submit · 生成视频`，等待任务结果和下载缓存。

此模式中的图片使用 `reference_image`，作为主体或风格参考，不保证与首帧完全一致。每个视频使用 `reference_video`；请求不会混入图生视频模式的 `first_frame`。

参考视频地址必须直接返回视频文件，分享页面、需登录的网页、本地文件路径均不适用。当前没有本地视频上传功能，图片上传仍只用于预览。请确保上游服务可以访问 URL；素材时长、文件大小等限制以所选模型文档为准。

## 7. 接口与响应兼容

### Seedance 2.0（默认）

提交任务：

```text
POST {MODEX_BASE_URL}/v1/videos
Authorization: Bearer {MODEX_API_KEY}
Content-Type: application/json
```

顶层参数为 `model`、`prompt`、`resolution`、`ratio`、`duration`、`seed`、`generate_audio`、`watermark`；图片或参考视频通过 `content` 传入。文生视频不发送 `content`。此格式不发送旧版的 `width`、`height`、`n`、`image`，也不在 `metadata` 中重复传参。

以下为“图片 + 参考视频”请求示例，示例素材 URL 需替换为真实直链：

```json
{
  "model": "doubao-seedance-2-0-260128",
  "prompt": "参考视频1的运镜，保持图片1中的主体外观，生成海边奔跑的镜头。",
  "content": [
    {
      "type": "image_url",
      "role": "reference_image",
      "image_url": {"url": "https://example.com/subject.jpg"}
    },
    {
      "type": "video_url",
      "role": "reference_video",
      "video_url": {"url": "https://example.com/motion.mp4"}
    }
  ],
  "resolution": "720p",
  "ratio": "16:9",
  "duration": 5,
  "seed": 0,
  "generate_audio": true,
  "watermark": false
}
```

只参考视频时，移除图片这一项即可。查询接口：

```text
GET {MODEX_BASE_URL}/v1/videos/{task_id}
```

### 旧版 Modex（兼容原有任务）

页面选择“旧版 Modex（兼容原有任务）”后，生成和查询均使用原路径：

```text
POST {MODEX_BASE_URL}/v1/video/generations
GET {MODEX_BASE_URL}/v1/video/generations/{task_id}
```

请求体保留 `model`、`prompt`、`width`、`height`、`duration`、`n: 1`、`seed`，图生视频额外包含 `image`。旧版格式不提供参考视频、音频或水印参数。生成与查询共用同一个接口格式选择；取回旧任务时应选回旧版，避免查错接口。

### 响应解析

| 数据 | 兼容的返回字段 |
| --- | --- |
| 任务 ID | `id`、`task_id`、`data.id`、`data.task_id` |
| 状态 | `status`、`data.status` |
| 视频地址 | `video_url`、`data.video_url`、`data.result_url`、`data.data.content.video_url`、`metadata.url`、`data.metadata.url`、`url`、`data.url` |

对于 Modex 包装后的响应，也支持从 `data.data` 读取状态与结果字段。任务失败时，状态区域会直接显示 `fail_reason` 或 `error.message` 中的具体原因，同时保留完整接口响应；外层 `code: "success"` 仅表示本次查询成功，不代表视频生成成功。

解析时优先使用明确的 `video_url`（含 `content.video_url`），再依次读取 `result_url`、`metadata.url` 和通用 `url`。`result_url` 中的错误文字会被跳过；查询用的任务 ID 优先读取 `task_id`，避免误用 Modex 任务记录的数字 `id`。

接口响应会打印到启动终端，并显示在网页中；响应中出现当前 API Key 时会脱敏。获得任务 ID 后默认每 **5 秒**查询一次，快速轮询时限为 **15 分钟**，到达边界仍会做最后一次查询。网页随后自动转为每 **30 秒**检查一次，最多再检查 **2 小时**；不占用持续生成队列。网络错误、请求超时、HTTP 429/5xx 也会进入后续查询；401/403/404 等错误则暂停，修正配置后可手动恢复。后续查询保留提交记录与最近记录，最多 240 条。任务失败时停止；成功但暂未返回 URL 时继续检查。请求连接超时为 **10 秒**，读取超时为 **60 秒**；正在进行的请求仍受单次请求超时控制。可在 `app.py` 调整轮询常量。程序不会自动重试创建任务的 POST 请求，避免重复创建视频任务。

视频结果使用浏览器播放器，可按需直接播放返回的 HTTP(S) 视频地址。播放器不预加载视频，以免与后台下载争抢带宽。即使视频地址失效或浏览器不支持其编码，任务状态、接口响应和可复制的视频 URL 仍然显示。

### 视频下载与缓存

取得视频 URL 后，页面会自动在后台准备下载，显示已下载大小和速度。准备完成后点击 **“下载视频（本地缓存）”**，浏览器从本机保存文件，无需再次从视频源站下载。已有任务也可以通过“查询已有任务”使用这一流程；失败时点击“重试下载”，或复制原始视频 URL 下载。

- 视频达到 4 MiB、源站支持 HTTP Range 且提供内容校验标识时，使用最多 **4 路并行下载**。如果源站不支持或拒绝分段请求，自动改用单连接流式下载。
- 完整文件保存到项目的 `output/videos/`，相同完整 URL 的重复请求复用缓存；并发请求同一视频也只下载一次。签名或查询参数变化会被视为新 URL。
- 缓存准备独立于视频生成和查询，不会因源站下载缓慢而延迟展示任务结果。源站拒绝访问或下载中断时，不会把半个文件作为完整视频提供。
- 最多同时准备 2 个视频，每个视频上限 2 GiB、准备时限约 30 分钟。下载请求不会携带 Modex API Key。缓存保留在本机，可在关闭应用后手动删除 `output/videos/` 释放磁盘空间；Gradio 还会在其临时目录中保存下载文件副本。

并行连接可能改善源站单连接限速；首次下载仍受源站和本机网络影响，无法保证固定提速倍数。缓存完成后的重复下载不再依赖源站速度。播放仍使用原始链接；希望避免播放占用下载带宽时，可等本地下载准备完成后再播放。

如果没有提取到任务 ID 或视频 URL，原始响应仍可查看，便于根据服务实际协议调整解析逻辑。

### 已核对的 API 文档

参考视频功能依据 [SeeDance2 系列视频生成说明](https://docs-api.apifox.cn/8754523m0)、[多模态视频生成](https://docs-api.apifox.cn/456703644e0)、[首尾帧生成视频](https://docs-api.apifox.cn/456703628e0) 和 [查询视频生成结果](https://docs-api.apifox.cn/456703505e0) 整理。模型可用性及素材限制仍以服务方当前文档和账号权限为准。

同时核对了相邻的[文生视频](https://docs-api.apifox.cn/456703490e0)、[多图参考生成](https://docs-api.apifox.cn/456703635e0)和[官方格式参数生成](https://docs-api.apifox.cn/456704217e0)。后者支持将参数放入 `metadata`，与顶层同名时优先使用 `metadata`；本项目统一采用顶层写法。旁边的[官方透传创建任务](https://docs-api.apifox.cn/514794354e0)使用独立的 `/api/v3/contents/generations/tasks` 协议，本页面没有混用该路径与响应格式。

## 8. 本地验证

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

这些测试不调用真实的 Modex 生成接口。下载测试通过本机 HTTP 服务验证分段拼接、缓存复用、普通下载回退和失败清理。实际模型名、支持的参数、查询路径和上传协议仍应以你的服务配置为准。

## 9. 常见问题

| 现象 | 排查方式 |
| --- | --- |
| 提示缺少配置 | 检查项目目录是否有 `.env`，确认三个配置项已填写并重启应用。 |
| `401` / `403` | 检查 API Key、账号权限以及模型访问权限。 |
| `404` | 检查服务根地址是否重复包含 `/v1`，确认所选接口格式受服务支持；查询已有任务时需选中创建时的格式。 |
| `400` / `422` | 根据显示的响应检查模型名、清晰度或尺寸、比例、时长、seed，以及图片/视频直链和模型支持的输入格式。 |
| `429` | 服务请求频率或额度受限，根据接口返回说明处理后再提交。 |
| 上传图片后没有创建任务 | 当前文件直传协议尚未确认，请改用 `image_url`。 |
| 参考视频未提交 | 选择 Seedance 2.0 格式，填写 1–3 个完整 HTTP(S) 视频直链，每行一个；检查所选模型是否支持多模态输入。 |
| 接口返回非 JSON | 网页会显示可读错误及响应内容；检查地址、代理和服务状态。 |
| 提交连接失败或超时 | 检查网络和服务状态；读取超时不一定代表服务未创建任务，重新提交前先检查服务端任务记录。 |
| 轮询超时 | 服务端任务未取消。保持页面打开，网页会每 30 秒继续检查；也可点击“立即查询当前任务”，成功后自动回填视频、URL 和下载入口。后续检查达到 2 小时会暂停，可手动继续。刷新页面后用原 task_id 查询恢复。 |
| 有视频 URL，但无法播放 | 打开返回的视频地址检查可访问性、有效期及浏览器对视频编码的支持。 |
| 视频下载很慢 | 查看“下载进度”，等待后台准备完成后使用“下载视频（本地缓存）”；播放器或原始 URL 的下载仍直接访问源站。若进度持续不动，可重试下载，或检查本机网络、代理及源站状态。 |
| 下载准备返回 `403` 或链接过期 | 使用原 `task_id` 重新查询并尝试下载；若上游仍返回同一失效链接，需要联系服务方刷新结果地址。 |
| `output audio may be related to copyright restrictions` | 服务端提示生成的音频可能涉及版权限制，任务已失败。若只需要无声视频，可在 Seedance 2.0 格式中取消“生成音频”，后续新任务会发送 `generate_audio: false`；这不会改变已有失败任务。也可提供任务 ID 和错误原文给 Modex 排查。 |
| `preupload video_url ... input video may be related to copyright restrictions` | 上游素材库在参考视频预上传/处理阶段将输入视频判定为可能涉及版权限制，并返回提交失败；这条提示不等于对素材版权的法律认定。可换用自己的原创视频验证，或将原始错误中的 Request ID、素材 ID 和错误原文提供给服务方排查误判。“生成音频”控制输出音频，不会关闭输入视频审核。页面会显示对应中文提示并保留原始错误，不自动重试；是否产生任务或费用需查询服务端记录。 |
| 端口 `7860` 被占用 | 停止占用端口的旧实例，或修改 `app.py` 中启动时的端口。 |

## 文件结构

```text
app.py              配置、请求构造、响应解析、轮询与 Gradio 页面
video_download.py   后台下载、并行分段校验与本地视频缓存
requirements.txt    Python 依赖
.env.example        环境变量模板
README.md           安装与使用说明
tests/              模拟接口测试
```
