"""Send motionEye notifications with recording links and inline video."""  # pylint: disable=invalid-name

# Python 2.7 does not support the "raise ... from ..." syntax.
# pylint: disable=raise-missing-from

# Initial version created by https://github.com/Bluscream and https://github.com/IAmOrion
# Originally copied from:
# https://github.com/ccrisan/motioneyeos/issues/1557#issuecomment-399692426
# and then modified slightly.

import argparse
import errno
import hashlib
import json
import math
import re
import socket
import ssl
import sys
import time

try:
    from http.cookiejar import CookieJar
    from urllib.error import HTTPError, URLError
    from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
    from urllib.request import HTTPCookieProcessor, Request, build_opener
except ImportError:  # Python 2.7 on older motionEyeOS installations.
    from cookielib import CookieJar  # pylint: disable=import-error
    from urllib import quote, urlencode  # pylint: disable=no-name-in-module
    from urllib2 import (  # pylint: disable=import-error
        HTTPError,
        HTTPCookieProcessor,
        Request,
        URLError,
        build_opener,
    )
    from urlparse import parse_qsl, urlsplit, urlunsplit  # pylint: disable=import-error

HTTP_TIMEOUT = 30
DISCORD_USER_AGENT = (
    "DiscordBot (https://github.com/RampantDespair/motioneyeos-discord-notifier, 1.0)"
)
COMPONENTS_V2 = 1 << 15
TEXT_DISPLAY = 10
MEDIA_GALLERY = 12
STRING_TYPES = (str, type(""))  # pylint: disable=redundant-u-string-prefix
SIGNATURE_FILTER = re.compile(r'[^a-zA-Z0-9/?_.=&{}\[\]":, -]')


class NotificationError(Exception):
    """A notification could not be built or delivered."""


def http_url(value):
    """Validate an HTTP URL without embedding login credentials in it."""
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in ("http", "https")
            and parts.hostname
            and parts.username is None
            and not parts.fragment
        )
        # Accessing port also validates malformed port numbers.
        if parts.port is not None and not 0 < parts.port < 65536:
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError(
            "Expected an HTTP(S) URL without credentials or a fragment"
        )
    return value


def parse_args(argv=None):
    """Parse notification settings and validate the arguments needed for each event."""
    parser = argparse.ArgumentParser(
        description="Configure motion notifications with a recording link and inline video."
    )
    parser.add_argument(
        "--webhook-url",
        help="Full Discord webhook URL",
        type=http_url,
        required=True,
    )
    parser.add_argument(
        "--event",
        help="Motion event being reported",
        choices=("start", "end"),
        required=True,
    )
    parser.add_argument(
        "-n", "--name", help="Camera name shown in the notification", required=True
    )
    parser.add_argument(
        "-t",
        "--time",
        help="Event timestamp or strftime format expanded using local system time",
        required=True,
    )
    parser.add_argument(
        "--motioneye-url",
        help="motionEye base URL for playback links and, by default, API requests",
        type=http_url,
    )
    parser.add_argument(
        "--motioneye-api-url",
        help="Optional separate base URL for API requests from this machine",
        type=http_url,
    )
    parser.add_argument(
        "--camera-id", help="Camera ID used in motionEye playback URLs", type=int
    )
    parser.add_argument(
        "--motioneye-username",
        help="Username for querying motionEye when authentication is required",
    )
    parser.add_argument(
        "--motioneye-password",
        help="Password for querying motionEye when authentication is required",
    )

    args = parser.parse_args(argv)
    if args.camera_id is not None and args.camera_id <= 0:
        parser.error("--camera-id must be a positive integer")
    for name in ("motioneye_url", "motioneye_api_url"):
        value = getattr(args, name)
        if value and urlsplit(value).query:
            parser.error(
                "--" + name.replace("_", "-") + " must be a base URL without a query string"
            )
    if args.motioneye_password is not None and not args.motioneye_username:
        parser.error("--motioneye-password requires --motioneye-username")

    if args.event == "end":
        required = ("motioneye_url", "camera_id")
        missing = [
            "--" + name.replace("_", "-")
            for name in required
            if not getattr(args, name)
        ]
        if missing:
            parser.error("--event end requires " + ", ".join(missing))

    return args


