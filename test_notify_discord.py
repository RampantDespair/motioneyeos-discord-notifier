"""Tests for motionEye recording lookup and Discord notification delivery."""

import contextlib
import errno
import io
import json
from pathlib import Path
import runpy
import socket
import ssl
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

NOTIFIER = runpy.run_path(str(Path(__file__).with_name("notify-discord.py")))
MAIN_GLOBALS = NOTIFIER["main"].__globals__
WEBHOOK = "https://discord.example/api/webhooks/123/-test-token"
MOVIES = [
    {"path": "/2026-09-14/17-12-02.mp4", "timestamp": 200},
    {"path": "/2026-09-13/20-00-01.mp4", "timestamp": 100},
]


def arguments(event="end"):
    """Return a CLI invocation with fake credentials and no storage settings."""
    values = [
        "--webhook-url", WEBHOOK, "--event", event, "-n", "server rack",
        "-t", "2026-09-14 17:15:00",
    ]
    if event == "end":
        values += ["--motioneye-url", "http://motioneye.example", "--camera-id", "1"]
    return values


class NotificationTests(unittest.TestCase):
    """Exercise the public command flow and error behavior without external requests."""

    def test_start_only_contacts_discord(self):
        """A start event has no recording or motionEye dependency."""
        mock_request = unittest.mock.Mock(return_value=(200, b'{"id":"1"}'))
        with patch.dict(MAIN_GLOBALS, request=mock_request):
            self.assertEqual(NOTIFIER["main"](arguments("start")), 0)
            calls = MAIN_GLOBALS["request"].call_args_list
        self.assertEqual(len(calls), 1)
        payload = json.loads(calls[0].args[2])
        self.assertEqual(
            payload["content"], "Motion started on server rack at `2026-09-14 17:15:00`"
        )
        self.assertNotIn("components", payload)
        self.assertNotIn("attachments", payload)
        self.assertEqual(calls[0].args[3]["User-Agent"], NOTIFIER["DISCORD_USER_AGENT"])

    def test_newest_movie_is_selected_across_dates_and_api_order(self):
        """Timestamps determine the result, including when returned as numeric strings."""
        movies = MOVIES + [{"path": "/older-name.mp4", "timestamp": "300"}]
        self.assertEqual(NOTIFIER["newest_recording"](movies)["path"], "/older-name.mp4")

    def test_manual_command_expands_time_placeholders(self):
        """A command run outside Motion renders the format using the local clock."""
        date_format = "%Y-%m-%d %H:%M:%S"
        before = time.strftime(date_format)
        mock_request = unittest.mock.Mock(return_value=(200, b'{"id":"1"}'))
        with patch.dict(MAIN_GLOBALS, request=mock_request):
            self.assertEqual(
                NOTIFIER["main"](arguments("start") + ["-t", date_format]), 0
            )
        after = time.strftime(date_format)
        payload = json.loads(mock_request.call_args.args[2])
        self.assertIn(payload["content"], [
            "Motion started on server rack at `" + before + "`",
            "Motion started on server rack at `" + after + "`",
        ])

    def test_bad_movie_lists_are_rejected(self):
        """An empty or malformed list must never produce a guessed recording URL."""
        invalid = [
            None, [], {}, [None], [{"path": "/clip.mp4"}],
            [{"path": "", "timestamp": 1}], [{"path": "/clip.mp4", "timestamp": "nan"}],
            [{"path": "/clip.mp4", "timestamp": float("inf")}],
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(NOTIFIER["NotificationError"]):
                    NOTIFIER["newest_recording"](value)

    def test_playback_url_preserves_prefix_and_encodes_recording_name(self):
        """The server-provided name determines the date, extension, and nested path."""
        result = NOTIFIER["playback_url"](
            "https://camera.example:8765/meye/", 7,
            {"path": "/2026-09-13/server rack #1%?.mp4"},
        )
        self.assertEqual(
            result,
            "https://camera.example:8765/meye/movie/7/playback/"
            "2026-09-13/server%20rack%20%231%25%3F.mp4",
        )

    def test_recording_names_cannot_escape_the_playback_route(self):
        """Reject traversal names rather than composing a misleading link."""
        for path in ("/../clip.mp4", "/date/./clip.mp4", "date\\clip.mp4"):
            with self.subTest(path=path):
                with self.assertRaises(NOTIFIER["NotificationError"]):
                    NOTIFIER["playback_url"]("https://camera.example", 1, {"path": path})

    def test_link_and_media_gallery_use_identical_urls(self):
        """Discord receives a link and an external media URL with no upload fields."""
        args = SimpleNamespace(event="end", name="server rack", time="17:15:00")
        url = "http://motioneye.example/movie/1/playback/2026-09-14/17-12-02.mp4"
        payload = NOTIFIER["build_payload"](args, url)
        self.assertEqual(payload["flags"], 32768)
        text, gallery = payload["components"]
        self.assertEqual(
            text, {"type": 10, "content": "Motion ended on server rack at `17:15:00`\n" + url}
        )
        self.assertEqual(gallery, {"type": 12, "items": [{"media": {"url": url}}]})
        self.assertEqual(set(payload), {"flags", "components", "allowed_mentions"})

    def test_empty_list_stops_before_posting_to_discord(self):
        """A missing recording is reported as a failure, not an unrelated old link."""
        mock_request = unittest.mock.Mock(return_value=(200, b'{"mediaList": []}'))
        with patch.dict(MAIN_GLOBALS, request=mock_request):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(NOTIFIER["main"](arguments()), 1)
        self.assertEqual(mock_request.call_count, 1)
        self.assertIn("No recordings", errors.getvalue())

    def test_discord_errors_and_missing_confirmation_fail(self):
        """HTTP failures and unconfirmed delivery must exit nonzero."""
        for response in ((429, b'{}'), (500, b'{}'), (200, b'{}'), (200, b'not-json')):
            with self.subTest(response=response):
                with patch.dict(MAIN_GLOBALS, request=unittest.mock.Mock(return_value=response)):
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        self.assertEqual(NOTIFIER["main"](arguments("start")), 1)
                self.assertNotIn("test-token", errors.getvalue())

    def test_argument_validation(self):
        """Validate required end settings and reject the removed storage arguments."""
        for extra in (
            ["--camera-id", "0"], ["--motioneye-password", "secret"],
            ["--recording-root", "/unused"], ["--video-url", "http://unused.example"],
            ["--motioneye-url", "http://camera.example?query=1"],
            ["--motioneye-api-url", "http://camera.example?query=1"],
            ["--motioneye-api-url", "file:///unused"],
            ["--webhook-url", "file:///unused"], ["--event", "end"],
        ):
            with self.subTest(extra=extra):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        NOTIFIER["parse_args"](arguments("start") + extra)
                self.assertEqual(error.exception.code, 2)

    def test_discord_403_reports_error_code_without_response_secrets(self):
        """Distinguish an API rejection from an upstream filter without logging raw bodies."""
        responses = [
            (b'{"code":50013,"message":"secret test-token"}', "API error code 50013"),
            (b'error code: 1010 secret test-token', "upstream error code 1010"),
            (b'<html>secret test-token</html>', "non-JSON response"),
            (b'\xff secret test-token', "non-JSON response"),
        ]
        for body, expected in responses:
            with self.subTest(expected=expected):
                mock_request = unittest.mock.Mock(return_value=(403, body))
                with patch.dict(MAIN_GLOBALS, request=mock_request):
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        self.assertEqual(NOTIFIER["main"](arguments("start")), 1)
                self.assertIn("HTTP 403", errors.getvalue())
                self.assertIn(expected, errors.getvalue())
                self.assertNotIn("test-token", errors.getvalue())

    def test_legacy_signature_matches_protocol_fixture(self):
        """Verify the signature bytes, rather than merely checking that a signature exists."""
        url = NOTIFIER["signed_motioneye_url"](
            "http://camera.example/meye/movie/1/list", "viewer", "test password"
        )
        self.assertEqual(
            url,
            "http://camera.example/meye/movie/1/list?_username=viewer"
            "&_signature=286242dacbc1cfc45eaf433be43d2157cf91f403",
        )

    def test_network_error_is_reported_without_request_secrets(self):
        """Connection failures produce a useful error without exposing the webhook URL."""
        opener = unittest.mock.Mock()
        opener.open.side_effect = NOTIFIER["URLError"]("Cannot connect to " + WEBHOOK)
        with patch.dict(MAIN_GLOBALS, build_opener=unittest.mock.Mock(return_value=opener)):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(NOTIFIER["main"](arguments("start")), 1)
        self.assertIn("Discord webhook connection failed", errors.getvalue())
        self.assertNotIn("test-token", errors.getvalue())

    def test_connection_errors_identify_cause_without_secrets(self):
        """Unwrap urllib errors and classify direct socket errors without their raw text."""
        failures = [
            (socket.gaierror(socket.EAI_NONAME, WEBHOOK), "DNS lookup failed"),
            (socket.timeout(WEBHOOK), "connection timed out"),
            (socket.error(errno.ECONNREFUSED, WEBHOOK), errno.errorcode[errno.ECONNREFUSED]),
            (socket.error(errno.ENETUNREACH, WEBHOOK), errno.errorcode[errno.ENETUNREACH]),
            (ssl.SSLError(1, WEBHOOK), "TLS/SSL handshake or certificate error"),
        ]
        for failure, expected in failures:
            for error in (failure, NOTIFIER["URLError"](failure)):
                with self.subTest(error=type(error).__name__, expected=expected):
                    opener = unittest.mock.Mock()
                    opener.open.side_effect = error
                    with self.assertRaises(NOTIFIER["NotificationError"]) as caught:
                        NOTIFIER["request"](opener, WEBHOOK, service="Discord webhook")
                    self.assertEqual(
                        str(caught.exception), "Discord webhook connection failed: " + expected
                    )

    def test_end_connection_failures_identify_the_failing_service(self):
        """Distinguish login, movie lookup, and webhook transport failures in end commands."""
        failure = NOTIFIER["URLError"](socket.gaierror(socket.EAI_NONAME, WEBHOOK))
        cases = [
            ([], [failure], "motionEye movie list"),
            (["--motioneye-username", "viewer"], [failure], "motionEye login"),
            ([], [unittest.mock.Mock(
                getcode=lambda: 200, read=lambda: json.dumps({"mediaList": MOVIES}).encode()
            ), failure], "Discord webhook"),
        ]
        for extra, responses, service in cases:
            with self.subTest(service=service):
                opener = unittest.mock.Mock()
                opener.open.side_effect = responses
                with patch.dict(MAIN_GLOBALS, build_opener=unittest.mock.Mock(return_value=opener)):
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        self.assertEqual(NOTIFIER["main"](arguments() + extra), 1)
                self.assertIn(service + " connection failed: DNS lookup failed", errors.getvalue())
                self.assertNotIn("test-token", errors.getvalue())
                self.assertEqual(opener.open.call_count, len(responses))


class HttpIntegrationTests(unittest.TestCase):
    """Use local fake HTTP services to exercise JSON, cookies, signing, and complete commands."""

    def setUp(self):
        self.calls = []
        self.auth_mode = "anonymous"
        self.login_status = 200
        self.movie_status = 200
        self.webhook_status = 200
        test = self

        class Handler(BaseHTTPRequestHandler):
            """Serve the two fake APIs and capture requests without logging secrets."""

            def log_message(self, *_args):
                pass

            def do_GET(self):  # pylint: disable=invalid-name
                """Serve the movie list, checking session cookies when required."""
                test.calls.append((self.path, None, dict(self.headers)))
                path = urlsplit(self.path)
                if path.path != "/meye/movie/1/list":
                    self.respond(404, {})
                    return
                if (test.auth_mode == "session"
                        and self.headers.get("Cookie") != "user=test-session"):
                    self.respond(403, {})
                    return
                if test.auth_mode == "legacy":
                    query = parse_qs(path.query)
                    if "_signature" not in query or query.get("_username") != ["viewer"]:
                        self.respond(403, {})
                        return
                self.respond(test.movie_status, {"mediaList": MOVIES})

            def do_POST(self):  # pylint: disable=invalid-name
                """Serve login or capture a Discord JSON payload."""
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                test.calls.append((self.path, body, dict(self.headers)))
                if urlsplit(self.path).path == "/meye/login":
                    if test.auth_mode == "legacy":
                        self.respond(404, {})
                    else:
                        self.respond(test.login_status, {}, cookie="user=test-session; Path=/")
                else:
                    self.respond(test.webhook_status, {"id": "123"})

            def respond(self, status, payload, cookie=None):
                """Write one bounded JSON response."""
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 2
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        """Return the local fake server's dynamically assigned address."""
        return "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def command(self, authenticated=False):
        """Build an end invocation that can only contact the local fake services."""
        result = arguments() + [
            "--motioneye-url", self.base_url + "/meye/",
            "--webhook-url", self.base_url + "/webhook?thread_id=42&wait=false",
        ]
        if authenticated:
            result += ["--motioneye-username", "viewer", "--motioneye-password", "test password"]
        return result

    def assert_delivery(self):
        """Check the exact JSON and ensure credentials stay out of Discord requests."""
        path, body, headers = self.calls[-1]
        self.assertEqual(parse_qs(urlsplit(path).query), {
            "thread_id": ["42"], "wait": ["true"], "with_components": ["true"],
        })
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["User-Agent"], NOTIFIER["DISCORD_USER_AGENT"])
        self.assertEqual(headers["Accept"], "application/json")
        self.assertNotIn("Cookie", headers)
        payload = json.loads(body)
        video_url = self.base_url + "/meye/movie/1/playback/2026-09-14/17-12-02.mp4"
        self.assertTrue(payload["components"][0]["content"].endswith("\n" + video_url))
        self.assertEqual(payload["components"][1]["items"][0]["media"]["url"], video_url)
        self.assertNotIn(b"test password", body)
        self.assertNotIn(b"_signature", body)

    def test_anonymous_end_to_end(self):
        """Fetch the latest recording and post both representations over real local HTTP."""
        self.assertEqual(NOTIFIER["main"](self.command()), 0)
        self.assertEqual(len(self.calls), 2)
        self.assert_delivery()

    def test_session_authentication(self):
        """POST login stores a cookie for motionEye requests only."""
        self.auth_mode = "session"
        self.assertEqual(NOTIFIER["main"](self.command(authenticated=True)), 0)
        self.assertEqual(parse_qs(self.calls[0][1].decode()), {
            "username": ["viewer"], "password": ["test password"],
        })
        self.assert_delivery()

    def test_separate_api_address_preserves_playback_links(self):
        """Login and lookup use the internal address; both video URLs use the playback base."""
        for mode in ("anonymous", "session", "legacy"):
            with self.subTest(mode=mode):
                self.auth_mode = mode
                self.calls.clear()
                command = self.command(authenticated=mode != "anonymous") + [
                    "--motioneye-api-url", self.base_url + "/meye/",
                    "--motioneye-url", "http://motioneye.example/watch",
                ]
                self.assertEqual(NOTIFIER["main"](command), 0)
                self.assertEqual(
                    [urlsplit(call[0]).path for call in self.calls],
                    (["/meye/login"] if mode != "anonymous" else [])
                    + ["/meye/movie/1/list", "/webhook"],
                )
                payload = json.loads(self.calls[-1][1])
                video_url = (
                    "http://motioneye.example/watch/movie/1/playback/"
                    "2026-09-14/17-12-02.mp4"
                )
                self.assertTrue(payload["components"][0]["content"].endswith("\n" + video_url))
                self.assertEqual(payload["components"][1]["items"][0]["media"]["url"], video_url)
                self.assertNotIn(self.base_url, self.calls[-1][1].decode())
                self.assertNotIn("Cookie", self.calls[-1][2])

    def test_legacy_authentication(self):
        """A server without POST login receives the legacy signed movie-list request."""
        self.auth_mode = "legacy"
        self.assertEqual(NOTIFIER["main"](self.command(authenticated=True)), 0)
        signed_query = parse_qs(urlsplit(self.calls[1][0]).query)
        self.assertEqual(
            signed_query["_signature"][0], "286242dacbc1cfc45eaf433be43d2157cf91f403"
        )
        self.assert_delivery()

    def test_rejected_login_does_not_post(self):
        """Bad credentials stop immediately without a legacy fallback or notification."""
        self.auth_mode = "session"
        self.login_status = 403
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(NOTIFIER["main"](self.command(authenticated=True)), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("HTTP 403", errors.getvalue())

    def test_movie_api_failure_does_not_post(self):
        """An HTTP error cannot become a success or a fabricated link."""
        self.movie_status = 500
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(NOTIFIER["main"](self.command()), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("HTTP 500", errors.getvalue())

    def test_discord_http_failure_is_reported(self):
        """HTTPError responses from urllib retain their status for diagnostics."""
        self.webhook_status = 429
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(NOTIFIER["main"](self.command()), 1)
        self.assertIn("HTTP 429", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
