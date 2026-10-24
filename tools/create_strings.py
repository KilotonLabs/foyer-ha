"""Generate HA's English translation from one source of truth."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "custom_components" / "foyer"
labels = {"title": "Home name shown on the TV", "areas": "Areas (every supported device in them is shown)",
          "entities": "Extra entities (up to 100, for example ones without an area)",
          "exclude": "Hide these entities", "cameras": "Cameras (up to 16)",
          "favorites": "Favorites on the Home screen (up to 12)", "scenes": "Scenes and scripts (up to 12)",
          "attention_when_on": "Also flag these helpers when ON", "weather": "Weather (optional)",
          "allow_control": "Allow controlling devices from the TV",
          "allow_guarded": "Also allow unlocking and opening garage doors and gates",
          "library": "Media library: browse and play My media on this TV",
          "convert": "Prepare videos for TV: convert files whose video Roku can't play",
          "renderer_url": "Dashboard renderer URL (optional, for casting dashboards)",
          "renderer_secret": "Dashboard renderer secret (required with a renderer)"}
descriptions = {"allow_control": "Off makes the TV read-only. Only lights, switches, fans, covers, locks, climate, media, scenes, scripts and buttons it shows can be controlled.",
                "allow_guarded": "Each unlock or garage action still needs a confirmation on the TV.",
                "cameras": "Anyone in front of the TV can see these cameras.",
                "library": "Shows Home Assistant's My media (Media > My media) on the TV. Add videos, music and pictures there, or upload them in that same panel.",
                "convert": "Most files play as they are or are repackaged on the fly at little cost. For the rest (old AVI and WMV video, for example) the TV offers to convert the file. Home Assistant converts one file at a time in the background at low priority, and saves the copy in a hidden .foyer folder beside the original. On a small box that takes about as long as the video, and needs free space about the size of the file. Not recommended when your media is on the SD card."}
description = "Choose what this TV shows. Areas bring in their lights, switches, covers, locks, climate, media players and the main sensors; hidden, disabled and diagnostic entities stay out. Casting media and cameras needs no extra setup. To cast dashboards and custom cards, run the Foyer renderer and enter its address."
errors = {"invalid_title": "Enter a name between 1 and 40 characters.", "area_count": "Select no more than 50 areas.",
          "entity_count": "Select no more than 100 extra entities.", "exclude_count": "Hide no more than 300 entities.",
          "camera_count": "Select no more than 16 cameras.", "favorite_count": "Select no more than 12 favorites.",
          "scene_count": "Select no more than 12 scenes and scripts.", "invalid_area": "Select each area once.",
          "invalid_entity": "Select supported entities without duplicates.", "invalid_camera": "Select each camera once.",
          "invalid_scene": "Select scenes or scripts, each once.",
          "invalid_attention": "Select up to 100 binary sensors or toggle helpers to flag.",
          "invalid_weather": "Select a weather entity or leave it empty.", "invalid_settings": "Check the permission switches.",
          "nothing_selected": "Select at least one area, entity or camera.",
          "invalid_renderer": "Enter the renderer address shown in its log, such as http://192.168.1.20:8099, and the same secret (16 to 128 characters) configured in the renderer.",
          "launch_failed": "Could not open Foyer on the TV. On the TV, allow Settings > System > Advanced system settings > Control by mobile apps > Network access, then submit to try again.",
          "no_url": "Home Assistant has no local network URL. Set one in Settings > System > Network, then submit to try again."}
pair = {"title": "Pair your TV", "description": "Your code is **{code}**. Write it down, then click Submit to activate it. Within 10 minutes, enter your Home Assistant server address and this code in the Roku app. Each entry pairs one TV. Generating a new code does not revoke the existing TV until the new pairing succeeds."}
strings = {"title": "Foyer", "config": {
    "flow_title": "{name}",
    "step": {
        "user": {"title": "Foyer", "description": description, "data": labels, "data_description": descriptions},
        "pair": pair,
        "discovery_confirm": {"title": "Set up Foyer on {name}?", "description": "Foyer is installed on the Roku TV **{name}**. Next, choose what it shows. Home Assistant then opens the app on the TV to pair it, with no code to type."},
        "link": {"title": "Open Foyer on {name}", "description": "Submit to open Foyer on the TV and pair it."}},
    "progress": {"wait_for_tv": "Foyer is open on **{name}**. On the TV, check that it shows the number **{match}**, then choose Connect. This waits up to 10 minutes."},
    "error": errors,
    "abort": {"already_configured": "This TV is already set up.", "already_in_progress": "This TV is already being set up.", "app_not_installed": "Install the Foyer app on this Roku TV first.",
              "cannot_connect": "Could not reach the Roku TV on the local network.",
              "pair_timeout": "The TV did not pair within 10 minutes. Set it up again from the discovered device."}},
    "options": {"step": {
        "init": {"title": "Manage this TV", "menu_options": {"settings": "Change what the TV shows", "pair": "Generate a pairing code", "revoke": "Revoke TV access"}},
        "settings": {"title": "What the TV shows", "description": description, "data": labels, "data_description": {
            **descriptions, "renderer_url": "Leave empty to stop casting dashboards.",
            "renderer_secret": "Leave empty to keep the saved secret for an unchanged renderer URL."}},
        "pair": pair, "revoke": {"title": "Revoke this TV?", "description": "Submit to immediately invalidate this entry's TV token and any pending pairing code. No device states or scripts are changed. Pair again to restore access."}}, "error": errors},
    "services": {
        "show_view": {"name": "Show dashboard", "description": "Casts a dashboard view, including custom cards, to the TV. Needs the Foyer renderer.",
            "fields": {"dashboard_path": {"name": "Dashboard path", "description": "Dashboard URL path, such as lovelace or dashboard-tv."},
                       "view_path": {"name": "View path", "description": "View URL path or index. Leave empty for the first view."},
                       "refresh_seconds": {"name": "Refresh interval", "description": "Seconds between pictures of the dashboard."}}},
        "show_camera": {"name": "Show camera", "description": "Brings the TV to Foyer and shows a camera full screen, for example when the doorbell rings.",
            "fields": {"camera": {"name": "Camera", "description": "A camera selected for this TV."},
                       "title": {"name": "Title", "description": "Defaults to the camera name."},
                       "message": {"name": "Message", "description": "A second line under the title."},
                       "timeout": {"name": "Timeout", "description": "Seconds before the TV goes back. 0 keeps the camera on screen."},
                       "actions": {"name": "Actions", "description": "Up to 3 devices shown on this TV to offer as buttons, such as a door lock or porch light."},
                       "return_to_app": {"name": "Return to app", "description": "Afterwards, go back to the app that was on screen."}}},
        "notify": {"name": "Notify", "description": "Shows a notice on the TV while Foyer is open.",
            "fields": {"title": {"name": "Title", "description": "Notice title."},
                       "message": {"name": "Message", "description": "A second line under the title."},
                       "camera": {"name": "Camera", "description": "Optional camera selected for this TV to show with the notice."},
                       "timeout": {"name": "Timeout", "description": "Seconds the notice stays. 0 keeps it until dismissed."},
                       "launch": {"name": "Open the app", "description": "Open Foyer on the TV if it is closed."}}}}}
(root / "translations").mkdir(exist_ok=True)
for path in [root / "strings.json", root / "translations" / "en.json"]:
    path.write_text(json.dumps(strings, indent=2) + "\n", encoding="utf-8")
print("Generated matching integration strings and English translation.")
