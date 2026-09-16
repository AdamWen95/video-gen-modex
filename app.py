"""Local Gradio client for the Modex Seedance video API."""

from __future__ import annotations

import html
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlsplit

import gradio as gr
import requests
from dotenv import load_dotenv

from video_download import DownloadCache, DownloadError


APP_DIR = Path(__file__).resolve().parent
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 15 * 60
REQUEST_TIMEOUT = (10, 60)  # Connect timeout, read timeout.
UPLOAD_NOTICE = "当前接口示例需要公网 URL；如果不能直接传文件，请先保留 image_url 方式"
SUCCESS_STATUSES = {"completed", "complete", "succeeded", "success", "done", "finished"}
FAILURE_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "rejected", "expired"}
VIDEO_CACHE = DownloadCache(APP_DIR / "output" / "videos")
DOWNLOAD_NOTICE = "取得视频后会自动准备本地下载；首次准备的速度取决于视频源站。"


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    default_model: str


def load_settings() -> Settings:
    """Read the .env next to app.py; existing process variables take priority."""
    load_dotenv(APP_DIR / ".env", encoding="utf-8-sig")
    return Settings(
        base_url=os.getenv("MODEX_BASE_URL", "").strip().rstrip("/"),
        api_key=os.getenv("MODEX_API_KEY", "").strip(),
        default_model=os.getenv("DEFAULT_MODEL", "").strip(),
    )


def validate_url(value: str, label: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not any(c.isspace() for c in value)
        )
        _ = parsed.port  # Reject invalid port values as well.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{label} 必须是完整的 http:// 或 https:// URL。")
    return value


def validate_settings(settings: Settings) -> None:
    if not settings.base_url or not settings.api_key:
        raise ValueError("请先在 .env 中填写 MODEX_BASE_URL 和 MODEX_API_KEY，然后重启应用。")
    validate_url(settings.base_url, "MODEX_BASE_URL")
    parsed = urlsplit(settings.base_url)
    if parsed.query or parsed.fragment:
        raise ValueError("MODEX_BASE_URL 请填写服务根地址，不要包含查询参数或 # 片段。")


def integer_value(value: Any, name: str, positive: bool = False) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError
        result = int(number)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} 必须是整数。") from None
    if positive and result <= 0:
        raise ValueError(f"{name} 必须大于 0。")
    return result


def prepare_uploaded_image(image_path: str) -> str:
    """Extension point: return a Modex-supported image value after integration.

    A local path is not reachable by Modex. No upload endpoint or base64 format
    was supplied, so do not invent one or send a local path in JSON.
    """
    raise ValueError(
        f"{UPLOAD_NOTICE}。当前版本仅本地预览上传图片；请把图片放到可公开访问的地址，"
        "再填写 image_url。确认 Modex 的文件协议后，可在 prepare_uploaded_image() 中接入。"
    )


def build_payload(
    model: str, mode: str, prompt: str, image_url: str, image_upload: str | None,
    width: Any, height: Any, duration: Any, seed: Any,
) -> dict[str, Any]:
    model, prompt = (model or "").strip(), (prompt or "").strip()
    if not model:
        raise ValueError("请填写 model，或在 .env 中设置 DEFAULT_MODEL 后重启应用。")
    if not prompt:
        raise ValueError("请输入 prompt。")
    if mode not in {"text2video", "image2video"}:
        raise ValueError("mode 必须是 text2video 或 image2video。")
    payload = {
        "model": model,
        "prompt": prompt,
        "width": integer_value(width, "width", positive=True),
        "height": integer_value(height, "height", positive=True),
        "duration": integer_value(duration, "duration", positive=True),
        "n": 1,
        "seed": integer_value(seed, "seed"),
    }
    if mode == "image2video":
        image_url = (image_url or "").strip()
        if image_url:
            payload["image"] = validate_url(image_url, "image_url")
        elif image_upload:
            payload["image"] = prepare_uploaded_image(image_upload)
        else:
            raise ValueError("图生视频需要 image_url 或上传图片；当前接口请优先填写公网图片 URL。")
    return payload


