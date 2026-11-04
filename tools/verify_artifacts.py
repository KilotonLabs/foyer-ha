"""Check the integration ZIP boundary and integrity without installing anywhere."""
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
from zipfile import ZipFile

root = Path(__file__).resolve().parents[1]
dist = root / "dist"
checksums = json.loads((dist / "checksums.json").read_text())
name = "foyer-integration.zip"
allowed = {"custom_components/foyer/" + item for item in
           ["__init__.py", "api.py", "core.py", "config_flow.py", "diagnostics.py", "media.py", "media_player.py", "playback.py", "services.py", "services.yaml",
            "manifest.json", "strings.json", "translations/en.json", "LICENSE", "brand/icon.png", "brand/icon@2x.png"]}
ICON_SIZES = {"brand/icon.png": 256, "brand/icon@2x.png": 512}


def check_png(filename, data):
    """A plain square PNG of the size HA's brands endpoint expects, carrying no text chunks (no paths or names)."""
    assert data[:8] == bytes.fromhex("89504e470d0a1a0a"), filename
    at, chunks = 8, []
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        chunks.append((kind, data[at + 8:at + 8 + length]))
        at += 12 + length
    assert chunks[0][0] == b"IHDR" and chunks[-1][0] == b"IEND", filename
    width, height = struct.unpack(">II", chunks[0][1][:8])
    side = ICON_SIZES[filename.removeprefix("custom_components/foyer/")]
    assert (width, height) == (side, side), (filename, width, height)
    assert not {kind for kind, _ in chunks} & {b"tEXt", b"iTXt", b"zTXt", b"eXIf"}, filename
path = dist / name
assert hashlib.sha256(path.read_bytes()).hexdigest() == checksums[name], name
with ZipFile(path) as archive:
    assert archive.testzip() is None, name
    members = [item for item in archive.infolist() if not item.is_dir()]
    assert len(members) == len(allowed) and {item.filename for item in members} == allowed, name
    for item in members:
        assert not PurePosixPath(item.filename).is_absolute() and ".." not in PurePosixPath(item.filename).parts
        if item.filename.endswith(".png"):
            check_png(item.filename, archive.read(item))
            continue
        content = archive.read(item).decode("utf-8")
        assert not re.search(r"[A-Za-z]:[/\\]Users[/\\]|private_[A-Za-z0-9_-]+|BEGIN .*PRIVATE KEY", content), item.filename
        assert Path.home().name.lower() not in content.lower(), item.filename
        if item.filename.endswith(".py"):
            ast.parse(content)
        elif item.filename.endswith(".json"):
            json.loads(content)
        elif item.filename.endswith("LICENSE"):
            assert "GNU GENERAL PUBLIC LICENSE" in content and "Version 3" in content, item.filename
    assert archive.read("custom_components/foyer/strings.json") == archive.read("custom_components/foyer/translations/en.json")
print(f"Verified {name}: {len(allowed)} allowed files, CRCs, SHA-256, syntax, icon sizes, and packaging boundary.")
