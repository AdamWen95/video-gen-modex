"""Bounded, streaming video downloads with validated ranges and a disk cache."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from urllib.parse import urlsplit

import requests


DOWNLOAD_TIMEOUT = (10, 15)
MAX_DOWNLOAD_SECONDS = 30 * 60
MAX_VIDEO_BYTES = 2 * 1024**3
PARALLEL_THRESHOLD = 4 * 1024**2
RANGE_WORKERS = 4
CHUNK_SIZE = 64 * 1024
HEADERS = {"Accept-Encoding": "identity"}


class DownloadError(Exception):
    """A safe error that does not expose signed URLs or credentials."""


@dataclass(frozen=True)
class DownloadProgress:
    downloaded: int = 0
    total: int | None = None
    mode: str = "正在连接视频源站"


def cache_path(url: str, directory: Path) -> Path:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise DownloadError("视频地址必须是 HTTP(S) URL。")
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".mp4", ".webm", ".mov", ".m4v"}:
        suffix = ".mp4"
    # Include the query: distinct versions/transformations must not share a file.
    key = hashlib.sha256(url.encode()).hexdigest()
    return directory / f"video-{key}{suffix}"


def _check_response(response: requests.Response) -> None:
    if response.status_code not in {200, 206}:
        raise DownloadError(f"视频源站返回 HTTP {response.status_code}，可重新查询任务获取新链接后重试。")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
    if content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
        raise DownloadError("视频源站返回了错误页面，未保存为视频。")
    if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
        raise DownloadError("源站返回了不支持的压缩传输格式。")


def _content_range(response: requests.Response) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
    return tuple(map(int, match.groups())) if match else None


def download_video(
    url: str, directory: Path, progress: Callable[[DownloadProgress], None] = lambda _: None,
) -> Path:
    """Fetch once; publish only a complete file, never a partial download.

    One one-byte GET probes Range support. A 200 response is consumed directly.
    Parallel downloads require a validator; rejected/inconsistent ranges fall
    back to one ordinary GET. No Modex API authorization is sent to the CDN.
    """
    destination = cache_path(url, directory)
    if destination.is_file() and destination.stat().st_size:
        size = destination.stat().st_size
        progress(DownloadProgress(size, size, "已使用本地缓存"))
        return destination
    started = time.monotonic()
    counter_lock = threading.Lock()
    downloaded = 0

    def report(total: int | None, mode: str, amount: int = 0, reset: bool = False) -> None:
        nonlocal downloaded
        with counter_lock:
            downloaded = amount if reset else downloaded + amount
            if downloaded > MAX_VIDEO_BYTES or (total is not None and total > MAX_VIDEO_BYTES):
                raise DownloadError("视频超过 2 GiB，请使用原始视频 URL 下载。")
            if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                raise DownloadError("下载等待超过 30 分钟，请重试或使用原始视频 URL。")
            progress(DownloadProgress(downloaded, total, mode))

    def copy_response(response, target, expected, total, mode, stop=None):
        received = 0
        for chunk in response.iter_content(CHUNK_SIZE):
            if stop is not None and stop.is_set():
                raise DownloadError("分段下载已停止。")
            if not chunk:
                continue
            received += len(chunk)
            if expected is not None and received > expected:
                raise DownloadError("源站返回的视频长度不一致。")
            report(total, mode, len(chunk))
            target.write(chunk)
        if not received or (expected is not None and received != expected):
            raise DownloadError("视频传输不完整，请重试。")

    def sequential(response, temporary):
        _check_response(response)
        if response.status_code != 200:
            raise DownloadError("源站未返回完整视频。")
        length = response.headers.get("Content-Length", "")
        total = int(length) if length.isdigit() else None
        report(total, "普通下载", reset=True)
        with temporary.open("wb") as target:
            copy_response(response, target, total, total, "普通下载")

    try:
        directory.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".download-", dir=directory) as staging:
            temporary = Path(staging) / "video.part"
            # A fresh session never inherits the Modex request's Bearer token.
            with requests.Session() as session:
                with session.get(url, headers=HEADERS | {"Range": "bytes=0-0"},
                                 stream=True, timeout=DOWNLOAD_TIMEOUT) as probe:
                    if probe.status_code == 200:
                        sequential(probe, temporary)
                    elif probe.status_code in {400, 405, 416, 501}:
                        # Some signed/CDN endpoints reject Range outright.
                        pass
                    else:
                        _check_response(probe)
                        span = _content_range(probe)
                        etag = probe.headers.get("ETag", "")
                        validator = etag if etag and not etag.startswith("W/") else probe.headers.get("Last-Modified")
                        if span and span[:2] == (0, 0) and span[2] >= PARALLEL_THRESHOLD and validator:
                            total = span[2]
                            report(total, f"{RANGE_WORKERS} 路并行下载", reset=True)
                            # Close the probe before opening the four connections.
                            probe.close()
                            stop = threading.Event()
                            with temporary.open("wb") as target:
                                target.truncate(total)

                            def fetch_range(start, end):
                                try:
                                    with requests.Session() as part_session:
                                        with part_session.get(
                                            url, headers=HEADERS | {"Range": f"bytes={start}-{end}", "If-Range": validator},
                                            stream=True, timeout=DOWNLOAD_TIMEOUT,
                                        ) as part:
                                            _check_response(part)
                                            if part.status_code != 206 or _content_range(part) != (start, end, total):
                                                raise DownloadError("源站不支持可靠的分段下载。")
                                            if etag and part.headers.get("ETag") != etag:
                                                raise DownloadError("下载期间视频内容发生变化。")
                                            with temporary.open("r+b") as target:
                                                target.seek(start)
                                                copy_response(part, target, end - start + 1, total,
                                                              f"{RANGE_WORKERS} 路并行下载", stop)
                                except Exception:
                                    stop.set()
                                    raise

                            try:
                                with ThreadPoolExecutor(max_workers=RANGE_WORKERS) as executor:
                                    futures = [executor.submit(fetch_range, total * i // RANGE_WORKERS,
                                                               total * (i + 1) // RANGE_WORKERS - 1)
                                               for i in range(RANGE_WORKERS)]
                                    for future in futures:
                                        future.result()
                            except (DownloadError, requests.RequestException):
                                temporary.unlink(missing_ok=True)
                                report(None, "分段下载不可用，正在切换普通下载", reset=True)
                if not temporary.exists():
                    with session.get(url, headers=HEADERS, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
                        sequential(response, temporary)
            temporary.replace(destination)
        return destination
    except requests.RequestException as exc:
        raise DownloadError("视频源站连接中断或超时，请重试；原始视频 URL 仍可使用。") from exc
    except OSError as exc:
        raise DownloadError("本地视频缓存写入失败，请检查磁盘空间及目录权限。") from exc


@dataclass
class DownloadJob:
    progress: DownloadProgress = DownloadProgress(mode="等待准备下载")
    future: Future | None = None


class DownloadCache:
    """Reuse active/completed jobs, with at most two videos fetched at once."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = threading.Lock()
        self._jobs: dict[str, DownloadJob] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="video-download")

    def get(self, url: str) -> DownloadJob:
        key = str(cache_path(url, self.directory))
        with self._lock:
            job = self._jobs.get(key)
            if job and job.future:
                if not job.future.done():
                    return job
                if job.future.exception() is None and job.future.result().is_file():
                    return job
            # Keep only active jobs; completed files remain reusable on disk.
            self._jobs = {k: v for k, v in self._jobs.items() if v.future and not v.future.done()}
            if len(self._jobs) >= 8:
                raise DownloadError("等待下载的视频较多，请稍后点击重试。")
            job = DownloadJob()
            self._jobs[key] = job
            job.future = self._executor.submit(download_video, url, self.directory,
                                                lambda value: setattr(job, "progress", value))
            return job
