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
RECOVERY_INTERVAL_SECONDS = 30
RECOVERY_TIMEOUT_SECONDS = 2 * 60 * 60
REQUEST_TIMEOUT = (10, 60)  # Connect timeout, read timeout.
UPLOAD_NOTICE = "当前接口示例需要公网 URL；如果不能直接传文件，请先保留 image_url 方式"
SUCCESS_STATUSES = {"completed", "complete", "succeeded", "success", "done", "finished"}
FAILURE_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "rejected", "expired"}
VIDEO_CACHE = DownloadCache(APP_DIR / "output" / "videos")
DOWNLOAD_NOTICE = "取得视频后会自动准备本地下载；首次准备的速度取决于视频源站。"
API_FORMAT_SEEDANCE = "seedance"
API_FORMAT_LEGACY = "legacy"
API_PATHS = {API_FORMAT_SEEDANCE: "/v1/videos", API_FORMAT_LEGACY: "/v1/video/generations"}
RESOLUTIONS = ["480p", "720p", "1080p"]
RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"]


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
    api_format: str = API_FORMAT_SEEDANCE, reference_video_urls: str = "",
    resolution: str = "720p", ratio: str = "16:9",
    generate_audio: bool = True, watermark: bool = False,
) -> dict[str, Any]:
    model, prompt = (model or "").strip(), (prompt or "").strip()
    if not model:
        raise ValueError("请填写 model，或在 .env 中设置 DEFAULT_MODEL 后重启应用。")
    if not prompt:
        raise ValueError("请输入 prompt。")
    if mode not in {"text2video", "image2video", "reference2video"}:
        raise ValueError("请选择文生视频、图生视频或参考视频生成。")
    if api_format not in API_PATHS:
        raise ValueError("请选择支持的接口格式。")
    if mode == "reference2video" and api_format == API_FORMAT_LEGACY:
        raise ValueError("参考视频生成需要选择 Seedance 2.0 接口格式。")
    payload = {
        "model": model,
        "prompt": prompt,
        "duration": integer_value(duration, "duration", positive=True),
        "seed": integer_value(seed, "seed"),
    }
    if api_format == API_FORMAT_LEGACY:
        payload.update(
            width=integer_value(width, "width", positive=True),
            height=integer_value(height, "height", positive=True),
            n=1,
        )
    else:
        if resolution not in RESOLUTIONS:
            raise ValueError("请选择支持的 resolution。")
        if ratio not in RATIOS:
            raise ValueError("请选择支持的 ratio。")
        if not isinstance(generate_audio, bool) or not isinstance(watermark, bool):
            raise ValueError("generate_audio 和 watermark 必须是布尔值。")
        payload.update(resolution=resolution, ratio=ratio, generate_audio=generate_audio, watermark=watermark)

    content = []
    if mode in {"image2video", "reference2video"}:
        image_url = (image_url or "").strip()
        if image_url:
            image_value = validate_url(image_url, "image_url")
        elif image_upload:
            image_value = prepare_uploaded_image(image_upload)
        elif mode == "image2video":
            raise ValueError("图生视频需要 image_url 或上传图片；当前接口请优先填写公网图片 URL。")
        else:
            image_value = None
        if image_value:
            if api_format == API_FORMAT_LEGACY:
                payload["image"] = image_value
            else:
                content.append({
                    "type": "image_url",
                    "role": "reference_image" if mode == "reference2video" else "first_frame",
                    "image_url": {"url": image_value},
                })
    if mode == "reference2video":
        videos = [line.strip() for line in (reference_video_urls or "").splitlines() if line.strip()]
        if not 1 <= len(videos) <= 3:
            raise ValueError("参考视频需要 1–3 个公网视频 URL，每行一个。")
        for index, video_url in enumerate(videos, 1):
            content.append({
                "type": "video_url", "role": "reference_video",
                "video_url": {"url": validate_url(video_url, f"参考视频 {index} URL")},
            })
    if content:
        payload["content"] = content
    return payload