def connection_error_detail(error):
    """Describe transport failures without exposing URLs or arbitrary exception text."""
    reason = getattr(error, "reason", error)
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, socket.timeout):
        return "connection timed out"
    if isinstance(reason, ssl.SSLError):
        return "TLS/SSL handshake or certificate error"
    error_number = getattr(reason, "errno", None)
    if isinstance(error_number, int):
        return errno.errorcode.get(error_number, "socket error " + str(error_number))
    return type(error).__name__


def request(opener, url, data=None, headers=None, service="HTTP"):
    """Return an HTTP status and body, keeping cookies in memory and bounding network waits."""
    http_request = Request(url, data=data, headers=headers or {})
    try:
        try:
            response = opener.open(http_request, timeout=HTTP_TIMEOUT)
        except HTTPError as error:
            response = error
        try:
            return response.getcode(), response.read()
        finally:
            response.close()
    except (URLError, socket.error) as error:
        # Do not include request URLs, webhook tokens, or login data in errors.
        raise NotificationError(service + " connection failed: " + connection_error_detail(error))


def response_error_detail(body):
    """Extract numeric error codes without printing response text that may contain secrets."""
    text = body.decode("utf-8", errors="replace")
    try:
        result = json.loads(text)
    except ValueError:
        match = re.search(r"\berror code:\s*(\d{3,6})\b", text, re.IGNORECASE)
        if match:
            return " (upstream error code " + match.group(1) + ")"
        return " (non-JSON response)"
    if isinstance(result, dict) and isinstance(result.get("code"), int):
        return " (API error code " + str(result["code"]) + ")"
    return ""


def read_json(status, body, service):
    """Decode an API response and report failures without exposing response secrets."""
    if not 200 <= status < 300:
        raise NotificationError(
            service + " returned HTTP " + str(status) + response_error_detail(body)
        )
    try:
        result = json.loads(body.decode("utf-8"))
    except ValueError:
        raise NotificationError(service + " returned invalid JSON")
    if not isinstance(result, dict) or result.get("error"):
        raise NotificationError(service + " returned an API error")
    return result


def signed_motioneye_url(url, username, password):
    """Sign a GET URL using the legacy motionEye request-signature protocol."""
    # Protocol reference: motioneye-project/motioneye-client, utils.compute_signature.
    parts = list(urlsplit(url))
    query = parse_qsl(parts[3], keep_blank_values=True)
    query = [
        (key, value) for key, value in query if key not in ("_username", "_signature")
    ]
    query.append(("_username", username))
    query.sort(key=lambda pair: pair[0])
    parts[3] = "&".join(
        key + "=" + quote(value.encode("utf-8"), safe="!'()*~") for key, value in query
    )
    canonical = urlunsplit(("", "", parts[2], parts[3], ""))
    canonical = SIGNATURE_FILTER.sub("-", canonical)
    password_hash = hashlib.sha1(password.encode("utf-8")).hexdigest()
    signature = hashlib.sha1(
        ("GET:" + canonical + "::" + password_hash).encode("utf-8")
    ).hexdigest()
    parts[3] += "&_signature=" + signature
    return urlunsplit(parts)


class MotionEyeClient:
    """Read recording metadata using anonymous, session, or legacy authentication."""

    def __init__(self, base_url, username=None, password=None):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password or ""
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.legacy_auth = False

    def login(self):
        """Log in if credentials were supplied; older servers use signed GET requests."""
        if not self.username:
            return
        form = urlencode({"username": self.username, "password": self.password}).encode(
            "utf-8"
        )
        status, body = request(
            self.opener,
            self.base_url + "/login",
            form,
            {"Content-Type": "application/x-www-form-urlencoded"},
            service="motionEye login",
        )
        if status in (400, 404, 405):
            self.legacy_auth = True
            return
        read_json(status, body, "motionEye login")

    def latest_recording(self, camera_id):
        """Fetch the camera's full movie list and return its newest recording metadata."""
        url = self.base_url + "/movie/" + str(camera_id) + "/list"
        if self.legacy_auth:
            url = signed_motioneye_url(url, self.username, self.password)
        status, body = request(self.opener, url, service="motionEye movie list")
        result = read_json(status, body, "motionEye movie list")
        return newest_recording(result.get("mediaList"))