def build_query_url(base_url: str, task_id: str) -> str:
    """Change only this function if Modex uses a different task query path."""
    return f"{base_url.rstrip('/')}/v1/video/generations/{quote(task_id, safe='')}"


def response_objects(response: Any) -> Iterator[dict[str, Any]]:
    """Read Modex's envelope, task record and nested upstream response."""
    for _ in range(3):
        if not isinstance(response, dict):
            return
        yield response
        response = response.get("data")


def response_field(response: Any, *names: str) -> Any:
    """Accept root/data/data.data fields, skipping empty placeholders."""
    for source in response_objects(response):
        for name in names:
            value = source.get(name)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                if str(value).strip():
                    return value
    return None


def extract_task_id(response: Any) -> str | None:
    # Modex's task record also has a numeric database id; use the public task id.
    value = response_field(response, "task_id")
    if value is None:
        value = response_field(response, "id")
    return str(value).strip() if value is not None else None


def extract_status(response: Any) -> str:
    value = response_field(response, "status")
    return str(value).strip().lower() if value is not None else "unknown"


def extract_failure_reason(response: Any) -> str | None:
    """Prefer task failure details to generic envelope messages such as success."""
    sources = list(response_objects(response))
    for source in sources:
        reason = source.get("fail_reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    for source in sources:
        error = source.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
        if isinstance(error, dict):
            for name in ("message", "detail"):
                reason = error.get(name)
                if isinstance(reason, str) and reason.strip():
                    return reason.strip()
    for source in reversed(sources):
        for name in ("message", "detail"):
            reason = source.get(name)
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
    return None


def failure_details(response: Any, api_key: str) -> str:
    reason = extract_failure_reason(redact(response, api_key))
    if not reason:
        return "服务端未提供具体失败原因，请查看下方接口响应。"
    details = f"失败原因：{reason}"
    if "output audio" in reason.lower() and "copyright" in reason.lower():
        details = "服务端提示：生成的音频可能涉及版权限制。\n" + details
    return details


def extract_video_url(response: Any) -> str | None:
    """Read explicit video fields, Modex result_url, then generic URL fields."""
    sources = list(response_objects(response))
    candidates = []
    for source in sources:
        candidates.append(source.get("video_url"))
        content = source.get("content")
        if isinstance(content, dict):
            candidates.append(content.get("video_url"))
    for name in ("result_url", "url"):
        candidates.extend(source.get(name) for source in sources)
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            return validate_url(value.strip(), "接口返回的视频 URL")
        except ValueError:
            # In failed tasks Modex sometimes puts an error message in result_url.
            continue
    return None


def redact(value: Any, api_key: str) -> Any:
    """Keep provider responses inspectable without echoing our API credential."""
    if isinstance(value, dict):
        return {k: redact(v, api_key) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, api_key) for v in value]
    if isinstance(value, str) and api_key:
        return value.replace(api_key, "[REDACTED]")
    return value


class APIError(Exception):
    def __init__(self, message: str, response: Any = None):
        super().__init__(message)
        self.response = response


def request_json(
    session: requests.Session, method: str, url: str, settings: Settings,
    payload: dict[str, Any] | None = None,
) -> Any:
    """One request only: a failed submission must not create duplicate jobs."""
    try:
        response = session.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
    except requests.Timeout as exc:
        raise APIError("接口请求超时。") from exc
    except requests.RequestException as exc:
        raise APIError(f"网络请求失败：{redact(str(exc), settings.api_key)}") from exc

    try:
        data = response.json()
        is_json = True
    except ValueError:
        data = {"body": response.text}
        is_json = False
    safe_data = redact(data, settings.api_key)
    print(f"\n[{method} {url}] HTTP {response.status_code}", flush=True)
    print(json.dumps(safe_data, ensure_ascii=False, indent=2), flush=True)
    if not 200 <= response.status_code < 300:
        hint = ""
        if response.status_code in {401, 403}:
            hint = " 请检查 API Key、账户权限及模型权限。"
        elif response.status_code == 404:
            hint = " 请检查服务根地址；查询路径可在 build_query_url() 中调整。"
        elif response.status_code == 429:
            hint = " 请求受限，请稍后再试。"
        raise APIError(f"接口返回 HTTP {response.status_code}。{hint}", safe_data)
    if not is_json:
        raise APIError("接口返回的内容不是 JSON，请检查原始响应及 MODEX_BASE_URL。", safe_data)
    return data


