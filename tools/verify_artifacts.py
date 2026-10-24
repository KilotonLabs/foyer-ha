"""Check the integration ZIP boundary and integrity without installing anywhere."""
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile

root = Path(__file__).resolve().parents[1]
dist = root / "dist"
checksums = json.loads((dist / "checksums.json").read_text())
name = "foyer-integration.zip"
allowed = {"custom_components/foyer/" + item for item in
           ["__init__.py", "api.py", "core.py", "config_flow.py", "media.py", "media_player.py", "playback.py", "services.py", "services.yaml",
            "manifest.json", "strings.json", "translations/en.json", "LICENSE"]}
path = dist / name
assert hashlib.sha256(path.read_bytes()).hexdigest() == checksums[name], name
with ZipFile(path) as archive:
    assert archive.testzip() is None, name
    members = [item for item in archive.infolist() if not item.is_dir()]
    assert len(members) == len(allowed) and {item.filename for item in members} == allowed, name
    for item in members:
        assert not PurePosixPath(item.filename).is_absolute() and ".." not in PurePosixPath(item.filename).parts
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
print(f"Verified {name}: {len(allowed)} allowed files, CRCs, SHA-256, syntax, and packaging boundary.")
