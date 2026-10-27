# Foyer for Home Assistant

The Home Assistant side of Foyer: a custom integration that turns each paired Roku TV into a home dashboard and cast target, plus an optional dashboard renderer. The Roku app lives in the companion repository, `foyer-roku`.

Documentation, support, privacy policy and terms: https://www.kilotonlabs.com/foyer/

**Status: 1.0.0.** Tested on a Roku TV (Roku OS 15) with Home Assistant 2026.9. Untested: cloud TTS engines, HD-resolution TVs and other Home Assistant versions. See [TESTING.md](TESTING.md).

## What it does

- Each paired TV is a `media_player` entity. Play from the media browser, automations, scripts or TTS: local media, Radio Browser and other media sources, plain HTTP(S) URLs, photos, announcements, camera streams. A plain image link needs `media_content_type: image` when its address has no file extension.
- Announcements play over whatever the TV shows and then give it back: a dashboard or camera picture reappears, and media continues where it was, paused if it was paused. The entity shows the spoken text as the title.
- Cameras with HLS play live. Cameras without HLS (MJPEG, WebRTC-only) show as a still that refreshes every two seconds. Home Assistant's `master_playlist.m3u8` is rewritten to `playlist.m3u8`, because Roku's player never leaves a master playlist whose only variant has no `RESOLUTION`.
- `foyer.show_view` casts a dashboard view, custom cards included, like `cast.show_lovelace_view` does for Chromecast. Roku has no web engine, so the **renderer** opens the view in headless Chromium and the TV shows a picture of it, refreshed every few seconds. View-only.
- Queued casts nudge the TV over Roku's network control (ECP): an open app reacts at once, and a closed one is launched when there is something to show. Stop, pause and resume never open a closed app.
- Dashboard: rooms from HA areas with a short summary each (temperature, humidity, lights on, open doors and windows), devices with their state, favorites, scene and script buttons, and weather with today's low and high.
- Controls from the TV through a fixed list of actions per device kind: lights (on/off, brightness, warm/neutral/daylight), switches, fans, covers, locks, climate target and mode, media play/pause, next and volume, scenes, scripts and buttons. Only devices the TV shows can be controlled; there is no general service call. Unlocking and garage doors or gates are off unless allowed, and each one needs a confirmation on the TV.
- Cameras: a grid of stills and one live HLS stream at a time. `foyer.show_camera` brings the TV over from another app (a doorbell, say), shows the camera with up to three device buttons, then goes back to the app that was on screen. `foyer.notify` shows a notice in the open app.
- Pairing: the integration finds Roku TVs with Foyer installed (SSDP) and opens the app on the TV, which only asks you to confirm a four-digit number. Manual pairing with a single-use eight-digit code, valid ten minutes after you activate it, still works. Either way the TV gets a scoped, revocable token, never an administrator token.

## Install