def generate_video(
    model: str, mode: str, prompt: str, image_url: str, image_upload: str | None,
    width: Any, height: Any, duration: Any, seed: Any,
) -> Iterator[tuple[str, str | None, Any, str]]:
    """Stream status, video, response history and the direct result URL to UI."""
    settings = load_settings()
    history: list[dict[str, Any]] = []
    task_id: str | None = None
    submitting = False
    try:
        payload = build_payload(model, mode, prompt, image_url, image_upload, width, height, duration, seed)
        validate_settings(settings)
        yield "正在提交任务…", None, history.copy(), ""
        with requests.Session() as session:
            submitting = True
            response = request_json(
                session, "POST", f"{settings.base_url}/v1/video/generations", settings, payload,
            )
            submitting = False
            history.append({"stage": "submit", "response": redact(response, settings.api_key)})
            task_id = extract_task_id(response)
            started = time.monotonic()
            poll_count = 0
            while True:
                status = extract_status(response)
                task_info = f"task_id: {task_id}" if task_id else "未返回 task_id"
                elapsed = int(time.monotonic() - started)
                prefix = f"{task_info}\n状态: {status} · 已等待 {elapsed} 秒"
                if status in FAILURE_STATUSES:
                    details = failure_details(response, settings.api_key)
                    yield f"生成失败或已终止。\n{prefix}\n{details}", None, history.copy(), ""
                    return
                video_url = extract_video_url(response)
                if video_url:
                    yield f"生成完成。\n{prefix}", video_url, history.copy(), video_url
                    return
                if status in SUCCESS_STATUSES and (poll_count > 0 or not task_id):
                    yield f"任务已完成，但响应中没有视频 URL。\n{prefix}\n可使用“查询已有任务”再次取回结果，或查看接口响应核对结果字段。", None, history.copy(), ""
                    return
                if not task_id:
                    yield "提交响应中未找到 task_id 或视频 URL，无法自动查询。请查看接口响应。", None, history.copy(), ""
                    return
                remaining = POLL_TIMEOUT_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    yield f"轮询超时，已停止本地查询；服务端任务可能仍在执行。\n{prefix}\n请保留 task_id，避免重复提交。", None, history.copy(), ""
                    return
                yield f"任务已提交，正在自动查询…\n{prefix}", None, history.copy(), ""
                time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
                if time.monotonic() - started >= POLL_TIMEOUT_SECONDS:
                    continue
                response = request_json(session, "GET", build_query_url(settings.base_url, task_id), settings)
                poll_count += 1
                history.append({"stage": f"poll_{poll_count}", "response": redact(response, settings.api_key)})
    except (ValueError, APIError) as exc:
        if isinstance(exc, APIError) and exc.response is not None:
            history.append({"stage": "error", "response": exc.response})
        message = f"错误：{redact(str(exc), settings.api_key)}"
        if isinstance(exc, APIError) and exc.response is not None:
            reason = extract_failure_reason(exc.response)
            if reason:
                message += f"\n失败原因：{redact(reason, settings.api_key)}"
        if task_id:
            message += f"\ntask_id: {task_id}\n已停止本地查询。服务端任务可能仍在执行，请保留任务 ID。"
        elif submitting:
            message += "\n已停止提交且不会自动重试。若服务端已收到请求，任务可能已创建，请先确认再重新提交。"
        yield message, None, history.copy(), ""