def build_submit_url(base_url: str, api_format: str = API_FORMAT_SEEDANCE) -> str:
    if api_format not in API_PATHS:
        raise ValueError("请选择支持的接口格式。")
    return f"{base_url.rstrip('/')}{API_PATHS[api_format]}"


def build_query_url(base_url: str, task_id: str, api_format: str = API_FORMAT_SEEDANCE) -> str:
    return f"{build_submit_url(base_url, api_format)}/{quote(task_id, safe='')}"


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
    normalized = reason.lower()
    if "input video" in normalized and "copyright" in normalized:
        stage = "在上游素材库预上传/处理时被" if "preupload video_url" in normalized else "被上游"
        details = (
            f"服务端提示：参考视频{stage}判定为可能涉及版权限制。\n"
            "可换用自己的原创视频验证；若认为是误判，请将原始错误中的 Request ID 和素材 ID 提供给服务方排查。\n"
            "“生成音频”控制输出音频，不会关闭参考视频的输入审核。\n"
            + details
        )
    elif "output audio" in normalized and "copyright" in normalized:
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
    candidates.extend(source.get("result_url") for source in sources)
    for source in sources:
        metadata = source.get("metadata")
        if isinstance(metadata, dict):
            candidates.append(metadata.get("url"))
    candidates.extend(source.get("url") for source in sources)
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
    def __init__(self, message: str, response: Any = None, *, retryable: bool = False):
        super().__init__(message)
        self.response = response
        self.retryable = retryable


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
        raise APIError("接口请求超时。", retryable=True) from exc
    except requests.RequestException as exc:
        raise APIError(f"网络请求失败：{redact(str(exc), settings.api_key)}", retryable=True) from exc

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
            hint = " 请检查服务根地址和接口格式；查询旧任务时请选择创建该任务时的接口格式。"
        elif response.status_code == 429:
            hint = " 请求受限，请稍后再试。"
        raise APIError(f"接口返回 HTTP {response.status_code}。{hint}", safe_data,
                       retryable=response.status_code == 429 or response.status_code >= 500)
    if not is_json:
        raise APIError("接口返回的内容不是 JSON，请检查原始响应及 MODEX_BASE_URL。", safe_data)
    return data


def generate_video(
    model: str, mode: str, prompt: str, image_url: str, image_upload: str | None,
    width: Any, height: Any, duration: Any, seed: Any,
    api_format: str = API_FORMAT_SEEDANCE, reference_video_urls: str = "",
    resolution: str = "720p", ratio: str = "16:9",
    generate_audio: bool = True, watermark: bool = False,
) -> Iterator[tuple[str, str | None, Any, str]]:
    """Stream status, video, response history and the direct result URL to UI."""
    settings = load_settings()
    history: list[dict[str, Any]] = []
    task_id: str | None = None
    submitting = False
    try:
        payload = build_payload(
            model, mode, prompt, image_url, image_upload, width, height, duration, seed,
            api_format, reference_video_urls, resolution, ratio, generate_audio, watermark,
        )
        validate_settings(settings)
        yield "正在提交任务…", None, history.copy(), ""
        with requests.Session() as session:
            submitting = True
            response = request_json(
                session, "POST", build_submit_url(settings.base_url, api_format), settings, payload,
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
                    yield f"快速轮询超时；服务端任务可能仍在执行。\n{prefix}\n请保留 task_id，避免重复提交。", None, history.copy(), ""
                    return
                yield f"任务已提交，正在自动查询…\n{prefix}", None, history.copy(), ""
                time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
                # Query once at the boundary as well: the result may have just arrived.
                response = request_json(session, "GET", build_query_url(settings.base_url, task_id, api_format), settings)
                poll_count += 1
                history.append({"stage": f"poll_{poll_count}", "response": redact(response, settings.api_key)})
    except (ValueError, APIError) as exc:
        if isinstance(exc, APIError):
            history.append({"stage": "error", "response": exc.response, "retryable": exc.retryable})
        message = f"错误：{redact(str(exc), settings.api_key)}"
        if isinstance(exc, APIError) and exc.response is not None:
            if extract_failure_reason(exc.response):
                message += f"\n{failure_details(exc.response, settings.api_key)}"
        if task_id:
            message += f"\ntask_id: {task_id}\n已停止本地查询。服务端任务可能仍在执行，请保留任务 ID。"
        elif submitting:
            message += "\n已停止提交且不会自动重试。若服务端已收到请求，任务可能已创建，请先确认再重新提交。"
        yield message, None, history.copy(), ""


def query_existing_task(task_id: str, api_format: str = API_FORMAT_SEEDANCE) -> Iterator[tuple[str, str | None, Any, str]]:
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
            response = request_json(session, "GET", build_query_url(settings.base_url, task_id, api_format), settings)
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
        if isinstance(exc, APIError):
            history.append({"stage": "error", "response": exc.response, "retryable": exc.retryable})
            if exc.response is not None and extract_failure_reason(exc.response):
                message += f"\n{failure_details(exc.response, settings.api_key)}"
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
    api_format: str = API_FORMAT_SEEDANCE, reference_video_urls: str = "",
    resolution: str = "720p", ratio: str = "16:9",
    generate_audio: bool = True, watermark: bool = False,
) -> Iterator[tuple]:
    for status, url, response, link in generate_video(
        model, mode, prompt, image_url, image_upload, width, height, duration, seed,
        api_format, reference_video_urls, resolution, ratio, generate_audio, watermark,
    ):
        yield status, video_html(url), response, link, gr.DownloadButton(value=None, interactive=False), DOWNLOAD_NOTICE, link


