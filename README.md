# VDM10 Intercom — Home Assistant Integration

[![Open your Home Assistant instance and open a repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=lodzen&repository=metzler-vdm10-ha&category=integration)

Home Assistant custom integration for the Metzler/Hikvision **VDM10** door intercom.
It embeds the standalone [`metzler-vdm10`](../metzler-vdm10) SIP library and registers
your HA instance as a room station on the VDM10 SIP registrar.

> **Firmware support: VDM10 1.0 only.** The integration targets the VDM10 1.0
> firmware (protocol reverse-engineered in `metzler-vdm10`). **VDM10 2.0 is not
> supported at the moment.**

- `binary_sensor.*_doorbell_ringing` — on while the doorbell rings (events for automations)
- `camera.*_door` — live view of the outdoor station, **pull-on-demand only**
- `button.*_answer_call` / `button.*_hang_up` — explicit call control
- `sensor.*_intercom_status` — registration diagnostics (`registered`/`connecting`/`unavailable`, `last_error`)
- Two-way audio via go2rtc/ISAPI — microphone **muted by default**
- Optional SIP provider / Fritz!Box trunk — the door call rings HA *and* your app/fritzfon; first answer wins
- LAN auto-discovery for the outdoor station (SADP) and the main indoor (registrar fingerprint)

## Install (HACS / Marketplace custom repository)

1. Click the badge above — or in Home Assistant: **Settings → Add-ons → HACS**
   (or the new Marketplace) → **Custom repositories / Add from link** → add
   `https://github.com/lodzen/metzler-vdm10-ha` as an *Integration* repository.
2. Install **VDM10 Intercom**, restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → VDM10 Intercom** —
   choose **Search the network** for auto-discovery or **Manual setup**.

The release ZIP ships the `metzler` library vendored under
`custom_components/vdm10/vendor/` (built by `tools/vendor.py`) — no manual
dependency install and no PyPI requirement.

## Configuration

Only the **main indoor station IP** and the **extension password** are
strictly required — everything else is derived or auto-discovered:

| Field | Meaning | Default |
|---|---|---|
| `registrar_host` | SIP registrar — the **main indoor station** IP | required / auto-found |
| `username` | your extension number | suggested: lowest free slot |
| `password` | extension SIP password | required |
| `door_host` | outdoor station (doorbell) IP | auto-found via SADP |
| `name` | display name | `VDM10 <extension>` |
| Advanced ⤵ | | |
| `registrar_port` | registrar SIP port | `5065` |
| `local_ip` | this host's IP on the intercom VLAN | auto (UDP-connect to registrar) |
| `serial` | device serial (`regDevSerial`) | auto-generated |
| `listen_port` | local SIP UDP port | `5060` |
| `rtsp_port` | local RTSP talkback port (callers pull our mic here) | `554` |
| `door_user` / `door_password` | door ISAPI/RTSP credentials (Basic auth) | `admin` / extension password |
| SIP provider ⤵ | optional — Linphone/PBX trunk (`sip_host`/`sip_user`/`sip_pass`/`sip_target`/`sip_domain`/`sip_srtp`) | off |
| Fritz!Box ⤵ | optional second trunk (`fritz_host`/`fritz_user`/`fritz_pass`/`fritz_target`) | off |

### Discovery

**Search the network** runs two firmware-native probes:

- **SADP inquiry** (`239.255.255.250:37020` + unicast sweep) finds the outdoor
  station — it answers with IP, serial, MAC and device type (`VDM10-VM-POE`).
- **Registrar fingerprint**: a REGISTER probe on `:5065` — only the *main*
  indoor answers (extensions stay silent). Slot probing also reveals which
  extension numbers are taken, so the flow suggests a **free extension**.

The indoor units do *not* answer SADP — the main is identified solely by the
REGISTER fingerprint (replies arrive on `:5060`, a firmware quirk).

### SIP provider / Fritz trunk (optional)

With `sip_host` (Linphone, Asterisk, any PBX) and/or the Fritz!Box section
filled in, an incoming door call is parked for HA **and** INVITEd to the
trunk — whichever side answers first wins; the other leg is CANCELed.
`vdm10_answered` reports `by: "ha"` or `by: "other"`. Leaving both empty keeps
HA-only answering.

## Entities, services, events

| Surface | Behavior |
|---|---|
| `binary_sensor.*_doorbell_ringing` | `on` only while a call is `early` (ringing); attrs `caller`, `call_id`; unavailable while unregistered |
| `camera.*_door` | `CameraEntityFeature.STREAM`; `stream_source` = `rtsp://<door_user>:<door_pass>@<door_host>:554/ch1/main/av_stream`; stills via one-shot ISAPI snapshot |
| `button.*_answer_call` | `available` only while ringing; press = library 200-OK → established |
| `button.*_hang_up` | decline while ringing (`486`/`487`), BYE while established |
| `vdm10.answer_call` / `vdm10.hangup_call` | services equivalent to the buttons |
| `event.*_doorbell` | `press` edge per ring, deduped by Call-ID — the Alexa announcement trigger |
| `vdm10.simulate_doorbell_press` | fires a synthetic press through the same code path — verifies the full Alexa chain without walking to the door |
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

The camera declares `two_way_audio` (HA 2026.11 preview feature) — it resolves
when a WebRTC stream provider is present and the Labs flag is on. You need:

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

## Alexa — one video-doorbell device

Amazon only plays video on a doorbell press when a **single** endpoint carries
`Alexa.DoorbellEventSource`, a camera stream interface and
`displayCategories: ["CAMERA", "DOORBELL"]`. Home Assistant never produces
that natively, so the integration patches it in itself: the door camera is
auto-detected (via its `*_door_camera` unique ID) and discovered with both
categories plus the doorbell interface — the same shape Scrypted announces.
A press on the internal `event.*_doorbell` entity is delivered under the
**camera's** endpoint ID, so the announcement and the video arrive on one
Alexa device.

User steps (the only manual part):

1. **Expose the camera** to Alexa. YAML skill: `camera` in
   `alexa.smart_home.filter.include_domains`. The `event.*_doorbell` entity
   must stay **unexposed** — do not include it; the press is forwarded
   internally. (`entity_config.display_categories` is *not* needed — the
   `[CAMERA, DOORBELL]` pair is forced for our camera automatically.)
2. Restart HA, then run **"Alexa, discover devices"** (or Alexa app →
   *Add device → Other → Discover*). The camera appears as one doorbell
   device named after the camera entity.
3. In the Alexa app enable **doorbell announcements** on that device for the
   Echo devices that should react (device settings → Announcement).
4. Verify without the physical button:
   `Developer tools → Actions → vdm10.simulate_doorbell_press` — the Echo
   should announce and an Echo Show should pop the stream. Debug lines
   (`custom_components.vdm10: debug`) show each stage, e.g.
   `doorbell press event.…_doorbell -> camera.…_door_camera (direct)`.

WebRTC (`Alexa.RTCSessionController`) is what drives the live view on Echo
Show — it requires the go2rtc WebRTC provider from the section above to be
registered before discovery runs. If announcements arrive but no video opens,
re-run discovery after confirming go2rtc serves the stream.

> **Nabu Casa note**: the direct press forwarding resolves the YAML
> `AlexaConfig`/`SmartHomeView`. With a cloud-only setup the press falls back
> to the listener path — expose `event.*_doorbell` in that case (you will see
> a second, inert doorbell entry in the app; presses still route to the
> camera endpoint).

## Development

```bash
uv sync                                  # metzler is a path dependency (../metzler-vdm10)
uv run pytest                            # component tests (localhost-only)
uv run pytest ../metzler-vdm10           # library suite (must stay green)
uv run ruff check custom_components tests tools
uv run ruff format custom_components tests tools
uv run ty check
uv run complexipy custom_components/vdm10 tests tools -e vendor
```

**Release**: `uv run python tools/vendor.py` vendors `../metzler-vdm10/src/metzler`
into `custom_components/vdm10/vendor/metzler` (with `_version.py` stamp); the
committed `vendor/__init__.py` loader appends that dir to `sys.path` before the
first `import metzler`. `tests/test_vendor.py` verifies the vendored import in
isolation. Then tag a GitHub release — HACS serves the tag.

Test environment pins `homeassistant==2026.11.1` with the officially
maintained `pytest-homeassistant` package (home-assistant/core); `hacs.json`
declares `homeassistant: 2026.11.0` as the install minimum (the camera's
`two_way_audio` capability needs 2026.11+).

## Notes & limitations

- **VDM10 1.0 only** — firmware 2.0 speaks a different dialect; not supported.
- The mic queue (`server.mic` → PCMU silence while empty) is the *fallback*
  talk path; on the primary go2rtc path, mute-by-default is enforced platform-side.
- `answered_elsewhere` is a best-effort label — see `vdm10_call_ended` above.
- Library internals: SIP registrar `:5065`, station SIP `:5060`, RTSP `:554`,
  talkback RTP `9654` — never bind TCP 8102 (visNet push port).
- Discovery probes bind `:5060` briefly (the registrar answers there) — running
  discovery while an entry is active can be unreliable; the form falls back to
  manual entry.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=lodzen/metzler-vdm10-ha&type=date&logscale&legend=top-left)](https://www.star-history.com/#lodzen/metzler-vdm10-ha&type=date&logscale&legend=top-left)

## License

GPL-3.0-only — see [LICENSE](LICENSE).