def query_existing_task(task_id: str) -> Iterator[tuple[str, str | None, Any, str]]:
    """Recover an existing result with one GET; never submit a generation."""
    settings = load_settings()
    history: list[dict[str, Any]] = []
    task_id = (task_id or "").strip()
    try:
        if not task_id:
            raise ValueError("请填写已有的 task_id，可从任务状态或接口响应中复制。")
        validate_settings(settings)
        yield f"正在查询已有任务…\ntask_id: {task_id}", None, history.copy(), ""
        with requests.Session() as session:
            response = request_json(session, "GET", build_query_url(settings.base_url, task_id), settings)
        history.append({"stage": "query", "response": redact(response, settings.api_key)})
        status = extract_status(response)
        prefix = f"task_id: {task_id}\n状态: {status}"
        if status in FAILURE_STATUSES:
            yield f"生成失败或已终止。\n{prefix}\n{failure_details(response, settings.api_key)}", None, history, ""
            return
        video_url = extract_video_url(response)
        if video_url:
            yield f"已取回视频。\n{prefix}", video_url, history, video_url
        elif status in SUCCESS_STATUSES:
            yield f"任务已完成，但未找到有效的视频 URL。\n{prefix}\n请查看接口响应。", None, history, ""
        else:
            yield f"尚未取得视频结果，可稍后再次查询。\n{prefix}", None, history, ""
    except (ValueError, APIError) as exc:
        message = f"查询失败：{redact(str(exc), settings.api_key)}"
        if isinstance(exc, APIError) and exc.response is not None:
            history.append({"stage": "error", "response": exc.response})
            reason = extract_failure_reason(exc.response)
            if reason:
                message += f"\n失败原因：{redact(reason, settings.api_key)}"
        if task_id:
            message += f"\ntask_id: {task_id}"
        yield message, None, history, ""


def video_html(video_url: str | None) -> str:
    """Let the browser play the URL, so CDN errors cannot hide the API result."""
    if not video_url:
        return '<div style="padding:3rem;text-align:center;color:#777">生成的视频将在这里显示</div>'
    source = html.escape(validate_url(video_url, "视频 URL"), quote=True)
    return (
        f'<video controls controlslist="nodownload" playsinline preload="none" src="{source}" '
        'style="width:100%;max-height:480px;border-radius:8px;background:#111">'
        '浏览器无法播放此视频，请复制下方视频 URL 打开。</video>'
    )


def generate_video_for_ui(
    model: str, mode: str, prompt: str, image_url: str, image_upload: str | None,
    width: Any, height: Any, duration: Any, seed: Any,
) -> Iterator[tuple]:
    for status, url, response, link in generate_video(
        model, mode, prompt, image_url, image_upload, width, height, duration, seed,
    ):
        yield status, video_html(url), response, link, gr.DownloadButton(value=None, interactive=False), DOWNLOAD_NOTICE, link


def query_existing_task_for_ui(task_id: str) -> Iterator[tuple]:
    for status, url, response, link in query_existing_task(task_id):
        yield status, video_html(url), response, link, gr.DownloadButton(value=None, interactive=False), DOWNLOAD_NOTICE, link


def prepare_video_download(video_url: str) -> Iterator[tuple[str, Any]]:
    """Observe a background download; cancellation never blocks on the CDN."""
    yield DOWNLOAD_NOTICE, gr.DownloadButton(value=None, interactive=False)
    if not video_url:
        return
    try:
        job = VIDEO_CACHE.get(validate_url(video_url, "视频 URL"))
        started = time.monotonic()
        initial_bytes = job.progress.downloaded
        previous_bytes = initial_bytes
        while not job.future.done():
            state = job.progress
            if state.downloaded < previous_bytes:
                started = time.monotonic()
                initial_bytes = state.downloaded
            previous_bytes = state.downloaded
            downloaded = state.downloaded / 1024**2
            total = f" / {state.total / 1024**2:.1f} MiB" if state.total else " MiB"
            elapsed = max(time.monotonic() - started, 0.1)
            speed = max(0, state.downloaded - initial_bytes) / elapsed / 1024
            message = f"{state.mode} · {downloaded:.1f}{total} · {speed:.0f} KiB/s"
            yield message, gr.skip()
            time.sleep(0.5)
        path = job.future.result()
        size = path.stat().st_size / 1024**2
        yield f"已准备好 · {size:.1f} MiB · 点击“下载视频”从本地缓存保存。", gr.DownloadButton(value=str(path), interactive=True)
    except (DownloadError, ValueError, OSError) as exc:
        yield f"下载准备失败：{exc} 可点击“重试下载”，或复制视频 URL 直接下载。", gr.DownloadButton(value=None, interactive=False)


