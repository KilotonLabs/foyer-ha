"""Draw the Home Assistant brand icons (custom_components/foyer/brand/icon.png 256 px, icon@2x.png 512 px) from the
Foyer mark on the Roku channel icon: cream house, amber arched door, near-black ground. Geometry is the channel
icon's, measured in its 540x405 pixels; drawn at 8x and reduced so the edges stay clean."""
from pathlib import Path

from PIL import Image, ImageDraw

GROUND, CREAM, AMBER = (26, 25, 23), (242, 238, 229), (255, 178, 36)
APEX, EAVES, BASE, LEFT, RIGHT = (269.5, 81), 161, 250, 181, 358  # house, channel-icon pixels
DOOR_LEFT, DOOR_RIGHT, DOOR_TOP = 253, 286, 181  # the door stands on the base line
MARK_HEIGHT = 0.58  # share of the tile the house fills, top to bottom
CORNER = 0.11  # tile corner radius as a share of its side

out = Path(__file__).resolve().parents[1] / "custom_components" / "foyer" / "brand"
out.mkdir(exist_ok=True)


def draw(size):
    big = size * 8
    scale = big * MARK_HEIGHT / (BASE - APEX[1])
    ox = big / 2 - (LEFT + RIGHT) / 2 * scale
    oy = big / 2 - (APEX[1] + BASE) / 2 * scale
    at = lambda x, y: (ox + x * scale, oy + y * scale)  # noqa: E731
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=big * CORNER, fill=GROUND)
    d.polygon([at(*APEX), at(RIGHT, EAVES), at(RIGHT, BASE), at(LEFT, BASE), at(LEFT, EAVES)], fill=CREAM)
    r = (DOOR_RIGHT - DOOR_LEFT) / 2
    d.rounded_rectangle([*at(DOOR_LEFT, DOOR_TOP), *at(DOOR_RIGHT, BASE + r)], radius=r * scale, fill=AMBER)
    d.rectangle([*at(DOOR_LEFT - 1, BASE), *at(DOOR_RIGHT + 1, BASE + r + 1)], fill=GROUND)  # cut the door square at the base
    return im.resize((size, size), Image.LANCZOS)


for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
    draw(size).save(out / name, optimize=True)
    print("wrote", (out / name).relative_to(out.parents[2]), size)