With [HACS](https://hacs.xyz/):

1. In HACS, open the menu in the top right corner, choose **Custom repositories**, add `https://github.com/KilotonLabs/foyer-ha` with the type **Integration**.
2. Search HACS for Foyer, download it, then restart Home Assistant.

Without HACS: download `foyer-integration.zip` from the [latest release](https://github.com/KilotonLabs/foyer-ha/releases/latest) and extract it into Home Assistant's configuration directory so `custom_components/foyer/manifest.json` sits beside `configuration.yaml`. Do not overwrite an existing `foyer` folder without reviewing it. Run the configuration check, then restart Home Assistant.

Then pair a TV:

1. Install Foyer on the Roku from the Roku Channel Store and open it once.
2. Home Assistant needs a local network URL (Settings > System > Network).
3. A discovered TV shows up in Settings > Devices & services. Choose what it shows, then confirm on the TV that it shows the same number as Home Assistant.
4. Without discovery: Add Foyer, choose what it shows, note the pairing code and submit the second form to activate it. On the TV, open the app, choose Connect, enter the server origin (for example `http://homeassistant.local:8123`) and all eight digits.

Settings per TV: home name, areas (every supported entity in them, minus hidden, disabled and diagnostic ones), extra entities, entities to hide, cameras, favorites, scenes and scripts, flagged helpers, weather, and two switches: allow control (default on) and allow unlocking and garage doors or gates (default off). Profiles from 0.4 are migrated: their checks become extra entities and favorites, their scripts become scene buttons, and control is allowed with guarded actions off.

Each entry is one TV. A successful replacement pairing revokes the prior token. Revoking a TV in Configure also forgets its address, so nothing can open the app on it until it pairs again. To wake a closed app, the Roku needs **Settings > System > Advanced system settings > Control by mobile apps** set to **Enabled** (on older Roku OS: **Network access** set to **Default**); the media player's `tv_wake` attribute shows `blocked` otherwise.

## Dashboard renderer

`renderer/` keeps up to four views open in headless Chromium, hides HA's toolbar and sidebar, and returns 1920x1080 JPEG frames. Only Home Assistant talks to it; TVs fetch frames through the integration's token-checked API. It stores nothing and serves no pages.

- **Any machine with Node.js 20+ and Chrome/Chromium:** `cd renderer && npm ci && HA_URL=http://homeassistant.local:8123 HA_TOKEN=... RENDERER_SECRET=... node server.mjs`. Copy the renderer URL it prints into Foyer's settings. Clearing the URL there turns dashboard casts off; leaving the secret field empty keeps the saved secret as long as the URL is unchanged.

Use a long-lived token from a dedicated **non-admin** user; the renderer sees whatever that user sees.

A TV is read from across the room, so the page is laid out at `1 / scale` of the frame size and rendered `scale` times larger (default 1.5, range 1 to 3; `SCALE` env var). Raise it for bigger text, set 1 for the old pixel-for-pixel layout.

```yaml
action: foyer.show_view
target:
  entity_id: media_player.living_room
data:
  dashboard_path: dashboard-tv
  view_path: cameras
  refresh_seconds: 3
```

Doorbell automation:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.doorbell
    to: "on"
actions:
  - action: foyer.show_camera
    target:
      entity_id: media_player.living_room
    data:
      camera: camera.front_door
      title: Someone is at the front door
      message: Doorbell rang
      timeout: 30
      actions:
        - lock.front_door
        - light.porch
```

## Access and data

The status API returns, for the selected areas and entities only, names, compact state values, icons, change times, the control options described above, camera names and any cast queued for that TV. It never forwards arbitrary attributes. Camera stills and streams are served only for the TV's selected cameras. Cast media URLs are short-lived signed HA URLs. The integration stores hashes of pairing codes and TV tokens, plus each TV's IP address and Roku app ID so it can wake the app. To hand the screen back after a camera takeover it asks the TV which app is in front and remembers it in memory. No analytics or telemetry.

Limits: 10 pairing attempts/minute per network address and 60 in total; 30 rejected TV tokens/minute per address and 240 in total; 60 status/stream requests/minute per TV; 40 controls per 10 s per TV, guarded actions at most one per 3 s; 1200 camera stills/minute per TV; 40 dashboard frames/minute per TV; queued casts expire after 60 s; request IDs remembered for ten minutes in memory.

## Development

HA needs Linux. On Windows the real-HA tests run in WSL (`tools/test_ha.mjs`, venv at `~/hc-ha-venv` with `pytest-homeassistant-custom-component`). From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm.cmd run verify
```

`verify` runs the unit tests (loopback HTTP server, fake HA adapters), the real-HA tests in `tests_ha/`, a syntax check of the renderer, then builds `dist/foyer-integration.zip` with `dist/checksums.json` and validates the packaging boundary. `tools/create_strings.py` regenerates `strings.json` and `translations/en.json`, which must stay identical.

## License

Copyright (c) 2026 Kiloton Labs. Licensed under the GNU General Public License v3.0 only; see [LICENSE](LICENSE). For other licensing terms, open an issue. Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md).