def build_app() -> gr.Blocks:
    settings = load_settings()
    with gr.Blocks(title="Modex · Seedance 视频生成", analytics_enabled=False) as demo:
        download_url = gr.State("")
        gr.Markdown("# Modex · Seedance 视频生成\n输入画面描述，生成一段视频。支持文生视频与图片 URL 图生视频。")
        with gr.Row():
            with gr.Column(scale=1):
                model = gr.Textbox(label="model", value=settings.default_model, placeholder="填写账号可用的 Seedance 模型名称")
                mode = gr.Dropdown(["text2video", "image2video"], value="text2video", label="mode")
                prompt = gr.Textbox(label="prompt", lines=5, placeholder="描述主体、动作、场景、光线和镜头运动…")
                with gr.Group(visible=False) as image_fields:
                    image_url = gr.Textbox(label="image_url（优先使用）", placeholder="https://example.com/image.jpg")
                    gr.Markdown(f"**上传提示：** {UPLOAD_NOTICE}。当前上传仅供本地预览，请填写公网 URL 后提交。")
                    image_upload = gr.Image(label="image_upload（本地预览）", type="filepath", sources=["upload"])
                with gr.Row():
                    width = gr.Number(label="width", value=1280, precision=0, minimum=1)
                    height = gr.Number(label="height", value=720, precision=0, minimum=1)
                with gr.Row():
                    duration = gr.Number(label="duration（秒）", value=5, precision=0, minimum=1)
                    seed = gr.Number(label="seed", value=0, precision=0)
                gr.Markdown("尺寸、时长及 seed 的可用范围以所选模型为准；每次提交生成 1 个视频。")
                submit = gr.Button("submit · 生成视频", variant="primary")
                with gr.Accordion("查询已有任务", open=True):
                    existing_task_id = gr.Textbox(label="已有 task_id", placeholder="粘贴任务 ID，取回已有视频")
                    query = gr.Button("查询已有任务（不创建新任务）")
            with gr.Column(scale=1):
                status = gr.Textbox(label="任务状态", value="就绪 · 填写参数后提交", lines=5, interactive=False)
                gr.Markdown("### 视频结果")
                video = gr.HTML(value=video_html(None))
                with gr.Row():
                    download = gr.DownloadButton("下载视频（本地缓存）", interactive=False, variant="primary")
                    retry_download = gr.Button("重试下载", size="sm")
                download_status = gr.Textbox(label="下载进度", value=DOWNLOAD_NOTICE, interactive=False)
                result_url = gr.Textbox(label="视频 URL（播放器无法播放时可复制打开）", interactive=False)
                with gr.Accordion("接口响应（提交及轮询记录）", open=True):
                    raw_response = gr.JSON(label="响应 JSON", value=[])
        mode.change(lambda value: gr.Group(visible=value == "image2video"), inputs=mode, outputs=image_fields, queue=False)
        download_event = gr.on(
            triggers=[result_url.change, retry_download.click],
            fn=prepare_video_download,
            inputs=download_url,
            outputs=[download_status, download],
            trigger_mode="always_last",
            concurrency_limit=2,
            show_progress="hidden",
            api_name=False,
        )
        submit.click(
            fn=generate_video_for_ui,
            inputs=[model, mode, prompt, image_url, image_upload, width, height, duration, seed],
            outputs=[status, video, raw_response, result_url, download, download_status, download_url],
            cancels=[download_event],
            concurrency_limit=1,
            concurrency_id="modex_tasks",
            trigger_mode="once",
            api_name=False,
        )
        query.click(
            fn=query_existing_task_for_ui,
            inputs=existing_task_id,
            outputs=[status, video, raw_response, result_url, download, download_status, download_url],
            cancels=[download_event],
            concurrency_limit=1,
            concurrency_id="modex_tasks",
            trigger_mode="once",
            api_name=False,
        )
    return demo


if __name__ == "__main__":
    build_app().queue(max_size=8).launch(server_name="127.0.0.1", server_port=7860, share=False)