def query_existing_task_for_ui(task_id: str, api_format: str = API_FORMAT_SEEDANCE) -> Iterator[tuple]:
    for status, url, response, link in query_existing_task(task_id, api_format):
        yield status, video_html(url), response, link, gr.DownloadButton(value=None, interactive=False), DOWNLOAD_NOTICE, link


def _with_task_recovery(updates: Iterator[tuple], api_format: str, task_id: str = "") -> Iterator[tuple]:
    """Remember the task while streaming; release the queue before slow checks."""
    state: dict[str, Any] = {}
    last_update = None
    for update in updates:
        last_update = update
        history = update[2]
        if not task_id:
            task_id = next((extract_task_id(entry.get("response")) for entry in history
                            if entry.get("stage") == "submit"), None) or ""
        state = {
            "task_id": task_id, "api_format": api_format, "active": False,
            "started": time.monotonic(), "history": history, "video_url": update[3],
        }
        yield (*update, state, task_id or gr.skip(), gr.Timer(active=False),
               gr.Button(interactive=bool(task_id)), gr.Button(interactive=bool(task_id)))
    if not last_update or not task_id or api_format not in API_PATHS:
        return
    history = state["history"]
    last = history[-1] if history else {}
    # A transport/envelope error is not a terminal task failure.
    terminal = (last.get("stage") != "error"
                and extract_status(last.get("response")) in FAILURE_STATUSES)
    retryable = last.get("stage") != "error" or last.get("retryable", False)
    if history and not state["video_url"] and not terminal and retryable:
        state = dict(state, active=True, started=time.monotonic())
        message = (last_update[0] + f"\n已开启后续自动查询，每 {RECOVERY_INTERVAL_SECONDS} 秒检查一次；"
                   "成功后会自动显示视频和 URL。也可点击“立即查询当前任务”。")
        yield (message, *(gr.skip() for _ in range(6)), state, gr.skip(),
               gr.Timer(RECOVERY_INTERVAL_SECONDS, active=True), gr.Button(interactive=True),
               gr.Button(interactive=True))