def newest_recording(recordings):
    """Select by numeric timestamp, rather than API order or a guessed filename."""
    if not isinstance(recordings, list):
        raise NotificationError("motionEye returned an invalid movie list")
    if not recordings:
        raise NotificationError("No recordings are available for this camera")
    candidates = []
    for recording in recordings:
        if not isinstance(recording, dict):
            raise NotificationError("motionEye returned invalid recording metadata")
        path = recording.get("path")
        if not isinstance(path, STRING_TYPES) or not path.strip("/"):
            raise NotificationError("motionEye returned a recording without a name")
        try:
            timestamp = float(recording["timestamp"])
        except (KeyError, TypeError, ValueError):
            raise NotificationError(
                "motionEye returned a recording without a valid timestamp"
            )
        if math.isnan(timestamp) or math.isinf(timestamp):
            raise NotificationError(
                "motionEye returned a non-finite recording timestamp"
            )
        candidates.append((timestamp, path, recording))
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def playback_url(base_url, camera_id, recording):
    """Build a playback URL from the relative recording name returned by motionEye."""
    name = recording["path"].lstrip("/")
    if any(part in (".", "..") for part in name.split("/")) or "\\" in name:
        raise NotificationError("motionEye returned an invalid recording name")
    return (
        base_url.rstrip("/")
        + "/movie/"
        + str(camera_id)
        + "/playback/"
        + quote(name.encode("utf-8"), safe="/")
    )


def build_payload(args, video_url=None):
    """Build a start message or an end message containing the same link and inline video."""
    action = "started" if args.event == "start" else "ended"
    event_time = time.strftime(args.time)
    text = "Motion " + action + " on " + args.name + " at `" + event_time + "`"
    mentions = {"parse": []}
    if args.event == "start":
        return {"content": text, "allowed_mentions": mentions}
    if not video_url:
        raise NotificationError("An end notification requires a recording URL")
    return {
        "flags": COMPONENTS_V2,
        "allowed_mentions": mentions,
        "components": [
            {"type": TEXT_DISPLAY, "content": text + "\n" + video_url},
            {"type": MEDIA_GALLERY, "items": [{"media": {"url": video_url}}]},
        ],
    }


def send_notification(webhook_url, payload):
    """Send JSON to Discord and require server confirmation of message creation."""
    parts = list(urlsplit(webhook_url))
    query = dict(parse_qsl(parts[3], keep_blank_values=True))
    query.update({"wait": "true", "with_components": "true"})
    parts[3] = urlencode(query)
    # A separate opener keeps motionEye session cookies away from the webhook request.
    status, body = request(
        build_opener(),
        urlunsplit(parts),
        json.dumps(payload).encode("utf-8"),
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": DISCORD_USER_AGENT,
        },
        service="Discord webhook",
    )
    result = read_json(status, body, "Discord webhook")
    if not result.get("id"):
        raise NotificationError("Discord did not confirm message creation")


def main(argv=None):
    """Resolve the recording for end events, deliver the notification, and report failures."""
    args = parse_args(argv)
    try:
        video_url = None
        if args.event == "end":
            client = MotionEyeClient(
                args.motioneye_api_url or args.motioneye_url,
                args.motioneye_username,
                args.motioneye_password,
            )
            client.login()
            recording = client.latest_recording(args.camera_id)
            video_url = playback_url(args.motioneye_url, args.camera_id, recording)
        send_notification(args.webhook_url, build_payload(args, video_url))
    except NotificationError as error:
        sys.stderr.write("Notification failed: " + str(error) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
