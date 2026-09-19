"""Contract tests for the local client; all HTTP requests are mocked."""

import io
import inspect
import json
import unittest
from concurrent.futures import Future
from contextlib import ExitStack, redirect_stdout
from html.parser import HTMLParser
from unittest.mock import Mock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

import app


PARAMETERS = {
    "model": "seedance-test",
    "mode": "text2video",
    "prompt": "A slow camera pan over a lake",
    "image_url": "",
    "image_upload": None,
    "width": 1280,
    "height": 720,
    "duration": 5,
    "seed": 42,
    "api_format": "legacy",
}
SEEDANCE_PARAMETERS = PARAMETERS | {"api_format": "seedance"}
REFERENCE_VIDEO_URL = "https://cdn.example.com/reference.mp4"
VIDEO_URL = "https://cdn.example.com/result.mp4"
INPUT_VIDEO_COPYRIGHT_REASON = (
    "preupload video_url to asset library failed: cloudwise asset asset-test processing failed: "
    "the request failed because the input video may be related to copyright restrictions. "
    "Request ID: req-test"
)


def http_response(body, status=200, *, json_body=True):
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response._content = (
        json.dumps(body, ensure_ascii=False) if json_body else body
    ).encode("utf-8")
    return response


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.settings = app.Settings(
            "https://modex.example.com", "private-api-key-for-test", "seedance-test"
        )
        self.context.enter_context(patch.object(app, "load_settings", return_value=self.settings))
        self.session = Mock()
        session_factory = self.context.enter_context(patch.object(app.requests, "Session"))
        session_factory.return_value.__enter__.return_value = self.session
        self.session_factory = session_factory
        self.stdout = io.StringIO()
        self.context.enter_context(redirect_stdout(self.stdout))
        self.now = 0.0
        self.context.enter_context(patch.object(app.time, "monotonic", side_effect=lambda: self.now))
        self.context.enter_context(patch.object(app.time, "sleep", side_effect=self.advance_clock))

    def advance_clock(self, seconds):
        self.now += seconds

    def generate(self, responses=(), **changes):
        self.session.request.side_effect = list(responses)
        return list(app.generate_video(**(PARAMETERS | changes)))

    def test_text_payload_has_exact_required_fields_and_omits_image(self):
        payload = app.build_payload(**(PARAMETERS | {
            "image_url": "https://example.com/ignored.jpg",
            "image_upload": "ignored.png",
        }))
        self.assertEqual(payload, {
            "model": PARAMETERS["model"], "prompt": PARAMETERS["prompt"],
            "width": 1280, "height": 720, "duration": 5, "n": 1, "seed": 42,
        })

    def test_image_url_takes_priority_over_upload(self):
        with patch.object(app, "prepare_uploaded_image") as prepare:
            payload = app.build_payload(**(PARAMETERS | {
                "mode": "image2video", "image_url": " https://example.com/input.jpg ",
                "image_upload": "local-image.png",
            }))
        self.assertEqual(payload["image"], "https://example.com/input.jpg")
        self.assertEqual(set(payload), {"model", "prompt", "width", "height", "duration", "n", "seed", "image"})
        prepare.assert_not_called()

    def test_upload_without_url_displays_notice_and_does_not_submit(self):
        updates = self.generate(mode="image2video", image_upload="local-image.png")
        self.assertIn(app.UPLOAD_NOTICE, updates[-1][0])
        self.session_factory.assert_not_called()
        self.session.request.assert_not_called()

    def test_invalid_inputs_fail_before_network(self):
        invalid_inputs = [
            {"model": " "}, {"prompt": ""}, {"mode": "unknown"},
            {"width": 0}, {"height": -1}, {"duration": 1.5},
            {"seed": float("nan")}, {"width": float("inf")}, {"seed": True},
            {"mode": "image2video"},
            {"mode": "image2video", "image_url": "file:///local/image.png"},
        ]
        for changes in invalid_inputs:
            with self.subTest(changes=changes):
                updates = self.generate(**changes)
                self.assertIn("错误", updates[-1][0])
                self.assertIsNone(updates[-1][1])
        self.session_factory.assert_not_called()

    def test_default_seedance_payload_matches_documented_text_format(self):
        original_arguments = [value for key, value in PARAMETERS.items() if key != "api_format"]
        payload = app.build_payload(*original_arguments)
        self.assertEqual(payload, {
            "model": PARAMETERS["model"], "prompt": PARAMETERS["prompt"],
            "duration": 5, "seed": 42, "resolution": "720p", "ratio": "16:9",
            "generate_audio": True, "watermark": False,
        })

    def test_reference_video_payload_matches_documented_content_format(self):
        payload = app.build_payload(**(SEEDANCE_PARAMETERS | {
            "mode": "reference2video", "image_url": "https://example.com/subject.jpg",
            "reference_video_urls": REFERENCE_VIDEO_URL,
            "resolution": "1080p", "ratio": "9:16", "generate_audio": False,
            "watermark": True,
        }))
        self.assertEqual(payload, {
            "model": PARAMETERS["model"], "prompt": PARAMETERS["prompt"],
            "duration": 5, "seed": 42, "resolution": "1080p", "ratio": "9:16",
            "generate_audio": False, "watermark": True,
            "content": [
                {"type": "image_url", "role": "reference_image",
                 "image_url": {"url": "https://example.com/subject.jpg"}},
                {"type": "video_url", "role": "reference_video",
                 "video_url": {"url": REFERENCE_VIDEO_URL}},
            ],
        })

    def test_reference_mode_accepts_one_or_three_videos_without_an_image(self):
        urls = [f"https://cdn.example.com/reference-{index}.mp4" for index in range(3)]
        for count in [1, 3]:
            with self.subTest(count=count):
                payload = app.build_payload(**(SEEDANCE_PARAMETERS | {
                    "mode": "reference2video",
                    "reference_video_urls": "\n  " + "  \r\n\n  ".join(urls[:count]) + "  \n",
                }))
                self.assertEqual(payload["content"], [
                    {"type": "video_url", "role": "reference_video", "video_url": {"url": url}}
                    for url in urls[:count]
                ])
                self.assertNotIn("image", payload)

    def test_seedance_image_role_depends_on_generation_mode_and_url_has_priority(self):
        for mode, role in [("image2video", "first_frame"), ("reference2video", "reference_image")]:
            with self.subTest(mode=mode), patch.object(app, "prepare_uploaded_image") as prepare:
                payload = app.build_payload(**(SEEDANCE_PARAMETERS | {
                    "mode": mode, "image_url": " https://example.com/subject.jpg ",
                    "image_upload": "local-image.png", "reference_video_urls": REFERENCE_VIDEO_URL,
                }))
                self.assertEqual(payload["content"][0], {
                    "type": "image_url", "role": role,
                    "image_url": {"url": "https://example.com/subject.jpg"},
                })
                prepare.assert_not_called()

    def test_reference_image_upload_without_public_url_fails_before_network(self):
        updates = self.generate(
            api_format="seedance", mode="reference2video",
            reference_video_urls=REFERENCE_VIDEO_URL, image_upload="local-image.png",
        )
        self.assertIn(app.UPLOAD_NOTICE, updates[-1][0])
        self.session_factory.assert_not_called()

    def test_invalid_reference_video_inputs_fail_before_network(self):
        invalid_references = [
            "", "  \n\r\n ", "\n".join([REFERENCE_VIDEO_URL] * 4),
            "file:///local/video.mp4", "data:video/mp4;base64,AAA", "not-a-url",
            "https://cdn.example.com/video with spaces.mp4",
            "https://user:password@cdn.example.com/video.mp4",
            "https://cdn.example.com:invalid/video.mp4",
            REFERENCE_VIDEO_URL + "\nfile:///local/second.mp4",
        ]
        for reference in invalid_references:
            with self.subTest(reference=reference):
                updates = self.generate(
                    api_format="seedance", mode="reference2video", reference_video_urls=reference,
                )
                self.assertIn("错误", updates[-1][0])
                self.assertIsNone(updates[-1][1])
        self.session_factory.assert_not_called()

    def test_reference_video_mode_is_rejected_for_legacy_without_network(self):
        updates = self.generate(mode="reference2video", reference_video_urls=REFERENCE_VIDEO_URL)
        self.assertIn("Seedance", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.session_factory.assert_not_called()

    def test_generation_modes_omit_inactive_reference_inputs(self):
        with patch.object(app, "prepare_uploaded_image") as prepare:
            text = app.build_payload(**(SEEDANCE_PARAMETERS | {
                "image_url": "invalid residual image", "image_upload": "local-image.png",
                "reference_video_urls": "invalid residual video", "width": None, "height": None,
            }))
        self.assertNotIn("content", text)
        prepare.assert_not_called()
        image = app.build_payload(**(SEEDANCE_PARAMETERS | {
            "mode": "image2video", "image_url": "https://example.com/input.jpg",
            "reference_video_urls": "invalid residual video",
        }))
        self.assertEqual(len(image["content"]), 1)
        self.assertEqual(image["content"][0]["type"], "image_url")
        legacy = app.build_payload(**(PARAMETERS | {
            "reference_video_urls": REFERENCE_VIDEO_URL, "resolution": "invalid residual value",
            "ratio": "invalid residual value", "generate_audio": False, "watermark": True,
        }))
        self.assertEqual(legacy, app.build_payload(**PARAMETERS))

    def test_invalid_seedance_options_fail_before_network(self):
        for changes in [
            {"resolution": "4k"}, {"ratio": "2:1"}, {"generate_audio": "false"},
            {"watermark": 1}, {"api_format": "unknown"},
        ]:
            with self.subTest(changes=changes):
                updates = self.generate(**({"api_format": "seedance"} | changes))
                self.assertIn("错误", updates[-1][0])
                self.assertIsNone(updates[-1][1])
        self.session_factory.assert_not_called()

    def test_missing_configuration_fails_before_network(self):
        for settings in [
            app.Settings("", "key", "model"),
            app.Settings("https://example.com", "", "model"),
            app.Settings("invalid-base-url", "key", "model"),
        ]:
            with self.subTest(settings=settings):
                with patch.object(app, "load_settings", return_value=settings):
                    updates = self.generate()
                self.assertIn("错误", updates[-1][0])
        self.session_factory.assert_not_called()

    def test_supported_response_field_variants(self):
        for body in [{"id": "job-1"}, {"task_id": "job-1"}, {"data": {"id": "job-1"}}, {"data": {"task_id": "job-1"}}]:
            with self.subTest(body=body):
                self.assertEqual(app.extract_task_id(body), "job-1")
        for body in [{"status": "completed"}, {"data": {"status": "COMPLETED"}}]:
            with self.subTest(body=body):
                self.assertEqual(app.extract_status(body), "completed")
        for body in [{"video_url": VIDEO_URL}, {"url": VIDEO_URL}, {"data": {"video_url": VIDEO_URL}}, {"data": {"url": VIDEO_URL}}]:
            with self.subTest(body=body):
                self.assertEqual(app.extract_video_url(body), VIDEO_URL)

    def test_explicit_video_url_takes_priority_over_generic_task_url(self):
        body = {"url": "https://modex.example.com/tasks/job-1", "data": {"video_url": VIDEO_URL}}
        self.assertEqual(app.extract_video_url(body), VIDEO_URL)

    def test_seedance_metadata_url_supports_response_envelopes_and_beats_task_url(self):
        for depth in range(3):
            body = {"status": "completed", "metadata": {"url": VIDEO_URL}}
            for _ in range(depth):
                body = {"data": body}
            body["url"] = "https://modex.example.com/tasks/job-1"
            with self.subTest(depth=depth):
                self.assertEqual(app.extract_video_url(body), VIDEO_URL)

    def test_explicit_video_url_takes_priority_over_seedance_metadata(self):
        body = {
            "metadata": {"url": "https://cdn.example.com/other-result.mp4"},
            "data": {"content": {"video_url": VIDEO_URL}},
        }
        self.assertEqual(app.extract_video_url(body), VIDEO_URL)
        self.assertIsNone(app.extract_video_url({"metadata": {"url": "generation failed"}}))

    def test_modex_completed_record_supports_result_url(self):
        body = {"code": "success", "message": "", "data": {
            "id": 123, "task_id": "job-1", "status": "SUCCESS",
            "result_url": VIDEO_URL,
        }}
        self.assertEqual(app.extract_video_url(body), VIDEO_URL)

    def test_modex_upstream_content_supports_video_url(self):
        body = {"code": "success", "data": {"data": {
            "status": "succeeded", "content": {"video_url": VIDEO_URL},
        }}}
        self.assertEqual(app.extract_video_url(body), VIDEO_URL)

    def test_explicit_video_fields_take_priority_over_result_and_generic_urls(self):
        for field in [
            {"video_url": VIDEO_URL},
            {"content": {"video_url": VIDEO_URL}},
        ]:
            body = {
                "result_url": "https://cdn.example.com/other-result.mp4",
                "url": "https://modex.example.com/tasks/job-1",
                "data": {"data": field},
            }
            with self.subTest(field=field):
                self.assertEqual(app.extract_video_url(body), VIDEO_URL)

    def test_failure_text_in_result_url_is_not_treated_as_video(self):
        body = {"code": "success", "data": {
            "status": "FAILURE", "result_url": "The provider could not generate this video.",
        }}
        self.assertIsNone(app.extract_video_url(body))
        body["data"]["data"] = {"content": {"video_url": VIDEO_URL}}
        self.assertEqual(app.extract_video_url(body), VIDEO_URL)

    def test_explicit_task_id_takes_priority_over_database_id(self):
        for body in [
            {"id": 123, "task_id": "job-1"},
            {"data": {"id": 123, "task_id": "job-1"}},
            {"id": 123, "data": {"task_id": "job-1"}},
        ]:
            with self.subTest(body=body):
                self.assertEqual(app.extract_task_id(body), "job-1")

    def test_query_existing_task_recovers_modex_video_without_submitting(self):
        body = {"code": "success", "message": "", "data": {
            "id": 123, "task_id": "job-1", "status": "SUCCESS",
            "result_url": VIDEO_URL,
            "data": {"status": "succeeded", "content": {"video_url": VIDEO_URL}},
        }}
        self.session.request.return_value = http_response(body)
        updates = list(app.query_existing_task(" job-1 ", api_format="legacy"))
        self.session.request.assert_called_once()
        call = self.session.request.call_args
        self.assertEqual(call.args[:2], (
            "GET", "https://modex.example.com/v1/video/generations/job-1",
        ))
        self.assertIn("状态: success", updates[-1][0])
        self.assertIn("job-1", updates[-1][0])
        self.assertEqual(updates[-1][1], VIDEO_URL)
        self.assertEqual(updates[-1][2][-1]["response"], body)
        self.assertEqual(updates[-1][3], VIDEO_URL)

    def test_query_existing_failed_task_displays_reason(self):
        reason = "The upstream model is unavailable."
        body = {"code": "success", "data": {
            "task_id": "job-1", "status": "FAILURE",
            "fail_reason": reason, "result_url": reason,
        }}
        self.session.request.return_value = http_response(body)
        updates = list(app.query_existing_task("job-1", api_format="legacy"))
        self.assertIn(reason, updates[-1][0])
        self.assertIn("job-1", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][3], "")
        self.assertEqual(updates[-1][2][-1]["response"], body)
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["GET"])

    def test_query_empty_task_id_does_not_request_api(self):
        for task_id in ["", " "]:
            with self.subTest(task_id=task_id):
                updates = list(app.query_existing_task(task_id, api_format="legacy"))
                self.assertIsNone(updates[-1][1])
                self.assertEqual(updates[-1][3], "")
                self.assertIn("任务", updates[-1][0])
        self.session.request.assert_not_called()

    def test_submission_and_polling_show_status_history_and_video(self):
        bodies = [
            {"task_id": "job/1", "status": "queued"},
            {"data": {"status": "processing"}},
            {"data": {"status": "completed", "video_url": VIDEO_URL}},
        ]
        updates = self.generate([http_response(body) for body in bodies])
        calls = self.session.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["POST", "GET", "GET"])
        self.assertEqual(calls[0].args[1], "https://modex.example.com/v1/video/generations")
        self.assertEqual(calls[1].args[1], "https://modex.example.com/v1/video/generations/job%2F1")
        self.assertEqual(calls[0].kwargs["headers"], {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        })
        self.assertEqual(calls[0].kwargs["json"], app.build_payload(**PARAMETERS))
        self.assertIsNotNone(calls[0].kwargs["timeout"])
        self.assertTrue(any("queued" in update[0] for update in updates))
        self.assertTrue(any("processing" in update[0] for update in updates))
        self.assertEqual(updates[-1][1], VIDEO_URL)
        self.assertEqual(updates[-1][3], VIDEO_URL)
        self.assertEqual([entry["response"] for entry in updates[-1][2]], bodies)
        self.assertIn("processing", self.stdout.getvalue())
        self.assertIn(VIDEO_URL, self.stdout.getvalue())

    def test_seedance_submission_and_polling_use_new_paths_and_metadata_result(self):
        bodies = [
            {"id": "seedance/job 1", "status": "queued"},
            {"id": "seedance/job 1", "status": "in_progress", "progress": 45},
            {"id": "seedance/job 1", "status": "completed", "metadata": {"url": VIDEO_URL}},
        ]
        changes = {
            "api_format": "seedance", "mode": "reference2video",
            "reference_video_urls": REFERENCE_VIDEO_URL, "generate_audio": False,
        }
        updates = self.generate([http_response(body) for body in bodies], **changes)
        calls = self.session.request.call_args_list
        self.assertEqual([(call.args[0], call.args[1]) for call in calls], [
            ("POST", "https://modex.example.com/v1/videos"),
            ("GET", "https://modex.example.com/v1/videos/seedance%2Fjob%201"),
            ("GET", "https://modex.example.com/v1/videos/seedance%2Fjob%201"),
        ])
        self.assertEqual(calls[0].kwargs["json"], app.build_payload(**(PARAMETERS | changes)))
        self.assertIs(calls[0].kwargs["json"]["generate_audio"], False)
        self.assertEqual(updates[-1][1], VIDEO_URL)
        self.assertEqual(updates[-1][3], VIDEO_URL)
        self.assertEqual([entry["response"] for entry in updates[-1][2]], bodies)

    def test_seedance_query_existing_task_defaults_to_new_path_without_submission(self):
        body = {"data": {"status": "completed", "metadata": {"url": VIDEO_URL}}}
        self.session.request.return_value = http_response(body)
        updates = list(app.query_existing_task(" seedance/job 1 "))
        self.session.request.assert_called_once()
        self.assertEqual(self.session.request.call_args.args[:2], (
            "GET", "https://modex.example.com/v1/videos/seedance%2Fjob%201",
        ))
        self.assertEqual(updates[-1][1], VIDEO_URL)
        self.assertEqual(updates[-1][2][-1]["response"], body)

    def test_query_url_selects_format_and_encodes_task_identifier(self):
        self.assertEqual(app.build_query_url("https://modex.example.com/", "job/1?x#y"),
                         "https://modex.example.com/v1/videos/job%2F1%3Fx%23y")
        self.assertEqual(app.build_query_url("https://modex.example.com/", "job/1", "legacy"),
                         "https://modex.example.com/v1/video/generations/job%2F1")
        with self.assertRaises(ValueError):
            app.build_query_url("https://modex.example.com", "job-1", "unknown")

    def test_seedance_failed_poll_keeps_error_and_stops_without_resubmission(self):
        reason = "Reference video format is unsupported."
        body = {"id": "job-1", "status": "failed", "error": {"message": reason}}
        updates = self.generate([
            http_response({"id": "job-1", "status": "queued"}), http_response(body),
        ], api_format="seedance", mode="reference2video", reference_video_urls=REFERENCE_VIDEO_URL)
        self.assertIn(reason, updates[-1][0])
        self.assertIn("job-1", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][2][-1]["response"], body)
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET"])

    def test_seedance_failed_submission_does_not_retry_or_fallback_to_legacy(self):
        for response in [http_response({"error": "unsupported reference input"}, 400),
                         requests.Timeout("submission timeout")]:
            with self.subTest(response=response):
                self.session.request.reset_mock()
                updates = self.generate([response], api_format="seedance", mode="reference2video",
                                        reference_video_urls=REFERENCE_VIDEO_URL)
                self.assertIn("错误", updates[-1][0])
                self.assertIsNone(updates[-1][1])
                self.session.request.assert_called_once()
                self.assertEqual(self.session.request.call_args.args[:2], (
                    "POST", "https://modex.example.com/v1/videos",
                ))

    def test_direct_video_response_needs_no_polling(self):
        updates = self.generate([http_response({"data": {"url": VIDEO_URL}})])
        self.assertEqual(updates[-1][1], VIDEO_URL)
        self.session.request.assert_called_once()

    def test_submission_without_id_or_url_preserves_response(self):
        body = {"message": "accepted without task identifier"}
        updates = self.generate([http_response(body)])
        self.assertIn("task_id", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][2][-1]["response"], body)
        self.session.request.assert_called_once()

    def test_failed_task_stops_and_keeps_error_details(self):
        body = {"data": {"id": "job-1", "status": "failed", "error": "model unavailable"}}
        updates = self.generate([http_response(body)])
        self.assertIn("失败", updates[-1][0])
        self.assertIn("model unavailable", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][2][-1]["response"], body)
        self.session.request.assert_called_once()

    def test_failure_reason_supports_provider_error_shapes_and_nesting(self):
        reason = "The provider could not generate this video."
        for field in [
            {"fail_reason": reason}, {"error": reason},
            {"error": {"message": reason}}, {"error": {"detail": reason}},
            {"message": reason}, {"detail": reason},
        ]:
            for depth in range(3):
                body = field
                for _ in range(depth):
                    body = {"data": body}
                with self.subTest(field=field, depth=depth):
                    self.assertEqual(app.extract_failure_reason(body), reason)

    def test_failure_reason_prefers_task_details_to_envelope_message(self):
        body = {
            "code": "success", "message": "Query succeeded",
            "error": "Generic error", "data": {
                "fail_reason": "Specific task failure",
                "data": {"error": {"message": "Upstream error"}},
            },
        }
        self.assertEqual(app.extract_failure_reason(body), "Specific task failure")
        self.assertEqual(app.extract_failure_reason({
            "message": "Query succeeded", "data": {"message": "Task was rejected"},
        }), "Task was rejected")

    def test_failed_poll_displays_audio_copyright_reason_and_stops(self):
        reason = (
            "The request failed because the output audio may be related to "
            "copyright restrictions."
        )
        bodies = [
            {"id": "job-1", "status": "queued"},
            {"code": "success", "message": "", "data": {
                "id": 123, "task_id": "job-1", "status": "FAILURE",
                "fail_reason": reason, "result_url": reason,
                "data": {"status": "failed", "error": {"message": reason}},
            }},
        ]
        updates = self.generate([http_response(body) for body in bodies])
        self.assertIn(reason, updates[-1][0])
        self.assertIn("音频", updates[-1][0])
        self.assertIn("版权", updates[-1][0])
        self.assertIn("job-1", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][3], "")
        self.assertEqual([entry["response"] for entry in updates[-1][2]], bodies)
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET"])

    def test_reference_video_preupload_http_error_explains_copyright_and_preserves_redacted_details(self):
        reason = INPUT_VIDEO_COPYRIGHT_REASON + f" Credential: {self.settings.api_key}"
        body = {"error": {"message": reason, "type": "upstream_error"}}
        updates = self.generate(
            [http_response(body, 500)], api_format="seedance", mode="reference2video",
            reference_video_urls=REFERENCE_VIDEO_URL,
        )
        status = updates[-1][0]
        self.assertIn("500", status)
        self.assertIn("参考视频", status)
        self.assertIn("版权", status)
        self.assertIn(INPUT_VIDEO_COPYRIGHT_REASON, status)
        self.assertIn("Credential: [REDACTED]", status)
        self.assertNotIn("生成的音频可能涉及版权", status)
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][3], "")
        self.assertEqual(updates[-1][2][-1]["response"], {
            "error": {"message": INPUT_VIDEO_COPYRIGHT_REASON + " Credential: [REDACTED]",
                      "type": "upstream_error"},
        })
        self.assertNotIn(self.settings.api_key, json.dumps(updates))
        self.assertNotIn(self.settings.api_key, self.stdout.getvalue())
        self.session.request.assert_called_once()
        self.assertEqual(self.session.request.call_args.args[:2], (
            "POST", "https://modex.example.com/v1/videos",
        ))

    def test_reference_video_copyright_failed_poll_explains_input_and_stops(self):
        body = {"data": {"status": "failed", "error": {"message": INPUT_VIDEO_COPYRIGHT_REASON}}}
        updates = self.generate([
            http_response({"id": "job-1", "status": "queued"}), http_response(body),
        ], api_format="seedance", mode="reference2video", reference_video_urls=REFERENCE_VIDEO_URL)
        self.assertIn("参考视频", updates[-1][0])
        self.assertIn("版权", updates[-1][0])
        self.assertIn(INPUT_VIDEO_COPYRIGHT_REASON, updates[-1][0])
        self.assertIn("job-1", updates[-1][0])
        self.assertNotIn("生成的音频可能涉及版权", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][2][-1]["response"], body)
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET"])

    def test_query_http_error_explains_reference_video_copyright_without_submission(self):
        body = {"error": {"message": INPUT_VIDEO_COPYRIGHT_REASON}}
        self.session.request.return_value = http_response(body, 500)
        updates = list(app.query_existing_task("job-1", api_format="seedance"))
        self.assertIn("500", updates[-1][0])
        self.assertIn("参考视频", updates[-1][0])
        self.assertIn("版权", updates[-1][0])
        self.assertIn(INPUT_VIDEO_COPYRIGHT_REASON, updates[-1][0])
        self.assertIn("job-1", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][2][-1]["response"], body)
        self.session.request.assert_called_once()
        self.assertEqual(self.session.request.call_args.args[:2], (
            "GET", "https://modex.example.com/v1/videos/job-1",
        ))

    def test_http_output_audio_copyright_error_retains_audio_explanation(self):
        reason = "The request failed because the output audio may be related to copyright restrictions."
        body = {"error": {"message": reason}}
        updates = self.generate([http_response(body, 500)], api_format="seedance")
        self.assertIn("生成的音频", updates[-1][0])
        self.assertIn("版权", updates[-1][0])
        self.assertIn(reason, updates[-1][0])
        self.assertNotIn("参考视频", updates[-1][0])
        self.session.request.assert_called_once()

    def test_http_error_without_reason_does_not_invent_a_copyright_explanation(self):
        body = {"error": {}, "message": ""}
        generated = self.generate([http_response(body, 500)], api_format="seedance")
        self.session.request.side_effect = None
        self.session.request.return_value = http_response(body, 500)
        queried = list(app.query_existing_task("job-1", api_format="seedance"))
        for updates in [generated, queried]:
            with self.subTest(status=updates[-1][0]):
                self.assertIn("500", updates[-1][0])
                self.assertNotIn("版权", updates[-1][0])
                self.assertNotIn("失败原因", updates[-1][0])
                self.assertIsNone(updates[-1][1])
                self.assertEqual(updates[-1][2][-1]["response"], body)
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET"])

    def test_nested_upstream_failure_status_and_error_stop_polling(self):
        reason = "The upstream model is unavailable."
        body = {"code": "success", "data": {"data": {
            "status": "failed", "error": {"message": reason},
        }}}
        updates = self.generate([
            http_response({"id": "job-1", "status": "queued"}),
            http_response(body),
        ])
        self.assertEqual(app.extract_status(body), "failed")
        self.assertIn(reason, updates[-1][0])
        self.assertIn("job-1", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][2][-1]["response"], body)
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET"])

    def test_failed_task_reason_redacts_api_key_from_status_and_history(self):
        body = {"id": "job-1", "status": "FAILURE", "data": {"data": {
            "error": {"message": f"Credential rejected: {self.settings.api_key}"},
        }}}
        updates = self.generate([http_response(body)])
        self.assertIn("Credential rejected: [REDACTED]", updates[-1][0])
        self.assertNotIn(self.settings.api_key, json.dumps(updates))
        self.assertNotIn(self.settings.api_key, self.stdout.getvalue())
        self.session.request.assert_called_once()

    def test_failed_task_without_reason_shows_friendly_fallback(self):
        body = {"id": "job-1", "status": "failed", "fail_reason": " ", "message": ""}
        updates = self.generate([http_response(body)])
        self.assertIn("未提供具体失败原因", updates[-1][0])
        self.assertIn("job-1", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(updates[-1][2][-1]["response"], body)
        self.session.request.assert_called_once()

    def test_completed_task_without_url_reports_missing_result(self):
        updates = self.generate([
            http_response({"id": "job-1", "status": "completed"}),
            http_response({"status": "completed"}),
        ])
        self.assertIn("URL", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual(self.session.request.call_count, 2)

    def test_completed_submission_can_get_video_from_query_response(self):
        updates = self.generate([
            http_response({"id": "job-1", "status": "completed"}),
            http_response({"data": {"status": "completed", "video_url": VIDEO_URL}}),
        ])
        self.assertEqual(updates[-1][1], VIDEO_URL)
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET"])

    def test_http_errors_preserve_raw_response_and_do_not_retry_submission(self):
        for status in [401, 404, 429, 500]:
            with self.subTest(status=status):
                self.session.request.reset_mock()
                body = {"error": f"provider response for HTTP {status}"}
                updates = self.generate([http_response(body, status)])
                self.assertIn(str(status), updates[-1][0])
                self.assertEqual(updates[-1][2][-1]["response"], body)
                self.session.request.assert_called_once()

    def test_non_json_body_is_printed_and_displayed(self):
        updates = self.generate([http_response("<html>upstream error</html>", json_body=False)])
        self.assertIn("JSON", updates[-1][0])
        self.assertEqual(updates[-1][2][-1]["response"]["body"], "<html>upstream error</html>")
        self.assertIn("upstream error", self.stdout.getvalue())

    def test_timeout_and_connection_error_do_not_retry_submission(self):
        for error in [requests.Timeout("read timeout"), requests.ConnectionError("connection lost")]:
            with self.subTest(error=type(error).__name__):
                self.session.request.reset_mock()
                updates = self.generate([error])
                self.assertIn("错误", updates[-1][0])
                self.assertIn("重试", updates[-1][0])
                self.assertIsNone(updates[-1][1])
                self.session.request.assert_called_once()

    def test_polling_error_preserves_task_id(self):
        updates = self.generate([
            http_response({"id": "recoverable-job", "status": "queued"}),
            http_response({"error": "poll endpoint unavailable"}, 404),
        ])
        self.assertIn("recoverable-job", updates[-1][0])
        self.assertIn("404", updates[-1][0])
        self.assertEqual(len(updates[-1][2]), 2)
        self.assertEqual(self.session.request.call_count, 2)

    def test_polling_timeout_stops_without_duplicate_submission(self):
        with patch.object(app, "POLL_TIMEOUT_SECONDS", 10), patch.object(app, "POLL_INTERVAL_SECONDS", 5):
            updates = self.generate([
                http_response({"id": "slow-job", "status": "queued"}),
                http_response({"status": "processing"}),
                http_response({"status": "processing"}),
            ])
        self.assertIn("超时", updates[-1][0])
        self.assertIn("slow-job", updates[-1][0])
        self.assertIsNone(updates[-1][1])
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET", "GET"])

    def test_api_key_is_redacted_from_displayed_and_printed_response(self):
        body = {"error": self.settings.api_key, "details": [{"echo": f"Bearer {self.settings.api_key}"}]}
        updates = self.generate([http_response(body, 400)])
        displayed = json.dumps(updates, ensure_ascii=False)
        self.assertNotIn(self.settings.api_key, displayed)
        self.assertNotIn(self.settings.api_key, self.stdout.getvalue())
        self.assertIn("[REDACTED]", displayed)

    def test_api_key_is_redacted_from_network_error(self):
        updates = self.generate([requests.ConnectionError(f"echoed credential: {self.settings.api_key}")])
        self.assertNotIn(self.settings.api_key, json.dumps(updates))
        self.assertIn("[REDACTED]", updates[-1][0])

    def test_ui_preserves_api_result_and_safely_embeds_remote_video_url(self):
        url = VIDEO_URL + '?token=abc&meta="><script>alert(\'injected\')</script>'
        history = [{"stage": "submit", "response": {"video_url": url}}]
        with patch.object(app, "generate_video", return_value=iter([
            ("生成完成", url, history, url),
        ])):
            updates = list(app.generate_video_for_ui(**PARAMETERS))

        status, markup, response, link, download, download_status, download_url = updates[-1]
        self.assertEqual(status, "生成完成")
        self.assertEqual(response, history)
        self.assertEqual(link, url)
        self.assertEqual(download_url, url)
        self.assertIsNone(download.value)
        self.assertFalse(download.interactive)

        class VideoParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.elements = []

            def handle_starttag(self, tag, attrs):
                self.elements.append((tag, dict(attrs)))

        parser = VideoParser()
        parser.feed(markup)
        self.assertEqual([tag for tag, _ in parser.elements], ["video"])
        self.assertEqual(parser.elements[0][1]["src"], url)
        self.assertNotIn("onerror", parser.elements[0][1])
        self.assertIn("&amp;", markup)
        self.assertIn("&quot;", markup)
        self.assertIn("&lt;script&gt;", markup)
        self.assertEqual(app.gr.HTML(render=False).postprocess(markup), markup)
        self.session.request.assert_not_called()

    def test_ui_forwards_all_seedance_reference_generation_options(self):
        parameters = SEEDANCE_PARAMETERS | {
            "mode": "reference2video", "image_url": "https://example.com/input.jpg",
            "reference_video_urls": REFERENCE_VIDEO_URL, "resolution": "1080p", "ratio": "9:16",
            "generate_audio": False, "watermark": True,
        }
        signature = inspect.signature(app.generate_video)
        with patch.object(app, "generate_video", return_value=iter([
            ("生成完成", VIDEO_URL, [], VIDEO_URL),
        ])) as generate:
            updates = list(app.generate_video_for_ui(**parameters))
        generate.assert_called_once()
        forwarded = signature.bind(*generate.call_args.args, **generate.call_args.kwargs)
        forwarded.apply_defaults()
        self.assertEqual(forwarded.arguments, parameters)
        self.assertEqual(updates[-1][3], VIDEO_URL)
        self.assertEqual(updates[-1][6], VIDEO_URL)
        self.session.request.assert_not_called()

    def test_query_ui_forwards_selected_api_format(self):
        signature = inspect.signature(app.query_existing_task)
        for api_format in ["seedance", "legacy"]:
            with self.subTest(api_format=api_format), patch.object(app, "query_existing_task", return_value=iter([
                ("已取回视频", VIDEO_URL, [], VIDEO_URL),
            ])) as query:
                updates = list(app.query_existing_task_for_ui("job-1", api_format))
                query.assert_called_once()
                forwarded = signature.bind(*query.call_args.args, **query.call_args.kwargs)
                forwarded.apply_defaults()
                self.assertEqual(forwarded.arguments, {"task_id": "job-1", "api_format": api_format})
                self.assertEqual(updates[-1][3], VIDEO_URL)
        self.session.request.assert_not_called()

    def test_download_returns_local_file_and_does_not_use_modex_session(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"video bytes")
            future = Future()
            future.set_result(path)
            job = Mock(future=future)
            job.progress.downloaded = len(b"video bytes")
            with patch.object(app.VIDEO_CACHE, "get", return_value=job) as get:
                updates = list(app.prepare_video_download(VIDEO_URL))
            get.assert_called_once_with(VIDEO_URL)
            message, button = updates[-1]
            self.assertIn("已准备好", message)
            self.assertEqual(Path(button.value["path"]).read_bytes(), path.read_bytes())
            self.assertTrue(button.interactive)
        self.session.request.assert_not_called()

    def test_download_failure_keeps_button_disabled_and_offers_retry(self):
        with patch.object(app.VIDEO_CACHE, "get", side_effect=app.DownloadError("源站连接超时")):
            updates = list(app.prepare_video_download(VIDEO_URL))
        self.assertIn("重试", updates[-1][0])
        self.assertIn("视频 URL", updates[-1][0])
        self.assertIsNone(updates[-1][1].value)
        self.assertFalse(updates[-1][1].interactive)

    def test_empty_download_url_resets_button_without_network(self):
        with patch.object(app.VIDEO_CACHE, "get") as get:
            updates = list(app.prepare_video_download(""))
        get.assert_not_called()
        self.assertIsNone(updates[-1][1].value)
        self.assertFalse(updates[-1][1].interactive)


if __name__ == "__main__":
    unittest.main()
