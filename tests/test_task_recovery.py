"""Regression coverage for retrieving results after the foreground poll expires."""

import io
import json
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch

import requests

import app


VIDEO_URL = "https://cdn.example.com/late-result.mp4"
PARAMETERS = {
    "model": "test-model", "mode": "text2video", "prompt": "A lake at sunrise",
    "image_url": "", "image_upload": None, "width": 1280, "height": 720,
    "duration": 5, "seed": 0, "api_format": app.API_FORMAT_SEEDANCE,
}


def http_response(body, status=200):
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response._content = json.dumps(body).encode("utf-8")
    return response


class TaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.settings = app.Settings("https://modex.example.com", "recovery-secret", "test-model")
        self.context.enter_context(patch.object(app, "load_settings", return_value=self.settings))
        self.session = Mock()
        factory = self.context.enter_context(patch.object(app.requests, "Session"))
        factory.return_value.__enter__.return_value = self.session
        self.context.enter_context(redirect_stdout(io.StringIO()))
        self.now = 100.0
        self.context.enter_context(patch.object(app.time, "monotonic", side_effect=lambda: self.now))

    def state(self, **changes):
        return {
            "task_id": "late-job",
            "api_format": app.API_FORMAT_SEEDANCE,
            "active": True,
            "started": 100.0,
            "history": [{"stage": "submit", "response": {"task_id": "late-job", "status": "queued"}}],
            "video_url": "",
        } | changes

    def assert_get_only(self, expected_url="https://modex.example.com/v1/videos/late-job"):
        self.session.request.assert_called_once()
        self.assertEqual(self.session.request.call_args.args[:2], ("GET", expected_url))

    def assert_result_preserved(self, result):
        for index in [1, 3, 4, 5, 6]:
            with self.subTest(output=index):
                self.assertEqual(result[index], app.gr.skip())

    def test_foreground_timeout_remembers_task_and_arms_recovery_without_resubmission(self):
        self.session.request.side_effect = [
            http_response({"task_id": "late-job", "status": "queued"}),
            http_response({"status": "processing"}),
            http_response({"status": "processing"}),
        ]

        def advance(seconds):
            self.now += seconds

        with patch.object(app, "POLL_TIMEOUT_SECONDS", 10), patch.object(app, "POLL_INTERVAL_SECONDS", 5), \
                patch.object(app.time, "sleep", side_effect=advance):
            updates = list(app.generate_video_with_recovery(**PARAMETERS))

        self.assertIn("超时", updates[-1][0])
        self.assertTrue(updates[-1][7]["active"])
        self.assertEqual(updates[-1][7]["task_id"], "late-job")
        self.assertEqual(updates[-1][7]["api_format"], app.API_FORMAT_SEEDANCE)
        self.assertTrue(any(update[8] == "late-job" for update in updates))
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET", "GET"])

    def test_result_available_at_foreground_deadline_is_shown_without_recovery(self):
        self.session.request.side_effect = [
            http_response({"task_id": "late-job", "status": "queued"}),
            http_response({"status": "processing"}),
            http_response({"status": "completed", "video_url": VIDEO_URL}),
        ]

        def advance(seconds):
            self.now += seconds

        with patch.object(app, "POLL_TIMEOUT_SECONDS", 10), patch.object(app, "POLL_INTERVAL_SECONDS", 5), \
                patch.object(app.time, "sleep", side_effect=advance):
            updates = list(app.generate_video_with_recovery(**PARAMETERS))

        self.assertEqual(updates[-1][3], VIDEO_URL)
        self.assertFalse(updates[-1][7]["active"])
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET", "GET"])

    def test_query_pending_task_remembers_format_and_arms_get_only_recovery(self):
        self.session.request.return_value = http_response({"status": "processing"})

        updates = list(app.query_task_with_recovery("  late-job  ", app.API_FORMAT_LEGACY))

        self.assertTrue(updates[-1][7]["active"])
        self.assertEqual(updates[-1][7]["task_id"], "late-job")
        self.assertEqual(updates[-1][7]["api_format"], app.API_FORMAT_LEGACY)
        self.assert_get_only("https://modex.example.com/v1/video/generations/late-job")

    def test_submission_timeout_without_task_id_does_not_arm_recovery_or_retry(self):
        self.session.request.side_effect = requests.Timeout("submission timed out")

        updates = list(app.generate_video_with_recovery(**PARAMETERS))

        self.assertFalse(updates[-1][7]["active"])
        self.assertFalse(updates[-1][7]["task_id"])
        self.session.request.assert_called_once()
        self.assertEqual(self.session.request.call_args.args[0], "POST")

    def test_failed_or_auth_rejected_query_does_not_arm_recovery(self):
        for response in [http_response({"status": "failed", "fail_reason": "rejected"}),
                         http_response({"error": "unauthorized"}, 401)]:
            with self.subTest(response=response):
                self.session.request.reset_mock()
                self.session.request.return_value = response

                updates = list(app.query_task_with_recovery("late-job"))

                self.assertFalse(updates[-1][7]["active"])
                self.assertEqual(updates[-1][7]["task_id"], "late-job")
                self.assert_get_only()

    def test_late_success_restores_player_url_and_download_then_stops_recovery(self):
        state = self.state()
        self.session.request.return_value = http_response({
            "data": {"task_id": "late-job", "status": "SUCCESS", "result_url": VIDEO_URL},
        })

        result = app.recover_task_for_ui(state)

        self.assertEqual(len(result), 12)
        self.assertIn(VIDEO_URL, result[1])
        self.assertEqual(result[3], VIDEO_URL)
        self.assertEqual(result[6], VIDEO_URL)
        self.assertFalse(result[7]["active"])
        self.assertEqual(result[7]["video_url"], VIDEO_URL)
        self.assertEqual(result[7]["task_id"], "late-job")
        self.assertEqual(result[7]["api_format"], app.API_FORMAT_SEEDANCE)
        self.assertEqual(result[2][0], state["history"][0])
        self.assert_get_only()

    def test_transient_network_and_http_errors_keep_recovery_and_existing_result(self):
        for response in [requests.Timeout("read timed out"), http_response({"error": "busy"}, 429),
                         http_response({"error": "unavailable"}, 503)]:
            with self.subTest(response=response):
                self.session.request.reset_mock()
                self.session.request.side_effect = [response]

                result = app.recover_task_for_ui(self.state())

                self.assertTrue(result[7]["active"])
                self.assertEqual(result[7]["task_id"], "late-job")
                self.assert_result_preserved(result)
                self.assert_get_only()

    def test_refreshing_same_result_keeps_ready_or_inflight_download(self):
        self.session.request.return_value = http_response({"status": "completed", "video_url": VIDEO_URL})

        result = app.resume_task_for_ui(self.state(active=False, video_url=VIDEO_URL))

        self.assertEqual(result[3], VIDEO_URL)
        for index in [4, 5, 6]:
            self.assertEqual(result[index], app.gr.skip())
        self.assertFalse(result[7]["active"])
        self.assert_get_only()

    def test_completed_without_result_url_remains_recoverable(self):
        self.session.request.return_value = http_response({"data": {"status": "completed"}})

        result = app.recover_task_for_ui(self.state())

        self.assertTrue(result[7]["active"])
        self.assert_result_preserved(result)
        self.assert_get_only()

    def test_terminal_failure_stops_recovery_and_displays_reason(self):
        self.session.request.return_value = http_response({
            "data": {"status": "failed", "fail_reason": "The upstream model failed."},
        })

        result = app.recover_task_for_ui(self.state())

        self.assertFalse(result[7]["active"])
        self.assertIn("The upstream model failed.", result[0])
        self.assert_get_only()

    def test_task_remembers_legacy_endpoint_and_encodes_task_id(self):
        self.session.request.return_value = http_response({"status": "processing"})

        result = app.recover_task_for_ui(self.state(
            task_id="old/job + 1", api_format=app.API_FORMAT_LEGACY,
        ))

        self.assertEqual(result[7]["api_format"], app.API_FORMAT_LEGACY)
        self.assert_get_only("https://modex.example.com/v1/video/generations/old%2Fjob%20%2B%201")

    def test_inactive_or_missing_task_does_not_request_or_clear_ui(self):
        for state in [None, {}, self.state(active=False), self.state(task_id="")]:
            with self.subTest(state=state):
                result = app.recover_task_for_ui(state)
                self.assertEqual(tuple(result), (app.gr.skip(),) * 12)
        self.session.request.assert_not_called()

    def test_expired_recovery_pauses_and_manual_resume_gets_late_result(self):
        self.now += app.RECOVERY_TIMEOUT_SECONDS + 1

        paused = app.recover_task_for_ui(self.state())

        self.assertFalse(paused[7]["active"])
        self.assert_result_preserved(paused)
        self.session.request.assert_not_called()
        self.session.request.return_value = http_response({"status": "completed", "video_url": VIDEO_URL})

        resumed = app.resume_task_for_ui(paused[7])

        self.assertEqual(resumed[3], VIDEO_URL)
        self.assertEqual(resumed[6], VIDEO_URL)
        self.assertFalse(resumed[7]["active"])
        self.assert_get_only()

    def test_manual_pause_preserves_task_identity_and_never_uses_network(self):
        result = app.pause_task_recovery(self.state())

        self.assertFalse(result[7]["active"])
        self.assertEqual(result[7]["task_id"], "late-job")
        self.assertEqual(result[7]["api_format"], app.API_FORMAT_SEEDANCE)
        self.assert_result_preserved(result)
        self.session.request.assert_not_called()

    def test_non_retryable_query_errors_pause_but_preserve_task_and_result(self):
        for status in [401, 403, 404]:
            with self.subTest(status=status):
                self.session.request.reset_mock()
                self.session.request.return_value = http_response({"error": "query rejected"}, status)

                result = app.recover_task_for_ui(self.state())

                self.assertFalse(result[7]["active"])
                self.assertEqual(result[7]["task_id"], "late-job")
                self.assert_result_preserved(result)
                self.assert_get_only()

    def test_recovery_error_redacts_credentials_from_status_and_history(self):
        self.session.request.return_value = http_response({
            "error": f"provider echoed {self.settings.api_key}",
        }, 503)

        result = app.recover_task_for_ui(self.state())

        self.assertNotIn(self.settings.api_key, json.dumps([result[0], result[2], result[7]]))
        self.assert_get_only()

    def test_pause_and_resume_stay_disabled_until_submission_task_id_is_known(self):
        self.session.request.return_value = http_response({"task_id": "late-job", "video_url": VIDEO_URL})
        generator = app.generate_video_with_recovery(**PARAMETERS)

        submitting = next(generator)

        self.assertFalse(submitting[10].interactive)
        self.assertFalse(submitting[11].interactive)
        self.session.request.assert_not_called()
        complete = list(generator)[-1]
        self.assertTrue(complete[10].interactive)
        self.assertTrue(complete[11].interactive)

    def test_invalid_settings_do_not_start_recovery_for_manually_entered_task(self):
        with patch.object(app, "load_settings", return_value=app.Settings("", "", "test-model")):
            updates = list(app.query_task_with_recovery("late-job"))

        self.assertFalse(updates[-1][7]["active"])
        self.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
