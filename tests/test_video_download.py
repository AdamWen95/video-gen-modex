"""Download integrity tests against a local HTTP server; no external requests."""

import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import video_download


VIDEO = bytes(range(256)) * 257
ETAG = '"video-version-1"'


@contextmanager
def video_source(mode="ranges"):
    requests_seen = []
    record_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requested_range = self.headers.get("Range")
            with record_lock:
                requests_seen.append(dict(self.headers))
            body = VIDEO
            status = 200
            content_range = None
            content_type = "video/mp4"
            length = len(body)
            if mode == "html":
                body = b"<html>CDN error page</html>"
                content_type = "text/html; charset=utf-8"
                length = len(body)
            elif mode == "truncated":
                body = VIDEO[:1000]
            elif mode == "unknown_length":
                length = None
            elif requested_range and mode != "no_ranges":
                start, end = map(int, requested_range.removeprefix("bytes=").split("-"))
                status = 206
                body = VIDEO[start:end + 1]
                length = len(body)
                content_range = f"bytes {start}-{end}/{len(VIDEO)}"
                if requested_range != "bytes=0-0":
                    if mode == "part_returns_full":
                        status = 200
                        body = VIDEO
                        length = len(body)
                        content_range = None
                    elif mode == "wrong_range":
                        content_range = f"bytes {start + 1}-{end}/{len(VIDEO)}"
                    elif mode == "truncated_part":
                        body = body[:100]
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("ETag", ETAG)
            if length is not None:
                self.send_header("Content-Length", str(length))
            if content_range:
                self.send_header("Content-Range", content_range)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Rejected ranges/probes are intentionally closed unread.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/result.mp4?signature=test", requests_seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class VideoDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "cache"
        threshold = patch.object(video_download, "PARALLEL_THRESHOLD", 1024)
        threshold.start()
        self.addCleanup(threshold.stop)
        chunk_size = patch.object(video_download, "CHUNK_SIZE", 1024)
        chunk_size.start()
        self.addCleanup(chunk_size.stop)

    def assert_published_only(self, path):
        self.assertEqual(path.read_bytes(), VIDEO)
        self.assertEqual(list(self.directory.iterdir()), [path])

    def test_four_ranges_preserve_bytes_and_headers(self):
        progress = []
        with video_source() as (url, seen):
            result = video_download.download_video(url, self.directory, progress.append)
        self.assert_published_only(result)
        expected_ranges = [
            f"bytes={len(VIDEO) * i // 4}-{len(VIDEO) * (i + 1) // 4 - 1}"
            for i in range(4)
        ]
        self.assertEqual(seen[0]["Range"], "bytes=0-0")
        self.assertCountEqual([request["Range"] for request in seen[1:]], expected_ranges)
        for request in seen:
            self.assertEqual(request["Accept-Encoding"], "identity")
            self.assertNotIn("Authorization", request)
        self.assertTrue(all(request["If-Range"] == ETAG for request in seen[1:]))
        self.assertEqual(progress[-1].downloaded, len(VIDEO))
        self.assertEqual(progress[-1].total, len(VIDEO))

    def test_cache_hit_does_not_repeat_remote_request(self):
        progress = []
        with video_source() as (url, seen):
            first = video_download.download_video(url, self.directory)
            request_count = len(seen)
            second = video_download.download_video(url, self.directory, progress.append)
            self.assertEqual(len(seen), request_count)
        self.assertEqual(first, second)
        self.assert_published_only(second)
        self.assertEqual(progress[-1].downloaded, len(VIDEO))
        self.assertIn("缓存", progress[-1].mode)

    def test_source_ignoring_range_is_consumed_without_second_request(self):
        with video_source("no_ranges") as (url, seen):
            result = video_download.download_video(url, self.directory)
        self.assert_published_only(result)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["Range"], "bytes=0-0")

    def test_unreliable_parts_fall_back_to_full_response(self):
        for mode in ("part_returns_full", "wrong_range", "truncated_part"):
            with self.subTest(mode=mode), video_source(mode) as (url, seen):
                progress = []
                result = video_download.download_video(url, self.directory, progress.append)
                self.assertEqual(result.read_bytes(), VIDEO)
                self.assertEqual(sum("Range" not in request for request in seen), 1)
                self.assertIn("切换普通下载", " ".join(update.mode for update in progress))
                self.assertEqual(progress[-1].downloaded, len(VIDEO))
                self.assertFalse(list(self.directory.glob(".download-*")))

    def test_truncated_response_is_not_published_and_staging_is_removed(self):
        with video_source("truncated") as (url, _):
            with self.assertRaises(video_download.DownloadError):
                video_download.download_video(url, self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_html_error_page_is_not_published_and_staging_is_removed(self):
        with video_source("html") as (url, _):
            with self.assertRaisesRegex(video_download.DownloadError, "错误页面"):
                video_download.download_video(url, self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_known_and_unknown_length_size_limits_do_not_publish_files(self):
        for mode in ("no_ranges", "unknown_length", "ranges"):
            with self.subTest(mode=mode), video_source(mode) as (url, _):
                with patch.object(video_download, "MAX_VIDEO_BYTES", 2048):
                    with self.assertRaisesRegex(video_download.DownloadError, "超过"):
                        video_download.download_video(url, self.directory)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_signed_url_query_distinguishes_cached_files(self):
        first = video_download.cache_path("https://example.com/video.mp4?version=1", self.directory)
        second = video_download.cache_path("https://example.com/video.mp4?version=2", self.directory)
        self.assertNotEqual(first, second)
        self.assertNotIn("version", first.name)


class DownloadCacheTests(unittest.TestCase):
    def test_same_url_reuses_inflight_job_and_completed_file(self):
        with TemporaryDirectory() as temporary:
            cache = video_download.DownloadCache(Path(temporary))
            release = threading.Event()
            started = threading.Event()
            destination = Path(temporary) / "result.mp4"

            def fetch(url, directory, progress):
                started.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("Test did not release downloader")
                destination.write_bytes(VIDEO)
                progress(video_download.DownloadProgress(len(VIDEO), len(VIDEO), "complete"))
                return destination

            try:
                with patch.object(video_download, "download_video", side_effect=fetch) as download:
                    first = cache.get("http://example.com/result.mp4")
                    self.assertTrue(started.wait(timeout=2))
                    second = cache.get("http://example.com/result.mp4")
                    self.assertIs(first, second)
                    self.assertIs(first.future, second.future)
                    release.set()
                    self.assertEqual(first.future.result(timeout=2), destination)
                    self.assertIs(cache.get("http://example.com/result.mp4"), first)
                    self.assertEqual(first.progress.downloaded, len(VIDEO))
                    download.assert_called_once()
            finally:
                release.set()
                cache._executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