def generate_video_with_recovery(
    model: str, mode: str, prompt: str, image_url: str, image_upload: str | None,
    width: Any, height: Any, duration: Any, seed: Any,
    api_format: str = API_FORMAT_SEEDANCE, reference_video_urls: str = "",
    resolution: str = "720p", ratio: str = "16:9",
    generate_audio: bool = True, watermark: bool = False,
) -> Iterator[tuple]:
    yield from _with_task_recovery(generate_video_for_ui(
        model, mode, prompt, image_url, image_upload, width, height, duration, seed,
        api_format, reference_video_urls, resolution, ratio, generate_audio, watermark,
    ), api_format)


def query_task_with_recovery(task_id: str, api_format: str = API_FORMAT_SEEDANCE) -> Iterator[tuple]:
    yield from _with_task_recovery(query_existing_task_for_ui(task_id, api_format),
                                   api_format, (task_id or "").strip())


def _recovery_update(state: dict, message: str) -> tuple:
    """Keep the current player, URL and download intact until a new result exists."""
    return (message, gr.skip(), state["history"], *(gr.skip() for _ in range(4)),
            state, gr.skip(), gr.Timer(RECOVERY_INTERVAL_SECONDS, active=state["active"]),
            gr.Button(interactive=True), gr.Button(interactive=True))


def recover_task_for_ui(state: dict | None, force: bool = False) -> tuple:
    """One GET per tick, using the saved task's format, never the current form."""
    if not state or not state.get("task_id") or (not force and not state.get("active")):
        return tuple(gr.skip() for _ in range(12))
    state = dict(state)
    if force:
        state.update(active=True, started=time.monotonic())
    task_id = state["task_id"]
    if time.monotonic() - state["started"] >= RECOVERY_TIMEOUT_SECONDS:
        state["active"] = False
        return _recovery_update(state, f"后续自动查询已达到 2 小时上限，已暂停；服务端任务未取消。\n"
                                f"task_id: {task_id}\n点击“立即查询当前任务”可继续取回结果，无需重新生成。")
    settings = load_settings()
    history = list(state.get("history", []))
    try:
        validate_settings(settings)
        with requests.Session() as session:
            response = request_json(session, "GET", build_query_url(
                settings.base_url, task_id, state["api_format"]), settings)
        history.append({"stage": "recovery", "response": redact(response, settings.api_key)})
        # Retain submission evidence and a bounded tail of subsequent checks.
        state["history"] = history[:1] + history[-239:] if len(history) > 240 else history
        status = extract_status(response)
        prefix = f"task_id: {task_id}\n状态: {status}"
        if status in FAILURE_STATUSES:
            state["active"] = False
            return _recovery_update(state, f"生成失败或已终止。\n{prefix}\n"
                                    f"{failure_details(response, settings.api_key)}")
        url = extract_video_url(response)
        if url:
            same_result = url == state.get("video_url")
            state.update(active=False, video_url=url)
            return (f"已取回视频。\n{prefix}", video_html(url), state["history"], url,
                    gr.skip() if same_result else gr.DownloadButton(value=None, interactive=False),
                    gr.skip() if same_result else DOWNLOAD_NOTICE, gr.skip() if same_result else url,
                    state, gr.skip(), gr.Timer(active=False), gr.Button(interactive=True),
                    gr.Button(interactive=True))
        detail = "任务已完成，视频地址尚未就绪" if status in SUCCESS_STATUSES else "任务仍在处理中"
        return _recovery_update(state, f"{detail}，每 {RECOVERY_INTERVAL_SECONDS} 秒自动查询。\n{prefix}")
    except (APIError, ValueError) as exc:
        retryable = isinstance(exc, APIError) and exc.retryable
        history.append({"stage": "recovery_error", "response": getattr(exc, "response", None),
                        "message": redact(str(exc), settings.api_key)})
        state["history"] = history[:1] + history[-239:] if len(history) > 240 else history
        state["active"] = retryable
        action = (f"将在 {RECOVERY_INTERVAL_SECONDS} 秒后继续查询。" if retryable else
                  "自动查询已暂停，请检查配置后点击“立即查询当前任务”。")
        return _recovery_update(state, f"查询暂未成功：{redact(str(exc), settings.api_key)}\n"
                                f"task_id: {task_id}\n{action}")


