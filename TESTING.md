# Home Assistant side: test checklist

Local gate: `npm.cmd run verify`. It does not replace a device pass; the Roku checklist is in the `foyer-roku` repository.

## Rules for any live pass

- Use a throwaway Home Assistant, never a household instance, unless the owner has approved the exact install and restart.
- Start with allow control off. Then allow control with one harmless light or switch. Turn on guarded actions only with a test lock or a simulated cover, never a real door.
- Never print or record tokens, pairing codes or the renderer secret. The TV needs a temporary pairing code, never an administrator token.
- On unexpected device action or repeated HA errors: stop, revoke the test TV in Configure, and inspect traces before retrying anything.

## Setup and pairing

1. Verify package contents and checksum, and that no conflicting `foyer` folder exists.
2. After the approved restart, confirm the integration loads and the forms render without repeated errors.
3. Create a profile, cancel it once, then create it. Check the area and camera defaults, count limits, "nothing selected", helper validation, code visibility and activation.
4. Pair, including a leading-zero code. Wrong, expired and reused codes must fail without changing any entity.
5. Change checks in HA; the next refresh reflects only the allowlist, with no reload.
6. Revoke: the TV loses access, and a cast to it fails with "This TV is not paired..." without opening the app. Pair again: replacement pairing invalidates the previous token.
7. Discovery: with the app installed, the TV appears in Devices & services. Confirm, choose settings; the app opens on the TV with the same number HA shows; Connect pairs it with no typing. With "Control by mobile apps" limited, the form says so and Submit retries. Without the app installed, no setup is offered. Closing the dialog before the TV connects leaves no working code.
8. Upgrade from 0.4: the existing entry migrates (checks become extras and favorites, scripts become scene buttons) and the paired TV keeps working after the TV app update.

## Casting

Record the media player's `tv_wake` attribute each time.

1. App open: play a local MP4 and an MP3. Starts in about two seconds, shows position, pauses and resumes, Back returns the entity to idle.
2. App closed, another app in front: the app launches and plays. Stop, pause and play from HA do not open it. With "Control by mobile apps" limited, expect `tv_wake: blocked`.
3. HLS camera through `media-source://camera/...` (cold start included) and an MJPEG-only camera: live video and a two-second still respectively.
4. TTS announcement (`tts.speak`, or `announce: true`) with the app closed, over a dashboard, over playing media and over paused media: the spoken text is the entity's title, and afterwards the TV returns to exactly what it showed before (paused media stays paused).
5. A photo from the media browser and a plain image link with `media_content_type: image` stay on screen with the entity `on`; a missing image shows "Could not show this picture" and the entity goes idle.
6. Renderer: `foyer.show_view` for a masonry view with a custom card and for a panel view. No toolbar, no flash between frames, state changes within the refresh interval. Stop the renderer: "Picture unavailable. Retrying..." within about 20 s, clears on recovery. Clearing the renderer URL in settings stops dashboard casts.
7. Soak: a dashboard cast for an hour or more; watch TV responsiveness and the renderer host's memory.
8. Revoke while casting: frames stop and no further casts arrive.

## Dashboard and controls

1. Areas: hidden, disabled and diagnostic entities stay out; excluded entities disappear; area summaries match the devices shown. A state change reaches the TV within one poll; nothing else changes the `rev`.
2. Allow control off: every control is refused. On: each kind does exactly its one action. A device that is not shown cannot be controlled, even by entity ID.
3. Guarded: unlock and garage open are refused with guarded actions off, refused without the TV's confirmation, and run once with both. Two within 3 s: the second is refused.
4. Scenes and scripts: one run produces one trace; "accepted" never claims completion; key repeat, an already-running script, a removed script and revoked access dispatch nothing extra. Never automatically retry a timed-out write.
5. Weather shows current conditions at once and today's low and high shortly after.

## Cameras and notices

1. Stills for every selected camera, none for others. Live: a streaming camera plays; a camera without a stream stays on refreshing stills.
2. `foyer.show_camera` from another app (and from HDMI input): the app opens with the camera; Back to that app or the timeout relaunches it. From the Home screen or from Foyer itself nothing is relaunched. Device buttons work and respect the guarded rules.
3. `foyer.notify` shows only while the app is open, unless `launch: true`.

## Release

Record versions, hashes from `dist/checksums.json`, results and bounded resource observations without secrets. A rebuild invalidates earlier acceptance until the affected checks are repeated. Complete an independent security review before distribution.
