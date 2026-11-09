# VDM10 Intercom — Home Assistant Integration

Home Assistant custom integration for the Metzler/Hikvision **VDM10** door intercom.
It embeds the standalone [`metzler-vdm10`](../metzler-vdm10) SIP library and registers
your HA instance as a room station on the VDM10 SIP registrar.

- `binary_sensor.*_doorbell_ringing` — on while the doorbell rings (events for automations)
- `camera.*_door` — live view of the outdoor station, **pull-on-demand only**
- `button.*_answer_call` / `button.*_hang_up` — explicit call control
- `sensor.*_intercom_status` — registration diagnostics (`registered`/`connecting`/`unavailable`, `last_error`)
- Two-way audio via go2rtc/ISAPI — microphone **muted by default**

## Install (HACS / Marketplace custom repository)

1. In Home Assistant: **Settings → Add-ons → HACS** (or the new Marketplace) →
   **Custom repositories / Add from link** → add `https://github.com/lodzen/metzler-vdm10-ha`
   as an *Integration* repository.
2. Install **VDM10 Intercom**, restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → VDM10 Intercom** and fill in the form.

The release ZIP ships the `metzler` library vendored under
`custom_components/vdm10/vendor/` (built by `tools/vendor.py`) — no manual
dependency install and no PyPI requirement.

## Configuration

| Field | Meaning | Default |
|---|---|---|
| `registrar_host` | SIP registrar — usually the **main indoor station** IP | required |
| `registrar_port` | registrar SIP port | `5065` |
| `username` | your extension number | e.g. `10000000005` |
| `password` | extension SIP password | — |
| `serial` | device serial (`regDevSerial`) | required |
| `local_ip` | this host's IP on the intercom VLAN | required |
| `listen_port` | local SIP UDP port | `5060` |
| `rtsp_port` | local RTSP talkback port (callers pull our mic here) | `554` |
| `door_host` | outdoor station (doorbell) IP — camera + ISAPI | required |
| `door_user` / `door_password` | door ISAPI/RTSP credentials (Basic auth) | `admin` |

One entry per system; `unique_id` = `serial@registrar`. Credentials are never
written to logs, entity attributes, unique IDs, or event payloads.

## Entities, services, events

| Surface | Behavior |
|---|---|
| `binary_sensor.*_doorbell_ringing` | `on` only while a call is `early` (ringing); attrs `caller`, `call_id`; unavailable while unregistered |
| `camera.*_door` | `CameraEntityFeature.STREAM`; `stream_source` = `rtsp://<door_user>:<door_pass>@<door_host>:554/ch1/main/av_stream`; stills via one-shot ISAPI snapshot |
| `button.*_answer_call` | `available` only while ringing; press = library 200-OK → established |
| `button.*_hang_up` | decline while ringing (`486`/`487`), BYE while established |
| `vdm10.answer_call` / `vdm10.hangup_call` | services equivalent to the buttons |
| `vdm10_ring` | bus event `{caller, call_id, door}` |
| `vdm10_answered` | bus event `{call_id, by: "ha" \| "other"}` |
| `vdm10_call_ended` | bus event `{call_id, reason}` — `answered_elsewhere` is **best-effort** (the registrar's forked-leg CANCEL is identical for answer-elsewhere and caller-timeout) |

## Live video — pull on demand only

The camera entity owns **no** puller and holds no connection while idle. HA's
`stream` integration opens the door's RTSP only while a viewer is attached and
tears it down after the viewer leaves. Stills are one-shot HTTP fetches —
there is no background preview pull, no keepalive, and opening the camera
during a ring does **not** answer the call.

## Two-way audio (go2rtc + ISAPI)

For browser talkback over WebRTC you need:

- the standalone **go2rtc** add-on with a stream merging video + ISAPI backchannel:

  ```yaml
  streams:
    vdm10_door:
      - rtsp://<door_user>:<door_pass>@<door_host>:554/ch1/main/av_stream#backchannel=0
      - isapi://<door_user>:<door_pass>@<door_host>:80/
  ```

- HA Labs WebRTC preview enabled + HTTPS frontend

go2rtc opens the door's audio channel only while the talk control is held —
that enforces **muted-by-default** on the primary path. If the door rejects
ISAPI audio while a SIP call is established, the fallback is the library's
RTSP `Require: …backchannel` path (tracked as T032/T039 — hardware gate).

## Development

```bash
uv sync                                  # metzler is a path dependency (../metzler-vdm10)
uv run pytest                            # component tests (localhost-only)
uv run pytest ../metzler-vdm10           # library suite (must stay green)
uv run ruff check custom_components tests tools
uv run ruff format custom_components tests tools
uv run ty check
uv run complexipy custom_components/vdm10 tests tools
```

**Release**: `uv run python tools/vendor.py` vendors `../metzler-vdm10/src/metzler`
into `custom_components/vdm10/vendor/metzler` (with `_version.py` stamp); the
committed `vendor/__init__.py` loader appends that dir to `sys.path` before the
first `import metzler`. `tests/test_vendor.py` verifies the vendored import in
isolation. Then tag a GitHub release — HACS serves the tag.

Test environment pins `homeassistant==2026.10.4` (the newest HA a released
`pytest-homeassistant-custom-component` supports); `hacs.json` declares
`homeassistant: 2026.10.0` as the install minimum.

## Notes & limitations

- The mic queue (`server.mic` → PCMU silence while empty) is the *fallback*
  talk path; on the primary go2rtc path, mute-by-default is enforced platform-side.
- `answered_elsewhere` is a best-effort label — see `vdm10_call_ended` above.
- Library internals: SIP registrar `:5065`, station SIP `:5060`, RTSP `:554`,
  talkback RTP `9654` — never bind TCP 8102 (visNet push port).

## License

MIT — see [LICENSE](LICENSE).
