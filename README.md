# motioneyeos-discord-notifier

Send motion start/end notifications to Discord. An end notification includes the
latest recording's motionEye playback URL as both a clickable link and an inline
video component. The script reads movie metadata over HTTP; it does not read,
upload, or delete recordings.

## Setup

Save [notify-discord.py](notify-discord.py) on the machine that runs the commands.
The examples below use `/data/etc/notify-discord.py`; substitute its actual location.
Only the Python standard library is required. The script retains Python 2.7-compatible
syntax for older motionEyeOS installations; automated tests run on Python 3.

In the camera's **Motion Notifications** settings, enable the start and end command
options. Use the commands below, replacing `YOUR_WEBHOOK_URL`, the motionEye base
URL, and camera ID as needed. Movies must be enabled for end notifications to have
a recording to link.

### Motion started — Run A Command (`on_event_start`)

```sh
python /data/etc/notify-discord.py --webhook-url "YOUR_WEBHOOK_URL" --event start -n "server rack" -t "%Y-%m-%d %H:%M:%S"
```

### Motion ended — Run An End Command (`on_event_end`)

```sh
python /data/etc/notify-discord.py --webhook-url "YOUR_WEBHOOK_URL" --event end -n "server rack" -t "%Y-%m-%d %H:%M:%S" --motioneye-url "http://motioneye.tower.home" --camera-id 1
```

If motionEye requires login, append `--motioneye-username "YOUR_USERNAME"
--motioneye-password "YOUR_PASSWORD"` to the end command. Both legacy request
signatures and newer session logins are supported. Credentials and cookies are
used for the metadata lookup only; they are not added to Discord messages or
playback URLs. Use `--motioneye-password="-PASSWORD"` if a password starts with `-`.

## Recording selection and playback

The end command requests `/movie/{camera-id}/list`, selects the largest numeric
`timestamp` in `mediaList`, and uses that entry's `path` to construct
`{motioneye-url}/movie/{camera-id}/playback/{recording-name}`. It preserves date
directories and the actual video extension, and URL-encodes the name. It queries
all dates, so recordings spanning midnight do not depend on the notification date.

The same URL goes into the message text and a Discord Media Gallery. Discord must
be able to reach the recording without your browser's login session for inline
playback. A private `.home` link remains useful in your browser when connected to
your network, but cannot be fetched by Discord. Supplying API login credentials to
this script does not grant Discord access to a protected recording.

If the container cannot reach the playback hostname, set `--motioneye-api-url`
to an address it can reach. For example, if motionEye listens on port 8765 inside
the same container, append `--motioneye-api-url "http://127.0.0.1:8765"` to the end
command. Login and movie-list requests use this address; the link and video
component still use `--motioneye-url "http://motioneye.tower.home"`. Substitute
the actual listening address and port for your setup. Without this option, API
requests use `--motioneye-url` as before.

Selection means **latest at the time of the query**, not guaranteed matching to an
event ID. The movie-list API does not expose a completed-recording flag. Run the
end command after recording has finished; if an event is split across videos, the
link points to the latest segment. Empty lists, malformed metadata, login errors,
network errors, and rejected Discord requests produce an error on stderr and exit
status 1. Successful delivery exits 0 after Discord confirms message creation.

See the [motionEye client's recording API](https://github.com/motioneye-project/motioneye-client)
and [Discord Media Gallery documentation](https://docs.discord.com/developers/components/reference#media-gallery).

## Checks

```sh
python -B -m unittest -v test_notify_discord
python -m pylint notify-discord.py test_notify_discord.py
```

Tests use fake local HTTP services and do not contact Discord or a real camera.

## Credits

Initial version created by [Bluscream](https://github.com/Bluscream) and
[IAmOrion](https://github.com/IAmOrion), based on
[this motionEyeOS discussion](https://github.com/ccrisan/motioneyeos/issues/1557#issuecomment-399692426).