def resume_task_for_ui(state: dict | None) -> tuple:
    return recover_task_for_ui(state, force=True)


def pause_task_recovery(state: dict | None) -> tuple:
    if not state or not state.get("task_id"):
        return tuple(gr.skip() for _ in range(12))
    state = dict(state, active=False)
    return _recovery_update(state, f"已暂停本页自动查询；服务端任务未取消。\ntask_id: {state['task_id']}\n"
                            "点击“立即查询当前任务”可继续取回结果。")


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
        current_task = gr.State({})
        recovery_timer = gr.Timer(RECOVERY_INTERVAL_SECONDS, active=False)
        gr.Markdown("# Modex · Seedance 视频生成\n输入画面描述，或结合图片与参考视频，生成一段新视频。")
        with gr.Row():
            with gr.Column(scale=1):
                model = gr.Textbox(label="model", value=settings.default_model, placeholder="填写账号可用的 Seedance 模型名称")
                api_format = gr.Dropdown(
                    [("Seedance 2.0（当前文档）", API_FORMAT_SEEDANCE), ("旧版 Modex（兼容原有任务）", API_FORMAT_LEGACY)],
                    value=API_FORMAT_SEEDANCE, label="接口格式（生成与查询共用）",
                )
                mode = gr.Dropdown(
                    [("文生视频", "text2video"), ("图生视频（首帧）", "image2video"), ("参考视频生成（文字 / 图片 + 视频）", "reference2video")],
                    value="text2video", label="生成方式",
                )
                prompt = gr.Textbox(label="prompt", lines=5, placeholder="描述主体、动作、场景、光线和镜头运动…")
                with gr.Group(visible=False) as reference_fields:
                    reference_video_urls = gr.Textbox(
                        label="参考视频 URL（必填，每行一个，最多 3 个）", lines=3,
                        placeholder="https://example.com/reference.mp4",
                    )
                    gr.Markdown(
                        "填写可公开访问的视频直链；不支持本地路径或视频分享页面。"
                        "可只提供文字和视频，也可在下方添加图片参考。\n\n"
                        "提示词示例：参考视频1的运镜和动作节奏，保持图片1中的主体外观，生成海边奔跑的镜头。"
                        "图片作为主体/风格参考，不保证与首帧完全一致。"
                    )
                with gr.Group(visible=False) as image_fields:
                    image_url = gr.Textbox(label="image_url（优先使用）", placeholder="https://example.com/image.jpg")
                    gr.Markdown(f"**上传提示：** {UPLOAD_NOTICE}。当前上传仅供本地预览，请填写公网 URL 后提交。")
                    image_upload = gr.Image(label="image_upload（本地预览）", type="filepath", sources=["upload"])
                with gr.Group() as seedance_fields:
                    with gr.Row():
                        resolution = gr.Dropdown(RESOLUTIONS, value="720p", label="清晰度 resolution")
                        ratio = gr.Dropdown(RATIOS, value="16:9", label="画面比例 ratio")
                    with gr.Row():
                        generate_audio = gr.Checkbox(value=True, label="生成音频")
                        watermark = gr.Checkbox(value=False, label="添加水印")
                with gr.Group(visible=False) as legacy_fields:
                    with gr.Row():
                        width = gr.Number(label="width", value=1280, precision=0, minimum=1)
                        height = gr.Number(label="height", value=720, precision=0, minimum=1)
                with gr.Row():
                    duration = gr.Number(label="duration（秒）", value=5, precision=0, minimum=1)
                    seed = gr.Number(label="seed", value=0, precision=0)
                gr.Markdown("参考视频需使用支持多模态输入的 Seedance 2.0 模型；清晰度、时长及素材限制以所选模型为准。")
                submit = gr.Button("submit · 生成视频", variant="primary")
                with gr.Accordion("查询已有任务", open=True):
                    gr.Markdown("查询以前创建的任务时，请在上方选择创建任务时使用的接口格式。")
                    existing_task_id = gr.Textbox(label="已有 task_id", placeholder="粘贴任务 ID，取回已有视频")
                    query = gr.Button("查询已有任务（不创建新任务）")
            with gr.Column(scale=1):
                status = gr.Textbox(label="任务状态", value="就绪 · 填写参数后提交", lines=5, interactive=False)
                with gr.Row():
                    resume_query = gr.Button("立即查询当前任务", interactive=False)
                    pause_query = gr.Button("暂停自动查询", size="sm", interactive=False)
                gr.Markdown("等待超过 15 分钟或查询暂时超时后，每 30 秒继续检查，最多 2 小时。"
                            "请保持页面打开；成功后会自动显示视频、URL 和下载入口。")
                gr.Markdown("### 视频结果")
                video = gr.HTML(value=video_html(None))
                with gr.Row():
                    download = gr.DownloadButton("下载视频（本地缓存）", interactive=False, variant="primary")
                    retry_download = gr.Button("重试下载", size="sm")
                download_status = gr.Textbox(label="下载进度", value=DOWNLOAD_NOTICE, interactive=False)
                result_url = gr.Textbox(label="视频 URL（播放器无法播放时可复制打开）", interactive=False)
                with gr.Accordion("接口响应（提交及轮询记录）", open=True):
                    raw_response = gr.JSON(label="响应 JSON", value=[])
        mode.change(
            lambda value: (
                gr.Group(visible=value in {"image2video", "reference2video"}),
                gr.Group(visible=value == "reference2video"),
                gr.Textbox(label="image_url（可选图片参考，优先使用 URL）" if value == "reference2video" else "image_url（优先使用）"),
            ),
            inputs=mode, outputs=[image_fields, reference_fields, image_url], queue=False,
        )
        api_format.change(
            lambda value: (gr.Group(visible=value == API_FORMAT_SEEDANCE), gr.Group(visible=value == API_FORMAT_LEGACY)),
            inputs=api_format, outputs=[seedance_fields, legacy_fields], queue=False,
        )
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
        task_outputs = [status, video, raw_response, result_url, download, download_status, download_url,
                        current_task, existing_task_id, recovery_timer, resume_query, pause_query]
        submit_event = submit.click(
            fn=generate_video_with_recovery,
            inputs=[model, mode, prompt, image_url, image_upload, width, height, duration, seed,
                    api_format, reference_video_urls, resolution, ratio, generate_audio, watermark],
            outputs=task_outputs,
            cancels=[download_event],
            concurrency_limit=1,
            concurrency_id="modex_tasks",
            trigger_mode="once",
            api_name=False,
        )
        query.click(
            fn=query_task_with_recovery,
            inputs=[existing_task_id, api_format],
            outputs=task_outputs,
            cancels=[download_event],
            concurrency_limit=1,
            concurrency_id="modex_tasks",
            trigger_mode="once",
            api_name=False,
        )
        recovery_event = recovery_timer.tick(
            fn=recover_task_for_ui, inputs=current_task, outputs=task_outputs,
            concurrency_limit=1, concurrency_id="modex_tasks", trigger_mode="once",
            show_progress="hidden", api_name=False,
        )
        resume_query.click(
            fn=resume_task_for_ui, inputs=current_task, outputs=task_outputs,
            cancels=[submit_event, recovery_event],
            concurrency_limit=1, concurrency_id="modex_tasks", trigger_mode="once",
            show_progress="hidden", api_name=False,
        )
        pause_query.click(
            fn=pause_task_recovery, inputs=current_task, outputs=task_outputs,
            cancels=[submit_event, recovery_event],
            concurrency_limit=1, concurrency_id="modex_tasks", trigger_mode="once",
            show_progress="hidden", api_name=False,
        )
    return demo


if __name__ == "__main__":
    build_app().queue(max_size=8).launch(server_name="127.0.0.1", server_port=7860, share=False)
